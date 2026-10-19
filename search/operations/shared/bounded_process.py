"""Bound captured pipes for an operator-owned process in a fresh session.

Callers own process creation, environment, umask, final wait and result policy.
This context owns the selector and pipes, and always terminates the spawned
process group, including descendants left behind by an exited leader.
"""

from contextlib import contextmanager
import os
import selectors
import signal
import time


@contextmanager
def captured_pipes(process, *, timeout, max_bytes, timeout_error, output_error):
    """Yield remaining wait time and raw stdout/stderr chunk lists.

    The process must have PIPE stdout/stderr and its own session. Error factories
    retain caller diagnostics; final wait errors remain caller policy. No bytes
    are decoded and the cap counts both streams together.
    """
    selector = None
    chunks, size = {"stdout": [], "stderr": []}, 0
    try:
        selector = selectors.DefaultSelector()
        deadline = time.monotonic() + timeout
        for name in chunks:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error()
            for key, _ in selector.select(min(remaining, 0.1)):
                part = os.read(key.fileobj.fileno(), 65536)
                if not part:
                    selector.unregister(key.fileobj)
                    continue
                size += len(part)
                if size > max_bytes:
                    raise output_error()
                chunks[key.data].append(part)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise timeout_error()
        yield remaining, chunks["stdout"], chunks["stderr"]
    finally:
        try:
            if selector is not None:
                selector.close()
        finally:
            # A child may retain the pipes after the leader exits. Address the
            # original process group even when the leader has already completed.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                process.wait(timeout=5)
            for name in chunks:
                getattr(process, name).close()
