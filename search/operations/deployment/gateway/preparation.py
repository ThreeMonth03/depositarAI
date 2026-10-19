"""Prepare the singleton gateway for an authenticated completed private release.

This first gateway lifecycle step writes a candidate route and pinned plan only.
It never starts Nginx, publishes active.conf, reloads routing or changes active
state. Activation must freshly authenticate routed peers and probe the host's
loopback endpoint; these prepared records grant neither capability.
"""

from __future__ import annotations

from contextlib import ExitStack
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import time

from search.operations.config import ProductConfig
from search.operations.deployment import (
    composition,
    image_authority as image_evidence,
)
from search.operations.deployment.gateway import runtime as gateway_runtime
from search.operations.deployment import image_build, inference
from search.operations.deployment.release import (
    controller as release,
    runtime as release_runtime,
)
from search.operations.deployment import runtime_docker, source_bundle
from search.operations.shared import private_io
from search.operations.state import StateStore


FILES = {"intent.json", "compose.json", "prepared.json", "routes"}
ROUTES = {"candidate.conf"}
MAX_ROUTE_BYTES = 16 * 1024


class GatewayError(ValueError):
    """Closed gateway diagnostics without private responses or operator bytes."""


def _require(condition, code):
    if not condition:
        raise GatewayError("gateway_" + code)


def _same(actual, expected, code="evidence_changed"):
    _require(private_io.canonical(actual) == private_io.canonical(expected), code)


def prepare_gateway(
    config,
    store,
    revision,
    catalog_plan,
    *,
    repo_root,
    source_runner=None,
    docker_runner=None,
    catalog_runner=None,
    gpu_runner=None,
):
    """Reverify existing CPU3 and stage candidates, with no deployment mutation.

    The caller holds the product lock. The existing release facade owns the
    catalog lock and validates its completed lease, query, GPU, model and HTTP
    evidence. A missing/partial release is rejected before invoking that facade,
    so its start path can never be used to create CPU services here.
    """
    try:
        _require(
            isinstance(config, ProductConfig)
            and isinstance(store, StateStore)
            and store.root == config.state_root
            and os.geteuid() != 0,
            "product_state_invalid",
        )
        return _prepare(
            config,
            store,
            revision,
            catalog_plan,
            Path(repo_root),
            source_runner,
            docker_runner,
            catalog_runner,
            gpu_runner,
        )
    except GatewayError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        OverflowError,
        subprocess.SubprocessError,
    ):
        raise GatewayError("gateway_preparation_failed") from None


