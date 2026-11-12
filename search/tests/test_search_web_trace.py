"""Synthetic consent, immutable turn projection and withdrawal races."""

import asyncio
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from search.api.conversation.contract import TurnRequest
from search.api.conversation.engine import ConversationEngine
from search.api.shared.config import Settings
from search.tests.asgi_client import ASGITestClient
from search.tests.fixtures import ConversationFixture, PLAN
from search.tests.test_search_web_identity import public_headers
from search.tests.web_support import FakeUpstream, ORIGIN, SETTINGS
from search.trace.contract import NOTICE_VERSION, validate_event
from search.trace.limits import TraceLimits
from search.trace.store import TraceStore
from search.web.app import create_app
from search.web.sessions import Sessions
from search.web.upstream import UpstreamError, WebSettings


class MemorySink:
    def __init__(self):
        self.events, self.gaps, self.revoked = [], [], {}
        self.failure = None
        self.deletion = "complete"

    def start(self):
        pass

    def close(self, timeout):
        self.timeout = timeout

    def enqueue(self, event):
        if self.failure:
            raise self.failure
        self.events.append(copy.deepcopy(event))
        return "queued"

    def note_gap(self, test_id, epoch, turn, code):
        self.gaps.append((test_id, epoch, turn, code))

    def revoke(self, test_id, epoch):
        self.revoked[test_id] = epoch
        self.events = [
            event
            for event in self.events
            if event["test_session_id"] != test_id
        ]
        return self.deletion

    def status(self, test_id):
        return {
            "capture_status": "revoked"
            if test_id in self.revoked
            else "queued",
            "deletion_status": self.deletion
            if test_id in self.revoked
            else "none",
        }


class EngineUpstream:
    def __init__(self):
        self.backend = ConversationFixture()
        self.engine = ConversationEngine(
            Settings("b" * 40, "b" * 64, Path("/catalog"), Path("/unused")),
            self.backend,
        )
        self.number = 0

    async def turn(self, payload):
        self.number += 1
        self.backend.plan = {
            **PLAN,
            "action": "new_search" if self.number == 1 else "refine",
            "topic": "氣候" if self.number == 1 else None,
            "time_op": "set",
            "time": {
                "start": "2024" if self.number == 1 else "2023",
                "end": "2024" if self.number == 1 else "2023",
            },
            "formats_op": "set" if self.number == 1 else "keep",
            "formats": ["CSV"] if self.number == 1 else [],
        }
        return await self.engine.turn(
            TurnRequest.model_validate(payload), f"upstream-{self.number}"
        )


class GatedUpstream(FakeUpstream):
    def __init__(self):
        super().__init__()
        self.started, self.release = threading.Event(), threading.Event()

    async def turn(self, payload):
        self.started.set()
        await asyncio.to_thread(self.release.wait, 5)
        return await super().turn(payload)


