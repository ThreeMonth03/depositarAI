"""Opt-in real OIDC, HTTPS cookie and durable admission checks without a GPU.

Uses only disposable Compose resources and existing explicitly selected images.
No host port is published and every network is internal.
"""

from __future__ import annotations

from collections import Counter
import json
import ipaddress
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from search.operations.deployment.public_entry.profile import (
    APISIX_IMAGE,
    REDIS_IMAGE,
    validate_public_entry_config,
    write_public_entry,
)


ROOT = Path(__file__).resolve().parents[3]
NGINX_IMAGE = (
    "nginx@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae6"
    "50cd66104c"
)
ORIGIN = "https://public.test:9443"
ISSUER = "https://oidc.test:9443"


def _install_counter_reader(root):
    """Observe only the disposable management fixture's admission keys."""
    path = root / "public-entry/redis-start.sh"
    script = path.read_text()
    marker = "unset password digest\n"
    if script.count(marker) != 1:
        raise AssertionError("fixture Redis startup changed")
    observer = (
        "printf 'user fixture-counter-reader on #%s "
        "~plugin-limit-count:v1:/routes/turn:* "
        "~plugin-limit-count:v1:/routes/session-create:* "
        "-@all +auth +ping +eval +scan +get\\n' "
        '"$digest" >> /tmp/users.acl\n'
    )
    path.write_text(script.replace(marker, observer + marker))


