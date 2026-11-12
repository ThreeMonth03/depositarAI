"""Real cookie tampering and owner transitions never reuse an earlier
session.
"""

import json
import unittest

from tests.integration.public_entry import test_runtime as runtime_fixture


# Only synthetic credentials in the disposable probe container are accessed.
# Nothing returns raw cookie or bearer values to the host test process.
COOKIE_PROBE = (
    "\n"
    "from http.cookiejar import MozillaCookieJar\n"
    "from urllib.error import HTTPError\n"
    "from urllib.request import Request, build_opener, "
    "HTTPCookieProcessor, HTTPSHandler\n"
    "from pathlib import Path\n"
    "import copy\n"
    "import json\n"
    "import shutil\n"
    "import ssl\n"
    "import sys\n"
    "account, operation, argument = sys.argv[1:]\n"
    "path = Path('/tmp/' + account + '.cookies')\n"
    "if operation in ('snapshot', 'restore'):\n"
    "    backup = Path('/tmp/' + account + '.cookie-backup-' + argument)\n"
    "    source, destination = (path, backup) if operation == "
    "'snapshot' else (backup, path)\n"
    "    shutil.copyfile(source, destination)\n"
    "    print(json.dumps({'changed': True}))\n"
    "    raise SystemExit\n"
    "jar = MozillaCookieJar(str(path))\n"
    "jar.load(ignore_discard=True, ignore_expires=True)\n"
    "if operation == 'expire':\n"
    "    cookie = next(cookie for cookie in jar if cookie.name == argument)\n"
    "    cookie.expires = 1\n"
    "    jar.save(ignore_discard=True, ignore_expires=True)\n"
    "    print(json.dumps({'changed': True}))\n"
    "    raise SystemExit\n"
    "if operation == 'tamper':\n"
    "    cookie = next(cookie for cookie in jar if cookie.name == "
    "argument)\n"
    "    offset = len(cookie.value) // 2\n"
    "    replacement = 'A' if cookie.value[offset] != 'A' else 'B'\n"
    "    cookie.value = cookie.value[:offset] + replacement + cookie."
    "value[offset + 1:]\n"
    "    jar.save(ignore_discard=True, ignore_expires=True)\n"
    "    print(json.dumps({'changed': True}))\n"
    "    raise SystemExit\n"
    "if operation == 'copy_login':\n"
    "    other = MozillaCookieJar('/tmp/' + argument + '.cookies')\n"
    "    other.load(ignore_discard=True, ignore_expires=True)\n"
    "    cookie = next(cookie for cookie in other if cookie.name == "
    "'__Host-depositar-login')\n"
    "    jar.set_cookie(copy.copy(cookie))\n"
    "    jar.save(ignore_discard=True, ignore_expires=True)\n"
    "    print(json.dumps({'changed': True}))\n"
    "    raise SystemExit\n"
    "origin = 'https://public.test:9443'\n"
    "opener = build_opener(HTTPCookieProcessor(jar), "
    "HTTPSHandler(context=ssl.create_default_context(cafile='/fixture/tl"
    "s.crt')))\n"
    "if operation == 'delete':\n"
    "    saved = json.loads(Path('/tmp/' + account + '.session')."
    "read_text())\n"
    "    request = Request(origin + '/api/session', method='DELETE', "
    "headers={\n"
    "        'Origin': origin, 'Authorization': 'Bearer ' + "
    "saved['token']})\n"
    "elif operation == 'logout':\n"
    "    request = Request(origin + '/auth/logout')\n"
    "else:\n"
    "    raise ValueError('unsupported isolated cookie operation')\n"
    "try:\n"
    "    response = opener.open(request, timeout=10)\n"
    "except HTTPError as error:\n"
    "    response = error\n"
    "response.read(128 * 1024)\n"
    "jar.save(ignore_discard=True, ignore_expires=True)\n"
    "print(json.dumps({'status': response.status}))\n"
)


class IdentityGatewayTests(runtime_fixture.PublicEntryRuntimeFixture):
    mixed = True

    @classmethod
    def wait_ready(cls):
        try:
            return super().wait_ready()
        except AssertionError as error:
            logs = cls.compose(
                "logs",
                "--tail",
                "15",
                "public-entry",
                "public-web",
                "public-frontend",
                "tls-front",
            ).stdout[-4000:]
            for value in (
                "fixture-oidc-client-secret",
                *(letter * 64 for letter in "bcdef"),
            ):
                logs = logs.replace(value, "[fixture credential]")
            raise AssertionError(str(error) + "\n" + logs) from None

    def cookie(self, account, operation, argument="unused"):
        result = self.compose(
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
        return json.loads(result.stdout)

    def test_tamper_login_account_switch_and_logout_keep_session_ownership(
        self,
    ):
        account = "identity-transition"
        original = self.probe("access", account)["value"]["principal"]
        self.assertEqual(original["kind"], "guest")
        self.assertEqual(self.probe("session", account)["status"], 200)
        self.cookie(account, "snapshot", "guest")
        self.cookie(account, "tamper", "__Host-depositar-guest")
        replacement = self.probe("access", account)["value"]["principal"]
        self.assertEqual(replacement["kind"], "guest")
        self.assertNotEqual(replacement["id"], original["id"])
        denied = self.probe("turn", account, {"query": "氣候", "version": 0})
        self.assertEqual(denied["status"], 410, denied)
        self.assertEqual(self.cookie(account, "delete")["status"], 200)
        self.assertEqual(self.calls(), 0)

        # Wrong-owner cleanup did not remove the original guest's session.
        self.cookie(account, "restore", "guest")
        self.assertEqual(
            self.probe("turn", account, {"query": "氣候", "version": 0})[
                "status"
            ],
            200,
        )
        self.assertEqual(self.calls(), 1)

        self.assertEqual(self.probe("login", account)["status"], 200)
        self.assertEqual(
            self.probe("access", account)["value"]["principal"]["kind"], "user"
        )
        # Login does not promote a bearer allocated to the guest principal.
        denied = self.probe("turn", account, {"query": "氣候", "version": 1})
        self.assertEqual(denied["status"], 410, denied)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.probe("session", account)["status"], 200)
        self.cookie(account, "snapshot", "user")

        self.assertEqual(self.probe("login", "other-identity")["status"], 200)
        self.cookie(account, "copy_login", "other-identity")
        # A valid different OIDC account still cannot use or delete this
        # bearer.
        denied = self.probe("turn", account, {"query": "氣候", "version": 0})
        self.assertEqual(denied["status"], 410, denied)
        self.assertEqual(self.cookie(account, "delete")["status"], 200)
        self.assertEqual(self.calls(), 1)
        self.cookie(account, "restore", "user")
        self.assertEqual(
            self.probe("turn", account, {"query": "氣候", "version": 0})[
                "status"
            ],
            200,
        )
        self.assertEqual(self.calls(), 2)

        self.cookie(account, "tamper", "__Host-depositar-login")
        denied = self.probe("access", account)
        self.assertEqual(denied["status"], 401, denied)
        self.assertEqual(self.calls(), 2)
        self.cookie(account, "restore", "user")
        self.assertEqual(self.cookie(account, "logout")["status"], 200)
        after_logout = self.probe("access", account)["value"]["principal"]
        self.assertEqual(after_logout["kind"], "guest")
        self.assertEqual(self.calls(), 2)


class MixedSessionManagementTests(
    runtime_fixture.PublicEntrySessionManagementTests
):
    mixed = True


if __name__ == "__main__":
    unittest.main()
