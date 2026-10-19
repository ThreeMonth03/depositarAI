#!/usr/bin/env bash
# Source-only handoff; this does not install, deploy or back up runtime data.
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 -I -S -B "$project_dir/scripts/ci/source_artifact.py" package "$@"
