#!/usr/bin/env python3
"""Validate the rendered, isolated Depositár search-canary Compose policy."""

from __future__ import annotations

import json
import math
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


EXPECTED_PROJECT = "depositar-search-canary"
EXPECTED_SERVICES = {"fess-search", "opensearch-search", "ollama-embedding"}
EXPECTED_NETWORK = "search_backend"
CATALOG_VOLUME_NAME = re.compile(
    r"depositar-ai-catalog-[0-9a-f]{12}-[0-9a-f]{12}-index\Z"
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
FESS_BASE_IMAGE = (
    "ghcr.io/codelibs/fess:15.8.0@sha256:"
    "442a58831e87aae6ae22ef60e0536ececbda553bd5f5e8e5dc98cc022578dc75"
)
OPENSEARCH_BASE_IMAGE = (
    "ghcr.io/codelibs/fess-opensearch:3.8.0@sha256:"
    "c59dd34da6d0c6e0117b8851ded0832f168de9b33e275e62cd3ef3139362d7fc"
)
OLLAMA_IMAGE = (
    "ollama/ollama:0.32.14@sha256:"
    "9d30908e41144b1f1da89b9d8e33c07e4aeb43ff41a8660241b1686e2cc330ad"
)
FESS_IMAGE = "depositar-fess-search-canary:15.8.0-local"
OPENSEARCH_IMAGE = "depositar-opensearch-search-canary:3.8.0-local"
FESS_PLUGIN_URL = (
    "https://maven.codelibs.org/release/org/codelibs/fess/"
    "fess-llm-ollama/15.8.0/fess-llm-ollama-15.8.0.jar"
)
FESS_PLUGIN_SHA256 = "08301d94101404ea8fd4ae9efb12cb61cf7d2f28b554653a1b65ee5233d39b84"
PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-fA-F]{64}$")
CREDENTIAL_WORD = re.compile(
    r"(?<![A-Za-z0-9])(?:api[_.-]?key|credential|passwd|password|secret|token)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
URL_WITH_CREDENTIALS = re.compile(r"\w+://[^/@\s:]+:[^/@\s]+@")
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9])(?:api[_.-]?key|credential|passwd|password|secret|token)"
    r"\s*[=:]\s*[^\s]+",
    re.IGNORECASE,
)
SIZE = re.compile(r"^([1-9][0-9]*)([kKmMgG])$")
MODEL_ASSIGNMENT = re.compile(r"(?:^|\s)[^\s=]*[._-]model=([^\s]+)", re.IGNORECASE)
OLLAMA_MODEL_COMMAND = re.compile(
    r"\bollama\s+(?:create|pull|run)\s+([^\s]+)", re.IGNORECASE
)
GRANITE_MODEL = "granite-embedding:278m"
FESS_DICTIONARY_PATH = "/usr/share/opensearch/config/dictionary/"
NUMERIC_NON_ROOT_USER = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")
FESS_LOGIN_TOKEN = "-Dfess.system.login.required=true"
FESS_HEALTHCHECK = ["CMD", "/usr/local/bin/depositar-fess-healthcheck"]
OPENSEARCH_HEALTHCHECK = [
    "CMD",
    "python3",
    "/usr/local/bin/depositar-opensearch-healthcheck.py",
    "https://127.0.0.1:9200",
]
OLLAMA_HEALTHCHECK = ["CMD", "ollama", "list"]
FESS_SECRET_NAMES = {
    "fess_opensearch_password",
    "fess_app_cipher_key",
    "fess_initial_admin_password",
}
COMMON_SERVICE_FIELDS = {
    "cap_drop",
    "command",
    "cpus",
    "entrypoint",
    "environment",
    "healthcheck",
    "image",
    "init",
    "logging",
    "mem_limit",
    "memswap_limit",
    "pids_limit",
    "read_only",
    "restart",
    "security_opt",
    "tmpfs",
    "user",
    "volumes",
}
EXPECTED_SERVICE_FIELDS = {
    "fess-search": COMMON_SERVICE_FIELDS
    | {"build", "depends_on", "networks", "secrets"},
    "opensearch-search": COMMON_SERVICE_FIELDS | {"build", "networks", "ulimits"},
    "ollama-embedding": COMMON_SERVICE_FIELDS | {"depends_on", "network_mode"},
}
FESS_JAVA_OPTIONS = (
    "-Dfess.system.content_chunker.enabled=true",
    "-Dfess.system.content_chunker.embedding.name=ollama",
    "-Dfess.system.content_chunker.embedding.dimension=768",
    "-Dfess.system.content_chunker.embedding.ollama.api.url=http://127.0.0.1:11434",
    "-Dfess.system.content_chunker.embedding.ollama.model=granite-embedding:278m",
    "-Dfess.system.content_chunker.embedding.ollama.document.prefix=",
    "-Dfess.system.content_chunker.embedding.ollama.query.prefix=",
    "-Dfess.system.content_chunker.job.concurrency=2",
    "-Dfess.system.content_chunker.job.bulk_size=100",
    "-Dfess.system.content_chunker.job.max_documents_per_run=10000",
    "-Dfess.system.content_chunker.search.enabled=true",
    "-Dfess.system.login.required=true",
    "-Duser.timezone=UTC",
    "-Dfess.config.thumbnail.crawler.enabled=false",
    "-Dfess.config.rag.chat.enabled=false",
    "-Dfess.config.api.access.token.required=true",
    "-Dfess.config.rate.limit.enabled=true",
    "-Dfess.config.paging.page.size=25",
    "-Dfess.config.query.additional.api.response.fields=rf_score,searcher",
    "-Dfess.config.query.orsearch.min.hit.count=-1",
    "-Drank.fusion.searchers=default,semantic_chunk",
)
OPENSEARCH_JAVA_OPTIONS = (
    "-Xms2g",
    "-Xmx2g",
    "-Djava.io.tmpdir=/run/opensearch-native",
)
EXPECTED_ENVIRONMENTS = {
    "fess-search": {
        "HOME": "/var/lib/fess",
        "TZ": "UTC",
        "NVIDIA_VISIBLE_DEVICES": "void",
        "SEARCH_ENGINE_HTTP_URL": "https://opensearch-search:9200",
        "SEARCH_ENGINE_TYPE": "opensearch",
        "FESS_DICTIONARY_PATH": FESS_DICTIONARY_PATH,
        "FESS_HEAP_SIZE": "2g",
        "FESS_CRAWLER_ENABLED": "false",
        "FESS_SCHEDULER_ENABLED": "false",
        "FESS_RAG_CHAT_ENABLED": "false",
        "FESS_LOGIN_REQUIRED": "true",
        "FESS_SEMANTIC_MODEL": GRANITE_MODEL,
        "FESS_PLUGINS": "",
        "FESS_JAVA_OPTS": " ".join(FESS_JAVA_OPTIONS),
    },
    "opensearch-search": {
        "HOME": "/tmp/opensearch-home",
        "NVIDIA_VISIBLE_DEVICES": "void",
        "DISABLE_INSTALL_DEMO_CONFIG": "true",
        "DISABLE_PERFORMANCE_ANALYZER_AGENT_CLI": "true",
        "discovery.type": "single-node",
        "cluster.name": EXPECTED_PROJECT,
        "node.name": "opensearch-search",
        "node.store.allow_mmap": "false",
        "OPENSEARCH_JAVA_OPTS": " ".join(OPENSEARCH_JAVA_OPTIONS),
    },
    "ollama-embedding": {
        "HOME": "/tmp/ollama-home",
        "CUDA_VISIBLE_DEVICES": "-1",
        "NVIDIA_VISIBLE_DEVICES": "void",
        "OLLAMA_HOST": "127.0.0.1:11434",
        "OLLAMA_MODELS": "/models",
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_MAX_QUEUE": "1",
        "OLLAMA_KEEP_ALIVE": "10m",
        "DEPOSITAR_EMBEDDING_MODEL": GRANITE_MODEL,
    },
}
EXPECTED_VOLUME_PATHS = {
    "opensearch-search": (
        (
            "opensearch/data",
            "/usr/share/opensearch/data",
            False,
        ),
        (
            "opensearch/dictionary",
            "/usr/share/opensearch/config/dictionary",
            False,
        ),
        (
            "opensearch/config/opensearch.yml",
            "/usr/share/opensearch/config/opensearch.yml",
            True,
        ),
        (
            "opensearch/config/root-ca.pem",
            "/usr/share/opensearch/config/root-ca.pem",
            True,
        ),
        (
            "opensearch/config/node.pem",
            "/usr/share/opensearch/config/node.pem",
            True,
        ),
        (
            "opensearch/config/node-key.pem",
            "/usr/share/opensearch/config/node-key.pem",
            True,
        ),
        (
            "opensearch/config/internal_users.yml",
            "/usr/share/opensearch/config/opensearch-security/internal_users.yml",
            True,
        ),
        (
            "opensearch/config/roles.yml",
            "/usr/share/opensearch/config/opensearch-security/roles.yml",
            True,
        ),
        (
            "opensearch/config/roles_mapping.yml",
            "/usr/share/opensearch/config/opensearch-security/roles_mapping.yml",
            True,
        ),
    ),
    "fess-search": (
        (
            "opensearch/config/root-ca.pem",
            "/run/opensearch-ca/root-ca.pem",
            True,
        ),
    ),
    "ollama-embedding": (("ollama/models", "/models", True),),
}


