# Search product source

`search/` contains the dataset-search runtime and the operator code that prepares
its private releases. [Product lifecycle](../docs/development/PRODUCT_LIFECYCLE.md)
owns the command contracts; this page explains where implementation belongs.

| Domain | Responsibility and entry point |
|---|---|
| [api/](api/README.md) | Conversation HTTP contract, internal retrieval and bounded execution workers |
| `web/` | CPU BFF: browser origin, temporary session ownership, version fencing and the private API client |
| `http/` | Shared Django/DRF settings, async views and ASGI lifecycle; independent of search and operator domains |
| `planning/` | Validated conditions, request clock, deterministic dates and model planning |
| `quality/` | Pure ordered-plan contracts and offline captured-response evaluation, shared by CLI and batch tools |
| [catalog/](catalog/README.md) | Offline metadata builds, sealed bundle validation, embedding profile, Fess administration, imports and vector work |
| [scope.py](scope.py) | Classify whether an input belongs to dataset search |
| [model_policy.py](model_policy.py) | Pinned scope/planner model identities and fixed request limits |
| [transport/](transport/http.py), [serialization.py](serialization.py), [secrets.py](secrets.py) | Bounded HTTP, pure strict JSON parsing and private service-key file validation, independent of HTTP app factories |
| `reranker/` | Separate reranker service and its wire contract |
| [operations/](operations/README.md) | Configuration, metadata/catalog preparation, receipts and deployment lifecycle |

The browser calls the BFF; the BFF calls the conversation API. The API validates
conditions and invokes the OpenSearch lexical/vector pipeline; Haystack RRF
combines candidates before the configured reranker. Fess is used for catalog
preparation and administrative jobs. Operator CLI code prepares and
verifies these services; request handling does not call deployment orchestration.
Pure planning, transport, serialization and secret-file validation must not
depend on app factories or operator lifecycle code.

Use explicit module imports. Package `__init__.py` files stay lightweight;
importing a package must not read secrets, start Docker,
initialize a model or load an HTTP app. When a module moves, update its consumers
and remove the retired import path; do not retain forwarding packages or test-only
aliases.
Package initializers contain documentation. Import shared HTTP interfaces from
`search.transport.http`. The secret reader performs file IO only when explicitly
called.

`search/tests/` covers search, API, BFF and planning behavior. Offline quality tests
live in `tests/unit/quality/`. Operator policy and
failure tests live in `tests/unit/{operations,catalog,deployment}/`; isolated OSS
runtime cases live in `tests/integration/`, and browser behavior in `web/tests/`.
See [API source](api/README.md), [catalog source](catalog/README.md) and
[operations source](operations/README.md) for
focused test locations. Existing gates and validation rules are documented in
[STYLE.md](../STYLE.md) and the [repository layout](../docs/development/REPOSITORY_LAYOUT.md).

Website behavior is defined in [SEARCH_WEBSITE.md](../docs/SEARCH_WEBSITE.md).
Operational and design references start in the [documentation index](../docs/README.md).
Source history and past acceptance belong in Git, PRs and versioned benchmark JSON.
