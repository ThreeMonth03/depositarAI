"""Closed records for three backend operations and their recovery protocol."""

from __future__ import annotations

import copy
import hashlib
import re

from search.operations.deployment.provider import contract as provider
from search.operations.shared import private_io


MAX_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 2 * MAX_BYTES
MAX_WIRE_BYTES = 8 * MAX_BYTES
MAX_FRAMES = 16
MAX_SECONDS = 1800
BACKEND_COMMAND_CREDITS = 32768
PARTICIPANT_COMMANDS = 128
MAX_EPOCH = 2**63 - 1
KINDS = frozenset(
    {"backend-code-update", "backend-code-rollback", "backend-code-retire"}
)
SIDES = frozenset({"predecessor", "target"})
VERBS = frozenset({"prepare", "observe", "publish", "reconcile", "release"})
OP_ID = re.compile(r"[0-9a-f]{32}\Z")
INTENT_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "operation_id",
        "operator",
        "authority",
        "predecessor_roles",
        "predecessor_roles_sha256",
        "candidate",
        "sides",
        "frontend_owner",
        "limits",
        "retirement",
    }
)
FRONTEND_FIELDS = frozenset(
    {
        "delivery_config",
        "canonical_profile",
        "profile",
        "env",
        "composition_sha256",
        "api_key_mount",
        "ca_mount",
        "state",
        "current",
        "previous",
        "namespace_sha256",
        "monitor_lock_identity",
    }
)
TLS_FIELDS = frozenset(
    {
        "plan",
        "config",
        "backend",
        "material_sha256",
        "files",
        "project",
        "compose_sha256",
    }
)
LIMITS = {
    "total_seconds": MAX_SECONDS,
    "frames": MAX_FRAMES,
    "frame_bytes": MAX_FRAME_BYTES,
    "total_wire_bytes": MAX_WIRE_BYTES,
    "owner_commands": BACKEND_COMMAND_CREDITS,
    "participant_commands": PARTICIPANT_COMMANDS,
}


class TransitionError(ValueError):
    """One fixed diagnostic without paths, response bodies or credentials."""


def require(condition, code="record_invalid"):
    if not condition:
        raise TransitionError("backend_transition_" + code)


def closed(value, fields, code="record_invalid"):
    require(type(value) is dict and set(value) == set(fields), code)


def version(value, fields, kind=None):
    closed(value, fields)
    require(type(value["schema_version"]) is int)
    require(value["schema_version"] == 1)
    if kind is not None:
        require(value["kind"] == kind)


def digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def hexadecimal(value):
    require(type(value) is str and provider.HEX64.fullmatch(value) is not None)


def operation_id(value):
    require(type(value) is str and OP_ID.fullmatch(value) is not None)


def integer(value, minimum=1, maximum=MAX_EPOCH):
    require(type(value) is int and minimum <= value <= maximum)


def choice(value, allowed, code="record_invalid"):
    require(type(value) is str and value in allowed, code)


def reference(value):
    try:
        provider.reference(value)
    except (ValueError, TypeError, KeyError):
        raise TransitionError("backend_transition_reference_invalid") from None


def source(value):
    try:
        provider.source(value)
    except (ValueError, TypeError, KeyError):
        raise TransitionError("backend_transition_source_invalid") from None


def path(value):
    try:
        return provider.path(value)
    except (ValueError, TypeError):
        raise TransitionError("backend_transition_path_invalid") from None


def bounded(value, maximum=MAX_BYTES):
    require(len(private_io.canonical(value)) <= maximum, "record_too_large")


def _authority(value):
    closed(
        value,
        {
            "control_config",
            "control_root",
            "owner_uid",
            "namespace_sha256",
            "backend_peer_sha256",
        },
    )
    reference(value["control_config"])
    path(value["control_root"])
    integer(value["owner_uid"])
    hexadecimal(value["namespace_sha256"])
    hexadecimal(value["backend_peer_sha256"])


def _frontend_owner(value):
    closed(
        value,
        {
            "transport_profile",
            "participant_config",
            "source",
            "entry_sha256",
            "peer_sha256",
        },
    )
    reference(value["transport_profile"])
    reference(value["participant_config"])
    source(value["source"])
    hexadecimal(value["entry_sha256"])
    hexadecimal(value["peer_sha256"])


def _candidate(value, roles):
    closed(
        value,
        {
            "profile",
            "journal_root",
            "record",
            "original_epoch",
            "original_roles_sha256",
            "backend",
        },
    )
    for key in ("profile", "record", "backend"):
        reference(value[key])
    path(value["journal_root"])
    integer(value["original_epoch"])
    require(value["original_epoch"] == roles["epoch"], "candidate_changed")
    require(
        value["original_roles_sha256"] == digest(roles), "candidate_changed"
    )


