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
from search.operations.catalog import client_lease, execution
from search.operations.catalog import commands as catalog_commands
from search.catalog.bundle import CatalogBundle
from search.operations.deployment import (
    composition,
    image_authority,
    image_build,
    inference,
    inference_runtime,
    private_api_tls,
    source_bundle,
)
from search.operations.deployment.release import controller, probe, runtime
from search.operations.deployment.shared_request_policy import (
    SharedRequestPolicyError,
    validate_shared_request_policy,
)
from search.operations.deployment.shared_backend_probe import SharedBackendProbeError
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
from search.operations.state import StateStore, locked_state
from search.serialization import strict_json


ROOT = Path(__file__).resolve().parents[3]
FIELDS = {
    "schema_version",
    "state_root",
    "production_config",
    "production_source",
    "tls_plan",
}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
ROLES = frozenset({"web", "conversation-api", "filtered-embedding"})

# Validate an existing catalog with its running application's committed recipe.
# A newer operator may require additional recipe inputs for NEW catalogs; those
# inputs must not retroactively redefine the old application's compatibility.
# This fixed audit invokes no lifecycle operation and makes no Docker changes.
_CONTEXT = r"""
import json,os,sys
from pathlib import Path
repo,config_file,revision=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
sys.path.insert(0,str(repo))
from search.operations.config import load_config
from search.operations.state import StateStore
from search.operations.catalog.execution import load_context
from search.operations.shared.private_io import open_directory
from search.operations.shared.source_identity import require_clean_revision
assert require_clean_revision(repo)==revision
config=load_config(config_file,repo_root=repo)
fd=open_directory(config.state_root)
try:
 store=StateStore(config.state_root,fd)
 context=load_context(config,store.read('index-candidate.json'),repo_root=repo)
 assert require_clean_revision(repo)==revision
 if len(sys.argv)==5:
  from search.operations.catalog import query
  from search.operations.shared.private_io import read_file
  p=Path(sys.argv[4]);parent=open_directory(p.parent)
  try:snapshot=json.loads(read_file(parent,p.name,mode=0o600,max_bytes=1024**2))
  finally:os.close(parent)
  assert type(snapshot) is list and len(snapshot)==3
  authority=query.verify_authority(context,observe_runtime=lambda _:tuple(snapshot))
  assert require_clean_revision(repo)==revision
  print(json.dumps({'source_revision':revision,'authority':authority}))
 else:
  print(json.dumps({'source_revision':revision,'plan':context['plan'],'compose':context['compose']}))
finally:os.close(fd)
"""


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
        and set(value) == FIELDS
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1,
        "config_invalid",
    )
    for key in FIELDS - {"schema_version"}:
        value[key] = path(value[key])
    root = value["state_root"]
    require(
        not any((p / ".git").exists() for p in (root, *root.parents)),
        "state_inside_git",
    )
    require(
        all(
            root != value[k] and root not in value[k].parents
            for k in ("production_config", "production_source", "tls_plan")
        ),
        "state_overlaps_inputs",
    )
    prod = product_config.load_config(
        value["production_config"], repo_root=value["production_source"]
    )
    for p in (
        prod.state_root,
        prod.api_model_root,
        prod.embedding_model_root,
        prod.reranker_model_root,
    ):
        require(
            root != p and root not in p.parents and p not in root.parents,
            "state_overlaps_production",
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
    repo = profile["production_source"]
    result = image_build._run(
        [
            "python3",
            "-I",
            "-S",
            "-B",
            "-c",
            _CONTEXT,
            str(repo),
            str(profile["production_config"]),
            revision,
        ],
        timeout=120,
        environment={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    require(result.returncode == 0, "baseline_catalog_source_failed")
    value = strict_json(result.stdout)
    require(
        type(value) is dict
        and set(value) == {"source_revision", "plan", "compose"}
        and value["source_revision"] == revision,
        "baseline_catalog_source_invalid",
    )
    controller._same(value["plan"], store.read("index-candidate.json"))
    plan = value["plan"]
    composition._catalog(config, plan, revision)
    root = Path(plan["work_root"])
    controller._same(value["compose"], execution.read_record(root, "compose.json"))
    require(require_clean_revision(repo) == revision, "production_source_changed")
    return {
        "plan": plan,
        "bundle": CatalogBundle.load(
            Path(plan["bundle_path"]), expected_manifest_sha256=plan["manifest_sha256"]
        ),
        "root": root,
        "data": Path(plan["data_root"]),
        "compose": value["compose"],
        "repo": repo,
        "runner": catalog_commands.default_runner(repo),
        "source_revision": revision,
    }


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
            inputs = controller._runtime_inputs(
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
            completed = controller._load_completed(slot, inputs)
            controller._same(store.read("release-candidate.json"), completed)
            session = Session(profile, config, store, context, inputs, completed)
            yield session
            inputs.refresh()
            controller._verify_records(
                config, parent, slot, name, inputs, controller.FILES
            )
            controller._same(image_build._record(slot, "complete.json"), completed)
            controller._same(store.read("release-candidate.json"), completed)
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
        groups, networks = self.native.collect(clients=True)
        if self.candidate is not None:
            compose = self.candidate.compose
            ids, names = controller.runtime_docker.project_inventory(
                self.docker, compose["name"]
            )
            require(
                set(ids) == set(self.record["container_ids"].values())
                and names == {compose["networks"]["website"]["name"]},
                "candidate_inventory_changed",
            )
            groups[compose["name"]] = image_build._json(
                self.docker.call(["inspect", *sorted(ids)])
            )
            website = compose["networks"]["website"]["name"]
            row = image_build._json(self.docker.call(["network", "inspect", website]))
            require(type(row) is list and len(row) == 1, "candidate_network_changed")
            networks[website] = row[0]
        return groups, networks

    def observe(self, _context=None):
        groups, networks = self.snapshot()
        catalog = groups[self.context["plan"]["project"]]
        execution.validate_storage_mounts(
            execution.require_storage(self.context), catalog
        )
        catalog_network = networks[self.context["plan"]["project"] + "_search_backend"]
        clients = [evidence(self.native, groups, networks)]
        if self.candidate is not None:
            clients.append(evidence(self.candidate, groups, networks))
        leased = client_lease.validate_leased_runtime(
            self.context["plan"],
            self.context["compose"],
            self.native.catalog_images,
            catalog,
            catalog_network,
            self.native.lease,
            core_image_inspections=self.native.catalog_inspections,
            **{
                k: v
                for k, v in clients[0].items()
                if k not in {"lease", "catalog_network"}
            },
            shared_clients=clients,
        )
        catalog_proof = {
            "container_ids": leased["catalog_container_ids"],
            "network_id": leased["catalog_network_id"],
        }
        controller._same(
            catalog_proof,
            {
                k: self.native.authority["runtime"][k]
                for k in ("container_ids", "network_id")
            },
        )
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
                catalog_identity=self.native.authority["catalog_identity"],
                image_proof=host.image_proof,
                image_environment=host.images["conversation-api"]["Config"]["Env"],
                catalog_network=catalog_network,
                shared_clients=clients,
            )
            if host is self.native:
                self.native.last = (gpu, proof)
            else:
                self.last_candidate = proof
        controller._same(
            controller._completed_receipt(self.inputs.identity, self.native.last[1]),
            self.completed,
        )
        return catalog_proof, catalog, catalog_network

    def verify(self, policy, expected_compute=None):
        from .shared_backend_probe import verify_residency

        snapshot = self.observe()
        # Query/vector receipt semantics belong to the sealed live data recipe.
        # The new operator independently validates actual shared peers on both
        # sides of that audit; the audit receives the FULL authenticated snapshot.
        with tempfile.TemporaryDirectory(prefix="shared-catalog-audit-") as directory:
            fd = private_io.open_directory(Path(directory))
            try:
                private_io.write_json(
                    fd, "snapshot.json", list(snapshot), max_bytes=1024**2
                )
            finally:
                os.close(fd)
            repo = self.profile["production_source"]
            revision = self.context["source_revision"]
            require(
                require_clean_revision(repo) == revision, "production_source_changed"
            )
            result = image_build._run(
                [
                    "python3",
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    _CONTEXT,
                    str(repo),
                    str(self.profile["production_config"]),
                    revision,
                    str(Path(directory) / "snapshot.json"),
                ],
                timeout=180,
                environment={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            require(result.returncode == 0, "baseline_query_audit_failed")
            value = strict_json(result.stdout)
            require(
                type(value) is dict
                and set(value) == {"source_revision", "authority"}
                and value["source_revision"] == revision,
                "baseline_query_audit_invalid",
            )
            controller._same(value["authority"], self.inputs.authority)
        after = self.observe()
        from search.operations.catalog.connection import snapshot_peers

        controller._same(
            snapshot_peers(self.context, self.inputs.authority["runtime"], snapshot),
            snapshot_peers(self.context, self.inputs.authority["runtime"], after),
        )
        cpu = private_api_tls.gpu_ownership_proofs(self.profile["tls_plan"])
        inference._gpu(
            self.config,
            inference_runtime.ownership_proofs(
                self.native.inference_compose,
                self.native.last[0],
                self.native.inference_images,
            ),
            None,
            cpu_proofs=cpu,
        )
        resident = verify_residency(
            self.docker, self.native.lease["api_container_id"], policy
        )
        result = image_build._run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid",
                "--format=csv,noheader,nounits",
            ],
            timeout=15,
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
        controller._same(policy, record["policy"])
        config = replace(
            self.config, state_root=build_root(self.profile, record["revision"])
        )
        with locked_state(config, create=False) as store:
            plan = source_bundle.committed_source_plan(source, record["revision"])
            authority = image_authority.read_completed_images(config, store, plan)
            images = controller._images(self.docker, plan, authority)
        images["filtered-embedding"] = self.native.inference_images["scope-model"]
        plans = composition.render_composition(
            self.config,
            self.context["plan"],
            composition.QueryBindingPaths.for_catalog(
                self.config, self.context["plan"]
            ),
            record["revision"],
            uid=os.geteuid(),
            gid=os.getegid(),
            inference_source_sha256=plan["services"]["reranker"]["source_sha256"],
        )
        gpu = copy.deepcopy(plans["inference"])
        for role, value in self.native.inference_images.items():
            gpu["services"][role]["image"] = value["Id"]
        controller._same(
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
        controller._same(compose, record["compose"])
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
                controller.runtime_docker.project_inventory(
                    session.docker, compose["name"]
                )
            ),
            "candidate_project_exists",
        )
        record.update(
            {
                "compose": compose,
                "baseline_receipt_sha256": digest(session.completed),
                "model_file_identity_sha256": session.inputs.model_file_identity_sha256,
                "compute_sha256": session.compute_sha256,
                "network_id": None,
                "external_network_ids": {
                    v["name"]: session.native.lease["network_ids"][v["name"]]
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
    ids, names = controller.runtime_docker.project_inventory(docker, compose["name"])
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
        controller._same(compose, record["compose"])
        require(
            record["baseline_receipt_sha256"] == digest(session.completed),
            "live_release_changed",
        )
        require(
            not any(
                controller.runtime_docker.project_inventory(
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
            ids, _ = controller.runtime_docker.project_inventory(
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
                **session.native.authority["catalog_identity"],
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
            ready = probe.verify_private_release(
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


def operate(filename, operation, *, source=None, text=None):
    require(
        operation in {"prepare", "start", "status", "query", "stop"},
        "operation_invalid",
    )
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
            require(operation == "status", "candidate_not_running")
            return summary(record) if record else {"status": "empty"}
        with live(profile, record) as session:
            session.attach_record(record)
            session.verify(record["policy"]["policy"], record["compute_sha256"])
            if operation == "query":
                from .shared_backend_probe import query as run_query

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
            return {**summary(record), "runtime_verified": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("prepare").add_argument("--source", required=True)
    commands.add_parser("start")
    commands.add_parser("status")
    commands.add_parser("stop")
    commands.add_parser("query").add_argument("--query", required=True)
    args = parser.parse_args(argv)
    try:
        value = operate(
            args.config,
            args.operation,
            source=getattr(args, "source", None),
            text=getattr(args, "query", None),
        )
    except KeyboardInterrupt:
        print('{"error":"shared_backend_interrupted"}')
        return 130
    except (
        SharedBackendError,
        SharedBackendProbeError,
        SharedRequestPolicyError,
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
    return 0
