from search.tests.catalog_fixtures import (
    vector_pass_arguments,
    PASSWORD,
    TOKEN,
    REMOTE_SECRET,
    NONCE,
    _document_sha256,
    _different_sha256,
    BuiltBundle,
    _response,
    FakeTransport,
    ScriptedInventory,
    ManualClock,
)


from search.catalog.admin import (
    AdminAccess,
    AdminSession,
    TOKEN_CLEANUP_SECONDS,
    TOKEN_LIFETIME_MINUTES,
    VECTOR_TOKEN_LIFETIME_MINUTES,
)
from search.catalog.bundle import (
    CATALOG_MANIFEST_POLICY,
    CatalogBundle,
    DOCUMENTS_NAME,
    RESOURCE_CONTEXT_NAME,
)
from search.catalog.errors import (
    AuthenticationError,
    BundleError,
    CatalogError,
    CatalogTimeout,
    ConfigurationError,
    ImportFailed,
    InventoryConflict,
    InventoryUnstable,
    ProtocolError,
    TokenCleanupError,
    TransportError,
    VectorBuildError,
    _diagnosed,
    failure_record,
    valid_failure_record,
)
from search.catalog.importer import (
    CatalogImporter,
    MAX_TOKEN_OPERATION_DEADLINE_SECONDS,
)
from search.catalog.inventory import InventorySnapshot
from search.catalog.policy import (
    CATALOG_LABEL,
    CATALOG_SEARCH_ROLE,
    CATALOG_VIRTUAL_HOST,
)
from search.catalog.session import FessSession
from search.catalog.transport import (
    Deadline,
    HTTPRequest,
    HTTPResponse,
    UrllibTransport,
)
from search.catalog.vectors import (
    MAX_VECTOR_DEADLINE_SECONDS,
    MAX_VECTOR_TOKEN_API_SECONDS,
    VECTOR_RECOVERY_SECONDS,
    VECTOR_START_QUIESCENCE_SECONDS,
    VECTOR_VERIFICATION_SECONDS,
    VectorBuilder,
)
#!/usr/bin/env python3

import contextlib
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.parse

from search.catalog.build import policy


