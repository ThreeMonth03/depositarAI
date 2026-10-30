#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
docker build -f deploy/docker/api/Dockerfile -t depositar-search-conversation:ci .
bash scripts/ci/runtime-imports.sh depositar-search-conversation:ci conversation
docker build --network none -f deploy/docker/importer/Dockerfile -t depositar-search-catalog-admin:ci .
bash scripts/ci/runtime-imports.sh depositar-search-catalog-admin:ci catalog-admin
docker build --target tests -f deploy/docker/api/Dockerfile -t depositar-search-api:ci .
# Product roots cover new modules and tests without a parallel file list.
lint_roots=(search scripts tests deploy/docker)
for lint_action in check format; do
  lint_options=(--no-cache)
  if [[ "$lint_action" == format ]]; then lint_options+=(--check); fi
  docker run --rm --network none --user "$(id -u):$(id -g)" --workdir /source \
    --mount "type=bind,src=$project_dir,dst=/source,readonly" \
    depositar-search-api:ci ruff "$lint_action" "${lint_options[@]}" "${lint_roots[@]}"
done
docker run --rm --network none --user "$(id -u):$(id -g)" --workdir /source \
  --mount "type=bind,src=$project_dir,dst=/source,readonly" \
  depositar-search-api:ci python -m unittest -v --durations 25 \
    tests.unit.deployment.test_python_unused_gate
docker run --rm --network none --user "$(id -u):$(id -g)" --workdir /source \
  --mount "type=bind,src=$project_dir,dst=/source,readonly" \
  depositar-search-api:ci bash scripts/ci/python-unused.sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --cpus 2 --memory 2g --pids-limit 96 \
  --tmpfs /tmp:rw,nosuid,nodev,size=32m,mode=1777 \
  --mount "type=bind,src=$project_dir/search/tests,dst=/app/search/tests,readonly" \
  --mount "type=bind,src=$project_dir/search/catalog/build,dst=/app/search/catalog/build,readonly" \
  --mount "type=bind,src=$project_dir/search/web,dst=/app/search/web,readonly" \
  depositar-search-api:ci python -m unittest --durations 25 \
    search.tests.test_http_application \
    search.tests.test_conversation_boundary search.tests.test_retrieval \
    search.tests.test_conversation_retrieval search.tests.test_model_transport \
    search.tests.test_planning_transport \
    search.tests.test_worker_io search.tests.test_readiness \
    search.tests.test_pipeline search.tests.test_index -v
