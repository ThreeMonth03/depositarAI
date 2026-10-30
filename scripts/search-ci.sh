#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

command -v python3 >/dev/null 2>&1 || {
  echo "Error: python3 is required" >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || {
  echo "Error: Docker with Compose v2 is required" >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "Error: Docker Compose v2 is required" >&2
  exit 1
}

shell_files=(
  scripts/search-ci.sh
  scripts/ci/install-compose.sh
  scripts/ci/runtime-imports.sh
  scripts/ci/python-unused.sh
  scripts/ci/install-gitleaks.sh
  scripts/ci/source-package.sh
  scripts/ci/source-restore.sh
  scripts/bootstrap-catalog.sh
  scripts/search-lib.sh
  deploy/docker/fess/entrypoint.sh
  deploy/docker/fess/fess_healthcheck.sh
  scripts/test-search-api.sh
  scripts/search-web.sh
  scripts/test-search-web.sh
)
bash -n "${shell_files[@]}"
docker run --rm --network none \
  --mount "type=bind,src=$project_dir,dst=/mnt,readonly" \
  koalaman/shellcheck:v0.10.0@sha256:2097951f02e735b613f4a34de20c40f937a6c8f18ecb170612c88c34517221fb \
  -x "${shell_files[@]}"

python3 -m unittest -v \
  search.tests.test_catalog_bundle \
  search.tests.test_retrieval_results \
  search.tests.test_scope \
  search.tests.test_http_transport \
  search.tests.test_admission \
  search.tests.test_serialization \
  search.tests.test_secrets \
  search.tests.test_worker_io \
  search.tests.test_readiness \
  search.tests.test_fess_catalog \
  search.tests.test_token_cleanup \
  search.tests.test_temporal_spatial \
  search.tests.test_index_filters \
  search.tests.test_index \
  search.tests.test_time_context \
  search.tests.test_reranker_service \
  tests.test_prepare_search_models \
  tests.test_catalog_bootstrap \
  tests.test_search_ci_scope \
  tests.unit.deployment.test_ci_compose_install \
  tests.unit.deployment.test_ci_workflows \
  tests.unit.deployment.test_contribution \
  tests.unit.deployment.test_repository_layout \
  tests.unit.deployment.test_runtime_dependencies \
  tests.unit.deployment.test_private_api_tls \
  tests.unit.deployment.frontend.test_check \
  tests.unit.deployment.frontend.test_artifact \
  tests.unit.deployment.frontend.test_delivery \
  tests.unit.deployment.frontend.test_publisher \
  tests.unit.deployment.test_python_package_layout \
  tests.unit.deployment.test_product_module_boundaries \
  tests.unit.deployment.test_runtime_import_gate \
  tests.unit.deployment.test_python_unused_gate \
  tests.unit.operations.test_config \
  tests.unit.operations.test_volume_config \
  tests.unit.operations.test_doctor \
  tests.unit.operations.test_cli \
  tests.unit.operations.test_lifecycle \
  tests.unit.operations.test_recovery_boundaries \
  tests.unit.operations.test_private_up \
  tests.unit.operations.test_activation_outcome \
  tests.unit.operations.test_status \
  tests.unit.operations.test_runtime_docker_diagnostics \
  tests.unit.operations.test_runtime_policy_diagnostics \
  tests.unit.deployment.test_first_activation \
  tests.unit.operations.test_product_restore_cli \
  tests.unit.operations.test_public_entry_cli \
  tests.unit.operations.test_metadata_sync \
  tests.unit.operations.test_metadata_storage \
  tests.unit.operations.test_document_source \
  tests.unit.operations.test_document_cache \
  tests.unit.operations.test_document_extractor \
  tests.unit.operations.test_document_profiles \
  tests.unit.operations.test_document_batches \
  tests.unit.operations.test_document_sets \
  tests.unit.jobs.test_queue \
  tests.unit.jobs.test_sources \
  tests.unit.jobs.test_profiles \
  tests.unit.jobs.test_execution \
  tests.unit.operations.test_resource_enrichment \
  tests.unit.operations.test_private_io \
  tests.unit.quality.test_evaluator \
  tests.unit.quality.test_cli \
  tests.unit.quality.test_batches \
  tests.unit.quality.test_batches_cli \
  tests.unit.operations.test_demo_connection \
  tests.unit.operations.test_bounded_process \
  tests.unit.operations.test_model_source \
  tests.unit.catalog.test_backend \
  tests.unit.catalog.test_commands \
  tests.unit.catalog.test_runtime \
  tests.unit.catalog.test_volumes \
  tests.unit.catalog.test_execution \
  tests.unit.catalog.test_results \
  tests.unit.catalog.test_module_boundaries \
  tests.unit.catalog.test_safe_inputs \
  tests.unit.catalog.test_vectors \
  tests.unit.catalog.test_vector_inputs \
  tests.unit.operations.test_vector_inputs \
  tests.unit.catalog.test_chunks \
  tests.unit.catalog.test_reuse \
  tests.unit.catalog.test_projection \
  tests.unit.catalog.test_access \
  tests.unit.catalog.test_transport \
  tests.unit.catalog.test_query \
  tests.unit.catalog.test_client_lease \
  search.tests.test_vector_pass \
  search.tests.test_vector_journal \
  tests.unit.deployment.test_source_artifact \
  tests.unit.deployment.test_ci_gitleaks_install \
  tests.unit.deployment.test_composition \
  tests.unit.deployment.test_gpu_ownership \
  tests.unit.deployment.test_source_bundle \
  tests.unit.deployment.images.test_build \
  tests.unit.deployment.images.test_staging \
  tests.unit.deployment.images.test_fixtures \
  tests.unit.deployment.test_credentials \
  tests.unit.deployment.test_inference \
  tests.unit.deployment.test_resident_models \
  tests.unit.deployment.candidate.test_policy \
  tests.unit.deployment.candidate.test_probe \
  tests.unit.deployment.candidate.test_catalog \
  tests.unit.deployment.candidate.test_controller \
  tests.unit.deployment.candidate.batch.test_capture \
  tests.unit.deployment.candidate.batch.test_probe \
  tests.unit.deployment.candidate.batch.test_execution \
  tests.unit.deployment.candidate.batch.test_budget \
  tests.unit.deployment.images.test_authority \
  tests.unit.deployment.test_runtime_docker \
  tests.unit.deployment.test_inference_runtime \
  tests.unit.deployment.test_process_identity \
  tests.unit.deployment.release.test_runtime \
  tests.unit.deployment.gateway.test_runtime \
  tests.unit.deployment.gateway.test_preparation \
  tests.unit.deployment.gateway.test_activation \
  tests.unit.deployment.release.test_host \
  tests.unit.deployment.release.test_controller \
  tests.unit.deployment.release.test_probe \
  tests.unit.deployment.gateway.test_probe_script \
  tests.unit.deployment.gateway.test_probe_runtime \
  tests.unit.deployment.test_product_package \
  tests.unit.deployment.test_product_restore \
  tests.unit.deployment.public_entry.test_profile \
  tests.unit.deployment.public_entry.test_identity_adapter \
  tests.unit.deployment.public_entry.test_antibot \
  tests.unit.deployment.public_entry.test_antibot_updates \
  tests.unit.deployment.public_entry.test_host \
  tests.unit.deployment.public_entry.test_preflight \
  tests.unit.deployment.public_entry.test_remote \
  tests.unit.deployment.public_entry.test_remote_lifecycle \
  tests.unit.deployment.public_entry.test_updates \
  tests.unit.deployment.test_packaging \
  tests.unit.operations.test_bounded_http \
  tests.integration.test_product_compose \
  tests.unit.operations.test_runtime_model_policy \
  tests.unit.operations.test_runtime_probe \
  tests.test_search_dictionary_permissions \
  tests.test_search_gitignore_policy \
  tests.test_search_image_hardening \
  tests.test_search_runtime_helpers \
  tests.test_validate_catalog_compose

python3 - <<'PY'
from pathlib import Path

roots = (Path("search"), Path("deploy/docker"))
files = [
    Path("scripts/read_env.py"),
    Path("scripts/search_dictionary_permissions.py"),
    Path("scripts/search_model_source.py"),
    Path("scripts/validate_catalog_compose.py"),
]
for root in roots:
    files.extend(root.rglob("*.py"))
for path in sorted(set(files)):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print("Search Python syntax checks passed.")
PY

normalized_cpu_config="$(mktemp)"
trap 'rm -f -- "$normalized_cpu_config"' EXIT HUP INT TERM
docker compose \
  --env-file deploy/env/catalog.env.example \
  --file deploy/compose/compose.catalog.yaml \
  config --format json >"$normalized_cpu_config"
python3 scripts/validate_catalog_compose.py <"$normalized_cpu_config"

echo "Search-only CI checks passed."
./scripts/test-search-api.sh
