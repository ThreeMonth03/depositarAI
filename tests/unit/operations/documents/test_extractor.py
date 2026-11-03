"""Protect bounded extraction, source identity and per-resource output
semantics."""

import copy
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

from search.operations.documents import controller, extractor


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
        self.snapshot_stat = None
        self.on_run = None
        self.run_error = None

    def __call__(self, argv, *, timeout, environment):
        self.calls.append((list(argv), timeout, environment))
        self.assert_isolated_client(argv, environment)
        arguments = argv[5:]
        if arguments[:2] == ["image", "inspect"]:
            if not self.exists:
                return subprocess.CompletedProcess(
                    argv, 1, b"", b"No such image"
                )
            metadata = {
                "Id": self.image["image_id"],
                "Config": {
                    "Labels": {
                        "org.depositarai.extractor.source-sha256": self.image[
                            "source_sha256"
                        ],
                        "org.depositarai.extractor.base": self.image[
                            "base_image"
                        ],
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
            self.snapshot_stat = snapshot.stat()
            if self.on_run:
                self.on_run()
            value = (
                report(self.snapshot_bytes)
                if self.output is None
                else self.output
            )
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
        assert json.loads((Path(argv[4]) / "config.json").read_text()) == {
            "auths": {}
        }
        assert set(environment) == {"PATH", "LANG", "LC_ALL"}


class ReportContractTests(unittest.TestCase):
    def test_limit_boundaries_remain_usable_without_normalization(self):
        for changes in (
            {"text": "x" * 65536},
            {"columns": ["x" * 512]},
            {"columns": [""] * 256},
            {"columns": ["x" * 512] * 16},
            {"rows_observed": 0},
            {"rows_observed": 10000},
            {"rows_observed": None},
        ):
            with self.subTest(changes=repr(changes)[:80]):
                value = report()
                value.update(changes)
                self.assertEqual(value, extractor.validate_report(value))
        for changes in (
            {"columns": [""] * 257},
            {"columns": ["x" * 512] * 16 + ["x"]},
        ):
            with self.subTest(changes=repr(changes)[:80]):
                value = report()
                value.update(changes)
                with self.assertRaisesRegex(
                    extractor.ExtractorError,
                    "^document_extractor_report_invalid$",
                ):
                    extractor.validate_report(value)

    def test_report_types_reject_subclasses_as_well_as_bool_integers(self):
        class String(str):
            pass

        class Integer(int):
            pass

        class List(list):
            pass

        class Dict(dict):
            pass

        with self.assertRaises(extractor.ExtractorError):
            extractor.validate_report(Dict(report()))
        for field, changed in (
            ("schema_version", Integer(1)),
            ("status", String("ok")),
            ("detected_mime", String("text/csv")),
            ("input_sha256", String("a" * 64)),
            ("columns", List(["station"])),
            ("columns", [String("station")]),
            ("text", String("station")),
            ("rows_observed", Integer(1)),
            ("rows_observed", True),
        ):
            with self.subTest(field=field, type=type(changed).__name__):
                value = report()
                value[field] = changed
                with self.assertRaisesRegex(
                    extractor.ExtractorError,
                    "^document_extractor_report_invalid$",
                ):
                    extractor.validate_report(value)

    def test_return_copies_containers_and_preserves_input_bytes_and_order(
        self,
    ):
        value = dict(reversed(list(report().items())))
        value.update(text=" \n\u6e2c\u7ad9 ", columns=[" station "])
        before = copy.deepcopy(value)
        result = extractor.validate_report(value)
        self.assertEqual(before, result)
        self.assertEqual(list(value), list(result))
        self.assertIsNot(value, result)
        self.assertIsNot(value["columns"], result["columns"])
        result["text"] = "changed"
        result["columns"].append("changed")
        self.assertEqual(before, value)

    def test_earlier_invalid_fields_prevent_later_string_validation(self):
        for changes, settings, expected in (
            ({"detected_mime": "\ud800"}, {"expected_format": []}, []),
            ({"schema_version": True, "detected_mime": "\ud800"}, {}, []),
            (
                {"input_sha256": "bad", "columns": ["\ud800"]},
                {},
                [call("text/csv", 128)],
            ),
            (
                {"columns": ["\ud800"], "text": "\ud800"},
                {},
                [call("text/csv", 128), call("\ud800", 512)],
            ),
        ):
            value = report()
            value.update(changes)
            with (
                self.subTest(changes=changes, settings=settings),
                patch.object(
                    extractor, "_string", wraps=extractor._string
                ) as validate_string,
                self.assertRaisesRegex(
                    extractor.ExtractorError,
                    "^document_extractor_report_invalid$",
                ),
            ):
                extractor.validate_report(value, **settings)
            self.assertEqual(expected, validate_string.call_args_list)

    def test_invalid_unicode_has_a_fixed_error_and_suppressed_context(self):
        for field, changed in (
            ("detected_mime", "\ud800"),
            ("columns", ["\ud800"]),
            ("text", "\ud800"),
        ):
            value = report()
            value[field] = changed
            with self.subTest(field=field):
                with self.assertRaises(extractor.ExtractorError) as failure:
                    extractor.validate_report(value)
                error = failure.exception
                self.assertEqual(
                    "document_extractor_report_invalid", str(error)
                )
                self.assertIsNone(error.__cause__)
                self.assertIsInstance(error.__context__, UnicodeEncodeError)
                self.assertTrue(error.__suppress_context__)

    def test_empty_and_error_reports_keep_their_existing_format_exemptions(
        self,
    ):
        for changes, settings in (
            ({"detected_mime": "text/plain"}, {"expected_format": "CSV"}),
            (
                {
                    "status": "empty_text",
                    "columns": [],
                    "text": " \n",
                    "text_truncated": True,
                    "rows_observed": None,
                },
                {"expected_format": "CSV", "header_policy": "absent"},
            ),
            (
                {
                    "status": "parse_error",
                    "detected_mime": "",
                    "columns": [],
                    "text": "",
                    "rows_observed": None,
                },
                {"expected_format": "PDF"},
            ),
        ):
            value = report()
            value.update(changes)
            with self.subTest(changes=changes, settings=settings):
                self.assertEqual(
                    value, extractor.validate_report(value, **settings)
                )

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
        value.update(
            status="parse_error", text="", columns=[], rows_observed=None
        )
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
        self.assertEqual(
            value, extractor.validate_report(value, expected_format="CSV")
        )
        value["detected_mime"] += "; version=4"
        with self.assertRaises(extractor.ExtractorError):
            extractor.validate_report(value, expected_format="CSV")


class OperatorIdentityTests(unittest.TestCase):
    def test_each_root_identity_is_rejected(self):
        identities = {
            "getuid": 1001,
            "getgid": 1002,
            "geteuid": 1003,
            "getegid": 1004,
        }
        for zero in (*identities, "all"):
            values = {
                name: (
                    lambda value=0 if zero in (name, "all") else value: value
                )
                for name, value in identities.items()
            }
            with (
                self.subTest(zero=zero),
                patch.multiple(extractor.os, **values),
            ):
                with self.assertRaisesRegex(
                    extractor.ExtractorError,
                    "^document_extractor_operator_must_be_nonroot$",
                ):
                    extractor.operator_ids()

    def test_nonroot_operator_uses_effective_ids(self):
        for real, effective in (
            ((1001, 1002), (1001, 1002)),
            ((1001, 1002), (1003, 1004)),
        ):
            with (
                self.subTest(real=real, effective=effective),
                patch.object(extractor.os, "getuid", return_value=real[0]),
                patch.object(extractor.os, "getgid", return_value=real[1]),
                patch.object(
                    extractor.os, "geteuid", return_value=effective[0]
                ),
                patch.object(
                    extractor.os, "getegid", return_value=effective[1]
                ),
            ):
                self.assertEqual(effective, extractor.operator_ids())


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
        docker.on_run = lambda: self.path.write_bytes(
            b"changed after snapshot"
        )
        result = extractor.extract(
            self.path, "CSV", image=IMAGE, runner=docker
        )
        self.assertEqual(
            hashlib.sha256(self.raw).hexdigest(), result["input_sha256"]
        )
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
            ("--user", f"{os.geteuid()}:{os.getegid()}"),
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
        self.assertEqual(
            ["rm", "--force", run[run.index("--name") + 1]], commands[-1]
        )

    def test_root_is_rejected_before_input_or_client_access(self):
        identities = {
            "getuid": 1001,
            "getgid": 1002,
            "geteuid": 1003,
            "getegid": 1004,
        }
        for zero in (*identities, "all"):
            docker = FakeDocker()
            values = {
                name: (
                    lambda value=0 if zero in (name, "all") else value: value
                )
                for name, value in identities.items()
            }
            with (
                self.subTest(zero=zero),
                patch.multiple(extractor.os, **values),
                patch.object(extractor, "_input_bytes") as read,
                patch.object(extractor, "_client") as client,
                self.assertRaisesRegex(
                    extractor.ExtractorError,
                    "^document_extractor_operator_must_be_nonroot$",
                ),
            ):
                extractor.extract(self.path, "CSV", image=IMAGE, runner=docker)
            read.assert_not_called()
            client.assert_not_called()
            self.assertEqual([], docker.calls)
            self.assertEqual(self.raw, self.path.read_bytes())

    def test_run_uses_one_validated_effective_pair_for_private_snapshot(self):
        effective = os.geteuid(), os.getegid()
        docker = FakeDocker()
        with (
            patch.object(
                extractor, "operator_ids", wraps=extractor.operator_ids
            ) as identity,
            patch.object(
                extractor.os, "getuid", return_value=effective[0] + 1
            ),
            patch.object(
                extractor.os, "getgid", return_value=effective[1] + 1
            ),
        ):
            result = extractor.extract(
                self.path, "CSV", image=IMAGE, runner=docker
            )
        identity.assert_called_once_with()
        run = next(call[0] for call in docker.calls if call[0][5] == "run")
        self.assertEqual(
            f"{effective[0]}:{effective[1]}", run[run.index("--user") + 1]
        )
        self.assertEqual(self.raw, docker.snapshot_bytes)
        self.assertEqual(0o600, docker.snapshot_stat.st_mode & 0o777)
        self.assertEqual(effective[0], docker.snapshot_stat.st_uid)
        self.assertEqual(
            hashlib.sha256(self.raw).hexdigest(), result["input_sha256"]
        )

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
        for invalid in (
            "",
            "foreign-service",
            "depositarai-extract-" + "D" * 32,
        ):
            with (
                self.subTest(name=invalid),
                self.assertRaisesRegex(
                    extractor.ExtractorError, "settings_invalid"
                ),
            ):
                extractor.extract(
                    self.path,
                    "CSV",
                    image=IMAGE,
                    container_name=invalid,
                    runner=docker,
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
                    extractor.ExtractorError,
                    "^document_extractor_inspect_failed$",
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
                    extractor.extract(
                        self.path, "CSV", image=IMAGE, runner=docker
                    )
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

    def test_input_links_permissions_and_wrong_image_are_rejected_before_run(
        self,
    ):
        alias = self.path.with_name("alias.csv")
        alias.symlink_to(self.path)
        for path in (alias,):
            with self.assertRaisesRegex(
                extractor.ExtractorError, "input_invalid"
            ):
                extractor.extract(
                    path, "CSV", image=IMAGE, runner=FakeDocker()
                )
        hardlink = self.path.with_name("hardlink.csv")
        os.link(self.path, hardlink)
        with self.assertRaisesRegex(extractor.ExtractorError, "input_invalid"):
            extractor.extract(
                self.path, "CSV", image=IMAGE, runner=FakeDocker()
            )
        hardlink.unlink()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(extractor.ExtractorError, "input_invalid"):
            extractor.extract(
                self.path, "CSV", image=IMAGE, runner=FakeDocker()
            )
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

    def test_each_recipe_file_byte_is_part_of_the_helper_identity(self):
        baseline = extractor.source_identity(self.root)
        for name in (
            "Dockerfile",
            "Dockerfile.dockerignore",
            "Main.java",
            "log4j2.xml",
        ):
            path = self.directory / name
            raw = path.read_bytes()
            with self.subTest(name=name):
                path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
                self.assertNotEqual(
                    baseline, extractor.source_identity(self.root)
                )
                path.write_bytes(raw)
                self.assertEqual(
                    baseline, extractor.source_identity(self.root)
                )

    def test_build_uses_only_snapshotted_recipe_and_reuses_matching_image(
        self,
    ):
        docker = FakeDocker()
        docker.exists = False
        first = extractor.build_image(self.root, runner=docker)
        builds = sum(call[0][5] == "build" for call in docker.calls)
        second = extractor.build_image(self.root, runner=docker)
        self.assertEqual(first, second)
        self.assertEqual(1, builds)
        self.assertEqual(
            builds, sum(call[0][5] == "build" for call in docker.calls)
        )
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


class ExtractionCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(
            self.stack.enter_context(tempfile.TemporaryDirectory())
        )
        self.raw = b"station,pH\nA,7.2\n"
        self.path = self.root / "input.csv"
        self.path.write_bytes(self.raw)
        self.path.chmod(0o600)
        self.acquired = {
            "sha256": hashlib.sha256(self.raw).hexdigest(),
            "local_path": self.path,
        }
        parent = self.stack.enter_context(
            controller._state(self.root / "state")
        )
        self.extractions = controller._child(parent, "extractions")
        self.stack.callback(os.close, self.extractions)

    def extract(self, *, cache_only=False):
        return controller._extraction(
            self.acquired,
            IMAGE,
            "CSV",
            "utf-8",
            "first_record",
            self.extractions,
            "depositarai-extract-" + "c" * 32,
            cache_only=cache_only,
        )

    def test_cache_hit_revalidates_without_running_or_rewriting_parser(self):
        value = report(self.raw)
        with patch.object(extractor, "extract", return_value=value) as parse:
            first, hit = self.extract()
            self.assertFalse(hit)
            path = next((self.root / "state/extractions").iterdir())
            before = path.read_bytes()
            second, hit = self.extract(cache_only=True)
        self.assertTrue(hit)
        self.assertEqual(first, second)
        self.assertEqual(value, second)
        self.assertEqual(before, path.read_bytes())
        parse.assert_called_once_with(
            self.path,
            "CSV",
            "utf-8",
            "first_record",
            image=IMAGE,
            container_name="depositarai-extract-" + "c" * 32,
        )

    def test_invalid_cached_report_refuses_without_parser_or_replacement(self):
        with patch.object(extractor, "extract", return_value=report(self.raw)):
            self.extract()
        path = next((self.root / "state/extractions").iterdir())
        record = json.loads(path.read_bytes())
        record["result"]["rows_observed"] = True
        damaged = json.dumps(record).encode()
        path.write_bytes(damaged)
        with (
            patch.object(extractor, "extract") as parse,
            self.assertRaisesRegex(
                extractor.ExtractorError,
                "^document_extractor_report_invalid$",
            ),
        ):
            self.extract()
        parse.assert_not_called()
        self.assertEqual(damaged, path.read_bytes())

    def test_cache_only_miss_has_no_parser_call_or_publication(self):
        with (
            patch.object(extractor, "extract") as parse,
            self.assertRaisesRegex(
                controller.ProfileError,
                "^document_extraction_cache_missing$",
            ),
        ):
            self.extract(cache_only=True)
        parse.assert_not_called()
        self.assertEqual([], list((self.root / "state/extractions").iterdir()))


if __name__ == "__main__":
    unittest.main()
