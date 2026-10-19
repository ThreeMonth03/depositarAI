"""Deterministic cleanup faults; no network, real sleeps, or credentials."""

from search.catalog.admin import (
    AdminAccess,
    AdminSession,
    VECTOR_TOKEN_LIFETIME_MINUTES,
)
from search.catalog.errors import (
    FessBusy,
    ImportFailed,
    ProtocolError,
    TokenCleanupError,
    TransportError,
)
from search.catalog.importer import CatalogImporter
from search.catalog.transport import Deadline
from search.catalog.vectors import MAX_VECTOR_TOKEN_API_SECONDS

import datetime
import urllib.parse
import unittest
from unittest.mock import patch


from search.tests.catalog_fixtures import (
    BuiltBundle,
    FakeTransport,
    ManualClock,
    NONCE,
    PASSWORD,
    REMOTE_SECRET,
    TOKEN,
    _response,
)

DELETE = ("DELETE", "/api/admin/accesstoken/setting/token-id")
BULK = ("PUT", "/api/admin/documents/bulk")
LOGOUT = ("POST", "/api/v2/auth/logout")
PROBE = ("GET", "/api/admin/systeminfo")


def busy(seconds="60"):
    headers = () if seconds is None else (("Retry-After", seconds),)
    return {
        "status": 429,
        "headers": headers,
        "value": {"status": 9, "message": REMOTE_SECRET},
    }


class FaultTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.faults = {}

    def send(self, request, **kwargs):
        key = (request.method, urllib.parse.urlsplit(request.url).path)
        actions = self.faults.get(key, [])
        action = actions.pop(0) if actions else None
        if action == "lost_after_commit":
            super().send(request, **kwargs)
            raise TransportError(REMOTE_SECRET)
        if action == "busy_after_commit":
            super().send(request, **kwargs)
            return _response(request, **busy())
        if action == "lost_before_commit":
            self.requests.append(request)
            raise TransportError(REMOTE_SECRET)
        if action is not None:
            self.requests.append(request)
            return _response(request, **action)
        return super().send(request, **kwargs)

    def count(self, key):
        return sum(
            (r.method, urllib.parse.urlsplit(r.url).path) == key for r in self.requests
        )


