#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly fess_home=/usr/share/fess
readonly override_dir=/opt/fess

die() {
  printf 'Fess catalog bootstrap refused: %s\n' "$1" >&2
  exit 1
}

validate_secret_file() {
  local path=$1 value
  [[ -f "$path" && ! -L "$path" && -r "$path" ]] || die "required secret is not a readable regular file"
  [[ $(wc -c <"$path") -le 129 ]] || die "secret is unexpectedly long"
  IFS= read -r value <"$path" || [[ -n "$value" ]] || die "secret is empty"
  case "${path##*/}" in
    fess_app_cipher_key)
      [[ "$value" =~ ^[0-9a-f]{32}$ ]] \
        || die "AES application cipher key must be exactly 32 ASCII bytes"
      ;;
    *)
      [[ "$value" =~ ^[0-9a-f]{64}$ ]] \
        || die "secret does not match the generated format"
      ;;
  esac
  unset value
}

set_secret_property() {
  local file=$1 key=$2 secret_path=$3 temporary
  temporary="${file}.new"
  awk -v wanted="$key" -v secret_path="$secret_path" '
    BEGIN {
      if ((getline replacement < secret_path) <= 0) exit 70
      close(secret_path)
      found = 0
    }
    {
      line = $0
      sub(/^[[:space:]]+/, "", line)
      if (index(line, wanted "=") == 1 || index(line, wanted " =") == 1) {
        if (!found) print wanted "=" replacement
        found = 1
        next
      }
      print
    }
    END { if (!found) print wanted "=" replacement }
  ' "$file" >"$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$file"
}

set_property() {
  local file=$1 key=$2 value=$3 temporary
  temporary="${file}.new"
  awk -v wanted="$key" -v replacement="$value" '
    BEGIN { found = 0 }
    {
      line = $0
      sub(/^[[:space:]]+/, "", line)
      if (index(line, wanted "=") == 1 || index(line, wanted " =") == 1) {
        if (!found) print wanted "=" replacement
        found = 1
        next
      }
      print
    }
    END { if (!found) print wanted "=" replacement }
  ' "$file" >"$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$file"
}

# Feed the service credential to curl through stdin.  It therefore never
# appears in the container environment, the process argv, or normal curl error
# output.  --disable is intentionally curl's first option so no user curlrc can
# alter the reviewed TLS/request policy.
opensearch_head_status() {
  local endpoint=$1 password curl_status
  IFS= read -r password </run/secrets/fess_opensearch_password \
    || die "could not read the OpenSearch service credential"
  if builtin printf 'user = "fess:%s"\n' "$password" | curl --disable --config - \
    --globoff --silent --show-error --noproxy '*' \
    --proto '=https' --tlsv1.2 --cacert /run/opensearch-ca/root-ca.pem \
    --connect-timeout 5 --max-time 15 --max-filesize 65536 \
    --head --output /dev/null --write-out '%{http_code}' "$endpoint"; then
    curl_status=0
  else
    curl_status=$?
  fi
  unset password
  return "$curl_status"
}

opensearch_post_json_status() {
  local endpoint=$1 request_file=$2 response_file=$3 password curl_status http_status
  IFS= read -r password </run/secrets/fess_opensearch_password \
    || die "could not read the OpenSearch service credential"
  if http_status=$(builtin printf 'user = "fess:%s"\n' "$password" | curl --disable --config - \
    --globoff --silent --show-error --noproxy '*' \
    --proto '=https' --tlsv1.2 --cacert /run/opensearch-ca/root-ca.pem \
    --connect-timeout 5 --max-time 30 --max-filesize 65536 \
    --request POST --header 'Content-Type: application/json' \
    --data-binary "@$request_file" --output "$response_file" \
    --write-out '%{http_code}' "$endpoint"); then
    curl_status=0
  else
    curl_status=$?
  fi
  unset password
  ((curl_status == 0)) || return "$curl_status"
  [[ $http_status =~ ^[0-9]{3}$ ]] || return 90
  printf '%s' "$http_status"
}

opensearch_post_json() {
  local http_status
  http_status=$(opensearch_post_json_status "$@") || return $?
  [[ $http_status == 200 ]]
}

