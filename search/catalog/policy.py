"""Fixed catalog wire fields, validation limits and endpoint policy."""

from __future__ import annotations

import re


CATALOG_LABEL = "depositar-public-dataset"


CATALOG_SEARCH_ROLE = "Rdepositar_catalog"


CATALOG_VIRTUAL_HOST = "depositar_catalog"


BUNDLE_BUILDER_NAME = "search.catalog.build"


MAX_DOCUMENTS = 10_000


MAX_INVENTORY_PAGE = 100


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


NONCE_RE = re.compile(r"^[0-9a-f]{12}$")


ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,1000}$")


_BIDI_CONTROLS = frozenset(
    chr(value)
    for value in (
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    )
)