def fail(message: str) -> NoReturn:
    raise ValueError(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    require(isinstance(value, Mapping), f"{label} must be an object")
    return value


def environment(service: Mapping[str, Any], name: str) -> dict[str, str]:
    value = as_mapping(service.get("environment"), f"{name}: environment")
    return {str(key): str(item) for key, item in value.items()}


def network_names(service: Mapping[str, Any], name: str) -> set[str]:
    value = service.get("networks") or {}
    if isinstance(value, list):
        return {str(item) for item in value}
    value = as_mapping(value, f"{name}: networks")
    return {str(item) for item in value}


def command_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return " ".join(str(item) for item in value)
    return str(value)


def is_enabled(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "on", "true", "yes"}


def assert_digest_pinned(value: Any, label: str) -> None:
    require(
        isinstance(value, str) and PINNED_IMAGE.fullmatch(value) is not None,
        f"{label} must be digest-pinned",
    )


def assert_exact_build(
    service: Mapping[str, Any],
    name: str,
    *,
    context: Path,
    image: str,
    args: Mapping[str, str],
    dockerfile: str = "Dockerfile",
) -> None:
    require(service.get("image") == image, f"{name}: output image name changed")
    build = as_mapping(service.get("build"), f"{name}: build")
    require(
        set(build) == {"context", "dockerfile", "args"},
        f"{name}: build fields changed",
    )
    require(build.get("context") == str(context), f"{name}: build context changed")
    require(build.get("dockerfile") == dockerfile, f"{name}: Dockerfile changed")
    actual_args = as_mapping(build.get("args"), f"{name}: build args")
    require(actual_args == args, f"{name}: reviewed build args changed")


def assert_exact_service_shape(service: Mapping[str, Any], name: str) -> None:
    expected_fields = set(EXPECTED_SERVICE_FIELDS[name])
    actual_fields = set(service)
    require(
        actual_fields == expected_fields,
        f"{name}: service fields changed "
        f"(missing={sorted(expected_fields - actual_fields)!r}, "
        f"unexpected={sorted(actual_fields - expected_fields)!r})",
    )
    require(service.get("command") is None, f"{name}: command override is forbidden")
    require(
        service.get("entrypoint") is None,
        f"{name}: entrypoint override is forbidden",
    )
    require(
        service.get("security_opt") == ["no-new-privileges:true"],
        f"{name}: security_opt must be exactly no-new-privileges:true",
    )


def assert_exact_environment(
    service: Mapping[str, Any],
    name: str,
    vector_pass_documents: int = 10000,
) -> None:
    actual = environment(service, name)
    expected = dict(EXPECTED_ENVIRONMENTS[name])
    if name == "fess-search":
        expected["FESS_JAVA_OPTS"] = " ".join(vector_options(vector_pass_documents))
    actual_keys = set(actual)
    expected_keys = set(expected)
    require(
        actual_keys == expected_keys,
        f"{name}: environment key allowlist changed "
        f"(missing={sorted(expected_keys - actual_keys)!r}, "
        f"unexpected={sorted(actual_keys - expected_keys)!r})",
    )
    for key, expected_value in expected.items():
        require(
            actual[key] == expected_value,
            f"{name}: environment value for {key} changed",
        )


RESOURCE_BOUNDS = {
    "fess-search": ((1.0, 8.0), (4 * 1024**3, 16 * 1024**3), (128, 2048)),
    "opensearch-search": ((1.0, 8.0), (4 * 1024**3, 16 * 1024**3), (256, 2048)),
    "ollama-embedding": ((1.0, 16.0), (2 * 1024**3, 16 * 1024**3), (64, 512)),
}


def assert_no_plaintext_credentials(service: Mapping[str, Any], name: str) -> None:
    env = environment(service, name)
    for key, value in env.items():
        require(
            CREDENTIAL_WORD.search(key) is None,
            f"{name}: credential-like environment key {key!r} is forbidden",
        )
        require(
            URL_WITH_CREDENTIALS.search(value) is None
            and CREDENTIAL_ASSIGNMENT.search(value) is None,
            f"{name}: credential-like environment value is forbidden",
        )

    for field in ("command", "entrypoint"):
        text = command_text(service.get(field))
        require(
            CREDENTIAL_WORD.search(text) is None
            and URL_WITH_CREDENTIALS.search(text) is None,
            f"{name}: credential-like material in {field} is forbidden",
        )

    labels = service.get("labels") or {}
    if isinstance(labels, Mapping):
        label_text = " ".join(f"{key}={value}" for key, value in labels.items())
    elif isinstance(labels, list):
        label_text = " ".join(str(item) for item in labels)
    else:
        fail(f"{name}: labels must be an object/list")
    require(
        CREDENTIAL_WORD.search(label_text) is None
        and URL_WITH_CREDENTIALS.search(label_text) is None,
        f"{name}: credential-like labels are forbidden",
    )


def volumes(service: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    value = service.get("volumes") or []
    require(isinstance(value, list), f"{name}: volumes must be a list")
    result: list[Mapping[str, Any]] = []
    for item in value:
        result.append(as_mapping(item, f"{name}: volume entry"))
    return result


def assert_exact_secrets(
    config: Mapping[str, Any],
    services: Mapping[str, Mapping[str, Any]],
    root: PurePosixPath,
) -> None:
    top_secrets = as_mapping(config.get("secrets"), "top-level secrets")
    require(set(top_secrets) == FESS_SECRET_NAMES, "top-level secret allowlist changed")
    for name in FESS_SECRET_NAMES:
        definition = as_mapping(top_secrets[name], f"top-level secret {name}")
        require(
            set(definition) == {"file", "name"},
            f"top-level secret {name}: fields changed",
        )
        require(
            definition.get("name") == f"{EXPECTED_PROJECT}_{name}",
            f"top-level secret {name}: generated name changed",
        )
        require(
            definition.get("file") == str(root / "secrets" / name),
            f"top-level secret {name}: file path changed",
        )

    for name in ("opensearch-search", "ollama-embedding"):
        require(not services[name].get("secrets"), f"{name}: secrets are forbidden")

    raw_fess_secrets = services["fess-search"].get("secrets")
    require(isinstance(raw_fess_secrets, list), "fess-search: secrets must be a list")
    require(
        len(raw_fess_secrets) == len(FESS_SECRET_NAMES),
        "fess-search: secret allowlist changed",
    )
    found: set[str] = set()
    for raw_secret in raw_fess_secrets:
        item = as_mapping(raw_secret, "fess-search: secret entry")
        require(
            set(item) == {"source", "target"},
            "fess-search: secret fields changed",
        )
        source = str(item.get("source") or "")
        require(source in FESS_SECRET_NAMES, "fess-search: unexpected secret source")
        require(source not in found, "fess-search: duplicate secret source")
        require(item.get("target") == source, "fess-search: secret target changed")
        found.add(source)
    require(found == FESS_SECRET_NAMES, "fess-search: secret allowlist changed")

    rendered = json.dumps(config, sort_keys=True)
    require(
        "opensearch_admin_password" not in rendered,
        "OpenSearch admin password must not enter the Compose model",
    )


def assert_no_device_reservations(service: Mapping[str, Any], name: str) -> None:
    deploy = as_mapping(service.get("deploy") or {}, f"{name}: deploy")
    resources = as_mapping(deploy.get("resources") or {}, f"{name}: deploy resources")
    reservations = as_mapping(
        resources.get("reservations") or {}, f"{name}: resource reservations"
    )
    require(
        not reservations.get("devices"),
        f"{name}: device reservations are forbidden",
    )


def assert_exact_runtime_device_requests(
    name: str,
    host_config: Mapping[str, Any],
) -> None:
    """Reject host devices and accelerator requests in CPU catalog containers."""

    for field in ("Devices", "DeviceRequests"):
        value = host_config.get(field)
        require(
            value is None or (isinstance(value, list) and not value),
            f"{name}: runtime {field} must be absent or empty",
        )


def assert_linux_boundary(
    service: Mapping[str, Any],
    name: str,
) -> None:
    require(not service.get("ports"), f"{name}: published ports are forbidden")
    require(
        service.get("read_only") is True, f"{name}: root filesystem must be read-only"
    )
    require(not service.get("privileged", False), f"{name}: privileged is forbidden")
    require(not service.get("cap_add"), f"{name}: cap_add is forbidden")
    require(not service.get("devices"), f"{name}: host devices are forbidden")
    require(
        not service.get("device_cgroup_rules"),
        f"{name}: device cgroup rules are forbidden",
    )
    require("gpus" not in service, f"{name}: GPU requests are forbidden")
    runtime = str(service.get("runtime") or "").lower()
    require(runtime != "nvidia", f"{name}: NVIDIA runtime is forbidden")
    assert_no_device_reservations(service, name)
    require(service.get("network_mode") != "host", f"{name}: host network is forbidden")
    require(service.get("pid") != "host", f"{name}: host PID namespace is forbidden")
    require(service.get("ipc") != "host", f"{name}: host IPC namespace is forbidden")
    require("configs" not in service, f"{name}: Compose configs are forbidden")
    require("volumes_from" not in service, f"{name}: volumes_from is forbidden")
    require(set(service.get("cap_drop") or []) == {"ALL"}, f"{name}: cap_drop ALL")
    require(
        service.get("security_opt") == ["no-new-privileges:true"],
        f"{name}: security_opt must be exactly no-new-privileges:true",
    )

    for mount in volumes(service, name):
        source = str(mount.get("source") or "").lower()
        target = str(mount.get("target") or "").lower()
        require(
            "docker.sock" not in source and "docker.sock" not in target,
            f"{name}: Docker socket mounts are forbidden",
        )
        if mount.get("type") == "bind":
            bind = as_mapping(mount.get("bind"), f"{name}: bind options")
            require(
                bind.get("create_host_path") is False,
                f"{name}: bind mounts must not auto-create host paths",
            )


def assert_resources_and_health(service: Mapping[str, Any], name: str) -> None:
    raw_cpus = service.get("cpus")
    raw_memory = service.get("mem_limit")
    raw_swap = service.get("memswap_limit")
    raw_pids = service.get("pids_limit")
    require(
        not any(
            isinstance(value, bool)
            for value in (raw_cpus, raw_memory, raw_swap, raw_pids)
        ),
        f"{name}: boolean resource limits are forbidden",
    )
    try:
        cpus = float(raw_cpus)
        memory = int(raw_memory)
        swap = int(raw_swap)
        pids = int(raw_pids)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name}: invalid resource limit") from error
    require(math.isfinite(cpus), f"{name}: CPU limit must be finite")
    cpu_bounds, memory_bounds, pid_bounds = RESOURCE_BOUNDS[name]
    require(
        cpu_bounds[0] <= cpus <= cpu_bounds[1],
        f"{name}: CPU limit is outside the reviewed bounds",
    )
    require(
        memory_bounds[0] <= memory <= memory_bounds[1],
        f"{name}: memory limit is outside the reviewed bounds",
    )
    require(
        swap == memory,
        f"{name}: memswap_limit must equal mem_limit to prevent swap growth",
    )
    require(
        pid_bounds[0] <= pids <= pid_bounds[1],
        f"{name}: PID limit is outside the reviewed bounds",
    )
    require(
        service.get("restart") == "unless-stopped", f"{name}: restart policy changed"
    )
    require(service.get("init") is True, f"{name}: init must be enabled")

    logging = as_mapping(service.get("logging"), f"{name}: logging")
    require(
        logging.get("driver") == "json-file", f"{name}: json-file logging is required"
    )
    options = as_mapping(logging.get("options"), f"{name}: logging options")
    size = SIZE.fullmatch(str(options.get("max-size") or ""))
    require(size is not None, f"{name}: a bounded log max-size is required")
    scale = {"k": 1024, "m": 1024**2, "g": 1024**3}[size.group(2).lower()]
    require(
        int(size.group(1)) * scale <= 100 * 1024**2,
        f"{name}: log max-size must not exceed 100 MiB",
    )
    try:
        max_files = int(options.get("max-file", 0))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name}: invalid log max-file") from error
    require(1 <= max_files <= 10, f"{name}: log max-file must be between 1 and 10")

    health = as_mapping(service.get("healthcheck"), f"{name}: healthcheck")
    require(not health.get("disable", False), f"{name}: healthcheck must be enabled")
    test = health.get("test")
    require(test and test != ["NONE"], f"{name}: healthcheck command is required")
    for key in ("interval", "timeout", "retries"):
        require(
            health.get(key) not in (None, "", 0),
            f"{name}: healthcheck {key} is required",
        )


