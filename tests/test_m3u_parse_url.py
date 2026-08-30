"""Tests de parse_url (m3u_parser) contra un http.server local, solo stdlib."""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from thetvview.m3u_parser import parse_url

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = (FIXTURES / "sample.m3u").read_text(encoding="utf-8")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (API del stdlib)
        if self.path == "/lista.m3u":
            body = SAMPLE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
        elif self.path == "/vacio.m3u":
            body = b"#EXTM3U\n"
            self.send_response(200)
        elif self.path == "/lento.m3u":
            import time

            time.sleep(1.5)
            body = b""
            self.send_response(200)
        else:
            body = b""
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silencia el log del server
        pass


class TestParseUrl(unittest.TestCase):
    server: ThreadingHTTPServer
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_descarga_y_parsea(self) -> None:
        pl = parse_url(f"{self.base}/lista.m3u")
        self.assertEqual(len(pl.channels), 3)
        self.assertEqual(pl.source, f"{self.base}/lista.m3u")
        self.assertEqual(pl.name, "lista")

    def test_playlist_vacia_remota(self) -> None:
        pl = parse_url(f"{self.base}/vacio.m3u")
        self.assertEqual(pl.channels, [])

    def test_404_error_amigable(self) -> None:
        with self.assertRaises(OSError) as ctx:
            parse_url(f"{self.base}/no-existe.m3u")
        self.assertIn("404", str(ctx.exception))

    def test_conexion_rechazada_error_amigable(self) -> None:
        # Puerto cerrado: conexión rechazada inmediata.
        with self.assertRaises(OSError):
            parse_url("http://127.0.0.1:1/x.m3u")

    def test_timeout_no_cuelga(self) -> None:
        with self.assertRaises(OSError) as ctx:
            parse_url(f"{self.base}/lento.m3u", timeout=0.2)
        self.assertIn("Tiempo de espera agotado", str(ctx.exception))

    def test_esquema_invalido(self) -> None:
        with self.assertRaises(ValueError):
            parse_url("ftp://example.com/lista.m3u")


if __name__ == "__main__":
    unittest.main()