@unittest.skipUnless(
    os.environ.get("DEPOSITAR_TEST_PUBLIC_ENTRY_RUNTIME") == "1",
    "real public-entry runtime requires an explicit isolated test opt-in",
)
class PublicEntryRuntimeFixture(unittest.TestCase):
    mixed = False
    antibot = False
    counter_observer = False
    challenge_lifetime = "24h"

    @classmethod
    def command(cls, argv, *, timeout=30, check=True):
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode:
            # Command arguments contain paths and test fixture values only.
            diagnostic = result.stderr[-2000:]
            for secret in (
                "fixture-oidc-client-secret",
                *(c * 64 for c in "cdef"),
            ):
                diagnostic = diagnostic.replace(secret, "[redacted]")
            raise AssertionError(
                "isolated runtime command failed: "
                + " ".join(argv[:5])
                + "\n"
                + diagnostic
            )
        return result

    @classmethod
    def compose(cls, *arguments, timeout=30):
        return cls.command(
            [
                "docker",
                "compose",
                "--project-name",
                cls.project,
                "--file",
                str(cls.compose_file),
                *arguments,
            ],
            timeout=timeout,
        )

    @classmethod
    def image(cls, variable, default):
        selected = os.environ.get(variable, default)
        result = cls.command(
            ["docker", "image", "inspect", selected, "--format", "{{.Id}}"]
        )
        identifier = result.stdout.strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", identifier) is None:
            raise AssertionError("test requires a local immutable image")
        return identifier

    @classmethod
    def networks(cls):
        """Avoid exhausted default pools without removing any existing
        network.
        """
        pool = ipaddress.ip_network(
            os.environ.get(
                "DEPOSITAR_TEST_PUBLIC_SUBNET_POOL", "198.18.0.0/15"
            )
        )
        if pool.version != 4 or not 8 <= pool.prefixlen <= 24:
            raise AssertionError(
                "test subnet pool must be an IPv4 /8 through /24"
            )
        used = []
        identifiers = cls.command(
            ["docker", "network", "ls", "--quiet"]
        ).stdout.split()
        if identifiers:
            networks = json.loads(
                cls.command(
                    ["docker", "network", "inspect", *identifiers]
                ).stdout
            )
            used.extend(
                ipaddress.ip_network(item["Subnet"])
                for network in networks
                for item in (network["IPAM"].get("Config") or [])
                if item.get("Subnet")
            )
        if shutil.which("ip"):
            routes = json.loads(
                cls.command(["ip", "-j", "route", "show"]).stdout
            )
            used.extend(
                ipaddress.ip_network(route["dst"], strict=False)
                for route in routes
                if route.get("dst", "default") != "default"
            )
        count = 1 << (28 - pool.prefixlen)
        start = uuid.uuid4().int % count
        selected = []
        for offset in range(min(count, 4096)):
            address = (
                int(pool.network_address) + ((start + offset) % count) * 16
            )
            candidate = ipaddress.ip_network((address, 28))
            if any(candidate.overlaps(existing) for existing in used):
                continue
            selected.append(candidate)
            if len(selected) == (4 if cls.antibot else 3):
                break
        if len(selected) != (4 if cls.antibot else 3):
            raise AssertionError(
                "no three nonoverlapping disposable test subnets available"
            )
        return {
            name: {
                "internal": True,
                "ipam": {"config": [{"subnet": str(subnet)}]},
            }
            for name, subnet in zip(
                ("website", "quota", "outbound", "antibot-gateway"), selected
            )
        }

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="depositar-public-runtime-"
        )
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        root.chmod(0o700)
        # Persistence scenarios must stay inside one epoch-aligned window.
        # Boundary resets are covered separately by the deterministic Lua test.
        now = int(time.time())
        cls.quota_window_seconds = next(
            seconds
            for seconds in range(3600, 86401)
            if seconds - now % seconds >= 900
        )
        cls.project = "depositar-public-test-" + uuid.uuid4().hex[:12]
        cls.compose_file = root / "runtime-compose.json"
        uid, gid = os.geteuid(), os.getegid()
        if uid == 0:
            raise unittest.SkipTest(
                "runtime fixture must run as a non-root operator"
            )
        images = {
            "apisix": cls.image("DEPOSITAR_TEST_APISIX_IMAGE", APISIX_IMAGE),
            "redis": cls.image("DEPOSITAR_TEST_REDIS_IMAGE", REDIS_IMAGE),
            "web": cls.image(
                "DEPOSITAR_TEST_PUBLIC_WEB_IMAGE", "depositar-search-web:ci"
            ),
            "python": cls.image(
                "DEPOSITAR_TEST_PUBLIC_FIXTURE_IMAGE",
                "depositar-search-api:ci",
            ),
            "nginx": cls.image("DEPOSITAR_TEST_PUBLIC_TLS_IMAGE", NGINX_IMAGE),
            "frontend": cls.image(
                "DEPOSITAR_TEST_PUBLIC_FRONTEND_IMAGE",
                "depositar-search-web-nginx:ci",
            ),
        }
        for name in ("idp", "attacker"):
            cls.command(
                [
                    "openssl",
                    "genpkey",
                    "-algorithm",
                    "RSA",
                    "-pkeyopt",
                    "rsa_keygen_bits:2048",
                    "-out",
                    str(root / (name + ".key")),
                ]
            )
        cls.command(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(root / "tls.key"),
                "-out",
                str(root / "tls.crt"),
                "-subj",
                "/CN=public-entry-fixture",
                "-addext",
                "subjectAltName=DNS:public.test,DNS:oidc.test",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ]
        )
        secrets = {
            "client-secret": "fixture-oidc-client-secret",
            "session-secret": "e" * 64,
            "edge-key": "d" * 64,
            "redis-password": "f" * 64,
            "api-key": "c" * 64,
            "guest-secret": "b" * 64,
            "antibot-key": "a" * 64,
        }
        for name, value in secrets.items():
            (root / name).write_text(value + "\n")
        for path in root.iterdir():
            path.chmod(0o600)
        config = validate_public_entry_config(
            {
                "schema_version": 1,
                "state_root": str(root),
                "public_origin": ORIGIN,
                "oidc_issuer": ISSUER,
                "oidc_discovery": ISSUER + "/.well-known/openid-configuration",
                "oidc_client_id": "public-entry-test",
                "oidc_client_secret_file": str(root / "client-secret"),
                "oidc_session_secret_file": str(root / "session-secret"),
                "edge_secret_file": str(root / "edge-key"),
                "redis_password_file": str(root / "redis-password"),
                "oidc_ca_file": str(root / "tls.crt"),
                "quota_per_account": 3 if cls.mixed else 2,
                "quota_global": 6 if cls.mixed else 4,
                **(
                    {
                        "access_mode": "mixed",
                        "guest_session_secret_file": str(
                            root / "guest-secret"
                        ),
                        "quota_per_guest": 1,
                        "quota_guest_pool": 2,
                        "quota_user_pool": 4,
                    }
                    if cls.mixed
                    else {}
                ),
                "quota_window_seconds": cls.quota_window_seconds,
                "session_window_seconds": cls.quota_window_seconds,
                **(
                    {
                        "antibot_enabled": True,
                        "antibot_signing_key_file": str(root / "antibot-key"),
                    }
                    if cls.antibot
                    else {}
                ),
            }
        )
        plan = write_public_entry(config, uid=uid, gid=gid)
        composition = plan["compose"]
        composition["name"] = cls.project
        composition["networks"] = cls.networks()
        services = composition["services"]
        services["public-entry"]["image"] = images["apisix"]
        services["public-entry"].pop("ports", None)
        if cls.antibot:
            services["public-edge"].pop("ports", None)
            services["antibot"]["environment"]["COOKIE_EXPIRATION_TIME"] = (
                cls.challenge_lifetime
            )
        services["quota-redis"]["image"] = images["redis"]
        # Check disposable startup promptly while preserving steady-state
        # commands, intervals, retries and the actual HTTPS readiness probe.
        for role in ("quota-redis", "public-entry"):
            services[role]["healthcheck"].update(
                start_period="20s", start_interval="1s"
            )
        common = {
            "user": f"{uid}:{gid}",
            "runtime": "runc",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": "256m",
            "pids_limit": 48,
            "cpus": 1,
            "tmpfs": ["/tmp:rw,nosuid,nodev,size=32m,mode=1777"],
            "environment": {
                "PYTHONDONTWRITEBYTECODE": "1",
                "NVIDIA_VISIBLE_DEVICES": "void",
            },
        }
        mounts = [
            {
                "type": "bind",
                "source": str(ROOT / "search"),
                "target": "/app/search",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": str(root),
                "target": "/fixture",
                "read_only": True,
            },
        ]
        services["fixture"] = {
            **common,
            "image": images["python"],
            "volumes": mounts,
            "entrypoint": [
                "python",
                "-m",
                "search.tests.public_entry.fixture",
                "serve",
            ],
            "networks": {
                "website": {"aliases": ["conversation-api"]},
                "outbound": {"aliases": ["oidc.test", "conversation-api"]},
            },
        }
        services["public-web"] = {
            **common,
            "image": images["web"],
            "environment": {
                **common["environment"],
                "WEB_ORIGIN": ORIGIN,
                "WEB_UPSTREAM": "http://conversation-api:8000",
                "WEB_EDGE_SECRET_FILE": "/run/secrets/edge-key",
            },
            "volumes": [
                *mounts,
                {
                    "type": "bind",
                    "source": str(root / "api-key"),
                    "target": "/run/secrets/api_key",
                    "read_only": True,
                },
                {
                    "type": "bind",
                    "source": str(root / "edge-key"),
                    "target": "/run/secrets/edge-key",
                    "read_only": True,
                },
            ],
            "entrypoint": [
                "python",
                "-m",
                "uvicorn",
                "search.web.app:create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                "--no-access-log",
            ],
            "networks": {"website": {"aliases": ["web"]}},
        }
        services["fixture"]["healthcheck"] = {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; urllib.request."
                "urlopen('http://127.0.0.1:8000/__calls', "
                "timeout=2).read()",
            ],
            "interval": "1s",
            "timeout": "3s",
            "retries": 15,
        }
        services["public-web"]["depends_on"] = {
            "fixture": {"condition": "service_healthy"}
        }
        services["public-web"]["healthcheck"] = {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; urllib.request."
                "urlopen('http://127.0.0.1:8000/healthz', "
                "timeout=2).read()",
            ],
            "interval": "1s",
            "timeout": "3s",
            "retries": 15,
        }
        services["public-frontend"] = {
            **common,
            "image": images["frontend"],
            "networks": ["website"],
            "depends_on": {"public-web": {"condition": "service_healthy"}},
            "healthcheck": {
                "test": [
                    "CMD-SHELL",
                    "wget -q -O /dev/null http://127.0.0.1:8080/healthz",
                ],
                "interval": "1s",
                "timeout": "3s",
                "retries": 15,
            },
        }
        services["public-entry"]["depends_on"]["public-frontend"] = {
            "condition": "service_healthy"
        }
        nginx = """worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr crit;
events { worker_connections 128; }
http {
 access_log off;
 resolver 127.0.0.11 valid=1s;
 client_body_temp_path /tmp/client_body;
 proxy_temp_path /tmp/proxy;
 fastcgi_temp_path /tmp/fastcgi;
 uwsgi_temp_path /tmp/uwsgi;
 scgi_temp_path /tmp/scgi;
 server {
  listen 9443 ssl;
  server_name public.test;
  ssl_certificate /fixture/tls.crt;
  ssl_certificate_key /fixture/tls.key;
  location / {
   set $gateway public-entry:9080;
   proxy_pass http://$gateway;
   proxy_set_header Host $http_host;
   proxy_set_header X-Forwarded-Proto https;
  }
 }
}
"""
        if cls.antibot:
            nginx = nginx.replace("public-entry:9080", "public-edge:8080")
        (root / "nginx.conf").write_text(nginx)
        (root / "nginx.conf").chmod(0o600)
        services["tls-front"] = {
            **common,
            "image": images["nginx"],
            "volumes": [mounts[1]],
            "entrypoint": [
                "nginx",
                "-c",
                "/fixture/nginx.conf",
                "-g",
                "daemon off;",
            ],
            "networks": {"outbound": {"aliases": ["public.test"]}},
            "depends_on": {"public-entry": {"condition": "service_healthy"}},
        }
        if cls.antibot:
            services["tls-front"]["networks"]["antibot-gateway"] = {}
        services["probe"] = {
            **common,
            "image": images["python"],
            "volumes": mounts,
            "entrypoint": ["python", "-c", "import time; time.sleep(600)"],
            "networks": ["outbound"],
        }
        if cls.counter_observer:
            _install_counter_reader(root)
        cls.compose_file.write_text(json.dumps(composition))
        cls.compose_file.chmod(0o600)
        cls.addClassCleanup(
            lambda: cls.compose("down", "--timeout", "2", timeout=30)
        )
        cls.compose(
            "up", "--detach", "--pull", "never", "--no-build", timeout=60
        )
        cls.wait_ready()

    @classmethod
    def probe(cls, action, account="anonymous", payload=None):
        command = [
            "exec",
            "--no-TTY",
            "probe",
            "python",
            "-m",
            "search.tests.public_entry.fixture",
            "probe",
            "--action",
            action,
            "--account",
            account,
        ]
        if payload is not None:
            command.extend(["--payload", json.dumps(payload)])
        return json.loads(
            cls.compose(
                *command, timeout=25 if action == "burst" else 15
            ).stdout
        )

    @classmethod
    def wait_ready(cls):
        deadline = time.monotonic() + 25
        last = None
        while time.monotonic() < deadline:
            try:
                last = (
                    cls.probe("access")["status"],
                    cls.probe("health")["status"],
                )
                if last == (200 if cls.mixed else 401, 200):
                    return
            except (AssertionError, ValueError):
                pass
            time.sleep(0.5)
        raise AssertionError(
            "isolated HTTPS entrance did not become ready: " + str(last)
        )

    def calls(self):
        return self.probe("counter")["value"]["calls"]


