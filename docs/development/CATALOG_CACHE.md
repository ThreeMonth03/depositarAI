# Reuse catalog data across application releases

`stage-catalog` can reuse a completed catalog after an application, frontend or
gateway change. It retains the original catalog plan, images, vector journals
and query identities. New application images and release receipts still belong
to the current clean Git commit. For command/configuration details, use the
[product CLI reference](PRODUCT_LIFECYCLE.md); for the first deployment, start
with the [operations guide](../guide/operations.md).

## Normal development flow

Use the same private product configuration and state root:

```bash
python3 scripts/depositarai.py --config /private/product.json stage-catalog \
  --snapshot /private/public-metadata.jsonl.gz
python3 scripts/depositarai.py --config /private/product.json prepare-query
```

With identical metadata and compatible data-producing sources, preparation
selects an existing complete slot instead of creating one for the new app
commit. Import is skipped when its matching completion receipt exists. Completed
vectors are reverified with `passes_this_invocation: 0`; no new vector helper,
administrator token or journal is created. The stage result reports the current
`source_revision`, original `catalog_revision` and actual `catalog_reused` flag.

The normal image, inference, release and gateway commands follow staging. Their
application source identity remains current; catalog provenance is not retagged.
Moving development to a different source checkout preserves the original
sealed Compose build-path metadata. Actual builds use the verified minimal
staged sources, and startup uses immutable images with `--no-build`; the old
checkout path is not used to build or authenticate current application code.

## Cache compatibility

Reuse requires the full metadata bundle path, manifest digest and document
count, plus compatible committed sources for:

- Fess/OpenSearch/Ollama Compose configuration, bootstrap and image builds;
- metadata generation, importer, vector journal and administrator image;
- embedding model identity, chunk/vector recipe, projection, filters and ACL;
- the original private security material and rendered Compose configuration.

`backend.DATA_FILES`, together with `STAGED_FILES` and `BUILD_FILES`,
enumerate the conservative source closure. For the vector module, compatibility
compares the complete `_recipe` function AST, so host-only orchestration changes
can remain compatible while the recipe is unchanged. Its fixed profile,
imported model identity and Compose inputs remain bound. Source-boundary tests
reject new unsealed external profile dependencies. Changes to validation
contracts may still reject an older catalog and require an explicit migration
or rebuild; compatibility never means ignoring a failed checkpoint check.

Catalog preparation lives in `search/catalog/`: sealed bundles, Fess
administration, inventory, import and vector work have separate owners. The
importer runs `python3 -m search.catalog_admin`. `embedding.py` owns the fixed
embedding model identity shared by vector jobs and runtime query embedding.
`query.py` owns literal query normalization and limits, without IO.
Runtime queries use `search/api/retrieval/index.py` and `pipeline.py` for
OpenSearch lexical/vector retrieval and Haystack RRF.
The admin/import/vector source closure, Docker COPY inputs and helper entrypoint
belong to the data recipe. API retrieval code stays outside that admin closure, so
a query change need not invalidate persisted data. Moving the data-producing
inputs changes their identity; no old-module forwarding or legacy hash allowlist
makes a prior slot compatible.

A changed data recipe creates a new slot. Missing original Git objects, dirty
current source or corrupted retained evidence is an error, not a cache hit.
Keep the original source history; the product's Git bundle includes it.

Only completed retained catalogs are selected automatically. A current commit's
partial slot keeps its existing recovery rules, rather than silently switching
to a different slot. Automatic selection is bounded by the existing slot and
journal limits and never prunes old data.

## Catalog storage and bootstrap changes

The product configuration uses schema `2` with
`catalog_storage: "docker-volume"`. OpenSearch index data belongs to a fixed
catalog-specific Docker local volume. Its sealed identity, local driver,
labels, initialization receipt and actual mount must agree before startup or
reuse. Compose references that volume as external with `nocopy: true`; an
existing volume is not adopted by name alone.

Storage and bootstrap inputs belong to the catalog recipe. A change requires a
new catalog slot, metadata import and vector generation. It does not itself
change the metadata builder: an intact sealed metadata bundle remains reusable
when its builder sources and manifest still pass validation. Keep its original
identity and snapshot rather than downloading the same source unnecessarily.

Old bind-based product state and runtime receipts are not migrated in place.
Prepare a private state root using the current
[configuration template](../../deploy/env/product.example.json), retain the old
state for handoff, and follow the normal preparation flow. Do not rewrite
`product.json`, catalog manifests or completion receipts, copy live index files,
or attach an old volume to make it appear to be a new completed catalog.

## When metadata generation sources change

