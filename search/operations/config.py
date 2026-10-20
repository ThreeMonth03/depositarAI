"""Private, path-only configuration for the complete search product.

Loading never creates directories, rewrites existing dotenv files, or returns
credentials. Physical GPU UUIDs, not mutable indexes, identify the four roles.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat


ROOT = Path(__file__).resolve().parents[2]
MAX_CONFIG_BYTES = 16 * 1024
GPU_UUID = re.compile(
    r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "catalog_storage",
        "state_root",
        "api_model_root",
        "embedding_model_root",
        "reranker_model_root",
        "gpus",
        "cache_ttl_seconds",
    }
)


@dataclass(frozen=True)
class GPUConfig:
    scope: str
    planner: tuple[str, str]
    reranker: str

    def all_uuids(self) -> tuple[str, ...]:
        return (self.scope, *self.planner, self.reranker)


@dataclass(frozen=True)
class ProductConfig:
    schema_version: int
    state_root: Path
    api_model_root: Path
    embedding_model_root: Path
    reranker_model_root: Path
    gpus: GPUConfig
    cache_ttl_seconds: int = 0
    catalog_storage: str = "docker-volume"

    def to_dict(self) -> dict:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("product_config_schema")
        if (
            type(self.catalog_storage) is not str
            or self.catalog_storage != "docker-volume"
        ):
            raise ValueError("product_catalog_storage_invalid")
        value = {
            "schema_version": self.schema_version,
            "catalog_storage": self.catalog_storage,
            "state_root": str(self.state_root),
            "api_model_root": str(self.api_model_root),
            "embedding_model_root": str(self.embedding_model_root),
            "reranker_model_root": str(self.reranker_model_root),
            "gpus": {
                "scope": self.gpus.scope,
                "planner": list(self.gpus.planner),
                "reranker": self.gpus.reranker,
            },
            "cache_ttl_seconds": self.cache_ttl_seconds,
        }
        return value


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("product_config_duplicate_key")
        result[key] = value
    return result


def _no_symlinks(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError("product_path_symlink")


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _root_path(raw, *, repo_root: Path, home: Path) -> Path:
    if type(raw) is not str or not raw or any(c in raw for c in "\x00\r\n,"):
        raise ValueError("product_path_invalid")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("product_path_not_absolute")
    if path in (Path("/"), home) or _overlaps(path, repo_root):
        raise ValueError("product_path_not_dedicated")
    _no_symlinks(path)
    return path


def _directory(path: Path, uid: int, *, private: bool) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid not in ({uid} if private else {0, uid})
        or info.st_mode & 0o022
        or (private and stat.S_IMODE(info.st_mode) != 0o700)
    ):
        raise ValueError("product_directory_unsafe")


def _gpu(raw) -> str:
    if type(raw) is not str or GPU_UUID.fullmatch(raw) is None:
        raise ValueError("product_gpu_uuid_invalid")
    return "GPU-" + raw[4:].lower()


def validate_config(
    value,
    *,
    repo_root: Path = ROOT,
    home: Path | None = None,
    uid: int | None = None,
) -> ProductConfig:
    """Validate a parsed object and existing roots without changing them."""
    home = Path.home() if home is None else Path(home)
    uid = os.geteuid() if uid is None else uid
    repo_root = Path(repo_root).resolve()
    if type(value) is not dict:
        raise ValueError("product_config_fields")
    schema = value.get("schema_version")
    if set(value) != CONFIG_KEYS:
        raise ValueError("product_config_fields")
    if type(schema) is not int or schema != 2:
        raise ValueError("product_config_schema")
    catalog_storage = value["catalog_storage"]
    if type(catalog_storage) is not str or catalog_storage != "docker-volume":
        raise ValueError("product_catalog_storage_invalid")
    ttl = value["cache_ttl_seconds"]
    if type(ttl) is not int or not 0 <= ttl <= 3600:
        raise ValueError("product_cache_ttl_invalid")
    raw_gpus = value["gpus"]
    if type(raw_gpus) is not dict or set(raw_gpus) != {"scope", "planner", "reranker"}:
        raise ValueError("product_gpu_fields")
    planner = raw_gpus["planner"]
    if type(planner) is not list or len(planner) != 2:
        raise ValueError("product_planner_requires_two_gpus")
    gpus = GPUConfig(
        _gpu(raw_gpus["scope"]),
        (_gpu(planner[0]), _gpu(planner[1])),
        _gpu(raw_gpus["reranker"]),
    )
    if len(set(gpus.all_uuids())) != 4:
        raise ValueError("product_gpus_overlap")
    roots = {
        name: _root_path(value[name], repo_root=repo_root, home=home)
        for name in (
            "state_root",
            "api_model_root",
            "embedding_model_root",
            "reranker_model_root",
        )
    }
    for index, path in enumerate(roots.values()):
        if any(_overlaps(path, other) for other in list(roots.values())[:index]):
            raise ValueError("product_roots_overlap")
    state = roots["state_root"]
    if state.exists():
        _directory(state, uid, private=True)
    else:
        # Only the final component may be absent. No implicit broad mkdir -p.
        _directory(state.parent, uid, private=True)
    for name in ("api_model_root", "embedding_model_root", "reranker_model_root"):
        _directory(roots[name], uid, private=False)
    return ProductConfig(
        schema,
        **roots,
        gpus=gpus,
        cache_ttl_seconds=ttl,
        catalog_storage=catalog_storage,
    )


def load_config(
    path: Path,
    *,
    repo_root: Path = ROOT,
    home: Path | None = None,
    uid: int | None = None,
) -> ProductConfig:
    """Load one operator-owned 0600 regular file, rejecting links and races."""
    uid = os.geteuid() if uid is None else uid
    path = Path(path).absolute()
    _no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != uid
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= MAX_CONFIG_BYTES
        ):
            raise ValueError("product_config_file_unsafe")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
        after = os.fstat(fd)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(raw) != before.st_size or any(
            getattr(before, field) != getattr(after, field) for field in fields
        ):
            raise ValueError("product_config_changed")
    finally:
        os.close(fd)
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("product_config_json_invalid") from error
    return validate_config(value, repo_root=repo_root, home=home, uid=uid)


def validate_gpu_inventory(config: ProductConfig, inventory: str) -> None:
    """Require all four UUIDs in strict nvidia-smi ``UUID,index`` records."""
    available = set()
    indexes = set()
    for line in inventory.splitlines():
        if not line.strip():
            continue
        fields = [part.strip() for part in line.split(",")]
        if len(fields) != 2 or not re.fullmatch(r"0|[1-9][0-9]*", fields[1]):
            raise ValueError("product_gpu_inventory_invalid")
        identifier = _gpu(fields[0])
        if identifier in available or fields[1] in indexes:
            raise ValueError("product_gpu_inventory_duplicate")
        available.add(identifier)
        indexes.add(fields[1])
    if not set(config.gpus.all_uuids()) <= available:
        raise ValueError("product_gpu_missing")
