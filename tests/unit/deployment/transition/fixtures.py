"""Synthetic closed records; no product settings or native services."""

import copy

from search.operations.deployment.provider import contract as provider
from search.operations.deployment.transition import contract, journal
from tests.unit.deployment.provider.test_contract import records


def reference(path, digest="f" * 64):
    return {"path": str(path), "sha256": digest}


def backend_pair(control):
    anchor, a, roles = records()
    a["compose"]["name"] = "depositar-ai-release-" + "a" * 12 + "-" + "1" * 12
    b = copy.deepcopy(a)
    b["source"]["revision"] = "b" * 40
    b["build_state_root"] = "/private/build-B"
    b["compose"]["name"] = "depositar-ai-release-" + "b" * 12 + "-" + "2" * 12
    control.publish("provider", anchor)
    a_sha = control.publish("backend", a)
    b_sha = control.publish("backend", b)
    roles["active_backend_sha256"] = a_sha
    return a, b, roles, a_sha, b_sha


def tls_projection(backend, plan):
    project = backend["compose"]["name"]
    network = project + "_website"
    ids = backend["runtime"]["container_ids"]
    config = {
        "schema_version": 2,
        "backend_mode": "isolated-release",
        "api_container_id": ids["conversation-api"],
        "nginx_image": "sha256:" + "a" * 64,
        "bind_address": "192.0.2.10",
        "allowed_source": "192.0.2.20/32",
        "listen_port": 8443,
        "outer_server_name": "api.example.test",
        "inner_server_name": "inner.example.test",
        "release_compose": "/private/" + project + "/compose.json",
    }
    for name in (
        "ca_file",
        "outer_certificate_file",
        "outer_key_file",
        "inner_certificate_file",
        "inner_key_file",
    ):
        config[name] = "/private/tls/" + name
    peers = {
        role: {
            "container_id": identifier,
            "image_id": "sha256:" + "9" * 64,
            "pid": index + 100,
            "started_at": "2026-01-01T00:00:00Z",
            "sandbox_id": format(index, "064x"),
            "networks": {network: {"NetworkID": "8" * 64}},
        }
        for index, (role, identifier) in enumerate(sorted(ids.items()), 1)
    }
    api = peers["conversation-api"]
    bound = {
        "backend_mode": "isolated-release",
        "release_project": project,
        "release_compose_sha256": "7" * 64,
        "api_container_id": api["container_id"],
        "api_image": api["image_id"],
        "api_pid": api["pid"],
        "api_started_at": api["started_at"],
        "api_sandbox_id": api["sandbox_id"],
        "website_network": network,
        "website_network_id": "8" * 64,
        "api_address": "192.0.2.30",
        "peer_ids": sorted(ids.values()),
        "peer_proofs": peers,
    }
    return {
        "plan": plan,
        "config": config,
        "backend": bound,
        "material_sha256": "6" * 64,
        "files": {
            name: "5" * 64
            for name in ("inner.conf", "outer.conf", "compose.json")
        },
        "project": "depositar-private-api-tls",
        "compose_sha256": "4" * 64,
    }


def frontend_projection(revision):
    profile = {
        "schema_version": 3,
        "frontend_env_file": "/private/web.env",
        "frontend_revision": "e" * 40,
        "deployment_mode": "private",
        "conversation_contract_version": 4,
        "web_platform_manifest": "sha256:" + "1" * 64,
        "web_nginx_platform_manifest": "sha256:" + "2" * 64,
        "api_revision": revision,
        "catalog_manifest_sha256": "3" * 64,
    }
    current = {
        "profile": profile,
        "composition": {"name": "fixture"},
        "runtime": {},
        "nonce": None,
    }
    return {
        "delivery_config": reference("/private/delivery.json"),
        "canonical_profile": reference("/private/check.json"),
        "profile": profile,
        "env": reference("/private/web.env"),
        "composition_sha256": "4" * 64,
        "api_key_mount": reference("/private/api-key"),
        "ca_mount": None,
        "state": reference("/private/frontend/state.json"),
        "current": current,
        "previous": None,
        "namespace_sha256": "5" * 64,
        "monitor_lock_identity": "6" * 64,
    }


