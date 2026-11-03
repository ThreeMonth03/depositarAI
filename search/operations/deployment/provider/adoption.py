"""Adopt one fully verified native provider into new control state."""

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import time

from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
from search.serialization import strict_json

from . import authority, contract, runtime


BOOTSTRAP_FIELDS = {
    "schema_version",
    "profile",
    "expected_revision",
    "release_receipt_sha256",
    "inference_receipt_sha256",
    "tls_plan_sha256",
}
PROFILE_FIELDS = {
    "schema_version",
    "state_root",
    "production_config",
    "production_source",
    "tls_plan",
}
QUIESCENT_FIELDS = {
    "schema_version",
    "phase",
    "revision",
    "source_origin",
    "policy",
    "container_ids",
    "lease",
    "build_complete",
    "compose",
    "catalog_binding",
    "baseline_receipt_sha256",
    "model_file_identity_sha256",
    "compute_sha256",
    "network_id",
    "external_network_ids",
    "runtime",
}


def _bootstrap(path, expected):
    contract.hexadecimal(expected)
    raw = authority.read_private(path, maximum=16384)
    contract.require(
        hashlib.sha256(raw).hexdigest() == expected, "bootstrap_changed"
    )
    value = strict_json(raw)
    contract.closed(value, BOOTSTRAP_FIELDS)
    contract.closed(value["profile"], PROFILE_FIELDS)
    for key in ("schema_version",):
        contract.require(
            type(value[key]) is int and value[key] == 1, "bootstrap_invalid"
        )
        contract.require(
            type(value["profile"][key]) is int and value["profile"][key] == 1,
            "bootstrap_invalid",
        )
    contract.hexadecimal(value["expected_revision"], contract.HEX40)
    for key in BOOTSTRAP_FIELDS:
        if key.endswith("_sha256"):
            contract.hexadecimal(value[key])
    for key in PROFILE_FIELDS - {"schema_version"}:
        contract.path(value["profile"][key])
    return value


def _fingerprint(path, expected):
    value = hashlib.sha256(authority.read_private(path)).hexdigest()
    contract.require(value == expected, "bootstrap_changed")
    return value


def _expected_native(bootstrap, config):
    _fingerprint(
        config.state_root / "release-candidate.json",
        bootstrap["release_receipt_sha256"],
    )
    _fingerprint(
        config.state_root / "inference-candidate.json",
        bootstrap["inference_receipt_sha256"],
    )
    _fingerprint(
        Path(bootstrap["profile"]["tls_plan"]), bootstrap["tls_plan_sha256"]
    )


@contextmanager
def _verified_native(bootstrap, bootstrap_sha256):
    from search.operations.state import locked_state, operation_receipts
    from search.operations.catalog import execution
    from search.operations.deployment.candidate import catalog, controller
    from search.operations.deployment.candidate.policy import (
        validate_shared_request_policy,
    )
    from search.operations.deployment.release import controller as native

    profile = {
        key: Path(value) if key != "schema_version" else value
        for key, value in bootstrap["profile"].items()
    }
    source = profile["production_source"]
    revision = require_clean_revision(source)
    contract.require(
        revision == bootstrap["expected_revision"], "source_changed"
    )
    config_reference = {
        "path": str(profile["production_config"]),
        "sha256": hashlib.sha256(
            authority.read_private(profile["production_config"])
        ).hexdigest(),
    }
    config = runtime.configuration(config_reference, source)
    with controller.journal(profile["state_root"]) as journal:
        prior = _quiescent(journal)
        with locked_state(config, create=False) as store:
            contract.require(
                not any(
                    value["status"] == "started"
                    for value in operation_receipts(store)
                ),
                "recovery_required",
            )
            _expected_native(bootstrap, config)
            plan = store.read("index-candidate.json")
            with execution.operation_lock({"data": Path(plan["data_root"])}):
                with ExitStack() as stack:
                    context, _ = catalog.load_context(
                        source, profile["production_config"], config, store
                    )
                    policy = validate_shared_request_policy(source, source)
                    state = _native_inputs(
                        stack,
                        config,
                        store,
                        context,
                        source,
                        revision,
                        bootstrap,
                    )
                    state.policy = policy
                    state.config_reference = config_reference
                    _candidate_absent(state, prior)
                    _verify_native(state, profile)
                    provider, backend = _records(
                        state, profile, bootstrap_sha256
                    )
                    yield provider, backend
                    _verify_native(state, profile)
                    state.inputs.refresh()
                native._final_authority(
                    config,
                    store,
                    revision,
                    context,
                    source,
                    None,
                    state.inputs,
                    state.deadline,
                )
            _expected_native(bootstrap, config)
            _fingerprint(
                profile["production_config"], config_reference["sha256"]
            )
            contract.require(
                require_clean_revision(source) == revision, "source_changed"
            )


