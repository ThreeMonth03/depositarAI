"""Pure identity and receipt rules for the one local OpenSearch data volume."""

from __future__ import annotations

import copy
import hashlib
import json
import stat

from search.operations.catalog.policy import IMAGE_ID, LABEL_PREFIX, RuntimeError

ALIAS = "catalog-index"
TARGET = "/usr/share/opensearch/data"
INSPECT_FORMAT = (
    '{"name":{{json .Name}},"driver":{{json .Driver}},'
    '"scope":{{json .Scope}},"options":{{json .Options}},'
    '"labels":{{json .Labels}},"created_at":{{json .CreatedAt}},'
    '"mountpoint":{{json .Mountpoint}}}'
)


def _canonical(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def enabled(compose):
    """Recognize the named-volume profile, including malformed declarations."""
    declarations = compose.get("volumes", {})
    mounts = compose["services"]["opensearch-search"].get("volumes", [])
    return ALIAS in declarations or any(
        type(mount) is dict
        and mount.get("type") == "volume"
        and (mount.get("source") == ALIAS or mount.get("target") == TARGET)
        for mount in mounts
    )


def identity(catalog_identity, *, compose, work_root, data_root, uid, gid):
    """Bind supplied catalog identity and operator roots without touching paths."""
    if type(uid) is not int or type(gid) is not int or uid <= 0 or gid <= 0:
        raise RuntimeError("catalog_volume_operator_must_be_nonroot")
    name = catalog_identity["project"] + "-index"
    declaration = compose.get("volumes", {}).get(ALIAS)
    if _canonical(declaration) != _canonical({"name": name, "external": True}):
        raise RuntimeError("catalog_volume_profile_changed")
    return {
        **catalog_identity,
        "name": name,
        "alias": ALIAS,
        "uid": uid,
        "gid": gid,
        "roots_sha256": hashlib.sha256(
            _canonical({"work_root": str(work_root), "data_root": str(data_root)})
        ).hexdigest(),
    }


def labels(identity):
    return {
        LABEL_PREFIX + "volume-" + key.replace("_", "-"): str(value)
        for key, value in identity.items()
    }


def inspect_metadata(value, identity):
    """Validate one Docker projection; treat Mountpoint solely as opaque text."""
    if (
        type(value) is not dict
        or set(value)
        != {"name", "driver", "scope", "options", "labels", "created_at", "mountpoint"}
        or value["name"] != identity["name"]
        or value["driver"] != "local"
        or value["scope"] != "local"
        or value["options"] not in (None, {})
        or value["labels"] != labels(identity)
        or any(
            type(value[key]) is not str or not value[key]
            for key in ("created_at", "mountpoint")
        )
    ):
        raise RuntimeError("catalog_volume_metadata_changed")
    return {**copy.deepcopy(value), "options": {}}


def started(identity, helper_image_id):
    if type(helper_image_id) is not str or IMAGE_ID.fullmatch(helper_image_id) is None:
        raise RuntimeError("catalog_volume_helper_image_invalid")
    return {**identity, "status": "started", "helper_image_id": helper_image_id}


def expected_receipt(identity, helper_image_id, metadata):
    """Reconstruct sealed evidence; ownership is the initial proved root stat.

    Runtime observation compares this receipt and one current metadata inspect.
    It does not claim to rerun an ownership helper or inspect index contents.
    """
    return {
        **started(identity, helper_image_id),
        "status": "ready",
        "metadata": inspect_metadata(metadata, identity),
        "ownership": {
            "uid": identity["uid"],
            "gid": identity["gid"],
            "mode": stat.S_IRWXU,
        },
    }
