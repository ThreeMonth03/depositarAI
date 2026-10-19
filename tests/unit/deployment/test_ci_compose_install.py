"""The CI renderer installer must not trust downloads or alter host plugins."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/ci/install-compose.sh"
SHA256 = "c57ab918abd5b05ca7e7d0f275875dd1330a695074f309dc9eab1b49efafcd4b"


class ComposeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="compose-installer-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env_file = self.root / "github-env"
        self.env_file.touch()
        self.calls = self.root / "calls"
        self.payload = self.root / "payload"
        self.payload.write_text(
            '#!/bin/bash\nprintf "executed\\n" >> "$CALLS"\n'
            'printf "%s\\n" "${REPORTED_VERSION:-5.5.0}"\n'
        )
        self.environment = {
            **os.environ,
            "GITHUB_ACTIONS": "true",
            "RUNNER_TEMP": str(self.root),
            "GITHUB_ENV": str(self.env_file),
            "PATH": str(self.bin) + ":" + os.environ["PATH"],
            "CALLS": str(self.calls),
            "PAYLOAD": str(self.payload),
            "EXPECTED_SHA256": SHA256,
        }
        self.stub(
            "uname",
            'if [[ "$1" == -s ]]; then echo Linux; else echo "${TEST_ARCH:-x86_64}"; fi',
        )
        self.stub(
            "curl",
            'printf "download\\n" >> "$CALLS"\n'
            'if [[ "${DOWNLOAD_FAIL:-0}" == 1 ]]; then exit 22; fi\n'
            "while (($#)); do\n"
            '  if [[ "$1" == --output ]]; then output="$2"; shift 2; else shift; fi\n'
            'done\ncp "$PAYLOAD" "$output"',
        )
        self.stub(
            "sha256sum",
            "read -r digest file\n"
            '[[ "$digest" == "$EXPECTED_SHA256" && -f "$file" ]] || exit 9\n'
            'printf "verified\\n" >> "$CALLS"\n'
            '[[ "${BAD_HASH:-0}" != 1 ]]',
        )
        self.stub(
            "docker",
            '[[ "$*" == "compose version --short" ]] || exit 9\n'
            '[[ "$DOCKER_CONFIG" == "$RUNNER_TEMP"/depositar-compose.* ]] || exit 9\n'
            'printf "docker-selected\\n" >> "$CALLS"\n'
            'if [[ "${WRONG_DOCKER_VERSION:-0}" == 1 ]]; then echo 2.38.2; exit 0; fi\n'
            '"$DOCKER_CONFIG/cli-plugins/docker-compose" version --short',
        )

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + body + "\n")
        path.chmod(0o700)

    def run_installer(self, **environment):
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env={**self.environment, **environment},
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_verified_plugin_is_scoped_to_fresh_runner_config(self):
        result = self.run_installer(DOCKER_CONFIG="/do/not/change")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls.read_text().splitlines(),
            ["download", "verified", "executed", "docker-selected", "executed"],
        )
        line = self.env_file.read_text().strip()
        self.assertTrue(line.startswith("DOCKER_CONFIG="), line)
        config = Path(line.split("=", 1)[1])
        self.assertEqual(config.parent, self.root)
        self.assertTrue(config.name.startswith("depositar-compose."))
        self.assertEqual(config.stat().st_mode & 0o777, 0o700)
        plugin = config / "cli-plugins/docker-compose"
        self.assertEqual(plugin.stat().st_mode & 0o777, 0o700)
        self.assertIn("5.5.0", result.stdout)

    def test_checksum_failure_cannot_execute_or_select_plugin(self):
        result = self.run_installer(BAD_HASH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA256", result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ["download", "verified"])
        self.assertEqual(self.env_file.read_text(), "")
        plugin = next(self.root.glob("depositar-compose.*/cli-plugins/docker-compose"))
        self.assertEqual(plugin.stat().st_mode & 0o111, 0)

    def test_network_failure_never_selects_plugin(self):
        result = self.run_installer(DOWNLOAD_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), ["download"])
        self.assertEqual(self.env_file.read_text(), "")

    def test_version_mismatch_never_selects_plugin(self):
        result = self.run_installer(REPORTED_VERSION="2.38.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected version", result.stderr)
        self.assertEqual(self.env_file.read_text(), "")

    def test_docker_selecting_another_plugin_never_exports_config(self):
        result = self.run_installer(WRONG_DOCKER_VERSION="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not select", result.stderr)
        self.assertEqual(self.env_file.read_text(), "")

    def test_non_actions_and_other_architectures_fail_before_download(self):
        for environment in ({"GITHUB_ACTIONS": "false"}, {"TEST_ARCH": "aarch64"}):
            with self.subTest(environment=environment):
                result = self.run_installer(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())
                self.assertEqual(self.env_file.read_text(), "")

    def test_invalid_runner_paths_fail_before_download(self):
        symlink = self.root / "linked-env"
        symlink.symlink_to(self.env_file)
        for environment in (
            {"RUNNER_TEMP": "/"},
            {"RUNNER_TEMP": "relative"},
            {"RUNNER_TEMP": str(self.root / "missing")},
            {"GITHUB_ENV": str(self.root / "missing-env")},
            {"GITHUB_ENV": str(symlink)},
        ):
            with self.subTest(environment=environment):
                result = self.run_installer(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
