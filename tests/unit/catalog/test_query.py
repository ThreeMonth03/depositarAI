"""Private query preparation with real projection/access and fake
Docker/OpenSearch."""

from __future__ import annotations

import base64
from contextlib import contextmanager, nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from search.operations.catalog import (
    access,
    connection,
    execution,
    projection,
    query,
    reuse,
    transport,
)
from search.operations import lifecycle
from search.operations.shared import private_io
from tests.unit.catalog.test_access import AccessClient
from tests.unit.catalog.test_projection import (
    INDEX,
    ProjectionClient,
    make_bundle,
)
from tests.unit.catalog.test_vectors import host_context
from tests.unit.catalog.test_transport import Response
from tests.unit.catalog.test_reuse import PROFILE

READ_MATERIALS = connection.materials
EXPORT_CACHE = reuse.export


class QueryClient:
    def __init__(self, bundle):
        self.projection = ProjectionClient(bundle)
        self.access = AccessClient(len(bundle.documents))
        self.after_access = None
        self.before_write = None

    def request(self, method, path, payload=None, **kwargs):
        if (
            kwargs
            and not kwargs.get("ndjson")
            or path.startswith(access.SECURITY_PATH)
        ):
            if self.before_write and method == "PUT" and not kwargs:
                self.before_write()
            result = self.access.request(method, path, payload, **kwargs)
            if (
                self.after_access
                and method == "PUT"
                and path == "/" + INDEX + "/_mapping"
            ):
                hook, self.after_access = self.after_access, None
                hook(result)
            return result
        return self.projection.request(method, path, payload, **kwargs)


class CatalogQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle_temp = tempfile.TemporaryDirectory(
            prefix="depositar-query-bundle-"
        )
        cls.bundle = make_bundle(Path(cls.bundle_temp.name), count=2)

    @classmethod
    def tearDownClass(cls):
        cls.bundle_temp.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-query-host-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = host_context(
            self.root, self.bundle, self.bundle.manifest_sha256, 2
        )
        self.context.update(repo=self.root, runner=None)
        self.client = QueryClient(self.bundle)
        self.scan = projection.verify_vectors(self.client, self.bundle)
        self.runtime = {
            **execution.catalog_identity(self.context),
            "container_ids": {
                name: digit * 64
                for name, digit in (
                    ("fess-search", "1"),
                    ("opensearch-search", "2"),
                    ("ollama-embedding", "3"),
                )
            },
            "network_id": "4" * 64,
        }
        self.gate = {
            **execution.catalog_identity(self.context),
            "status": "vectors-complete",
            "runtime_sha256": execution.digest_record(self.runtime),
            "recipe_sha256": "5" * 64,
            "after_state_sha256": self.scan["after_state_sha256"],
        }
        name = self.context["plan"]["project"] + "_search_backend"
        self.proof = {
            key: self.runtime[key] for key in ("container_ids", "network_id")
        }
        self.containers, peers = [], {}
        for role, literal in (
            ("fess-search", "172.19.0.2"),
            ("opensearch-search", "172.19.0.3"),
        ):
            cid = self.runtime["container_ids"][role]
            self.containers.append(
                {
                    "Id": cid,
                    "NetworkSettings": {
                        "Networks": {
                            name: {
                                "IPAddress": literal,
                                "NetworkID": self.runtime["network_id"],
                            }
                        }
                    },
                }
            )
            peers[cid] = {"IPv4Address": literal + "/16"}
        self.network = {"Containers": peers}
        self.addCleanup(patch.stopall)
        self.runtime_mock = patch.object(
            execution, "require_runtime", return_value=(self.runtime, {})
        ).start()
        patch.object(
            execution, "inspect_runtime", side_effect=self.snapshot
        ).start()
        self.gate_mock = patch.object(
            query.vectors, "verify_complete", return_value=self.gate
        ).start()
        self.source_mock = patch.object(
            query,
            "require_clean_revision",
            return_value=self.context["plan"]["revision"],
        ).start()
        self.materials_mock = patch.object(
            connection, "materials", return_value=("a" * 64, b"sealed-ca")
        ).start()
        self.cache_mock = patch.object(
            reuse,
            "export",
            return_value={"status": "exported", "cached_count": 2},
        ).start()
        self.factory_calls = []

    def snapshot(self, context, images, *, include_snapshot):
        self.assertIs(include_snapshot, True)
        return self.proof, self.containers, self.network

    def os_factory(self, peer, ca, admin):
        self.factory_calls.append(("index", peer, ca, admin))
        return self.client

    def runs(self):
        root = self.root / "query-operations"
        return sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda p: p.stat().st_mtime_ns,
        )

    def provision(self):
        return query._provision(self.context, self.os_factory)

    def replace_record(self, path, value):
        fd = private_io.open_directory(path.parent)
        try:
            private_io.write_json(
                fd, path.name, value, replace=True, max_bytes=32 * 1024
            )
        finally:
            os.close(fd)

    def test_real_projection_access_and_durable_intent_without_website(self):
        intents = []

        def require_intent():
            runs = self.runs()
            self.assertEqual(1, len(runs))
            self.assertEqual(
                {"started.json"}, {p.name for p in runs[0].iterdir()}
            )
            started = json.loads((runs[0] / "started.json").read_text())
            self.assertEqual(runs[0].name, started["run_id"])
            intents.append(started)

        self.client.before_write = require_intent
        report = self.provision()
        self.assertEqual("query-ready", report["status"])
        self.assertIs(report["active_changed"], False)
        self.assertIs(report["website_started"], False)
        self.assertIs(report["runtime_verified"], True)
        self.assertEqual(INDEX, report["index"])
        self.assertEqual(2, report["document_count"])
        self.assertEqual(
            [("index", "172.19.0.3", b"sealed-ca", "a" * 64)] * 2,
            self.factory_calls,
        )
        root = self.root / "query-access"
        self.assertEqual(query.QUERY_FILES, {p.name for p in root.iterdir()})
        for path in (*root.iterdir(), *self.runs()[0].iterdir()):
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
        encoded = (
            json.dumps(report) + (self.runs()[0] / "complete.json").read_text()
        )
        self.assertNotIn(self.client.access.password, encoded)
        self.assertNotIn("a" * 64, encoded)
        self.assertNotIn("b" * 64, encoded)
        completed = json.loads((self.runs()[0] / "complete.json").read_text())
        self.assertEqual(6, len(completed["reports"]["access"]["probes"]))
        self.assertEqual(3, len(self.client.access.admin_writes()))
        self.assertEqual(3, len(intents))

    def test_completed_repeat_rechecks_without_rotation_or_index_writes(
        self,
    ):
        self.provision()
        root = self.root / "query-access"
        before = {
            name: ((root / name).stat().st_ino, (root / name).read_bytes())
            for name in query.QUERY_FILES - {"ready.json"}
        }
        self.client.projection.calls.clear()
        self.client.access.calls.clear()
        report = self.provision()
        self.assertEqual("query-ready", report["status"])
        self.assertEqual(2, len(self.runs()))
        self.assertEqual([], self.client.access.admin_writes())
        self.assertTrue(
            all(
                method == "GET" or path.endswith("/_search")
                for method, path, _, _ in self.client.projection.calls
            )
        )
        for name, state in before.items():
            self.assertEqual(
                state,
                ((root / name).stat().st_ino, (root / name).read_bytes()),
            )

    def test_cache_export_follows_query_completion_and_receives_verified_scan(
        self,
    ):
        def export(context, client, scan, ready):
            self.assertIs(context, self.context)
            self.assertIs(client, self.client)
            self.assertEqual(self.scan, scan)
            saved = execution.read_record(
                self.root / "query-access", "ready.json"
            )
            self.assertEqual(saved, ready)
            completed = execution.read_record(
                self.root / "query-operations" / ready["run_id"],
                "complete.json",
            )
            self.assertEqual(
                ready["complete_sha256"], execution.digest_record(completed)
            )
            return {"status": "exported", "cached_count": 2}

        self.cache_mock.side_effect = export
        report = self.provision()
        self.assertEqual(
            {"status": "exported", "cached_count": 2}, report["vector_cache"]
        )
        self.cache_mock.assert_called_once()

    def test_failed_export_can_retry_from_completed_query_evidence(
        self,
    ):
        self.cache_mock.side_effect = ValueError("cache write interrupted")
        with self.assertRaisesRegex(ValueError, "cache write interrupted"):
            self.provision()
        first = execution.read_record(self.root / "query-access", "ready.json")
        self.assertTrue(
            (
                self.root
                / "query-operations"
                / first["run_id"]
                / "complete.json"
            ).exists()
        )
        self.client.access.calls.clear()
        self.cache_mock.side_effect = None
        report = self.provision()
        self.assertEqual("query-ready", report["status"])
        self.assertEqual([], self.client.access.admin_writes())
        self.assertEqual(2, self.cache_mock.call_count)

    def test_interrupted_access_never_publishes_ready_or_auto_retries(self):
        self.client.after_access = lambda _: (_ for _ in ()).throw(
            ValueError("controlled-failure")
        )
        with self.assertRaisesRegex(access.AccessError, "transport_failed"):
            self.provision()
        self.assertIsNone(self.client.after_access)
        self.assertFalse((self.root / "query-access/ready.json").exists())
        self.assertFalse((self.runs()[0] / "complete.json").exists())
        self.client.after_access = None
        with self.assertRaisesRegex(query.RuntimeError, "recovery_required"):
            self.provision()
        self.assertEqual(1, len(self.runs()))

    def test_checkpoint_gate_blocks_before_credentials_or_query_files(self):
        self.gate_mock.side_effect = query.RuntimeError(
            "catalog_vector_completion_evidence_missing"
        )
        with self.assertRaisesRegex(
            query.RuntimeError, "completion_evidence_missing"
        ):
            self.provision()
        self.materials_mock.assert_not_called()
        self.assertEqual([], list(self.root.iterdir()))

    def test_partition_digest_mismatch_blocks_before_any_query_mutation(self):
        self.gate["after_state_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            query.RuntimeError, "vector_evidence_changed"
        ):
            self.provision()
        self.assertEqual([], list(self.root.iterdir()))
        self.assertEqual([], self.client.access.admin_writes())

    def test_invalid_network_or_peer_blocks_before_credentials(
        self,
    ):
        for field, value in (
            ("IPAddress", "8.8.8.8"),
            ("NetworkID", "8" * 64),
            ("IPAddress", "172.19.0.9"),
        ):
            with self.subTest(field=field):
                snapshot = copy.deepcopy(self.containers)
                attachment = next(
                    iter(
                        self.containers[0]["NetworkSettings"][
                            "Networks"
                        ].values()
                    )
                )
                attachment[field] = value
                with self.assertRaisesRegex(
                    query.RuntimeError, "peer_changed"
                ):
                    self.provision()
                self.materials_mock.assert_not_called()
                self.containers = snapshot
        self.assertEqual([], list(self.root.iterdir()))

    def test_changed_runtime_proof_blocks_before_credentials(self):
        self.proof = {**self.proof, "network_id": "0" * 64}
        with self.assertRaisesRegex(query.RuntimeError, "runtime_changed"):
            self.provision()
        self.materials_mock.assert_not_called()

    def test_invalid_query_access_report_never_publishes_ready(self):
        original = access.provision_access

        def invalid(*args):
            return {**original(*args), "created": 1}

        with patch.object(access, "provision_access", side_effect=invalid):
            with self.assertRaisesRegex(query.RuntimeError, "report_invalid"):
                self.provision()
        self.assertFalse((self.root / "query-access/ready.json").exists())

    def test_changed_vector_bytes_after_accounts_fail_closed(self):
        from search.planning import index_filters

        self.client.after_access = lambda _: self.client.projection.rows[0][
            "_source"
        ][index_filters.VECTOR_PATH][0]["vector"].__setitem__(0, 2.0)
        with self.assertRaisesRegex(
            query.RuntimeError, "permissions_or_projection_changed"
        ):
            self.provision()
        self.assertFalse((self.root / "query-access/ready.json").exists())

    def test_secret_replacement_after_probes_prevents_completion(
        self,
    ):
        def replace_secret(_):
            fd = private_io.open_directory(self.root / "query-access")
            try:
                private_io.write_file(
                    fd,
                    access.SECRET_NAME,
                    b"9" * 64 + b"\n",
                    replace=True,
                    max_bytes=65,
                )
            finally:
                os.close(fd)

        self.client.after_access = replace_secret
        with self.assertRaisesRegex(access.AccessError, "secret_changed"):
            self.provision()
        self.assertFalse((self.runs()[0] / "complete.json").exists())

    def test_started_receipt_replacement_after_query_probes_fails_closed(self):
        def replace_started(_):
            path = self.runs()[-1] / "started.json"
            value = json.loads(path.read_text())
            value["run_id"] = "9" * 32
            self.replace_record(path, value)

        self.client.after_access = replace_started
        with self.assertRaisesRegex(query.RuntimeError, "receipt_changed"):
            self.provision()
        self.assertFalse((self.root / "query-access/ready.json").exists())

    def test_same_shape_ready_receipt_tampering_is_not_authority(self):
        self.provision()
        path = self.root / "query-access/ready.json"
        value = json.loads(path.read_text())
        value["complete_sha256"] = "0" * 64
        self.replace_record(path, value)
        with self.assertRaisesRegex(query.RuntimeError, "receipt_changed"):
            self.provision()
        self.assertEqual(1, len(self.runs()))

    def test_history_report_integer_instead_of_bool_is_rejected(self):
        self.provision()
        path = self.runs()[0] / "complete.json"
        value = json.loads(path.read_text())
        value["reports"]["access"]["created"] = 1
        self.replace_record(path, value)
        with self.assertRaisesRegex(query.RuntimeError, "report_invalid"):
            self.provision()
        self.assertEqual(1, len(self.runs()))

    def test_symlink_or_extra_query_file_not_adopted(self):
        self.provision()
        (self.root / "query-access/unexpected").touch()
        with self.assertRaisesRegex(query.RuntimeError, "inventory_changed"):
            self.provision()
        self.assertEqual(1, len(self.runs()))

    def test_legacy_fess_query_secret_is_rejected_without_mutation(self):
        self.prepare_authority()
        legacy = self.root / "query-access/fess-query-password"
        legacy.write_text("7" * 64 + "\n")
        legacy.chmod(0o600)
        before = self.authority_snapshot()
        for action in (
            self.provision,
            lambda: query.read_authority(self.context),
        ):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(
                    query.RuntimeError, "inventory_changed"
                ),
            ):
                action()
            self.assertEqual(before, self.authority_snapshot())
        self.assertEqual([], self.client.access.admin_writes())

    def test_legacy_dual_actor_identity_is_not_automatically_adopted(self):
        self.prepare_authority()
        for path in (
            self.root / "query-operations/identity.json",
            self.root / "query-access/ready.json",
        ):
            record = json.loads(path.read_text())
            identity = record["identity"] if "identity" in record else record
            identity["kind"] = "private-catalog-query"
            self.replace_record(path, record)
        before = self.authority_snapshot()
        for action in (
            self.provision,
            lambda: query.read_authority(self.context),
        ):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(query.RuntimeError, "identity_changed"),
            ):
                action()
            self.assertEqual(before, self.authority_snapshot())
        self.assertEqual([], self.client.access.admin_writes())

    def test_legacy_fess_report_is_rejected_even_with_updated_receipt_digest(
        self,
    ):
        self.prepare_authority()
        complete_path = self.runs()[0] / "complete.json"
        complete = json.loads(complete_path.read_text())
        complete["reports"]["fess"] = {"created": True}
        self.replace_record(complete_path, complete)
        ready_path = self.root / "query-access/ready.json"
        ready = json.loads(ready_path.read_text())
        ready["complete_sha256"] = execution.digest_record(complete)
        self.replace_record(ready_path, ready)
        before = self.authority_snapshot()
        for action in (
            self.provision,
            lambda: query.read_authority(self.context),
        ):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(query.RuntimeError, "report_invalid"),
            ):
                action()
            self.assertEqual(before, self.authority_snapshot())
        self.assertEqual([], self.client.access.admin_writes())

    def test_existing_remote_query_user_missing_is_not_silently_recreated(
        self,
    ):
        self.provision()
        self.client.access.values.pop("internalusers")
        writes = list(self.client.access.admin_writes())
        with self.assertRaisesRegex(access.AccessError, "permissions_changed"):
            self.provision()
        self.assertEqual(writes, self.client.access.admin_writes())
        self.assertFalse((self.runs()[-1] / "complete.json").exists())

    def test_denial_probe_failure_never_claims_query_access_ready(self):
        self.client.access.probe_status[
            ("PUT", "/" + INDEX + "/_doc/authorization-probe")
        ] = 200
        with self.assertRaisesRegex(
            access.AccessError, "privilege_probe_failed"
        ):
            self.provision()
        self.assertFalse((self.root / "query-access/ready.json").exists())

    def test_projection_removed_during_query_probes_never_publishes_ready(
        self,
    ):
        from search.planning import index_filters

        def remove_projection(_):
            self.client.projection.mapping[INDEX]["mappings"][
                "properties"
            ].pop(index_filters.FIELD)
            for row in self.client.projection.rows:
                row["_source"].pop(index_filters.FIELD)

        self.client.after_access = remove_projection
        with self.assertRaisesRegex(
            projection.ProjectionError, "mapping_changed"
        ):
            self.provision()
        self.assertFalse((self.root / "query-access/ready.json").exists())
        self.assertFalse((self.runs()[0] / "complete.json").exists())

    def test_role_escalation_during_query_probes_never_publishes_ready(self):
        self.client.after_access = lambda _: self.client.access.values[
            "roles"
        ].__setitem__("cluster_permissions", ["cluster_all"])
        with self.assertRaisesRegex(access.AccessError, "permissions_changed"):
            self.provision()
        self.assertFalse((self.root / "query-access/ready.json").exists())
        self.assertFalse((self.runs()[0] / "complete.json").exists())

    def test_history_limit_rejects_new_run_but_accepts_final_completion(
        self,
    ):
        with patch.object(query, "MAX_RUNS", 1):
            self.assertEqual("query-ready", self.provision()["status"])
            with self.assertRaisesRegex(
                query.RuntimeError, "history_invalid_or_full"
            ):
                self.provision()
        self.assertEqual(1, len(self.runs()))

    def test_final_privilege_probe_cannot_replace_query_secret_before_ready(
        self,
    ):
        verify = access.verify_access

        def replace_after_probe(*args):
            report = verify(*args)
            fd = private_io.open_directory(self.root / "query-access")
            try:
                private_io.write_file(
                    fd,
                    access.SECRET_NAME,
                    b"9" * 64 + b"\n",
                    replace=True,
                    max_bytes=65,
                )
            finally:
                os.close(fd)
            return report

        with (
            patch.object(
                access, "verify_access", side_effect=replace_after_probe
            ),
            self.assertRaisesRegex(query.RuntimeError, "secret_changed"),
        ):
            self.provision()
        self.assertFalse((self.root / "query-access/ready.json").exists())
        self.assertFalse((self.runs()[0] / "complete.json").exists())

    def test_material_reader_uses_bounded_private_files_and_never_writes(self):
        secret_root, ca_root = (
            self.root / "secrets",
            self.root / "opensearch/config",
        )
        secret_root.mkdir(mode=0o700)
        ca_root.parent.mkdir(mode=0o700)
        ca_root.mkdir(mode=0o700)
        fd = private_io.open_directory(secret_root)
        try:
            for name, digit in (
                ("opensearch_admin_password", "6"),
                ("fess_initial_admin_password", "7"),
            ):
                private_io.write_file(
                    fd, name, (digit * 64 + "\n").encode("ascii"), max_bytes=65
                )
        finally:
            os.close(fd)
        (secret_root / "fess_initial_admin_password").chmod(0o644)
        fd = private_io.open_directory(ca_root)
        try:
            private_io.write_file(
                fd, "root-ca.pem", b"test-ca-bytes\n", max_bytes=64 * 1024
            )
            os.chmod("root-ca.pem", 0o400, dir_fd=fd, follow_symlinks=False)
        finally:
            os.close(fd)
        files = [*secret_root.iterdir(), *ca_root.iterdir()]
        before = {
            path: (path.stat().st_ino, path.read_bytes()) for path in files
        }
        self.assertEqual(
            ("6" * 64, b"test-ca-bytes\n"), READ_MATERIALS(self.context)
        )
        self.assertEqual(
            before,
            {path: (path.stat().st_ino, path.read_bytes()) for path in files},
        )
        (secret_root / "opensearch_admin_password").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "file_unsafe"):
            READ_MATERIALS(self.context)

    def test_public_facade_loads_sealed_context_and_holds_catalog_lock(self):
        events = []

        @contextmanager
        def lock(context):
            self.assertIs(context, self.context)
            events.append("lock")
            yield
            events.append("unlock")

        with (
            patch.object(
                execution, "load_context", return_value=self.context
            ) as load,
            patch.object(execution, "operation_lock", side_effect=lock),
        ):
            report = query.provision(
                "config",
                "plan",
                repo_root=self.root,
                opensearch_factory=self.os_factory,
            )
        load.assert_called_once_with(
            "config", "plan", repo_root=self.root, runner=None
        )
        self.assertEqual(["lock", "unlock"], events)
        self.assertEqual("query-ready", report["status"])

    def prepare_authority(self):
        self.provision()
        patch.object(
            query.vectors,
            "read_complete_evidence",
            return_value=(self.gate, self.runtime, {}),
        ).start()
        self.client.projection.calls.clear()
        self.client.access.calls.clear()
        self.runtime_mock.reset_mock()

    def authority_snapshot(self):
        return {
            str(path.relative_to(self.root)): (
                path.stat().st_ino,
                path.read_bytes(),
            )
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def verify_authority(self, observer=None):
        return query.verify_authority(
            self.context,
            observe_runtime=observer
            or (lambda _: (self.proof, self.containers, self.network)),
            opensearch_factory=self.os_factory,
        )

    def test_read_authority_rechecks_sealed_receipts_without_writes_or_http(
        self,
    ):
        self.prepare_authority()
        before = self.authority_snapshot()
        with (
            patch.object(
                connection,
                "materials",
                side_effect=AssertionError("admin HTTP material"),
            ),
            patch.object(query, "_write", side_effect=AssertionError("write")),
            patch.object(
                private_io,
                "write_file",
                side_effect=AssertionError("secret write"),
            ),
        ):
            report = query.read_authority(self.context)
        self.assertEqual(before, self.authority_snapshot())
        self.assertEqual([], self.client.projection.calls)
        self.assertEqual([], self.client.access.calls)
        self.runtime_mock.assert_not_called()
        self.assertEqual(INDEX, report["catalog_identity"]["physical_index"])
        self.assertEqual(self.runtime, report["runtime"])
        self.assertEqual(
            {access.SECRET_NAME, access.BINDING_NAME},
            set(report["credential_sha256"]),
        )
        self.assertNotIn(self.client.access.password, json.dumps(report))
        complete = json.loads((self.runs()[0] / "complete.json").read_text())
        self.assertEqual(
            execution.digest_record(
                {
                    **complete["reports"]["projection"],
                    "projection_updated_count": 0,
                }
            ),
            report["catalog_identity"]["projection_sha256"],
        )
        self.assertEqual(
            execution.digest_record(
                {**complete["reports"]["access"], "created": False}
            ),
            report["catalog_identity"]["access_sha256"],
        )

    def reuse_current_source(self):
        self.context["source_revision"] = "c" * 40
        self.source_mock.return_value = self.context["source_revision"]

    def test_cross_commit_authority_adds_origin_without_rewriting_old_evidence(
        self,
    ):
        self.prepare_authority()
        baseline = query.read_authority(self.context)
        self.assertNotIn("catalog_revision", baseline["catalog_identity"])
        before = self.authority_snapshot()
        self.reuse_current_source()
        reused = query.read_authority(self.context)
        self.assertEqual(
            {
                **baseline["catalog_identity"],
                "catalog_revision": self.context["plan"]["revision"],
            },
            reused["catalog_identity"],
        )
        self.assertEqual(baseline["runtime"], reused["runtime"])
        self.assertEqual(
            baseline["credential_sha256"], reused["credential_sha256"]
        )
        self.assertEqual(before, self.authority_snapshot())
        self.assertEqual([], self.client.projection.calls)
        self.assertEqual([], self.client.access.calls)

    def test_cross_commit_query_rechecks_without_rotation_or_import(
        self,
    ):
        self.prepare_authority()
        self.reuse_current_source()
        root = self.root / "query-access"
        credentials = {
            name: ((root / name).stat().st_ino, (root / name).read_bytes())
            for name in query.QUERY_FILES - {"ready.json"}
        }
        report = self.provision()
        self.assertEqual("query-ready", report["status"])
        self.assertEqual(self.context["plan"]["revision"], report["revision"])
        self.assertEqual(2, len(self.runs()))
        self.assertEqual([], self.client.access.admin_writes())
        self.assertTrue(
            all(
                method == "GET" or path.endswith("/_search")
                for method, path, _, _ in self.client.projection.calls
            )
        )
        for name, original in credentials.items():
            self.assertEqual(
                original,
                ((root / name).stat().st_ino, (root / name).read_bytes()),
            )
        self.assertEqual(
            self.context["plan"]["revision"],
            query.read_authority(self.context)["catalog_identity"][
                "catalog_revision"
            ],
        )

    def test_cross_commit_authority_checks_source_before_and_after_read(
        self,
    ):
        self.prepare_authority()
        self.reuse_current_source()
        current, origin = (
            self.context["source_revision"],
            self.context["plan"]["revision"],
        )
        before = self.authority_snapshot()
        for revisions in ([origin], [current, origin]):
            with self.subTest(revisions=revisions):
                self.source_mock.side_effect = revisions
                with self.assertRaisesRegex(
                    query.RuntimeError, "source_changed"
                ):
                    query.read_authority(self.context)
                self.assertEqual(before, self.authority_snapshot())

    def test_cross_commit_live_authority_still_rejects_acl_escalation(self):
        self.prepare_authority()
        self.reuse_current_source()
        self.client.access.probe_status[
            ("PUT", "/" + INDEX + "/_doc/authorization-probe")
        ] = 200
        before = self.authority_snapshot()
        with self.assertRaisesRegex(
            access.AccessError, "privilege_probe_failed"
        ):
            self.verify_authority()
        self.assertEqual(before, self.authority_snapshot())

    def test_cross_commit_live_authority_still_rejects_changed_vector_bytes(
        self,
    ):
        from search.planning import index_filters

        self.prepare_authority()
        self.reuse_current_source()
        self.client.projection.rows[0]["_source"][index_filters.VECTOR_PATH][
            0
        ]["vector"][0] = 2.0
        before = self.authority_snapshot()
        with self.assertRaisesRegex(
            query.RuntimeError, "vector_evidence_changed"
        ):
            self.verify_authority()
        self.assertEqual(before, self.authority_snapshot())

    def test_cross_commit_source_drift_retains_only_old_ready(
        self,
    ):
        self.prepare_authority()
        self.reuse_current_source()
        ready = self.root / "query-access" / "ready.json"
        before = ready.read_bytes()
        self.client.after_access = lambda _: setattr(
            self.source_mock, "return_value", self.context["plan"]["revision"]
        )
        with self.assertRaisesRegex(query.RuntimeError, "runtime_changed"):
            self.provision()
        self.assertEqual(before, ready.read_bytes())
        self.assertEqual(2, len(self.runs()))
        self.assertEqual(
            1, sum((run / "complete.json").exists() for run in self.runs())
        )

    def test_read_authority_never_creates_missing_evidence(self):
        patch.object(
            query.vectors,
            "read_complete_evidence",
            return_value=(self.gate, self.runtime, {}),
        ).start()
        with self.assertRaisesRegex(
            query.RuntimeError, "complete_evidence_missing"
        ):
            query.read_authority(self.context)
        self.assertEqual([], list(self.root.iterdir()))

    def test_read_authority_rejects_incomplete_history_instead_of_new_run(
        self,
    ):
        self.prepare_authority()
        pending = self.root / "query-operations" / ("0" * 32)
        pending.mkdir(mode=0o700)
        before = self.authority_snapshot()
        with self.assertRaisesRegex(query.RuntimeError, "recovery_required"):
            query.read_authority(self.context)
        self.assertEqual(before, self.authority_snapshot())

    def test_read_authority_rejects_changed_secret_and_complete_receipt(self):
        self.prepare_authority()
        secret = self.root / "query-access" / access.SECRET_NAME
        secret.write_bytes(b"9" * 64 + b"\n")
        with self.assertRaisesRegex(query.RuntimeError, "receipt_changed"):
            query.read_authority(self.context)

    def test_read_authority_rejects_ready_digest_and_projection_integer_bool(
        self,
    ):
        self.prepare_authority()
        ready_path = self.root / "query-access/ready.json"
        ready = json.loads(ready_path.read_text())
        self.replace_record(ready_path, {**ready, "complete_sha256": "0" * 64})
        with self.assertRaisesRegex(query.RuntimeError, "receipt_changed"):
            query.read_authority(self.context)
        self.replace_record(ready_path, ready)
        complete_path = self.runs()[0] / "complete.json"
        complete = json.loads(complete_path.read_text())
        complete["reports"]["projection"]["projection_verified"] = 1
        self.replace_record(complete_path, complete)
        with self.assertRaisesRegex(query.RuntimeError, "report_invalid"):
            query.read_authority(self.context)

    def test_read_authority_rejects_vector_gate_drift_and_dirty_source(self):
        self.prepare_authority()
        self.gate["after_state_sha256"] = "0" * 64
        with self.assertRaisesRegex(query.RuntimeError, "identity_changed"):
            query.read_authority(self.context)
        with patch.object(
            query, "require_clean_revision", return_value="f" * 40
        ):
            with self.assertRaisesRegex(query.RuntimeError, "source_changed"):
                query.read_authority(self.context)

    def test_live_authority_uses_host_snapshot_without_baseline_peer_bypass(
        self,
    ):
        self.prepare_authority()
        self.network["Containers"]["8" * 64] = {"IPv4Address": "172.19.0.8/16"}
        observer_calls = []

        def observer(context):
            self.assertIs(context, self.context)
            self.assertIn("8" * 64, self.network["Containers"])
            observer_calls.append(True)
            return self.proof, self.containers, self.network

        before = self.authority_snapshot()
        report = self.verify_authority(observer)
        self.assertEqual(2, len(observer_calls))
        self.assertEqual(before, self.authority_snapshot())
        self.assertEqual([], self.client.access.admin_writes())
        self.runtime_mock.assert_not_called()
        self.assertEqual(report, query.read_authority(self.context))
        self.materials_mock.assert_called_with(self.context)

    def test_live_authority_rejects_failed_denial_probe(self):
        self.prepare_authority()
        self.client.access.probe_status[
            ("PUT", "/" + INDEX + "/_doc/authorization-probe")
        ] = 200
        with self.assertRaisesRegex(
            access.AccessError, "privilege_probe_failed"
        ):
            self.verify_authority()

    def test_live_authority_rechecks_projection_after_query_probes(self):
        self.prepare_authority()

        def change_projection(_):
            from search.planning import index_filters

            self.client.projection.rows[0]["_source"][
                index_filters.VECTOR_PATH
            ][0]["vector"][0] = 2.0

        self.client.after_access = change_projection
        with self.assertRaisesRegex(
            query.RuntimeError, "permissions_or_projection_changed"
        ):
            self.verify_authority()

    def test_live_authority_rechecks_credentials_after_query_probes(self):
        self.prepare_authority()
        self.client.after_access = lambda _: (
            self.root / "query-access" / access.SECRET_NAME
        ).write_bytes(b"9" * 64 + b"\n")
        with self.assertRaisesRegex(
            ValueError, "secret_changed|receipt_changed"
        ):
            self.verify_authority()

    def test_live_authority_rechecks_host_observer_after_http(self):
        self.prepare_authority()
        changed = {**self.proof, "network_id": "0" * 64}
        observations = iter(
            (
                (self.proof, self.containers, self.network),
                (changed, self.containers, self.network),
            )
        )
        with self.assertRaisesRegex(query.RuntimeError, "runtime_changed"):
            self.verify_authority(lambda _: next(observations))

    def clocked_transport(self, tick):
        clients = []
        self.clock_inputs = []

        def factory(peer, ca, admin):
            self.clock_inputs.append((peer, ca, admin))
            client = transport.OpenSearchClient(
                peer, ca, admin, clock=lambda: tick[0]
            )
            clients.append(client)
            self.clock_trace.append(
                ("client", tick[0], client._started, client._deadline)
            )
            return client

        def connect(_peer, *, context, timeout):
            self.assertIsNotNone(context)
            self.assertTrue(0 < timeout <= 20)
            channel = Mock()

            def request(method, path, *, body, headers):
                encoded = headers["Authorization"].split(" ", 1)[1]
                user, password = (
                    base64.b64decode(encoded).decode().split(":", 1)
                )
                kwargs = (
                    {}
                    if user == "admin"
                    else dict(username=user, password=password)
                )
                if path.endswith("/_bulk"):
                    payload = body.decode()
                    kwargs["ndjson"] = True
                else:
                    payload = None if body is None else json.loads(body)
                status, value = self.client.request(
                    method, path, payload, **kwargs
                )
                channel.getresponse.return_value = Response(
                    b"" if value is None else private_io.canonical(value),
                    status=status,
                )

            channel.request.side_effect = request
            return channel

        patch.object(transport.ssl, "SSLContext").start()
        patch.object(
            transport, "_PinnedHTTPSConnection", side_effect=connect
        ).start()
        timers = patch.object(transport.threading, "Timer").start()
        return factory, clients, timers

    def phase_window(self, *, repeat):
        if repeat:
            self.provision()
        tick = [0.0]
        self.clock_trace = []
        factory, clients, timers = self.clocked_transport(tick)

        def query_elapsed(*_args, **_kwargs):
            tick[0] = 590.0
            self.clock_trace.append(("query-complete", tick[0]))
            return self.context["plan"]["revision"]

        def export(_context, client, _scan, _ready):
            self.clock_trace.append(("export-start", tick[0]))
            client.request("GET", "/" + INDEX + "/_mapping")
            tick[0] += 590.0
            self.clock_trace.append(("export-end", tick[0]))
            client.request("GET", "/" + INDEX + "/_mapping")
            return {"status": "exported", "cached_count": 2}

        self.source_mock.side_effect = query_elapsed
        self.cache_mock.side_effect = export
        result = query._provision(self.context, factory)
        self.assertEqual("query-ready", result["status"])
        self.assertEqual(2, len(clients))
        self.assertIsNot(clients[0], clients[1])
        self.assertEqual([0.0, 590.0], [c._started for c in clients])
        self.assertEqual([600.0, 1190.0], [c._deadline for c in clients])
        self.assertTrue(
            all(0 < call.args[0] <= 20 for call in timers.call_args_list)
        )

    def test_cold_query_and_export_have_independent_http_windows(self):
        self.phase_window(repeat=False)

    def test_ready_repeat_and_export_have_independent_http_windows(self):
        self.phase_window(repeat=True)

    def native_export_context(self):
        old_project = self.context["plan"]["project"]
        revision = self.context["plan"]["revision"]
        slot = self.root / (self.bundle.bundle_id[:12] + "-" + revision[:12])
        slot.mkdir(mode=0o700)
        self.root = slot
        self.context.update(root=slot, data=slot)
        self.context["plan"].update(
            project="depositar-ai-catalog-" + slot.name,
            bundle_path=str(
                next(
                    path
                    for path in (
                        Path(self.bundle_temp.name) / "bundles"
                    ).iterdir()
                    if path.is_dir()
                )
            ),
            source_sha256=PROFILE["fess_sources"],
        )
        self.context["compose"] = {
            "services": {
                "fess-search": {
                    "build": {"args": PROFILE["fess_build_arguments"]},
                    "environment": PROFILE["fess_environment"],
                },
                "ollama-embedding": {
                    "image": PROFILE["embedding_image"],
                    "environment": PROFILE["embedding_environment"],
                },
            }
        }
        self.context["plan"]["compose_sha256"] = execution.digest_record(
            self.context["compose"]
        )
        for container in self.containers:
            networks = container["NetworkSettings"]["Networks"]
            networks[self.context["plan"]["project"] + "_search_backend"] = (
                networks.pop(old_project + "_search_backend")
            )
        identity = execution.catalog_identity(self.context)
        self.runtime.update(identity)
        recipe = {
            **identity,
            "embedding_model": PROFILE["model"],
            "embedding_manifest_sha256": PROFILE["model_manifest_sha256"],
            "dimensions": PROFILE["dimensions"],
        }
        self.gate.update(
            **identity,
            runtime_sha256=execution.digest_record(self.runtime),
            recipe_sha256=execution.digest_record(recipe),
        )
        (slot / "vector-operations").mkdir(mode=0o700)
        for path, value in (
            (slot / "plan.json", self.context["plan"]),
            (slot / "compose.json", self.context["compose"]),
            (slot / "vector-operations/recipe.json", recipe),
        ):
            self.replace_record(path, value)
        patch.object(reuse, "_profile", return_value=PROFILE).start()
        self.cache_mock.side_effect = EXPORT_CACHE

    def native_export_window(self, elapsed, *, invoke=None):
        self.native_export_context()
        tick = [0.0]
        self.clock_trace = []
        factory, clients, _timers = self.clocked_transport(tick)
        self.export_clients = clients
        self.export_snapshot = {}

        def query_elapsed(*_args, **_kwargs):
            tick[0] = 590.0
            return self.context["plan"]["revision"]

        original_write = reuse._write

        def write(path, value, **kwargs):
            digest = original_write(path, value, **kwargs)
            if len(path.stem) == 64 and not self.export_snapshot:
                self.assertFalse((path.parent / "manifest.json").exists())
                paths = [path, path.parent / "export-started.json"] + [
                    self.root / "query-access" / name
                    for name in query.QUERY_FILES
                ]
                self.export_snapshot.update(
                    (p, (p.stat().st_ino, p.read_bytes())) for p in paths
                )
                tick[0] += elapsed
            return digest

        self.source_mock.side_effect = query_elapsed
        patch.object(reuse, "_write", side_effect=write).start()
        return (
            query._provision(self.context, factory)
            if invoke is None
            else invoke(factory)
        )

    def test_real_export_uses_its_own_window_and_publishes_manifest_last(self):
        result = self.native_export_window(590.0)
        self.assertEqual("query-ready", result["status"])
        self.assertEqual(2, result["vector_cache"]["cached_documents"])
        self.assertEqual(2, len(self.export_clients))
        self.assertEqual(
            [("172.19.0.3", b"sealed-ca", "a" * 64)] * 2,
            self.clock_inputs,
        )
        manifest = self.root / "vector-cache/manifest.json"
        self.assertTrue(manifest.exists())
        for path, state in self.export_snapshot.items():
            self.assertEqual(state, (path.stat().st_ino, path.read_bytes()))
        self.assertEqual(2, json.loads(manifest.read_bytes())["cached_count"])

    def test_export_own_deadline_keeps_ready_and_partial_objects(self):
        with self.assertRaisesRegex(
            query.RuntimeError, "reuse_export_invalid"
        ):
            self.native_export_window(601.0)
        self.assertEqual(2, len(self.export_clients))
        self.assertEqual(1, self.cache_mock.call_count)
        self.assertTrue(self.export_snapshot)
        self.assertFalse((self.root / "vector-cache/manifest.json").exists())
        for path, state in self.export_snapshot.items():
            self.assertEqual(state, (path.stat().st_ino, path.read_bytes()))
        objects = [
            path
            for path in (self.root / "vector-cache").iterdir()
            if len(path.stem) == 64
        ]
        self.assertTrue(objects)
        for path in objects:
            self.assertEqual(0o400, path.stat().st_mode & 0o777)
            self.assertEqual(
                path.stem, hashlib.sha256(path.read_bytes()).hexdigest()
            )

    def test_export_deadline_never_publishes_product_query_candidate(self):
        store = Mock()

        def invoke(factory):
            with (
                patch.object(lifecycle, "_require_revision"),
                patch.object(
                    lifecycle, "operation", return_value=nullcontext({})
                ),
                patch.object(lifecycle, "_candidate", return_value={}),
                patch.object(
                    lifecycle, "_index_plan", return_value=self.context["plan"]
                ),
                patch.object(
                    query,
                    "provision",
                    side_effect=lambda *_args, **_kwargs: query._provision(
                        self.context, factory
                    ),
                ),
            ):
                return lifecycle._prepare_query_locked(
                    None,
                    store,
                    self.context["plan"]["revision"],
                    repo_root=self.root,
                )

        with self.assertRaisesRegex(
            query.RuntimeError, "reuse_export_invalid"
        ):
            self.native_export_window(601.0, invoke=invoke)
        store.write.assert_not_called()
        self.assertEqual(2, len(self.export_clients))
        self.assertTrue((self.root / "query-access/ready.json").exists())
        self.assertFalse((self.root / "vector-cache/manifest.json").exists())

    def cutover_drift(self, change, diagnostic):
        original_write = query._write
        calls_at_cutover = []

        def write(root, name, value, **kwargs):
            original_write(root, name, value, **kwargs)
            if name == "ready.json":
                calls_at_cutover.extend(
                    (
                        len(self.client.projection.calls),
                        len(self.client.access.calls),
                    )
                )
                change()

        with (
            patch.object(query, "_write", side_effect=write),
            self.assertRaisesRegex(ValueError, diagnostic),
        ):
            self.provision()
        self.assertEqual(1, len(self.factory_calls))
        self.cache_mock.assert_not_called()
        self.assertEqual(
            calls_at_cutover,
            [len(self.client.projection.calls), len(self.client.access.calls)],
        )
        self.assertTrue((self.root / "query-access/ready.json").exists())
        self.assertFalse((self.root / "vector-cache/manifest.json").exists())

    def test_source_drift_at_export_cutover_blocks_second_client(self):
        self.cutover_drift(
            lambda: setattr(self.source_mock, "return_value", "9" * 40),
            "runtime_changed",
        )

    def test_runtime_drift_at_export_cutover_blocks_second_client(self):
        self.cutover_drift(
            lambda: setattr(
                self.runtime_mock,
                "return_value",
                ({**self.runtime, "network_id": "9" * 64}, {}),
            ),
            "runtime_changed",
        )

    def test_peer_drift_at_export_cutover_blocks_second_client(self):
        def change():
            cid = self.runtime["container_ids"]["opensearch-search"]
            name = self.context["plan"]["project"] + "_search_backend"
            self.containers[1]["NetworkSettings"]["Networks"][name][
                "IPAddress"
            ] = "172.19.0.8"
            self.network["Containers"][cid]["IPv4Address"] = "172.19.0.8/16"

        self.cutover_drift(change, "runtime_changed")

    def test_admin_drift_at_export_cutover_blocks_second_client(self):
        self.cutover_drift(
            lambda: setattr(
                self.materials_mock, "return_value", ("9" * 64, b"sealed-ca")
            ),
            "secret_changed",
        )

    def test_ca_drift_at_export_cutover_blocks_second_client(self):
        self.cutover_drift(
            lambda: setattr(
                self.materials_mock, "return_value", ("a" * 64, b"changed-ca")
            ),
            "secret_changed",
        )

    def test_query_secret_drift_at_export_cutover_blocks_second_client(self):
        self.cutover_drift(
            lambda: (
                self.root / "query-access" / access.SECRET_NAME
            ).write_bytes(b"9" * 64 + b"\n"),
            "secret_changed",
        )

    def test_query_binding_drift_at_export_cutover_blocks_second_client(self):
        path = self.root / "query-access" / access.BINDING_NAME

        def change():
            binding = json.loads(path.read_bytes())
            binding["bundle_id"] = "9" * 64
            self.replace_record(path, binding)

        self.cutover_drift(change, "binding_changed")

    def test_export_vector_drift_cannot_publish_manifest_or_retry(self):
        self.native_export_context()

        def factory(peer, ca, admin):
            client = self.os_factory(peer, ca, admin)
            if len(self.factory_calls) == 2:
                self.client.projection.rows[0]["_source"][
                    "content_chunk_vector"
                ][0]["vector"][0] = 2.0
            return client

        with self.assertRaisesRegex(
            query.RuntimeError, "reuse_export_invalid"
        ):
            query._provision(self.context, factory)
        self.assertEqual(2, len(self.factory_calls))
        self.assertEqual(1, self.cache_mock.call_count)
        self.assertTrue((self.root / "query-access/ready.json").exists())
        self.assertTrue(
            (self.root / "vector-cache/export-started.json").exists()
        )
        self.assertFalse((self.root / "vector-cache/manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
