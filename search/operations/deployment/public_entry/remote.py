"""Bind a CPU entrance to one sealed HTTPS backend; probe versions, never AI.

The operator supplies the expected API/catalog identity independently. A remote
binding does not inspect or join the GPU host's Docker networks. Credentials
remain in private files; commands and operator diagnostics never contain them.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys

from search.serialization import strict_json
from .. import frontend_check, image_build
from . import profile as public_entry


HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "origin",
        "api_revision",
        "catalog_manifest_sha256",
        "api_key_file",
        "ca_file",
    }
)
PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "origin",
        "api_revision",
        "catalog_manifest_sha256",
        "conversation_contract_version",
    }
)
API_KEY_TARGET = "/run/secrets/api_key"
CA_TARGET = "/run/certs/upstream-ca.pem"
CONVERSATION_CONTRACT_VERSION = 3
PROOF = {
    "authenticated_version_status": 200,
    "backend_identity_verified": True,
    "unauthenticated_version_status": 401,
    "upstream_tls_verified": True,
}
COMMAND_TIMEOUT = 15
MAX_OUTPUT_BYTES = 16 * 1024
ERROR_CODES = frozenset(
    "public_entry_remote_" + code
    for code in ("config_invalid", "plan_invalid", "probe_failed", "output_limit")
)


class PublicEntryRemoteError(ValueError):
    """Closed diagnostics without paths, credentials or remote response bytes."""


def _require(condition, code):
    if not condition:
        raise PublicEntryRemoteError("public_entry_remote_" + code)


def _identity(origin, revision, manifest):
    public_entry._url(origin, origin=True)
    _require(
        type(revision) is str
        and HEX40.fullmatch(revision) is not None
        and type(manifest) is str
        and HEX64.fullmatch(manifest) is not None,
        "config_invalid",
    )


@dataclass(frozen=True)
class RemoteBackend:
    schema_version: int
    origin: str
    api_revision: str
    catalog_manifest_sha256: str
    api_key_file: Path
    ca_file: Path

    def to_plan(self):
        return {
            "schema_version": 2,
            "kind": "https",
            "origin": self.origin,
            "api_revision": self.api_revision,
            "catalog_manifest_sha256": self.catalog_manifest_sha256,
            "conversation_contract_version": CONVERSATION_CONTRACT_VERSION,
        }


def load_remote_backend(path, *, repo_root=None):
    """Read a private explicit backend configuration without network access."""
    repo_root = public_entry.ROOT if repo_root is None else Path(repo_root).resolve()
    uid = os.geteuid()
    try:
        path = public_entry._path(str(path), repo_root)
        value = strict_json(public_entry._read_private(path, uid=uid, maximum=16384))
        _require(type(value) is dict and set(value) == CONFIG_FIELDS, "config_invalid")
        _require(
            type(value["schema_version"]) is int and value["schema_version"] == 1,
            "config_invalid",
        )
        _identity(
            value["origin"], value["api_revision"], value["catalog_manifest_sha256"]
        )
        key = public_entry._path(value["api_key_file"], repo_root)
        ca = public_entry._path(value["ca_file"], repo_root)
        _require(len({path, key, ca}) == 3, "config_invalid")
        raw_key = public_entry._read_private(key, uid=uid, maximum=65)
        _require(
            re.fullmatch(rb"[0-9a-f]{64}\n?", raw_key) is not None, "config_invalid"
        )
        raw_ca = public_entry._read_private(ca, uid=uid, maximum=256 * 1024)
        _require(b"-----BEGIN CERTIFICATE-----" in raw_ca, "config_invalid")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=raw_ca.decode("ascii"))
        return RemoteBackend(
            1,
            value["origin"],
            value["api_revision"],
            value["catalog_manifest_sha256"],
            key,
            ca,
        )
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise PublicEntryRemoteError("public_entry_remote_config_invalid") from None


def _binding(profile):
    try:
        plan = profile["x-depositar-plan"]["private_backend"]
        _require(type(plan) is dict and set(plan) == PLAN_FIELDS, "plan_invalid")
        _require(
            type(plan["schema_version"]) is int
            and plan["schema_version"] == 2
            and plan["kind"] == "https"
            and type(plan["conversation_contract_version"]) is int
            and plan["conversation_contract_version"] == CONVERSATION_CONTRACT_VERSION,
            "plan_invalid",
        )
        _identity(plan["origin"], plan["api_revision"], plan["catalog_manifest_sha256"])
        web = profile["services"]["public-web"]
        environment = web["environment"]
        _require(
            type(environment) is dict
            and environment.get("WEB_UPSTREAM") == plan["origin"]
            and environment.get("WEB_UPSTREAM_CA_FILE") == CA_TARGET
            and environment.get("NVIDIA_VISIBLE_DEVICES") == "void"
            and web.get("runtime") == "runc"
            and not web.get("ports")
            and not web.get("devices")
            and not web.get("deploy", {}).get("resources", {}).get("reservations"),
            "plan_invalid",
        )
        networks = profile["networks"]
        _require(
            "existing-release" not in networks
            and set(web["networks"]) == {"website", "outbound"}
            and networks["website"].get("internal") is True
            and networks["outbound"].get("internal") is False,
            "plan_invalid",
        )
        paths = {}
        for target in (API_KEY_TARGET, CA_TARGET):
            mounts = [m for m in web["volumes"] if m.get("target") == target]
            _require(
                len(mounts) == 1
                and mounts[0].get("type") == "bind"
                and mounts[0].get("read_only") is True,
                "plan_invalid",
            )
            paths[target] = public_entry._path(mounts[0]["source"], public_entry.ROOT)
        _require(paths[API_KEY_TARGET] != paths[CA_TARGET], "plan_invalid")
        return plan, paths
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise PublicEntryRemoteError("public_entry_remote_plan_invalid") from None


def verify_remote_backend(profile, *, runner=subprocess.run, compose_file=None):
    """Check authenticated TLS identity from the host or the deployed CPU BFF.

    Container checks use the mounted key/CA inside that container. Both variants
    execute the existing bounded version-only probe; neither submits a turn.
    """
    plan, paths = _binding(profile)
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    if compose_file is None:
        source = (
            "import sys\nsys.path.insert(0, "
            + repr(str(frontend_check.ROOT))
            + ")\n"
            + frontend_check._probe_source(
                key_file=str(paths[API_KEY_TARGET]),
                contract_version=CONVERSATION_CONTRACT_VERSION,
            )
        )
        argv = [sys.executable, "-E", "-s", "-B", "-c", source]
        environment.update(
            WEB_UPSTREAM=plan["origin"], WEB_UPSTREAM_CA_FILE=str(paths[CA_TARGET])
        )
    else:
        try:
            compose_file = public_entry._path(str(compose_file), public_entry.ROOT)
        except (OSError, ValueError, TypeError):
            raise PublicEntryRemoteError("public_entry_remote_plan_invalid") from None
        argv = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "exec",
            "-T",
            "public-web",
            "python",
            "-E",
            "-s",
            "-B",
            "-c",
            frontend_check._probe_source(
                contract_version=CONVERSATION_CONTRACT_VERSION
            ),
        ]
        # Use the same operator-selected daemon/context as the surrounding
        # lifecycle commands, including rootless Docker. Never change daemons
        # between Compose startup and its in-container identity check.
        for name in (
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            if name in os.environ:
                environment[name] = os.environ[name]
    argv.extend([plan["api_revision"], plan["catalog_manifest_sha256"]])
    try:
        if runner is subprocess.run:
            result = image_build._run(
                argv, timeout=COMMAND_TIMEOUT, environment=environment
            )
        else:
            result = runner(
                argv,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        stdout, stderr = result.stdout, result.stderr
        _require(type(stdout) is bytes and type(stderr) is bytes, "probe_failed")
        _require(len(stdout) + len(stderr) <= MAX_OUTPUT_BYTES, "output_limit")
        _require(
            type(result.returncode) is int and result.returncode == 0, "probe_failed"
        )
        proof = strict_json(stdout)
        _require(
            type(proof) is dict
            and proof == PROOF
            and type(proof.get("authenticated_version_status")) is int
            and type(proof.get("unauthenticated_version_status")) is int
            and type(proof.get("backend_identity_verified")) is bool
            and type(proof.get("upstream_tls_verified")) is bool,
            "probe_failed",
        )
        return proof
    except PublicEntryRemoteError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        UnicodeError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        raise PublicEntryRemoteError("public_entry_remote_probe_failed") from None
