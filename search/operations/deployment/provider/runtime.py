"""Verify the fixed provider independently of a former backend's lifetime."""

from contextlib import ExitStack, contextmanager
import copy
from dataclasses import replace
from functools import partial
import hashlib
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision

from . import authority, contract


def namespace_digest(rows):
    """Seal namespace identities only after native runtime validation."""
    projection = {}
    for row in rows:
        role = row["Config"]["Labels"]["com.docker.compose.service"]
        network = row["NetworkSettings"]
        contract.hexadecimal(network["SandboxID"])
        contract.require(role not in projection, "runtime_changed")
        projection[role] = {
            "sandbox_id": network["SandboxID"],
            "network_ids": {
                key: item["NetworkID"]
                for key, item in network["Networks"].items()
            },
        }
    return contract.digest(projection)


def configuration(reference, source_root):
    return authority._configuration(reference, source_root)


def _source(value):
    root = Path(value["root"])
    contract.require(
        require_clean_revision(root) == value["revision"], "source_changed"
    )
    return root


class _ImageBinding:
    """One session's native build evidence and immutable image inspections."""

    def __init__(self, owner, config, source, plan, sealed, images, store):
        self._owner, self._store, self._config = owner, store, config
        self._source, self._plan = dict(source), copy.deepcopy(plan)
        self._authority = copy.deepcopy(sealed)
        self.images = copy.deepcopy(images)
        self.proof = copy.deepcopy(sealed["images"]["conversation-api"])
        self._key = (config.state_root, source["root"], source["revision"])
        self._sha256 = contract.digest(self._identity())

    @property
    def authority_sha256(self):
        return contract.digest(self._authority)

    def _identity(self):
        return {
            "source": self._source,
            "configuration": self._config.to_dict(),
            "source_plan": self._plan,
            "authority": self._authority,
            "images": self.images,
            "proof": self.proof,
        }

    def require_owner(self, owner):
        contract.require(
            self._owner is owner
            and owner._image_bindings.get(self._key) is self
            and contract.digest(self._identity()) == self._sha256,
            "authority_changed",
        )

    def require_host(self, owner, host):
        self.require_owner(owner)
        contract.require(
            host.source_root == Path(self._source["root"])
            and host.source_revision == self._source["revision"]
            and host.images == self.images
            and host.image_proof == self.proof,
            "authority_changed",
        )

    def refresh(self, owner):
        from search.operations.deployment.images import authority as images
        from search.operations.state import _marker

        self.require_owner(owner)
        root = owner._source(self._source)
        contract.require(
            owner._source_plan(root, self._source["revision"]) == self._plan
            and self._store.read("product.json") == _marker(self._config)
            and images.read_completed_images(
                self._config, self._store, self._plan
            )
            == self._authority,
            "authority_changed",
        )

    def inspect(self, owner):
        from search.operations.deployment.release import controller

        self.require_owner(owner)
        current = controller._images(owner.docker, self._plan, self._authority)
        current["filtered-embedding"] = owner.inference_images["scope-model"]
        contract.require(current == self.images, "authority_changed")