class ConsentedWebsiteTests(unittest.TestCase):
    def setUp(self):
        self.sink, self.sessions = MemorySink(), Sessions()
        self.upstream = FakeUpstream()
        self.settings = replace(SETTINGS, trace_root=Path("/unused-private"))
        self.headers = {"Origin": ORIGIN}

    def start(self, upstream=None, settings=None):
        client = ASGITestClient(
            create_app(
                settings or self.settings,
                upstream or self.upstream,
                self.sessions,
                self.sink,
            ),
            raise_app_exceptions=False,
        )
        self.addCleanup(client.close)
        return client

    def session(self, client, *, capture=True, headers=None):
        response = client.post(
            "/api/session",
            json={"capture": True, "notice_version": NOTICE_VERSION}
            if capture
            else {},
            headers=headers or self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        return value, {
            **(headers or self.headers),
            "Authorization": "Bearer " + value["token"],
        }

    def turn(self, client, headers, *, version=0, query="氣候2024 CSV"):
        return client.post(
            "/api/turn",
            json={"query": query, "version": version},
            headers=headers,
        )

    def test_notice_policy_precedes_choice_and_requires_new_session(self):
        client = self.start()
        policy = client.get("/api/access", headers=self.headers).json()
        self.assertEqual(
            policy["trace_policy"],
            {
                "available": True,
                "notice_version": NOTICE_VERSION,
                "retention_days": 7.0,
            },
        )
        self.assertEqual(self.sessions.items, {})
        for choice in (
            {"capture": True},
            {"capture": 1},
            {"capture": True, "notice_version": "old"},
        ):
            response = client.post(
                "/api/session", json=choice, headers=self.headers
            )
            self.assertEqual(response.status_code, 422)
        default, headers = self.session(client, capture=False)
        self.turn(client, headers, query="unconsented fixture")
        self.assertEqual(self.sink.events, [])
        fresh, _ = self.session(client)
        self.assertNotEqual(default["token"], fresh["token"])
        self.assertNotEqual(fresh["token"], fresh["trace"]["test_session_id"])
        self.assertEqual(self.sessions.get(fresh["token"]).state.topic, "")
        self.assertIsNone(default["trace"]["test_session_id"])

    def test_status_read_is_payload_free_and_keeps_owner_and_origin_checks(
        self,
    ):
        client = self.start()
        _, headers = self.session(client)
        self.turn(client, headers)
        response = client.get(
            "/api/session",
            headers={"Authorization": headers["Authorization"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"trace"})
        self.assertNotIn("氣候", response.text)
        self.assertEqual(client.get("/api/session").status_code, 401)
        self.assertEqual(
            client.get(
                "/api/session",
                headers={**headers, "Origin": "https://evil.example"},
            ).status_code,
            403,
        )
        self.assertEqual(
            client.request(
                "GET", "/api/session", content=b"x", headers=headers
            ).status_code,
            400,
        )

    def test_disabled_operator_configuration_does_not_admit_optin(self):
        client = self.start(settings=SETTINGS)
        response = client.post(
            "/api/session",
            json={"capture": True, "notice_version": NOTICE_VERSION},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.sessions.items, {})
        self.assertEqual(self.sink.events, [])

    def test_two_legacy_turns_preserve_actual_whitelisted_evidence(self):
        client = self.start(EngineUpstream())
        value, headers = self.session(client)
        self.assertEqual(self.turn(client, headers).status_code, 200)
        self.assertEqual(
            self.turn(
                client, headers, version=1, query="改2023年"
            ).status_code,
            200,
        )
        first, second = [validate_event(value) for value in self.sink.events]
        self.assertEqual(first["state_before"]["topic"], "")
        self.assertEqual(first["state_after"]["time"]["start"], "2024")
        self.assertEqual(second["state_before"], first["state_after"])
        self.assertEqual(second["state_after"]["time"]["start"], "2023")
        self.assertEqual(second["state_after"]["formats"], ["CSV"])
        self.assertEqual(second["input"]["query"], "改2023年")
        self.assertEqual(second["execution"]["kind"], "legacy_conditions")
        search = second["output"]["search"]
        self.assertEqual(search["cards"][0]["rank"], 1)
        self.assertIn("unknown_condition_results_retained", search["warnings"])
        self.assertEqual(second["upstream_request_id"], "upstream-2")
        self.assertEqual(
            second["identities"]["catalog_manifest_sha256"], "b" * 64
        )
        self.assertIsNone(second["identities"]["model_identity"])
        self.assertNotIn("url", search["cards"][0])
        self.assertNotIn("snippet", search["cards"][0])
        self.sessions.get(value["token"]).state.formats.clear()
        self.assertEqual(first["state_after"]["formats"], ["CSV"])
        self.assertTrue(second["capture"]["complete"])

    def test_real_writer_round_trip_keeps_default_payload_absent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "traces"
            client = ASGITestClient(
                create_app(
                    replace(self.settings, trace_root=root),
                    EngineUpstream(),
                    self.sessions,
                )
            )
            self.addCleanup(client.close)
            _, default_headers = self.session(client, capture=False)
            self.turn(client, default_headers, query="unconsented sentinel")
            client.close()
            client = ASGITestClient(
                create_app(
                    replace(self.settings, trace_root=root),
                    EngineUpstream(),
                    self.sessions,
                )
            )
            self.addCleanup(client.close)
            value, headers = self.session(client)
            self.turn(client, headers)
            self.turn(client, headers, version=1, query="改2023年")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status = client.get("/api/session", headers=headers).json()[
                    "trace"
                ]
                if status["capture_status"] == "captured":
                    break
                time.sleep(0.01)
            self.assertEqual(status["capture_status"], "captured")
            identifier = value["trace"]["test_session_id"]
            self.assertFalse(TraceStore(root).export(identifier)["complete"])
            client.close()
            exported = TraceStore(root).export(identifier)
            self.assertTrue(exported["complete"])
            self.assertEqual(len(exported["events"]), 2)
            self.assertNotIn("unconsented sentinel", json.dumps(exported))
            self.assertEqual(
                {name.name for name in root.iterdir()},
                {".trace.lock", identifier},
            )

    def test_unavailable_attempts_do_not_advance_committed_version(self):
        client = self.start()
        value, headers = self.session(client)
        self.upstream.error = UpstreamError()
        self.assertEqual(self.turn(client, headers).status_code, 503)
        self.upstream.error = None
        self.upstream.status = "unavailable"
        self.assertEqual(self.turn(client, headers).status_code, 503)
        self.upstream.status = "ok"
        self.assertEqual(self.turn(client, headers).status_code, 200)
        self.assertEqual(
            [event["turn"] for event in self.sink.events], [1, 2, 3]
        )
        self.assertEqual(
            [event["output"]["version"] for event in self.sink.events],
            [0, 0, 1],
        )
        self.assertEqual(self.sessions.get(value["token"]).version, 1)
        self.assertFalse(self.sink.events[0]["capture"]["complete"])
        self.assertEqual(
            self.sink.events[1]["capture"]["gaps"], ["result_unavailable"]
        )

    def test_clarification_and_rejection_keep_before_after_pending(self):
        client = self.start()
        _, headers = self.session(client)
        self.upstream.status = "clarification_required"
        self.turn(client, headers, query="最近水質")
        self.upstream.status = "rejected"
        self.turn(client, headers, version=1, query="無關問題")
        first, second = self.sink.events
        self.assertIsNone(first["pending_before"])
        self.assertEqual(first["pending_after"], "最近水質")
        self.assertEqual(second["pending_before"], second["pending_after"])
        self.assertEqual(second["state_before"], second["state_after"])

    def test_sink_and_projection_failure_preserve_search_and_mark_gaps(self):
        client = self.start()
        value, headers = self.session(client)
        self.sink.failure = OSError("private sentinel")
        reply = self.turn(client, headers)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(
            reply.json()["trace"]["capture_status"], "unavailable"
        )
        self.assertEqual(self.sessions.get(value["token"]).version, 1)
        self.assertEqual(self.sink.gaps[-1][-1], "unavailable")
        with patch(
            "search.web.trace.SearchTrace.begin", side_effect=ValueError
        ):
            reply = self.turn(client, headers, version=1)
        self.assertEqual(reply.status_code, 200)
        self.assertFalse(self.sessions.get(value["token"]).busy)
        self.assertEqual(self.sessions.get(value["token"]).version, 2)
        self.assertEqual(self.sink.gaps[-1][-1], "projection_invalid")
        self.assertNotIn("private sentinel", reply.text)

    def test_withdraw_fences_inflight_upstream_without_waiting(self):
        upstream = GatedUpstream()
        client = self.start(upstream)
        value, headers = self.session(client)
        replies = []
        thread = threading.Thread(
            target=lambda: replies.append(self.turn(client, headers))
        )
        thread.start()
        self.addCleanup(upstream.release.set)
        self.assertTrue(upstream.started.wait(2))
        try:
            response = client.request(
                "PATCH",
                "/api/session",
                json={"capture": False},
                headers=headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["trace"]["enabled"])
            self.assertEqual(
                response.json()["trace"]["deletion_status"], "complete"
            )
        finally:
            upstream.release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(replies[0].status_code, 200)
        self.assertFalse(replies[0].json()["trace"]["enabled"])
        self.assertEqual(self.sink.events, [])
        self.assertEqual(self.sessions.get(value["token"]).version, 1)
        self.turn(client, headers, version=1)
        self.assertEqual(self.sink.events, [])

    def test_withdraw_and_reset_report_pending_without_claiming_deleted(self):
        client = self.start()
        _, headers = self.session(client)
        self.turn(client, headers)
        self.sink.deletion = "pending"
        response = client.request(
            "PATCH", "/api/session", json={"capture": False}, headers=headers
        )
        self.assertEqual(
            response.json()["trace"]["deletion_status"], "pending"
        )
        for body in ({"capture": True}, {"capture": 0}, {}):
            response = client.request(
                "PATCH", "/api/session", json=body, headers=headers
            )
            self.assertEqual(response.status_code, 422)
        response = client.delete("/api/session", headers=headers)
        self.assertEqual(
            response.json()["trace"]["deletion_status"], "pending"
        )
        self.assertEqual(self.sessions.items, {})

    def test_owner_origin_and_token_cannot_be_replaced_by_test_identifier(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "key"
            path.write_text("d" * 64)
            path.chmod(0o600)
            client = self.start(
                settings=replace(self.settings, edge_secret_file=path)
            )
            value, headers = self.session(client, headers=public_headers())
            for change, status in (
                ({"Origin": "https://evil.example"}, 403),
                ({"X-Depositar-Subject": "b" * 64}, 410),
                (
                    {
                        "Authorization": "Bearer "
                        + value["trace"]["test_session_id"]
                    },
                    401,
                ),
            ):
                response = client.request(
                    "PATCH",
                    "/api/session",
                    json={"capture": False},
                    headers={**headers, **change},
                )
                self.assertEqual(response.status_code, status)
            self.assertTrue(
                self.sessions.get(
                    value["token"], "user:" + "a" * 64
                ).capture_enabled
            )
            self.assertEqual(self.sink.revoked, {})

    def test_development_labels_are_unavailable_source_identities(self):
        class UnboundUpstream(FakeUpstream):
            async def turn(self, payload):
                result = await super().turn(payload)
                result.revision = "development"
                return result

        client = self.start(
            UnboundUpstream(), replace(self.settings, revision="development")
        )
        _, headers = self.session(client)
        self.assertEqual(self.turn(client, headers).status_code, 200)
        event = self.sink.events[0]
        self.assertIsNone(event["identities"]["bff_revision"])
        self.assertIsNone(event["identities"]["backend_revision"])
        self.assertIn("bff_identity", event["unavailable_trace"])
        self.assertIn("backend_identity", event["unavailable_trace"])

    def test_credentials_and_unlisted_fields_never_enter_event(self):
        client = self.start(EngineUpstream())
        value, headers = self.session(client)
        headers.update(Cookie="cookie-sentinel", X_Config="config-sentinel")
        self.turn(client, headers)
        raw = json.dumps(self.sink.events)
        for sentinel in (
            value["token"],
            "cookie-sentinel",
            "config-sentinel",
            "/unused-private",
            "/unused",
        ):
            self.assertNotIn(sentinel, raw)
        self.assertNotIn("authorization", raw.lower())
        self.assertNotIn("resource_context", raw)
        self.assertNotIn("rank_fusion_score", raw)

    def test_public_owner_manages_inflight_synthetic_capture(self):
        # This injected private sink is synthetic; public profiles stay off.
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "edge-key"
            key.write_text("d" * 64)
            key.chmod(0o600)
            upstream = GatedUpstream()
            client = self.start(
                upstream,
                replace(self.settings, edge_secret_file=key),
            )
            value, headers = self.session(client, headers=public_headers())
            session = self.sessions.get(value["token"], "user:" + "a" * 64)
            before = (
                session.version,
                session.state.model_dump(),
                session.pending,
            )
            replies = []
            thread = threading.Thread(
                target=lambda: replies.append(self.turn(client, headers))
            )
            thread.start()
            try:
                self.assertTrue(upstream.started.wait(2))
                read_headers = {
                    name: item
                    for name, item in headers.items()
                    if name != "Origin"
                }
                read = client.get("/api/session", headers=read_headers)
                self.assertEqual(read.status_code, 200)
                self.assertEqual(set(read.json()), {"trace"})
                self.assertTrue(read.json()["trace"]["enabled"])
                other = {**headers, "X-Depositar-Subject": "b" * 64}
                self.assertEqual(
                    client.get("/api/session", headers=other).status_code, 410
                )
                denied = client.request(
                    "PATCH",
                    "/api/session",
                    json={"capture": False},
                    headers=other,
                )
                self.assertEqual(denied.status_code, 410)
                self.assertEqual(session.trace_epoch, 0)
                self.assertEqual(self.sink.revoked, {})
                withdrawn = client.request(
                    "PATCH",
                    "/api/session",
                    json={"capture": False},
                    headers=headers,
                )
                self.assertEqual(withdrawn.status_code, 200)
                self.assertFalse(withdrawn.json()["trace"]["enabled"])
                self.assertEqual(session.trace_epoch, 1)
                self.assertTrue(session.busy)
                self.assertEqual(
                    (
                        session.version,
                        session.state.model_dump(),
                        session.pending,
                    ),
                    before,
                )
                self.assertEqual(upstream.calls, [])
                stopped = client.get("/api/session", headers=read_headers)
                self.assertFalse(stopped.json()["trace"]["enabled"])
            finally:
                upstream.release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(replies[0].status_code, 200)
            self.assertFalse(replies[0].json()["trace"]["enabled"])
            self.assertEqual(self.sink.events, [])
            self.assertEqual(session.version, 1)
            self.assertFalse(session.busy)
            self.assertEqual(len(upstream.calls), 1)


class TraceSettingsTests(unittest.TestCase):
    def test_limits_and_root_reject_unbounded_or_unsafe_values(self):
        for values in (
            {"queue_items": 0},
            {"shutdown_seconds": float("inf")},
            {"max_turns": 101},
            {"queue_bytes": True},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                TraceLimits(**values)
        for root in (Path("relative"), Path("/"), Path("/a/../b")):
            with self.subTest(root=root), self.assertRaises(ValueError):
                replace(SETTINGS, trace_root=root)
        with patch.dict(
            "os.environ",
            {
                "WEB_ORIGIN": ORIGIN,
                "WEB_UPSTREAM": SETTINGS.upstream,
                "WEB_TRACE_ROOT": "/private/traces",
                "WEB_TRACE_QUEUE_ITEMS": "2",
            },
            clear=True,
        ):
            settings = WebSettings.from_environment()
        self.assertEqual(settings.trace_limits.queue_items, 2)
        self.assertEqual(settings.trace_root, Path("/private/traces"))
