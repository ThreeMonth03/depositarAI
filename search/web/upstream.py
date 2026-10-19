"""Fixed operator-owned destination, TLS verification, no redirects or proxies."""

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from search.secrets import read_secret
from search.serialization import strict_json
from search.api.conversation.contract import TurnResponse


@dataclass(frozen=True)
class WebSettings:
    origin: str
    upstream: str
    key_file: Path
    revision: str = "development"
    static_dir: Path = Path("/app/web-dist")
    edge_secret_file: Path | None = None
    upstream_ca_file: Path | None = None

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or url.path
            or not url.hostname
        ):
            raise ValueError("origin must be a bare origin")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname == "127.0.0.1"
        ):
            raise ValueError("HTTP is allowed only for the local private demo")
        peer = urlsplit(self.upstream)
        if (
            peer.username
            or peer.password
            or peer.query
            or peer.fragment
            or peer.path
            or not peer.hostname
        ):
            raise ValueError("upstream must be an operator configured origin")
        if peer.scheme != "https" and self.upstream != "http://conversation-api:8000":
            raise ValueError("remote GPU connections require verified HTTPS")
        if self.upstream_ca_file is not None and (
            not self.upstream_ca_file.is_absolute() or peer.scheme != "https"
        ):
            raise ValueError("upstream CA requires HTTPS and an absolute file path")

    @classmethod
    def from_environment(cls):
        upstream_ca_file = os.environ.get("WEB_UPSTREAM_CA_FILE")
        return cls(
            os.environ["WEB_ORIGIN"],
            os.environ["WEB_UPSTREAM"],
            Path("/run/secrets/api_key"),
            os.environ.get("SOURCE_REVISION", "development"),
            edge_secret_file=(
                Path(os.environ["WEB_EDGE_SECRET_FILE"])
                if "WEB_EDGE_SECRET_FILE" in os.environ
                else None
            ),
            upstream_ca_file=Path(upstream_ca_file) if upstream_ca_file else None,
        )


class UpstreamError(Exception):
    def __init__(self, status=503):
        self.status = status


class Upstream:
    def __init__(self, settings):
        self.settings = settings
        # Keep HTTPX's public CA roots, without environment overrides. A private
        # CA extends that trust store; certificate and hostname checks stay on.
        self.tls_context = httpx.create_ssl_context(trust_env=False)
        if settings.upstream_ca_file is not None:
            self.tls_context.load_verify_locations(cafile=settings.upstream_ca_file)

    async def turn(self, payload):
        try:
            async with asyncio.timeout(125):
                async with httpx.AsyncClient(
                    verify=self.tls_context,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(122, connect=5),
                ) as client:
                    async with client.stream(
                        "POST",
                        self.settings.upstream + "/v1/turn",
                        json=payload,
                        headers={
                            "Authorization": "Bearer "
                            + read_secret(self.settings.key_file)
                        },
                    ) as response:
                        if response.status_code not in {200, 503}:
                            raise UpstreamError(
                                429 if response.status_code == 429 else 503
                            )
                        raw = bytearray()
                        async for block in response.aiter_bytes():
                            raw.extend(block)
                            if len(raw) > 256 * 1024:
                                raise ValueError("upstream response limit")
                        value = TurnResponse.model_validate(strict_json(bytes(raw)))
                        if (response.status_code == 503) != (
                            value.status == "unavailable"
                        ):
                            raise ValueError("upstream status mismatch")
                        return value
        except (httpx.HTTPError, TimeoutError, ValueError, OSError):
            raise UpstreamError() from None
