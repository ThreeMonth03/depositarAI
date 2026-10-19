"""Socket lifetime shared by pinned operator HTTP and HTTPS connections.

This mixin does not select an address, create a socket, authorize an endpoint,
or implement deadlines. A caller-owned watchdog can interrupt the retained
channel even when ``HTTPConnection.getresponse`` clears ``self.sock`` for a
Connection: close response whose makefile is still reading.
"""

from __future__ import annotations

import socket
import threading


class AbortableConnection:
    """Mix into HTTPConnection and publish each raw/TLS channel after creation."""

    def _init_abortable(self):
        self._aborted = threading.Event()
        self._socket_lock = threading.Lock()
        self._active_channel = None

    def _publish_channel(self, channel):
        with self._socket_lock:
            self.sock = channel
            self._active_channel = channel
        if self._aborted.is_set():
            self.abort()
            raise TimeoutError("operator_connection_aborted")

    def abort(self):
        self._aborted.set()
        with self._socket_lock:
            channel = self._active_channel
        if channel is not None:
            try:
                channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            channel.close()

    def finish(self):
        # Keep the handle until the response's makefile has been closed.
        try:
            self.close()
        finally:
            with self._socket_lock:
                channel, self._active_channel = self._active_channel, None
            if channel is not None:
                channel.close()
