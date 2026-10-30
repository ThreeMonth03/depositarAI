"""Compare the fixed request policy of two independently verified source roots.

The host binds both roots to committed source maps and immutable API images
before calling this pure, offline check. This is a compatibility gate for
reviewed candidates, not an interpreter, provenance proof or network ACL.
Prompt text, schemas and search flow may change without changing this policy.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from search.operations.shared import private_io


SOURCE_FILES = (
    "search/model_policy.py",
    "search/scope.py",
    "search/planning/model.py",
    "search/api/worker.py",
    "search/api/conversation/worker.py",
    "search/api/shared/model_transport.py",
)
MODEL_FIELDS = (
    "SCOPE_MODEL",
    "SCOPE_MODEL_DIGEST",
    "SCOPE_NUM_CTX",
    "SCOPE_NUM_PREDICT",
    "PLANNER_MODEL",
    "PLANNER_MODEL_DIGEST",
)


class SharedRequestPolicyError(ValueError):
    """Closed diagnostics without source text, prompts or host paths."""


def _require(condition):
    if not condition:
        raise SharedRequestPolicyError("shared_request_policy_unsupported")


def _one(values):
    values = list(values)
    _require(len(values) == 1)
    return values[0]


def _named(body, kind, name):
    return _one(node for node in body if isinstance(node, kind) and node.name == name)


def _assignment(body, name):
    return _one(
        node.value
        for node in body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
    )


def _fields(node):
    _require(isinstance(node, ast.Dict))
    names = [ast.literal_eval(key) for key in node.keys]
    _require(all(type(key) is str for key in names) and len(set(names)) == len(names))
    return dict(zip(names, node.values))


def _value(node, constants, attributes=None):
    # Only the existing fixed constant references and self attributes resolve.
    # Calls, expressions and arbitrary candidate code are never evaluated.
    if isinstance(node, ast.Name):
        _require(node.id in constants)
        return constants[node.id]
    if isinstance(node, ast.Attribute):
        _require(
            attributes is not None
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in attributes
        )
        return attributes[node.attr]
    return ast.literal_eval(node)


def _request(node, constants, attributes=None):
    fields = _fields(node)
    _require(
        set(fields)
        in (
            {"model", "messages", "stream", "think", "format", "options", "keep_alive"},
            {
                "model",
                "messages",
                "stream",
                "think",
                "truncate",
                "shift",
                "format",
                "options",
                "keep_alive",
            },
        )
    )
    values = {
        key: _value(value, constants, attributes)
        for key, value in fields.items()
        if key not in {"messages", "format", "options"}
    }
    options = fields["options"]
    values["options"] = (
        {
            key: _value(value, constants, attributes)
            for key, value in _fields(options).items()
        }
        if isinstance(options, ast.Dict)
        else _value(options, constants)
    )
    return values


def _transport(call, role):
    _require(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "ModelTransport"
        and len(call.args) == 1
        and ast.literal_eval(call.args[0]) == role
        and len(call.keywords) == 1
        and call.keywords[0].arg == "keep_alive"
    )
    value = ast.literal_eval(call.keywords[0].value)
    _require(type(value) is int and value == -1)
    return value


def _projection(root):
    root = Path(root)
    trees = {}
    for relative in SOURCE_FILES:
        path = root / relative
        _require(not path.is_symlink())
        with path.open("rb") as stream:
            raw = stream.read(512 * 1024 + 1)
        _require(len(raw) <= 512 * 1024)
        trees[relative] = ast.parse(raw)
    policy = trees[SOURCE_FILES[0]]
    constants = {
        name: ast.literal_eval(_assignment(policy.body, name)) for name in MODEL_FIELDS
    }
    classifier = _named(
        trees[SOURCE_FILES[1]].body, ast.ClassDef, "OllamaScopeClassifier"
    )
    attributes = {
        name: _value(_assignment(classifier.body, name), constants)
        for name in ("model", "model_digest", "num_ctx", "num_predict", "thinking")
    }
    complete = _named(classifier.body, ast.FunctionDef, "_complete")
    scope = _request(_assignment(complete.body, "payload"), constants, attributes)
    scope["model_digest"] = attributes["model_digest"]
    _require(
        scope["stream"] is False
        and scope["think"] is False
        and scope["truncate"] is False
        and scope["shift"] is False
    )

    planner_module = trees[SOURCE_FILES[2]]
    constants["OPTIONS"] = ast.literal_eval(_assignment(planner_module.body, "OPTIONS"))
    planner_class = _named(planner_module.body, ast.ClassDef, "LocalPlanner")
    generate = _named(planner_class.body, ast.FunctionDef, "generate")
    constants["think"] = (
        "low"  # Resolved only after checking the production call below.
    )
    planner = _request(_assignment(generate.body, "body"), constants)
    planner["model_digest"] = constants["PLANNER_MODEL_DIGEST"]
    _require(planner["stream"] is False)

    scope_calls = (
        node
        for node in ast.walk(trees[SOURCE_FILES[3]])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ModelTransport"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "scope"
    )
    scope["production_keep_alive"] = _transport(_one(scope_calls), "scope")
    planner_calls = (
        node
        for node in ast.walk(trees[SOURCE_FILES[4]])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "generate"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "LocalPlanner"
    )
    call = _one(planner_calls)
    _require(
        len(call.func.value.args) == 1
        and not call.func.value.keywords
        and len(call.keywords) == 1
        and call.keywords[0].arg == "think"
        and ast.literal_eval(call.keywords[0].value) == "low"
    )
    planner["production_keep_alive"] = _transport(call.func.value.args[0], "answer")
    transport = _named(trees[SOURCE_FILES[5]].body, ast.ClassDef, "ModelTransport")
    methods = [
        _named(transport.body, ast.FunctionDef, name) for name in ("__init__", "send")
    ]
    transport_sha256 = hashlib.sha256(
        private_io.canonical(
            [ast.dump(node, include_attributes=False) for node in methods]
        )
    ).hexdigest()
    return {"scope": scope, "planner": planner, "transport_sha256": transport_sha256}


def validate_shared_request_policy(baseline_source, candidate_source):
    """Compare reviewed fixed model requests without importing either source.

    Baseline is the host-verified production source (currently 9701e7b). The host
    separately checks Ollama's queue/parallel settings and the existing reranker
    lock. Matching request context/options avoids incompatible shared residency;
    this check neither schedules requests nor prevents model management traffic.
    """
    try:
        baseline = _projection(baseline_source)
        candidate = _projection(candidate_source)
        canonical = private_io.canonical(baseline)
        if canonical != private_io.canonical(candidate):
            raise SharedRequestPolicyError("shared_request_policy_changed")
        return {
            "schema_version": 1,
            "status": "compatible",
            "request_policy_sha256": hashlib.sha256(canonical).hexdigest(),
            "policy": baseline,
        }
    except SharedRequestPolicyError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        SyntaxError,
        RecursionError,
    ):
        raise SharedRequestPolicyError("shared_request_policy_unsupported") from None
