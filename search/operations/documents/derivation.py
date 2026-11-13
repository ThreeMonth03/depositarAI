"""Derive normal profiles from authenticated frozen input, without refresh."""

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Callable, Optional
import uuid

from search.operations.documents import batches, cache, controller, extractor
from search.operations.documents import source
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision


MAX_BYTES = 20 * cache.MAX_FILE_BYTES
MAX_WALL_SECONDS = 300
ELIGIBLE = batches.PROFILE_STATUSES
ERROR_CODES = frozenset(
    {
        "document_derivation_options_invalid",
        "document_derivation_namespace_invalid",
        "document_derivation_state_invalid",
        "document_derivation_identity_changed",
        "document_derivation_operator_changed",
        "document_derivation_source_incomplete",
        "document_derivation_source_missing",
        "document_derivation_source_ineligible",
        "document_derivation_cache_missing",
        "document_derivation_cache_invalid",
        "document_derivation_budget_exhausted",
        "document_derivation_deadline_exceeded",
    }
)


class DerivationError(batches.BatchError):
    """A closed offline refusal without resource text or private paths."""


@dataclass(frozen=True)
class _State:
    root: Path
    descriptor: int
    verify_lock: Callable[[], None]
    plan: dict
    digest: str
    resources: dict
    tool: Optional[dict]


class _Budget:
    def __init__(self, max_bytes, wall_seconds):
        self.remaining = max_bytes
        self.verified = 0
        self.deadline = time.monotonic() + wall_seconds

    def check(self):
        if time.monotonic() >= self.deadline:
            raise DerivationError("document_derivation_deadline_exceeded")

    def body_limit(self):
        self.check()
        if self.remaining == 0:
            raise DerivationError("document_derivation_budget_exhausted")
        return min(self.remaining, cache.MAX_FILE_BYTES)

    def verified_body(self, size):
        self.remaining -= size
        self.verified += size
        self.check()


def _options(max_resources, max_bytes, wall_seconds):
    limits = (
        (max_resources, controller.MAX_RESOURCES),
        (max_bytes, MAX_BYTES),
        (wall_seconds, MAX_WALL_SECONDS),
    )
    if any(
        type(value) is not int or not 1 <= value <= cap
        for value, cap in limits
    ):
        raise DerivationError("document_derivation_options_invalid")


def _paths(source_bundle, source_state_root, bundle, state_root):
    paths = tuple(
        controller._external_root(value)
        for value in (source_bundle, source_state_root, bundle, state_root)
    )
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise DerivationError("document_derivation_namespace_invalid")
            a, b = left.lstat(), right.lstat()
            if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
                raise DerivationError("document_derivation_namespace_invalid")
    return paths


def _operator_root(source_root):
    root = batches._source_root(source_root)
    if root != Path(__file__).resolve().parents[3]:
        raise DerivationError("document_derivation_operator_changed")
    return root


def _state(stack, bundle, root, digest, *, read_only):
    guards = []
    root, descriptor = stack.enter_context(
        batches._state(
            root, create=False, read_only=read_only, on_lock=guards.append
        )
    )
    if len(guards) != 1:
        raise DerivationError("document_derivation_state_invalid")
    guards[0]()
    plan = batches._plan(descriptor, digest)
    selected = controller._selected_resources(
        bundle,
        plan["catalog_manifest_sha256"],
        [row["resource_id"] for row in plan["resources"]],
        max_resources=batches.sets.MAX_SELECTED,
        metadata_only=True,
    )
    artifact = batches._manifest(bundle, plan["catalog_manifest_sha256"])[
        "artifacts"
    ]["source.jsonl.gz"]["sha256"]
    if (
        batches._entries(selected) != plan["resources"]
        or artifact != plan["source_sha256"]
    ):
        raise DerivationError("document_derivation_identity_changed")
    resources = {
        resource["id"]: (dataset, resource) for dataset, resource in selected
    }
    return _State(
        root,
        descriptor,
        guards[0],
        plan,
        digest,
        resources,
        batches._tool(descriptor, plan),
    )


