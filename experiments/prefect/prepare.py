"""Exercise existing preparation with synthetic HTTP and parser boundaries."""

from contextlib import contextmanager
import csv
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from search.catalog.build import builder
from search.catalog.bundle import CatalogBundle
from search.operations.documents import controller, extractor, source
from search.operations.metadata import sync
from search.operations.shared import private_io


DATASET_ID = "00000000-0000-4000-8000-000000000001"
RESOURCE_ID = "11111111-1111-4111-8111-111111111111"
CSV_BODY = b"station,fixture_temperature\nA,18\n"
IMAGE = {
    "image_id": "sha256:" + hashlib.sha256(b"synthetic-parser-image").hexdigest(),
    "source_sha256": hashlib.sha256(b"synthetic-parser-source").hexdigest(),
}


class RetryableFixtureError(RuntimeError):
    """The injected failure happened before publishing an extraction result."""


def prepare_fixture(state_root: str) -> dict:
    """Create one fresh private workspace and a real metadata-only bundle."""
    root = controller._external_root(Path(state_root))
    if root.exists():
        raise ValueError("the proof requires a new, dedicated state directory")
    descriptor = private_io.open_directory(root, create=True)
    try:
        private_io.write_json(
            descriptor,
            "experiment.json",
            {"schema_version": 1, "kind": "prefect-synthetic-resource-proof"},
        )
        private_io.write_json(
            descriptor,
            "snapshot.jsonl",
            {
                "id": DATASET_ID,
                "name": "prefect-fixture",
                "title": "Synthetic resource preparation",
                "private": False,
                "state": "active",
                "type": "dataset",
                "resources": [
                    {
                        "id": RESOURCE_ID,
                        "name": "Synthetic observations",
                        "format": "CSV",
                        "url": "https://data.depositar.io/synthetic-prefect.csv",
                        "size": len(CSV_BODY),
                    }
                ],
            },
        )
        private_io.write_json(
            descriptor,
            "boundary-counts.json",
            {"fetches": 0, "extract_attempts": 0, "image_identity_calls": 0},
        )
    finally:
        os.close(descriptor)
    bundles = root / "bundles"
    bundles.mkdir(mode=0o700)
    result = builder.build_bundle(root / "snapshot.jsonl", bundles)
    return {
        "state_root": str(root),
        "bundle_path": result["bundle_path"],
        "manifest_sha256": result["manifest_sha256"],
    }


def boundary_counts(root: Path) -> dict:
    return json.loads((root / "boundary-counts.json").read_bytes())


@contextmanager
def synthetic_boundaries(root: Path):
    """Replace external IO only; product caches and validation remain real."""
    def count(name):
        counts = boundary_counts(root)
        counts[name] += 1
        descriptor = private_io.open_directory(root)
        try:
            private_io.write_json(
                descriptor, "boundary-counts.json", counts, replace=True
            )
        finally:
            os.close(descriptor)
        return counts[name]

    def fetch(resource):
        if resource["id"] != RESOURCE_ID:
            raise AssertionError("only the selected synthetic resource is allowed")
        count("fetches")
        return CSV_BODY, {
            "content_type": "text/csv",
            "etag": None,
            "last_modified": None,
        }

    def image(source_root):
        del source_root
        count("image_identity_calls")
        return dict(IMAGE)

    def extract(path, expected_format, encoding, header_policy, *, image):
        if (
            expected_format != "CSV"
            or encoding != "utf-8"
            or header_policy != "first_record"
            or image != IMAGE
            or Path(path).read_bytes() != CSV_BODY
        ):
            raise AssertionError("synthetic parser input changed")
        if count("extract_attempts") == 1:
            raise extractor.ExtractorError("document_extractor_command_timeout")
        rows = list(csv.reader(CSV_BODY.decode("utf-8").splitlines()))
        return {
            "schema_version": 1,
            "status": "ok",
            "detected_mime": "text/csv",
            "input_sha256": hashlib.sha256(CSV_BODY).hexdigest(),
            "columns": rows[0],
            "text": CSV_BODY.decode("utf-8"),
            "text_truncated": False,
            "rows_observed": len(rows) - 1,
            "rows_complete": False,
            "may_require_ocr": False,
        }

    with (
        patch.object(source, "fetch", side_effect=fetch),
        patch.object(extractor, "build_image", side_effect=image),
        patch.object(extractor, "extract", side_effect=extract),
    ):
        yield


def profile_selected(fixture: dict) -> dict:
    """Use the profile CLI's existing controller with explicit mock boundaries."""
    root = Path(fixture["state_root"])
    with synthetic_boundaries(root):
        report = controller.run(
            Path(fixture["bundle_path"]),
            fixture["manifest_sha256"],
            root / "profiles-state",
            [RESOURCE_ID],
        )
    if report["failed_count"]:
        if (
            report["failed_count"] == 1
            and report["resources"][0].get("code")
            == "document_extractor_command_timeout"
            and boundary_counts(root)["extract_attempts"] == 1
        ):
            raise RetryableFixtureError(
                "injected parser timeout; no result was published"
            )
        raise RuntimeError(
            "resource preparation failed; automatic retry is not allowed"
        )
    return report


def prepare_candidate(fixture: dict, profile: dict) -> dict:
    """Build an offline candidate with the existing sync/profile importer."""
    root = Path(fixture["state_root"])
    (root / "metadata-cache").mkdir(mode=0o700)
    return sync.sync(
        root / "metadata-cache",
        snapshot=root / "snapshot.jsonl",
        profile_files=[
            root / "profiles-state" / profile["resources"][0]["profile_file"]
        ],
    )


def verify_candidate(fixture: dict, candidate: dict, first: dict, replay: dict) -> dict:
    """Check retry/cache evidence and the real sealed artifact without publishing."""
    root = Path(fixture["state_root"])
    bundle = CatalogBundle.load(
        Path(candidate["bundle_path"]),
        expected_manifest_sha256=candidate["manifest_sha256"],
    )
    counts = boundary_counts(root)
    context = bundle.documents[0]["resource_context"]
    if (
        counts["fetches"] != 1
        or counts["extract_attempts"] != 2
        or first["acquisition_cache_hits"] != 1
        or first["extraction_cache_hits"] != 0
        or replay["acquisition_cache_hits"] != 1
        or replay["extraction_cache_hits"] != 1
        or len(bundle.documents) != 1
        or len(context) != 1
        or list(context[0]["columns"]) != ["station", "fixture_temperature"]
        or "fixture_temperature" not in bundle.documents[0]["content"]
        or candidate["promotion_allowed"] is not False
        or candidate["live_index_modified"] is not False
        or candidate["models_called"] is not False
    ):
        raise AssertionError("the preparation proof is incomplete")
    return {
        "schema_version": 1,
        "scope": "synthetic-boundary-flow",
        "mocked_boundaries": ["resource HTTP", "extractor image", "Tika parser"],
        "real_components": [
            "acquisition cache",
            "extraction cache",
            "profile importer",
            "sealed bundle",
        ],
        "boundary_counts": counts,
        "replay_cache_hits": {"acquisition": 1, "extraction": 1},
        "candidate_bundle_id": bundle.bundle_id,
        "candidate_manifest_sha256": bundle.manifest_sha256,
        "document_count": 1,
        "publication_performed": False,
        "models_called": False,
    }