class TokenCleanupTests(unittest.TestCase):
    def setUp(self):
        self.built = BuiltBundle()
        self.addCleanup(self.built.close)
        self.transport = FaultTransport()
        self.clock = ManualClock()
        self.client = CatalogImporter(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=self.clock,
                sleep=self.clock.sleep,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, tzinfo=datetime.timezone.utc
                ),
            ),
        )

    def run_import(self, **kwargs):
        return self.client.import_bundle("admin", PASSWORD, nonce=NONCE, **kwargs)

    def assert_safe_error(self, error):
        for secret in (PASSWORD, TOKEN, REMOTE_SECRET):
            self.assertNotIn(secret, str(error))

    def test_revoke_429_waits_full_retry_after_and_preserves_bulk_single_dispatch(self):
        self.transport.faults[DELETE] = [busy()]
        self.assertEqual(self.run_import().status, "imported")
        self.assertEqual(self.clock.value, 60)
        self.assertEqual(self.transport.count(DELETE), 2)
        self.assertEqual(self.transport.count(BULK), 1)
        self.assertFalse(self.transport.token_active)

    def test_partial_import_remains_failed_after_successful_cleanup(self):
        self.transport.faults[BULK] = [None, busy()]
        self.transport.faults[DELETE] = [busy()]
        with self.assertRaises(ImportFailed) as caught:
            self.run_import(batch_size=1)
        self.assertTrue(caught.exception.possibly_polluted)
        self.assertEqual(len(self.transport.documents), 1)
        self.assertEqual(self.transport.count(BULK), 2)
        self.assertEqual(self.transport.count(DELETE), 2)
        self.assertFalse(self.transport.token_active)
        self.assert_safe_error(caught.exception)

    def test_lost_delete_response_is_reconciled_without_second_delete(self):
        self.transport.faults[DELETE] = ["lost_after_commit"]
        self.assertEqual(self.run_import().status, "imported")
        self.assertEqual(self.transport.count(DELETE), 1)
        self.assertFalse(self.transport.token_active)
        self.assertGreater(self.transport.count(PROBE), 0)

    def test_retry_rechecks_absence_before_any_second_delete(self):
        self.transport.faults[DELETE] = ["busy_after_commit"]
        self.assertEqual(self.run_import().status, "imported")
        self.assertEqual(self.transport.count(DELETE), 1)
        self.assertEqual(self.clock.value, 60)
        self.assertFalse(self.transport.token_active)

    def test_cleanup_phases_share_one_budget_instead_of_renewing_it(self):
        self.transport.faults[DELETE] = [busy()]
        self.transport.faults[PROBE] = [None, busy()]
        self.transport.faults[LOGOUT] = [busy()]
        with self.assertRaises(TokenCleanupError):
            self.run_import()
        self.assertEqual(self.clock.value, 120)
        self.assertEqual(self.transport.count(LOGOUT), 1)
        self.assertFalse(self.transport.token_active)

    def test_ambiguous_uncommitted_delete_is_not_replayed(self):
        self.transport.faults[DELETE] = ["lost_before_commit"]
        with self.assertRaises(TokenCleanupError) as caught:
            self.run_import()
        self.assertTrue(caught.exception.possibly_polluted)
        self.assertTrue(self.transport.token_active)
        self.assertEqual(self.transport.count(DELETE), 1)
        self.assert_safe_error(caught.exception)

    def test_json_status_nine_is_not_http_429_and_is_not_retried(self):
        self.transport.faults[DELETE] = [
            {"value": {"status": 9, "message": REMOTE_SECRET}}
        ]
        with self.assertRaises(TokenCleanupError):
            self.run_import()
        self.assertEqual(self.transport.count(DELETE), 1)
        self.assertEqual(self.clock.value, 0)

    def test_persistent_429_has_three_attempt_cap(self):
        self.transport.faults[DELETE] = [busy()] * 4
        with self.assertRaises(TokenCleanupError):
            self.run_import()
        self.assertEqual(self.clock.value, 120)
        self.assertEqual(self.transport.count(DELETE), 3)
        self.assertTrue(self.transport.token_active)

    def test_missing_retry_after_has_bounded_backoff(self):
        self.transport.faults[DELETE] = [busy(None), busy(None)]
        self.assertEqual(self.run_import().status, "imported")
        self.assertEqual(self.clock.value, 6)

    def test_retry_after_exceeding_budget_is_not_shortened(self):
        self.transport.faults[DELETE] = [busy("600")]
        with self.assertRaises(TokenCleanupError):
            self.run_import()
        self.assertEqual(self.transport.count(DELETE), 1)
        self.assertEqual(self.clock.value, 0)

    def test_unsupported_retry_hints_fail_without_retry(self):
        for headers in (
            (("Retry-After", "60"), ("Retry-After", "1")),
            (("Retry-After", "-1"),),
            (("Retry-After", "9999999"),),
            (("Retry-After", "Wed, 21 Oct 2030 07:28:00 GMT"),),
        ):
            with self.subTest(headers=headers):
                transport = FaultTransport()
                transport.faults[PROBE] = [{**busy(), "headers": headers}]
                session = AdminSession("http://127.0.0.1:8080", transport=transport)
                with self.assertRaises(ProtocolError) as caught:
                    session.system_info(TOKEN, Deadline.after(10))
                self.assertNotIsInstance(caught.exception, FessBusy)
                self.assert_safe_error(caught.exception)

    def test_logout_429_retains_cookie_only_for_retry_then_clears_it(self):
        self.transport.faults[LOGOUT] = [busy()]
        session = self.client.access._session()
        with patch.object(self.client.access, "_session", return_value=session):
            self.assertEqual(self.run_import().status, "imported")
        self.assertEqual(self.transport.count(LOGOUT), 2)
        self.assertEqual(self.clock.value, 60)
        self.assertIsNone(session._session_id)
        self.assertFalse(self.transport.token_active)
        self.assertTrue(
            all(
                r.header("Cookie") is None
                for r in self.transport.requests
                if r.header("Authorization")
            )
        )

    def test_lost_logout_response_is_not_replayed(self):
        self.transport.faults[LOGOUT] = ["lost_after_commit"]
        with self.assertRaises(TokenCleanupError):
            self.run_import()
        self.assertEqual(self.transport.count(LOGOUT), 1)
        self.assertFalse(self.transport.token_active)

    def test_revocation_probe_429_retries_read_only(self):
        self.transport.faults[PROBE] = [None, busy()]
        self.assertEqual(self.run_import().status, "imported")
        self.assertEqual(self.transport.count(DELETE), 1)
        self.assertEqual(self.clock.value, 60)

    def test_ip_block_403_does_not_prove_revocation(self):
        self.transport.faults[PROBE] = [
            None,
            {"status": 403, "value": b'{"error":"ip_blocked"}'},
        ]
        with self.assertRaises(TokenCleanupError):
            self.run_import()
        self.assertFalse(self.transport.token_active)

    def test_explicit_authentication_failure_envelope_is_accepted(self):
        self.transport.faults[PROBE] = [None, {"status": 403, "value": {"status": 3}}]
        self.assertEqual(self.run_import().status, "imported")

    def test_retry_refuses_a_changed_token_identity(self):
        self.transport.faults[DELETE] = [busy("0")]
        with patch.object(
            AdminSession,
            "recover_ephemeral_token",
            return_value=("other-id", TOKEN),
        ):
            with self.assertRaises(TokenCleanupError):
                self.run_import()
        self.assertEqual(self.transport.count(DELETE), 1)
        self.assertTrue(self.transport.token_active)

    def test_late_vector_cleanup_cannot_borrow_past_existing_expiry_margin(self):
        self.transport.faults[DELETE] = [busy()]
        with self.assertRaises(TokenCleanupError):
            with self.client.access.token(
                "admin",
                PASSWORD,
                NONCE,
                Deadline.after(1800, self.clock),
                lifetime_minutes=VECTOR_TOKEN_LIFETIME_MINUTES,
            ):
                self.clock.value = MAX_VECTOR_TOKEN_API_SECONDS
        self.assertEqual(self.clock.value, 2040)
        self.assertEqual(self.transport.count(DELETE), 1)
        self.assertEqual(self.transport.created_token_expiries, ["2026-09-09T12:35:00"])

    def test_import_can_use_spare_cleanup_time_without_extending_token_lifetime(self):
        self.transport.faults[DELETE] = [busy(), busy()]
        with self.client.access.token(
            "admin",
            PASSWORD,
            NONCE,
            Deadline.after(600, self.clock),
        ):
            self.clock.value = 600
        self.assertEqual(self.clock.value, 720)
        self.assertEqual(self.transport.created_token_expiries, ["2026-09-09T12:15:00"])
        self.assertFalse(self.transport.token_active)

    def test_busy_token_creation_is_never_replayed(self):
        create = ("POST", "/admin/accesstoken/")
        self.transport.faults[create] = [busy()]
        with self.assertRaises(FessBusy):
            self.run_import()
        self.assertEqual(self.transport.count(create), 1)
        self.assertEqual(self.transport.count(BULK), 0)
        self.assertFalse(self.transport.token_active)


if __name__ == "__main__":
    unittest.main()
