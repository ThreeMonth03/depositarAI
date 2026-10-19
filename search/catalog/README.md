# Catalog domain

This package builds and validates sealed metadata and implements Fess
administration, inventory, imports and vector work. Host preparation and Docker
lifecycle belong to `search/operations/catalog/`; HTTP request handlers do not
perform administration.

| Module | Responsibility |
|---|---|
| `build/builder.py`, `build/cli.py` | Manifest construction, offline bundle orchestration and the `python3 -m search.catalog.build` command |
| `build/normalize.py`, `build/codec.py`, `build/policy.py` | Dataset normalization, Fess documents, strict JSON and bounded input/output policy |
| `build/snapshot.py`, `build/storage.py` | Snapshot reading, retained file identity, sealed writes and atomic publication |
| `build/identity.py` | Exact builder source closure and its manifest source digest |
| [bundle.py](bundle.py), [inventory.py](inventory.py) | Sealed bundle integrity and exact remote document identity/pagination |
| [query.py](query.py) | Literal query normalization and input/result-count limits, without IO |
| [embedding.py](embedding.py) | Fixed embedding model identity shared by catalog vectors and API query embedding |
| [transport.py](transport.py), [session.py](session.py) | Bounded request deadlines and common authenticated Fess session behavior |
| [admin.py](admin.py) | Administrative endpoints and the single `AdminAccess` token lifecycle |
| [importer.py](importer.py), [vectors.py](vectors.py) | Import and vector workflows, each using `AdminAccess` |
| [journal.py](journal.py) | Durable event interface shared by token and vector operations |
| [codec.py](codec.py), [policy.py](policy.py), [errors.py](errors.py) | Exact data shapes, catalog identity policy and bounded failure diagnostics |

Import the owner explicitly:

```python
from search.catalog.build.builder import build_bundle
from search.catalog.bundle import CatalogBundle
```

The builder accepts an existing JSONL or gzip snapshot, without network or
third-party dependencies. Public-source downloads, TTL cache and staged diffs
belong to `search/operations/metadata/`. `bundle.py` reads and validates a sealed
bundle; it does not build one.

Runtime queries belong to `search/api/retrieval/`: OpenSearch lexical/vector
retrieval and Haystack RRF use the sealed catalog, without a Fess search adapter.
Package initializers have no re-exports or runtime side effects. Query callers
do not gain builder or admin operations through an importer or compatibility facade.

For a standalone offline build, use an operator-owned snapshot and an existing
private output directory with mode `0700`:

```sh
python3 -m search.catalog.build build \
  --input /private/datasets.jsonl.gz \
  --output-root /private/catalog-bundles
```

The command publishes a content-addressed bundle and reports its path, manifest
digest and counts. It does not import an index, build vectors or activate a
release. The manifest identifies `search.catalog.build` and binds its exact
source closure, including package initializers and catalog identity policy.
Changing these inputs changes bundle provenance even when normalized data is
identical; retain the original snapshot and prepare a new catalog recipe.

The importer container runs `python3 -E -s -B -u -m search.catalog_admin` against the same
canonical package. Source moves, Docker inputs and the catalog data recipe must
change together; do not retain old module aliases or accept previous recipe
hashes to conceal a changed source identity.

Operational guidance: [catalog cache and recipe changes](../../docs/development/CATALOG_CACHE.md),
[vector passes](../../docs/development/VECTOR_PASSES.md) and
[token cleanup](../../docs/CATALOG_CLEANUP_RECOVERY.md). Search and failure tests
live in `search/tests/`; host/runtime policy tests live in `tests/unit/catalog/`.
