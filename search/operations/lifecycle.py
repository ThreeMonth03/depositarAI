"""Private candidate lifecycle; metadata/index steps never promote live service."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from search.catalog.bundle import CatalogBundle
from search.operations.config import ProductConfig
from search.operations.metadata import sync as catalog_sync
from search.operations.shared.source_identity import require_clean_revision
from search.operations.state import (
    _directory,
    locked_state,
    operation,
    operation_receipts,
)


ROOT = Path(__file__).resolve().parents[2]


def revision(repo_root=ROOT):
    return require_clean_revision(repo_root)


def _require_revision(source_revision, repo_root):
    if revision(repo_root) != source_revision:
        raise ValueError("product_invocation_source_changed")


def _outcome(candidate):
    return {key: candidate[key] for key in ("bundle_id", "manifest_sha256")}


def _candidate(store):
    value = store.read("candidate.json")
    keys = {
        "schema_version",
        "bundle_path",
        "bundle_id",
        "manifest_sha256",
        "document_count",
        "source_revision",
        "receipt_path",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["source_revision"]) is not str
        or re.fullmatch(r"[0-9a-f]{40}", value["source_revision"]) is None
        or type(value["bundle_id"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", value["bundle_id"])
        or type(value["document_count"]) is not int
    ):
        raise ValueError("product_candidate_invalid")
    expected_path = store.root / "cache/bundles" / ("sha256-" + value["bundle_id"])
    if value["bundle_path"] != str(expected_path):
        raise ValueError("product_candidate_path_invalid")
    bundle = CatalogBundle.load(
        expected_path, expected_manifest_sha256=value["manifest_sha256"]
    )
    if (
        bundle.bundle_id != value["bundle_id"]
        or len(bundle.documents) != value["document_count"]
    ):
        raise ValueError("product_candidate_identity_mismatch")
    return value


def sync_metadata(
    config: ProductConfig,
    *,
    snapshot=None,
    force=False,
    baseline=None,
    repo_root=ROOT,
    fetch=None,
):
    """Download two matching public scans or use one bounded offline snapshot.

    A previous *staged* bundle is only a diff baseline, not an active deployment.
    Every invocation returns the real bundle/manifest and source commit.
    """
    source_revision = revision(repo_root)
    with locked_state(config, create=True) as store:
        return _sync_locked(
            config,
            store,
            source_revision,
            repo_root=repo_root,
            snapshot=snapshot,
            force=force,
            baseline=baseline,
            fetch=fetch,
        )


def _sync_locked(
    config, store, source_revision, *, repo_root, snapshot, force, baseline, fetch
):
    """The public command and one-click staging share one sync implementation."""
    _require_revision(source_revision, repo_root)
    with operation(store, "sync", source_revision) as outcome:
        cache = config.state_root / "cache"
        os.close(_directory(cache, create=True))
        comparison = "provided" if baseline is not None else "none"
        if baseline is None:
            try:
                old = _candidate(store)
            except FileNotFoundError:
                pass
            else:
                baseline = Path(old["bundle_path"]), old["manifest_sha256"]
                comparison = "previous_staged_metadata"
        report = catalog_sync.sync(
            cache,
            baseline=baseline,
            snapshot=snapshot,
            ttl_seconds=config.cache_ttl_seconds,
            force=force,
            fetch=fetch,
        )
        _require_revision(source_revision, repo_root)
        value = {
            key: report[key]
            for key in (
                "bundle_path",
                "bundle_id",
                "manifest_sha256",
                "document_count",
                "receipt_path",
            )
        }
        store.write(
            "candidate.json",
            {"schema_version": 1, "source_revision": source_revision, **value},
            replace=True,
        )
        outcome.update(_outcome(report))
        return {
            **report,
            "source_revision": source_revision,
            "comparison_baseline": comparison,
            "active_changed": False,
        }


def status(config: ProductConfig):
    """Report local receipt identities; do not pretend they are runtime health."""
    if not config.state_root.exists():
        return {
            "schema_version": 1,
            "status": "not_initialized",
            "active_release": None,
            "candidate": None,
            "runtime_verified": False,
        }
    with locked_state(config) as store:
        try:
            candidate = _candidate(store)
        except FileNotFoundError:
            candidate = None
        operations = operation_receipts(store)
        return {
            "schema_version": 1,
            "status": "recovery_required"
            if any(item["status"] == "started" for item in operations)
            else "staged",
            "active_release": None,
            "candidate": candidate,
            "operations": operations,
            "runtime_verified": False,
        }


def prepare_secrets(config: ProductConfig, *, repo_root=ROOT, runner=None):
    """Create/reverify the private product API key; no ports or service starts."""
    from search.operations.deployment.credentials import prepare_api_key

    source_revision = revision(repo_root)
    with locked_state(config, create=True) as store:
        _require_revision(source_revision, repo_root)
        with operation(store, "prepare-secrets", source_revision):
            report = prepare_api_key(
                config, store, source_revision, repo_root=repo_root, runner=runner
            )
            _require_revision(source_revision, repo_root)
            return report


def start_inference(
    config: ProductConfig,
    *,
    repo_root=ROOT,
    source_runner=None,
    docker_runner=None,
    gpu_runner=None,
):
    """Start/reverify unpublished GPU services; runtime is not model warmup."""
    from search.operations.deployment.inference import start_private_inference

    source_revision = revision(repo_root)
    with locked_state(config) as store:
        _require_revision(source_revision, repo_root)
        with operation(store, "start-inference", source_revision):
            report = start_private_inference(
                config,
                store,
                source_revision,
                repo_root=repo_root,
                source_runner=source_runner,
                docker_runner=docker_runner,
                gpu_runner=gpu_runner,
            )
            _require_revision(source_revision, repo_root)
            return report


def start_release(
    config: ProductConfig,
    *,
    repo_root=ROOT,
    source_runner=None,
    docker_runner=None,
    catalog_runner=None,
    gpu_runner=None,
):
    """Start/reverify one private CPU release; no gateway or live activation."""
    from search.operations.deployment.release.controller import start_private_release

    source_revision = revision(repo_root)
    with locked_state(config) as store:
        _require_revision(source_revision, repo_root)
        with operation(store, "start-release", source_revision) as outcome:
            candidate = _candidate(store)
            if candidate["source_revision"] != source_revision:
                raise ValueError("product_release_candidate_source_changed")
            plan = _index_plan(
                store,
                candidate,
                source_revision,
                repo_root=repo_root,
                runner=catalog_runner,
            )
            report = start_private_release(
                config,
                store,
                source_revision,
                plan,
                repo_root=repo_root,
                source_runner=source_runner,
                docker_runner=docker_runner,
                catalog_runner=catalog_runner,
                gpu_runner=gpu_runner,
            )
            _require_revision(source_revision, repo_root)
            outcome.update(_outcome(candidate))
            return report


def prepare_gateway(
    config: ProductConfig,
    *,
    repo_root=ROOT,
    source_runner=None,
    docker_runner=None,
    catalog_runner=None,
    gpu_runner=None,
):
    """Reverify completed CPU3 and stage a gateway candidate; no live routing."""
    from search.operations.deployment.gateway.preparation import (
        prepare_gateway as prepare,
    )

    source_revision = revision(repo_root)
    with locked_state(config) as store:
        _require_revision(source_revision, repo_root)
        with operation(store, "prepare-gateway", source_revision) as outcome:
            candidate = _candidate(store)
            if candidate["source_revision"] != source_revision:
                raise ValueError("product_gateway_candidate_source_changed")
            plan = _index_plan(
                store,
                candidate,
                source_revision,
                repo_root=repo_root,
                runner=catalog_runner,
            )
            report = prepare(
                config,
                store,
                source_revision,
                plan,
                repo_root=repo_root,
                source_runner=source_runner,
                docker_runner=docker_runner,
                catalog_runner=catalog_runner,
                gpu_runner=gpu_runner,
            )
            _require_revision(source_revision, repo_root)
            outcome.update(_outcome(candidate))
            return report


def activate_gateway(
    config: ProductConfig,
    *,
    repo_root=ROOT,
    source_runner=None,
    docker_runner=None,
    catalog_runner=None,
    gpu_runner=None,
    http_probe=None,
):
    """First activation only; partial attempts require explicit recovery."""
    from search.operations.deployment.gateway.activation import (
        activate_gateway as activate,
    )

    source_revision = revision(repo_root)
    with locked_state(config) as store:
        _require_revision(source_revision, repo_root)
        with operation(store, "activate-gateway", source_revision) as outcome:
            candidate = _candidate(store)
            if candidate["source_revision"] != source_revision:
                raise ValueError("product_gateway_candidate_source_changed")
            plan = _index_plan(
                store,
                candidate,
                source_revision,
                repo_root=repo_root,
                runner=catalog_runner,
            )
            metadata = _outcome(candidate)
            report = activate(
                config,
                store,
                source_revision,
                plan,
                repo_root=repo_root,
                source_runner=source_runner,
                docker_runner=docker_runner,
                catalog_runner=catalog_runner,
                gpu_runner=gpu_runner,
                http_probe=http_probe,
            )
            if (
                report.get("active_changed") is not True
                or report.get("activation_performed") is not True
                or report.get("confers_runtime_authority") is not True
                or report.get("status") != "gateway_active"
            ):
                raise ValueError("product_gateway_activation_result_invalid")
            outcome.update(metadata)
            outcome["active_changed"] = True
            return report


def prepare_index(
    config: ProductConfig, *, repo_root=ROOT, runner=None, catalog_revision=None
):
    """Prepare an isolated catalog from the last verified metadata candidate."""
    source_revision = revision(repo_root)
    with locked_state(config) as store:
        return _prepare_locked(
            config,
            store,
            source_revision,
            repo_root=repo_root,
            runner=runner,
            catalog_revision=catalog_revision,
        )


def _prepare_locked(
    config, store, source_revision, *, repo_root, runner, catalog_revision=None
):
    from search.operations.catalog.backend import prepare

    _require_revision(source_revision, repo_root)
    with operation(store, "prepare-index", source_revision) as outcome:
        candidate = _candidate(store)
        result = prepare(
            config,
            repo_root=repo_root,
            bundle_path=Path(candidate["bundle_path"]),
            manifest_sha256=candidate["manifest_sha256"],
            revision=source_revision,
            runner=runner,
            **(
                {}
                if catalog_revision is None
                else {"catalog_revision": catalog_revision}
            ),
        )
        _require_revision(source_revision, repo_root)
        store.write("index-candidate.json", result, replace=True)
        outcome.update(_outcome(candidate))
        return result


def _index_plan(store, candidate, source_revision, *, repo_root=ROOT, runner=None):
    """Bind staged metadata to its original catalog and compatible app source.

    The execution layer revalidates the complete sealed plan, source bytes and
    actual Docker identity; this guard prevents silently selecting an old slot.
    It never calls prepare to create a replacement during start or import.
    """
    plan = store.read("index-candidate.json")
    if (
        type(plan) is not dict
        or type(plan.get("schema_version")) is not int
        or plan["schema_version"] != 1
        or type(plan.get("revision")) is not str
        or re.fullmatch(r"[0-9a-f]{40}", plan["revision"]) is None
        or type(plan.get("document_count")) is not int
        or any(
            plan.get(key) != candidate[key]
            for key in ("bundle_path", "manifest_sha256", "document_count")
        )
    ):
        raise ValueError("product_index_candidate_changed_prepare_required")
    if plan["revision"] != source_revision:
        from search.operations.catalog.backend import validate_compatibility

        try:
            validate_compatibility(
                plan, repo_root=repo_root, revision=source_revision, runner=runner
            )
        except ValueError:
            raise ValueError(
                "product_index_candidate_changed_prepare_required"
            ) from None
    return plan


def _index_operation(config, name, *, repo_root, runner, prebuilt_images=None):
    source_revision = revision(repo_root)
    with locked_state(config) as store:
        return _index_locked(
            config,
            store,
            source_revision,
            name,
            repo_root=repo_root,
            runner=runner,
            prebuilt_images=prebuilt_images,
        )


def _index_locked(
    config, store, source_revision, name, *, repo_root, runner, prebuilt_images=None
):
    from search.operations.catalog import runtime as catalog_runtime

    _require_revision(source_revision, repo_root)
    with operation(store, name, source_revision) as outcome:
        candidate = _candidate(store)
        plan = _index_plan(
            store, candidate, source_revision, repo_root=repo_root, runner=runner
        )
        if name == "start-index":
            options = (
                {} if prebuilt_images is None else {"prebuilt_images": prebuilt_images}
            )
            result = catalog_runtime.start(
                config, plan, repo_root=repo_root, runner=runner, **options
            )
        elif name == "build-catalog-images":
            result = catalog_runtime.stage_images(
                config, plan, repo_root=repo_root, runner=runner
            )
        else:
            result = catalog_runtime.admin(
                config,
                plan,
                repo_root=repo_root,
                operation={
                    "import-index": "import",
                    "reconcile-index": "reconcile",
                }[name],
                runner=runner,
            )
        _require_revision(source_revision, repo_root)
        outcome.update(_outcome(candidate))
        return result


def start_index(
    config: ProductConfig, *, repo_root=ROOT, runner=None, prebuilt_images=None
):
    """Start only the prepared private CPU catalog; no active release change."""
    return _index_operation(
        config,
        "start-index",
        repo_root=repo_root,
        runner=runner,
        prebuilt_images=prebuilt_images,
    )


def build_catalog_images(config: ProductConfig, *, repo_root=ROOT, runner=None):
    """Build/reverify catalog and admin images without starting catalog services."""
    return _index_operation(
        config, "build-catalog-images", repo_root=repo_root, runner=runner
    )


def package_product(
    config: ProductConfig,
    *,
    destination,
    scanner,
    include_models=True,
    repo_root=ROOT,
    source_runner=None,
    docker_runner=None,
    catalog_runner=None,
):
    """Scan/roundtrip source and package completed same-source product assets."""
    from search.operations.deployment import packaging

    source_revision = revision(repo_root)
    with locked_state(config) as store:
        _require_revision(source_revision, repo_root)
        with operation(store, "package-product", source_revision) as outcome:
            candidate = _candidate(store)
            plan = _index_plan(
                store,
                candidate,
                source_revision,
                repo_root=repo_root,
                runner=catalog_runner,
            )
            result = packaging.package_product(
                config,
                store,
                source_revision,
                plan,
                repo_root=repo_root,
                destination=destination,
                scanner=scanner,
                include_models=include_models,
                source_runner=source_runner,
                docker_runner=docker_runner,
                catalog_runner=catalog_runner,
            )
            _require_revision(source_revision, repo_root)
            outcome.update(_outcome(candidate))
            return result


def import_index(config: ProductConfig, *, repo_root=ROOT, runner=None):
    """Import metadata into the isolated catalog, without vectors or promotion."""
    return _index_operation(config, "import-index", repo_root=repo_root, runner=runner)


def reconcile_index(config: ProductConfig, *, repo_root=ROOT, runner=None):
    """Explicitly verify complete inventory after an uncertain import; no retry."""
    return _index_operation(
        config, "reconcile-index", repo_root=repo_root, runner=runner
    )


def build_images(
    config: ProductConfig, *, repo_root=ROOT, source_runner=None, docker_runner=None
):
    """Build/reverify committed private product images; never start services."""
    from search.operations.deployment.image_staging import stage_product_images

    source_revision = revision(repo_root)
    with locked_state(config, create=True) as store:
        _require_revision(source_revision, repo_root)
        with operation(store, "build-images", source_revision):
            result = stage_product_images(
                config,
                store,
                source_revision,
                repo_root=repo_root,
                source_runner=source_runner,
                docker_runner=docker_runner,
            )
            _require_revision(source_revision, repo_root)
            return result


def prepare_query(config: ProductConfig, *, repo_root=ROOT, runner=None):
    """Provision verified query-only access to a complete isolated catalog."""
    from search.operations.catalog import query

    source_revision = revision(repo_root)
    with locked_state(config) as store:
        _require_revision(source_revision, repo_root)
        with operation(store, "prepare-query", source_revision) as outcome:
            candidate = _candidate(store)
            plan = _index_plan(
                store, candidate, source_revision, repo_root=repo_root, runner=runner
            )
            result = query.provision(config, plan, repo_root=repo_root, runner=runner)
            _require_revision(source_revision, repo_root)
            if (
                result.get("status") != "query-ready"
                or result.get("active_changed") is not False
            ):
                raise ValueError("product_query_preparation_not_verified")
            store.write("query-candidate.json", result, replace=True)
            outcome.update(_outcome(candidate))
            return result


def build_vectors(
    config: ProductConfig,
    *,
    repo_root=ROOT,
    runner=None,
    max_passes=16,
    wall_seconds=7200,
):
    """Build/resume only proven clean isolated vectors; never activate a site."""
    source_revision = revision(repo_root)
    with locked_state(config) as store:
        return _vectors_locked(
            config,
            store,
            source_revision,
            repo_root=repo_root,
            runner=runner,
            max_passes=max_passes,
            wall_seconds=wall_seconds,
        )


def _vectors_locked(
    config, store, source_revision, *, repo_root, runner, max_passes, wall_seconds
):
    from search.operations.catalog import vectors

    _require_revision(source_revision, repo_root)
    with operation(store, "build-vectors", source_revision) as outcome:
        candidate = _candidate(store)
        plan = _index_plan(
            store, candidate, source_revision, repo_root=repo_root, runner=runner
        )
        result = vectors.build(
            config,
            plan,
            repo_root=repo_root,
            runner=runner,
            max_passes=max_passes,
            wall_seconds=wall_seconds,
        )
        _require_revision(source_revision, repo_root)
        _validate_vector_outcome(result, candidate, plan["revision"])
        outcome.update(_outcome(candidate))
        return result


def _validate_vector_outcome(result, candidate, source_revision):
    """Refuse a malformed host result before publishing a successful step."""
    if (
        type(result) is not dict
        or result.get("status") not in {"vectors-complete", "checkpoint"}
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 1
        or result.get("revision") != source_revision
        or result.get("active_changed") is not False
        or result.get("runtime_verified") is not True
        or type(result.get("host_cleanup_verified")) is not bool
        or type(result.get("document_count")) is not int
        or result["document_count"] != candidate["document_count"]
        or any(result.get(key) != value for key, value in _outcome(candidate).items())
    ):
        raise ValueError("product_stage_vector_outcome_invalid")
    if result["status"] == "vectors-complete" and (
        result["host_cleanup_verified"] is not True
        or type(result.get("done_count")) is not int
        or result["done_count"] != candidate["document_count"]
        or type(result.get("pending_count")) is not int
        or result["pending_count"] != 0
    ):
        raise ValueError("product_stage_vector_outcome_unverified")


def _import_staged(store, candidate, source_revision, *, catalog_revision=None):
    """A local skip hint, never a substitute for actual vector/runtime proofs."""
    return any(
        receipt["operation"] == "import-index"
        and receipt["status"] == "staged"
        and receipt["source_revision"] in {source_revision, catalog_revision}
        and all(receipt.get(key) == value for key, value in _outcome(candidate).items())
        for receipt in operation_receipts(store)
    )


def stage_catalog(
    config: ProductConfig,
    *,
    snapshot=None,
    force=False,
    repo_root=ROOT,
    fetch=None,
    runner=None,
    max_passes=16,
    wall_seconds=7200,
    catalog_revision=None,
):
    """One locked staging invocation, not install/update, reconcile or activation.

    Every step retains its existing granular operation receipt. No outer
    started receipt is nested around them. Failed/uncertain catalog operations
    are not retried or reconciled automatically. Fresh staging seals catalog
    images before startup; completed image stages are freshly reverified. An
    older granular runtime without image-stage records only skips prestaging:
    start still proves its actual runtime, without fabricating image receipts.
    Matching import receipts skip another import; vectors still revalidate
    private runtime, helper/admin evidence, full inventory and the journal.
    """
    from search.operations.catalog.vectors import HELPER_TIMEOUT, MAX_PASSES

    if (
        type(max_passes) is not int
        or not 1 <= max_passes <= MAX_PASSES
        or type(wall_seconds) is not int
        or not HELPER_TIMEOUT <= wall_seconds <= 36000
    ):
        raise ValueError("catalog_vector_budget_invalid")
    source_revision = revision(repo_root)
    with locked_state(config, create=True) as store:
        _sync_locked(
            config,
            store,
            source_revision,
            repo_root=repo_root,
            snapshot=snapshot,
            force=force,
            baseline=None,
            fetch=fetch,
        )
        _prepare_locked(
            config,
            store,
            source_revision,
            repo_root=repo_root,
            runner=runner,
            catalog_revision=catalog_revision,
        )
        plan = _index_plan(
            store,
            _candidate(store),
            source_revision,
            repo_root=repo_root,
            runner=runner,
        )
        root = Path(plan["work_root"])
        # Presence is only a skip hint for the reviewed granular-start path.
        # start-index must still prove the complete runtime; no image-stage
        # authority is reconstructed from it. Partial markers cannot skip.
        markers = {"image-stage-started.json", "image-stage-complete.json"}
        present = set()
        for name in markers | {"runtime.json"}:
            try:
                metadata = (root / name).lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("product_stage_catalog_hint_invalid")
            present.add(name)
        image_staging_skipped = "runtime.json" in present and not present & markers
        if not image_staging_skipped:
            _index_locked(
                config,
                store,
                source_revision,
                "build-catalog-images",
                repo_root=repo_root,
                runner=runner,
            )
        _index_locked(
            config,
            store,
            source_revision,
            "start-index",
            repo_root=repo_root,
            runner=runner,
        )
        candidate = _candidate(store)
        import_skipped = _import_staged(
            store, candidate, source_revision, catalog_revision=plan["revision"]
        )
        if not import_skipped:
            _index_locked(
                config,
                store,
                source_revision,
                "import-index",
                repo_root=repo_root,
                runner=runner,
            )
        result = _vectors_locked(
            config,
            store,
            source_revision,
            repo_root=repo_root,
            runner=runner,
            max_passes=max_passes,
            wall_seconds=wall_seconds,
        )
        complete = result["status"] == "vectors-complete"
        return {
            **result,
            "schema_version": 1,
            "operation": "stage-catalog",
            "source_revision": source_revision,
            "catalog_revision": plan["revision"],
            "catalog_reused": complete and result.get("passes_this_invocation") == 0,
            "bundle_id": candidate["bundle_id"],
            "manifest_sha256": candidate["manifest_sha256"],
            "document_count": candidate["document_count"],
            "staging_only": True,
            "active_changed": False,
            "runtime_verified": complete,
            "image_staging_skipped": image_staging_skipped,
            "import_skipped": import_skipped,
            "steps": [
                {"operation": name, "status": status}
                for name, status in (
                    ("sync", "staged"),
                    ("prepare-index", "staged"),
                    (
                        "build-catalog-images",
                        "skipped_existing_runtime"
                        if image_staging_skipped
                        else "staged",
                    ),
                    ("start-index", "staged"),
                    ("import-index", "skipped" if import_skipped else "staged"),
                    ("build-vectors", result["status"]),
                )
            ],
        }
