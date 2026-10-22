"""Start a source-bound private CPU website against existing sealed services.

The lifecycle owns the product lock; this facade also holds the catalog lock.
There are no builds, pulls, query provisioning, GPU starts, public ports or
route changes. A partial Compose start remains quarantined for explicit repair.
Start/reuse performs full functional acceptance. Gateway preparation rechecks
completed authority and runtime without another start or model acceptance.
"""

from __future__ import annotations

from contextlib import ExitStack
import copy
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Callable

from search.operations.catalog import client_lease, execution, policy, query
from search.operations.config import ProductConfig
from search.operations.deployment import (
    composition,
    image_authority as image_evidence,
    credentials,
    image_build,
    inference,
    inference_runtime,
)
from search.operations.deployment.release import (
    probe as release_probe,
    runtime as release_runtime,
)
from search.operations.deployment import runtime_docker, source_bundle
from search.operations.shared import private_io
from search.operations.state import StateStore


SLOT = re.compile(r"release-[0-9a-f]{12}-[0-9a-f]{12}\Z")
FILES = {"started.json", "compose.json", "lease.json", "complete.json"}
PROOF_FIELDS = {"container_ids", "network_ids", "start_identity_sha256"}


class ReleaseError(ValueError):
    """Fixed operator errors without raw commands, inspect rows or secrets."""


def _same(actual, expected, code="release_authority_changed"):
    if private_io.canonical(actual) != private_io.canonical(expected):
        raise ReleaseError(code)


def _images(docker, source_plan, authority):
    images = {}
    for role in ("web", "conversation-api"):
        proof = authority["images"][role]
        image = docker.inspect(proof["image_id"])
        for inspected in (
            image,
            docker.inspect(image_build.TAGS[role] + source_plan["revision"]),
        ):
            _same(
                source_bundle.validate_built_image(
                    source_plan, role, proof["image_id"], inspected
                ),
                proof,
                "release_source_image_changed",
            )
        images[role] = image
    return images


def _sealed_inference(stack, config, store, compose, identity):
    parent = config.state_root / "inference"
    parent_fd = private_io.open_directory(parent)
    stack.callback(os.close, parent_fd)
    names = set(os.listdir(parent_fd))
    if len(names) > inference.MAX_SLOTS or any(
        inference.SLOT.fullmatch(name) is None for name in names
    ):
        raise ReleaseError("release_inference_inventory_changed")
    name = compose["name"].removeprefix("depositar-ai-")
    descriptor = private_io.open_child_directory(parent_fd, name)
    stack.callback(os.close, descriptor)
    complete = image_build._record(descriptor, "complete.json")

    def verify(proof):
        if set(os.listdir(descriptor)) != {
            "started.json",
            "compose.json",
            "complete.json",
        }:
            raise ReleaseError("release_inference_incomplete")
        _same(image_build._record(descriptor, "started.json"), identity)
        _same(image_build._record(descriptor, "compose.json"), compose)
        expected = {
            **identity,
            "status": "runtime_running",
            **{key: proof[key] for key in PROOF_FIELDS},
            "runtime_verified": True,
            "models_verified": True,
            "gpu_ownership_snapshot_verified": True,
            "model_warmup_verified": False,
            "website_started": False,
        }
        _same(complete, expected, "release_inference_receipt_changed")
        _same(image_build._record(descriptor, "complete.json"), complete)
        _same(store.read("inference-candidate.json"), complete)
        private_io.verify_directory(parent_fd, parent)
        private_io.verify_directory(descriptor, parent / name)

    # The initial check authenticates the receipt; a current runtime proof is
    # still mandatory before any query connection or Compose mutation.
    verify(complete)
    return verify


