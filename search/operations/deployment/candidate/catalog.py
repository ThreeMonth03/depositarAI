"""Authenticate each shared consumer's catalog using its own sealed source.

The live provider owns model authority. A selected catalog supplies only its
own product key, data, core image/storage proofs and read-only query authority;
it never supplies an inferred or copied inference completion receipt.
"""

from dataclasses import dataclass, replace
import ast
import os
from pathlib import Path
import tempfile

from search.catalog.bundle import CatalogBundle
from search.operations import config as product_config
from search.operations.catalog import chunks, client_lease, commands, execution, policy
from search.operations.deployment import composition, credentials, runtime_docker
from search.operations.deployment.images import build as image_build
from search.operations.deployment.release import controller as release_controller
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
from search.operations.state import StateStore, locked_state
from search.serialization import strict_json


# Invoke only the operator-selected clean checkout, never a path from a case.
# Its recipe authenticates its own catalog without redefining older receipts.
_CONTEXT = r"""
import json,os,sys
from pathlib import Path
repo,config_file,revision=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
sys.path.insert(0,str(repo))
from search.operations.config import load_config
from search.operations.state import StateStore
from search.operations.catalog import execution,query
from search.operations.shared.private_io import open_directory,read_file
from search.operations.shared.source_identity import require_clean_revision
assert require_clean_revision(repo)==revision
config=load_config(config_file,repo_root=repo)
fd=open_directory(config.state_root)
try:
 store=StateStore(config.state_root,fd)
 context=execution.load_context(config,store.read('index-candidate.json'),repo_root=repo)
 if len(sys.argv)==5:
  p=Path(sys.argv[4]);parent=open_directory(p.parent)
  try:snapshot=json.loads(read_file(parent,p.name,mode=0o600,max_bytes=1024**2))
  finally:os.close(parent)
  assert type(snapshot) is list and len(snapshot)==3
  authority=query.verify_authority(context,observe_runtime=lambda _:tuple(snapshot))
 else:authority=query.read_authority(context)
 assert require_clean_revision(repo)==revision
 print(json.dumps({'source_revision':revision,'plan':context['plan'],
  'compose':context['compose'],'authority':authority,'images':execution.load_images(context)}))
finally:os.close(fd)
"""


class SharedCatalogError(ValueError):
    """Fixed diagnostic; never include source output or credential values."""


def require(value, code):
    if not value:
        raise SharedCatalogError("shared_backend_catalog_" + code)


def query_profile(source):
    """Read fixed embedding/projection constants, without importing candidate code."""
    values = {}
    for relative, names in (
        ("search/catalog/embedding.py", ("MODEL", "MODEL_DIGEST", "DIMENSIONS")),
        ("search/planning/index_filters.py", ("VERSION", "VECTOR_PATH")),
    ):
        with (source / relative).open("rb") as stream:
            raw = stream.read(65537)
        require(len(raw) <= 65536, "query_profile_invalid")
        try:
            nodes = ast.parse(raw).body
            for name in names:
                matches = [
                    node.value
                    for node in nodes
                    if isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == name
                        for target in node.targets
                    )
                ]
                require(len(matches) == 1, "query_profile_invalid")
                values[name] = ast.literal_eval(matches[0])
        except (SyntaxError, ValueError, TypeError, RecursionError):
            raise SharedCatalogError(
                "shared_backend_catalog_query_profile_invalid"
            ) from None
    require(
        all(type(values[k]) is str and values[k] for k in values if k != "DIMENSIONS")
        and type(values["DIMENSIONS"]) is int
        and values["DIMENSIONS"] == 768,
        "query_profile_invalid",
    )
    return values


def validate_chunk_profile(context):
    """Require the explicit fixed recipe for a newly selected shadow catalog."""
    options = context["compose"]["services"]["fess-search"]["environment"][
        "FESS_JAVA_OPTS"
    ].split()
    for expected in chunks.JAVA_OPTIONS:
        name = expected.split("=", 1)[0]
        actual = [
            value for value in options if value == name or value.startswith(name + "=")
        ]
        require(actual == [expected], "chunk_profile_changed")


