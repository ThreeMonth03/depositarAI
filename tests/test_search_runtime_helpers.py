from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
HEALTHCHECK_PATH = ROOT / "deploy/docker/opensearch/opensearch_healthcheck.py"


def load_healthcheck_module():
    spec = importlib.util.spec_from_file_location(
        "depositar_opensearch_healthcheck", HEALTHCHECK_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load OpenSearch healthcheck")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse(io.BytesIO):
    def __init__(
        self,
        value: bytes,
        *,
        status: int = 200,
        url: str = "https://127.0.0.1:9200/_plugins/_security/health",
    ) -> None:
        super().__init__(value)
        self.status = status
        self.url = url

    def geturl(self) -> str:
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


class OpenSearchHealthcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_healthcheck_module()

    def test_only_reviewed_loopback_https_endpoint_is_accepted(self) -> None:
        with mock.patch.object(
            self.module.sys, "argv", ["check", "http://127.0.0.1:9200"]
        ):
            self.assertEqual(2, self.module.main())

    def test_ca_verified_security_plugin_up_is_healthy(self) -> None:
        response = FakeResponse(b'{"message":null,"mode":"strict","status":"UP"}')
        with (
            mock.patch.object(
                self.module.sys, "argv", ["check", "https://127.0.0.1:9200"]
            ),
            mock.patch.object(
                self.module.ssl, "create_default_context", return_value=object()
            ) as create_context,
            mock.patch.object(
                self.module.urllib.request,
                "urlopen",
                return_value=response,
            ) as urlopen,
        ):
            self.assertEqual(0, self.module.main())
        create_context.assert_called_once_with(cafile=str(self.module.CA_PATH))
        request = urlopen.call_args.args[0]
        self.assertEqual("https", request.type)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(self.module.HEALTH_URL, request.full_url)
        self.assertTrue(response.closed)

    def test_unauthorized_cluster_response_is_never_treated_as_ready(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                error = urllib.error.HTTPError(
                    self.module.HEALTH_URL,
                    status,
                    "access denied",
                    None,
                    None,
                )
                with (
                    mock.patch.object(
                        self.module.sys,
                        "argv",
                        ["check", "https://127.0.0.1:9200"],
                    ),
                    mock.patch.object(
                        self.module.ssl, "create_default_context", return_value=object()
                    ),
                    mock.patch.object(
                        self.module.urllib.request,
                        "urlopen",
                        side_effect=error,
                    ),
                ):
                    self.assertEqual(1, self.module.main())

    def test_malformed_down_redirected_or_oversized_response_is_unhealthy(self) -> None:
        responses = (
            FakeResponse(b"{}"),
            FakeResponse(b'{"status":"DOWN"}'),
            FakeResponse(b'{"status":"up"}'),
            FakeResponse(b'{"status":"UP","status":"DOWN"}'),
            FakeResponse(b'["UP"]'),
            FakeResponse(b'{"status":"UP"}', status=201),
            FakeResponse(b'{"status":"UP"}', url="https://127.0.0.1:9200/redirected"),
            FakeResponse(b"x" * (4096 + 1)),
        )
        for response in responses:
            with (
                self.subTest(value=response.getvalue()[:40]),
                mock.patch.object(
                    self.module.sys,
                    "argv",
                    ["check", "https://127.0.0.1:9200"],
                ),
                mock.patch.object(
                    self.module.ssl, "create_default_context", return_value=object()
                ),
                mock.patch.object(
                    self.module.urllib.request, "urlopen", return_value=response
                ),
            ):
                self.assertEqual(1, self.module.main())

    def test_http_or_transport_failure_is_unhealthy(self) -> None:
        errors = (
            urllib.error.HTTPError(
                self.module.HEALTH_URL,
                500,
                "failure",
                None,
                None,
            ),
            urllib.error.URLError("offline"),
        )
        for error in errors:
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(
                    self.module.sys,
                    "argv",
                    ["check", "https://127.0.0.1:9200"],
                ),
                mock.patch.object(
                    self.module.ssl, "create_default_context", return_value=object()
                ),
                mock.patch.object(
                    self.module.urllib.request, "urlopen", side_effect=error
                ),
            ):
                self.assertEqual(1, self.module.main())


class SearchRuntimeStaticTests(unittest.TestCase):
    def test_authenticated_queries_receive_only_the_shared_catalog_role(self) -> None:
        script = (ROOT / "deploy/docker/fess/entrypoint.sh").read_text(encoding="utf-8")
        self.assertEqual(
            1,
            script.count(
                'set_property "$override_dir/fess_config.properties" '
                "role.search.default.permissions '{role}depositar_catalog'"
            ),
        )
        self.assertEqual(
            1,
            script.count(
                'set_property "$override_dir/fess_config.properties" '
                "query.additional.response.fields "
                "depositar_bundle_id,depositar_document_sha256,content_chunk_status"
            ),
        )
        self.assertEqual(
            1,
            script.count(
                'set_property "$override_dir/fess_config.properties" '
                "paging.page.size 25"
            ),
        )
        compose = (ROOT / "deploy/compose/compose.catalog.yaml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(1, compose.count("-Dfess.config.paging.page.size=25"))
        self.assertEqual(1, compose.count("-Duser.timezone=UTC"))
        self.assertIn("Fess JVM timezone property must appear exactly once", script)
        self.assertIn("conflicting Fess JVM timezone property", script)
        self.assertNotIn(
            "query.additional.api.response.fields=depositar_bundle_id", script
        )
        self.assertNotIn(
            "query.additional.api.response.fields=depositar_document_sha256", script
        )
        self.assertEqual(
            1,
            script.count(
                'set_property "$override_dir/fess_config.properties" '
                "query.additional.sort.fields url"
            ),
        )
        self.assertEqual(
            1,
            script.count(
                'set_property "$override_dir/fess_config.properties" '
                "virtual.host.headers 'X-Depositar-Catalog:catalog=depositar_catalog'"
            ),
        )

    def test_fess_secrets_are_read_from_files_not_argv_or_environment(self) -> None:
        script = (ROOT / "deploy/docker/fess/entrypoint.sh").read_text(encoding="utf-8")
        for name in (
            "fess_opensearch_password",
            "fess_app_cipher_key",
            "fess_initial_admin_password",
        ):
            self.assertIn(f"/run/secrets/{name}", script)
        self.assertIn("set_secret_property", script)
        self.assertNotIn("SEARCH_ENGINE_PASSWORD=", script)
        self.assertNotIn("-Dfess.config.search_engine.password=", script)

    def test_security_templates_have_no_demo_credentials_or_disable_switch(
        self,
    ) -> None:
        template_dir = ROOT / "deploy/config/opensearch"
        rendered = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(template_dir.iterdir())
        ).lower()
        self.assertNotIn("admin/admin", rendered)
        self.assertNotIn("plugins.security.disabled", rendered)
        self.assertNotIn("disable_security_plugin", rendered)
        self.assertIn("__admin_hash__", rendered)
        self.assertIn("__fess_hash__", rendered)
        self.assertIn("plugins.security.ssl.http.enabled: true", rendered)

    def test_security_rest_api_is_enabled_only_for_the_operator_role(self) -> None:
        template_dir = ROOT / "deploy/config/opensearch"
        config = (template_dir / "opensearch.yml").read_text(encoding="utf-8")
        key = "plugins.security.restapi.roles_enabled"
        self.assertEqual(
            [key + ": [all_access]"],
            [line for line in config.splitlines() if key in line],
        )
        self.assertNotIn("plugins.security.restapi.admin.enabled", config)
        roles = (template_dir / "roles.yml").read_text(encoding="utf-8")
        self.assertNotIn("restapi:admin", roles)
        mapping = (template_dir / "roles_mapping.yml").read_text(encoding="utf-8")
        for role, backend in (
            ("all_access", "admin"),
            ("fess_backend", "fess_backend"),
        ):
            with self.subTest(role=role):
                blocks = re.findall(rf"(?ms)^{role}:\n(.*?)(?=^\S|\Z)", mapping)
                self.assertEqual(1, len(blocks))
                self.assertEqual(
                    ["  reserved: false", "  backend_roles:", "    - " + backend],
                    [line for line in blocks[0].splitlines() if line],
                )

    def test_fess_identity_cannot_inherit_the_operator_backend_role(self) -> None:
        users = (ROOT / "deploy/config/opensearch/internal_users.yml.in").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("opendistro_security_roles", users)
        for user, backend in (("admin", "admin"), ("fess", "fess_backend")):
            with self.subTest(user=user):
                blocks = re.findall(rf"(?ms)^{user}:\n(.*?)(?=^\S|\Z)", users)
                self.assertEqual(1, len(blocks))
                backend_roles = re.findall(
                    r"(?m)^  backend_roles:\n((?:    - [^\n]*\n)+)", blocks[0]
                )
                self.assertEqual(["    - " + backend + "\n"], backend_roles)

    def test_configsync_dictionary_path_index_and_role_are_exact(self) -> None:
        dictionary_path = "/usr/share/opensearch/config/dictionary/"
        opensearch = (ROOT / "deploy/config/opensearch/opensearch.yml").read_text(
            encoding="utf-8"
        )
        roles = (ROOT / "deploy/config/opensearch/roles.yml").read_text(
            encoding="utf-8"
        )
        compose = (ROOT / "deploy/compose/compose.catalog.yaml").read_text(
            encoding="utf-8"
        )
        entrypoint = (ROOT / "deploy/docker/fess/entrypoint.sh").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            1,
            opensearch.count(f"configsync.config_path: {dictionary_path}"),
        )
        self.assertEqual(1, opensearch.count("configsync.index: configsync"))
        for composite_permission in (
            "indices:data/write/bulk",
            "indices:data/read/scroll",
            "indices:data/read/scroll/clear",
        ):
            self.assertEqual(1, roles.count(f'- "{composite_permission}"'))
        self.assertRegex(roles, re.compile(r'^\s*- "configsync"$', re.MULTILINE))
        self.assertNotRegex(
            roles,
            re.compile(r'^\s*- ["\']?\.configsync["\']?$', re.MULTILINE),
        )
        self.assertEqual(
            1,
            compose.count(f"FESS_DICTIONARY_PATH: {dictionary_path}"),
        )
        self.assertIn(
            "[[ ${FESS_DICTIONARY_PATH:-} == " + dictionary_path + " ]]",
            entrypoint,
        )
        self.assertIn("export FESS_DICTIONARY_PATH", entrypoint)

    def test_fess_15_8_scheduler_seed_has_all_jobs_fail_closed(self) -> None:
        bulk_path = ROOT / "deploy/config/fess/scheduled_job.bulk"
        lines = bulk_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(28, len(lines))

        expected_ids = {
            "default_crawler",
            "suggest_indexer",
            "log_aggregator",
            "log_purger",
            "doc_purger",
            "thumbnail_generate",
            "thumbnail_purger",
            "reload_config",
            "ping_es",
            "score_booster",
            "label_updater",
            "index_exporter",
            "log_notification",
            "content-chunk-vector-indexer",
        }
        ids: set[str] = set()
        orders: list[int] = []
        for offset in range(0, len(lines), 2):
            action = json.loads(lines[offset])
            document = json.loads(lines[offset + 1])
            self.assertEqual({"index"}, set(action))
            self.assertEqual("fess_config.scheduled_job", action["index"]["_index"])
            ids.add(action["index"]["_id"])
            self.assertIs(document["available"], False)
            self.assertEqual("", document["cronExpression"])
            orders.append(document["sortOrder"])
        self.assertEqual(expected_ids, ids)
        self.assertEqual(list(range(1, 15)), orders)

        digest = hashlib.sha256(bulk_path.read_bytes()).hexdigest()
        dockerfile = (ROOT / "deploy/docker/fess/Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn(digest, dockerfile)
        self.assertIn(
            "fess_indices/fess_config.scheduled_job/scheduled_job.bulk",
            dockerfile,
        )

    def test_existing_scheduler_guard_is_authenticated_and_fail_closed(self) -> None:
        script = (ROOT / "deploy/docker/fess/entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("FESS_SCHEDULER_ENABLED", script)
        self.assertIn("fess_config.scheduled_job", script)
        self.assertIn("/_update_by_query?", script)
        self.assertIn("/_count?filter_path=count", script)
        self.assertIn("for attempt in {1..24}; do", script)
        self.assertIn("for attempt in {1..12}; do", script)
        self.assertIn("opensearch_post_json_status", script)
        self.assertIn("429 | 502 | 503 | 504", script)
        self.assertIn("unexpected OpenSearch scheduler-update HTTP status", script)
        self.assertIn(
            "OpenSearch scheduler primary shard did not become readable",
            script,
        )
        self.assertLess(
            script.index("OpenSearch scheduler primary shard did not become readable"),
            script.index("/_update_by_query?"),
        )
        self.assertIn("--cacert /run/opensearch-ca/root-ca.pem", script)
        self.assertIn("--config -", script)
        self.assertIn("--noproxy '*'", script)
        self.assertNotIn("--user", script)
        self.assertRegex(
            script,
            re.compile(
                r"disable_existing_scheduled_jobs\s*\n\s*\n\s*fess_pid=", re.MULTILINE
            ),
        )
        self.assertIn(
            'set_property "$override_dir/fess_config.properties" job.max.crawler.processes 1',
            script,
        )
        self.assertIn(
            'set_property "$override_dir/fess_config.properties" crawler.http.thread_pool.size 1',
            script,
        )
        self.assertNotIn("job.max.crawler.processes 0", script)
        self.assertNotIn("crawler.http.thread_pool.size 0", script)

    def test_scheduler_readiness_json_regex_is_executable_bash(self) -> None:
        script = (ROOT / "deploy/docker/fess/entrypoint.sh").read_text(encoding="utf-8")
        match = re.search(
            r"local ready_response_regex='([^']+)'",
            script,
        )
        self.assertIsNotNone(match)
        readiness_regex = match.group(1)
        for value, expected in (
            ('{"count":14}', True),
            ('{"count":0}', True),
            ('{"count":-1}', False),
            ('{"count":14,"timed_out":false}', False),
            ('{"count":"14"}', False),
        ):
            with self.subTest(value=value):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        '[[ "$1" =~ $2 ]]',
                        "bash",
                        value,
                        readiness_regex,
                    ],
                    check=False,
                    timeout=5,
                )
                self.assertEqual(expected, result.returncode == 0)


if __name__ == "__main__":
    unittest.main()