disable_existing_scheduled_jobs() {
  local index_url="${SEARCH_ENGINE_HTTP_URL}/fess_config.scheduled_job"
  local attempt index_status=000 now ready=false ready_status=000 response
  local ready_response_regex='^\{"count":[0-9]+\}$'
  local response_regex
  local scheduler_deadline=$((SECONDS + 300))
  local total update_complete=false update_status=000 updated
  local ready_request="$override_dir/check-scheduled-jobs-ready.json"
  local ready_response="$override_dir/check-scheduled-jobs-ready.response"
  local update_request="$override_dir/disable-scheduled-jobs.json"
  local update_response="$override_dir/disable-scheduled-jobs.response"
  local verify_request="$override_dir/verify-scheduled-jobs.json"
  local verify_response="$override_dir/verify-scheduled-jobs.response"

  # The OpenSearch container healthcheck proves that TLS and authentication are
  # active.  This bounded loop additionally waits until the authenticated Fess
  # identity can resolve the scheduler index as either present or absent.
  for attempt in {1..12}; do
    ((SECONDS < scheduler_deadline)) || break
    if index_status=$(opensearch_head_status "$index_url"); then
      case "$index_status" in
        200)
          break
          ;;
        404)
          printf '%s\n' 'Fess scheduler guard: disabled seed will initialize the new scheduler index.'
          return 0
          ;;
        401 | 403)
          die "OpenSearch rejected the Fess service identity"
          ;;
        429 | 502 | 503 | 504)
          ;;
        *)
          die "unexpected OpenSearch scheduler-index HTTP status"
          ;;
      esac
    else
      index_status=000
    fi
    if ((attempt < 12 && SECONDS < scheduler_deadline)); then
      sleep 5
    fi
  done
  [[ $index_status == 200 ]] || die "OpenSearch scheduler index did not become ready"

  # A HEAD 200 only proves that the index metadata exists.  During a restart,
  # the Security plugin can already be healthy while this index's primary
  # shard is still recovering.  Exercise an actual read before mutating it so
  # a transient "missing shards" 503 cannot make a safe restart fail.
  printf '%s\n' '{"query":{"match_all":{}}}' >"$ready_request"
  for attempt in {1..24}; do
    ((SECONDS < scheduler_deadline)) || break
    if ready_status=$(opensearch_post_json_status \
      "${index_url}/_count?filter_path=count" \
      "$ready_request" "$ready_response"); then
      case "$ready_status" in
        200)
          [[ $(wc -c <"$ready_response") -le 128 ]] \
            || die "OpenSearch scheduler readiness response was unexpectedly large"
          response=$(LC_ALL=C tr -d '[:space:]' <"$ready_response")
          [[ $response =~ $ready_response_regex ]] \
            || die "OpenSearch returned an unexpected scheduler readiness response"
          ready=true
          break
          ;;
        401 | 403)
          die "OpenSearch rejected the scheduler readiness probe"
          ;;
        429 | 502 | 503 | 504)
          ;;
        *)
          die "unexpected OpenSearch scheduler-readiness HTTP status"
          ;;
      esac
    else
      ready_status=000
    fi
    rm -f "$ready_response"
    if ((attempt < 24 && SECONDS < scheduler_deadline)); then
      sleep 5
    fi
  done
  [[ $ready == true ]] \
    || die "OpenSearch scheduler primary shard did not become readable"
  rm -f "$ready_request" "$ready_response"

  now=$(( $(date +%s) * 1000 ))
  printf '%s\n' \
    "{\"script\":{\"lang\":\"painless\",\"source\":\"ctx._source.available = false; ctx._source.cronExpression = params.cron; ctx._source.updatedBy = params.actor; ctx._source.updatedTime = params.now\",\"params\":{\"cron\":\"\",\"actor\":\"depositar-catalog-bootstrap\",\"now\":${now}}},\"query\":{\"match_all\":{}}}" \
    >"$update_request"

  # The readiness read and this mutation are separate requests. Retry only
  # transport failures and explicitly transient HTTP statuses to close that
  # small recovery-time race. The script is idempotent and the final count
  # below still proves every document is disabled before Java may start.
  for attempt in {1..12}; do
    ((SECONDS < scheduler_deadline)) || break
    if update_status=$(opensearch_post_json_status \
      "${index_url}/_update_by_query?conflicts=abort&refresh=true&wait_for_completion=true&filter_path=timed_out,total,updated,version_conflicts,failures" \
      "$update_request" "$update_response"); then
      case "$update_status" in
        200)
          update_complete=true
          break
          ;;
        401 | 403)
          die "OpenSearch rejected the scheduler safety update"
          ;;
        429 | 502 | 503 | 504)
          ;;
        *)
          die "unexpected OpenSearch scheduler-update HTTP status"
          ;;
      esac
    else
      update_status=000
    fi
    rm -f "$update_response"
    if ((attempt < 12 && SECONDS < scheduler_deadline)); then
      sleep 5
    fi
  done
  [[ $update_complete == true ]] \
    || die "failed to disable the existing Fess scheduled jobs"

  [[ $(wc -c <"$update_response") -le 4096 ]] \
    || die "OpenSearch scheduler update response was unexpectedly large"
  response=$(LC_ALL=C tr -d '[:space:]' <"$update_response")
  response_regex='^\{"timed_out":false,"total":([0-9]+),"updated":([0-9]+),"version_conflicts":0,"failures":\[\]\}$'
  [[ $response =~ $response_regex ]] \
    || die "OpenSearch did not return the expected scheduler update result"
  total=${BASH_REMATCH[1]}
  updated=${BASH_REMATCH[2]}
  [[ $updated == "$total" ]] \
    || die "not every existing Fess scheduled job was disabled"

  # Count any document that remains enabled, has a non-empty cron expression,
  # or is missing either fail-closed field.  Fess 15.8 maps both fields as
  # boolean/keyword, so this query is independent of source-field ordering.
  printf '%s\n' \
    '{"query":{"bool":{"minimum_should_match":1,"should":[{"term":{"available":true}},{"wildcard":{"cronExpression":{"value":"?*"}}},{"bool":{"must_not":[{"exists":{"field":"available"}}]}},{"bool":{"must_not":[{"exists":{"field":"cronExpression"}}]}}]}}}' \
    >"$verify_request"
  opensearch_post_json \
    "${index_url}/_count?filter_path=count" \
    "$verify_request" "$verify_response" \
    || die "failed to verify the Fess scheduled-job policy"
  [[ $(wc -c <"$verify_response") -le 128 ]] \
    || die "OpenSearch scheduler verification response was unexpectedly large"
  response=$(LC_ALL=C tr -d '[:space:]' <"$verify_response")
  [[ $response == '{"count":0}' ]] \
    || die "an enabled or scheduled Fess job remains after bootstrap"

  rm -f "$update_request" "$update_response" "$verify_request" "$verify_response"
  printf 'Fess scheduler guard: disabled and verified %s existing jobs.\n' "$total"
}

