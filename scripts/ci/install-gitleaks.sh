#!/usr/bin/env bash
# CI-only, checksum-pinned open-source scanner; never alter host installations.
set -euo pipefail

readonly gitleaks_version='8.30.1'
readonly gitleaks_asset="gitleaks_${gitleaks_version}_linux_x64.tar.gz"
# Official release checksum, reviewed when this pin was added:
# https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt
readonly gitleaks_sha256='551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'
readonly gitleaks_url="https://github.com/gitleaks/gitleaks/releases/download/v${gitleaks_version}/${gitleaks_asset}"

fail() { echo "Gitleaks CI setup: $*" >&2; exit 1; }

[[ "${GITHUB_ACTIONS:-}" == true ]] || fail 'run this installer only in GitHub Actions'
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || fail 'only Linux amd64 runners are supported'
[[ "${RUNNER_TEMP:-}" == /* && "$RUNNER_TEMP" != / && -d "$RUNNER_TEMP" && ! -L "$RUNNER_TEMP" ]] || fail 'RUNNER_TEMP must be an existing absolute runner directory'
[[ "$RUNNER_TEMP" != *$'\n'* && "$RUNNER_TEMP" != *$'\r'* ]] || fail 'invalid runner directory'
[[ "${GITHUB_ENV:-}" == "$RUNNER_TEMP/"* && -f "$GITHUB_ENV" && -w "$GITHUB_ENV" && ! -L "$GITHUB_ENV" ]] || fail 'GITHUB_ENV must be an existing writable runner file'
for command in curl sha256sum mktemp chmod tar timeout python3; do
  command -v "$command" >/dev/null || fail "$command is required"
done

umask 077
scanner_directory="$(mktemp -d "$RUNNER_TEMP/depositar-gitleaks.XXXXXX")"
scanner_archive="$scanner_directory/$gitleaks_asset"
scanner_binary="$scanner_directory/gitleaks"
scanner_config="$scanner_directory/default.toml"
scanner_ignore="$scanner_directory/empty.gitleaksignore"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --tlsv1.2 --connect-timeout 10 --max-time 120 --retry 3 --retry-max-time 240 \
  --output "$scanner_archive" "$gitleaks_url"
if ! printf '%s  %s\n' "$gitleaks_sha256" "$scanner_archive" | sha256sum --check --status; then
  fail 'download does not match the reviewed SHA256; refusing to extract or execute it'
fi
tar --extract --gzip --file "$scanner_archive" --directory "$scanner_directory" \
  --no-same-owner --no-same-permissions gitleaks
[[ -f "$scanner_binary" && ! -L "$scanner_binary" ]] || fail 'release has no regular scanner binary'
chmod 700 "$scanner_binary"
[[ "$(timeout --signal=TERM --kill-after=5 20 "$scanner_binary" version)" == "$gitleaks_version" ]] || fail 'downloaded CLI reports an unexpected version'

# Do not inherit a caller's permissive scanner configuration or ignore file.
unset GITLEAKS_CONFIG GITLEAKS_CONFIG_TOML
printf '[extend]\nuseDefault = true\n' > "$scanner_config"
: > "$scanner_ignore"
canary_suffix="$(python3 -I -S -c 'import secrets,string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36)))')"
printf 'token = "ghp_%s"\n' "$canary_suffix" > "$scanner_directory/positive.txt"
printf 'This is a clean public source fixture.\n' > "$scanner_directory/negative.txt"
scanner_options=(--config "$scanner_config" --gitleaks-ignore-path "$scanner_ignore"
  --ignore-gitleaks-allow --redact=100 --no-banner --log-level error --timeout 20)
positive_status=0
timeout --signal=TERM --kill-after=5 30 "$scanner_binary" dir "${scanner_options[@]}" \
  "$scanner_directory/positive.txt" >/dev/null 2>&1 || positive_status=$?
[[ "$positive_status" == 1 ]] || fail 'synthetic positive control was not detected'
if ! timeout --signal=TERM --kill-after=5 30 "$scanner_binary" dir "${scanner_options[@]}" \
  "$scanner_directory/negative.txt" >/dev/null 2>&1; then
  fail 'clean negative control was rejected'
fi

printf 'GITLEAKS_BINARY=%s\n' "$scanner_binary" >> "$GITHUB_ENV"
echo "Pinned Gitleaks ${gitleaks_version}; synthetic positive and negative controls passed."
