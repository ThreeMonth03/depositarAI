"""Synthetic image archives and Docker calls shared by payload tests."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

from search.operations.shared import private_io


def record(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def oci_fixture(
    *, architecture="amd64", corrupt_blob=False, wrong_compatibility=False
):
    """Docker 29's observed index ID, config blob, and gzip-layer layout."""
    blobs = {}

    def descriptor(raw, media_type):
        digest = hashlib.sha256(raw).hexdigest()
        blobs["blobs/sha256/" + digest] = raw
        return {
            "digest": "sha256:" + digest,
            "size": len(raw),
            "mediaType": media_type,
        }

    uncompressed = b"synthetic fixed layer bytes"
    layer = descriptor(
        gzip.compress(uncompressed, mtime=0),
        "application/vnd.oci.image.layer.v1.tar+gzip",
    )
    config_value = {
        "architecture": architecture,
        "os": "linux",
        "config": {"Env": ["PATH=/usr/bin"], "Labels": {}},
        "rootfs": {
            "type": "layers",
            "diff_ids": ["sha256:" + hashlib.sha256(uncompressed).hexdigest()],
        },
    }
    config = descriptor(
        private_io.canonical(config_value),
        "application/vnd.oci.image.config.v1+json",
    )
    manifest = descriptor(
        private_io.canonical(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config,
                "layers": [layer],
            }
        ),
        "application/vnd.oci.image.manifest.v1+json",
    )
    manifest["platform"] = {"architecture": architecture, "os": "linux"}
    # Real save retains other-platform index references without their blobs.
    uncached = {
        "digest": "sha256:" + "f" * 64,
        "size": 200,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "platform": {"architecture": "arm64", "os": "linux"},
    }
    root = descriptor(
        private_io.canonical(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [manifest, uncached],
            }
        ),
        "application/vnd.oci.image.index.v1+json",
    )
    blobs["index.json"] = private_io.canonical(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [root],
        }
    )
    blobs["oci-layout"] = private_io.canonical({"imageLayoutVersion": "1.0.0"})
    blobs["manifest.json"] = private_io.canonical(
        [
            {
                "Config": "blobs/sha256/" + config["digest"][7:],
                "Layers": [
                    "blobs/sha256/"
                    + (
                        "e" * 64
                        if wrong_compatibility
                        else layer["digest"][7:]
                    )
                ],
                "RepoTags": ["fixture:test"],
            }
        ]
    )
    if corrupt_blob:
        blobs["blobs/sha256/" + layer["digest"][7:]] += b"corruption"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, raw in blobs.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    environment_digest = hashlib.sha256(
        private_io.canonical({"PATH": "/usr/bin"})
    ).hexdigest()
    return stream.getvalue(), {
        "fixture": {
            "image_id": root["digest"],
            "image_environment_sha256": environment_digest,
        }
    }


class FakeDocker:
    def __init__(self, plan, images, configs, layer):
        self.plan, self.images, self.configs, self.layer = (
            plan,
            images,
            configs,
            layer,
        )
        self.calls = []
        self.corrupt_layer = False
        self.omit_image = False
        self.fail_save = False
        self.after_save = None

    def __call__(self, argv, *, timeout, environment):
        self.calls.append(list(argv))
        assert environment == {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        assert not list(Path(argv[4]).iterdir())
        arguments = argv[5:]
        if arguments[:2] == ["image", "inspect"]:
            inspected = self.images[arguments[-1]]
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(inspected), ""
            )
        assert arguments[:2] == ["image", "save"]
        assert set(arguments[4:]) == set(self.images)
        assert not Path(arguments[3]).exists()
        if self.fail_save:
            return subprocess.CompletedProcess(
                argv, 1, b"", b"private-output-sentinel"
            )
        rows = []
        with tarfile.open(arguments[3], mode="w") as archive:

            def add(name, raw):
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))

            for index, (identifier, config) in enumerate(self.configs.items()):
                if self.omit_image and index == 0:
                    continue
                name = identifier[7:] + ".json"
                add(name, config)
                rows.append(
                    {
                        "Config": name,
                        "RepoTags": None,
                        "Layers": ["layer/layer.tar"],
                    }
                )
            add(
                "layer/layer.tar",
                self.layer + (b"corrupt" if self.corrupt_layer else b""),
            )
            add("manifest.json", json.dumps(rows).encode())
        if self.after_save:
            self.after_save()
        return subprocess.CompletedProcess(argv, 0, b"", b"")
