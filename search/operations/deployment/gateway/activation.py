"""First gateway activation only; failed intents require explicit recovery.

The product lifecycle owns its lock; this facade additionally holds the catalog
lock. Completed private CPU3 acceptance is mandatory before any mutation. The
routed host preserves unfiltered catalog/GPU networks and proves website4 plus
gateway-only ingress. An active pointer is published last, after real loopback
search/reset acceptance and a fresh proof. No retry, rollback or adoption exists.
"""

from __future__ import annotations

from contextlib import ExitStack
import copy
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from search.operations.catalog import execution, query
from search.operations.config import ProductConfig
from search.operations.deployment import composition, credentials
from search.operations.deployment.images import authority as image_evidence
from search.operations.deployment.gateway import (
    preparation as gateway,
    probe_runtime as gateway_probe_runtime,
    runtime as gateway_runtime,
)
from search.operations.deployment import inference, inference_runtime
from search.operations.deployment.images import build as image_build
from search.operations.deployment.release import (
    controller as release,
    probe as release_probe,
)
from search.operations.deployment import runtime_docker, source_bundle
from search.operations.shared import private_io
from search.operations.state import StateStore


TOTAL_SECONDS = runtime_docker.TOTAL_SECONDS + 600
MAX_COMMANDS = 128
# Before the first singleton inventory there are 43 native calls: initial
# image/daemon proof 14, two private host snapshots 16, authority refresh 13.
# Remaining: singleton inventory 2, config/recheck/up/inventory/network 7,
# bootstrap snapshot 12, nginx test/test/reload 3, native probe 12, two routed
# query snapshots 24, authority refresh 13, and final routed snapshot 12.
REMAINING_COMMANDS = 85
PROBE_FILES = {"intent.json", "container.json", "complete.json"}
ACTIVATION_FILES = {
    "intent.json",
    "compose.json",
    "created.json",
    "routed.json",
    "complete.json",
    "probe",
}
BOOTSTRAP = b"""worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr crit;
events { worker_connections 128; }
http {
    access_log off;
    server_tokens off;
    client_body_temp_path /tmp/client;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    server {
        listen 8080;
        location = /healthz { return 200; }
        location / { return 503; }
    }
}
"""


class GatewayActivationError(ValueError):
    """Closed activation failures; retained intent never implies success."""


def _require(condition, code):
    if not condition:
        raise GatewayActivationError("gateway_activation_" + code)


def _same(left, right, code="evidence_changed"):
    _require(private_io.canonical(left) == private_io.canonical(right), code)


class _RoutedHost(release._Host):
    """Extend the existing host snapshot with one separately sealed gateway.

    The base collector still authenticates its original three project IDs and
    supplies the entire website network. No gateway peer is removed or hidden.
    The inherited observer keeps catalog/GPU/API lease validation unchanged.
    """

    def __init__(
        self,
        *args,
        gateway_compose,
        gateway_image,
        gateway_proof,
        gateway_id,
        ingress_id,
        release_proof,
    ):
        super().__init__(*args)
        self.gateway_compose, self.gateway_image = gateway_compose, gateway_image
        self.gateway_proof, self.gateway_id = gateway_proof, gateway_id
        self.ingress_id, self.release_proof = ingress_id, release_proof

    def collect(self, *, clients):
        _require(clients is True and self.lease is not None, "private_lease_required")
        groups, networks = super().collect(clients=True)
        ingress = composition.GATEWAY_PROJECT + "_ingress"
        ids, names = runtime_docker.project_inventory(
            self.docker, composition.GATEWAY_PROJECT
        )
        _require(
            ids == [self.gateway_id] and names == {ingress}, "gateway_inventory_changed"
        )
        _require(
            self.gateway_id
            not in {row["Id"] for group in groups.values() for row in group},
            "identity_collision",
        )
        rows = image_build._json(self.docker.call(["inspect", self.gateway_id]))
        network = image_build._json(self.docker.call(["network", "inspect", ingress]))
        _require(
            type(rows) is list
            and len(rows) == 1
            and type(rows[0]) is dict
            and rows[0].get("Id") == self.gateway_id
            and type(network) is list
            and len(network) == 1
            and type(network[0]) is dict
            and network[0].get("Name") == ingress
            and network[0].get("Id") == self.ingress_id
            and ingress not in networks,
            "gateway_snapshot_changed",
        )
        groups[self.compose["name"]].append(rows[0])
        networks[ingress] = network[0]
        return groups, networks

    def _validate_release(self, clients, networks, catalog_network):
        names = {
            self.compose["networks"]["website"]["name"],
            composition.GATEWAY_PROJECT + "_ingress",
        }
        return gateway_runtime.validate_runtime(
            self.compose,
            self.images,
            clients,
            [networks[name] for name in sorted(names)],
            self.lease,
            gateway_compose=self.gateway_compose,
            gateway_image=self.gateway_image,
            gateway_image_proof=self.gateway_proof,
            gateway_container_id=self.gateway_id,
            ingress_network_id=self.ingress_id,
            release_proof=self.release_proof,
            catalog_identity=self.authority["catalog_identity"],
            image_proof=self.image_proof,
            image_environment=self.images["conversation-api"]["Config"]["Env"],
            catalog_network=catalog_network,
        )


