#!/usr/bin/env bash
set -euo pipefail
suite=all
if [[ "$#" != 0 ]]; then
  if [[ "$#" != 2 || "$1" != --suite ]]; then
    echo 'Usage: test-search-web.sh [--suite {all|oss|browser}]' >&2
    exit 64
  fi
  suite="$2"
fi
case "$suite" in
  all|oss|browser) ;;
  *)
    echo 'Usage: test-search-web.sh [--suite {all|oss|browser}]' >&2
    exit 64
    ;;
esac
printf 'CI_SUITE suite=%s\n' "$suite"
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
ci_name="depositar-web-ci-$$"
browser_fixture_started=0
browser_pull_pid=''
phase_name=contracts
phase_started=$SECONDS
ci_started=$SECONDS
begin_phase() {
  printf 'CI_PHASE phase=%s seconds=%s exit=0\n' "$phase_name" "$((SECONDS - phase_started))"
  phase_name="$1"
  phase_started=$SECONDS
}
cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$browser_pull_pid" ]]; then
    # GNU timeout owns a process group containing only this disposable pull.
    if jobs -pr | grep -qx "$browser_pull_pid"; then
      kill -KILL -- "-$browser_pull_pid" 2>/dev/null || true
      kill -KILL "$browser_pull_pid" 2>/dev/null || true
    fi
    wait "$browser_pull_pid" 2>/dev/null || true
  fi
  if [[ "$browser_fixture_started" == 1 ]]; then
    docker rm --force "$ci_name-gpu" "$ci_name-web" "$ci_name-nginx" >/dev/null 2>&1 || true
    docker network rm "$ci_name" >/dev/null 2>&1 || true
  fi
  printf 'CI_PHASE phase=%s seconds=%s exit=%s\n' "$phase_name" "$((SECONDS - phase_started))" "$status"
  printf 'CI_TOTAL seconds=%s exit=%s\n' "$((SECONDS - ci_started))" "$status"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 -m unittest tests.test_search_web_deploy -v
docker compose --env-file deploy/env/.env.search-web.example -f deploy/compose/compose.search-web.yaml \
  config --format json | python3 scripts/validate_search_web.py
node_image='node:24.14.0-bookworm-slim@sha256:d8e448a56fc63242f70026718378bd4b00f8c82e78d20eefb199224a4d8e33d8'
browser_image='mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27'
begin_phase python-images
docker build --target tests -f deploy/docker/api/Dockerfile -t depositar-search-api:ci .
docker build -f deploy/docker/api/Dockerfile -t depositar-search-conversation:ci .
begin_phase python-unit
bash scripts/ci/runtime-imports.sh depositar-search-conversation:ci conversation
docker run --rm --network none --user "$(id -u):$(id -g)" --workdir /app \
  --mount "type=bind,src=$project_dir/search/tests,dst=/app/search/tests,readonly" \
  --mount "type=bind,src=$project_dir/search/web,dst=/app/search/web,readonly" \
  --mount "type=bind,src=$project_dir/search/catalog/build,dst=/app/search/catalog/build,readonly" \
  depositar-search-api:ci python -m unittest search.tests.test_http_application search.tests.test_conversation search.tests.test_conversation_calendar search.tests.test_conversation_time search.tests.test_search_web search.tests.test_search_web_identity search.tests.test_worker_io -v
begin_phase python-style
for action in check format; do
  options=(--no-cache)
  if [[ "$action" == format ]]; then options+=(--check); fi
  docker run --rm --network none --user "$(id -u):$(id -g)" --workdir /app \
    --mount "type=bind,src=$project_dir/ruff.toml,dst=/app/ruff.toml,readonly" \
    --mount "type=bind,src=$project_dir/search,dst=/app/search,readonly" \
    depositar-search-api:ci ruff "$action" "${options[@]}" search/web search/api/conversation/app.py \
      search/api/conversation/contract.py search/api/conversation/engine.py search/api/conversation/worker.py \
      search/api/conversation/calendar.py search/tests/test_conversation_calendar.py \
      search/api/conversation/time.py search/tests/test_conversation_time.py \
      search/tests/test_conversation.py search/tests/test_search_web.py search/tests/test_search_web_identity.py search/tests/web_fixture.py
done
for action in check format; do
  options=(--no-cache)
  if [[ "$action" == format ]]; then options+=(--check); fi
  docker run --rm --network none --user "$(id -u):$(id -g)" --workdir /app \
    --mount "type=bind,src=$project_dir/ruff.toml,dst=/app/ruff.toml,readonly" \
    --mount "type=bind,src=$project_dir/scripts,dst=/app/scripts,readonly" \
    --mount "type=bind,src=$project_dir/tests,dst=/app/tests,readonly" \
    depositar-search-api:ci ruff "$action" "${options[@]}" scripts/validate_search_web.py \
      tests/test_search_web_deploy.py
