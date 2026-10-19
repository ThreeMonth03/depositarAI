#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
command="${1:-help}"
if [[ "$#" != 1 ]]; then
  echo 'Usage: bash scripts/search-web.sh {up|down|status|config}' >&2
  exit 2
fi
case "$command" in
  up|down|status|config) ;;
  *) echo 'Usage: bash scripts/search-web.sh {up|down|status|config}' >&2; exit 2 ;;
esac
config="${SEARCH_WEB_ENV_FILE:-.env.search-web}"
# The dedicated file owns frontend settings, independent of an API checkout.
unset WEB_UID WEB_GID WEB_API_KEY_FILE WEB_ORIGIN WEB_UPSTREAM WEB_UPSTREAM_CA_FILE COMPOSE_PROJECT_NAME
export SOURCE_REVISION
SOURCE_REVISION="$(git rev-parse HEAD)"
if [[ "$command" == up ]] && ! git diff --quiet HEAD -- search web deploy scripts/search-web.sh scripts/validate_search_web.py; then
  echo 'Commit reviewed source changes before starting a versioned website.' >&2; exit 1
fi
compose=(docker compose --project-name depositar-search-web --env-file "$config" -f deploy/compose/compose.search-web.yaml)
has_private_ca="$("${compose[@]}" config --format json | python3 -c '
import json, sys
from pathlib import PurePosixPath
value = json.load(sys.stdin)["services"]["web"]["environment"].get("WEB_UPSTREAM_CA_FILE")
if value and (
    not isinstance(value, str)
    or not PurePosixPath(value).is_absolute()
    or any(c in value for c in "\0\r\n")
):
    raise SystemExit("Private upstream CA must be an absolute file path.")
print("yes" if value else "no")
')"
if [[ "$has_private_ca" == yes ]]; then
  compose+=(-f deploy/compose/compose.search-web.ca.yaml)
fi
if [[ "$command" == up || "$command" == config ]]; then
  "${compose[@]}" config --format json | python3 scripts/validate_search_web.py
fi
case "$command" in
  up)
    # Build committed bytes, never untracked files or a stale shared-FS snapshot.
    for role in web web-nginx; do
      git archive "$SOURCE_REVISION" | docker build -f "deploy/docker/$role/Dockerfile" \
        --build-arg "SOURCE_REVISION=$SOURCE_REVISION" -t "depositar-search-$role:$SOURCE_REVISION" -
    done
    "${compose[@]}" up --no-build -d --wait --wait-timeout 180
    echo 'Private frontend: http://127.0.0.1:18081 (SSH tunnel or approved outer ingress).' ;;
  down) "${compose[@]}" down ;;
  status) "${compose[@]}" ps ;;
  config) "${compose[@]}" config --quiet ;;
esac