class PublicEntrySessionManagementTests(PublicEntryRuntimeFixture):
    counter_observer = True

    def management(self, action, account, options=None):
        # Keep these checks inside the unchanged gateway's ordinary IP rate.
        time.sleep(0.35)
        return self.probe(action, account, options)

    def counter_snapshot(self):
        script = """local values = {}
for _, pattern in ipairs({
 'plugin-limit-count:v1:/routes/turn:*',
 'plugin-limit-count:v1:/routes/session-create:*'
}) do
 local cursor = '0'
 repeat
  local page = redis.call('SCAN', cursor, 'MATCH', pattern, 'COUNT', 1000)
  cursor = page[1]
  for _, key in ipairs(page[2]) do
   table.insert(values, {key, redis.call('GET', key)})
  end
 until cursor == '0'
end
return values"""
        result = self.compose(
            "exec",
            "--no-TTY",
            "quota-redis",
            "/bin/sh",
            "-c",
            'export REDISCLI_AUTH="$(cat /run/secrets/redis-password)"; '
            "exec redis-cli --no-auth-warning --user fixture-counter-reader "
            '--json "$@"',
            "fixture-read",
            "EVAL",
            script,
            "0",
        )
        return dict(
            sorted(
                (key, int(value)) for key, value in json.loads(result.stdout)
            )
        )

    def assert_disabled_management(self, account):
        status = self.management("session-status", account)
        withdrawn = self.management("session-withdraw", account)
        for response in (status, withdrawn):
            self.assertEqual(response["status"], 200, response)
            self.assertEqual(set(response["value"]), {"trace"})
            trace = response["value"]["trace"]
            self.assertFalse(trace["enabled"])
            self.assertIsNone(trace["test_session_id"])
            self.assertEqual(trace["capture_status"], "disabled")
            self.assertEqual(trace["deletion_status"], "none")
        self.assertEqual(status["value"], withdrawn["value"])
        return {"GET": status["status"], "PATCH": withdrawn["status"]}

    def management_accounts(self):
        owners = [("management-user", 3 if self.mixed else 2)]
        if self.mixed:
            owners.append(("management-guest", 1))
        for account, _ in owners:
            if self.antibot:
                self.assertEqual(self.probe("verify", account)["status"], 200)
            if account == "management-user":
                self.assertEqual(self.probe("login", account)["status"], 200)
            self.assertEqual(self.probe("session", account)["status"], 200)
        return owners

    def exhaust_turn_allowances(self, owners):
        exhausted = {}
        for account, allowance in owners:
            if self.antibot:
                self.assertEqual(
                    self.probe("verify", account, {"renew": True})["status"],
                    200,
                )
            for version in range(allowance):
                response = self.probe(
                    "turn", account, {"query": "氣候", "version": version}
                )
                self.assertEqual(response["status"], 200, response)
            response = self.probe(
                "turn", account, {"query": "氣候", "version": allowance}
            )
            self.assertEqual(response["status"], 429, response)
            expected = "quota_exceeded"
            if self.mixed:
                expected = (
                    "user_quota_exceeded"
                    if account == "management-user"
                    else "guest_quota_exceeded"
                )
            self.assertEqual(response["value"]["error"]["code"], expected)
            exhausted[account] = response["value"]["error"]["code"]
        return exhausted

    def assert_management_denials(self, owner, other):
        for action in ("session-status", "session-withdraw"):
            for options, expected in (
                ({"token_account": None}, (401,)),
                ({"token": "invalid-browser-token"}, (401, 410)),
                ({"token": "test-session-00000000"}, (401, 410)),
                ({"origin": "https://other.example"}, (403,)),
                ({"query": "?unexpected=1"}, (400,)),
            ):
                response = self.management(action, owner, options)
                self.assertIn(response["status"], expected, response)
            for account in (other, "management-anonymous"):
                response = self.management(
                    action, account, {"token_account": owner, "spoof": True}
                )
                self.assertIn(response["status"], (401, 410), response)
        for options, expected in (
            ({"origin": None}, 403),
            ({"body": {"capture": True}}, 422),
            ({"body": {"capture": False, "extra": True}}, 422),
        ):
            response = self.management("session-withdraw", owner, options)
            self.assertEqual(response["status"], expected, response)
        for method in ("HEAD", "PUT", "OPTIONS"):
            response = self.management(
                "session-status", owner, {"method": method}
            )
            self.assertIn(response["status"], (404, 405), response)

    def assert_login_cookie_fences(self, account):
        from tests.integration.public_entry.test_identity_gateway import (
            COOKIE_PROBE,
        )

        def cookie(operation, argument):
            return self.compose(
                "exec",
                "--no-TTY",
                "probe",
                "python",
                "-c",
                COOKIE_PROBE,
                account,
                operation,
                argument,
            )

        cookie("snapshot", "session-management")
        for operation in ("tamper", "expire"):
            cookie(operation, "__Host-depositar-login")
            for action in ("session-status", "session-withdraw"):
                response = self.management(action, account)
                self.assertIn(response["status"], (401, 410), response)
            if self.mixed and operation == "expire":
                principal = self.probe("access", account)["value"]["principal"]
                self.assertEqual(principal["kind"], "guest")
            cookie("restore", "session-management")
            self.assertEqual(
                self.management("session-status", account)["status"], 200
            )

    def expired_challenge_management(self, owners):
        time.sleep(11)
        results = {
            account: self.assert_disabled_management(account)
            for account, _ in owners
        }
        owner, allowance = owners[0]
        for action, payload in (
            ("session", None),
            ("turn", {"query": "氣候", "version": allowance}),
        ):
            response = self.probe(action, owner, payload)
            self.assertEqual(response["status"], 403, response)
            self.assertEqual(
                response["value"]["error"]["code"], "challenge_required"
            )
            results[owner][action] = response["status"]
        login = self.login_challenge_redirect(owner)
        self.assertEqual(
            login, {"status": 302, "verification_path": "/verify"}
        )
        results[owner]["login"] = login
        deleted = self.management("session-delete", owner)
        self.assertEqual(deleted["status"], 200, deleted)
        results[owner]["DELETE"] = deleted["status"]
        self.assertEqual(self.probe("verify", owner)["status"], 200)
        self.assertEqual(self.probe("session", owner)["status"], 200)
        return results

    def login_challenge_redirect(self, account):
        code = (
            "import http.client,json,ssl,sys\n"
            "from http.cookiejar import MozillaCookieJar\n"
            "from urllib.request import Request\n"
            "from urllib.parse import urlsplit\n"
            "jar=MozillaCookieJar('/tmp/'+sys.argv[1]+'.cookies')\n"
            "jar.load(ignore_discard=True,ignore_expires=True)\n"
            "request=Request('https://public.test:9443/auth/login')\n"
            "jar.add_cookie_header(request)\n"
            "context=ssl.create_default_context(cafile='/fixture/tls.crt')\n"
            "connection=http.client.HTTPSConnection('public.test',9443,"
            "context=context,timeout=10)\n"
            "connection.request('GET','/auth/login',"
            "headers=dict(request.header_items()))\n"
            "response=connection.getresponse();response.read(16384)\n"
            "print(json.dumps({'status':response.status,'verification_path':"
            "urlsplit(response.getheader('Location') or '').path}))\n"
        )
        result = self.compose(
            "exec", "--no-TTY", "probe", "python", "-c", code, account
        )
        return json.loads(result.stdout)

    def redis_down_management(self, owners):
        self.compose("stop", "--timeout", "2", "quota-redis", timeout=15)
        results = {
            account: self.assert_disabled_management(account)
            for account, _ in owners
        }
        owner, allowance = owners[0]
        for action, payload in (
            ("session", None),
            ("turn", {"query": "氣候", "version": allowance}),
        ):
            if self.antibot:
                self.assertEqual(
                    self.probe("verify", owner, {"renew": True})["status"],
                    200,
                )
            response = self.probe(action, owner, payload)
            self.assertIn(response["status"], (500, 503), response)
        self.assertEqual(
            self.management("session-delete", owner)["status"], 200
        )
        return results

    def test_session_management_keeps_identity_and_quota_boundaries(self):
        composition = json.loads(self.compose_file.read_text())
        owners = self.management_accounts()
        owner = owners[0][0]
        report = {
            "project": self.project,
            "mixed": self.mixed,
            "antibot": self.antibot,
            "images": {
                name: service["image"]
                for name, service in composition["services"].items()
            },
            "initial": {owner: self.assert_disabled_management(owner)},
        }
        self.assertEqual(
            self.management("session-delete", owner)["status"], 200
        )
        self.assertEqual(self.probe("session", owner)["status"], 200)
        for account, _ in owners[1:]:
            report["initial"][account] = self.assert_disabled_management(
                account
            )
        report["quota_exhausted"] = self.exhaust_turn_allowances(owners)
        before = self.counter_snapshot()
        report["healthy_counter_before"] = before
        type(self).session_management_evidence = report
        self.assertTrue(
            any(
                key.startswith("plugin-limit-count:v1:/routes/turn:")
                for key in before
            )
        )
        self.assertTrue(
            any(
                key.startswith("plugin-limit-count:v1:/routes/session-create:")
                for key in before
            )
        )
        calls = self.calls()
        report["exhausted_management"] = {
            account: self.assert_disabled_management(account)
            for account, _ in owners
        }
        other = owners[-1][0]
        if not self.mixed:
            other = "management-other"
            self.assertEqual(self.probe("login", other)["status"], 200)
        self.assert_management_denials(owner, other)
        self.assert_login_cookie_fences(owner)
        after = self.counter_snapshot()
        self.assertEqual(after, before)
        self.assertEqual(self.calls(), calls)
        report["healthy_counter_before"] = before
        report["healthy_counter_after"] = after
        if self.antibot:
            report["expired_challenge"] = self.expired_challenge_management(
                owners
            )
        report["redis_down"] = self.redis_down_management(owners)
        self.assertEqual(self.calls(), calls)
        report["upstream_calls"] = calls
        type(self).session_management_evidence = report


