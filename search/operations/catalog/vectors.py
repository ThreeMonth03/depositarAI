"""Bounded vector passes with host-owned, durable cleanup checkpoints.

An incomplete receipt blocks a new pass. An exited helper is not enough:
scheduler/token evidence, ordinary Docker removal and exact-ID absence must
all be verified before a checkpoint permits another pass. No live activation,
forced removal or automatic recovery of uncertain mutations is provided.
"""

from __future__ import annotations

import os
import re
import time
import uuid

from search.operations.catalog import (
    administration,
    backend,
    execution,
    results,
    reuse,
)
from search.operations.catalog.policy import CONTAINER_ID, SERVICES, RuntimeError
from search.operations.shared import private_io
from search.vector_journal import read_events, validate_all, validate_report


MAX_PASSES = 40
MAX_HISTORY = 128
HELPER_TIMEOUT = 2400
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


def _recipe(context):
    # The exact Fess image/config and sealed Ollama manifest also bind chunking
    # defaults and all model layer digests. Do not pretend to tune them here.
    from search.catalog.embedding import MODEL, MODEL_DIGEST

    options = context["compose"]["services"]["fess-search"]["environment"][
        "FESS_JAVA_OPTS"
    ]
    required = (
        "-Dfess.system.content_chunker.job.max_documents_per_run=256",
        "-Dfess.system.content_chunker.job.concurrency=2",
        "-Dfess.system.content_chunker.job.bulk_size=100",
    )
    if any(options.split().count(option) != 1 for option in required):
        raise RuntimeError("catalog_vector_profile_changed")
    return {
        **execution.catalog_identity(context),
        "embedding_model": MODEL,
        "embedding_manifest_sha256": MODEL_DIGEST,
        "dimensions": 768,
        "documents_per_pass": 256,
        "concurrency": 2,
        "bulk_size": 100,
        "work_seconds": 1200,
        "token_lifetime_minutes": 35,
    }


def _input_identity(context):
    """Read authenticated static inputs while the caller holds the catalog lock."""
    runtime, images = execution.require_runtime(context)
    image_record = execution.read_record(context["root"], "images.json")
    if (
        type(image_record) is not dict
        or image_record.get("images") != images
        or execution.digest_record(image_record) != runtime["images_sha256"]
    ):
        raise RuntimeError("catalog_vector_inputs_changed")
    return {
        "source_revision": context["source_revision"],
        "plan": context["plan"],
        "recipe": _recipe(context),
        "images": image_record,
        "admin_image": administration._admin_image(context, read_only=True),
        "runtime": runtime,
    }


def inputs(config, plan, *, repo_root, runner=None):
    """Report static-input identity without model hashing or catalog writes."""
    context = execution.load_context(config, plan, repo_root=repo_root, runner=runner)
    with execution.operation_lock(context):
        identity = _input_identity(context)
        return {
            "inputs_sha256": execution.digest_record(identity),
            "source_revision": context["source_revision"],
            "catalog_revision": context["plan"]["revision"],
            "bundle_id": context["bundle"].bundle_id,
            "manifest_sha256": context["plan"]["manifest_sha256"],
            "read_only": True,
        }


def _journal_identity(identity):
    return {
        "schema_version": 1,
        **{
            key: identity[key]
            for key in ("run_id", "bundle_id", "manifest_sha256", "recipe_sha256")
        },
    }


def _validate_result(result, identity, count):
    validate_report(result)
    if (
        any(
            result[key] != identity[key]
            for key in ("run_id", "recipe_sha256", "bundle_id", "manifest_sha256")
        )
        or result["document_count"] != count
    ):
        raise RuntimeError("catalog_vector_result_values_invalid")


