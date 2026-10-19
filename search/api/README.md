# Search API domain

The Django/DRF conversation API is the only search HTTP application. The CPU BFF in
`search/web/` owns browser origin, temporary sessions and caller identity;
service-key authentication belongs to this private API.

| Source | Responsibility |
|---|---|
| [conversation/app.py](conversation/app.py) | Private `/v1/turn`, version and readiness endpoints |
| [conversation/](conversation/engine.py) | Turn planning, state edits, clarification, calendar/time validation and execution |
| [retrieval/](retrieval/engine.py) | Validated condition-aware retrieval, filtering, candidate ranking and result contracts; an internal library, without a separate HTTP app |
| [retrieval/results.py](retrieval/results.py) | Validated result cards and the bounded `ValidatedSearch` result contract |
| [retrieval/index.py](retrieval/index.py) | Fixed OpenSearch query-only client, embedding requests and sealed index/result validation |
| [retrieval/query.py](retrieval/query.py) | Literal lexical/vector query compilation with shared catalog and condition filters |
| [retrieval/pipeline.py](retrieval/pipeline.py) | Lexical and vector retrieval with the same index conditions, joined by Haystack reciprocal rank fusion |
| [shared/schemas.py](shared/schemas.py) | Strict model base used by API contracts |
| [shared/admission.py](shared/admission.py) | Bounded execution admission without importing an engine |
| [shared/peers.py](shared/peers.py), [shared/model_transport.py](shared/model_transport.py) | Reviewed private peers and role-bound model requests |
| [shared/](shared/config.py) | Runtime settings, service-key middleware, readiness and subprocess worker IO |
| [worker.py](worker.py) | Bounded retrieval, scope-model, reranker and readiness operations |

Scope classification lives in [search/scope.py](../scope.py). Shared model pins
and request limits live in [search/model_policy.py](../model_policy.py); condition
planning lives in `search/planning/`.

Requests flow from conversation to retrieval and narrow workers. Pure planning,
HTTP, JSON and secret-file validation do not import an app factory or operator
controller. Every search uses OpenSearch lexical and vector retrieval, with
time/format conditions applied in the index before candidate limits. Haystack
RRF combines the two ranked lists; the configured reranker can then reorder the
candidates. Fess handles catalog administration, import and vector jobs, outside
the runtime query path. The shared embedding profile lives in
[catalog/embedding.py](../catalog/embedding.py).

HTTP views use ADRF for asynchronous DRF dispatch and share the Django settings
and ASGI lifecycle in `search/http/`. The existing Pydantic request/response
contracts remain framework-independent. Neither HTTP service needs a relational
database; browser identity is provided by the existing OIDC entrance.

Import each responsibility directly;
package initializers contain documentation or small static interfaces and do
not load models, read keys or start services.

Use `search.api.shared.schemas.StrictModel`,
`search.api.shared.admission.Slots`, `search.transport.http`,
`search.serialization.strict_json` and `search.secrets.read_secret` as their
canonical owners. Retired module paths have no forwarding packages. Catalog
source identity is handled by the [catalog recipe](../../docs/development/CATALOG_CACHE.md),
not by maintaining another API version.

API behavior and HTTP boundary tests live in [search/tests/](../tests/).
Import and image-closure checks live in
[tests/unit/deployment/](../../tests/unit/deployment/); browser tests exercise
Nginx, BFF and API together. Runtime dependencies are pinned in this directory;
BFF and reranker have separate locks for their separate images.

The private wire contract is [API.md](../../docs/API.md). Browser sessions and
website settings are documented in [SEARCH_WEBSITE.md](../../docs/SEARCH_WEBSITE.md).
Deployment and activation belong to the
[product lifecycle](../../docs/development/PRODUCT_LIFECYCLE.md).