class PublicEntryRuntimeTests(PublicEntryRuntimeFixture):
    def test_real_oidc_account_quotas_restart_and_fail_closed(self):
        self.assertEqual(self.probe("access")["status"], 401)
        self.assertEqual(self.probe("spoof")["status"], 401)
        self.assertEqual(self.calls(), 0)

        for account in ("alice", "bob"):
            login = self.probe("login", account)
            self.assertEqual(login["status"], 200)
            identity_cookies = [
                cookie
                for cookie in login["cookies"]
                if cookie["name"].startswith("__Host-depositar-login")
            ]
            self.assertTrue(identity_cookies)
            for cookie in identity_cookies:
                self.assertTrue(cookie["secure"])
                self.assertTrue(cookie["http_only"])
                self.assertFalse(cookie["domain_specified"])
                self.assertEqual(cookie["path"], "/")
                self.assertEqual(cookie["same_site"], "Lax")
            access = self.probe("access", account)["value"]
            self.assertEqual(access["mode"], "login_required")
            self.assertEqual(access["principal"]["kind"], "user")
            self.assertEqual(
                access["limits"],
                {"turns": 2, "window_seconds": self.quota_window_seconds},
            )
            self.assertEqual(self.probe("session", account)["status"], 200)
        # Even a stolen search token and correct internal headers cannot turn
        # an anonymous alternate/encoded route into an authenticated request.
        for path in (
            "/assets/../api/turn",
            "/assets/%2e%2e/api/turn",
            "/api/%74urn",
            "/api//turn",
        ):
            self.assertIn(
                self.probe("path", payload={"path": path})["status"],
                (400, 401, 403, 404, 405),
            )
        self.assertIn(
            self.probe(
                "path",
                payload={
                    "path": "/api/turn",
                    "method": "PUT",
                },
            )["status"],
            (400, 401, 403, 404, 405),
        )
        self.assertEqual(self.calls(), 0)
        for version in (0, 1):
            self.assertEqual(
                self.probe(
                    "turn",
                    "alice",
                    {
                        "query": "氣候",
                        "version": version,
                    },
                )["status"],
                200,
            )
        self.assertEqual(self.calls(), 2)
        self.assertEqual(
            self.probe(
                "turn",
                "alice",
                {
                    "query": "氣候",
                    "version": 2,
                },
            )["status"],
            429,
        )
        self.assertEqual(self.calls(), 2)
        self.assertEqual(
            self.probe(
                "turn",
                "bob",
                {
                    "query": "氣候",
                    "version": 0,
                },
            )["status"],
            200,
        )
        self.assertEqual(self.calls(), 3)

        for account in (
            "forged",
            "wrong-issuer",
            "wrong-audience",
            "wrong-nonce",
            "number-audience",
        ):
            self.probe("login", account)
            self.assertEqual(
                self.probe("access", account)["status"], 401, account
            )
            self.assertEqual(self.calls(), 3)
        self.assertEqual(self.probe("login", "array-audience")["status"], 200)
        self.assertEqual(self.probe("access", "array-audience")["status"], 200)

        self.compose("restart", "public-entry", timeout=30)
        self.wait_ready()
        self.assertEqual(
            self.probe(
                "turn",
                "alice",
                {
                    "query": "氣候",
                    "version": 2,
                },
            )["status"],
            429,
        )
        self.assertEqual(self.calls(), 3)

        self.compose("stop", "--timeout", "2", "quota-redis", timeout=15)
        self.assertIn(
            self.probe(
                "turn",
                "bob",
                {
                    "query": "氣候",
                    "version": 1,
                },
            )["status"],
            (500, 503),
        )
        self.assertEqual(self.calls(), 3)
        self.compose("start", "quota-redis", timeout=15)
        time.sleep(1)
        self.assertEqual(
            self.probe(
                "turn",
                "alice",
                {
                    "query": "氣候",
                    "version": 2,
                },
            )["status"],
            429,
        )
        self.assertEqual(
            self.probe(
                "turn",
                "bob",
                {
                    "query": "氣候",
                    "version": 1,
                },
            )["status"],
            200,
        )
        self.assertEqual(self.calls(), 4)
        self.assertEqual(self.probe("login", "charlie")["status"], 200)
        self.assertEqual(self.probe("session", "charlie")["status"], 200)
        self.assertEqual(
            self.probe(
                "turn",
                "charlie",
                {
                    "query": "氣候",
                    "version": 0,
                },
            )["status"],
            429,
        )
        self.assertEqual(self.calls(), 4)


