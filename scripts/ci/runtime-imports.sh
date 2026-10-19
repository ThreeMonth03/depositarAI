#!/usr/bin/env bash
# Verify the built image itself; source bind mounts must not mask missing files.
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo 'Usage: runtime-imports.sh IMAGE {conversation|web|reranker|catalog-admin}' >&2
  exit 64
fi

image="$1"
role="$2"
if [[ -z "$image" || "$image" == -* ]]; then
  echo 'Error: a Docker image reference is required.' >&2
  exit 64
fi
case "$role" in
  conversation|web|catalog-admin) interpreter='/usr/local/bin/python' ;;
  reranker) interpreter='/opt/conda/bin/python' ;;
  *)
    echo 'Error: unsupported runtime import role.' >&2
    exit 64
    ;;
esac

docker run --rm --runtime runc --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --cpus 2 --memory 2g --pids-limit 96 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777 \
  --entrypoint "$interpreter" "$image" -I -B -c '
import importlib
from importlib.metadata import distributions
import json
from pathlib import Path
import sys

roles = {
    "conversation": (
        "search.api.conversation.app",
        "search.api.conversation.worker",
        "search.api.retrieval.engine",
        "search.api.retrieval.worker",
        "search.api.retrieval.pipeline",
        "search.operations.shared.runtime_probe",
    ),
    "web": (
        "search.web.app",
        "search.web.sessions",
        "search.web.upstream",
    ),
    "reranker": ("search.reranker.server",),
    "catalog-admin": ("search.catalog_admin",),
}
role = sys.argv[1]
package_root = "/opt/depositar-search" if role == "catalog-admin" else "/app"
search_root = Path(package_root) / "search"
assert not any(Path("/dev").glob("nvidia*")), "GPU exposed to CPU-only import probe"
assert search_root.is_dir() and not search_root.is_symlink(), "missing image package"
for relative in (
    "evaluation",
    "operations/catalog",
    "operations/metadata",
    "operations/cli.py",
    "catalog/build",
    "catalog_bundle.py",
    "fess_catalog.py",
    "customer_service.py",
    "customer_service_fess.py",
    "customer_service_ollama.py",
    "api/planned",
    "api/app.py",
    "api/engine.py",
    "api/schemas.py",
    "api/selector.py",
    "api/native_search.py",
    "api/native_pipeline.py",
    "api/provision.py",
    "api/retrieval/fess.py",
    "catalog/client.py",
):
    path = search_root / relative
    assert not (path.exists() or path.is_symlink()), "offline code in runtime image"

if role == "reranker":
    for relative in ("api", "web", "operations", "transport"):
        path = search_root / relative
        assert not (path.exists() or path.is_symlink()), "unrelated code in reranker image"

if role == "conversation":
    for relative in ("catalog/admin.py", "catalog/journal.py",
                     "catalog/session.py", "catalog/transport.py"):
        path = search_root / relative
        assert not (path.exists() or path.is_symlink()), "admin code in query image"

if role == "web":
    for relative in ("operations", "catalog/admin.py", "catalog/client.py",
                     "catalog/importer.py", "catalog/vectors.py",
                     "model_policy.py", "scope.py", "api/retrieval/results.py",
                     "api/retrieval/fess.py",
                     "api/conversation/app.py", "api/conversation/worker.py"):
        path = search_root / relative
        assert not (path.exists() or path.is_symlink()), "unrelated code in web image"

if role == "catalog-admin":
    for relative in ("api", "web", "catalog/query.py", "catalog/client.py"):
        path = search_root / relative
        assert not (path.exists() or path.is_symlink()), "query code in admin image"

sys.path.insert(0, package_root)
importlib.import_module("search.serialization")
if role != "catalog-admin":
    importlib.import_module("search.secrets")
for name in roles[role]:
    importlib.import_module(name)

if role in {"conversation", "web"}:
    installed = {distribution.metadata["Name"].lower() for distribution in distributions()}
    assert {"django", "djangorestframework", "adrf", "asgiref"} <= installed
    assert not {"fastapi", "starlette"} & installed, "retired HTTP framework installed"

loaded = {
    name: module
    for name, module in sys.modules.items()
    if name == "search" or name.startswith("search.")
}
for name, module in loaded.items():
    filename = getattr(module, "__file__", None)
    assert filename is not None, "runtime package initializer missing: " + name
    path = Path(filename).resolve()
    assert path.is_relative_to(search_root), "module outside image source: " + name
    relative = path.relative_to(search_root).as_posix()
    assert not (
        relative.startswith("evaluation/")
        or relative.startswith("catalog/build/")
        or relative.startswith("operations/catalog/")
        or relative.startswith("operations/metadata/")
    ), "offline implementation loaded: " + name

print(json.dumps({
    "role": role,
    "imports": roles[role],
    "loaded_search_modules": len(loaded),
    "runtime_source": str(search_root),
    "status": "passed",
}, sort_keys=True))
' "$role"
