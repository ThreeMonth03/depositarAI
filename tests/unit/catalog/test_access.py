"""Query-only operator provisioning with fake HTTP and private temporary state."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.api.retrieval import index as index_client
from search.operations.catalog import access
from search.operations.shared import private_io
from tests.unit.catalog.test_projection import INDEX, make_bundle, shards


class AccessClient:
    def __init__(self, count):
        self.count = count
        self.values = {}
        self.password = None
        self.calls = []
        self.fail_put = None
        self.role_fault = None
        self.probe_status = {}
        self.search_fault = None

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(payload), copy.deepcopy(kwargs)))
        if kwargs:
            assert set(kwargs) == {"username", "password"}
            assert kwargs["username"] == access.USER
            if kwargs["password"] != self.password:
                return 401, None
            if method == "POST" and path == "/" + INDEX + "/_search":
                response = {
                    "timed_out": False,
                    "_shards": shards(),
                    "hits": {
                        "total": {"value": self.count, "relation": "eq"},
                        "hits": [],
                    },
                }
                if self.search_fault:
                    self.search_fault(response)
                return self.probe_status.get((method, path), 200), response
            return self.probe_status.get((method, path), 403), None
        for kind in access.KINDS:
            if path != access.SECURITY_PATH + kind + "/" + access.USER:
                continue
            if method == "GET":
                if kind not in self.values:
                    return 404, None
                value = copy.deepcopy(self.values[kind])
                if self.role_fault and kind == "roles":
                    self.role_fault(value)
                return 200, {access.USER: value}
            if method == "PUT":
                if self.fail_put == kind:
                    return 500, None
                value = copy.deepcopy(payload)
                if kind == "internalusers":
                    self.password = value.pop("password")
                    value.update(
                        hash="redacted-private-hash", opendistro_security_roles=[]
                    )
                value.update(reserved=False, hidden=False, static=False)
                self.values[kind] = value
                return 201, {"status": "CREATED"}
        raise AssertionError("unexpected fixed operator request")

    def admin_writes(self):
        return [call for call in self.calls if call[0] == "PUT" and not call[3]]


class CatalogAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle_temporary = tempfile.TemporaryDirectory(
            prefix="depositar-access-bundle-"
        )
        cls.bundle = make_bundle(Path(cls.bundle_temporary.name), count=2)

    @classmethod
    def tearDownClass(cls):
        cls.bundle_temporary.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-access-state-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = AccessClient(len(self.bundle.documents))

    def provision(self):
        return access.provision_access(self.client, self.bundle, INDEX, self.root)

    def save(self, name, encoded):
        fd = private_io.open_directory(self.root)
        try:
            private_io.write_file(fd, name, encoded, max_bytes=4096)
        finally:
            os.close(fd)

    def test_creates_private_secret_exact_binding_and_query_only_privileges(self):
        proof = self.provision()
        self.assertEqual("ready", proof["status"])
        self.assertIs(proof["created"], True)
        self.assertIs(proof["admin_mounted_in_api"], False)
        self.assertEqual(6, len(proof["probes"]))
        self.assertTrue(all(row["passed"] for row in proof["probes"]))
        self.assertEqual(3, len(self.client.admin_writes()))
        self.assertEqual(access.role_definition(INDEX), proof["role"])
        secret = self.root / access.SECRET_NAME
        self.assertRegex(secret.read_text(), r"\A[0-9a-f]{64}\n\Z")
        for name in (access.SECRET_NAME, access.BINDING_NAME):
            info = (self.root / name).stat()
            self.assertEqual(0o600, info.st_mode & 0o777)
            self.assertEqual(1, info.st_nlink)
        binding = json.loads((self.root / access.BINDING_NAME).read_text())
        self.assertEqual(proof["binding"], binding)
        # Actual API parser, not a duplicated expectation of its contract.
        self.assertEqual(
            binding,
            index_client.load_binding(self.root / access.BINDING_NAME, self.bundle),
        )
        self.assertNotIn(self.client.password, json.dumps(proof))
        self.assertNotIn("redacted-private-hash", json.dumps(proof))

    def test_exact_completed_repeat_is_readback_only_no_secret_rotation(self):
        self.provision()
        old = {
            name: ((self.root / name).stat().st_ino, (self.root / name).read_bytes())
            for name in (access.SECRET_NAME, access.BINDING_NAME)
        }
        self.client.calls.clear()
        proof = self.provision()
        self.assertIs(proof["created"], False)
        self.assertEqual([], self.client.admin_writes())
        for name, identity in old.items():
            self.assertEqual(
                identity,
                ((self.root / name).stat().st_ino, (self.root / name).read_bytes()),
            )

    def test_verify_does_not_provision_missing_identity(self):
        with self.assertRaisesRegex(access.AccessError, "recovery_required"):
            access.verify_access(self.client, self.bundle, INDEX, self.root)
        self.assertEqual([], self.client.calls)
        self.assertEqual([], list(self.root.iterdir()))
        self.provision()
        self.client.calls.clear()
        proof = access.verify_access(self.client, self.bundle, INDEX, self.root)
        self.assertEqual([], self.client.admin_writes())
        self.assertIs(proof["created"], False)

    def test_secret_without_binding_requires_explicit_recovery_without_http(self):
        secret = b"a" * 64 + b"\n"
        self.save(access.SECRET_NAME, secret)
        with self.assertRaisesRegex(access.AccessError, "recovery_required"):
            self.provision()
        self.assertEqual(secret, (self.root / access.SECRET_NAME).read_bytes())
        self.assertEqual([], self.client.calls)
        self.assertFalse((self.root / access.BINDING_NAME).exists())

    def test_binding_without_secret_requires_recovery(self):
        self.save(
            access.BINDING_NAME,
            private_io.canonical(access._binding(self.bundle, INDEX)),
        )
        with self.assertRaisesRegex(access.AccessError, "recovery_required"):
            self.provision()
        self.assertEqual([], self.client.calls)

    def test_remote_existing_or_partial_identity_is_never_overwritten(self):
        for kind in access.KINDS:
            self.client = AccessClient(len(self.bundle.documents))
            self.client.values[kind] = {"unrelated": "existing-identity"}
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(access.AccessError, "recovery_required"),
            ):
                self.provision()
            self.assertEqual([], self.client.admin_writes())
            self.assertFalse((self.root / access.SECRET_NAME).exists())

    def test_uncertain_put_preserves_secret_and_blocks_a_second_attempt(self):
        self.client.fail_put = "internalusers"
        with self.assertRaisesRegex(access.AccessError, "provision_failed"):
            self.provision()
        secret = (self.root / access.SECRET_NAME).read_bytes()
        self.assertFalse((self.root / access.BINDING_NAME).exists())
        self.client.calls.clear()
        with self.assertRaisesRegex(access.AccessError, "recovery_required"):
            self.provision()
        self.assertEqual([], self.client.calls)
        self.assertEqual(secret, (self.root / access.SECRET_NAME).read_bytes())

    def test_widened_role_is_not_repaired_or_accepted_on_repeat(self):
        self.provision()
        for fault in (
            lambda value: value.update(cluster_permissions=["cluster_all"]),
            lambda value: value.update(tenant_permissions=[{"tenant_patterns": ["*"]}]),
            lambda value: value["index_permissions"][0].update(index_patterns=["*"]),
            lambda value: value["index_permissions"][0].update(
                allowed_actions=["indices_all"]
            ),
            lambda value: value["index_permissions"].append(
                copy.deepcopy(value["index_permissions"][0])
            ),
            lambda value: value.update(hidden=True),
            lambda value: value.update(unknown_permissions=["admin"]),
        ):
            self.client.calls.clear()
            self.client.role_fault = fault
            with (
                self.subTest(fault=fault),
                self.assertRaisesRegex(access.AccessError, "permissions_changed"),
            ):
                self.provision()
            self.assertEqual([], self.client.admin_writes())

    def test_user_or_mapping_permissions_drift_is_rejected(self):
        self.provision()
        original = copy.deepcopy(self.client.values)
        for kind, key, value in (
            ("internalusers", "backend_roles", ["admin"]),
            ("internalusers", "opendistro_security_roles", ["all_access"]),
            ("rolesmapping", "users", [access.USER, "admin"]),
            ("rolesmapping", "hosts", ["*"]),
            ("rolesmapping", "and_backend_roles", ["admin"]),
        ):
            self.client.values = copy.deepcopy(original)
            self.client.values[kind][key] = value
            with (
                self.subTest(kind=kind, key=key),
                self.assertRaisesRegex(access.AccessError, "permissions_changed"),
            ):
                self.provision()

    def test_each_negative_probe_must_really_deny_access(self):
        self.provision()
        for method, path in (
            ("PUT", "/" + INDEX + "/_doc/authorization-probe"),
            ("POST", "/fess_user.user/_search"),
            ("GET", access.SECURITY_PATH + "internalusers"),
            ("PUT", "/" + INDEX + "/_mapping"),
        ):
            self.client.probe_status = {(method, path): 200}
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(access.AccessError, "privilege_probe_failed"),
            ):
                self.provision()

    def test_allowed_search_requires_complete_inventory_and_shards(self):
        self.provision()
        for fault in (
            lambda value: value["hits"]["total"].update(value=1),
            lambda value: value["hits"]["total"].update(relation="gte"),
            lambda value: value["_shards"].update(failed=1),
            lambda value: value.update(timed_out=True),
            lambda value: value.update(terminated_early=True),
        ):
            self.client.search_fault = fault
            with (
                self.subTest(fault=fault),
                self.assertRaisesRegex(access.AccessError, "search_incomplete"),
            ):
                self.provision()

    def test_wrong_password_prevents_binding_publication(self):
        original = self.client.request

        def wrong(method, path, payload=None, **kwargs):
            result = original(method, path, payload, **kwargs)
            if method == "PUT" and path.endswith("internalusers/" + access.USER):
                self.client.password = "b" * 64
            return result

        with patch.object(self.client, "request", side_effect=wrong):
            with self.assertRaisesRegex(access.AccessError, "privilege_probe_failed"):
                self.provision()
        self.assertTrue((self.root / access.SECRET_NAME).exists())
        self.assertFalse((self.root / access.BINDING_NAME).exists())

    def test_binding_change_and_duplicate_json_keys_are_rejected(self):
        self.provision()
        path = self.root / access.BINDING_NAME
        for encoded in (
            private_io.canonical(
                {**access._binding(self.bundle, INDEX), "index": "fess.20261008120001"}
            ),
            b'{"index":"wrong","index":"' + INDEX.encode() + b'"}\n',
        ):
            path.write_bytes(encoded)
            self.client.calls.clear()
            with self.assertRaises(access.AccessError):
                self.provision()
            self.assertEqual([], self.client.calls)

    def test_private_modes_symlink_and_hardlink_are_not_relaxed(self):
        self.provision()
        secret = self.root / access.SECRET_NAME
        secret.chmod(0o644)
        with self.assertRaises(ValueError):
            self.provision()
        secret.chmod(0o600)
        os.link(secret, self.root / "hardlink")
        with self.assertRaises(ValueError):
            self.provision()
        with tempfile.TemporaryDirectory(prefix="depositar-access-link-") as temporary:
            root = Path(temporary)
            (root / access.SECRET_NAME).symlink_to(secret)
            with self.assertRaises(OSError):
                access.provision_access(self.client, self.bundle, INDEX, root)

    def test_directory_must_exist_be_private_and_not_a_symlink(self):
        public = self.root / "public"
        public.mkdir(mode=0o755)
        link = self.root / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        for path in (public, link, self.root / "absent"):
            with self.subTest(path=path.name), self.assertRaises((OSError, ValueError)):
                access.provision_access(self.client, self.bundle, INDEX, path)
        self.assertEqual([], self.client.calls)

    def test_different_index_cannot_relabel_an_existing_binding(self):
        self.provision()
        self.client.calls.clear()
        with self.assertRaisesRegex(access.AccessError, "binding_changed"):
            access.provision_access(
                self.client, self.bundle, "fess.20261008120001", self.root
            )
        self.assertEqual([], self.client.calls)

    def test_physical_index_not_wildcard_alias_or_url(self):
        for index in (
            "fess.search",
            "fess.*",
            "*",
            "https://evil.invalid",
            "fess.123\n",
        ):
            with self.subTest(index=index), self.assertRaises(ValueError):
                access.provision_access(self.client, self.bundle, index, self.root)
        self.assertEqual([], self.client.calls)

    def test_transport_output_is_sanitized_and_secret_stays_out_of_reports(self):
        class Broken:
            def request(self, *_args, **_kwargs):
                raise OSError("administrator-password-sensitive-server-body")

        with self.assertRaises(access.AccessError) as error:
            access.provision_access(Broken(), self.bundle, INDEX, self.root)
        self.assertNotIn("administrator-password", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        self.assertEqual([], list(self.root.iterdir()))

    def test_all_zero_generated_secret_has_a_different_negative_probe_password(self):
        with patch.object(access.secrets, "token_hex", return_value="0" * 64):
            proof = self.provision()
        self.assertTrue(all(row["passed"] for row in proof["probes"]))
        query_credentials = [
            kwargs["password"] for _, _, _, kwargs in self.client.calls if kwargs
        ]
        self.assertIn("1" * 64, query_credentials)


if __name__ == "__main__":
    unittest.main()
