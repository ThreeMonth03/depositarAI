"""Isolated CI identity provider and inference counter; never a production IdP."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
from html import unescape
from http.cookies import SimpleCookie
from http.cookiejar import Cookie, MozillaCookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener


ISSUER = "https://oidc.test:9443"
ORIGIN = "https://public.test:9443"
CLIENT_ID = "public-entry-test"
CLIENT_SECRET = "fixture-oidc-client-secret"
API_KEY = "c" * 64


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def signed_token(claims, key):
    header = b64(json.dumps({"alg": "RS256", "kid": "fixture"}).encode())
    payload = b64(json.dumps(claims, separators=(",", ":")).encode())
    message = (header + "." + payload).encode("ascii")
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key)],
        input=message,
        capture_output=True,
        check=True,
        timeout=5,
    ).stdout
    return message.decode("ascii") + "." + b64(signature)


def serve(directory):
    """Both services are reachable only on the disposable internal test network."""
    root = Path(directory)
    modulus = (
        subprocess.run(
            ["openssl", "rsa", "-in", str(root / "idp.key"), "-noout", "-modulus"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        .stdout.strip()
        .split("=", 1)[1]
    )
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "kid": "fixture",
        "alg": "RS256",
        "n": b64(bytes.fromhex(modulus)),
        "e": "AQAB",
    }
    codes, access_tokens = {}, {}
    counter = {"calls": 0}
    counter_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, value, headers=()):
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path)
            if path.path == "/.well-known/openid-configuration":
                return self.reply(
                    200,
                    {
                        "issuer": ISSUER,
                        "authorization_endpoint": ISSUER + "/authorize",
                        "token_endpoint": ISSUER + "/token",
                        "userinfo_endpoint": ISSUER + "/userinfo",
                        "jwks_uri": ISSUER + "/jwks",
                        "response_types_supported": ["code"],
                        "subject_types_supported": ["public"],
                        "id_token_signing_alg_values_supported": ["RS256"],
                        "token_endpoint_auth_methods_supported": [
                            "client_secret_post",
                            "client_secret_basic",
                        ],
                    },
                )
            if path.path == "/jwks":
                return self.reply(200, {"keys": [jwk]})
            if path.path == "/authorize":
                query = parse_qs(path.query)
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                account = cookies.get("fixture_account")
                account = account.value if account else "alice"
                if query.get("client_id") != [CLIENT_ID] or query.get(
                    "redirect_uri"
                ) != [ORIGIN + "/auth/callback"]:
                    return self.reply(400, {"error": "invalid_request"})
                code = secrets.token_urlsafe(24)
                codes[code] = {
                    "account": account,
                    "nonce": query.get("nonce", [None])[0],
                }
                location = (
                    ORIGIN
                    + "/auth/callback?"
                    + urlencode(
                        {
                            "code": code,
                            "state": query.get("state", [""])[0],
                        }
                    )
                )
                return self.reply(302, {}, [("Location", location)])
            if path.path == "/userinfo":
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                account = access_tokens.get(token)
                if account is None:
                    return self.reply(401, {"error": "invalid_token"})
                return self.reply(
                    200,
                    {
                        "iss": ISSUER,
                        "sub": account,
                        "email": account + "@example.test",
                        "email_verified": True,
                    },
                )
            if path.path == "/__calls":
                with counter_lock:
                    snapshot = dict(counter)
                return self.reply(200, snapshot)
            return self.reply(404, {})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                return self.reply(413, {})
            raw = self.rfile.read(length)
            if self.path == "/token":
                query = parse_qs(raw.decode("ascii"))
                basic = (
                    "Basic "
                    + base64.b64encode(
                        (CLIENT_ID + ":" + CLIENT_SECRET).encode()
                    ).decode()
                )
                authenticated = self.headers.get("Authorization") == basic or (
                    query.get("client_id") == [CLIENT_ID]
                    and query.get("client_secret") == [CLIENT_SECRET]
                )
                record = codes.pop(query.get("code", [""])[0], None)
                if not authenticated or record is None:
                    return self.reply(400, {"error": "invalid_grant"})
                now = int(time.time())
                account = record["account"]
                claims = {
                    "iss": ISSUER,
                    "sub": account,
                    "aud": CLIENT_ID,
                    "iat": now,
                    "exp": now + 600,
                    "auth_time": now,
                    "nonce": record["nonce"],
                }
                if account == "wrong-issuer":
                    claims["iss"] = "https://untrusted-issuer.example"
                elif account == "wrong-audience":
                    claims["aud"] = "unrelated-client"
                elif account == "wrong-nonce":
                    claims["nonce"] = "unrelated-authorization-request"
                elif account == "number-audience":
                    claims["aud"] = 42
                elif account == "array-audience":
                    claims["aud"] = [CLIENT_ID, "another-client"]
                    claims["azp"] = CLIENT_ID
                key = root / ("attacker.key" if account == "forged" else "idp.key")
                access = secrets.token_urlsafe(32)
                access_tokens[access] = account
                return self.reply(
                    200,
                    {
                        "access_token": access,
                        "token_type": "Bearer",
                        "expires_in": 600,
                        "scope": "openid profile email",
                        "id_token": signed_token(claims, key),
                    },
                )
            if self.path == "/v1/turn":
                if self.headers.get("Authorization") != "Bearer " + API_KEY:
                    return self.reply(401, {"error": "invalid_api_key"})
                with counter_lock:
                    counter["calls"] += 1
                return self.reply(
                    200,
                    {
                        "status": "ok",
                        "message": "isolated fixture",
                        "state": {"topic": "fixture", "time": None, "formats": []},
                        "reason": "none",
                        "revision": "a" * 40,
                        "timing_ms": {},
                        "search": None,
                    },
                )
            return self.reply(404, {})

    private = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    threading.Thread(target=private.serve_forever, daemon=True).start()
    provider = ThreadingHTTPServer(("0.0.0.0", 9443), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(root / "tls.crt", root / "tls.key")
    provider.socket = tls.wrap_socket(provider.socket, server_side=True)
    provider.serve_forever()


def burst(directory, payload):
    """Send at most twelve requests together with isolated browser snapshots.

    Cookie/session files are read before threads start and never written by
    workers. Results intentionally contain no session tokens or cookie values.
    """
    root = Path(directory)
    if (
        type(payload) is not dict
        or set(payload) != {"action", "requests"}
        or payload["action"] not in {"turn", "session"}
        or type(payload["requests"]) is not list
        or not 1 <= len(payload["requests"]) <= 12
    ):
        raise ValueError("bounded burst fixture requires one through twelve requests")
    action = payload["action"]
    browsers = {}
    for item in payload["requests"]:
        if (
            type(item) is not dict
            or set(item) != {"account", "payload"}
            or type(item["account"]) is not str
            or not item["account"].isascii()
            or not item["account"].replace("-", "").isalnum()
            or not 1 <= len(item["account"]) <= 64
            or type(item["payload"]) is not dict
        ):
            raise ValueError("invalid bounded burst fixture request")
        account = item["account"]
        if account not in browsers:
            jar = MozillaCookieJar("/tmp/" + account + ".cookies")
            jar.load(ignore_discard=True, ignore_expires=True)
            token = None
            if action == "turn":
                token = json.loads(Path("/tmp/" + account + ".session").read_text())[
                    "token"
                ]
            browsers[account] = (tuple(jar), token)
    barrier = threading.Barrier(len(payload["requests"]))
    context = ssl.create_default_context(cafile=root / "tls.crt")

    def request(item):
        account = item["account"]
        cookies, token = browsers[account]
        jar = MozillaCookieJar()
        for cookie in cookies:
            jar.set_cookie(copy.copy(cookie))
        opener = build_opener(HTTPCookieProcessor(jar), HTTPSHandler(context=context))
        headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        outgoing = Request(
            ORIGIN + ("/api/turn" if action == "turn" else "/api/session"),
            data=json.dumps(item["payload"]).encode(),
            headers=headers,
            method="POST",
        )
        barrier.wait(timeout=5)
        try:
            response = opener.open(outgoing, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(16384)
            status = response.status
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            value = None
        error = value.get("error") if isinstance(value, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        return {"account": account, "status": status, "code": code}

    with ThreadPoolExecutor(max_workers=len(payload["requests"])) as workers:
        return {"results": list(workers.map(request, payload["requests"]))}


def probe(action, account, directory, payload=None):
    if action == "burst":
        print(json.dumps(burst(directory, payload)))
        return
    root = Path(directory)
    jar = MozillaCookieJar("/tmp/" + account + ".cookies")
    if Path(jar.filename).exists():
        jar.load(ignore_discard=True, ignore_expires=True)
    context = ssl.create_default_context(cafile=root / "tls.crt")
    opener = build_opener(HTTPCookieProcessor(jar), HTTPSHandler(context=context))
    headers = {}
    data = None
    method = "GET"
    if action == "verify":

        def fetch(url):
            try:
                return opener.open(url, timeout=10)
            except HTTPError as error:
                return error

        with fetch(ORIGIN + "/verify") as response:
            text = response.read(128 * 1024).decode("utf-8")
        match = re.search(
            r'<script[^>]*id="anubis_challenge"[^>]*>(.*?)</script>', text, re.S
        )
        if not match:
            raise ValueError("fixture expected bounded Anubis challenge")
        challenge = json.loads(unescape(match[1]))
        data, difficulty = challenge["challenge"], challenge["rules"]["difficulty"]
        if type(difficulty) is not int or not 1 <= difficulty <= 4:
            raise ValueError("fixture challenge difficulty exceeded")
        for nonce in range(1000000):
            digest = hashlib.sha256(
                (data["randomData"] + str(nonce)).encode()
            ).hexdigest()
            if digest.startswith("0" * difficulty):
                break
        else:
            raise ValueError("fixture challenge budget exceeded")
        query = urlencode(
            {
                "id": data["id"],
                "response": digest,
                "nonce": nonce,
                "redir": ORIGIN + "/verify",
                "elapsedTime": 1000,
            }
        )
        with fetch(
            ORIGIN + "/.within.website/x/cmd/anubis/api/pass-challenge?" + query
        ) as response:
            response.read(128 * 1024)
            status = response.status
        jar.save(ignore_discard=True, ignore_expires=True)
        print(json.dumps({"status": status}))
        return
    if action == "login":
        jar.set_cookie(
            Cookie(
                0,
                "fixture_account",
                account,
                None,
                False,
                "oidc.test",
                False,
                False,
                "/",
                True,
                True,
                None,
                True,
                None,
                None,
                {},
                False,
            )
        )
        url = ORIGIN + "/auth/login"
    elif action == "counter":
        url = "http://conversation-api:8000/__calls"
    elif action in ("access", "health"):
        url = ORIGIN + ("/api/access" if action == "access" else "/healthz")
    else:
        url = ORIGIN + (
            payload["path"]
            if action == "path"
            else "/api/turn"
            if action == "turn"
            else "/api/session"
        )
        method = payload.get("method", "POST") if action == "path" else "POST"
        data = json.dumps(
            {"query": "氣候", "version": 0} if action == "path" else payload or {}
        ).encode()
        headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
        if action == "turn":
            saved = json.loads(Path("/tmp/" + account + ".session").read_text())
            headers["Authorization"] = "Bearer " + saved["token"]
        if action in ("spoof", "path"):
            headers.update(
                {
                    "X-Depositar-Subject": hashlib.sha256(
                        (ISSUER + "\x00alice").encode()
                    ).hexdigest(),
                    "X-Depositar-Edge-Key": "d" * 64,
                    "X-Userinfo": b64(
                        json.dumps({"iss": ISSUER, "sub": "alice"}).encode()
                    ),
                    "X-ID-Token": b64(
                        json.dumps({"iss": ISSUER, "sub": "alice"}).encode()
                    ),
                }
            )
            if action == "path":
                stolen = json.loads(Path("/tmp/alice.session").read_text())
                headers["Authorization"] = "Bearer " + stolen["token"]
    request = Request(url, data=data, headers=headers, method=method)
    try:
        response = opener.open(request, timeout=10)
    except HTTPError as error:
        response = error
    raw = response.read(16384)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        value = None
    if action == "session" and response.status == 200:
        Path("/tmp/" + account + ".session").write_text(json.dumps(value))
    jar.save(ignore_discard=True, ignore_expires=True)
    # Session tokens and cookies stay inside the disposable probe container.
    if isinstance(value, dict) and "token" in value:
        value = {key: val for key, val in value.items() if key != "token"}
    cookie_flags = [
        {
            "name": cookie.name,
            "secure": cookie.secure,
            "path": cookie.path,
            "domain_specified": cookie.domain_specified,
            "http_only": "HttpOnly" in cookie._rest,
            "same_site": cookie._rest.get("SameSite"),
        }
        for cookie in jar
        if cookie.domain == "public.test"
    ]
    print(
        json.dumps({"status": response.status, "value": value, "cookies": cookie_flags})
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("serve", "probe"))
    parser.add_argument("--directory", default="/fixture")
    parser.add_argument("--action")
    parser.add_argument("--account", default="anonymous")
    parser.add_argument("--payload")
    args = parser.parse_args()
    if args.role == "serve":
        serve(args.directory)
    else:
        probe(
            args.action,
            args.account,
            args.directory,
            json.loads(args.payload or "null"),
        )
