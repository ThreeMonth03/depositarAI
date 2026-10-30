"""One reviewed CPU candidate sharing a sealed live inference/catalog plane.

The operator owns a separate journal and frozen source worktree. It never
starts, warms, repairs or stops GPU/catalog services, nor changes the live route.
Old single-client entry points stay strict; this explicit phase verifies both
complete clients against the actual unfiltered networks.
"""

from contextlib import ExitStack, contextmanager
from dataclasses import replace
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

from search.operations import config as product_config, lifecycle
from search.operations.catalog import execution
from search.operations.deployment import (
    composition,
    inference,
    inference_runtime,
    private_api_tls,
    source_bundle,
)
from search.operations.deployment.images import authority as image_authority
from search.operations.deployment.images import build as image_build
from search.operations.deployment.release import controller as release_controller
from search.operations.deployment.release import probe as release_probe
from search.operations.deployment.release import runtime
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
from search.operations.state import StateStore, locked_state
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
    "production_config",
    "production_source",
    "tls_plan",
}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
ROLES = frozenset({"web", "conversation-api", "filtered-embedding"})


class SharedBackendError(ValueError):
    """Closed diagnostics, without paths, keys or child output."""


def require(value, code):
    if not value:
        raise SharedBackendError("shared_backend_" + code)


def digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def path(value):
    require(
        type(value) is str and value.isascii() and not any(ord(c) < 32 for c in value),
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
            private_io.read_file(fd, filename.name, mode=0o600, max_bytes=16384)
        )
    finally:
        os.close(fd)
    require(
        type(value) is dict
        and set(value) in (FIELDS, FIELDS | {"catalog_config", "catalog_source"})
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1,
        "config_invalid",
    )
    for key in set(value) - {"schema_version"}:
        value[key] = path(value[key])
    root = value["state_root"]
    require(
        not any((p / ".git").exists() for p in (root, *root.parents)),
        "state_inside_git",
    )
    require(
        all(
            root != value[k] and root not in value[k].parents
            for k in set(value) - {"schema_version", "state_root"}
        ),
        "state_overlaps_inputs",
    )
    prod = product_config.load_config(
        value["production_config"], repo_root=value["production_source"]
    )
    products = [prod]
    if "catalog_config" in value:
        products.append(
            product_config.load_config(
                value["catalog_config"], repo_root=value["catalog_source"]
            )
        )
    for product in products:
        for p in (
            product.state_root,
            product.api_model_root,
            product.embedding_model_root,
            product.reranker_model_root,
        ):
            require(
                root != p and root not in p.parents and p not in root.parents,
                "state_overlaps_production",
            )
    if len(products) == 2:
        a, b = (p.state_root for p in products)
        require(
            a == b or a not in b.parents and b not in a.parents,
            "catalog_state_overlaps_production",
        )
    return value


@contextmanager
def journal(root):
    fd = private_io.open_directory(root)
    lock = None
    try:
        lock = os.open(
            "operations.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd
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
            (row.st_dev, row.st_ino) == (named.st_dev, named.st_ino), "lock_changed"
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
        and value.get("schema_version") == 1
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
        type(value.get("revision")) is str and HEX40.fullmatch(value["revision"]),
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
                type(value.get(k)) is str and re.fullmatch(r"[0-9a-f]{64}", value[k])
                for k in (
                    "baseline_receipt_sha256",
                    "model_file_identity_sha256",
                    "compute_sha256",
                )
            ),
            "journal_invalid",
        )
    return value


def save(store, value, phase=None):
    if phase is not None:
        value["phase"] = phase
    store.write("current.json", value, replace=True)


def git(source, *arguments):
    result = image_build._run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(source), *arguments],
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
    require(type(revision) is str and HEX40.fullmatch(revision), "revision_invalid")
    return profile["state_root"] / "sources" / revision


def build_root(profile, revision):
    require(type(revision) is str and HEX40.fullmatch(revision), "revision_invalid")
    return profile["state_root"] / "builds" / revision


def baseline_context(profile, config, store, revision):
    context, _value = catalog.load_context(
        profile["production_source"], profile["production_config"], config, store
    )
    require(context["source_revision"] == revision, "production_source_changed")
    return context


