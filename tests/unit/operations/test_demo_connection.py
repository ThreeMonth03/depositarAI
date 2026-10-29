"""CPU-only SSH command, response, deadline and owned-child regressions."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from search.operations.deployment import demo_connection as demo

NONCE = "f" * 64


def profile():
    return {
        "schema_version": 1,
        "browser_origin": demo.ORIGIN,
        "remote_port": 18081,
        "hops": [
            {
                "destination": "operator@gpu-alias",
                "ssh_config": "/private/client ssh.conf",
                "local_forward_port": 18081,
            },
            {
                "destination": "vm-alias",
                "ssh_config": "/private/server's ssh.conf",
                "local_forward_port": 38081,
            },
        ],
        "remote_checker": "/private/operator's source/checker.py",
        "remote_profile": "/private/operator's config.json",
        "expected": {
            "frontend_revision": "a" * 40,
            "api_revision": "b" * 40,
            "conversation_contract_version": 4,
            "catalog_manifest_sha256": "c" * 64,
        },
    }


def checked():
    return {
        "schema_version": 3,
        "deployment_mode": "private",
        "project": "depositar-search-web",
        "origin": "http://127.0.0.1:18081",
        "status": "frontend_checked",
        **profile()["expected"],
        "runtime": {
            role: {
                "container_id": digit * 64,
                "container_image_index": "sha256:" + digit * 64,
                "platform_manifest": "sha256:" + digit * 64,
            }
            for role, digit in (("web", "1"), ("web-nginx", "2"))
        },
        "homepage_verified": True,
        "assets_verified": 2,
        "backend_identity_verified": True,
        "upstream_tls_verified": True,
        "authenticated_version_status": 200,
        "unauthenticated_version_status": 401,
        "model_inference_verified": False,
        "public_readiness_verified": False,
        "deployment_performed": False,
    }


class ProfileTests(unittest.TestCase):
    def test_private_profile_rejects_unsafe_files_and_duplicate_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile()))
            path.chmod(0o600)
            self.assertEqual(demo.load_profile(path), profile())
            path.chmod(0o644)
            with self.assertRaisesRegex(demo.DemoConnectionError, "permissions"):
                demo.load_profile(path)
            path.chmod(0o600)
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(demo.DemoConnectionError):
                demo.load_profile(path)
            path.write_text(json.dumps(profile()))
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(demo.DemoConnectionError):
                demo.load_profile(link)

    def test_invalid_origin_hops_paths_ports_and_types_are_rejected(self):
        mutations = [
            lambda p: p.update(browser_origin="http://localhost:18081"),
            lambda p: p.update(browser_origin="http://127.0.0.1:18082"),
            lambda p: p.update(remote_port=True),
            lambda p: p.update(remote_port=18083),
            lambda p: p.update(hops=[]),
            lambda p: p.update(hops=p["hops"] * 2),
            lambda p: p["hops"][0].update(destination="-oProxyCommand=bad"),
            lambda p: p["hops"][0].update(destination="host; touch /tmp/bad"),
            lambda p: p["hops"][0].update(local_forward_port=18082),
            lambda p: p["hops"][1].update(local_forward_port=True),
            lambda p: p["hops"][1].update(local_forward_port=65536),
            lambda p: p["hops"][1].update(ssh_config="relative"),
            lambda p: p.update(remote_checker="/private/checker\n.py"),
            lambda p: p.update(remote_profile="relative"),
            lambda p: p["expected"].update(conversation_contract_version=True),
            lambda p: p["expected"].update(conversation_contract_version=3),
            lambda p: p["expected"].update(frontend_revision="latest"),
            lambda p: p.update(extra=True),
        ]
        for mutate in mutations:
            value = profile()
            mutate(value)
            with self.subTest(value=value), self.assertRaises(demo.DemoConnectionError):
                demo.validate_profile(value)

    def test_nested_ssh_preserves_remote_paths_and_local_forward_ownership(self):
        command = demo.ssh_command(profile(), tunnel=True, nonce=NONCE)
        self.assertIn("-tt", command)
        self.assertIn("127.0.0.1:18081:127.0.0.1:38081", command)
        inner = shlex.split(command[-1])
        self.assertEqual(inner[:3], ["ssh", "-F", "/private/server's ssh.conf"])
        self.assertIn("-T", inner)
        self.assertIn("-N", inner)
        self.assertNotIn("-tt", inner)
        self.assertIn("127.0.0.1:38081:127.0.0.1:18081", inner)
        self.assertEqual(inner[-1], "vm-alias")
        self.assertIn("PermitLocalCommand=no", command)
        self.assertNotIn("PermitLocalCommand=yes", command)
        self.assertIn("PermitLocalCommand=yes", inner)
        self.assertNotIn("PermitLocalCommand=no", inner)
        self.assertIn(f"LocalCommand=printf '{demo.READY_PREFIX}{NONCE}\\n'", inner)
        for args in (command, inner):
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("ControlMaster=no", args)
            self.assertIn("ForwardAgent=no", args)
            self.assertIn("RemoteCommand=none", args)
            self.assertIn("ForwardX11=no", args)
            self.assertIn("ClearAllForwardings=no", args)
            self.assertIn("ForkAfterAuthentication=no", args)
            self.assertNotIn("-A", args)
            self.assertNotIn("-f", args)

    def test_three_hop_checker_never_forwards_or_allocates_pty(self):
        value = profile()
        value["hops"].insert(
            1,
            {"destination": "middle", "ssh_config": None, "local_forward_port": 28081},
        )
        command = demo.ssh_command(value, tunnel=False)
        for _ in range(3):
            self.assertEqual(command[0], "ssh")
            self.assertNotIn("-L", command)
            self.assertNotIn("-tt", command)
            self.assertNotIn("-N", command)
            command = shlex.split(command[-1])
        self.assertEqual(
            command,
            [
                "python3",
                "-I",
                "-S",
                "-B",
                value["remote_checker"],
                "--config",
                value["remote_profile"],
            ],
        )

    def test_remote_identity_is_closed_and_does_not_accept_health_only(self):
        self.assertEqual(
            demo.validate_remote_check(checked(), profile()["expected"]), checked()
        )
        for field, value in (
            ("api_revision", "d" * 40),
            ("homepage_verified", 1),
            ("assets_verified", True),
            ("deployment_performed", True),
            ("model_inference_verified", True),
            ("conversation_contract_version", 4.0),
            ("schema_version", True),
            ("schema_version", 2),
            ("deployment_mode", "intranet-http"),
            ("project", "depositar-search-intranet"),
            ("origin", "http://10.20.30.40:18082"),
            ("extra", "PRIVATE_SENTINEL"),
        ):
            report = checked()
            report[field] = value
            with self.subTest(field=field), self.assertRaises(demo.DemoConnectionError):
                demo.validate_remote_check(report, profile()["expected"])
        with self.assertRaises(demo.DemoConnectionError):
            demo.validate_remote_check({"status": "ok"}, profile()["expected"])


@contextmanager
def website(*, access_status=200, oversized=False):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.command, self.path, self.headers.get("Origin")))
            status = access_status if self.path == "/api/access" else 200
            if self.path == "/":
                body = b'<title>depositar AI</title><div id="root"></div>'
            elif self.path == "/healthz":
                body = b'{"status":"ok"}'
            else:
                body = json.dumps(
                    {
                        "mode": "private",
                        "principal": {"kind": "private", "id": None},
                        "can_search": True,
                        "login_url": None,
                        "logout_url": None,
                        "limits": {"turns": None, "window_seconds": None},
                    }
                ).encode()
                if oversized:
                    body += b" " * 5000
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = socket.create_connection

    def create(_address, **kwargs):
        return original(server.server_address, **kwargs)

    try:
        with mock.patch.object(demo.socket, "create_connection", side_effect=create):
            yield calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class NetworkTests(unittest.TestCase):
    def test_local_checks_only_get_and_send_exact_origin_without_environment_proxy(
        self,
    ):
        with (
            website() as calls,
            mock.patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://bad.invalid",
                    "HTTPS_PROXY": "http://bad.invalid",
                },
            ),
        ):
            demo.local_check()
        self.assertEqual(
            calls,
            [("GET", path, demo.ORIGIN) for path in ("/healthz", "/", "/api/access")],
        )

    def test_origin_denial_redirect_and_body_limits_remain_failures(self):
        for status in (403, 302):
            with (
                self.subTest(status=status),
                website(access_status=status),
                self.assertRaises(demo.DemoConnectionError),
            ):
                demo.local_check()
        with (
            website(oversized=True),
            self.assertRaisesRegex(demo.DemoConnectionError, "body_invalid"),
        ):
            demo.local_check()

    def test_port_conflict_keeps_existing_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaisesRegex(demo.DemoConnectionError, "local_port_in_use"):
                demo.available_port(listener.getsockname()[1])
            self.assertGreaterEqual(listener.fileno(), 0)

    def test_closed_ssh_and_http_failure_have_clear_stage(self):
        tunnel = mock.Mock()
        with (
            mock.patch.object(demo, "local_check", side_effect=ConnectionRefusedError),
            mock.patch.object(demo, "START_SECONDS", 0),
        ):
            with self.assertRaisesRegex(
                demo.DemoConnectionError, "tunnel_not_reachable"
            ):
                demo.verify_local(tunnel)

    def test_remote_checker_errors_do_not_expose_captured_output(self):
        with mock.patch.object(
            demo.subprocess, "Popen", side_effect=OSError("PRIVATE_SENTINEL")
        ):
            with self.assertRaisesRegex(
                demo.DemoConnectionError, "^remote_check_failed$"
            ):
                demo.remote_check(profile())


class ProcessTests(unittest.TestCase):
    def spawn(self, code):
        return subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def test_cleanup_terminates_only_owned_group(self):
        own = self.spawn("import time;time.sleep(30)")
        unrelated = self.spawn("import time;time.sleep(30)")
        tunnel = demo.Tunnel(own, NONCE)
        try:
            tunnel.verify()
            tunnel.close()
            self.assertIsNotNone(own.poll())
            self.assertIsNone(unrelated.poll())
        finally:
            os.killpg(unrelated.pid, signal.SIGKILL)
            unrelated.wait(timeout=5)
            unrelated.stdout.close()
            unrelated.stderr.close()

    def test_tunnel_output_is_bounded_and_sanitized(self):
        own = self.spawn(
            "import sys,time;sys.stderr.write('PRIVATE_SENTINEL'*10000);sys.stderr.flush();time.sleep(30)"
        )
        tunnel = demo.Tunnel(own, NONCE)
        try:
            deadline = time.monotonic() + 3
            while tunnel.failure is None and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.assertRaisesRegex(
                demo.DemoConnectionError, "^tunnel_output_limit$"
            ):
                tunnel.verify()
        finally:
            tunnel.close()

    def test_check_context_closes_on_success_and_interrupt(self):
        @contextmanager
        def opened(_profile):
            try:
                yield mock.Mock()
            finally:
                closed.append(True)

        closed = []
        with (
            mock.patch.object(demo, "remote_check"),
            mock.patch.object(demo, "open_tunnel", opened),
            mock.patch.object(demo, "verify_local"),
        ):
            result = demo.run(profile())
        self.assertTrue(result["own_tunnel_closed"])
        self.assertEqual(closed, [True])
        with (
            mock.patch.object(demo, "remote_check"),
            mock.patch.object(demo, "open_tunnel", opened),
            mock.patch.object(demo, "verify_local", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            demo.run(profile(), connect=True)
        self.assertEqual(closed, [True, True])

    def test_no_http_before_same_chain_forward_readiness(self):
        for code, expected in (
            ("import time;time.sleep(30)", "forward_readiness_timeout"),
            (
                "print('DEPOSITAR_DEMO_READY_'+'a'*64,flush=True);import time;time.sleep(30)",
                "forward_readiness_invalid",
            ),
        ):
            own = self.spawn(code)
            tunnel = demo.Tunnel(own, NONCE)
            try:
                with (
                    self.subTest(code=expected),
                    mock.patch.object(demo, "START_SECONDS", 0.1),
                    mock.patch.object(demo, "local_check") as check,
                ):
                    with self.assertRaisesRegex(
                        demo.DemoConnectionError, "^" + expected + "$"
                    ):
                        demo.verify_local(tunnel)
                    check.assert_not_called()
            finally:
                tunnel.close()

    def test_delayed_crlf_readiness_gates_http_on_owned_chain(self):
        for carriage_returns in (1, 2):
            with self.subTest(intermediate_ptys=carriage_returns):
                own = self.spawn(
                    "import time;time.sleep(.1);print('DEPOSITAR_DEMO_READY_'+"
                    f" 'f'*64+'\\r'*{carriage_returns},flush=True);time.sleep(30)"
                )
                tunnel = demo.Tunnel(own, NONCE)
                try:

                    def http(**_kwargs):
                        self.assertTrue(tunnel.ready.is_set())

                    with mock.patch.object(
                        demo, "local_check", side_effect=http
                    ) as check:
                        demo.verify_local(tunnel)
                        check.assert_called_once()
                finally:
                    tunnel.close()

    def test_cli_prints_fixed_errors_without_private_arguments(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(
                demo, "load_profile", side_effect=ValueError("PRIVATE_SENTINEL")
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = demo.main(["check", "--config", "/private/profile.json"])
        self.assertEqual(code, 1)
        self.assertNotIn("PRIVATE_SENTINEL", stderr.getvalue())
        self.assertEqual(
            json.loads(stderr.getvalue())["code"], "demo_connection_failed"
        )
        self.assertEqual(stdout.getvalue(), "")

    def test_termination_signals_close_owned_tunnel_and_preserve_other_process(self):
        unrelated = self.spawn("import time;time.sleep(30)")
        root = Path(__file__).resolve().parents[3]
        try:
            for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                with (
                    self.subTest(signal=number),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    config = Path(directory) / "demo.json"
                    config.write_text(json.dumps(profile()))
                    config.chmod(0o600)
                    owned = Path(directory) / "owned.pid"
                    code = (
                        "from pathlib import Path\nimport sys\n"
                        "from search.operations.deployment import demo_connection as d\n"
                        "d.remote_check=lambda p:None\n"
                        "d.ssh_command=lambda p,**kw:[sys.executable,'-c','import time;time.sleep(30)']\n"
                        "d.available_port=lambda p:None\n"
                        f"d.verify_local=lambda t:Path({str(owned)!r}).write_text(str(t.process.pid))\n"
                        f"raise SystemExit(d.main(['connect','--config',{str(config)!r}]))\n"
                    )
                    child = subprocess.Popen(
                        [sys.executable, "-c", code],
                        cwd=root,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                    )
                    try:
                        deadline = time.monotonic() + 5
                        while (
                            not owned.exists()
                            and child.poll() is None
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)
                        self.assertTrue(owned.exists())
                        identifier = int(owned.read_text())
                        child.send_signal(number)
                        out, err = child.communicate(timeout=8)
                        self.assertEqual(child.returncode, 128 + number, err.decode())
                        self.assertEqual(
                            json.loads(out.splitlines()[-1])["status"], "closed"
                        )
                        with self.assertRaises(ProcessLookupError):
                            os.kill(identifier, 0)
                        self.assertIsNone(unrelated.poll())
                    finally:
                        if child.poll() is None:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.wait(timeout=5)
                        child.stdout.close()
                        child.stderr.close()
        finally:
            os.killpg(unrelated.pid, signal.SIGKILL)
            unrelated.wait(timeout=5)
            unrelated.stdout.close()
            unrelated.stderr.close()

    def test_remote_checker_has_real_process_timeout_and_output_bounds(self):
        cases = [
            ("import time;time.sleep(10)", "remote_check_timeout"),
            (
                "import sys,time;sys.stderr.write('PRIVATE_SENTINEL'*10000);sys.stderr.flush();time.sleep(10)",
                "remote_check_output_limit",
            ),
        ]
        for code, expected in cases:
            with (
                self.subTest(code=expected),
                mock.patch.object(
                    demo, "ssh_command", return_value=[sys.executable, "-c", code]
                ),
                mock.patch.object(demo, "CHECK_SECONDS", 0.3),
            ):
                with self.assertRaisesRegex(
                    demo.DemoConnectionError, "^" + expected + "$"
                ):
                    demo.remote_check(profile())
