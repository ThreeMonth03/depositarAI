"""Public frontend release transport and verified, single-platform OCI identity.

GitHub CLI owns Sigstore verification. This module restricts the signed source
identity and archive format before the deployment controller may call Docker.
No credential, downloaded script, image build, or production operation is used.
"""

from __future__ import annotations

import hashlib
import http.client
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import threading
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from search.operations.deployment.images import build as image_build
from search.operations.shared.bounded_http import AbortableConnection
from search.serialization import strict_json
from search.transport.version import CONVERSATION_CONTRACT_VERSION
from . import policy


WORKFLOW = ".github/workflows/frontend-release.yml"
SOURCE_REF = "refs/heads/main"
MANIFEST_FILE = "frontend-release.json"
BUNDLE_FILE = "frontend-release.sigstore.json"
ROLES = ("web", "web-nginx")
PLATFORM = {"architecture": "amd64", "os": "linux"}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
MAX_ARCHIVE_BYTES = 1024**3
MAX_METADATA_BYTES = 1024**2
CHUNK = 1024**2
DOWNLOAD_SECONDS = 240
IMAGE_FIELDS = {
    "archive",
    "archive_sha256",
    "archive_bytes",
    "platform_manifest",
    "image_config",
}
MANIFEST_FIELDS = {
    "schema_version",
    "repository",
    "revision",
    "source_ref",
    "workflow",
    "platform",
    "conversation_contract_version",
    "images",
}


class FrontendArtifactError(ValueError):
    """Closed diagnostics: never include HTTP, signature or daemon output."""


def _require(condition, code):
    if not condition:
        raise FrontendArtifactError("frontend_artifact_" + code)


def _identity(repository, revision):
    _require(
        type(repository) is str
        and REPOSITORY.fullmatch(repository)
        and repository.split("/")[1] not in {".", ".."}
        and type(revision) is str
        and HEX40.fullmatch(revision),
        "identity_invalid",
    )


def validate_manifest(value, *, repository, revision):
    _identity(repository, revision)
    _require(type(value) is dict and set(value) == MANIFEST_FIELDS, "manifest_invalid")
    _require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["repository"] == repository
        and value["revision"] == revision
        and value["source_ref"] == SOURCE_REF
        and value["workflow"] == WORKFLOW
        and value["platform"] == "linux/amd64"
        and type(value["conversation_contract_version"]) is int
        and value["conversation_contract_version"] == CONVERSATION_CONTRACT_VERSION
        and type(value["images"]) is dict
        and set(value["images"]) == set(ROLES),
        "manifest_invalid",
    )
    for role in ROLES:
        item = value["images"][role]
        _require(type(item) is dict and set(item) == IMAGE_FIELDS, "manifest_invalid")
        _require(
            item["archive"] == role + ".oci.tar"
            and type(item["archive_bytes"]) is int
            and 0 < item["archive_bytes"] <= MAX_ARCHIVE_BYTES
            and type(item["archive_sha256"]) is str
            and HEX64.fullmatch(item["archive_sha256"])
            and all(
                type(item[k]) is str and DIGEST.fullmatch(item[k])
                for k in ("platform_manifest", "image_config")
            ),
            "manifest_invalid",
        )
    return value


