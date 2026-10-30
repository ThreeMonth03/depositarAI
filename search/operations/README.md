# Product operations domain

`scripts/depositarai.py` launches the product CLI owned by [cli.py](cli.py).
Operations code prepares and verifies
metadata, catalogs, model services, releases and entry points with source-bound
receipts. For first private activation on a host with Docker, GPUs and models
already prepared, use `up-private` as described in the
[operations guide](../../docs/guide/operations.md). Granular commands remain for
diagnosis and explicit staged operations;
[PRODUCT_LIFECYCLE.md](../../docs/development/PRODUCT_LIFECYCLE.md) owns their
prerequisites, side effects and incomplete installer boundaries.

| Source | Responsibility |
|---|---|
| [cli.py](cli.py) | Argument parsing, command dispatch, safe diagnostics and exit codes |
| [config.py](config.py), [doctor.py](doctor.py) | Validate operator configuration and perform read-only preflight |
| [state.py](state.py), [lifecycle.py](lifecycle.py) | Private state, operation locks/receipts, metadata lifecycle and first private activation orchestration |
| [local_status.py](local_status.py) | Local preparation/activation records and recovery reasons; never live service health |
| [metadata/](metadata/sync.py) | Public source acquisition, bounded sync and metadata cache |
| [documents/](documents/controller.py) | Selected CSV/PDF acquisition, private content/profile caches and isolated Fess extraction; no catalog activation |
| [metadata/storage.py](metadata/storage.py) | Metadata/catalog private receipt IO, with the existing 4 MiB record limit and `SyncError` contract |
| [catalog/](catalog/execution.py) | Sealed source preparation, CPU runtime, import/reconciliation, permissions, projection, vector passes and leases |
| [catalog/commands.py](catalog/commands.py) | Catalog text-command runner and stdout validation; command selection and runtime authority stay with callers |
| [deployment/](deployment/composition.py) | Images/models, private release, gateway activation, product packages and offline restore |
| [candidate/](deployment/candidate/controller.py), [batch/](deployment/candidate/batch/execution.py) | Shared-model CPU candidates, independently verified catalog authority and bounded private case capture |
| [frontend/](deployment/frontend/delivery.py) | Standalone CPU website artifacts, admission policy, authenticated checks and delivery/rollback transactions |
| [release/controller.py](deployment/release/controller.py), [runtime.py](deployment/release/runtime.py), [probe.py](deployment/release/probe.py) | Private CPU release startup and completed-release verification; startup owns model and HTTP acceptance |
| [gateway/preparation.py](deployment/gateway/preparation.py), [activation.py](deployment/gateway/activation.py) | Verify completed release authority without restarting services or repeating model acceptance; prepare routes and verify the routed website at first activation |
| [gateway/runtime.py](deployment/gateway/runtime.py), [probe_runtime.py](deployment/gateway/probe_runtime.py) | Routing profile and bounded loopback HTTP acceptance; probes do not grant routing authority |
| [deployment/runtime_docker.py](deployment/runtime_docker.py) | Shared local Docker client with fixed CLI environment, command/output accounting, client guards and bounded project container/network inventories; controllers retain runtime authority |
| [deployment/process_identity.py](deployment/process_identity.py) | Pure process-start digest shared by inference, CPU release and gateway proofs; callers validate snapshots and own restart and acceptance policy |
| [deployment/image_authority.py](deployment/image_authority.py) | Read and authenticate completed source/image evidence against the caller's source plan; no runtime mutation or GPU/model policy |
| [public_entry/profile.py](deployment/public_entry/profile.py), [host.py](deployment/public_entry/host.py) | Optional APISIX/OIDC entrance configuration and atomic private-material preparation |
| [public_entry/preflight.py](deployment/public_entry/preflight.py), [updates.py](deployment/public_entry/updates.py) | Private-backend admission and paired CPU website startup, update and manual rollback, retaining Redis quotas |
| [shared/](shared/private_io.py) | Private IO, bounded HTTP/JSON, process-pipe capture, source identity and runtime probes |