def _clean_events(directory, identity, result, expected_inventory):
    events = read_events(
        directory / "events", expected_identity=_journal_identity(identity)
    )
    validate_all(
        events, expected_identity=_journal_identity(identity), require_result=True
    )
    if events[-1]["payload"] != result:
        raise RuntimeError("catalog_vector_journal_result_mismatch")
    for event in events:
        if event["event"] in {"before-state", "after-state"}:
            payload = event["payload"]
            inventory = tuple(
                sorted(
                    tuple(row)
                    for row in (*payload["done_entries"], *payload["pending_entries"])
                )
            )
            if inventory != expected_inventory:
                raise RuntimeError("catalog_vector_journal_inventory_changed")
    return events


def _host_record(directory, name):
    value = execution.read_record(directory, name)
    if (
        type(value) is not dict
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("active_changed") is not False
    ):
        raise RuntimeError("catalog_vector_host_receipt_invalid")
    return value


def _history(context, image, recipe, *, read_only=False):
    """Read complete cleanup chains; only the build path may create evidence."""
    if type(read_only) is not bool:
        raise RuntimeError("catalog_vector_readonly_invalid")
    root = context["root"] / "vector-operations"
    os.close(private_io.open_directory(root, create=not read_only))
    recipe_hash = execution.digest_record(recipe)
    if (root / "recipe.json").exists():
        if (
            execution.digest_record(execution.read_record(root, "recipe.json"))
            != recipe_hash
        ):
            raise RuntimeError("catalog_vector_recipe_changed")
    else:
        if read_only:
            raise RuntimeError("catalog_vector_recipe_evidence_missing")
        if list(root.iterdir()):
            raise RuntimeError("catalog_vector_orphan_evidence")
        execution.write_record(root, "recipe.json", recipe)
    runs = [path for path in root.iterdir() if path.name != "recipe.json"]
    if (len(runs) > MAX_HISTORY or not read_only and len(runs) == MAX_HISTORY) or any(
        not RUN_ID.fullmatch(path.name) for path in runs
    ):
        raise RuntimeError("catalog_vector_history_invalid_or_full")
    completed, checkpoint_before = {}, {}
    for directory in runs:
        os.close(private_io.open_directory(directory))
        names = {path.name for path in directory.iterdir()}
        if names != {"started.json", "container.json", "complete.json", "events"}:
            raise RuntimeError("catalog_vector_pass_requires_explicit_recovery")
        started = _host_record(directory, "started.json")
        ordinal = started.get("pass_number")
        expected = {
            **execution.catalog_identity(context),
            "operation": "vector-pass",
            "run_id": directory.name,
            "admin_image_id": image["image_id"],
            "recipe_sha256": recipe_hash,
            "pass_number": ordinal,
            "expected_before_state_sha256": started.get("expected_before_state_sha256"),
        }
        if (
            type(ordinal) is not int
            or not 1 <= ordinal <= MAX_HISTORY
            or ordinal in completed
            or started != {**expected, "status": "started"}
        ):
            raise RuntimeError("catalog_vector_started_receipt_changed")
        complete = _host_record(directory, "complete.json")
        container = _host_record(directory, "container.json")
        container_id = container.get("container_id")
        if (
            type(container_id) is not str
            or not CONTAINER_ID.fullmatch(container_id)
            or type(container.get("pass_number")) is not int
            or type(complete.get("pass_number")) is not int
            or complete.get("helper_removed_verified") is not True
            or container != {**expected, "container_id": container_id}
            or complete
            != {
                **expected,
                "status": "completed",
                "container_id": container_id,
                "helper_removed_verified": True,
                "result": complete.get("result"),
            }
        ):
            raise RuntimeError("catalog_vector_host_cleanup_receipt_changed")
        result = complete["result"]
        _validate_result(result, expected, context["plan"]["document_count"])
        _clean_events(
            directory, expected, result, context["bundle"].expected_inventory_entries
        )
        completed[ordinal] = result
        checkpoint_before[ordinal] = expected["expected_before_state_sha256"]
    if set(completed) != set(range(1, len(completed) + 1)):
        raise RuntimeError("catalog_vector_history_sequence_invalid")
    previous = None
    for ordinal in sorted(completed):
        result = completed[ordinal]
        expected_before = previous["after_state_sha256"] if previous else None
        if (
            checkpoint_before[ordinal] != expected_before
            or previous
            and result["before_state_sha256"] != expected_before
        ):
            raise RuntimeError("catalog_vector_progress_checkpoint_changed")
        previous = result
    return root, recipe_hash, len(completed), previous