def _http_report(value, *, revision, bundle_id, manifest_sha256, run_id):
    expected = {
        "schema_version": 1,
        "revision": revision,
        "bundle_id": bundle_id,
        "manifest_sha256": manifest_sha256,
        "status": "ready",
        "code": "ready",
        "gateway_http_verified": True,
        "model_warmup_verified": False,
        "readiness_verified": False,
        "private_release_acceptance_required": True,
        "gateway_runtime_validation_required": True,
        "confers_runtime_authority": False,
        "active_changed": False,
        "search_quality_verified": False,
    }
    _require(
        type(value) is dict
        and set(value) == set(expected) | {"website", "probe_runtime"},
        "http_contract_invalid",
    )
    _same({key: value[key] for key in expected}, expected, "http_contract_invalid")
    runtime = value["probe_runtime"]
    _require(
        type(runtime) is dict
        and set(runtime)
        == {
            "container_id",
            "run_id",
            "helper_removed_verified",
            "host_network_verified",
        }
        and type(runtime.get("container_id")) is str
        and re.fullmatch(r"[0-9a-f]{64}", runtime["container_id"]) is not None,
        "http_contract_invalid",
    )
    _same(
        runtime,
        {
            "container_id": runtime["container_id"],
            "run_id": run_id,
            "helper_removed_verified": True,
            "host_network_verified": True,
        },
        "http_contract_invalid",
    )
    website = release_probe._web(value["website"], revision, bundle_id, manifest_sha256)
    _require(website["code"] == "ready", "http_acceptance_blocked")
    return copy.deepcopy(value)


def _probe_records(fd, path, report, *, image_id):
    """Bind the native callback to its exact successful retained receipts."""
    private_io.verify_directory(fd, path)
    _require(set(os.listdir(fd)) == PROBE_FILES, "probe_receipts_invalid")
    runtime = report["probe_runtime"]
    identity = {
        "schema_version": 1,
        **{key: report[key] for key in ("revision", "bundle_id", "manifest_sha256")},
        "run_id": runtime["run_id"],
        "image_id": image_id,
        "container_name": "depositar-ai-gateway-probe-" + runtime["run_id"],
        "probe_sha256": hashlib.sha256(
            gateway_probe_runtime.SCRIPT.encode()
        ).hexdigest(),
        "confers_runtime_authority": False,
        "active_changed": False,
    }
    values = {
        "intent.json": {**identity, "status": "probe_started"},
        "container.json": {
            **identity,
            "status": "probe_created",
            "container_id": runtime["container_id"],
        },
        "complete.json": {**identity, "status": "probe_complete", "report": report},
    }
    records = {name: private_io.canonical(value) for name, value in values.items()}
    for name, raw in records.items():
        _require(
            private_io.read_file(fd, name, mode=0o600, max_bytes=16384) == raw,
            "probe_receipts_invalid",
        )
    return records