def _policy(origin, target, source_root):
    if (
        origin.plan["catalog_manifest_sha256"]
        == target.plan["catalog_manifest_sha256"]
        or private_io.canonical(origin.plan["policy"])
        != private_io.canonical(target.plan["policy"])
        or origin.plan["extractor_source_sha256"]
        != target.plan["extractor_source_sha256"]
        or extractor.source_identity(source_root)
        != target.plan["extractor_source_sha256"]
    ):
        raise DerivationError("document_derivation_identity_changed")
    if target.tool is not None and target.tool != origin.tool:
        raise DerivationError("document_derivation_identity_changed")


def _children(stack, state, names, handles):
    result = {}
    for name in names:
        descriptor = private_io.open_child_directory(state.descriptor, name)
        stack.callback(os.close, descriptor)
        handles.append((descriptor, state.root / name, None))
        result[name] = descriptor
    return result


def _verify_handles(handles):
    for descriptor, path, lock in handles:
        private_io.verify_directory(descriptor, path)
        if lock is not None:
            lock()


def _candidates(origin, target, rows, outcomes, budget):
    source_entries = {
        row["resource_id"]: row for row in origin.plan["resources"]
    }
    chosen, unavailable = [], {}
    for index, (entry, row) in enumerate(zip(target.plan["resources"], rows)):
        budget.check()
        if row is not None:
            if row["status"] == "running":
                raise DerivationError("document_derivation_source_incomplete")
            continue
        old_entry = source_entries.get(entry["resource_id"])
        old = (
            batches._outcome(outcomes, old_entry, origin.digest)
            if old_entry
            else None
        )
        if old is None:
            code = "document_derivation_source_missing"
        elif old["status"] == "running":
            raise DerivationError("document_derivation_source_incomplete")
        elif old["status"] not in ELIGIBLE:
            code = "document_derivation_source_ineligible"
        else:
            chosen.append((index, old_entry, old))
            continue
        unavailable[code] = unavailable.get(code, 0) + 1
    return chosen, unavailable


def _origin_profile(origin, entry, row, profiles):
    if origin.tool is None:
        raise DerivationError("document_derivation_state_invalid")
    batches._verify_profile(profiles, row, entry, origin.plan, origin.tool)
    profile = batches._read(
        profiles, row["profile_sha256"] + ".json", controller.MAX_RECORD_BYTES
    )
    if controller.digest(profile) != row["profile_sha256"]:
        raise DerivationError("document_derivation_state_invalid")
    return profile


def _resource_identity(origin, target, identifier):
    old_dataset, old = origin.resources[identifier]
    new_dataset, new = target.resources[identifier]
    old_identity, new_identity = (
        source.source_identity(old),
        source.source_identity(new),
    )
    if old_dataset != new_dataset or private_io.canonical(
        old_identity
    ) != private_io.canonical(new_identity):
        raise DerivationError("document_derivation_identity_changed")
    return new_dataset, new, new_identity


def _acquired(acquisition, identity, profile, budget):
    try:
        record = cache.load(
            acquisition,
            cache.key_for(identity),
            identity,
            max_body_bytes=budget.body_limit(),
        )
    except cache.CacheError as error:
        code = (
            "document_derivation_budget_exhausted"
            if str(error) == "document_cache_body_limit"
            else "document_derivation_cache_invalid"
        )
        raise DerivationError(code) from None
    if record is None:
        raise DerivationError("document_derivation_cache_missing")
    original = profile["source"]
    facts = ("bytes", "acquired_at")
    if (
        any(record[key] != original[key] for key in facts)
        or record["sha256"] != original["content_sha256"]
        or record["acquired_at"] > int(time.time())
    ):
        raise DerivationError("document_derivation_identity_changed")
    budget.verified_body(record["bytes"])
    return record, {
        "sha256": record["sha256"],
        "bytes": record["bytes"],
        "acquired_at": record["acquired_at"],
        "source_url_sha256": identity["source_url_sha256"],
    }


