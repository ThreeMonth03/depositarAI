"""Validate sealed metadata bundles and their file/content identities."""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .codec import _canonical_json, _exact_int, _exact_json_equal, _strict_json
from .errors import BundleError, ProtocolError
from . import resources as resource_contract
from .policy import (
    BUNDLE_BUILDER_NAME,
    CATALOG_LABEL,
    CATALOG_SEARCH_ROLE,
    CATALOG_VIRTUAL_HOST,
    MAX_DOCUMENTS,
    SHA256_RE,
    _BIDI_CONTROLS,
)

MANIFEST_NAME = "bundle-manifest.json"


DOCUMENTS_NAME = "fess-documents.jsonl.gz"


RESOURCE_CONTEXT_NAME = "resource-context.v1.jsonl.gz"


ARTIFACT_NAMES = (
    "source.jsonl.gz",
    "corpus.v2.jsonl.gz",
    DOCUMENTS_NAME,
    RESOURCE_CONTEXT_NAME,
)


MAX_MANIFEST_BYTES = 64 * 1024


MAX_MANIFEST_LINES = 128


MAX_MANIFEST_LINE_BYTES = 16 * 1024


MAX_ARTIFACT_FILE_BYTES = 64 * 1024 * 1024


MAX_ARTIFACT_EXPANDED_BYTES = 128 * 1024 * 1024


MAX_DOCUMENT_EXPANDED_BYTES = 128 * 1024 * 1024


MAX_DOCUMENT_LINE_BYTES = 2 * 1024 * 1024


MAX_BULK_DOCUMENTS = 500


SAFE_FILE_MODES = frozenset((0o400, 0o600))


DATASET_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


_MANIFEST_KEYS = frozenset(
    (
        "schema_version",
        "bundle_format",
        "bundle_id",
        "artifacts",
        "dataset_count",
        "resource_count",
        "dataset_ids_sha256",
        "builder",
        "policy",
        "normalization_counters",
    )
)


_ARTIFACT_KEYS = frozenset(
    ("sha256", "bytes", "expanded_bytes", "records", "schema_version", "media_type")
)


_DOCUMENT_FIELDS = frozenset(
    (
        "url",
        "title",
        "content",
        "important_content",
        "mimetype",
        "filetype",
        "lang",
        "dataset_id",
        "name",
        "notes",
        "organization_id",
        "organization_name",
        "organization_title",
        "tags",
        "groups",
        "resource_formats",
        "license_id",
        "license_title",
        "metadata_created",
        "metadata_modified",
        "resource_count",
        "resource_context",
        "label",
        "role",
        "boost",
        "virtual_host",
    )
)


_CATALOG_STRING_LIMITS = {
    "slug": 256,
    "title": 512,
    "notes": 32_768,
    "remarks": 32_768,
    "process_step": 32_768,
    "person": 512,
    "organization": 512,
    "license": 512,
    "date": 128,
    "resource_id": 128,
    "resource_name": 512,
    "resource_description": 16_384,
    "resource_format": 128,
    "resource_mimetype": 256,
    "resource_url": 2_048,
    "list_item": 512,
    "fess_important_content": 16_384,
    "fess_content": 50_000,
    "resource_context_name": resource_contract.MAX_RESOURCE_NAME_CHARS,
    "resource_context_column": resource_contract.MAX_COLUMN_CHARS,
    "resource_context_index_text": resource_contract.MAX_INDEX_TEXT_CHARS,
}


_CATALOG_ARRAY_LIMITS = {
    "keywords": 128,
    "tags": 128,
    "language": 32,
    "data_type": 64,
    "groups": 64,
    "resources": 256,
}


