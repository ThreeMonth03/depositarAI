"""Keep optional trace writes narrower than the readonly frontend inputs."""

import copy
import json
import unittest
from unittest.mock import Mock

from search.operations.deployment.frontend import check, policy, traces
from tests.unit.deployment.frontend.test_policy import composition


def with_traces():
    config = composition()
    web = config["services"]["web"]
    web["environment"]["WEB_TRACE_ROOT"] = traces.TARGET
    web["volumes"].append(
        {
            "type": "bind",
            "source": "/absolute/private/search-traces",
            "target": traces.TARGET,
            "read_only": False,
            "bind": {"create_host_path": False},
        }
    )
    return config


class TraceMountPolicyTests(unittest.TestCase):
    def test_default_and_explicit_trace_output_keep_input_readonly(self):
        for config in (composition(), with_traces()):
            before = copy.deepcopy(config)
            self.assertIsNone(policy.validate(config))
            self.assertEqual(before, config)
        config = with_traces()
        config["services"]["web"]["volumes"][0]["read_only"] = False
        with self.assertRaisesRegex(ValueError, "read-only bind inputs"):
            policy.validate(config)

    def test_mismatched_extra_and_implicit_mounts_are_rejected(self):
        mutations = {
            "missing_environment": lambda web: web["environment"].pop(
                "WEB_TRACE_ROOT"
            ),
            "missing_mount": lambda web: web["volumes"].pop(),
            "wrong_environment": lambda web: web["environment"].update(
                WEB_TRACE_ROOT="/arbitrary/output"
            ),
            "readonly": lambda web: web["volumes"][-1].update(read_only=True),
            "auto_create": lambda web: web["volumes"][-1]["bind"].update(
                create_host_path=True
            ),
            "implicit_create": lambda web: web["volumes"][-1].pop("bind"),
            "relative": lambda web: web["volumes"][-1].update(source="data"),
            "volume": lambda web: web["volumes"][-1].update(type="volume"),
            "duplicate": lambda web: web["volumes"].append(
                copy.deepcopy(web["volumes"][-1])
            ),
            "wrong_target": lambda web: web["volumes"][-1].update(
                target="/arbitrary/output"
            ),
            "docker_socket": lambda web: web["volumes"][-1].update(
                source="/var/run/docker.sock"
            ),
            "input_overlap": lambda web: web["volumes"][-1].update(
                source="/absolute/private"
            ),
        }
        for name, mutate in mutations.items():
            config = with_traces()
            mutate(config["services"]["web"])
            with self.subTest(name=name), self.assertRaises(ValueError):
                policy.validate(config)

    def test_disabled_trace_root_is_an_empty_string_or_absent(self):
        config = composition()
        environment = config["services"]["web"]["environment"]
        environment["WEB_TRACE_ROOT"] = ""
        self.assertIsNone(policy.validate(config))
        for value in (False, 0, None):
            environment["WEB_TRACE_ROOT"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.validate(config)

    def test_arbitrary_writable_bind_and_nginx_output_stay_rejected(self):
        config = with_traces()
        config["services"]["web"]["volumes"].append(
            {"type": "bind", "source": "/private/other", "target": "/other"}
        )
        with self.assertRaisesRegex(ValueError, "read-only bind inputs"):
            policy.validate(config)
        config = with_traces()
        config["services"]["web-nginx"]["volumes"] = [
            config["services"]["web"]["volumes"].pop()
        ]
        with self.assertRaises(ValueError):
            policy.validate(config)

    def test_trace_output_cannot_overlap_intranet_nginx_acl(self):
        config = composition(intranet=True)
        web = config["services"]["web"]
        web["environment"]["WEB_TRACE_ROOT"] = traces.TARGET
        web["volumes"].append(with_traces()["services"]["web"]["volumes"][-1])
        config["services"]["web-nginx"]["volumes"][0]["source"] = (
            "/absolute/private/search-traces/intranet-allow.conf"
        )
        with self.assertRaisesRegex(ValueError, "overlaps a readonly input"):
            policy.validate(config, intranet=True)

    def test_limits_are_finite_and_only_enabled_with_output(self):
        for name, value in (
            ("WEB_TRACE_TTL_SECONDS", "0"),
            ("WEB_TRACE_MAX_TURNS", "101"),
            ("WEB_TRACE_MAX_SESSIONS", "10001"),
            ("WEB_TRACE_QUEUE_ITEMS", "1.5"),
            ("WEB_TRACE_MAX_TOTAL_BYTES", "1073741825"),
            ("WEB_TRACE_SHUTDOWN_SECONDS", "nan"),
            ("WEB_TRACE_SHUTDOWN_SECONDS", "inf"),
            ("WEB_TRACE_SHUTDOWN_SECONDS", "11"),
        ):
            config = with_traces()
            config["services"]["web"]["environment"][name] = value
            with (
                self.subTest(name=name, value=value),
                self.assertRaises(ValueError),
            ):
                policy.validate(config)
        config = composition()
        config["services"]["web"]["environment"]["WEB_TRACE_QUEUE_ITEMS"] = "4"
        with self.assertRaisesRegex(ValueError, "require an enabled"):
            policy.validate(config)
        config = with_traces()
        config["services"]["web"]["environment"].update(
            WEB_TRACE_QUEUE_ITEMS="4", WEB_TRACE_SHUTDOWN_SECONDS="0.1"
        )
        self.assertIsNone(policy.validate(config))

    def test_ambiguous_host_directory_is_rejected_before_compose(self):
        for value in (
            "/",
            "relative",
            "/private/../data",
            "/data//trace",
            "/a\n",
        ):
            with self.subTest(value=value):
                self.assertFalse(traces.absolute_directory(value))
        self.assertTrue(traces.absolute_directory("/private/search traces"))

    def test_checker_selects_trace_override_from_dedicated_env_render(self):
        first = composition()
        first["services"]["web"]["environment"]["WEB_TRACE_ROOT"] = (
            "/private/selected-traces"
        )
        final = with_traces()
        client = Mock()
        client.call.side_effect = [
            json.dumps(value).encode() for value in (first, final)
        ]
        result = check._compose(
            client,
            {
                "deployment_mode": "private",
                "frontend_env_file": "/private/env",
            },
        )
        self.assertEqual(final, result)
        self.assertEqual(2, len(client.call.call_args_list))
        self.assertIn(
            str(check.ROOT / "deploy/compose/compose.search-web.traces.yaml"),
            client.call.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