def _parsed(origin, resource, acquired, profile, extractions):
    policy = origin.plan["policy"]
    arguments = (
        acquired,
        origin.tool,
        resource["format"].strip().upper(),
        policy["encoding"],
        policy["header_policy"],
    )
    try:
        result, hit = controller._extraction(
            *arguments, extractions, None, cache_only=True
        )
    except controller.ProfileError as error:
        code = (
            "document_derivation_cache_missing"
            if str(error) == "document_extraction_cache_missing"
            else "document_derivation_cache_invalid"
        )
        raise DerivationError(code) from None
    if not hit or private_io.canonical(result) != private_io.canonical(
        profile["result"]
    ):
        raise DerivationError("document_derivation_identity_changed")
    key = controller.digest(controller.extraction_identity(*arguments))
    record = batches._read(
        extractions, key + ".json", controller.MAX_RECORD_BYTES
    )
    expected = {
        "identity": controller.extraction_identity(*arguments),
        "result": result,
    }
    if private_io.canonical(record) != private_io.canonical(expected):
        raise DerivationError("document_derivation_identity_changed")
    return result, key, controller.digest(record)


def _lineage(
    origin, target, row, old_profile, record, parsed, profile, revision
):
    key, extraction_sha = parsed
    return {
        "schema_version": 1,
        "mode": "offline_frozen_input",
        "operator_revision": revision,
        "source_plan_sha256": origin.digest,
        "source_manifest_sha256": origin.plan["catalog_manifest_sha256"],
        "source_outcome_sha256": controller.digest(row),
        "source_profile_sha256": row["profile_sha256"],
        "source_acquisition_sha256": controller.digest(record),
        "source_cache_key_sha256": record["cache_key"],
        "source_extraction_key_sha256": key,
        "source_extraction_sha256": extraction_sha,
        "content_sha256": record["sha256"],
        "original_acquired_at": old_profile["source"]["acquired_at"],
        "extractor": old_profile["extractor"],
        "policy": target.plan["policy"],
        "target_plan_sha256": target.digest,
        "target_manifest_sha256": target.plan["catalog_manifest_sha256"],
        "target_profile_sha256": controller.digest(profile),
        "dataset_id": profile["source"]["dataset_id"],
        "resource_id": profile["source"]["resource_id"],
        "fresh_acquisition": False,
        "parser_executed": False,
    }


def _publish(target, tool, children, handles, profile, lineage, budget):
    budget.check()
    _verify_handles(handles)
    controller._publish(target.descriptor, "tool.json", tool)
    _verify_handles(handles)
    evidence = controller._child(target.descriptor, "derivations")
    evidence_path = target.root / "derivations"
    publication_handles = [*handles, (evidence, evidence_path, None)]
    try:
        private_io.verify_directory(evidence, evidence_path)
        proof = controller.digest(lineage)
        _verify_handles(publication_handles)
        controller._publish(evidence, proof + ".json", lineage)
        private_io.verify_directory(evidence, evidence_path)
        _verify_handles(publication_handles)
        budget.check()
        sha = controller.publish_profile(children["profiles"], profile)
        if sha != lineage["target_profile_sha256"]:
            raise DerivationError("document_derivation_state_invalid")
        _verify_handles(publication_handles)
        budget.check()
        row = {
            "schema_version": 1,
            "plan_sha256": target.digest,
            "dataset_id": profile["source"]["dataset_id"],
            "resource_id": profile["source"]["resource_id"],
            "attempt": 1,
            "status": profile["result"]["status"],
            "helper_name": "depositarai-extract-" + uuid.uuid4().hex,
            "profile_sha256": sha,
            "acquisition_cache_hit": False,
            "extraction_cache_hit": True,
        }
        controller._publish(
            children["outcomes"], row["resource_id"] + ".json", row
        )
        _verify_handles(publication_handles)
        return row, {"profile_sha256": sha, "derivation_sha256": proof}
    finally:
        os.close(evidence)


def _derive_one(
    origin, target, item, children, handles, acquisition, budget, revision
):
    _, entry, row = item
    old = _origin_profile(origin, entry, row, children["source_profiles"])
    dataset, resource, identity = _resource_identity(
        origin, target, entry["resource_id"]
    )
    record, acquired = _acquired(acquisition, identity, old, budget)
    result, key, parsed_sha = _parsed(
        origin, resource, acquired, old, children["source_extractions"]
    )
    policy = target.plan["policy"]
    profile = controller.profile_record(
        dataset,
        resource,
        target.plan["catalog_manifest_sha256"],
        acquired,
        result,
        origin.tool,
        policy["encoding"],
        policy["header_policy"],
    )
    lineage = _lineage(
        origin, target, row, old, record, (key, parsed_sha), profile, revision
    )
    return _publish(
        target, origin.tool, children, handles, profile, lineage, budget
    )


