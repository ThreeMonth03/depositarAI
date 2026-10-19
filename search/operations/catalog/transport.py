"""Bounded operator HTTPS to one previously verified OpenSearch container IP.

The host orchestrator must bind the IP and CA to sealed Docker/catalog evidence
before constructing this client. This component does not discover Docker peers,
grant deployment authority, accept a caller URL, or expose administrator output.
TLS still verifies the fixed OpenSearch hostname; connecting to its IP does not
disable certificate verification. There are no redirects, proxies or retries.
Payloads must come from the sealed projection/access builders, never an HTTP
caller. The endpoint allowlist alone is not a query DSL or bulk-body sandbox.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import math
import re
import socket
import ssl
import threading
import time

from search.serialization import strict_json
from search.operations.shared import bounded_json, private_io
from search.operations.shared.bounded_http import AbortableConnection


HOST = "opensearch-search"
PORT = 9200
MAX_CA_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 4 * 1024**2
MAX_RESPONSE_BYTES = 32 * 1024**2
MAX_JSON_DEPTH = bounded_json.MAX_DEPTH
REQUEST_SECONDS = 20
TOTAL_SECONDS = 600
INDEX = r"fess\.[0-9]{14,20}"
SECURITY = "/_plugins/_security/api/"
QUERY_USER = "depositar_filtered"


class TransportError(ValueError):
    """Fixed codes only; never include a URL, credential or response body."""


def _response_depth(value):
    """Bound nesting explicitly; decoder recursion limits vary by Python version."""
    try:
        if bounded_json.decoded_depth(value) > MAX_JSON_DEPTH:
            raise TransportError("catalog_transport_response_invalid")
    except bounded_json.BoundsError:
        raise TransportError("catalog_transport_response_invalid") from None


def _peer(value):
    if type(value) is not str:
        raise TransportError("catalog_transport_peer_invalid")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise TransportError("catalog_transport_peer_invalid") from None
    private_ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if str(address) != value or not any(
        address in ipaddress.IPv4Network(network) for network in private_ranges
    ):
        raise TransportError("catalog_transport_peer_invalid")
    return value


def _operation(method, path):
    if type(method) is not str or type(path) is not str or len(path) > 256:
        raise TransportError("catalog_transport_operation_invalid")
    allowed = (
        method == "GET"
        and (
            path == "/fess.search/_mapping"
            or re.fullmatch("/" + INDEX + "/_mapping", path)
            or re.fullmatch(
                re.escape(SECURITY)
                + r"(?:roles|internalusers|rolesmapping)/depositar_filtered",
                path,
            )
            or path == SECURITY + "internalusers"
        )
        or method == "POST"
        and (
            re.fullmatch("/" + INDEX + r"/_(?:search|bulk|refresh)", path)
            or path == "/fess_user.user/_search"
        )
        or method == "PUT"
        and (
            re.fullmatch("/" + INDEX + r"/_mapping", path)
            or re.fullmatch("/" + INDEX + r"/_doc/authorization-probe", path)
            or re.fullmatch(
                re.escape(SECURITY)
                + r"(?:roles|internalusers|rolesmapping)/depositar_filtered",
                path,
            )
        )
    )
    if not allowed:
        raise TransportError("catalog_transport_operation_invalid")


def _credential_operation(username, method, path):
    probe = (
        method == "GET"
        and path == SECURITY + "internalusers"
        or method == "POST"
        and path == "/fess_user.user/_search"
        or method == "PUT"
        and re.fullmatch("/" + INDEX + "/_doc/authorization-probe", path)
    )
    if username == QUERY_USER:
        if not (
            probe
            or method == "POST"
            and re.fullmatch("/" + INDEX + "/_search", path)
            or method in {"GET", "PUT"}
            and re.fullmatch("/" + INDEX + "/_mapping", path)
        ):
            raise TransportError("catalog_transport_credentials_operation_invalid")
    elif probe:
        raise TransportError("catalog_transport_credentials_operation_invalid")


class _PinnedHTTPSConnection(AbortableConnection, http.client.HTTPSConnection):
    def __init__(self, peer, *, context, timeout):
        super().__init__(HOST, PORT, context=context, timeout=timeout)
        self._peer = peer
        self._init_abortable()

    def connect(self):
        # No DNS, tunnel or proxy path. The fixed hostname remains the TLS SNI
        # and certificate hostname, independently of the selected Docker IP.
        raw = socket.create_connection((self._peer, PORT), self.timeout)
        try:
            self._publish_channel(raw)
            channel = self._context.wrap_socket(
                raw, server_hostname=HOST, do_handshake_on_connect=False
            )
            self._publish_channel(channel)
            channel.do_handshake()
        except BaseException:
            raw.close()
            raise


class OpenSearchClient:
    def __init__(
        self,
        peer,
        ca_pem,
        admin_password,
        *,
        clock=time.monotonic,
        budget_seconds=TOTAL_SECONDS,
    ):
        self._peer = _peer(peer)
        if (
            type(ca_pem) is not bytes
            or not 1 <= len(ca_pem) <= MAX_CA_BYTES
            or type(admin_password) is not str
            or re.fullmatch(r"[0-9a-f]{64}", admin_password) is None
            or type(budget_seconds) not in {int, float}
            or not math.isfinite(budget_seconds)
            or not 0 < budget_seconds <= TOTAL_SECONDS
        ):
            raise TransportError("catalog_transport_configuration_invalid")
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=ca_pem.decode("ascii"))
        except (ValueError, UnicodeError, ssl.SSLError):
            raise TransportError("catalog_transport_ca_invalid") from None
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self._context = context
        self._admin_password = admin_password
        self._clock = clock
        self._started = clock()
        if not math.isfinite(self._started):
            raise TransportError("catalog_transport_clock_invalid")
        self._last_tick = self._started
        self._deadline = self._started + budget_seconds

    def _remaining(self):
        tick = self._clock()
        if not math.isfinite(tick) or tick < self._last_tick or tick >= self._deadline:
            raise TransportError("catalog_transport_deadline_exceeded")
        self._last_tick = tick
        return min(REQUEST_SECONDS, self._deadline - tick)

    def request(
        self,
        method,
        path,
        payload=None,
        *,
        username=None,
        password=None,
        ndjson=False,
    ):
        _operation(method, path)
        if (username is None) != (password is None):
            raise TransportError("catalog_transport_credentials_invalid")
        if username is None:
            username, password = "admin", self._admin_password
        elif (
            username != QUERY_USER
            or type(password) is not str
            or re.fullmatch(r"[0-9a-f]{64}", password) is None
        ):
            raise TransportError("catalog_transport_credentials_invalid")
        _credential_operation(username, method, path)
        if type(ndjson) is not bool or ndjson != path.endswith("/_bulk"):
            raise TransportError("catalog_transport_payload_invalid")
        try:
            if ndjson:
                if type(payload) is not str or not payload.endswith("\n"):
                    raise TransportError("catalog_transport_payload_invalid")
                body = payload.encode("utf-8")
            else:
                if payload is not None and type(payload) is not dict:
                    raise TransportError("catalog_transport_payload_invalid")
                body = None if payload is None else private_io.canonical(payload)
        except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
            raise TransportError("catalog_transport_payload_invalid") from None
        if (
            body is not None
            and len(body) > MAX_REQUEST_BYTES
            or method == "GET"
            and body is not None
        ):
            raise TransportError("catalog_transport_payload_invalid")
        auth = base64.b64encode((username + ":" + password).encode("ascii"))
        remaining = self._remaining()
        connection = _PinnedHTTPSConnection(
            self._peer, context=self._context, timeout=remaining
        )
        expired = threading.Event()

        def expire():
            expired.set()
            connection.abort()

        # http.client header/readline and chunk framing may receive a slow drip
        # indefinitely without hitting an inactivity timeout. A per-request
        # absolute watchdog interrupts the underlying socket at the earlier of
        # the fixed 20-second request cap and the total remaining budget.
        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        response = None
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={
                    "Authorization": "Basic " + auth.decode("ascii"),
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/x-ndjson"
                    if ndjson
                    else "application/json",
                },
            )
            channel = connection.sock
            channel.settimeout(self._remaining())
            response = connection.getresponse()
            if (
                type(response.status) is not int
                or not 200 <= response.status <= 599
                or 300 <= response.status <= 399
                or response.getheader("Content-Encoding", "identity") != "identity"
            ):
                raise TransportError("catalog_transport_response_invalid")
            declared = response.getheader("Content-Length")
            if declared is not None and (
                not declared.isdecimal() or int(declared) > MAX_RESPONSE_BYTES
            ):
                raise TransportError("catalog_transport_response_invalid")
            data = bytearray()
            while not response.isclosed():
                channel.settimeout(self._remaining())
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise TransportError("catalog_transport_response_invalid")
            self._remaining()
            if expired.is_set():
                raise TransportError("catalog_transport_deadline_exceeded")
            if declared is not None and len(data) != int(declared):
                raise TransportError("catalog_transport_response_invalid")
            if not 200 <= response.status <= 299:
                # Denial/absence probes only require status. Never return an
                # administrator error body, even if it happens to be JSON.
                return response.status, None
            if (
                response.getheader("Content-Type", "").split(";", 1)[0].strip()
                != "application/json"
            ):
                raise TransportError("catalog_transport_response_invalid")
            raw = bytes(data)
            bounded_json.predecode(raw)
            value = strict_json(raw)
            if type(value) is not dict:
                raise TransportError("catalog_transport_response_invalid")
            _response_depth(value)
            self._remaining()
            if expired.is_set():
                raise TransportError("catalog_transport_deadline_exceeded")
            return response.status, value
        except (
            OSError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
            http.client.HTTPException,
        ):
            raise TransportError("catalog_transport_request_failed") from None
        finally:
            timer.cancel()
            timer.join(timeout=1)
            if response is not None:
                response.close()
            connection.finish()
