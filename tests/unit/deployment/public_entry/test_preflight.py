"""A public website must bind to one healthy private conversation backend."""

import copy
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment.public_entry import preflight


PROJECT = "depositar-ai-release-" + "a" * 12 + "-" + "b" * 12
NETWORK = PROJECT + "_website"
NETWORK_ID, API_ID = "c" * 64, "d" * 64
API_IMAGE = "sha256:" + "e" * 64
KEY_PATH = "/private/api-key"


def fixture():
    profile = {
        "x-depositar-plan": {
            "private_backend": {
                "schema_version": 1,
                "release_project": PROJECT,
                "network_name": NETWORK,
                "api_image": API_IMAGE,
            }
        },
        "networks": {"existing-release": {"external": True, "name": NETWORK}},
        "services": {
            "public-web": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": KEY_PATH,
                        "target": "/run/secrets/api_key",
                        "read_only": True,
                    }
                ]
            }
        },
    }
    release = {
        "name": PROJECT,
        "networks": {"website": {"name": NETWORK}},
        "services": {"conversation-api": {"image": API_IMAGE}},
    }
    network = {
        "Id": NETWORK_ID,
        "Name": NETWORK,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": True,
        "Labels": {
            "com.docker.compose.project": PROJECT,
            "com.docker.compose.network": "website",
        },
        "Containers": {API_ID: {"Name": PROJECT + "-conversation-api-1"}},
    }
    api = {
        "Id": API_ID,
        "Image": API_IMAGE,
        "Labels": {
            "com.docker.compose.project": PROJECT,
            "com.docker.compose.service": "conversation-api",
        },
        "State": {
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
            "Health": {"Status": "healthy"},
        },
        "Networks": {
            NETWORK: {"NetworkID": NETWORK_ID, "Aliases": ["conversation-api"]}
        },
        "Ports": {"8000/tcp": None},
        "PortBindings": {},
        "Mounts": [
            {
                "Type": "bind",
                "Source": KEY_PATH,
                "Destination": "/run/secrets/api_key",
                "RW": False,
            }
        ],
    }
    return profile, release, network, api


class MetadataRunner:
    def __init__(self, network, containers):
        self.network, self.containers = network, containers
        self.calls = []
        self.failure = None
        self.raw = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.failure:
            return self.failure
        if argv[:3] == ["docker", "network", "inspect"]:
            raw = json.dumps(self.network).encode() + b"\n"
        elif argv[:2] == ["docker", "inspect"]:
            raw = b"\n".join(json.dumps(row).encode() for row in self.containers)
        else:
            raise AssertionError("preflight must only inspect Docker metadata")
        return SimpleNamespace(
            returncode=0, stdout=self.raw if self.raw is not None else raw, stderr=b""
        )


