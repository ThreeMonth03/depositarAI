"""Profile the explicit pilot selection with real acquisition and extraction."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time

from search.catalog.bundle import CatalogBundle
from search.operations.documents import cache, controller, extractor, source
from search.operations.shared import private_io
from search.serialization import strict_json


MAX_REPORT_BYTES = 1024 * 1024
SOURCE_ROOT = Path(__file__).resolve().parents[2]


def read_private_json(path):
    path = Path(path)
    parent = controller._external_root(path.parent)
    descriptor = private_io.open_directory(parent)
    try:
        value = strict_json(
            private_io.read_file(
                descriptor, path.name, mode=0o600, max_bytes=MAX_REPORT_BYTES
            )
        )
        private_io.verify_directory(descriptor, parent)
        return value
    finally:
        os.close(descriptor)


def write_report(root, name, value):
    root = controller._external_root(Path(root))
    descriptor = private_io.open_directory(root, create=True)
    try:
        private_io.write_json(
            descriptor, name, value, replace=True, max_bytes=MAX_REPORT_BYTES
        )
    finally:
        os.close(descriptor)


def error_record(error):
    code = str(error)
    return {
        "error_type": type(error).__name__,
        "error_code": code
        if re.fullmatch(r"document_[a-z_]{1,80}", code)
        else "pilot_stage_failed",
    }


def seed_verified_cache(seed_root, state_root, selection):
    """Copy verified cache records unchanged, including original acquisition times."""
    state_descriptor = private_io.open_directory(state_root, create=True)
    os.close(state_descriptor)
    copied = {"acquisition_receipts": [], "extraction_records": []}
    if seed_root is None:
        return copied
    allowed = {row["resource_id"] for row in selection["resources"]}
    content_hashes = set()
    for folder in ("acquisition", "extractions"):
        source_root, target_root = seed_root / folder, state_root / folder
        target_root.mkdir(mode=0o700)
        source_fd = private_io.open_directory(source_root)
        target_fd = private_io.open_directory(target_root)
        try:
            for name in sorted(os.listdir(source_fd)):
                if not name.endswith(".json"):
                    continue
                raw = private_io.read_file(
                    source_fd, name, mode=0o600, max_bytes=controller.MAX_RECORD_BYTES
                )
                value = json.loads(raw)
                if private_io.canonical(value) != raw:
                    raise ValueError("seed cache is not canonical")
                if folder == "acquisition":
                    identity = value.get("source", {})
                    if identity.get("resource_id") not in allowed:
                        continue
                    key = cache.key_for(identity)
                    if name != "resource-" + key + ".json":
                        raise ValueError("seed acquisition filename mismatch")
                    receipt = cache.load(source_fd, key, identity)
                    if receipt is None:
                        raise ValueError("seed acquisition receipt is missing")
                    body = private_io.read_file(
                        source_fd,
                        cache.body_name(receipt["sha256"]),
                        mode=0o600,
                        max_bytes=source.MAX_FILE_BYTES,
                    )
                    _copy_record(target_fd, cache.body_name(receipt["sha256"]), body)
                    _copy_record(target_fd, name, raw)
                    content_hashes.add(receipt["sha256"])
                    copied["acquisition_receipts"].append(
                        {
                            "resource_id": identity["resource_id"],
                            "content_sha256": receipt["sha256"],
                            "bytes": receipt["bytes"],
                            "acquired_at": receipt["acquired_at"],
                        }
                    )
                else:
                    identity = value.get("identity", {})
                    if identity.get("content_sha256") not in content_hashes:
                        continue
                    if (
                        set(value) != {"identity", "result"}
                        or name != controller.digest(identity) + ".json"
                    ):
                        raise ValueError("seed extraction identity mismatch")
                    result = extractor.validate_report(
                        value["result"],
                        expected_format=identity["expected_format"],
                        header_policy=identity["header_policy"],
                    )
                    if result["input_sha256"] != identity["content_sha256"]:
                        raise ValueError("seed extraction content mismatch")
                    _copy_record(target_fd, name, raw)
                    copied["extraction_records"].append(name)
            private_io.verify_directory(source_fd, source_root)
            private_io.verify_directory(target_fd, target_root)
        finally:
            os.close(source_fd)
            os.close(target_fd)
    return copied


def _copy_record(descriptor, name, raw):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    destination = os.open(name, flags, 0o600, dir_fd=descriptor)
    with os.fdopen(destination, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


class StageTrace:
    """Measure existing functions while retaining their actual IO and failures."""

    def __init__(self, selection, results_root):
        self.cases = {row["resource_id"]: row for row in selection["resources"]}
        self.results_root = results_root
        self.phase = "first"
        self.entries = []
        self.acquired_resources = {}
        self.original = source.acquire, controller._extraction, extractor.build_image

    def flush(self):
        write_report(
            self.results_root,
            "profile-timings.json",
            {"schema_version": 1, "phase": self.phase, "stages": self.entries},
        )

    @contextmanager
    def measure(self, stage, resource_id=None):
        entry = {"phase": self.phase, "stage": stage, "resource_id": resource_id}
        if resource_id is not None:
            entry["dataset_id"] = self.cases[resource_id]["dataset_id"]
        self.entries.append(entry)
        started = time.perf_counter()
        try:
            yield entry
        except Exception as error:
            entry.update(status="failed", **error_record(error))
            raise
        finally:
            entry["elapsed_seconds"] = time.perf_counter() - started
            self.flush()

    def acquire(self, root, resource, **options):
        identifier = resource["id"]
        with self.measure("acquisition", identifier) as entry:
            value = self.original[0](root, resource, **options)
            self.acquired_resources[value["cache_key"]] = identifier
            prior = self.cases[identifier].get("cached_material")
            entry.update(
                status="completed",
                bytes=value["bytes"],
                cache_hit=value["cache_hit"],
                content_sha256=value["sha256"],
                acquired_at=value["acquired_at"],
                prior_content_comparison=(
                    "not_available"
                    if prior is None
                    else "same"
                    if prior["sha256"] == value["sha256"]
                    else "changed"
                ),
            )
            return value

    def extraction(
        self, acquired, image, file_format, encoding, header_policy, descriptor
    ):
        identifier = self.acquired_resources[acquired["cache_key"]]
        with self.measure("extraction", identifier) as entry:
            result, hit = self.original[1](
                acquired, image, file_format, encoding, header_policy, descriptor
            )
            entry.update(
                status="completed",
                result_status=result["status"],
                cache_hit=hit,
                bytes=acquired["bytes"],
                content_sha256=result["input_sha256"],
                detected_mime=result["detected_mime"],
                column_count=len(result["columns"]),
                text_characters=len(result["text"]),
                text_truncated=result["text_truncated"],
                may_require_ocr=result["may_require_ocr"],
            )
            return result, hit

    def image(self, root):
        with self.measure("extractor_image") as entry:
            image = self.original[2](root)
            entry.update(status="completed", **image)
            return image

    def __enter__(self):
        source.acquire, controller._extraction, extractor.build_image = (
            self.acquire,
            self.extraction,
            self.image,
        )
        return self

    def __exit__(self, *_error):
        source.acquire, controller._extraction, extractor.build_image = self.original


def run(
    selection_path,
    plain_report_path,
    state_root,
    results_root,
    source_root,
    seed_cache=None,
):
    state_root = controller._external_root(Path(state_root))
    results_root = controller._external_root(Path(results_root))
    if state_root.exists():
        raise ValueError("use a new, dedicated pilot profile state directory")
    source_root = Path(source_root)
    if (
        source_root != SOURCE_ROOT
        or source_root.resolve() != source_root
        or Path(controller.__file__).resolve().parents[3] != source_root
    ):
        raise ValueError("source root must be this experiment's checkout")
    if seed_cache is not None:
        seed_cache = controller._external_root(Path(seed_cache))
        if seed_cache in state_root.parents or state_root in seed_cache.parents:
            raise ValueError("seed cache and pilot state must not overlap")
    selection = read_private_json(selection_path)
    plain = read_private_json(plain_report_path)
    if (
        type(selection) is not dict
        or type(selection.get("schema_version")) is not int
        or selection["schema_version"] != 1
        or selection.get("kind") != "selected-real-resource-offline-pilot"
        or type(selection.get("resources")) is not list
        or type(plain) is not dict
        or plain.get("status") != "ready"
        or type(plain.get("bundle_path")) is not str
        or type(plain.get("manifest_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", plain["manifest_sha256"]) is None
    ):
        raise ValueError("invalid pilot selection or baseline report")
    for row in selection["resources"]:
        if (
            type(row) is not dict
            or any(
                type(row.get(key)) is not str
                or controller.UUID.fullmatch(row[key]) is None
                for key in ("dataset_id", "resource_id")
            )
            or type(row.get("expected_format")) is not str
            or row["expected_format"] not in {"CSV", "PDF"}
            or type(row.get("source_url_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", row["source_url_sha256"]) is None
        ):
            raise ValueError("invalid selected resource identity")
        prior = row.get("cached_material")
        status = row.get("prior_status")
        if (
            prior is not None
            and (
                type(prior) is not dict
                or type(prior.get("sha256")) is not str
                or re.fullmatch(r"[0-9a-f]{64}", prior["sha256"]) is None
            )
        ) or (
            status is not None
            and (
                type(status) is not dict
                or type(status.get("status")) is not str
                or status["status"]
                not in {"ok", "empty_text", "unsupported_format", "parse_error"}
            )
        ):
            raise ValueError("invalid prior comparison evidence")
    bundle_path = controller._external_root(Path(plain["bundle_path"]))
    bundle = CatalogBundle.load(
        bundle_path, expected_manifest_sha256=plain["manifest_sha256"]
    )
    if (
        plain.get("bundle_id") != bundle.bundle_id
        or type(plain.get("document_count")) is not int
        or plain["document_count"] != len(bundle.documents)
    ):
        raise ValueError("baseline receipt and sealed bundle disagree")
    identifiers = [row["resource_id"] for row in selection["resources"]]
    if len(identifiers) != 20 or len(set(identifiers)) != 20:
        raise ValueError("the pilot requires exactly twenty distinct resources")
    if any(document["resource_context"] for document in bundle.documents) or {
        document["dataset_id"] for document in bundle.documents
    } != {row["dataset_id"] for row in selection["resources"]}:
        raise ValueError("the pilot baseline must contain only selected metadata")
    selected = controller._selected_resources(
        Path(plain["bundle_path"]), plain["manifest_sha256"], identifiers
    )
    for (dataset_id, resource), row in zip(selected, selection["resources"]):
        identity = source.source_identity(resource)
        if (
            dataset_id != row["dataset_id"]
            or identity["format"] != row["expected_format"]
            or identity["source_url_sha256"] != row["source_url_sha256"]
        ):
            raise ValueError("selection and sealed source disagree")
    copied = seed_verified_cache(seed_cache, state_root, selection)
    write_report(results_root, "profile-cache-seed.json", copied)
    trace = StageTrace(selection, results_root)
    first, replay = None, None
    guard_calls = {"fetch": 0, "parser": 0}

    def forbidden_fetch(*_args, **_kwargs):
        guard_calls["fetch"] += 1
        raise RuntimeError("pilot replay attempted HTTP")

    def forbidden_parser(*_args, **_kwargs):
        guard_calls["parser"] += 1
        raise RuntimeError("pilot replay attempted extraction")

    with trace:
        started = time.perf_counter()
        try:
            first = controller.run(
                Path(plain["bundle_path"]),
                plain["manifest_sha256"],
                state_root,
                identifiers,
                source_root=source_root,
            )
        except Exception as error:
            first = {"schema_version": 1, "status": "failed", **error_record(error)}
        first["elapsed_seconds"] = time.perf_counter() - started
        write_report(results_root, "profile-first.json", first)
        replay_ids = [
            row["resource_id"]
            for row in first.get("resources", [])
            if row.get("profile_file")
        ]
        trace.phase = "replay"
        fetch, parse = source.fetch, extractor.extract
        source.fetch, extractor.extract = forbidden_fetch, forbidden_parser
        started = time.perf_counter()
        try:
            if replay_ids:
                replay = controller.run(
                    Path(plain["bundle_path"]),
                    plain["manifest_sha256"],
                    state_root,
                    replay_ids,
                    source_root=source_root,
                )
                if replay["acquisition_cache_hits"] != len(replay_ids) or replay[
                    "extraction_cache_hits"
                ] != len(replay_ids):
                    raise RuntimeError("pilot replay did not use every cache")
            else:
                replay = {
                    "schema_version": 1,
                    "status": "skipped",
                    "reason": "no_profiles",
                }
        except Exception as error:
            replay = {"schema_version": 1, "status": "failed", **error_record(error)}
        finally:
            source.fetch, extractor.extract = fetch, parse
        replay.update(
            elapsed_seconds=time.perf_counter() - started,
            forbidden_io_calls=guard_calls,
        )
        write_report(results_root, "profile-replay.json", replay)
        trace.phase = "finished"
        trace.flush()
    summary = {
        "first_status": first["status"],
        "replay_status": replay["status"],
        "selected_count": first.get("selected_count", 0),
        "profile_count": len(replay_ids),
        "status_counts": {
            status: sum(row["status"] == status for row in first.get("resources", []))
            for status in sorted({row["status"] for row in first.get("resources", [])})
        },
        "known_status_comparison": [
            {
                "resource_id": row["resource_id"],
                "expected_previous_status": trace.cases[row["resource_id"]][
                    "prior_status"
                ]["status"],
                "current_status": row["status"],
            }
            for row in first.get("resources", [])
            if trace.cases[row["resource_id"]].get("prior_status")
        ],
        "bundle_id": bundle.bundle_id,
        "manifest_sha256": bundle.manifest_sha256,
        "live_index_modified": False,
        "gpu_used": False,
    }
    write_report(results_root, "profile-summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "selection",
        "plain-report",
        "state-root",
        "results-root",
        "source-root",
    ):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--seed-cache", type=Path)
    options = parser.parse_args(argv)
    summary = run(
        options.selection,
        options.plain_report,
        options.state_root,
        options.results_root,
        options.source_root,
        options.seed_cache,
    )
    print(json.dumps(summary, sort_keys=True))
    return int(
        summary["first_status"] == "failed" or summary["replay_status"] == "failed"
    )


if __name__ == "__main__":
    raise SystemExit(main())
