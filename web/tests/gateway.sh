#!/bin/sh
set -eu
control=${BROWSER_GATEWAY_CONTROL:?Browser gateway control directory is required}
nginx -g 'daemon off;' &
gateway_pid=$!
stop_gateway() {
  if ! kill -TERM "$gateway_pid" 2>/dev/null; then
    wait "$gateway_pid" 2>/dev/null || true
    return 1
  fi
  attempt=0
  while kill -0 "$gateway_pid" 2>/dev/null; do
    if [ "$attempt" -eq 100 ]; then
      kill -KILL "$gateway_pid" 2>/dev/null || true
      wait "$gateway_pid" 2>/dev/null || true
      return 1
    fi
    attempt=$((attempt + 1))
    sleep 0.02
  done
  wait "$gateway_pid" 2>/dev/null
}
cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ]; then
    printf '%s' "$status" > "$control/failed"
  fi
  stop_gateway || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
previous=''
while :; do
  request="$(cat "$control/request" 2>/dev/null || true)"
  if [ -n "$request" ] && [ "$request" != "$previous" ]; then
    # A new master allocates a fresh native rate-limit zone; nginx reloads
    # retain that zone, and would leave the previous test's budget in place.
    stop_gateway
    nginx -g 'daemon off;' &
    gateway_pid=$!
    ready=0
    deadline=$(($(date +%s) + 5))
    while [ "$(date +%s)" -lt "$deadline" ]; do
      kill -0 "$gateway_pid"
      if wget -q -T 1 -O /dev/null http://127.0.0.1:8080/healthz; then
        ready=1
        break
      fi
      sleep 0.05
    done
    [ "$ready" = 1 ]
    printf '%s' "$request" > "$control/ready.tmp"
    mv "$control/ready.tmp" "$control/ready"
    previous=$request
  fi
  kill -0 "$gateway_pid"
  sleep 0.02
done