class Provider:
    def __init__(self, control, stack, deadline):
        self.control, self.stack, self.deadline = control, stack, deadline
        self.roles = control.roles
        self.roles_sha256 = control.roles_sha256
        self.provider_sha256 = control.provider_sha256
        self.anchor = control.provider
        self.source_root = self._source(self.anchor["source"])
        self.source_revision = self.anchor["source"]["revision"]
        self.config = configuration(
            self.anchor["configuration"], self.source_root
        )
        self.stores = {}
        self.backends = []
        self.last_gpu = None
        self.last_backend_proofs = {}
        self._image_bindings = {}
        self._topology = None

    def store(self, config):
        from search.operations.state import locked_state

        if config.state_root not in self.stores:
            self.stores[config.state_root] = self.stack.enter_context(
                locked_state(config, create=False)
            )
        return self.stores[config.state_root]

    def _source(self, value):
        return _source(value)

    def _source_plan(self, root, revision):
        from search.operations.deployment import source_bundle

        return source_bundle.committed_source_plan(root, revision)

    def _catalog_context(self, source, filename, config, store):
        from search.operations.deployment.candidate import catalog

        return catalog.load_context(source, filename, config, store)

    def _verified_backend_images(self, config, source_root, source_revision):
        from search.operations.deployment.images import authority as images
        from search.operations.deployment.release import controller

        source = {"root": str(source_root), "revision": source_revision}
        root = self._source(source)
        plan = self._source_plan(root, source_revision)
        store = self.store(config)
        sealed = images.read_completed_images(config, store, plan)
        inspected = controller._images(self.docker, plan, sealed)
        inspected["filtered-embedding"] = self.inference_images["scope-model"]
        binding = _ImageBinding(
            self, config, source, plan, sealed, inspected, store
        )
        prior = self._image_bindings.get(binding._key)
        if prior is not None:
            prior.require_owner(self)
            contract.require(
                prior._sha256 == binding._sha256, "authority_changed"
            )
            return prior
        self._image_bindings[binding._key] = binding
        return binding

    def load(self):
        from search.operations.deployment import inference, composition
        from search.operations.deployment.release import controller

        self._lock_states()
        self.store_native = self.store(self.config)
        self.source_plan = self._source_plan(
            self.source_root, self.source_revision
        )
        from search.operations.deployment.candidate.policy import (
            validate_shared_request_policy,
        )

        contract.require(
            validate_shared_request_policy(self.source_root, self.source_root)
            == self.anchor["policy"],
            "authority_changed",
        )
        contract.require(
            contract.digest(self.source_plan)
            == self.anchor["source_plan_sha256"],
            "authority_changed",
        )
        model = self.stack.enter_context(
            inference._resident_models(
                self.config,
                self.anchor["model_file_identity_sha256"],
                deadline=self.deadline,
                include_embedding=True,
            )
        )
        self.models, self.verify_models = model
        self.model_manifest_sha256 = contract.digest(self.models)
        self.model_file_identity_sha256 = self.anchor[
            "model_file_identity_sha256"
        ]
        contract.require(
            self.model_manifest_sha256 == self.anchor["model_manifest_sha256"],
            "model_changed",
        )
        self._docker()
        self.inference_compose = composition._inference(
            self.config,
            self.source_revision,
            os.geteuid(),
            os.getegid(),
            inference_source_sha256=self.source_plan["services"]["reranker"][
                "source_sha256"
            ],
        )
        self.image_authority = inference._authority(
            self.config,
            self.store_native,
            self.source_plan,
            self.inference_compose,
        )
        self.inference_images = inference._images(
            self.docker, self.source_plan, self.image_authority
        )
        for role, image in self.inference_images.items():
            self.inference_compose["services"][role]["image"] = image["Id"]
        identity = inference._runtime_identity(
            self.inference_compose,
            {role: self.models[role] for role in ("api", "reranker")},
            self.image_authority,
            self.inference_images,
        )
        contract.require(
            identity == self.anchor["inference_identity"], "authority_changed"
        )
        self.verify_inference = controller._sealed_inference(
            self.stack,
            self.config,
            self.store_native,
            self.inference_compose,
            identity,
        )
        self._backends()

    def _lock_states(self):
        from search.operations.catalog import execution

        self.backend_records = [
            (name, authority.read_backend(self.control, name))
            for name in (
                self.roles["active_backend_sha256"],
                self.roles["rollback_backend_sha256"],
            )
            if name is not None
        ]
        configs, catalogs = {self.config.state_root: self.config}, {}
        for _name, record in self.backend_records:
            config = configuration(
                record["configuration"], self._source(record["catalog_source"])
            )
            catalogs[config.state_root] = config
            for entry in (
                config,
                replace(config, state_root=Path(record["build_state_root"])),
            ):
                contract.require(
                    configs.get(entry.state_root, entry) == entry,
                    "authority_changed",
                )
                configs[entry.state_root] = entry
        for root in sorted(configs, key=str):
            self.store(configs[root])
        locks = {}
        for config in catalogs.values():
            plan = self.store(config).read("index-candidate.json")
            data = Path(contract.path(plan["data_root"]))
            locks[data] = {"data": data}
        for root in sorted(locks, key=str):
            self.stack.enter_context(execution.operation_lock(locks[root]))

    def _docker(self):
        from search.operations.deployment.runtime_docker import Docker

        self.client = Path(
            self.stack.enter_context(
                tempfile.TemporaryDirectory(prefix="provider-client-")
            )
        )
        fd = private_io.open_directory(self.client)
        self.stack.callback(os.close, fd)

        def check_client():
            contract.require(not os.listdir(fd), "authority_changed")
            private_io.verify_directory(fd, self.client)

        self.docker = Docker(
            self.client,
            None,
            deadline=self.deadline,
            max_commands=128,
            verify_client=check_client,
        )

    def _backends(self):
        for name, record in self.backend_records:
            backend = _backend(self, record, name)
            for prior in self.backends:
                contract.require(
                    prior.compose["name"] != backend.compose["name"]
                    and prior.lease["api_container_id"]
                    != backend.lease["api_container_id"],
                    "runtime_changed",
                )
                if (
                    prior.context["plan"]["project"]
                    == backend.context["plan"]["project"]
                ):
                    contract.require(
                        prior.catalog.identity() == backend.catalog.identity(),
                        "authority_changed",
                    )
            self.backends.append(backend)

    def refresh(self):
        self._refresh_authorities()
        for binding in self._image_bindings.values():
            binding.inspect(self)

    def _refresh_authorities(self, *, deadline=None):
        from search.operations.deployment import inference

        self._roles_admission()
        contract.require(
            self.control.provider == self.anchor
            and contract.digest(self.anchor) == self.provider_sha256
            and self.control.provider_sha256 == self.provider_sha256,
            "authority_changed",
        )
        self._source(self.anchor["source"])
        contract.require(
            configuration(self.anchor["configuration"], self.source_root)
            == self.config,
            "authority_changed",
        )
        contract.require(
            contract.digest(self.source_plan)
            == self.anchor["source_plan_sha256"],
            "authority_changed",
        )
        contract.require(
            contract.digest(self.store_native.read("inference-candidate.json"))
            == self.anchor["inference_complete_sha256"],
            "authority_changed",
        )
        contract.require(
            inference._runtime_identity(
                self.inference_compose,
                {role: self.models[role] for role in ("api", "reranker")},
                self.image_authority,
                self.inference_images,
            )
            == self.anchor["inference_identity"],
            "authority_changed",
        )
        self.verify_models()
        contract.require(
            contract.digest(self.models)
            == self.anchor["model_manifest_sha256"],
            "model_changed",
        )
        contract.require(
            inference._refresh_authority(
                self.config,
                self.store_native,
                self.source_plan,
                self.image_authority,
            )
            == self.image_authority,
            "authority_changed",
        )
        for backend in self.backends:
            contract.require(
                authority.read_backend(self.control, backend.backend_sha256)
                == backend.record,
                "authority_changed",
            )
            backend.catalog.refresh()
            self._source(backend.record["source"])
            configuration(
                backend.record["configuration"],
                Path(backend.record["catalog_source"]["root"]),
            )
        for binding in self._image_bindings.values():
            binding.refresh(self)
        if self._topology is not None:
            for catalog in self._topology[1]:
                _catalog_authority(
                    catalog, timeout=self.remaining(120, deadline=deadline)
                )

    def _roles_admission(self):
        self.control.refresh()
        contract.require(
            self.control.roles_sha256 == self.roles_sha256, "cas_conflict"
        )

    def _tls_reference(self):
        return self.roles["tls_plan_ref"]

    def observe(self, backends=None, catalogs=None):
        backends = self.backends if backends is None else backends
        catalogs = (
            [backend.catalog for backend in backends]
            if catalogs is None
            else catalogs
        )
        groups, networks = self._snapshot(backends, catalogs)
        result = self._validate_snapshot(backends, catalogs, groups, networks)
        self._topology = (tuple(backends), tuple(catalogs))
        self._topology_sha256 = _topology_identity(backends, catalogs)
        self._namespace_sha256 = {
            host.compose["name"]: namespace_digest(
                groups[host.compose["name"]]
            )
            for host in backends
        }
        return result

    def _validate_snapshot(self, backends, catalogs, groups, networks):
        from search.operations.deployment import inference_runtime
        from search.operations.deployment.candidate import catalog
        from search.operations.deployment.release import runtime

        clients = [_client(backend, groups, networks) for backend in backends]
        snapshots = {
            binding.context["plan"]["project"]: catalog.observe(
                binding, groups, networks, clients
            )
            for binding in catalogs
        }
        gpu_rows = groups[self.inference_compose["name"]]
        gpu = inference_runtime.validate_shared_runtime(
            self.inference_compose,
            self.inference_images,
            gpu_rows,
            [
                networks[value["name"]]
                for value in self.inference_compose["networks"].values()
            ],
            clients,
        )
        self.verify_inference(gpu)
        contract.require(
            namespace_digest(gpu_rows) == self.anchor["namespace_sha256"],
            "runtime_changed",
        )
        proofs = {
            backend.compose["name"]: _backend_runtime(
                backend, groups, networks, clients, runtime
            )
            for backend in backends
        }
        self.last_gpu, self.last_backend_proofs = gpu, proofs
        return {
            "groups": groups,
            "networks": networks,
            "gpu": gpu,
            "backend_proofs": proofs,
            "catalog_snapshots": snapshots,
        }

    def _checkpoint_authorities(self, *, deadline=None):
        contract.require(self._topology is not None, "authority_changed")
        backends, catalogs = self._topology
        contract.require(
            _topology_identity(backends, catalogs) == self._topology_sha256,
            "authority_changed",
        )
        for host in backends:
            binding = getattr(host, "image_binding", None)
            contract.require(
                type(binding) is _ImageBinding, "authority_changed"
            )
            binding.require_host(self, host)
        self._refresh_authorities(deadline=deadline)

    def _checkpoint_runtime(self, *, deadline=None):
        groups, network_map = self._snapshot(
            *self._topology, deadline=deadline
        )
        expected = self.last_backend_proofs
        result = self._validate_snapshot(*self._topology, groups, network_map)
        contract.require(
            result["backend_proofs"] == expected, "runtime_changed"
        )
        contract.require(
            {
                host.compose["name"]: namespace_digest(
                    groups[host.compose["name"]]
                )
                for host in self._topology[0]
            }
            == self._namespace_sha256,
            "runtime_changed",
        )

    def _snapshot(self, backends, catalogs, *, deadline=None):
        from search.operations.deployment.candidate.catalog import collect

        projects = {
            self.inference_compose["name"]: {
                value["name"]
                for value in self.inference_compose["networks"].values()
            }
        }
        for binding in catalogs:
            name = binding.context["plan"]["project"]
            projects[name] = {name + "_search_backend"}
        for backend in backends:
            projects[backend.compose["name"]] = {
                backend.compose["networks"]["website"]["name"]
            }

        def call(arguments, *, timeout=30):
            return self.docker.call(
                arguments, timeout=self.remaining(timeout, deadline=deadline)
            )

        return collect(SimpleNamespace(call=call), list(projects.items()))

    def check_models(
        self,
        policy,
        expected_compute=None,
        *,
        deadline=None,
        api_container_id=None,
    ):
        from search.operations.deployment import inference, inference_runtime
        from search.operations.deployment import private_api_tls
        from search.operations.deployment.candidate import probe

        active = self.backends[0]
        identifier = active.lease["api_container_id"]
        contract.require(
            api_container_id in (None, identifier)
            and self.last_gpu is not None
            and active.compose["name"] in self.last_backend_proofs,
            "runtime_changed",
        )
        self._checkpoint_authorities(deadline=deadline)
        tls = self._tls_reference()
        contract.require(
            hashlib.sha256(authority.read_private(tls["path"])).hexdigest()
            == tls["sha256"],
            "authority_changed",
        )
        cpu = private_api_tls.gpu_ownership_proofs(
            Path(tls["path"]),
            runner=partial(self._tls_runner, deadline=deadline),
        )
        inference._gpu(
            self.config,
            inference_runtime.ownership_proofs(
                self.inference_compose, self.last_gpu, self.inference_images
            ),
            partial(self._gpu_runner, deadline=deadline),
            cpu_proofs=cpu,
        )
        resident = probe.verify_residency(
            self.docker,
            identifier,
            policy,
            timeout=self.remaining(20, deadline=deadline),
        )
        result = self._compute_process(
            deadline=deadline,
        )
        contract.require(result.returncode == 0, "model_changed")
        self.compute_sha256 = _compute(self.config, result.stdout)
        expected = (
            self.anchor["compute_sha256"]
            if expected_compute is None
            else expected_compute
        )
        contract.require(self.compute_sha256 == expected, "model_changed")
        self._checkpoint_runtime(deadline=deadline)
        self._checkpoint_authorities(deadline=deadline)
        return resident

    def _compute_process(self, *, deadline=None):
        from search.operations.deployment.images import build

        return build._run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid",
                "--format=csv,noheader,nounits",
            ],
            timeout=self.remaining(15, deadline=deadline),
            environment={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
            },
        )

    def remaining(self, maximum, *, deadline=None):
        left = (
            min(self.deadline, self.deadline if deadline is None else deadline)
            - time.monotonic()
        )
        contract.require(left > 0, "deadline")
        return min(maximum, left)

    def _tls_runner(self, argv, *, deadline=None, **kwargs):
        from search.operations.deployment.images import build

        return build._run(
            argv,
            timeout=self.remaining(kwargs["timeout"], deadline=deadline),
            environment={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
            },
        )

    def _gpu_runner(self, argv, *, timeout, max_bytes, deadline=None):
        from search.operations.deployment.gpu import collect as gpu_collect

        return gpu_collect.bounded_runner(
            argv,
            timeout=self.remaining(timeout, deadline=deadline),
            max_bytes=max_bytes,
        )


