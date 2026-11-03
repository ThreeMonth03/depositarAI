"""CPU provider fixtures with explicit native file and tool I/O edges."""

import hashlib
import copy
import json
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import subprocess
from types import SimpleNamespace
import time
from unittest.mock import Mock, patch

from search.operations.catalog import client_lease
from search.operations.deployment import composition
from search.operations.deployment.candidate import catalog
from search.operations.deployment.provider import runtime, contract
from search.operations.deployment.release import runtime as cpu_runtime
from tests.unit.catalog.test_client_lease import (
    evidence as client_evidence,
    leased_runtime_evidence,
    shared_evidence,
)
from tests.unit.deployment import test_inference_runtime as gpu_fixtures
from tests.unit.deployment.release import test_runtime as cpu_fixtures
from tests.unit.deployment.candidate import test_probe as probe_fixtures
from .test_contract import records


def provider_cpu_images(cpu, models):
    image = cpu["images"]["web"]
    image["Id"] = "sha256:" + "c" * 64
    cpu["compose"]["services"]["web"]["image"] = image["Id"]
    cpu["images"]["filtered-embedding"] = copy.deepcopy(models["scope-model"])
    for row in cpu["containers"][1:]:
        role = row["Config"]["Labels"]["com.docker.compose.service"]
        planned = cpu["compose"]["services"][role]
        image = cpu["images"][role]
        planned["image"] = image["Id"]
        row["Image"], row["Config"]["Image"] = image["Id"], image["Id"]
        row["Config"]["Env"] = [
            key + "=" + value
            for key, value in (
                dict(entry.split("=", 1) for entry in image["Config"]["Env"])
                | planned["environment"]
            ).items()
        ]
    cpu_fixtures.reseal(cpu)


def provider_fixture():
    def factory():
        return client_evidence(
            revision="6" * 40, api_id="d" * 64, image_id="sha256:" + "f" * 64
        )

    with patch.object(cpu_fixtures, "client_evidence", factory):
        cpu = cpu_fixtures.evidence()
    models = gpu_fixtures.images()
    models["scope-model"]["Config"]["Cmd"] = ["serve"]
    provider_cpu_images(cpu, models)
    cpu["catalog_network"].update(
        Driver="bridge", Scope="local", Ingress=False
    )
    for index, row in enumerate(cpu["containers"], 10):
        row["NetworkSettings"]["SandboxID"] = format(index, "064x")
    core = leased_runtime_evidence()
    core_context = {
        "plan": core["core_plan"],
        "compose": core["core_compose"],
        "repo": Path("/private/catalog-source"),
        "source_revision": "a" * 40,
        "query_profile": {
            "MODEL": "fixed",
            "MODEL_DIGEST": "a" * 64,
            "DIMENSIONS": 768,
        },
    }
    binding = catalog.Catalog(
        Path("/private/catalog.json"),
        gpu_fixtures.product(),
        None,
        core_context,
        {
            "catalog_identity": cpu["catalog_identity"],
            "runtime": {
                "container_ids": {
                    row["Config"]["Labels"]["com.docker.compose.service"]: row[
                        "Id"
                    ]
                    for row in core["core_containers"]
                },
                "network_id": cpu["catalog_network"]["Id"],
            },
        },
        core["core_images"],
        core["core_image_inspections"],
        {},
    )
    lease = cpu["lease"]
    client = {
        "lease": lease,
        "catalog_identity": cpu["catalog_identity"],
        "release_compose": cpu["compose"],
        "image_proof": cpu["image_proof"],
        "image_environment": cpu["image_environment"],
        "api_container": cpu["containers"][0],
        "catalog_network": cpu["catalog_network"],
    }
    proof = cpu_runtime.validate_runtime(**cpu, shared_clients=[client])
    backend = SimpleNamespace(
        compose=cpu["compose"],
        images=cpu["images"],
        lease=lease,
        context=core_context,
        authority=binding.authority,
        image_proof=cpu["image_proof"],
        expected_runtime=proof,
        backend_sha256="b" * 64,
        catalog=binding,
        namespace_sha256=runtime.namespace_digest(cpu["containers"]),
        source_root=Path("/private/backend-source"),
        source_revision="6" * 40,
    )
    config = gpu_fixtures.product()
    compose = composition._inference(
        config, "a" * 40, 21599, 564, inference_source_sha256="6" * 64
    )
    images = models
    for role, image in images.items():
        compose["services"][role]["image"] = image["Id"]
    gpu_rows, model_networks = gpu_fixtures.snapshot(compose, images)
    old_ids = [row["Id"] for row in gpu_rows]
    new_ids = [value * 64 for value in "789"]
    for index, row in enumerate(gpu_rows):
        row["Id"] = new_ids[index]
        row["NetworkSettings"]["SandboxID"] = format(index + 20, "064x")
        for name, value in row["NetworkSettings"]["Networks"].items():
            value["NetworkID"] = lease["network_ids"][name]
    for network in model_networks:
        network["Id"] = lease["network_ids"][network["Name"]]
        network["Containers"] = {
            new_ids[old_ids.index(identifier)]: value
            for identifier, value in network["Containers"].items()
        }
        network["Containers"][lease["api_container_id"]] = {}
    groups = {
        compose["name"]: gpu_rows,
        cpu["compose"]["name"]: cpu["containers"],
        core_context["plan"]["project"]: core["core_containers"],
    }
    networks = {
        row["Name"]: row
        for row in model_networks + cpu["networks"] + [cpu["catalog_network"]]
    }
    provider = runtime.Provider.__new__(runtime.Provider)
    provider.inference_compose, provider.inference_images = compose, images
    provider.anchor = {"namespace_sha256": runtime.namespace_digest(gpu_rows)}
    provider.backends = [backend]
    provider.verify_inference = Mock()
    provider._snapshot = Mock(return_value=(groups, networks))
    provider.last_gpu = None
    provider._image_bindings = {}
    provider._topology = None
    return provider, backend, groups, networks


