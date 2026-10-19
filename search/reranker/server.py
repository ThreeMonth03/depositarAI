"""Internal-only, bounded HTTP adapter. No credentials, URLs, tools or generation.

The API has an eight-second hard subprocess deadline. This service has one
inference slot, a six-second cooperative model budget and a nine-second process
watchdog for stuck CUDA work; the container restart policy restores the model.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading

from search.serialization import strict_json
from .contract import (
    MAX_REQUEST_BYTES,
    MODEL_REVISION,
    validate_request,
    validate_order,
)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self, address, ranker):
        self.ranker = ranker
        self.slots = threading.BoundedSemaphore(4)
        self.inference = threading.Lock()
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, _request, _address):
        pass  # Never log request data or exception details.


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "PrivateRanker"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *_args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {"error": "invalid_request"})

    def reply(self, status, value):
        body = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        if self.path != "/healthz":
            return self.reply(404, {"error": "not_found"})
        self.reply(200, {"ready": True, "model_revision": MODEL_REVISION})

    def do_POST(self):
        if self.path != "/rerank":
            return self.reply(404, {"error": "not_found"})
        lengths = self.headers.get_all("Content-Length", [])
        if (
            len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdecimal()
            or len(lengths[0]) > 8
            or not 1 <= int(lengths[0]) <= MAX_REQUEST_BYTES
            or self.headers.get_all("Content-Type", []) != ["application/json"]
            or "Transfer-Encoding" in self.headers
            or "Content-Encoding" in self.headers
        ):
            return self.reply(400, {"error": "invalid_request"})
        try:
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ValueError("incomplete body")
            query, passages = validate_request(strict_json(raw))
        except (ValueError, UnicodeError, RecursionError, OSError):
            return self.reply(400, {"error": "invalid_request"})
        if not self.server.inference.acquire(blocking=False):
            return self.reply(503, {"error": "busy"})
        # A disconnected client cannot release the GPU slot while CUDA still runs.
        watchdog = threading.Timer(9, os._exit, args=(70,))
        watchdog.daemon = True
        watchdog.start()
        status = 200
        try:
            value = self.server.ranker.rank(query, passages)
            validate_order(value, len(passages))
        except Exception:
            status, value = 503, {"error": "ranking_unavailable"}
        finally:
            watchdog.cancel()
            self.server.inference.release()
        self.reply(status, value)


def main():
    from .model import Ranker

    Server(("0.0.0.0", 8001), Ranker("/model")).serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