def activate_gateway(
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
    http_probe=None,
):
    """Activate the explicitly prepared, absent singleton once; never retry."""
    try:
        _require(
            isinstance(config, ProductConfig)
            and isinstance(store, StateStore)
            and store.root == config.state_root
            and os.geteuid() != 0,
            "product_state_invalid",
        )
        deadline = time.monotonic() + TOTAL_SECONDS
        _require(
            "active.json" not in os.listdir(store.fd), "existing_active_not_adopted"
        )
        with ExitStack() as initial:
            descriptor = private_io.open_child_directory(store.fd, "gateway")
            initial.callback(os.close, descriptor)
            _require(
                set(os.listdir(descriptor)) == gateway.FILES,
                "partial_state_recovery_required",
            )
            routes = private_io.open_child_directory(descriptor, "routes")
            initial.callback(os.close, routes)
            _require(
                set(os.listdir(routes)) == gateway.ROUTES,
                "partial_state_recovery_required",
            )
        prepared = gateway.prepare_gateway(
            config,
            store,
            revision,
            catalog_plan,
            repo_root=repo_root,
            source_runner=source_runner,
            docker_runner=docker_runner,
            catalog_runner=catalog_runner,
            gpu_runner=gpu_runner,
        )
        _require(
            prepared.get("preparation_reused") is True, "prepared_candidate_required"
        )
        context = execution.load_context(
            config, catalog_plan, repo_root=Path(repo_root), runner=catalog_runner
        )
        with execution.operation_lock(context):
            return _activate(
                config,
                store,
                revision,
                context,
                Path(repo_root),
                prepared,
                deadline,
                source_runner,
                docker_runner,
                gpu_runner,
                gateway_probe_runtime.verify_gateway_from_image
                if http_probe is None
                else http_probe,
            )
    except GatewayActivationError:
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
        raise GatewayActivationError("gateway_activation_failed") from None


