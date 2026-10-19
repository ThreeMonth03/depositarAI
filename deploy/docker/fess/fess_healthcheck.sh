#!/bin/sh
set -eu

[ "$#" -eq 0 ] || exit 2

status="$({
  curl \
    --silent \
    --show-error \
    --connect-timeout 2 \
    --max-time 3 \
    --output /dev/null \
    --write-out '%{http_code}' \
    http://127.0.0.1:8080/api/v2/health
})" || exit 1

# curl treats redirects as success.  Readiness requires the public health API
# itself to answer, not a login redirect or another 2xx/3xx endpoint.
[ "$status" = 200 ]
