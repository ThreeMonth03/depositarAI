"""Remote CPU lifecycle seals backend identity before any container mutation."""

from contextlib import redirect_stdout
import http.client
import io
import json
import ssl
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment.public_entry import (
    host,
    preflight,
    profile,
    remote,
    updates,
)
from tests.unit.deployment.public_entry import test_remote as remote_fixture
from tests.unit.deployment.public_entry import test_updates as update_fixture


class RemoteLifecycleTests(unittest.TestCase):
    # Compose fixture helpers, rather than inherit its TestCase and rerun its tests.
    @classmethod
    def setUpClass(cls):
        remote_fixture.RemoteFixture.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        remote_fixture.RemoteFixture.tearDownClass.__func__(cls)

    def setUp(self):
        self.verify_bound = preflight.verify_bound_backend
        update_fixture.PublicEntryUpdatesTests.setUp(self)
        self.ca = self.root / "backend-ca.pem"
        self.ca.write_bytes((self.cert_root / "ca.pem").read_bytes())
        self.ca.chmod(0o600)
        self.backend_path = self.root / "backend.json"
        self.backend_value = {
            "schema_version": 1,
            "origin": "https://backend.example.test",
            "api_revision": "5" * 40,
            "catalog_manifest_sha256": "6" * 64,
            "api_key_file": str(self.key),
            "ca_file": str(self.ca),
        }
        self.write_backend()
        self.remote_backend = remote.load_remote_backend(self.backend_path)
        self.config = profile.load_public_entry_config(self.config_path)

    def write_backend(self, **changes):
        self.backend_path.write_text(json.dumps({**self.backend_value, **changes}))
        self.backend_path.chmod(0o600)

    def inspector(self, image):
        proof = update_fixture.PublicEntryUpdatesTests.inspector(self, image)
        if image in (update_fixture.OLD, update_fixture.NEW):
            proof["Labels"]["io.depositar.ai.conversation.version"] = "2"
        return proof

    def invoke(self, function, web=update_fixture.OLD, frontend=update_fixture.FRONT):
        options = {
            "runner": self.runner,
            "image_inspector": self.inspector,
            "smoke_probe": self.smoke,
        }
        if function is updates.rollback_public_entry:
            return function(self.config_path, **options)
        return function(
            self.config_path,
            None,
            web,
            frontend,
            backend_config=self.backend_path,
            **options,
        )

    def active(self):
        return update_fixture.PublicEntryUpdatesTests.active(self)

    def bind(self, proof=None):
        return host.bind_remote_web(
            self.config,
            profile.render_public_entry(self.config),
            self.remote_backend,
            update_fixture.OLD,
            self.inspector(update_fixture.OLD) if proof is None else proof,
            update_fixture.FRONT,
            self.inspector(update_fixture.FRONT),
        )["compose"]

    def snapshot(self, result):
        return json.loads(
            (
                self.base / "versions" / result["generation"] / "snapshot.json"
            ).read_text()
        )

    def assert_no_mutations(self, commands):
        for command in commands:
            operation = command[command.index("-f") + 2]
            self.assertEqual(operation, "ps", command)

    def assert_rejected_without_changes(self, action, error):
        before = len(self.runner.commands)
        active = (self.base / "active.json").read_bytes()
        pending = self.base / "pending.json"
        self.assertFalse(pending.exists())
        with self.assertRaisesRegex(ValueError, error):
            action()
        self.assertEqual(self.runner.commands[before:], [])
        self.assertEqual((self.base / "active.json").read_bytes(), active)
        self.assertFalse(pending.exists())

    def test_remote_prepare_never_reads_a_local_release_or_observes_gpu_docker(self):
        self.release_path.unlink()
        with (
            patch.object(
                host,
                "bind_public_web",
                side_effect=AssertionError("local release forbidden"),
            ),
            patch.object(
                subprocess,
                "run",
                side_effect=AssertionError("prepare must stay offline"),
            ),
        ):
            result = host.prepare_public_entry(
                self.config_path,
                None,
                update_fixture.OLD,
                update_fixture.FRONT,
                backend_config=self.backend_path,
                image_inspector=self.inspector,
            )
        self.assertEqual(result["status"], "public_entry_prepared")
        self.assertFalse(result["deployment_performed"])
        self.assertFalse(self.release_path.exists())
        self.assertEqual(self.runner.commands, [])
        prepared = json.loads((self.base / "compose.json").read_text())
        self.assertEqual(
            prepared["x-depositar-plan"]["private_backend"],
            self.remote_backend.to_plan(),
        )

    def test_remote_binding_keeps_keys_and_ca_in_the_cpu_bff_only(self):
        composition = self.bind()
        self.assertNotIn("existing-release", composition["networks"])
        web = composition["services"]["public-web"]
        self.assertEqual(set(web["networks"]), {"website", "outbound"})
        self.assertTrue(composition["networks"]["website"]["internal"])
        self.assertFalse(composition["networks"]["outbound"]["internal"])
        self.assertEqual(web["environment"]["WEB_UPSTREAM"], self.remote_backend.origin)
        self.assertEqual(web["environment"]["WEB_UPSTREAM_CA_FILE"], remote.CA_TARGET)
        mounts = {mount["target"]: mount for mount in web["volumes"]}
        for target, source in (
            (remote.API_KEY_TARGET, self.key),
            (remote.CA_TARGET, self.ca),
        ):
            self.assertEqual(mounts[target]["source"], str(source))
            self.assertTrue(mounts[target]["read_only"])
        self.assertFalse(web.get("ports"))
        self.assertFalse(web.get("devices"))
        self.assertEqual(web["runtime"], "runc")
        frontend = composition["services"]["public-frontend"]
        self.assertEqual(frontend["networks"], ["website"])
        self.assertFalse(frontend.get("volumes"))
        self.assertEqual(frontend["environment"], {"NVIDIA_VISIBLE_DEVICES": "void"})
        self.assertEqual(
            composition["x-depositar-plan"]["private_backend"],
            {
                "schema_version": 2,
                "kind": "https",
                "origin": self.backend_value["origin"],
                "api_revision": self.backend_value["api_revision"],
                "catalog_manifest_sha256": self.backend_value[
                    "catalog_manifest_sha256"
                ],
                "conversation_contract_version": 2,
            },
        )

    def test_missing_or_legacy_bff_wire_capability_is_rejected_before_prepare(self):
        for version in (None, "1", 2, True):
            proof = self.inspector(update_fixture.OLD)
            if version is None:
                proof["Labels"].pop("io.depositar.ai.conversation.version")
            else:
                proof["Labels"]["io.depositar.ai.conversation.version"] = version
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(ValueError, "remote_contract_invalid"),
            ):
                self.bind(proof)
        self.assertEqual(self.runner.commands, [])
        self.assertFalse(self.base.exists())

    def test_exactly_one_backend_source_is_required_before_publication(self):
        for release, backend in ((None, None), (self.release_path, self.backend_path)):
            with (
                self.subTest(release=release),
                self.assertRaisesRegex(ValueError, "backend_source_invalid"),
            ):
                host.prepare_public_entry(
                    self.config_path,
                    release,
                    update_fixture.OLD,
                    update_fixture.FRONT,
                    backend_config=backend,
                    image_inspector=self.inspector,
                )
        self.assertFalse(self.base.exists())
        self.assertEqual(self.runner.commands, [])

    def test_remote_update_and_rollback_keep_sealed_backend_material_and_redis(self):
        started = self.invoke(updates.bootstrap_public_entry)
        first = self.snapshot(started)
        updated = self.invoke(
            updates.update_public_entry, update_fixture.NEW, update_fixture.NEW_FRONT
        )
        second = self.snapshot(updated)
        self.assertEqual(first["material_sha256"], second["material_sha256"])
        for saved in (first, second):
            self.assertEqual(
                saved["compose"]["x-depositar-plan"]["private_backend"],
                self.remote_backend.to_plan(),
            )
            mounts = {
                mount["target"]: mount["source"]
                for mount in saved["compose"]["services"]["public-web"]["volumes"]
            }
            self.assertEqual(mounts[remote.API_KEY_TARGET], str(self.key))
            self.assertEqual(mounts[remote.CA_TARGET], str(self.ca))
        sentinel = self.base / "redis-data" / "retained-counter"
        sentinel.write_text("keep-existing-budget")
        # Rollback trusts its sealed snapshot, not a newly edited input config.
        self.write_backend(
            origin="https://different.example.test", api_revision="7" * 40
        )
        rolled = self.invoke(updates.rollback_public_entry)
        self.assertEqual(rolled["status"], "public_entry_rolled_back")
        self.assertEqual(rolled["generation"], started["generation"])
        self.assertEqual(self.runner.images, (update_fixture.OLD, update_fixture.FRONT))
        self.assertEqual(self.runner.redis_starts, 1)
        self.assertEqual(sentinel.read_text(), "keep-existing-budget")
        self.assertEqual(
            self.snapshot(rolled)["compose"]["x-depositar-plan"]["private_backend"],
            self.remote_backend.to_plan(),
        )

    def test_changed_remote_identity_or_mount_paths_refuse_update_before_mutation(self):
        self.invoke(updates.bootstrap_public_entry)
        alternate_key = self.root / "alternate-key"
        alternate_ca = self.root / "alternate-ca.pem"
        for destination, source in ((alternate_key, self.key), (alternate_ca, self.ca)):
            destination.write_bytes(source.read_bytes())
            destination.chmod(0o600)
        for field, value in (
            ("origin", "https://different.example.test"),
            ("api_revision", "7" * 40),
            ("catalog_manifest_sha256", "8" * 64),
            ("api_key_file", str(alternate_key)),
            ("ca_file", str(alternate_ca)),
        ):
            self.write_backend(**{field: value})
            with self.subTest(field=field):
                self.assert_rejected_without_changes(
                    lambda: self.invoke(
                        updates.update_public_entry,
                        update_fixture.NEW,
                        update_fixture.NEW_FRONT,
                    ),
                    "profile_changed",
                )
        self.write_backend()

    def test_key_and_ca_content_drift_refuse_update_and_rollback_before_mutation(self):
        self.invoke(updates.bootstrap_public_entry)
        self.invoke(
            updates.update_public_entry, update_fixture.NEW, update_fixture.NEW_FRONT
        )
        for path, changed in (
            (self.key, b"8" * 64),
            (self.ca, self.ca.read_bytes() + b"\n"),
        ):
            original = path.read_bytes()
            try:
                path.write_bytes(changed)
                for action in (
                    lambda: self.invoke(updates.update_public_entry),
                    lambda: self.invoke(updates.rollback_public_entry),
                ):
                    with self.subTest(path=path.name, action=action):
                        self.assert_rejected_without_changes(action, "material_changed")
            finally:
                path.write_bytes(original)

    def test_saved_backend_plan_drift_refuses_rollback_before_mutation(self):
        first = self.invoke(updates.bootstrap_public_entry)
        self.invoke(
            updates.update_public_entry, update_fixture.NEW, update_fixture.NEW_FRONT
        )
        path = self.base / "versions" / first["generation"] / "snapshot.json"
        original = path.read_bytes()
        for field, value in (
            ("origin", "https://different.example.test"),
            ("api_revision", "7" * 40),
            ("catalog_manifest_sha256", "8" * 64),
        ):
            changed = json.loads(original)
            changed["compose"]["x-depositar-plan"]["private_backend"][field] = value
            path.write_text(json.dumps(changed))
            try:
                with self.subTest(field=field):
                    self.assert_rejected_without_changes(
                        lambda: self.invoke(updates.rollback_public_entry),
                        "snapshot_invalid",
                    )
            finally:
                path.write_bytes(original)

    def wire_runner(self, version, requests):
        """Run the real version-only source against an in-memory HTTPS transport."""

        def run(argv, **options):
            self.assertEqual(options["timeout"], remote.COMMAND_TIMEOUT)
            source = argv[argv.index("-c") + 1]
            output = io.StringIO()

            def connection(*_args, **_kwargs):
                authenticated = False

                def request(method, path, headers):
                    nonlocal authenticated
                    authenticated = "Authorization" in headers
                    requests.append((method, path, authenticated))

                def getresponse():
                    body = {
                        "revision": self.backend_value["api_revision"],
                        "catalog_manifest_sha256": self.backend_value[
                            "catalog_manifest_sha256"
                        ],
                    }
                    if version is not None:
                        body["conversation_contract_version"] = version
                    raw = json.dumps(body).encode() if authenticated else b"{}"
                    return SimpleNamespace(
                        status=200 if authenticated else 401, read=lambda _limit: raw
                    )

                return SimpleNamespace(
                    request=request, getresponse=getresponse, close=lambda: None
                )

            result = 0
            with (
                patch.dict("os.environ", options["env"], clear=True),
                patch.object(sys, "argv", [argv[0], *argv[-2:]]),
                patch.object(sys, "path", list(sys.path)),
                patch.object(threading, "Timer"),
                patch.object(ssl, "SSLContext"),
                patch.object(http.client, "HTTPSConnection", side_effect=connection),
                redirect_stdout(output),
            ):
                try:
                    exec(source, {"__name__": "__main__"})
                except SystemExit as error:
                    result = error.code
            return SimpleNamespace(
                returncode=result, stdout=output.getvalue().encode(), stderr=b""
            )

        return run

    def test_preflight_rejects_wire_version_mismatch_without_querying_or_docker(self):
        bound = self.bind()
        for version in (None, 1, True, "2"):
            requests = []
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(remote.PublicEntryRemoteError, "probe_failed"),
            ):
                self.verify_bound(bound, runner=self.wire_runner(version, requests))
            self.assertEqual(
                requests, [("GET", "/v1/version", False), ("GET", "/v1/version", True)]
            )
        self.assertEqual(
            self.verify_bound(bound, runner=self.wire_runner(2, [])), remote.PROOF
        )
        self.assertEqual(self.runner.commands, [])

    def test_wire_contract_failure_prevents_first_container_start(self):
        self.backend.side_effect = lambda bound, **_options: self.verify_bound(
            bound, runner=self.wire_runner(1, [])
        )
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "probe_failed"):
            self.invoke(updates.bootstrap_public_entry)
        self.assert_no_mutations(self.runner.commands)
        self.assertFalse((self.base / "active.json").exists())
        self.assertFalse((self.base / "pending.json").exists())

    def test_wire_contract_failure_prevents_update_and_rollback_mutation(self):
        self.invoke(updates.bootstrap_public_entry)
        self.invoke(
            updates.update_public_entry, update_fixture.NEW, update_fixture.NEW_FRONT
        )
        self.backend.side_effect = lambda bound, **_options: self.verify_bound(
            bound, runner=self.wire_runner(1, [])
        )
        for action in (
            lambda: self.invoke(updates.update_public_entry),
            lambda: self.invoke(updates.rollback_public_entry),
        ):
            self.assert_rejected_without_changes(action, "probe_failed")
