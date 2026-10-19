"""One sealed local OpenSearch volume; callers retain the catalog operation lock."""

from __future__ import annotations

import os
import stat
import uuid

from search.operations.catalog import administration, commands, execution, volume_policy
from search.operations.catalog.policy import LABEL_PREFIX, RuntimeError

ALIAS = volume_policy.ALIAS
TARGET = volume_policy.TARGET
_PROBE = (
    "import json,os,stat; s=os.stat('/data',follow_symlinks=False); "
    "assert stat.S_ISDIR(s.st_mode); "
    "print(json.dumps({'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode)}))"
)


def enabled(context):
    """Recognize the named-volume profile, including malformed declarations."""
    return volume_policy.enabled(context["compose"])


def _identity(context):
    return volume_policy.identity(
        execution.catalog_identity(context),
        compose=context["compose"],
        work_root=context["root"],
        data_root=context["data"],
        uid=os.geteuid(),
        gid=os.getegid(),
    )


def _exists(context, name):
    names = commands.command_output(
        context["runner"],
        ["docker", "volume", "ls", "--quiet", "--filter", "name=" + name],
    ).split()
    if len(names) != len(set(names)):
        raise RuntimeError("catalog_volume_inventory_invalid")
    return name in names


def _inspect(context, identity):
    value = execution.run_json(
        context["runner"],
        [
            "docker",
            "volume",
            "inspect",
            "--format",
            volume_policy.INSPECT_FORMAT,
            identity["name"],
        ],
    )
    return volume_policy.inspect_metadata(value, identity)


def _image(context, *, read_only):
    entry = administration._admin_image(context, read_only=read_only)
    inspected = execution.inspect_image(context, entry["image_id"])
    if inspected.get("Config", {}).get("Volumes") not in (None, {}):
        raise RuntimeError("catalog_volume_helper_declares_extra_volumes")
    return entry["image_id"]


def _run(context, identity, image, *, initialize=False):
    # Metadata is proved before every mount, including root's single new root.
    _inspect(context, identity)
    mount = "type=volume,source=" + identity["name"] + ",target=/data,volume-nocopy"
    if not initialize:
        mount += ",readonly"
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        identity["name"] + "-proof-" + uuid.uuid4().hex,
        "--label",
        LABEL_PREFIX + "volume-project=" + identity["project"],
        "--pull",
        "never",
        "--network",
        "none",
        "--runtime",
        "runc",
        "--env",
        "NVIDIA_VISIBLE_DEVICES=void",
        "--env",
        "CUDA_VISIBLE_DEVICES=",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--cpus",
        "0.25",
        "--memory",
        "64m",
        "--memory-swap",
        "64m",
        "--pids-limit",
        "16",
        "--mount",
        mount,
        "--workdir",
        "/data",
        "--user",
        "0:0" if initialize else f"{identity['uid']}:{identity['gid']}",
    ]
    program = _PROBE
    if initialize:
        argv += ["--cap-add", "CHOWN"]
        program = (
            "import os; assert not os.listdir('/data'); os.chmod('/data',0o700); "
            f"os.chown('/data',{identity['uid']},{identity['gid']})"
        )
    argv += ["--entrypoint", "python3", image, "-c", program]
    if initialize:
        commands.command_output(context["runner"], argv, timeout=30)
        return None
    proof = execution.run_json(context["runner"], argv, timeout=30)
    expected = {"uid": identity["uid"], "gid": identity["gid"], "mode": stat.S_IRWXU}
    if execution.digest_record(proof) != execution.digest_record(expected):
        raise RuntimeError("catalog_volume_ownership_changed")
    return proof


def require(context):
    """Verify sealed metadata and root ownership without reading index contents."""
    identity = _identity(context)
    execution.verify_security_material(context)
    root = context["root"]
    if (
        not (root / "volume.json").exists()
        or not (root / "volume-started.json").exists()
    ):
        raise RuntimeError("catalog_volume_requires_explicit_recovery")
    image = _image(context, read_only=True)
    started = volume_policy.started(identity, image)
    if execution.digest_record(
        execution.read_record(root, "volume-started.json")
    ) != execution.digest_record(started):
        raise RuntimeError("catalog_volume_receipt_changed")
    receipt = execution.read_record(root, "volume.json")
    metadata = _inspect(context, identity)
    expected = volume_policy.expected_receipt(identity, image, metadata)
    if execution.digest_record(receipt) != execution.digest_record(expected):
        raise RuntimeError("catalog_volume_receipt_changed")
    _run(context, identity, image)
    if execution.digest_record(_inspect(context, identity)) != execution.digest_record(
        metadata
    ):
        raise RuntimeError("catalog_volume_metadata_changed")
    return receipt


def ensure(context):
    """Create once after durable startup intent; partial state never auto-retries."""
    identity = _identity(context)
    root = context["root"]
    intent = {**execution.catalog_identity(context), "status": "started"}
    if execution.digest_record(
        execution.read_record(root, "start-started.json")
    ) != execution.digest_record(intent):
        raise RuntimeError("catalog_volume_start_intent_changed")
    execution.verify_security_material(context)
    if (root / "volume.json").exists():
        return require(context)
    if (root / "volume-started.json").exists():
        raise RuntimeError("catalog_volume_requires_explicit_recovery")
    if _exists(context, identity["name"]):
        raise RuntimeError("catalog_volume_existing_without_receipt")
    image = _image(context, read_only=False)
    execution.write_record(
        root,
        "volume-started.json",
        volume_policy.started(identity, image),
    )
    argv = ["docker", "volume", "create", "--driver", "local"]
    for key, value in sorted(volume_policy.labels(identity).items()):
        argv += ["--label", key + "=" + value]
    commands.command_output(context["runner"], [*argv, identity["name"]])
    metadata = _inspect(context, identity)
    _run(context, identity, image, initialize=True)
    proof = _run(context, identity, image)
    if execution.digest_record(_inspect(context, identity)) != execution.digest_record(
        metadata
    ):
        raise RuntimeError("catalog_volume_metadata_changed")
    result = volume_policy.expected_receipt(identity, image, metadata)
    if execution.digest_record(proof) != execution.digest_record(result["ownership"]):
        raise RuntimeError("catalog_volume_ownership_changed")
    execution.write_record(root, "volume.json", result)
    return result
