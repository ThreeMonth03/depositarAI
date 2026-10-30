"""Test image-owned import smoke boundaries using only a fake Docker binary."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/ci/runtime-imports.sh"
IMAGE = "depositar-runtime:unit-test"
ROLE_MODULES = {
    "conversation": (
        "search.api.conversation.app",
        "search.api.conversation.worker",
        "search.api.retrieval.engine",
        "search.api.retrieval.worker",
        "search.api.retrieval.pipeline",
        "search.operations.shared.runtime_probe",
    ),
    "web": (
        "search.web.app",
        "search.web.sessions",
        "search.web.upstream",
    ),
    "reranker": ("search.reranker.server",),
    "catalog-admin": ("search.catalog_admin",),
}
CI_GATES = {
    "scripts/test-search-api.sh": (
        ("depositar-search-conversation:ci", "conversation"),
        ("depositar-search-catalog-admin:ci", "catalog-admin"),
    ),
    "scripts/test-search-web.sh": (
        ("depositar-search-conversation:ci", "conversation"),
        ("depositar-search-web:ci", "web"),
    ),
}


class RuntimeImportGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="runtime-import-gate-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.call_log = self.root / "docker-calls.jsonl"
        fake_docker = self.bin / "docker"
        fake_docker.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['DEPOSITAR_IMPORT_GATE_CALL_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('fake Docker: imports accepted')\n"
            "sys.exit(int(os.environ.get('DEPOSITAR_IMPORT_GATE_EXIT_CODE', '0')))\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o700)
        self.environment = {
            **os.environ,
            "PATH": str(self.bin) + ":" + os.environ["PATH"],
            "DEPOSITAR_IMPORT_GATE_CALL_LOG": str(self.call_log),
        }

    def run_gate(self, *arguments: str, **environment: str):
        return subprocess.run(
            ["bash", str(SCRIPT), *arguments],
            cwd=ROOT,
            env={**self.environment, **environment},
            text=True,
            capture_output=True,
            timeout=10,
        )

    def calls(self) -> list[list[str]]:
        if not self.call_log.exists():
            return []
        return [json.loads(line) for line in self.call_log.read_text().splitlines()]

    def option(self, arguments: list[str], name: str) -> str:
        self.assertEqual(1, arguments.count(name), arguments)
        index = arguments.index(name)
        self.assertLess(index + 1, len(arguments))
        return arguments[index + 1]

    def payload(self, role: str):
        previous_calls = len(self.calls())
        result = self.run_gate(IMAGE, role)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("fake Docker: imports accepted\n", result.stdout)
        self.assertEqual("", result.stderr)
        calls = self.calls()
        self.assertEqual(previous_calls + 1, len(calls))
        arguments = calls[-1]
        image_index = arguments.index(IMAGE)
        self.assertEqual(["-I", "-B", "-c"], arguments[image_index + 1 : -2])
        self.assertEqual(role, arguments[-1])
        return arguments, ast.parse(arguments[-2])

    def test_current_roles_use_image_source_and_bounded_cpu_only_containers(
        self,
    ) -> None:
        for role in ROLE_MODULES:
            with self.subTest(role=role):
                arguments, _ = self.payload(role)
                options = arguments[: arguments.index(IMAGE)]
                self.assertEqual("run", options[0])
                self.assertIn("--rm", options)
                self.assertIn("--read-only", options)
                self.assertEqual("runc", self.option(options, "--runtime"))
                self.assertEqual("none", self.option(options, "--network"))
                self.assertEqual("ALL", self.option(options, "--cap-drop"))
                self.assertEqual(
                    "no-new-privileges", self.option(options, "--security-opt")
                )
                self.assertEqual(
                    f"{os.geteuid()}:{os.getegid()}", self.option(options, "--user")
                )
                self.assertEqual("2", self.option(options, "--cpus"))
                self.assertEqual("2g", self.option(options, "--memory"))
                self.assertEqual("96", self.option(options, "--pids-limit"))
                self.assertEqual(
                    "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
                    self.option(options, "--tmpfs"),
                )
                self.assertEqual(
                    "/opt/conda/bin/python"
                    if role == "reranker"
                    else "/usr/local/bin/python",
                    self.option(options, "--entrypoint"),
                )
                forbidden = {
                    "--gpus",
                    "--device",
                    "--device-cgroup-rule",
                    "--privileged",
                    "--cap-add",
                    "--mount",
                    "--volume",
                    "--volumes-from",
                    "-v",
                    "--publish",
                    "--publish-all",
                    "-p",
                    "-P",
                    "--pid",
                    "--ipc",
                    "--env",
                    "--env-file",
                    "-e",
                }
                self.assertFalse(
                    any(option.split("=", 1)[0] in forbidden for option in options)
                )
                self.assertNotIn("/app/search", options)
                self.assertFalse(any("docker.sock" in option for option in options))

    def test_missing_extra_invalid_role_or_option_image_stops_before_docker(
        self,
    ) -> None:
        for arguments in (
            (),
            (IMAGE,),
            (IMAGE, "conversation", "extra"),
            (IMAGE, "unknown"),
            (IMAGE, "api"),
            (IMAGE, "filtered"),
            (IMAGE, "API"),
            ("", "conversation"),
            ("--privileged", "conversation"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_gate(*arguments)
                self.assertEqual(64, result.returncode)
                self.assertNotEqual("", result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual([], self.calls())

    def test_docker_failure_exit_codes_are_preserved(self) -> None:
        for code in (1, 23, 125):
            with self.subTest(exit_code=code):
                previous_calls = len(self.calls())
                result = self.run_gate(
                    IMAGE, "conversation", DEPOSITAR_IMPORT_GATE_EXIT_CODE=str(code)
                )
                self.assertEqual(code, result.returncode)
                self.assertEqual(previous_calls + 1, len(self.calls()))

    def test_copied_offline_code_is_rejected_without_importing_it(self):
        _, tree = self.payload("conversation")
        guard = next(
            statement
            for statement in tree.body
            if isinstance(statement, ast.For)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "relative"
            and isinstance(statement.iter, ast.Tuple)
        )
        program = compile(
            ast.Module(body=[guard], type_ignores=[]), "runtime-import-policy", "exec"
        )
        search_root = self.root / "image/search"
        search_root.mkdir(parents=True)
        for relative in ("operations/documents", "quality"):
            path = search_root / relative
            path.mkdir(parents=True)
            with (
                self.subTest(relative=relative),
                self.assertRaisesRegex(AssertionError, "offline code in runtime image"),
            ):
                exec(program, {"search_root": search_root})
            path.rmdir()
        (search_root / "operations/shared").mkdir()
        exec(program, {"search_root": search_root})

    def test_loaded_resource_batching_is_rejected_but_shared_probe_is_allowed(self):
        _, tree = self.payload("conversation")
        guard = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assert)
            and isinstance(node.msg, ast.BinOp)
            and isinstance(node.msg.left, ast.Constant)
            and node.msg.left.value == "offline implementation loaded: "
        )
        program = compile(
            ast.Module(body=[guard], type_ignores=[]), "runtime-import-policy", "exec"
        )
        for relative in (
            "operations/documents/__init__.py",
            "operations/documents/batches.py",
            "operations/documents/sets.py",
            "operations/jobs/__init__.py",
            "operations/jobs/queue.py",
            "operations/jobs/pueue.py",
            "operations/jobs/sources.py",
        ):
            with (
                self.subTest(relative=relative),
                self.assertRaisesRegex(AssertionError, "offline implementation loaded"),
            ):
                exec(program, {"relative": relative, "name": "resource_batching"})
        exec(
            program,
            {
                "relative": "operations/shared/runtime_probe.py",
                "name": "search.operations.shared.runtime_probe",
            },
        )

    def test_embedded_python_has_exact_roles_fixed_package_roots_and_no_startup_calls(
        self,
    ) -> None:
        _, tree = self.payload("conversation")
        declared_roles = [
            statement.value
            for statement in tree.body
            if isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "roles"
                for target in statement.targets
            )
        ]
        self.assertEqual(1, len(declared_roles))
        self.assertEqual(ROLE_MODULES, ast.literal_eval(declared_roles[0]))
        inserts = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
            and node.func.value.attr == "path"
            and node.func.attr == "insert"
        ]
        self.assertEqual(1, len(inserts))
        self.assertEqual(
            ["0", "package_root"], [ast.unparse(arg) for arg in inserts[0].args]
        )
        root_values = [
            statement.value
            for statement in tree.body
            if isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "package_root"
                for target in statement.targets
            )
        ]
        self.assertEqual(1, len(root_values))
        self.assertEqual(
            ast.dump(
                ast.parse(
                    '"/opt/depositar-search" if role == "catalog-admin" else "/app"',
                    mode="eval",
                ).body
            ),
            ast.dump(root_values[0]),
        )
        forbidden_calls = {
            "create_app",
            "startup",
            "start_models",
            "load_model",
            "from_pretrained",
            "Ranker",
            "probe_models",
            "load_config",
            "from_environment",
            "read_secret",
            "Popen",
            "run",
            "system",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                self.assertNotIn(name, forbidden_calls)
        self.assertFalse(
            any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for node in tree.body
            )
        )

    def test_product_ci_builds_gate_the_image_before_source_bind_tests(
        self,
    ) -> None:
        for relative, gates in CI_GATES.items():
            source = (ROOT / relative).read_text()
            for image, role in gates:
                with self.subTest(script=relative, role=role):
                    command = (
                        "bash scripts/ci/runtime-imports.sh "
                        + shlex.quote(image)
                        + " "
                        + shlex.quote(role)
                    )
                    self.assertEqual(1, source.splitlines().count(command))
                    before_gate = source[: source.index(command)]
                    self.assertIn(" -t " + image + " .", before_gate)
                    after_gate = source[source.index(command) + len(command) :]
                    self.assertIn("docker run", after_gate)


if __name__ == "__main__":
    unittest.main()
