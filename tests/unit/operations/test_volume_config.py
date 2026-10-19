"""Explicit volume-profile configuration and bootstrap path policy, no daemon."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from search.operations.config import CONFIG_KEYS, load_config, validate_config


ROOT = Path(__file__).resolve().parents[3]
PROJECT = "depositar-ai-catalog-" + "a" * 12 + "-" + "b" * 12


class VolumeConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-volume-config-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        roots = {}
        for name in ("api_model_root", "embedding_model_root", "reranker_model_root"):
            path = self.root / name
            path.mkdir(mode=0o700)
            roots[name] = str(path)
        self.value = {
            "schema_version": 2,
            "catalog_storage": "docker-volume",
            "state_root": str(self.home / "dedicated-state"),
            **roots,
            "gpus": {
                "scope": "GPU-00000000-0000-0000-0000-000000000001",
                "planner": [
                    "GPU-00000000-0000-0000-0000-000000000002",
                    "GPU-00000000-0000-0000-0000-000000000003",
                ],
                "reranker": "GPU-00000000-0000-0000-0000-000000000004",
            },
            "cache_ttl_seconds": 0,
        }

    def validate(self, value=None):
        return validate_config(
            self.value if value is None else value,
            repo_root=self.repo,
            home=self.home,
        )

    def test_schema_two_round_trip_is_explicit_and_read_only(self):
        config = self.validate()
        self.assertEqual("docker-volume", config.catalog_storage)
        self.assertEqual(self.value, config.to_dict())
        self.assertFalse(config.state_root.exists())
        path = self.root / "config.json"
        raw = json.dumps(self.value).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        loaded = load_config(path, repo_root=self.repo, home=self.home)
        self.assertEqual(config, loaded)
        self.assertEqual(raw, path.read_bytes())

    def test_schema_one_keeps_exact_original_fields_and_default(self):
        value = {
            key: item for key, item in self.value.items() if key != "catalog_storage"
        }
        value["schema_version"] = 1
        config = self.validate(value)
        self.assertEqual("bind", config.catalog_storage)
        self.assertEqual(CONFIG_KEYS, set(config.to_dict()))
        self.assertEqual(value, config.to_dict())

    def test_schema_storage_mismatches_missing_and_unknown_values_fail(self):
        cases = [
            {key: item for key, item in self.value.items() if key != "catalog_storage"},
            {**self.value, "schema_version": 1},
            {**self.value, "schema_version": True},
            {**self.value, "schema_version": 3},
            {**self.value, "unexpected": "value"},
        ]
        cases.extend(
            {**self.value, "catalog_storage": raw}
            for raw in ("bind", "local", "", "docker-volume ", None, True, [])
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.validate(value)

    def test_forged_typed_storage_cannot_disappear_during_serialization(self):
        config = self.validate()
        for invalid in (
            replace(config, schema_version=1),
            replace(config, catalog_storage="bind"),
            replace(config, catalog_storage=True),
        ):
            with (
                self.subTest(config=invalid),
                self.assertRaisesRegex(ValueError, "catalog_storage_invalid"),
            ):
                invalid.to_dict()

    def test_volume_profile_retains_home_root_repo_overlap_and_private_guards(self):
        for value in (
            {**self.value, "state_root": str(self.home)},
            {**self.value, "state_root": str(self.repo / "runtime")},
            {**self.value, "state_root": self.value["api_model_root"]},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.validate(value)
        self.home.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "directory_unsafe"):
            self.validate()
        self.home.chmod(0o700)
        link = self.home / "linked-state"
        link.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.validate({**self.value, "state_root": str(link)})


class VolumeBootstrapPolicyTests(unittest.TestCase):
    """Run actual bootstrap preflight bytes; no service or model phase exists."""

    def run_policy(self, *, mode=None, isolated=True, root_case="child", ambient=None):
        with tempfile.TemporaryDirectory(prefix="depositar-volume-bootstrap-") as raw:
            directory = Path(raw)
            project = directory / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            home = directory / "home"
            home.mkdir(mode=0o700)
            private = home / "private"
            private.mkdir(mode=0o700)
            root = private / "state"
            if root_case == "home":
                root = home
            elif root_case == "repo":
                root = project / "runtime"
            elif root_case == "symlink":
                link = home / "linked"
                link.symlink_to(private, target_is_directory=True)
                root = link / "state"
            elif root_case == "unsafe_parent":
                private.chmod(0o755)
            elif root_case == "unsafe_existing":
                root.mkdir(mode=0o755)
            source = (ROOT / "scripts/bootstrap-search-canary.sh").read_text()
            prefix = source.split('if [[ -e "$project_dir/.env"', 1)[0]
            bootstrap = scripts / "bootstrap-search-canary.sh"
            bootstrap.write_text(prefix + "\nexit 0\n")
            bootstrap.chmod(0o700)
            for name in ("search-lib.sh", "read_env.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            # The prefix only verifies these helpers exist; neither is invoked.
            for name in ("search_dictionary_permissions.py", "search_model_source.py"):
                (scripts / name).write_text("# fixture\n")
            text = (
                f"COMPOSE_PROJECT_NAME={PROJECT if isolated else 'depositar-search-canary'}\n"
                f"SEARCH_CANARY_DATA_ROOT={root}\n"
                f"SEARCH_CANARY_RUNTIME_UID={os.geteuid()}\n"
                f"SEARCH_CANARY_RUNTIME_GID={os.getegid()}\n"
                "SEARCH_OPENSEARCH_BASE_IMAGE=fixture\n"
            )
            if mode is not None:
                text += f"SEARCH_CANARY_CATALOG_STORAGE={mode}\n"
            env_file = project / ".env.search-canary"
            env_file.write_text(text)
            env_file.chmod(0o600)
            fake_bin = directory / "bin"
            fake_bin.mkdir()
            # Keep HOME untouched. Only this disposable realpath fixture maps
            # its canonical result, allowing path-policy tests off real NFS.
            realpath = fake_bin / "realpath"
            realpath.write_text(
                '#!/bin/bash\nif [[ "$*" == "-m -- $FIXTURE_REAL_HOME" ]]; then\n'
                '  printf "%s\\n" "$FIXTURE_HOME"\n'
                'else exec /usr/bin/realpath "$@"; fi\n'
            )
            realpath.chmod(0o700)
            docker = fake_bin / "docker"
            docker.write_text(
                '#!/bin/bash\nprintf invoked >"$FIXTURE_DOCKER_LOG"\nexit 99\n'
            )
            docker.chmod(0o700)
            environment = dict(os.environ)
            environment.update(
                PATH=str(fake_bin) + os.pathsep + environment.get("PATH", ""),
                FIXTURE_REAL_HOME=os.environ.get("HOME", ""),
                FIXTURE_HOME=str(home),
                FIXTURE_DOCKER_LOG=str(directory / "docker.log"),
            )
            environment.pop("SEARCH_CANARY_CATALOG_STORAGE", None)
            if ambient is not None:
                environment["SEARCH_CANARY_CATALOG_STORAGE"] = ambient
            arguments = ["--isolated-project", PROJECT] if isolated else []
            result = subprocess.run(
                [str(bootstrap), *arguments],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertFalse((directory / "docker.log").exists())
            self.assertFalse(
                (private / "state").exists() and root_case != "unsafe_existing"
            )
            return result

    def test_explicit_envfile_volume_and_isolated_project_allow_private_home_child(
        self,
    ):
        result = self.run_policy(mode="docker-volume")
        self.assertEqual(0, result.returncode, result.stderr)

    def test_default_bind_explicit_bind_and_ambient_optin_keep_home_rejection(self):
        for mode, ambient in ((None, None), ("bind", None), (None, "docker-volume")):
            with self.subTest(mode=mode, ambient=ambient):
                result = self.run_policy(mode=mode, ambient=ambient)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("cannot be stored under HOME", result.stderr)

    def test_volume_optin_does_not_relax_standalone_home_policy(self):
        result = self.run_policy(mode="docker-volume", isolated=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("cannot be stored under HOME", result.stderr)

    def test_unknown_and_empty_envfile_storage_fail(self):
        for mode in ("local", "", "Docker-Volume"):
            with self.subTest(mode=mode):
                result = self.run_policy(mode=mode)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("must be bind or docker-volume", result.stderr)

    def test_volume_optin_retains_home_root_repo_symlink_and_mode_guards(self):
        for root_case in (
            "home",
            "repo",
            "symlink",
            "unsafe_parent",
            "unsafe_existing",
        ):
            with self.subTest(root_case=root_case):
                result = self.run_policy(mode="docker-volume", root_case=root_case)
                self.assertNotEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