def _complete_evidence(context, runtime):
    """Shared journal gate; its caller separately establishes runtime authority."""
    runtime_sha256 = execution.digest_record(runtime)
    if administration._admin_ids(context):
        raise RuntimeError("catalog_vector_existing_helper_requires_recovery")
    image = administration._admin_image(context, read_only=True)
    _, pending, _ = administration._admin_records(context, read_only=True, image=image)
    if pending:
        raise RuntimeError("catalog_vector_import_requires_reconcile")
    _, recipe_hash, ordinal, previous = _history(
        context, image, _recipe(context), read_only=True
    )
    count = context["plan"]["document_count"]
    if (
        previous is None
        or ordinal < 1
        or type(count) is not int
        or previous["status"] != "complete-clean"
        or type(previous["done_count"]) is not int
        or previous["done_count"] != count
        or type(previous["pending_count"]) is not int
        or previous["pending_count"] != 0
    ):
        raise RuntimeError("catalog_vector_completion_evidence_missing")
    if administration._admin_ids(context):
        raise RuntimeError("catalog_vector_existing_helper_requires_recovery")
    return {
        **execution.catalog_identity(context),
        "status": "vectors-complete",
        "recipe_sha256": recipe_hash,
        "completed_passes": ordinal,
        "document_count": count,
        "done_count": previous["done_count"],
        "pending_count": previous["pending_count"],
        "after_state_sha256": previous["after_state_sha256"],
        "admin_image_id": image["image_id"],
        "runtime_sha256": runtime_sha256,
        "runtime_verified": False,
        "host_cleanup_verified": True,
        "read_only": True,
    }


def _runtime_evidence(context):
    """Authenticate sealed runtime bytes without claiming its current topology.

    A release observer must independently validate the actual, unfiltered
    network under its exact baseline or sealed client-lease policy. Reading a
    receipt cannot confer that permission and never ignores an attached peer.
    """
    root = context["root"]
    runtime = execution.read_record(root, "runtime.json")
    images = execution.load_images(context)
    overlay = {
        "services": {name: {"image": item["image_id"]} for name, item in images.items()}
    }
    ids = runtime.get("container_ids") if type(runtime) is dict else None
    network = runtime.get("network_id") if type(runtime) is dict else None
    if (
        type(ids) is not dict
        or set(ids) != SERVICES
        or any(
            type(value) is not str or not CONTAINER_ID.fullmatch(value)
            for value in ids.values()
        )
        or len(set(ids.values())) != len(SERVICES)
        or type(network) is not str
        or not CONTAINER_ID.fullmatch(network)
    ):
        raise RuntimeError("catalog_vector_runtime_receipt_changed")
    expected = {
        **execution.catalog_identity(context),
        "status": "running",
        "container_ids": ids,
        "network_id": network,
        "image_ids": {name: item["image_id"] for name, item in images.items()},
        "image_lock_sha256": execution.digest_record(overlay),
        "images_sha256": execution.digest_record(
            execution.read_record(root, "images.json")
        ),
    }
    if (
        type(runtime.get("schema_version")) is not int
        or runtime.get("active_changed") is not False
        or private_io.canonical(runtime) != private_io.canonical(expected)
        or private_io.canonical(execution.read_record(root, "image-lock.json"))
        != private_io.canonical(overlay)
    ):
        raise RuntimeError("catalog_vector_runtime_receipt_changed")
    return runtime, images


