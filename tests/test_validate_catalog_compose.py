#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from typing import Any, Callable


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

from validate_catalog_compose import (  # noqa: E402
    FESS_BASE_IMAGE,
    FESS_IMAGE,
    FESS_PLUGIN_SHA256,
    FESS_PLUGIN_URL,
    OLLAMA_IMAGE,
    OPENSEARCH_BASE_IMAGE,
    OPENSEARCH_IMAGE,
    assert_exact_runtime_device_requests,
    validate,
)
import validate_catalog_compose as compose_policy  # noqa: E402


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
CATALOG_ROOT = "/srv/depositar/catalog"
RUNTIME_UID = "1000"
RUNTIME_GID = "1000"
GPU_DEVICE = "GPU-01234567-89ab-cdef-0123-456789abcdef"
CATALOG_PROJECT = "depositar-ai-catalog-" + "a" * 12 + "-" + "b" * 12
CATALOG_VOLUME_NAME = CATALOG_PROJECT + "-index"


def bind_mount(relative: str, target: str, *, read_only: bool) -> dict[str, Any]:
    value = {
        "type": "bind",
        "source": f"{CATALOG_ROOT}/{relative}",
        "target": target,
        "bind": {"create_host_path": False},
    }
    if read_only:
        value["read_only"] = True
    return value


def base_service() -> dict[str, Any]:
    return {
        "command": None,
        "entrypoint": None,
        "restart": "unless-stopped",
        "read_only": True,
        "user": f"{RUNTIME_UID}:{RUNTIME_GID}",
        "cpus": 4.0,
        "mem_limit": 8 * 1024**3,
        "memswap_limit": 8 * 1024**3,
        "pids_limit": 512,
        "init": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "logging": {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "5"},
        },
        "healthcheck": {
            "test": ["CMD", "/opt/depositar/healthcheck"],
            "interval": "30s",
            "timeout": "10s",
            "retries": 10,
        },
    }


def valid_config() -> dict[str, Any]:
    fess = base_service()
    fess.update(
        {
            "build": {
                "context": str(PROJECT_DIR),
                "dockerfile": "deploy/docker/fess/Dockerfile",
                "args": {
                    "FESS_BASE_IMAGE": FESS_BASE_IMAGE,
                    "FESS_OLLAMA_PLUGIN_URL": FESS_PLUGIN_URL,
                    "FESS_OLLAMA_PLUGIN_SHA256": FESS_PLUGIN_SHA256,
                },
            },
            "image": FESS_IMAGE,
            "environment": {
                "HOME": "/var/lib/fess",
                "TZ": "UTC",
                "NVIDIA_VISIBLE_DEVICES": "void",
                "SEARCH_ENGINE_HTTP_URL": "https://opensearch-search:9200",
                "SEARCH_ENGINE_TYPE": "opensearch",
                "FESS_DICTIONARY_PATH": ("/usr/share/opensearch/config/dictionary/"),
                "FESS_HEAP_SIZE": "2g",
                "FESS_CRAWLER_ENABLED": "false",
                "FESS_SCHEDULER_ENABLED": "false",
                "FESS_RAG_CHAT_ENABLED": "false",
                "FESS_LOGIN_REQUIRED": "true",
                "FESS_SEMANTIC_MODEL": "granite-embedding:278m",
                "FESS_PLUGINS": "",
                "FESS_JAVA_OPTS": (
                    "-Dfess.system.content_chunker.enabled=true "
                    "-Dfess.system.content_chunker.embedding.name=ollama "
                    "-Dfess.system.content_chunker.embedding.dimension=768 "
                    "-Dfess.system.content_chunker.embedding.ollama.api.url="
                    "http://127.0.0.1:11434 "
                    "-Dfess.system.content_chunker.embedding.ollama.model="
                    "granite-embedding:278m "
                    "-Dfess.system.content_chunker.embedding.ollama.document.prefix= "
                    "-Dfess.system.content_chunker.embedding.ollama.query.prefix= "
                    "-Dfess.system.content_chunker.job.concurrency=2 "
                    "-Dfess.system.content_chunker.job.bulk_size=100 "
                    "-Dfess.system.content_chunker.job.max_documents_per_run=256 "
                    "-Dfess.system.content_chunker.search.enabled=true "
                    "-Dfess.system.login.required=true "
                    "-Duser.timezone=UTC "
                    "-Dfess.config.thumbnail.crawler.enabled=false "
                    "-Dfess.config.rag.chat.enabled=false "
                    "-Dfess.config.api.access.token.required=true "
                    "-Dfess.config.rate.limit.enabled=true "
                    "-Dfess.config.paging.page.size=25 "
                    "-Dfess.config.query.additional.api.response.fields="
                    "rf_score,searcher "
                    "-Dfess.config.query.orsearch.min.hit.count=-1 "
                    "-Drank.fusion.searchers=default,semantic_chunk"
                ),
            },
            "networks": {"search_backend": None},
            "depends_on": {
                "opensearch-search": {
                    "condition": "service_healthy",
                    "required": True,
                    "restart": False,
                }
            },
            "secrets": [
                {
                    "source": name,
                    "target": name,
                }
                for name in (
                    "fess_opensearch_password",
                    "fess_app_cipher_key",
                    "fess_initial_admin_password",
                )
            ],
            "volumes": [
                bind_mount(
                    "opensearch/config/root-ca.pem",
                    "/run/opensearch-ca/root-ca.pem",
                    read_only=True,
                )
            ],
            "tmpfs": [
                f"/tmp:rw,noexec,nosuid,nodev,size=256m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=1770",
                f"/opt/fess:rw,noexec,nosuid,nodev,size=2m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=0700",
                f"/var/lib/fess:rw,noexec,nosuid,nodev,size=256m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=0700",
                f"/var/log/fess:rw,noexec,nosuid,nodev,size=512m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=0700",
                f"/var/tmp/fess:rw,noexec,nosuid,nodev,size=512m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=0700",
            ],
        }
    )
    fess["healthcheck"]["test"] = [
        "CMD",
        "/usr/local/bin/depositar-fess-healthcheck",
    ]

    opensearch = base_service()
    opensearch.update(
        {
            "build": {
                "context": str(PROJECT_DIR / "deploy" / "docker" / "opensearch"),
                "dockerfile": "Dockerfile",
                "args": {
                    "OPENSEARCH_BASE_IMAGE": OPENSEARCH_BASE_IMAGE,
                },
            },
            "image": OPENSEARCH_IMAGE,
            "environment": {
                "HOME": "/tmp/opensearch-home",
                "NVIDIA_VISIBLE_DEVICES": "void",
                "DISABLE_INSTALL_DEMO_CONFIG": "true",
                "DISABLE_PERFORMANCE_ANALYZER_AGENT_CLI": "true",
                "cluster.name": "depositar-catalog",
                "discovery.type": "single-node",
                "node.name": "opensearch-search",
                "node.store.allow_mmap": "false",
                "OPENSEARCH_JAVA_OPTS": (
                    "-Xms2g -Xmx2g -Djava.io.tmpdir=/run/opensearch-native"
                ),
            },
            "networks": {"search_backend": None},
            "ulimits": {"nofile": {"soft": 65536, "hard": 65536}},
            "volumes": [
                {
                    "type": "volume",
                    "source": "catalog-index",
                    "target": "/usr/share/opensearch/data",
                    "volume": {"nocopy": True},
                },
                bind_mount(
                    "opensearch/dictionary",
                    "/usr/share/opensearch/config/dictionary",
                    read_only=False,
                ),
                bind_mount(
                    "opensearch/config/opensearch.yml",
                    "/usr/share/opensearch/config/opensearch.yml",
                    read_only=True,
                ),
                bind_mount(
                    "opensearch/config/root-ca.pem",
                    "/usr/share/opensearch/config/root-ca.pem",
                    read_only=True,
                ),
                bind_mount(
                    "opensearch/config/node.pem",
                    "/usr/share/opensearch/config/node.pem",
                    read_only=True,
                ),
                bind_mount(
                    "opensearch/config/node-key.pem",
                    "/usr/share/opensearch/config/node-key.pem",
                    read_only=True,
                ),
                bind_mount(
                    "opensearch/config/internal_users.yml",
                    "/usr/share/opensearch/config/opensearch-security/internal_users.yml",
                    read_only=True,
                ),
                bind_mount(
                    "opensearch/config/roles.yml",
                    "/usr/share/opensearch/config/opensearch-security/roles.yml",
                    read_only=True,
                ),
                bind_mount(
                    "opensearch/config/roles_mapping.yml",
                    "/usr/share/opensearch/config/opensearch-security/roles_mapping.yml",
                    read_only=True,
                ),
            ],
            "tmpfs": [
                f"/tmp:rw,noexec,nosuid,nodev,size=256m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=1770",
                f"/run/opensearch-native:rw,exec,nosuid,nodev,size=64m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=0700",
                f"/usr/share/opensearch/logs:rw,noexec,nosuid,nodev,size=512m,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=0750",
            ],
        }
    )
    opensearch["healthcheck"]["test"] = [
        "CMD",
        "python3",
        "/usr/local/bin/depositar-opensearch-healthcheck.py",
        "https://127.0.0.1:9200",
    ]

    ollama = base_service()
    ollama.update(
        {
            "image": OLLAMA_IMAGE,
            "environment": {
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
                "DEPOSITAR_EMBEDDING_MODEL": "granite-embedding:278m",
            },
            "network_mode": "service:fess-search",
            "depends_on": {
                "fess-search": {
                    "condition": "service_started",
                    "required": True,
                    "restart": False,
                }
            },
            "volumes": [bind_mount("ollama/models", "/models", read_only=True)],
            "tmpfs": [
                f"/tmp:rw,noexec,nosuid,nodev,size=1g,uid={RUNTIME_UID},gid={RUNTIME_GID},mode=0700"
            ],
        }
    )
    ollama["healthcheck"]["test"] = ["CMD", "ollama", "list"]

    return {
        "name": CATALOG_PROJECT,
        "volumes": {"catalog-index": {"name": CATALOG_VOLUME_NAME, "external": True}},
        "networks": {"search_backend": {"internal": True}},
        "secrets": {
            name: {
                "file": f"{CATALOG_ROOT}/secrets/{name}",
                "name": f"{CATALOG_PROJECT}_{name}",
            }
            for name in (
                "fess_opensearch_password",
                "fess_app_cipher_key",
                "fess_initial_admin_password",
            )
        },
        "services": {
            "fess-search": fess,
            "opensearch-search": opensearch,
            "ollama-embedding": ollama,
        },
    }


