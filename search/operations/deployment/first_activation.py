"""Read-only barriers before the first private product activation.

The caller retains the product lock. Existing activation or uncertain state
requires an explicit operator workflow; this check never repairs or adopts it.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time

from search.operations import state
from search.operations.deployment import (
    composition,
    credentials,
    runtime_docker,
)
from search.operations.shared import private_io


ERROR_CODES = frozenset(
    {
        "product_private_existing_activation",
        "product_private_recovery_required",
        "product_private_receipt_capacity",
        "product_private_gateway_inventory_unverified",
    }
)
REQUIRED_RECEIPTS = 13
_CATALOG_OPERATIONS = frozenset(
    {
        "sync",
        "prepare-index",
        "build-catalog-images",
        "start-index",
        "import-index",
        "reconcile-index",
        "build-vectors",
    }
)
_STARTUP_ARTIFACTS = frozenset(
    {
        "image-builds",
        "image-candidate.json",
        "secrets",
        credentials.ANCHOR,
        "inference",
        "inference-candidate.json",
        "releases",
        "release-candidate.json",
        "query-candidate.json",
    }
)


class FirstActivationError(ValueError):
    """Fixed diagnostics without state bytes or Docker output."""


def _require_local_state(store):
    try:
        if not isinstance(store, state.StateStore):
            raise ValueError("invalid state store")
        store._check_directory()
        receipts = state.operation_receipts(store)
        names = set(os.listdir(store.fd))
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        RecursionError,
        OverflowError,
    ):
        raise FirstActivationError("product_private_recovery_required") from None
    if any(receipt["status"] in {"started", "failed"} for receipt in receipts):
        raise FirstActivationError("product_private_recovery_required")
    if names & {"active.json", "gateway"} or any(
        receipt["operation"] == "activate-gateway" for receipt in receipts
    ):
        raise FirstActivationError("product_private_existing_activation")
    if names & _STARTUP_ARTIFACTS or any(
        receipt["operation"] not in _CATALOG_OPERATIONS for receipt in receipts
    ):
        raise FirstActivationError("product_private_recovery_required")
    if len(receipts) > state.MAX_OPERATIONS - REQUIRED_RECEIPTS:
        raise FirstActivationError("product_private_receipt_capacity")


def require_first_activation(store, *, docker_runner=None):
    """Require clean local state and an absent gateway before expensive work."""
    _require_local_state(store)
    try:
        with tempfile.TemporaryDirectory(prefix="depositar-first-activation-") as raw:
            client = Path(raw)
            descriptor = private_io.open_directory(client)
            try:

                def verify_client():
                    private_io.verify_directory(descriptor, client)
                    if os.listdir(descriptor):
                        raise ValueError("private Docker client changed")

                docker = runtime_docker.Docker(
                    client,
                    docker_runner,
                    deadline=time.monotonic() + 30,
                    max_commands=2,
                    verify_client=verify_client,
                )
                containers, networks = runtime_docker.project_inventory(
                    docker, composition.GATEWAY_PROJECT
                )
                if containers or networks:
                    raise FirstActivationError("product_private_existing_activation")
                verify_client()
            finally:
                os.close(descriptor)
    except FirstActivationError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        OverflowError,
        subprocess.SubprocessError,
    ):
        raise FirstActivationError(
            "product_private_gateway_inventory_unverified"
        ) from None
    _require_local_state(store)
