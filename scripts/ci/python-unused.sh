#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_dir"

# These stdlib overrides must preserve their caller's keyword signature.
# Each waiver applies to one file only; all other files see the same names.
framework_arguments=(
  "search/catalog/transport.py:fp,message,new_url"
  "search/operations/metadata/source.py:req,fp,code,msg,newurl"
  "search/reranker/server.py:message,explain"
  "search/transport/http.py:message,new_url"
  "tests/test_search_runtime_helpers.py:exc_type,exc,traceback"
)
excluded_files=()
for entry in "${framework_arguments[@]}"; do
  excluded_files+=("$project_dir/${entry%%:*}")
done
excluded_paths="$(IFS=,; printf '%s' "${excluded_files[*]}")"

python3 -m vulture search scripts tests deploy/docker \
  --min-confidence 100 --exclude "$excluded_paths"
for entry in "${framework_arguments[@]}"; do
  python3 -m vulture "${entry%%:*}" \
    --min-confidence 100 --ignore-names "${entry#*:}"
done
echo "Python unused-code checks passed."
