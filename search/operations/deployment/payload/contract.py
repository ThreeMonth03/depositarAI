"""Closed product-payload admission rules shared by planning and readers."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from search.operations.deployment import source_bundle


MAX_RECORD_BYTES = 1024**2
MAX_ASSET_BYTES = 1024**3
MAX_ARCHIVE_BYTES = 64 * 1024**3
MAX_MEMBERS = 100000
SECRET_KEY = re.compile(
    "(?:^|[_.-])(?:passwords?|secrets?|credentials?|tokens?|api[_-]?key|access"
    "[_-]?key|private[_-]?key)(?:$|[_.-])",
    re.I,
)


class ProductPackageError(ValueError):
    """Fixed diagnostic codes; never include source, paths, or Docker
    output.
    """


def fail(code):
    raise ProductPackageError("product_package_" + code)


def keys(value, keys, code):
    if type(value) is not dict or set(value) != set(keys):
        fail(code)


def hash_value(value, pattern=source_bundle.DIGEST):
    if type(value) is not str or pattern.fullmatch(value) is None:
        fail("identity_invalid")
    return value


def relative(value, *, reviewed_model_file=False):
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or any(character in value for character in "\\\x00\r\n")
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(
            part.startswith(".")
            or (SECRET_KEY.search(part) and not reviewed_model_file)
            for part in value.split("/")
        )
    ):
        fail("asset_name_invalid")
    return value


def image_environment(config):
    if type(config) is not dict:
        fail("image_config_invalid")
    environment = source_bundle.parse_image_environment(config.get("Env"))
    labels = config.get("Labels") or {}
    if type(labels) is not dict:
        fail("image_labels_invalid")
    for key, value in (*environment.items(), *labels.items()):
        if type(key) is not str or type(value) is not str:
            fail("image_config_invalid")
        if value and (
            SECRET_KEY.search(key)
            or "PRIVATE KEY-----" in value
            or re.search(r"https?://[^/\s]+:[^/\s]+@", value)
        ):
            fail("image_secret_configuration")
    return environment, labels
