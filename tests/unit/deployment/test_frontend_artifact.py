"""Real OCI parsing and fake signature/HTTP edges; no Docker or network.

The fake verifier exercises the caller's identity policy and failure ordering;
it does not claim to validate a real Sigstore signature.
"""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from search.operations.deployment import frontend_artifact as artifact
from search.operations.shared.bounded_http import AbortableConnection


REVISION = "a" * 40
REPOSITORY = "ThreeMonth03/depositarAI"
REPOSITORY_ID = 1425391257
PRIVATE = "private-owner-credential-must-not-be-forwarded"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def oci_archive(path, role="web", *, mutate=None, architecture="amd64"):
    """Build a genuine single-platform OCI graph plus Docker save metadata."""
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as archive:
        content = b"controlled fixture\n"
        member = tarfile.TarInfo("fixture.txt")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    layer = layer.getvalue()
    labels = {
        "org.opencontainers.image.revision": REVISION,
        "io.depositar.ai.public-entry.version": "2",
        **(
            {"io.depositar.ai.conversation.version": "4"}
            if role == "web"
            else {"io.depositar.ai.frontend.version": "1"}
        ),
    }
    config = encoded(
        {
            "architecture": architecture,
            "os": "linux",
            "config": {"Labels": labels},
            "rootfs": {"type": "layers", "diff_ids": [digest(layer)]},
        }
    )
    config_descriptor = {
        "mediaType": artifact.OCI_CONFIG,
        "digest": digest(config),
        "size": len(config),
    }
    layer_descriptor = {
        "mediaType": "application/vnd.oci.image.layer.v1.tar",
        "digest": digest(layer),
        "size": len(layer),
    }
    image = encoded(
        {
            "schemaVersion": 2,
            "mediaType": artifact.OCI_MANIFEST,
            "config": config_descriptor,
            "layers": [layer_descriptor],
        }
    )
    tag = "depositar-search-" + role + ":" + REVISION
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": artifact.OCI_MANIFEST,
                "digest": digest(image),
                "size": len(image),
                "platform": dict(artifact.PLATFORM),
                "annotations": {
                    "io.containerd.image.name": "docker.io/library/" + tag,
                    "org.opencontainers.image.ref.name": REVISION,
                },
            }
        ],
    }
    blobs = {
        "blobs/sha256/" + digest(value)[7:]: value for value in (image, config, layer)
    }
    members = {
        "oci-layout": encoded({"imageLayoutVersion": "1.0.0"}),
        "index.json": encoded(index),
        "manifest.json": encoded(
            [
                {
                    "Config": "blobs/sha256/" + digest(config)[7:],
                    "RepoTags": [tag],
                    "Layers": ["blobs/sha256/" + digest(layer)[7:]],
                }
            ]
        ),
        **blobs,
    }
    if mutate:
        mutate(members)
    with tarfile.open(path, "w") as archive:
        for name, raw in members.items():
            if isinstance(raw, tarfile.TarInfo):
                archive.addfile(raw)
            else:
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
    raw = path.read_bytes()
    return {
        "archive": path.name,
        "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "archive_bytes": len(raw),
        "platform_manifest": digest(image),
        "image_config": digest(config),
    }


