"""Protect bounded extraction, source identity and per-resource output semantics."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from search.operations.documents import extractor


IMAGE = {
    "image_id": "sha256:" + "b" * 64,
    "source_sha256": "a" * 64,
    "base_image": extractor.FESS_IMAGE,
}


def report(raw=b"station,pH\nA,7.2\n"):
    return {
        "schema_version": 1,
        "status": "ok",
        "detected_mime": "text/csv",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "columns": ["station", "pH"],
        "text": "station: A pH: 7.2",
        "text_truncated": False,
        "rows_observed": 1,
        "rows_complete": False,
        "may_require_ocr": False,
    }


class FakeDocker:
    def __init__(self, image=None, output=None, raw_output=None):
        self.image = copy.deepcopy(IMAGE if image is None else image)
        self.output = output
        self.raw_output = raw_output
        self.calls = []
        self.exists = True
        self.build_context_files = None
        self.snapshot_bytes = None
        self.on_run = None
        self.run_error = None

    def __call__(self, argv, *, timeout, environment):
        self.calls.append((list(argv), timeout, environment))
        self.assert_isolated_client(argv, environment)
        arguments = argv[5:]
        if arguments[:2] == ["image", "inspect"]:
            if not self.exists:
                return subprocess.CompletedProcess(argv, 1, b"", b"No such image")
            metadata = {
                "Id": self.image["image_id"],
                "Config": {
                    "Labels": {
                        "org.depositarai.extractor.source-sha256": self.image[
                            "source_sha256"
                        ],
                        "org.depositarai.extractor.base": self.image["base_image"],
                    }
                },
            }
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(metadata).encode(), b""
            )
        if arguments[0] == "build":
            labels = {
                value.split("=", 1)[0]: value.split("=", 1)[1]
                for index, value in enumerate(arguments)
                if index and arguments[index - 1] == "--label"
            }
            self.image["source_sha256"] = labels[
                "org.depositarai.extractor.source-sha256"
            ]
            self.exists = True
            context = Path(arguments[-1])
            self.build_context_files = {
                str(path.relative_to(context))
                for path in context.rglob("*")
                if path.is_file()
            }
            return subprocess.CompletedProcess(argv, 0, b"built", b"")
        if arguments[0] == "run":
            if self.run_error:
                raise self.run_error
            mount = arguments[arguments.index("--mount") + 1]
            snapshot = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            self.snapshot_bytes = snapshot.read_bytes()
            if self.on_run:
                self.on_run()
            value = report(self.snapshot_bytes) if self.output is None else self.output
            encoded = (
                json.dumps(value).encode()
                if self.raw_output is None
                else self.raw_output
            )
            return subprocess.CompletedProcess(argv, 0, encoded, b"")
        if arguments[:2] == ["rm", "--force"]:
            return subprocess.CompletedProcess(argv, 0, b"removed", b"")
        raise AssertionError(arguments)

    @staticmethod
    def assert_isolated_client(argv, environment):
        assert argv[:4] == [
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "--config",
        ]
        assert json.loads((Path(argv[4]) / "config.json").read_text()) == {"auths": {}}
        assert set(environment) == {"PATH", "LANG", "LC_ALL"}


class ReportContractTests(unittest.TestCase):
    def test_cached_report_must_match_declared_format_and_header_policy(self):
        value = report()
        self.assertEqual(
            value,
            extractor.validate_report(
                value, expected_format="CSV", header_policy="first_record"
            ),
        )
        for context in (
            {"expected_format": "PDF"},
            {"header_policy": "absent"},
            {"expected_format": []},
            {"header_policy": []},
        ):
            with self.subTest(context=context):
                with self.assertRaises(extractor.ExtractorError):
                    extractor.validate_report(value, **context)

    def test_success_and_empty_pdf_have_distinct_semantics(self):
        value = report()
        self.assertEqual(value, extractor.validate_report(value))
        value.update(
            status="empty_text",
            detected_mime="application/pdf",
            columns=[],
            text="\n ",
            rows_observed=None,
            may_require_ocr=True,
        )
        self.assertEqual(value, extractor.validate_report(value))

    def test_malformed_values_never_become_usable_profile_records(self):
        mutations = {
            "schema_version": [True, 2, "1"],
            "status": [[], "success", "unknown"],
            "detected_mime": [True, "text/html", "text/csv\nAuthorization:x"],
            "input_sha256": [None, "A" * 64, "bad"],
            "columns": ["station", [None], ["a\x00b"], ["a" * 513]],
            "text": [True, "a" * 65537, "\ud800", "a\x00b"],
            "text_truncated": [0, "false"],
            "rows_observed": [True, -1, 10001, "1"],
            "rows_complete": [True, 0, None],
            "may_require_ocr": [1, True],
        }
        for field, values in mutations.items():
            for changed in values:
                with self.subTest(field=field, changed=repr(changed)[:50]):
                    value = report()
                    value[field] = changed
                    with self.assertRaises(extractor.ExtractorError):
                        extractor.validate_report(value)
        for field in ("extra", "missing"):
            value = report()
            if field == "extra":
                value["raw_exception"] = "private"
            else:
                del value["status"]
            with self.assertRaises(extractor.ExtractorError):
                extractor.validate_report(value)

    def test_aggregate_columns_and_false_success_are_rejected(self):
        value = report()
        value["columns"] = ["a" * 512] * 17
        with self.assertRaises(extractor.ExtractorError):
            extractor.validate_report(value)
        value = report()
        value.update(text="", columns=[])
        with self.assertRaises(extractor.ExtractorError):
            extractor.validate_report(value)
        value = report()
        value.update(status="parse_error", text="", columns=[], rows_observed=None)
        self.assertEqual(value, extractor.validate_report(value))
        value["text"] = "untrusted diagnostic"
        with self.assertRaises(extractor.ExtractorError):
            extractor.validate_report(value)

    def test_unsupported_archive_uses_a_parameter_free_mime(self):
        value = report()
        value.update(
            status="unsupported_format",
            detected_mime="application/x-rar-compressed",
            text="",
            columns=[],
            rows_observed=None,
        )
        self.assertEqual(value, extractor.validate_report(value, expected_format="CSV"))
        value["detected_mime"] += "; version=4"
        with self.assertRaises(extractor.ExtractorError):
            extractor.validate_report(value, expected_format="CSV")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "original.csv"
        self.raw = b"station,pH\nA,7.2\n"
        self.path.write_bytes(self.raw)
        self.path.chmod(0o600)

    def test_snapshot_run_is_cpu_only_and_container_is_removed(self):
        docker = FakeDocker()
        docker.on_run = lambda: self.path.write_bytes(b"changed after snapshot")
        result = extractor.extract(self.path, "CSV", image=IMAGE, runner=docker)
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), result["input_sha256"])
        self.assertEqual(self.raw, docker.snapshot_bytes)
        commands = [call[0][5:] for call in docker.calls]
        run = next(command for command in commands if command[0] == "run")
        for key, expected in (
            ("--pull", "never"),
            ("--runtime", "runc"),
            ("--network", "none"),
            ("--memory", "2g"),
            ("--cpus", "2"),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges"),
            ("--user", f"{os.getuid()}:{os.getgid()}"),
        ):
            self.assertEqual(expected, run[run.index(key) + 1])
        self.assertIn("--read-only", run)
        self.assertIn("NVIDIA_VISIBLE_DEVICES=void", run)
        self.assertIn("CUDA_VISIBLE_DEVICES=-1", run)
        self.assertEqual(1, run.count("--mount"))
        self.assertNotIn("--publish", run)
        self.assertEqual(IMAGE["image_id"], run[-5])
        self.assertEqual(
            ["CSV", "/input/resource.csv", "utf-8", "first_record"], run[-4:]
        )
        self.assertEqual(["rm", "--force", run[run.index("--name") + 1]], commands[-1])

    def test_attempt_name_is_used_for_run_and_exact_cleanup(self):
        docker = FakeDocker()
        name = "depositarai-extract-" + "d" * 32
        extractor.extract(
            self.path, "CSV", image=IMAGE, container_name=name, runner=docker
        )
        commands = [call[0][5:] for call in docker.calls]
        run = next(command for command in commands if command[0] == "run")
        self.assertEqual(run[run.index("--name") + 1], name)
        self.assertEqual(commands[-1], ["rm", "--force", name])
        for invalid in ("", "foreign-service", "depositarai-extract-" + "D" * 32):
            with (
                self.subTest(name=invalid),
                self.assertRaisesRegex(extractor.ExtractorError, "settings_invalid"),
            ):
                extractor.extract(
                    self.path, "CSV", image=IMAGE, container_name=invalid, runner=docker
                )

    def test_resume_inspects_only_exact_recorded_helper_and_fails_closed(self):
        name = "depositarai-extract-" + "d" * 32
        calls = []

        def runner(argv, *, timeout, environment):
            calls.append(argv[5:])
            return subprocess.CompletedProcess(
                argv, 0, ("/" + name + "\n").encode(), b""
            )

        self.assertTrue(extractor.helper_exists(name, runner=runner))
        self.assertEqual(
            calls, [["container", "inspect", "--format", "{{.Name}}", name]]
        )
        for code, output, expected in (
            (1, b"Error: No such container: " + name.encode(), False),
            (1, b"Cannot connect to Docker daemon: private details", None),
        ):

            def failed(argv, *, timeout, environment):
                return subprocess.CompletedProcess(argv, code, b"", output)

            if expected is None:
                with self.assertRaisesRegex(
                    extractor.ExtractorError, "^document_extractor_inspect_failed$"
                ):
                    extractor.helper_exists(name, runner=failed)
            else:
                self.assertFalse(extractor.helper_exists(name, runner=failed))

    def test_digest_or_mime_mismatch_and_duplicate_json_are_rejected(self):
        wrong_digest = report()
        wrong_digest["input_sha256"] = "0" * 64
        wrong_mime = report()
        wrong_mime.update(
            detected_mime="application/pdf", columns=[], rows_observed=None
        )
        cases = [
            FakeDocker(output=wrong_digest),
            FakeDocker(output=wrong_mime),
            FakeDocker(raw_output=b'{"status":"ok","status":"parse_error"}'),
        ]
        for docker in cases:
            with self.subTest(output=docker.output):
                with self.assertRaises(extractor.ExtractorError):
                    extractor.extract(self.path, "CSV", image=IMAGE, runner=docker)
                self.assertEqual("rm", docker.calls[-1][0][5])

    def test_timeout_cleans_container_and_hides_raw_process_diagnostics(self):
        docker = FakeDocker()
        docker.run_error = subprocess.TimeoutExpired("private credential", 30)
        with self.assertRaisesRegex(
            extractor.ExtractorError, "^document_extractor_command_timeout$"
        ):
            extractor.extract(self.path, "CSV", image=IMAGE, runner=docker)
        self.assertEqual("rm", docker.calls[-1][0][5])

    def test_oversized_output_is_bounded_and_still_cleans_container(self):
        docker = FakeDocker(raw_output=b"x" * (extractor.MAX_REPORT_BYTES + 1))
        with self.assertRaisesRegex(extractor.ExtractorError, "output_limit"):
            extractor.extract(self.path, "CSV", image=IMAGE, runner=docker)
        self.assertEqual("rm", docker.calls[-1][0][5])

    def test_input_links_permissions_and_wrong_image_are_rejected_before_run(self):
        alias = self.path.with_name("alias.csv")
        alias.symlink_to(self.path)
        for path in (alias,):
            with self.assertRaisesRegex(extractor.ExtractorError, "input_invalid"):
                extractor.extract(path, "CSV", image=IMAGE, runner=FakeDocker())
        hardlink = self.path.with_name("hardlink.csv")
        os.link(self.path, hardlink)
        with self.assertRaisesRegex(extractor.ExtractorError, "input_invalid"):
            extractor.extract(self.path, "CSV", image=IMAGE, runner=FakeDocker())
        hardlink.unlink()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(extractor.ExtractorError, "input_invalid"):
            extractor.extract(self.path, "CSV", image=IMAGE, runner=FakeDocker())
        self.path.chmod(0o600)
        changed = dict(IMAGE, source_sha256="c" * 64)
        docker = FakeDocker(image=changed)
        with self.assertRaisesRegex(extractor.ExtractorError, "image_invalid"):
            extractor.extract(self.path, "CSV", image=IMAGE, runner=docker)
        self.assertFalse(any(call[0][5] == "run" for call in docker.calls))


class BuildIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "deploy/docker/extractor"
        self.directory.mkdir(parents=True)
        for filename in (
            "Dockerfile",
            "Dockerfile.dockerignore",
            "Main.java",
            "log4j2.xml",
        ):
            path = self.directory / filename
            path.write_text(filename)
            path.chmod(0o644)

    def test_build_uses_only_snapshotted_recipe_and_reuses_matching_image(self):
        docker = FakeDocker()
        docker.exists = False
        first = extractor.build_image(self.root, runner=docker)
        builds = sum(call[0][5] == "build" for call in docker.calls)
        second = extractor.build_image(self.root, runner=docker)
        self.assertEqual(first, second)
        self.assertEqual(1, builds)
        self.assertEqual(builds, sum(call[0][5] == "build" for call in docker.calls))
        self.assertEqual(
            {
                "deploy/docker/extractor/" + name
                for name in (
                    "Dockerfile",
                    "Dockerfile.dockerignore",
                    "Main.java",
                    "log4j2.xml",
                )
            },
            docker.build_context_files,
        )
        build = next(call[0] for call in docker.calls if call[0][5] == "build")
        self.assertEqual("none", build[build.index("--network") + 1])
        (self.directory / "Main.java").write_text("changed source")
        docker.exists = False
        changed = extractor.build_image(self.root, runner=docker)
        self.assertNotEqual(first["source_sha256"], changed["source_sha256"])


if __name__ == "__main__":
    unittest.main()
