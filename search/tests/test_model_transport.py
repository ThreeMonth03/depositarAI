"""Reviewed private peers and model HTTP operations, without network access."""

import unittest
from unittest.mock import patch
from search.api.shared.model_transport import ModelTransport
from search.api.shared.peers import service_address
from search.transport.http import HTTPRequest, HTTPResponse
from search.model_policy import OLLAMA_BASE_URL


class ModelTransportTests(unittest.TestCase):
    def test_dns_only_accepts_one_private_address_of_allowlisted_service(self):
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("172.20.0.2", 11434))]
        ):
            self.assertEqual(
                "http://172.20.0.2:11434", service_address("scope-model", 11434)
            )
            with self.assertRaises(ValueError):
                service_address("evil.example", 11434)
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 11434))]
        ):
            with self.assertRaises(ValueError):
                service_address("scope-model", 11434)

    def test_transport_cannot_rewrite_arbitrary_urls_or_enable_pull(self):
        with patch(
            "search.api.shared.model_transport.service_address",
            return_value="http://172.20.0.2:11434",
        ):
            transport = ModelTransport("scope")
        for path in ("/api/pull", "/api/chat?x=1", "/api/chat#x", "//evil.example"):
            with self.assertRaises(ValueError):
                transport.send(
                    HTTPRequest("POST", OLLAMA_BASE_URL + path, ()),
                    timeout=1,
                    max_bytes=100,
                )
        with patch.object(
            transport.transport,
            "send",
            return_value=HTTPResponse(
                200, (), b"{}", "http://172.20.0.2:11434/api/tags"
            ),
        ) as call:
            result = transport.send(
                HTTPRequest("GET", OLLAMA_BASE_URL + "/api/tags", ()),
                timeout=1,
                max_bytes=100,
            )
            self.assertEqual(OLLAMA_BASE_URL + "/api/tags", result.url)
            self.assertEqual(
                "http://172.20.0.2:11434/api/tags", call.call_args.args[0].url
            )