def _resources(value):
    closed(
        value,
        {
            "cpu_container_ids",
            "exclusive_network_ids",
            "lease_sha256",
            "retain_paths",
        },
    )
    ids = value["cpu_container_ids"]
    closed(ids, provider.CPU_ROLES)
    for identifier in ids.values():
        hexadecimal(identifier)
    require(len(set(ids.values())) == len(ids))
    networks = value["exclusive_network_ids"]
    require(type(networks) is list and len(networks) <= 8)
    for identifier in networks:
        hexadecimal(identifier)
    require(len(set(networks)) == len(networks))
    hexadecimal(value["lease_sha256"])
    require(type(value["retain_paths"]) is list)
    require(len(value["retain_paths"]) <= 64)
    for item in value["retain_paths"]:
        path(item)


def retirement(value):
    closed(value, {"decision", "backend", "dispose", "retain"})
    reference(value["decision"])
    reference(value["backend"])
    _resources(value["dispose"])
    closed(value["retain"], {"paths", "network_ids", "records"})
    require(type(value["retain"]["paths"]) is list)
    require(type(value["retain"]["network_ids"]) is list)
    require(type(value["retain"]["records"]) is list)
    for item in value["retain"]["paths"]:
        path(item)
    for item in value["retain"]["network_ids"]:
        hexadecimal(item)
    for item in value["retain"]["records"]:
        reference(item)


def tls(value):
    closed(value, TLS_FIELDS)
    reference(value["plan"])
    from search.operations.deployment import private_api_tls

    private_api_tls.validate_config(value["config"])
    require(value["config"]["schema_version"] == 2, "binding_invalid")
    private_api_tls._validate_isolated_binding(value["backend"])
    hexadecimal(value["material_sha256"])
    closed(value["files"], {"inner.conf", "outer.conf", "compose.json"})
    for item in value["files"].values():
        hexadecimal(item)
    hexadecimal(value["compose_sha256"])
    require(type(value["project"]) is str)
    require(
        re.fullmatch(
            r"depositar-private-api-tls(?:-[a-z0-9-]{1,40})?", value["project"]
        )
        is not None
    )


def frontend(value):
    closed(value, FRONTEND_FIELDS)
    for key in (
        "delivery_config",
        "canonical_profile",
        "env",
        "api_key_mount",
        "state",
    ):
        reference(value[key])
    if value["ca_mount"] is not None:
        reference(value["ca_mount"])
    from search.operations.deployment.frontend import check

    closed(value["profile"], check.FIELDS)
    require(value["profile"]["schema_version"] == 3)
    require(type(value["profile"]["schema_version"]) is int)
    for key in (
        "composition_sha256",
        "namespace_sha256",
        "monitor_lock_identity",
    ):
        hexadecimal(value[key])
    require(type(value["current"]) is dict)
    require(value["previous"] is None or type(value["previous"]) is dict)


def frontend_actual(value, expected, operation=None):
    """Admit the pinned participant's fresh proof, including JSON types."""
    closed(
        value,
        {
            "canonical_profile_sha256",
            "frontend_state_sha256",
            "env_sha256",
            "composition_sha256",
            "runtime",
            "version",
            "homepage_verified",
            "assets_verified",
            "input_bindings",
            "reservation",
        },
    )
    for actual, reference_key in (
        ("canonical_profile_sha256", "canonical_profile"),
        ("frontend_state_sha256", "state"),
        ("env_sha256", "env"),
    ):
        require(
            value[actual] == expected[reference_key]["sha256"],
            "binding_changed",
        )
    require(
        value["composition_sha256"] == expected["composition_sha256"],
        "binding_changed",
    )
    require(
        private_io.canonical(value["runtime"])
        == private_io.canonical(expected["current"]["runtime"]),
        "runtime_changed",
    )
    require(
        private_io.canonical(value["version"])
        == private_io.canonical(
            {
                "authenticated_version_status": 200,
                "unauthenticated_version_status": 401,
                "backend_identity_verified": True,
                "upstream_tls_verified": True,
            }
        ),
        "version_invalid",
    )
    require(value["homepage_verified"] is True, "homepage_invalid")
    integer(value["assets_verified"], minimum=1, maximum=8)
    require(
        value["input_bindings"]
        == {
            "profile": expected["canonical_profile"],
            "key": expected["api_key_mount"],
            "ca": expected["ca_mount"],
        },
        "binding_changed",
    )
    _participant_reservation(value["reservation"], operation)
    return value


