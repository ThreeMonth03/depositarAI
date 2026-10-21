"""One native quota namespace and a small adapter with no admission algorithm."""

import json
from types import SimpleNamespace
import unittest

from search.operations.deployment.public_entry.identity import render_identity
from search.operations.deployment.public_entry.quota import render_counter


def policy(**changes):
    return SimpleNamespace(
        **{
            "access_mode": "mixed",
            "public_origin": "https://public.example.org",
            "oidc_issuer": "https://identity.example.org",
            "oidc_client_id": "depositar-test",
            "quota_per_guest": 3,
            "quota_per_account": 10,
            "quota_guest_pool": 20,
            "quota_user_pool": 80,
            "quota_global": 100,
            "quota_window_seconds": 86400,
            "session_limit": 10,
            "session_window_seconds": 3600,
            **changes,
        }
    )


class AdapterRenderingTests(unittest.TestCase):
    def test_adapter_passes_only_public_policy_to_the_lua_module(self):
        rendered = render_identity(policy(oidc_client_id='quoted"client'))
        marker = "require('cjson.safe').decode("
        encoded = rendered.split(marker, 1)[1][:-2]
        options = json.loads(json.loads(encoded))
        self.assertEqual(options["client_id"], 'quoted"client')
        self.assertEqual(options["quota_per_guest"], 3)
        self.assertEqual(options["quota_user_pool"], 80)
        self.assertFalse(options["redirect"])
        self.assertEqual(len(options["bucket"]), 64)
        self.assertFalse(any("secret" in field for field in options))

    def test_mixed_native_rules_share_one_global_key_across_principals(self):
        config = render_counter(policy())
        individual, pool, total = config["rules"]
        self.assertEqual(individual["count"], "$http_x_depositar_turn_limit")
        self.assertEqual(pool["count"], "$http_x_depositar_quota_pool_limit")
        self.assertEqual(total["count"], 100)
        self.assertNotIn("principal", total["key"])
        self.assertNotIn("subject", total["key"])
        self.assertIn("principal_kind", individual["key"])
        self.assertIn("principal_kind", pool["key"])
        self.assertEqual(len({rule["key"] for rule in config["rules"]}), 3)
        self.assertTrue(all("quota_period" in rule["key"] for rule in config["rules"]))
        self.assertTrue(
            all(
                rule["time_window"] == "$http_x_depositar_quota_ttl"
                for rule in config["rules"]
            )
        )
        self.assertFalse(config["allow_degradation"])
        self.assertEqual(config["sync_interval"], -1)

    def test_session_admission_does_not_reuse_turn_counters(self):
        rules = render_counter(policy(), session=True)["rules"]
        self.assertEqual(len(rules), 2)
        self.assertEqual([rule["count"] for rule in rules], [10, 100])
        self.assertTrue(all("session_period" in rule["key"] for rule in rules))
        self.assertTrue(
            all(
                rule["time_window"] == "$http_x_depositar_session_ttl" for rule in rules
            )
        )

    def test_login_required_keeps_account_and_global_limits_without_a_user_pool(self):
        rules = render_counter(policy(access_mode="login_required", quota_global=4))[
            "rules"
        ]
        self.assertEqual(
            [rule["header_prefix"] for rule in rules], ["Individual", "Global"]
        )
        self.assertEqual(rules[-1]["count"], 4)