def source_record(source, config_file, revision, *, snapshot=None, timeout=120):
    require(require_clean_revision(source) == revision, "source_changed")
    with tempfile.TemporaryDirectory(prefix="shared-catalog-audit-") as directory:
        args = [
            "python3",
            "-I",
            "-S",
            "-B",
            "-c",
            _CONTEXT,
            str(source),
            str(config_file),
            revision,
        ]
        if snapshot is not None:
            fd = private_io.open_directory(Path(directory))
            try:
                private_io.write_json(
                    fd, "snapshot.json", list(snapshot), max_bytes=1024**2
                )
            finally:
                os.close(fd)
            args.append(str(Path(directory) / "snapshot.json"))
        result = image_build._run(
            args,
            timeout=timeout,
            environment={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    require(result.returncode == 0, "source_audit_failed")
    value = strict_json(result.stdout)
    require(
        type(value) is dict
        and set(value) == {"source_revision", "plan", "compose", "authority", "images"}
        and value["source_revision"] == revision,
        "source_audit_invalid",
    )
    require(require_clean_revision(source) == revision, "source_changed")
    return value


def load_context(source, config_file, config, store):
    revision = require_clean_revision(source)
    value = source_record(source, config_file, revision)
    release_controller._same(value["plan"], store.read("index-candidate.json"))
    plan = value["plan"]
    composition._catalog(config, plan, revision)
    root = Path(plan["work_root"])
    release_controller._same(
        value["compose"], execution.read_record(root, "compose.json")
    )
    context = {
        "plan": plan,
        "bundle": CatalogBundle.load(
            Path(plan["bundle_path"]), expected_manifest_sha256=plan["manifest_sha256"]
        ),
        "root": root,
        "data": Path(plan["data_root"]),
        "compose": value["compose"],
        "repo": source,
        "source_revision": revision,
        "runner": commands.default_runner(source),
        "query_profile": query_profile(source),
    }
    return context, value


@dataclass
class Catalog:
    config_file: Path
    config: product_config.ProductConfig
    store: StateStore
    context: dict
    authority: dict
    images: dict
    inspections: dict
    key: dict

    def identity(self):
        return {
            "source_revision": self.context["source_revision"],
            "configuration_sha256": execution.digest_record(self.config.to_dict()),
            "plan_sha256": execution.digest_record(self.context["plan"]),
            "query_authority_sha256": execution.digest_record(self.authority),
            "api_key_authority_sha256": execution.digest_record(self.key),
            "query_profile_sha256": execution.digest_record(
                self.context["query_profile"]
            ),
        }

    def refresh(self):
        require(
            product_config.load_config(self.config_file, repo_root=self.context["repo"])
            == self.config,
            "config_changed",
        )
        require(
            require_clean_revision(self.context["repo"])
            == self.context["source_revision"],
            "source_changed",
        )
        release_controller._same(
            self.store.read("index-candidate.json"), self.context["plan"]
        )
        release_controller._same(
            credentials.read_api_key_authority(self.config, self.store), self.key
        )

    def audit(self, snapshot, *, timeout):
        self.refresh()
        value = source_record(
            self.context["repo"],
            self.config_file,
            self.context["source_revision"],
            snapshot=snapshot,
            timeout=timeout,
        )
        for key, expected in (
            ("plan", self.context["plan"]),
            ("compose", self.context["compose"]),
            ("authority", self.authority),
            ("images", self.images),
        ):
            release_controller._same(
                value[key], expected, "shared_backend_catalog_authority_changed"
            )
        self.refresh()


def select(profile, stack, config, store, native, key):
    """Retain distinct product/catalog locks; never initialize selected state."""
    baseline = Catalog(
        profile["production_config"],
        config,
        store,
        native.context,
        native.authority,
        native.catalog_images,
        native.catalog_inspections,
        key,
    )
    if "catalog_config" not in profile:
        return baseline, baseline
    source = profile["catalog_source"]
    selected = product_config.load_config(profile["catalog_config"], repo_root=source)
    if selected.state_root == config.state_root:
        require(
            selected == config and source == profile["production_source"],
            "selection_invalid",
        )
        return baseline, baseline
    # Independent data state may share only the fixed existing model recipe.
    require(
        replace(selected, state_root=config.state_root) == config,
        "provider_config_changed",
    )
    target_store = stack.enter_context(locked_state(selected, create=False))
    plan = target_store.read("index-candidate.json")
    stack.enter_context(execution.operation_lock({"data": Path(plan["data_root"])}))
    context, value = load_context(
        source, profile["catalog_config"], selected, target_store
    )
    validate_chunk_profile(context)
    require(
        context["plan"]["project"] != native.context["plan"]["project"],
        "identity_collision",
    )
    images = value["images"]
    inspections = {
        role: native.docker.inspect(proof["image_id"]) for role, proof in images.items()
    }
    binding = Catalog(
        profile["catalog_config"],
        selected,
        target_store,
        context,
        value["authority"],
        images,
        inspections,
        credentials.read_api_key_authority(selected, target_store),
    )
    binding.refresh()
    return baseline, binding


def observe(binding, groups, networks, clients):
    """Validate this catalog's full snapshot, with or without its selected API."""
    context = binding.context
    project = context["plan"]["project"]
    containers, network = groups[project], networks[project + "_search_backend"]
    require(
        network.get("Driver") == "bridge"
        and network.get("Scope") == "local"
        and network.get("Ingress") is False,
        "network_changed",
    )
    execution.validate_storage_mounts(execution.require_storage(context), containers)
    members = [item for item in clients if item["lease"]["catalog_project"] == project]
    require(len(members) <= 2, "clients_invalid")
    if members:
        member = members[0]
        release_controller._same(
            member["catalog_identity"],
            binding.authority["catalog_identity"],
            "shared_backend_catalog_authority_changed",
        )
        proof = client_lease.validate_leased_runtime(
            context["plan"],
            context["compose"],
            binding.images,
            containers,
            network,
            member["lease"],
            core_image_inspections=binding.inspections,
            **{
                k: v for k, v in member.items() if k not in {"lease", "catalog_network"}
            },
            shared_clients=clients,
        )
        runtime = {
            "container_ids": proof["catalog_container_ids"],
            "network_id": proof["catalog_network_id"],
        }
    else:
        for row in containers:
            role = row["Config"]["Labels"]["com.docker.compose.service"]
            for key in ("Cmd", "Entrypoint"):
                release_controller._same(
                    row["Config"].get(key), binding.inspections[role]["Config"].get(key)
                )
        runtime = policy.validate_runtime(
            context["plan"], context["compose"], binding.images, containers, network
        )
    release_controller._same(
        runtime,
        {k: binding.authority["runtime"][k] for k in ("container_ids", "network_id")},
    )
    return runtime, containers, network


def collect(docker, projects):
    """Inspect all catalog/provider/client projects together, without filtering peers."""
    require(
        len({name for name, _networks in projects}) == len(projects),
        "project_collision",
    )
    inventories, expected_networks = {}, set()
    for project, owned in projects:
        ids, names = runtime_docker.project_inventory(docker, project)
        require(len(ids) == len(set(ids)) == 3 and names == owned, "inventory_changed")
        inventories[project] = set(ids)
        expected_networks.update(owned)
    ids = [identifier for values in inventories.values() for identifier in values]
    require(len(set(ids)) == len(ids), "identity_collision")
    rows = image_build._json(docker.call(["inspect", *sorted(ids)]))
    require(
        type(rows) is list
        and len(rows) == len(ids)
        and all(type(row) is dict for row in rows)
        and {row.get("Id") for row in rows} == set(ids),
        "snapshot_invalid",
    )
    networks = image_build._json(
        docker.call(["network", "inspect", *sorted(expected_networks)])
    )
    require(
        type(networks) is list
        and len(networks) == len(expected_networks)
        and all(type(row) is dict for row in networks)
        and {row.get("Name") for row in networks} == expected_networks,
        "snapshot_invalid",
    )
    return (
        {
            project: [row for row in rows if row["Id"] in identifiers]
            for project, identifiers in inventories.items()
        },
        {row["Name"]: row for row in networks},
    )