class _Host:
    def __init__(
        self,
        docker,
        context,
        catalog_authority,
        catalog_images,
        catalog_inspections,
        inference_compose,
        inference_images,
        verify_inference,
        compose,
        images,
        image_proof,
    ):
        self.docker, self.context = docker, context
        self.authority, self.catalog_images = catalog_authority, catalog_images
        self.catalog_inspections = catalog_inspections
        self.inference_compose, self.inference_images = (
            inference_compose,
            inference_images,
        )
        self.verify_inference = verify_inference
        self.compose, self.images, self.image_proof = compose, images, image_proof
        self.lease = None
        self.last = None

    def collect(self, *, clients):
        projects = [self.context["plan"]["project"], self.inference_compose["name"]]
        if clients:
            projects.append(self.compose["name"])
        expected_networks = {
            projects[0]: {projects[0] + "_search_backend"},
            projects[1]: {
                item["name"] for item in self.inference_compose["networks"].values()
            },
            self.compose["name"]: {self.compose["networks"]["website"]["name"]},
        }
        inventories = {}
        for project in projects:
            ids, names = runtime_docker.project_inventory(self.docker, project)
            if len(ids) != 3 or names != expected_networks[project]:
                raise ReleaseError("release_runtime_inventory_changed")
            inventories[project] = set(ids)
        all_ids = [value for ids in inventories.values() for value in ids]
        if len(set(all_ids)) != len(all_ids):
            raise ReleaseError("release_runtime_identity_collision")
        rows = image_build._json(self.docker.call(["inspect", *sorted(all_ids)]))
        if (
            type(rows) is not list
            or len(rows) != len(all_ids)
            or any(type(row) is not dict for row in rows)
            or {row.get("Id") for row in rows} != set(all_ids)
        ):
            raise ReleaseError("release_runtime_snapshot_invalid")
        names = sorted(set().union(*(expected_networks[p] for p in projects)))
        networks = image_build._json(self.docker.call(["network", "inspect", *names]))
        if (
            type(networks) is not list
            or len(networks) != len(names)
            or any(type(row) is not dict for row in networks)
            or {row.get("Name") for row in networks} != set(names)
        ):
            raise ReleaseError("release_network_snapshot_invalid")
        return (
            {
                project: [row for row in rows if row["Id"] in ids]
                for project, ids in inventories.items()
            },
            {row["Name"]: row for row in networks},
        )

    def observe(self, _context=None, *, snapshot=None):
        storage_proof = execution.require_storage(self.context)
        groups, networks = snapshot or self.collect(clients=self.lease is not None)
        catalog = groups[self.context["plan"]["project"]]
        execution.validate_storage_mounts(storage_proof, catalog)
        catalog_network = networks[self.context["plan"]["project"] + "_search_backend"]
        gpu = groups[self.inference_compose["name"]]
        gpu_networks = [
            networks[entry["name"]]
            for entry in self.inference_compose["networks"].values()
        ]
        if self.lease is None:
            for row in catalog:
                role = row["Config"]["Labels"]["com.docker.compose.service"]
                for key in ("Cmd", "Entrypoint"):
                    _same(
                        row["Config"].get(key),
                        self.catalog_inspections[role]["Config"].get(key),
                    )
            catalog_proof = policy.validate_runtime(
                self.context["plan"],
                self.context["compose"],
                self.catalog_images,
                catalog,
                catalog_network,
            )
            gpu_proof = inference_runtime.validate_runtime(
                self.inference_compose, self.inference_images, gpu, gpu_networks
            )
            release_proof = None
        else:
            clients = groups[self.compose["name"]]
            api = [
                row for row in clients if row["Id"] == self.lease["api_container_id"]
            ]
            if len(api) != 1:
                raise ReleaseError("release_api_identity_changed")
            arguments = {
                "catalog_identity": self.authority["catalog_identity"],
                "release_compose": self.compose,
                "image_proof": self.image_proof,
                "image_environment": self.images["conversation-api"]["Config"]["Env"],
                "api_container": api[0],
            }
            leased = client_lease.validate_leased_runtime(
                self.context["plan"],
                self.context["compose"],
                self.catalog_images,
                catalog,
                catalog_network,
                self.lease,
                core_image_inspections=self.catalog_inspections,
                **arguments,
            )
            catalog_proof = {
                "container_ids": leased["catalog_container_ids"],
                "network_id": leased["catalog_network_id"],
            }
            gpu_proof = inference_runtime.validate_leased_runtime(
                self.inference_compose,
                self.inference_images,
                gpu,
                gpu_networks,
                self.lease,
                catalog_network=catalog_network,
                **arguments,
            )
            release_proof = self._validate_release(clients, networks, catalog_network)
        _same(
            catalog_proof,
            {
                key: self.authority["runtime"][key]
                for key in ("container_ids", "network_id")
            },
            "release_catalog_runtime_changed",
        )
        self.verify_inference(gpu_proof)
        self.last = (gpu_proof, release_proof)
        return catalog_proof, catalog, catalog_network

    def _validate_release(self, clients, networks, catalog_network):
        return release_runtime.validate_runtime(
            self.compose,
            self.images,
            clients,
            list(networks.values()),
            self.lease,
            catalog_identity=self.authority["catalog_identity"],
            image_proof=self.image_proof,
            image_environment=self.images["conversation-api"]["Config"]["Env"],
            catalog_network=catalog_network,
        )

    def make_lease(self, snapshot):
        groups, networks = snapshot
        clients = groups[self.compose["name"]]
        api = [
            row
            for row in clients
            if row.get("Config", {}).get("Labels", {}).get("com.docker.compose.service")
            == "conversation-api"
        ]
        if len(api) != 1:
            raise ReleaseError("release_api_identity_changed")
        return {
            **self.authority["catalog_identity"],
            "lease_schema": 1,
            "phase": "private_clients",
            "release_project": self.compose["name"],
            "release_compose_sha256": execution.digest_record(self.compose),
            "revision": self.image_proof["revision"],
            "source_sha256": self.image_proof["source_sha256"],
            "api_container_id": api[0]["Id"],
            "api_image_id": self.image_proof["image_id"],
            "network_ids": {name: row["Id"] for name, row in networks.items()},
        }