class PublicEntryConcurrencyTests(PublicEntryRuntimeFixture):
    def burst(self, action, accounts):
        payload = {"query": "氣候", "version": 0} if action == "turn" else {}
        results = self.probe(
            "burst",
            payload={
                "action": action,
                "requests": [
                    {"account": account, "payload": payload}
                    for account in accounts
                ],
            },
        )["results"]
        self.assertEqual(len(results), len(accounts))
        # IP limits run after count limits. Any IP rejection would spend quota
        # while obscuring whether the admission counter itself was correct.
        self.assertNotIn(
            "rate_limited", [result["code"] for result in results]
        )
        return results

    def turn_admissions(self, results):
        admitted = Counter()
        for result in results:
            status, code = result["status"], result["code"]
            if status == 200 or (status, code) in {
                (429, "busy"),
                (429, "session_busy"),
                (409, "session_changed"),
            }:
                admitted[result["account"]] += 1
            else:
                self.assertEqual(status, 429, result)
                self.assertIn(
                    code,
                    {
                        "quota_exceeded",
                        "guest_quota_exceeded",
                        "user_quota_exceeded",
                        "pool_quota_exceeded",
                        "global_quota_exceeded",
                    },
                    result,
                )
        return admitted

    def test_authenticated_bursts_keep_account_global_and_session_caps(self):
        for account in ("alice", "bob", "carol", "dave"):
            self.assertEqual(self.probe("login", account)["status"], 200)
            self.assertEqual(self.probe("session", account)["status"], 200)
        self.assertEqual(self.calls(), 0)

        # Input validation happens after gateway admission. This malformed
        # query consumes one of Alice's two entries without starting inference.
        malformed = self.probe(
            "turn", "alice", {"query": {"bad": "input"}, "version": 0}
        )
        self.assertEqual(malformed["status"], 422)
        self.assertEqual(
            malformed["value"], {"error": {"code": "invalid_request"}}
        )
        self.assertEqual(self.calls(), 0)
        admissions = Counter({"alice": 1})

        alice = self.turn_admissions(self.burst("turn", ["alice"] * 4))
        self.assertEqual(alice, Counter({"alice": 1}))
        admissions.update(alice)
        self.assertEqual(self.calls(), 1)

        # Refill the shared 3r/s IP bucket, then race twelve valid requests for
        # the two remaining global admissions. The production limits are kept.
        time.sleep(2)
        others = self.turn_admissions(
            self.burst("turn", ["bob", "carol", "dave"] * 4)
        )
        self.assertEqual(sum(others.values()), 2)
        admissions.update(others)
        self.assertEqual(sum(admissions.values()), 4)
        self.assertTrue(all(count <= 2 for count in admissions.values()))
        calls = self.calls()
        self.assertGreaterEqual(calls, 2)
        self.assertLessEqual(calls, 3)
        # BFF busy/version failures count as admitted HTTP requests. Comparing
        # only successful model calls would let its single slot hide overshoot.
        for account in ("alice", "bob", "carol", "dave"):
            rejected = self.probe(
                "turn", account, {"query": "氣候", "version": 0}
            )
            self.assertEqual(rejected["status"], 429)
            self.assertEqual(
                rejected["value"], {"error": {"code": "quota_exceeded"}}
            )
        self.assertEqual(self.calls(), calls)

        sessions = self.burst("session", ["alice"] * 12)
        self.assertEqual(
            sum(result["status"] == 200 for result in sessions), 1
        )
        for result in sessions:
            self.assertTrue(
                result["status"] == 200
                or (result["status"], result["code"])
                in {
                    (429, "session_capacity"),
                    (429, "quota_exceeded"),
                },
                result,
            )
        # Alice cannot monopolize another user's two-session allowance.
        time.sleep(1)
        self.assertEqual(self.probe("session", "bob")["status"], 200)
        self.assertEqual(self.calls(), calls)


