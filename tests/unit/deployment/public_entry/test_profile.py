"""Public edge admission, private inputs and deployment boundaries."""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.operations.deployment.public_entry import profile as edge


class PublicEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-public-entry-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.raw = {
            "schema_version": 1,
            "state_root": str(self.state),
            "public_origin": "https://public.example.org",
            "oidc_issuer": "https://identity.example.org",
            "oidc_discovery": "https://identity.example.org/.well-known/openid-configuration",
            "oidc_client_id": "depositar-public",
        }
        for index, name in enumerate(
            (
                "oidc_client_secret_file",
                "oidc_session_secret_file",
                "edge_secret_file",
                "redis_password_file",
            )
        ):
            path = self.root / name
            path.write_text(
                "fixture-client-secret\n" if index == 0 else str(index) * 64 + "\n"
            )
            path.chmod(0o600)
            self.raw[name] = str(path)
        self.config = edge.validate_public_entry_config(self.raw)

    def plan(self):
        return edge.render_public_entry(self.config)

    def routes(self):
        return {route["id"]: route for route in self.plan()["apisix_routes"]["routes"]}

    def test_private_path_config_loads_and_contains_no_credentials(self):
        path = self.root / "entry.json"
        path.write_text(json.dumps(self.raw))
        path.chmod(0o600)
        self.assertEqual(edge.load_public_entry_config(path), self.config)
        serialized = json.dumps(self.plan())
        self.assertNotIn("fixture-client-secret", serialized)
        self.assertNotIn('"' + "2" * 64 + '"', serialized)
        self.assertIn("$ENV://DEPOSITAR_OIDC_CLIENT_SECRET", serialized)

    def test_unknown_inline_credentials_and_duplicate_keys_are_rejected(self):
        with self.assertRaisesRegex(edge.PublicEntryError, "fields"):
            edge.validate_public_entry_config(
                {**self.raw, "oidc_client_secret": "ignored"}
            )
        path = self.root / "duplicate.json"
        path.write_text('{"schema_version":1,"schema_version":1}')
        path.chmod(0o600)
        with self.assertRaisesRegex(edge.PublicEntryError, "duplicate_key"):
            edge.load_public_entry_config(path)

    def test_config_json_rejects_nonfinite_invalid_utf8_and_implicit_encodings(self):
        path = self.root / "entry.json"
        path.write_text(json.dumps(self.raw))
        path.chmod(0o600)
        for raw in (
            b'{"value":NaN}',
            b'{"value":Infinity}',
            b'{"value":-Infinity}',
            b'{"value":"\xff"}',
            json.dumps(self.raw).encode("utf-16"),
            json.dumps(self.raw).encode("utf-32"),
            json.dumps(self.raw).encode("utf-8-sig"),
        ):
            path.write_bytes(raw)
            with (
                self.subTest(raw=raw[:8]),
                self.assertRaisesRegex(
                    edge.PublicEntryError, "^public_entry_configuration_invalid$"
                ),
            ):
                edge.load_public_entry_config(path)
        path.write_text(json.dumps(self.raw))
        with (
            patch.object(edge, "strict_json", side_effect=RecursionError),
            self.assertRaises(RecursionError),
        ):
            edge.load_public_entry_config(path)
        path.write_bytes(b" " * (edge.MAX_CONFIG_BYTES + 1))
        with self.assertRaisesRegex(edge.PublicEntryError, "^public_entry_file_size$"):
            edge.load_public_entry_config(path)

    def test_secrets_require_private_regular_nonlinked_files(self):
        path = Path(self.raw["edge_secret_file"])
        path.chmod(0o644)
        with self.assertRaisesRegex(edge.PublicEntryError, "file_unsafe"):
            edge.validate_public_entry_config(self.raw)
        path.chmod(0o600)
        linked = self.root / "hardlink"
        os.link(path, linked)
        with self.assertRaisesRegex(edge.PublicEntryError, "file_unsafe"):
            edge.validate_public_entry_config(self.raw)
        linked.unlink()
        symlink = self.root / "symlink"
        symlink.symlink_to(path)
        with self.assertRaisesRegex(edge.PublicEntryError, "path_symlink"):
            edge.validate_public_entry_config(
                {**self.raw, "edge_secret_file": str(symlink)}
            )

    def test_private_file_parent_cannot_be_shared_or_writable(self):
        self.root.chmod(0o755)
        try:
            with self.assertRaisesRegex(edge.PublicEntryError, "file_parent_unsafe"):
                edge.validate_public_entry_config(self.raw)
        finally:
            self.root.chmod(0o700)

    def test_invalid_secret_bytes_and_reused_paths_are_rejected(self):
        path = Path(self.raw["edge_secret_file"])
        for value in (
            "A" * 64,
            "a" * 63,
            "a" * 64 + "\n\n",
            "a" * 32 + "\x00" + "a" * 31,
        ):
            with self.subTest(value_length=len(value)):
                path.write_text(value)
                with self.assertRaises(edge.PublicEntryError):
                    edge.validate_public_entry_config(self.raw)
        path.write_text("a" * 64)
        with self.assertRaisesRegex(edge.PublicEntryError, "secret_paths_overlap"):
            edge.validate_public_entry_config(
                {**self.raw, "redis_password_file": str(path)}
            )

    def test_public_origin_and_identity_endpoints_require_fixed_https(self):
        for name in ("public_origin", "oidc_issuer", "oidc_discovery"):
            for value in (
                "http://example.org",
                "https://user:pass@example.org",
                "https://example.org?x=1",
                "https://example.org/#x",
                "https://example.org\n",
                "https://bad'host.example.org",
                "https://bad;host.example.org",
            ):
                with self.subTest(field=name, value=value):
                    with self.assertRaises(edge.PublicEntryError):
                        edge.validate_public_entry_config({**self.raw, name: value})
        for value in (
            "https://example.org/",
            "https://example.org/path",
            "https://example.org:443",
        ):
            with self.assertRaises(edge.PublicEntryError):
                edge.validate_public_entry_config({**self.raw, "public_origin": value})
        value = edge.validate_public_entry_config(
            {**self.raw, "public_origin": "https://public.example.org:9443"}
        )
        self.assertEqual(value.public_origin, "https://public.example.org:9443")

    def test_limits_and_proxy_trust_reject_unsafe_values(self):
        for fields in (
            {"loopback_port": 18083},
            {"loopback_port": True},
            {"quota_global": 0},
            {"quota_per_account": 101},
            {"quota_window_seconds": 59},
            {"trusted_proxy_cidrs": ["0.0.0.0/0"]},
            {"trusted_proxy_cidrs": ["::/0"]},
            {"trusted_proxy_cidrs": ["10.0.0.1/24"]},
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(edge.PublicEntryError):
                    edge.validate_public_entry_config({**self.raw, **fields})

    def test_explicit_network_subnets_are_private_canonical_and_separate(self):
        subnets = {
            "website": "10.253.250.0/28",
            "quota": "10.253.250.16/28",
            "outbound": "10.253.250.32/28",
        }
        config = edge.validate_public_entry_config(
            {**self.raw, "network_subnets": subnets}
        )
        plan = edge.render_public_entry(config)
        for name, subnet in subnets.items():
            self.assertEqual(
                plan["compose"]["networks"][name]["ipam"]["config"],
                [{"subnet": subnet}],
            )
        for changed in (
            None,
            [],
            {"website": subnets["website"]},
            {**subnets, "website": "10.253.250.1/28"},
            {**subnets, "website": "198.18.0.0/28"},
            {**subnets, "website": "10.253.250.0/23"},
            {**subnets, "website": "10.253.250.0/29"},
            {**subnets, "website": "::1/128"},
            {**subnets, "website": subnets["quota"]},
        ):
            with self.subTest(subnets=changed):
                with self.assertRaises(edge.PublicEntryError):
                    edge.validate_public_entry_config(
                        {**self.raw, "network_subnets": changed}
                    )

    def test_every_api_route_requires_cookie_identity(self):
        routes = self.routes()
        api = [route for route in routes.values() if route["uri"].startswith("/api/")]
        self.assertEqual(
            {route["uri"] for route in api},
            {"/api/access", "/api/session", "/api/turn"},
        )
        for route in api:
            oidc = route["plugins"]["openid-connect"]
            self.assertEqual(oidc["unauth_action"], "deny")
            self.assertFalse(oidc["bearer_only"])
            self.assertFalse(oidc["use_jwks"])
            self.assertFalse(oidc["set_access_token_header"])
            self.assertFalse(oidc["accept_none_alg"])
            self.assertFalse(oidc["accept_unsupported_alg"])
            self.assertTrue(oidc["session"]["cookie_secure"])
            self.assertEqual(
                oidc["_meta"]["error_response"],
                {"error": {"code": "authentication_required"}},
            )
        self.assertEqual(
            routes["login"]["plugins"]["openid-connect"]["unauth_action"], "auth"
        )
        self.assertFalse(
            any(route["uri"] in ("/*", "/api/*") for route in routes.values())
        )

    def test_origin_and_token_adapter_runs_around_oidc_rewrite(self):
        turn = self.routes()["turn"]["plugins"]
        before = turn["serverless-pre-function"]
        after = turn["serverless-post-function"]
        self.assertEqual(before["phase"], "rewrite")
        self.assertEqual(after["phase"], "rewrite")
        self.assertIn(
            "ctx.depositar_authorization = h.authorization", before["functions"][0]
        )
        self.assertIn(
            "'Authorization','X-Depositar-Subject','X-Depositar-Edge-Key'",
            before["functions"][0],
        )
        self.assertIn("if h.origin ~=", before["functions"][0])
        self.assertIn("require('depositar_identity').handler", after["functions"][0])
        adapter = self.plan()["assets"]["depositar_identity.lua"]
        self.assertIn("claims.iss ~=", adapter)
        self.assertIn("'__Host-depositar-login'", before["functions"][0])
        self.assertIn("X-Depositar-Principal-Kind", before["functions"][0])
        self.assertIn("X-Depositar-Quota-Pool-Limit", before["functions"][0])
        public_before = self.routes()["public-home"]["plugins"][
            "serverless-pre-function"
        ]["functions"][0]
        self.assertIn("core.request.set_header(ctx, 'Cookie', nil)", public_before)
        self.assertNotIn(
            "core.request.set_header(ctx, 'Cookie', nil)", before["functions"][0]
        )

    def test_account_and_global_limits_are_synchronous_fail_closed_stable(self):
        counter = self.routes()["turn"]["plugins"]["limit-count"]
        self.assertEqual(counter["policy"], "redis")
        self.assertFalse(counter["allow_degradation"])
        self.assertEqual(counter["sync_interval"], -1)
        self.assertNotIn("group", counter)
        self.assertEqual(len(counter["rules"]), 2)
        self.assertEqual(counter["rules"][0]["count"], "$http_x_depositar_turn_limit")
        self.assertEqual(counter["rules"][1]["count"], 100)
        for rule in counter["rules"]:
            self.assertIn("$http_x_depositar_quota_period", rule["key"])
            self.assertEqual(rule["time_window"], "$http_x_depositar_quota_ttl")
        changed = edge.render_public_entry(replace(self.config, quota_global=200))
        other = next(
            route
            for route in changed["apisix_routes"]["routes"]
            if route["id"] == "turn"
        )
        self.assertEqual(other["id"], "turn")
        self.assertEqual(other["plugins"]["limit-count"]["rules"][1]["count"], 200)
        self.assertIn("limit-count", self.routes()["session-create"]["plugins"])
        self.assertNotIn("limit-count", self.routes()["session-delete"]["plugins"])

    def test_only_loopback_is_published_and_private_services_are_bounded(self):
        plan = self.plan()
        compose = plan["compose"]
        apisix = compose["services"]["public-entry"]
        redis = compose["services"]["quota-redis"]
        self.assertEqual(apisix["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(apisix["ports"][0]["published"], "18084")
        self.assertNotIn("ports", redis)
        self.assertEqual(redis["networks"], ["quota"])
        self.assertTrue(compose["networks"]["quota"]["internal"])
        self.assertTrue(compose["networks"]["website"]["internal"])
        self.assertNotIn("public-web", compose["services"])
        for service in (apisix, redis):
            self.assertIn("@sha256:", service["image"])
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertIn("mem_limit", service)
            self.assertIn("pids_limit", service)
        self.assertIn("appendfsync always", plan["redis_config"])
        self.assertIn("maxmemory-policy noeviction", plan["redis_config"])
        self.assertFalse(plan["apisix_config"]["apisix"]["enable_admin"])
        self.assertFalse(plan["apisix_config"]["apisix"]["enable_control"])
        self.assertFalse(
            plan["apisix_config"]["nginx_config"]["http"]["enable_access_log"]
        )

    def test_ca_is_optional_and_never_disables_tls_verification(self):
        ca = self.root / "ca.pem"
        ca.write_text(
            "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
        )
        ca.chmod(0o600)
        config = edge.validate_public_entry_config(
            {**self.raw, "oidc_ca_file": str(ca)}
        )
        plan = edge.render_public_entry(config)
        self.assertEqual(
            plan["apisix_config"]["apisix"]["ssl"]["ssl_trusted_certificate"],
            "/run/secrets/oidc-ca.pem",
        )
        self.assertTrue(
            plan["apisix_routes"]["routes"][4]["plugins"]["openid-connect"][
                "ssl_verify"
            ]
        )

    def mixed_raw(self, **extra):
        for name in ("guest_session_secret_file", "antibot_signing_key_file"):
            path = self.root / name
            path.write_text("a" * 64)
            path.chmod(0o600)
        return {
            **self.raw,
            "access_mode": "mixed",
            "guest_session_secret_file": str(self.root / "guest_session_secret_file"),
            **extra,
        }

    def test_mixed_policy_rejects_missing_secret_and_unreserved_budgets(self):
        with self.assertRaisesRegex(edge.PublicEntryError, "guest_secret_required"):
            edge.validate_public_entry_config({**self.raw, "access_mode": "mixed"})
        for extra in (
            {"access_mode": "invalid"},
            {"quota_per_guest": True},
            {"quota_per_guest": 10},
            {"quota_guest_pool": 2},
            {"quota_user_pool": 9},
            {"quota_guest_pool": 50},
            {"antibot_enabled": "true"},
            {"antibot_enabled": True},
        ):
            with self.subTest(extra=extra), self.assertRaises(edge.PublicEntryError):
                edge.validate_public_entry_config(self.mixed_raw(**extra))
        edge.validate_public_entry_config(self.mixed_raw())

    def test_mixed_turn_uses_one_route_with_individual_pool_and_global_limits(self):
        config = edge.validate_public_entry_config(self.mixed_raw())
        plan = edge.render_public_entry(config)
        turns = [r for r in plan["apisix_routes"]["routes"] if r["uri"] == "/api/turn"]
        self.assertEqual(len(turns), 1)
        plugins = turns[0]["plugins"]
        self.assertEqual(plugins["openid-connect"]["unauth_action"], "pass")
        self.assertEqual(
            [r["header_prefix"] for r in plugins["limit-count"]["rules"]],
            ["Individual", "Pool", "Global"],
        )
        self.assertIn("depositar-quota-response", plugins)
        self.assertTrue(plan["compose"]["x-depositar-plan"]["anonymous_ai"])

    def test_antibot_has_no_gateway_bypass_and_state_is_staged_privately(self):
        raw = self.mixed_raw(
            antibot_enabled=True,
            antibot_signing_key_file=str(self.root / "antibot_signing_key_file"),
        )
        config = edge.validate_public_entry_config(raw)
        plan = edge.write_public_entry(config)
        services = plan["compose"]["services"]
        self.assertEqual(services["public-entry"]["ports"], [])
        self.assertEqual(services["public-edge"]["ports"][0]["host_ip"], "127.0.0.1")
        self.assertNotIn("ports", services["antibot"])
        self.assertEqual(
            plan["apisix_config"]["nginx_config"]["http"]["real_ip_from"],
            ["public-edge"],
        )
        self.assertEqual(
            (self.state / "public-entry/antibot-data").stat().st_mode & 0o777, 0o700
        )

    def test_staging_is_private_and_refuses_overwrite(self):
        plan = edge.write_public_entry(self.config)
        output = self.state / "public-entry"
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((output / "compose.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((output / "redis-data").stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            json.loads((output / "compose.json").read_text()), plan["compose"]
        )
        with self.assertRaises(FileExistsError):
            edge.write_public_entry(self.config)


if __name__ == "__main__":
    unittest.main()
