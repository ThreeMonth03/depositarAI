"""Prepare and resume bounded resource profiling against one sealed catalog."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from search.catalog import resources
from search.catalog.errors import BundleError
from search.operations.documents import controller, extractor, sets, source
from search.operations.shared import private_io
from search.serialization import strict_json


MAX_PLAN_BYTES = 8 * 1024**2
MAX_ATTEMPTS = 3
MAX_OUTCOME_BYTES = 4096
CHECKPOINT_RESERVE = 16 * 1024**2
PROFILE_STATUSES = frozenset({"ok", "empty_text", "unsupported_format", "parse_error"})
ERROR_CODES = frozenset(
    {
        "document_batch_selection_invalid",
        "document_batch_plan_invalid",
        "document_batch_plan_changed",
        "document_batch_outcome_invalid",
        "document_batch_profile_invalid",
        "document_batch_tool_changed",
        "document_batch_options_invalid",
        "document_batch_insufficient_space",
        "document_batch_helper_active",
        "document_batch_state_invalid",
    }
)


class BatchError(ValueError):
    """A fixed private-state diagnostic without paths or source contents."""


def _source_root(path):
    return Path(path) if path is not None else Path(__file__).resolve().parents[3]


def _read(parent, name, maximum):
    try:
        raw = private_io.read_file(parent, name, mode=0o600, max_bytes=maximum)
        value = strict_json(raw)
        if private_io.canonical(value) != raw:
            raise ValueError
        return value
    except FileNotFoundError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise BatchError("document_batch_state_invalid") from None


@contextmanager
def _state(path, *, create, read_only=False):
    path = controller._external_root(path)
    try:
        with controller._state(path, create=create, read_only=read_only) as descriptor:
            yield path, descriptor
    except (OSError, ValueError) as error:
        if isinstance(
            error,
            (
                BatchError,
                controller.ProfileError,
                sets.ProfileSetError,
                extractor.ExtractorError,
                source.SourceError,
            ),
        ):
            raise
        raise BatchError("document_batch_state_invalid") from None


def _selection_file(path):
    path = Path(path)
    parent = controller._external_root(path.parent)
    descriptor = private_io.open_directory(parent)
    try:
        raw = private_io.read_file(
            descriptor, path.name, mode=0o600, max_bytes=MAX_PLAN_BYTES
        )
        values = strict_json(raw)
        private_io.verify_directory(descriptor, parent)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise BatchError("document_batch_selection_invalid") from None
    finally:
        os.close(descriptor)
    if (
        type(values) is not list
        or not 1 <= len(values) <= sets.MAX_SELECTED
        or any(type(v) is not str or not controller.UUID.fullmatch(v) for v in values)
        or len(values) != len(set(values))
    ):
        raise BatchError("document_batch_selection_invalid")
    return values


def _manifest(bundle_path, expected):
    descriptor = private_io.open_directory(bundle_path, mode=0o500)
    try:
        raw = private_io.read_file(
            descriptor, "bundle-manifest.json", mode=0o400, max_bytes=64 * 1024
        )
        if hashlib.sha256(raw).hexdigest() != expected:
            raise BatchError("document_batch_plan_changed")
        return strict_json(raw)
    finally:
        os.close(descriptor)


def _entries(selected):
    rows = [
        {
            "dataset_id": dataset_id,
            "resource_id": resource["id"],
            "format": resource["format"].strip().upper(),
            "url_sha256": hashlib.sha256(resource["url"].encode("utf-8")).hexdigest(),
        }
        for dataset_id, resource in selected
    ]
    rows.sort(key=lambda row: (row["dataset_id"], row["resource_id"]))
    return rows


def _validate_plan(plan):
    try:
        if (
            type(plan) is not dict
            or set(plan)
            != {
                "schema_version",
                "catalog_manifest_sha256",
                "source_sha256",
                "extractor_source_sha256",
                "policy",
                "resources",
            }
            or type(plan["schema_version"]) is not int
            or plan["schema_version"] != 1
            or any(
                type(plan[key]) is not str or not controller.SHA256.fullmatch(plan[key])
                for key in (
                    "catalog_manifest_sha256",
                    "source_sha256",
                    "extractor_source_sha256",
                )
            )
            or type(plan["policy"]) is not dict
            or set(plan["policy"]) != {"encoding", "header_policy"}
            or plan["policy"]["encoding"] not in controller.ENCODINGS
            or plan["policy"]["header_policy"] not in controller.HEADER_POLICIES
            or type(plan["resources"]) is not list
            or not 1 <= len(plan["resources"]) <= sets.MAX_SELECTED
        ):
            raise ValueError
        pairs, resource_ids, counts = [], set(), Counter()
        for row in plan["resources"]:
            if (
                type(row) is not dict
                or set(row) != {"dataset_id", "resource_id", "format", "url_sha256"}
                or any(
                    type(row[key]) is not str or not controller.UUID.fullmatch(row[key])
                    for key in ("dataset_id", "resource_id")
                )
                or row["format"] not in {"CSV", "PDF"}
                or type(row["url_sha256"]) is not str
                or not controller.SHA256.fullmatch(row["url_sha256"])
                or row["resource_id"] in resource_ids
            ):
                raise ValueError
            resource_ids.add(row["resource_id"])
            pairs.append((row["dataset_id"], row["resource_id"]))
            counts[row["dataset_id"]] += 1
        if pairs != sorted(pairs) or max(counts.values()) > resources.MAX_RESOURCES:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise BatchError("document_batch_plan_invalid") from None
    return plan


def _plan(descriptor, expected):
    if type(expected) is not str or not controller.SHA256.fullmatch(expected):
        raise BatchError("document_batch_plan_invalid")
    value = _validate_plan(_read(descriptor, "plan.json", MAX_PLAN_BYTES))
    if controller.digest(value) != expected:
        raise BatchError("document_batch_plan_changed")
    return value


def _outcome(descriptor, entry, plan_sha):
    try:
        row = _read(descriptor, entry["resource_id"] + ".json", MAX_OUTCOME_BYTES)
    except FileNotFoundError:
        return None
    base = {
        "schema_version",
        "plan_sha256",
        "dataset_id",
        "resource_id",
        "attempt",
        "status",
        "helper_name",
    }
    try:
        status = row["status"]
        fields = (
            {"profile_sha256", "acquisition_cache_hit", "extraction_cache_hit"}
            if status in PROFILE_STATUSES
            else {"code"}
            if status == "failed"
            else set()
        )
        if (
            type(row) is not dict
            or set(row) != base | fields
            or type(row["schema_version"]) is not int
            or row["schema_version"] != 1
            or row["plan_sha256"] != plan_sha
            or any(row[key] != entry[key] for key in ("dataset_id", "resource_id"))
            or type(row["attempt"]) is not int
            or not 1 <= row["attempt"] <= MAX_ATTEMPTS
            or status not in PROFILE_STATUSES | {"running", "failed"}
            or type(row["helper_name"]) is not str
            or not re.fullmatch(r"depositarai-extract-[0-9a-f]{32}", row["helper_name"])
        ):
            raise ValueError
        if status in PROFILE_STATUSES and (
            type(row["profile_sha256"]) is not str
            or not controller.SHA256.fullmatch(row["profile_sha256"])
            or type(row["acquisition_cache_hit"]) is not bool
            or type(row["extraction_cache_hit"]) is not bool
        ):
            raise ValueError
        if status == "failed" and (
            type(row["code"]) is not str
            or not re.fullmatch(r"document_[a-z_]{1,80}", row["code"])
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise BatchError("document_batch_outcome_invalid") from None
    return row


def _verify_profile(profiles, row, entry, plan, tool):
    from search.operations.documents.enrichment import _profile_fields

    try:
        profile = _read(
            profiles, row["profile_sha256"] + ".json", controller.MAX_RECORD_BYTES
        )
        if controller.digest(profile) != row["profile_sha256"]:
            raise ValueError
        _profile_fields(profile, plan["catalog_manifest_sha256"])
        if (
            any(
                profile["source"][key] != entry[key]
                for key in ("dataset_id", "resource_id", "url_sha256")
            )
            or profile["policy"] != plan["policy"]
            or profile["extractor"]
            != {
                "image_id": tool["image_id"],
                "source_sha256": plan["extractor_source_sha256"],
            }
            or profile["result"]["status"] != row["status"]
        ):
            raise ValueError
        result = extractor.validate_report(
            profile["result"],
            expected_format=entry["format"],
            header_policy=plan["policy"]["header_policy"],
        )
        if result["input_sha256"] != profile["source"]["content_sha256"]:
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        raise BatchError("document_batch_profile_invalid") from None


def _tool(descriptor, plan):
    try:
        value = _read(descriptor, "tool.json", controller.MAX_RECORD_BYTES)
    except FileNotFoundError:
        return None
    if (
        type(value) is not dict
        or set(value) != {"image_id", "source_sha256", "base_image"}
        or value.get("source_sha256") != plan["extractor_source_sha256"]
        or value.get("base_image") != extractor.FESS_IMAGE
        or type(value.get("image_id")) is not str
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_id"])
    ):
        raise BatchError("document_batch_tool_changed")
    return value


def _snapshot(state_root, descriptor, plan, plan_sha):
    outcomes = private_io.open_child_directory(descriptor, "outcomes")
    profiles = private_io.open_child_directory(descriptor, "profiles")
    try:
        tool = _tool(descriptor, plan)
        rows = []
        for entry in plan["resources"]:
            row = _outcome(outcomes, entry, plan_sha)
            if row is not None and row["status"] in PROFILE_STATUSES:
                if tool is None:
                    raise BatchError("document_batch_tool_changed")
                _verify_profile(profiles, row, entry, plan, tool)
            rows.append(row)
        return rows, tool
    finally:
        try:
            private_io.verify_directory(profiles, state_root / "profiles")
            private_io.verify_directory(outcomes, state_root / "outcomes")
        finally:
            os.close(profiles)
            os.close(outcomes)


def _report(plan, plan_sha, rows):
    pending = sum(row is None or row["status"] == "running" for row in rows)
    failures = sum(
        row is not None and row["status"] in {"failed", "parse_error"} for row in rows
    )
    return {
        "schema_version": 1,
        "status": "checkpoint" if pending else "partial" if failures else "completed",
        "plan_sha256": plan_sha,
        "catalog_manifest_sha256": plan["catalog_manifest_sha256"],
        "selected_count": len(rows),
        "pending_count": pending,
        "profile_count": sum(
            row is not None and row["status"] in PROFILE_STATUSES for row in rows
        ),
        "failed_count": failures,
        "retryable_count": sum(
            row is not None
            and row["status"] == "failed"
            and row["attempt"] < MAX_ATTEMPTS
            for row in rows
        ),
    }


def prepare(
    bundle_path,
    manifest_sha256,
    state_root,
    resource_id_file,
    *,
    encoding="utf-8",
    header_policy="first_record",
    source_root=None,
):
    """Freeze explicit metadata selection and policy without downloads or Docker."""
    if (
        type(encoding) is not str
        or encoding not in controller.ENCODINGS
        or type(header_policy) is not str
        or header_policy not in controller.HEADER_POLICIES
    ):
        raise BatchError("document_batch_options_invalid")
    identifiers = _selection_file(resource_id_file)
    try:
        selected = controller._selected_resources(
            bundle_path,
            manifest_sha256,
            identifiers,
            max_resources=sets.MAX_SELECTED,
            metadata_only=True,
        )
    except BundleError:
        raise BatchError("document_batch_plan_invalid") from None
    plan = _validate_plan(
        {
            "schema_version": 1,
            "catalog_manifest_sha256": manifest_sha256,
            "source_sha256": _manifest(bundle_path, manifest_sha256)["artifacts"][
                "source.jsonl.gz"
            ]["sha256"],
            "extractor_source_sha256": extractor.source_identity(
                _source_root(source_root)
            ),
            "policy": {"encoding": encoding, "header_policy": header_policy},
            "resources": _entries(selected),
        }
    )
    state_root = controller._external_root(state_root)
    bundle_path = Path(bundle_path)
    if (
        state_root == bundle_path
        or state_root in bundle_path.parents
        or bundle_path in state_root.parents
    ):
        raise controller.ProfileError("document_state_overlaps_catalog")
    created = True
    with _state(state_root, create=True) as (_, descriptor):
        try:
            private_io.write_json(
                descriptor, "plan.json", plan, max_bytes=MAX_PLAN_BYTES
            )
        except FileExistsError:
            created = False
            if _read(descriptor, "plan.json", MAX_PLAN_BYTES) != plan:
                raise BatchError("document_batch_plan_changed") from None
        for name in ("acquisition", "extractions", "profiles", "outcomes"):
            os.close(controller._child(descriptor, name))
        rows, _ = _snapshot(state_root, descriptor, plan, controller.digest(plan))
    report = _report(plan, controller.digest(plan), rows)
    if created:
        report["status"] = "prepared"
    return report


def _export_record(plan, rows):
    profiles, failures = [], []
    for row in rows:
        fields = {key: row[key] for key in ("dataset_id", "resource_id")}
        if row["status"] == "failed":
            failures.append({**fields, "code": row["code"]})
        else:
            profiles.append({**fields, "profile_sha256": row["profile_sha256"]})
    return sets.validate(
        {
            "schema_version": 1,
            "catalog_manifest_sha256": plan["catalog_manifest_sha256"],
            "source_sha256": plan["source_sha256"],
            "profiles": profiles,
            "failures": failures,
        }
    )


def _export(descriptor, plan, rows):
    value = _export_record(plan, rows)
    return sets.publish(
        descriptor,
        value["catalog_manifest_sha256"],
        value["source_sha256"],
        value["profiles"],
        value["failures"],
    )


def run(
    bundle_path,
    state_root,
    expected_plan_sha256,
    *,
    max_resources=20,
    retry_failed=False,
    source_root=None,
):
    """Process one bounded sequential pass, persisting each result before the next."""
    if (
        type(max_resources) is not int
        or not 1 <= max_resources <= controller.MAX_RESOURCES
        or type(retry_failed) is not bool
    ):
        raise BatchError("document_batch_options_invalid")
    with _state(state_root, create=False) as (state_root, descriptor):
        plan = _plan(descriptor, expected_plan_sha256)
        try:
            selected = controller._selected_resources(
                bundle_path,
                plan["catalog_manifest_sha256"],
                [row["resource_id"] for row in plan["resources"]],
                max_resources=sets.MAX_SELECTED,
                metadata_only=True,
            )
        except BundleError:
            raise BatchError("document_batch_plan_changed") from None
        if (
            _entries(selected) != plan["resources"]
            or _manifest(bundle_path, plan["catalog_manifest_sha256"])["artifacts"][
                "source.jsonl.gz"
            ]["sha256"]
            != plan["source_sha256"]
        ):
            raise BatchError("document_batch_plan_changed")
        selected = {
            resource["id"]: (dataset_id, resource) for dataset_id, resource in selected
        }
        if (
            extractor.source_identity(_source_root(source_root))
            != plan["extractor_source_sha256"]
        ):
            raise BatchError("document_batch_tool_changed")
        rows, tool = _snapshot(Path(state_root), descriptor, plan, expected_plan_sha256)
        pending = [
            i for i, row in enumerate(rows) if row is None or row["status"] == "running"
        ]
        retries = (
            [
                i
                for i, row in enumerate(rows)
                if row is not None
                and row["status"] == "failed"
                and row["attempt"] < MAX_ATTEMPTS
            ]
            if retry_failed
            else []
        )
        work = (pending + retries)[:max_resources]
        if work:
            required = (
                len(work) * (source.MAX_FILE_BYTES + 2 * controller.MAX_RECORD_BYTES)
                + CHECKPOINT_RESERVE
            )
            if (
                shutil.disk_usage(state_root).free < required
                or shutil.disk_usage(tempfile.gettempdir()).free
                < source.MAX_FILE_BYTES + CHECKPOINT_RESERVE
            ):
                raise BatchError("document_batch_insufficient_space")
            for index in work:
                prior = rows[index]
                if (
                    prior is not None
                    and prior["status"] == "running"
                    and extractor.helper_exists(prior["helper_name"])
                ):
                    raise BatchError("document_batch_helper_active")
            image = extractor.build_image(_source_root(source_root))
            if (
                tool is not None
                and image != tool
                or image["source_sha256"] != plan["extractor_source_sha256"]
            ):
                raise BatchError("document_batch_tool_changed")
            private_io.verify_directory(descriptor, state_root)
            controller._publish(descriptor, "tool.json", image)
            extractions = private_io.open_child_directory(descriptor, "extractions")
            profiles = private_io.open_child_directory(descriptor, "profiles")
            outcomes = private_io.open_child_directory(descriptor, "outcomes")
            try:
                for index in work:
                    entry, prior = plan["resources"][index], rows[index]
                    row = {
                        "schema_version": 1,
                        "plan_sha256": expected_plan_sha256,
                        "dataset_id": entry["dataset_id"],
                        "resource_id": entry["resource_id"],
                        "attempt": (
                            prior["attempt"]
                            if prior["status"] == "running"
                            else prior["attempt"] + 1
                        )
                        if prior
                        else 1,
                        "status": "running",
                        "helper_name": "depositarai-extract-" + uuid.uuid4().hex,
                    }
                    name = entry["resource_id"] + ".json"
                    private_io.write_json(
                        outcomes, name, row, replace=True, max_bytes=MAX_OUTCOME_BYTES
                    )
                    try:
                        dataset_id, resource = selected[entry["resource_id"]]
                        result = controller.process(
                            dataset_id,
                            resource,
                            plan["catalog_manifest_sha256"],
                            state_root,
                            image,
                            plan["policy"]["encoding"],
                            plan["policy"]["header_policy"],
                            extractions,
                            profiles,
                            container_name=row["helper_name"],
                        )
                        row.update(result)
                    except (source.SourceError, extractor.ExtractorError) as error:
                        if str(error) == "document_extractor_cleanup_failed":
                            raise
                        code = str(error)
                        row.update(
                            status="failed",
                            code=code
                            if re.fullmatch(r"document_[a-z_]{1,80}", code)
                            else "document_resource_failed",
                        )
                    for child, child_name in (
                        (extractions, "extractions"),
                        (profiles, "profiles"),
                        (outcomes, "outcomes"),
                    ):
                        private_io.verify_directory(child, state_root / child_name)
                    private_io.verify_directory(descriptor, state_root)
                    private_io.write_json(
                        outcomes, name, row, replace=True, max_bytes=MAX_OUTCOME_BYTES
                    )
                    rows[index] = row
            finally:
                for child in (outcomes, profiles, extractions):
                    os.close(child)
        report = _report(plan, expected_plan_sha256, rows)
        report["attempted_count"] = len(work)
        if not report["pending_count"]:
            report.update(_export(descriptor, plan, rows))
        return report


def status(state_root, expected_plan_sha256):
    """Read verified progress; never build, download, repair, or publish a receipt."""
    with _state(state_root, create=False, read_only=True) as (_, descriptor):
        plan = _plan(descriptor, expected_plan_sha256)
        rows, _ = _snapshot(Path(state_root), descriptor, plan, expected_plan_sha256)
        report = _report(plan, expected_plan_sha256, rows)
        if not report["pending_count"]:
            expected = _export_record(plan, rows)
            digest = controller.digest(expected)
            filename = "profile-set-" + digest + ".json"
            try:
                value, actual = sets.read(Path(state_root) / filename)
            except sets.ProfileSetError:
                try:
                    private_io.read_file(
                        descriptor, filename, mode=0o600, max_bytes=sets.MAX_SET_BYTES
                    )
                except FileNotFoundError:
                    report.update(status="checkpoint", export_pending=True)
                    return report
                raise
            if actual != digest or value != expected:
                raise BatchError("document_batch_state_invalid")
            report.update(profile_set_file=filename, profile_set_sha256=digest)
        return report