[[ ${SEARCH_ENGINE_HTTP_URL:-} == https://opensearch-search:9200 ]] || die "OpenSearch URL must use the reviewed HTTPS endpoint"
[[ ${FESS_DICTIONARY_PATH:-} == /usr/share/opensearch/config/dictionary/ ]] \
  || die "Fess dictionary path must match the reviewed OpenSearch ConfigSync path"
[[ ${FESS_CRAWLER_ENABLED:-} == false ]] || die "crawler policy marker is not false"
[[ ${FESS_SCHEDULER_ENABLED:-} == false ]] || die "scheduler policy marker is not false"
[[ ${FESS_RAG_CHAT_ENABLED:-} == false ]] || die "RAG chat policy marker is not false"
[[ ${FESS_LOGIN_REQUIRED:-} == true ]] || die "login-required policy marker is not true"
[[ ${FESS_SEMANTIC_MODEL:-} == granite-embedding:278m ]] || die "unexpected embedding model"
[[ ${TZ:-} == UTC ]] || die "Fess process timezone must remain UTC"
[[ -z ${FESS_PLUGINS:-} ]] || die "runtime plugin download is forbidden"

# Fess 15.8 reads this switch from a Java system property, not from the
# similarly named fess_config.properties namespace. Reject duplicate or
# conflicting values so a later JVM argument cannot silently turn login off.
login_property_count=0
timezone_property_count=0
read -r -a fess_java_options <<<"${FESS_JAVA_OPTS:-}"
for java_option in "${fess_java_options[@]}"; do
  case "$java_option" in
    -Dfess.system.login.required=true)
      ((login_property_count += 1))
      ;;
    -Dfess.system.login.required=*)
      die "conflicting Fess login-required Java property"
      ;;
    -Duser.timezone=UTC)
      ((timezone_property_count += 1))
      ;;
    -Duser.timezone=*)
      die "conflicting Fess JVM timezone property"
      ;;
  esac
done
[[ "$login_property_count" == 1 ]] \
  || die "Fess login-required Java property must appear exactly once"
[[ "$timezone_property_count" == 1 ]] \
  || die "Fess JVM timezone property must appear exactly once"
unset fess_java_options java_option login_property_count timezone_property_count

validate_secret_file /run/secrets/fess_opensearch_password
validate_secret_file /run/secrets/fess_app_cipher_key
validate_secret_file /run/secrets/fess_initial_admin_password
[[ -r /run/opensearch-ca/root-ca.pem && ! -L /run/opensearch-ca/root-ca.pem ]] || die "OpenSearch CA is unavailable"

install -m 0600 /etc/fess/fess_config.properties "$override_dir/fess_config.properties"
install -m 0600 /etc/fess/tomcat_config.properties "$override_dir/tomcat_config.properties"