def read_complete_evidence(context):
    """Read sealed image/runtime/import/vector cleanup authority, without HTTP.

    The caller holds the catalog lock. Actual Docker topology must be verified
    independently before using this evidence, including after an API attaches.
    No runtime dictionaries supplied by a caller become sealed authority here.
    """
    before = _runtime_evidence(context)
    gate = _complete_evidence(context, before[0])
    if execution.digest_record(before) != execution.digest_record(
        _runtime_evidence(context)
    ):
        raise RuntimeError("catalog_vector_runtime_changed")
    return gate, before[0], before[1]


def verify_complete(context):
    """Observe existing complete-clean evidence; never build or resume vectors.

    The caller has loaded the sealed context and holds its catalog operation
    lock. Runtime, existing image/import receipts and every vector journal are
    verified without obtaining admin HTTP credentials, creating a helper or
    token, or writing any files. Existing runtime verification still hashes its
    sealed security material. This is a host cleanup proof, not a fresh OpenSearch
    vector scan: the query provisioner must separately compare actual vectors
    with ``after_state_sha256`` before allowing query access.
    """
    before = execution.require_runtime(context)
    before_sha256 = execution.digest_record(before)
    gate = _complete_evidence(context, before[0])
    after = execution.require_runtime(context)
    if before_sha256 != execution.digest_record(after):
        raise RuntimeError("catalog_vector_runtime_changed")
    return {**gate, "runtime_verified": True}


def _one_pass(context, image, secret, root, recipe_hash, ordinal, previous):
    run_id = uuid.uuid4().hex
    directory = root / run_id
    os.close(private_io.open_directory(directory, create=True))
    journal = directory / "events"
    os.close(private_io.open_directory(journal, create=True))
    identity = {
        **execution.catalog_identity(context),
        "operation": "vector-pass",
        "run_id": run_id,
        "admin_image_id": image["image_id"],
        "recipe_sha256": recipe_hash,
        "pass_number": ordinal,
        "expected_before_state_sha256": previous["after_state_sha256"]
        if previous
        else None,
    }
    execution.write_record(directory, "started.json", {**identity, "status": "started"})
    container_id = None
    try:
        runtime, _ = execution.require_runtime(context)
        fess_id = runtime["container_ids"]["fess-search"]
        container_id = backend._command(
            context["runner"],
            administration._admin_create(
                context, image, identity, fess_id, secret, journal=journal
            ),
        ).strip()
        if not CONTAINER_ID.fullmatch(container_id):
            raise RuntimeError("catalog_vector_helper_identity_invalid")
        execution.write_record(
            directory, "container.json", {**identity, "container_id": container_id}
        )
        before = administration._inspect_admin(
            context, image, identity, container_id, fess_id, secret, journal=journal
        )
        if before["State"].get("Status") != "created":
            raise RuntimeError("catalog_vector_helper_already_started")
        execution.verify_image(
            context, image["reference"], image["image_id"], image["labels"]
        )
        outcome = context["runner"](
            ["docker", "start", "--attach", container_id], timeout=HELPER_TIMEOUT
        )
        after = administration._inspect_admin(
            context, image, identity, container_id, fess_id, secret, journal=journal
        )
        if (
            after["State"].get("Status") != "exited"
            or outcome.returncode != 0
            or after["State"].get("ExitCode") != 0
            or outcome.stderr
            or type(outcome.stdout) is not str
        ):
            raise RuntimeError("catalog_vector_helper_failed")
        result = results.parse_record(outcome.stdout.encode("utf-8"))
        _validate_result(result, identity, context["plan"]["document_count"])
        _clean_events(
            directory, identity, result, context["bundle"].expected_inventory_entries
        )
        if previous and result["before_state_sha256"] != previous["after_state_sha256"]:
            raise RuntimeError("catalog_vector_progress_checkpoint_changed")
        execution.require_runtime(context)
        backend._command(context["runner"], ["docker", "rm", container_id])
        remaining = backend._command(
            context["runner"],
            [
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--quiet",
                "--filter",
                "id=" + container_id,
            ],
        )
        if remaining.strip():
            raise RuntimeError("catalog_vector_helper_removal_unverified")
        execution.write_record(
            directory,
            "complete.json",
            {
                **identity,
                "status": "completed",
                "container_id": container_id,
                "helper_removed_verified": True,
                "result": result,
            },
        )
        return result
    except Exception as error:
        execution.write_record(
            directory,
            "failure.json",
            {
                **identity,
                "status": "quarantined",
                "error_type": type(error).__name__,
            },
        )
        # Preserve the helper and all evidence. Never infer Fess job cleanup
        # from a process exit, remove it forcibly, or create another helper.
        raise