def start_private_release(
    config,
    store,
    source_revision,
    catalog_plan,
    *,
    repo_root,
    source_runner=None,
    docker_runner=None,
    catalog_runner=None,
    gpu_runner=None,
):
    """Start exactly three CPU services or freshly verify completed private reuse."""
    return _run(
        _start,
        config,
        store,
        source_revision,
        catalog_plan,
        repo_root=repo_root,
        source_runner=source_runner,
        docker_runner=docker_runner,
        catalog_runner=catalog_runner,
        gpu_runner=gpu_runner,
    )


def verify_completed_private_release(
    config,
    store,
    source_revision,
    catalog_plan,
    *,
    repo_root,
    source_runner=None,
    docker_runner=None,
    catalog_runner=None,
    gpu_runner=None,
):
    """Recheck a completed release without starting services or model acceptance.

    The returned completion contains the original successful ML acceptance.
    This call freshly verifies its source, images, model files, query authority
    and leased runtime, including the existing denied query-write checks. It
    never creates a release, repairs state or repeats model/HTTP acceptance.
    """
    return _run(
        _verify_completed,
        config,
        store,
        source_revision,
        catalog_plan,
        repo_root=repo_root,
        source_runner=source_runner,
        docker_runner=docker_runner,
        catalog_runner=catalog_runner,
        gpu_runner=gpu_runner,
    )


def _run(
    operation,
    config,
    store,
    source_revision,
    catalog_plan,
    *,
    repo_root,
    source_runner,
    docker_runner,
    catalog_runner,
    gpu_runner,
):
    try:
        if (
            not isinstance(config, ProductConfig)
            or not isinstance(store, StateStore)
            or store.root != config.state_root
            or os.geteuid() == 0
        ):
            raise ReleaseError("release_product_state_invalid")
        repo_root = Path(repo_root)
        inference._source_revision(repo_root, source_revision, source_runner)
        context = execution.load_context(
            config, catalog_plan, repo_root=repo_root, runner=catalog_runner
        )
        with execution.operation_lock(context):
            return operation(
                config,
                store,
                source_revision,
                context,
                repo_root,
                source_runner,
                docker_runner,
                gpu_runner,
            )
    except ReleaseError:
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
        raise ReleaseError("release_verification_failed") from None


@dataclass(frozen=True)
class _ReleaseInputs:
    host: _Host
    identity: dict
    authority: dict
    key: dict
    verify_client: Callable[[], None]
    refresh: Callable[[], None]


