"""Share a fake catalog volume with cleanup owned by the running test."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from search.operations.catalog import execution, volumes


IMAGE = "sha256:" + "a" * 64


class CatalogVolumeFixture:
    """Create private volume evidence and register cleanup before setup IO."""

    def __init__(self, test):
        self.test = test
        temporary = tempfile.TemporaryDirectory(prefix="depositar-volume-test-")
        self.test.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.data.mkdir(mode=0o700)
        self.context = {
            "root": self.root,
            "data": self.data,
            "plan": {
                "project": "depositar-ai-catalog-test",
                "revision": "b" * 40,
                "manifest_sha256": "c" * 64,
                "compose_sha256": "d" * 64,
                "security_material_sha256": "e" * 64,
            },
            "bundle": SimpleNamespace(bundle_id="f" * 64),
            "compose": {
                "volumes": {
                    "catalog-index": {
                        "name": "depositar-ai-catalog-test-index",
                        "external": True,
                    }
                },
                "services": {
                    "opensearch-search": {
                        "volumes": [
                            {
                                "type": "volume",
                                "source": "catalog-index",
                                "target": volumes.TARGET,
                            }
                        ]
                    }
                },
            },
            "runner": self.runner,
        }
        self.calls = []
        self.metadata = None
        self.ownership = {"uid": 0, "gid": 0, "mode": 0o755}
        self.fail_initialize = False
        self.create_mutation = None
        self.security = self.patched(execution, "verify_security_material")
        self.image = self.patched(
            volumes.administration, "_admin_image", return_value={"image_id": IMAGE}
        )
        self.inspect_image = self.patched(
            execution,
            "inspect_image",
            return_value={"Id": IMAGE, "Config": {"Volumes": None}},
        )

    def patched(self, owner, name, **options):
        patcher = patch.object(owner, name, **options)
        result = patcher.start()
        self.test.addCleanup(patcher.stop)
        return result

    def runner(self, argv, *, timeout):
        self.calls.append(argv)
        raw = ""
        if argv[:3] == ["docker", "volume", "ls"]:
            raw = self.metadata["name"] if self.metadata is not None else ""
        elif argv[:3] == ["docker", "volume", "create"]:
            labels = dict(
                argv[index + 1].split("=", 1)
                for index, value in enumerate(argv)
                if value == "--label"
            )
            self.metadata = {
                "name": argv[-1],
                "driver": "local",
                "scope": "local",
                "options": None,
                "labels": labels,
                "created_at": "2026-10-13T00:00:00Z",
                "mountpoint": "/opaque/docker/volume",
            }
            if self.create_mutation:
                self.create_mutation(self.metadata)
            raw = argv[-1]
        elif argv[:3] == ["docker", "volume", "inspect"]:
            raw = json.dumps(self.metadata)
        elif argv[:2] == ["docker", "run"]:
            self.test.assertEqual(30, timeout)
            if "--cap-add" in argv:
                if self.fail_initialize:
                    return subprocess.CompletedProcess(
                        argv, 1, "", "bounded init failure"
                    )
                self.ownership = {
                    "uid": os.geteuid(),
                    "gid": os.getegid(),
                    "mode": 0o700,
                }
            else:
                raw = json.dumps(self.ownership)
        else:
            self.test.fail("unexpected command " + repr(argv))
        return subprocess.CompletedProcess(argv, 0, raw, "")

    def start_intent(self):
        execution.write_record(
            self.root,
            "start-started.json",
            {**execution.catalog_identity(self.context), "status": "started"},
        )
