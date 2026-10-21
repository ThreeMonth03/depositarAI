"""Private product CLI. Credentials and raw Docker output are never printed."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_COMMANDS = frozenset(
    {
        "prepare-public-entry",
        "up-public-entry",
        "update-public-entry",
        "rollback-public-entry",
    }
)

# Only exact operator-owned exception classes and fixed literals may add a
# diagnostic. Error owners are loaded only when their failure is encountered.

_PRIVATE_RUNTIME_COMMANDS = frozenset(
    {
        "up-private",
        "start-inference",
        "start-release",
        "prepare-gateway",
        "activate-gateway",
    }
)

_RUNTIME_DOCKER_CODES = frozenset(
    {
        "inference_command_budget_invalid",
        "inference_client_guard_invalid",
        "inference_command_budget",
        "inference_command_invalid",
        "inference_output_limit",
        "inference_command_timeout",
        "inference_command_failed",
        "inference_image_inspection_invalid",
        "inference_container_inventory_invalid",
        "inference_network_inventory_invalid",
        "inference_partial_state_recovery_required",
        "inference_completed_runtime_changed",
        "inference_start_identity_changed",
    }
)

_INFERENCE_RUNTIME_CODES = frozenset(
    {
        "inference_runtime_resource_or_health_changed",
        "inference_runtime_network_peers_changed",
        "inference_runtime_snapshot_invalid",
    }
)

_CATALOG_RUNTIME_CODES = frozenset(
    {
        "catalog_admin_inventory_conflict",
        "catalog_admin_deadline_exceeded",
        "catalog_admin_authentication_failed",
        "catalog_admin_transport_failed",
        "catalog_admin_diagnostic_failed",
        "catalog_runtime_not_healthy",
        "catalog_requires_exactly_three_services",
        "catalog_runtime_receipt_changed",
    }
)

_CATALOG_ACCESS_CODES = frozenset(
    {
        "catalog_access_binding_changed",
        "catalog_access_binding_invalid",
        "catalog_access_bundle_invalid",
        "catalog_access_identity_invalid",
        "catalog_access_identity_recovery_required",
        "catalog_access_permissions_changed",
        "catalog_access_privilege_probe_failed",
        "catalog_access_provision_failed",
        "catalog_access_recovery_required",
        "catalog_access_search_incomplete",
        "catalog_access_secret_changed",
        "catalog_access_secret_invalid",
        "catalog_access_transport_failed",
    }
)

_CATALOG_PROJECTION_CODES = frozenset(
    {
        "catalog_projection_bulk_incomplete",
        "catalog_projection_bulk_item_failed",
        "catalog_projection_bulk_partial_shards",
        "catalog_projection_bundle_invalid",
        "catalog_projection_document_changed",
        "catalog_projection_document_invalid",
        "catalog_projection_index_changed",
        "catalog_projection_index_invalid",
        "catalog_projection_inventory_changed",
        "catalog_projection_inventory_incomplete",
        "catalog_projection_mapping_changed",
        "catalog_projection_mapping_unconfirmed",
        "catalog_projection_partial_search",
        "catalog_projection_partial_shards",
        "catalog_projection_request_failed",
        "catalog_projection_requires_one_index",
        "catalog_projection_response_invalid",
        "catalog_projection_transport_failed",
        "catalog_projection_vector_invalid",
        "catalog_projection_vector_mapping_invalid",
        "catalog_projection_vectors_changed",
        "catalog_projection_vectors_incomplete",
    }
)


def _operator_error_code(error, command):
    error_type = type(error)
    owner = error_type.__module__
    if type(owner) is not str:
        return None
    if owner == "search.operations.catalog.policy":
        from search.operations.catalog.policy import RuntimeError as CatalogRuntimeError

        if error_type is not CatalogRuntimeError:
            return None
        codes = _CATALOG_RUNTIME_CODES
    elif owner == "search.operations.catalog.access":
        from search.operations.catalog.access import AccessError

        if error_type is not AccessError:
            return None
        codes = _CATALOG_ACCESS_CODES
    elif owner == "search.operations.catalog.projection":
        from search.operations.catalog.projection import ProjectionError

        if error_type is not ProjectionError:
            return None
        codes = _CATALOG_PROJECTION_CODES
    elif (
        owner == "search.operations.deployment.runtime_docker"
        and command in _PRIVATE_RUNTIME_COMMANDS
    ):
        from search.operations.deployment.runtime_docker import InferenceError

        if error_type is not InferenceError:
            return None
        codes = _RUNTIME_DOCKER_CODES
    elif (
        owner == "search.operations.deployment.inference_runtime"
        and command in _PRIVATE_RUNTIME_COMMANDS
    ):
        from search.operations.deployment.inference_runtime import InferenceRuntimeError

        if error_type is not InferenceRuntimeError:
            return None
        codes = _INFERENCE_RUNTIME_CODES
    elif command == "up-private" and owner == "search.operations.lifecycle":
        from search.operations import lifecycle

        if error_type is not lifecycle.UpPrivateError:
            return None
        codes = lifecycle.UP_PRIVATE_ERROR_CODES
    elif (
        command == "restore-product"
        and owner == "search.operations.deployment.product_restore"
    ):
        from search.operations.deployment import product_restore

        if error_type is not product_restore.ProductRestoreError:
            return None
        codes = product_restore.ERROR_CODES
    elif (
        command in PUBLIC_COMMANDS
        and owner == "search.operations.deployment.public_entry.preflight"
    ):
        from search.operations.deployment.public_entry import preflight

        if error_type is not preflight.PublicEntryPreflightError:
            return None
        codes = preflight.ERROR_CODES
    elif (
        command in PUBLIC_COMMANDS
        and owner == "search.operations.deployment.public_entry.updates"
    ):
        from search.operations.deployment.public_entry import updates

        if error_type is not updates.PublicEntryUpdateError:
            return None
        codes = updates.ERROR_CODES
    else:
        return None
    arguments = error.args
    if type(arguments) is not tuple or len(arguments) != 1:
        return None
    candidate = arguments[0]
    if type(candidate) is str and candidate == "catalog_admin_diagnostic_failed":
        from search.catalog.errors import valid_failure_record

        if not valid_failure_record(error.__dict__.get("catalog_diagnostic")):
            return None
    return candidate if type(candidate) is str and candidate in codes else None


def _activation_outcome(error, command):
    if (
        command not in {"activate-gateway", "up-private"}
        or type(error).__module__ != "search.operations.state"
    ):
        return None
    from search.operations.state import ActivationOutcomeError

    arguments = error.args
    if (
        type(error) is not ActivationOutcomeError
        or type(arguments) is not tuple
        or len(arguments) != 1
        or type(arguments[0]) is not str
        or arguments[0] != "product_activation_outcome_unconfirmed"
        or "active_changed" not in error.__dict__
    ):
        return None
    changed = error.__dict__["active_changed"]
    if changed is not True and changed is not None:
        return None
    return {
        "status": "recovery_required",
        "error_code": "product_activation_outcome_unconfirmed",
        "active_changed": changed,
        "activation_performed": changed,
        "runtime_verified": False,
        "confers_runtime_authority": False,
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config",
        type=Path,
        help="operator-owned 0600 product JSON; required for runtime commands",
    )
    commands = result.add_subparsers(dest="command", required=True)
    restore = commands.add_parser(
        "restore-product",
        help="verify a trusted product package and restore source without runtime configuration",
    )
    restore.add_argument("--package", type=Path, required=True)
    restore.add_argument("--expected-revision", required=True)
    restore.add_argument("--expected-manifest-sha256", required=True)
    restore.add_argument("--destination", type=Path, required=True)
    for command in ("prepare-public-entry", "up-public-entry", "update-public-entry"):
        entry = commands.add_parser(
            command,
            help="prepare/start a separate loopback authenticated CPU website; no GPU changes",
        )
        entry.add_argument("--entry-config", type=Path, required=True)
        entry.add_argument("--release-compose", type=Path, required=True)
        entry.add_argument("--web-image", required=True)
        entry.add_argument("--web-nginx-image", required=True)
    rollback = commands.add_parser(
        "rollback-public-entry",
        help="restore the recorded website image pair; retain Redis quotas",
    )
    rollback.add_argument("--entry-config", type=Path, required=True)
    doctor = commands.add_parser(
        "doctor", help="read-only storage, GPU and model checks"
    )
    doctor.add_argument(
        "--deep", action="store_true", help="SHA256-check all model blobs"
    )
    sync = commands.add_parser(
        "sync", help="cache public metadata; never alter the live index"
    )
    sync.add_argument("--snapshot", type=Path, help="absolute JSONL(.gz), no network")
    sync.add_argument("--force", action="store_true", help="bypass opt-in cache TTL")
    sync.add_argument("--baseline-bundle", type=Path)
    sync.add_argument("--baseline-manifest-sha256")
    commands.add_parser("status", help="local staging receipts, not runtime health")
    commands.add_parser(
        "prepare-secrets",
        help="create/reverify product-owned API key; never emit or rotate its bytes",
    )
    commands.add_parser(
        "start-inference",
        help="start/reverify three private GPU services; no website or warmup claim",
    )
    commands.add_parser(
        "start-release",
        help="start/reverify three private CPU website services; no ports or gateway",
    )
    commands.add_parser(
        "prepare-gateway",
        help="reverify completed CPU release and stage a route candidate; no activation",
    )
    commands.add_parser(
        "activate-gateway",
        help="first gateway activation after completed preparation; partial state requires recovery",
    )
    commands.add_parser(
        "build-images",
        help="build/reverify sealed committed private images; no services or registry",
    )
    commands.add_parser(
        "build-catalog-images",
        help="build/reverify private catalog and admin images; never start services",
    )
    package = commands.add_parser(
        "package-product",
        help="scan source and export prepared images, metadata and pinned models",
    )
    package.add_argument("--destination", type=Path, required=True)
    package.add_argument(
        "--scanner", type=Path, required=True, help="reviewed Gitleaks binary"
    )
    package.add_argument(
        "--external-models",
        action="store_true",
        help="record required models without copying them; package is incomplete",
    )
    commands.add_parser(
        "prepare-query",
        help="provision query-only identities for complete private vectors; no website",
    )
    prepare = commands.add_parser(
        "prepare-index", help="prepare an isolated CPU catalog; no activation"
    )
    prepare.add_argument(
        "--catalog-revision",
        help="original catalog revision from a product package; requires compatible data recipe",
    )
    start = commands.add_parser(
        "start-index", help="start prepared private CPU catalog; no published ports"
    )
    start.add_argument(
        "--prebuilt-images",
        type=Path,
        help="absolute private images.json receipt; use only existing local images",
    )
    commands.add_parser(
        "import-index", help="import metadata into the private candidate; no vectors"
    )
    commands.add_parser(
        "reconcile-index", help="explicitly check full inventory; never retry import"
    )
    vectors = commands.add_parser(
        "build-vectors",
        help="bounded CPU vector passes with clean checkpoints; no activation",
    )
    vectors.add_argument("--max-passes", type=int, default=16)
    vectors.add_argument("--wall-seconds", type=int, default=7200)
    for command, description in (
        (
            "stage-catalog",
            "one locked metadata/index/vector staging flow; no website or activation",
        ),
        (
            "up-private",
            "first private website activation on a prepared host; no update or public ingress",
        ),
    ):
        stage = commands.add_parser(
            command,
            help=description,
            description=description,
            epilog="Pass --config /absolute/private/product.json before the command.",
        )
        stage.add_argument(
            "--snapshot",
            type=Path,
            help="absolute JSONL(.gz); rebuild metadata offline",
        )
        stage.add_argument(
            "--force", action="store_true", help="bypass opt-in cache TTL"
        )
        stage.add_argument("--max-passes", type=int, default=16)
        stage.add_argument("--wall-seconds", type=int, default=7200)
        stage.add_argument(
            "--catalog-revision",
            help="original catalog revision from a compatible product package",
        )
    return result


def _prebuilt_receipt(path):
    from search.operations.shared import private_io
    from search.serialization import strict_json

    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("product_image_receipt_path_invalid")
    descriptor = private_io.open_directory(path.parent)
    try:
        raw = private_io.read_file(descriptor, path.name, mode=0o600, max_bytes=1024**2)
        private_io.verify_directory(descriptor, path.parent)
    finally:
        os.close(descriptor)
    try:
        receipt = strict_json(raw)
    except RecursionError:
        raise ValueError("product_image_receipt_json_invalid") from None
    if type(receipt) is not dict:
        raise ValueError("product_image_receipt_json_invalid")
    # JSON decoder recursion limits differ between supported Python versions.
    # Bound receipt nesting explicitly before any lifecycle operation starts.
    pending = [(receipt, 1)]
    while pending:
        value, depth = pending.pop()
        if depth > 16:
            raise ValueError("product_image_receipt_json_invalid")
        children = value.values() if type(value) is dict else value
        pending.extend(
            (child, depth + 1) for child in children if type(child) in (dict, list)
        )
    return receipt


def _runtime_report(options):
    from search.operations import config as product_config

    config = product_config.load_config(options.config, repo_root=ROOT)
    if options.command == "doctor":
        from search.operations import doctor

        return doctor.run_doctor(config, repo_root=ROOT, deep_verify=options.deep)
    from search.operations import lifecycle

    if options.command == "sync":
        if bool(options.baseline_bundle) != bool(options.baseline_manifest_sha256):
            raise ValueError("baseline_requires_path_and_manifest")
        baseline = (
            (options.baseline_bundle, options.baseline_manifest_sha256)
            if options.baseline_bundle
            else None
        )
        report = lifecycle.sync_metadata(
            config,
            snapshot=options.snapshot,
            force=options.force,
            baseline=baseline,
        )
    elif options.command in {"stage-catalog", "up-private"}:
        stage = (
            lifecycle.stage_catalog
            if options.command == "stage-catalog"
            else lifecycle.up_private
        )
        report = stage(
            config,
            snapshot=options.snapshot,
            force=options.force,
            max_passes=options.max_passes,
            wall_seconds=options.wall_seconds,
            **(
                {}
                if options.catalog_revision is None
                else {"catalog_revision": options.catalog_revision}
            ),
        )
    elif options.command == "build-vectors":
        report = lifecycle.build_vectors(
            config, max_passes=options.max_passes, wall_seconds=options.wall_seconds
        )
    elif options.command == "start-index" and options.prebuilt_images is not None:
        report = lifecycle.start_index(
            config, prebuilt_images=_prebuilt_receipt(options.prebuilt_images)
        )
    elif options.command == "prepare-index" and options.catalog_revision is not None:
        report = lifecycle.prepare_index(
            config, catalog_revision=options.catalog_revision
        )
    elif options.command == "package-product":
        report = lifecycle.package_product(
            config,
            destination=options.destination,
            scanner=options.scanner,
            include_models=not options.external_models,
        )
    elif options.command in {
        "prepare-secrets",
        "start-inference",
        "start-release",
        "prepare-gateway",
        "activate-gateway",
        "build-images",
        "build-catalog-images",
        "prepare-query",
        "prepare-index",
        "start-index",
        "import-index",
        "reconcile-index",
    }:
        actions = {
            "prepare-secrets": lifecycle.prepare_secrets,
            "start-inference": lifecycle.start_inference,
            "start-release": lifecycle.start_release,
            "prepare-gateway": lifecycle.prepare_gateway,
            "activate-gateway": lifecycle.activate_gateway,
            "build-images": lifecycle.build_images,
            "build-catalog-images": lifecycle.build_catalog_images,
            "prepare-query": lifecycle.prepare_query,
            "prepare-index": lifecycle.prepare_index,
            "start-index": lifecycle.start_index,
            "import-index": lifecycle.import_index,
            "reconcile-index": lifecycle.reconcile_index,
        }
        report = actions[options.command](config)
    else:
        report = lifecycle.status(config)
    return report


def main(argv=None):
    arguments = parser()
    options = arguments.parse_args(argv)
    independent = {"restore-product", *PUBLIC_COMMANDS}
    if options.command in independent:
        if options.config is not None:
            arguments.error(f"{options.command} does not use --config")
    elif options.config is None:
        arguments.error("--config is required for runtime commands")
    try:
        if options.command in PUBLIC_COMMANDS:
            extra = (
                (options.release_compose, options.web_image, options.web_nginx_image)
                if options.command
                in {"prepare-public-entry", "up-public-entry", "update-public-entry"}
                else ()
            )
            if options.command in {"prepare-public-entry", "up-public-entry"}:
                from search.operations.deployment.public_entry import host

                action = (
                    host.prepare_public_entry
                    if options.command == "prepare-public-entry"
                    else host.up_public_entry
                )
            else:
                from search.operations.deployment.public_entry import updates

                action = {
                    "update-public-entry": updates.update_public_entry,
                    "rollback-public-entry": updates.rollback_public_entry,
                }[options.command]
            report = action(options.entry_config, *extra)
            print(
                json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False)
            )
            return 1 if report.get("status") == "deployment_failed" else 0
        if options.command == "restore-product":
            from search.operations.deployment import product_restore

            report = product_restore.restore_product(
                options.package,
                expected_revision=options.expected_revision,
                expected_manifest_sha256=options.expected_manifest_sha256,
                destination=options.destination,
            )
            print(
                json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False)
            )
            return 0
        report = _runtime_report(options)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        if report.get("status") == "checkpoint":
            return 2
        return 1 if report.get("status") in {"blocked", "recovery_required"} else 0
    except Exception as error:
        # Preserve except-clause matching against the real exception type.
        # Catalog protocol failures derive from RuntimeError; load that ancestor
        # only for non-standard failures and propagate unrelated exceptions.
        if not issubclass(
            type(error),
            (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError),
        ):
            from search.catalog.errors import CatalogError

            if not issubclass(type(error), CatalogError):
                raise
        report = {
            "schema_version": 1,
            "status": "failed",
            "code": "product_operation_failed",
            "error_type": type(error).__name__,
            "active_changed": False,
        }
        activation_outcome = _activation_outcome(error, options.command)
        if activation_outcome is not None:
            report.update(activation_outcome)
        error_code = _operator_error_code(error, options.command)
        if error_code is not None:
            report["error_code"] = error_code
            if error_code == "catalog_admin_diagnostic_failed":
                report["catalog_diagnostic"] = error.__dict__["catalog_diagnostic"]
        print(json.dumps(report), file=sys.stderr)
        return 1
