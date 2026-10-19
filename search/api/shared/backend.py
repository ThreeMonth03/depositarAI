"""Bounded subprocess calls keep synchronous adapters off the HTTP event loop."""

import asyncio
import json
import sys

from search.serialization import strict_json


# Up to twenty bounded metadata records; still capped independently of HTTP input.
MAX_WORKER_BYTES = 4 * 1024 * 1024


class BackendFailure(RuntimeError):
    pass


class BackendTimeout(BackendFailure):
    pass


class SubprocessBackend:
    operations = frozenset({"scope", "retrieve", "rerank", "ready"})
    worker_module = "search.api.worker"

    async def call(self, operation: str, payload: dict, timeout: float):
        if operation not in self.operations:
            raise ValueError("unsupported worker operation")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                self.worker_module,
                operation,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            raise BackendFailure("worker unavailable") from None

        async def exchange():
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            if len(encoded) > MAX_WORKER_BYTES:
                raise BackendFailure("worker input exceeds limit")
            proc.stdin.write(encoded)
            await proc.stdin.drain()
            proc.stdin.close()
            chunks, size = [], 0
            while part := await proc.stdout.read(65536):
                size += len(part)
                if size > MAX_WORKER_BYTES:
                    raise BackendFailure("worker output exceeds limit")
                chunks.append(part)
            if await proc.wait() != 0:
                raise BackendFailure("backend unavailable")
            try:
                return strict_json(b"".join(chunks))
            except (ValueError, UnicodeError, RecursionError):
                raise BackendFailure("invalid backend response") from None

        try:
            return await asyncio.wait_for(exchange(), timeout=timeout)
        except asyncio.TimeoutError:
            raise BackendTimeout("backend deadline exceeded") from None
        except (OSError, ConnectionError):
            raise BackendFailure("worker connection failed") from None
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
