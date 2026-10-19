"""Non-retrying, token-bounded administration of a sealed isolated catalog."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import uuid

from search.operations.catalog import backend, execution, results
from search.operations.catalog.policy import (
    CONTAINER_ID,
    IMAGE_ID,
    LABEL_PREFIX,
    RuntimeError,
    admin_arguments,
    catalog_bundle_target,
    validate_admin,
)
from search.operations.shared import private_io
from search.catalog.errors import valid_failure_record

MAX_OPERATIONS = 128
ADMIN_BASE = "python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d"
ADMIN_FILES = (
    "deploy/docker/importer/Dockerfile",
    "deploy/docker/importer/Dockerfile.dockerignore",
    "search/__init__.py",
    "search/serialization.py",
    "search/catalog_admin.py",
    "search/vector_journal.py",
    "search/operations/__init__.py",
    "search/operations/shared/__init__.py",
    "search/operations/shared/private_io.py",
    "search/catalog/__init__.py",
    "search/catalog/errors.py",
    "search/catalog/policy.py",
    "search/catalog/codec.py",
    "search/catalog/transport.py",
    "search/catalog/session.py",
    "search/catalog/admin.py",
    "search/catalog/bundle.py",
    "search/catalog/inventory.py",
    "search/catalog/importer.py",
    "search/catalog/journal.py",
    "search/catalog/vectors.py",
)


def _admin_failure_diagnostic(outcome, state):
    """Read only a closed failed-helper record after native identity proof."""
    if (
        state.get("Status") != "exited"
        or type(state.get("ExitCode")) is not int
        or type(outcome.returncode) is not int
        or outcome.returncode not in {3, 4}
        or state["ExitCode"] != outcome.returncode
        or type(outcome.stdout) is not str
        or outcome.stdout != ""
        or type(outcome.stderr) is not str
        or not 1 <= len(outcome.stderr) <= 4096
    ):
        return None
    try:
        raw = outcome.stderr.encode("utf-8", "strict")
        if len(raw) > 4096:
            return None
        value = results.parse_record(raw)
    except (UnicodeError, results.ResultError):
        return None
    if not valid_failure_record(value) or outcome.returncode != (
        4 if value["possibly_polluted"] else 3
    ):
        return None
    return value


def _admin_image(context, *, read_only=False):
    """Verify the existing image, or build it only in the normal admin workflow."""
    if type(read_only) is not bool:
        raise RuntimeError("catalog_admin_readonly_invalid")
    path = context["root"] / "admin-image.json"
    expected_sources = {
        name: hashlib.sha256(execution.sealed_source(context, name)).hexdigest()
        for name in ADMIN_FILES
    }
    if path.exists():
        entry = execution.read_record(context["root"], path.name)
    else:
        if read_only:
            raise RuntimeError("catalog_admin_image_evidence_missing")
        entry = execution.build_image(
            context,
            "catalog-admin",
            ADMIN_FILES,
            "Dockerfile",
            {"SEARCH_CATALOG_ADMIN_BASE_IMAGE": ADMIN_BASE},
            network="none",
        )
        execution.write_record(context["root"], path.name, entry)
    if (
        type(entry) is not dict
        or set(entry) != {"image_id", "reference", "labels", "sources"}
        or entry["reference"] != context["plan"]["project"] + "-catalog-admin:1"
        or entry["sources"] != expected_sources
        or entry["labels"]
        != execution.image_labels(
            context, "catalog-admin", expected_sources, ADMIN_BASE
        )
    ):
        raise RuntimeError("catalog_admin_image_receipt_changed")
    execution.verify_image(
        context, entry["reference"], entry["image_id"], entry["labels"]
    )
    return entry


def _admin_ids(context):
    raw = backend._command(
        context["runner"],
        [
            "docker",
            "ps",
            "--all",
            "--no-trunc",
            "--quiet",
            "--filter",
            "label=" + LABEL_PREFIX + "admin-project=" + context["plan"]["project"],
        ],
    )
    ids = raw.split()
    if len(ids) != len(set(ids)) or any(
        CONTAINER_ID.fullmatch(value) is None for value in ids
    ):
        raise RuntimeError("catalog_admin_inventory_invalid")
    return ids


def _secret(context):
    directory = context["data"] / "secrets"
    descriptor = private_io.open_directory(directory)
    try:
        raw = private_io.read_file(
            descriptor, "fess_initial_admin_password", mode=0o600, max_bytes=65
        )
        if re.fullmatch(rb"[0-9a-f]{64}\n", raw) is None:
            raise RuntimeError("catalog_admin_secret_invalid")
        digest = hashlib.sha256(raw).digest()
        if context.get("_secret_digest", digest) != digest:
            raise RuntimeError("catalog_admin_secret_changed")
        context["_secret_digest"] = digest
        private_io.verify_directory(descriptor, directory)
    finally:
        os.close(descriptor)
    return directory / "fess_initial_admin_password"


def _admin_create(context, image, identity, fess_id, secret, *, journal=None):
    uid, gid = os.geteuid(), os.getegid()
    bundle_target = catalog_bundle_target(context["bundle"].bundle_id)
    for path in (Path(context["plan"]["bundle_path"]), secret):
        if any(character in str(path) for character in (",", "\n", "\r")):
            raise RuntimeError("catalog_admin_mount_path_invalid")
    command = [
        "docker",
        "create",
        "--pull",
        "never",
        "--name",
        context["plan"]["project"] + "-admin-" + identity["run_id"],
        "--label",
        LABEL_PREFIX + "admin-project=" + context["plan"]["project"],
        "--label",
        LABEL_PREFIX + "admin-run=" + identity["run_id"],
        "--label",
        LABEL_PREFIX + "admin-operation=" + identity["operation"],
        "--user",
        f"{uid}:{gid}",
        "--runtime",
        "runc",
        "--env",
        "NVIDIA_VISIBLE_DEVICES=void",
        "--env",
        "CUDA_VISIBLE_DEVICES=",
        "--network",
        "container:" + fess_id,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--init",
        "--pids-limit",
        "64",
        "--memory",
        "1g",
        "--memory-swap",
        "1g",
        "--cpus",
        "1",
        "--ulimit",
        "nofile=1024:1024",
        "--restart",
        "no",
        "--log-driver",
        "none",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size=64m,uid={uid},gid={gid},mode=700",
        "--mount",
        "type=bind,src="
        + str(Path(context["plan"]["bundle_path"]))
        + ",dst="
        + bundle_target
        + ",readonly",
        "--mount",
        "type=bind,src="
        + str(secret)
        + ",dst=/run/secrets/fess_initial_admin_password,readonly",
    ]
    if journal is not None:
        command.extend(
            ["--mount", "type=bind,src=" + str(journal) + ",dst=/vector-journal"]
        )
    return [
        *command,
        image["image_id"],
        *admin_arguments(context, identity, journal=journal),
    ]


def _inspect_admin(
    context, image, identity, container_id, fess_id, secret, *, journal=None
):
    if _secret(context) != secret:
        raise RuntimeError("catalog_admin_secret_changed")
    rows = execution.run_json(context["runner"], ["docker", "inspect", container_id])
    if (
        type(rows) is not list
        or len(rows) != 1
        or type(rows[0]) is not dict
        or rows[0].get("Id") != container_id
    ):
        raise RuntimeError("catalog_admin_identity_changed")
    validate_admin(
        context,
        image,
        identity,
        rows[0],
        fess_id,
        secret,
        stopped=True,
        journal=journal,
    )
    return rows[0]


def _admin_records(context, *, read_only=False, image=None):
    """Validate the journal without creating evidence in read-only mode.

    A verified image may be supplied by the completion verifier to avoid a
    second image inspection. Historical failed imports are accepted only when
    an existing completed reconciliation explicitly resolves them.
    """
    if type(read_only) is not bool:
        raise RuntimeError("catalog_admin_readonly_invalid")
    directory = context["root"] / "admin-operations"
    os.close(private_io.open_directory(directory, create=not read_only))
    if read_only:
        image = image if image is not None else _admin_image(context, read_only=True)
    entries = list(directory.iterdir())
    names = {path.name for path in entries}
    if any(
        name != "reconciled.json"
        and re.fullmatch(
            r"[0-9a-f]{32}-(started|container|complete|failure)\.json", name
        )
        is None
        for name in names
    ):
        raise RuntimeError("catalog_admin_unknown_evidence")
    starts = [path for path in entries if path.name.endswith("-started.json")]
    start_ids = {path.name[:32] for path in starts}
    if any(name != "reconciled.json" and name[:32] not in start_ids for name in names):
        raise RuntimeError("catalog_admin_orphan_evidence")
    if len(starts) > MAX_OPERATIONS or not read_only and len(starts) == MAX_OPERATIONS:
        raise RuntimeError("catalog_admin_retention_limit")
    pending = set()
    complete_records = {}
    for path in starts:
        run_id = path.name[:32]
        started = execution.read_record(directory, path.name)
        _validate_admin_receipt(context, started, run_id, status="started")
        if read_only and started["admin_image_id"] != image["image_id"]:
            raise RuntimeError("catalog_admin_evidence_image_changed")
        fixed = {key: value for key, value in started.items() if key != "status"}
        container = None
        container_name = run_id + "-container.json"
        if container_name in names:
            container = execution.read_record(directory, container_name)
            if type(container) is not dict:
                raise RuntimeError("catalog_admin_container_evidence_changed")
            identifier = container.get("container_id")
            if (
                type(identifier) is not str
                or CONTAINER_ID.fullmatch(identifier) is None
                or read_only
                and (
                    type(container.get("schema_version")) is not int
                    or container.get("active_changed") is not False
                )
                or container != {**fixed, "container_id": identifier}
            ):
                raise RuntimeError("catalog_admin_container_evidence_changed")
        failure_name = run_id + "-failure.json"
        if failure_name in names:
            failure = execution.read_record(directory, failure_name)
            if type(failure) is not dict:
                raise RuntimeError("catalog_admin_failure_evidence_changed")
            error_type = failure.get("error_type")
            if (
                type(error_type) is not str
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", error_type) is None
                or read_only
                and (
                    type(failure.get("schema_version")) is not int
                    or failure.get("active_changed") is not False
                )
                or failure
                != {**fixed, "status": "quarantined", "error_type": error_type}
            ):
                raise RuntimeError("catalog_admin_failure_evidence_changed")
        complete_name = run_id + "-complete.json"
        if complete_name in names:
            if read_only and failure_name in names:
                raise RuntimeError("catalog_admin_operation_requires_recovery")
            complete = execution.read_record(directory, complete_name)
            _validate_admin_receipt(context, complete, run_id, status="completed")
            if (
                container is None
                or complete["container_id"] != container["container_id"]
            ):
                raise RuntimeError("catalog_admin_container_evidence_changed")
            if any(
                complete[key] != value
                for key, value in started.items()
                if key != "status"
            ):
                raise RuntimeError("catalog_admin_evidence_identity_changed")
            complete_records[run_id] = complete
        elif started["operation"] == "import":
            pending.add(run_id)
        elif read_only:
            raise RuntimeError("catalog_admin_operation_requires_recovery")
    resolved_runs = set()
    if (directory / "reconciled.json").exists():
        reconciled = execution.read_record(directory, "reconciled.json")
        if type(reconciled) is not dict:
            raise RuntimeError("catalog_reconciliation_identity_changed")
        resolved = reconciled.get("resolved_import_runs")
        run_id = reconciled.get("run_id")
        if (
            type(resolved) is not list
            or any(
                type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None
                for value in resolved
            )
            or resolved != sorted(set(resolved))
            or read_only
            and (
                type(reconciled.get("schema_version")) is not int
                or reconciled.get("active_changed") is not False
                or not set(resolved) <= start_ids
            )
            or run_id not in complete_records
            or reconciled
            != {**complete_records[run_id], "resolved_import_runs": resolved}
            or reconciled.get("operation") != "reconcile"
        ):
            raise RuntimeError("catalog_reconciliation_identity_changed")
        resolved_runs = set(resolved)
        pending -= resolved_runs
    if read_only and not complete_records:
        raise RuntimeError("catalog_admin_completion_evidence_missing")
    return directory, pending, resolved_runs


def _validate_admin_receipt(context, receipt, run_id, *, status):
    fixed = execution.catalog_identity(context)
    keys = set(fixed) | {"operation", "run_id", "admin_image_id", "status"}
    if status == "completed":
        keys |= {"container_id", "result"}
    if (
        type(receipt) is not dict
        or set(receipt) != keys
        or any(receipt.get(key) != value for key, value in fixed.items())
        or type(receipt["schema_version"]) is not int
        or receipt.get("active_changed") is not False
        or receipt["run_id"] != run_id
        or receipt["operation"] not in {"import", "reconcile"}
        or receipt["status"] != status
        or type(receipt["admin_image_id"]) is not str
        or IMAGE_ID.fullmatch(receipt["admin_image_id"]) is None
    ):
        raise RuntimeError("catalog_admin_evidence_identity_changed")
    if status == "completed":
        if (
            type(receipt["container_id"]) is not str
            or CONTAINER_ID.fullmatch(receipt["container_id"]) is None
            or type(receipt["result"]) is not dict
        ):
            raise RuntimeError("catalog_admin_evidence_identity_changed")
        results.validate_import(
            receipt["result"],
            context["bundle"].bundle_id,
            context["plan"]["manifest_sha256"],
            operation=receipt["operation"],
        )
        if receipt["result"]["document_count"] != context["plan"]["document_count"]:
            raise RuntimeError("catalog_admin_document_count_changed")


def admin(config, plan, *, repo_root, operation, runner=None):
    """One bounded import or read-only reconcile, never retry an uncertain write.

    Empty/partial/foreign inventory is not accepted as a completed catalog by
    reconcile. A timeout with a still-running helper requires separate explicit
    operator recovery; it is not killed or silently rerun here.
    """
    if operation not in {"import", "reconcile"}:
        raise RuntimeError("catalog_admin_operation_not_supported")
    context = execution.load_context(config, plan, repo_root=repo_root, runner=runner)
    runner = context["runner"]
    with execution.operation_lock(context):
        runtime, _images = execution.require_runtime(context)
        if _admin_ids(context):
            raise RuntimeError("catalog_admin_helper_requires_explicit_recovery")
        directory, pending, resolved_runs = _admin_records(context)
        if operation == "import" and pending:
            raise RuntimeError("catalog_import_requires_readonly_reconcile")
        image = _admin_image(context)
        secret = _secret(context)
        identity = {
            **execution.catalog_identity(context),
            "operation": operation,
            "run_id": uuid.uuid4().hex,
            "admin_image_id": image["image_id"],
        }
        run_id = identity["run_id"]
        execution.write_record(
            directory, run_id + "-started.json", {**identity, "status": "started"}
        )
        container_id = None
        try:
            container_id = backend._command(
                runner,
                _admin_create(
                    context,
                    image,
                    identity,
                    runtime["container_ids"]["fess-search"],
                    secret,
                ),
            ).strip()
            if CONTAINER_ID.fullmatch(container_id) is None:
                raise RuntimeError("catalog_admin_create_identity_invalid")
            fess_id = runtime["container_ids"]["fess-search"]
            execution.write_record(
                directory,
                run_id + "-container.json",
                {**identity, "container_id": container_id},
            )
            before = _inspect_admin(
                context, image, identity, container_id, fess_id, secret
            )
            if before["State"].get("Status") != "created":
                raise RuntimeError("catalog_admin_already_started")
            execution.require_runtime(context)
            execution.verify_image(
                context, image["reference"], image["image_id"], image["labels"]
            )
            outcome = runner(
                ["docker", "start", "--attach", container_id],
                timeout=840 if operation == "import" else 420,
            )
            after = _inspect_admin(
                context, image, identity, container_id, fess_id, secret
            )
            if (
                after["State"].get("Status") != "exited"
                or outcome.returncode != 0
                or after["State"].get("ExitCode") != 0
                or outcome.stderr
                or type(outcome.stdout) is not str
            ):
                diagnostic = _admin_failure_diagnostic(outcome, after["State"])
                error = RuntimeError(
                    "catalog_admin_diagnostic_failed"
                    if diagnostic is not None
                    else "catalog_admin_operation_failed"
                )
                if diagnostic is not None:
                    error.catalog_diagnostic = diagnostic
                raise error
            result = results.parse_record(outcome.stdout.encode("utf-8"))
            results.validate_import(
                result,
                context["bundle"].bundle_id,
                plan["manifest_sha256"],
                operation=operation,
            )
            if result["document_count"] != plan["document_count"]:
                raise RuntimeError("catalog_admin_document_count_changed")
            execution.verify_image(
                context, image["reference"], image["image_id"], image["labels"]
            )
            execution.require_runtime(context)
            receipt = {
                **identity,
                "status": "completed",
                "container_id": container_id,
                "result": result,
            }
            execution.write_record(directory, run_id + "-complete.json", receipt)
            if operation == "reconcile":
                execution.write_record(
                    directory,
                    "reconciled.json",
                    {
                        **receipt,
                        "resolved_import_runs": sorted(pending | resolved_runs),
                    },
                    replace=True,
                )
            return receipt
        except Exception as error:
            execution.write_record(
                directory,
                run_id + "-failure.json",
                {
                    **identity,
                    "status": "quarantined",
                    "error_type": type(error).__name__,
                },
            )
            raise
        finally:
            if container_id is not None and CONTAINER_ID.fullmatch(container_id):
                # Never force-remove a running or identity/resource-mismatched helper.
                # Failure evidence remains immutable even after an exited helper is removed.
                try:
                    _inspect_admin(
                        context,
                        image,
                        identity,
                        container_id,
                        runtime["container_ids"]["fess-search"],
                        secret,
                    )
                    backend._command(runner, ["docker", "rm", container_id])
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