def build(
    config,
    plan,
    *,
    repo_root,
    runner=None,
    opensearch_factory=None,
    max_passes=16,
    wall_seconds=7200,
    clock=time.monotonic,
    expected_inputs_sha256=None,
):
    """Resume only proven clean passes, bounded by count and invocation time."""
    if (
        type(max_passes) is not int
        or not 1 <= max_passes <= MAX_PASSES
        or type(wall_seconds) is not int
        or not HELPER_TIMEOUT <= wall_seconds <= 36000
    ):
        raise RuntimeError("catalog_vector_budget_invalid")
    if expected_inputs_sha256 is not None and (
        type(expected_inputs_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_inputs_sha256) is None
    ):
        raise RuntimeError("catalog_vector_expected_inputs_invalid")
    context = execution.load_context(config, plan, repo_root=repo_root, runner=runner)
    expires = clock() + wall_seconds
    with execution.operation_lock(context):
        image = None
        if expected_inputs_sha256 is None:
            execution.require_runtime(context)
        else:
            identity = _input_identity(context)
            if execution.digest_record(identity) != expected_inputs_sha256:
                raise RuntimeError("catalog_vector_inputs_changed")
            image = identity["admin_image"]
        from search.operations.doctor import _embedding_model

        _embedding_model(context["data"] / "ollama/models", deep=True)
        if administration._admin_ids(context):
            raise RuntimeError("catalog_vector_existing_helper_requires_recovery")
        _, pending, _ = administration._admin_records(context)
        if pending:
            raise RuntimeError("catalog_vector_import_requires_reconcile")
        if image is None:
            image = administration._admin_image(context)
        root, recipe_hash, ordinal, previous = _history(
            context, image, _recipe(context)
        )
        if previous and previous["status"] == "complete-clean":
            # Reverify actual runtime and every complete journal, without a new
            # helper, administrator token or no-op vector pass. Query provision
            # still scans the real index before granting query access.
            return {**verify_complete(context), "passes_this_invocation": 0}
        secret = administration._secret(context)
        result = None
        reuse_report = None
        passes = 0
        while (
            passes < max_passes
            and ordinal < MAX_HISTORY
            and clock() + HELPER_TIMEOUT <= expires
        ):
            if ordinal == 0:
                runtime, images = execution.require_runtime(context)
                options = (
                    {}
                    if opensearch_factory is None
                    else {"opensearch_factory": opensearch_factory}
                )
                reuse_report = reuse.seed(context, runtime, images, **options)
                if clock() + HELPER_TIMEOUT > expires:
                    break
            result = _one_pass(
                context, image, secret, root, recipe_hash, ordinal + 1, previous
            )
            ordinal += 1
            passes += 1
            previous = result
            if result["status"] == "complete-clean":
                break
        return {
            **execution.catalog_identity(context),
            "status": "vectors-complete"
            if result and result["status"] == "complete-clean"
            else "checkpoint",
            "recipe_sha256": recipe_hash,
            "passes_this_invocation": passes,
            "completed_passes": ordinal,
            "document_count": plan["document_count"],
            "done_count": previous["done_count"] if previous else None,
            "pending_count": previous["pending_count"] if previous else None,
            "runtime_verified": True,
            "host_cleanup_verified": bool(result),
            "vector_reuse": reuse_report,
        }
