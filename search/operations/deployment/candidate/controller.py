"""One reviewed CPU candidate sharing a sealed live inference/catalog plane.

The operator owns a separate journal and frozen source worktree. It never
starts, warms, repairs or stops GPU/catalog services, nor changes the live
route. Old single-client entry points stay strict; this explicit phase verifies
both complete clients against the actual unfiltered networks.
"""

from contextlib import contextmanager
from dataclasses import replace
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from search.operations import config as product_config, lifecycle
from search.operations.deployment import composition
from search.operations.deployment.images import build as image_build
from search.operations.deployment.provider import (
    authority as provider_authority,
)
from search.operations.deployment.provider import runtime as provider_runtime
from search.operations.deployment.provider.contract import ProviderError
from search.operations.deployment.release import (
    controller as release_controller,
)
from search.operations.deployment.release import probe as release_probe
from search.operations.deployment.release import runtime
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
from search.operations.state import StateStore
from search.planning.contract import checked_query
from search.serialization import strict_json
from . import catalog
from .policy import (
    SharedRequestPolicyError,
    validate_shared_request_policy,
)
from .probe import SharedBackendProbeError


ROOT = Path(__file__).resolve().parents[4]
FIELDS = {
    "schema_version",
    "state_root",
    "control_config",
    "catalog_config",
    "catalog_source",
}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
ROLES = frozenset({"web", "conversation-api", "filtered-embedding"})
MAX_QUERY_INPUT_BYTES = 4096
QUERY_INPUT_SECONDS = 30


