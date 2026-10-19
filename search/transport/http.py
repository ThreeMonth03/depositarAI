"""Bounded HTTP primitives with no proxy, redirects or automatic retries.

Callers validate destinations, operation allowlists and response contracts.
Transport failures use fixed, sanitized diagnostic strings.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Optional, Protocol
import urllib.error
import urllib.request


@dataclass(frozen=True)
class HTTPRequest:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: Optional[bytes] = None


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    url: str


class HTTPTransport(Protocol):
    def send(
        self, request: HTTPRequest, *, timeout: float, max_bytes: int
    ) -> HTTPResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class UrllibTransport:
    """Bounded proxy-free HTTP; callers own origin and operation policy."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def send(
        self, request: HTTPRequest, *, timeout: float, max_bytes: int
    ) -> HTTPResponse:
        if not math.isfinite(timeout) or timeout <= 0 or max_bytes <= 0:
            raise OSError("invalid transport bound")
        raw_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        response: Any
        deadline = time.monotonic() + timeout
        try:
            response = self._opener.open(raw_request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        except Exception:
            raise OSError("Ollama transport failed") from None
        try:
            # HTTPError wraps HTTPResponse; only its body reader is one level
            # deeper. Keep error metadata and closure on the outer response.
            read_response = (
                response.fp
                if isinstance(response, urllib.error.HTTPError)
                else response
            )
            parts: list[bytes] = []
            body_size = 0
            while body_size <= max_bytes:
                remaining = deadline - time.monotonic()
                if not math.isfinite(remaining) or remaining <= 0:
                    raise TimeoutError("Ollama transport deadline expired")
                # Python 3.13 clears ``HTTPResponse.fp`` immediately when a
                # small Content-Length-delimited body is consumed by read1().
                # That is a normal EOF, not a loss of deadline enforcement.
                # A prematurely short declared body is still rejected later by
                # the exact Content-Length check in _OllamaAdapter._send().
                is_closed = getattr(read_response, "isclosed", None)
                if callable(is_closed) and is_closed():
                    break
                fp = getattr(read_response, "fp", None)
                raw = getattr(fp, "raw", None)
                socket = getattr(raw, "_sock", None)
                if socket is None or not callable(getattr(socket, "settimeout", None)):
                    raise OSError("cannot enforce Ollama transport deadline")
                socket.settimeout(max(0.001, remaining))
                read1 = getattr(read_response, "read1", None)
                if not callable(read1):
                    raise OSError("cannot enforce bounded Ollama body reads")
                part = read1(min(64 * 1024, max_bytes + 1 - body_size))
                if not part:
                    break
                parts.append(part)
                body_size += len(part)
            body = b"".join(parts)
            if len(body) > max_bytes:
                raise OSError("Ollama response exceeded its cap")
            return HTTPResponse(
                status=int(response.code),
                headers=tuple(
                    (str(name), str(value)) for name, value in response.headers.items()
                ),
                body=body,
                url=str(response.geturl()),
            )
        finally:
            try:
                response.close()
            except Exception:
                pass
