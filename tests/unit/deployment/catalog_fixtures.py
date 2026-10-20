"""Private catalog storage evidence shared by offline deployment tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

from search.operations.catalog import execution, volume_policy


def sealed_catalog_storage(test, context):
    """Provide real private receipts and a read-only fake volume observer."""
    temporary = tempfile.TemporaryDirectory(prefix="depositar-release-volume-")
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    data = root / "data"
    data.mkdir(mode=0o700)
    context.update(root=root, data=data)
    plan = context["plan"]
    plan.setdefault("compose_sha256", execution.digest_record(context["compose"]))
    plan.setdefault("security_material_sha256", "e" * 64)
    identity = volume_policy.identity(
        execution.catalog_identity(context),
        compose=context["compose"],
        work_root=root,
        data_root=data,
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    metadata = {
        "name": identity["name"],
        "driver": "local",
        "scope": "local",
        "options": None,
        "labels": volume_policy.labels(identity),
        "created_at": "2026-10-13T00:00:00Z",
        "mountpoint": "/opaque/docker/volume",
    }
    image = "sha256:" + "1" * 64
    for name, record in {
        "admin-image.json": {"image_id": image},
        "volume-started.json": volume_policy.started(identity, image),
        "volume.json": volume_policy.expected_receipt(identity, image, metadata),
    }.items():
        execution.write_record(root, name, record)
    storage = SimpleNamespace(metadata=metadata, calls=[])

    def runner(argv, *, timeout):
        test.assertEqual(
            [
                "docker",
                "volume",
                "inspect",
                "--format",
                volume_policy.INSPECT_FORMAT,
                identity["name"],
            ],
            argv,
        )
        test.assertEqual(30, timeout)
        storage.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(storage.metadata), "")

    context["runner"] = runner
    return storage