def model_checkpoint_fixture():
    provider, backend, _groups, _networks = provider_fixture()
    provider.observe()
    raw = b"private TLS fixture\n"
    provider.roles = {
        "tls_plan_ref": {
            "path": "/private/tls/plan.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    }
    provider.config = gpu_fixtures.product()
    provider.deadline = time.monotonic() + 600
    compute = "\n".join(
        f"{uuid},{index}"
        for index, uuid in enumerate(provider.config.gpus.all_uuids(), 100)
    ).encode()
    provider.anchor["compute_sha256"] = runtime._compute(
        provider.config, compute
    )
    provider._checkpoint_authorities = Mock()
    provider._checkpoint_runtime = Mock()
    return provider, backend, raw, compute


def image_binding_fixture(test, provider, host, docker=None, *, shared=None):
    """Isolate native source/file I/O; retain the real factory/image reader."""
    from search.operations.deployment import source_bundle
    from search.operations.deployment.images import authority as image_owner
    from search.operations.deployment.images import build
    from search.operations.state import _marker

    stack = ExitStack()
    test.addCleanup(stack.close)
    shared = {} if shared is None else shared
    plans = shared.setdefault("plans", {})
    authorities = shared.setdefault("authorities", {})
    stores = shared.setdefault("stores", {})
    commands = shared.setdefault("commands", [])
    plan = {"revision": host.source_revision}
    inspections = {
        image["Id"]: copy.deepcopy(image)
        for role, image in host.images.items()
        if role != "filtered-embedding"
    }
    for role in ("web", "conversation-api"):
        inspections[build.TAGS[role] + host.source_revision] = inspections[
            host.images[role]["Id"]
        ]

    def measured_proof(_plan, role, _identifier, image):
        labels = image["Config"]["Labels"]
        return {
            "schema_version": 1,
            "revision": labels["org.opencontainers.image.revision"],
            "service": role,
            "source_sha256": labels["io.depositar.ai.source.sha256"],
            "image_id": image["Id"],
            "image_environment_sha256": contract.digest(
                dict(entry.split("=", 1) for entry in image["Config"]["Env"])
            ),
        }

    sealed = {
        "images": {
            role: measured_proof(plan, role, image["Id"], image)
            for role, image in host.images.items()
            if role != "filtered-embedding"
        }
    }
    config = replace(
        gpu_fixtures.product(),
        state_root=Path("/private/build") / host.source_revision,
    )
    store = SimpleNamespace(read=lambda _name: _marker(config))
    plans[host.source_root] = plan
    authorities[config.state_root], stores[config.state_root] = sealed, store
    if docker is None:
        docker = Mock()
        docker.inspect.side_effect = lambda reference: copy.deepcopy(
            inspections[reference]
        )
    else:
        original = docker.runner

        def runner(argv, *, timeout, environment, input_bytes=None):
            if argv[5:7] == ["image", "inspect"] and argv[-1] in inspections:
                commands.append(list(argv))
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(inspections[argv[-1]]).encode(), b""
                )
            return original(
                argv,
                timeout=timeout,
                environment=environment,
                **(
                    {"input_bytes": input_bytes}
                    if input_bytes is not None
                    else {}
                ),
            )

        stack.enter_context(patch.object(docker, "runner", runner))
    provider.docker = docker
    provider.store = Mock(side_effect=lambda value: stores[value.state_root])
    stack.enter_context(
        patch.object(
            runtime,
            "require_clean_revision",
            side_effect=lambda root: plans[root]["revision"],
        )
    )
    stack.enter_context(
        patch.object(
            source_bundle,
            "committed_source_plan",
            side_effect=lambda root, _revision: plans[root],
        )
    )
    stack.enter_context(
        patch.object(
            image_owner,
            "read_completed_images",
            side_effect=lambda value, *_args: authorities[value.state_root],
        )
    )
    stack.enter_context(
        patch.object(
            source_bundle, "validate_built_image", side_effect=measured_proof
        )
    )
    binding = provider._verified_backend_images(
        config, host.source_root, host.source_revision
    )
    test.assertEqual(binding.images, host.images)
    test.assertEqual(binding.proof, host.image_proof)
    host.image_binding = binding
    return SimpleNamespace(
        binding=binding,
        plan=plan,
        sealed=sealed,
        inspections=inspections,
        config=config,
        shared=shared,
    )


def _checkpoint_evidence(test, data):
    """Real provider/image/runtime consumers with isolated native file I/O."""
    from search.operations.deployment import inference, runtime_docker
    from search.operations.deployment import private_api_tls, credentials
    from search.operations.deployment.images import build
    from search.operations.deployment.candidate import probe

    stack = ExitStack()
    test.addCleanup(stack.close)
    provider, groups, networks = data.provider, data.groups, data.networks
    host = provider.backends[0]
    members, catalogs = data.hosts, data.catalogs
    commands = []
    config, raw = gpu_fixtures.product(), b"private TLS fixture\n"
    policy = probe_fixtures.POLICY
    resident = {
        "schema_version": 1,
        "status": "resident",
        "full_gpu_residency_verified": True,
        "models": [
            {**item, "size_bytes": 1234, "vram_bytes": 1234}
            for item in probe._expected(policy)
        ],
    }

    def command(argv, *, timeout, environment, input_bytes=None):
        commands.append(list(argv))
        args = argv[5:]
        if args[0] == "ps":
            value = b"\n".join(
                json.dumps({"Id": row["Id"], "Project": project}).encode()
                for project, rows in groups.items()
                for row in rows
            )
        elif args[:2] == ["network", "ls"]:
            value = b"\n".join(
                json.dumps({"Name": name}).encode() for name in networks
            )
        elif args[0] == "inspect":
            value = json.dumps(
                [
                    row
                    for rows in groups.values()
                    for row in rows
                    if row["Id"] in args[1:]
                ]
            ).encode()
        elif args[:2] == ["network", "inspect"]:
            value = json.dumps([networks[name] for name in args[2:]]).encode()
        else:
            test.assertEqual(args[0], "exec")
            value = json.dumps(resident).encode()
        return subprocess.CompletedProcess(argv, 0, value, b"")

    docker = runtime_docker.Docker(
        Path("/private/fake-client"), command, max_commands=128
    )
    shared = {"commands": commands}
    images = {
        member.source_revision: image_binding_fixture(
            test, provider, member, docker, shared=shared
        )
        for member in members
    }
    image = images[host.source_revision]
    provider.config, provider.deadline = config, docker.deadline
    provider.source_root, provider.source_revision = (
        Path("/private/provider-source"),
        "a" * 40,
    )
    provider.source_plan = {"revision": provider.source_revision}
    provider.models = {"api": {}, "reranker": {}}
    provider.image_authority = {
        "revision": "a" * 40,
        "source_manifest_sha256": "3" * 64,
    }
    native = {"fixture": "original-native-inference"}
    provider.store_native = SimpleNamespace(read=lambda _name: native)
    provider.verify_models = Mock()
    compute = "\n".join(
        f"{uuid},{index}"
        for index, uuid in enumerate(config.gpus.all_uuids(), 100)
    ).encode()
    anchor = records()[0]
    anchor.update(
        {
            "source": {
                "root": str(provider.source_root),
                "revision": provider.source_revision,
            },
            "source_plan_sha256": contract.digest(provider.source_plan),
            "namespace_sha256": provider.anchor["namespace_sha256"],
            "inference_complete_sha256": contract.digest(native),
            "inference_identity": inference._runtime_identity(
                provider.inference_compose,
                provider.models,
                provider.image_authority,
                provider.inference_images,
            ),
            "compute_sha256": runtime._compute(config, compute),
            "model_manifest_sha256": contract.digest(provider.models),
        }
    )
    provider.anchor = anchor
    host.catalog.key = {"credential_sha256": "1" * 64}
    host.catalog.store = SimpleNamespace(
        read=lambda _name: host.context["plan"]
    )
    for binding in catalogs:
        binding.key = dict(host.catalog.key)
        binding.store = SimpleNamespace(
            read=lambda _name, binding=binding: binding.context["plan"]
        )
    record = records()[1]
    record.update(
        {
            "source": {
                "root": str(host.source_root),
                "revision": host.source_revision,
            },
            "configuration": {
                "path": str(host.catalog.config_file),
                "sha256": "1" * 64,
            },
            "catalog_source": {
                "root": str(host.context["repo"]),
                "revision": host.context["source_revision"],
            },
            "build_state_root": str(image.config.state_root),
            "compose": host.compose,
            "lease": host.lease,
            "runtime": host.expected_runtime,
            "image_proof": host.image_proof,
            "image_authority_sha256": image.binding.authority_sha256,
            "catalog_binding": host.catalog.identity(),
            "provider_sha256": contract.digest(anchor),
            "namespace_sha256": host.namespace_sha256,
        }
    )
    host.record, host.backend_sha256 = (
        copy.deepcopy(record),
        contract.digest(record),
    )
    roles = {
        **records()[2],
        "provider_sha256": contract.digest(anchor),
        "active_backend_sha256": host.backend_sha256,
        "tls_plan_ref": {
            "path": "/private/tls.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    }
    provider.roles, provider.roles_sha256 = roles, contract.digest(roles)
    provider.provider_sha256 = contract.digest(anchor)
    provider.model_manifest_sha256 = anchor["model_manifest_sha256"]
    provider.model_file_identity_sha256 = anchor["model_file_identity_sha256"]
    current = {
        "record": copy.deepcopy(record),
        "key": dict(host.catalog.key),
        "gpu": copy.deepcopy(provider.image_authority),
    }
    current["catalog"] = {
        "source_revision": host.context["source_revision"],
        "plan": host.context["plan"],
        "compose": host.context["compose"],
        "authority": copy.deepcopy(host.authority),
        "images": host.catalog.images,
    }
    current["catalogs"] = {
        str(binding.config_file): {
            "source_revision": binding.context["source_revision"],
            "plan": binding.context["plan"],
            "compose": binding.context["compose"],
            "authority": copy.deepcopy(binding.authority),
            "images": binding.images,
        }
        for binding in catalogs
    }
    current["catalogs"][str(host.catalog.config_file)] = current["catalog"]
    sources = {
        **{member.source_root: member.source_revision for member in members},
        **{
            binding.context["repo"]: binding.context["source_revision"]
            for binding in catalogs
        },
        provider.source_root: provider.source_revision,
    }
    provider.control = SimpleNamespace(
        refresh=Mock(),
        roles_sha256=provider.roles_sha256,
        provider=copy.deepcopy(anchor),
        provider_sha256=provider.provider_sha256,
        roles=roles,
        root=Path("/private/control"),
        path=Path("/private/control.json"),
    )
    gpu = {
        "schema_version": 1,
        "status": "ok",
        "code": "gpu_ownership_verified",
        "snapshot_only": True,
        "exclusive_reservation": False,
        "runtime_modified": False,
        "sealed_gpu_container_count": 3,
        "gpu_count": 4,
    }
    for owner, name, replacement in (
        (
            runtime,
            "require_clean_revision",
            lambda root: sources[root],
        ),
        (runtime, "configuration", lambda *_args: config),
        (
            runtime.authority,
            "read_backend",
            lambda *_args: copy.deepcopy(current["record"]),
        ),
        (runtime.authority, "read_private", lambda _path: raw),
        (
            catalog.product_config,
            "load_config",
            lambda *_args, **_kwargs: config,
        ),
        (
            catalog,
            "require_clean_revision",
            lambda root: sources[root],
        ),
        (
            catalog,
            "source_record",
            lambda _source, path, *_args, **_kwargs: copy.deepcopy(
                current["catalogs"][str(path)]
            ),
        ),
        (
            credentials,
            "read_api_key_authority",
            lambda *_args: dict(current["key"]),
        ),
        (inference, "_refresh_authority", lambda *_args: current["gpu"]),
        (
            private_api_tls,
            "gpu_ownership_proofs",
            lambda *_args, **_kwargs: (),
        ),
        (
            inference.gpu_ownership,
            "run_gpu_ownership",
            lambda *_args, **_kwargs: gpu,
        ),
        (
            build,
            "_run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0, stdout=compute
            ),
        ),
    ):
        stack.enter_context(patch.object(owner, name, replacement))
    del provider._snapshot
    provider.observe(members, catalogs)
    return SimpleNamespace(
        provider=provider,
        host=host,
        groups=groups,
        networks=networks,
        image=image,
        current=current,
        policy=policy,
        members=members,
        catalogs=catalogs,
        images=images,
        commands=commands,
        sources=sources,
    )


def checkpoint_evidence(test):
    provider, host, groups, networks = provider_fixture()
    return _checkpoint_evidence(
        test,
        SimpleNamespace(
            provider=provider,
            hosts=[host],
            catalogs=[host.catalog],
            groups=groups,
            networks=networks,
        ),
    )


def shared_checkpoint_evidence(
    test, *, separate_catalogs=False, active_first=False
):
    data = dual_snapshot(separate_catalogs=separate_catalogs)
    if active_first:
        data.provider.backends = [data.hosts[0]]
    active = data.provider.backends[0]
    for host in data.hosts:
        if host is not active:
            host.backend_sha256, host.record, host.namespace_sha256 = (
                None,
                None,
                None,
            )
            host.source_root = (
                Path("/private/candidate-journal/sources")
                / host.source_revision
            )
    evidence = _checkpoint_evidence(test, data)
    return _session_evidence(evidence)


def _session_evidence(evidence):
    from search.operations.deployment.candidate import controller

    candidate = next(
        host for host in evidence.members if host is not evidence.host
    )
    selected = candidate.catalog
    for index, binding in enumerate(evidence.catalogs):
        name = binding.context["plan"]["project"] + "_search_backend"
        for offset, identifier_ in enumerate(
            binding.authority["catalog_identity"][
                "core_container_ids"
            ].values(),
            2,
        ):
            row = next(
                row
                for row in evidence.groups[binding.context["plan"]["project"]]
                if row["Id"] == identifier_
            )
            literal = f"172.19.{index}.{offset}"
            row["NetworkSettings"]["Networks"][name]["IPAddress"] = literal
            evidence.networks[name]["Containers"][identifier_][
                "IPv4Address"
            ] = literal + "/24"
    profile = {
        "schema_version": 2,
        "state_root": Path("/private/candidate-journal"),
        "control_config": evidence.provider.control.path,
        "catalog_config": selected.config_file,
        "catalog_source": selected.context["repo"],
        "_control": evidence.provider.control,
    }
    record = {
        "schema_version": 2,
        "phase": "running",
        "revision": candidate.source_revision,
        "policy": {
            "policy": evidence.policy,
            "request_policy_sha256": contract.digest(evidence.policy),
        },
        "compute_sha256": evidence.provider.anchor["compute_sha256"],
        "container_ids": candidate.expected_runtime["container_ids"],
        "runtime": candidate.expected_runtime,
        "lease": candidate.lease,
        "compose": candidate.compose,
        "provider_authority_sha256": evidence.provider.provider_sha256,
        "roles_sha256": evidence.provider.roles_sha256,
        "roles_epoch": evidence.provider.roles["epoch"],
        "catalog_binding": selected.identity(),
    }
    for index, row in enumerate(evidence.groups[candidate.compose["name"]], 4):
        row["NetworkSettings"]["Networks"][
            candidate.compose["networks"]["website"]["name"]
        ]["IPAddress"] = f"172.20.0.{index}"
    session = controller.Session(
        profile, evidence.provider, evidence.provider.control, selected
    )
    session.candidate, session.record = candidate, record
    session._candidate_image_binding = candidate.image_binding
    evidence.session, evidence.profile, evidence.record = (
        session,
        profile,
        record,
    )
    evidence.candidate, evidence.query_catalog = candidate, selected
    return evidence


def identifier(number):
    return format(number, "064x")


def _cpu(member, index, models):
    with patch.object(
        cpu_fixtures, "client_evidence", lambda: copy.deepcopy(member)
    ):
        cpu = cpu_fixtures.evidence()
    provider_cpu_images(cpu, models)
    web_image = "sha256:" + identifier(2000 + index)
    cpu["images"]["web"]["Id"] = web_image
    cpu["compose"]["services"]["web"]["image"] = web_image
    remap = {
        row["Id"]: identifier(200 + index * 10 + offset)
        for offset, row in enumerate(cpu["containers"])
    }
    cpu["lease"]["api_container_id"] = remap[cpu["lease"]["api_container_id"]]
    for offset, row in enumerate(cpu["containers"]):
        row["Id"] = remap[row["Id"]]
        row["State"]["Pid"] = 5000 + index * 10 + offset
        row["NetworkSettings"]["SandboxID"] = identifier(
            500 + index * 10 + offset
        )
        if row["Config"]["Labels"]["com.docker.compose.service"] == "web":
            row["Image"] = row["Config"]["Image"] = web_image
    for network in cpu["networks"]:
        network["Containers"] = {
            remap[key]: value for key, value in network["Containers"].items()
        }
    cpu_fixtures.reseal(cpu)
    return cpu


def _core_containers(core, index, network, roles):
    remap = {
        row["Id"]: identifier(300 + index * 10 + offset)
        for offset, row in enumerate(core["core_containers"])
    }
    core_ids = {
        row["Config"]["Labels"]["com.docker.compose.service"]: remap[row["Id"]]
        for row in core["core_containers"]
        if row["Config"]["Labels"]["com.docker.compose.service"] in roles
    }
    for offset, row in enumerate(core["core_containers"]):
        role = row["Config"]["Labels"]["com.docker.compose.service"]
        if role in core_ids:
            row["Id"] = core_ids[role]
            row["NetworkSettings"]["Networks"][network["Name"]][
                "NetworkID"
            ] = network["Id"]
        else:
            row["Id"] = identifier(300 + index * 10 + offset)
            row["HostConfig"]["NetworkMode"] = (
                "container:" + core_ids["fess-search"]
            )
        row["State"]["Pid"] = 6000 + index * 10 + offset
    return core_ids


def _attach_catalog(cpu, core_ids, network):
    for record in (cpu["lease"], cpu["catalog_identity"]):
        record["core_container_ids"] = dict(core_ids)
        record["catalog_network_id"] = network["Id"]
    cpu["lease"]["network_ids"][network["Name"]] = network["Id"]
    cpu["containers"][0]["NetworkSettings"]["Networks"][network["Name"]][
        "NetworkID"
    ] = network["Id"]


def _catalog(index, cpu, cpus):
    project = cpu["lease"]["catalog_project"]
    producer = cpu["lease"].get("catalog_revision", cpu["lease"]["revision"])
    core = leased_runtime_evidence(catalog_revision=producer)
    network = copy.deepcopy(cpu["catalog_network"])
    network.update(Driver="bridge", Scope="local", Ingress=False)
    core_ids = _core_containers(
        core, index, network, cpu["catalog_identity"]["core_container_ids"]
    )
    members = [
        member
        for member in cpus
        if member["lease"]["catalog_project"] == project
    ]
    for member in members:
        _attach_catalog(member, core_ids, network)
    network["Containers"] = {value: {} for value in core_ids.values()} | {
        member["lease"]["api_container_id"]: {} for member in members
    }
    # Preserve fresh producer authority; consumer projection owns the optional
    # catalog_revision field, and must not mutate this record.
    identity = {
        key: copy.deepcopy(cpu["catalog_identity"][key])
        for key in client_lease.CATALOG_FIELDS
    }
    context = {
        "plan": core["core_plan"],
        "compose": core["core_compose"],
        "repo": Path("/private/catalog-source") / producer,
        "source_revision": producer,
        "query_profile": {
            "MODEL": "fixed",
            "MODEL_DIGEST": "a" * 64,
            "DIMENSIONS": 768,
        },
    }
    binding = catalog.Catalog(
        Path("/private/catalog") / (producer + ".json"),
        gpu_fixtures.product(),
        None,
        context,
        {
            "catalog_identity": identity,
            "runtime": {
                "container_ids": {
                    row["Config"]["Labels"]["com.docker.compose.service"]: row[
                        "Id"
                    ]
                    for row in core["core_containers"]
                },
                "network_id": network["Id"],
            },
        },
        core["core_images"],
        core["core_image_inspections"],
        {},
    )
    return binding, core["core_containers"], network


def _catalogs(cpus):
    bindings, groups, networks = {}, {}, {}
    for index, cpu in enumerate(cpus):
        project = cpu["lease"]["catalog_project"]
        if project in bindings:
            continue
        binding, rows, network = _catalog(index, cpu, cpus)
        bindings[project] = binding
        groups[project] = rows
        networks[network["Name"]] = network
    return bindings, groups, networks


def _host(index, cpu, binding):
    return SimpleNamespace(
        compose=cpu["compose"],
        images=cpu["images"],
        lease=cpu["lease"],
        context=binding.context,
        authority=binding.authority,
        image_proof=cpu["image_proof"],
        expected_runtime=None,
        backend_sha256="b" * 64 if index == 1 else None,
        catalog=binding,
        namespace_sha256=runtime.namespace_digest(cpu["containers"]),
        source_root=Path("/private/backend-source") / str(index),
        source_revision=cpu["lease"]["revision"],
        config=binding.config,
    )


def _gpu(hosts, models):
    compose = composition._inference(
        gpu_fixtures.product(),
        "a" * 40,
        21599,
        564,
        inference_source_sha256="6" * 64,
    )
    for role, image in models.items():
        compose["services"][role]["image"] = image["Id"]
    rows, networks = gpu_fixtures.snapshot(compose, models)
    remap = {
        row["Id"]: identifier(100 + index) for index, row in enumerate(rows)
    }
    for index, row in enumerate(rows):
        row["Id"] = remap[row["Id"]]
        row["State"]["Pid"] = 7000 + index
        row["NetworkSettings"]["SandboxID"] = identifier(700 + index)
        for name, attachment in row["NetworkSettings"]["Networks"].items():
            attachment["NetworkID"] = hosts[0].lease["network_ids"][name]
    for network in networks:
        network["Id"] = hosts[0].lease["network_ids"][network["Name"]]
        network["Containers"] = {
            remap[key]: value for key, value in network["Containers"].items()
        } | {host.lease["api_container_id"]: {} for host in hosts}
    return compose, rows, networks


def _provider(compose, models, hosts, groups, networks):
    provider = runtime.Provider.__new__(runtime.Provider)
    provider.inference_compose, provider.inference_images = compose, models
    provider.anchor = {
        "namespace_sha256": runtime.namespace_digest(groups[compose["name"]])
    }
    provider.backends = [hosts[1]]
    provider._image_bindings, provider._topology = {}, None
    provider.verify_inference = Mock()
    provider._snapshot = Mock(return_value=(groups, networks))
    provider.last_gpu = None
    return provider


def _backend_proofs(hosts, cpus, groups, networks):
    clients = [runtime._client(host, groups, networks) for host in hosts]
    client_lease.validate_clients(clients)
    for host, cpu, member in zip(hosts, cpus, clients):
        host.expected_runtime = cpu_runtime.validate_runtime(
            **{
                **cpu,
                "catalog_identity": member["catalog_identity"],
                "catalog_network": networks[
                    host.lease["catalog_project"] + "_search_backend"
                ],
            },
            shared_clients=clients,
        )


def dual_snapshot(*, separate_catalogs=False):
    models = gpu_fixtures.images()
    models["scope-model"]["Config"]["Cmd"] = ["serve"]
    cpus = [
        _cpu(member, index, models)
        for index, member in enumerate(
            shared_evidence(separate_catalogs=separate_catalogs)
        )
    ]
    bindings, groups, networks = _catalogs(cpus)
    hosts = []
    for index, cpu in enumerate(cpus):
        host = _host(index, cpu, bindings[cpu["lease"]["catalog_project"]])
        hosts.append(host)
        groups[host.compose["name"]] = cpu["containers"]
        networks.update({row["Name"]: row for row in cpu["networks"]})
    compose, rows, model_networks = _gpu(hosts, models)
    groups[compose["name"]] = rows
    networks.update({network["Name"]: network for network in model_networks})
    all_ids = [row["Id"] for members in groups.values() for row in members]
    assert len(all_ids) == len(set(all_ids))
    _backend_proofs(hosts, cpus, groups, networks)
    return SimpleNamespace(
        provider=_provider(compose, models, hosts, groups, networks),
        hosts=hosts,
        catalogs=list(bindings.values()),
        groups=groups,
        networks=networks,
    )
