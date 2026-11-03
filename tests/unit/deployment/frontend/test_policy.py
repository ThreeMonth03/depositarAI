"""Preserve frontend admission order across policy responsibilities."""

import copy
import json
import unittest
from unittest.mock import Mock, call, patch

from search.operations.deployment.frontend import access, policy


def composition(*, intranet=False):
    """Return only the composition facts consumed by the pure policy."""
    address, port = (
        ("10.20.30.40", "18082") if intranet else ("127.0.0.1", "18081")
    )
    service = {
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "user": "1003:1003",
        "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=16m"],
        "mem_limit": 64 * 1024**2,
        "cpus": 0.25,
        "pids_limit": 64,
    }
    web, nginx = copy.deepcopy(service), copy.deepcopy(service)
    web.update(
        environment={
            "WEB_ORIGIN": f"http://{address}:{port}",
            "WEB_UPSTREAM": "https://backend.example.test",
            "WEB_UPSTREAM_CA_FILE": "",
            "NVIDIA_VISIBLE_DEVICES": "void",
        },
        networks=["website", "upstream-route"],
        volumes=[
            {
                "type": "bind",
                "source": "/absolute/private/example-api-key",
                "target": "/run/secrets/api_key",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
    )
    nginx.update(
        environment={},
        networks=["website", "ingress"],
        ports=[
            {
                "host_ip": address,
                "published": port,
                "target": 8080,
                "protocol": "tcp",
            }
        ],
    )
    if intranet:
        web["environment"]["WEB_ACCESS_MODE"] = "intranet-http"
        nginx["environment"]["WEB_ALLOWED_CIDRS"] = "10.20.30.0/24"
        nginx["volumes"] = [
            {
                "type": "bind",
                "source": "/absolute/private/intranet-allow.conf",
                "target": access.ACCESS_TARGET,
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ]
    return {
        "name": policy.PROJECTS["intranet-http" if intranet else "private"],
        "services": {"web": web, "web-nginx": nginx},
        "networks": {
            "website": {"internal": True},
            "upstream-route": {},
            "ingress": {},
        },
    }


class FrontendPolicyTests(unittest.TestCase):
    def assert_rejected(self, config, message, **options):
        before = json.dumps(config)
        admission = Mock()
        with (
            patch.multiple(
                access,
                intranet_settings=admission.settings,
                verify_access=admission.verify,
                prepare_access=admission.prepare,
            ),
            self.assertRaises(ValueError) as raised,
        ):
            policy.validate(config, **options)
        self.assertEqual(str(raised.exception), message)
        self.assertEqual(admission.mock_calls, [])
        self.assertEqual(json.dumps(config), before)

    def test_success_preserves_original_input_and_ordered_admission(self):
        for intranet, prepare in ((False, False), (True, False), (True, True)):
            config = composition(intranet=intranet)
            before = json.dumps(config)
            admission = Mock()
            with (
                self.subTest(intranet=intranet, prepare=prepare),
                patch.multiple(
                    access,
                    intranet_settings=admission.settings,
                    verify_access=admission.verify,
                    prepare_access=admission.prepare,
                ),
            ):
                self.assertIsNone(
                    policy.validate(config, intranet=intranet, prepare=prepare)
                )
            expected = []
            if intranet:
                expected = [
                    call.settings(config),
                    call.prepare(config) if prepare else call.verify(config),
                ]
            self.assertEqual(admission.mock_calls, expected)
            for recorded in admission.mock_calls:
                self.assertIs(recorded.args[0], config)
            self.assertEqual(json.dumps(config), before)

    def test_prepare_requires_intranet_before_reading_composition(self):
        self.assert_rejected(
            None, "admission preparation requires intranet mode", prepare=True
        )

    def test_runtime_restrictions_apply_to_each_service_before_its_ports(self):
        restrictions = (
            ("read_only", False, "read-only unprivileged runtime required"),
            ("cap_drop", [], "read-only unprivileged runtime required"),
            ("security_opt", [], "read-only unprivileged runtime required"),
            (
                "privileged",
                True,
                "website services must not allocate GPUs or host namespaces",
            ),
            ("user", "0:0", "non-root numeric user required"),
            ("tmpfs", [], "one bounded /tmp mount required"),
            ("mem_limit", 0, "resource limits required"),
            ("cpus", 0, "resource limits required"),
            ("pids_limit", 0, "resource limits required"),
        )
        for role in ("web", "web-nginx"):
            for field, value, message in restrictions:
                config = composition()
                config["services"][role][field] = value
                config["services"][role]["ports"] = [{"published": "9999"}]
                with self.subTest(role=role, field=field):
                    self.assert_rejected(config, message)

    def test_service_iteration_order_determines_first_error(self):
        config = composition()
        config["services"]["web"]["read_only"] = False
        config["services"]["web-nginx"]["user"] = "0:0"
        self.assert_rejected(config, "read-only unprivileged runtime required")
        config["services"] = dict(reversed(list(config["services"].items())))
        self.assert_rejected(config, "non-root numeric user required")

    def test_environment_precedes_runtime_and_admission(self):
        config = composition(intranet=True)
        config["services"]["web"]["environment"]["EXTRA"] = "fixture"
        config["services"]["web"]["read_only"] = False
        self.assert_rejected(
            config,
            "standalone frontend environment policy drift",
            intranet=True,
        )

    def test_mount_checks_precede_port_and_secret_target_checks(self):
        config = composition()
        web = config["services"]["web"]
        web["volumes"][0]["read_only"] = False
        web["volumes"][0]["target"] = "/run/secrets/other"
        web["ports"] = [{"published": "9999"}]
        self.assert_rejected(
            config, "only existing read-only bind inputs permitted"
        )
        web["volumes"][0]["read_only"] = True
        self.assert_rejected(config, "private backend cannot publish a port")

    def test_internal_network_and_upstream_precede_topology_and_inputs(self):
        config = composition()
        config["networks"]["website"]["internal"] = False
        config["networks"]["extra"] = {}
        config["services"]["web"]["environment"]["WEB_UPSTREAM"] = (
            "http://backend.example.test"
        )
        config["services"]["web"]["volumes"] = []
        self.assert_rejected(
            config, "website backend network must be internal"
        )
        config["networks"]["website"]["internal"] = True
        self.assert_rejected(
            config,
            "HTTP requires an explicit intranet mode or local private demo",
        )
        config["services"]["web"]["environment"]["WEB_UPSTREAM"] = (
            "https://backend.example.test"
        )
        self.assert_rejected(config, "remote frontend network policy drift")
        del config["networks"]["extra"]
        self.assert_rejected(
            config, "remote frontend receives only its API key and optional CA"
        )

    def test_optional_ca_requires_exact_mount_and_fixed_target(self):
        config = composition()
        web = config["services"]["web"]
        ca = "/run/certs/upstream-ca.pem"
        web["environment"]["WEB_UPSTREAM_CA_FILE"] = ca
        mount = copy.deepcopy(web["volumes"][0])
        mount.update(source="/absolute/private/example-ca.pem", target=ca)
        web["volumes"].append(mount)
        self.assertIsNone(policy.validate(config))
        web["environment"]["WEB_UPSTREAM_CA_FILE"] = "/run/certs/other.pem"
        self.assert_rejected(
            config, "private upstream CA requires its fixed read-only mount"
        )

    def test_existing_json_value_distinctions_are_not_normalized(self):
        for field, value in (("read_only", 1), ("pids_limit", True)):
            config = composition()
            config["services"]["web"][field] = value
            before = json.dumps(config)
            with self.subTest(field=field):
                self.assertIsNone(policy.validate(config))
                self.assertEqual(json.dumps(config), before)
        config = composition()
        port = config["services"]["web-nginx"]["ports"][0]
        port.update(published=18081, target=8080.0)
        self.assertIsNone(policy.validate(config))
        port["target"] = True
        self.assert_rejected(
            config, "only the configured frontend address and port permitted"
        )

    def test_missing_structure_keeps_original_exception_types(self):
        with self.assertRaises(KeyError) as raised:
            policy.validate({})
        self.assertEqual(raised.exception.args, ("services",))
        config = composition()
        config["services"]["web"]["tmpfs"] = None
        with self.assertRaises(TypeError):
            policy.validate(config)


if __name__ == "__main__":
    unittest.main()
