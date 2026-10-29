"""Run a real Prefect flow around the isolated selected-resource proof."""

import argparse
import json
from pathlib import Path

from prefect import flow, task
from prefect.cache_policies import NO_CACHE
from prefect.context import get_run_context
from prefect.testing.utilities import prefect_test_harness

from experiments.prefect import prepare


def retry_fixture_failure(task, task_run, state):
    del task, task_run
    return isinstance(
        state.result(raise_on_failure=False), prepare.RetryableFixtureError
    )


@task(cache_policy=NO_CACHE, persist_result=False)
def create_fixture(state_root: str) -> dict:
    return prepare.prepare_fixture(state_root)


@task(
    retries=1,
    retry_delay_seconds=0.1,
    retry_condition_fn=retry_fixture_failure,
    cache_policy=NO_CACHE,
    persist_result=False,
)
def profile_resource(fixture: dict) -> dict:
    report = prepare.profile_selected(fixture)
    return {
        "report": report,
        "task_run_id": str(get_run_context().task_run.id),
        "run_count": get_run_context().task_run.run_count,
    }


@task(cache_policy=NO_CACHE, persist_result=False)
def stage_candidate(fixture: dict, profile: dict) -> dict:
    return prepare.prepare_candidate(fixture, profile)


@task(cache_policy=NO_CACHE, persist_result=False)
def verify_preparation(
    fixture: dict, candidate: dict, first: dict, replay: dict
) -> dict:
    if first["run_count"] != 2 or replay["run_count"] != 1:
        raise AssertionError("Prefect did not record the expected retry and replay")
    return {
        **prepare.verify_candidate(
            fixture, candidate, first["report"], replay["report"]
        ),
        "profile_task_run_count": first["run_count"],
        "profile_task_run_id": first["task_run_id"],
    }


@flow(name="selected-resource-preparation-proof", persist_result=False)
def preparation_flow(state_root: str) -> dict:
    fixture = create_fixture(state_root)
    first = profile_resource(fixture)
    replay = profile_resource(fixture)
    candidate = stage_candidate(fixture, replay["report"])
    return verify_preparation(fixture, candidate, first, replay)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-root",
        type=Path,
        required=True,
        help="new private directory outside Git",
    )
    options = parser.parse_args(argv)
    failure = None
    # The harness owns one temporary local API/SQLite database and stops it
    # before returning. A failed flow is inspected after leaving the harness.
    with prefect_test_harness(server_startup_timeout=60):
        state = preparation_flow(str(options.state_root), return_state=True)
        if state.is_completed():
            report = {
                **state.result(),
                "flow_state": state.type.value,
                "flow_run_id": str(state.state_details.flow_run_id),
            }
        else:
            failure = state.result(raise_on_failure=False)
    if failure is not None:
        raise RuntimeError("the isolated preparation flow failed") from failure
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