CATALOG_MANIFEST_POLICY = {
    "input": {
        "compressed_bytes_max": 64 * 1024 * 1024,
        "expanded_bytes_max": 128 * 1024 * 1024,
        "line_bytes_max": 1024 * 1024,
        "rows_min": 1,
        "rows_max": 10_000,
        "json_nesting_max": 32,
        "resources_per_dataset_max": 256,
        "regular_file": True,
        "no_symlink": True,
        "link_count": 1,
        "owner": "effective_uid",
        "strict_utf8": True,
        "strict_json": True,
    },
    "selection": {"private": False, "state": "active", "type": "dataset"},
    "output": {
        "artifact_compressed_bytes_max": 64 * 1024 * 1024,
        "artifact_expanded_bytes_max": 128 * 1024 * 1024,
        "artifact_line_bytes_max": 2 * 1024 * 1024,
        "artifact_records_min": 1,
        "artifact_records_max": 10_000,
        "gzip_mtime": 0,
        "file_mode": "0400",
        "directory_mode": "0500",
    },
    "normalization": {
        "unicode": "NFKC",
        "html": "text-only; script/style/noscript removed",
        "controls": "Unicode Cc/Cf removed",
        "string_character_limits": _CATALOG_STRING_LIMITS,
        "array_item_limits": _CATALOG_ARRAY_LIMITS,
    },
    "fess": {
        "field_allowlist": sorted(_DOCUMENT_FIELDS),
        "fixed_label": CATALOG_LABEL,
        "role": [CATALOG_SEARCH_ROLE],
        "boost": 1.0,
        "virtual_host": [CATALOG_VIRTUAL_HOST],
        "resource_download_url_field_included": False,
        "resource_landing_url_field_included": True,
        "email_addresses": False,
        "raw_spatial": False,
    },
    "resource_context": {
        "resources_per_dataset_max": resource_contract.MAX_RESOURCES,
        "columns_per_resource_max": resource_contract.MAX_COLUMNS,
        "column_characters_max": resource_contract.MAX_COLUMN_CHARS,
        "column_total_characters_max": resource_contract.MAX_COLUMN_TOTAL_CHARS,
        "excerpt_characters_max": resource_contract.MAX_EXCERPT_CHARS,
        "index_text_characters_max": resource_contract.MAX_INDEX_TEXT_CHARS,
    },
    "dataset_id_digest_encoding": "lowercase UUIDs sorted, one UTF-8 ID plus LF per record",
}


_NORMALIZATION_COUNTERS = frozenset(
    {
        "html_fields_cleaned",
        "control_characters_removed",
        "bidi_characters_removed",
        "truncated_fields",
        "normalized_fields",
        "duplicate_list_items_removed",
        "fess_email_addresses_redacted",
        "fess_fields_truncated_after_redaction",
        "fess_important_content_truncated",
        "fess_content_truncated",
        "fess_duplicate_list_items_removed_after_redaction",
        "bbox_absent",
        "bbox_partial",
        "bbox_invalid",
        "bbox_complete",
        "temporal_absent",
        "temporal_partial",
        "temporal_complete",
    }
)


_CORPUS_KEYS = frozenset(
    {
        "schema_version",
        "dataset_id",
        "name",
        "url",
        "title",
        "notes",
        "remarks",
        "process_step",
        "author",
        "maintainer",
        "organization",
        "tags",
        "groups",
        "keywords",
        "language",
        "data_type",
        "license_id",
        "license_title",
        "temporal",
        "bbox",
        "resources",
        "resource_count",
        "resource_formats",
        "metadata_created",
        "metadata_modified",
        "visibility",
        "state",
        "dataset_type",
    }
)


def _stat_identity(value: os.stat_result) -> Tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_sealed(
    path: Path, *, expected_uid: int, max_bytes: int
) -> Tuple[int, os.stat_result]:
    try:
        by_name = path.lstat()
    except OSError:
        raise BundleError("cannot inspect a sealed bundle file") from None
    if stat.S_ISLNK(by_name.st_mode):
        raise BundleError("sealed bundle files must not be symlinks")
    try:
        descriptor = os.open(
            str(path),
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        raise BundleError("cannot open a sealed bundle file") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) not in SAFE_FILE_MODES
            or metadata.st_size < 1
            or metadata.st_size > max_bytes
            or _stat_identity(metadata) != _stat_identity(by_name)
        ):
            raise BundleError("sealed bundle file metadata is unsafe")
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _assert_unchanged(path: Path, descriptor: int, initial: os.stat_result) -> None:
    try:
        by_descriptor = os.fstat(descriptor)
        by_name = path.lstat()
    except OSError:
        raise BundleError("sealed bundle file changed while reading") from None
    if _stat_identity(initial) != _stat_identity(by_descriptor) or _stat_identity(
        initial
    ) != _stat_identity(by_name):
        raise BundleError("sealed bundle file changed while reading")