@contextmanager
def live(profile, record=None):
    """Retain original source/model/query authority and product/catalog locks."""
    repo = profile["production_source"]
    revision = require_clean_revision(repo)
    config = product_config.load_config(profile["production_config"], repo_root=repo)
    with locked_state(config, create=False) as store:
        require(
            store.read("release-candidate.json").get("revision") == revision,
            "production_source_not_running_release",
        )
        plan = store.read("index-candidate.json")
        composition._catalog(config, plan, revision)
        with (
            execution.operation_lock({"data": Path(plan["data_root"])}),
            ExitStack() as stack,
        ):
            context = baseline_context(profile, config, store, revision)
            inputs = release_controller._runtime_inputs(
                stack,
                config,
                store,
                revision,
                context,
                repo,
                None,
                None,
                time.monotonic() + 1800,
                model_file_identity=record.get("model_file_identity_sha256")
                if record
                else None,
            )
            parent = private_io.open_child_directory(store.fd, "releases")
            stack.callback(os.close, parent)
            name = inputs.host.compose["name"].removeprefix("depositar-ai-")
            slot = private_io.open_child_directory(parent, name)
            stack.callback(os.close, slot)
            completed = release_controller._load_completed(slot, inputs)
            release_controller._same(store.read("release-candidate.json"), completed)
            session = Session(profile, config, store, context, inputs, completed)
            session.baseline_catalog, session.query_catalog = catalog.select(
                profile, stack, config, store, session.native, inputs.key
            )
            session.query_context = session.query_catalog.context
            session.query_authority = session.query_catalog.authority
            yield session
            session.query_catalog.refresh()
            inputs.refresh()
            release_controller._verify_records(
                config, parent, slot, name, inputs, release_controller.FILES
            )
            release_controller._same(
                image_build._record(slot, "complete.json"), completed
            )
            release_controller._same(store.read("release-candidate.json"), completed)
            require(
                require_clean_revision(repo) == revision, "production_source_changed"
            )


def evidence(native, groups, networks):
    compose, lease = native.compose, native.lease
    rows = groups[compose["name"]]
    api = [r for r in rows if r["Id"] == lease["api_container_id"]]
    require(len(api) == 1, "api_changed")
    return {
        "lease": lease,
        "catalog_identity": native.authority["catalog_identity"],
        "release_compose": compose,
        "image_proof": native.image_proof,
        "image_environment": native.images["conversation-api"]["Config"]["Env"],
        "api_container": api[0],
        "catalog_network": networks[
            native.context["plan"]["project"] + "_search_backend"
        ],
    }