Mutation = tuple[str, Callable[[dict[str, Any]], None]]


class CatalogVolumeComposePolicyTests(unittest.TestCase):
    def validate(self, config, *, volume_name=CATALOG_VOLUME_NAME):
        config["volumes"]["catalog-index"]["name"] = volume_name
        validate(config)

    def assert_mutations_fail(self, mutations):
        for label, mutate in mutations:
            with self.subTest(label=label), self.assertRaises(ValueError):
                config = valid_config()
                mutate(config)
                validate(config)

    def test_named_index_is_required_and_bound_to_exact_project(self):
        config = valid_config()
        original = copy.deepcopy(config)
        validate(config)
        self.assertEqual(original, config)
        self.assertEqual(
            Path(CATALOG_ROOT),
            compose_policy.derive_catalog_root(config["services"]["opensearch-search"]),
        )
        config.pop("volumes")
        config["services"]["opensearch-search"]["volumes"][0] = bind_mount(
            "opensearch/data", "/usr/share/opensearch/data", read_only=False
        )
        with self.assertRaises(ValueError):
            validate(config)

    def test_volume_name_cannot_select_another_project_or_arbitrary_storage(self):
        for name in (
            "",
            True,
            1,
            {},
            "arbitrary-volume",
            "/var/lib/docker/volumes/index",
            CATALOG_VOLUME_NAME.upper(),
            CATALOG_VOLUME_NAME + "\n",
            "depositar-ai-catalog-" + "c" * 12 + "-" + "b" * 12 + "-index",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.validate(valid_config(), volume_name=name)

    def test_top_level_volume_definition_is_closed_and_external(self):
        self.assert_mutations_fail(
            (
                ("missing volumes", lambda c: c.pop("volumes")),
                ("empty volumes", lambda c: c.update(volumes={})),
                (
                    "extra volume",
                    lambda c: c["volumes"].update(extra={"external": True}),
                ),
                (
                    "renamed volume key",
                    lambda c: c.update(
                        volumes={"other": c["volumes"]["catalog-index"]}
                    ),
                ),
            )
        )
        for changes in (
            {"name": CATALOG_VOLUME_NAME + "-other"},
            {"external": False},
            {"external": 1},
            {"driver": "local"},
            {"driver_opts": {"type": "nfs", "device": ":/data"}},
            {"labels": {"caller": "untrusted"}},
            {"unknown": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config = valid_config()
                config["volumes"]["catalog-index"].update(changes)
                validate(config)

    def test_named_data_mount_is_exact_writable_and_nocopy(self):
        config = valid_config()
        config["services"]["opensearch-search"]["volumes"][0]["read_only"] = False
        validate(config)
        for changes in (
            {"type": "bind"},
            {"source": "another-volume"},
            {"target": "/tmp/index"},
            {"read_only": True},
            {"read_only": 1},
            {"read_only": 0},
            {"read_only": None},
            {"bind": {"create_host_path": False}},
            {"consistency": "cached"},
            {"volume": {}},
            {"volume": {"nocopy": False}},
            {"volume": {"nocopy": 1}},
            {"volume": {"nocopy": True, "subpath": "index"}},
            {"volume": {"nocopy": True, "unknown": True}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config = valid_config()
                config["services"]["opensearch-search"]["volumes"][0].update(changes)
                validate(config)
        self.assert_mutations_fail(
            (
                (
                    "missing data volume",
                    lambda c: c["services"]["opensearch-search"]["volumes"].pop(0),
                ),
                (
                    "duplicate data volume",
                    lambda c: c["services"]["opensearch-search"]["volumes"].append(
                        copy.deepcopy(c["services"]["opensearch-search"]["volumes"][0])
                    ),
                ),
                (
                    "missing volume options",
                    lambda c: c["services"]["opensearch-search"]["volumes"][0].pop(
                        "volume"
                    ),
                ),
            )
        )

    def test_dictionary_root_and_remaining_bind_mounts_stay_closed(self):
        for source in (
            "opensearch/dictionary",
            "/opensearch/dictionary",
            CATALOG_ROOT + "/opensearch/../opensearch/dictionary",
            CATALOG_ROOT + "//opensearch/dictionary",
            CATALOG_ROOT + "/opensearch/./dictionary",
            CATALOG_ROOT + "/opensearch/data",
            "/other/catalog/opensearch/dictionary",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                config = valid_config()
                config["services"]["opensearch-search"]["volumes"][1]["source"] = source
                validate(config)
        config = valid_config()
        for role, service in config["services"].items():
            for number, mount in enumerate(service["volumes"]):
                if mount["type"] != "bind":
                    continue
                with (
                    self.subTest(role=role, number=number),
                    self.assertRaises(ValueError),
                ):
                    changed = copy.deepcopy(config)
                    changed["services"][role]["volumes"][number] = {
                        "type": "volume",
                        "source": "catalog-index",
                        "target": mount["target"],
                        "volume": {"nocopy": True},
                    }
                    self.validate(changed)


class SearchComposePolicyTests(unittest.TestCase):
    def test_only_fixed_product_vector_budget_is_accepted(self):
        validate(valid_config())
        for invalid in (10000, True, "256 ", 0, 255, 257, 50000):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                config = valid_config()
                env = config["services"]["fess-search"]["environment"]
                env["FESS_JAVA_OPTS"] = env["FESS_JAVA_OPTS"].replace(
                    "max_documents_per_run=256", f"max_documents_per_run={invalid}"
                )
                validate(config)

    def assert_mutations_fail(self, mutations: tuple[Mutation, ...]) -> None:
        for label, mutate in mutations:
            with self.subTest(label=label), self.assertRaises(ValueError):
                config = copy.deepcopy(valid_config())
                mutate(config)
                validate(config)

    def test_secure_normalized_config_is_accepted(self) -> None:
        config = valid_config()
        fess_options = config["services"]["fess-search"]["environment"][
            "FESS_JAVA_OPTS"
        ]
        ollama_environment = config["services"]["ollama-embedding"]["environment"]
        self.assertIn("-Dfess.system.content_chunker.job.concurrency=2", fess_options)
        self.assertIn("-Dfess.system.content_chunker.job.bulk_size=100", fess_options)
        self.assertEqual("1", ollama_environment["OLLAMA_NUM_PARALLEL"])
        self.assertEqual("1", ollama_environment["OLLAMA_MAX_QUEUE"])
        self.assertEqual("1", ollama_environment["OLLAMA_MAX_LOADED_MODELS"])
        validate(config)

    def test_cpu_catalog_rejects_device_reservations(self) -> None:
        for name in ("fess-search", "opensearch-search", "ollama-embedding"):
            with self.subTest(service=name), self.assertRaises(ValueError):
                config = valid_config()
                config["services"][name]["deploy"] = {
                    "resources": {
                        "reservations": {
                            "devices": [
                                {
                                    "driver": "nvidia",
                                    "device_ids": [GPU_DEVICE],
                                    "capabilities": ["gpu"],
                                }
                            ]
                        }
                    }
                }
                validate(config)

    def test_cpu_embedding_rejects_matching_gpu_environment(self) -> None:
        config = valid_config()
        config["services"]["ollama-embedding"]["environment"].update(
            CUDA_VISIBLE_DEVICES=GPU_DEVICE,
            NVIDIA_VISIBLE_DEVICES=GPU_DEVICE,
        )
        with self.assertRaises(ValueError):
            validate(config)

    def test_runtime_devices_and_accelerator_requests_are_forbidden(self) -> None:
        for name in ("fess-search", "opensearch-search", "ollama-embedding"):
            for host_config in ({}, {"Devices": [], "DeviceRequests": None}):
                with self.subTest(service=name, host_config=host_config):
                    assert_exact_runtime_device_requests(name, host_config)
            unsafe = (
                {"Devices": [{"PathOnHost": "/dev/nvidia0"}]},
                {"Devices": {}},
                {"Devices": False},
                {"DeviceRequests": {}},
                {"DeviceRequests": False},
                {
                    "DeviceRequests": [
                        {
                            "Driver": "nvidia",
                            "Count": 0,
                            "DeviceIDs": [GPU_DEVICE],
                            "Capabilities": [["gpu"]],
                            "Options": {},
                        }
                    ]
                },
            )
            for host_config in unsafe:
                with (
                    self.subTest(service=name, host_config=host_config),
                    self.assertRaises(ValueError),
                ):
                    assert_exact_runtime_device_requests(name, host_config)

    def test_project_service_and_network_allowlists_fail_closed(self) -> None:
        self.assert_mutations_fail(
            (
                ("wrong project", lambda c: c.update(name="depositar")),
                (
                    "missing service",
                    lambda c: c["services"].pop("opensearch-search"),
                ),
                (
                    "extra service",
                    lambda c: c["services"].update(evaluator=base_service()),
                ),
                (
                    "backend not internal",
                    lambda c: c["networks"]["search_backend"].update(internal=False),
                ),
                (
                    "external backend",
                    lambda c: c["networks"]["search_backend"].update(external=True),
                ),
                (
                    "extra network",
                    lambda c: c["networks"].update(public={}),
                ),
                (
                    "fess extra network",
                    lambda c: c["services"]["fess-search"]["networks"].update(
                        public=None
                    ),
                ),
                (
                    "opensearch extra network",
                    lambda c: c["services"]["opensearch-search"]["networks"].update(
                        public=None
                    ),
                ),
            )
        )

    def test_published_ports_and_host_escape_paths_are_rejected(self) -> None:
        def reserve_device(config: dict[str, Any]) -> None:
            config["services"]["fess-search"]["deploy"] = {
                "resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}
            }

        self.assert_mutations_fail(
            (
                *tuple(
                    (
                        f"{name} published port",
                        lambda c, name=name: c["services"][name].update(
                            ports=["8080:8080"]
                        ),
                    )
                    for name in sorted(
                        {"fess-search", "opensearch-search", "ollama-embedding"}
                    )
                ),
                (
                    "privileged",
                    lambda c: c["services"]["fess-search"].update(privileged=True),
                ),
                (
                    "host network",
                    lambda c: c["services"]["opensearch-search"].update(
                        network_mode="host"
                    ),
                ),
                (
                    "host pid",
                    lambda c: c["services"]["fess-search"].update(pid="host"),
                ),
                (
                    "host ipc",
                    lambda c: c["services"]["fess-search"].update(ipc="host"),
                ),
                (
                    "cap add",
                    lambda c: c["services"]["fess-search"].update(
                        cap_add=["SYS_ADMIN"]
                    ),
                ),
                (
                    "direct device",
                    lambda c: c["services"]["fess-search"].update(
                        devices=["/dev/nvidia0"]
                    ),
                ),
                (
                    "device rule",
                    lambda c: c["services"]["fess-search"].update(
                        device_cgroup_rules=["c 195:* rmw"]
                    ),
                ),
                (
                    "gpus shorthand",
                    lambda c: c["services"]["fess-search"].update(gpus="all"),
                ),
                (
                    "nvidia runtime",
                    lambda c: c["services"]["fess-search"].update(runtime="nvidia"),
                ),
                ("device reservation", reserve_device),
                (
                    "docker socket source",
                    lambda c: c["services"]["fess-search"]["volumes"].append(
                        {
                            "type": "bind",
                            "source": "/var/run/docker.sock",
                            "target": "/var/run/docker.sock",
                            "bind": {"create_host_path": False},
                        }
                    ),
                ),
                (
                    "cap drop removed",
                    lambda c: c["services"]["fess-search"].update(cap_drop=[]),
                ),
                (
                    "nnp removed",
                    lambda c: c["services"]["fess-search"].update(security_opt=[]),
                ),
            )
        )

    def test_service_field_allowlists_and_runtime_overrides_fail_closed(self) -> None:
        mutations: list[Mutation] = []
        for name in ("fess-search", "opensearch-search", "ollama-embedding"):
            mutations.extend(
                (
                    (
                        f"{name} empty command override",
                        lambda c, name=name: c["services"][name].update(command=[]),
                    ),
                    (
                        f"{name} empty entrypoint override",
                        lambda c, name=name: c["services"][name].update(entrypoint=""),
                    ),
                    (
                        f"{name} omitted normalized command",
                        lambda c, name=name: c["services"][name].pop("command"),
                    ),
                    (
                        f"{name} seccomp escape",
                        lambda c, name=name: c["services"][name].update(
                            security_opt=[
                                "no-new-privileges:true",
                                "seccomp=unconfined",
                            ]
                        ),
                    ),
                    (
                        f"{name} apparmor escape",
                        lambda c, name=name: c["services"][name].update(
                            security_opt=[
                                "no-new-privileges:true",
                                "apparmor=unconfined",
                            ]
                        ),
                    ),
                    (
                        f"{name} duplicate security option",
                        lambda c, name=name: c["services"][name].update(
                            security_opt=[
                                "no-new-privileges:true",
                                "no-new-privileges:true",
                            ]
                        ),
                    ),
                )
            )

        for field, value in (
            ("ports", []),
            ("privileged", False),
            ("cap_add", []),
            ("devices", []),
            ("deploy", {}),
            ("env_file", []),
            ("labels", {"owner": "depositar"}),
            ("profiles", []),
            ("working_dir", "/tmp"),
            ("post_start", [{"command": ["sh", "-c", "id"]}]),
            ("pre_stop", [{"command": ["sh", "-c", "id"]}]),
        ):
            mutations.append(
                (
                    f"unexpected service field {field}",
                    lambda c, field=field, value=value: c["services"][
                        "fess-search"
                    ].update({field: copy.deepcopy(value)}),
                )
            )

        self.assert_mutations_fail(tuple(mutations))

    def test_environment_keys_and_values_are_exact_for_every_service(self) -> None:
        mutations: list[Mutation] = []
        baseline = valid_config()
        for name in ("fess-search", "opensearch-search", "ollama-embedding"):
            environment = baseline["services"][name]["environment"]
            for key in environment:
                mutations.extend(
                    (
                        (
                            f"{name} missing environment key {key}",
                            lambda c, name=name, key=key: c["services"][name][
                                "environment"
                            ].pop(key),
                        ),
                        (
                            f"{name} changed environment value {key}",
                            lambda c, name=name, key=key: c["services"][name][
                                "environment"
                            ].update(
                                {
                                    key: c["services"][name]["environment"][key]
                                    + "-mutated"
                                }
                            ),
                        ),
                    )
                )

        mutations.extend(
            (
                (
                    "Fess JAVA_TOOL_OPTIONS injection",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        JAVA_TOOL_OPTIONS="-Djavax.net.ssl.trustStore=/tmp/evil"
                    ),
                ),
                (
                    "OpenSearch _JAVA_OPTIONS injection",
                    lambda c: c["services"]["opensearch-search"]["environment"].update(
                        _JAVA_OPTIONS="-Dplugins.security.disabled=true"
                    ),
                ),
                (
                    "Ollama origins expansion",
                    lambda c: c["services"]["ollama-embedding"]["environment"].update(
                        OLLAMA_ORIGINS="*"
                    ),
                ),
                (
                    "proxy environment injection",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        HTTP_PROXY="http://127.0.0.1:3128"
                    ),
                ),
            )
        )

        self.assert_mutations_fail(tuple(mutations))

    def test_runtime_user_is_explicit_shared_and_non_root(self) -> None:
        self.assert_mutations_fail(
            (
                (
                    "missing user",
                    lambda c: c["services"]["fess-search"].pop("user"),
                ),
                (
                    "root user",
                    lambda c: c["services"]["fess-search"].update(user="0:0"),
                ),
                (
                    "symbolic user",
                    lambda c: c["services"]["fess-search"].update(user="fess:fess"),
                ),
                (
                    "different UID",
                    lambda c: c["services"]["opensearch-search"].update(
                        user=f"{int(RUNTIME_UID) + 1}:{RUNTIME_GID}"
                    ),
                ),
            )
        )

    def test_every_service_requires_resource_log_and_health_bounds(self) -> None:
        mutations: list[Mutation] = []
        for name in ("fess-search", "opensearch-search", "ollama-embedding"):
            mutations.extend(
                (
                    (
                        f"{name} CPU",
                        lambda c, name=name: c["services"][name].update(cpus=0),
                    ),
                    (
                        f"{name} CPU below reviewed range",
                        lambda c, name=name: c["services"][name].update(cpus=0.5),
                    ),
                    (
                        f"{name} CPU above reviewed range",
                        lambda c, name=name: c["services"][name].update(cpus=99),
                    ),
                    (
                        f"{name} non-finite CPU",
                        lambda c, name=name: c["services"][name].update(
                            cpus=float("nan")
                        ),
                    ),
                    (
                        f"{name} memory",
                        lambda c, name=name: c["services"][name].pop("mem_limit"),
                    ),
                    (
                        f"{name} memory below reviewed range",
                        lambda c, name=name: c["services"][name].update(
                            mem_limit=1024**3, memswap_limit=1024**3
                        ),
                    ),
                    (
                        f"{name} memory above reviewed range",
                        lambda c, name=name: c["services"][name].update(
                            mem_limit=128 * 1024**3, memswap_limit=128 * 1024**3
                        ),
                    ),
                    (
                        f"{name} swap missing",
                        lambda c, name=name: c["services"][name].pop("memswap_limit"),
                    ),
                    (
                        f"{name} swap can grow",
                        lambda c, name=name: c["services"][name].update(
                            memswap_limit=16 * 1024**3
                        ),
                    ),
                    (
                        f"{name} PIDs",
                        lambda c, name=name: c["services"][name].update(pids_limit=0),
                    ),
                    (
                        f"{name} PIDs above reviewed range",
                        lambda c, name=name: c["services"][name].update(
                            pids_limit=99999
                        ),
                    ),
                    (
                        f"{name} boolean resource",
                        lambda c, name=name: c["services"][name].update(cpus=True),
                    ),
                    (
                        f"{name} restart policy",
                        lambda c, name=name: c["services"][name].update(
                            restart="always"
                        ),
                    ),
                    (
                        f"{name} init disabled",
                        lambda c, name=name: c["services"][name].update(init=False),
                    ),
                    (
                        f"{name} logging",
                        lambda c, name=name: c["services"][name].pop("logging"),
                    ),
                    (
                        f"{name} log bytes",
                        lambda c, name=name: c["services"][name]["logging"][
                            "options"
                        ].update(**{"max-size": "1g"}),
                    ),
                    (
                        f"{name} log files",
                        lambda c, name=name: c["services"][name]["logging"][
                            "options"
                        ].update(**{"max-file": "100"}),
                    ),
                    (
                        f"{name} health",
                        lambda c, name=name: c["services"][name].pop("healthcheck"),
                    ),
                    (
                        f"{name} health disabled",
                        lambda c, name=name: c["services"][name]["healthcheck"].update(
                            disable=True
                        ),
                    ),
                )
            )
        self.assert_mutations_fail(tuple(mutations))

    def test_every_service_requires_a_read_only_root_filesystem(self) -> None:
        mutations: list[Mutation] = []
        for name in ("fess-search", "opensearch-search", "ollama-embedding"):
            mutations.extend(
                (
                    (
                        f"{name} writable root",
                        lambda c, name=name: c["services"][name].update(
                            read_only=False
                        ),
                    ),
                    (
                        f"{name} implicit writable root",
                        lambda c, name=name: c["services"][name].pop("read_only"),
                    ),
                )
            )
        self.assert_mutations_fail(tuple(mutations))

    def test_volume_allowlist_is_exact_for_every_service(self) -> None:
        def remove_read_only(config: dict[str, Any]) -> None:
            config["services"]["fess-search"]["volumes"][0].pop("read_only")

        def remove_create_host_path(config: dict[str, Any]) -> None:
            config["services"]["fess-search"]["volumes"][0]["bind"].pop(
                "create_host_path"
            )

        mutations: list[Mutation] = []
        for name in ("fess-search", "opensearch-search", "ollama-embedding"):
            mutations.extend(
                (
                    (
                        f"{name} missing allowed mount",
                        lambda c, name=name: c["services"][name]["volumes"].pop(),
                    ),
                    (
                        f"{name} extra bind mount",
                        lambda c, name=name: c["services"][name]["volumes"].append(
                            bind_mount("unexpected", "/unexpected", read_only=True)
                        ),
                    ),
                    (
                        f"{name} Compose config key",
                        lambda c, name=name: c["services"][name].update(configs=[]),
                    ),
                    (
                        f"{name} volumes_from key",
                        lambda c, name=name: c["services"][name].update(
                            volumes_from=[]
                        ),
                    ),
                )
            )
        mutations.extend(
            (
                (
                    "changed mount source",
                    lambda c: c["services"]["fess-search"]["volumes"][0].update(
                        source=f"{CATALOG_ROOT}/opensearch/config/other-ca.pem"
                    ),
                ),
                (
                    "changed mount target",
                    lambda c: c["services"]["fess-search"]["volumes"][0].update(
                        target="/tmp/root-ca.pem"
                    ),
                ),
                (
                    "named volume type",
                    lambda c: c["services"]["fess-search"]["volumes"][0].update(
                        type="volume"
                    ),
                ),
                ("removed read-only flag", remove_read_only),
                (
                    "writable config mount",
                    lambda c: c["services"]["fess-search"]["volumes"][0].update(
                        read_only=False
                    ),
                ),
                (
                    "nonboolean read-only field on writable data",
                    lambda c: c["services"]["opensearch-search"]["volumes"][0].update(
                        read_only=0
                    ),
                ),
                ("missing create_host_path", remove_create_host_path),
                (
                    "auto-created host path",
                    lambda c: c["services"]["fess-search"]["volumes"][0]["bind"].update(
                        create_host_path=True
                    ),
                ),
                (
                    "extra bind option",
                    lambda c: c["services"]["fess-search"]["volumes"][0]["bind"].update(
                        propagation="rshared"
                    ),
                ),
                (
                    "extra mount field",
                    lambda c: c["services"]["fess-search"]["volumes"][0].update(
                        consistency="cached"
                    ),
                ),
                (
                    "top-level named volume key",
                    lambda c: c.update(volumes={}),
                ),
                (
                    "top-level Compose config key",
                    lambda c: c.update(configs={}),
                ),
            )
        )
        self.assert_mutations_fail(tuple(mutations))

    def test_tmpfs_allowlist_is_exact_and_native_exec_is_scoped(self) -> None:
        mutations: list[Mutation] = []
        for name in ("fess-search", "opensearch-search", "ollama-embedding"):
            mutations.extend(
                (
                    (
                        f"{name} missing tmpfs",
                        lambda c, name=name: c["services"][name]["tmpfs"].pop(),
                    ),
                    (
                        f"{name} extra tmpfs",
                        lambda c, name=name: c["services"][name]["tmpfs"].append(
                            "/run/extra:rw,noexec,nosuid,nodev,size=1m,mode=0700"
                        ),
                    ),
                )
            )
        mutations.extend(
            (
                (
                    "general OpenSearch tmp is executable",
                    lambda c: c["services"]["opensearch-search"]["tmpfs"].__setitem__(
                        0,
                        c["services"]["opensearch-search"]["tmpfs"][0].replace(
                            "noexec", "exec"
                        ),
                    ),
                ),
                (
                    "native OpenSearch tmp is noexec",
                    lambda c: c["services"]["opensearch-search"]["tmpfs"].__setitem__(
                        1,
                        c["services"]["opensearch-search"]["tmpfs"][1].replace(
                            ":rw,exec,", ":rw,noexec,"
                        ),
                    ),
                ),
                (
                    "native OpenSearch tmp is too large",
                    lambda c: c["services"]["opensearch-search"]["tmpfs"].__setitem__(
                        1,
                        c["services"]["opensearch-search"]["tmpfs"][1].replace(
                            "size=64m", "size=1g"
                        ),
                    ),
                ),
                (
                    "tmpfs runtime owner changed",
                    lambda c: c["services"]["ollama-embedding"]["tmpfs"].__setitem__(
                        0,
                        c["services"]["ollama-embedding"]["tmpfs"][0].replace(
                            f"uid={RUNTIME_UID}", f"uid={int(RUNTIME_UID) + 1}"
                        ),
                    ),
                ),
            )
        )
        self.assert_mutations_fail(tuple(mutations))

    def test_images_build_inputs_and_runtime_entrypoints_are_exact(self) -> None:
        self.assert_mutations_fail(
            (
                (
                    "unpinned fess base",
                    lambda c: c["services"]["fess-search"]["build"]["args"].update(
                        FESS_BASE_IMAGE="ghcr.io/codelibs/fess:latest"
                    ),
                ),
                (
                    "different digest-pinned fess base",
                    lambda c: c["services"]["fess-search"]["build"]["args"].update(
                        FESS_BASE_IMAGE="evil.invalid/fess@sha256:" + DIGEST_A
                    ),
                ),
                (
                    "unpinned opensearch base",
                    lambda c: c["services"]["opensearch-search"]["build"][
                        "args"
                    ].update(
                        OPENSEARCH_BASE_IMAGE="ghcr.io/codelibs/fess-opensearch:3.8.0"
                    ),
                ),
                (
                    "different digest-pinned opensearch base",
                    lambda c: c["services"]["opensearch-search"]["build"][
                        "args"
                    ].update(
                        OPENSEARCH_BASE_IMAGE="evil.invalid/opensearch@sha256:"
                        + DIGEST_B
                    ),
                ),
                (
                    "unpinned ollama image",
                    lambda c: c["services"]["ollama-embedding"].update(
                        image="ollama/ollama:latest"
                    ),
                ),
                (
                    "different digest-pinned ollama image",
                    lambda c: c["services"]["ollama-embedding"].update(
                        image="evil.invalid/ollama@sha256:" + DIGEST_C
                    ),
                ),
                (
                    "plugin checksum",
                    lambda c: c["services"]["fess-search"]["build"]["args"].update(
                        FESS_OLLAMA_PLUGIN_SHA256=DIGEST_B
                    ),
                ),
                (
                    "plugin URL",
                    lambda c: c["services"]["fess-search"]["build"]["args"].update(
                        FESS_OLLAMA_PLUGIN_URL="https://evil.invalid/plugin.jar"
                    ),
                ),
                (
                    "secret build arg",
                    lambda c: c["services"]["fess-search"]["build"]["args"].update(
                        ADMIN_PASSWORD="plaintext"
                    ),
                ),
                (
                    "fess build context",
                    lambda c: c["services"]["fess-search"]["build"].update(
                        context="/tmp/evil"
                    ),
                ),
                (
                    "opensearch Dockerfile",
                    lambda c: c["services"]["opensearch-search"]["build"].update(
                        dockerfile="Dockerfile.evil"
                    ),
                ),
                (
                    "fess output image",
                    lambda c: c["services"]["fess-search"].update(
                        image="depositar-fess-catalog:unreviewed"
                    ),
                ),
                (
                    "opensearch output image",
                    lambda c: c["services"]["opensearch-search"].update(
                        image="depositar-opensearch-catalog:unreviewed"
                    ),
                ),
                (
                    "ollama local build",
                    lambda c: c["services"]["ollama-embedding"].update(
                        build={
                            "context": "/tmp/evil",
                            "dockerfile": "Dockerfile",
                            "args": {},
                        }
                    ),
                ),
                (
                    "command override",
                    lambda c: c["services"]["fess-search"].update(
                        command=["sleep", "infinity"]
                    ),
                ),
                (
                    "entrypoint override",
                    lambda c: c["services"]["opensearch-search"].update(
                        entrypoint=["/tmp/evil"]
                    ),
                ),
            )
        )

    def test_plaintext_credentials_are_rejected_in_all_compose_channels(self) -> None:
        self.assert_mutations_fail(
            (
                (
                    "password environment",
                    lambda c: c["services"]["opensearch-search"]["environment"].update(
                        OPENSEARCH_INITIAL_ADMIN_PASSWORD="plaintext"
                    ),
                ),
                (
                    "token environment",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_ADMIN_TOKEN="plaintext"
                    ),
                ),
                (
                    "URL userinfo",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        SEARCH_ENGINE_HTTP_URL="https://admin:plaintext@opensearch-search:9200"
                    ),
                ),
                (
                    "password hidden in generic options",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_JAVA_OPTS="-Dsearch_engine.password=plaintext"
                    ),
                ),
                (
                    "command credential",
                    lambda c: c["services"]["opensearch-search"].update(
                        command=["sh", "-c", "use-password plaintext"]
                    ),
                ),
                (
                    "label credential",
                    lambda c: c["services"]["fess-search"].update(
                        labels={"admin-token": "plaintext"}
                    ),
                ),
            )
        )

    def test_only_exact_fess_secret_files_are_accepted(self) -> None:
        def add_admin_secret(config: dict[str, Any]) -> None:
            config["secrets"]["opensearch_admin_password"] = {
                "file": f"{CATALOG_ROOT}/secrets/opensearch_admin_password",
                "name": "depositar-catalog_opensearch_admin_password",
            }
            config["services"]["opensearch-search"]["secrets"] = [
                {
                    "source": "opensearch_admin_password",
                    "target": "opensearch_admin_password",
                }
            ]

        self.assert_mutations_fail(
            (
                (
                    "missing Fess service secret",
                    lambda c: c["services"]["fess-search"]["secrets"].pop(),
                ),
                (
                    "duplicate Fess service secret",
                    lambda c: c["services"]["fess-search"]["secrets"].append(
                        copy.deepcopy(c["services"]["fess-search"]["secrets"][0])
                    ),
                ),
                (
                    "unexpected Fess service secret",
                    lambda c: c["services"]["fess-search"]["secrets"][0].update(
                        source="undefined"
                    ),
                ),
                (
                    "changed secret target",
                    lambda c: c["services"]["fess-search"]["secrets"][0].update(
                        target="renamed-secret"
                    ),
                ),
                (
                    "unsupported file-secret target mode",
                    lambda c: c["services"]["fess-search"]["secrets"][0].update(
                        mode="0440"
                    ),
                ),
                (
                    "extra secret mount field",
                    lambda c: c["services"]["fess-search"]["secrets"][0].update(
                        uid="21599"
                    ),
                ),
                (
                    "missing top-level secret",
                    lambda c: c["secrets"].pop("fess_app_cipher_key"),
                ),
                (
                    "relative top-level secret file",
                    lambda c: c["secrets"]["fess_app_cipher_key"].update(
                        file="./secret"
                    ),
                ),
                (
                    "changed top-level secret name",
                    lambda c: c["secrets"]["fess_app_cipher_key"].update(
                        name="shared-secret"
                    ),
                ),
                (
                    "external top-level secret",
                    lambda c: c["secrets"]["fess_app_cipher_key"].update(external=True),
                ),
                (
                    "OpenSearch receives a Fess secret",
                    lambda c: c["services"]["opensearch-search"].update(
                        secrets=[
                            copy.deepcopy(c["services"]["fess-search"]["secrets"][0])
                        ]
                    ),
                ),
                (
                    "Ollama receives a Fess secret",
                    lambda c: c["services"]["ollama-embedding"].update(
                        secrets=[
                            copy.deepcopy(c["services"]["fess-search"]["secrets"][0])
                        ]
                    ),
                ),
                ("OpenSearch admin password reintroduced", add_admin_secret),
            )
        )

    def test_file_secret_target_mode_is_rejected_as_unsupported(self) -> None:
        config = valid_config()
        config["services"]["fess-search"]["secrets"][0]["mode"] = 0o400
        with self.assertRaisesRegex(ValueError, "secret fields changed"):
            validate(config)

    def test_opensearch_tls_and_security_cannot_be_disabled(self) -> None:
        def mutate_os_java_options(config: dict[str, Any], old: str, new: str) -> None:
            environment = config["services"]["opensearch-search"]["environment"]
            environment["OPENSEARCH_JAVA_OPTS"] = environment[
                "OPENSEARCH_JAVA_OPTS"
            ].replace(old, new)

        self.assert_mutations_fail(
            (
                (
                    "Fess plaintext backend",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        SEARCH_ENGINE_HTTP_URL="http://opensearch-search:9200"
                    ),
                ),
                (
                    "disabled plugin env",
                    lambda c: c["services"]["opensearch-search"]["environment"].update(
                        DISABLE_SECURITY_PLUGIN="true"
                    ),
                ),
                (
                    "disabled plugin option",
                    lambda c: c["services"]["opensearch-search"].update(
                        command=["opensearch", "-Eplugins.security.disabled=true"]
                    ),
                ),
                (
                    "HTTP healthcheck",
                    lambda c: c["services"]["opensearch-search"]["healthcheck"].update(
                        test=["CMD", "curl", "http://127.0.0.1:9200"]
                    ),
                ),
                (
                    "missing native tmpdir property",
                    lambda c: mutate_os_java_options(
                        c, " -Djava.io.tmpdir=/run/opensearch-native", ""
                    ),
                ),
                (
                    "native tmpdir property points elsewhere",
                    lambda c: mutate_os_java_options(
                        c,
                        "-Djava.io.tmpdir=/run/opensearch-native",
                        "-Djava.io.tmpdir=/tmp",
                    ),
                ),
                (
                    "duplicate native tmpdir property",
                    lambda c: c["services"]["opensearch-search"]["environment"].update(
                        OPENSEARCH_JAVA_OPTS=(
                            c["services"]["opensearch-search"]["environment"][
                                "OPENSEARCH_JAVA_OPTS"
                            ]
                            + " -Djava.io.tmpdir=/run/opensearch-native"
                        )
                    ),
                ),
            )
        )

    def test_fess_crawler_chat_login_and_model_policy_is_enforced(self) -> None:
        def mutate_fess_java_options(
            config: dict[str, Any], old: str, new: str
        ) -> None:
            environment = config["services"]["fess-search"]["environment"]
            environment["FESS_JAVA_OPTS"] = environment["FESS_JAVA_OPTS"].replace(
                old, new
            )

        self.assert_mutations_fail(
            (
                (
                    "redirect-tolerant Fess healthcheck",
                    lambda c: c["services"]["fess-search"]["healthcheck"].update(
                        test=[
                            "CMD",
                            "curl",
                            "--fail",
                            "http://127.0.0.1:8080/api/v2/health",
                        ]
                    ),
                ),
                (
                    "crawler marker",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_CRAWLER_ENABLED="true"
                    ),
                ),
                (
                    "missing dictionary path",
                    lambda c: c["services"]["fess-search"]["environment"].pop(
                        "FESS_DICTIONARY_PATH"
                    ),
                ),
                (
                    "mismatched dictionary path",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_DICTIONARY_PATH="/usr/share/opensearch/config/"
                    ),
                ),
                (
                    "scheduler marker",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_SCHEDULER_ENABLED="true"
                    ),
                ),
                (
                    "crawler override",
                    lambda c: mutate_fess_java_options(
                        c,
                        "-Dfess.config.thumbnail.crawler.enabled=false",
                        "-Dfess.config.thumbnail.crawler.enabled=true",
                    ),
                ),
                (
                    "chat marker",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_RAG_CHAT_ENABLED="true"
                    ),
                ),
                (
                    "chat override",
                    lambda c: mutate_fess_java_options(
                        c,
                        "-Dfess.config.rag.chat.enabled=false",
                        "-Dfess.config.rag.chat.enabled=true",
                    ),
                ),
                (
                    "login disabled",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_LOGIN_REQUIRED="false"
                    ),
                ),
                (
                    "missing Java login property",
                    lambda c: mutate_fess_java_options(
                        c, " -Dfess.system.login.required=true", ""
                    ),
                ),
                (
                    "false Java login property",
                    lambda c: mutate_fess_java_options(
                        c,
                        "-Dfess.system.login.required=true",
                        "-Dfess.system.login.required=false",
                    ),
                ),
                (
                    "duplicate Java login property",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_JAVA_OPTS=(
                            c["services"]["fess-search"]["environment"][
                                "FESS_JAVA_OPTS"
                            ]
                            + " -Dfess.system.login.required=true"
                        )
                    ),
                ),
                (
                    "malformed Java options",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_JAVA_OPTS='"unterminated'
                    ),
                ),
                (
                    "different embedding model",
                    lambda c: c["services"]["fess-search"]["environment"].update(
                        FESS_SEMANTIC_MODEL="snowflake-arctic-embed2"
                    ),
                ),
                (
                    "second Ollama model",
                    lambda c: c["services"]["ollama-embedding"]["environment"].update(
                        OLLAMA_MODEL="mistral:7b"
                    ),
                ),
                (
                    "Java property model override",
                    lambda c: mutate_fess_java_options(
                        c,
                        "-Dfess.system.content_chunker.embedding.ollama.model=granite-embedding:278m",
                        "-Dfess.system.content_chunker.embedding.ollama.model=mistral:7b",
                    ),
                ),
                (
                    "single vector batch regression",
                    lambda c: mutate_fess_java_options(
                        c,
                        "-Dfess.system.content_chunker.job.concurrency=2",
                        "-Dfess.system.content_chunker.job.concurrency=1",
                    ),
                ),
                (
                    "expanded vector batches",
                    lambda c: mutate_fess_java_options(
                        c,
                        "-Dfess.system.content_chunker.job.concurrency=2",
                        "-Dfess.system.content_chunker.job.concurrency=4",
                    ),
                ),
                (
                    "old vector bulk size",
                    lambda c: mutate_fess_java_options(
                        c,
                        "-Dfess.system.content_chunker.job.bulk_size=100",
                        "-Dfess.system.content_chunker.job.bulk_size=20",
                    ),
                ),
                (
                    "Ollama pull command",
                    lambda c: c["services"]["ollama-embedding"].update(
                        command=["ollama", "pull", "mistral:7b"]
                    ),
                ),
                (
                    "GPT OSS anywhere",
                    lambda c: c["services"]["fess-search"].update(
                        labels={"model": "gpt-oss:20b"}
                    ),
                ),
            )
        )

    def test_ollama_is_loopback_and_bounded_in_fess_namespace(self) -> None:
        self.assert_mutations_fail(
            (
                (
                    "wildcard bind",
                    lambda c: c["services"]["ollama-embedding"]["environment"].update(
                        OLLAMA_HOST="0.0.0.0:11434"
                    ),
                ),
                (
                    "separate network",
                    lambda c: c["services"]["ollama-embedding"].update(
                        network_mode=None, networks={"search_backend": None}
                    ),
                ),
                (
                    "wrong shared namespace",
                    lambda c: c["services"]["ollama-embedding"].update(
                        network_mode="service:opensearch-search"
                    ),
                ),
                (
                    "visible GPU",
                    lambda c: c["services"]["ollama-embedding"]["environment"].update(
                        NVIDIA_VISIBLE_DEVICES="all"
                    ),
                ),
                (
                    "CUDA GPU",
                    lambda c: c["services"]["ollama-embedding"]["environment"].update(
                        CUDA_VISIBLE_DEVICES="0"
                    ),
                ),
                (
                    "parallel embeddings",
                    lambda c: c["services"]["ollama-embedding"]["environment"].update(
                        OLLAMA_NUM_PARALLEL="4"
                    ),
                ),
                (
                    "expanded embedding queue",
                    lambda c: c["services"]["ollama-embedding"]["environment"].update(
                        OLLAMA_MAX_QUEUE="4"
                    ),
                ),
                (
                    "multiple loaded models",
                    lambda c: c["services"]["ollama-embedding"]["environment"].update(
                        OLLAMA_MAX_LOADED_MODELS="2"
                    ),
                ),
            )
        )

    def test_opensearch_data_is_explicit_persistent_and_safe(self) -> None:
        def data_mount(config: dict[str, Any]) -> dict[str, Any]:
            return config["services"]["opensearch-search"]["volumes"][0]

        self.assert_mutations_fail(
            (
                (
                    "missing data",
                    lambda c: c["services"]["opensearch-search"]["volumes"].pop(0),
                ),
                (
                    "tmpfs data",
                    lambda c: data_mount(c).update(type="tmpfs"),
                ),
                (
                    "read-only data",
                    lambda c: data_mount(c).update(read_only=True),
                ),
                (
                    "relative data source",
                    lambda c: data_mount(c).update(source="./opensearch/data"),
                ),
                (
                    "auto-created data source",
                    lambda c: data_mount(c).update(
                        type="bind",
                        source=CATALOG_ROOT + "/opensearch/data",
                        bind={"create_host_path": True},
                    ),
                ),
                (
                    "duplicate data mount",
                    lambda c: c["services"]["opensearch-search"]["volumes"].append(
                        copy.deepcopy(data_mount(c))
                    ),
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