def assert_catalog_volume_name(name: Any) -> None:
    require(
        type(name) is str and CATALOG_VOLUME_NAME.fullmatch(name) is not None,
        "catalog index volume name must identify one qualified catalog",
    )


def assert_catalog_data_mount(mount: Mapping[str, Any]) -> None:
    require(
        set(mount) == {"type", "source", "target", "volume"}
        and mount.get("type") == "volume"
        and mount.get("source") == "catalog-index"
        and mount.get("target") == "/usr/share/opensearch/data",
        "opensearch-search: catalog index mount changed",
    )
    options = as_mapping(mount.get("volume"), "catalog index volume options")
    require(
        set(options) == {"nocopy"} and options.get("nocopy") is True,
        "opensearch-search: catalog index volume must use only nocopy=true",
    )


def derive_canary_root(
    opensearch: Mapping[str, Any], *, catalog_volume_name: str | None = None
) -> PurePosixPath:
    data_mounts = [
        mount
        for mount in volumes(opensearch, "opensearch-search")
        if mount.get("target") == "/usr/share/opensearch/data"
    ]
    require(len(data_mounts) == 1, "opensearch-search: one data mount is required")
    root_mount = data_mounts[0]
    leaf = "data"
    if catalog_volume_name is not None:
        assert_catalog_volume_name(catalog_volume_name)
        assert_catalog_data_mount(root_mount)
        dictionary = [
            mount
            for mount in volumes(opensearch, "opensearch-search")
            if mount.get("target") == "/usr/share/opensearch/config/dictionary"
        ]
        require(
            len(dictionary) == 1 and dictionary[0].get("type") == "bind",
            "opensearch-search: one dictionary bind mount is required",
        )
        root_mount, leaf = dictionary[0], "dictionary"
    source = str(root_mount.get("source") or "")
    source_path = PurePosixPath(source)
    require(
        source_path.is_absolute(), "opensearch-search: data source must be absolute"
    )
    require(
        ".." not in source_path.parts and str(source_path) == source,
        "opensearch-search: unsafe data source",
    )
    require(
        source_path.name == leaf and source_path.parent.name == "opensearch",
        "opensearch-search: data source path changed",
    )
    root = source_path.parent.parent
    require(root != PurePosixPath("/"), "search canary storage root is too broad")
    return root