def _participant_reservation(value, operation):
    if value is None:
        return
    require(operation is not None, "reservation_invalid")
    fields = {"op_id", "intent_sha256"}
    if type(value) is dict and "cleanup_decision_sha256" in value:
        fields.add("cleanup_decision_sha256")
        require(
            operation["kind"] == "backend-code-update", "reservation_invalid"
        )
        hexadecimal(value["cleanup_decision_sha256"])
    closed(value, fields)
    require(
        value["op_id"] == operation["operation_id"]
        and value["intent_sha256"] == digest(operation),
        "reservation_invalid",
    )


def binding(value):
    closed(value, {"backend", "tls", "frontend"})
    reference(value["backend"])
    tls(value["tls"])
    frontend(value["frontend"])


def _fixed_bindings(before, after):
    a, b = before["frontend"], after["frontend"]
    fixed = FRONTEND_FIELDS - {
        "canonical_profile",
        "profile",
        "current",
        "state",
    }
    require(all(a[key] == b[key] for key in fixed), "binding_changed")
    for key in ("canonical_profile", "state"):
        require(a[key]["path"] == b[key]["path"], "binding_changed")
    profile = {**a["profile"], "api_revision": b["profile"]["api_revision"]}
    require(profile == b["profile"], "binding_changed")
    require(
        {**a["current"], "profile": b["profile"]} == b["current"],
        "binding_changed",
    )
    old, new = before["tls"], after["tls"]
    for key in ("material_sha256", "project"):
        require(old[key] == new[key], "binding_changed")
    changing = {"api_container_id", "release_compose"}
    require(
        {k: v for k, v in old["config"].items() if k not in changing}
        == {k: v for k, v in new["config"].items() if k not in changing},
        "binding_changed",
    )


def _kind_admission(value):
    roles, kind = value["predecessor_roles"], value["kind"]
    if kind == "backend-code-update":
        require(roles["rollback_backend_sha256"] is None, "capacity")
        _candidate(value["candidate"], roles)
        require(value["retirement"] is None)
    else:
        require(value["candidate"] is None)
        require(roles["rollback_backend_sha256"] is not None, "no_rollback")
        if kind == "backend-code-retire":
            retirement(value["retirement"])
        else:
            require(value["retirement"] is None)


def intent(value):
    version(value, INTENT_FIELDS)
    choice(value["kind"], KINDS, "kind_invalid")
    operation_id(value["operation_id"])
    source(value["operator"])
    _authority(value["authority"])
    _frontend_owner(value["frontend_owner"])
    roles = provider.roles(value["predecessor_roles"])
    integer(roles["epoch"], maximum=MAX_EPOCH - 2)
    require(roles["pending_operation_sha256"] is None, "recovery_required")
    require(digest(roles) == value["predecessor_roles_sha256"], "cas_conflict")
    closed(value["sides"], SIDES)
    for side in SIDES:
        binding(value["sides"][side])
    closed(value["limits"], LIMITS)
    require(
        all(type(item) is int for item in value["limits"].values())
        and value["limits"] == LIMITS,
        "limits_invalid",
    )
    _kind_admission(value)
    _fixed_bindings(value["sides"]["predecessor"], value["sides"]["target"])
    _side_admission(value)
    bounded(value)
    return value


def _side_admission(value):
    roles, sides = value["predecessor_roles"], value["sides"]
    require(
        sides["predecessor"]["backend"]["sha256"]
        == roles["active_backend_sha256"],
        "binding_changed",
    )
    require(
        sides["predecessor"]["tls"]["plan"] == roles["tls_plan_ref"],
        "binding_changed",
    )
    target = sides["target"]["backend"]["sha256"]
    if value["kind"] == "backend-code-update":
        require(target == value["candidate"]["backend"]["sha256"])
        require(target != roles["active_backend_sha256"], "same_binding")
    elif value["kind"] == "backend-code-rollback":
        require(target == roles["rollback_backend_sha256"])
    else:
        require(target == roles["active_backend_sha256"])
        require(sides["target"] == sides["predecessor"], "binding_changed")
        require(
            value["retirement"]["backend"]["sha256"]
            == roles["rollback_backend_sha256"]
        )


def pending_roles(value):
    return {
        **copy.deepcopy(value["predecessor_roles"]),
        "epoch": value["predecessor_roles"]["epoch"] + 1,
        "pending_operation_sha256": digest(value),
    }


def terminal_roles(value, side):
    choice(side, SIDES)
    roles = copy.deepcopy(value["predecessor_roles"])
    roles["epoch"] += 2
    if side == "target":
        roles = _target_roles(value, roles)
    return provider.roles(roles)


