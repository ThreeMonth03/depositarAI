#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/search-lib.sh
source "$project_dir/scripts/search-lib.sh"
cd "$project_dir"

search_project=""
readonly search_env_file="$project_dir/.env.catalog"
readonly dictionary_permissions="$project_dir/scripts/search_dictionary_permissions.py"
readonly model_source_helper="$project_dir/scripts/search_model_source.py"

isolated_project_seen=false
hash_image=""
hash_image_id=""
while (( $# > 0 )); do
  case "$1" in
    --isolated-project)
      [[ $# -ge 2 && "$isolated_project_seen" == false ]] \
        || die "--isolated-project requires one unique catalog project"
      [[ "$2" =~ ^depositar-ai-catalog-[0-9a-f]{12}-[0-9a-f]{12}$ ]] \
        || die "--isolated-project must be a qualified catalog project"
      search_project=$2
      isolated_project_seen=true
      shift 2
      ;;
    --hash-image)
      [[ $# -ge 2 && -z "$hash_image" ]] \
        || die "--hash-image requires one unique revision-qualified image"
      [[ "$2" =~ ^depositar-opensearch-search-hash:[0-9a-f]{40}$ ]] \
        || die "--hash-image must be a reviewed revision-qualified helper image"
      hash_image=$2
      shift 2
      ;;
    --hash-image-id)
      [[ $# -ge 2 && -z "$hash_image_id" ]] \
        || die "--hash-image-id requires one unique immutable image ID"
      [[ "$2" =~ ^sha256:[0-9a-f]{64}$ ]] \
        || die "--hash-image-id must be the immutable ID captured from the sealed build"
      hash_image_id=$2
      shift 2
      ;;
    *) die "usage: $0 --isolated-project depositar-ai-catalog-<bundle12>-<revision12> --hash-image depositar-opensearch-search-hash:<revision40> --hash-image-id sha256:<digest64>" ;;
  esac
done
[[ "$isolated_project_seen" == true ]] \
  || die "--isolated-project is required"
[[ -n "$hash_image" && -n "$hash_image_id" ]] \
  || die "--hash-image and --hash-image-id are required together"
readonly search_project
readonly hash_image
readonly hash_image_id

assert_regular_owned_mode() {
  local path="$1"
  local expected_mode="$2"
  local description="$3"
  local actual_owner actual_mode

  [[ -f "$path" && ! -L "$path" ]] \
    || die "$description must be a regular file, not a symlink or special file: $path"
  actual_owner="$(stat -c '%u' -- "$path")"
  actual_mode="$(stat -c '%a' -- "$path")"
  [[ "$actual_owner" == "$(id -u)" ]] \
    || die "$description has the wrong owner: $path"
  [[ "$actual_mode" == "$expected_mode" ]] \
    || die "$description must have mode $expected_mode (found $actual_mode): $path"
  [[ "$(stat -c '%h' -- "$path")" == 1 ]] \
    || die "$description must have exactly one hard link: $path"
}

assert_directory_owned_mode() {
  local path="$1"
  local expected_mode="$2"
  local description="$3"
  local actual_owner actual_mode

  [[ -d "$path" && ! -L "$path" ]] \
    || die "$description must be a directory, not a symlink: $path"
  actual_owner="$(stat -c '%u' -- "$path")"
  actual_mode="$(stat -c '%a' -- "$path")"
  [[ "$actual_owner" == "$(id -u)" ]] \
    || die "$description has the wrong owner: $path"
  [[ "$actual_mode" == "$expected_mode" ]] \
    || die "$description must have mode $expected_mode (found $actual_mode): $path"
}

assert_no_symlink_components() {
  local candidate="$1"
  local probe="$candidate"
  local parent

  while [[ "$probe" != / ]]; do
    [[ ! -L "$probe" ]] || die "storage paths must not traverse symlinks: $probe"
    parent="$(dirname -- "$probe")"
    [[ "$parent" != "$probe" ]] \
      || die "could not safely walk storage path components: $candidate"
    probe="$parent"
  done
}

require_command python3
require_command timeout
[[ -f "$dictionary_permissions" && ! -L "$dictionary_permissions" ]] \
  || die "$dictionary_permissions is missing or unsafe"
[[ -f "$model_source_helper" && ! -L "$model_source_helper" ]] \
  || die "$model_source_helper is missing or unsafe"
assert_regular_owned_mode "$search_env_file" 600 ".env.catalog"
export COMPOSE_PROJECT_NAME="$search_project"
load_search_env "$project_dir" "$search_env_file"
[[ "$COMPOSE_PROJECT_NAME" == "$search_project" ]] \
  || die "COMPOSE_PROJECT_NAME must remain $search_project"
require_env \
  SEARCH_CATALOG_DATA_ROOT SEARCH_CATALOG_RUNTIME_UID SEARCH_CATALOG_RUNTIME_GID \
  SEARCH_OPENSEARCH_BASE_IMAGE
require_command realpath
[[ "$SEARCH_CATALOG_RUNTIME_UID" =~ ^[1-9][0-9]*$ \
  && "$SEARCH_CATALOG_RUNTIME_UID" == "$(id -u)" ]] \
  || die "SEARCH_CATALOG_RUNTIME_UID must equal the invoking non-root UID ($(id -u))"
[[ "$SEARCH_CATALOG_RUNTIME_GID" =~ ^[1-9][0-9]*$ \
  && "$SEARCH_CATALOG_RUNTIME_GID" == "$(id -g)" ]] \
  || die "SEARCH_CATALOG_RUNTIME_GID must equal the invoking non-root GID ($(id -g))"
[[ "$(id -u)" != 0 ]] \
  || die "run the catalog as its dedicated non-root operator, not root"

raw_search_root="$SEARCH_CATALOG_DATA_ROOT"
[[ "$raw_search_root" == /* ]] || die "SEARCH_CATALOG_DATA_ROOT must be absolute"
assert_no_symlink_components "$raw_search_root"
SEARCH_CATALOG_DATA_ROOT="$(realpath -m -- "$raw_search_root")"
export SEARCH_CATALOG_DATA_ROOT
[[ "$SEARCH_CATALOG_DATA_ROOT" != / ]] \
  || die "SEARCH_CATALOG_DATA_ROOT cannot be /"

canonical_project_dir="$(realpath -e -- "$project_dir")"
if path_is_within "$SEARCH_CATALOG_DATA_ROOT" "$canonical_project_dir" \
  || path_is_within "$canonical_project_dir" "$SEARCH_CATALOG_DATA_ROOT"; then
  die "SEARCH_CATALOG_DATA_ROOT cannot overlap the repository"
fi
if [[ -n "${HOME:-}" ]]; then
  canonical_home="$(realpath -m -- "$HOME")"
  if path_is_within "$SEARCH_CATALOG_DATA_ROOT" "$canonical_home"; then
    [[ "$SEARCH_CATALOG_DATA_ROOT" != "$canonical_home" ]] \
      || die "SEARCH_CATALOG_DATA_ROOT cannot be HOME"
    if [[ -e "$SEARCH_CATALOG_DATA_ROOT" ]]; then
      assert_directory_owned_mode "$SEARCH_CATALOG_DATA_ROOT" 700 "product control state"
    else
      assert_directory_owned_mode "$(dirname -- "$SEARCH_CATALOG_DATA_ROOT")" 700 \
        "product control state parent"
    fi
  fi
fi

if [[ -n "${SEARCH_CATALOG_STORAGE_ALLOWED_ROOT:-}" ]]; then
  [[ "$SEARCH_CATALOG_STORAGE_ALLOWED_ROOT" == /* ]] \
    || die "SEARCH_CATALOG_STORAGE_ALLOWED_ROOT must be absolute"
  [[ -d "$SEARCH_CATALOG_STORAGE_ALLOWED_ROOT" \
    && ! -L "$SEARCH_CATALOG_STORAGE_ALLOWED_ROOT" ]] \
    || die "SEARCH_CATALOG_STORAGE_ALLOWED_ROOT must be an existing non-symlink directory"
  allowed_root="$(realpath -e -- "$SEARCH_CATALOG_STORAGE_ALLOWED_ROOT")"
  [[ "$allowed_root" != / ]] \
    || die "SEARCH_CATALOG_STORAGE_ALLOWED_ROOT cannot be /"
  path_is_within "$SEARCH_CATALOG_DATA_ROOT" "$allowed_root" \
    || die "SEARCH_CATALOG_DATA_ROOT is outside SEARCH_CATALOG_STORAGE_ALLOWED_ROOT"
  [[ "$SEARCH_CATALOG_DATA_ROOT" != "$allowed_root" ]] \
    || die "SEARCH_CATALOG_DATA_ROOT must be below SEARCH_CATALOG_STORAGE_ALLOWED_ROOT"
fi

if [[ -n "${SEARCH_CATALOG_STORAGE_REQUIRED_FS_TYPES:-}" ]]; then
  require_command findmnt
  fs_probe="${allowed_root:-$SEARCH_CATALOG_DATA_ROOT}"
  actual_fs="$(findmnt -n -T "$fs_probe" -o FSTYPE)"
  fs_match=false
  IFS=',' read -r -a required_fs_types \
    <<<"$SEARCH_CATALOG_STORAGE_REQUIRED_FS_TYPES"
  for expected_fs in "${required_fs_types[@]}"; do
    [[ -n "$expected_fs" && "$actual_fs" == "$expected_fs" ]] && fs_match=true
  done
  $fs_match \
    || die "storage filesystem for $fs_probe is $actual_fs; expected $SEARCH_CATALOG_STORAGE_REQUIRED_FS_TYPES"
  fs_options="$(findmnt -n -T "$fs_probe" -o OPTIONS)"
  [[ ",$fs_options," == *,rw,* ]] \
    || die "storage filesystem for $fs_probe is not mounted read-write"
fi

root_existed=false
if [[ -e "$SEARCH_CATALOG_DATA_ROOT" || -L "$SEARCH_CATALOG_DATA_ROOT" ]]; then
  root_existed=true
  assert_directory_owned_mode "$SEARCH_CATALOG_DATA_ROOT" 700 \
    "catalog data root"
fi

old_umask="$(umask)"
umask 077
marker_tmp=""
secret_tmp=""
lock_tmp=""
security_stage=""
model_tmp=""
secret_stage=""
cleanup_bootstrap_temps() {
  rm -f -- \
    "${marker_tmp:-}" "${secret_tmp:-}" "${lock_tmp:-}" \
    "${model_tmp:-}"
  if [[ -n "${security_stage:-}" \
    && "$security_stage" == "$SEARCH_CATALOG_DATA_ROOT/state/.security-config.tmp."* ]]; then
    rm -f -- \
      "$security_stage/opensearch.yml" \
      "$security_stage/root-ca.pem" \
      "$security_stage/root-ca-key.pem" \
      "$security_stage/root-ca.srl" \
      "$security_stage/node.pem" \
      "$security_stage/node-key.pem" \
      "$security_stage/node.csr" \
      "$security_stage/openssl-ca.cnf" \
      "$security_stage/openssl-node.cnf" \
      "$security_stage/internal_users.yml" \
      "$security_stage/roles.yml" \
      "$security_stage/roles_mapping.yml" \
      "$security_stage/security-material.sha256"
    rmdir -- "$security_stage" 2>/dev/null || true
  fi
  if [[ -n "${secret_stage:-}" \
    && "$secret_stage" == "$SEARCH_CATALOG_DATA_ROOT/state/.secret-set.tmp."* ]]; then
    rm -f -- \
      "$secret_stage/opensearch_admin_password" \
      "$secret_stage/fess_opensearch_password" \
      "$secret_stage/fess_app_cipher_key" \
      "$secret_stage/fess_initial_admin_password"
    rmdir -- "$secret_stage" 2>/dev/null || true
  fi
}
trap cleanup_bootstrap_temps EXIT
mkdir -p -- "$SEARCH_CATALOG_DATA_ROOT"
if ! $root_existed; then
  chmod 700 "$SEARCH_CATALOG_DATA_ROOT"
fi
assert_directory_owned_mode "$SEARCH_CATALOG_DATA_ROOT" 700 \
  "catalog data root"

marker="$SEARCH_CATALOG_DATA_ROOT/.depositar-catalog-root"
verify_marker() {
  assert_regular_owned_mode "$marker" 600 "catalog managed-root marker"
  grep -Fqx 'schema=1' "$marker" || die "$marker has an unsupported schema"
  grep -Fqx "project=$search_project" "$marker" \
    || die "$marker belongs to a different project"
  grep -Fqx 'kind=catalog-data' "$marker" \
    || die "$marker has the wrong kind"
}

if [[ -e "$marker" || -L "$marker" ]]; then
  verify_marker
else
  existing_entry="$({
    find "$SEARCH_CATALOG_DATA_ROOT" -mindepth 1 -maxdepth 1 \
      ! -name .snap -print -quit
  })"
  [[ -z "$existing_entry" ]] \
    || die "$SEARCH_CATALOG_DATA_ROOT is non-empty and unmanaged; refusing to adopt it"
  marker_tmp="$(mktemp "$SEARCH_CATALOG_DATA_ROOT/.managed-root.tmp.XXXXXX")"
  {
    printf 'schema=1\n'
    printf 'project=%s\n' "$search_project"
    printf 'kind=catalog-data\n'
  } >"$marker_tmp"
  chmod 600 "$marker_tmp"
  if ! ln -- "$marker_tmp" "$marker"; then
    die "$marker appeared during bootstrap; inspect it and retry"
  fi
  rm -f -- "$marker_tmp"
  marker_tmp=""
  verify_marker
fi

ensure_runtime_directory() {
  local directory="$1"
  local owner mode
  if [[ -e "$directory" || -L "$directory" ]]; then
    [[ -d "$directory" && ! -L "$directory" ]] \
      || die "runtime path must be a non-symlink directory: $directory"
    owner="$(stat -c '%u' -- "$directory")"
    mode="$(stat -c '%a' -- "$directory")"
    [[ "$owner" == "$(id -u)" ]] \
      || die "runtime directory has the wrong owner ($owner): $directory"
    (( (8#$mode & 8#022) == 0 )) \
      || die "runtime directory must not be group/world-writable (mode $mode): $directory"
    return
  fi
  mkdir -- "$directory"
  chmod 700 "$directory"
}

ensure_runtime_directory "$SEARCH_CATALOG_DATA_ROOT/opensearch"
ensure_runtime_directory "$SEARCH_CATALOG_DATA_ROOT/opensearch/data"
ensure_runtime_directory "$SEARCH_CATALOG_DATA_ROOT/opensearch/dictionary"
ensure_runtime_directory "$SEARCH_CATALOG_DATA_ROOT/opensearch/config"
ensure_runtime_directory "$SEARCH_CATALOG_DATA_ROOT/fess"
ensure_runtime_directory "$SEARCH_CATALOG_DATA_ROOT/fess/data"
ensure_runtime_directory "$SEARCH_CATALOG_DATA_ROOT/ollama"
ensure_runtime_directory "$SEARCH_CATALOG_DATA_ROOT/ollama/models"
ensure_runtime_directory "$SEARCH_CATALOG_DATA_ROOT/state"

secret_names=(
  opensearch_admin_password
  fess_opensearch_password
  fess_app_cipher_key
  fess_initial_admin_password
)
secret_dir="$SEARCH_CATALOG_DATA_ROOT/secrets"

lock_file="$SEARCH_CATALOG_DATA_ROOT/state/operations.lock"
if [[ -e "$lock_file" || -L "$lock_file" ]]; then
  assert_regular_owned_mode "$lock_file" 600 "catalog operations lock"
else
  lock_tmp="$(mktemp "$SEARCH_CATALOG_DATA_ROOT/state/.operations.lock.tmp.XXXXXX")"
  chmod 600 "$lock_tmp"
  if ! ln -- "$lock_tmp" "$lock_file"; then
    die "$lock_file appeared during bootstrap; inspect it and retry"
  fi
  rm -f -- "$lock_tmp"
  lock_tmp=""
fi
require_command flock
exec 9<>"$lock_file"
flock -n 9 || die "another catalog deploy/down/bootstrap operation is already running"

# A cold Ceph metadata cache can take tens of seconds even for this bounded
# (4,096-entry / 16-level) walk.  Keep a hard deadline while avoiding a false
# deployment failure on the reviewed shared filesystem.
timeout --signal=TERM --kill-after=5s 60s \
  python3 "$dictionary_permissions" --converge "$SEARCH_CATALOG_DATA_ROOT" \
  || die "could not safely converge the ConfigSync dictionary tree"

assert_recoverable_secret_initialization_state() {
  local existing_catalog_containers existing_index_volumes

  python3 - "$SEARCH_CATALOG_DATA_ROOT" "$(id -u)" <<'PY'
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
operator_uid = int(sys.argv[2])


def fail(message: str) -> None:
    raise SystemExit(f"Error: credential initialization state is not pristine: {message}")


def entry_names(directory: Path) -> set[str]:
    try:
        return {entry.name for entry in directory.iterdir()}
    except OSError as error:
        fail(f"cannot inspect {directory}: {error}")


expected_root_entries = {
    ".depositar-catalog-root",
    "opensearch",
    "fess",
    "ollama",
    "state",
}
actual_root_entries = entry_names(root)
if ".snap" in actual_root_entries:
    snapshot_directory = root / ".snap"
    try:
        snapshot_metadata = snapshot_directory.lstat()
    except OSError as error:
        fail(f"cannot inspect {snapshot_directory}: {error}")
    if not stat.S_ISDIR(snapshot_metadata.st_mode):
        fail(f"the allowlisted snapshot entry is not a directory: {snapshot_directory}")
    actual_root_entries.remove(".snap")
if actual_root_entries != expected_root_entries:
    fail(
        "managed-root entries changed "
        f"(extras={sorted(actual_root_entries - expected_root_entries)!r}, "
        f"missing={sorted(expected_root_entries - actual_root_entries)!r})"
    )

expected_children = {
    "opensearch": {"data", "dictionary", "config"},
    "fess": {"data"},
    "ollama": {"models"},
    "state": {"operations.lock"},
}
for relative, expected in expected_children.items():
    directory = root / relative
    actual = entry_names(directory)
    if actual != expected:
        fail(
            f"{directory} entries changed "
            f"(extras={sorted(actual - expected)!r}, missing={sorted(expected - actual)!r})"
        )

for relative in (
    "opensearch/data",
    "opensearch/dictionary",
    "opensearch/config",
    "fess/data",
    "ollama/models",
):
    directory = root / relative
    if entry_names(directory):
        fail(f"service state is not empty: {directory}")

lock_file = root / "state" / "operations.lock"
try:
    lock_metadata = lock_file.lstat()
except OSError as error:
    fail(f"cannot inspect {lock_file}: {error}")
if (
    not stat.S_ISREG(lock_metadata.st_mode)
    or lock_metadata.st_uid != operator_uid
    or stat.S_IMODE(lock_metadata.st_mode) != 0o600
    or lock_metadata.st_nlink != 1
):
    fail(f"operations lock metadata is unsafe: {lock_file}")
PY

  require_command docker
  if ! existing_catalog_containers="$({
    docker ps --all --quiet \
      --filter "label=com.docker.compose.project=$search_project"
  })"; then
    die "a working Docker daemon is required to prove that credential initialization is safe"
  fi
  [[ -z "$existing_catalog_containers" ]] \
    || die "a catalog container exists; refusing to regenerate credentials"
  if ! existing_index_volumes="$({
    docker volume ls --quiet --filter "name=$search_project-index"
  })"; then
    die "a working Docker daemon is required to prove that credential initialization is safe"
  fi
  [[ -z "$existing_index_volumes" ]] \
    || die "a catalog index volume exists; refusing to regenerate credentials"
}

verify_secret_set() {
  local candidate name value
  local count=0

  assert_directory_owned_mode "$secret_dir" 700 "catalog secrets directory"
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    name="$(basename -- "$candidate")"
    case "$name" in
      opensearch_admin_password|fess_opensearch_password|fess_app_cipher_key|fess_initial_admin_password) ;;
      *) die "unexpected entry in the immutable catalog secret set: $name" ;;
    esac
    assert_regular_owned_mode "$candidate" 600 "$name secret"
    value="$(<"$candidate")"
    if [[ "$name" == fess_app_cipher_key ]]; then
      # Fess passes this value directly to Java's AES implementation.  The
      # textual key must therefore be exactly 16, 24, or 32 bytes; a
      # 64-character hex encoding is 64 bytes and leaves the cipher
      # uninitialised.  Thirty-two hex characters supply 128 bits of random
      # entropy in an AES-accepted 32-byte textual key.
      [[ "$value" =~ ^[0-9a-f]{32}$ ]] \
        || die "$name must contain exactly 32 lowercase hexadecimal characters"
    else
      [[ "$value" =~ ^[0-9a-f]{64}$ ]] \
        || die "$name secret must contain exactly 64 lowercase hexadecimal characters"
    fi
    unset value
    ((count += 1))
  done < <(find "$secret_dir" -mindepth 1 -maxdepth 1 -print)
  [[ "$count" == "${#secret_names[@]}" ]] \
    || die "immutable catalog secret set is incomplete; refusing to regenerate it"
  for name in "${secret_names[@]}"; do
    [[ -f "$secret_dir/$name" && ! -L "$secret_dir/$name" ]] \
      || die "immutable catalog secret is missing; refusing to regenerate it: $name"
  done
  for ((left_index = 0; left_index < ${#secret_names[@]}; left_index++)); do
    for ((right_index = left_index + 1; right_index < ${#secret_names[@]}; right_index++)); do
      if cmp -s \
        "$secret_dir/${secret_names[$left_index]}" \
        "$secret_dir/${secret_names[$right_index]}"; then
        die "catalog secrets must all have distinct values"
      fi
    done
  done
}

if [[ -e "$secret_dir" || -L "$secret_dir" ]]; then
  verify_secret_set
else
  assert_recoverable_secret_initialization_state
  require_command openssl
  secret_stage="$(mktemp -d "$SEARCH_CATALOG_DATA_ROOT/state/.secret-set.tmp.XXXXXX")"
  chmod 700 "$secret_stage"
  for name in "${secret_names[@]}"; do
    if [[ "$name" == fess_app_cipher_key ]]; then
      openssl rand -hex 16 >"$secret_stage/$name"
    else
      openssl rand -hex 32 >"$secret_stage/$name"
    fi
    chmod 600 "$secret_stage/$name"
  done
  for ((left_index = 0; left_index < ${#secret_names[@]}; left_index++)); do
    for ((right_index = left_index + 1; right_index < ${#secret_names[@]}; right_index++)); do
      if cmp -s \
        "$secret_stage/${secret_names[$left_index]}" \
        "$secret_stage/${secret_names[$right_index]}"; then
        die "generated catalog secrets unexpectedly match"
      fi
    done
  done
  mv -T --no-clobber -- "$secret_stage" "$secret_dir"
  [[ ! -e "$secret_stage" && ! -L "$secret_stage" ]] \
    || die "secret directory appeared during atomic initialization; refusing to overwrite it"
  secret_stage=""
  verify_secret_set
fi

readonly approved_hash_image="ghcr.io/codelibs/fess-opensearch:3.8.0@sha256:c59dd34da6d0c6e0117b8851ded0832f168de9b33e275e62cd3ef3139362d7fc"
[[ "$SEARCH_OPENSEARCH_BASE_IMAGE" == "$approved_hash_image" ]] \
  || die "SEARCH_OPENSEARCH_BASE_IMAGE must match the reviewed OpenSearch digest"

security_template_dir="$project_dir/deploy/config/opensearch"
for template_name in \
  opensearch.yml internal_users.yml.in roles.yml roles_mapping.yml; do
  template_path="$security_template_dir/$template_name"
  [[ -f "$template_path" && ! -L "$template_path" ]] \
    || die "required OpenSearch security template is missing or unsafe: $template_path"
done

security_config_dir="$SEARCH_CATALOG_DATA_ROOT/opensearch/config"
security_manifest="$security_config_dir/security-material.sha256"

render_security_manifest() {
  local config_root="$1"
  local file_name digest

  printf 'schema=2\n'
  for file_name in "${secret_names[@]}"; do
    digest="$(sha256sum "$secret_dir/$file_name" | awk '{print $1}')"
    printf '%s_sha256=%s\n' "$file_name" "$digest"
  done
  for file_name in root-ca.pem node.pem node-key.pem internal_users.yml; do
    digest="$(sha256sum "$config_root/$file_name" | awk '{print $1}')"
    printf '%s_sha256=%s\n' "$file_name" "$digest"
  done
}

verify_security_material() {
  local file_name file_path digest
  local node_public_key certificate_public_key
  local manifest_schema expected_manifest actual_manifest
  local -a config_files=(
    opensearch.yml
    root-ca.pem
    node.pem
    node-key.pem
    internal_users.yml
    roles.yml
    roles_mapping.yml
  )

  for file_name in "${config_files[@]}"; do
    file_path="$security_config_dir/$file_name"
    assert_regular_owned_mode "$file_path" 400 "OpenSearch security material"
  done
  assert_regular_owned_mode "$security_manifest" 600 \
    "OpenSearch security-material manifest"

  extra_config_entry="$({
    find "$security_config_dir" -mindepth 1 -maxdepth 1 -printf '%f\n' \
      | while IFS= read -r candidate; do
          case "$candidate" in
            opensearch.yml|root-ca.pem|node.pem|node-key.pem|internal_users.yml|roles.yml|roles_mapping.yml|security-material.sha256) ;;
            *) printf '%s\n' "$candidate"; break ;;
          esac
        done
  })"
  [[ -z "$extra_config_entry" ]] \
    || die "unexpected file in OpenSearch security config: $extra_config_entry"

  cmp -s "$security_template_dir/opensearch.yml" \
    "$security_config_dir/opensearch.yml" \
    || die "rendered opensearch.yml differs from its reviewed template"
  cmp -s "$security_template_dir/roles.yml" "$security_config_dir/roles.yml" \
    || die "rendered roles.yml differs from its reviewed template"
  cmp -s "$security_template_dir/roles_mapping.yml" \
    "$security_config_dir/roles_mapping.yml" \
    || die "rendered roles_mapping.yml differs from its reviewed template"
  ! grep -Fq '__ADMIN_HASH__' "$security_config_dir/internal_users.yml" \
    || die "admin hash placeholder remains in internal_users.yml"
  ! grep -Fq '__FESS_HASH__' "$security_config_dir/internal_users.yml" \
    || die "Fess hash placeholder remains in internal_users.yml"
  python3 - "$security_config_dir/internal_users.yml" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
hashes = re.findall(r'^\s*hash:\s*"(\$2y\$12\$[./A-Za-z0-9]{53})"\s*$', text, re.MULTILINE)
if len(hashes) != 2 or len(set(hashes)) != 2:
    raise SystemExit("Error: internal_users.yml must contain two distinct BCrypt-12 hashes")
PY

  manifest_schema="$(sed -n 's/^schema=//p' "$security_manifest")"
  [[ "$manifest_schema" == 2 ]] \
    || die "security-material manifest has an unsupported schema"
  expected_manifest="$({ render_security_manifest "$security_config_dir"; })"
  actual_manifest="$(<"$security_manifest")"
  [[ "$actual_manifest" == "$expected_manifest" ]] \
    || die "security-material manifest does not exactly bind its credentials and files"

  openssl verify -CAfile "$security_config_dir/root-ca.pem" \
    "$security_config_dir/root-ca.pem" >/dev/null \
    || die "OpenSearch root CA is invalid"
  openssl verify -CAfile "$security_config_dir/root-ca.pem" \
    "$security_config_dir/node.pem" >/dev/null \
    || die "OpenSearch node certificate does not verify against its CA"
  openssl x509 -checkend 2592000 -noout \
    -in "$security_config_dir/root-ca.pem" >/dev/null \
    || die "OpenSearch root CA expires within 30 days"
  openssl x509 -checkend 2592000 -noout \
    -in "$security_config_dir/node.pem" >/dev/null \
    || die "OpenSearch node certificate expires within 30 days"
  openssl x509 -checkhost opensearch-search -noout \
    -in "$security_config_dir/node.pem" >/dev/null \
    || die "OpenSearch node certificate lacks the opensearch-search DNS SAN"
  openssl x509 -checkip 127.0.0.1 -noout \
    -in "$security_config_dir/node.pem" >/dev/null \
    || die "OpenSearch node certificate lacks the loopback IP SAN"
  openssl x509 -text -noout -in "$security_config_dir/root-ca.pem" \
    | grep -Fq 'CA:TRUE' || die "OpenSearch root certificate is not a CA"

  node_public_key="$({
    openssl pkey -in "$security_config_dir/node-key.pem" -pubout 2>/dev/null \
      | sha256sum | awk '{print $1}'
  })"
  certificate_public_key="$({
    openssl x509 -in "$security_config_dir/node.pem" -pubkey -noout \
      | sha256sum | awk '{print $1}'
  })"
  [[ "$node_public_key" == "$certificate_public_key" ]] \
    || die "OpenSearch node certificate and private key do not match"

  for secret_name in "${secret_names[@]}"; do
    if grep -Fq -f "$SEARCH_CATALOG_DATA_ROOT/secrets/$secret_name" -- \
      "$security_config_dir"/*; then
      die "$secret_name plaintext leaked into OpenSearch security material"
    fi
  done
}

existing_security_entry="$({
  find "$security_config_dir" -mindepth 1 -maxdepth 1 -print -quit
})"
if [[ -n "$existing_security_entry" ]]; then
  verify_security_material
else
  require_command docker
  require_command openssl
  docker version >/dev/null 2>&1 || die "a working Docker daemon is required to hash OpenSearch passwords"
  hash_image_metadata="$({
    docker image inspect --format \
      '{"id":{{json .Id}},"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}' \
      "$hash_image" 2>/dev/null
  })" || die "the reviewed password-hash image must already exist locally"
  [[ ${#hash_image_metadata} -le 4096 ]] \
    || die "password-hash image metadata exceeds its limit"
  verified_hash_image_id="$({
    python3 - "$hash_image" "$hash_image_id" "$hash_image_metadata" <<'PY'
import json
import re
import sys


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate image metadata field")
        value[key] = item
    return value


try:
    image, expected_id, raw = sys.argv[1:]
    value = json.loads(raw, object_pairs_hook=unique_object)
    if (
        type(value) is not dict
        or set(value) != {"id", "revision"}
        or value["revision"] != image.rsplit(":", 1)[1]
        or type(value["id"]) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["id"]) is None
        or value["id"] != expected_id
    ):
        raise ValueError("unqualified image")
except (ValueError, TypeError):
    raise SystemExit("Error: password-hash image provenance mismatch") from None
print(value["id"])
PY
  })" || die "password-hash image provenance mismatch"
  readonly verified_hash_image_id

  stale_security_stage="$({
    find "$SEARCH_CATALOG_DATA_ROOT/state" -mindepth 1 -maxdepth 1 \
      -type d -name '.security-config.tmp.*' -print -quit
  })"
  [[ -z "$stale_security_stage" ]] \
    || die "stale security-material staging directory requires inspection: $stale_security_stage"
  security_stage="$(mktemp -d "$SEARCH_CATALOG_DATA_ROOT/state/.security-config.tmp.XXXXXX")"
  chmod 700 "$security_stage"

  generate_bcrypt_hash() {
    local password_path="$1"
    local result
    # OpenSearch's Hasher has no non-interactive stdin option.  Keep the
    # plaintext out of Docker arguments and the host environment by reading it
    # only inside this isolated helper.  Map the helper to the already-checked
    # operator identity instead of granting an unrelated host UID access to its
    # short-lived environment.
    result="$({
      docker run --rm --interactive \
        --pull=never \
        --runtime=runc \
        --env NVIDIA_VISIBLE_DEVICES=void \
        --user "$SEARCH_CATALOG_RUNTIME_UID:$SEARCH_CATALOG_RUNTIME_GID" \
        --hostname hash-helper \
        --network none \
        --read-only \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --pids-limit 128 \
        --memory 768m \
        --cpus 1 \
        --entrypoint /bin/bash \
        "$verified_hash_image_id" \
        -ceu 'IFS= read -r HASH_INPUT; export HASH_INPUT; exec /usr/share/opensearch/plugins/opensearch-security/tools/hash.sh -env HASH_INPUT -a BCrypt -r 12 -min Y' \
        <"$password_path" 2>/dev/null
    })" || die "OpenSearch password hashing failed"
    [[ "$result" =~ ^\$2y\$12\$[./A-Za-z0-9]{53}$ ]] \
      || die "OpenSearch hash helper returned an unexpected value"
    printf '%s' "$result"
  }

  admin_hash="$(generate_bcrypt_hash "$SEARCH_CATALOG_DATA_ROOT/secrets/opensearch_admin_password")"
  fess_hash="$(generate_bcrypt_hash "$SEARCH_CATALOG_DATA_ROOT/secrets/fess_opensearch_password")"
  [[ "$admin_hash" != "$fess_hash" ]] \
    || die "OpenSearch account hashes unexpectedly match"

  install -m 0400 "$security_template_dir/opensearch.yml" \
    "$security_stage/opensearch.yml"
  install -m 0400 "$security_template_dir/roles.yml" \
    "$security_stage/roles.yml"
  install -m 0400 "$security_template_dir/roles_mapping.yml" \
    "$security_stage/roles_mapping.yml"
  python3 - \
    "$security_template_dir/internal_users.yml.in" \
    "$security_stage/internal_users.yml" \
    "$admin_hash" "$fess_hash" <<'PY'
import sys
from pathlib import Path

source, destination, admin_hash, fess_hash = sys.argv[1:]
text = Path(source).read_text(encoding="utf-8")
if text.count("__ADMIN_HASH__") != 1 or text.count("__FESS_HASH__") != 1:
    raise SystemExit("invalid internal_users.yml template placeholders")
text = text.replace("__ADMIN_HASH__", admin_hash).replace("__FESS_HASH__", fess_hash)
Path(destination).write_text(text, encoding="utf-8")
PY
  chmod 400 "$security_stage/internal_users.yml"
  unset admin_hash fess_hash

  {
    printf '[req]\n'
    printf 'prompt = no\n'
    printf 'distinguished_name = dn\n'
    printf 'x509_extensions = v3_ca\n'
    printf '[dn]\n'
    printf 'C = TW\nO = depositar\nOU = Search\nCN = depositar-search-root-ca\n'
    printf '[v3_ca]\n'
    printf 'basicConstraints = critical,CA:TRUE,pathlen:0\n'
    printf 'keyUsage = critical,keyCertSign,cRLSign\n'
    printf 'subjectKeyIdentifier = hash\n'
    printf 'authorityKeyIdentifier = keyid:always\n'
  } >"$security_stage/openssl-ca.cnf"
  {
    printf '[req]\n'
    printf 'prompt = no\n'
    printf 'distinguished_name = dn\n'
    printf 'req_extensions = v3_req\n'
    printf '[dn]\n'
    printf 'C = TW\nO = depositar\nOU = Search\nCN = opensearch-search\n'
    printf '[v3_req]\n'
    printf 'basicConstraints = critical,CA:FALSE\n'
    printf 'keyUsage = critical,digitalSignature,keyEncipherment\n'
    printf 'extendedKeyUsage = serverAuth,clientAuth\n'
    printf 'subjectAltName = @alt_names\n'
    printf '[alt_names]\n'
    printf 'DNS.1 = opensearch-search\n'
    printf 'IP.1 = 127.0.0.1\n'
  } >"$security_stage/openssl-node.cnf"
  chmod 600 "$security_stage/openssl-ca.cnf" "$security_stage/openssl-node.cnf"

  openssl req -new -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
    -config "$security_stage/openssl-ca.cnf" \
    -keyout "$security_stage/root-ca-key.pem" \
    -out "$security_stage/root-ca.pem" >/dev/null 2>&1 \
    || die "could not generate the OpenSearch root CA"
  openssl req -new -newkey rsa:3072 -sha256 -nodes \
    -config "$security_stage/openssl-node.cnf" \
    -keyout "$security_stage/node-key.pem" \
    -out "$security_stage/node.csr" >/dev/null 2>&1 \
    || die "could not generate the OpenSearch node key and CSR"
  openssl x509 -req -sha256 -days 365 \
    -in "$security_stage/node.csr" \
    -CA "$security_stage/root-ca.pem" \
    -CAkey "$security_stage/root-ca-key.pem" \
    -CAcreateserial \
    -extfile "$security_stage/openssl-node.cnf" \
    -extensions v3_req \
    -out "$security_stage/node.pem" >/dev/null 2>&1 \
    || die "could not sign the OpenSearch node certificate"
  rm -f -- \
    "$security_stage/root-ca-key.pem" \
    "$security_stage/root-ca.srl" \
    "$security_stage/node.csr" \
    "$security_stage/openssl-ca.cnf" \
    "$security_stage/openssl-node.cnf"
  chmod 400 \
    "$security_stage/root-ca.pem" \
    "$security_stage/node.pem" \
    "$security_stage/node-key.pem"

  render_security_manifest "$security_stage" \
    >"$security_stage/security-material.sha256"
  chmod 600 "$security_stage/security-material.sha256"

  rmdir -- "$security_config_dir"
  mv -T -- "$security_stage" "$security_config_dir"
  security_stage=""
  verify_security_material
fi

readonly granite_manifest_sha256="1a37926bf842899cbf90583e3932f3820548716d9d07661ba622199ffe95c552"
readonly granite_model_sha256="a658c903bf83a9d84fe1b9869874229ddfdfc1c88b04f958f17dd24e243d262c"
readonly granite_config_sha256="eb0753f1773ea272958bcbd3d5c2a248b6edc665e02274fc720b1dbd58b02153"
readonly granite_license_sha256="492069a62c2545ecde9f8cbc5c3d46adadaa3f95c0e1f68aea7ed2cca548484b"
readonly granite_manifest_relative="manifests/registry.ollama.ai/library/granite-embedding/278m"
model_root="$SEARCH_CATALOG_DATA_ROOT/ollama/models"
ensure_runtime_directory "$model_root/blobs"
ensure_runtime_directory "$model_root/manifests"
ensure_runtime_directory "$model_root/manifests/registry.ollama.ai"
ensure_runtime_directory "$model_root/manifests/registry.ollama.ai/library"
ensure_runtime_directory "$model_root/manifests/registry.ollama.ai/library/granite-embedding"

model_source="${SEARCH_CATALOG_MODEL_SOURCE:-}"
if [[ -n "$model_source" ]]; then
  [[ "$model_source" == /* ]] || die "SEARCH_CATALOG_MODEL_SOURCE must be absolute"
  assert_no_symlink_components "$model_source"
  [[ -d "$model_source" && ! -L "$model_source" ]] \
    || die "SEARCH_CATALOG_MODEL_SOURCE must be an existing non-symlink Ollama models directory"
  model_source="$(realpath -e -- "$model_source")"
  [[ "$model_source" != "$model_root" ]] \
    || die "SEARCH_CATALOG_MODEL_SOURCE must differ from the catalog model destination"
  python3 "$model_source_helper" check --source-root "$model_source"
fi

copy_or_verify_model_file() {
  local relative_path="$1"
  local expected_sha256="$2"
  local expected_size="$3"
  local destination="$model_root/$relative_path"
  local source="${model_source:+$model_source/$relative_path}"
  local destination_directory actual_sha256

  destination_directory="$(dirname -- "$destination")"
  if [[ -e "$destination" || -L "$destination" ]]; then
    assert_regular_owned_mode "$destination" 400 "pinned Granite model file"
    [[ "$(stat -c '%h' -- "$destination")" == 1 ]] \
      || die "pinned Granite model file must have exactly one hard link: $relative_path"
  else
    [[ -n "$source" ]] \
      || die "pinned Granite model is missing; set SEARCH_CATALOG_MODEL_SOURCE to a reviewed Ollama models directory"
    assert_no_symlink_components "$source"
    [[ -f "$source" && ! -L "$source" ]] \
      || die "pinned Granite source file is missing or unsafe: $source"
    model_tmp="$(mktemp "$destination_directory/.granite-model.tmp.XXXXXX")"
    python3 "$model_source_helper" copy \
      --source-root "$model_source" --relative "$relative_path" \
      --destination "$model_tmp" --sha256 "$expected_sha256" --size "$expected_size"
    chmod 400 "$model_tmp"
    actual_sha256="$(sha256sum "$model_tmp" | awk '{print $1}')"
    [[ "$actual_sha256" == "$expected_sha256" ]] \
      || die "copied Granite model digest mismatch: $relative_path"
    if ! ln -- "$model_tmp" "$destination"; then
      die "$destination appeared while seeding the model; inspect it and retry"
    fi
    rm -f -- "$model_tmp"
    model_tmp=""
  fi
  actual_sha256="$(sha256sum "$destination" | awk '{print $1}')"
  [[ "$actual_sha256" == "$expected_sha256" ]] \
    || die "pinned Granite model digest mismatch: $relative_path"
  [[ "$(stat -c '%s' -- "$destination")" == "$expected_size" ]] \
    || die "pinned Granite model size mismatch: $relative_path"
}

copy_or_verify_model_file "$granite_manifest_relative" "$granite_manifest_sha256" 686
copy_or_verify_model_file "blobs/sha256-$granite_model_sha256" "$granite_model_sha256" 562765632
copy_or_verify_model_file "blobs/sha256-$granite_config_sha256" "$granite_config_sha256" 337
copy_or_verify_model_file "blobs/sha256-$granite_license_sha256" "$granite_license_sha256" 11332
python3 - "$model_root/$granite_manifest_relative" \
  "$granite_model_sha256" "$granite_config_sha256" "$granite_license_sha256" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
model_digest, config_digest, license_digest = (f"sha256:{value}" for value in sys.argv[2:])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("schemaVersion") != 2:
    raise SystemExit("Error: unexpected Granite manifest schema")
config = manifest.get("config")
layers = manifest.get("layers")
if not isinstance(config, dict) or config.get("digest") != config_digest:
    raise SystemExit("Error: Granite manifest config digest changed")
if not isinstance(layers, list) or [item.get("digest") for item in layers] != [
    model_digest,
    license_digest,
]:
    raise SystemExit("Error: Granite manifest layer authority changed")
expected_sizes = {config_digest: 337, model_digest: 562765632, license_digest: 11332}
for item in [config, *layers]:
    if item.get("size") != expected_sizes.get(item.get("digest")):
        raise SystemExit("Error: Granite manifest file size authority changed")
PY

python3 - "$model_root" "$(id -u)" \
  "$granite_manifest_relative" \
  "blobs/sha256-$granite_model_sha256" \
  "blobs/sha256-$granite_config_sha256" \
  "blobs/sha256-$granite_license_sha256" <<'PY'
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
operator_uid = int(sys.argv[2])
expected_files = set(sys.argv[3:])
expected_directories = {"."}
for file_name in expected_files:
    parent = Path(file_name).parent
    while str(parent) != ".":
        expected_directories.add(parent.as_posix())
        parent = parent.parent

actual_files: set[str] = set()
actual_directories = {"."}
stack = [root]
while stack:
    directory = stack.pop()
    directory_metadata = directory.lstat()
    if (
        stat.S_ISLNK(directory_metadata.st_mode)
        or not stat.S_ISDIR(directory_metadata.st_mode)
        or directory_metadata.st_uid != operator_uid
        or stat.S_IMODE(directory_metadata.st_mode) != 0o700
    ):
        raise SystemExit(f"Error: unsafe directory in catalog model tree: {directory}")
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise SystemExit(f"Error: symlink is forbidden in catalog model tree: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                actual_directories.add(relative)
                stack.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                if (
                    metadata.st_uid != operator_uid
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                    or metadata.st_nlink != 1
                ):
                    raise SystemExit(f"Error: unsafe file in catalog model tree: {relative}")
                actual_files.add(relative)
            else:
                raise SystemExit(f"Error: special file is forbidden in catalog model tree: {relative}")

if actual_directories != expected_directories or actual_files != expected_files:
    extras = sorted(
        (actual_directories - expected_directories)
        | (actual_files - expected_files)
    )
    missing = sorted(
        (expected_directories - actual_directories)
        | (expected_files - actual_files)
    )
    raise SystemExit(
        f"Error: catalog model tree is not exact; extras={extras!r}, missing={missing!r}"
    )
PY

# Catalog bundles and operator reports are rebuild inputs/evidence rather than
# live database files.  Create them only after the immutable credential and
# model checks have completed, so their presence can never make a missing
# credential set look pristine or eligible for automatic regeneration.
converge_private_catalog_directory() {
  local directory=$1
  ensure_runtime_directory "$directory"
  python3 - "$directory" "$(id -u)" <<'PY'
import os
import stat
import sys

path, uid_text = sys.argv[1:]
uid = int(uid_text)
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != uid:
        raise SystemExit(f"Error: private catalog path is unsafe: {path}")
    os.fchmod(descriptor, 0o700)
    current = os.lstat(path)
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        or current.st_uid != uid
        or stat.S_IMODE(current.st_mode) != 0o700
    ):
        raise SystemExit(f"Error: private catalog path changed during convergence: {path}")
finally:
    os.close(descriptor)
PY
}
converge_private_catalog_directory "$SEARCH_CATALOG_DATA_ROOT/catalog"
converge_private_catalog_directory "$SEARCH_CATALOG_DATA_ROOT/state/catalog-imports"

trap - EXIT
umask "$old_umask"
echo "Catalog storage ready:"
echo "  private control/model root: $SEARCH_CATALOG_DATA_ROOT"
echo "  secrets created or verified without printing their values"