def _activate(
    config,
    store,
    revision,
    context,
    repo,
    prepared,
    deadline,
    source_runner,
    docker_runner,
    gpu_runner,
    http_probe,
):
    _require(callable(http_probe), "http_probe_invalid")
    authority = query.read_authority(context)
    key = credentials.read_api_key_authority(config, store)
    source_plan = source_bundle.committed_source_plan(
        repo, revision, runner=source_runner
    )
    image_authority = image_evidence.read_completed_images(config, store, source_plan)
    inference_plan = composition._inference(
        config,
        revision,
        os.geteuid(),
        os.getegid(),
        inference_source_sha256=source_plan["services"]["reranker"]["source_sha256"],
    )
    gpu_authority = inference._authority(config, store, source_plan, inference_plan)
    root = config.state_root / "gateway"
    with ExitStack() as state:
        gateway_fd = private_io.open_child_directory(store.fd, "gateway")
        state.callback(os.close, gateway_fd)
        routes_fd = private_io.open_child_directory(gateway_fd, "routes")
        state.callback(os.close, routes_fd)
        preparation = {
            name: image_build._record(gateway_fd, name)
            for name in gateway.FILES - {"routes"}
        }
        _same(
            preparation["prepared.json"],
            {
                key: value
                for key, value in prepared.items()
                if key != "preparation_reused"
            },
        )
        gateway_compose = preparation["compose.json"]
        candidate = private_io.read_file(
            routes_fd, "candidate.conf", mode=0o600, max_bytes=gateway.MAX_ROUTE_BYTES
        )
        _same(hashlib.sha256(candidate).hexdigest(), prepared["route_sha256"])
        parents = private_io.open_child_directory(store.fd, "releases")
        state.callback(os.close, parents)
        name = prepared["release_project"].removeprefix("depositar-ai-")
        slot = private_io.open_child_directory(parents, name)
        state.callback(os.close, slot)
        records = {
            filename: image_build._record(slot, filename) for filename in release.FILES
        }
        completed, lease, compose = (
            records["complete.json"],
            records["lease.json"],
            records["compose.json"],
        )
        _same(execution.digest_record(completed), prepared["release_complete_sha256"])
        _same(execution.digest_record(lease), prepared["release_lease_sha256"])
        _same(
            image_authority["source_manifest_sha256"],
            prepared["source_manifest_sha256"],
        )
        proof = image_authority["images"][gateway_runtime.ROLE]
        _same(proof, prepared["gateway_image_proof"])
        gateway_runtime._plan(gateway_compose, compose, lease, proof)
        native = {
            field: copy.deepcopy(gateway_compose[field])
            for field in ("name", "services", "networks")
        }
        _require(
            b"$" not in private_io.canonical(native), "compose_interpolation_forbidden"
        )
        activation_fd = None
        probe_fd = None
        probe_records = {}
        activation_records = {}
        route_bytes = None

        def state_check():
            inference._source_revision(repo, revision, source_runner)
            store._check_directory()
            _require(
                "active.json" not in os.listdir(store.fd), "existing_active_not_adopted"
            )
            private_io.verify_directory(parents, config.state_root / "releases")
            private_io.verify_directory(slot, config.state_root / "releases" / name)
            _require(set(os.listdir(slot)) == release.FILES, "release_changed")
            for filename, value in records.items():
                _same(image_build._record(slot, filename), value)
            _same(store.read("release-candidate.json"), completed)
            private_io.verify_directory(gateway_fd, root)
            private_io.verify_directory(routes_fd, root / "routes")
            _require(
                set(os.listdir(gateway_fd))
                == gateway.FILES
                | ({"activation"} if activation_fd is not None else set()),
                "state_changed",
            )
            for filename, value in preparation.items():
                _same(image_build._record(gateway_fd, filename), value)
            if activation_fd is not None:
                private_io.verify_directory(activation_fd, root / "activation")
                _require(
                    set(os.listdir(activation_fd))
                    == set(activation_records)
                    | ({"probe"} if probe_fd is not None else set()),
                    "state_changed",
                )
                for filename, value in activation_records.items():
                    _same(image_build._record(activation_fd, filename), value)
            if probe_fd is not None:
                private_io.verify_directory(probe_fd, root / "activation/probe")
                _require(
                    set(os.listdir(probe_fd)) == set(probe_records), "probe_changed"
                )
                for filename, raw in probe_records.items():
                    _require(
                        private_io.read_file(
                            probe_fd, filename, mode=0o600, max_bytes=16384
                        )
                        == raw,
                        "probe_changed",
                    )
            _require(
                set(os.listdir(routes_fd))
                == gateway.ROUTES
                | ({"active.conf"} if route_bytes is not None else set()),
                "state_changed",
            )
            _require(
                private_io.read_file(
                    routes_fd,
                    "candidate.conf",
                    mode=0o600,
                    max_bytes=gateway.MAX_ROUTE_BYTES,
                )
                == candidate,
                "candidate_changed",
            )
            if route_bytes is not None:
                _require(
                    private_io.read_file(
                        routes_fd,
                        "active.conf",
                        mode=0o600,
                        max_bytes=gateway.MAX_ROUTE_BYTES,
                    )
                    == route_bytes,
                    "active_route_changed",
                )
            _require(time.monotonic() < deadline, "deadline")

        def publish(filename, value):
            state_check()
            private_io.write_json(
                activation_fd, filename, value, max_bytes=image_build.MAX_RECORD_BYTES
            )
            activation_records[filename] = value

        with ExitStack() as transport:
            models, verify_models = transport.enter_context(
                inference._verified_models(
                    config, deadline=deadline, include_embedding=True
                )
            )
            client = Path(
                transport.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix="depositar-gateway-activation-client-"
                    )
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
                max_commands=MAX_COMMANDS,
                verify_client=verify_client,
            )
            _require(
                image_build._json(docker.call(["info", "--format", "{{json .OSType}}"]))
                == "linux",
                "daemon_unsupported",
            )
            gpu_images = inference._images(docker, source_plan, gpu_authority)
            images = release._images(docker, source_plan, image_authority)
            images["filtered-embedding"] = gpu_images["scope-model"]
            catalog_images = execution.load_images(context)
            catalog_inspections = {
                role: docker.inspect(item["image_id"])
                for role, item in catalog_images.items()
            }
            plans = composition.render_composition(
                config,
                context["plan"],
                composition.QueryBindingPaths.for_catalog(config, context["plan"]),
                revision,
                uid=os.geteuid(),
                gid=os.getegid(),
                inference_source_sha256=source_plan["services"]["reranker"][
                    "source_sha256"
                ],
            )
            gpu_compose = copy.deepcopy(plans["inference"])
            for role, image in gpu_images.items():
                gpu_compose["services"][role]["image"] = image["Id"]
            gpu_identity = inference._runtime_identity(
                gpu_compose,
                {role: models[role] for role in ("api", "reranker")},
                gpu_authority,
                gpu_images,
            )
            verify_inference = release._sealed_inference(
                transport, config, store, gpu_compose, gpu_identity
            )
            host_args = (
                docker,
                context,
                authority,
                catalog_images,
                catalog_inspections,
                gpu_compose,
                gpu_images,
                verify_inference,
                compose,
                images,
                image_authority["images"]["conversation-api"],
            )
            host = release._Host(*host_args)
            host.lease = lease

            def gateway_image():
                image = docker.inspect(proof["image_id"])
                for row in (
                    image,
                    docker.inspect(image_build.TAGS[gateway_runtime.ROLE] + revision),
                ):
                    _same(
                        source_bundle.validate_built_image(
                            source_plan, gateway_runtime.ROLE, proof["image_id"], row
                        ),
                        proof,
                        "gateway_image_changed",
                    )
                return image

            built = gateway_image()
            _same(
                query.verify_authority(context, observe_runtime=host.observe), authority
            )
            private_proof = host.last[1]
            _same(
                {field: completed[field] for field in private_proof},
                private_proof,
                "private_release_changed",
            )
            inference._gpu(
                config,
                inference_runtime.ownership_proofs(
                    gpu_compose, host.last[0], gpu_images
                ),
                gpu_runner,
            )

            def authority_check():
                verify_models()
                _same(query.read_authority(context), authority)
                _same(credentials.read_api_key_authority(config, store), key)
                _same(
                    image_evidence.read_completed_images(config, store, source_plan),
                    image_authority,
                )
                _same(execution.load_images(context), catalog_images)
                _same(
                    inference._refresh_authority(
                        config, store, source_plan, gpu_authority
                    ),
                    gpu_authority,
                )
                _same(inference._images(docker, source_plan, gpu_authority), gpu_images)
                _same(
                    release._images(docker, source_plan, image_authority),
                    {role: images[role] for role in ("web", "conversation-api")},
                )
                _same(
                    {
                        role: docker.inspect(item["image_id"])
                        for role, item in catalog_images.items()
                    },
                    catalog_inspections,
                )
                _same(gateway_image(), built)

            authority_check()
            state_check()
            _require(
                type(docker.calls) is int
                and docker.max_commands == MAX_COMMANDS
                and docker.max_commands - docker.calls >= REMAINING_COMMANDS,
                "command_budget",
            )
            _require(
                not any(
                    runtime_docker.project_inventory(
                        docker, composition.GATEWAY_PROJECT
                    )
                ),
                "existing_gateway_not_adopted",
            )
            identity = {
                "schema_version": 1,
                "revision": revision,
                "project": composition.GATEWAY_PROJECT,
                "release_project": compose["name"],
                "bundle_id": context["bundle"].bundle_id,
                "manifest_sha256": context["plan"]["manifest_sha256"],
                "source_manifest_sha256": image_authority["source_manifest_sha256"],
                "prepared_sha256": execution.digest_record(
                    preparation["prepared.json"]
                ),
                "native_compose_sha256": execution.digest_record(native),
                "bootstrap_sha256": hashlib.sha256(BOOTSTRAP).hexdigest(),
                "route_sha256": hashlib.sha256(candidate).hexdigest(),
                "private_release_proof_sha256": execution.digest_record(private_proof),
                "probe_run_id": uuid.uuid4().hex,
                "active_changed": False,
                "confers_runtime_authority": False,
            }
            activation_fd = private_io.open_child_directory(
                gateway_fd, "activation", create=True
            )
            state.callback(os.close, activation_fd)
            publish("intent.json", identity)
            publish("compose.json", native)
            state_check()
            private_io.write_file(
                routes_fd, "active.conf", BOOTSTRAP, max_bytes=gateway.MAX_ROUTE_BYTES
            )
            route_bytes = BOOTSTRAP
            state_check()
            prefix = [
                "compose",
                "--env-file",
                "/dev/null",
                "--project-directory",
                str(root / "activation"),
                "--project-name",
                composition.GATEWAY_PROJECT,
                "--file",
                str(root / "activation/compose.json"),
            ]
            docker.call([*prefix, "config", "--quiet"])
            state_check()
            _require(
                not any(
                    runtime_docker.project_inventory(
                        docker, composition.GATEWAY_PROJECT
                    )
                ),
                "existing_gateway_not_adopted",
            )
            docker.call(
                [
                    *prefix,
                    "up",
                    "--detach",
                    "--no-build",
                    "--pull",
                    "never",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    gateway_runtime.ROLE,
                ],
                timeout=150,
            )
            ids, names = runtime_docker.project_inventory(
                docker, composition.GATEWAY_PROJECT
            )
            ingress = composition.GATEWAY_PROJECT + "_ingress"
            _require(len(ids) == 1 and names == {ingress}, "created_inventory_changed")
            network = image_build._json(docker.call(["network", "inspect", ingress]))
            _require(
                type(network) is list
                and len(network) == 1
                and type(network[0]) is dict
                and network[0].get("Name") == ingress,
                "created_network_invalid",
            )
            routed = _RoutedHost(
                *host_args,
                gateway_compose=gateway_compose,
                gateway_image=built,
                gateway_proof=proof,
                gateway_id=ids[0],
                ingress_id=network[0]["Id"],
                release_proof=private_proof,
            )
            routed.lease = lease
            routed.observe()
            routed_proof = routed.last[1]
            publish(
                "created.json",
                {**identity, "status": "bootstrap_running", "proof": routed_proof},
            )
            state_check()
            docker.call(
                [
                    "exec",
                    ids[0],
                    "nginx",
                    "-t",
                    "-c",
                    "/etc/nginx/routes/candidate.conf",
                ]
            )
            state_check()
            private_io.write_file(
                routes_fd,
                "active.conf",
                candidate,
                replace=True,
                max_bytes=gateway.MAX_ROUTE_BYTES,
            )
            route_bytes = candidate
            state_check()
            docker.call(
                ["exec", ids[0], "nginx", "-t", "-c", "/etc/nginx/routes/active.conf"]
            )
            state_check()
            docker.call(
                [
                    "exec",
                    ids[0],
                    "nginx",
                    "-s",
                    "reload",
                    "-c",
                    "/etc/nginx/routes/active.conf",
                ]
            )
            publish(
                "routed.json",
                {
                    **identity,
                    "status": "route_reloaded",
                    "created_sha256": execution.digest_record(
                        activation_records["created.json"]
                    ),
                },
            )
            probe_fd = private_io.open_child_directory(
                activation_fd, "probe", create=True
            )
            state.callback(os.close, probe_fd)
            state_check()
            acceptance = _http_report(
                http_probe(
                    docker,
                    source_plan=source_plan,
                    image_inspection=images["web"],
                    image_proof=image_authority["images"]["web"],
                    revision=revision,
                    bundle_id=identity["bundle_id"],
                    manifest_sha256=identity["manifest_sha256"],
                    directory_fd=probe_fd,
                    directory_path=root / "activation/probe",
                    run_id=identity["probe_run_id"],
                ),
                revision=revision,
                bundle_id=identity["bundle_id"],
                manifest_sha256=identity["manifest_sha256"],
                run_id=identity["probe_run_id"],
            )
            probe_records = _probe_records(
                probe_fd,
                root / "activation/probe",
                acceptance,
                image_id=image_authority["images"]["web"]["image_id"],
            )
            state_check()
            _same(
                query.verify_authority(context, observe_runtime=routed.observe),
                authority,
            )
            _same(routed.last[1], routed_proof, "runtime_changed_during_acceptance")
            inference._gpu(
                config,
                inference_runtime.ownership_proofs(
                    gpu_compose, routed.last[0], gpu_images
                ),
                gpu_runner,
            )
            authority_check()
            routed.observe()
            _same(routed.last[1], routed_proof, "runtime_changed_during_acceptance")
            verify_client()
            transport.close()
        _same(query.read_authority(context), authority)
        _same(credentials.read_api_key_authority(config, store), key)
        state_check()
        result = {
            **identity,
            "status": "gateway_active",
            "proof": routed_proof,
            "http_acceptance": acceptance,
            "runtime_verified": True,
            "gateway_verified": True,
            "loopback_verified": True,
            "two_turn_search_verified": True,
            "session_reset_verified": True,
            "ports_published": True,
            "private_release_acceptance_verified": True,
            "search_quality_verified": False,
            "activation_performed": True,
            "confers_runtime_authority": True,
        }
        publish("complete.json", result)
        _require(
            set(activation_records) | {"probe"} == ACTIVATION_FILES, "state_changed"
        )
        state_check()
        pointer = {
            "schema_version": 1,
            "status": "gateway_active",
            "revision": revision,
            "project": composition.GATEWAY_PROJECT,
            "release_project": compose["name"],
            "bundle_id": identity["bundle_id"],
            "manifest_sha256": identity["manifest_sha256"],
            "activation_complete_sha256": execution.digest_record(result),
            "active_changed": True,
        }
        returned = {**result, "active_changed": True, "activation_performed": True}
        store.write("active.json", pointer)
        return returned
