from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
APPROVED_OS_IMAGE = (
    "ghcr.io/codelibs/fess-opensearch:3.8.0@sha256:"
    "c59dd34da6d0c6e0117b8851ded0832f168de9b33e275e62cd3ef3139362d7fc"
)
HASH_REVISION = "a" * 40
HASH_IMAGE = "depositar-opensearch-search-hash:" + HASH_REVISION
HASH_IMAGE_ID = "sha256:" + "b" * 64
SECRET_NAMES = (
    "opensearch_admin_password",
    "fess_opensearch_password",
    "fess_app_cipher_key",
    "fess_initial_admin_password",
)
MODEL_MANIFEST = "manifests/registry.ollama.ai/library/granite-embedding/278m"
MODEL_DIGEST = "a658c903bf83a9d84fe1b9869874229ddfdfc1c88b04f958f17dd24e243d262c"
CONFIG_DIGEST = "eb0753f1773ea272958bcbd3d5c2a248b6edc665e02274fc720b1dbd58b02153"
LICENSE_DIGEST = "492069a62c2545ecde9f8cbc5c3d46adadaa3f95c0e1f68aea7ed2cca548484b"


def write_file(path: Path, value: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


class SearchCanaryBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="depositar-bootstrap-test-")
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def workspace(self, name: str) -> tuple[Path, Path, Path]:
        workspace = self.base / name
        project = workspace / "project"
        root = workspace / "storage"
        fake_bin = workspace / "fake-bin"
        (project / "scripts").mkdir(parents=True)
        (project / "deploy" / "config").mkdir(parents=True)
        fake_bin.mkdir(parents=True)
        for relative in (
            "scripts/bootstrap-search-canary.sh",
            "scripts/search-lib.sh",
            "scripts/read_env.py",
            "scripts/search_dictionary_permissions.py",
            "scripts/search_model_source.py",
        ):
            source = REPOSITORY / relative
            destination = project / relative
            shutil.copy2(source, destination)
        shutil.copytree(
            REPOSITORY / "deploy" / "config" / "opensearch",
            project / "deploy" / "config" / "opensearch",
        )
        env_text = "\n".join(
            (
                "COMPOSE_PROJECT_NAME=depositar-search-canary",
                f"SEARCH_CANARY_DATA_ROOT={root}",
                f"SEARCH_CANARY_STORAGE_ALLOWED_ROOT={workspace}",
                "SEARCH_CANARY_STORAGE_REQUIRED_FS_TYPES=",
                f"SEARCH_CANARY_RUNTIME_UID={os.getuid()}",
                f"SEARCH_CANARY_RUNTIME_GID={os.getgid()}",
                "SEARCH_CANARY_MODEL_SOURCE=",
                f"SEARCH_OPENSEARCH_BASE_IMAGE={APPROVED_OS_IMAGE}",
                "",
            )
        )
        write_file(project / ".env.search-canary", env_text, 0o600)
        self.write_default_fake_docker(fake_bin)
        return project, root, fake_bin

    @staticmethod
    def write_default_fake_docker(fake_bin: Path) -> None:
        write_file(
            fake_bin / "docker",
            """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  ps) exit 0 ;;
  version) exit 42 ;;
  *) exit 42 ;;
esac
""",
            0o700,
        )

    @staticmethod
    def write_fake_openssl(fake_bin: Path) -> None:
        write_file(
            fake_bin / "openssl",
            """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  verify) exit 0 ;;
  pkey) printf 'test-public-key\\n'; exit 0 ;;
  x509)
    case " $* " in
      *' -text '*) printf 'CA:TRUE\\n' ;;
      *' -pubkey '*) printf 'test-public-key\\n' ;;
    esac
    exit 0
    ;;
  *) exit 41 ;;
esac
""",
            0o700,
        )

    @staticmethod
    def write_model_sha256_wrapper(fake_bin: Path) -> None:
        write_file(
            fake_bin / "sha256sum",
            f"""#!/usr/bin/env bash
set -eu
if [[ $# -eq 1 ]]; then
  case "$1" in
    */ollama/models/{MODEL_MANIFEST})
      printf '%s  %s\\n' '1a37926bf842899cbf90583e3932f3820548716d9d07661ba622199ffe95c552' "$1"
      exit 0
      ;;
    */ollama/models/blobs/sha256-*)
      name="${{1##*/sha256-}}"
      case "$name" in
        {MODEL_DIGEST}|{CONFIG_DIGEST}|{LICENSE_DIGEST})
          printf '%s  %s\\n' "$name" "$1"
          exit 0
          ;;
      esac
      ;;
  esac
fi
exec /usr/bin/sha256sum "$@"
""",
            0o700,
        )

    @staticmethod
    def prepare_managed_root(root: Path) -> None:
        root.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        write_file(
            root / ".depositar-search-canary-root",
            "schema=1\nproject=depositar-search-canary\nkind=search-canary-data\n",
            0o600,
        )

    @staticmethod
    def prepare_secrets(root: Path) -> dict[str, str]:
        secret_dir = root / "secrets"
        secret_dir.mkdir(mode=0o700)
        values = {
            name: character * (32 if name == "fess_app_cipher_key" else 64)
            for name, character in zip(SECRET_NAMES, "abcd")
        }
        for name, value in values.items():
            write_file(secret_dir / name, value + "\n", 0o600)
        return values

    @staticmethod
    def prepare_security(root: Path, values: dict[str, str], schema: int) -> None:
        config = root / "opensearch" / "config"
        config.mkdir(parents=True, mode=0o700)
        templates = REPOSITORY / "deploy" / "config" / "opensearch"
        for name in ("opensearch.yml", "roles.yml", "roles_mapping.yml"):
            shutil.copyfile(templates / name, config / name)
            (config / name).chmod(0o400)
        internal = (templates / "internal_users.yml.in").read_text(encoding="utf-8")
        internal = internal.replace("__ADMIN_HASH__", "$2y$12$" + "A" * 53)
        internal = internal.replace("__FESS_HASH__", "$2y$12$" + "B" * 53)
        write_file(config / "internal_users.yml", internal, 0o400)
        for name, value in (
            ("root-ca.pem", "test root ca\n"),
            ("node.pem", "test node certificate\n"),
            ("node-key.pem", "test node key\n"),
        ):
            write_file(config / name, value, 0o400)

        secret_subset = SECRET_NAMES if schema == 2 else SECRET_NAMES[:2]
        lines = [f"schema={schema}"]
        for name in secret_subset:
            lines.append(f"{name}_sha256={sha256(root / 'secrets' / name)}")
        for name in ("root-ca.pem", "node.pem", "node-key.pem", "internal_users.yml"):
            lines.append(f"{name}_sha256={sha256(config / name)}")
        write_file(config / "security-material.sha256", "\n".join(lines) + "\n", 0o600)

    @staticmethod
    def prepare_model(root: Path) -> Path:
        model_root = root / "ollama" / "models"
        manifest = {
            "schemaVersion": 2,
            "config": {"digest": f"sha256:{CONFIG_DIGEST}", "size": 337},
            "layers": [
                {"digest": f"sha256:{MODEL_DIGEST}", "size": 562765632},
                {"digest": f"sha256:{LICENSE_DIGEST}", "size": 11332},
            ],
        }
        raw_manifest = json.dumps(manifest, separators=(",", ":"))
        if len(raw_manifest) > 686:
            raise AssertionError(
                "synthetic manifest unexpectedly exceeds the pinned size"
            )
        write_file(
            model_root / MODEL_MANIFEST,
            raw_manifest + " " * (686 - len(raw_manifest)),
            0o400,
        )
        for digest, size in (
            (MODEL_DIGEST, 562765632),
            (CONFIG_DIGEST, 337),
            (LICENSE_DIGEST, 11332),
        ):
            path = model_root / "blobs" / f"sha256-{digest}"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as stream:
                stream.truncate(size)
            path.chmod(0o400)
        for directory in (model_root, *model_root.rglob("*")):
            if directory.is_dir():
                directory.chmod(0o700)
        return model_root

    def run_bootstrap(
        self,
        project: Path,
        fake_bin: Path,
        *arguments: str,
        include_hash_image: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if include_hash_image and "--hash-image" not in arguments:
            arguments = (
                *arguments,
                "--hash-image",
                HASH_IMAGE,
                "--hash-image-id",
                HASH_IMAGE_ID,
            )
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
        return subprocess.run(
            [str(project / "scripts" / "bootstrap-search-canary.sh"), *arguments],
            cwd=project,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )

    def test_pristine_secret_set_is_atomic_and_missing_secret_is_not_regenerated(
        self,
    ) -> None:
        project, root, fake_bin = self.workspace("secret-set")
        first = self.run_bootstrap(project, fake_bin)
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("working Docker daemon", first.stderr)

        secret_dir = root / "secrets"
        self.assertEqual(
            set(SECRET_NAMES), {entry.name for entry in secret_dir.iterdir()}
        )
        values = []
        for name in SECRET_NAMES:
            path = secret_dir / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            value = path.read_text(encoding="ascii").strip()
            expected_length = 32 if name == "fess_app_cipher_key" else 64
            self.assertRegex(value, rf"^[0-9a-f]{{{expected_length}}}$")
            self.assertNotIn(value, first.stdout + first.stderr)
            values.append(value)
        self.assertEqual(len(values), len(set(values)))

        missing = secret_dir / SECRET_NAMES[-1]
        missing.unlink()
        second = self.run_bootstrap(project, fake_bin)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("incomplete", second.stderr)
        self.assertFalse(
            missing.exists(), "bootstrap must never regenerate a missing secret"
        )

    def test_legacy_64_character_fess_cipher_key_is_rejected(self) -> None:
        project, root, fake_bin = self.workspace("legacy-fess-cipher-key")
        self.prepare_managed_root(root)
        self.prepare_secrets(root)
        write_file(root / "secrets" / "fess_app_cipher_key", "c" * 64 + "\n", 0o600)

        result = self.run_bootstrap(project, fake_bin)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "fess_app_cipher_key must contain exactly 32 lowercase hexadecimal characters",
            result.stderr,
        )

    def test_marker_only_crash_and_unused_deleted_secret_set_are_recoverable(
        self,
    ) -> None:
        project, root, fake_bin = self.workspace("marker-only-recovery")
        self.prepare_managed_root(root)

        marker_only = self.run_bootstrap(project, fake_bin)
        self.assertNotEqual(marker_only.returncode, 0)
        self.assertIn("working Docker daemon", marker_only.stderr)
        self.assertEqual(
            set(SECRET_NAMES),
            {entry.name for entry in (root / "secrets").iterdir()},
        )

        project, root, fake_bin = self.workspace("deleted-unused-secret-set")
        first = self.run_bootstrap(project, fake_bin)
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("working Docker daemon", first.stderr)

        secret_dir = root / "secrets"
        original_values = {
            name: (secret_dir / name).read_text(encoding="ascii")
            for name in SECRET_NAMES
        }
        for name in SECRET_NAMES:
            (secret_dir / name).unlink()
        secret_dir.rmdir()

        second = self.run_bootstrap(project, fake_bin)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("working Docker daemon", second.stderr)
        self.assertEqual(
            set(SECRET_NAMES),
            {entry.name for entry in secret_dir.iterdir()},
        )
        for name in SECRET_NAMES:
            self.assertNotEqual(
                original_values[name],
                (secret_dir / name).read_text(encoding="ascii"),
            )

    def test_absent_secret_set_is_blocked_when_any_managed_state_is_used(self) -> None:
        mutations = {
            "opensearch-data": ("opensearch/data/node-state", "used\n"),
            "opensearch-config": ("opensearch/config/config.yml", "used\n"),
            "opensearch-dictionary": ("opensearch/dictionary/words.txt", "used\n"),
            "fess-data": ("fess/data/fess.db", "used\n"),
            "ollama-model": ("ollama/models/blob", "used\n"),
            "unexpected-state": ("state/unexpected", "used\n"),
            "unexpected-root": ("unexpected", "used\n"),
        }
        for name, (relative, value) in mutations.items():
            with self.subTest(state=name):
                project, root, fake_bin = self.workspace(f"used-{name}")
                self.prepare_managed_root(root)
                write_file(root / relative, value, 0o600)

                result = self.run_bootstrap(project, fake_bin)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "credential initialization state is not pristine",
                    result.stderr,
                )
                self.assertFalse((root / "secrets").exists())

    def test_absent_secret_set_is_blocked_when_canary_container_exists(self) -> None:
        project, root, fake_bin = self.workspace("existing-container")
        self.prepare_managed_root(root)
        write_file(
            fake_bin / "docker",
            """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  ps) printf '%s\n' '0123456789ab'; exit 0 ;;
  *) exit 42 ;;
esac
""",
            0o700,
        )

        result = self.run_bootstrap(project, fake_bin)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("a search canary container exists", result.stderr)
        self.assertFalse((root / "secrets").exists())

    def isolated_project_workspace(self, name: str) -> tuple[Path, Path, Path, str]:
        project, root, fake_bin = self.workspace(name)
        isolated = "depositar-ai-catalog-" + "a" * 12 + "-" + "b" * 12
        env = project / ".env.search-canary"
        write_file(
            env,
            env.read_text().replace(
                "COMPOSE_PROJECT_NAME=depositar-search-canary",
                "COMPOSE_PROJECT_NAME=" + isolated,
            ),
            0o600,
        )
        return project, root, fake_bin, isolated

    def fake_existing_project(self, fake_bin: Path, existing: str) -> Path:
        log = fake_bin.parent / "docker-arguments.log"
        write_file(
            fake_bin / "docker",
            f"""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >>'{log}'
case "${{1:-}}" in
  ps)
    [[ " $* " != *' label=com.docker.compose.project={existing} '* ]] \\
      || printf '%s\\n' '0123456789ab'
    exit 0
    ;;
  *) exit 42 ;;
esac
""",
            0o700,
        )
        return log

    def test_isolated_catalog_initializes_its_own_secrets_with_old_canary_running(
        self,
    ) -> None:
        project, root, fake_bin, isolated = self.isolated_project_workspace(
            "isolated-old-live"
        )
        log = self.fake_existing_project(fake_bin, "depositar-search-canary")
        old_secret = self.base / "old-live-secret"
        write_file(old_secret, "must remain unchanged\n", 0o600)

        result = self.run_bootstrap(project, fake_bin, "--isolated-project", isolated)

        # The fake daemon deliberately stops after the credential check, before
        # any bcrypt helper. New-slot secrets prove the old project did not block.
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("working Docker daemon", result.stderr)
        self.assertNotIn("container exists", result.stderr)
        self.assertEqual(
            set(SECRET_NAMES), {entry.name for entry in (root / "secrets").iterdir()}
        )
        self.assertIn(
            "project=" + isolated + "\n",
            (root / ".depositar-search-canary-root").read_text(),
        )
        self.assertIn("label=com.docker.compose.project=" + isolated, log.read_text())
        self.assertNotIn(
            "label=com.docker.compose.project=depositar-search-canary", log.read_text()
        )
        self.assertEqual("must remain unchanged\n", old_secret.read_text())

    def test_isolated_catalog_refuses_credentials_when_same_project_exists(
        self,
    ) -> None:
        project, root, fake_bin, isolated = self.isolated_project_workspace(
            "isolated-new-live"
        )
        log = self.fake_existing_project(fake_bin, isolated)

        result = self.run_bootstrap(project, fake_bin, "--isolated-project", isolated)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("container exists", result.stderr)
        self.assertFalse((root / "secrets").exists())
        self.assertIn("label=com.docker.compose.project=" + isolated, log.read_text())

    def test_isolated_project_option_is_narrow_and_duplicate_flags_are_rejected(
        self,
    ) -> None:
        project, root, fake_bin = self.workspace("isolated-invalid")
        qualified = "depositar-ai-catalog-" + "a" * 12 + "-" + "b" * 12
        for arguments in (
            ("--isolated-project",),
            ("--isolated-project", "depositar-search-canary"),
            ("--isolated-project", qualified.upper()),
            ("--isolated-project", qualified + "-extra"),
            ("--isolated-project", qualified, "--isolated-project", qualified),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_bootstrap(project, fake_bin, *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--isolated-project", result.stderr)
                self.assertFalse(root.exists())

    def test_isolated_project_auto_created_env_uses_qualified_project(self) -> None:
        project, root, fake_bin = self.workspace("isolated-auto-env")
        (project / ".env.search-canary").unlink()
        example = project / "deploy/env/.env.search-canary.example"
        example.parent.mkdir(parents=True)
        shutil.copy2(REPOSITORY / "deploy/env/.env.search-canary.example", example)
        isolated = "depositar-ai-catalog-" + "a" * 12 + "-" + "b" * 12

        result = self.run_bootstrap(project, fake_bin, "--isolated-project", isolated)

        self.assertEqual(2, result.returncode)
        self.assertIn("Created .env.search-canary", result.stdout)
        env = (project / ".env.search-canary").read_text()
        self.assertIn("COMPOSE_PROJECT_NAME=" + isolated + "\n", env)
        self.assertIn(f"SEARCH_CANARY_RUNTIME_UID={os.getuid()}\n", env)
        self.assertIn(f"SEARCH_CANARY_RUNTIME_GID={os.getgid()}\n", env)
        self.assertFalse(root.exists())

    def test_absent_secret_set_is_blocked_when_docker_state_cannot_be_proved(
        self,
    ) -> None:
        project, root, fake_bin = self.workspace("docker-state-unknown")
        self.prepare_managed_root(root)
        write_file(
            fake_bin / "docker",
            """#!/usr/bin/env bash
exit 46
""",
            0o700,
        )

        result = self.run_bootstrap(project, fake_bin)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "working Docker daemon is required to prove",
            result.stderr,
        )
        self.assertFalse((root / "secrets").exists())

    def test_deleted_secret_set_is_not_recreated_after_security_state_exists(
        self,
    ) -> None:
        project, root, fake_bin = self.workspace("deleted-committed-secret-set")
        self.prepare_managed_root(root)
        values = self.prepare_secrets(root)
        self.prepare_security(root, values, schema=2)
        secret_dir = root / "secrets"
        for name in SECRET_NAMES:
            (secret_dir / name).unlink()
        secret_dir.rmdir()

        result = self.run_bootstrap(project, fake_bin)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "credential initialization state is not pristine",
            result.stderr,
        )
        self.assertFalse(secret_dir.exists())

    def test_failed_generation_leaves_no_partial_secret_directory(self) -> None:
        project, root, fake_bin = self.workspace("atomic-failure")
        counter = self.base / "openssl-count"
        write_file(
            fake_bin / "openssl",
            """#!/usr/bin/env bash
set -eu
count=0
[[ ! -f "$FAKE_OPENSSL_COUNT" ]] || count=$(<"$FAKE_OPENSSL_COUNT")
count=$((count + 1))
printf '%s' "$count" >"$FAKE_OPENSSL_COUNT"
[[ "$count" -ne 3 ]] || exit 43
case "$count" in
  1) printf '%064d\\n' 0 ;;
  2) printf '%064d\\n' 1 ;;
  *) printf '%064d\\n' 2 ;;
esac
""",
            0o700,
        )
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
        environment["FAKE_OPENSSL_COUNT"] = str(counter)
        result = subprocess.run(
            [str(project / "scripts" / "bootstrap-search-canary.sh")],
            cwd=project,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "secrets").exists())
        self.assertEqual([], list((root / "state").glob(".secret-set.tmp.*")))

    def test_bcrypt_helper_runs_as_the_validated_invoking_uid_and_gid(self) -> None:
        project, root, fake_bin = self.workspace("bcrypt-user")
        docker_log = self.base / "bcrypt-docker.log"
        docker_count = self.base / "bcrypt-docker-count"
        write_file(
            fake_bin / "docker",
            """#!/usr/bin/env bash
set -eu
{
  printf 'CALL'
  for argument in "$@"; do
    printf '\t%s' "$argument"
  done
  printf '\n'
} >>"$FAKE_DOCKER_LOG"
case "${1:-}" in
  ps|version) exit 0 ;;
  image)
    printf '%s\n' '{"id":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    exit 0
    ;;
  run)
    IFS= read -r password
    [[ "$password" =~ ^[0-9a-f]{64}$ ]] || exit 44
    count=0
    [[ ! -f "$FAKE_DOCKER_COUNT" ]] || count=$(<"$FAKE_DOCKER_COUNT")
    count=$((count + 1))
    printf '%s' "$count" >"$FAKE_DOCKER_COUNT"
    if [[ "$count" == 1 ]]; then
      printf '%s\n' '$2y$12$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    else
      printf '%s\n' '$2y$12$BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'
    fi
    exit 0
    ;;
  *) exit 42 ;;
esac
""",
            0o700,
        )
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
        environment["FAKE_DOCKER_LOG"] = str(docker_log)
        environment["FAKE_DOCKER_COUNT"] = str(docker_count)

        result = subprocess.run(
            [
                str(project / "scripts" / "bootstrap-search-canary.sh"),
                "--hash-image",
                HASH_IMAGE,
                "--hash-image-id",
                HASH_IMAGE_ID,
            ],
            cwd=project,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pinned Granite model is missing", result.stderr)
        docker_calls = docker_log.read_text(encoding="utf-8")
        self.assertIn(
            f"\t--user\t{os.getuid()}:{os.getgid()}\t",
            docker_calls,
        )
        for required in (
            "\t--pull=never\t",
            "\t--runtime=runc\t",
            "\t--env\tNVIDIA_VISIBLE_DEVICES=void\t",
            "\t--network\tnone\t",
            "\t--read-only\t",
            "\t--cap-drop\tALL\t",
            "\t--security-opt\tno-new-privileges:true\t",
            "\t--pids-limit\t128\t",
            "\t--memory\t768m\t",
            "\t--cpus\t1\t",
        ):
            self.assertIn(required, docker_calls)
        runs = [
            line for line in docker_calls.splitlines() if line.startswith("CALL\trun\t")
        ]
        self.assertEqual(2, len(runs))
        for line in runs:
            self.assertIn("\t" + HASH_IMAGE_ID + "\t", line)
            self.assertNotIn(HASH_IMAGE, line)
            self.assertNotIn(APPROVED_OS_IMAGE, line)
        bootstrap_source = (
            project / "scripts" / "bootstrap-search-canary.sh"
        ).read_text(encoding="utf-8")
        self.assertNotIn("--user 1000:1000", bootstrap_source)
        self.assertIn(
            '--user "$SEARCH_CANARY_RUNTIME_UID:$SEARCH_CANARY_RUNTIME_GID"',
            bootstrap_source,
        )
        for name in SECRET_NAMES:
            secret = (root / "secrets" / name).read_text(encoding="ascii").strip()
            self.assertNotIn(secret, docker_calls)

    def test_new_security_refuses_legacy_upstream_hash_helper_without_flag(
        self,
    ) -> None:
        project, root, fake_bin = self.workspace("bcrypt-no-qualified-image")
        result = self.run_bootstrap(project, fake_bin, include_hash_image=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "requires --hash-image from sealed product preparation", result.stderr
        )
        self.assertFalse((root / "opensearch/config/internal_users.yml").exists())

    def test_hash_image_option_rejects_unqualified_or_duplicate_references(
        self,
    ) -> None:
        project, root, fake_bin = self.workspace("bcrypt-invalid-image-option")
        for arguments in (
            ("--hash-image",),
            ("--hash-image", APPROVED_OS_IMAGE),
            ("--hash-image", HASH_IMAGE.upper()),
            ("--hash-image", HASH_IMAGE + "-extra"),
            ("--hash-image", HASH_IMAGE, "--hash-image", HASH_IMAGE),
            ("--hash-image", HASH_IMAGE),
            ("--hash-image-id", HASH_IMAGE_ID),
            ("--hash-image-id", "sha256:short"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_bootstrap(
                    project, fake_bin, *arguments, include_hash_image=False
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn("--hash-image", result.stderr)
                self.assertFalse(root.exists())

    def test_hash_image_inspection_rejects_untrusted_metadata_before_reading_secrets(
        self,
    ) -> None:
        mutations = (
            {"id": HASH_IMAGE_ID, "revision": "c" * 40},
            {"id": "sha256:" + "c" * 64, "revision": HASH_REVISION},
            {"id": "sha256:short", "revision": HASH_REVISION},
            {"id": True, "revision": HASH_REVISION},
            {"id": HASH_IMAGE_ID, "revision": HASH_REVISION, "extra": "unsafe"},
        )
        for number, metadata in enumerate(mutations):
            with self.subTest(metadata=metadata):
                project, root, fake_bin = self.workspace(
                    f"bcrypt-invalid-metadata-{number}"
                )
                raw = json.dumps(metadata)
                write_file(
                    fake_bin / "docker",
                    "#!/usr/bin/env bash\nset -eu\n"
                    'case "${1:-}" in\n'
                    "  ps|version) exit 0 ;;\n"
                    f"  image) printf '%s\\n' '{raw}'; exit 0 ;;\n"
                    "  run) exit 99 ;;\n"
                    "  *) exit 42 ;;\nesac\n",
                    0o700,
                )
                result = self.run_bootstrap(project, fake_bin)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("image provenance mismatch", result.stderr)
                self.assertFalse(
                    (root / "opensearch/config/internal_users.yml").exists()
                )

    def test_schema_one_migrates_only_while_data_is_empty(self) -> None:
        project, root, fake_bin = self.workspace("schema-migration")
        self.prepare_managed_root(root)
        values = self.prepare_secrets(root)
        self.prepare_security(root, values, schema=1)
        self.write_fake_openssl(fake_bin)

        result = self.run_bootstrap(project, fake_bin)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pinned Granite model is missing", result.stderr)
        manifest = root / "opensearch" / "config" / "security-material.sha256"
        lines = manifest.read_text(encoding="ascii").splitlines()
        self.assertEqual("schema=2", lines[0])
        self.assertEqual(9, len(lines))
        for name in SECRET_NAMES:
            expected = sha256(root / "secrets" / name)
            self.assertIn(f"{name}_sha256={expected}", lines)
            self.assertNotIn(values[name], result.stdout + result.stderr)

        blocked_project, blocked_root, blocked_bin = self.workspace("schema-blocked")
        self.prepare_managed_root(blocked_root)
        blocked_values = self.prepare_secrets(blocked_root)
        self.prepare_security(blocked_root, blocked_values, schema=1)
        write_file(blocked_root / "opensearch" / "data" / "node-state", "used\n", 0o600)
        self.write_fake_openssl(blocked_bin)
        blocked = self.run_bootstrap(blocked_project, blocked_bin)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("cannot be upgraded after service data exists", blocked.stderr)
        self.assertEqual(
            "schema=1",
            (blocked_root / "opensearch" / "config" / "security-material.sha256")
            .read_text(encoding="ascii")
            .splitlines()[0],
        )

    def model_workspace(self, name: str) -> tuple[Path, Path, Path, Path]:
        project, root, fake_bin = self.workspace(name)
        self.prepare_managed_root(root)
        values = self.prepare_secrets(root)
        self.prepare_security(root, values, schema=2)
        model_root = self.prepare_model(root)
        self.write_fake_openssl(fake_bin)
        self.write_model_sha256_wrapper(fake_bin)
        return project, root, fake_bin, model_root

    def test_exact_model_tree_is_accepted(self) -> None:
        project, root, fake_bin, _model_root = self.model_workspace("model-exact")
        result = self.run_bootstrap(project, fake_bin)
        self.assertEqual(0, result.returncode, result.stderr)
        for path in (root / "catalog", root / "state" / "catalog-imports"):
            self.assertTrue(path.is_dir())
            self.assertFalse(path.is_symlink())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_real_stdlib_helper_seeds_all_four_pinned_fixture_files(self) -> None:
        project, root, fake_bin = self.workspace("model-source-seeding")
        self.prepare_managed_root(root)
        values = self.prepare_secrets(root)
        self.prepare_security(root, values, schema=2)
        self.write_fake_openssl(fake_bin)
        allocated = self.base / "allocated-model-source"
        allocated.mkdir(mode=0o1770)
        allocated.chmod(0o1770)
        source = allocated / "private-models"
        source.mkdir(mode=0o700)
        payloads = ("fixture model", "fixture config", "fixture license")
        digests = [hashlib.sha256(value.encode()).hexdigest() for value in payloads]
        manifest = {
            "schemaVersion": 2,
            "config": {"digest": "sha256:" + digests[1], "size": len(payloads[1])},
            "layers": [
                {"digest": "sha256:" + digests[0], "size": len(payloads[0])},
                {"digest": "sha256:" + digests[2], "size": len(payloads[2])},
            ],
        }
        manifest_text = json.dumps(manifest, separators=(",", ":")).ljust(686)
        write_file(source / MODEL_MANIFEST, manifest_text, 0o400)
        for digest, value in zip(digests, payloads):
            write_file(source / "blobs" / ("sha256-" + digest), value, 0o400)
        # Replace only this isolated, nonsecret test fixture's authorities. The
        # checked-in production model pins and sizes remain unchanged.
        script = project / "scripts/bootstrap-search-canary.sh"
        raw = script.read_text()
        for old, new in (
            (
                "1a37926bf842899cbf90583e3932f3820548716d9d07661ba622199ffe95c552",
                hashlib.sha256(manifest_text.encode()).hexdigest(),
            ),
            (MODEL_DIGEST, digests[0]),
            (CONFIG_DIGEST, digests[1]),
            (LICENSE_DIGEST, digests[2]),
            ("562765632", str(len(payloads[0]))),
            ("337", str(len(payloads[1]))),
            ("11332", str(len(payloads[2]))),
        ):
            self.assertIn(old, raw)
            raw = re.sub(r"\b" + old + r"\b", new, raw)
        script.write_text(raw)
        env = project / ".env.search-canary"
        env.write_text(
            env.read_text().replace(
                "SEARCH_CANARY_MODEL_SOURCE=\n",
                f"SEARCH_CANARY_MODEL_SOURCE={source}\n",
            )
        )

        result = self.run_bootstrap(project, fake_bin)

        self.assertEqual(0, result.returncode, result.stderr)
        destination = root / "ollama/models"
        expected = {MODEL_MANIFEST, *("blobs/sha256-" + digest for digest in digests)}
        for relative in expected:
            copied = destination / relative
            self.assertEqual((source / relative).read_bytes(), copied.read_bytes())
            self.assertEqual(0o400, stat.S_IMODE(copied.stat().st_mode))
            self.assertEqual(1, copied.stat().st_nlink)
        self.assertEqual(0o1770, stat.S_IMODE(allocated.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(source.stat().st_mode))

    def test_roles_mapping_accepts_only_the_legacy_trailing_empty_line(self) -> None:
        project, root, fake_bin, _model_root = self.model_workspace(
            "roles-mapping-legacy-newline"
        )
        roles_mapping = root / "opensearch" / "config" / "roles_mapping.yml"
        roles_mapping.chmod(0o600)
        roles_mapping.write_bytes(roles_mapping.read_bytes() + b"\n")
        roles_mapping.chmod(0o400)

        accepted = self.run_bootstrap(project, fake_bin)

        self.assertEqual(0, accepted.returncode, accepted.stderr)

        roles_mapping.chmod(0o600)
        roles_mapping.write_bytes(roles_mapping.read_bytes() + b"\n")
        roles_mapping.chmod(0o400)
        rejected = self.run_bootstrap(project, fake_bin)

        self.assertNotEqual(0, rejected.returncode)
        self.assertIn(
            "rendered roles_mapping.yml differs from its reviewed template",
            rejected.stderr,
        )

    def test_private_catalog_directories_converge_and_reject_symlinks(self) -> None:
        project, root, fake_bin, _model_root = self.model_workspace("catalog-modes")
        (root / "catalog").mkdir(mode=0o750)
        (root / "state").mkdir(mode=0o700)
        (root / "state" / "catalog-imports").mkdir(mode=0o500)

        result = self.run_bootstrap(project, fake_bin)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(stat.S_IMODE((root / "catalog").stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((root / "state" / "catalog-imports").stat().st_mode),
            0o700,
        )

        blocked_project, blocked_root, blocked_bin, _ = self.model_workspace(
            "catalog-symlink"
        )
        (blocked_root / "catalog").symlink_to(self.base)
        blocked = self.run_bootstrap(blocked_project, blocked_bin)
        self.assertNotEqual(0, blocked.returncode)
        self.assertIn("runtime path must be a non-symlink directory", blocked.stderr)

    def test_model_tree_rejects_extra_file_directory_symlink_and_hardlink(self) -> None:
        mutations = ("file", "directory", "symlink", "hardlink")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                project, _root, fake_bin, model_root = self.model_workspace(
                    f"model-extra-{mutation}"
                )
                if mutation == "file":
                    write_file(model_root / "unexpected", "unexpected\n", 0o400)
                elif mutation == "directory":
                    (model_root / "unexpected").mkdir(mode=0o700)
                elif mutation == "symlink":
                    (model_root / "unexpected").symlink_to("/dev/null")
                else:
                    os.link(
                        model_root / "blobs" / f"sha256-{CONFIG_DIGEST}",
                        model_root / "unexpected",
                    )
                result = self.run_bootstrap(project, fake_bin)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(
                    result.stderr,
                    r"model tree|hard link|pinned Granite model file",
                )


if __name__ == "__main__":
    unittest.main()
