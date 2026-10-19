#!/usr/bin/env bash
# CI-only renderer pin. Never replace GPU06's system or user Docker plugins.
set -euo pipefail

readonly compose_version='5.5.0'
readonly compose_asset='docker-compose-linux-x86_64'
# Official release manifest, reviewed when this pin was added:
# https://github.com/docker/compose/releases/download/v5.5.0/checksums.txt
readonly compose_sha256='c57ab918abd5b05ca7e7d0f275875dd1330a695074f309dc9eab1b49efafcd4b'
readonly compose_url="https://github.com/docker/compose/releases/download/v${compose_version}/${compose_asset}"

fail() { echo "Compose CI setup: $*" >&2; exit 1; }

[[ "${GITHUB_ACTIONS:-}" == true ]] || fail 'run this installer only in GitHub Actions'
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || fail 'only Linux amd64 runners are supported'
[[ "${RUNNER_TEMP:-}" == /* && "$RUNNER_TEMP" != / && -d "$RUNNER_TEMP" && ! -L "$RUNNER_TEMP" ]] || fail 'RUNNER_TEMP must be an existing absolute runner directory'
[[ "$RUNNER_TEMP" != *$'\n'* && "$RUNNER_TEMP" != *$'\r'* ]] || fail 'invalid runner directory'
[[ "${GITHUB_ENV:-}" == "$RUNNER_TEMP/"* && -f "$GITHUB_ENV" && -w "$GITHUB_ENV" && ! -L "$GITHUB_ENV" ]] || fail 'GITHUB_ENV must be an existing writable runner file'
for command in curl sha256sum mktemp mkdir chmod docker; do
  command -v "$command" >/dev/null || fail "$command is required"
done

umask 077
compose_config="$(mktemp -d "$RUNNER_TEMP/depositar-compose.XXXXXX")"
mkdir "$compose_config/cli-plugins"
compose_binary="$compose_config/cli-plugins/docker-compose"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --tlsv1.2 --connect-timeout 10 --max-time 120 --retry 3 --retry-max-time 240 \
  --output "$compose_binary" "$compose_url"
if ! printf '%s  %s\n' "$compose_sha256" "$compose_binary" | sha256sum --check --status; then
  fail 'download does not match the reviewed SHA256; refusing to execute it'
fi
chmod 700 "$compose_binary"
[[ "$("$compose_binary" version --short)" == "$compose_version" ]] || fail 'downloaded CLI reports an unexpected version'
[[ "$(DOCKER_CONFIG="$compose_config" docker compose version --short)" == "$compose_version" ]] || fail 'Docker CLI did not select the pinned Compose plugin'

# Each Actions step reads this environment file. Its Docker CLI will prefer the
# pinned plugin under this private config, retaining its system Buildx plugin.
# Registry authentication, if needed later, must be configured in a later step.
printf 'DOCKER_CONFIG=%s\n' "$compose_config" >> "$GITHUB_ENV"
echo "Pinned Docker Compose ${compose_version} in an isolated CI Docker config."