set_property "$override_dir/fess_config.properties" search_engine.type opensearch
set_property "$override_dir/fess_config.properties" search_engine.http.url "$SEARCH_ENGINE_HTTP_URL"
set_property "$override_dir/fess_config.properties" search_engine.http.ssl.certificate_authorities /run/opensearch-ca/root-ca.pem
set_property "$override_dir/fess_config.properties" search_engine.username fess
set_secret_property "$override_dir/fess_config.properties" search_engine.password /run/secrets/fess_opensearch_password
set_secret_property "$override_dir/fess_config.properties" app.cipher.key /run/secrets/fess_app_cipher_key
set_secret_property "$override_dir/fess_config.properties" index.user.initial_password /run/secrets/fess_initial_admin_password
set_property "$override_dir/fess_config.properties" job.max.crawler.processes 1
set_property "$override_dir/fess_config.properties" crawler.http.thread_pool.size 1
set_property "$override_dir/fess_config.properties" thumbnail.crawler.enabled false
set_property "$override_dir/fess_config.properties" rag.chat.enabled false
set_property "$override_dir/fess_config.properties" api.access.token.required true
set_property "$override_dir/fess_config.properties" rate.limit.enabled true
set_property "$override_dir/fess_config.properties" rate.limit.requests.per.window 60
set_property "$override_dir/fess_config.properties" rate.limit.window.ms 60000
set_property "$override_dir/fess_config.properties" rate.limit.block.duration.ms 300000
set_property "$override_dir/fess_config.properties" web.load.control 85
set_property "$override_dir/fess_config.properties" api.load.control 85
# A dedicated permission namespace scopes both the lexical and semantic search
# branches to this importer.  RoleQueryHelper loads this default before serving
# requests, so authenticated lab users receive exactly the catalog read role;
# unrelated documents cannot affect rank fusion even if they later coexist.
set_property "$override_dir/fess_config.properties" role.search.default.permissions '{role}depositar_catalog'
set_property "$override_dir/fess_config.properties" virtual.host.headers 'X-Depositar-Catalog:catalog=depositar_catalog'
# SearchList is an operator-only reconciliation API.  Its source-field list is
# configured separately from the public v2 response allowlist; include only
# the sealed bundle and per-document markers required to detect partial,
# foreign, or content-tampered imports.
set_property "$override_dir/fess_config.properties" query.additional.response.fields depositar_bundle_id,depositar_document_sha256,content_chunk_status
set_property "$override_dir/fess_config.properties" paging.page.size 25
set_property "$override_dir/fess_config.properties" query.additional.sort.fields url
# The operator-only SearchList inventory must reconcile every bounded catalog
# document (the product cap is 10,000). End-user access remains unpublished;
# the shipped query client separately caps top_k at 20.
set_property "$override_dir/fess_config.properties" query.max.search.result.offset 10000
set_property "$override_dir/fess_config.properties" index.number_of_shards 1
set_property "$override_dir/fess_config.properties" index.auto_expand_replicas 0-0
# Only the internal Docker network can reach this authenticated listener.
# The private API uses a dedicated query-only user; no host port is published.
set_property "$override_dir/tomcat_config.properties" tomcat.bindAddress 0.0.0.0

export JAVA_HOME=/opt/java/openjdk
export FESS_CONF_PATH=/etc/fess
export FESS_OVERRIDE_CONF_PATH="$override_dir"
export FESS_TEMP_PATH=/var/tmp/fess
export FESS_LOG_PATH=/var/log/fess
export FESS_VAR_PATH=/var/lib/fess
export FESS_PORT=8080
export FESS_DICTIONARY_PATH
export SEARCH_ENGINE_HOME=/usr/share/opensearch

touch /var/log/fess/fess.log /var/log/fess/audit.log /var/log/fess/fess-llm.log

# Do not start Java unless both fresh-index and existing-index scheduler paths
# are fail-closed.  The fresh path uses the image-baked disabled bulk seed; the
# existing path is updated and independently queried above.
disable_existing_scheduled_jobs

fess_pid=
tail_pid=
# The commands below are reached by the signal traps registered immediately
# after the function definition; ShellCheck cannot infer that call edge.
# shellcheck disable=SC2317
terminate() {
  [[ -z $fess_pid ]] || kill -TERM "$fess_pid" 2>/dev/null || true
  [[ -z $tail_pid ]] || kill -TERM "$tail_pid" 2>/dev/null || true
}
trap terminate TERM INT HUP

"$fess_home/bin/fess" &
fess_pid=$!
tail -qF /var/log/fess/fess.log /var/log/fess/audit.log /var/log/fess/fess-llm.log 2>/dev/null &
tail_pid=$!

set +e
wait "$fess_pid"
status=$?
set -e
kill -TERM "$tail_pid" 2>/dev/null || true
wait "$tail_pid" 2>/dev/null || true
exit "$status"
