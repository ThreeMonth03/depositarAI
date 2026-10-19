"""Cookie authentication and bounded fixed-origin Fess administration requests."""

from __future__ import annotations

import http.cookies
import ipaddress
import math
import re
import time
from typing import (
    Any,
    Callable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
import urllib.error
import urllib.parse
import urllib.request

from .codec import _canonical_json, _exact_int, _strict_json
from .errors import (
    AuthenticationError,
    ConfigurationError,
    FessBusy,
    ProtocolError,
    _HTTP_FAILURE_CAUSES,
    _diagnosed,
)
from .transport import (
    Deadline,
    HTTPRequest,
    HTTPResponse,
    HTTPTransport,
    MAX_RESPONSE_HEADER_BYTES,
    UrllibTransport,
)

MAX_BULK_REQUEST_BYTES = 16 * 1024 * 1024


MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024


TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{20,512}$")


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{16,256}$")


CSRF_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")


def _validate_base_url(base_url: str) -> Tuple[str, Tuple[str, str, int]]:
    try:
        parsed = urllib.parse.urlsplit(base_url)
        address = ipaddress.ip_address(parsed.hostname or "")
    except (TypeError, ValueError):
        raise ConfigurationError(
            "Fess base URL must use a numeric private address"
        ) from None
    private_v4 = address.version == 4 and (
        address.is_loopback
        or address in ipaddress.ip_network("10.0.0.0/8")
        or address in ipaddress.ip_network("172.16.0.0/12")
        or address in ipaddress.ip_network("192.168.0.0/16")
    )
    if (
        parsed.scheme != "http"
        or not private_v4
        or parsed.port != 8080
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("Fess base URL is outside the private HTTP boundary")
    normalized = "http://%s:8080" % address.compressed
    return normalized, ("http", address.compressed, 8080)


class FessSession:
    """Bounded cookie authentication and catalog search session."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: Optional[HTTPTransport] = None,
        request_timeout: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url, self.origin = _validate_base_url(base_url)
        if (
            isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not math.isfinite(float(request_timeout))
            or not 0 < float(request_timeout) <= 60
        ):
            raise ConfigurationError("request timeout must be finite and in (0, 60]")
        self.transport = transport or UrllibTransport()
        self.request_timeout = float(request_timeout)
        self.clock = clock
        self._session_id: Optional[str] = None
        self._csrf_token: Optional[str] = None
        self._admin_keepalive_at: Optional[float] = None
        self._token_creation_dispatched = False
        self._document_bulk_dispatched = False

    def _fixed_url(
        self, path: str, query: Optional[Sequence[Tuple[str, str]]] = None
    ) -> str:
        # All call sites pass literals or validated, quoted opaque IDs.
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
        ):
            raise ConfigurationError("internal Fess endpoint is invalid")
        encoded = urllib.parse.urlencode(query or (), doseq=True)
        return self.base_url + path + (("?" + encoded) if encoded else "")

    @staticmethod
    def _assert_allowlisted_endpoint(method: str, path: str) -> None:
        if (method, path) not in {
            ("POST", "/api/v2/auth/login"),
            ("POST", "/api/v2/auth/logout"),
        }:
            raise ConfigurationError("internal Fess endpoint is not allowlisted")

    def _request(
        self,
        method: str,
        path: str,
        *,
        deadline: Deadline,
        query: Optional[Sequence[Tuple[str, str]]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        form_body: Optional[Mapping[str, str]] = None,
        token: Optional[str] = None,
        max_bytes: int = MAX_JSON_RESPONSE_BYTES,
        allow_redirect_path: Optional[str] = None,
        accepted_http_statuses: Sequence[int] = (),
        require_session: bool = False,
        csrf: bool = False,
        on_dispatch: Optional[Callable[[], None]] = None,
    ) -> HTTPResponse:
        if json_body is not None and form_body is not None:
            raise ConfigurationError("internal request has multiple body encodings")
        if method not in ("GET", "POST", "PUT", "DELETE"):
            raise ConfigurationError("internal request method is invalid")
        self._assert_allowlisted_endpoint(method, path)
        headers: List[Tuple[str, str]] = [("Accept", "application/json")]
        if path.startswith("/admin/"):
            # Access-token detail labels are localized by Fess.  Keep the
            # reviewed structural parser independent of the host/browser
            # locale; callers cannot override request headers through this
            # fixed endpoint client.
            headers.append(("Accept-Language", "en"))
        body: Optional[bytes] = None
        if json_body is not None:
            body = _canonical_json(json_body)
            headers.append(("Content-Type", "application/json"))
        elif form_body is not None:
            body = urllib.parse.urlencode(tuple(form_body.items())).encode("utf-8")
            headers[0] = ("Accept", "text/html")
            headers.append(("Content-Type", "application/x-www-form-urlencoded"))
        elif path.startswith("/admin/"):
            headers[0] = ("Accept", "text/html")
        if body is not None and len(body) > MAX_BULK_REQUEST_BYTES:
            raise ProtocolError("Fess request body exceeded its byte cap")
        if token is not None:
            if TOKEN_RE.fullmatch(token) is None:
                raise AuthenticationError("ephemeral access token is malformed")
            headers.append(("Authorization", "Bearer " + token))
        if require_session:
            if (
                self._session_id is None
                or SESSION_RE.fullmatch(self._session_id) is None
            ):
                raise AuthenticationError("Fess session is unavailable")
            headers.append(("Cookie", "JSESSIONID=" + self._session_id))
        if csrf:
            if self._csrf_token is None or CSRF_RE.fullmatch(self._csrf_token) is None:
                raise AuthenticationError("Fess CSRF state is unavailable")
            headers.append(("X-Fess-CSRF-Token", self._csrf_token))
        url = self._fixed_url(path, query)
        request = HTTPRequest(method=method, url=url, headers=tuple(headers), body=body)
        timeout = deadline.remaining(self.request_timeout)
        if on_dispatch is not None:
            on_dispatch()
        response = self.transport.send(
            request,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        if not isinstance(response, HTTPResponse):
            raise ProtocolError("Fess transport returned an unsupported response")
        if not _exact_int(response.status, minimum=100, maximum=599):
            raise ProtocolError("Fess response status is malformed")
        if not isinstance(response.body, bytes) or len(response.body) > max_bytes:
            raise ProtocolError("Fess response exceeded its byte cap")
        if not isinstance(response.headers, tuple):
            raise ProtocolError("Fess response headers are malformed")
        header_bytes = 0
        for item in response.headers:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or any(character in item[0] + item[1] for character in "\r\n")
            ):
                raise ProtocolError("Fess response headers are malformed")
            header_bytes += (
                len(item[0].encode("utf-8")) + len(item[1].encode("utf-8")) + 4
            )
        if header_bytes > MAX_RESPONSE_HEADER_BYTES:
            raise ProtocolError("Fess response headers exceeded their cap")
        lengths = response.header_values("Content-Length")
        if len(lengths) > 1:
            raise ProtocolError("Fess response has ambiguous content length")
        if lengths and (
            not lengths[0].isascii()
            or not lengths[0].isdigit()
            or int(lengths[0]) != len(response.body)
        ):
            raise ProtocolError("Fess response content length is malformed")
        try:
            parsed = urllib.parse.urlsplit(response.url)
            response_origin = (
                parsed.scheme,
                ipaddress.ip_address(parsed.hostname or "").compressed,
                parsed.port,
            )
        except (TypeError, ValueError):
            raise ProtocolError("Fess response origin is malformed") from None
        if response_origin != self.origin or response.url != url:
            raise ProtocolError("Fess transport crossed the fixed origin")
        if 300 <= response.status < 400:
            locations = response.header_values("Location")
            if len(locations) != 1 or allow_redirect_path is None:
                raise ProtocolError("Fess redirect was not explicitly expected")
            redirected = urllib.parse.urlsplit(urllib.parse.urljoin(url, locations[0]))
            if (
                redirected.scheme != self.origin[0]
                or redirected.hostname != self.origin[1]
                or redirected.port != self.origin[2]
                or redirected.path != allow_redirect_path
                or redirected.query
                or redirected.fragment
            ):
                raise ProtocolError("Fess returned an unsafe redirect target")
        elif response.status in accepted_http_statuses:
            pass
        elif response.status == 429:
            values = response.header_values("Retry-After")
            if len(values) > 1 or (
                values
                and (
                    not values[0].isascii()
                    or not values[0].isdigit()
                    or len(values[0]) > 6
                )
            ):
                # An HTTP-date or malformed hint is not permission to retry
                # sooner. This pinned Fess version emits delta-seconds.
                raise _diagnosed(
                    ProtocolError("Fess Retry-After is not a supported delta"),
                    cause="http_429",
                )
            raise FessBusy(int(values[0]) if values else None)
        elif not 200 <= response.status < 300:
            # The body is intentionally neither copied nor interpolated.
            raise _diagnosed(
                ProtocolError("Fess returned an unsuccessful HTTP status"),
                cause=_HTTP_FAILURE_CAUSES.get(response.status, "http_error"),
            )
        return response

    @staticmethod
    def _envelope(
        response: HTTPResponse, *, accepted_statuses: Sequence[int] = (0,)
    ) -> Mapping[str, Any]:
        root = _strict_json(response.body)
        value = root.get("response") if isinstance(root, Mapping) else None
        if not isinstance(value, Mapping) or type(value.get("status")) is not int:
            raise ProtocolError("Fess returned an invalid API envelope")
        if value["status"] not in accepted_statuses:
            raise _diagnosed(
                ProtocolError("Fess API operation was rejected"), cause="api_rejected"
            )
        return value

    @staticmethod
    def _one_session_cookie(response: HTTPResponse) -> str:
        values: List[str] = []
        for raw in response.header_values("Set-Cookie"):
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(raw)
            except http.cookies.CookieError:
                raise AuthenticationError("Fess login cookie is malformed") from None
            if "JSESSIONID" in jar:
                values.append(jar["JSESSIONID"].value)
        if len(values) != 1 or SESSION_RE.fullmatch(values[0]) is None:
            raise AuthenticationError(
                "Fess login did not issue one safe session cookie"
            )
        return values[0]

    def login(
        self,
        username: str,
        password: str,
        deadline: Deadline,
    ) -> None:
        if USERNAME_RE.fullmatch(username or "") is None:
            raise AuthenticationError("Fess username is malformed")
        if (
            not isinstance(password, str)
            or not password
            or len(password) > 4096
            or "\x00" in password
        ):
            raise AuthenticationError("Fess password is malformed")
        if self._session_id is not None:
            raise AuthenticationError("Fess session is already authenticated")
        response = self._request(
            "POST",
            "/api/v2/auth/login",
            deadline=deadline,
            json_body={"username": username, "password": password},
            max_bytes=64 * 1024,
        )
        envelope = self._envelope(response)
        user = envelope.get("user")
        csrf = envelope.get("csrf_token")
        if (
            not isinstance(user, Mapping)
            or not isinstance(csrf, str)
            or CSRF_RE.fullmatch(csrf) is None
        ):
            raise AuthenticationError("Fess login response is invalid")
        self._session_id = self._one_session_cookie(response)
        self._csrf_token = csrf

    def logout(self, deadline: Deadline) -> None:
        if self._session_id is None:
            return
        forget = True
        try:
            response = self._request(
                "POST",
                "/api/v2/auth/logout",
                deadline=deadline,
                json_body={},
                max_bytes=64 * 1024,
                require_session=True,
                csrf=True,
            )
            self._envelope(response)
        except FessBusy:
            # Only an explicit pre-handler HTTP 429 permits a bounded retry.
            # Other failures may mean logout committed; never replay those.
            forget = False
            raise
        finally:
            if forget:
                self._forget_session()

    def _forget_session(self) -> None:
        self._session_id = None
        self._csrf_token = None
        self._admin_keepalive_at = None