def _execute(stack, origin, target, chosen, rows, budget, revision, handles):
    old = _children(stack, origin, ("profiles", "extractions"), handles)
    new = _children(stack, target, ("profiles", "outcomes"), handles)
    children = dict(
        new,
        source_profiles=old["profiles"],
        source_extractions=old["extractions"],
    )
    guards = []
    try:
        acquisition = stack.enter_context(
            cache.locked(
                origin.root / "acquisition",
                read_only=True,
                on_lock=guards.append,
            )
        )
        if len(guards) != 1:
            raise DerivationError("document_derivation_state_invalid")
        guards[0]()
    except cache.CacheError:
        raise DerivationError("document_derivation_cache_invalid") from None
    handles.append((acquisition, origin.root / "acquisition", guards[0]))
    receipts = []
    for item in chosen:
        budget.check()
        row, proof = _derive_one(
            origin,
            target,
            item,
            children,
            handles,
            acquisition,
            budget,
            revision,
        )
        rows[item[0]] = row
        receipts.append(proof)
    return receipts


def _run(paths, expected, max_resources, budget, source_root):
    revision = require_clean_revision(source_root)
    with ExitStack() as stack:
        origin = _state(stack, paths[0], paths[1], expected[0], read_only=True)
        target = _state(
            stack, paths[2], paths[3], expected[1], read_only=False
        )
        _policy(origin, target, source_root)
        budget.check()
        handles = [
            (
                state.descriptor,
                state.root,
                state.verify_lock,
            )
            for state in (origin, target)
        ]
        old = _children(stack, origin, ("outcomes",), handles)
        rows, _ = batches._snapshot(
            target.root, target.descriptor, target.plan, target.digest
        )
        chosen, unavailable = _candidates(
            origin, target, rows, old["outcomes"], budget
        )
        receipts = []
        if chosen:
            receipts = _execute(
                stack,
                origin,
                target,
                chosen[:max_resources],
                rows,
                budget,
                revision,
                handles,
            )
        _verify_handles(handles)
        budget.check()
        if require_clean_revision(source_root) != revision:
            raise DerivationError("document_derivation_operator_changed")
        budget.check()
        report = batches._report(target.plan, target.digest, rows)
        if not report["pending_count"]:
            _verify_handles(handles)
            report.update(
                batches._export(target.descriptor, target.plan, rows)
            )
        budget.check()
        _verify_handles(handles)
        report.update(
            phase="offline_derivation",
            operator_revision=revision,
            source_plan_sha256=origin.digest,
            derived_count=len(receipts),
            source_candidate_count=len(chosen),
            unverified_candidate_count=len(chosen) - len(receipts),
            unavailable_counts=unavailable,
            verified_body_bytes=budget.verified,
            fresh_acquisitions=0,
            parser_executions=0,
            receipts=receipts,
        )
        return report


def derive(
    source_bundle,
    source_state_root,
    expected_source_plan_sha256,
    bundle,
    state_root,
    expected_plan_sha256,
    *,
    max_resources=20,
    max_bytes=MAX_BYTES,
    wall_seconds=MAX_WALL_SECONDS,
    source_root=None,
):
    """Publish bounded new wrappers from verified frozen acquisition/parser
    facts.

    Source state is read-only. Expired acquisition stays explicitly frozen,
    with its original time; no download, parser, Docker or model fallback is
    available. The deadline is checked between bounded IO operations.
    """
    extractor.operator_ids()
    _options(max_resources, max_bytes, wall_seconds)
    budget = _Budget(max_bytes, wall_seconds)
    try:
        paths = _paths(source_bundle, source_state_root, bundle, state_root)
        return _run(
            paths,
            (expected_source_plan_sha256, expected_plan_sha256),
            max_resources,
            budget,
            _operator_root(source_root),
        )
    except DerivationError:
        raise
    except cache.CacheError:
        raise DerivationError("document_derivation_cache_invalid") from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise DerivationError("document_derivation_state_invalid") from None