def assert_exact_volumes(
    services: Mapping[str, Mapping[str, Any]],
    root: PurePosixPath,
    *,
    catalog_volume_name: str | None = None,
) -> None:
    if catalog_volume_name is not None:
        assert_catalog_volume_name(catalog_volume_name)
    for name, policies in EXPECTED_VOLUME_PATHS.items():
        service_mounts = volumes(services[name], name)
        require(
            len(service_mounts) == len(policies), f"{name}: volume allowlist changed"
        )
        mounts_by_target: dict[str, Mapping[str, Any]] = {}
        for mount in service_mounts:
            target = str(mount.get("target") or "")
            require(target not in mounts_by_target, f"{name}: duplicate volume target")
            mounts_by_target[target] = mount
        require(
            set(mounts_by_target) == {target for _, target, _ in policies},
            f"{name}: volume target allowlist changed",
        )
        for relative_source, target, read_only in policies:
            mount = mounts_by_target[target]
            if (
                catalog_volume_name is not None
                and name == "opensearch-search"
                and target == "/usr/share/opensearch/data"
            ):
                assert_catalog_data_mount(mount)
                continue
            expected_fields = {"type", "source", "target", "bind"}
            if read_only:
                expected_fields.add("read_only")
            require(
                set(mount) == expected_fields, f"{name}: mount fields changed: {target}"
            )
            require(
                mount.get("type") == "bind", f"{name}: only bind mounts are allowed"
            )
            require(
                mount.get("source") == str(root / relative_source),
                f"{name}: mount source changed: {target}",
            )
            require(
                bool(mount.get("read_only")) is read_only,
                f"{name}: mount read-only policy changed: {target}",
            )
            require(
                as_mapping(mount.get("bind"), f"{name}: bind options")
                == {"create_host_path": False},
                f"{name}: bind options changed: {target}",
            )


