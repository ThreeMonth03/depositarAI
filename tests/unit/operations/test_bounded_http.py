"""Abortable channel lifetime does not depend on HTTPConnection.sock retention."""

import socket
import unittest
from unittest.mock import Mock

from search.operations.shared.bounded_http import AbortableConnection


class Connection(AbortableConnection):
    def __init__(self):
        self._init_abortable()
        self.sock = None
        self.close = Mock()


class AbortableConnectionTests(unittest.TestCase):
    def test_abort_retains_channel_even_after_http_clears_sock(self):
        connection, channel = Connection(), Mock()
        connection._publish_channel(channel)
        connection.sock = None
        connection.abort()
        channel.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        channel.close.assert_called_once()
        self.assertTrue(connection._aborted.is_set())

    def test_abort_before_connect_rejects_and_closes_published_socket(self):
        connection, channel = Connection(), Mock()
        connection.abort()
        with self.assertRaises(TimeoutError):
            connection._publish_channel(channel)
        channel.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        channel.close.assert_called_once()

    def test_tls_replacement_is_the_watchdog_handle(self):
        connection, raw, tls = Connection(), Mock(), Mock()
        connection._publish_channel(raw)
        connection._publish_channel(tls)
        connection.abort()
        raw.shutdown.assert_not_called()
        tls.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_shutdown_failure_still_closes_channel(self):
        connection, channel = Connection(), Mock()
        channel.shutdown.side_effect = OSError("closed peer")
        connection._publish_channel(channel)
        connection.abort()
        channel.close.assert_called_once()

    def test_finish_releases_once_after_response_was_closed(self):
        connection, channel = Connection(), Mock()
        connection._publish_channel(channel)
        connection.finish()
        connection.finish()
        channel.close.assert_called_once()
        self.assertIsNone(connection._active_channel)

    def test_finish_releases_channel_even_if_parent_close_is_interrupted(self):
        connection, channel = Connection(), Mock()
        connection._publish_channel(channel)
        connection.close.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            connection.finish()
        channel.close.assert_called_once()
        self.assertIsNone(connection._active_channel)


if __name__ == "__main__":
    unittest.main()
