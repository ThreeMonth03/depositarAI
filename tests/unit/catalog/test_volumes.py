"""Offline named-volume creation, ownership and non-adoption contracts."""

from __future__ import annotations

import ast
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from search.operations.catalog import commands, execution, volume_policy, volumes
from tests.unit.catalog import volume_fixtures
from tests.unit.catalog.volume_fixtures import IMAGE, CatalogVolumeFixture


class CatalogVolumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = CatalogVolumeFixture(self)

    def test_ensure_requires_durable_start_intent_before_docker_or_image_build(self):
        with self.assertRaises(FileNotFoundError):
            volumes.ensure(self.fixture.context)
        self.assertEqual([], self.fixture.calls)
        self.fixture.image.assert_not_called()

    def test_new_volume_is_bounded_sealed_and_reused_without_root_mutation(self):
        self.fixture.start_intent()
        receipt = volumes.ensure(self.fixture.context)
        self.assertEqual("ready", receipt["status"])
        self.assertEqual("catalog-index", receipt["alias"])
        self.assertEqual(
            {"uid": os.geteuid(), "gid": os.getegid(), "mode": 0o700},
            receipt["ownership"],
        )
        self.assertEqual(
            receipt, execution.read_record(self.fixture.root, "volume.json")
        )
        for name in ("volume-started.json", "volume.json"):
            self.assertEqual(0o600, (self.fixture.root / name).stat().st_mode & 0o777)
        runs = [argv for argv in self.fixture.calls if argv[:2] == ["docker", "run"]]
        self.assertEqual(2, len(runs))
        for argv in runs:
            self.assertEqual("never", argv[argv.index("--pull") + 1])
            self.assertEqual("none", argv[argv.index("--network") + 1])
            self.assertEqual("runc", argv[argv.index("--runtime") + 1])
            self.assertEqual("ALL", argv[argv.index("--cap-drop") + 1])
            self.assertEqual("64m", argv[argv.index("--memory") + 1])
            self.assertIn("--read-only", argv)
            self.assertIn("volume-nocopy", argv[argv.index("--mount") + 1])
            self.assertNotIn("--gpus", argv)
            self.assertNotIn("--privileged", argv)
            self.assertEqual(IMAGE, argv[argv.index("--entrypoint") + 2])
        root, probe = runs
        self.assertEqual("CHOWN", root[root.index("--cap-add") + 1])
        self.assertLess(root[-1].index("os.chmod"), root[-1].index("os.chown"))
        self.assertNotIn("-R", root)
        self.assertIn(",readonly", probe[probe.index("--mount") + 1])
        self.assertNotIn("--cap-add", probe)
        self.fixture.calls.clear()
        self.assertEqual(receipt, volumes.ensure(self.fixture.context))
        self.assertEqual(receipt, volumes.require(self.fixture.context))
        self.assertTrue(
            all(
                call.kwargs["read_only"]
                for call in self.fixture.image.call_args_list[1:]
            )
        )
        self.assertFalse(
            any(
                "create" in argv or "--cap-add" in argv or "rm" in argv
                for argv in self.fixture.calls
            )
        )

    def test_unknown_existing_volume_is_never_adopted_or_chowned(self):
        self.fixture.start_intent()
        self.fixture.metadata = {
            "name": self.fixture.context["plan"]["project"] + "-index"
        }
        with self.assertRaisesRegex(ValueError, "existing_without_receipt"):
            volumes.ensure(self.fixture.context)
        self.fixture.image.assert_not_called()
        self.assertFalse((self.fixture.root / "volume-started.json").exists())
        self.assertFalse(
            any(argv[:2] == ["docker", "run"] for argv in self.fixture.calls)
        )

    def test_failed_initialization_preserves_intent_and_never_retries_or_deletes(self):
        self.fixture.start_intent()
        self.fixture.fail_initialize = True
        with self.assertRaises(commands.BackendError):
            volumes.ensure(self.fixture.context)
        self.assertTrue((self.fixture.root / "volume-started.json").exists())
        self.assertFalse((self.fixture.root / "volume.json").exists())
        self.fixture.calls.clear()
        with self.assertRaisesRegex(ValueError, "explicit_recovery"):
            volumes.ensure(self.fixture.context)
        with self.assertRaisesRegex(ValueError, "explicit_recovery"):
            volumes.require(self.fixture.context)
        self.assertEqual([], self.fixture.calls)

    def test_driver_options_scope_labels_and_recreated_volume_identity_are_rejected(
        self,
    ):
        self.fixture.start_intent()
        volumes.ensure(self.fixture.context)
        original = copy.deepcopy(self.fixture.metadata)
        for key, changed in (
            ("driver", "nfs"),
            ("options", {"device": "/host"}),
            ("scope", "global"),
            ("labels", {}),
            ("created_at", "2026-10-14T00:00:00Z"),
            ("mountpoint", "/different/opaque/path"),
        ):
            self.fixture.metadata = {**copy.deepcopy(original), key: changed}
            self.fixture.calls.clear()
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "(metadata|receipt)_changed"),
            ):
                volumes.require(self.fixture.context)
            self.assertFalse(
                any(argv[:2] == ["docker", "run"] for argv in self.fixture.calls)
            )

    def test_bad_created_volume_is_rejected_before_root_helper(self):
        self.fixture.start_intent()
        self.fixture.create_mutation = lambda value: value.update(labels={})
        with self.assertRaisesRegex(ValueError, "metadata_changed"):
            volumes.ensure(self.fixture.context)
        self.assertFalse(
            any(argv[:2] == ["docker", "run"] for argv in self.fixture.calls)
        )
        self.assertFalse((self.fixture.root / "volume.json").exists())

    def test_ownership_change_fails_readonly_probe_without_chown(self):
        self.fixture.start_intent()
        volumes.ensure(self.fixture.context)
        self.fixture.ownership["mode"] = 0o755
        self.fixture.calls.clear()
        with self.assertRaisesRegex(ValueError, "ownership_changed"):
            volumes.require(self.fixture.context)
        self.assertFalse(any("--cap-add" in argv for argv in self.fixture.calls))

    def test_tampered_receipts_and_extra_image_volumes_fail_closed(self):
        self.fixture.start_intent()
        volumes.ensure(self.fixture.context)
        receipt = execution.read_record(self.fixture.root, "volume.json")
        receipt["schema_version"] = True
        execution.write_record(self.fixture.root, "volume.json", receipt, replace=True)
        with self.assertRaisesRegex(ValueError, "receipt_changed"):
            volumes.require(self.fixture.context)
        self.fixture.inspect_image.return_value["Config"]["Volumes"] = {
            "/unexpected": {}
        }
        with self.assertRaisesRegex(ValueError, "extra_volumes"):
            volumes.require(self.fixture.context)

    def test_missing_or_malformed_volume_never_falls_back_to_bind(self):
        original = copy.deepcopy(self.fixture.context["compose"])
        for change in (
            lambda compose, mount: compose.pop("volumes"),
            lambda compose, mount: compose["volumes"].clear(),
            lambda compose, mount: compose["volumes"].update(other={}),
            lambda compose, mount: compose["services"].clear(),
            lambda compose, mount: mount.update(type="bind"),
            lambda compose, mount: mount.update(source="other-volume"),
            lambda compose, mount: mount.update(read_only=True),
            lambda compose, mount: mount.update(read_only=1),
            lambda compose, mount: mount.update(volume={"nocopy": 1}),
            lambda compose, mount: mount.update(
                volume={"nocopy": True, "subpath": "other"}
            ),
            lambda compose, mount: mount.update(unexpected=True),
            lambda compose, mount: compose["services"]["opensearch-search"][
                "volumes"
            ].append(copy.deepcopy(mount)),
            lambda compose, mount: compose["services"]["opensearch-search"][
                "volumes"
            ].clear(),
        ):
            compose = copy.deepcopy(original)
            mount = compose["services"]["opensearch-search"]["volumes"][0]
            change(compose, mount)
            self.fixture.context["compose"] = compose
            for verify in (volumes.ensure, volumes.require, execution.require_storage):
                with self.subTest(compose=compose, verify=verify.__name__):
                    with self.assertRaisesRegex(ValueError, "profile_changed"):
                        verify(self.fixture.context)
            self.assertEqual([], self.fixture.calls)
            self.fixture.image.assert_not_called()

    def test_explicit_readwrite_mount_matches_compose_default(self):
        kwargs = {
            "compose": self.fixture.context["compose"],
            "work_root": self.fixture.root,
            "data_root": self.fixture.data,
            "uid": os.geteuid(),
            "gid": os.getegid(),
        }
        catalog = execution.catalog_identity(self.fixture.context)
        implicit = volume_policy.identity(catalog, **kwargs)
        kwargs["compose"]["services"]["opensearch-search"]["volumes"][0][
            "read_only"
        ] = False
        self.assertEqual(implicit, volume_policy.identity(catalog, **kwargs))

    def test_security_failure_precedes_all_volume_commands(self):
        self.fixture.start_intent()
        self.fixture.security.side_effect = ValueError("changed security material")
        with self.assertRaisesRegex(ValueError, "changed security material"):
            volumes.ensure(self.fixture.context)
        self.assertEqual([], self.fixture.calls)

    def test_root_operator_is_rejected_before_any_mutation(self):
        for name in ("geteuid", "getegid"):
            with patch.object(volumes.os, name, return_value=0):
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(ValueError, "operator_must_be_nonroot"),
                ):
                    volumes.ensure(self.fixture.context)
        self.assertEqual([], self.fixture.calls)
        self.fixture.image.assert_not_called()

    def test_pure_projection_matches_sealed_lifecycle_receipts_without_execution(self):
        self.fixture.start_intent()
        receipt = volumes.ensure(self.fixture.context)
        self.fixture.calls.clear()
        self.fixture.image.reset_mock()
        identity = volume_policy.identity(
            execution.catalog_identity(self.fixture.context),
            compose=self.fixture.context["compose"],
            work_root=self.fixture.root,
            data_root=self.fixture.data,
            uid=os.geteuid(),
            gid=os.getegid(),
        )
        self.assertEqual(
            execution.digest_record(
                {
                    "work_root": str(self.fixture.root),
                    "data_root": str(self.fixture.data),
                }
            ),
            identity["roots_sha256"],
        )
        self.assertEqual(
            execution.read_record(self.fixture.root, "volume-started.json"),
            volume_policy.started(identity, IMAGE),
        )
        metadata = volume_policy.inspect_metadata(self.fixture.metadata, identity)
        self.assertEqual(
            receipt, volume_policy.expected_receipt(identity, IMAGE, metadata)
        )
        self.assertEqual([], self.fixture.calls)
        self.fixture.image.assert_not_called()
        metadata["labels"]["unexpected"] = "changed after validation"
        self.assertNotIn("unexpected", receipt["metadata"]["labels"])

    def test_pure_policy_has_no_execution_or_filesystem_dependencies(self):
        tree = ast.parse(Path(volume_policy.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertLessEqual(
            imported,
            {
                "__future__",
                "copy",
                "hashlib",
                "json",
                "stat",
                "search.operations.catalog.policy",
            },
        )

    def test_pure_identity_and_helper_reject_boolean_numeric_and_mutable_inputs(self):
        kwargs = {
            "compose": self.fixture.context["compose"],
            "work_root": self.fixture.root,
            "data_root": self.fixture.data,
            "uid": os.geteuid(),
            "gid": os.getegid(),
        }
        catalog = execution.catalog_identity(self.fixture.context)
        for key in ("uid", "gid"):
            for value in (True, 0, -1, 2.0):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(ValueError, "must_be_nonroot"),
                ):
                    volume_policy.identity(catalog, **{**kwargs, key: value})
        identity = volume_policy.identity(catalog, **kwargs)
        for value in ("mutable:tag", True, "sha256:short"):
            with (
                self.subTest(image=value),
                self.assertRaisesRegex(ValueError, "helper_image_invalid"),
            ):
                volume_policy.started(identity, value)
        self.fixture.context["compose"]["volumes"][volume_policy.ALIAS]["external"] = 1
        with self.assertRaisesRegex(ValueError, "profile_changed"):
            volume_policy.identity(catalog, **kwargs)


class CatalogVolumeFixtureTests(unittest.TestCase):
    def temporary_factory(self, *, cleanup_failure=None):
        temporary_type = tempfile.TemporaryDirectory
        allocations = []

        def create(*args, **kwargs):
            temporary = temporary_type(*args, **kwargs)
            allocations.append(temporary)
            # The outer test retains a real cleanup when setup or the injected
            # parent cleanup fails, independently of the fixture under test.
            self.addCleanup(temporary.cleanup)
            if cleanup_failure is not None:
                temporary.cleanup = Mock(side_effect=cleanup_failure)
            return temporary

        return allocations, create

    @staticmethod
    def parent_case():
        class Parent(unittest.TestCase):
            def setUp(self):
                self.fixture = CatalogVolumeFixture(self)

            def runTest(self):
                pass

        return Parent()

    def assert_parent_error(self, parent, result, failure):
        self.assertEqual(1, result.testsRun)
        self.assertEqual([], result.failures)
        self.assertEqual(1, len(result.errors))
        self.assertIs(parent, result.errors[0][0])
        self.assertIn(str(failure), result.errors[0][1])
        self.assertFalse(result.wasSuccessful())

    def test_setup_failure_cleans_the_parent_owned_temporary(self):
        allocations, factory = self.temporary_factory()
        failure = OSError("injected volume setup failure")
        parent = self.parent_case()
        result = unittest.TestResult()
        with (
            patch.object(
                volume_fixtures.tempfile, "TemporaryDirectory", side_effect=factory
            ),
            patch.object(volume_fixtures.Path, "mkdir", side_effect=failure),
        ):
            parent.run(result)
        self.assert_parent_error(parent, result, failure)
        self.assertEqual(1, len(allocations))
        self.assertFalse(Path(allocations[0].name).exists())

    def test_partial_setup_failure_restores_parent_owned_patches(self):
        allocations, factory = self.temporary_factory()
        failure = OSError("injected volume patch setup failure")
        original_patch = CatalogVolumeFixture.patched
        original_security = execution.verify_security_material
        original_image = volumes.administration._admin_image

        def fail_inspection(fixture, owner, name, **options):
            if owner is execution and name == "inspect_image":
                raise failure
            return original_patch(fixture, owner, name, **options)

        parent = self.parent_case()
        result = unittest.TestResult()
        with (
            patch.object(
                volume_fixtures.tempfile, "TemporaryDirectory", side_effect=factory
            ),
            patch.object(
                CatalogVolumeFixture,
                "patched",
                autospec=True,
                side_effect=fail_inspection,
            ),
        ):
            parent.run(result)
        self.assert_parent_error(parent, result, failure)
        self.assertIs(original_security, execution.verify_security_material)
        self.assertIs(original_image, volumes.administration._admin_image)
        self.assertEqual(1, len(allocations))
        self.assertFalse(Path(allocations[0].name).exists())

    def test_cleanup_failure_is_reported_in_the_real_parent_result(self):
        failure = OSError("injected volume cleanup failure")
        allocations, factory = self.temporary_factory(cleanup_failure=failure)
        original_security = execution.verify_security_material
        parent = self.parent_case()
        result = unittest.TestResult()
        with patch.object(
            volume_fixtures.tempfile, "TemporaryDirectory", side_effect=factory
        ):
            parent.run(result)
        self.assert_parent_error(parent, result, failure)
        self.assertIs(parent, parent.fixture.test)
        self.assertIs(original_security, execution.verify_security_material)
        self.assertEqual(1, len(allocations))
        allocations[0].cleanup.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