def _quiescent(store):
    try:
        value = store.read("current.json")
    except FileNotFoundError:
        return None
    contract.require(
        type(value) is dict
        and set(value) <= QUIESCENT_FIELDS
        and {"schema_version", "phase", "revision", "container_ids"}
        <= set(value),
        "bootstrap_candidate",
    )
    contract.require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["phase"] == "stopped",
        "bootstrap_candidate",
    )
    contract.hexadecimal(value["revision"], contract.HEX40)
    contract.require(
        type(value["container_ids"]) is dict
        and set(value["container_ids"]) <= contract.CPU_ROLES,
        "bootstrap_candidate",
    )
    for identifier in value["container_ids"].values():
        contract.hexadecimal(identifier)
    return value


def _candidate_absent(state, prior):
    from search.operations.deployment.runtime_docker import project_inventory

    if prior is None or "compose" not in prior:
        return
    compose = prior["compose"]
    contract.require(
        type(compose) is dict and type(compose.get("name")) is str,
        "bootstrap_candidate",
    )
    name = compose["name"]
    contract.require(
        name.isascii()
        and len(name) <= 128
        and name.startswith("depositar-ai-release-")
        and name != state.host.compose["name"],
        "bootstrap_candidate",
    )
    contract.require(
        not any(project_inventory(state.host.docker, name)),
        "bootstrap_candidate",
    )


def _native_inputs(stack, config, store, context, source, revision, bootstrap):
    from types import SimpleNamespace
    from search.operations.deployment.release import controller
    from search.operations.deployment.images import build

    deadline = time.monotonic() + controller.runtime_docker.TOTAL_SECONDS
    inputs = controller._runtime_inputs(
        stack,
        config,
        store,
        revision,
        context,
        source,
        None,
        None,
        deadline,
    )
    name = inputs.host.compose["name"].removeprefix("depositar-ai-")
    parent = private_io.open_child_directory(store.fd, "releases")
    stack.callback(os.close, parent)
    slot = private_io.open_child_directory(parent, name)
    stack.callback(os.close, slot)
    complete = controller._load_completed(slot, inputs)
    contract.require(
        store.read("release-candidate.json") == complete, "authority_changed"
    )
    return SimpleNamespace(
        inputs=inputs,
        host=inputs.host,
        completed=complete,
        config=config,
        store=store,
        context=context,
        source=source,
        revision=revision,
        bootstrap=bootstrap,
        deadline=deadline,
        final_records=lambda: controller._verify_records(
            config, parent, slot, name, inputs, controller.FILES
        ),
        complete_record=lambda: build._record(slot, "complete.json"),
    )


def _verify_native(state, profile):
    from search.operations.catalog import query
    from search.operations.deployment import inference, inference_runtime
    from search.operations.deployment import private_api_tls
    from search.operations.deployment.candidate import probe
    from search.operations.deployment.images import build
    from search.operations.deployment.release import controller

    host, inputs = state.host, state.inputs
    contract.require(
        query.verify_authority(state.context, observe_runtime=host.observe)
        == inputs.authority,
        "authority_changed",
    )
    groups, networks = host.collect(clients=True)
    host.observe(snapshot=(groups, networks))
    contract.require(
        controller._completed_receipt(inputs.identity, host.last[1])
        == state.completed,
        "runtime_changed",
    )
    cpu = private_api_tls.gpu_ownership_proofs(profile["tls_plan"])
    inference._gpu(
        state.config,
        inference_runtime.ownership_proofs(
            host.inference_compose, host.last[0], host.inference_images
        ),
        None,
        cpu_proofs=cpu,
    )
    probe.verify_residency(
        host.docker, host.lease["api_container_id"], state.policy["policy"]
    )
    result = build._run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid",
            "--format=csv,noheader,nounits",
        ],
        timeout=15,
        environment={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
        },
    )
    contract.require(result.returncode == 0, "model_changed")
    compute = runtime._compute(state.config, result.stdout)
    namespaces = {
        "provider": runtime.namespace_digest(
            groups[host.inference_compose["name"]]
        ),
        "backend": runtime.namespace_digest(groups[host.compose["name"]]),
    }
    _seal_observation(state, compute, namespaces)
    state.final_records()
    contract.require(
        state.complete_record() == state.completed, "authority_changed"
    )


def _seal_observation(state, compute, namespaces):
    if hasattr(state, "compute_sha256"):
        contract.require(state.compute_sha256 == compute, "model_changed")
        contract.require(state.namespaces == namespaces, "runtime_changed")
    state.compute_sha256, state.namespaces = compute, namespaces