def _topology_identity(backends, catalogs):
    return contract.digest(
        {
            "backends": [
                {
                    "source": {
                        "root": str(host.source_root),
                        "revision": host.source_revision,
                    },
                    "compose": host.compose,
                    "images": host.images,
                    "image_proof": host.image_proof,
                    "lease": host.lease,
                    "authority": host.authority,
                    "runtime": host.expected_runtime,
                    "backend_sha256": host.backend_sha256,
                }
                for host in backends
            ],
            "catalogs": [binding.identity() for binding in catalogs],
        }
    )


def _catalog_authority(binding, *, timeout):
    from search.operations.deployment.candidate import catalog

    binding.refresh()
    context = binding.context
    current = catalog.source_record(
        context["repo"],
        binding.config_file,
        context["source_revision"],
        timeout=timeout,
        **catalog._audit_runners(context),
    )
    contract.require(
        current
        == {
            "source_revision": context["source_revision"],
            "plan": context["plan"],
            "compose": context["compose"],
            "authority": binding.authority,
            "images": binding.images,
        },
        "authority_changed",
    )
    binding.refresh()


def _compute(config, raw):
    from search.operations.config import GPU_UUID

    pairs = []
    for line in raw.decode().splitlines():
        fields = [part.strip() for part in line.split(",")]
        contract.require(
            len(fields) == 2
            and GPU_UUID.fullmatch(fields[0])
            and fields[1].isascii()
            and fields[1].isdecimal()
            and int(fields[1]) > 0,
            "model_changed",
        )
        pairs.append((fields[0], int(fields[1])))
    contract.require(
        len(pairs) == 4
        and len(set(pairs)) == 4
        and {item[0] for item in pairs}
        == {config.gpus.scope, config.gpus.reranker, *config.gpus.planner},
        "model_changed",
    )
    return contract.digest(sorted(pairs))


