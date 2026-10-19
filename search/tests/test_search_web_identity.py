"""Authenticated entrance cannot allocate or use another caller's session."""

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.tests.asgi_client import ASGITestClient

from search.tests.web_support import FakeUpstream, ORIGIN, SETTINGS
from search.web.app import create_app
from search.web.sessions import SessionError, Sessions
from search.web.upstream import WebSettings


KEY = "d" * 64
ALICE, BOB = "a" * 64, "b" * 64


class OwnerSessionTests(unittest.TestCase):
    def test_other_owner_cannot_read_begin_or_delete(self):
        store = Sessions()
        token = store.create(ALICE)
        for owner in (BOB, None):
            for operation in (
                lambda: store.get(token, owner),
                lambda: store.begin(token, 0, owner),
                lambda: store.get("missing", owner),
            ):
                with self.assertRaises(SessionError) as caught:
                    operation()
                self.assertEqual(
                    (caught.exception.status, caught.exception.code),
                    (410, "session_expired"),
                )
            store.delete(token, owner)
            self.assertIn(token, store.items)
        self.assertFalse(store.get(token, ALICE).busy)
        store.delete(token, ALICE)
        self.assertNotIn(token, store.items)

    def test_busy_sessions_count_against_owner_capacity(self):
        store = Sessions()
        first = store.create(ALICE)
        store.begin(first, 0, ALICE)
        second = store.create(ALICE)
        with self.assertRaises(SessionError) as caught:
            store.create(ALICE)
        self.assertEqual(caught.exception.code, "session_capacity")
        self.assertEqual(store.get(first, ALICE).busy, True)
        self.assertIsNotNone(store.create(BOB))
        store.delete(second, ALICE)
        self.assertIsNotNone(store.create(ALICE))

    def test_expiry_releases_owner_capacity_and_keeps_absolute_ttl(self):
        now = [0]
        store = Sessions(owner_capacity=1, ttl=10, clock=lambda: now[0])
        first = store.create(ALICE)
        now[0] = 9
        store.get(first, ALICE)
        with self.assertRaises(SessionError):
            store.create(ALICE)
        now[0] = 10
        self.assertNotEqual(store.create(ALICE), first)

    def test_private_capacity_remains_unchanged(self):
        store = Sessions(capacity=3)
        for _ in range(3):
            store.create()
        with self.assertRaises(SessionError):
            store.create()

    def test_inflight_owner_capacity_survives_expiry_and_delete_attempt(self):
        now = [0]
        store = Sessions(owner_capacity=1, ttl=10, clock=lambda: now[0])
        token = store.create(ALICE)
        session = store.begin(token, 0, ALICE)
        with self.assertRaises(SessionError) as caught:
            store.delete(token, ALICE)
        self.assertEqual(caught.exception.code, "session_busy")
        store.delete(token, BOB)
        now[0] = 10
        with self.assertRaises(SessionError) as caught:
            store.get(token, ALICE)
        self.assertEqual(caught.exception.code, "session_expired")
        with self.assertRaises(SessionError) as caught:
            store.create(ALICE)
        self.assertEqual(caught.exception.code, "session_capacity")
        session.busy = False
        self.assertNotEqual(store.create(ALICE), token)


class PublicIdentityHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.secret = Path(directory.name) / "edge-key"
        self.secret.write_text(KEY + "\n")
        self.secret.chmod(0o600)
        self.store, self.upstream = Sessions(), FakeUpstream()
        self.client = ASGITestClient(
            create_app(
                replace(SETTINGS, edge_secret_file=self.secret),
                self.upstream,
                self.store,
            ),
            raise_app_exceptions=False,
        )
        self.addCleanup(self.client.close)
        self.headers = {
            "Origin": ORIGIN,
            "X-Depositar-Subject": ALICE,
            "X-Depositar-Edge-Key": KEY,
        }

    def test_missing_bad_and_duplicate_identity_never_allocate_or_infer(self):
        invalid = (
            {},
            {"X-Depositar-Subject": ALICE},
            {"X-Depositar-Edge-Key": KEY},
            {"X-Depositar-Subject": "A" * 64, "X-Depositar-Edge-Key": KEY},
            {"X-Depositar-Subject": "a" * 63, "X-Depositar-Edge-Key": KEY},
            {"X-Depositar-Subject": ALICE, "X-Depositar-Edge-Key": "e" * 64},
            [
                ("X-Depositar-Subject", ALICE),
                ("X-Depositar-Subject", BOB),
                ("X-Depositar-Edge-Key", KEY),
            ],
            [
                ("X-Depositar-Subject", ALICE),
                ("X-Depositar-Edge-Key", KEY),
                ("X-Depositar-Edge-Key", KEY),
            ],
        )
        for identity in invalid:
            headers = [("Origin", ORIGIN), *dict_or_pairs(identity)]
            for path in ("/api/session", "/api/turn"):
                with self.subTest(identity=identity, path=path):
                    response = self.client.post(path, json={}, headers=headers)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"], "authentication_required"
                    )
        self.assertEqual(self.store.items, {})
        self.assertEqual(self.upstream.calls, [])

    def test_access_has_no_session_or_model_side_effect(self):
        for headers in (
            self.headers,
            {key: value for key, value in self.headers.items() if key != "Origin"},
        ):
            response = self.client.get("/api/access", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(), {"auth_required": True, "authenticated": True}
            )
        self.assertEqual(self.client.get("/api/access").status_code, 401)
        for origin in ("https://evil.example", "null"):
            response = self.client.get(
                "/api/access", headers={**self.headers, "Origin": origin}
            )
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.items, {})
        self.assertEqual(self.upstream.calls, [])

    def test_non_post_bodies_do_not_bypass_origin_or_identity_checks(self):
        for method, path in (
            ("GET", "/api/access"),
            ("HEAD", "/api/access"),
            ("DELETE", "/api/session"),
        ):
            for headers, status in (
                ({"Origin": ORIGIN}, 401),
                ({**self.headers, "Origin": "https://evil.example"}, 403),
            ):
                with self.subTest(method=method, status=status):
                    response = self.client.request(
                        method, path, content=b"x", headers=headers
                    )
                    self.assertEqual(response.status_code, status)
        self.assertEqual(self.store.items, {})
        self.assertEqual(self.upstream.calls, [])

    def test_public_page_and_health_do_not_require_identity(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertNotEqual(self.client.get("/").status_code, 401)

    def test_same_session_token_is_not_usable_by_another_owner(self):
        response = self.client.post("/api/session", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        token = response.json()["token"]
        bob = {
            **self.headers,
            "X-Depositar-Subject": BOB,
            "Authorization": "Bearer " + token,
        }
        response = self.client.post(
            "/api/turn", json={"query": "氣候", "version": 0}, headers=bob
        )
        self.assertEqual(response.status_code, 410, response.text)
        self.assertEqual(self.upstream.calls, [])
        self.assertEqual(
            self.client.delete("/api/session", headers=bob).status_code, 200
        )
        self.assertIn(token, self.store.items)
        response = self.client.post(
            "/api/turn",
            json={"query": "氣候", "version": 0},
            headers={**self.headers, "Authorization": "Bearer " + token},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.get(token, ALICE).version, 1)

    def test_owner_capacity_is_enforced_before_model_calls(self):
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    "/api/session", json={}, headers=self.headers
                ).status_code,
                200,
            )
        response = self.client.post("/api/session", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "session_capacity")
        self.assertEqual(self.upstream.calls, [])

    def test_exact_origin_still_applies_after_authentication(self):
        for origin in ("https://evil.example", "null", None):
            headers = dict(self.headers)
            if origin is None:
                headers.pop("Origin")
            else:
                headers["Origin"] = origin
            self.assertEqual(
                self.client.post("/api/session", json={}, headers=headers).status_code,
                403,
            )
        self.assertEqual(self.store.items, {})


def dict_or_pairs(value):
    return list(value.items()) if isinstance(value, dict) else value


class IdentityConfigurationTests(unittest.TestCase):
    def test_environment_opt_in_does_not_change_private_default(self):
        environment = {"WEB_ORIGIN": ORIGIN, "WEB_UPSTREAM": SETTINGS.upstream}
        with patch.dict(os.environ, environment, clear=True):
            self.assertIsNone(WebSettings.from_environment().edge_secret_file)
            os.environ["WEB_EDGE_SECRET_FILE"] = "/run/secrets/public_edge_key"
            self.assertEqual(
                WebSettings.from_environment().edge_secret_file,
                Path("/run/secrets/public_edge_key"),
            )

    def test_unsafe_secret_files_fail_before_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "key"
            secret.write_text(KEY)
            secret.chmod(0o600)
            link = root / "link"
            link.symlink_to(secret)
            hardlink = root / "hardlink"
            hardlink.hardlink_to(secret)
            for path in (link, hardlink, root):
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    create_app(replace(SETTINGS, edge_secret_file=path), FakeUpstream())
            hardlink.unlink()
            secret.chmod(0o644)
            with self.assertRaises(ValueError):
                create_app(replace(SETTINGS, edge_secret_file=secret), FakeUpstream())
            secret.chmod(0o600)
            for value in ("z" * 64, "A" * 64, "a" * 63, "a" * 4096):
                secret.write_text(value)
                with self.subTest(value=value[:8]), self.assertRaises(ValueError):
                    create_app(
                        replace(SETTINGS, edge_secret_file=secret), FakeUpstream()
                    )

    def test_private_mode_ignores_unverified_caller_identity(self):
        store = Sessions()
        with ASGITestClient(create_app(SETTINGS, FakeUpstream(), store)) as client:
            response = client.post(
                "/api/session",
                json={},
                headers={"Origin": ORIGIN, "X-Depositar-Subject": ALICE},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(store.get(response.json()["token"]).owner)