The CLI loads a command's domain when dispatching it. Help and argument parsing
do not import product configuration, Fess or deployment controllers. CLI tests
import `search.operations.cli` directly; the launcher only locates the
repository and invokes `main`.

CLI and lifecycle orchestration depend on domain modules. Fess data/admin
primitives live in [search/catalog](../catalog/README.md); this operations domain
owns host preparation and runtime authority. Shared IO and metadata
acquisition must not depend on deployment controllers. Catalog code does not
import deployment orchestration; deployment consumes verified catalog state and
leases. A failed or uncertain receipt does not grant runtime authority, and a
cached catalog still undergoes compatibility and actual-runtime verification.
Shared HTTP types live in `search.transport.http`. Package initializers contain
documentation; callers import implementations from their owning modules.
Private service-key reads use [search/secrets.py](../secrets.py), separate from
operator receipt IO. Neither primitive depends on an API app or controller.
Data-producing source bytes remain bound to the catalog recipe. Review data
compatibility when changing them; do not relabel retained data as a new app version.

[bounded_process.py](shared/bounded_process.py) captures both pipes of an already
created process, enforces their combined byte cap and performs process-group
cleanup even if selector creation or closing fails. `image_build._run` and
`source_bundle._runner` retain process creation, environment/umask and final-wait
error handling; workflow budgets stay with their callers. API workers keep their
separate subprocess contract.
`runtime_docker.Docker` owns the shared runtime client. Release, gateway and
inference controllers use this client while retaining their own authority rules.
`image_authority.read_completed_images` reads source-bound staging/build receipts;
reading them does not prove that a deployment is running or healthy.
`catalog.commands` owns the text-command runner and its 4 MiB UTF-8 stdout check.
Metadata receipt IO belongs to storage; product-state schemas and image receipt
rules retain their own domains. Use these owners directly when updating callers,
rather than adding aliases for retired paths or test patches.

The package initializer contains documentation only. Import configuration from
`search.operations.config` and other domains explicitly, for example
`from search.operations.deployment.public_entry import profile`. Importing the
package does not load host configuration or start services.
Keep security checks at their actual trust boundaries rather than merging
similar-looking catalog, gateway and public-entry validators.

File and package names follow the [naming rules](../../STYLE.md#檔案與目錄命名).

Tests are split into [operations](../../tests/unit/operations/),
[catalog](../../tests/unit/catalog/) and [deployment](../../tests/unit/deployment/)
policy/failure cases. [tests/integration/](../../tests/integration/) covers rendered
Compose and isolated real OSS bootstrap, update and manual rollback. Use existing
gates from [CI](../../.github/workflows/ci.yml); unit receipts alone do not prove a
live deployment is healthy.
Public-entry unit tests mirror the source domain in
[deployment/public_entry/](../../tests/unit/deployment/public_entry/); real OSS
cases live in [integration/public_entry/](../../tests/integration/public_entry/).
Gateway and private-release unit tests likewise live in
[deployment/gateway/](../../tests/unit/deployment/gateway/) and
[deployment/release/](../../tests/unit/deployment/release/).
Standalone frontend unit tests live in
[deployment/frontend/](../../tests/unit/deployment/frontend/).
Shared-model candidate and batch unit tests live in
[deployment/candidate/](../../tests/unit/deployment/candidate/).

Follow [private runtime](../../docs/development/PRIVATE_RUNTIME.md),
[catalog reuse](../../docs/development/CATALOG_CACHE.md),
[offline restore](../../docs/development/PRODUCT_RESTORE.md) and
[public access](../../docs/development/PUBLIC_ACCESS.md) for operational detail.
Public-entry update/rollback manages the separate CPU entrance; it does not
implement full-product catalog/model update or rollback.