def _prepare(
    config,
    store,
    revision,
    catalog_plan,
    repo,
    source_runner,
    docker_runner,
    catalog_runner,
    gpu_runner,
):
    inference._source_revision(repo, revision, source_runner)
    plans = composition.render_composition(
        config,
        catalog_plan,
        composition.QueryBindingPaths.for_catalog(config, catalog_plan),
        revision,
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    cpu_project = plans["release"]["name"]
    slot_name = cpu_project.removeprefix("depositar-ai-")
    root = config.state_root / "gateway"
    with ExitStack() as stack:
        parent = private_io.open_child_directory(store.fd, "releases")
        stack.callback(os.close, parent)
        slot = private_io.open_child_directory(parent, slot_name)
        stack.callback(os.close, slot)
        _require(set(os.listdir(slot)) == release.FILES, "completed_release_required")
        records = {name: image_build._record(slot, name) for name in release.FILES}
        completed = records["complete.json"]
        _same(store.read("release-candidate.json"), completed)
        _require(
            completed.get("status") == "private_release_running"
            and completed.get("revision") == revision
            and completed.get("project") == cpu_project,
            "completed_release_required",
        )
        reused = "gateway" in os.listdir(store.fd)
        gateway_fd = routes_fd = None
        if reused:
            gateway_fd = private_io.open_child_directory(store.fd, "gateway")
            stack.callback(os.close, gateway_fd)
            _require(
                set(os.listdir(gateway_fd)) == FILES, "partial_state_recovery_required"
            )
            routes_fd = private_io.open_child_directory(gateway_fd, "routes")
            stack.callback(os.close, routes_fd)
            _require(
                set(os.listdir(routes_fd)) == ROUTES, "partial_state_recovery_required"
            )
        deadline = time.monotonic() + runtime_docker.TOTAL_SECONDS + 600
        source_plan = source_bundle.committed_source_plan(
            repo, revision, runner=source_runner
        )
        authority = image_evidence.read_completed_images(config, store, source_plan)
        proof = authority["images"][gateway_runtime.ROLE]
        # Every client handle exits successfully before any candidate publication.
        with ExitStack() as transport:
            client = Path(
                transport.enter_context(
                    tempfile.TemporaryDirectory(prefix="depositar-gateway-client-")
                )
            )
            client_fd = private_io.open_directory(client)
            transport.callback(os.close, client_fd)

            def verify_client():
                private_io.verify_directory(client_fd, client)
                _require(not os.listdir(client_fd), "client_changed")

            docker = runtime_docker.Docker(
                client,
                docker_runner,
                deadline=deadline,
                max_commands=16,
                verify_client=verify_client,
            )

            def verify_gateway_image():
                for reference in (
                    proof["image_id"],
                    image_build.TAGS[gateway_runtime.ROLE] + revision,
                ):
                    _same(
                        source_bundle.validate_built_image(
                            source_plan,
                            gateway_runtime.ROLE,
                            proof["image_id"],
                            docker.inspect(reference),
                        ),
                        proof,
                        "image_changed",
                    )

            def no_gateway_runtime():
                _require(
                    not any(
                        inference._project_inventory(
                            docker, composition.GATEWAY_PROJECT
                        )
                    ),
                    "existing_runtime_not_adopted",
                )

            verify_gateway_image()
            no_gateway_runtime()
            accepted = release.start_private_release(
                config,
                store,
                revision,
                catalog_plan,
                repo_root=repo,
                source_runner=source_runner,
                docker_runner=docker_runner,
                catalog_runner=catalog_runner,
                gpu_runner=gpu_runner,
            )
            _require(
                accepted.get("runtime_reused") is True, "completed_release_required"
            )
            _same(
                {
                    key: value
                    for key, value in accepted.items()
                    if key != "runtime_reused"
                },
                completed,
            )
            verify_gateway_image()
            no_gateway_runtime()
            _same(
                image_evidence.read_completed_images(config, store, source_plan),
                authority,
            )
            verify_client()
        gateway = copy.deepcopy(plans["gateway"])
        service = gateway["services"][gateway_runtime.ROLE]
        service.update(
            image=proof["image_id"],
            labels=release_runtime._labels(
                records["lease.json"], gateway_runtime.ROLE, proof["source_sha256"]
            ),
        )
        gateway_runtime._plan(
            gateway, records["compose.json"], records["lease.json"], proof
        )
        route = gateway["x-depositar-plan"]["route_text"].encode("utf-8")
        _require(0 < len(route) <= MAX_ROUTE_BYTES, "route_invalid")
        identity = {
            "schema_version": 1,
            "revision": revision,
            "project": composition.GATEWAY_PROJECT,
            "release_project": cpu_project,
            "release_complete_sha256": release.execution.digest_record(completed),
            "release_lease_sha256": release.execution.digest_record(
                records["lease.json"]
            ),
            "source_manifest_sha256": authority["source_manifest_sha256"],
            "gateway_image_proof": proof,
            "compose_sha256": release.execution.digest_record(gateway),
            "route_sha256": hashlib.sha256(route).hexdigest(),
            "active_changed": False,
        }
        result = {
            **identity,
            "status": "gateway_prepared",
            "activation_required": True,
            "confers_runtime_authority": False,
            "runtime_verified": False,
            "gateway_verified": False,
            "ports_published": False,
            "active_route_published": False,
            "activation_performed": False,
            "loopback_verified": False,
            "routed_two_turn_search_verified": False,
            "routed_session_reset_verified": False,
        }

        def check_release():
            inference._source_revision(repo, revision, source_runner)
            store._check_directory()
            private_io.verify_directory(parent, config.state_root / "releases")
            private_io.verify_directory(
                slot, config.state_root / "releases" / slot_name
            )
            _require(set(os.listdir(slot)) == release.FILES, "release_changed")
            for name, value in records.items():
                _same(image_build._record(slot, name), value)
            _same(store.read("release-candidate.json"), completed)
            _require(time.monotonic() < deadline, "preparation_deadline")

        check_release()

        def check_candidate(expected_files):
            private_io.verify_directory(gateway_fd, root)
            private_io.verify_directory(routes_fd, root / "routes")
            _require(
                set(os.listdir(gateway_fd)) == expected_files
                and set(os.listdir(routes_fd)) == ROUTES,
                "state_changed",
            )
            _same(image_build._record(gateway_fd, "intent.json"), identity)
            _same(image_build._record(gateway_fd, "compose.json"), gateway)
            _require(
                private_io.read_file(
                    routes_fd, "candidate.conf", mode=0o600, max_bytes=MAX_ROUTE_BYTES
                )
                == route,
                "route_changed",
            )

        if reused:
            check_candidate(FILES)
            _same(image_build._record(gateway_fd, "prepared.json"), result)
        else:
            gateway_fd = private_io.open_child_directory(
                store.fd, "gateway", create=True
            )
            stack.callback(os.close, gateway_fd)
            private_io.write_json(
                gateway_fd,
                "intent.json",
                identity,
                max_bytes=image_build.MAX_RECORD_BYTES,
            )
            routes_fd = private_io.open_child_directory(
                gateway_fd, "routes", create=True
            )
            stack.callback(os.close, routes_fd)
            private_io.write_file(
                routes_fd, "candidate.conf", route, max_bytes=MAX_ROUTE_BYTES
            )
            private_io.write_json(
                gateway_fd,
                "compose.json",
                gateway,
                max_bytes=image_build.MAX_RECORD_BYTES,
            )
            check_release()
            check_candidate(FILES - {"prepared.json"})
            _require(time.monotonic() < deadline, "preparation_deadline")
            private_io.write_json(
                gateway_fd,
                "prepared.json",
                result,
                max_bytes=image_build.MAX_RECORD_BYTES,
            )
        private_io.verify_directory(gateway_fd, root)
        private_io.verify_directory(routes_fd, root / "routes")
        check_release()
        _same(image_build._record(gateway_fd, "prepared.json"), result)
        check_candidate(FILES)
        _require(time.monotonic() < deadline, "preparation_deadline")
        return {**result, "preparation_reused": reused}