def _secure_read(path: Path, *, expected_uid: int, max_bytes: int) -> bytes:
    descriptor, metadata = _open_sealed(
        path, expected_uid=expected_uid, max_bytes=max_bytes
    )
    try:
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise BundleError("sealed bundle file exceeded its byte cap")
        if total != metadata.st_size:
            raise BundleError("sealed bundle file size changed while reading")
        _assert_unchanged(path, descriptor, metadata)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _manifest_line(raw: bytes) -> bytes:
    if (
        not raw.endswith(b"\n")
        or raw.count(b"\n") != 1
        or b"\r" in raw
        or len(raw) - 1 > MAX_MANIFEST_LINE_BYTES
    ):
        raise BundleError("sealed manifest must be one newline-terminated JSON record")
    return raw[:-1]


def _secure_gzip_lines(
    path: Path,
    *,
    expected_uid: int,
    expected_sha256: str,
    expected_bytes: int,
    expected_expanded_bytes: int,
    expected_records: int,
    max_expanded_bytes: int,
    collect: bool,
) -> List[bytes]:
    descriptor, metadata = _open_sealed(
        path, expected_uid=expected_uid, max_bytes=MAX_ARTIFACT_FILE_BYTES
    )
    try:
        if metadata.st_size != expected_bytes:
            raise BundleError(
                "sealed artifact compressed size differs from its manifest"
            )
        digest = hashlib.sha256()
        total_compressed = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total_compressed += len(chunk)
        if total_compressed != expected_bytes or digest.hexdigest() != expected_sha256:
            raise BundleError("sealed artifact digest differs from its manifest")
        os.lseek(descriptor, 0, os.SEEK_SET)
        duplicate = os.dup(descriptor)
        lines: List[bytes] = []
        expanded = 0
        records = 0
        try:
            with os.fdopen(duplicate, "rb", closefd=True) as raw:
                duplicate = -1
                with gzip.GzipFile(fileobj=raw, mode="rb") as stream:
                    while True:
                        line = stream.readline(MAX_DOCUMENT_LINE_BYTES + 1)
                        if not line:
                            break
                        expanded += len(line)
                        records += 1
                        if (
                            len(line) > MAX_DOCUMENT_LINE_BYTES
                            or expanded > max_expanded_bytes
                            or records > MAX_DOCUMENTS
                            or not line.endswith(b"\n")
                            or line == b"\n"
                            or b"\r" in line
                        ):
                            raise BundleError(
                                "sealed gzip JSON Lines exceeds its bounds"
                            )
                        if collect:
                            lines.append(line[:-1])
        except (gzip.BadGzipFile, EOFError, OSError):
            raise BundleError(
                "sealed artifact is not a valid bounded gzip stream"
            ) from None
        finally:
            if duplicate >= 0:
                os.close(duplicate)
        if expanded != expected_expanded_bytes or records != expected_records:
            raise BundleError(
                "sealed artifact expanded size or record count differs from its manifest"
            )
        _assert_unchanged(path, descriptor, metadata)
        return lines
    finally:
        os.close(descriptor)


def _validate_dataset_url(value: Any, slug: Any) -> str:
    if not isinstance(value, str) or not isinstance(slug, str):
        raise BundleError("catalog document URL or slug is malformed")
    slug = _safe_text(slug, "name", 256)
    if not slug:
        raise BundleError("catalog dataset slug is empty")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise BundleError("catalog document URL is malformed") from None
    canonical_path = "/dataset/" + urllib.parse.quote(slug, safe="")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "data.depositar.io"
        or parsed.hostname != "data.depositar.io"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != canonical_path
        or value != "https://data.depositar.io" + canonical_path
    ):
        raise BundleError(
            "catalog document URL is outside the canonical Depositar dataset origin"
        )
    return value