def _records(state, profile, bootstrap_sha256):
    from search.operations.deployment import source_bundle
    from search.operations.deployment.images import authority as images
    from search.operations.deployment.candidate.catalog import Catalog

    inputs, host = state.inputs, state.host
    plan = source_bundle.committed_source_plan(state.source, state.revision)
    authority_images = images.read_completed_images(
        state.config, state.store, plan
    )
    native = state.store.read("inference-candidate.json")
    identity = {
        key: native[key]
        for key in (
            "schema_version",
            "revision",
            "inference_source_sha256",
            "project",
            "compose_sha256",
            "model_manifest_sha256",
            "source_manifest_sha256",
            "image_ids",
            "image_config_sha256",
            "active_changed",
        )
    }
    provider = {
        "schema_version": 1,
        "kind": "model-provider",
        "source": {"root": str(state.source), "revision": state.revision},
        "configuration": dict(state.config_reference),
        "source_plan_sha256": contract.digest(plan),
        "inference_identity": identity,
        "inference_complete_sha256": contract.digest(native),
        "model_manifest_sha256": inputs.identity["model_manifest_sha256"],
        "model_file_identity_sha256": inputs.model_file_identity_sha256,
        "compute_sha256": state.compute_sha256,
        "policy": state.policy,
        "namespace_sha256": state.namespaces["provider"],
        "adopted_by_revision": require_clean_revision(
            Path(__file__).resolve().parents[4]
        ),
        "bootstrap_sha256": bootstrap_sha256,
    }
    binding = Catalog(
        profile["production_config"],
        state.config,
        state.store,
        state.context,
        inputs.authority,
        host.catalog_images,
        host.catalog_inspections,
        inputs.key,
    )
    backend = {
        "schema_version": 1,
        "kind": "backend",
        "source": dict(provider["source"]),
        "configuration": dict(provider["configuration"]),
        "catalog_source": dict(provider["source"]),
        "build_state_root": str(state.config.state_root),
        "compose": host.compose,
        "lease": host.lease,
        "runtime": host.last[1],
        "image_proof": host.image_proof,
        "image_authority_sha256": contract.digest(authority_images),
        "catalog_binding": binding.identity(),
        "owned_paths": [
            str(state.source),
            str(
                state.config.state_root
                / "releases"
                / host.compose["name"].removeprefix("depositar-ai-")
            ),
        ],
        "provider_sha256": contract.digest(provider),
        "namespace_sha256": state.namespaces["backend"],
    }
    return provider, backend


def adopt_running(config_path, bootstrap_path, *, expected_bootstrap_sha256):
    bootstrap = _bootstrap(bootstrap_path, expected_bootstrap_sha256)
    _disjoint(config_path, bootstrap_path, bootstrap)
    with authority.open_control(config_path, create=True) as control:
        contract.require(
            set(os.listdir(control.fd)) == {"operations.lock"},
            "already_adopted",
        )
        with _verified_native(bootstrap, expected_bootstrap_sha256) as records:
            provider, backend = records
        _bootstrap(bootstrap_path, expected_bootstrap_sha256)
        provider_sha256 = control.publish("provider", provider)
        contract.require(
            backend["provider_sha256"] == provider_sha256, "authority_changed"
        )
        backend_sha256 = control.publish("backend", backend)
        control.adopt_roles(
            {
                "schema_version": 1,
                "epoch": 1,
                "provider_sha256": provider_sha256,
                "active_backend_sha256": backend_sha256,
                "rollback_backend_sha256": None,
                "tls_plan_ref": {
                    "path": bootstrap["profile"]["tls_plan"],
                    "sha256": bootstrap["tls_plan_sha256"],
                },
                "pending_operation_sha256": None,
            }
        )
        report = {
            "status": "backend_provider_adopted",
            "provider_sha256": provider_sha256,
            "roles_sha256": control.roles_sha256,
            "epoch": 1,
            "active_changed": False,
        }
    return report


def _disjoint(config_path, bootstrap_path, bootstrap):
    from search.operations.config import load_config

    target = authority.load_config(config_path)["state_root"]
    profile = bootstrap["profile"]
    config = load_config(
        Path(profile["production_config"]),
        repo_root=Path(profile["production_source"]),
    )
    protected = [
        config.state_root,
        config.api_model_root,
        config.embedding_model_root,
        config.reranker_model_root,
        Path(profile["production_source"]),
        Path(profile["state_root"]),
        Path(bootstrap_path),
        Path(profile["tls_plan"]),
        Path(config_path),
    ]
    contract.require(
        not any(
            target == path or target in path.parents or path in target.parents
            for path in protected
        ),
        "config_invalid",
    )