def assert_exact_tmpfs(
    services: Mapping[str, Mapping[str, Any]], runtime_identity: str
) -> None:
    uid, gid = runtime_identity.split(":", 1)
    expected = {
        "opensearch-search": {
            f"/tmp:rw,noexec,nosuid,nodev,size=256m,uid={uid},gid={gid},mode=1770",
            f"/run/opensearch-native:rw,exec,nosuid,nodev,size=64m,uid={uid},gid={gid},mode=0700",
            f"/usr/share/opensearch/logs:rw,noexec,nosuid,nodev,size=512m,uid={uid},gid={gid},mode=0750",
        },
        "fess-search": {
            f"/tmp:rw,noexec,nosuid,nodev,size=256m,uid={uid},gid={gid},mode=1770",
            f"/opt/fess:rw,noexec,nosuid,nodev,size=2m,uid={uid},gid={gid},mode=0700",
            f"/var/lib/fess:rw,noexec,nosuid,nodev,size=256m,uid={uid},gid={gid},mode=0700",
            f"/var/log/fess:rw,noexec,nosuid,nodev,size=512m,uid={uid},gid={gid},mode=0700",
            f"/var/tmp/fess:rw,noexec,nosuid,nodev,size=512m,uid={uid},gid={gid},mode=0700",
        },
        "ollama-embedding": {
            f"/tmp:rw,noexec,nosuid,nodev,size=1g,uid={uid},gid={gid},mode=0700"
        },
    }
    for name, expected_entries in expected.items():
        entries = services[name].get("tmpfs")
        require(isinstance(entries, list), f"{name}: tmpfs must be a list")
        require(
            len(entries) == len(expected_entries)
            and all(isinstance(item, str) for item in entries)
            and set(entries) == expected_entries,
            f"{name}: tmpfs policy changed",
        )


