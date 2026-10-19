from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SearchImageHardeningTests(unittest.TestCase):
    def test_importer_ignores_python_startup_redirects(self) -> None:
        dockerfile = (ROOT / "deploy/docker/importer/Dockerfile").read_text()
        entrypoint = json.loads(
            next(
                line
                for line in dockerfile.splitlines()
                if line.startswith("ENTRYPOINT ")
            ).split(" ", 1)[1]
        )
        with tempfile.TemporaryDirectory() as directory:
            poison = Path(directory)
            (poison / "sitecustomize.py").write_text("raise SystemExit(91)\n")
            user_site = (
                poison
                / ".local/lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            user_site.mkdir(parents=True)
            (user_site / "usercustomize.py").write_text("raise SystemExit(92)\n")
            environment = {
                **os.environ,
                "HOME": str(poison),
                "PYTHONPATH": str(poison),
                "PYTHONHOME": str(poison / "not-python"),
            }
            result = subprocess.run(
                [sys.executable, *entrypoint[1:], "--help"],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("vector-pass", result.stdout)
        self.assertEqual("", result.stderr)

    def test_derivative_build_contexts_use_exact_allowlists(self) -> None:
        expected = {
            "deploy/docker/fess/Dockerfile.dockerignore": {
                "**",
                "!deploy/",
                "!deploy/docker/",
                "!deploy/docker/fess/",
                "!deploy/docker/fess/entrypoint.sh",
                "!deploy/docker/fess/fess_healthcheck.sh",
                "!deploy/config/",
                "!deploy/config/fess/",
                "!deploy/config/fess/scheduled_job.bulk",
            },
            "deploy/docker/opensearch/.dockerignore": {
                "**",
                "!Dockerfile",
                "!opensearch_healthcheck.py",
            },
            "deploy/docker/web-nginx/Dockerfile.dockerignore": {
                "**",
                "!web/",
                "!web/package.json",
                "!web/package-lock.json",
                "!web/index.html",
                "!web/tsconfig.json",
                "!web/vite.config.ts",
                "!web/src/",
                "!web/src/*.ts",
                "!web/src/*.tsx",
                "!web/src/*.css",
                "!web/src/search/",
                "!web/src/search/session.ts",
                "!web/src/assets/",
                "!web/src/assets/search-symbol.svg",
                "!web/src/assets/search-logo.svg",
                "!deploy/",
                "!deploy/config/",
                "!deploy/config/web-nginx/",
                "!deploy/config/web-nginx/nginx.conf",
                "!deploy/config/web-nginx/http.conf",
                "!deploy/config/web-nginx/website.conf",
            },
        }
        for relative, allowed in expected.items():
            with self.subTest(relative=relative):
                lines = set(
                    line
                    for line in (ROOT / relative)
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.strip() and not line.startswith("#")
                )
                directory_resets = {
                    pattern[1:] + "**"
                    for pattern in allowed
                    if pattern.startswith("!") and pattern.endswith("/")
                }
                self.assertEqual(allowed | directory_resets, lines)

    def test_opensearch_keystore_is_baked_for_read_only_runtime(self) -> None:
        dockerfile = (ROOT / "deploy/docker/opensearch/Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "/usr/share/opensearch/bin/opensearch-keystore create", dockerfile
        )
        self.assertIn(
            "test -s /usr/share/opensearch/config/opensearch.keystore",
            dockerfile,
        )
        self.assertIn(
            "chmod 0444 /usr/share/opensearch/config/opensearch.keystore",
            dockerfile,
        )
        self.assertIn("USER 1000:1000", dockerfile)

    def test_opensearch_public_hasher_supports_arbitrary_non_root_operator(
        self,
    ) -> None:
        dockerfile = (ROOT / "deploy/docker/opensearch/Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("chmod 0755 /usr/share/opensearch ", dockerfile)
        self.assertIn(
            "chmod 0755 /usr/share/opensearch/plugins/opensearch-security/tools/hash.sh",
            dockerfile,
        )
        self.assertNotIn("chmod -R a+rX /usr/share/opensearch/plugins", dockerfile)
        self.assertNotIn("chmod 0777", dockerfile)
        self.assertTrue(dockerfile.rstrip().endswith("USER 1000:1000"))

    def test_fess_healthcheck_requires_the_exact_200_status(self) -> None:
        dockerfile = (ROOT / "deploy/docker/fess/Dockerfile").read_text(
            encoding="utf-8"
        )
        healthcheck = (ROOT / "deploy/docker/fess/fess_healthcheck.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "fess_healthcheck.sh /usr/local/bin/depositar-fess-healthcheck",
            dockerfile,
        )
        self.assertIn("--write-out '%{http_code}'", healthcheck)
        self.assertIn('[ "$status" = 200 ]', healthcheck)
        self.assertNotIn("--location", healthcheck)


if __name__ == "__main__":
    unittest.main()
