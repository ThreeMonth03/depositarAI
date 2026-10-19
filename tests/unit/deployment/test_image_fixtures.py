"""Keep image fixture ownership and cleanup errors on the real parent test."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.unit.deployment import image_fixtures


ROOT = Path(__file__).resolve().parents[3]


class ImageFixtureBoundaryTests(unittest.TestCase):
    def test_legacy_helpers_keep_the_same_canonical_objects(self):
        from tests.unit.deployment import (
            test_image_build,
            test_image_staging,
            test_source_bundle,
        )

        for module, names in (
            (test_source_bundle, ("blob", "fixture_sources", "fixture_tree")),
            (test_image_build, ("FakeDocker", "fixture_sources", "fixture_tree")),
            (test_image_staging, ("FakeDocker", "fixture_sources", "fixture_tree")),
        ):
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertIs(getattr(module, name), getattr(image_fixtures, name))

    def test_isolated_import_needs_no_test_cases_or_filesystem_and_process_io(self):
        script = """
import builtins
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError("fixture import performed filesystem or process IO")
subprocess.Popen = forbidden
subprocess.run = forbidden
tempfile.TemporaryDirectory = forbidden
tempfile.mkdtemp = forbidden
builtins.open = forbidden
io.open = forbidden
Path.open = forbidden
Path.mkdir = forbidden
os.open = forbidden
os.mkdir = forbidden
from tests.unit.deployment import image_fixtures
assert image_fixtures.ImageStagingFixture.__bases__ == (object,)
for name in sys.modules:
    assert name != "unittest" and not name.startswith("unittest."), name
    if name.startswith("tests."):
        assert not any(part.startswith("test_") for part in name.split(".")), name
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)


class ImageFixtureCleanupTests(unittest.TestCase):
    def temporary_factory(self, *, cleanup_failure=None):
        temporary_type = tempfile.TemporaryDirectory
        allocations = []

        def create(*args, **kwargs):
            temporary = temporary_type(*args, **kwargs)
            allocations.append(temporary)
            # Keep a strong reference and independent fallback for a broken
            # constructor or an intentionally failing cleanup callback.
            self.addCleanup(temporary.cleanup)
            if cleanup_failure is not None:
                temporary.cleanup = mock.Mock(side_effect=cleanup_failure)
            return temporary

        return allocations, create

    @staticmethod
    def parent_case():
        class Parent(unittest.TestCase):
            def setUp(self):
                self.fixture = image_fixtures.ImageStagingFixture(self)

            def runTest(self):
                pass

        return Parent()

    def assert_failed_setup_is_cleaned(self, target, attribute):
        allocations, factory = self.temporary_factory()
        failure = OSError("injected fixture setup failure")
        parent = self.parent_case()
        result = unittest.TestResult()
        with (
            mock.patch.object(
                image_fixtures.tempfile, "TemporaryDirectory", side_effect=factory
            ),
            mock.patch.object(target, attribute, side_effect=failure),
        ):
            parent.run(result)
        self.assertEqual(1, result.testsRun)
        self.assertEqual([], result.failures)
        self.assertEqual(1, len(result.errors))
        self.assertIs(parent, result.errors[0][0])
        self.assertIn(str(failure), result.errors[0][1])
        self.assertEqual(1, len(allocations))
        self.assertFalse(Path(allocations[0].name).exists())

    def test_git_setup_failure_still_removes_the_parent_owned_temporary(self):
        self.assert_failed_setup_is_cleaned(image_fixtures.ImageStagingFixture, "git")

    def test_plan_setup_failure_still_removes_the_parent_owned_temporary(self):
        self.assert_failed_setup_is_cleaned(
            image_fixtures.source_bundle, "plan_source_bundle"
        )

    def test_cleanup_failure_is_reported_in_the_real_parent_test_result(self):
        failure = OSError("injected fixture cleanup failure")
        allocations, factory = self.temporary_factory(cleanup_failure=failure)
        parent = self.parent_case()
        result = unittest.TestResult()
        with mock.patch.object(
            image_fixtures.tempfile, "TemporaryDirectory", side_effect=factory
        ):
            parent.run(result)
        self.assertEqual(1, result.testsRun)
        self.assertEqual([], result.failures)
        self.assertEqual(1, len(result.errors))
        self.assertIs(parent, result.errors[0][0])
        self.assertIn(str(failure), result.errors[0][1])
        self.assertFalse(result.wasSuccessful())
        self.assertIs(parent, parent.fixture.test)
        self.assertEqual(1, len(allocations))
        allocations[0].cleanup.assert_called_once_with()