class PublicEntryMixedAccessTests(PublicEntryRuntimeFixture):
    mixed = True

    def test_guest_identity_pool_reservation_login_and_durable_quotas(self):
        first = self.probe("access", "visitor-one")
        self.assertEqual(first["status"], 200)
        access = first["value"]
        self.assertEqual(access["mode"], "mixed")
        self.assertEqual(access["principal"]["kind"], "guest")
        self.assertEqual(
            access["limits"],
            {"turns": 1, "window_seconds": self.quota_window_seconds},
        )
        self.assertEqual(self.probe("access", "visitor-one")["value"], access)
        flags = next(
            c
            for c in first["cookies"]
            if c["name"] == "__Host-depositar-guest"
        )
        self.assertTrue(flags["secure"] and flags["http_only"])
        self.assertFalse(flags["domain_specified"])
        self.assertEqual((flags["path"], flags["same_site"]), ("/", "Lax"))
        for account in ("visitor-one", "visitor-two", "visitor-three"):
            self.assertEqual(self.probe("session", account)["status"], 200)
        self.assertEqual(
            self.probe("turn", "visitor-one", {"query": "氣候", "version": 0})[
                "status"
            ],
            200,
        )
        # A new search session retains the visitor's budget.
        self.assertEqual(self.probe("session", "visitor-one")["status"], 200)
        rejected = self.probe(
            "turn", "visitor-one", {"query": "氣候", "version": 0}
        )
        self.assertEqual(rejected["status"], 429)
        self.assertEqual(
            rejected["value"]["error"]["code"], "guest_quota_exceeded"
        )
        self.assertEqual(
            self.probe("turn", "visitor-two", {"query": "氣候", "version": 0})[
                "status"
            ],
            200,
        )
        rejected = self.probe(
            "turn", "visitor-three", {"query": "氣候", "version": 0}
        )
        self.assertEqual(rejected["status"], 429)
        self.assertEqual(
            rejected["value"]["error"]["code"], "pool_quota_exceeded"
        )
        self.assertEqual(self.calls(), 2)
        # Exhausting the guest pool leaves the configured user reservation.
        self.assertEqual(self.probe("login", "alice")["status"], 200)
        user = self.probe("access", "alice")["value"]
        self.assertEqual(user["principal"]["kind"], "user")
        self.assertEqual(user["limits"]["turns"], 3)
        self.assertEqual(self.probe("session", "alice")["status"], 200)
        for version in range(3):
            self.assertEqual(
                self.probe(
                    "turn", "alice", {"query": "氣候", "version": version}
                )["status"],
                200,
            )
        rejected = self.probe("turn", "alice", {"query": "氣候", "version": 3})
        self.assertEqual(
            rejected["value"]["error"]["code"], "user_quota_exceeded"
        )
        self.assertEqual(self.calls(), 5)
        # Caller identity headers and another principal's bearer cannot claim
        # it.
        denied = self.probe("path", "attacker-guest", {"path": "/api/turn"})
        self.assertIn(denied["status"], (401, 429))
        self.assertEqual(self.calls(), 5)
        self.compose("restart", "public-entry", timeout=30)
        self.wait_ready()
        rejected = self.probe("turn", "alice", {"query": "氣候", "version": 3})
        self.assertEqual(
            rejected["value"]["error"]["code"], "user_quota_exceeded"
        )
        self.compose("stop", "--timeout", "2", "quota-redis", timeout=15)
        rejected = self.probe(
            "turn", "visitor-one", {"query": "氣候", "version": 0}
        )
        self.assertIn(rejected["status"], (500, 503))
        self.assertEqual(self.calls(), 5)