def update_json(members, name, change):
    value = json.loads(members[name])
    change(value)
    members[name] = encoded(value)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frontend-artifact-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "web.oci.tar"

    def test_both_roles_preserve_platform_manifest_and_config_identity(self):
        for role in artifact.ROLES:
            with self.subTest(role=role):
                path = self.root / (role + ".oci.tar")
                expected = oci_archive(path, role)
                self.assertEqual(
                    artifact.describe_archive(path, REVISION, role), expected
                )
                artifact.validate_archive(path, expected, REVISION, role)

    def test_rejects_tar_paths_links_extra_images_and_incomplete_graphs(self):
        def symlink(members):
            member = tarfile.TarInfo("blobs/sha256/" + "f" * 64)
            member.type, member.linkname = tarfile.SYMTYPE, "/untrusted-target"
            members[member.name] = member

        def bad_config_digest(members):
            name = next(
                name
                for name, value in members.items()
                if name.startswith("blobs/") and b'"rootfs"' in value
            )
            members[name] += b" "

        def bad_layer_hash(members):
            name = next(
                name
                for name, value in members.items()
                if name.startswith("blobs/") and len(value) == 10240
            )
            members[name] = b"x" + members[name][1:]

        def extra_platform(members):
            def change(value):
                other = copy.deepcopy(value["manifests"][0])
                other["platform"]["architecture"] = "arm64"
                value["manifests"].append(other)

            update_json(members, "index.json", change)

        mutations = {
            "parent-path": lambda m: m.update({"../escape": b"bad"}),
            "absolute-path": lambda m: m.update({"/escape": b"bad"}),
            "symlink": symlink,
            "extra-platform": extra_platform,
            "wrong-platform": lambda m: update_json(
                m,
                "index.json",
                lambda v: v["manifests"][0]["platform"].update(architecture="arm64"),
            ),
            "wrong-tag": lambda m: update_json(
                m, "manifest.json", lambda v: v[0].update(RepoTags=["unapproved:tag"])
            ),
            "extra-image-tag": lambda m: update_json(
                m, "manifest.json", lambda v: v[0]["RepoTags"].append("unapproved:tag")
            ),
            "config-hash": bad_config_digest,
            "layer-hash": bad_layer_hash,
            "missing-layer": lambda m: m.pop(
                next(
                    name
                    for name, value in m.items()
                    if name.startswith("blobs/") and len(value) == 10240
                )
            ),
            "extra-blob": lambda m: m.update({"blobs/sha256/" + "f" * 64: b"extra"}),
            "missing-index": lambda m: m.pop("index.json"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                oci_archive(self.path, mutate=mutate)
                with self.assertRaises(artifact.FrontendArtifactError):
                    artifact.describe_archive(self.path, REVISION, "web")

    def test_config_architecture_must_agree_with_signed_platform(self):
        oci_archive(self.path, architecture="arm64")
        with self.assertRaises(artifact.FrontendArtifactError):
            artifact.describe_archive(self.path, REVISION, "web")

    def test_attested_outer_hash_and_config_digest_are_required(self):
        expected = oci_archive(self.path)
        for field in ("archive_sha256", "platform_manifest", "image_config"):
            with self.subTest(field=field):
                changed = {
                    **expected,
                    field: ("" if field == "archive_sha256" else "sha256:") + "f" * 64,
                }
                with self.assertRaisesRegex(
                    artifact.FrontendArtifactError, "archive_identity_changed"
                ):
                    artifact.validate_archive(self.path, changed, REVISION, "web")

    def test_non_tar_and_symlink_inputs_are_rejected(self):
        self.path.write_bytes(b"not an archive")
        with self.assertRaises(artifact.FrontendArtifactError):
            artifact.describe_archive(self.path, REVISION, "web")
        target = self.root / "target"
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            artifact.describe_archive(self.path, REVISION, "web")

    def test_archive_size_limit_is_checked_before_hashing_or_parsing(self):
        oci_archive(self.path)
        with (
            mock.patch.object(
                artifact, "MAX_ARCHIVE_BYTES", self.path.stat().st_size - 1
            ),
            mock.patch.object(artifact, "_hash") as hashed,
            self.assertRaises(artifact.FrontendArtifactError),
        ):
            artifact.describe_archive(self.path, REVISION, "web")
        hashed.assert_not_called()

    def test_first_read_atime_change_does_not_look_like_content_mutation(self):
        expected = oci_archive(self.path)
        real_fstat, observations = os.fstat, []

        def changed_atime(descriptor):
            info = real_fstat(descriptor)
            values = {
                name: getattr(info, name)
                for name in dir(info)
                if name.startswith("st_")
            }
            values.update(
                st_atime=len(observations) + 1, st_atime_ns=len(observations) + 1
            )
            observations.append(values["st_atime_ns"])
            return SimpleNamespace(**values)

        with mock.patch.object(artifact.os, "fstat", side_effect=changed_atime):
            self.assertEqual(
                artifact.describe_archive(self.path, REVISION, "web"), expected
            )
        self.assertGreater(len(set(observations)), 1)

    def test_named_path_replacement_is_rejected_even_when_open_bytes_stay_valid(self):
        oci_archive(self.path)
        real_hash, changed = artifact._hash, []

        def swap_after_hash(stream):
            value = real_hash(stream)
            if not changed:
                changed.append(True)
                replacement = self.root / "replacement"
                replacement.write_bytes(self.path.read_bytes())
                replacement.replace(self.path)
            return value

        with mock.patch.object(artifact, "_hash", side_effect=swap_after_hash):
            with self.assertRaisesRegex(
                artifact.FrontendArtifactError, "archive_changed"
            ):
                artifact.describe_archive(self.path, REVISION, "web")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frontend-release-fetch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "destination"
        self.destination.mkdir(mode=0o700)
        self.binary = self.root / "gh"
        self.binary.write_text("#!/bin/sh\nexit 1\n")
        self.binary.chmod(0o700)
        self.events, self.verify_calls = [], []
        entries, self.assets = {}, {}
        for role in artifact.ROLES:
            path = self.root / (role + ".oci.tar")
            entries[role] = oci_archive(path, role)
            self.assets[path.name] = path.read_bytes()
        self.manifest = {
            "schema_version": 1,
            "repository": REPOSITORY,
            "revision": REVISION,
            "source_ref": "refs/heads/main",
            "workflow": artifact.WORKFLOW,
            "platform": "linux/amd64",
            "conversation_contract_version": 4,
            "images": entries,
        }
        self.assets[artifact.MANIFEST_FILE] = encoded(self.manifest)
        self.assets[artifact.BUNDLE_FILE] = b"{}"
        tag = "frontend-" + REVISION
        self.metadata = {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "target_commitish": REVISION,
            "assets": [
                {
                    "name": name,
                    "state": "uploaded",
                    "size": len(raw),
                    "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}",
                }
                for name, raw in self.assets.items()
            ],
        }
        self.verified = [
            {
                "verificationResult": {
                    "signature": {
                        "certificate": {
                            "sourceRepositoryIdentifier": str(REPOSITORY_ID),
                        }
                    }
                }
            }
        ]
        self.verifier_status = 0

    def download(self, url, *, maximum, destination=None, expected_bytes=None):
        if destination is None:
            self.events.append("metadata")
            return encoded(self.metadata)
        name = Path(destination).name
        self.events.append(name)
        raw = self.assets[name]
        self.assertLessEqual(len(raw), maximum)
        self.assertEqual(len(raw), expected_bytes)
        Path(destination).write_bytes(raw)

    def verify(self, arguments, *, timeout, environment):
        self.events.append("verify")
        self.verify_calls.append((arguments, timeout, environment))
        return SimpleNamespace(
            returncode=self.verifier_status,
            stdout=encoded(self.verified),
            stderr=PRIVATE,
        )

    def fetch(self):
        with mock.patch.object(artifact, "_download", side_effect=self.download):
            return artifact.fetch_release(
                REVISION,
                REPOSITORY,
                self.destination,
                self.binary,
                repository_id=REPOSITORY_ID,
                runner=self.verify,
            )

    def test_verification_precedes_archives_and_does_not_inherit_credentials(self):
        with mock.patch.dict(
            os.environ, {"GH_TOKEN": PRIVATE, "GITHUB_TOKEN": PRIVATE}
        ):
            self.assertEqual(self.fetch(), self.manifest)
        self.assertEqual(
            self.events,
            [
                "metadata",
                artifact.MANIFEST_FILE,
                artifact.BUNDLE_FILE,
                "verify",
                "web.oci.tar",
                "web-nginx.oci.tar",
            ],
        )
        arguments, timeout, environment = self.verify_calls[0]
        self.assertEqual(arguments[:3], [str(self.binary), "attestation", "verify"])
        for flag, value in {
            "--repo": REPOSITORY,
            "--source-ref": "refs/heads/main",
            "--source-digest": REVISION,
            "--signer-digest": REVISION,
            "--signer-workflow": REPOSITORY + "/" + artifact.WORKFLOW,
            "--format": "json",
        }.items():
            self.assertEqual(arguments[arguments.index(flag) + 1], value)
        self.assertIn("--deny-self-hosted-runners", arguments)
        self.assertEqual(timeout, 90)
        self.assertTrue(Path(environment["GH_CONFIG_DIR"]).is_absolute())
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn(PRIVATE, json.dumps(environment))

    def test_failed_source_or_signature_verification_never_fetches_archives(self):
        self.verifier_status = 1
        with self.assertRaises(artifact.FrontendArtifactError) as raised:
            self.fetch()
        self.assertNotIn(PRIVATE, str(raised.exception))
        self.assertEqual(self.events[-1], "verify")
        self.assertFalse(list(self.destination.glob("*.tar")))

    def test_repository_id_must_come_from_verified_certificate_as_a_string(self):
        for index, identifier in enumerate(("1425391258", REPOSITORY_ID, None)):
            with self.subTest(identifier=identifier):
                self.destination = self.root / ("repository-case-" + str(index))
                self.destination.mkdir(mode=0o700)
                self.verify_calls.clear()
                certificate = (
                    {}
                    if identifier is None
                    else {"sourceRepositoryIdentifier": identifier}
                )
                self.verified = [
                    {
                        "verificationResult": {
                            "signature": {"certificate": certificate},
                            "statement": {
                                "predicate": {
                                    "sourceRepositoryIdentifier": str(REPOSITORY_ID)
                                }
                            },
                        }
                    }
                ]
                with self.assertRaises(artifact.FrontendArtifactError):
                    self.fetch()
                self.assertEqual(len(self.verify_calls), 1)
                self.assertEqual(self.events[-1], "verify")
                self.assertFalse(list(self.destination.glob("*.tar")))

    def test_release_inventory_and_destination_fail_before_provenance(self):
        self.metadata["assets"][0]["browser_download_url"] = (
            "https://untrusted.example/asset"
        )
        with self.assertRaises(artifact.FrontendArtifactError):
            self.fetch()
        self.assertEqual(self.verify_calls, [])
        self.assertEqual(self.events, ["metadata"])
        self.destination.chmod(0o755)
        with self.assertRaises(artifact.FrontendArtifactError):
            self.fetch()

    def test_manifest_is_closed_and_uses_the_exact_supported_contract(self):
        mutations = {
            "source-ref": {"source_ref": "refs/heads/untrusted"},
            "workflow": {"workflow": ".github/workflows/other.yml"},
            "revision": {"revision": "b" * 40},
            "contract": {"conversation_contract_version": 3},
            "bool-contract": {"conversation_contract_version": True},
            "platform": {"platform": "linux/arm64"},
            "unknown-field": {"extra": True},
        }
        for name, change in mutations.items():
            with self.subTest(name=name):
                with self.assertRaises(artifact.FrontendArtifactError):
                    artifact.validate_manifest(
                        {**self.manifest, **change},
                        repository=REPOSITORY,
                        revision=REVISION,
                    )


class DownloadTests(unittest.TestCase):
    def test_untrusted_origin_and_redirect_are_rejected_without_opening(self):
        for url in (
            "http://github.com/file",
            "https://untrusted.example/file",
            "https://user:password@github.com/file",
            "https://github.com:444/file",
            "https://github.com/file#fragment",
        ):
            with (
                self.subTest(url=url),
                mock.patch.object(artifact, "build_opener") as opener,
            ):
                with self.assertRaises(artifact.FrontendArtifactError):
                    artifact._download(url, maximum=100)
                opener.assert_not_called()
        with self.assertRaises(artifact.FrontendArtifactError):
            artifact._ReleaseRedirect().redirect_request(
                None,
                None,
                302,
                "redirect",
                {},
                "https://untrusted.example/file",
            )

    def test_global_watchdog_interrupts_a_slow_drip_and_closes_retained_channel(self):
        channel = SimpleNamespace(stopped=threading.Event(), closed=False)

        def shutdown(_how):
            channel.stopped.set()

        def close():
            channel.closed = True

        channel.shutdown, channel.close = shutdown, close

        class Connection(AbortableConnection):
            def __init__(self):
                self._init_abortable()
                self._publish_channel(channel)

            def close(self):
                pass

        class Response:
            status, url = 200, "https://github.com/file"
            progress = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _maximum):
                # Data keeps arriving inside one buffered read. A socket idle
                # timeout alone would not bound this operation's duration.
                self.progress += 1
                while not channel.stopped.wait(0.002):
                    self.progress += 1
                return b"x" * self.progress

        response = Response()

        def opener(*handlers):
            owner = next(
                h for h in handlers if isinstance(h, artifact._DownloadConnections)
            )
            owner.connections.append(Connection())
            return SimpleNamespace(open=lambda *_args, **_kwargs: response)

        started = time.monotonic()
        with (
            mock.patch.object(artifact, "build_opener", side_effect=opener),
            mock.patch.object(artifact, "DOWNLOAD_SECONDS", 0.04),
            self.assertRaises(artifact.FrontendArtifactError),
        ):
            artifact._download(response.url, maximum=1000)
        self.assertLess(time.monotonic() - started, 2)
        self.assertGreater(response.progress, 0)
        self.assertTrue(channel.stopped.is_set())
        self.assertTrue(channel.closed)


if __name__ == "__main__":
    unittest.main()
