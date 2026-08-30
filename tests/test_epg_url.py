"""Tests de epg_parser.load_url: descarga HTTP, cache TTL y errores."""

import functools
import http.server
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from thetvview.epg_parser import load_url

FIXTURES = Path(__file__).parent / "fixtures"

XML_B = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="canal1.es"><display-name>Canal Uno</display-name></channel>
  <programme channel="canal1.es" start="20260825120000 +0000" stop="20260825130000 +0000">
    <title>Tarde</title>
  </programme>
</tv>
"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class _EpgServer:
    """Servidor HTTP mínimo sobre un directorio temporal."""

    def __init__(self, root: Path) -> None:
        handler = functools.partial(_QuietHandler, directory=str(root))
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)


class TestEpgUrl(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cache_dir = self.root / "cache"
        self.served = self.root / "epg.xml"
        self.served.write_text(
            (FIXTURES / "sample.xmltv").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.server = _EpgServer(self.root)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.stop)

    def url(self) -> str:
        return f"{self.server.base_url}/epg.xml"

    def test_descarga_y_parsea(self) -> None:
        epg = load_url(self.url(), cache_dir=self.cache_dir)
        self.assertEqual(epg.channel_name("canal1.es"), "Canal Uno")
        cache_files = list(self.cache_dir.glob("epg_*.xml"))
        self.assertEqual(len(cache_files), 1)

    def test_ttl_valido_usa_cache_sin_red(self) -> None:
        load_url(self.url(), cache_dir=self.cache_dir)
        # Se cambia el contenido servido; dentro del TTL no debe notarse.
        self.served.write_text(XML_B, encoding="utf-8")
        epg = load_url(self.url(), cache_dir=self.cache_dir)
        titles = [p.title for p in epg.programmes_for("canal1.es")]
        self.assertIn("Titulares", titles)

    def test_ttl_expirado_redescarga(self) -> None:
        first = load_url(self.url(), cache_dir=self.cache_dir)
        self.assertIn("Titulares", [p.title for p in first.programmes_for("canal1.es")])
        # Expirar el cache manualmente y cambiar lo servido.
        for f in self.cache_dir.iterdir():
            os.utime(f, (time.time() - 13 * 3600, time.time() - 13 * 3600))
        self.served.write_text(XML_B, encoding="utf-8")
        second = load_url(self.url(), cache_dir=self.cache_dir)
        self.assertEqual([p.title for p in second.programmes_for("canal1.es")], ["Tarde"])

    def test_force_refresh_ignora_cache(self) -> None:
        load_url(self.url(), cache_dir=self.cache_dir)
        self.served.write_text(XML_B, encoding="utf-8")
        epg = load_url(self.url(), force_refresh=True, cache_dir=self.cache_dir)
        self.assertEqual([p.title for p in epg.programmes_for("canal1.es")], ["Tarde"])

    def test_ttl_cero_siempre_redescarga(self) -> None:
        load_url(self.url(), ttl_hours=0, cache_dir=self.cache_dir)
        self.served.write_text(XML_B, encoding="utf-8")
        epg = load_url(self.url(), ttl_hours=0, cache_dir=self.cache_dir)
        self.assertEqual([p.title for p in epg.programmes_for("canal1.es")], ["Tarde"])

    def test_servidor_caido_con_cache_fresca_funciona(self) -> None:
        load_url(self.url(), cache_dir=self.cache_dir)
        self.server.stop()
        epg = load_url(self.url(), cache_dir=self.cache_dir)
        self.assertEqual(epg.channel_name("canal1.es"), "Canal Uno")

    def test_servidor_caido_sin_cache_lanza_oserror(self) -> None:
        self.server.stop()
        with self.assertRaises(OSError):
            load_url(f"{self.server.base_url}/noexiste.xml", cache_dir=self.cache_dir)

    def test_url_invalida_value_error(self) -> None:
        with self.assertRaises(ValueError):
            load_url("ftp://ejemplo.com/epg.xml", cache_dir=self.cache_dir)


if __name__ == "__main__":
    unittest.main()
