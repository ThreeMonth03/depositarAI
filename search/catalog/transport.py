"""Bounded HTTP values, monotonic deadlines and proxy-free transport."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Protocol,
    Tuple,
)
import urllib.error
import urllib.parse
import urllib.request

from .errors import (
    CatalogError,
    CatalogTimeout,
    ConfigurationError,
    ProtocolError,
    _transport_failure,
)

MAX_RESPONSE_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True)
class Deadline:
    end: float
    clock: Callable[[], float]

    @classmethod
    def after(
        cls, seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> "Deadline":
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(float(seconds))
            or not 0 < float(seconds) <= 3600
        ):
            raise ConfigurationError("deadline must be finite and in (0, 3600]")
        return cls(clock() + float(seconds), clock)

    def remaining(self, cap: float) -> float:
        value = min(float(cap), self.end - self.clock())
        if not math.isfinite(value) or value <= 0:
            raise CatalogTimeout("operation deadline expired")
        return value


@dataclass(frozen=True)
class HTTPRequest:
    method: str
    url: str
    headers: Tuple[Tuple[str, str], ...]
    body: Optional[bytes] = None

    def header(self, name: str) -> Optional[str]:
        matches = [
            value for key, value in self.headers if key.casefold() == name.casefold()
        ]
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Tuple[Tuple[str, str], ...]
    body: bytes
    url: str

    def header_values(self, name: str) -> List[str]:
        return [
            value for key, value in self.headers if key.casefold() == name.casefold()
        ]


class HTTPTransport(Protocol):
    def send(
        self, request: HTTPRequest, *, timeout: float, max_bytes: int
    ) -> HTTPResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class UrllibTransport:
    """A proxy-free transport with automatic redirect handling disabled."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def send(
        self, request: HTTPRequest, *, timeout: float, max_bytes: int
    ) -> HTTPResponse:
        if timeout <= 0 or max_bytes <= 0:
            raise ConfigurationError("transport bounds must be positive")
        raw_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        response: Any
        try:
            response = self._opener.open(raw_request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # Redirects and ordinary HTTP errors are data for the strict protocol
            # layer.  Never include their body or request headers in an exception.
            response = exc
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise _transport_failure(error) from None
        try:
            header_pairs = tuple(
                (str(key), str(value)) for key, value in response.headers.items()
            )
            header_bytes = sum(len(key) + len(value) + 4 for key, value in header_pairs)
            if header_bytes > MAX_RESPONSE_HEADER_BYTES:
                raise ProtocolError("Fess response headers exceeded their cap")
            lengths = [
                value
                for key, value in header_pairs
                if key.casefold() == "content-length"
            ]
            if len(lengths) > 1:
                raise ProtocolError("Fess response has ambiguous content length")
            if lengths:
                if not lengths[0].isascii() or not lengths[0].isdigit():
                    raise ProtocolError("Fess response content length is malformed")
                if int(lengths[0]) > max_bytes:
                    raise ProtocolError("Fess response exceeded its byte cap")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ProtocolError("Fess response exceeded its byte cap")
            return HTTPResponse(
                status=int(response.code),
                headers=header_pairs,
                body=body,
                url=str(response.geturl()),
            )
        except CatalogError:
            raise
        except Exception as error:
            raise _transport_failure(error) from None
        finally:
            try:
                response.close()
            except Exception:
                pass
