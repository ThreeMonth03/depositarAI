"""Exercise frontend admission boundaries with Git and Docker fixture commands."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tests.unit.deployment.frontend.test_check import Runner


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/search-web.sh"
GIT = """
import sys

arguments = sys.argv[1:]
if arguments == ["rev-parse", "HEAD"]:
    print("1" * 40)
elif arguments[:3] == ["diff", "--quiet", "HEAD"]:
    pass
elif arguments == ["archive", "1" * 40]:
    sys.stdout.buffer.write(b"fixture archive\\n")
else:
    raise SystemExit("unexpected Git fixture command")
"""
DOCKER = """
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["FRONTEND_FIXTURE_ROOT"])
arguments = sys.argv[1:]
with (root / "calls.jsonl").open("a", encoding="ascii") as stream:
    stream.write(json.dumps(arguments) + "\\n")

def change_admission(stage):
    if os.environ["FRONTEND_FIXTURE_FAULT"] != stage:
        return
    marker = root / (stage + ".injected")
    if marker.exists():
        return
    admission = root / "intranet-allow.conf"
    admission.chmod(0o600)
    admission.write_text("allow all;\\n", encoding="ascii")
    admission.chmod(0o444)
    marker.touch(mode=0o600)

if arguments[0] == "build":
    if sys.stdin.buffer.read() != b"fixture archive\\n":
        raise SystemExit("missing Git archive fixture")
    change_admission("build")
elif arguments[0] == "compose":
    if arguments[-3:] == ["config", "--format", "json"]:
        print((root / "compose.json").read_text(encoding="ascii"))
    elif arguments[-2:] == ["config", "--quiet"]:
        pass
    elif "stop" in arguments:
        change_admission("stop")
    elif "up" in arguments or arguments[-1] in ("down", "ps"):
        pass
    else:
        raise SystemExit("unexpected Compose fixture command")
else:
    raise SystemExit("unexpected Docker fixture command")
"""


class FrontendWrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frontend-wrapper-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        (self.bin / "python3").symlink_to(sys.executable)
        for name, program in (("git", GIT), ("docker", DOCKER)):
            target = self.bin / name
            target.write_text(
                f"#!{sys.executable}\n" + textwrap.dedent(program), encoding="utf-8"
            )
            target.chmod(0o700)
        self.calls_file = self.root / "calls.jsonl"
        self.access_file = self.root / "intranet-allow.conf"
        self.environment_file = self.root / "frontend.env"
        self.environment_file.write_text("# Synthetic Compose input.\n")
        self.environment_file.chmod(0o600)
        self.configure("intranet-http")

    def configure(self, mode):
        config = Runner(self.root, mode=mode).config
        for service in config["services"].values():
            service["user"] = "1001:1001"
        path = self.root / "compose.json"
        path.write_text(json.dumps(config), encoding="ascii")
        path.chmod(0o600)
        self.mode = mode

    def run_wrapper(self, command, *, fault="none"):
        self.calls_file.unlink(missing_ok=True)
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), command],
            cwd=ROOT,
            env={
                "PATH": str(self.bin) + ":/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "SEARCH_WEB_MODE": self.mode,
                "SEARCH_WEB_ENV_FILE": str(self.environment_file),
                "FRONTEND_FIXTURE_ROOT": str(self.root),
                "FRONTEND_FIXTURE_FAULT": fault,
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertTrue(self.calls_file.exists(), result.stderr)
        calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        return result, calls

    def operations(self, calls):
        return [
            "build"
            if call[0] == "build"
            else next(
                name for name in ("config", "stop", "up", "down", "ps") if name in call
            )
            for call in calls
        ]

    def test_normal_up_preserves_an_existing_approved_inode(self):
        original = self.access_file.stat()
        rules = self.access_file.read_bytes()
        result, calls = self.run_wrapper("up")
        self.assertEqual(0, result.returncode, result.stderr)
        operations = self.operations(calls)
        self.assertEqual(2, operations.count("build"))
        self.assertEqual(1, operations.count("up"))
        stop, up = operations.index("stop"), operations.index("up")
        last_build = len(operations) - 1 - operations[::-1].index("build")
        self.assertIn("config", operations[last_build + 1 : stop])
        self.assertIn("config", operations[stop + 1 : up])
        current = self.access_file.stat()
        self.assertEqual(
            (original.st_dev, original.st_ino), (current.st_dev, current.st_ino)
        )
        self.assertEqual(rules, self.access_file.read_bytes())
        self.assertEqual(0o444, current.st_mode & 0o777)

    def test_unsafe_admission_parent_stops_before_any_build(self):
        self.root.chmod(0o755)
        try:
            result, calls = self.run_wrapper("up")
        finally:
            self.root.chmod(0o700)
        self.assertNotEqual(0, result.returncode)
        self.assertTrue(all(name == "config" for name in self.operations(calls)))

    def test_drift_during_build_stops_before_admission_changes(self):
        result, calls = self.run_wrapper("up", fault="build")
        self.assertNotEqual(0, result.returncode)
        operations = self.operations(calls)
        self.assertEqual(2, operations.count("build"))
        self.assertNotIn("stop", operations)
        self.assertNotIn("up", operations)
        self.assertEqual(b"allow all;\n", self.access_file.read_bytes())

    def test_drift_after_stop_never_reopens_the_frontend(self):
        result, calls = self.run_wrapper("up", fault="stop")
        self.assertNotEqual(0, result.returncode)
        operations = self.operations(calls)
        self.assertEqual(2, operations.count("build"))
        self.assertIn("stop", operations)
        self.assertNotIn("up", operations)
        self.assertTrue(
            all(call[-1] == "web-nginx" for call in calls if "stop" in call)
        )
        self.assertEqual(b"allow all;\n", self.access_file.read_bytes())

    def test_down_and_status_remain_available_with_broken_admission(self):
        for command, expected in (("down", "down"), ("status", "ps")):
            for admission in ("missing", "tampered"):
                with self.subTest(command=command, admission=admission):
                    self.access_file.unlink(missing_ok=True)
                    if admission == "tampered":
                        self.access_file.write_text("allow all;\n")
                        self.access_file.chmod(0o444)
                    result, calls = self.run_wrapper(command)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(["config", expected], self.operations(calls))
                    if admission == "missing":
                        self.assertFalse(self.access_file.exists())
                    else:
                        self.assertEqual(b"allow all;\n", self.access_file.read_bytes())

    def test_private_up_does_not_require_an_intranet_admission_file(self):
        self.access_file.unlink()
        self.configure("private")
        result, calls = self.run_wrapper("up")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, self.operations(calls).count("build"))
        self.assertEqual(1, self.operations(calls).count("up"))
        self.assertFalse(self.access_file.exists())


if __name__ == "__main__":
    unittest.main()
