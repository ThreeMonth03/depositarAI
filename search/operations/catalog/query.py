"""Prepare query access to one complete private catalog, never activate a site.

The lifecycle caller holds the product lock. This facade owns the catalog lock,
binds transports to the same validated Docker snapshot, and records intent before
any projection/account write. An incomplete run requires operator recovery;
neither passwords nor remote identities are rotated or retried automatically.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid

from search.operations.catalog import (
    access,
    connection,
    execution,
    projection,
    reuse,
    vectors,
)
from search.operations.catalog.policy import RuntimeError
from search.operations.catalog.transport import OpenSearchClient
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
from search.planning import index_filters


MAX_RUNS = 128
MAX_RECORD_BYTES = 32 * 1024
QUERY_FILES = {access.SECRET_NAME, access.BINDING_NAME, "ready.json"}
VECTOR_FIELDS = {
    "schema_version",
    "status",
    "index",
    "bundle_id",
    "manifest_sha256",
    "document_count",
    "inventory_verified",
    "vector_state_verified",
    "vector_dimension",
    "vector_chunk_count",
    "vectors_sha256",
    "inventory_sha256",
}


def _read(root, name):
    return execution.read_record(root, name)


def _write(root, name, value, *, replace=False):
    fd = private_io.open_directory(root)
    try:
        private_io.verify_directory(fd, root)
        private_io.write_json(
            fd, name, value, replace=replace, max_bytes=MAX_RECORD_BYTES
        )
        private_io.verify_directory(fd, root)
    finally:
        os.close(fd)


def _identity(context, gate, scan):
    if (
        scan.get("after_state_sha256") != gate["after_state_sha256"]
        or type(scan.get("done_count")) is not int
        or scan["done_count"] != context["plan"]["document_count"]
        or type(scan.get("pending_count")) is not int
        or scan["pending_count"] != 0
        or scan.get("acl_verified") is not True
    ):
        raise RuntimeError("catalog_query_vector_evidence_changed")
    return {
        **execution.catalog_identity(context),
        "kind": "private-opensearch-query",
        "runtime_sha256": gate["runtime_sha256"],
        "recipe_sha256": gate["recipe_sha256"],
        "after_state_sha256": scan["after_state_sha256"],
        "index": scan["index"],
        "document_count": scan["document_count"],
        "inventory_sha256": scan["inventory_sha256"],
        "vectors_sha256": scan["vectors_sha256"],
        "vector_chunk_count": scan["vector_chunk_count"],
        "projection_schema": index_filters.VERSION,
    }


def _reports(identity, scan, reports, bundle):
    """Validate closed, credential-free reports before interpreting old receipts."""
    if type(reports) is not dict or set(reports) != {"projection", "access"}:
        raise RuntimeError("catalog_query_report_invalid")
    projected, indexed = (reports[key] for key in ("projection", "access"))
    if type(projected) is not dict or type(indexed) is not dict:
        raise RuntimeError("catalog_query_report_invalid")
    updated = projected.get("projection_updated_count")
    expected_projection = {
        **{key: scan[key] for key in VECTOR_FIELDS},
        "projection_schema": index_filters.VERSION,
        "projection_updated_count": updated,
        "projection_verified": True,
    }
    if (
        type(updated) is not int
        or not 0 <= updated <= identity["document_count"]
        or not projection.same_json(projected, expected_projection)
        or type(indexed.get("created")) is not bool
        or not projection.same_json(
            indexed,
            access._proof(
                bundle,
                identity["index"],
                [
                    {"name": name, "http": status, "expected": status, "passed": True}
                    for name, status in (
                        ("search_allowed", 200),
                        ("wrong_key", 401),
                        ("write_denied", 403),
                        ("config_denied", 403),
                        ("security_denied", 403),
                        ("mapping_write_denied", 403),
                    )
                ],
                created=indexed.get("created"),
            ),
        )
    ):
        raise RuntimeError("catalog_query_report_invalid")


def _credentials(root, bundle, index):
    fd = private_io.open_directory(root)
    try:
        query = access._local(
            fd, root, access._binding(bundle, index), require_complete=True
        )
        encoded = {
            name: private_io.read_file(fd, name, mode=0o600, max_bytes=4096)
            for name in (access.SECRET_NAME, access.BINDING_NAME)
        }
        private_io.verify_directory(fd, root)
        return (
            query,
            {name: hashlib.sha256(raw).hexdigest() for name, raw in encoded.items()},
        )
    finally:
        os.close(fd)


def _history(root, identity, scan, bundle, credential_hashes, *, reserve=False):
    runs = [path for path in root.iterdir() if path.name != "identity.json"]
    if (len(runs) > MAX_RUNS or reserve and len(runs) == MAX_RUNS) or any(
        re.fullmatch(r"[0-9a-f]{32}", path.name) is None for path in runs
    ):
        raise RuntimeError("catalog_query_history_invalid_or_full")
    for path in runs:
        fd = private_io.open_directory(path)
        try:
            names = set(os.listdir(fd))
        finally:
            os.close(fd)
        if names != {"started.json", "complete.json"}:
            raise RuntimeError("catalog_query_recovery_required")
        started, completed = _read(path, "started.json"), _read(path, "complete.json")
        if (
            type(started) is not dict
            or set(started) != {"identity", "run_id"}
            or not projection.same_json(started["identity"], identity)
            or started["run_id"] != path.name
            or type(completed) is not dict
            or set(completed) != {"started_sha256", "credential_sha256", "reports"}
            or completed["started_sha256"] != execution.digest_record(started)
            or not projection.same_json(
                completed["credential_sha256"], credential_hashes
            )
        ):
            raise RuntimeError("catalog_query_receipt_changed")
        _reports(identity, scan, completed["reports"], bundle)
    return len(runs)


def _sealed_scan(context, gate, identity):
    """Interpret sealed query identity only against the existing vector gate."""
    if type(identity) is not dict:
        raise RuntimeError("catalog_query_identity_changed")
    index = projection.require_index(identity.get("index"))
    count = context["plan"]["document_count"]
    chunks = identity.get("vector_chunk_count")
    vector_digest = identity.get("vectors_sha256")
    inventory_digest = hashlib.sha256(
        private_io.canonical(context["bundle"].expected_inventory_entries)
    ).hexdigest()
    if (
        type(chunks) is not int
        or not count <= chunks <= count * projection.MAX_CHUNKS_PER_DOCUMENT
        or type(vector_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", vector_digest) is None
        or identity.get("inventory_sha256") != inventory_digest
    ):
        raise RuntimeError("catalog_query_vector_evidence_changed")
    scan = {
        "schema_version": 1,
        "status": "ready",
        "index": index,
        "bundle_id": context["bundle"].bundle_id,
        "manifest_sha256": context["bundle"].manifest_sha256,
        "document_count": count,
        "inventory_verified": True,
        "vector_state_verified": True,
        "vector_dimension": projection.VECTOR_DIMENSION,
        "vector_chunk_count": chunks,
        "vectors_sha256": vector_digest,
        "inventory_sha256": inventory_digest,
        "acl_verified": True,
        "done_count": count,
        "pending_count": 0,
        "after_state_sha256": gate["after_state_sha256"],
    }
    if not projection.same_json(identity, _identity(context, gate, scan)):
        raise RuntimeError("catalog_query_identity_changed")
    return scan


def read_authority(context):
    """Read sealed, complete query authority without HTTP or any state writes.

    The host holds the product/catalog locks and separately verifies actual
    runtime topology, either the strict baseline or a sealed client lease.
    This entry accepts no caller-supplied runtime/query dictionaries as facts.
    Returned credential hashes bind private files; no password is returned.
    """
    if require_clean_revision(context["repo"], runner=context["runner"]) != context.get(
        "source_revision", context["plan"]["revision"]
    ):
        raise RuntimeError("catalog_query_source_changed")
    try:
        gate, runtime, _images = vectors.read_complete_evidence(context)
        root, history = (
            context["root"] / name for name in ("query-access", "query-operations")
        )
        root_fd, history_fd = private_io.open_directory(root), None
        try:
            history_fd = private_io.open_directory(history)
            if set(os.listdir(root_fd)) != QUERY_FILES:
                raise RuntimeError("catalog_query_inventory_changed")
            ready = _read(root, "ready.json")
            if (
                type(ready) is not dict
                or set(ready) != {"identity", "run_id", "complete_sha256"}
                or type(ready["run_id"]) is not str
                or re.fullmatch(r"[0-9a-f]{32}", ready["run_id"]) is None
            ):
                raise RuntimeError("catalog_query_receipt_changed")
            identity = ready["identity"]
            scan = _sealed_scan(context, gate, identity)
            if not projection.same_json(_read(history, "identity.json"), identity):
                raise RuntimeError("catalog_query_identity_changed")
            _index, hashes = _credentials(root, context["bundle"], identity["index"])
            if _history(history, identity, scan, context["bundle"], hashes) < 1:
                raise RuntimeError("catalog_query_receipt_changed")
            complete = _read(history / ready["run_id"], "complete.json")
            if ready["complete_sha256"] != execution.digest_record(complete):
                raise RuntimeError("catalog_query_receipt_changed")
            projected = {
                **complete["reports"]["projection"],
                "projection_updated_count": 0,
            }
            indexed = {**complete["reports"]["access"], "created": False}
            _index, final_hashes = _credentials(
                root, context["bundle"], identity["index"]
            )
            if (
                hashes != final_hashes
                or not projection.same_json(_read(root, "ready.json"), ready)
                or set(os.listdir(root_fd)) != QUERY_FILES
            ):
                raise RuntimeError("catalog_query_receipt_changed")
            private_io.verify_directory(root_fd, root)
            private_io.verify_directory(history_fd, history)
        finally:
            if history_fd is not None:
                os.close(history_fd)
            os.close(root_fd)
    except FileNotFoundError:
        raise RuntimeError("catalog_query_complete_evidence_missing") from None
    if require_clean_revision(context["repo"], runner=context["runner"]) != context.get(
        "source_revision", context["plan"]["revision"]
    ):
        raise RuntimeError("catalog_query_source_changed")
    return {
        "catalog_identity": {
            **(
                {"catalog_revision": identity["revision"]}
                if context.get("source_revision", identity["revision"])
                != identity["revision"]
                else {}
            ),
            "catalog_project": identity["project"],
            "catalog_network_id": runtime["network_id"],
            "core_container_ids": {
                role: runtime["container_ids"][role]
                for role in ("fess-search", "opensearch-search")
            },
            "bundle_id": identity["bundle_id"],
            "manifest_sha256": identity["manifest_sha256"],
            "physical_index": identity["index"],
            "projection_sha256": execution.digest_record(projected),
            "access_sha256": execution.digest_record(indexed),
        },
        "runtime": runtime,
        "runtime_sha256": execution.digest_record(runtime),
        "query_receipt_sha256": execution.digest_record(ready),
        "credential_sha256": hashes,
        "vector_evidence_sha256": execution.digest_record(gate),
    }


def verify_authority(
    context,
    *,
    observe_runtime,
    opensearch_factory=OpenSearchClient,
):
    """Recheck sealed authority and live read-only query behavior under a lock.

    ``observe_runtime(context)`` is a trusted host observer, never user input.
    It must first validate complete unfiltered Docker snapshots under either
    the baseline or sealed client-lease policy and return the catalog snapshot
    ``(proof, containers, network)``. This function does not relax peer policy,
    create operation receipts, create identities, rotate secrets or get tokens.
    Denied OpenSearch write probes must remain denied for a successful result.
    """
    if not callable(observe_runtime):
        raise RuntimeError("catalog_query_runtime_observer_invalid")
    authority = read_authority(context)
    peers = connection.snapshot_peers(
        context, authority["runtime"], observe_runtime(context)
    )
    admin, ca = connection.materials(context)
    client = opensearch_factory(peers["opensearch-search"], ca, admin)
    root = context["root"] / "query-access"
    catalog = authority["catalog_identity"]
    index = catalog["physical_index"]
    scan = projection.verify_vectors(client, context["bundle"], index)
    ready = _read(root, "ready.json")
    if type(ready) is not dict:
        raise RuntimeError("catalog_query_receipt_changed")
    if not projection.same_json(
        scan,
        _sealed_scan(
            context, vectors.read_complete_evidence(context)[0], ready.get("identity")
        ),
    ):
        raise RuntimeError("catalog_query_vector_evidence_changed")
    projected = projection.verify_projection(client, context["bundle"], index)
    indexed = access.verify_access(client, context["bundle"], index, root)
    if (
        execution.digest_record(projected) != catalog["projection_sha256"]
        or execution.digest_record(indexed) != catalog["access_sha256"]
    ):
        raise RuntimeError("catalog_query_permissions_or_projection_changed")
    _index_password, hashes = _credentials(root, context["bundle"], index)
    if hashes != authority["credential_sha256"]:
        raise RuntimeError("catalog_query_secret_changed")
    if (
        execution.digest_record(
            projection.verify_projection(client, context["bundle"], index)
        )
        != catalog["projection_sha256"]
        or execution.digest_record(
            access.verify_access(client, context["bundle"], index, root)
        )
        != catalog["access_sha256"]
    ):
        raise RuntimeError("catalog_query_permissions_or_projection_changed")
    if not projection.same_json(
        authority, read_authority(context)
    ) or peers != connection.snapshot_peers(
        context, authority["runtime"], observe_runtime(context)
    ):
        raise RuntimeError("catalog_query_authority_changed")
    return authority


def provision(
    config,
    plan,
    *,
    repo_root,
    runner=None,
    opensearch_factory=OpenSearchClient,
):
    """Provision/verify query access with no API client attachment or host ports."""
    context = execution.load_context(config, plan, repo_root=repo_root, runner=runner)
    with execution.operation_lock(context):
        return _provision(context, opensearch_factory)


def _provision(context, opensearch_factory):
    gate = vectors.verify_complete(context)
    runtime, images = execution.require_runtime(context)
    before = execution.digest_record((runtime, images))
    if execution.digest_record(runtime) != gate["runtime_sha256"]:
        raise RuntimeError("catalog_query_runtime_changed")
    peers = connection.peers(context, runtime, images)
    admin, ca = connection.materials(context)
    client = opensearch_factory(peers["opensearch-search"], ca, admin)
    scan = projection.verify_vectors(client, context["bundle"])
    identity = _identity(context, gate, scan)
    root, history = (
        context["root"] / name for name in ("query-access", "query-operations")
    )
    for directory in (root, history):
        os.close(private_io.open_directory(directory, create=True))
    try:
        saved = _read(history, "identity.json")
    except FileNotFoundError:
        if list(root.iterdir()) or list(history.iterdir()):
            raise RuntimeError("catalog_query_orphan_evidence")
        _write(history, "identity.json", identity)
    else:
        if not projection.same_json(saved, identity):
            raise RuntimeError("catalog_query_identity_changed")
    try:
        ready = _read(root, "ready.json")
    except FileNotFoundError:
        ready = None
    if ready is None:
        if list(root.iterdir()):
            raise RuntimeError("catalog_query_recovery_required")
        hashes = None
    else:
        if set(path.name for path in root.iterdir()) != QUERY_FILES:
            raise RuntimeError("catalog_query_inventory_changed")
        _query, hashes = _credentials(root, context["bundle"], identity["index"])
    completed_runs = _history(
        history, identity, scan, context["bundle"], hashes, reserve=True
    )
    if ready is not None and (
        type(ready) is not dict
        or set(ready) != {"identity", "run_id", "complete_sha256"}
        or not projection.same_json(ready["identity"], identity)
        or type(ready["run_id"]) is not str
        or re.fullmatch(r"[0-9a-f]{32}", ready["run_id"]) is None
        or completed_runs < 1
        or ready["complete_sha256"]
        != execution.digest_record(_read(history / ready["run_id"], "complete.json"))
    ):
        raise RuntimeError("catalog_query_receipt_changed")
    # Recheck the peer/security proof immediately before publishing write intent.
    if before != execution.digest_record(
        execution.require_runtime(context)
    ) or peers != connection.peers(context, runtime, images):
        raise RuntimeError("catalog_query_runtime_changed")
    run_id = uuid.uuid4().hex
    run_root = history / run_id
    os.close(private_io.open_directory(run_root, create=True))
    started = {"identity": identity, "run_id": run_id}
    _write(run_root, "started.json", started)
    if ready is None:
        projected = projection.provision_projection(client, context["bundle"])
        indexed = access.provision_access(
            client, context["bundle"], identity["index"], root
        )
    else:
        projected = projection.verify_projection(
            client, context["bundle"], identity["index"]
        )
        indexed = access.verify_access(
            client, context["bundle"], identity["index"], root
        )

    reports = {"projection": projected, "access": indexed}
    _reports(identity, scan, reports, context["bundle"])
    _index_password, observed_hashes = _credentials(
        root, context["bundle"], identity["index"]
    )
    if hashes is not None and hashes != observed_hashes:
        raise RuntimeError("catalog_query_secret_changed")
    # A valid vector scan alone also permits absent projection fields. Recheck
    # their strict mapping/content and query privilege boundaries before
    # publishing any completion authority.
    final_projection = projection.verify_projection(
        client, context["bundle"], identity["index"]
    )
    final_access = access.verify_access(
        client, context["bundle"], identity["index"], root
    )
    if not projection.same_json(
        final_projection, {**projected, "projection_updated_count": 0}
    ) or not projection.same_json(final_access, {**indexed, "created": False}):
        raise RuntimeError("catalog_query_permissions_or_projection_changed")
    after_scan = projection.verify_vectors(client, context["bundle"], identity["index"])
    if not projection.same_json(scan, after_scan) or not projection.same_json(
        gate, vectors.verify_complete(context)
    ):
        raise RuntimeError("catalog_query_vector_evidence_changed")
    if (
        before != execution.digest_record(execution.require_runtime(context))
        or peers != connection.peers(context, runtime, images)
        or require_clean_revision(context["repo"], runner=context["runner"])
        != context.get("source_revision", context["plan"]["revision"])
    ):
        raise RuntimeError("catalog_query_runtime_changed")
    if (
        not projection.same_json(_read(run_root, "started.json"), started)
        or not projection.same_json(_read(history, "identity.json"), identity)
        or ready is not None
        and not projection.same_json(_read(root, "ready.json"), ready)
    ):
        raise RuntimeError("catalog_query_receipt_changed")
    if set(path.name for path in root.iterdir()) != (
        QUERY_FILES if ready is not None else QUERY_FILES - {"ready.json"}
    ):
        raise RuntimeError("catalog_query_inventory_changed")
    _final_index, final_hashes = _credentials(
        root, context["bundle"], identity["index"]
    )
    if final_hashes != observed_hashes:
        raise RuntimeError("catalog_query_secret_changed")
    # Privilege probes and credential readback must agree before completion.
    completed = {
        "started_sha256": execution.digest_record(started),
        "credential_sha256": observed_hashes,
        "reports": reports,
    }
    _write(run_root, "complete.json", completed)
    _history(history, identity, scan, context["bundle"], observed_hashes)
    _write(
        root,
        "ready.json",
        {
            "identity": identity,
            "run_id": run_id,
            "complete_sha256": execution.digest_record(completed),
        },
        replace=ready is not None,
    )
    cache = reuse.export(context, client, after_scan, _read(root, "ready.json"))
    return {
        **identity,
        "status": "query-ready",
        "run_id": run_id,
        "query_receipt_sha256": execution.digest_record(_read(root, "ready.json")),
        "permissions_verified": True,
        "runtime_verified": True,
        "host_cleanup_verified": True,
        "website_started": False,
        "vector_cache": cache,
    }