class PublicEntryMixedConcurrencyTests(PublicEntryRuntimeFixture):
    mixed = True
    burst = PublicEntryConcurrencyTests.burst
    turn_admissions = PublicEntryConcurrencyTests.turn_admissions

    def test_parallel_guests_cannot_consume_reserved_user_pool(self):
        guests = ["visitor-" + str(i) for i in range(4)]
        users = ["account-" + str(i) for i in range(3)]
        for account in guests + users:
            if account in users:
                self.assertEqual(self.probe("login", account)["status"], 200)
            self.assertEqual(self.probe("session", account)["status"], 200)
        time.sleep(2)
        admitted_guests = self.turn_admissions(self.burst("turn", guests))
        self.assertEqual(sum(admitted_guests.values()), 2)
        self.assertTrue(all(count <= 1 for count in admitted_guests.values()))
        time.sleep(2)
        admitted_users = self.turn_admissions(self.burst("turn", users * 4))
        self.assertEqual(sum(admitted_users.values()), 4)
        self.assertTrue(all(count <= 3 for count in admitted_users.values()))
        self.assertEqual(
            sum(admitted_guests.values()) + sum(admitted_users.values()), 6
        )
        # Count admissions, including BFF busy/version errors; GPU calls alone
        # could hide an overshoot behind the BFF's independent single slot.
        self.assertLessEqual(self.calls(), 6)


if __name__ == "__main__":
    unittest.main()