done
begin_phase npm
npm_cache_dir="${CI_NPM_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/depositar-ai/npm}"
[[ "$npm_cache_dir" == /* && "$npm_cache_dir" != *,* && \
  "$npm_cache_dir" != *$'\n'* && "$npm_cache_dir" != *$'\r'* ]] || {
  echo 'Error: npm cache must be an absolute path without commas or line breaks' >&2
  exit 1
}
mkdir -p -- "$npm_cache_dir"
docker run --rm --user "$(id -u):$(id -g)" -e npm_config_cache=/tmp/npm \
  --mount "type=bind,src=$npm_cache_dir,dst=/tmp/npm" \
  --mount "type=bind,src=$project_dir/web,dst=/web" --workdir /web "$node_image" \
  npm ci --ignore-scripts --no-audit --no-fund
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$project_dir/web,dst=/web,readonly" --workdir /web "$node_image" npm run format:check
begin_phase web-image
docker build -f deploy/docker/web/Dockerfile -t depositar-search-web:ci .
docker build -f deploy/docker/web-nginx/Dockerfile -t depositar-search-web-nginx:ci .
bash scripts/ci/runtime-imports.sh depositar-search-web:ci web
if [[ "$suite" != oss ]]; then
  # Download bytes before the sequential browser phase; all overlaps the OSS suite.
  if ! docker image inspect "$browser_image" >/dev/null 2>&1; then
    timeout --kill-after=5s 180s bash -s -- "$browser_image" <<'BROWSER_PULL' &
    # Keep timeout's monitored child alive until the pull exits or KILL fires.
    trap : TERM
    started=$SECONDS
    status=0
    docker pull "$1" >/dev/null || status=$?
    printf "CI_PHASE phase=browser-image seconds=%s exit=%s\n" "$((SECONDS - started))" "$status"
    exit "$status"
BROWSER_PULL
    browser_pull_pid=$!
  else
    printf 'CI_PHASE phase=browser-image seconds=0 exit=0\n'
  fi
fi
if [[ "$suite" != browser ]]; then
  # Exercise actual OSS authentication/counters with a disposable HTTPS identity
  # provider and a deterministic CPU upstream. No production service or GPU is used.
  begin_phase public-entry-images
  python3 - <<'PY'
import subprocess
from search.operations.deployment.public_entry.profile import APISIX_IMAGE, REDIS_IMAGE
from search.operations.deployment.source_bundle import NGINX_BASE
from search.operations.deployment.public_entry.antibot import ANUBIS_IMAGE
for image in (APISIX_IMAGE, REDIS_IMAGE, NGINX_BASE, ANUBIS_IMAGE):
    result = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        subprocess.run(["docker", "pull", image], check=True, timeout=180)
PY
  begin_phase public-entry-runtime
  DEPOSITAR_TEST_PUBLIC_ENTRY_RUNTIME=1 DEPOSITAR_TEST_PRIVATE_API_TLS_RUNTIME=1 DEPOSITAR_TEST_ANTIBOT_RUNTIME=1 DEPOSITAR_TEST_REMOTE_BACKEND_RUNTIME=1 \
    python3 -m unittest tests.integration.public_entry.test_identity_adapter \
      tests.integration.public_entry.test_antibot_runtime \
      tests.integration.public_entry.test_antibot_gateway \
      tests.integration.public_entry.test_runtime \
      tests.integration.public_entry.test_identity_gateway \
      tests.integration.public_entry.test_updates \
      tests.integration.public_entry.test_remote_backend \
      tests.integration.test_private_api_tls_runtime -v
fi
if [[ "$suite" != oss ]]; then
  begin_phase gateway-bootstrap
  bootstrap_image_id="$(docker image inspect --format '{{.Id}}' depositar-search-web-nginx:ci)"
  DEPOSITAR_TEST_GATEWAY_IMAGE="$bootstrap_image_id" \
    python3 -B -m unittest tests.unit.deployment.gateway.test_activation.NativeBootstrapTests -v
  begin_phase browser-fixture
  browser_fixture_started=1
  docker network create --internal "$ci_name" >/dev/null
  common=(--network "$ci_name" --read-only --cap-drop ALL --security-opt no-new-privileges
    --user "$(id -u):$(id -g)" --memory 512m --pids-limit 96 --tmpfs "/tmp:rw,nosuid,nodev,size=32m,mode=1777")
  docker run -d --name "$ci_name-gpu" --network-alias conversation-api "${common[@]}" \
    --mount "type=bind,src=$project_dir/search,dst=/app/search,readonly" \
    depositar-search-api:ci python -m uvicorn search.tests.web_fixture:conversation --factory \
    --host 0.0.0.0 --port 8000 --no-access-log >/dev/null
  docker run -d --name "$ci_name-web" --network-alias web "${common[@]}" \
    --mount "type=bind,src=$project_dir/search,dst=/app/search,readonly" \
    depositar-search-web:ci python -m uvicorn search.tests.web_fixture:website --factory \
    --host 0.0.0.0 --port 8000 --no-access-log >/dev/null
  docker run -d --name "$ci_name-nginx" "${common[@]}" \
    depositar-search-web-nginx:ci >/dev/null
  for attempt in {1..20}; do
    if docker exec "$ci_name-nginx" wget -q -O /dev/null http://127.0.0.1:8080/healthz; then break; fi
    if [[ "$attempt" == 20 ]]; then docker logs "$ci_name-web"; docker logs "$ci_name-nginx"; exit 1; fi
    sleep 1
  done
  begin_phase browser-image-wait
  if [[ -n "$browser_pull_pid" ]]; then
    status=0
    wait "$browser_pull_pid" || status=$?
    browser_pull_pid=''
    if [[ "$status" != 0 ]]; then exit "$status"; fi
  fi
  begin_phase browser
  docker run --rm --network "container:$ci_name-nginx" --ipc=private --shm-size=256m \
    --cap-drop ALL --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
    --cpus 2 --memory 2g --pids-limit 256 -e HOME=/tmp/browser-home \
    --mount "type=bind,src=$project_dir/web,dst=/web" --workdir /web \
    "$browser_image" npm test
fi
case "$suite" in
  all) echo 'Website unit/contract, build, OSS, native bootstrap and real HTTP browser checks passed.' ;;
  oss) echo 'Website unit/contract, build and OSS runtime checks passed.' ;;
  browser) echo 'Website unit/contract, build, native bootstrap and real HTTP browser checks passed.' ;;
esac
