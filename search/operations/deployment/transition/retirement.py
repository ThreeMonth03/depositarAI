"""Remove only one sealed CPU backend; preserve shared provider and data."""

from __future__ import annotations

import copy

from search.operations.deployment import inference_runtime, runtime_docker
from search.operations.deployment.candidate import catalog
from search.operations.deployment.images import build
from search.operations.deployment.provider import runtime
from search.operations.deployment.release import runtime as release_runtime

from . import contract


STABLE_CONTAINER = ("Id", "Image", "Created", "Config", "HostConfig", "Mounts")
STABLE_NETWORK = (
    "Id",
    "Name",
    "Driver",
    "Scope",
    "Internal",
    "Attachable",
    "Ingress",
    "Labels",
    "Options",
    "IPAM",
)


def disposal(owner, host, snapshot):
    website = host.compose["networks"]["website"]["name"]
    network = snapshot["networks"][website]
    protected = {
        value
        for peer in owner.members()
        if peer is not host
        for value in peer.expected_runtime["network_ids"].values()
    }
    contract.require(network["Id"] not in protected, "resource_protected")
    result = {
        "cpu_container_ids": copy.deepcopy(
            host.expected_runtime["container_ids"]
        ),
        "exclusive_network_ids": [network["Id"]],
        "lease_sha256": contract.digest(host.lease),
        "retain_paths": sorted(
            {
                path
                for peer in owner.members()
                for path in peer.record["owned_paths"]
            }
        ),
    }
    contract._resources(result)
    return result


def seal(owner, operation, host):
    """Generate the cleanup baseline from a fresh complete owner snapshot."""
    contract.require(operation.refresh() == ("pending", None), "phase")
    snapshot = owner.provider.observe(owner.members(), owner.catalogs())
    own = host.compose["name"]
    value = {
        "schema_version": 1,
        "operation_id": operation.operation_id,
        "intent_sha256": operation.sha256,
        "backend_sha256": host.backend_sha256,
        "resources": disposal(owner, host, snapshot),
        "containers": copy.deepcopy(snapshot["groups"][own]),
        "network": copy.deepcopy(
            snapshot["networks"][host.compose["networks"]["website"]["name"]]
        ),
    }
    return operation.ensure("cleanup-baseline", value)