def _safe_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise BundleError("catalog document text field is invalid")
    if unicodedata.normalize("NFKC", value) != value:
        raise BundleError("catalog document text is not NFKC-normalized")
    if any(character in _BIDI_CONTROLS for character in value):
        raise BundleError("catalog document contains a bidi control")
    if any(unicodedata.category(character) in ("Cc", "Cf") for character in value):
        raise BundleError("catalog document contains a control character")
    return value


def _safe_string_list(value: Any, *, maximum_items: int = 256) -> List[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise BundleError("catalog document array field is invalid")
    result = [_safe_text(item, "array", 1024) for item in value]
    if len(result) != len(set(result)):
        raise BundleError("catalog document array contains duplicates")
    return result


def _validated_document(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _DOCUMENT_FIELDS:
        raise BundleError("catalog document does not match its exact field allowlist")
    dataset_id = value.get("dataset_id")
    if not isinstance(dataset_id, str) or DATASET_ID_RE.fullmatch(dataset_id) is None:
        raise BundleError("catalog dataset identifier is malformed")
    result = dict(value)
    result["name"] = _safe_text(result["name"], "name", 256)
    result["url"] = _validate_dataset_url(result["url"], result["name"])
    for field, maximum in (
        ("title", 512),
        ("notes", 32_768),
        ("important_content", 16_384),
        ("content", 50_000),
        ("mimetype", 256),
        ("filetype", 128),
        ("organization_id", 512),
        ("organization_name", 512),
        ("organization_title", 512),
        ("license_id", 512),
        ("license_title", 512),
        ("metadata_created", 128),
        ("metadata_modified", 128),
    ):
        result[field] = _safe_text(result[field], field, maximum)
    for field, maximum_items in (
        ("tags", 128),
        ("groups", 64),
        ("resource_formats", 256),
    ):
        result[field] = _safe_string_list(result[field], maximum_items=maximum_items)
    if not _exact_int(result.get("resource_count"), maximum=100_000):
        raise BundleError("catalog resource count is invalid")
    try:
        result["resource_context"] = resource_contract.validate_resource_context(
            result["resource_context"]
        )
    except ValueError as exc:
        raise BundleError("catalog resource context is invalid: %s" % exc) from exc
    if (
        result.get("label") != [CATALOG_LABEL]
        or result.get("lang") != ["auto"]
        or result.get("role") != [CATALOG_SEARCH_ROLE]
        or result.get("boost") != 1.0
        or type(result.get("boost")) is not float
        or result.get("virtual_host") != [CATALOG_VIRTUAL_HOST]
        or result.get("mimetype") != "text/plain"
        or result.get("filetype") != "txt"
    ):
        raise BundleError("catalog document fixed Fess fields are invalid")
    return result


@dataclass(frozen=True)
class CatalogBundle:
    bundle_id: str
    manifest_sha256: str
    documents_sha256: str
    documents: Tuple[Mapping[str, Any], ...]
    by_url: Mapping[str, Mapping[str, Any]]
    condition_metadata: Mapping[str, Mapping[str, Any]]

    @property
    def expected_inventory_entries(self) -> Tuple[Tuple[str, str, str], ...]:
        """Return the exact immutable identity expected in the active index."""

        return tuple(
            (
                url,
                self.bundle_id,
                document["depositar_document_sha256"],
            )
            for url, document in sorted(self.by_url.items())
        )

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        expected_manifest_sha256: str,
        operator_uid: Optional[int] = None,
    ) -> "CatalogBundle":
        if SHA256_RE.fullmatch(expected_manifest_sha256 or "") is None:
            raise BundleError("expected manifest digest is malformed")
        uid = os.geteuid() if operator_uid is None else operator_uid
        if not _exact_int(uid, minimum=0):
            raise BundleError("operator UID is invalid")
        directory = Path(directory)
        try:
            root = directory.lstat()
            names = set(os.listdir(str(directory)))
        except OSError:
            raise BundleError("cannot inspect the catalog bundle directory") from None
        if (
            not stat.S_ISDIR(root.st_mode)
            or stat.S_ISLNK(root.st_mode)
            or root.st_uid != uid
            or stat.S_IMODE(root.st_mode) != 0o500
            or names != set(ARTIFACT_NAMES + (MANIFEST_NAME,))
        ):
            raise BundleError("catalog bundle directory is not sealed")
        manifest_raw = _secure_read(
            directory / MANIFEST_NAME,
            expected_uid=uid,
            max_bytes=MAX_MANIFEST_BYTES,
        )
        if hashlib.sha256(manifest_raw).hexdigest() != expected_manifest_sha256:
            raise BundleError("catalog bundle manifest digest mismatch")
        try:
            manifest = _strict_json(_manifest_line(manifest_raw))
        except ProtocolError:
            raise BundleError("catalog bundle manifest JSON is malformed") from None
        if not isinstance(manifest, Mapping) or set(manifest) != _MANIFEST_KEYS:
            raise BundleError("catalog bundle manifest shape is invalid")
        bundle_id = manifest.get("bundle_id")
        artifacts = manifest.get("artifacts")
        builder = manifest.get("builder")
        policy = manifest.get("policy")
        counters = manifest.get("normalization_counters")
        if (
            not _exact_int(manifest.get("schema_version"), minimum=1, maximum=1)
            or manifest.get("bundle_format") != "depositar-public-metadata-v3"
            or not isinstance(bundle_id, str)
            or SHA256_RE.fullmatch(bundle_id) is None
            or directory.name != "sha256-" + bundle_id
            or not isinstance(artifacts, Mapping)
            or set(artifacts) != set(ARTIFACT_NAMES)
            or not _exact_int(
                manifest.get("dataset_count"), minimum=1, maximum=MAX_DOCUMENTS
            )
            or not _exact_int(
                manifest.get("resource_count"), maximum=MAX_DOCUMENTS * 256
            )
            or not isinstance(manifest.get("dataset_ids_sha256"), str)
            or SHA256_RE.fullmatch(manifest["dataset_ids_sha256"]) is None
            or not isinstance(builder, Mapping)
            or set(builder) != {"name", "source_sha256", "python_minimum"}
            or builder.get("name") != BUNDLE_BUILDER_NAME
            or not isinstance(builder.get("source_sha256"), str)
            or SHA256_RE.fullmatch(builder["source_sha256"]) is None
            or builder.get("python_minimum") != "3.9"
            or not _exact_json_equal(policy, CATALOG_MANIFEST_POLICY)
            or not isinstance(counters, Mapping)
            or not set(counters).issubset(_NORMALIZATION_COUNTERS)
            or any(
                not isinstance(key, str) or not _exact_int(value, minimum=0)
                for key, value in counters.items()
            )
            or sum(
                counters.get("bbox_" + status, 0)
                for status in ("absent", "partial", "invalid", "complete")
            )
            != manifest.get("dataset_count")
            or sum(
                counters.get("temporal_" + status, 0)
                for status in ("absent", "partial", "complete")
            )
            != manifest.get("dataset_count")
        ):
            raise BundleError("catalog bundle manifest values are invalid")
        core = dict(manifest)
        del core["bundle_id"]
        try:
            calculated_bundle_id = hashlib.sha256(_canonical_json(core)).hexdigest()
        except ProtocolError:
            raise BundleError("catalog bundle identity cannot be reproduced") from None
        if calculated_bundle_id != bundle_id:
            raise BundleError("catalog bundle content identity is invalid")

        artifact_lines: Dict[str, List[bytes]] = {}
        expected_versions = {
            "source.jsonl.gz": 1,
            "corpus.v2.jsonl.gz": 2,
            DOCUMENTS_NAME: 1,
            RESOURCE_CONTEXT_NAME: 1,
        }
        for name in ARTIFACT_NAMES:
            descriptor = artifacts.get(name)
            if not isinstance(descriptor, Mapping) or set(descriptor) != _ARTIFACT_KEYS:
                raise BundleError("catalog artifact descriptor shape is invalid")
            if (
                not isinstance(descriptor.get("sha256"), str)
                or SHA256_RE.fullmatch(descriptor["sha256"]) is None
                or not _exact_int(
                    descriptor.get("bytes"), minimum=1, maximum=MAX_ARTIFACT_FILE_BYTES
                )
                or not _exact_int(
                    descriptor.get("expanded_bytes"),
                    minimum=2,
                    maximum=(
                        MAX_DOCUMENT_EXPANDED_BYTES
                        if name == DOCUMENTS_NAME
                        else MAX_ARTIFACT_EXPANDED_BYTES
                    ),
                )
                or not _exact_int(
                    descriptor.get("records"), minimum=1, maximum=MAX_DOCUMENTS
                )
                or descriptor["records"] != manifest["dataset_count"]
                or not _exact_int(
                    descriptor.get("schema_version"),
                    minimum=expected_versions[name],
                    maximum=expected_versions[name],
                )
                or descriptor.get("media_type") != "application/x-ndjson+gzip"
            ):
                raise BundleError("catalog artifact descriptor values are invalid")
            lines = _secure_gzip_lines(
                directory / name,
                expected_uid=uid,
                expected_sha256=descriptor["sha256"],
                expected_bytes=descriptor["bytes"],
                expected_expanded_bytes=descriptor["expanded_bytes"],
                expected_records=descriptor["records"],
                max_expanded_bytes=(
                    MAX_DOCUMENT_EXPANDED_BYTES
                    if name == DOCUMENTS_NAME
                    else MAX_ARTIFACT_EXPANDED_BYTES
                ),
                collect=True,
            )
            artifact_lines[name] = lines

        documents: List[Mapping[str, Any]] = []
        by_url: Dict[str, Mapping[str, Any]] = {}
        condition_metadata: Dict[str, Mapping[str, Any]] = {}
        dataset_ids = set()
        dataset_id_digest = hashlib.sha256()
        summed_resource_count = 0
        for source_line, corpus_line, document_line, context_line in zip(
            artifact_lines["source.jsonl.gz"],
            artifact_lines["corpus.v2.jsonl.gz"],
            artifact_lines[DOCUMENTS_NAME],
            artifact_lines[RESOURCE_CONTEXT_NAME],
        ):
            try:
                source = _strict_json(source_line)
                corpus = _strict_json(corpus_line)
                document = _validated_document(_strict_json(document_line))
                context_row = _strict_json(context_line)
            except ProtocolError:
                raise BundleError("catalog artifact JSON is malformed") from None
            source_id = source.get("id") if isinstance(source, Mapping) else None
            if (
                not isinstance(source, Mapping)
                or not isinstance(source_id, str)
                or DATASET_ID_RE.fullmatch(source_id.casefold()) is None
                or source.get("private") is not False
                or source.get("state") != "active"
                or source.get("type") != "dataset"
                or not isinstance(corpus, Mapping)
                or set(corpus) != _CORPUS_KEYS
                or not _exact_int(corpus.get("schema_version"), minimum=2, maximum=2)
                or corpus.get("visibility") != "public"
                or corpus.get("state") != "active"
                or corpus.get("dataset_type") != "dataset"
                or not isinstance(corpus.get("resources"), list)
                or len(corpus["resources"]) > 256
                or not _exact_int(corpus.get("resource_count"), maximum=256)
                or corpus["resource_count"] != len(corpus["resources"])
            ):
                raise BundleError("catalog source or corpus semantics are invalid")
            source_resources = source.get("resources")
            if source_resources is None:
                source_resources = []
            if (
                not isinstance(source_resources, list)
                or len(source_resources) > 256
                or not all(
                    isinstance(resource, Mapping) for resource in source_resources
                )
                or len(source_resources) != corpus["resource_count"]
            ):
                raise BundleError("catalog source and corpus resource counts disagree")
            url = document["url"]
            dataset_id = document["dataset_id"]
            if (
                source_id.casefold() != dataset_id
                or corpus.get("dataset_id") != dataset_id
                or corpus.get("name") != document["name"]
                or corpus.get("url") != url
                or corpus.get("resource_count") != document["resource_count"]
            ):
                raise BundleError("catalog artifacts disagree on dataset identity")
            if (
                not isinstance(context_row, Mapping)
                or set(context_row) != {"schema_version", "dataset_id", "resources"}
                or not _exact_int(
                    context_row.get("schema_version"), minimum=1, maximum=1
                )
                or context_row.get("dataset_id") != dataset_id
            ):
                raise BundleError("catalog resource context row is invalid")
            try:
                enrichment = resource_contract.validate_enrichment_resources(
                    context_row["resources"]
                )
            except ValueError as exc:
                raise BundleError(
                    "catalog resource enrichment is invalid: %s" % exc
                ) from exc
            contexts = [entry["context"] for entry in enrichment]
            if not _exact_json_equal(contexts, document["resource_context"]):
                raise BundleError("catalog document and resource context disagree")
            resources_by_id = {
                resource.get("id"): resource
                for resource in corpus["resources"]
                if isinstance(resource, Mapping) and isinstance(resource.get("id"), str)
            }
            source_resource_ids = {
                resource["id"]
                for resource in source_resources
                if isinstance(resource.get("id"), str)
            }
            for context in contexts:
                resource = resources_by_id.get(context["resource_id"])
                if (
                    resource is None
                    or context["resource_id"] not in source_resource_ids
                    or not isinstance(resource.get("format"), str)
                    or resource["format"].upper() != context["format"]
                    or context["url"]
                    != resource_contract.resource_url(url, context["resource_id"])
                ):
                    raise BundleError(
                        "catalog enrichment does not match its dataset resource"
                    )
            if any(
                entry["index_text"] not in document["content"] for entry in enrichment
            ):
                raise BundleError("catalog indexed content omits resource enrichment")
            if url in by_url or dataset_id in dataset_ids:
                raise BundleError("catalog bundle contains duplicate datasets")
            document_sha256 = hashlib.sha256(_canonical_json(document)).hexdigest()
            outbound = dict(document)
            outbound["depositar_bundle_id"] = bundle_id
            outbound["depositar_document_sha256"] = document_sha256
            outbound["resource_context"] = tuple(
                MappingProxyType({**context, "columns": tuple(context["columns"])})
                for context in contexts
            )
            frozen = MappingProxyType(
                {
                    key: tuple(value) if isinstance(value, list) else value
                    for key, value in outbound.items()
                }
            )
            documents.append(frozen)
            by_url[url] = frozen
            if not isinstance(corpus.get("temporal"), dict) or not isinstance(
                corpus.get("bbox"), dict
            ):
                raise BundleError("condition metadata shape is invalid")
            condition_metadata[dataset_id] = MappingProxyType(
                {
                    "temporal": MappingProxyType(dict(corpus["temporal"])),
                    "bbox": MappingProxyType(dict(corpus["bbox"])),
                    "resource_formats": tuple(corpus["resource_formats"]),
                }
            )
            dataset_ids.add(dataset_id)
            summed_resource_count += document["resource_count"]
            dataset_id_digest.update(dataset_id.encode("ascii") + b"\n")
        if dataset_id_digest.hexdigest() != manifest["dataset_ids_sha256"]:
            raise BundleError("catalog dataset identity digest is invalid")
        if summed_resource_count != manifest["resource_count"]:
            raise BundleError("catalog resource count differs from its manifest")
        return cls(
            bundle_id=bundle_id,
            manifest_sha256=expected_manifest_sha256,
            documents_sha256=artifacts[DOCUMENTS_NAME]["sha256"],
            documents=tuple(documents),
            by_url=MappingProxyType(by_url),
            condition_metadata=MappingProxyType(condition_metadata),
        )