def _target_roles(value, roles):
    kind = value["kind"]
    if kind == "backend-code-retire":
        roles["rollback_backend_sha256"] = None
    else:
        roles["rollback_backend_sha256"] = roles["active_backend_sha256"]
        roles["active_backend_sha256"] = value["sides"]["target"]["backend"][
            "sha256"
        ]
        roles["tls_plan_ref"] = copy.deepcopy(
            value["sides"]["target"]["tls"]["plan"]
        )
    return roles


def phase(value, roles):
    if roles == value["predecessor_roles"]:
        return "prepared", None
    if roles == pending_roles(value):
        return "pending", None
    for side in SIDES:
        if roles == terminal_roles(value, side):
            return "terminal", side
    raise TransitionError("backend_transition_cas_conflict")


def resolution(value, operation):
    closed(
        value,
        {
            "schema_version",
            "mode",
            "operation_id",
            "intent_sha256",
            "roles",
            "roles_sha256",
            "side",
            "resolution",
            "observations",
        },
    )
    require(value["schema_version"] == 1)
    require(type(value["schema_version"]) is int)
    require(value["operation_id"] == operation["operation_id"])
    require(value["intent_sha256"] == digest(operation))
    require(value["roles_sha256"] == digest(value["roles"]))
    require(
        type(value["observations"]) is dict and bool(value["observations"])
    )
    _resolution_roles(value, operation)
    bounded(value)
    return value


def _resolution_roles(value, operation):
    if value["mode"] == "cancelled_before_begin":
        require(value["side"] == "predecessor")
        require(value["resolution"] == "no_dispatch")
        require(value["roles"] == operation["predecessor_roles"])
    else:
        require(value["mode"] == "terminal")
        choice(value["side"], SIDES)
        require(value["roles"] == terminal_roles(operation, value["side"]))
        allowed = (
            {"target_verified"}
            if value["side"] == "target"
            else {"no_effect", "verified_return"}
        )
        choice(value["resolution"], allowed)


def discard(value, operation):
    version(
        value,
        {
            "schema_version",
            "kind",
            "operation_id",
            "intent_sha256",
            "expected_pending_epoch",
            "expected_pending_roles_sha256",
            "candidate_snapshot_sha256",
            "candidate_backend_sha256",
            "outcome",
            "resources",
            "owner_source",
        },
        "update-return-discard",
    )
    require(operation["kind"] == "backend-code-update")
    require(value["operation_id"] == operation["operation_id"])
    require(value["intent_sha256"] == digest(operation))
    expected = pending_roles(operation)
    integer(value["expected_pending_epoch"])
    require(value["expected_pending_epoch"] == expected["epoch"])
    require(value["expected_pending_roles_sha256"] == digest(expected))
    require(
        value["candidate_snapshot_sha256"]
        == operation["candidate"]["record"]["sha256"]
    )
    require(
        value["candidate_backend_sha256"]
        == operation["candidate"]["backend"]["sha256"]
    )
    require(value["outcome"] == "predecessor")
    require(value["owner_source"] == operation["operator"])
    _resources(value["resources"])
    bounded(value)
    return value


def wire_request(value):
    closed(
        value, {"v", "op_id", "intent_sha256", "seq", "nonce", "verb", "body"}
    )
    require(type(value["v"]) is int and value["v"] == 1)
    operation_id(value["op_id"])
    if value["intent_sha256"] is not None:
        hexadecimal(value["intent_sha256"])
    integer(value["seq"], maximum=MAX_FRAMES)
    hexadecimal(value["nonce"])
    require(type(value["verb"]) is str and value["verb"] in VERBS)
    require(type(value["body"]) is dict)
    return value


def wire_response(value, request, owner):
    closed(
        value,
        {
            "v",
            "op_id",
            "intent_sha256",
            "seq",
            "nonce",
            "owner_source",
            "owner_namespace_sha256",
            "phase",
            "observation",
        },
    )
    for key in ("v", "op_id", "intent_sha256", "seq", "nonce"):
        require(
            type(value[key]) is type(request[key])
            and value[key] == request[key],
            "response_changed",
        )
    require(value["owner_source"] == owner["source"], "source_changed")
    hexadecimal(value["owner_namespace_sha256"])
    require(
        type(value["phase"]) is str
        and value["phase"]
        in {
            "observed",
            "prepared",
            "published",
            "frozen",
            "released",
            "unknown",
        },
        "phase",
    )
    closed(value["observation"], {"side", "input_projection_sha256", "actual"})
    choice(value["observation"]["side"], {*SIDES, "mixed", "unknown"})
    hexadecimal(value["observation"]["input_projection_sha256"])
    require(type(value["observation"]["actual"]) is dict)
    return value
