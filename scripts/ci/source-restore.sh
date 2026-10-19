#!/usr/bin/env bash
# Restore only a trusted, digest-checked source bundle into a new private path.
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 -I -S -B "$project_dir/scripts/ci/source_artifact.py" restore "$@"