def assert_granite_only(services: Mapping[str, Mapping[str, Any]]) -> None:
    for name in ("fess-search", "ollama-embedding"):
        service = services[name]
        env = environment(service, name)
        for key, value in env.items():
            normalized_key = key.upper().replace(".", "_").replace("-", "_")
            if normalized_key.endswith(("_MODEL", "_MODEL_NAME")):
                require(
                    value == GRANITE_MODEL, f"{name}: only {GRANITE_MODEL} is allowed"
                )
            for model in MODEL_ASSIGNMENT.findall(value):
                require(
                    model == GRANITE_MODEL, f"{name}: only {GRANITE_MODEL} is allowed"
                )
        for field in ("command", "entrypoint"):
            for model in OLLAMA_MODEL_COMMAND.findall(command_text(service.get(field))):
                require(
                    model == GRANITE_MODEL, f"{name}: only {GRANITE_MODEL} is allowed"
                )


def vector_options(document_limit: int) -> tuple[str, ...]:
    """Only the legacy and bounded product profiles are reviewed."""
    require(
        type(document_limit) is int and document_limit in {256, 10000},
        "vector pass profile is not reviewed",
    )
    return tuple(
        option.replace(
            "max_documents_per_run=10000", f"max_documents_per_run={document_limit}"
        )
        for option in FESS_JAVA_OPTIONS
    )


