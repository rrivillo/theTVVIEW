"""Edge cases Xtream detectados con servidor real (flexlive.lat).

- HTTP 404 en player_api.php -> UnsupportedProviderError
- HTTP 512 con {"login":false} -> AuthenticationError
- HTTP 200 con user_info.auth==0 -> AuthenticationError
- build_m3u_url como fallback get.php
Solo stdlib, fake server local, sin credenciales reales.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from thetvview.xtream_config import XtreamConfig
from thetvview.xtream_errors import (
    AuthenticationError,
    UnsupportedProviderError,
)
from thetvview.xtream_provider import authenticate
from thetvview.xtream_security import build_m3u_url, redact_secret


class EdgeHandler(BaseHTTPRequestHandler):
    mode = "auth_zero"

    def log_message(self, *a):  # noqa
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if "player_api.php" in parsed.path:
            if self.mode == "404":
                self.send_response(404)
                self.end_headers()
                return
            if self.mode == "512":
                body = json.dumps({"login": False, "message": "invalid user, pass."}).encode()
                self.send_response(512)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.mode == "auth_zero":
                body = json.dumps({"user_info": {"auth": 0, "status": "Active"},
                                   "server_info": {}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.mode == "login_false":
                body = json.dumps({"login": False}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_response(404)
        self.end_headers()


class TestAuthEdge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), EdgeHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _cfg(self):
        return XtreamConfig(
            server_url=f"http://127.0.0.1:{self.port}", username="u", password="p")

    def test_404_player_api(self):
        EdgeHandler.mode = "404"
        with self.assertRaises(UnsupportedProviderError):
            authenticate(self._cfg(), force_refresh=True)

    def test_512_login_false(self):
        EdgeHandler.mode = "512"
        with self.assertRaises(AuthenticationError):
            authenticate(self._cfg(), force_refresh=True)

    def test_auth_zero(self):
        EdgeHandler.mode = "auth_zero"
        with self.assertRaises(AuthenticationError):
            authenticate(self._cfg(), force_refresh=True)

    def test_login_false_200(self):
        EdgeHandler.mode = "login_false"
        with self.assertRaises(AuthenticationError):
            authenticate(self._cfg(), force_refresh=True)

    def test_build_m3u_url(self):
        url = build_m3u_url("http://x.com:8080/", "user1", "pass1")
        self.assertEqual(
            url,
            "http://x.com:8080/get.php?username=user1&password=pass1&type=m3u_plus&output=ts",
        )
        # redact no expone password
        red = redact_secret(url)
        self.assertNotIn("pass1", red)
        self.assertIn("password=***", red)


if __name__ == "__main__":
    unittest.main()