def _client(backend, groups, networks):
    from search.operations.deployment.candidate.catalog import client_identity

    rows = groups.get(backend.compose["name"])
    contract.require(type(rows) is list, "runtime_changed")
    api = [
        row
        for row in rows
        if type(row) is dict
        and row.get("Id") == backend.lease["api_container_id"]
    ]
    contract.require(len(api) == 1, "runtime_changed")
    return {
        "lease": backend.lease,
        "catalog_identity": client_identity(
            backend.catalog, backend.source_revision
        ),
        "release_compose": backend.compose,
        "image_proof": backend.image_proof,
        "image_environment": backend.images["conversation-api"]["Config"][
            "Env"
        ],
        "api_container": api[0],
        "catalog_network": networks[
            backend.context["plan"]["project"] + "_search_backend"
        ],
    }


def _backend_runtime(backend, groups, networks, clients, runtime):
    from search.operations.deployment.candidate.catalog import client_identity

    own = backend.compose
    proof = runtime.validate_runtime(
        own,
        backend.images,
        groups[own["name"]],
        [networks[value["name"]] for value in own["networks"].values()],
        backend.lease,
        catalog_identity=client_identity(
            backend.catalog, backend.source_revision
        ),
        image_proof=backend.image_proof,
        image_environment=backend.images["conversation-api"]["Config"]["Env"],
        catalog_network=networks[
            backend.context["plan"]["project"] + "_search_backend"
        ],
        shared_clients=clients,
    )
    if backend.expected_runtime is not None:
        contract.require(proof == backend.expected_runtime, "runtime_changed")
    if backend.backend_sha256 is not None:
        contract.require(
            namespace_digest(groups[own["name"]]) == backend.namespace_sha256,
            "runtime_changed",
        )
    return proof


