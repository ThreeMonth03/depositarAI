"""Compare the fixed request policy of two independently verified source roots.

The host binds both roots to committed source maps and immutable API images
before calling this pure, offline check. This is a compatibility gate for
reviewed candidates, not an interpreter, provenance proof or network ACL.
Prompt text, schemas and search flow may change without changing this policy.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path
import re

from search.operations.shared import private_io


SOURCE_FILES = (
    "search/model_policy.py",
    "search/scope.py",
    "search/planning/model.py",
    "search/api/worker.py",
    "search/api/conversation/worker.py",
    "search/api/shared/model_transport.py",
    "search/reranker/contract.py",
    "search/api/retrieval/engine.py",
    "search/api/shared/backend.py",
    "search/transport/http.py",
    "search/api/shared/peers.py",
    "search/serialization.py",
)
MODEL_FIELDS = (
    "SCOPE_MODEL",
    "SCOPE_MODEL_DIGEST",
    "SCOPE_NUM_CTX",
    "SCOPE_NUM_PREDICT",
    "PLANNER_MODEL",
    "PLANNER_MODEL_DIGEST",
)
RERANKER_FIELDS = {
    "model_id": "MODEL_ID",
    "model_revision": "MODEL_REVISION",
    "max_candidates": "MAX_CANDIDATES",
    "max_passage_chars": "MAX_PASSAGE_CHARS",
    "max_request_bytes": "MAX_REQUEST_BYTES",
    "max_response_bytes": "MAX_RESPONSE_BYTES",
    "request_deadline_seconds": "REQUEST_DEADLINE_S",
    "inference_budget_seconds": "INFERENCE_BUDGET_S",
}
WIRE_IMPORTS = {
    "search/api/worker.py": (
        "json",
        "MAX_RESPONSE_BYTES",
        "validate_order",
        "validate_request",
        "service_address",
        "strict_json",
        "SubprocessBackend",
        "ModelTransport",
        "HTTPRequest",
        "UrllibTransport",
    ),
    "search/api/retrieval/engine.py": (
        "time",
        "unicodedata",
        "ValidatedSearch",
        "REQUEST_DEADLINE_S",
        "passage",
        "validate_order",
        "Busy",
        "BackendFailure",
        "BackendTimeout",
    ),
    "search/api/shared/backend.py": (
        "asyncio",
        "json",
        "sys",
        "strict_json",
    ),
    "search/api/shared/model_transport.py": (
        "dataclasses",
        "json",
        "urllib.parse",
        "MAX_REQUEST_BYTES",
        "OLLAMA_BASE_URL",
        "strict_json",
        "HTTPResponse",
        "UrllibTransport",
        "service_address",
    ),
    "search/transport/http.py": (
        "dataclass",
        "math",
        "time",
        "Any",
        "Optional",
        "urllib.error",
        "urllib.request",
    ),
    "search/api/shared/peers.py": ("ipaddress", "socket"),
    "search/serialization.py": ("json",),
}
WIRE_NODES = {
    "search/reranker/contract.py": (
        (ast.FunctionDef, "validate_request"),
        (ast.FunctionDef, "validate_order"),
    ),
    "search/api/shared/backend.py": (
        (ast.ClassDef, "BackendFailure"),
        (ast.ClassDef, "BackendTimeout"),
        (ast.ClassDef, "SubprocessBackend"),
    ),
    "search/transport/http.py": (
        (ast.ClassDef, "HTTPRequest"),
        (ast.ClassDef, "HTTPResponse"),
        (ast.ClassDef, "_NoRedirect"),
        (ast.ClassDef, "UrllibTransport"),
    ),
    "search/api/shared/peers.py": ((ast.FunctionDef, "service_address"),),
    "search/serialization.py": ((ast.FunctionDef, "strict_json"),),
}
WIRE_DECLARATIONS = {
    **WIRE_NODES,
    "search/api/worker.py": ((ast.FunctionDef, "run"),),
    "search/api/retrieval/engine.py": ((ast.ClassDef, "RetrievalEngine"),),
    "search/api/shared/model_transport.py": (
        (ast.ClassDef, "ModelTransport"),
    ),
}
WIRE_ASSIGNMENTS = {
    "search/reranker/contract.py": tuple(RERANKER_FIELDS.values()),
    "search/api/shared/backend.py": ("MAX_WORKER_BYTES",),
}
BINDING_FIELDS = {
    "arg": "arg",
    "FunctionDef": "name",
    "AsyncFunctionDef": "name",
    "ClassDef": "name",
    "ExceptHandler": "name",
    "MatchAs": "name",
    "MatchStar": "name",
    "MatchMapping": "rest",
}


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
    return _one(
        node for node in body if isinstance(node, kind) and node.name == name
    )


def _assignment_node(body, name):
    return _one(
        node
        for node in body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
    )


def _assignment(body, name):
    return _assignment_node(body, name).value


def _fields(node):
    _require(isinstance(node, ast.Dict))
    names = [ast.literal_eval(key) for key in node.keys]
    _require(
        all(type(key) is str for key in names)
        and len(set(names)) == len(names)
    )
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
            {
                "model",
                "messages",
                "stream",
                "think",
                "format",
                "options",
                "keep_alive",
            },
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


def _wire_integer(node, *, allow_product=False):
    # The existing byte cap is written as two fixed integer literals.
    # Keep that representation without evaluating general source expressions.
    if isinstance(node, ast.BinOp):
        _require(allow_product and isinstance(node.op, ast.Mult))
        left, right = ast.literal_eval(node.left), ast.literal_eval(node.right)
        _require(
            type(left) is int
            and type(right) is int
            and 0 < left <= 2**31
            and 0 < right <= 2**31
        )
        value = left * right
    else:
        value = ast.literal_eval(node)
    _require(type(value) is int and 0 < value <= 2**31)
    return value


def _reranker(body):
    values = {}
    for name, constant in RERANKER_FIELDS.items():
        node = _assignment(body, constant)
        values[name] = (
            ast.literal_eval(node)
            if name in {"model_id", "model_revision"}
            else _wire_integer(
                node, allow_product=constant == "MAX_REQUEST_BYTES"
            )
        )
    for name in ("model_id", "model_revision"):
        value = values[name]
        _require(
            type(value) is str
            and 1 <= len(value) <= 256
            and value.isascii()
            and all(33 <= ord(character) <= 126 for character in value)
        )
    _require(
        re.fullmatch(r"[0-9a-f]{40}", values["model_revision"]) is not None
    )
    return values


def _ast_sha256(nodes):
    normalized = copy.deepcopy(nodes)
    for node in normalized:
        for part in ast.walk(node):
            if (
                isinstance(
                    part, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                )
                and part.body
            ):
                first = part.body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and type(first.value.value) is str
                ):
                    part.body.pop(0)
    return hashlib.sha256(
        private_io.canonical(
            [ast.dump(node, include_attributes=False) for node in normalized]
        )
    ).hexdigest()


def _wire_imports(tree, names):
    selected, found = [], []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        aliases = [alias for alias in node.names if alias.name in names]
        if aliases:
            selected.append(
                {
                    "module": getattr(node, "module", None),
                    "level": getattr(node, "level", 0),
                    "names": sorted(
                        (alias.name, alias.asname) for alias in aliases
                    ),
                }
            )
            found.extend(alias.name for alias in aliases)
    _require(sorted(found) == sorted(names))
    return sorted(selected, key=private_io.canonical)


def _scope_nodes(body):
    pending = list(body)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pending.extend(node.decorator_list)
            pending.extend(node.args.defaults)
            pending.extend(
                value for value in node.args.kw_defaults if value is not None
            )
        elif isinstance(node, ast.ClassDef):
            pending.extend(node.decorator_list + node.bases + node.keywords)
        else:
            pending.extend(ast.iter_child_nodes(node))


def _stored_name(node):
    if not isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)) or not (
        isinstance(node.ctx, (ast.Store, ast.Del))
    ):
        return None
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _binding_names(node):
    if isinstance(node, ast.alias):
        return [node.asname or node.name.partition(".")[0]]
    field = BINDING_FIELDS.get(type(node).__name__)
    name = getattr(node, field) if field else _stored_name(node)
    return [name] if name else []


def _guard_bindings(body, names, allowed):
    approved = {id(node) for node in allowed}
    for node in _scope_nodes(body):
        for name in _binding_names(node):
            _require(
                name != "*" and (name not in names or id(node) in approved)
            )


def _module_bindings(tree, relative):
    imports = WIRE_IMPORTS.get(relative, ())
    declarations = WIRE_DECLARATIONS[relative]
    assignments = WIRE_ASSIGNMENTS.get(relative, ())
    allowed = [_named(tree.body, kind, name) for kind, name in declarations]
    allowed.extend(
        _assignment_node(tree.body, name).targets[0] for name in assignments
    )
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            allowed.extend(
                alias for alias in node.names if alias.name in imports
            )
    names = {name for _, name in declarations} | set(assignments)
    names.update(name.partition(".")[0] for name in imports)
    _guard_bindings(tree.body, names, allowed)


def _partial_class_bindings(tree, name, names):
    owner = _named(tree.body, ast.ClassDef, name)
    _require(
        not owner.bases
        and not owner.keywords
        and not owner.decorator_list
        and not getattr(owner, "type_params", [])
    )
    methods = [
        node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    _guard_bindings(owner.body, names, methods)
    return methods


def _engine_bindings(tree):
    methods = _partial_class_bindings(
        tree, "RetrievalEngine", {"search", "rerank_candidates"}
    )
    names = set(WIRE_IMPORTS["search/api/retrieval/engine.py"]) | {"call"}
    for method in methods:
        allowed = (
            _named(method.body, ast.AsyncFunctionDef, "call")
            if method.name == "search"
            else _one(arg for arg in method.args.args if arg.arg == "call")
        )
        _guard_bindings([method.args, *method.body], names, [allowed])


def _wire_bindings(trees):
    # Own only the selected names and their known lexical scopes. This does
    # not interpret source or track dynamic exec/getattr/setattr behavior.
    for relative in WIRE_DECLARATIONS:
        _module_bindings(trees[relative], relative)
    worker = _named(trees["search/api/worker.py"].body, ast.FunctionDef, "run")
    _guard_bindings(
        [worker.args, *worker.body],
        set(WIRE_IMPORTS["search/api/worker.py"]),
        [],
    )
    _engine_bindings(trees["search/api/retrieval/engine.py"])
    _partial_class_bindings(
        trees["search/api/shared/model_transport.py"],
        "ModelTransport",
        {"__init__", "send"},
    )


def _worker_rerank(tree):
    run = _named(tree.body, ast.FunctionDef, "run")
    branches = (
        node
        for node in run.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "operation"
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "rerank"
    )
    return _one(branches)


def _rerank_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "call"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "rerank"
    )


def _stage_link(methods, call):
    owner = _one(method for method in methods if call in ast.walk(method))
    if owner.name == "search":
        return
    search = _named(methods, ast.AsyncFunctionDef, "search")
    link = _one(
        node
        for node in ast.walk(search)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rerank_candidates"
    )
    expected = ast.Assign(
        targets=[ast.Name(id="found", ctx=ast.Store())],
        value=ast.Await(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="self", ctx=ast.Load()),
                    attr="rerank_candidates",
                    ctx=ast.Load(),
                ),
                args=[
                    ast.Name(id=name, ctx=ast.Load())
                    for name in ("request", "result", "found", "call")
                ],
                keywords=[],
            )
        ),
    )
    _require(
        any(
            ast.dump(node) == ast.dump(expected) and link in ast.walk(node)
            for node in search.body
        )
    )


def _engine_rerank(tree):
    engine = _named(tree.body, ast.ClassDef, "RetrievalEngine")
    # The existing stage was extracted from search into a named helper. Bind
    # its actual wire operation, independently of that method boundary.
    methods = [
        node
        for node in engine.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {"search", "rerank_candidates"}
    ]
    names = [method.name for method in methods]
    _require(len(names) == len(set(names)))
    call = _one(
        node
        for method in methods
        for node in ast.walk(method)
        if _rerank_call(node)
    )
    _stage_link(methods, call)
    boundary = _one(
        node
        for method in methods
        for node in ast.walk(method)
        if isinstance(node, ast.Try) and call in ast.walk(node)
    )
    search = _named(methods, ast.AsyncFunctionDef, "search")
    return boundary, _named(search.body, ast.AsyncFunctionDef, "call")


def _reranker_wire(trees):
    _wire_bindings(trees)
    nodes = [
        _worker_rerank(trees["search/api/worker.py"]),
        *_engine_rerank(trees["search/api/retrieval/engine.py"]),
        _assignment(
            trees["search/api/shared/backend.py"].body, "MAX_WORKER_BYTES"
        ),
    ]
    for relative, names in WIRE_NODES.items():
        nodes.extend(
            _named(trees[relative].body, kind, name) for kind, name in names
        )
    imports = {
        relative: _wire_imports(trees[relative], names)
        for relative, names in WIRE_IMPORTS.items()
    }
    return hashlib.sha256(
        private_io.canonical(
            {"ast_sha256": _ast_sha256(nodes), "imports": imports}
        )
    ).hexdigest()


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
        name: ast.literal_eval(_assignment(policy.body, name))
        for name in MODEL_FIELDS
    }
    classifier = _named(
        trees[SOURCE_FILES[1]].body, ast.ClassDef, "OllamaScopeClassifier"
    )
    attributes = {
        name: _value(_assignment(classifier.body, name), constants)
        for name in (
            "model",
            "model_digest",
            "num_ctx",
            "num_predict",
            "thinking",
        )
    }
    complete = _named(classifier.body, ast.FunctionDef, "_complete")
    scope = _request(
        _assignment(complete.body, "payload"), constants, attributes
    )
    scope["model_digest"] = attributes["model_digest"]
    _require(
        scope["stream"] is False
        and scope["think"] is False
        and scope["truncate"] is False
        and scope["shift"] is False
    )

    planner_module = trees[SOURCE_FILES[2]]
    constants["OPTIONS"] = ast.literal_eval(
        _assignment(planner_module.body, "OPTIONS")
    )
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
    planner["production_keep_alive"] = _transport(
        call.func.value.args[0], "answer"
    )
    transport = _named(
        trees[SOURCE_FILES[5]].body, ast.ClassDef, "ModelTransport"
    )
    methods = [
        _named(transport.body, ast.FunctionDef, name)
        for name in ("__init__", "send")
    ]
    return {
        "scope": scope,
        "planner": planner,
        "reranker": _reranker(trees[SOURCE_FILES[6]].body),
        "transport_sha256": _ast_sha256(methods),
        "reranker_wire_sha256": _reranker_wire(trees),
    }


def validate_shared_request_policy(baseline_source, candidate_source):
    """Compare reviewed fixed model requests without importing either source.

    Baseline is the host-verified production source (currently 9701e7b). The
    host separately checks Ollama's queue/parallel settings and the existing
    reranker lock. Matching request context/options and fixed reranker model/
    wire limits avoids incompatible shared requests. This check neither
    schedules requests nor prevents model management traffic.
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
        raise SharedRequestPolicyError(
            "shared_request_policy_unsupported"
        ) from None
