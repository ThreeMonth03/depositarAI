#!/usr/bin/env bash

# Minimal shared helpers for the standalone Depositár search product.

die() {
  echo "Error: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

load_search_env() {
  local project_dir="$1"
  local env_file="$2"
  local parser="$project_dir/scripts/read_env.py"
  local parsed

  [[ -f "$env_file" && ! -L "$env_file" ]] \
    || die "$env_file must be a regular file (not a symlink)"
  [[ -f "$parser" && ! -L "$parser" ]] \
    || die "$parser must be a regular file (not a symlink)"
  require_command python3
  if ! parsed="$(python3 "$parser" "$env_file")"; then
    die "could not parse $env_file"
  fi

  # read_env.py validates keys and shell-quotes every value; no content from
  # the environment file is evaluated as code.
  eval "$parsed"
  export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-depositar-search-canary}"
  [[ "$COMPOSE_PROJECT_NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] \
    || die "COMPOSE_PROJECT_NAME must contain only lowercase letters, digits, hyphens, and underscores"
}

require_env() {
  local name
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || die "$name is required in .env.search-canary"
  done
}

path_is_within() {
  local child="${1%/}"
  local parent="${2%/}"
  [[ "$child" == "$parent" || "$child" == "$parent/"* ]]
}