class Session:
    def __init__(self, profile, config, store, context, inputs, completed):
        self.profile, self.config, self.store, self.context = (
            profile,
            config,
            store,
            context,
        )
        self.inputs, self.completed, self.native = inputs, completed, inputs.host
        self.docker = inputs.host.docker
        self.candidate = None
        self.record = None
        self.last_candidate = None

    def snapshot(self):
        projects = [
            (
                binding.context["plan"]["project"],
                {binding.context["plan"]["project"] + "_search_backend"},
            )
            for binding in self.catalogs()
        ]
        projects.append(
            (
                self.native.inference_compose["name"],
                {v["name"] for v in self.native.inference_compose["networks"].values()},
            )
        )
        for host in (self.native, self.candidate):
            if host is not None:
                projects.append(
                    (
                        host.compose["name"],
                        {host.compose["networks"]["website"]["name"]},
                    )
                )
        groups, networks = catalog.collect(self.docker, projects)
        if self.candidate is not None:
            require(
                {row["Id"] for row in groups[self.candidate.compose["name"]]}
                == set(self.record["container_ids"].values()),
                "candidate_inventory_changed",
            )
        return groups, networks

    def catalogs(self):
        return [self.baseline_catalog] + (
            [self.query_catalog]
            if self.query_catalog is not self.baseline_catalog
            else []
        )

    def observe(self, _context=None):
        groups, networks = self.snapshot()
        clients = [evidence(self.native, groups, networks)]
        if self.candidate is not None:
            clients.append(evidence(self.candidate, groups, networks))
        self.catalog_snapshots = {
            binding.context["plan"]["project"]: catalog.observe(
                binding, groups, networks, clients
            )
            for binding in self.catalogs()
        }
        gpu = inference_runtime.validate_shared_runtime(
            self.native.inference_compose,
            self.native.inference_images,
            groups[self.native.inference_compose["name"]],
            [
                networks[v["name"]]
                for v in self.native.inference_compose["networks"].values()
            ],
            clients,
        )
        self.native.verify_inference(gpu)
        for host in (self.native, self.candidate):
            if host is None:
                continue
            own = host.compose
            proof = runtime.validate_runtime(
                own,
                host.images,
                groups[own["name"]],
                [networks[v["name"]] for v in own["networks"].values()],
                host.lease,
                catalog_identity=host.authority["catalog_identity"],
                image_proof=host.image_proof,
                image_environment=host.images["conversation-api"]["Config"]["Env"],
                catalog_network=networks[
                    host.context["plan"]["project"] + "_search_backend"
                ],
                shared_clients=clients,
            )
            if host is self.native:
                self.native.last = (gpu, proof)
            else:
                if self.record.get("runtime") is not None:
                    release_controller._same(
                        proof,
                        self.record["runtime"],
                        "shared_backend_candidate_runtime_changed",
                    )
                self.last_candidate = proof
                web = next(
                    row
                    for row in groups[own["name"]]
                    if row["Id"] == self.record["container_ids"]["web"]
                )
                self.candidate_endpoint = web["NetworkSettings"]["Networks"][
                    own["networks"]["website"]["name"]
                ]["IPAddress"]
        release_controller._same(
            release_controller._completed_receipt(
                self.inputs.identity, self.native.last[1]
            ),
            self.completed,
        )
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
                    binding.context, binding.authority["runtime"], snapshots[project]
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
        """Bounded ownership/residency checkpoint, without scanning the catalog."""
        from .probe import verify_residency

        def tls_runner(argv, **kwargs):
            return image_build._run(
                argv,
                timeout=self.remaining(kwargs["timeout"], deadline=deadline),
                environment={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            )

        def gpu_runner(argv, *, timeout, max_bytes):
            return inference.gpu_ownership.bounded_runner(
                argv,
                timeout=self.remaining(timeout, deadline=deadline),
                max_bytes=max_bytes,
            )

        require(
            require_clean_revision(self.profile["production_source"])
            == self.context["source_revision"],
            "production_source_changed",
        )
        cpu = private_api_tls.gpu_ownership_proofs(
            self.profile["tls_plan"], runner=tls_runner
        )
        inference._gpu(
            self.config,
            inference_runtime.ownership_proofs(
                self.native.inference_compose,
                self.native.last[0],
                self.native.inference_images,
            ),
            gpu_runner,
            cpu_proofs=cpu,
        )
        resident = verify_residency(
            self.docker,
            self.native.lease["api_container_id"],
            policy,
            timeout=self.remaining(20, deadline=deadline),
        )
        result = image_build._run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid",
                "--format=csv,noheader,nounits",
            ],
            timeout=self.remaining(15, deadline=deadline),
            environment={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        require(result.returncode == 0, "compute_snapshot_failed")
        pairs = []
        for line in result.stdout.decode().splitlines():
            fields = [p.strip() for p in line.split(",")]
            require(
                len(fields) == 2
                and product_config.GPU_UUID.fullmatch(fields[0])
                and fields[1].isascii()
                and fields[1].isdecimal()
                and int(fields[1]) > 0,
                "compute_snapshot_invalid",
            )
            pairs.append((fields[0], int(fields[1])))
        require(
            len(pairs) == 4
            and len(set(pairs)) == 4
            and {x[0] for x in pairs}
            == {
                self.config.gpus.scope,
                self.config.gpus.reranker,
                *self.config.gpus.planner,
            },
            "compute_snapshot_changed",
        )
        self.compute_sha256 = digest(sorted(pairs))
        require(
            expected_compute is None or self.compute_sha256 == expected_compute,
            "model_process_changed",
        )
        self.remaining(1, deadline=deadline)
        return resident

    def candidate_inputs(self, record):
        source = frozen_source(self.profile, record["revision"])
        require(
            require_clean_revision(source) == record["revision"],
            "candidate_source_changed",
        )
        policy = validate_shared_request_policy(
            self.profile["production_source"], source
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
            self.config, state_root=build_root(self.profile, record["revision"])
        )
        with locked_state(config, create=False) as store:
            plan = source_bundle.committed_source_plan(source, record["revision"])
            authority = image_authority.read_completed_images(config, store, plan)
            images = release_controller._images(self.docker, plan, authority)
        images["filtered-embedding"] = self.native.inference_images["scope-model"]
        plans = composition.render_composition(
            self.query_catalog.config,
            self.query_context["plan"],
            composition.QueryBindingPaths.for_catalog(
                self.query_catalog.config, self.query_context["plan"]
            ),
            record["revision"],
            uid=os.geteuid(),
            gid=os.getegid(),
            inference_source_sha256=plan["services"]["reranker"]["source_sha256"],
        )
        gpu = copy.deepcopy(plans["inference"])
        for role, value in self.native.inference_images.items():
            gpu["services"][role]["image"] = value["Id"]
        release_controller._same(
            gpu, self.native.inference_compose, "shared_backend_model_recipe_changed"
        )
        compose = copy.deepcopy(plans["release"])
        for role, image in images.items():
            compose["services"][role]["image"] = image["Id"]
            if role != "filtered-embedding":
                p = authority["images"][role]
                compose["services"][role]["labels"] = {
                    "org.opencontainers.image.revision": record["revision"],
                    "io.depositar.ai.source.revision": record["revision"],
                    "io.depositar.ai.source.service": role,
                    "io.depositar.ai.source.sha256": p["source_sha256"],
                }
        require(compose["name"] != self.native.compose["name"], "candidate_is_live")
        return compose, images, authority["images"]["conversation-api"]

    def attach_record(self, record):
        compose, images, proof = self.candidate_inputs(record)
        release_controller._same(compose, record["compose"])
        require(
            record["baseline_receipt_sha256"] == digest(self.completed),
            "live_release_changed",
        )
        # The existing host supplies the same complete evidence shape without
        # adopting another catalog or manufacturing a baseline snapshot.
        candidate = copy.copy(self.native)
        candidate.compose, candidate.images, candidate.image_proof = (
            compose,
            images,
            proof,
        )
        candidate.context = self.query_context
        candidate.authority = self.query_authority
        candidate.catalog_images = self.query_catalog.images
        candidate.catalog_inspections = self.query_catalog.inspections
        candidate.lease = record["lease"]
        self.candidate, self.record = candidate, record


def prepare(profile, store, source):
    prior = current(store)
    require(
        prior is None or prior["phase"] == "stopped", "candidate_already_registered"
    )
    source = path(str(source))
    revision = require_clean_revision(source)
    policy = validate_shared_request_policy(profile["production_source"], source)
    frozen = frozen_source(profile, revision)
    for folder in (frozen.parent, build_root(profile, revision).parent):
        os.close(private_io.open_directory(folder, create=True))
    require(not frozen.exists(), "source_cache_exists")
    record = {
        "schema_version": 1,
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
    prod = product_config.load_config(
        profile["production_config"], repo_root=profile["production_source"]
    )
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
                "baseline_receipt_sha256": digest(session.completed),
                "model_file_identity_sha256": session.inputs.model_file_identity_sha256,
                "compute_sha256": session.compute_sha256,
                "network_id": None,
                "external_network_ids": {
                    v["name"]: (
                        session.query_authority["runtime"]["network_id"]
                        if key == "catalog"
                        else session.native.lease["network_ids"][v["name"]]
                    )
                    for key, v in compose["networks"].items()
                    if key != "website"
                },
            }
        )
        require(
            type(record["model_file_identity_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", record["model_file_identity_sha256"]),
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
        names <= {compose["networks"]["website"]["name"]}, "candidate_inventory_changed"
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
            labels.get("com.docker.compose.project.config_files") == str(compose_file),
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
            and all(labels.get(k) == v for k, v in service.get("labels", {}).items()),
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
        for name, attachment in (row["NetworkSettings"].get("Networks") or {}).items():
            expected = record.get("external_network_ids", {}).get(name)
            if name == compose["networks"]["website"]["name"]:
                expected = record.get("network_id")
            if expected is not None and attachment.get("NetworkID"):
                require(
                    attachment["NetworkID"] == expected, "candidate_network_changed"
                )
        observed[role] = row["Id"]
    if record["container_ids"]:
        require(
            set(observed) <= set(record["container_ids"]), "candidate_inventory_changed"
        )
    return rows, names


def start(profile, store, record):
    require(
        record is not None and record["phase"] in {"prepared", "running"},
        "candidate_not_prepared",
    )
    with live(profile, record) as session:
        if record["phase"] == "running":
            session.attach_record(record)
            session.verify(record["policy"]["policy"], record["compute_sha256"])
            return summary(record)
        session.verify(record["policy"]["policy"], record["compute_sha256"])
        compose, images, api_proof = session.candidate_inputs(record)
        release_controller._same(compose, record["compose"])
        require(
            record["baseline_receipt_sha256"] == digest(session.completed),
            "live_release_changed",
        )
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
                [*prefix, "create", "--no-build", "--pull", "never"], timeout=180
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
            save(store, record, "created")
            session.docker.call(
                [*prefix, "start", "--wait", "--wait-timeout", "180"], timeout=210
            )
            groups, networks = session.native.collect(clients=True)
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
            rows = image_build._json(session.docker.call(["inspect", *sorted(ids)]))
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
                **session.query_authority["catalog_identity"],
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
            session.verify(record["policy"]["policy"], record["compute_sha256"])
            ready = release_probe.verify_private_release(
                session.docker,
                record["container_ids"],
                revision=record["revision"],
                bundle_id=record["lease"]["bundle_id"],
                manifest_sha256=record["lease"]["manifest_sha256"],
                search=False,
            )
            require(ready["readiness_verified"] is True, "candidate_not_ready")
            session.verify(record["policy"]["policy"], record["compute_sha256"])
            record["runtime"] = session.last_candidate
            save(store, record, "starting")
        except BaseException:
            save(store, record, "failed")
            raise
    save(store, record, "running")
    return summary(record)


def stop(profile, store, record):
    require(record is not None, "candidate_not_registered")
    if record["phase"] == "stopped":
        return summary(record)
    from search.operations.deployment.runtime_docker import Docker

    with tempfile.TemporaryDirectory(prefix="shared-backend-stop-") as temporary:
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
                    r["Config"]["Labels"]["com.docker.compose.service"]: r["Id"]
                    for r in rows
                }
                save(store, record)
            owned_networks = []
            for name in names:
                networks = image_build._json(docker.call(["network", "inspect", name]))
                require(
                    len(networks) == 1
                    and networks[0].get("Labels", {}).get("com.docker.compose.project")
                    == record["compose"]["name"],
                    "candidate_network_changed",
                )
                if record.get("network_id") is None:
                    require(bool(rows), "candidate_network_unsealed")
                    record["network_id"] = networks[0]["Id"]
                    save(store, record)
                require(
                    networks[0]["Id"] == record["network_id"]
                    and set(networks[0].get("Containers") or {})
                    <= {r["Id"] for r in rows},
                    "candidate_network_changed",
                )
                owned_networks.append(networks[0]["Id"])
            for row in rows:
                docker.call(["rm", "--force", row["Id"]], timeout=30)
            for identifier in owned_networks:
                rows = image_build._json(
                    docker.call(["network", "inspect", identifier])
                )
                require(
                    len(rows) == 1
                    and rows[0].get("Id") == identifier
                    and not rows[0].get("Containers")
                    and rows[0].get("Labels", {}).get("com.docker.compose.project")
                    == record["compose"]["name"],
                    "candidate_network_changed",
                )
                docker.call(["network", "rm", rows[0]["Id"]])
    source = frozen_source(profile, record["revision"])
    if source.exists():
        require(
            require_clean_revision(source) == record["revision"],
            "candidate_source_changed",
        )
        common = (
            git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .decode()
            .strip()
        )
        result = image_build._run(
            ["git", "--git-dir", common, "worktree", "remove", str(source)],
            timeout=60,
            environment={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        require(result.returncode == 0, "source_operation_failed")
    build = build_root(profile, record["revision"])
    if build.exists() and record.get("build_complete") is True:
        require(
            build.resolve() == build and build.stat().st_uid == os.geteuid(),
            "build_cache_unsafe",
        )
        shutil.rmtree(build)
    save(store, record, "stopped")
    return summary(record)


def operate(filename, operation, *, source=None, text=None, batch_identity=False):
    require(
        operation in {"prepare", "start", "status", "query", "stop"},
        "operation_invalid",
    )
    require(
        type(batch_identity) is bool and (not batch_identity or operation == "status"),
        "batch_identity_operation_invalid",
    )
    operator_revision = require_clean_revision(ROOT) if batch_identity else None
    profile = load_config(filename)
    with journal(profile["state_root"]) as store:
        record = current(store)
        if operation == "prepare":
            return prepare(profile, store, source)
        if operation == "stop":
            return stop(profile, store, record)
        if operation == "start":
            return start(profile, store, record)
        if record is None or record["phase"] != "running":
            require(
                operation == "status" and not batch_identity, "candidate_not_running"
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
                    session.verify(record["policy"]["policy"], record["compute_sha256"])
                return {**summary(record), **result}
            result = {**summary(record), "runtime_verified": True}
            if batch_identity:
                from .batch.execution import runtime_identity

                require(
                    require_clean_revision(ROOT) == operator_revision,
                    "batch_operator_changed",
                )
                result["batch_identity_sha256"] = digest(
                    runtime_identity(session, record, resident, operator_revision)
                )
        if batch_identity:
            require(
                require_clean_revision(ROOT) == operator_revision,
                "batch_operator_changed",
            )
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("prepare").add_argument("--source", required=True)
    commands.add_parser("start")
    commands.add_parser("status").add_argument("--batch-identity", action="store_true")
    commands.add_parser("stop")
    commands.add_parser("query").add_argument("--query", required=True)
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
            value = operate(
                args.config,
                args.operation,
                source=getattr(args, "source", None),
                text=getattr(args, "query", None),
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