`search.catalog.build` owns offline bundle generation. Its manifest binds the
exact builder source closure, package initializers and catalog identity policy.
The reader validates the manifest format, fixed policy and artifact integrity;
product restore independently checks the builder digest against committed source.
Changing these inputs changes the bundle and catalog recipe even if normalized
documents are identical. Prepare a new catalog slot, import metadata and build vectors.
An application-only change outside this closure can still use the normal reuse
flow described above.

A verified original JSONL/gzip snapshot can be used explicitly with `--snapshot`
to rebuild offline. It is an input to a new bundle, not a cache hit or a new
verification that CKAN still exposes those datasets. Offline sync records
`source_consistency=offline_not_checked`. Keep the original snapshot digest and
provenance; do not rewrite an existing manifest or completion receipt.

Existing state also needs a deliberate handoff. TTL cache verifies its sealed
bundle, and lifecycle sync automatically uses the previous staged metadata as
the diff baseline. That baseline is validated before either download or offline
build. An incompatible baseline therefore stops `sync` and `stage-catalog` even
with `--snapshot` or `--force`; the CLI does not provide a baseline-reset or
automatic migration option. Preserve the old state and running consumers, then
plan isolated metadata/catalog preparation and the deployment handoff. Building
a standalone bundle alone does not migrate product state or activate a release.

## Runtime checks before reuse

Reuse rechecks immutable images, original security/configuration, actual Docker
runtime and complete import/vector cleanup history. `prepare-query` and release
acceptance still scan the real index, check vectors/projection and positive and
negative permissions. A local completion receipt alone grants no live authority.

An attached release remains subject to the existing explicit client lease and
handoff rules. Reuse does not stop existing consumers, detach peers, recover
uncertain jobs or bypass storage write failures.

## Product packages and a fresh host

A package may contain current application source/images and compatible catalog
images from an earlier commit. `package.json.catalog.revision` and
`catalog-images.json.revision` record that original commit; the package's top
level revision records the application commit. Both image provenances and the
original image receipt are verified without relabelling images.

[Product restore](PRODUCT_RESTORE.md) verifies the package and restores source;
large models and `images.tar` remain in the retained package. It does not load
Docker images, install models in configured directories or establish runtime
authority. Keep the original package and reverify its assets before use.

Separately prepare the host, private product configuration, model directories
and required local images, including their runtime identity checks. Full
fresh-host installation and registry identity restore are not provided by
`restore-product`. Once those prerequisites are ready and metadata is synced
from the packaged snapshot, choose the package's original catalog revision
when preparing its fresh slot:

```bash
python3 scripts/depositarai.py --config /private/product.json prepare-index \
  --catalog-revision ORIGINAL_CATALOG_COMMIT
python3 scripts/depositarai.py --config /private/product.json build-catalog-images
python3 scripts/depositarai.py --config /private/product.json start-index
python3 scripts/depositarai.py --config /private/product.json import-index
python3 scripts/depositarai.py --config /private/product.json build-vectors
python3 scripts/depositarai.py --config /private/product.json prepare-query
```

`build-catalog-images` prepares the service images and catalog-admin helper,
including their sealed receipts for this slot; it may build images. This is
source-based preparation, not an archive-only installer. Volume initialization
requires a verified local catalog-admin image and this slot's completed
`admin-image.json` in addition to the three catalog service images.

`start-index --prebuilt-images /private/package/catalog-images.json` is an
alternative only after those admin prerequisites already exist and pass
verification. That startup path does not build or pull missing images; it
rejects absent or invalid admin evidence before writing startup intent. A
package's three-service image receipt alone cannot cold-start a new catalog,
and `restore-product` does not install images or restore runtime receipts.

The explicit revision option requires the same source compatibility check; it
does not permit an incompatible catalog. `stage-catalog --catalog-revision COMMIT`
also supports this choice when using the normal image-staging flow.

**Packages currently carry metadata, models and images, not built index
snapshots.** A fresh host still imports metadata and builds vectors. Same-host
reuse avoids repeating that work across compatible application commits.

The current package does not automate index snapshot transfer. OpenSearch
snapshot/restore would need a repository-enabled runtime profile and fresh
restored-index validation; copying data directories or old host runtime receipts
does not establish a restored catalog. See the [official snapshot documentation](https://docs.opensearch.org/latest/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-restore/).

## Keep development checks bounded

Use the [development guide](../guide/development.md) to select affected checks
and the [CI performance reference](CI_PERFORMANCE.md) for gate selection and
cache behavior. Real catalog/model acceptance is needed when data, models,
runtime or relevant contracts change; a cache hit does not replace it.