def operation_record(control, *, kind="backend-code-update"):
    a, b, roles, a_sha, b_sha = backend_pair(control)
    control.adopt_roles(roles)
    if kind != "backend-code-update":
        # Seed a previously accepted role snapshot through the existing writer;
        # this unit fixture does not claim a native update/adoption ceremony.
        previous = dict(roles)
        roles = {
            **roles,
            "epoch": roles["epoch"] + 1,
            "rollback_backend_sha256": b_sha,
        }
        control._compare_and_swap_roles(
            roles,
            expected_epoch=previous["epoch"],
            expected_sha256=contract.digest(previous),
        )
    before = {
        "backend": reference(
            control.root / ("backend-" + a_sha + ".json"), a_sha
        ),
        "tls": tls_projection(a, roles["tls_plan_ref"]),
        "frontend": frontend_projection(a["source"]["revision"]),
    }
    target = {
        "backend": reference(
            control.root / ("backend-" + b_sha + ".json"), b_sha
        ),
        "tls": tls_projection(b, reference("/private/tls/B/plan.json")),
        "frontend": frontend_projection(b["source"]["revision"]),
    }
    candidate = {
        "profile": reference("/private/candidate.json"),
        "journal_root": "/private/candidate",
        "record": reference("/private/candidate-record.json"),
        "original_epoch": 1,
        "original_roles_sha256": contract.digest(roles),
        "backend": target["backend"],
    }
    retirement = None
    if kind == "backend-code-retire":
        target = copy.deepcopy(before)
        retirement = {
            "decision": reference("/private/retirement.json"),
            "backend": reference(
                control.root / ("backend-" + b_sha + ".json"), b_sha
            ),
            "dispose": resources(b),
            "retain": {"paths": [], "network_ids": [], "records": []},
        }
    return {
        "schema_version": 1,
        "kind": kind,
        "operation_id": "a" * 32,
        "operator": {"root": "/private/operator", "revision": "c" * 40},
        "authority": {
            "control_config": reference(control.path, control.config_sha256),
            "control_root": str(control.root),
            "owner_uid": __import__("os").geteuid(),
            "namespace_sha256": journal.namespace_identity(control),
            "backend_peer_sha256": "7" * 64,
        },
        "predecessor_roles": roles,
        "predecessor_roles_sha256": contract.digest(roles),
        "candidate": candidate if kind == "backend-code-update" else None,
        "sides": {"predecessor": before, "target": target},
        "frontend_owner": {
            "transport_profile": reference("/private/ssh.json"),
            "participant_config": reference("/private/vm.json"),
            "source": {"root": "/private/vm-source", "revision": "d" * 40},
            "entry_sha256": "8" * 64,
            "peer_sha256": "9" * 64,
        },
        "limits": dict(contract.LIMITS),
        "retirement": retirement,
    }


def resources(backend):
    return {
        "cpu_container_ids": dict(backend["runtime"]["container_ids"]),
        "exclusive_network_ids": list(
            backend["runtime"]["network_ids"].values()
        ),
        "lease_sha256": provider.digest(backend["lease"]),
        "retain_paths": [],
    }


class ObservingOwner:
    """Unit journal double; complete owner harness uses real verifier seams."""

    def __init__(self):
        self.calls = []

    def prepare(self, operation):
        self.calls.append(("prepare", operation.operation_id))

    def observe(self, operation, side):
        self.calls.append(("observe", side))
        return {"side": side, "intent_sha256": operation.sha256}

    def require_undispatched(self, operation):
        self.calls.append(("undispatched", operation.operation_id))

    def reconcile(self, operation, side, *, decision):
        self.calls.append(("reconcile", side, provider.digest(decision)))

    def release(self, operation, decision):
        self.calls.append(("release", operation.operation_id))
        return {
            "released": operation.sha256,
            "decision": provider.digest(decision),
        }
