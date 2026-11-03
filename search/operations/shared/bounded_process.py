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


MAX_INPUT_BYTES = 4096


def _write_input(selector, key, input_bytes, offset):
    """Advance pending stdin bytes and close the pipe after its final write."""
    try:
        written = os.write(key.fd, input_bytes[offset:])
    except BlockingIOError:
        return offset
    if written == 0:
        raise OSError("process_input_closed")
    offset += written
    if offset == len(input_bytes):
        selector.unregister(key.fileobj)
        key.fileobj.close()
    return offset


def _pump(
    selector,
    chunks,
    *,
    input_bytes,
    deadline,
    max_bytes,
    timeout_error,
    output_error,
):
    """Drain both outputs while advancing input under one absolute deadline."""
    offset, size = 0, 0
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise timeout_error()
        for key, _ in selector.select(min(remaining, 0.1)):
            if key.data == "stdin":
                offset = _write_input(selector, key, input_bytes, offset)
                continue
            try:
                part = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if not part:
                selector.unregister(key.fileobj)
                continue
            size += len(part)
            if size > max_bytes:
                raise output_error()
            chunks[key.data].append(part)


def _cleanup(process, chunks):
    """Terminate the original group before closing its retained pipes."""
    # A child may retain the pipes after the leader exits. Address the
    # original process group even when the leader has already completed.
    try:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=5)
    finally:
        for name in ("stdin", *chunks):
            stream = getattr(process, name, None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


@contextmanager
def captured_pipes(
    process,
    *,
    timeout,
    max_bytes,
    timeout_error,
    output_error,
    input_bytes=None,
):
    """Yield remaining wait time and raw stdout/stderr chunk lists.

    The process must have PIPE stdout/stderr and its own session. Error
    factories retain caller diagnostics; final wait errors remain caller
    policy. No bytes are decoded and the cap counts both output streams
    together. Optional input is bounded and written concurrently, then its pipe
    closes to deliver EOF.
    """
    selector = None
    chunks = {"stdout": [], "stderr": []}
    try:
        if input_bytes is not None and (
            type(input_bytes) is not bytes
            or len(input_bytes) > MAX_INPUT_BYTES
        ):
            raise ValueError("process_input_invalid")
        selector = selectors.DefaultSelector()
        deadline = time.monotonic() + timeout
        for name in chunks:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if input_bytes is not None:
            if process.stdin is None:
                raise ValueError("process_input_invalid")
            os.set_blocking(process.stdin.fileno(), False)
            if input_bytes:
                selector.register(
                    process.stdin, selectors.EVENT_WRITE, "stdin"
                )
            else:
                process.stdin.close()
        _pump(
            selector,
            chunks,
            input_bytes=input_bytes,
            deadline=deadline,
            max_bytes=max_bytes,
            timeout_error=timeout_error,
            output_error=output_error,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise timeout_error()
        yield remaining, chunks["stdout"], chunks["stderr"]
    finally:
        try:
            if selector is not None:
                selector.close()
        finally:
            _cleanup(process, chunks)