def _runtime_inputs(
    stack,
    config,
    store,
    revision,
    context,
    repo,
    source_runner,
    docker_runner,
    deadline,
):
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
    # All acquired model/ancestry/client handles must exit successfully before
    # publication; no success survives a failed final FD/path verification.
    models, verify_models = stack.enter_context(
        inference._verified_models(config, deadline=deadline, include_embedding=True)
    )
    client = Path(
        stack.enter_context(
            tempfile.TemporaryDirectory(prefix="depositar-release-client-")
        )
    )
    client_fd = private_io.open_directory(client)
    stack.callback(os.close, client_fd)

    def verify_client():
        if os.listdir(client_fd):
            raise ReleaseError("release_client_configuration_changed")
        private_io.verify_directory(client_fd, client)

    verify_client()
    docker = runtime_docker.Docker(
        client,
        docker_runner,
        deadline=deadline,
        max_commands=128,
        verify_client=verify_client,
    )
    if (
        image_build._json(docker.call(["info", "--format", "{{json .OSType}}"]))
        != "linux"
    ):
        raise ReleaseError("release_daemon_unsupported")
    gpu_images = inference._images(docker, source_plan, gpu_authority)
    images = _images(docker, source_plan, image_authority)
    images["filtered-embedding"] = gpu_images["scope-model"]
    catalog_images = execution.load_images(context)
    catalog_inspections = {
        role: docker.inspect(proof["image_id"])
        for role, proof in catalog_images.items()
    }
    plans = composition.render_composition(
        config,
        context["plan"],
        composition.QueryBindingPaths.for_catalog(config, context["plan"]),
        revision,
        uid=os.geteuid(),
        gid=os.getegid(),
        inference_source_sha256=source_plan["services"]["reranker"]["source_sha256"],
    )
    gpu_compose, compose = (
        copy.deepcopy(plans["inference"]),
        copy.deepcopy(plans["release"]),
    )
    for role, image in gpu_images.items():
        gpu_compose["services"][role]["image"] = image["Id"]
    for role, image in images.items():
        compose["services"][role]["image"] = image["Id"]
        if role != "filtered-embedding":
            proof = image_authority["images"][role]
            compose["services"][role]["labels"] = {
                "org.opencontainers.image.revision": revision,
                "io.depositar.ai.source.revision": revision,
                "io.depositar.ai.source.service": role,
                "io.depositar.ai.source.sha256": proof["source_sha256"],
            }
    if b"$" in private_io.canonical(compose):
        raise ReleaseError("release_compose_interpolation_forbidden")
    gpu_identity = inference._runtime_identity(
        gpu_compose,
        {role: models[role] for role in ("api", "reranker")},
        gpu_authority,
        gpu_images,
    )
    verify_inference = _sealed_inference(
        stack, config, store, gpu_compose, gpu_identity
    )
    host = _Host(
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
    identity = {
        "schema_version": 1,
        "revision": revision,
        "project": compose["name"],
        "bundle_id": context["bundle"].bundle_id,
        "manifest_sha256": context["plan"]["manifest_sha256"],
        "compose_sha256": execution.digest_record(compose),
        "query_authority_sha256": execution.digest_record(authority),
        "api_key_authority_sha256": execution.digest_record(key),
        "source_manifest_sha256": image_authority["source_manifest_sha256"],
        "model_manifest_sha256": execution.digest_record(models),
        "image_ids": {role: image["Id"] for role, image in images.items()},
        "image_config_sha256": {
            role: execution.digest_record(image["Config"])
            for role, image in images.items()
        },
        "active_changed": False,
    }

    def authority_check():
        verify_models()
        inference._source_revision(repo, revision, source_runner)
        _same(query.read_authority(context), authority)
        _same(credentials.read_api_key_authority(config, store), key)
        _same(
            image_evidence.read_completed_images(config, store, source_plan),
            image_authority,
        )
        _same(execution.load_images(context), catalog_images)
        _same(
            inference._refresh_authority(config, store, source_plan, gpu_authority),
            gpu_authority,
        )
        _same(inference._images(docker, source_plan, gpu_authority), gpu_images)
        _same(
            _images(docker, source_plan, image_authority),
            {role: images[role] for role in ("web", "conversation-api")},
        )

    return _ReleaseInputs(
        host, identity, authority, key, verify_client, authority_check
    )


def _release_slots(parent_fd):
    names = set(os.listdir(parent_fd))
    if (
        any(SLOT.fullmatch(value) is None for value in names)
        or len(names) > inference.MAX_SLOTS
    ):
        raise ReleaseError("release_unknown_state")
    return names


def _load_completed(slot_fd, inputs):
    if set(os.listdir(slot_fd)) != FILES:
        raise ReleaseError("release_partial_state_recovery_required")
    _same(image_build._record(slot_fd, "started.json"), inputs.identity)
    _same(image_build._record(slot_fd, "compose.json"), inputs.host.compose)
    inputs.host.lease = image_build._record(slot_fd, "lease.json")
    return image_build._record(slot_fd, "complete.json")


def _verify_runtime(config, context, inputs, gpu_runner):
    host = inputs.host
    _same(
        query.verify_authority(context, observe_runtime=host.observe), inputs.authority
    )
    inference._gpu(
        config,
        inference_runtime.ownership_proofs(
            host.inference_compose, host.last[0], host.inference_images
        ),
        gpu_runner,
    )


def _completed_receipt(identity, proof):
    return {
        **identity,
        "status": "private_release_running",
        **proof,
        "runtime_verified": True,
        "models_verified": True,
        "query_permissions_verified": True,
        "gpu_ownership_snapshot_verified": True,
        "model_warmup_verified": True,
        "readiness_verified": True,
        "two_turn_search_verified": True,
        "session_reset_verified": True,
        "search_quality_verified": False,
        "gateway_verified": False,
        "ports_published": False,
    }


def _final_authority(
    config, store, revision, context, repo, source_runner, inputs, deadline
):
    # Model, ancestry and Docker client handles must have exited before this fence.
    _same(query.read_authority(context), inputs.authority)
    _same(credentials.read_api_key_authority(config, store), inputs.key)
    inference._source_revision(repo, revision, source_runner)
    if time.monotonic() >= deadline:
        raise ReleaseError("release_operation_deadline")
    store._check_directory()


def _verify_records(config, parent_fd, slot_fd, name, inputs, expected_files):
    parent = config.state_root / "releases"
    private_io.verify_directory(parent_fd, parent)
    private_io.verify_directory(slot_fd, parent / name)
    _same(image_build._record(slot_fd, "started.json"), inputs.identity)
    _same(image_build._record(slot_fd, "compose.json"), inputs.host.compose)
    _same(image_build._record(slot_fd, "lease.json"), inputs.host.lease)
    if set(os.listdir(slot_fd)) != expected_files:
        raise ReleaseError("release_state_inventory_changed")


def _verify_completed(
    config, store, revision, context, repo, source_runner, docker_runner, gpu_runner
):
    deadline = time.monotonic() + runtime_docker.TOTAL_SECONDS
    with ExitStack() as stack:
        inputs = _runtime_inputs(
            stack,
            config,
            store,
            revision,
            context,
            repo,
            source_runner,
            docker_runner,
            deadline,
        )
        host = inputs.host
        name = host.compose["name"].removeprefix("depositar-ai-")
        # Missing or partial releases are never repaired or started by this path.
        parent_fd = private_io.open_child_directory(store.fd, "releases")
        slot_fd = None
        try:
            if name not in _release_slots(parent_fd):
                raise ReleaseError("release_completed_required")
            slot_fd = private_io.open_child_directory(parent_fd, name)
            completed = _load_completed(slot_fd, inputs)
            _verify_runtime(config, context, inputs, gpu_runner)
            proof = host.last[1]
            result = _completed_receipt(inputs.identity, proof)
            _same(completed, result, "release_completed_runtime_changed")
            _same(store.read("release-candidate.json"), result)
            _verify_runtime(config, context, inputs, gpu_runner)
            _same(host.last[1], proof, "release_runtime_changed_during_verification")
            inputs.refresh()
            host.observe()
            _same(host.last[1], proof, "release_runtime_changed_during_verification")
            inputs.verify_client()
            stack.close()
            _final_authority(
                config, store, revision, context, repo, source_runner, inputs, deadline
            )
            _verify_records(config, parent_fd, slot_fd, name, inputs, FILES)
            _same(image_build._record(slot_fd, "complete.json"), result)
            _same(store.read("release-candidate.json"), result)
            return {**result, "runtime_reused": True}
        finally:
            if slot_fd is not None:
                os.close(slot_fd)
            os.close(parent_fd)


def _start(
    config, store, revision, context, repo, source_runner, docker_runner, gpu_runner
):
    deadline = time.monotonic() + runtime_docker.TOTAL_SECONDS
    # All acquired model/ancestry/client handles must exit before publication.
    with ExitStack() as stack:
        inputs = _runtime_inputs(
            stack,
            config,
            store,
            revision,
            context,
            repo,
            source_runner,
            docker_runner,
            deadline,
        )
        host, identity = inputs.host, inputs.identity
        docker, compose = host.docker, host.compose
        gpu_compose, gpu_images = host.inference_compose, host.inference_images
        name = compose["name"].removeprefix("depositar-ai-")
        parent = config.state_root / "releases"
        parent_fd = private_io.open_child_directory(
            store.fd, "releases", create="releases" not in os.listdir(store.fd)
        )
        # These state descriptors outlive the IO/model stack below.
        slot_fd = None
        try:
            names = _release_slots(parent_fd)
            reused = name in names
            if reused:
                slot_fd = private_io.open_child_directory(parent_fd, name)
                completed = _load_completed(slot_fd, inputs)
            else:
                if len(names) >= inference.MAX_SLOTS:
                    raise ReleaseError("release_retention_limit")
                try:
                    prior = store.read("release-candidate.json")
                except FileNotFoundError:
                    prior = None
                if type(prior) is dict and prior.get("project") == compose["name"]:
                    raise ReleaseError("release_partial_state_recovery_required")
                if any(runtime_docker.project_inventory(docker, compose["name"])):
                    raise ReleaseError("release_existing_runtime_not_adopted")
            _verify_runtime(config, context, inputs, gpu_runner)

            if not reused:
                inputs.refresh()
                private_io.verify_directory(parent_fd, parent)
                slot_fd = private_io.open_child_directory(parent_fd, name, create=True)
                for filename, value in (
                    ("started.json", identity),
                    ("compose.json", compose),
                ):
                    private_io.write_json(
                        slot_fd, filename, value, max_bytes=image_build.MAX_RECORD_BYTES
                    )
                prefix = [
                    "compose",
                    "--env-file",
                    "/dev/null",
                    "--project-directory",
                    str(parent / name),
                    "--project-name",
                    compose["name"],
                    "--file",
                    str(parent / name / "compose.json"),
                ]
                docker.call([*prefix, "config", "--quiet"])
                _same(image_build._record(slot_fd, "started.json"), identity)
                _same(image_build._record(slot_fd, "compose.json"), compose)
                host.observe()
                inference._gpu(
                    config,
                    inference_runtime.ownership_proofs(
                        gpu_compose, host.last[0], gpu_images
                    ),
                    gpu_runner,
                )
                if any(runtime_docker.project_inventory(docker, compose["name"])):
                    raise ReleaseError("release_existing_runtime_not_adopted")
                private_io.verify_directory(parent_fd, parent)
                private_io.verify_directory(slot_fd, parent / name)
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
                        "600",
                        *sorted(release_runtime.SERVICES),
                    ],
                    timeout=660,
                )
                snapshot = host.collect(clients=True)
                host.lease = host.make_lease(snapshot)
                host.observe(snapshot=snapshot)
                private_io.write_json(
                    slot_fd,
                    "lease.json",
                    host.lease,
                    max_bytes=image_build.MAX_RECORD_BYTES,
                )
            else:
                host.observe()
            proof = host.last[1]
            result = _completed_receipt(identity, proof)
            if reused:
                _same(completed, result, "release_completed_runtime_changed")
                _same(store.read("release-candidate.json"), result)
            try:
                probe = release_probe.verify_private_release(
                    docker,
                    proof["container_ids"],
                    revision=revision,
                    bundle_id=identity["bundle_id"],
                    manifest_sha256=identity["manifest_sha256"],
                )
            except release_probe.ReleaseProbeError:
                raise ReleaseError("release_acceptance_probe_failed") from None
            for field in (
                "model_warmup_verified",
                "readiness_verified",
                "two_turn_search_verified",
                "session_reset_verified",
            ):
                if probe.get(field) is not True:
                    raise ReleaseError("release_acceptance_blocked")
            if (
                probe.get("status") != "ready"
                or probe.get("search_quality_verified") is not False
                or probe.get("gateway_verified") is not False
            ):
                raise ReleaseError("release_acceptance_invalid")
            _same(
                query.verify_authority(context, observe_runtime=host.observe),
                inputs.authority,
            )
            _same(host.last[1], proof, "release_runtime_changed_during_acceptance")
            inference._gpu(
                config,
                inference_runtime.ownership_proofs(
                    gpu_compose, host.last[0], gpu_images
                ),
                gpu_runner,
            )
            inputs.refresh()
            host.observe()
            _same(host.last[1], proof, "release_runtime_changed_during_acceptance")
            inputs.verify_client()
            # Exit every model/source handle before a completion can be written.
            stack.close()
            _final_authority(
                config, store, revision, context, repo, source_runner, inputs, deadline
            )
            expected_files = FILES if reused else FILES - {"complete.json"}
            _verify_records(config, parent_fd, slot_fd, name, inputs, expected_files)
            if reused:
                _same(image_build._record(slot_fd, "complete.json"), result)
                _same(store.read("release-candidate.json"), result)
            else:
                private_io.write_json(
                    slot_fd,
                    "complete.json",
                    result,
                    max_bytes=image_build.MAX_RECORD_BYTES,
                )
                store.write("release-candidate.json", result, replace=True)
            return {**result, "runtime_reused": reused}
        finally:
            if slot_fd is not None:
                os.close(slot_fd)
            os.close(parent_fd)