class Cleanup:
    """Admit partial absence only for this operation's irreversible outcome."""

    def __init__(self, owner, operation):
        self.owner, self.operation = owner, operation
        self.provider = owner.provider
        self.baseline = operation.read("cleanup-baseline")
        contract.require(self.baseline is not None, "cleanup_baseline_missing")
        self._decision()
        matches = [
            host
            for host in owner.members()
            if host.backend_sha256 == self.baseline["backend_sha256"]
        ]
        contract.require(len(matches) == 1, "cleanup_changed")
        self.host = matches[0]
        self.survivors = [
            host for host in owner.members() if host is not self.host
        ]
        self.last = None
        self._baseline_admission()

    def _decision(self):
        operation = self.operation
        kind = operation.intent["kind"]
        if kind == "backend-code-update":
            decision = operation.read("discard")
            contract.require(decision is not None, "cleanup_not_authorized")
            contract.discard(decision, operation.intent)
            expected = operation.intent["candidate"]["backend"]["sha256"]
            resources = decision["resources"]
        else:
            contract.require(kind == "backend-code-retire", "kind_invalid")
            contract.require(
                operation.read("retire-dispatch") is not None,
                "cleanup_not_authorized",
            )
            expected = operation.intent["predecessor_roles"][
                "rollback_backend_sha256"
            ]
            resources = operation.intent["retirement"]["dispose"]
        contract.require(
            self.baseline["backend_sha256"] == expected
            and self.baseline["resources"] == resources,
            "cleanup_changed",
        )

    def _baseline_admission(self):
        value, operation = self.baseline, self.operation
        contract.version(
            value,
            {
                "schema_version",
                "operation_id",
                "intent_sha256",
                "backend_sha256",
                "resources",
                "containers",
                "network",
            },
        )
        contract.require(
            value["operation_id"] == operation.operation_id
            and value["intent_sha256"] == operation.sha256,
            "cleanup_changed",
        )
        indexed, _ids = release_runtime._container_inventory(
            self.host.compose, self.host.images, value["containers"]
        )
        contract.require(
            {role: row["Id"] for role, row in indexed.items()}
            == self.host.expected_runtime["container_ids"],
            "cleanup_changed",
        )
        contract.require(
            value["resources"]["lease_sha256"]
            == contract.digest(self.host.lease),
            "cleanup_changed",
        )
        contract.require(
            value["resources"]["exclusive_network_ids"]
            == [value["network"]["Id"]],
            "cleanup_changed",
        )

    def _projects(self):
        provider = self.provider
        values = {
            provider.inference_compose["name"]: {
                value["name"]
                for value in provider.inference_compose["networks"].values()
            }
        }
        for binding in self.owner.catalogs():
            project = binding.context["plan"]["project"]
            values[project] = {project + "_search_backend"}
        for host in self.owner.members():
            values[host.compose["name"]] = {
                host.compose["networks"]["website"]["name"]
            }
        return values

    def _inventory(self):
        projects = self._projects()
        found = runtime_docker.project_inventories(
            self.provider.docker, list(projects)
        )
        ids, names = set(), set()
        for name, networks in projects.items():
            containers, actual = found[name]
            if name == self.host.compose["name"]:
                contract.require(
                    set(containers)
                    <= set(
                        self.baseline["resources"][
                            "cpu_container_ids"
                        ].values()
                    )
                    and actual <= networks,
                    "cleanup_changed",
                )
                contract.require(
                    not containers or actual == networks, "cleanup_changed"
                )
            else:
                contract.require(
                    len(containers) == 3 and actual == networks,
                    "runtime_changed",
                )
            contract.require(
                not ids.intersection(containers), "runtime_changed"
            )
            ids.update(containers)
            names.update(actual)
        rows = build._json(
            self.provider.docker.call(["inspect", *sorted(ids)])
        )
        networks = build._json(
            self.provider.docker.call(["network", "inspect", *sorted(names)])
        )
        contract.require(
            type(rows) is list
            and len(rows) == len(ids)
            and {row.get("Id") for row in rows} == ids,
            "runtime_changed",
        )
        contract.require(
            type(networks) is list
            and len(networks) == len(names)
            and {row.get("Name") for row in networks} == names,
            "runtime_changed",
        )
        return (
            {
                name: [row for row in rows if row["Id"] in set(found[name][0])]
                for name in projects
            },
            {row["Name"]: row for row in networks},
        )

    def _partial_containers(self, rows, networks):
        original = {row["Id"]: row for row in self.baseline["containers"]}
        for row in rows:
            saved = original[row["Id"]]
            contract.require(
                all(
                    row.get(key) == saved.get(key) for key in STABLE_CONTAINER
                ),
                "cleanup_changed",
            )
            state = row["State"]
            contract.require(
                row["RestartCount"] == saved["RestartCount"]
                and state["StartedAt"] == saved["State"]["StartedAt"],
                "runtime_changed",
            )
            if state["Running"] is True:
                contract.require(
                    state["Pid"] == saved["State"]["Pid"]
                    and row["NetworkSettings"]["SandboxID"]
                    == saved["NetworkSettings"]["SandboxID"],
                    "runtime_changed",
                )
            else:
                contract.require(
                    state["Running"] is False and state["Pid"] == 0,
                    "runtime_changed",
                )
                contract.require(
                    all(
                        row["Id"] not in (network.get("Containers") or {})
                        for network in networks.values()
                    ),
                    "runtime_changed",
                )
            self._attachments(row, saved, networks)

    def _attachments(self, row, saved, networks):
        actual = row["NetworkSettings"]["Networks"]
        expected = saved["NetworkSettings"]["Networks"]
        contract.require(set(actual) <= set(expected), "runtime_changed")
        for name, attached in actual.items():
            contract.require(
                attached["NetworkID"] == expected[name]["NetworkID"],
                "runtime_changed",
            )
            if row["State"]["Running"]:
                contract.require(
                    attached == expected[name]
                    and row["Id"] in networks[name]["Containers"],
                    "runtime_changed",
                )

    def _partial_network(self, rows, networks):
        saved = self.baseline["network"]
        current = networks.get(saved["Name"])
        if current is None:
            contract.require(not rows, "cleanup_changed")
            return
        contract.require(
            all(current.get(key) == saved.get(key) for key in STABLE_NETWORK),
            "runtime_changed",
        )
        expected = {row["Id"] for row in rows if row["State"]["Running"]}
        contract.require(
            set(current.get("Containers") or {}) == expected, "runtime_changed"
        )

    def observe(self):
        self.operation.refresh()
        self._decision()
        self.provider._refresh_authorities()
        groups, networks = self._inventory()
        rows = groups[self.host.compose["name"]]
        self._partial_containers(rows, networks)
        self._partial_network(rows, networks)
        if self.operation.read("cleanup-complete") is not None:
            contract.require(
                not rows and self.baseline["network"]["Name"] not in networks,
                "cleanup_changed",
            )
        snapshot = self._protected(groups, networks, rows)
        self.last = snapshot
        return snapshot

    def _protected(self, groups, networks, rows):
        clients = [
            runtime._client(host, groups, networks) for host in self.survivors
        ]
        if any(
            row["Id"] == self.host.lease["api_container_id"]
            and row["State"]["Running"] is True
            for row in rows
        ):
            clients.append(runtime._client(self.host, groups, networks))
        snapshots = {
            binding.context["plan"]["project"]: catalog.observe(
                binding, groups, networks, clients
            )
            for binding in self.owner.catalogs()
        }
        provider = self.provider
        gpu_rows = groups[provider.inference_compose["name"]]
        gpu = inference_runtime.validate_shared_runtime(
            provider.inference_compose,
            provider.inference_images,
            gpu_rows,
            [
                networks[value["name"]]
                for value in provider.inference_compose["networks"].values()
            ],
            clients,
        )
        provider.verify_inference(gpu)
        contract.require(
            runtime.namespace_digest(gpu_rows)
            == provider.anchor["namespace_sha256"],
            "runtime_changed",
        )
        proofs = {
            host.compose["name"]: runtime._backend_runtime(
                host, groups, networks, clients, release_runtime
            )
            for host in self.survivors
        }
        provider.last_gpu, provider.last_backend_proofs = gpu, proofs
        provider._topology = (
            tuple(self.survivors),
            tuple(self.owner.catalogs()),
        )
        provider._topology_sha256 = runtime._topology_identity(
            *provider._topology
        )
        return {
            "groups": groups,
            "networks": networks,
            "gpu": gpu,
            "backend_proofs": proofs,
            "catalog_snapshots": snapshots,
        }

    def proof(self):
        contract.require(self.last is not None, "fresh_observation_required")
        rows = self.last["groups"][self.host.compose["name"]]
        network = self.baseline["network"]
        return {
            "backend_sha256": self.host.backend_sha256,
            "remaining_container_ids": sorted(row["Id"] for row in rows),
            "exclusive_network_absent": network["Name"]
            not in self.last["networks"],
            "runtime_lease_closed": not rows
            and network["Name"] not in self.last["networks"],
            "retained_paths": self.baseline["resources"]["retain_paths"],
        }

    def _dispatch(self, name, observation):
        self.operation.ensure(
            name,
            {
                "schema_version": 1,
                "operation_id": self.operation.operation_id,
                "intent_sha256": self.operation.sha256,
                "observed_sha256": contract.digest(observation),
            },
        )

    def finish(self):
        """Reobserve owned identities before each bounded continuation."""
        snapshot = self.observe()
        rows = snapshot["groups"][self.host.compose["name"]]
        if rows:
            identifiers = sorted(row["Id"] for row in rows)
            self._dispatch(
                "remove-cpu-" + contract.digest(identifiers), identifiers
            )
            self.provider.docker.call(
                ["rm", "--force", *identifiers], timeout=60
            )
            snapshot = self.observe()
            contract.require(
                not snapshot["groups"][self.host.compose["name"]],
                "cleanup_incomplete",
            )
        network = self.baseline["network"]
        if network["Name"] in snapshot["networks"]:
            self._dispatch("remove-network", network["Id"])
            self.provider.docker.call(
                ["network", "rm", network["Id"]], timeout=30
            )
            self.observe()
        proof = self.proof()
        contract.require(proof["runtime_lease_closed"], "cleanup_incomplete")
        self.operation.ensure(
            "cleanup-complete",
            {
                "schema_version": 1,
                "operation_id": self.operation.operation_id,
                "intent_sha256": self.operation.sha256,
                "baseline_sha256": contract.digest(self.baseline),
                "proof": proof,
            },
        )
        return proof