class SharedBackendError(ValueError):
    """Closed diagnostics, without paths, keys or child output."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Parser diagnostics may repeat rejected query arguments verbatim.
        del message
        self.exit(2, '{"error":"shared_backend_arguments_invalid"}\n')


def require(value, code):
    if not value:
        raise SharedBackendError("shared_backend_" + code)


def digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def path(value):
    require(
        type(value) is str
        and value.isascii()
        and not any(ord(c) < 32 for c in value),
        "path_invalid",
    )
    p = Path(value)
    require(p.is_absolute() and p.resolve() == p, "path_invalid")
    return p


def load_config(filename):
    filename = path(str(filename))
    fd = private_io.open_directory(filename.parent)
    try:
        value = strict_json(
            private_io.read_file(
                fd, filename.name, mode=0o600, max_bytes=16384
            )
        )
    finally:
        os.close(fd)
    require(
        type(value) is dict
        and set(value) == FIELDS
        and type(value["schema_version"]) is int
        and value["schema_version"] == 2,
        "config_invalid",
    )
    for key in FIELDS - {"schema_version"}:
        value[key] = path(value[key])
    root = value["state_root"]
    require(
        not any((p / ".git").exists() for p in (root, *root.parents)),
        "state_inside_git",
    )
    inputs = [value[k] for k in FIELDS - {"schema_version", "state_root"}]
    require(
        all(root != p and root not in p.parents for p in inputs),
        "state_overlaps_inputs",
    )
    control = provider_authority.load_config(value["control_config"])
    product = product_config.load_config(
        value["catalog_config"], repo_root=value["catalog_source"]
    )
    protected = (
        control["state_root"],
        product.state_root,
        product.api_model_root,
        product.embedding_model_root,
        product.reranker_model_root,
    )
    require(
        all(
            root != p and root not in p.parents and p not in root.parents
            for p in protected
        ),
        "state_overlaps_production",
    )
    return value


@contextmanager
def controlled(profile):
    """Keep canonical control outside every candidate journal lock."""
    with provider_authority.open_control(profile["control_config"]) as control:
        yield {**profile, "_control": control}


def control_for(profile):
    require("_control" in profile, "control_lock_required")
    control = profile["_control"]
    control.refresh()
    return control


def require_binding(control, record):
    require(
        type(record["roles_epoch"]) is int
        and 1 <= record["roles_epoch"] < 2**63
        and record["provider_authority_sha256"] == control.provider_sha256
        and record["roles_sha256"] == control.roles_sha256
        and record["roles_epoch"] == control.roles["epoch"],
        "live_binding_changed",
    )


@contextmanager
def journal(root):
    fd = private_io.open_directory(root)
    lock = None
    try:
        lock = os.open(
            "operations.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        row = os.fstat(lock)
        require(
            stat.S_ISREG(row.st_mode)
            and row.st_uid == os.geteuid()
            and row.st_nlink == 1
            and stat.S_IMODE(row.st_mode) == 0o600,
            "lock_unsafe",
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SharedBackendError("shared_backend_busy") from None
        named = os.stat("operations.lock", dir_fd=fd, follow_symlinks=False)
        require(
            (row.st_dev, row.st_ino) == (named.st_dev, named.st_ino),
            "lock_changed",
        )
        store = StateStore(root, fd)
        yield store
        store._check_directory()
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


def current(store):
    try:
        value = store.read("current.json")
    except FileNotFoundError:
        return None
    require(
        type(value) is dict
        and type(value.get("schema_version")) is int
        and value.get("schema_version") == 2
        and value.get("phase")
        in {
            "preparing",
            "prepared",
            "creating",
            "created",
            "starting",
            "running",
            "failed",
            "stopped",
        },
        "journal_invalid",
    )
    require(
        type(value.get("revision")) is str
        and HEX40.fullmatch(value["revision"]),
        "journal_invalid",
    )
    require(
        type(value.get("container_ids")) is dict
        and set(value["container_ids"]) <= ROLES
        and all(
            type(v) is str and re.fullmatch(r"[0-9a-f]{64}", v)
            for v in value["container_ids"].values()
        ),
        "journal_invalid",
    )
    if value["phase"] in {"prepared", "created", "starting", "running"}:
        require(
            all(
                type(value.get(k)) is str
                and re.fullmatch(r"[0-9a-f]{64}", value[k])
                for k in (
                    "provider_authority_sha256",
                    "roles_sha256",
                    "model_file_identity_sha256",
                    "compute_sha256",
                )
            ),
            "journal_invalid",
        )
        require(
            type(value.get("roles_epoch")) is int
            and 1 <= value["roles_epoch"] < 2**63,
            "journal_invalid",
        )
    return value


def save(store, value, phase=None):
    if phase is not None:
        value["phase"] = phase
    store.write("current.json", value, replace=True)


def git(source, *arguments):
    result = image_build._run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(source),
            *arguments,
        ],
        timeout=60,
        environment={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "GIT_NO_REPLACE_OBJECTS": "1",
        },
    )
    require(result.returncode == 0, "source_operation_failed")
    return result.stdout


def summary(record):
    return {
        "status": record["phase"],
        "revision": record["revision"],
        "models_restarted": False,
        "public_route_changed": False,
    }


def frozen_source(profile, revision):
    require(
        type(revision) is str and HEX40.fullmatch(revision), "revision_invalid"
    )
    return profile["state_root"] / "sources" / revision


def build_root(profile, revision):
    require(
        type(revision) is str and HEX40.fullmatch(revision), "revision_invalid"
    )
    return profile["state_root"] / "builds" / revision


@contextmanager
def live(profile, record=None):
    """Authenticate the permanent provider and current registered backends."""
    control = control_for(profile)
    if record is not None:
        require_binding(control, record)
    with provider_runtime.open_provider(control) as provider:
        bindings = [backend.catalog for backend in provider.backends]
        selected = catalog.select(
            profile, provider.stack, provider.config, bindings, provider.docker
        )
        session = Session(profile, provider, control, selected)
        yield session
        selected.refresh()
        control.refresh()


class Session:
    def __init__(self, profile, provider, control, selected):
        self.profile, self.provider, self.control = profile, provider, control
        self.config = provider.config
        self.baseline_catalog = provider.backends[0].catalog
        self.context = self.baseline_catalog.context
        self.query_catalog = selected
        self.query_context = selected.context
        self.query_authority = selected.authority
        self.docker = provider.docker
        self.candidate = None
        self.record = None
        self.last_candidate = None
        self._candidate_image_binding = None

    def members(self):
        return [*self.provider.backends] + (
            [self.candidate] if self.candidate is not None else []
        )

    def catalogs(self):
        by_project = {
            host.catalog.context["plan"]["project"]: host.catalog
            for host in self.provider.backends
        }
        by_project[self.query_context["plan"]["project"]] = self.query_catalog
        return list(by_project.values())

    def observe(self, _context=None):
        snapshot = self.provider.observe(self.members(), self.catalogs())
        self.provider._checkpoint_authorities()
        self.catalog_snapshots = snapshot["catalog_snapshots"]
        if self.candidate is not None:
            own = self.candidate.compose
            rows = snapshot["groups"][own["name"]]
            require(
                {row["Id"] for row in rows}
                == set(self.record["container_ids"].values()),
                "candidate_inventory_changed",
            )
            proof = snapshot["backend_proofs"][own["name"]]
            if self.record.get("runtime") is not None:
                release_controller._same(
                    proof,
                    self.record["runtime"],
                    "shared_backend_candidate_runtime_changed",
                )
            self.last_candidate = proof
            web = next(
                row
                for row in rows
                if row["Id"] == self.record["container_ids"]["web"]
            )
            self.candidate_endpoint = web["NetworkSettings"]["Networks"][
                own["networks"]["website"]["name"]
            ]["IPAddress"]
        self.control.refresh()
        return self.catalog_snapshots[self.context["plan"]["project"]]

    def verify(self, policy, expected_compute=None):
        self.observe()
        snapshots = self.catalog_snapshots
        for binding in self.catalogs():
            binding.audit(
                snapshots[binding.context["plan"]["project"]],
                timeout=self.remaining(180),
            )
        self.observe()
        from search.operations.catalog.connection import snapshot_peers

        for binding in self.catalogs():
            project = binding.context["plan"]["project"]
            release_controller._same(
                snapshot_peers(
                    binding.context,
                    binding.authority["runtime"],
                    snapshots[project],
                ),
                snapshot_peers(
                    binding.context,
                    binding.authority["runtime"],
                    self.catalog_snapshots[project],
                ),
            )
        return self.check_models(policy, expected_compute)

    def remaining(self, maximum, *, deadline=None):
        limit = self.docker.deadline
        if deadline is not None:
            limit = min(limit, deadline)
        left = limit - time.monotonic()
        require(left > 0, "batch_deadline_exceeded")
        return min(maximum, left)

    def check_models(self, policy, expected_compute=None, *, deadline=None):
        resident = self.provider.check_models(
            policy,
            expected_compute,
            deadline=deadline,
            api_container_id=self.provider.backends[0].lease[
                "api_container_id"
            ],
        )
        self.compute_sha256 = self.provider.compute_sha256
        self.control.refresh()
        return resident

    def candidate_inputs(self, record):
        source = frozen_source(self.profile, record["revision"])
        require(
            require_clean_revision(
                source, **getattr(self.provider, "source_options", {})
            )
            == record["revision"],
            "candidate_source_changed",
        )
        policy = validate_shared_request_policy(
            self.provider.source_root, source
        )
        release_controller._same(policy, record["policy"])
        release_controller._same(
            catalog.query_profile(source),
            self.query_context["query_profile"],
            "shared_backend_catalog_query_profile_changed",
        )
        release_controller._same(
            {
                key: self.query_context["query_profile"][key]
                for key in ("MODEL", "MODEL_DIGEST", "DIMENSIONS")
            },
            {
                key: self.context["query_profile"][key]
                for key in ("MODEL", "MODEL_DIGEST", "DIMENSIONS")
            },
            "shared_backend_catalog_embedding_provider_changed",
        )
        if record["phase"] != "preparing":
            require("catalog_binding" in record, "catalog_selection_missing")
            release_controller._same(
                record["catalog_binding"],
                self.query_catalog.identity(),
                "shared_backend_catalog_selection_changed",
            )
        config = replace(
            self.config,
            state_root=build_root(self.profile, record["revision"]),
        )
        binding = self.provider._verified_backend_images(
            config, source, record["revision"]
        )
        images = binding.images
        plans = composition.render_composition(
            self.query_catalog.config,
            self.query_context["plan"],
            composition.QueryBindingPaths.for_catalog(
                self.query_catalog.config, self.query_context["plan"]
            ),
            record["revision"],
            uid=os.geteuid(),
            gid=os.getegid(),
            inference_source_sha256=self.provider.inference_compose[
                "x-depositar-plan"
            ]["inference_source_sha256"],
        )
        gpu = copy.deepcopy(plans["inference"])
        for role, value in self.provider.inference_images.items():
            gpu["services"][role]["image"] = value["Id"]
        release_controller._same(
            gpu,
            self.provider.inference_compose,
            "shared_backend_model_recipe_changed",
        )
        compose = copy.deepcopy(plans["release"])
        for role, image in images.items():
            compose["services"][role]["image"] = image["Id"]
            if role != "filtered-embedding":
                labels = image["Config"]["Labels"]
                compose["services"][role]["labels"] = {
                    key: labels[key]
                    for key in (
                        "org.opencontainers.image.revision",
                        "io.depositar.ai.source.revision",
                        "io.depositar.ai.source.service",
                        "io.depositar.ai.source.sha256",
                    )
                }
        require(
            all(
                compose["name"] != host.compose["name"]
                for host in self.provider.backends
            ),
            "candidate_is_live",
        )
        self._candidate_image_binding = binding
        return compose, images, binding.proof

    def check_binding(self, record):
        self.control.refresh()
        require_binding(self.control, record)

    def attach_record(self, record):
        compose, images, proof = self.candidate_inputs(record)
        release_controller._same(compose, record["compose"])
        self.check_binding(record)
        candidate = copy.copy(self.provider.backends[0])
        candidate.compose, candidate.images, candidate.image_proof = (
            compose,
            images,
            proof,
        )
        candidate.context = self.query_context
        candidate.authority = self.query_authority
        candidate.catalog = self.query_catalog
        candidate.config = self.query_catalog.config
        candidate.config_file = self.query_catalog.config_file
        candidate.key = self.query_catalog.key
        candidate.catalog_images = self.query_catalog.images
        candidate.catalog_inspections = self.query_catalog.inspections
        candidate.lease = record["lease"]
        candidate.expected_runtime = record.get("runtime")
        candidate.source_root = frozen_source(self.profile, record["revision"])
        candidate.source_revision = record["revision"]
        candidate.record = None
        candidate.backend_sha256 = None
        candidate.namespace_sha256 = None
        candidate.image_binding = self._candidate_image_binding
        self.candidate, self.record = candidate, record


def disposable(profile, record):
    """Check canonical lifetime ownership before touching candidate assets."""
    control = control_for(profile)
    containers = list(record.get("container_ids", {}).values())
    networks = [record["network_id"]] if record.get("network_id") else []
    owned = [
        str(frozen_source(profile, record["revision"])),
        str(build_root(profile, record["revision"])),
    ]
    control.require_disposable(
        {
            "container_ids": containers,
            "network_ids": networks,
            "owned_paths": owned,
        }
    )


def prepare(profile, store, source):
    prior = current(store)
    require(
        prior is None or prior["phase"] == "stopped",
        "candidate_already_registered",
    )
    control = control_for(profile)
    require(
        control.roles["rollback_backend_sha256"] is None,
        "candidate_capacity_exhausted",
    )
    source = path(str(source))
    revision = require_clean_revision(source)
    disposable(profile, {"revision": revision})
    provider_source = Path(control.provider["source"]["root"])
    require(
        require_clean_revision(provider_source)
        == control.provider["source"]["revision"],
        "provider_source_changed",
    )
    policy = validate_shared_request_policy(provider_source, source)
    prod = provider_runtime.configuration(
        control.provider["configuration"], provider_source
    )
    frozen = frozen_source(profile, revision)
    for folder in (frozen.parent, build_root(profile, revision).parent):
        os.close(private_io.open_directory(folder, create=True))
    require(not frozen.exists(), "source_cache_exists")
    record = {
        "schema_version": 2,
        "phase": "preparing",
        "revision": revision,
        "source_origin": str(source),
        "policy": policy,
        "container_ids": {},
        "lease": None,
        "build_complete": False,
    }
    save(store, record)
    git(source, "worktree", "add", "--detach", str(frozen), revision)
    frozen.chmod(0o700)
    build = replace(prod, state_root=build_root(profile, revision))
    lifecycle.build_images(build, repo_root=frozen)
    record["build_complete"] = True
    save(store, record)
    with live(profile) as session:
        session.verify(policy["policy"])
        compose, _images, _proof = session.candidate_inputs(record)
        require(
            not any(
                release_controller.runtime_docker.project_inventory(
                    session.docker, compose["name"]
                )
            ),
            "candidate_project_exists",
        )
        record.update(
            {
                "compose": compose,
                "catalog_binding": session.query_catalog.identity(),
                "provider_authority_sha256": control.provider_sha256,
                "roles_sha256": control.roles_sha256,
                "roles_epoch": control.roles["epoch"],
                (
                    "model_file_identity_sha256"
                ): session.provider.model_file_identity_sha256,
                "compute_sha256": session.compute_sha256,
                "network_id": None,
                "external_network_ids": {
                    v["name"]: (
                        session.query_authority["runtime"]["network_id"]
                        if key == "catalog"
                        else session.provider.backends[0].lease["network_ids"][
                            v["name"]
                        ]
                    )
                    for key, v in compose["networks"].items()
                    if key != "website"
                },
            }
        )
        require(
            type(record["model_file_identity_sha256"]) is str
            and re.fullmatch(
                r"[0-9a-f]{64}", record["model_file_identity_sha256"]
            ),
            "model_verification_cache_missing",
        )
    # Completed model/source FD checks must succeed before the cache is usable.
    save(store, record, "prepared")
    return summary(record)


def candidate_rows(docker, record, compose_file):
    compose = record["compose"]
    ids, names = release_controller.runtime_docker.project_inventory(
        docker, compose["name"]
    )
    require(
        names <= {compose["networks"]["website"]["name"]},
        "candidate_inventory_changed",
    )
    if not ids:
        return [], names
    rows = image_build._json(docker.call(["inspect", *sorted(ids)]))
    require(
        type(rows) is list
        and len(rows) == len(ids) <= 3
        and {r["Id"] for r in rows} == set(ids),
        "candidate_inventory_changed",
    )
    observed = {}
    for row in rows:
        labels = row["Config"].get("Labels") or {}
        role = labels.get("com.docker.compose.service")
        require(
            role in ROLES
            and role not in observed
            and labels.get("com.docker.compose.project") == compose["name"],
            "candidate_identity_changed",
        )
        require(
            labels.get("com.docker.compose.project.config_files")
            == str(compose_file),
            "candidate_launcher_changed",
        )
        require(
            labels.get("com.docker.compose.project.working_dir")
            == str(compose_file.parent),
            "candidate_launcher_changed",
        )
        if record["container_ids"]:
            require(
                row["Id"] == record["container_ids"].get(role),
                "candidate_identity_changed",
            )
        service = compose["services"][role]
        require(
            row.get("Image") == service["image"]
            and row["Config"].get("Image") == service["image"]
            and all(
                labels.get(k) == v
                for k, v in service.get("labels", {}).items()
            ),
            "candidate_image_changed",
        )
        runtime._runtime_resources(
            row,
            service,
            (2, 2 * 1024**3, 96)
            if role == "conversation-api"
            else runtime.RESOURCE_PROFILES[role],
        )
        runtime._mounts(row, service)
        host = row["HostConfig"]
        require(
            host.get("PidMode") in ("", "default")
            and host.get("IpcMode") in ("", "private")
            and not host.get("PortBindings")
            and host.get("NetworkMode")
            in {compose["networks"][k]["name"] for k in service["networks"]},
            "candidate_capabilities_changed",
        )
        require(
            set(row["NetworkSettings"].get("Networks") or {})
            <= {compose["networks"][k]["name"] for k in service["networks"]},
            "candidate_network_changed",
        )
        for name, attachment in (
            row["NetworkSettings"].get("Networks") or {}
        ).items():
            expected = record.get("external_network_ids", {}).get(name)
            if name == compose["networks"]["website"]["name"]:
                expected = record.get("network_id")
            if expected is not None and attachment.get("NetworkID"):
                require(
                    attachment["NetworkID"] == expected,
                    "candidate_network_changed",
                )
        observed[role] = row["Id"]
    if record["container_ids"]:
        require(
            set(observed) <= set(record["container_ids"]),
            "candidate_inventory_changed",
        )
    return rows, names


def start(profile, store, record):
    require(
        record is not None and record["phase"] in {"prepared", "running"},
        "candidate_not_prepared",
    )
    disposable(profile, record)
    with live(profile, record) as session:
        if record["phase"] == "running":
            session.attach_record(record)
            session.verify(
                record["policy"]["policy"], record["compute_sha256"]
            )
            return summary(record)
        session.verify(record["policy"]["policy"], record["compute_sha256"])
        compose, images, api_proof = session.candidate_inputs(record)
        release_controller._same(compose, record["compose"])
        session.check_binding(record)
        require(
            not any(
                release_controller.runtime_docker.project_inventory(
                    session.docker, compose["name"]
                )
            ),
            "candidate_project_exists",
        )
        store.write("compose.json", compose, replace=True)
        prefix = [
            "compose",
            "--env-file",
            "/dev/null",
            "--project-name",
            compose["name"],
            "-f",
            str(profile["state_root"] / "compose.json"),
        ]
        save(store, record, "creating")
        try:
            session.docker.call(
                [*prefix, "create", "--no-build", "--pull", "never"],
                timeout=180,
            )
            rows, _ = candidate_rows(
                session.docker, record, profile["state_root"] / "compose.json"
            )
            require(len(rows) == 3, "candidate_creation_incomplete")
            website = compose["networks"]["website"]["name"]
            net = image_build._json(
                session.docker.call(["network", "inspect", website])
            )
            require(
                len(net) == 1
                and net[0].get("Labels", {}).get("com.docker.compose.project")
                == compose["name"],
                "candidate_network_changed",
            )
            record["network_id"] = net[0]["Id"]
            record["container_ids"] = {
                r["Config"]["Labels"]["com.docker.compose.service"]: r["Id"]
                for r in rows
            }
            disposable(profile, record)
            save(store, record, "created")
            session.docker.call(
                [*prefix, "start", "--wait", "--wait-timeout", "180"],
                timeout=210,
            )
            _groups, networks = catalog.collect(
                session.docker,
                [
                    (
                        binding.context["plan"]["project"],
                        {
                            binding.context["plan"]["project"]
                            + "_search_backend"
                        },
                    )
                    for binding in session.catalogs()
                ]
                + [
                    (
                        session.provider.inference_compose["name"],
                        {
                            v["name"]
                            for v in session.provider.inference_compose[
                                "networks"
                            ].values()
                        },
                    )
                ],
            )
            catalog_name = compose["networks"]["catalog"]["name"]
            if catalog_name not in networks:
                observed = image_build._json(
                    session.docker.call(["network", "inspect", catalog_name])
                )
                require(
                    type(observed) is list and len(observed) == 1,
                    "catalog_network_changed",
                )
                networks[catalog_name] = observed[0]
            ids, _ = release_controller.runtime_docker.project_inventory(
                session.docker, compose["name"]
            )
            rows = image_build._json(
                session.docker.call(["inspect", *sorted(ids)])
            )
            website = compose["networks"]["website"]["name"]
            networks[website] = image_build._json(
                session.docker.call(["network", "inspect", website])
            )[0]
            api = next(
                r
                for r in rows
                if r["Id"] == record["container_ids"]["conversation-api"]
            )
            record["lease"] = {
                **catalog.client_identity(
                    session.query_catalog, record["revision"]
                ),
                "lease_schema": 1,
                "phase": "private_clients",
                "release_project": compose["name"],
                "release_compose_sha256": digest(compose),
                "revision": record["revision"],
                "source_sha256": api_proof["source_sha256"],
                "api_container_id": api["Id"],
                "api_image_id": api_proof["image_id"],
                "network_ids": {
                    v["name"]: networks[v["name"]]["Id"]
                    for v in compose["networks"].values()
                },
            }
            save(store, record, "starting")
            session.attach_record(record)
            session.verify(
                record["policy"]["policy"], record["compute_sha256"]
            )
            ready = release_probe.verify_private_release(
                session.docker,
                record["container_ids"],
                revision=record["revision"],
                bundle_id=record["lease"]["bundle_id"],
                manifest_sha256=record["lease"]["manifest_sha256"],
                search=False,
            )
            require(ready["readiness_verified"] is True, "candidate_not_ready")
            session.verify(
                record["policy"]["policy"], record["compute_sha256"]
            )
            record["runtime"] = session.last_candidate
            save(store, record, "starting")
        except BaseException:
            save(store, record, "failed")
            raise
    save(store, record, "running")
    return summary(record)


def stop(profile, store, record):
    require(record is not None, "candidate_not_registered")
    disposable(profile, record)
    if record["phase"] == "stopped":
        return summary(record)
    from search.operations.deployment.runtime_docker import Docker

    with tempfile.TemporaryDirectory(
        prefix="shared-backend-stop-"
    ) as temporary:
        docker = Docker(
            temporary, None, deadline=time.monotonic() + 180, max_commands=32
        )
        if "compose" in record:
            rows, names = candidate_rows(
                docker, record, profile["state_root"] / "compose.json"
            )
            if not record["container_ids"] and record["phase"] in {
                "creating",
                "failed",
            }:
                # A killed Compose client does not prove daemon cancellation.
                # Never discard an uncertain intent on an empty/partial view.
                require(len(rows) == 3, "partial_creation_unconfirmed")
                record["container_ids"] = {
                    r["Config"]["Labels"]["com.docker.compose.service"]: r[
                        "Id"
                    ]
                    for r in rows
                }
                disposable(profile, record)
                save(store, record)
            owned_networks = []
            for name in names:
                networks = image_build._json(
                    docker.call(["network", "inspect", name])
                )
                require(
                    len(networks) == 1
                    and networks[0]
                    .get("Labels", {})
                    .get("com.docker.compose.project")
                    == record["compose"]["name"],
                    "candidate_network_changed",
                )
                if record.get("network_id") is None:
                    require(bool(rows), "candidate_network_unsealed")
                    record["network_id"] = networks[0]["Id"]
                    disposable(profile, record)
                    save(store, record)
                require(
                    networks[0]["Id"] == record["network_id"]
                    and set(networks[0].get("Containers") or {})
                    <= {r["Id"] for r in rows},
                    "candidate_network_changed",
                )
                owned_networks.append(networks[0]["Id"])
            for row in rows:
                disposable(profile, record)
                docker.call(["rm", "--force", row["Id"]], timeout=30)
            for identifier in owned_networks:
                rows = image_build._json(
                    docker.call(["network", "inspect", identifier])
                )
                require(
                    len(rows) == 1
                    and rows[0].get("Id") == identifier
                    and not rows[0].get("Containers")
                    and rows[0]
                    .get("Labels", {})
                    .get("com.docker.compose.project")
                    == record["compose"]["name"],
                    "candidate_network_changed",
                )
                disposable(profile, record)
                docker.call(["network", "rm", rows[0]["Id"]])
    disposable(profile, record)
    source = frozen_source(profile, record["revision"])
    if source.exists():
        require(
            require_clean_revision(source) == record["revision"],
            "candidate_source_changed",
        )
        common = (
            git(
                source,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
            .decode()
            .strip()
        )
        result = image_build._run(
            ["git", "--git-dir", common, "worktree", "remove", str(source)],
            timeout=60,
            environment={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
            },
        )
        require(result.returncode == 0, "source_operation_failed")
    build = build_root(profile, record["revision"])
    if build.exists() and record.get("build_complete") is True:
        require(
            build.resolve() == build and build.stat().st_uid == os.geteuid(),
            "build_cache_unsafe",
        )
        disposable(profile, record)
        shutil.rmtree(build)
    save(store, record, "stopped")
    return summary(record)


def operate(
    filename, operation, *, source=None, text=None, batch_identity=False
):
    require(
        operation in {"prepare", "start", "status", "query", "stop"},
        "operation_invalid",
    )
    require(
        type(batch_identity) is bool
        and (not batch_identity or operation == "status"),
        "batch_identity_operation_invalid",
    )
    operator_revision = (
        require_clean_revision(ROOT) if batch_identity else None
    )
    profile = load_config(filename)
    with (
        controlled(profile) as profile,
        journal(profile["state_root"]) as store,
    ):
        record = current(store)
        if operation == "prepare":
            return prepare(profile, store, source)
        if operation == "stop":
            return stop(profile, store, record)
        if operation == "start":
            return start(profile, store, record)
        if record is None or record["phase"] != "running":
            require(
                operation == "status" and not batch_identity,
                "candidate_not_running",
            )
            return summary(record) if record else {"status": "empty"}
        with live(profile, record) as session:
            session.attach_record(record)
            resident = session.verify(
                record["policy"]["policy"], record["compute_sha256"]
            )
            if operation == "query":
                from .probe import query as run_query

                try:
                    result = run_query(
                        session.docker,
                        record["container_ids"]["web"],
                        text,
                        record["revision"],
                    )
                finally:
                    session.verify(
                        record["policy"]["policy"], record["compute_sha256"]
                    )
                return {**summary(record), **result}
            result = {**summary(record), "runtime_verified": True}
            if batch_identity:
                from .batch.execution import runtime_identity

                require(
                    require_clean_revision(ROOT) == operator_revision,
                    "batch_operator_changed",
                )
                result["batch_identity_sha256"] = digest(
                    runtime_identity(
                        session, record, resident, operator_revision
                    )
                )
        if batch_identity:
            require(
                require_clean_revision(ROOT) == operator_revision,
                "batch_operator_changed",
            )
        return result


def _query_stdin():
    """Read one bounded UTF-8 query through EOF without waiting
    indefinitely.
    """
    deadline = time.monotonic() + QUERY_INPUT_SECONDS
    try:
        descriptor = sys.stdin.fileno()
        require(
            type(descriptor) is int
            and descriptor >= 0
            and not os.isatty(descriptor),
            "query_invalid",
        )
        blocking = os.get_blocking(descriptor)
        os.set_blocking(descriptor, False)
        try:
            raw = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                require(remaining > 0, "query_input_timeout")
                ready, _, _ = select.select([descriptor], [], [], remaining)
                require(
                    ready and time.monotonic() < deadline,
                    "query_input_timeout",
                )
                try:
                    part = os.read(
                        descriptor, MAX_QUERY_INPUT_BYTES + 1 - len(raw)
                    )
                except BlockingIOError:
                    continue
                raw.extend(part)
                require(len(raw) <= MAX_QUERY_INPUT_BYTES, "query_invalid")
                if not part:
                    text = raw.decode("utf-8", "strict")
                    if text.endswith("\n"):
                        text = text[:-1]
                        if text.endswith("\r"):
                            text = text[:-1]
                    text = checked_query(text)
                    require(time.monotonic() < deadline, "query_input_timeout")
                    return text
        finally:
            os.set_blocking(descriptor, blocking)
    except SharedBackendError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise SharedBackendError("shared_backend_query_invalid") from None


def main(argv=None):
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("prepare").add_argument("--source", required=True)
    commands.add_parser("start")
    commands.add_parser("status").add_argument(
        "--batch-identity", action="store_true"
    )
    commands.add_parser("stop")
    commands.add_parser("query").add_argument(
        "--stdin", action="store_true", required=True
    )
    batch = commands.add_parser("batch")
    batch.add_argument("--cases", required=True)
    batch.add_argument("--capture", required=True)
    batch.add_argument("--expected-revision", required=True)
    batch.add_argument("--expected-identity-sha256")
    args = parser.parse_args(argv)
    try:
        if args.operation == "batch":
            from .batch.execution import run_batch
            from .batch.capture import SharedBatchCaptureError

            try:
                value = run_batch(
                    args.config,
                    cases=args.cases,
                    destination=args.capture,
                    expected_revision=args.expected_revision,
                    expected_identity_sha256=args.expected_identity_sha256,
                )
            except SharedBatchCaptureError as error:
                print(json.dumps({"error": str(error)}))
                return 1
        else:
            text = _query_stdin() if args.operation == "query" else None
            value = operate(
                args.config,
                args.operation,
                source=getattr(args, "source", None),
                text=text,
                batch_identity=getattr(args, "batch_identity", False),
            )
    except KeyboardInterrupt:
        print('{"error":"shared_backend_interrupted"}')
        return 130
    except (
        SharedBackendError,
        SharedBackendProbeError,
        SharedRequestPolicyError,
        catalog.SharedCatalogError,
        ProviderError,
    ) as error:
        print(json.dumps({"error": str(error)}))
        return 1
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ):
        print('{"error":"shared_backend_failed"}')
        return 1
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))
    if args.operation == "batch" and value["status"] != "complete":
        return (
            130
            if value["error"]
            in {"shared_backend_interrupted", "shared_batch_interrupted"}
            else 1
        )
    return 0
