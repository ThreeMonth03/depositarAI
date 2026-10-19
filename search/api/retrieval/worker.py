"""Bounded catalog operations used after conversation state has been verified."""

from search.catalog.bundle import CatalogBundle


from search.planning.predicates import classify, project, validate_conditions
from ..shared.backend import SubprocessBackend
from ..shared.config import Settings
from ..shared.worker_io import run_worker
from ..worker import run as api_run


class RetrievalBackend(SubprocessBackend):
    operations = SubprocessBackend.operations | {"condition_states"}
    worker_module = "search.api.retrieval.worker"


def run(operation, payload, settings):
    if operation not in RetrievalBackend.operations:
        raise ValueError("unsupported operation")
    if operation == "condition_states":
        if type(payload) is not dict or set(payload) != {"ids", "conditions"}:
            raise ValueError("condition request shape")
        ids, conditions = payload["ids"], payload["conditions"]
        if (
            type(ids) is not list
            or not 1 <= len(ids) <= 20
            or any(type(k) is not str for k in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("condition ID bound")
        validate_conditions(conditions)
        if "bbox" in conditions:
            raise ValueError("spatial filtering is not enabled")
        bundle = CatalogBundle.load(
            settings.bundle_path, expected_manifest_sha256=settings.manifest_sha256
        )
        return {
            "manifest_sha256": bundle.manifest_sha256,
            "states": {
                k: classify(project(bundle.condition_metadata[k]), conditions)
                for k in ids
            },
        }
    return api_run(operation, payload, settings)


def main():
    return run_worker(
        lambda operation, payload: run(operation, payload, Settings.from_environment())
    )


if __name__ == "__main__":
    raise SystemExit(main())