def validate(
    config: Any,
    *,
    vector_pass_documents: int = 10000,
    catalog_volume_name: str | None = None,
) -> None:
    approved_vector_options = vector_options(vector_pass_documents)
    config = as_mapping(config, "Compose config")
    require(
        config.get("name") == EXPECTED_PROJECT, "search canary project name changed"
    )
    require("configs" not in config, "top-level Compose configs are forbidden")
    if catalog_volume_name is None:
        require("volumes" not in config, "top-level named volumes are forbidden")
    else:
        assert_catalog_volume_name(catalog_volume_name)
        require(
            catalog_volume_name == EXPECTED_PROJECT + "-index",
            "catalog index volume must belong to this exact catalog project",
        )
        top_volumes = as_mapping(config.get("volumes"), "top-level volumes")
        require(
            set(top_volumes) == {"catalog-index"},
            "catalog index volume allowlist changed",
        )
        definition = as_mapping(top_volumes["catalog-index"], "catalog index volume")
        require(
            set(definition) == {"name", "external"}
            and definition.get("name") == catalog_volume_name
            and definition.get("external") is True,
            "catalog index volume definition changed",
        )

    services = as_mapping(config.get("services"), "services")
    require(
        set(services) == EXPECTED_SERVICES,
        f"service allowlist changed: expected {sorted(EXPECTED_SERVICES)!r}",
    )
    top_networks = as_mapping(config.get("networks"), "networks")
    require(set(top_networks) == {EXPECTED_NETWORK}, "search network allowlist changed")
    backend = as_mapping(top_networks.get(EXPECTED_NETWORK), EXPECTED_NETWORK)
    require(backend.get("internal") is True, f"{EXPECTED_NETWORK} must be internal")
    require(
        backend.get("external") is not True, f"{EXPECTED_NETWORK} must not be external"
    )

    typed_services: dict[str, Mapping[str, Any]] = {}
    for name in sorted(EXPECTED_SERVICES):
        service = as_mapping(services.get(name), f"{name}: service")
        typed_services[name] = service
        assert_exact_service_shape(service, name)
        assert_exact_environment(service, name, vector_pass_documents)
        assert_linux_boundary(service, name)
        assert_resources_and_health(service, name)
        assert_no_plaintext_credentials(service, name)

    runtime_users = {
        str(service.get("user") or "") for service in typed_services.values()
    }
    require(
        len(runtime_users) == 1
        and NUMERIC_NON_ROOT_USER.fullmatch(next(iter(runtime_users))) is not None,
        "all search services must use the same explicit numeric non-root UID:GID",
    )
    runtime_identity = next(iter(runtime_users))

    fess = typed_services["fess-search"]
    opensearch = typed_services["opensearch-search"]
    ollama = typed_services["ollama-embedding"]

    root = derive_canary_root(opensearch, catalog_volume_name=catalog_volume_name)
    assert_exact_volumes(typed_services, root, catalog_volume_name=catalog_volume_name)
    assert_exact_secrets(config, typed_services, root)
    assert_exact_tmpfs(typed_services, runtime_identity)

    assert_exact_build(
        fess,
        "fess-search",
        context=PROJECT_ROOT,
        dockerfile="deploy/docker/fess/Dockerfile",
        image=FESS_IMAGE,
        args={
            "FESS_BASE_IMAGE": FESS_BASE_IMAGE,
            "FESS_OLLAMA_PLUGIN_URL": FESS_PLUGIN_URL,
            "FESS_OLLAMA_PLUGIN_SHA256": FESS_PLUGIN_SHA256,
        },
    )
    assert_exact_build(
        opensearch,
        "opensearch-search",
        context=PROJECT_ROOT / "deploy" / "docker" / "opensearch",
        image=OPENSEARCH_IMAGE,
        args={"OPENSEARCH_BASE_IMAGE": OPENSEARCH_BASE_IMAGE},
    )
    require(ollama.get("image") == OLLAMA_IMAGE, "ollama-embedding: image changed")
    require("build" not in ollama, "ollama-embedding: local build is forbidden")

    require(
        network_names(fess, "fess-search") == {EXPECTED_NETWORK}
        and not fess.get("network_mode"),
        "fess-search: must attach only to search_backend",
    )
    require(
        network_names(opensearch, "opensearch-search") == {EXPECTED_NETWORK}
        and not opensearch.get("network_mode"),
        "opensearch-search: must attach only to search_backend",
    )
    require(
        ollama.get("network_mode") == "service:fess-search"
        and not network_names(ollama, "ollama-embedding"),
        "ollama-embedding: must share only the fess-search network namespace",
    )

    fess_env = environment(fess, "fess-search")
    try:
        fess_java_options = shlex.split(fess_env.get("FESS_JAVA_OPTS", ""))
    except ValueError as error:
        raise ValueError("fess-search: FESS_JAVA_OPTS is malformed") from error
    login_options = [
        option
        for option in fess_java_options
        if option.startswith("-Dfess.system.login.required=")
    ]
    require(
        tuple(fess_java_options) == approved_vector_options,
        "fess-search: reviewed FESS_JAVA_OPTS changed",
    )
    require(
        login_options == [FESS_LOGIN_TOKEN],
        f"fess-search: exactly one {FESS_LOGIN_TOKEN} option is required",
    )
    fess_health = as_mapping(fess.get("healthcheck"), "fess-search: healthcheck")
    require(
        fess_health.get("test") == FESS_HEALTHCHECK,
        "fess-search: exact HTTP-200 healthcheck helper is required",
    )

    fess_text = json.dumps(fess, sort_keys=True).lower()
    require(
        re.search(r"(?:rag[._-])?chat[._-]enabled\s*[=:]\s*true", fess_text) is None,
        "fess-search: RAG chat must remain disabled",
    )
    require(
        re.search(r"crawler[._-]enabled\s*[=:]\s*true", fess_text) is None,
        "fess-search: crawler must remain disabled",
    )
    require(
        re.search(r"scheduler[._-]enabled\s*[=:]\s*true", fess_text) is None,
        "fess-search: scheduler must remain disabled",
    )

    os_env = environment(opensearch, "opensearch-search")
    try:
        os_java_options = shlex.split(os_env.get("OPENSEARCH_JAVA_OPTS", ""))
    except ValueError as error:
        raise ValueError(
            "opensearch-search: OPENSEARCH_JAVA_OPTS is malformed"
        ) from error
    require(
        tuple(os_java_options) == OPENSEARCH_JAVA_OPTIONS,
        "opensearch-search: reviewed OPENSEARCH_JAVA_OPTS changed",
    )
    for key, value in os_env.items():
        normalized_key = key.lower().replace("-", "_").replace(".", "_")
        if normalized_key in {
            "disable_security_plugin",
            "plugins_security_disabled",
            "opensearch_security_disabled",
        }:
            require(
                not is_enabled(value), "opensearch-search: security plugin is disabled"
            )
    os_text = json.dumps(opensearch, sort_keys=True).lower()
    require(
        re.search(r"plugins[._-]security[._-]disabled\s*[=:]\s*true", os_text) is None,
        "opensearch-search: security plugin is disabled",
    )
    opensearch_health = as_mapping(
        opensearch.get("healthcheck"), "opensearch-search: healthcheck"
    )
    require(
        opensearch_health.get("test") == OPENSEARCH_HEALTHCHECK,
        "opensearch-search: exact TLS healthcheck helper is required",
    )

    ollama_health = as_mapping(
        ollama.get("healthcheck"), "ollama-embedding: healthcheck"
    )
    require(
        ollama_health.get("test") == OLLAMA_HEALTHCHECK,
        "ollama-embedding: exact local healthcheck is required",
    )

    assert_granite_only(typed_services)
    rendered = json.dumps(config, sort_keys=True).lower()
    require(
        "gpt-oss" not in rendered and "gpt_oss" not in rendered, "GPT-OSS is forbidden"
    )
    require(
        GRANITE_MODEL in rendered,
        "the pinned Granite embedding model is required",
    )


def main() -> int:
    try:
        validate(json.load(sys.stdin))
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        print(f"Error: invalid search-canary Compose policy: {error}", file=sys.stderr)
        return 1
    print("Search-canary Compose isolation and resource policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
