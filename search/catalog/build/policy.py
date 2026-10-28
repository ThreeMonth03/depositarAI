"""Metadata builder limits, output fields and fixed validation policy."""

from search.catalog import resources

MAX_COMPRESSED_BYTES = 64 * 1024 * 1024


MAX_EXPANDED_BYTES = 128 * 1024 * 1024


MAX_LINE_BYTES = 1024 * 1024


MAX_ROWS = 10_000


MAX_RESOURCES = 256


MAX_NESTING = 32


MAX_ARTIFACT_COMPRESSED_BYTES = 64 * 1024 * 1024


MAX_ARTIFACT_EXPANDED_BYTES = 128 * 1024 * 1024


MAX_ARTIFACT_LINE_BYTES = 2 * 1024 * 1024


DATASET_URL_PREFIX = "https://data.depositar.io/dataset/"


FIXED_LABEL = "depositar-public-dataset"


FIXED_SEARCH_ROLE = "Rdepositar_catalog"


FIXED_VIRTUAL_HOST = "depositar_catalog"


ARTIFACT_NAMES = (
    "source.jsonl.gz",
    "corpus.v2.jsonl.gz",
    "fess-documents.jsonl.gz",
    "resource-context.v1.jsonl.gz",
)


MANIFEST_NAME = "bundle-manifest.json"


LOCK_NAME = ".catalog-bundle.lock"


STRING_LIMITS = {
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
    "resource_context_name": resources.MAX_RESOURCE_NAME_CHARS,
    "resource_context_column": resources.MAX_COLUMN_CHARS,
    "resource_context_index_text": resources.MAX_INDEX_TEXT_CHARS,
}


ARRAY_LIMITS = {
    "keywords": 128,
    "tags": 128,
    "language": 32,
    "data_type": 64,
    "groups": 64,
    "resources": MAX_RESOURCES,
}


FESS_FIELD_ALLOWLIST = frozenset(
    {
        "url",
        "title",
        "important_content",
        "content",
        "mimetype",
        "filetype",
        "label",
        "lang",
        "role",
        "boost",
        "virtual_host",
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
    }
)


POLICY = {
    "input": {
        "compressed_bytes_max": MAX_COMPRESSED_BYTES,
        "expanded_bytes_max": MAX_EXPANDED_BYTES,
        "line_bytes_max": MAX_LINE_BYTES,
        "rows_min": 1,
        "rows_max": MAX_ROWS,
        "json_nesting_max": MAX_NESTING,
        "resources_per_dataset_max": MAX_RESOURCES,
        "regular_file": True,
        "no_symlink": True,
        "link_count": 1,
        "owner": "effective_uid",
        "strict_utf8": True,
        "strict_json": True,
    },
    "selection": {"private": False, "state": "active", "type": "dataset"},
    "output": {
        "artifact_compressed_bytes_max": MAX_ARTIFACT_COMPRESSED_BYTES,
        "artifact_expanded_bytes_max": MAX_ARTIFACT_EXPANDED_BYTES,
        "artifact_line_bytes_max": MAX_ARTIFACT_LINE_BYTES,
        "artifact_records_min": 1,
        "artifact_records_max": MAX_ROWS,
        "gzip_mtime": 0,
        "file_mode": "0400",
        "directory_mode": "0500",
    },
    "normalization": {
        "unicode": "NFKC",
        "html": "text-only; script/style/noscript removed",
        "controls": "Unicode Cc/Cf removed",
        "string_character_limits": STRING_LIMITS,
        "array_item_limits": ARRAY_LIMITS,
    },
    "fess": {
        "field_allowlist": sorted(FESS_FIELD_ALLOWLIST),
        "fixed_label": FIXED_LABEL,
        # The Documents API accepts already-encoded role values. ``{role}``
        # is UI/config notation and is not expanded by bulk registration.
        "role": [FIXED_SEARCH_ROLE],
        "boost": 1.0,
        "virtual_host": [FIXED_VIRTUAL_HOST],
        "resource_download_url_field_included": False,
        "resource_landing_url_field_included": True,
        "email_addresses": False,
        "raw_spatial": False,
    },
    "resource_context": {
        "resources_per_dataset_max": resources.MAX_RESOURCES,
        "columns_per_resource_max": resources.MAX_COLUMNS,
        "column_characters_max": resources.MAX_COLUMN_CHARS,
        "column_total_characters_max": resources.MAX_COLUMN_TOTAL_CHARS,
        "excerpt_characters_max": resources.MAX_EXCERPT_CHARS,
        "index_text_characters_max": resources.MAX_INDEX_TEXT_CHARS,
    },
    "dataset_id_digest_encoding": "lowercase UUIDs sorted, one UTF-8 ID plus LF per record",
}


class BundleError(ValueError):
    """The input or destination violates a bundle safety invariant."""