def _backend(provider, record, name):
    from search.operations.deployment import credentials, composition
    from search.operations.deployment.candidate import catalog

    root = provider._source(record["source"])
    catalog_root = provider._source(record["catalog_source"])
    config = configuration(record["configuration"], catalog_root)
    contract.require(
        replace(config, state_root=provider.config.state_root)
        == provider.config,
        "authority_changed",
    )
    store = provider.store(config)
    context, value = provider._catalog_context(
        catalog_root, Path(record["configuration"]["path"]), config, store
    )
    key = credentials.read_api_key_authority(config, store)
    inspections = {
        role: provider.docker.inspect(proof["image_id"])
        for role, proof in value["images"].items()
    }
    binding = catalog.Catalog(
        Path(record["configuration"]["path"]),
        config,
        store,
        context,
        value["authority"],
        value["images"],
        inspections,
        key,
    )
    contract.require(
        binding.identity() == record["catalog_binding"], "authority_changed"
    )
    image_binding = _backend_images(provider, record, root, config)
    images, proof = image_binding.images, image_binding.proof
    contract.require(proof == record["image_proof"], "authority_changed")
    composed = _backend_compose(
        provider, record, config, context, images, composition
    )
    contract.require(composed == record["compose"], "authority_changed")
    return SimpleNamespace(
        record=record,
        backend_sha256=name,
        config=config,
        namespace_sha256=record["namespace_sha256"],
        config_file=Path(record["configuration"]["path"]),
        source_root=root,
        source_revision=record["source"]["revision"],
        context=context,
        authority=value["authority"],
        catalog_images=value["images"],
        catalog_inspections=inspections,
        catalog=binding,
        key=key,
        compose=composed,
        images=images,
        image_proof=proof,
        image_binding=image_binding,
        lease=record["lease"],
        expected_runtime=record["runtime"],
    )


