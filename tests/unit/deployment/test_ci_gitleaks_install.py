"""Checksum and functional controls before the CI scanner can be selected."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/ci/install-gitleaks.sh"
SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"


class GitleaksInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gitleaks-installer-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env_file = self.root / "github-env"
        self.env_file.touch()
        self.calls = self.root / "calls"
        self.payload = self.root / "payload"
        self.payload.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            'if [[ "$1" == version ]]; then\n'
            '  printf "version\\n" >> "$CALLS"\n'
            '  echo "${REPORTED_VERSION:-8.30.1}"; exit 0\n'
            "fi\n"
            'printf "scan\\n" >> "$CALLS"\n'
            '[[ "$*" == *"--redact=100"* && "$*" == *"--ignore-gitleaks-allow"* ]] || exit 9\n'
            'if [[ "${@: -1}" == */positive.txt ]]; then\n'
            '  exit "${POSITIVE_STATUS:-1}"\n'
            "fi\n"
            'exit "${NEGATIVE_STATUS:-0}"\n'
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
            "done\n"
            'cp "$PAYLOAD" "$output"',
        )
        self.stub(
            "sha256sum",
            "read -r digest file\n"
            '[[ "$digest" == "$EXPECTED_SHA256" && -f "$file" ]] || exit 9\n'
            'printf "verified\\n" >> "$CALLS"\n'
            '[[ "${BAD_HASH:-0}" != 1 ]]',
        )
        self.stub(
            "tar",
            'printf "extracted\\n" >> "$CALLS"\n'
            "while (($#)); do\n"
            '  if [[ "$1" == --directory ]]; then destination="$2"; shift 2; else shift; fi\n'
            "done\n"
            'cp "$PAYLOAD" "$destination/gitleaks"',
        )

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + body + "\n")
        path.chmod(0o700)

    def run_installer(self, **environment):
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=ROOT,
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_binary_is_exported_only_after_checksum_version_and_controls(self):
        result = self.run_installer()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["download", "verified", "extracted", "version", "scan", "scan"],
            self.calls.read_text().splitlines(),
        )
        line = self.env_file.read_text().strip()
        self.assertTrue(line.startswith("GITLEAKS_BINARY="))
        binary = Path(line.split("=", 1)[1])
        self.assertEqual(binary.parent.parent, self.root)
        self.assertEqual(0o700, binary.stat().st_mode & 0o777)
        self.assertIn("synthetic positive and negative controls passed", result.stdout)

    def test_checksum_failure_cannot_extract_or_execute_download(self):
        result = self.run_installer(BAD_HASH="1")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("SHA256", result.stderr)
        self.assertEqual(["download", "verified"], self.calls.read_text().splitlines())
        self.assertEqual("", self.env_file.read_text())

    def test_network_failure_never_exports_binary(self):
        result = self.run_installer(DOWNLOAD_FAIL="1")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(["download"], self.calls.read_text().splitlines())
        self.assertEqual("", self.env_file.read_text())

    def test_wrong_version_never_runs_controls(self):
        result = self.run_installer(REPORTED_VERSION="8.18.4")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(
            ["download", "verified", "extracted", "version"],
            self.calls.read_text().splitlines(),
        )
        self.assertEqual("", self.env_file.read_text())

    def test_positive_control_must_report_exact_detection_status(self):
        for status in ("0", "2", "124"):
            with self.subTest(status=status):
                result = self.run_installer(POSITIVE_STATUS=status)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("positive control", result.stderr)
                self.assertEqual("", self.env_file.read_text())

    def test_negative_control_must_pass(self):
        result = self.run_installer(NEGATIVE_STATUS="1")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("negative control", result.stderr)
        self.assertEqual("", self.env_file.read_text())

    def test_non_actions_and_other_architectures_fail_before_download(self):
        for environment in ({"GITHUB_ACTIONS": "false"}, {"TEST_ARCH": "aarch64"}):
            with self.subTest(environment=environment):
                result = self.run_installer(**environment)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(self.calls.exists())

    def test_invalid_runner_paths_fail_before_download(self):
        linked_env = self.root / "linked-env"
        linked_env.symlink_to(self.env_file)
        for environment in (
            {"RUNNER_TEMP": "/"},
            {"RUNNER_TEMP": "relative"},
            {"RUNNER_TEMP": str(self.root / "missing")},
            {"GITHUB_ENV": str(self.root / "missing-env")},
            {"GITHUB_ENV": str(linked_env)},
        ):
            with self.subTest(environment=environment):
                result = self.run_installer(**environment)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