class PrivateBackendPreflightTests(unittest.TestCase):
    def setUp(self):
        self.profile, self.release, self.network, self.api = fixture()
        self.runner = MetadataRunner(self.network, [self.api])

    def verify(self):
        return preflight.verify_private_backend(
            self.profile, self.release, runner=self.runner
        )

    def rejected(self, code):
        return self.assertRaisesRegex(
            preflight.PublicEntryPreflightError, "^public_entry_backend_" + code + "$"
        )

    def test_healthy_backend_uses_only_two_bounded_readonly_inspections(self):
        self.assertIsNone(self.verify())
        self.assertEqual(len(self.runner.calls), 2)
        for argv, options in self.runner.calls:
            self.assertEqual(options["timeout"], preflight.COMMAND_TIMEOUT)
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertNotIn(".Config.Env", " ".join(argv))
            self.assertNotIn("Health.Log", " ".join(argv))

    def test_existing_gateway_and_embedding_peers_are_allowed(self):
        for number, role in ((1, "web-nginx"), (2, "filtered-embedding")):
            identifier = str(number) * 64
            row = copy.deepcopy(self.api)
            row.update(Id=identifier)
            row["Labels"]["com.docker.compose.service"] = role
            row["Networks"][NETWORK]["Aliases"] = [role]
            self.network["Containers"][identifier] = {}
            self.runner.containers.append(row)
        self.verify()

    def test_wrong_network_identity_or_public_network_is_rejected(self):
        original = copy.deepcopy(self.network)
        for changed in (
            {"Name": "unrelated"},
            {"Driver": "overlay"},
            {"Scope": "swarm"},
            {"Internal": False},
            {"Id": "short"},
            {
                "Labels": {
                    "com.docker.compose.project": "different",
                    "com.docker.compose.network": "website",
                }
            },
            {
                "Labels": {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.network": "other",
                }
            },
        ):
            self.network.clear()
            self.network.update(copy.deepcopy(original), **changed)
            with self.subTest(changed=changed), self.rejected("network_invalid"):
                self.verify()

    def test_missing_network_and_daemon_errors_never_disclose_output(self):
        self.runner.failure = SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"private-secret-sentinel"
        )
        with self.rejected("inspection_failed") as caught:
            self.verify()
        self.assertNotIn("sentinel", str(caught.exception))

    def test_no_or_multiple_matching_api_services_are_rejected(self):
        self.api["Labels"]["com.docker.compose.service"] = "web"
        with self.rejected("ambiguous"):
            self.verify()
        self.api["Labels"]["com.docker.compose.service"] = "conversation-api"
        duplicate = copy.deepcopy(self.api)
        duplicate["Id"] = "f" * 64
        self.network["Containers"][duplicate["Id"]] = {}
        self.runner.containers.append(duplicate)
        with self.rejected("ambiguous"):
            self.verify()

    def test_dns_alias_cannot_be_missing_or_shared_with_foreign_peer(self):
        self.api["Networks"][NETWORK]["Aliases"] = []
        with self.rejected("ambiguous"):
            self.verify()
        self.api["Networks"][NETWORK]["Aliases"] = ["conversation-api"]
        foreign = copy.deepcopy(self.api)
        foreign["Id"] = "f" * 64
        foreign["Labels"]["com.docker.compose.project"] = "foreign"
        self.network["Containers"][foreign["Id"]] = {}
        self.runner.containers.append(foreign)
        with self.rejected("ambiguous"):
            self.verify()

    def test_stopped_unhealthy_paused_or_restarting_api_is_rejected(self):
        original = copy.deepcopy(self.api["State"])
        for changed in (
            {"Running": False},
            {"Paused": True},
            {"Restarting": True},
            {"Dead": True},
            {"Health": {"Status": "unhealthy"}},
            {"Health": None},
        ):
            self.api["State"] = {**original, **changed}
            with self.subTest(changed=changed), self.rejected("unhealthy"):
                self.verify()

    def test_wrong_immutable_image_and_replaced_network_are_rejected(self):
        self.api["Image"] = "sha256:" + "f" * 64
        with self.rejected("image_changed"):
            self.verify()
        self.api["Image"] = API_IMAGE
        self.api["Networks"][NETWORK]["NetworkID"] = "f" * 64
        with self.rejected("network_invalid"):
            self.verify()

    def test_published_api_ports_are_rejected_from_both_docker_fields(self):
        binding = {"HostIp": "127.0.0.1", "HostPort": "18000"}
        self.api["PortBindings"] = {"8000/tcp": [binding]}
        with self.rejected("ports_exposed"):
            self.verify()
        self.api["PortBindings"] = {}
        self.api["Ports"] = {"8000/tcp": [binding]}
        with self.rejected("ports_exposed"):
            self.verify()

    def test_key_mount_must_match_the_public_web_credential_readonly(self):
        original = copy.deepcopy(self.api["Mounts"][0])
        for changed in (
            {"RW": True},
            {"Source": "/private/other-key"},
            {"Type": "volume"},
            {"Destination": "/other"},
        ):
            self.api["Mounts"] = [{**original, **changed}]
            with self.subTest(changed=changed), self.rejected("key_changed"):
                self.verify()

    def test_peer_inventory_and_response_identity_are_bounded(self):
        self.network["Containers"] = {
            f"{value:064x}": {} for value in range(preflight.MAX_PEERS + 1)
        }
        with self.rejected("network_invalid"):
            self.verify()
        self.assertEqual(len(self.runner.calls), 1)
        self.network["Containers"] = {API_ID: {}}
        self.api["Id"] = "f" * 64
        with self.rejected("inspection_failed"):
            self.verify()

    def test_malformed_duplicate_or_nonfinite_json_is_closed(self):
        for raw in (b"not-json", b'{"Id":"a","Id":"b"}', b'{"value":NaN}', b"\xff"):
            self.runner.raw = raw
            with self.subTest(raw=raw), self.rejected("inspection_failed"):
                self.verify()

    def test_oversized_stdout_or_stderr_is_closed(self):
        for stdout, stderr in (
            (b"a" * (preflight.MAX_OUTPUT_BYTES + 1), b""),
            (b"", b"b" * (preflight.MAX_OUTPUT_BYTES + 1)),
        ):
            self.runner.failure = SimpleNamespace(
                returncode=0, stdout=stdout, stderr=stderr
            )
            with self.rejected("output_limit"):
                self.verify()

    def test_invalid_plan_never_invokes_docker(self):
        for image in ("mutable:latest", None, True):
            self.release["services"]["conversation-api"]["image"] = image
            with self.subTest(image=image), self.rejected("plan_invalid"):
                self.verify()
        self.assertEqual(self.runner.calls, [])

    def verify_bound(self):
        return preflight.verify_bound_backend(self.profile, runner=self.runner)

    def test_sealed_backend_plan_uses_existing_readonly_verifier_without_release_file(
        self,
    ):
        self.release.clear()
        original = copy.deepcopy(self.profile)
        self.assertIsNone(self.verify_bound())
        self.assertEqual(self.profile, original)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.runner.calls[0][0][-1], NETWORK)

    def test_legacy_missing_backend_plan_is_rejected_before_docker(self):
        for profile in (
            {},
            {**self.profile, "x-depositar-plan": {}},
            {**self.profile, "x-depositar-plan": None},
        ):
            with self.subTest(profile=profile), self.rejected("plan_invalid"):
                preflight.verify_bound_backend(profile, runner=self.runner)
        self.assertEqual(self.runner.calls, [])

    def test_sealed_plan_requires_exact_fields_and_integer_supported_version(self):
        original = self.profile["x-depositar-plan"]["private_backend"]
        invalid = [None, [], "invalid", {}, {**original, "unexpected": "ignored"}]
        invalid.extend(
            {key: value for key, value in original.items() if key != missing}
            for missing in original
        )
        invalid.extend(
            {**original, "schema_version": version}
            for version in (None, True, 1.0, "1", 0, 2)
        )
        for plan in invalid:
            self.profile["x-depositar-plan"]["private_backend"] = plan
            with self.subTest(plan=plan), self.rejected("plan_invalid"):
                self.verify_bound()
        self.assertEqual(self.runner.calls, [])

    def test_sealed_plan_identity_must_match_bound_network_before_docker(self):
        original = self.profile["x-depositar-plan"]["private_backend"]
        for changed in (
            {"release_project": "unrelated"},
            {"release_project": None},
            {"network_name": "foreign_website"},
            {"network_name": None},
            {"api_image": "mutable:latest"},
            {"api_image": None},
        ):
            self.profile["x-depositar-plan"]["private_backend"] = {
                **original,
                **changed,
            }
            with self.subTest(changed=changed), self.rejected("plan_invalid"):
                self.verify_bound()
        self.assertEqual(self.runner.calls, [])

    def test_wrong_live_image_is_rejected_instead_of_adopted_into_sealed_plan(self):
        original = copy.deepcopy(self.profile)
        self.api["Image"] = "sha256:" + "f" * 64
        with self.rejected("image_changed"):
            self.verify_bound()
        self.assertEqual(self.profile, original)
        self.assertEqual(len(self.runner.calls), 2)

    def test_bound_plan_preserves_unhealthy_and_key_mismatch_diagnostics(self):
        self.api["State"]["Paused"] = True
        with self.rejected("unhealthy"):
            self.verify_bound()
        self.api["State"]["Paused"] = False
        self.api["Mounts"][0]["Source"] = "/private/other-key"
        with self.rejected("key_changed"):
            self.verify_bound()

    def test_native_runner_reuses_existing_live_output_and_timeout_guard(self):
        responses = [
            SimpleNamespace(
                returncode=0, stdout=json.dumps(self.network).encode(), stderr=b""
            ),
            SimpleNamespace(
                returncode=0, stdout=json.dumps(self.api).encode(), stderr=b""
            ),
        ]
        with patch.object(
            preflight.image_build, "_run", side_effect=responses
        ) as capture:
            preflight.verify_private_backend(self.profile, self.release)
        self.assertEqual(capture.call_count, 2)
        for call in capture.call_args_list:
            self.assertEqual(call.kwargs["timeout"], preflight.COMMAND_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