def _backend_images(provider, record, root, config):
    build_config = replace(config, state_root=Path(record["build_state_root"]))
    binding = provider._verified_backend_images(
        build_config, root, record["source"]["revision"]
    )
    contract.require(
        binding.authority_sha256 == record["image_authority_sha256"],
        "authority_changed",
    )
    return binding


def _backend_compose(provider, record, config, context, images, module):
    plans = module.render_composition(
        config,
        context["plan"],
        module.QueryBindingPaths.for_catalog(config, context["plan"]),
        record["source"]["revision"],
        uid=os.geteuid(),
        gid=os.getegid(),
        inference_source_sha256=provider.anchor["inference_identity"][
            "inference_source_sha256"
        ],
    )
    compose = plans["release"]
    for role, image in images.items():
        compose["services"][role]["image"] = image["Id"]
        if role != "filtered-embedding":
            labels = image["Config"].get("Labels") or {}
            compose["services"][role]["labels"] = {
                key: labels[key]
                for key in (
                    "org.opencontainers.image.revision",
                    "io.depositar.ai.source.revision",
                    "io.depositar.ai.source.service",
                    "io.depositar.ai.source.sha256",
                )
            }
    return compose


@contextmanager
def open_provider(control, *, deadline=None):
    with ExitStack() as stack:
        provider = Provider(
            control,
            stack,
            time.monotonic() + 1800 if deadline is None else deadline,
        )
        provider.load()
        provider.refresh()
        yield provider
        provider.refresh()


def verify_provider(config_path, *, deadline=None):
    with authority.open_control(config_path) as control:
        with open_provider(control, deadline=deadline) as provider:
            first = provider.observe()
            for backend in provider.backends:
                binding = backend.catalog
                binding.audit(
                    first["catalog_snapshots"][
                        binding.context["plan"]["project"]
                    ],
                    timeout=provider.remaining(180),
                )
            second = provider.observe()
            contract.require(
                first["catalog_snapshots"] == second["catalog_snapshots"],
                "runtime_changed",
            )
            provider.check_models(provider.anchor["policy"]["policy"])
            report = {
                "status": "backend_provider_verified",
                "provider_sha256": provider.provider_sha256,
                "roles_sha256": provider.roles_sha256,
                "epoch": provider.roles["epoch"],
                "active_changed": False,
            }
    return report