def _open_regular(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        _require(
            stat.S_ISREG(info.st_mode) and 0 < info.st_size <= maximum, "file_invalid"
        )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def load_manifest(path, *, repository, revision):
    with _open_regular(path, 16384) as stream:
        value = strict_json(stream.read(16385))
    return validate_manifest(value, repository=repository, revision=revision)


def _hash(stream):
    digest = hashlib.sha256()
    while block := stream.read(CHUNK):
        digest.update(block)
    return digest.hexdigest()


def describe_archive(path, revision, role):
    """Describe Docker29 containerd `image save --platform linux/amd64` bytes.

    Never extract archive paths. Admit one image, its exact native tag, and the
    closed graph of SHA256-addressed blobs; reject extra images and Docker-only
    archives. The signed archive hash and existing runtime checker remain the
    before-load and after-load authorities respectively.
    """
    _require(
        role in ROLES and type(revision) is str and HEX40.fullmatch(revision),
        "identity_invalid",
    )
    path = Path(path)
    _require(path.name == role + ".oci.tar", "archive_invalid")
    with _open_regular(path, MAX_ARCHIVE_BYTES) as stream:
        info = os.fstat(stream.fileno())
        archive_sha256 = _hash(stream)
        stream.seek(0)
        try:
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                members = {}
                for member in archive:
                    _require(
                        len(members) < 512 and member.name not in members,
                        "archive_invalid",
                    )
                    name = member.name.rstrip("/") if member.isdir() else member.name
                    _require(name not in members, "archive_invalid")
                    if member.isdir():
                        _require(name in {"blobs", "blobs/sha256"}, "archive_invalid")
                    else:
                        _require(
                            member.isfile()
                            and member.size <= MAX_ARCHIVE_BYTES
                            and (
                                name in {"index.json", "oci-layout", "manifest.json"}
                                or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name)
                            ),
                            "archive_invalid",
                        )
                    members[name] = member

                def read_json(name):
                    _require(
                        name in members
                        and members[name].isfile()
                        and members[name].size <= MAX_METADATA_BYTES,
                        "archive_invalid",
                    )
                    with archive.extractfile(members[name]) as item:
                        return strict_json(item.read(MAX_METADATA_BYTES + 1))

                visited = set()

                def blob(descriptor, media_types, *, json_value=False):
                    _require(
                        type(descriptor) is dict
                        and descriptor.get("mediaType") in media_types
                        and type(descriptor.get("digest")) is str
                        and DIGEST.fullmatch(descriptor["digest"])
                        and type(descriptor.get("size")) is int
                        and 0 < descriptor["size"] <= MAX_ARCHIVE_BYTES,
                        "archive_invalid",
                    )
                    name = "blobs/sha256/" + descriptor["digest"][7:]
                    _require(
                        name in members
                        and members[name].isfile()
                        and members[name].size == descriptor["size"],
                        "archive_invalid",
                    )
                    with archive.extractfile(members[name]) as item:
                        _require(
                            _hash(item) == descriptor["digest"][7:], "archive_invalid"
                        )
                    visited.add(name)
                    return read_json(name) if json_value else None

                _require(
                    read_json("oci-layout") == {"imageLayoutVersion": "1.0.0"},
                    "archive_invalid",
                )
                index = read_json("index.json")
                _require(
                    type(index) is dict
                    and index.get("schemaVersion") == 2
                    and type(index.get("manifests")) is list
                    and len(index["manifests"]) == 1,
                    "archive_invalid",
                )
                descriptor = index["manifests"][0]
                tag = "depositar-search-" + role + ":" + revision
                _require(
                    descriptor.get("platform") == PLATFORM
                    and descriptor.get("annotations", {}).get(
                        "io.containerd.image.name"
                    )
                    == "docker.io/library/" + tag
                    and descriptor["annotations"].get(
                        "org.opencontainers.image.ref.name"
                    )
                    == revision,
                    "archive_invalid",
                )
                image = blob(descriptor, {OCI_MANIFEST}, json_value=True)
                _require(
                    type(image) is dict
                    and image.get("schemaVersion") == 2
                    and image.get("mediaType") == OCI_MANIFEST,
                    "archive_invalid",
                )
                config_descriptor = image["config"]
                config = blob(config_descriptor, {OCI_CONFIG}, json_value=True)
                _require(
                    type(config) is dict
                    and config.get("os") == "linux"
                    and config.get("architecture") == "amd64",
                    "archive_invalid",
                )
                policy.validate_image_labels(config["config"]["Labels"], revision, role)
                layers = image["layers"]
                _require(
                    type(layers) is list and 0 < len(layers) <= 128, "archive_invalid"
                )
                for layer in layers:
                    blob(
                        layer,
                        {
                            "application/vnd.oci.image.layer.v1.tar",
                            "application/vnd.oci.image.layer.v1.tar+gzip",
                            "application/vnd.oci.image.layer.v1.tar+zstd",
                        },
                    )
                compatibility = read_json("manifest.json")
                _require(
                    type(compatibility) is list
                    and len(compatibility) == 1
                    and compatibility[0].get("RepoTags") == [tag]
                    and compatibility[0].get("Config")
                    == "blobs/sha256/" + config_descriptor["digest"][7:]
                    and compatibility[0].get("Layers")
                    == ["blobs/sha256/" + layer["digest"][7:] for layer in layers]
                    and {n for n in members if n.startswith("blobs/sha256/")}
                    == visited,
                    "archive_invalid",
                )
                after = os.fstat(stream.fileno())
                named = path.lstat()
                stable_fields = (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_nlink",
                    "st_uid",
                    "st_gid",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
                _require(
                    all(
                        getattr(info, field)
                        == getattr(after, field)
                        == getattr(named, field)
                        for field in stable_fields
                    ),
                    "archive_changed",
                )
                return {
                    "archive": path.name,
                    "archive_sha256": archive_sha256,
                    "archive_bytes": info.st_size,
                    "platform_manifest": descriptor["digest"],
                    "image_config": config_descriptor["digest"],
                }
        except (tarfile.TarError, KeyError, TypeError, AttributeError, EOFError):
            raise FrontendArtifactError("frontend_artifact_archive_invalid") from None


def validate_archive(path, image_entry, revision, role):
    _require(
        describe_archive(path, revision, role) == image_entry,
        "archive_identity_changed",
    )


_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


def _url(url):
    parsed = urlsplit(url)
    _require(
        parsed.scheme == "https"
        and parsed.hostname in _DOWNLOAD_HOSTS
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and not parsed.fragment,
        "download_origin_invalid",
    )


class _ReleaseRedirect(HTTPRedirectHandler):
    max_repeats = 2
    max_redirections = 3

    def redirect_request(self, request, response, code, message, headers, newurl):
        _url(newurl)
        return super().redirect_request(
            request, response, code, message, headers, newurl
        )


class _DownloadConnections(HTTPSHandler):
    """One watchdog owns every retained channel, including redirect responses."""

    def __init__(self):
        super().__init__()
        self.stopped = threading.Event()
        self.connections = []

    def abort(self):
        self.stopped.set()
        for connection in tuple(self.connections):
            connection.abort()

    def finish(self):
        for connection in self.connections:
            connection.finish()

    def https_open(self, request):
        owner = self

        class Connection(AbortableConnection, http.client.HTTPSConnection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._init_abortable()
                owner.connections.append(self)
                if owner.stopped.is_set():
                    self.abort()
                    raise TimeoutError("frontend_artifact_download_deadline")

            def connect(self):
                http.client.HTTPConnection.connect(self)
                self._publish_channel(self.sock)
                self._publish_channel(
                    self._context.wrap_socket(
                        self.sock,
                        server_hostname=self.host,
                    )
                )

        return self.do_open(Connection, request)


def _download(url, *, maximum, destination=None, expected_bytes=None):
    _url(url)
    connections = _DownloadConnections()
    opener = build_opener(ProxyHandler({}), _ReleaseRedirect(), connections)
    request = Request(url, headers={"User-Agent": "depositarAI-frontend-delivery/1"})
    watchdog = threading.Timer(
        min(DOWNLOAD_SECONDS, 30)
        if maximum <= MAX_METADATA_BYTES
        else DOWNLOAD_SECONDS,
        connections.abort,
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        with opener.open(request, timeout=15) as response:
            _require(response.status == 200, "download_failed")
            _url(response.url)
            if destination is None:
                raw = response.read(maximum + 1)
                _require(
                    not connections.stopped.is_set() and 0 < len(raw) <= maximum,
                    "download_limit",
                )
                return raw
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                size = 0
                while block := response.read(min(CHUNK, maximum + 1 - size)):
                    size += len(block)
                    _require(
                        not connections.stopped.is_set() and size <= maximum,
                        "download_limit",
                    )
                    stream.write(block)
                _require(
                    not connections.stopped.is_set()
                    and size > 0
                    and (expected_bytes is None or size == expected_bytes),
                    "download_size",
                )
    finally:
        watchdog.cancel()
        connections.finish()


def _verify_provenance(
    path, bundle, *, revision, repository, repository_id, gh_binary, runner
):
    _require(type(repository_id) is int and repository_id > 0, "identity_invalid")
    binary = Path(gh_binary)
    _require(binary.is_absolute(), "verifier_invalid")
    info = binary.lstat()
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid in {0, os.geteuid()}
        and info.st_mode & 0o111
        and not info.st_mode & 0o022,
        "verifier_invalid",
    )
    with tempfile.TemporaryDirectory(
        prefix="depositar-frontend-verifier-"
    ) as temporary:
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": temporary,
            "GH_CONFIG_DIR": temporary + "/gh",
            "XDG_CACHE_HOME": temporary + "/cache",
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
        }
        result = runner(
            [
                str(binary),
                "attestation",
                "verify",
                str(path),
                "--bundle",
                str(bundle),
                "--repo",
                repository,
                "--source-ref",
                SOURCE_REF,
                "--source-digest",
                revision,
                "--signer-digest",
                revision,
                "--signer-workflow",
                repository + "/" + WORKFLOW,
                "--deny-self-hosted-runners",
                "--format",
                "json",
            ],
            timeout=90,
            environment=environment,
        )
    _require(
        type(result.returncode) is int and result.returncode == 0, "provenance_invalid"
    )
    raw = result.stdout
    if type(raw) is str:
        raw = raw.encode()
    _require(
        type(raw) is bytes and 0 < len(raw) <= 2 * MAX_METADATA_BYTES,
        "provenance_invalid",
    )
    verified = strict_json(raw)
    _require(type(verified) is list and 0 < len(verified) <= 8, "provenance_invalid")
    try:
        for item in verified:
            certificate = item["verificationResult"]["signature"]["certificate"]
            _require(
                certificate["sourceRepositoryIdentifier"] == str(repository_id),
                "provenance_invalid",
            )
    except (KeyError, TypeError):
        raise FrontendArtifactError("frontend_artifact_provenance_invalid") from None


def fetch_release(
    revision, repository, destination, gh_binary, *, repository_id, runner=None
):
    """Fetch an exact public release without GitHub credentials or Docker calls."""
    _identity(repository, revision)
    destination = Path(destination)
    info = destination.lstat()
    _require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700
        and not list(destination.iterdir()),
        "destination_invalid",
    )
    try:
        tag = "frontend-" + revision
        metadata = strict_json(
            _download(
                f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
                maximum=128 * 1024,
            )
        )
        _require(
            type(metadata) is dict
            and metadata.get("tag_name") == tag
            and metadata.get("draft") is False
            and metadata.get("prerelease") is False
            and metadata.get("target_commitish") == revision,
            "release_invalid",
        )
        assets = metadata["assets"]
        expected = {MANIFEST_FILE, BUNDLE_FILE, *(role + ".oci.tar" for role in ROLES)}
        _require(
            type(assets) is list and len(assets) == len(expected), "release_invalid"
        )
        entries = {}
        for item in assets:
            name = item["name"]
            _require(
                name in expected
                and name not in entries
                and item["state"] == "uploaded"
                and type(item["size"]) is int
                and item["size"] > 0
                and item["browser_download_url"]
                == f"https://github.com/{repository}/releases/download/{tag}/{name}",
                "release_invalid",
            )
            entries[name] = item
        for name, limit in ((MANIFEST_FILE, 16384), (BUNDLE_FILE, 512 * 1024)):
            item = entries[name]
            _require(item["size"] <= limit, "download_limit")
            _download(
                item["browser_download_url"],
                maximum=limit,
                destination=destination / name,
                expected_bytes=item["size"],
            )
        _verify_provenance(
            destination / MANIFEST_FILE,
            destination / BUNDLE_FILE,
            revision=revision,
            repository=repository,
            repository_id=repository_id,
            gh_binary=gh_binary,
            runner=image_build._run if runner is None else runner,
        )
        manifest = load_manifest(
            destination / MANIFEST_FILE, repository=repository, revision=revision
        )
        for role in ROLES:
            item = manifest["images"][role]
            asset = entries[item["archive"]]
            _require(asset["size"] == item["archive_bytes"], "download_size")
            path = destination / item["archive"]
            _download(
                asset["browser_download_url"],
                maximum=item["archive_bytes"],
                destination=path,
                expected_bytes=item["archive_bytes"],
            )
            validate_archive(path, item, revision, role)
        return manifest
    except FrontendArtifactError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        http.client.HTTPException,
    ):
        raise FrontendArtifactError("frontend_artifact_fetch_failed") from None