class FessCatalogTests(unittest.TestCase):
    def setUp(self):
        self.built = BuiltBundle()
        self.transport = FakeTransport()
        self.client = CatalogImporter(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
                sleep=lambda _seconds: None,
            ),
        )

        self.vectors = VectorBuilder(self.built.bundle, self.client.access)

    def tearDown(self):
        self.built.close()

    def _import(self):
        return self.client.import_bundle(
            "admin", PASSWORD, nonce=NONCE, batch_size=2, deadline_seconds=30
        )

    def assert_no_secret(self, value):
        rendered = str(value)
        for secret in (PASSWORD, TOKEN, REMOTE_SECRET):
            self.assertNotIn(secret, rendered)

    def test_admin_cookie_lifetime_and_bearer_isolation(
        self,
    ):
        now = [0.0]

        class ExpiringCookieTransport(FakeTransport):
            last_cookie_use = 0.0

            def send(inner, request, **kwargs):
                path = urllib.parse.urlsplit(request.url).path
                if path == "/api/v2/auth/login":
                    inner.last_cookie_use = now[0]
                elif request.header("Cookie"):
                    if now[0] - inner.last_cookie_use >= 1800:
                        inner.requests.append(request)
                        return _response(
                            request,
                            b"",
                            status=302,
                            headers=(("Location", "/login"),),
                        )
                    inner.last_cookie_use = now[0]
                return super().send(request, **kwargs)

        transport = ExpiringCookieTransport()
        session = AdminSession(
            "http://127.0.0.1:8080", transport=transport, clock=lambda: now[0]
        )
        session.login_admin(
            "admin", PASSWORD, Deadline.after(10, lambda: now[0])
        )
        transport.token_active = True
        transport.token_name = "keepalive-probe"
        for point in (300, 600, 900, 1200, 1500, 1800, 1980):
            now[0] = point
            session.system_info(TOKEN, Deadline.after(10, lambda: now[0]))
        session.revoke_access_token(
            "token-id", TOKEN, Deadline.after(10, lambda: now[0])
        )
        session.assert_token_revoked(TOKEN, Deadline.after(10, lambda: now[0]))
        self.assertEqual(
            [],
            session._listed_token_paths(
                "keepalive-probe", Deadline.after(10, lambda: now[0])
            ),
        )
        session.logout(Deadline.after(10, lambda: now[0]))
        self.assertIsNone(session._admin_keepalive_at)
        self.assertFalse(transport.token_active)
        self.assertEqual(
            8,
            sum(
                "/admin/accesstoken/list/1" in r.url
                for r in transport.requests
            ),
        )
        # FakeTransport also rejects any bearer request carrying a cookie.
        self.assertTrue(
            all(
                r.header("Cookie") is None
                for r in transport.requests
                if r.header("Authorization")
            )
        )

    def test_admin_keepalive_failure_prevents_the_next_bulk_dispatch(self):
        now = [0.0]
        session = AdminSession(
            "http://127.0.0.1:8080",
            transport=self.transport,
            clock=lambda: now[0],
        )
        session.login_admin(
            "admin", PASSWORD, Deadline.after(10, lambda: now[0])
        )
        now[0] = 301
        with mock.patch.object(
            session,
            "_admin_html",
            side_effect=ProtocolError("session unavailable"),
        ):
            with self.assertRaises(CatalogError):
                session.documents_bulk(
                    TOKEN,
                    self.built.bundle.documents,
                    Deadline.after(10, lambda: now[0]),
                )
        self.assertFalse(session._document_bulk_dispatched)
        self.assertFalse(
            any(
                "/api/admin/documents" in r.url
                for r in self.transport.requests
            )
        )

    def test_ordinary_cookie_session_never_uses_admin_keepalive(self):
        now = [0.0]
        session = AdminSession(
            "http://127.0.0.1:8080",
            transport=self.transport,
            clock=lambda: now[0],
        )
        session.login("member", PASSWORD, Deadline.after(10, lambda: now[0]))
        self.transport.token_active = True
        now[0] = 301
        session.system_info(TOKEN, Deadline.after(10, lambda: now[0]))
        self.assertFalse(
            any(
                "/admin/" in urllib.parse.urlsplit(r.url).path
                and "/api/admin/" not in r.url
                for r in self.transport.requests
            )
        )
        self.assertIsNone(session._admin_keepalive_at)

    def test_admin_keepalive_uses_the_existing_deadline(self):
        now = [0.0]
        session = AdminSession(
            "http://127.0.0.1:8080",
            transport=self.transport,
            clock=lambda: now[0],
        )
        session.login_admin(
            "admin", PASSWORD, Deadline.after(10, lambda: now[0])
        )
        deadline = Deadline.after(300, lambda: now[0])
        now[0] = 301
        count = len(self.transport.requests)
        with self.assertRaises(CatalogTimeout):
            session.system_info(TOKEN, deadline)
        self.assertEqual(count, len(self.transport.requests))

    def test_builder_bundle_contract_loads_and_canonicalizes_unicode_url(self):
        bundle = self.built.bundle
        self.assertEqual(2, len(bundle.documents))
        self.assertRegex(bundle.bundle_id, r"^[0-9a-f]{64}$")
        self.assertIn(
            "%E8%B3%87%E6%96%99%20%E9%9B%86%2F1", tuple(bundle.by_url)[0]
        )
        self.assertEqual((CATALOG_LABEL,), bundle.documents[0]["label"])
        self.assertEqual((CATALOG_SEARCH_ROLE,), bundle.documents[0]["role"])
        self.assertEqual(
            (CATALOG_VIRTUAL_HOST,), bundle.documents[0]["virtual_host"]
        )
        for document in bundle.documents:
            self.assertRegex(
                document["depositar_document_sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertEqual(
                _document_sha256(document),
                document["depositar_document_sha256"],
            )
        self.assertEqual(
            tuple(
                (
                    document["url"],
                    bundle.bundle_id,
                    document["depositar_document_sha256"],
                )
                for document in sorted(
                    bundle.documents, key=lambda row: row["url"]
                )
            ),
            bundle.expected_inventory_entries,
        )
        with self.assertRaises(TypeError):
            bundle.documents[0]["title"] = "mutated"
        with self.assertRaises(TypeError):
            bundle.by_url["https://data.depositar.io/dataset/injected"] = (
                bundle.documents[0]
            )

    def test_successful_import_reconciles_inventory_and_revokes_token(self):
        report = self._import()
        self.assertEqual("imported", report.status)
        self.assertEqual(2, report.document_count)
        self.assertEqual(
            ["2026-09-09T12:15:00"], self.transport.created_token_expiries
        )
        self.assertFalse(self.transport.token_active)
        self.assertTrue(
            any(
                request.url.endswith("/api/v2/auth/logout")
                for request in self.transport.requests
            )
        )
        self.assert_no_secret(report.as_dict())

    def test_import_waits_for_refresh_but_only_accepts_exact_bundle_subsets(
        self,
    ):
        entries = self.built.bundle.expected_inventory_entries
        inventory = ScriptedInventory(
            [
                InventorySnapshot(()),
                InventoryUnstable("refresh changed between pages"),
                InventorySnapshot(entries[:1]),
                InventorySnapshot(entries),
            ]
        )
        sleeps = []
        client = CatalogImporter(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=lambda: 0.0,
                sleep=sleeps.append,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
            inventory=inventory,
        )
        report = client.import_bundle(
            "admin", PASSWORD, nonce=NONCE, batch_size=2, deadline_seconds=30
        )
        self.assertEqual("imported", report.status)
        self.assertEqual(4, inventory.calls)
        self.assertEqual([0.25, 0.5], sleeps)

        self.transport = FakeTransport()
        foreign = InventorySnapshot(
            (
                (
                    "https://data.depositar.io/dataset/foreign",
                    self.built.bundle.bundle_id,
                    "e" * 64,
                ),
            )
        )
        inventory = ScriptedInventory([InventorySnapshot(()), foreign])
        client = CatalogImporter(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=lambda: 0.0,
                sleep=lambda _seconds: self.fail(
                    "polluted inventory must fail without sleeping"
                ),
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
            inventory=inventory,
        )
        with self.assertRaisesRegex(ImportFailed, "outside the sealed bundle"):
            client.import_bundle(
                "admin",
                PASSWORD,
                nonce=NONCE,
                batch_size=2,
                deadline_seconds=30,
            )

    def test_same_bundle_is_noop_and_different_bundle_is_rejected(self):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        report = self._import()
        self.assertEqual("already-active-no-op", report.status)
        self.assertFalse(
            any(
                "documents/bulk" in request.url
                for request in self.transport.requests
            )
        )

    def test_same_url_and_bundle_with_changed_document_digest_is_not_active(
        self,
    ):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        first = next(iter(self.transport.documents.values()))
        first["depositar_document_sha256"] = _different_sha256(
            first["depositar_document_sha256"]
        )

        with self.assertRaisesRegex(InventoryConflict, "complete copy"):
            self._import()
        self.assertFalse(
            any(
                "documents/bulk" in request.url
                for request in self.transport.requests
            )
        )

        self.transport.requests.clear()
        with self.assertRaisesRegex(InventoryConflict, "exactly match"):
            self.client.reconcile_bundle(
                "admin",
                PASSWORD,
                nonce="abcdefabcdef",
                deadline_seconds=30,
            )
        self.assertFalse(
            any(
                "documents/bulk" in request.url
                for request in self.transport.requests
            )
        )

    def test_post_import_document_content_digest_mismatch_fails_closed(self):
        self.transport.tamper_document_on_bulk = True
        with self.assertRaisesRegex(ImportFailed, "outside the sealed bundle"):
            self._import()
        self.assertTrue(self.transport.documents)
        self.assertFalse(self.transport.token_active)

    def test_inventory_snapshot_rejects_missing_or_malformed_document_digest(
        self,
    ):
        url, bundle_id, document_sha256 = (
            self.built.bundle.expected_inventory_entries[0]
        )
        for entry in (
            (url, bundle_id),
            (url, bundle_id, document_sha256.upper()),
            (url, bundle_id, True),
        ):
            with self.subTest(entry=entry), self.assertRaises(ProtocolError):
                InventorySnapshot((entry,))

    def test_reconcile_is_read_only_and_requires_the_exact_complete_bundle(
        self,
    ):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        report = self.client.reconcile_bundle(
            "admin", PASSWORD, nonce=NONCE, deadline_seconds=30
        )
        self.assertEqual("reconciled-active", report.status)
        self.assertEqual(0, report.batch_count)
        self.assertEqual(2, report.document_count)
        self.assertFalse(
            any(
                "documents/bulk" in request.url
                for request in self.transport.requests
            )
        )
        self.assertFalse(self.transport.token_active)

        first_url = next(iter(self.transport.documents))
        self.transport.documents[first_url]["depositar_bundle_id"] = "f" * 64
        with self.assertRaises(InventoryConflict):
            self.client.reconcile_bundle(
                "admin",
                PASSWORD,
                nonce="abcdefabcdef",
                deadline_seconds=30,
            )
        self.assertFalse(
            any(
                "documents/bulk" in request.url
                for request in self.transport.requests
            )
        )

        self.transport.requests.clear()
        first = next(iter(self.transport.documents.values()))
        first["depositar_bundle_id"] = "f" * 64
        with self.assertRaises(InventoryConflict):
            self._import()
        self.assertFalse(
            any(
                "documents/bulk" in request.url
                for request in self.transport.requests
            )
        )

    def test_partial_and_timeout_after_commit_fail_closed_as_polluted(self):
        for mode in ("partial", "timeout"):
            with self.subTest(mode=mode):
                self.transport.documents.clear()
                self.transport.bulk_mode = mode
                with self.assertRaises(ImportFailed) as caught:
                    self._import()
                self.assertTrue(caught.exception.possibly_polluted)
                self.assertTrue(self.transport.documents)
                self.assertFalse(self.transport.token_active)
                self.assert_no_secret(caught.exception)

    def test_transport_origin_retains_only_typed_timeout_or_transport_cause(
        self,
    ):
        request = HTTPRequest(
            "GET", "http://127.0.0.1:8080/api/v2/auth/logout", ()
        )
        for failure, cause in (
            (TimeoutError(REMOTE_SECRET), "request_timeout"),
            (
                urllib.error.URLError(TimeoutError(REMOTE_SECRET)),
                "request_timeout",
            ),
            (OSError(REMOTE_SECRET), "transport"),
            (urllib.error.URLError("timed out " + REMOTE_SECRET), "transport"),
        ):
            for during_read in (False, True):
                with self.subTest(cause=cause, during_read=during_read):
                    transport = UrllibTransport()
                    response = mock.Mock(headers={})
                    if during_read:
                        transport._opener.open = mock.Mock(
                            return_value=response
                        )
                        response.read.side_effect = failure
                    else:
                        transport._opener.open = mock.Mock(side_effect=failure)
                    with self.assertRaises(TransportError) as caught:
                        transport.send(request, timeout=1, max_bytes=100)
                    self.assertIs(type(caught.exception), TransportError)
                    record = failure_record(
                        caught.exception, possibly_polluted=False
                    )
                    self.assertEqual(cause, record["diagnostic"]["cause"])
                    self.assertTrue(valid_failure_record(record))
                    self.assertNotIn(REMOTE_SECRET, json.dumps(record))
                    self.assertEqual(
                        ("Fess transport failed",), caught.exception.args
                    )
                    transport._opener.open.assert_called_once()
                    if during_read:
                        response.close.assert_called_once()

    def test_bulk_failures_are_closed_without_retries(
        self,
    ):
        cases = [
            (
                status,
                REMOTE_SECRET.encode(),
                "http_" + str(status),
                "bulk_dispatched",
            )
            for status in (401, 403, 429, 500, 502, 503)
        ]
        cases += [
            (418, REMOTE_SECRET.encode(), "http_error", "bulk_dispatched"),
            (
                200,
                REMOTE_SECRET.encode(),
                "response_invalid",
                "bulk_dispatched",
            ),
            (
                200,
                {"status": 777, "message": REMOTE_SECRET},
                "api_rejected",
                "bulk_dispatched",
            ),
            (
                200,
                {"status": 9, "message": REMOTE_SECRET},
                "api_rejected",
                "bulk_response",
            ),
            (
                200,
                {"status": 0, "items": []},
                "bulk_response_partial",
                "bulk_response",
            ),
        ]
        for status, body, cause, stage in cases:
            with self.subTest(status=status, cause=cause):

                class FailingBulkTransport(FakeTransport):
                    def send(inner, request, **kwargs):
                        if request.url.endswith("/api/admin/documents/bulk"):
                            inner.requests.append(request)
                            return _response(request, body, status=status)
                        return super().send(request, **kwargs)

                self.transport = FailingBulkTransport()
                self.client.access.transport = self.transport
                with self.assertRaises(ImportFailed) as caught:
                    self._import()
                record = failure_record(
                    caught.exception, possibly_polluted=True
                )
                self.assertEqual(
                    {
                        "error_type": "ImportFailed",
                        "stage": stage,
                        "cause": cause,
                    },
                    record["diagnostic"],
                )
                self.assertTrue(valid_failure_record(record))
                self.assertTrue(caught.exception.possibly_polluted)
                self.assertEqual(
                    1,
                    sum(
                        request.url.endswith("/api/admin/documents/bulk")
                        for request in self.transport.requests
                    ),
                )
                self.assertFalse(self.transport.token_active)
                self.assertNotIn(REMOTE_SECRET, json.dumps(record))

    def test_post_import_wrapper_retains_only_exact_cause_and_stage(self):
        with mock.patch.object(
            self.client.inventory,
            "snapshot",
            side_effect=[
                InventorySnapshot(()),
                TransportError(REMOTE_SECRET),
            ],
        ):
            with self.assertRaises(ImportFailed) as caught:
                self._import()
        record = failure_record(caught.exception, possibly_polluted=True)
        self.assertEqual(
            {
                "error_type": "ImportFailed",
                "stage": "post_import_inventory",
                "cause": "transport",
            },
            record["diagnostic"],
        )
        self.assertFalse(self.transport.token_active)
        self.assertNotIn(REMOTE_SECRET, json.dumps(record))

    def test_unknown_or_forged_wrapped_cause_remains_generic(self):
        class UnknownTransport(TransportError):
            def __str__(self):
                raise AssertionError("unknown exception must not be rendered")

        forged = TransportError(REMOTE_SECRET)
        forged.diagnostic_cause = {"private": REMOTE_SECRET}
        for error in (UnknownTransport(REMOTE_SECRET), forged):
            session = AdminSession(
                "http://127.0.0.1:8080", transport=self.transport
            )

            def fail_after_dispatch(*args, **kwargs):
                kwargs["on_dispatch"]()
                raise error

            with mock.patch.object(
                session, "_api_request", side_effect=fail_after_dispatch
            ):
                with self.assertRaises(ImportFailed) as caught:
                    session.documents_bulk(TOKEN, [{}], Deadline.after(10))
            self.assertTrue(caught.exception.possibly_polluted)
            self.assertTrue(caught.exception.__suppress_context__)
            record = failure_record(caught.exception, possibly_polluted=True)
            self.assertEqual("unknown", record["error_type"])
            self.assertFalse(valid_failure_record(record))
            self.assertNotIn(REMOTE_SECRET, json.dumps(record))

    def test_cleanup_retains_primary_and_first_unrecovered_error(
        self,
    ):
        class BulkAndCleanupFailure(FakeTransport):
            bulk_failed = False

            def send(inner, request, **kwargs):
                path = urllib.parse.urlsplit(request.url).path
                if inner.bulk_failed and path.startswith(
                    "/admin/accesstoken/list/"
                ):
                    raise TransportError(REMOTE_SECRET)
                if inner.bulk_failed and path == "/api/v2/auth/logout":
                    inner.requests.append(request)
                    return _response(
                        request, REMOTE_SECRET.encode(), status=500
                    )
                try:
                    return super().send(request, **kwargs)
                finally:
                    if path == "/api/admin/documents/bulk":
                        inner.bulk_failed = True

        self.transport = BulkAndCleanupFailure()
        self.transport.bulk_mode = "timeout"
        self.client.access.transport = self.transport
        with self.assertRaises(TokenCleanupError) as caught:
            self._import()
        record = failure_record(caught.exception, possibly_polluted=True)
        self.assertTrue(valid_failure_record(record))
        self.assertEqual(
            {
                "error_type": "ImportFailed",
                "stage": "bulk_dispatched",
                "cause": "operation_deadline",
            },
            record["primary_diagnostic"],
        )
        self.assertEqual(
            {
                "error_type": "TransportError",
                "stage": "token_cleanup",
                "cause": "transport",
            },
            record["cleanup_diagnostic"],
        )
        self.assertTrue(caught.exception.possibly_polluted)
        self.assertEqual(
            1,
            sum(
                request.url.endswith("/api/admin/documents/bulk")
                for request in self.transport.requests
            ),
        )
        self.assertTrue(
            any(
                request.url.endswith("/api/v2/auth/logout")
                for request in self.transport.requests
            )
        )
        self.assertNotIn(REMOTE_SECRET, json.dumps(record))

    def test_reconciled_revoke_failure_does_not_become_cleanup_failure(self):
        class LostDeleteResponse(FakeTransport):
            def send(inner, request, **kwargs):
                response = super().send(request, **kwargs)
                if request.url.endswith(
                    "/api/admin/accesstoken/setting/token-id"
                ):
                    raise TransportError(REMOTE_SECRET)
                return response

        self.transport = LostDeleteResponse()
        self.client.access.transport = self.transport
        self.assertEqual("imported", self._import().status)
        self.assertFalse(self.transport.token_active)
        self.assertEqual(
            1,
            sum(
                request.url.endswith("/api/admin/accesstoken/setting/token-id")
                for request in self.transport.requests
            ),
        )

    def test_timeout_before_first_bulk_dispatch_does_not_quarantine(self):
        expired = [False]

        class ExpiringInventory:
            def snapshot(inner_self, session, token, deadline):
                del inner_self, session, token, deadline
                expired[0] = True
                return InventorySnapshot(())

        client = CatalogImporter(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=lambda: 31.0 if expired[0] else 0.0,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
            inventory=ExpiringInventory(),
        )
        with self.assertRaises(CatalogTimeout) as caught:
            client.import_bundle(
                "admin",
                PASSWORD,
                nonce=NONCE,
                batch_size=2,
                deadline_seconds=30,
            )
        self.assertFalse(
            bool(getattr(caught.exception, "possibly_polluted", False))
        )
        self.assertEqual({}, self.transport.documents)
        self.assertFalse(
            any(
                "documents/bulk" in request.url
                for request in self.transport.requests
            )
        )
        self.assertFalse(self.transport.token_active)

    def test_predispatch_timeout_after_a_committed_batch_stays_quarantined(
        self,
    ):
        expired = [False]

        class ExpireAfterFirstBulk(FakeTransport):
            def send(inner_self, request, *, timeout, max_bytes):
                response = super().send(
                    request, timeout=timeout, max_bytes=max_bytes
                )
                if "/api/admin/documents/bulk" in request.url:
                    expired[0] = True
                return response

        transport = ExpireAfterFirstBulk()
        client = CatalogImporter(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=transport,
                clock=lambda: 31.0 if expired[0] else 0.0,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
            inventory=ScriptedInventory([InventorySnapshot(())]),
        )
        with self.assertRaises(ImportFailed) as caught:
            client.import_bundle(
                "admin",
                PASSWORD,
                nonce=NONCE,
                batch_size=1,
                deadline_seconds=30,
            )
        self.assertTrue(caught.exception.possibly_polluted)
        self.assertEqual(
            {
                "error_type": "ImportFailed",
                "stage": "bulk_after_prior_batch",
                "cause": "operation_deadline",
            },
            failure_record(caught.exception, possibly_polluted=True)[
                "diagnostic"
            ],
        )
        self.assertEqual(1, len(transport.documents))
        self.assertEqual(
            1,
            sum(
                "/api/admin/documents/bulk" in request.url
                for request in transport.requests
            ),
        )
        self.assertFalse(transport.token_active)

    def test_import_and_reconcile_deadlines_have_a_token_safe_hard_cap(self):
        for operation in (
            self.client.import_bundle,
            self.client.reconcile_bundle,
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(
                    ConfigurationError, "token-safe cap"
                ):
                    operation(
                        "admin",
                        PASSWORD,
                        nonce=NONCE,
                        deadline_seconds=600.001,
                    )
        self.assertEqual([], self.transport.requests)

    def test_catalog_admin_cli_rejects_unsafe_token_operation_deadlines(self):
        prefix = [
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-u",
            "-m",
            "search.catalog_admin",
            "--bundle",
            str(self.built.path),
            "--manifest-sha256",
            self.built.manifest_sha256,
        ]
        for command in ("import", "reconcile"):
            for value in ("0", "600.001", "nan"):
                with self.subTest(command=command, value=value):
                    completed = subprocess.run(
                        prefix + [command, "--deadline-seconds", value],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertEqual(2, completed.returncode)
                    self.assertIn(b"deadline-seconds", completed.stderr)
        for value in ("0", "1800.001", "nan"):
            with self.subTest(command="vector-pass", value=value):
                completed = subprocess.run(
                    prefix
                    + [
                        "vector-pass",
                        "--journal-dir",
                        "unused",
                        "--run-id",
                        "1" * 32,
                        "--recipe-sha256",
                        "2" * 64,
                        "--deadline-seconds",
                        value,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(2, completed.returncode)
                self.assertIn(b"deadline-seconds", completed.stderr)

    def test_catalog_admin_maps_cleanup_pollution_to_quarantine_exit_code(
        self,
    ):
        script = Path(__file__).resolve().parents[1] / "catalog_admin.py"
        specification = importlib.util.spec_from_file_location(
            "depositar_catalog_admin_exit_test", script
        )
        self.assertIsNotNone(specification)
        self.assertIsNotNone(specification.loader)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)

        for possibly_polluted, expected_exit in ((False, 3), (True, 4)):

            class CleanupFailureCatalog:
                def __init__(inner_self, *args, **kwargs):
                    del inner_self, args, kwargs

                def import_bundle(inner_self, *args, **kwargs):
                    del inner_self, args, kwargs
                    raise TokenCleanupError(
                        "cleanup could not be verified",
                        possibly_polluted=possibly_polluted,
                    )

            stderr = io.StringIO()
            with (
                self.subTest(possibly_polluted=possibly_polluted),
                mock.patch.object(
                    module.CatalogBundle,
                    "load",
                    return_value=self.built.bundle,
                ),
                mock.patch.object(
                    module, "read_admin_secret", return_value=PASSWORD
                ),
                mock.patch.object(
                    module, "CatalogImporter", CleanupFailureCatalog
                ),
                contextlib.redirect_stderr(stderr),
            ):
                exit_code = module.main(
                    [
                        "--bundle",
                        str(self.built.path),
                        "--manifest-sha256",
                        self.built.manifest_sha256,
                        "import",
                    ]
                )
            self.assertEqual(expected_exit, exit_code)
            self.assertNotIn(PASSWORD, stderr.getvalue())

    def test_admin_failure_json_hides_unknown_or_forged_details(
        self,
    ):
        specification = importlib.util.spec_from_file_location(
            "depositar_catalog_admin_diagnostic_test",
            Path(__file__).resolve().parents[1] / "catalog_admin.py",
        )
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)

        class PrivateDiagnostic:
            def __str__(self):
                raise AssertionError("private value must not be rendered")

        class UnknownTransport(TransportError):
            pass

        class UnknownImport(ImportFailed):
            pass

        forged = TransportError(REMOTE_SECRET)
        forged.diagnostic_cause = PrivateDiagnostic()
        known = _diagnosed(
            ImportFailed(REMOTE_SECRET),
            stage="bulk_dispatched",
            cause="http_503",
        )
        for error, expected_type in (
            (known, "ImportFailed"),
            (TokenCleanupError(REMOTE_SECRET), "TokenCleanupError"),
            (UnknownTransport(REMOTE_SECRET), "unknown"),
            (UnknownImport(REMOTE_SECRET), "unknown"),
            (
                type("ImportFailed", (CatalogError,), {})(REMOTE_SECRET),
                "unknown",
            ),
            (forged, "unknown"),
        ):
            with self.subTest(error_type=type(error).__name__):
                polluted = bool(getattr(error, "possibly_polluted", False))
                fake_catalog = mock.Mock()
                fake_catalog.import_bundle.side_effect = error
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(
                        module.CatalogBundle,
                        "load",
                        return_value=self.built.bundle,
                    ),
                    mock.patch.object(
                        module, "read_admin_secret", return_value=PASSWORD
                    ),
                    mock.patch.object(
                        module, "CatalogImporter", return_value=fake_catalog
                    ),
                    mock.patch.object(
                        type(error),
                        "__str__",
                        side_effect=AssertionError(
                            "must not render exception"
                        ),
                    ),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = module.main(
                        [
                            "--bundle",
                            str(self.built.path),
                            "--manifest-sha256",
                            self.built.manifest_sha256,
                            "import",
                        ]
                    )
                self.assertEqual(4 if polluted else 3, code)
                self.assertEqual("", stdout.getvalue())
                self.assertEqual(1, stderr.getvalue().count("\n"))
                self.assertLessEqual(len(stderr.getvalue().encode()), 4096)
                self.assertNotIn(REMOTE_SECRET, stderr.getvalue())
                self.assertNotIn(PASSWORD, stderr.getvalue())
                record = json.loads(stderr.getvalue())
                self.assertEqual(expected_type, record["error_type"])
                self.assertEqual(
                    expected_type != "unknown", valid_failure_record(record)
                )

    def test_token_cleanup_failure_after_bulk_is_polluted_without_leaking(
        self,
    ):
        self.transport.revoke_fails = True
        with self.assertRaises(TokenCleanupError) as caught:
            self._import()
        self.assertTrue(caught.exception.possibly_polluted)
        self.assertTrue(self.transport.token_active)
        self.assert_no_secret(caught.exception)
        self.assertTrue(
            any(
                request.url.endswith("/api/v2/auth/logout")
                for request in self.transport.requests
            )
        )

    def test_token_cleanup_failure_before_bulk_is_not_polluted(self):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.transport.revoke_fails = True
        with self.assertRaises(TokenCleanupError) as caught:
            self._import()
        self.assertFalse(caught.exception.possibly_polluted)
        self.assertTrue(self.transport.token_active)
        self.assertFalse(
            any(
                "documents/bulk" in request.url
                for request in self.transport.requests
            )
        )
        self.assert_no_secret(caught.exception)

    def test_token_detail_must_confirm_the_exact_utc_expiry(self):
        self.transport.token_expiry_override = "2026-09-09T20:15:00"
        with self.assertRaises(ProtocolError) as caught:
            self._import()
        self.assertFalse(self.transport.token_active)
        self.assert_no_secret(caught.exception)

    def test_access_token_inventory_traverses_every_page_twice(self):
        self.transport.token_listing_pages = [
            [("alpha", "alpha-id")],
            [("target", "target-id")],
        ]
        session = AdminSession(
            "http://127.0.0.1:8080", transport=self.transport
        )
        deadline = Deadline.after(10)
        session.login_admin("admin", PASSWORD, deadline)
        self.assertEqual(
            ["/admin/accesstoken/details/4/target-id"],
            session._listed_token_paths("target", deadline),
        )
        listing_requests = [
            urllib.parse.urlsplit(request.url).path
            for request in self.transport.requests
            if "/admin/accesstoken/list/" in request.url
        ]
        self.assertEqual(
            [
                "/admin/accesstoken/list/1",
                "/admin/accesstoken/list/2",
                "/admin/accesstoken/list/1",
                "/admin/accesstoken/list/2",
            ],
            listing_requests,
        )

    def test_vector_build_requires_active_bundle_and_proves_disabled_job(self):
        with self.assertRaises(VectorBuildError):
            self.vectors.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=30,
                **vector_pass_arguments(self.built.bundle),
            )
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        report = self.vectors.build_vector_pass(
            "admin",
            PASSWORD,
            nonce="abcdefabcdef",
            deadline_seconds=30,
            poll_seconds=0.1,
            **vector_pass_arguments(self.built.bundle),
        )
        self.assertEqual("new-job", report.job_id)
        self.assertFalse(report.as_dict()["generation_enabled"])
        self.assertTrue(report.as_dict()["scheduler_restored_disabled"])
        self.assertTrue(report.as_dict()["progress_verified"])
        self.assertEqual(report.document_count, report.done_count)
        self.assertEqual(["true", "false"], self.transport.scheduler_updates)
        self.assertFalse(self.transport.scheduler_available)
        self.assertEqual(0, self.transport.stop_calls)
        self.assert_no_secret(report.as_dict())

    def test_vector_pass_rejects_document_digest_changes_before_and_after_job(
        self,
    ):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        first = next(iter(self.transport.documents.values()))
        first["depositar_document_sha256"] = _different_sha256(
            first["depositar_document_sha256"]
        )
        with self.assertRaisesRegex(
            VectorBuildError, "exact sealed inventory"
        ):
            self.vectors.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=30,
                poll_seconds=0.1,
                **vector_pass_arguments(self.built.bundle),
            )
        self.assertEqual([], self.transport.scheduler_updates)

        class TamperedAfterJob(FakeTransport):
            def send(inner, request, **kwargs):
                response = super().send(request, **kwargs)
                if urllib.parse.urlsplit(request.url).path.endswith(
                    "/vector-scheduler/start"
                ):
                    first = next(iter(inner.documents.values()))
                    first["depositar_document_sha256"] = _different_sha256(
                        first["depositar_document_sha256"]
                    )
                return response

        transport = TamperedAfterJob()
        transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        client = VectorBuilder(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=transport,
                sleep=lambda _seconds: None,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, tzinfo=datetime.timezone.utc
                ),
            ),
        )
        with self.assertRaisesRegex(
            VectorBuildError, "exact sealed inventory"
        ):
            client.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=30,
                poll_seconds=0.1,
                **vector_pass_arguments(self.built.bundle),
            )
        self.assertEqual(["true", "false"], transport.scheduler_updates)
        self.assertFalse(transport.scheduler_available)

    def test_vector_start_failure_is_not_retried_and_restores_scheduler(self):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.transport.start_failures_remaining = 1
        clock = ManualClock()
        self.client = VectorBuilder(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=clock,
                sleep=clock.sleep,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
        )
        with self.assertRaises(VectorBuildError) as caught:
            self.client.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=300,
                poll_seconds=10,
                **vector_pass_arguments(self.built.bundle),
            )
        starts = [
            request
            for request in self.transport.requests
            if request.url.endswith(
                "/api/admin/scheduler/vector-scheduler/start"
            )
        ]
        self.assertEqual(1, len(starts))
        self.assertEqual(["true", "false"], self.transport.scheduler_updates)
        self.assertEqual(0, self.transport.stop_calls)
        self.assertTrue(caught.exception.possibly_polluted)

    def test_lost_start_waits_for_delayed_launch_quiescence(
        self,
    ):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.transport.start_commits_then_times_out = True
        clock = ManualClock()
        self.client = VectorBuilder(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=clock,
                sleep=clock.sleep,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
        )

        with self.assertRaises(VectorBuildError) as caught:
            self.client.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=300,
                poll_seconds=10,
                **vector_pass_arguments(self.built.bundle),
            )

        starts = [
            request
            for request in self.transport.requests
            if request.url.endswith(
                "/api/admin/scheduler/vector-scheduler/start"
            )
        ]
        self.assertEqual(1, len(starts))
        self.assertTrue(self.transport.ambiguous_running_observed)
        self.assertFalse(self.transport.ambiguous_start_pending)
        self.assertEqual(["true", "false"], self.transport.scheduler_updates)
        self.assertFalse(self.transport.scheduler_available)
        self.assertEqual(0, self.transport.stop_calls)
        self.assertTrue(caught.exception.possibly_polluted)
        self.assertGreaterEqual(clock.value, 120.0)

    def test_scheduler_stop_endpoint_and_response_are_exact(self):
        self.transport.token_active = True
        session = AdminSession(
            "http://127.0.0.1:8080", transport=self.transport
        )
        session.stop_scheduler(TOKEN, "vector-scheduler", Deadline.after(5))
        stop = self.transport.requests[-1]
        self.assertEqual("PUT", stop.method)
        self.assertTrue(
            stop.url.endswith("/api/admin/scheduler/vector-scheduler/stop")
        )
        self.assertIsNone(stop.body)
        before = len(self.transport.requests)
        with self.assertRaises(ConfigurationError):
            session.stop_scheduler(
                TOKEN, "vector-scheduler/other", Deadline.after(5)
            )
        self.assertEqual(before, len(self.transport.requests))
        with self.assertRaises(ConfigurationError):
            session._request(
                "PUT",
                "/api/admin/scheduler/vector-scheduler/stop/again",
                token=TOKEN,
                deadline=Deadline.after(5),
            )

        transport = FakeTransport()
        transport.token_active = True
        transport.stop_mode = "extra_field"
        session = AdminSession("http://127.0.0.1:8080", transport=transport)
        with self.assertRaisesRegex(ProtocolError, "stop response"):
            session.stop_scheduler(
                TOKEN, "vector-scheduler", Deadline.after(5)
            )
        self.assertEqual(1, transport.stop_calls)

    def test_vector_deadline_stops_confirmed_child_once_and_proves_terminal(
        self,
    ):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.transport.vector_job_status = "running"
        clock = ManualClock()
        self.client = VectorBuilder(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=clock,
                sleep=clock.sleep,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
        )
        with self.assertRaises(VectorBuildError) as caught:
            self.client.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=61,
                poll_seconds=1,
                **vector_pass_arguments(self.built.bundle),
            )
        self.assertFalse(caught.exception.possibly_polluted)
        self.assertEqual(1, self.transport.stop_calls)
        self.assertFalse(self.transport.vector_job_running)
        self.assertEqual("fail", self.transport.vector_job_status)
        self.assertEqual(["true", "false"], self.transport.scheduler_updates)
        self.assertFalse(self.transport.scheduler_available)
        update_index = next(
            index
            for index, request in enumerate(self.transport.requests)
            if request.url.endswith("/api/admin/scheduler/setting")
            and request.method == "PUT"
            and json.loads(request.body)["available"] == "false"
        )
        stop_index = next(
            index
            for index, request in enumerate(self.transport.requests)
            if request.url.endswith(
                "/api/admin/scheduler/vector-scheduler/stop"
            )
        )
        self.assertLess(update_index, stop_index)

    def test_proven_child_termination_prevents_stop_retry(
        self,
    ):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.transport.vector_job_status = "running"
        self.transport.stop_mode = "commit_timeout"
        clock = ManualClock()
        self.client = VectorBuilder(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=clock,
                sleep=clock.sleep,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
        )
        with self.assertRaises(VectorBuildError) as caught:
            self.client.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=61,
                poll_seconds=1,
                **vector_pass_arguments(self.built.bundle),
            )
        self.assertEqual(1, self.transport.stop_calls)
        self.assertFalse(caught.exception.possibly_polluted)
        self.assertFalse(self.transport.vector_job_running)

    def test_stop_failure_is_not_retried_and_keeps_vector_quarantined(self):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.transport.vector_job_status = "running"
        self.transport.stop_mode = "failure"
        clock = ManualClock()
        self.client = VectorBuilder(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=clock,
                sleep=clock.sleep,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
        )
        with self.assertRaises(VectorBuildError) as caught:
            self.client.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=61,
                poll_seconds=10,
                **vector_pass_arguments(self.built.bundle),
            )
        self.assertEqual(1, self.transport.stop_calls)
        self.assertTrue(caught.exception.possibly_polluted)
        self.assertTrue(self.transport.vector_job_running)
        self.assertFalse(self.transport.scheduler_available)

    def test_protocol_failure_after_confirmed_start_stops_exact_child(self):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.transport.vector_job_status = "invalid-terminal-status"
        with self.assertRaises(VectorBuildError) as caught:
            self.vectors.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=30,
                poll_seconds=0.1,
                **vector_pass_arguments(self.built.bundle),
            )
        self.assertEqual(1, self.transport.stop_calls)
        self.assertFalse(caught.exception.possibly_polluted)
        self.assertFalse(self.transport.vector_job_running)

    def test_vector_deadline_accepts_1800_but_cannot_exceed_its_token_safe_cap(
        self,
    ):
        with self.assertRaisesRegex(ConfigurationError, "token-safe cap"):
            self.vectors.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=1800.001,
                **vector_pass_arguments(self.built.bundle),
            )
        self.assertEqual([], self.transport.requests)
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        report = self.vectors.build_vector_pass(
            "admin",
            PASSWORD,
            nonce="abcdefabcdef",
            deadline_seconds=1800,
            poll_seconds=0.1,
            **vector_pass_arguments(self.built.bundle),
        )
        self.assertEqual("complete-clean", report.status)
        self.assertEqual(
            ["2026-09-09T12:35:00"], self.transport.created_token_expiries
        )

    def test_vector_deadlines_preserve_cleanup_and_expiry_margin(
        self,
    ):
        self.assertEqual(15, TOKEN_LIFETIME_MINUTES)
        self.assertEqual(35, VECTOR_TOKEN_LIFETIME_MINUTES)
        self.assertEqual(600.0, MAX_TOKEN_OPERATION_DEADLINE_SECONDS)
        self.assertEqual(1800.0, MAX_VECTOR_DEADLINE_SECONDS)
        self.assertEqual(2040.0, MAX_VECTOR_TOKEN_API_SECONDS)
        self.assertEqual(240.0, VECTOR_RECOVERY_SECONDS)
        normal_success_margin = (
            VECTOR_TOKEN_LIFETIME_MINUTES * 60
            - MAX_VECTOR_DEADLINE_SECONDS
            - VECTOR_VERIFICATION_SECONDS
            - TOKEN_CLEANUP_SECONDS
        )
        self.assertEqual(210.0, normal_success_margin)
        self.assertEqual(
            VECTOR_RECOVERY_SECONDS,
            MAX_VECTOR_TOKEN_API_SECONDS - MAX_VECTOR_DEADLINE_SECONDS,
        )
        self.assertLessEqual(
            VECTOR_START_QUIESCENCE_SECONDS + VECTOR_VERIFICATION_SECONDS,
            VECTOR_RECOVERY_SECONDS,
        )
        self.assertEqual(
            30.0,
            VECTOR_TOKEN_LIFETIME_MINUTES * 60
            - MAX_VECTOR_TOKEN_API_SECONDS
            - TOKEN_CLEANUP_SECONDS,
        )

    def test_vector_recovery_respects_shared_token_deadline(
        self,
    ):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.transport.vector_job_status = "running"
        self.transport.stop_mode = "failure"
        clock = ManualClock()
        client = VectorBuilder(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=clock,
                sleep=clock.sleep,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            ),
        )

        with self.assertRaises(VectorBuildError) as caught:
            client.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=1800,
                poll_seconds=10,
                **vector_pass_arguments(self.built.bundle),
            )

        self.assertTrue(caught.exception.possibly_polluted)
        self.assertEqual(MAX_VECTOR_TOKEN_API_SECONDS, clock.value)
        self.assertFalse(self.transport.token_active)

    def test_vector_failure_never_enables_generation(self):
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.transport.vector_job_status = "failed"
        with self.assertRaises(VectorBuildError) as caught:
            self.vectors.build_vector_pass(
                "admin",
                PASSWORD,
                nonce=NONCE,
                deadline_seconds=30,
                poll_seconds=0.1,
                **vector_pass_arguments(self.built.bundle),
            )
        self.assertFalse(caught.exception.generation_enabled)
        self.assertEqual(["true", "false"], self.transport.scheduler_updates)
        self.assertFalse(self.transport.scheduler_available)
        self.assertFalse(self.transport.token_active)

    def test_injected_transport_respects_caps_origin_and_endpoints(
        self,
    ):
        session = AdminSession(
            "http://127.0.0.1:8080", transport=self.transport
        )
        deadline = Deadline.after(5)
        with self.assertRaises(ConfigurationError):
            session._request("GET", "/api/admin/arbitrary", deadline=deadline)

        class Oversized:
            def send(self, request, *, timeout, max_bytes):
                return HTTPResponse(
                    200, (), b"x" * (max_bytes + 1), request.url
                )

        session = AdminSession("http://127.0.0.1:8080", transport=Oversized())
        with self.assertRaises(ProtocolError):
            session.login("user", PASSWORD, Deadline.after(5))

        class CrossOrigin:
            def send(self, request, *, timeout, max_bytes):
                return HTTPResponse(
                    200, (), b"{}", "http://127.0.0.2:8080/api/v2/auth/login"
                )

        session = AdminSession(
            "http://127.0.0.1:8080", transport=CrossOrigin()
        )
        with self.assertRaises(ProtocolError):
            session.login("user", PASSWORD, Deadline.after(5))

    def test_bundle_rejects_wrong_digest_and_unsafe_mode(self):
        with self.assertRaises(BundleError):
            CatalogBundle.load(
                self.built.path, expected_manifest_sha256="0" * 64
            )
        os.chmod(self.built.path, 0o700)
        document_path = self.built.path / DOCUMENTS_NAME
        os.chmod(document_path, 0o644)
        os.chmod(self.built.path, 0o500)
        with self.assertRaises(BundleError):
            CatalogBundle.load(
                self.built.path,
                expected_manifest_sha256=self.built.manifest_sha256,
            )

    def test_builder_and_loader_manifest_policies_are_exactly_identical(self):
        builder_policy = json.dumps(
            policy.POLICY,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        loader_policy = json.dumps(
            CATALOG_MANIFEST_POLICY,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(builder_policy, loader_policy)

    def test_manifest_rejects_policy_type_substitution(self):
        self.built.reseal(
            manifest_mutation=lambda manifest: manifest["policy"][
                "fess"
            ].__setitem__("email_addresses", 0)
        )
        with self.assertRaisesRegex(BundleError, "manifest values"):
            CatalogBundle.load(
                self.built.path,
                expected_manifest_sha256=self.built.manifest_sha256,
            )

    def test_manifest_and_artifact_schema_booleans_are_not_integers(self):
        mutations = (
            lambda manifest: manifest.__setitem__("schema_version", True),
            lambda manifest: manifest["artifacts"][DOCUMENTS_NAME].__setitem__(
                "schema_version", True
            ),
            lambda manifest: manifest["artifacts"][DOCUMENTS_NAME].__setitem__(
                "records", True
            ),
        )
        for mutation in mutations:
            candidate = BuiltBundle(count=1)
            try:
                candidate.reseal(manifest_mutation=mutation)
                with self.assertRaises(BundleError):
                    CatalogBundle.load(
                        candidate.path,
                        expected_manifest_sha256=candidate.manifest_sha256,
                    )
            finally:
                candidate.close()

    def test_manifest_resource_total_is_cross_checked(self):
        self.built.reseal(
            manifest_mutation=lambda manifest: manifest.__setitem__(
                "resource_count", manifest["resource_count"] + 1
            )
        )
        with self.assertRaisesRegex(BundleError, "resource count"):
            CatalogBundle.load(
                self.built.path,
                expected_manifest_sha256=self.built.manifest_sha256,
            )

    def test_source_corpus_resource_counts_are_cross_checked_after_resealing(
        self,
    ):
        def remove_source_resource(rows):
            rows[0]["resources"] = []

        self.built.reseal(
            artifact_mutations={"source.jsonl.gz": remove_source_resource}
        )
        with self.assertRaisesRegex(BundleError, "resource counts disagree"):
            CatalogBundle.load(
                self.built.path,
                expected_manifest_sha256=self.built.manifest_sha256,
            )

    def test_resealed_corpus_document_identities_are_cross_checked(
        self,
    ):
        def replace_document_id(rows):
            rows[0]["dataset_id"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"

        self.built.reseal(
            artifact_mutations={DOCUMENTS_NAME: replace_document_id}
        )
        with self.assertRaisesRegex(
            BundleError, "disagree on dataset identity"
        ):
            CatalogBundle.load(
                self.built.path,
                expected_manifest_sha256=self.built.manifest_sha256,
            )


class ResourceContextBundleTests(unittest.TestCase):
    def setUp(self):
        self.built = BuiltBundle(count=1)
        self.addCleanup(self.built.close)
        resource_id = "00000000-0000-4000-8000-000000000003"
        self.entry = {
            "context": {
                "resource_id": resource_id,
                "name": "測站 CSV",
                "format": "CSV",
                "url": self.built.bundle.documents[0]["url"]
                + "/resource/"
                + resource_id,
                "columns": ["profile_only_column"],
                "excerpt": "",
            },
            "index_text": "profile_only_column",
            "profile_sha256": "a" * 64,
            "source_manifest_sha256": "b" * 64,
            "content_sha256": "c" * 64,
            "extractor_image_id": "sha256:" + "d" * 64,
            "extractor_source_sha256": "e" * 64,
            "acquired_at": 1_800_000_000,
            "truncated": False,
        }

        def bind_resource(rows):
            rows[0]["resources"][0]["id"] = resource_id

        def bind_document(rows):
            rows[0]["resource_context"] = [self.entry["context"]]
            rows[0]["content"] += " " + self.entry["index_text"]

        self.built.reseal(
            artifact_mutations={
                "source.jsonl.gz": bind_resource,
                "corpus.v2.jsonl.gz": bind_resource,
                DOCUMENTS_NAME: bind_document,
                RESOURCE_CONTEXT_NAME: lambda rows: rows[0].__setitem__(
                    "resources", [self.entry]
                ),
            }
        )
        self.load()

    def load(self):
        return CatalogBundle.load(
            self.built.path,
            expected_manifest_sha256=self.built.manifest_sha256,
        )

    def test_fess_bulk_serializes_public_context_without_private_provenance(
        self,
    ):
        transport = FakeTransport()
        client = CatalogImporter(
            self.load(),
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=transport,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
                sleep=lambda _seconds: None,
            ),
        )
        client.import_bundle(
            "admin", PASSWORD, nonce=NONCE, batch_size=1, deadline_seconds=30
        )
        document = next(iter(transport.documents.values()))
        self.assertEqual([self.entry["context"]], document["resource_context"])
        self.assertIsInstance(document["resource_context"][0]["columns"], list)
        self.assertNotIn("profile_sha256", json.dumps(document))
        self.assertNotIn("source_manifest_sha256", json.dumps(document))

    def test_document_public_projection_is_cross_checked_after_resealing(self):
        self.built.reseal(
            artifact_mutations={
                DOCUMENTS_NAME: lambda rows: rows[0].__setitem__(
                    "resource_context", []
                )
            }
        )
        with self.assertRaisesRegex(
            BundleError, "document and resource context disagree"
        ):
            self.load()

    def test_context_dataset_and_schema_are_exact_after_resealing(self):
        for field, value in (
            ("dataset_id", "f" * 36),
            ("schema_version", True),
        ):
            # Restore the valid row between independent shape checks.
            self.built.reseal(
                artifact_mutations={
                    RESOURCE_CONTEXT_NAME: lambda rows: rows[0].update(
                        dataset_id="00000000-0000-4000-8000-000000000001",
                        schema_version=1,
                    )
                }
            )
            self.built.reseal(
                artifact_mutations={
                    RESOURCE_CONTEXT_NAME: lambda rows: rows[0].__setitem__(
                        field, value
                    )
                }
            )
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(BundleError, "resource context row"),
            ):
                self.load()

    def test_profile_provenance_is_validated_after_resealing(self):
        self.built.reseal(
            artifact_mutations={
                RESOURCE_CONTEXT_NAME: lambda rows: rows[0]["resources"][
                    0
                ].__setitem__("profile_sha256", "invalid")
            }
        )
        with self.assertRaisesRegex(
            BundleError, "resource enrichment is invalid"
        ):
            self.load()

    def test_resource_must_exist_in_original_source_after_resealing(self):
        self.built.reseal(
            artifact_mutations={
                "source.jsonl.gz": lambda rows: rows[0]["resources"][
                    0
                ].__setitem__("id", "unknown")
            }
        )
        with self.assertRaisesRegex(
            BundleError, "does not match its dataset resource"
        ):
            self.load()

    def test_resource_format_must_match_normalized_metadata_after_resealing(
        self,
    ):
        self.built.reseal(
            artifact_mutations={
                "corpus.v2.jsonl.gz": lambda rows: rows[0]["resources"][
                    0
                ].__setitem__("format", "PDF")
            }
        )
        with self.assertRaisesRegex(
            BundleError, "does not match its dataset resource"
        ):
            self.load()

    def test_indexed_content_cannot_omit_bound_profile_text(self):
        self.built.reseal(
            artifact_mutations={
                DOCUMENTS_NAME: lambda rows: rows[0].__setitem__(
                    "content", "metadata only"
                )
            }
        )
        with self.assertRaisesRegex(BundleError, "indexed content omits"):
            self.load()

    def test_old_bundle_format_has_no_parallel_compatibility_path(self):
        self.built.reseal(
            manifest_mutation=lambda manifest: manifest.__setitem__(
                "bundle_format", "depositar-public-metadata-v2"
            )
        )
        with self.assertRaisesRegex(BundleError, "manifest values"):
            self.load()


class QueryBoundaryTests(unittest.TestCase):
    def test_request_representation_keeps_ordered_credentials_and_identity(
        self,
    ):
        cases = (
            (
                {"json_body": {"z": 1, "a": "中"}},
                (
                    ("Accept", "application/json"),
                    ("Accept-Language", "en"),
                    ("Content-Type", "application/json"),
                ),
                '{"a":"中","z":1}'.encode("utf-8"),
            ),
            (
                {"form_body": {"z": "two words", "a": "中"}},
                (
                    ("Accept", "text/html"),
                    ("Accept-Language", "en"),
                    ("Content-Type", "application/x-www-form-urlencoded"),
                ),
                b"z=two+words&a=%E4%B8%AD",
            ),
            ({}, (("Accept", "text/html"), ("Accept-Language", "en")), None),
        )
        for arguments, prefix, expected_body in cases:
            with self.subTest(arguments=arguments):
                events, responses = [], []
                transport = mock.Mock()
                session = AdminSession(
                    "http://127.0.0.1:8080", transport=transport
                )
                session._session_id, session._csrf_token = "s" * 16, "c" * 20
                before = dict(vars(session))

                def clock():
                    events.append("clock")
                    return 1.0

                def send(request, *, timeout, max_bytes):
                    events.append("send")
                    self.assertEqual(timeout, 10.0)
                    self.assertEqual(max_bytes, 32)
                    response = HTTPResponse(
                        200, (("Content-Length", "2"),), b"{}", request.url
                    )
                    responses.append(response)
                    return response

                transport.send.side_effect = send
                result = session._request(
                    "POST" if arguments else "GET",
                    "/admin/accesstoken/",
                    deadline=Deadline(11.0, clock),
                    query=(("q", "a b"), ("q", "中")),
                    token=TOKEN,
                    require_session=True,
                    csrf=True,
                    max_bytes=32,
                    on_dispatch=lambda: events.append("dispatch"),
                    **arguments,
                )
                self.assertIs(result, responses[0])
                self.assertEqual(vars(session), before)
                self.assertEqual(events, ["clock", "dispatch", "send"])
                transport.send.assert_called_once()
                request = transport.send.call_args.args[0]
                self.assertEqual(request.body, expected_body)
                self.assertEqual(
                    request.url,
                    "http://127.0.0.1:8080/admin/accesstoken/"
                    "?q=a+b&q=%E4%B8%AD",
                )
                self.assertEqual(
                    request.headers,
                    prefix
                    + (
                        ("Authorization", "Bearer " + TOKEN),
                        ("Cookie", "JSESSIONID=" + "s" * 16),
                        ("X-Fess-CSRF-Token", "c" * 20),
                    ),
                )

    def test_admission_and_credentials_precede_expired_deadline(self):
        cases = (
            (
                "TRACE",
                "/forbidden",
                {"json_body": {}, "form_body": {}},
                None,
                None,
                ConfigurationError,
                "internal request has multiple body encodings",
            ),
            (
                "TRACE",
                "/forbidden",
                {},
                None,
                None,
                ConfigurationError,
                "internal request method is invalid",
            ),
            (
                "GET",
                "/api/admin/systeminfo",
                {},
                None,
                None,
                ConfigurationError,
                "internal Fess endpoint is not allowlisted",
            ),
            (
                "POST",
                "/api/v2/auth/login",
                {"token": "bad"},
                None,
                None,
                AuthenticationError,
                "ephemeral access token is malformed",
            ),
            (
                "POST",
                "/api/v2/auth/login",
                {"token": TOKEN},
                None,
                None,
                AuthenticationError,
                "Fess session is unavailable",
            ),
            (
                "POST",
                "/api/v2/auth/login",
                {"token": TOKEN},
                "s" * 16,
                None,
                AuthenticationError,
                "Fess CSRF state is unavailable",
            ),
            (
                "POST",
                "/api/v2/auth/login",
                {"token": TOKEN},
                "s" * 16,
                "c" * 20,
                CatalogTimeout,
                "operation deadline expired",
            ),
        )
        for method, path, arguments, sid, csrf, error, message in cases:
            with self.subTest(message=message):
                events = []
                transport = mock.Mock()
                session = FessSession(
                    "http://127.0.0.1:8080", transport=transport
                )
                session._session_id, session._csrf_token = sid, csrf
                before = dict(vars(session))

                def clock():
                    events.append("clock")
                    return 1.0

                with self.assertRaises(error) as raised:
                    session._request(
                        method,
                        path,
                        deadline=Deadline(0.0, clock),
                        require_session=True,
                        csrf=True,
                        on_dispatch=lambda: events.append("dispatch"),
                        **arguments,
                    )
                self.assertEqual(raised.exception.args, (message,))
                self.assertEqual(vars(session), before)
                self.assertEqual(
                    events, ["clock"] if error is CatalogTimeout else []
                )
                transport.send.assert_not_called()

    def test_response_admission_precedes_explicit_status_acceptance(self):
        url = "http://127.0.0.1:8080/api/v2/auth/login"
        cases = (
            (
                HTTPResponse(True, [], "bad", "/wrong"),
                32,
                (),
                "Fess response status is malformed",
            ),
            (
                HTTPResponse(200, [], b"private response fixture", url),
                1,
                (),
                "Fess response exceeded its byte cap",
            ),
            (
                HTTPResponse(
                    200,
                    (("Content-Length", "2"), ("content-length", "2")),
                    b"{}",
                    "/wrong",
                ),
                32,
                (),
                "Fess response has ambiguous content length",
            ),
            (
                HTTPResponse(200, (), b"{}", url + "/changed"),
                32,
                (),
                "Fess transport crossed the fixed origin",
            ),
            (
                HTTPResponse(302, (("Location", "/login"),), b"", url),
                32,
                (302,),
                "Fess redirect was not explicitly expected",
            ),
            (
                HTTPResponse(429, (("Retry-After", "tomorrow"),), b"", url),
                32,
                (),
                "Fess Retry-After is not a supported delta",
            ),
            (
                HTTPResponse(429, (("Retry-After", "tomorrow"),), b"", url),
                32,
                (429,),
                None,
            ),
        )
        for response, cap, accepted, message in cases:
            with self.subTest(message=message):
                transport = mock.Mock()
                transport.send.return_value = response
                session = FessSession(
                    "http://127.0.0.1:8080", transport=transport
                )
                session._session_id, session._csrf_token = "s" * 16, "c" * 20
                before = dict(vars(session))
                arguments = {
                    "deadline": Deadline(10.0, lambda: 0.0),
                    "max_bytes": cap,
                    "accepted_http_statuses": accepted,
                }
                if message is None:
                    self.assertIs(
                        session._request(
                            "POST", "/api/v2/auth/login", **arguments
                        ),
                        response,
                    )
                else:
                    with self.assertRaises(ProtocolError) as raised:
                        session._request(
                            "POST", "/api/v2/auth/login", **arguments
                        )
                    self.assertEqual(raised.exception.args, (message,))
                    self.assertNotIn(
                        "private response fixture", str(raised.exception)
                    )
                self.assertEqual(vars(session), before)
                transport.send.assert_called_once()

    def test_query_session_rejects_administrator_endpoints_before_dispatch(
        self,
    ):
        transport = FakeTransport()
        session = FessSession("http://127.0.0.1:8080", transport=transport)
        for method, path in (
            ("GET", "/api/admin/systeminfo"),
            ("PUT", "/api/admin/documents/bulk"),
            ("GET", "/admin/accesstoken/"),
        ):
            with (
                self.subTest(method=method, path=path),
                self.assertRaises(ConfigurationError),
            ):
                session._request(method, path, deadline=Deadline.after(1))
        self.assertEqual([], transport.requests)

    def test_retired_cli_modes_are_rejected_before_loading_catalog_data(self):
        from search import catalog_admin

        for mode in ("query", "vectors"):
            with (
                self.subTest(mode=mode),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                catalog_admin.parse_args(
                    [
                        "--bundle",
                        "/unused",
                        "--manifest-sha256",
                        "0" * 64,
                        mode,
                    ]
                )
            self.assertEqual(2, caught.exception.code)


class FakeTransportContractTests(unittest.TestCase):
    AUTH = (("Authorization", "Bearer " + TOKEN),)
    ADMIN = (("Accept-Language", "en"),)
    PERMISSION = "{role}admin-api"

    @staticmethod
    def request(path, *, method="GET", headers=(), payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        return HTTPRequest(method, "http://example.test" + path, headers, body)

    @staticmethod
    def authorized():
        transport = FakeTransport()
        transport.token_active = True
        return transport

    def send(self, transport, request):
        response = transport.send(request, timeout=0, max_bytes=0)
        self.assertIs(request, transport.requests[-1])
        self.assertIs(request.url, response.url)
        return response

    def response(self, actual, value, *, status=200, headers=()):
        expected = (
            value
            if isinstance(value, bytes)
            else json.dumps(
                {"response": value}, sort_keys=True, separators=(",", ":")
            ).encode()
        )
        self.assertEqual(
            (status, headers, expected),
            (actual.status, actual.headers, actual.body),
        )

    def timeout(self, transport, request, message):
        sentinel = CatalogTimeout("fixed synthetic timeout sentinel")
        module = sys.modules[type(transport).__module__]
        with mock.patch.object(
            module, "CatalogTimeout", return_value=sentinel
        ) as factory:
            with self.assertRaises(CatalogTimeout) as caught:
                transport.send(request, timeout=0, max_bytes=0)
        factory.assert_called_once_with(message)
        self.assertIs(sentinel, caught.exception)
        self.assertIs(request, transport.requests[-1])

    def test_entry_dispatch_locale_and_auth_keep_request_identity(self):
        cookie = (("Cookie", "admin-session"),)
        bad = (("Authorization", "Bearer wrong"), *cookie)
        cases = (
            (
                "/admin/unknown",
                "GET",
                (),
                None,
                False,
                "admin HTML request did not pin the English locale",
                None,
            ),
            (
                "/admin/unknown",
                "GET",
                self.ADMIN,
                None,
                False,
                "unexpected path /admin/unknown",
                None,
            ),
            (
                "/api/admin/searchlist/docs",
                "GET",
                bad,
                None,
                True,
                None,
                {"status": 3},
            ),
            (
                "/api/admin/searchlist/docs",
                "GET",
                (*self.AUTH, *cookie),
                None,
                True,
                "bearer API request also carried an admin cookie",
                None,
            ),
            (
                "/api/admin/searchlist/docs",
                "GET",
                self.AUTH,
                None,
                True,
                "SearchList pagination was not sent as a PUT JSON body",
                None,
            ),
            (
                "/api/admin/scheduler/setting/vector-scheduler",
                "POST",
                (),
                None,
                False,
                "unexpected path /api/admin/scheduler/setting/"
                "vector-scheduler",
                None,
            ),
            (
                "/api/admin/documents/bulk",
                "GET",
                self.AUTH,
                {"documents": []},
                True,
                None,
                {"status": 0, "items": []},
            ),
            (
                "/api/v2/auth/login",
                "GET",
                (),
                {"username": "admin", "password": "wrong"},
                False,
                None,
                {"status": 3},
            ),
        )
        for path, method, headers, payload, active, error, expected in cases:
            with self.subTest(path=path, method=method, error=error):
                transport = FakeTransport()
                transport.token_active = active
                request = self.request(
                    path, method=method, headers=headers, payload=payload
                )
                if error is not None:
                    with self.assertRaisesRegex(AssertionError, error):
                        transport.send(request, timeout=0, max_bytes=0)
                else:
                    status = (
                        403 if expected == {"status": 3} and active else 200
                    )
                    self.response(
                        self.send(transport, request), expected, status=status
                    )
                self.assertEqual(1, len(transport.requests))
                self.assertIs(request, transport.requests[0])
                self.assertEqual({}, transport.documents)
        transport = FakeTransport()
        request = HTTPRequest("GET", "http://[invalid", ())
        with self.assertRaises(ValueError):
            transport.send(request, timeout=0, max_bytes=0)
        self.assertIs(request, transport.requests[0])

    def test_token_state_precedes_permission_and_expiry_assertions(self):
        valid = "2026-09-09T12:15:00"
        for permissions, expires, error in (
            ("wrong", valid, "token was overprivileged"),
            (self.PERMISSION, "wrong", "token lifetime was not allowlisted"),
            ("wrong", "wrong", "token was overprivileged"),
        ):
            with self.subTest(permissions=permissions, expires=expires):
                transport = FakeTransport()
                body = urllib.parse.urlencode(
                    {
                        "name": "fixture-token",
                        "permissions": permissions,
                        "expires": expires,
                    }
                ).encode("ascii")
                request = HTTPRequest(
                    "POST",
                    "http://example.test/admin/accesstoken/",
                    self.ADMIN,
                    body,
                )
                with self.assertRaisesRegex(AssertionError, error):
                    transport.send(request, timeout=0, max_bytes=0)
                self.assertEqual("fixture-token", transport.token_name)
                self.assertTrue(transport.token_active)
                self.assertEqual([], transport.created_token_expiries)
                self.assertIs(request, transport.requests[0])
        transport = FakeTransport()
        body = urllib.parse.urlencode(
            {
                "name": "fixture-token",
                "permissions": self.PERMISSION,
                "expires": valid,
            }
        ).encode("ascii")
        request = HTTPRequest(
            "POST", "http://example.test/admin/accesstoken/", self.ADMIN, body
        )
        self.response(
            self.send(transport, request),
            b"",
            status=302,
            headers=(("Location", "/admin/accesstoken/"),),
        )
        self.assertEqual([valid], transport.created_token_expiries)

    def test_bulk_keeps_partial_and_timeout_committed_prefix(self):
        rows = [
            {"url": "https://example.test/one", "title": "one"},
            {"url": "https://example.test/two", "title": "two"},
        ]
        for mode, count, expected in (
            (
                "success",
                2,
                {
                    "status": 0,
                    "items": [
                        {"result": "CREATED", "id": "created-1"},
                        {"result": "CREATED", "id": "created-2"},
                    ],
                },
            ),
            (
                "partial",
                1,
                {
                    "status": 9,
                    "items": [{"result": "CREATED", "id": "created-1"}],
                    "message": REMOTE_SECRET,
                },
            ),
            ("timeout", 2, None),
        ):
            with self.subTest(mode=mode):
                transport = self.authorized()
                documents = transport.documents
                documents["prior"] = {"url": "prior", "title": "retained"}
                transport.bulk_mode = mode
                request = self.request(
                    "/api/admin/documents/bulk",
                    method="PUT",
                    headers=self.AUTH,
                    payload={"documents": rows},
                )
                if mode == "timeout":
                    self.timeout(
                        transport,
                        request,
                        "simulated timeout without secret data",
                    )
                else:
                    self.response(self.send(transport, request), expected)
                self.assertIs(documents, transport.documents)
                self.assertEqual(
                    ["prior", *[row["url"] for row in rows[:count]]],
                    list(transport.documents),
                )
                self.assertEqual(
                    {
                        "prior": {"url": "prior", "title": "retained"},
                        **{row["url"]: row for row in rows[:count]},
                    },
                    transport.documents,
                )
                self.assertEqual(1, len(transport.requests))

    def test_detail_keeps_each_observation_and_stored_running(self):
        for stored in (False, True):
            with self.subTest(stored_running=stored):
                transport = self.authorized()
                transport.vector_job_running = stored
                transport.ambiguous_start_pending = True
                transport.documents = {"one": {"marker": "retained"}}
                documents = transport.documents
                denied = self.request(
                    "/api/admin/scheduler/setting/vector-scheduler"
                )
                self.response(
                    self.send(transport, denied), {"status": 3}, status=403
                )
                self.assertEqual(0, transport.ambiguous_detail_checks)
                for index, running in enumerate(
                    (stored, stored, True, True, stored), 1
                ):
                    request = self.request(
                        "/api/admin/scheduler/setting/vector-scheduler",
                        headers=self.AUTH,
                    )
                    self.response(
                        self.send(transport, request),
                        {
                            "status": 0,
                            "setting": {
                                "id": "vector-scheduler",
                                "version_no": -1,
                                "name": "Content Chunk Vector Indexer",
                                "target": "all",
                                "script_type": "groovy",
                                "script_data": (
                                    "return container.getComponent("
                                    '"chunkVectorJob")'
                                    '.logLevel("info").sessionId("CHUNK")'
                                    ".execute(executor);"
                                ),
                                "crawler": "false",
                                "job_logging": "true",
                                "available": "false",
                                "sort_order": 14,
                                "running": running,
                            },
                        },
                    )
                    self.assertEqual(index, transport.ambiguous_detail_checks)
                    self.assertEqual(
                        index < 5, transport.ambiguous_start_pending
                    )
                    self.assertEqual(index >= 3, transport.scheduler_started)
                    self.assertEqual(
                        index >= 3, transport.ambiguous_running_observed
                    )
                    self.assertIs(stored, transport.vector_job_running)
                    expected = {"marker": "retained"}
                    if index >= 3:
                        expected["content_chunk_status"] = "done"
                    self.assertEqual({"one": expected}, documents)
                    self.assertIs(documents, transport.documents)
                self.assertEqual(6, len(transport.requests))

    def test_start_stop_keep_timeout_identity_and_mutation_order(self):
        for (
            available,
            failures,
            lost,
            expected,
            remaining,
            pending,
            started,
        ) in (
            (False, 2, True, {"status": 9}, 2, False, False),
            (True, 1, True, {"status": 9}, 0, False, False),
            (True, 0, True, None, 0, True, False),
            (
                True,
                0,
                False,
                {"status": 0, "job_log_id": "new-job"},
                0,
                False,
                True,
            ),
        ):
            with self.subTest(start=(available, failures, lost)):
                transport = self.authorized()
                transport.scheduler_available = available
                transport.start_failures_remaining = failures
                transport.start_commits_then_times_out = lost
                transport.ambiguous_detail_checks = 9
                transport.documents = {
                    "one": {"content_chunk_status": "pending"}
                }
                request = self.request(
                    "/api/admin/scheduler/vector-scheduler/start",
                    headers=self.AUTH,
                )
                if expected is None:
                    self.timeout(
                        transport, request, "simulated lost start response"
                    )
                else:
                    self.response(self.send(transport, request), expected)
                self.assertEqual(
                    (remaining, pending, started, started, False),
                    (
                        transport.start_failures_remaining,
                        transport.ambiguous_start_pending,
                        transport.scheduler_started,
                        transport.job_log_created,
                        transport.vector_job_running,
                    ),
                )
                self.assertEqual(
                    0 if pending else 9, transport.ambiguous_detail_checks
                )
                self.assertEqual(
                    "done" if started else "pending",
                    transport.documents["one"]["content_chunk_status"],
                )
                self.assertEqual(1, len(transport.requests))
        for mode, expected, timeout, committed in (
            ("success", {"status": 0}, False, True),
            ("failure", {"status": 9}, False, False),
            ("commit_timeout", None, True, True),
            ("no_commit_timeout", None, True, False),
            ("extra_field", {"status": 0, "unexpected": True}, False, True),
            ("unknown-mode", {"status": 0}, False, True),
        ):
            with self.subTest(stop=mode):
                transport = self.authorized()
                transport.stop_mode = mode
                transport.vector_job_running = True
                transport.vector_job_status = "running"
                transport.vector_script_result = "original"
                request = self.request(
                    "/api/admin/scheduler/vector-scheduler/stop",
                    headers=self.AUTH,
                )
                if timeout:
                    self.timeout(
                        transport, request, "simulated lost stop response"
                    )
                else:
                    self.response(self.send(transport, request), expected)
                self.assertEqual(1, transport.stop_calls)
                self.assertEqual(
                    (
                        not committed,
                        "fail" if committed else "running",
                        "stopped" if committed else "original",
                    ),
                    (
                        transport.vector_job_running,
                        transport.vector_job_status,
                        transport.vector_script_result,
                    ),
                )
                self.assertEqual(1, len(transport.requests))


if __name__ == "__main__":
    unittest.main()
