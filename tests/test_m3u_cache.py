"""Tests de m3u_parser.load_url: cache TTL, gzip y fallback stale."""

import gzip
import http.server
import os
import tempfile
import threading
import time
import unittest
from functools import partial
from pathlib import Path

from thetvview.m3u_parser import load_url

SAMPLE = (
    "#EXTM3U\n"
    '#EXTINF:-1 tvg-id="a1" group-title="News",Canal Uno\n'
    "http://ejemplo.com/a1.ts\n"
    '#EXTINF:-1 tvg-id="a2" group-title="News",Canal Dos\n'
    "http://ejemplo.com/a2.ts\n"
)

OTHER = (
    "#EXTM3U\n"
    '#EXTINF:-1 tvg-id="b1" group-title="News",Canal Nuevo\n'
    "http://ejemplo.com/b1.ts\n"
)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class _M3uServer:
    def __init__(self, root: Path) -> None:
        handler = partial(_QuietHandler, directory=str(root))
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


class TestM3uLoadUrl(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cache_dir = self.root / "cache"
        self.served = self.root / "lista.m3u"
        self.served.write_text(SAMPLE, encoding="utf-8")
        self.server = _M3uServer(self.root)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.stop)

    def url(self) -> str:
        return f"{self.server.base_url}/lista.m3u"

    def test_descarga_y_cachea(self) -> None:
        pl = load_url(self.url(), cache_dir=self.cache_dir)
        self.assertEqual(len(pl.channels), 2)
        self.assertEqual(len(list(self.cache_dir.glob("playlist_*.m3u"))), 1)

    def test_ttl_valido_usa_cache_sin_red(self) -> None:
        load_url(self.url(), cache_dir=self.cache_dir)
        self.served.write_text(OTHER, encoding="utf-8")
        pl = load_url(self.url(), cache_dir=self.cache_dir)
        self.assertEqual([c.name for c in pl.channels], ["Canal Uno", "Canal Dos"])

    def test_ttl_expirado_redescarga(self) -> None:
        load_url(self.url(), cache_dir=self.cache_dir)
        for f in self.cache_dir.iterdir():
            old = time.time() - 7 * 3600
            os.utime(f, (old, old))
        self.served.write_text(OTHER, encoding="utf-8")
        pl = load_url(self.url(), cache_dir=self.cache_dir)
        self.assertEqual([c.name for c in pl.channels], ["Canal Nuevo"])

    def test_force_refresh_ignora_cache(self) -> None:
        load_url(self.url(), cache_dir=self.cache_dir)
        self.served.write_text(OTHER, encoding="utf-8")
        pl = load_url(self.url(), force_refresh=True, cache_dir=self.cache_dir)
        self.assertEqual([c.name for c in pl.channels], ["Canal Nuevo"])

    def test_servidor_caido_con_cache_expirada_devuelve_stale(self) -> None:
        load_url(self.url(), cache_dir=self.cache_dir)
        for f in self.cache_dir.iterdir():
            old = time.time() - 7 * 3600
            os.utime(f, (old, old))
        self.server.stop()
        pl = load_url(self.url(), cache_dir=self.cache_dir)
        self.assertEqual(len(pl.channels), 2)

    def test_servidor_caido_sin_cache_lanza_oserror(self) -> None:
        self.server.stop()
        with self.assertRaises(OSError):
            load_url(f"{self.server.base_url}/noexiste.m3u", cache_dir=self.cache_dir)

    def test_gzip_por_sufijo(self) -> None:
        gz_path = self.root / "lista.m3u.gz"
        gz_path.write_bytes(gzip.compress(SAMPLE.encode("utf-8")))
        pl = load_url(f"{self.server.base_url}/lista.m3u.gz", cache_dir=self.cache_dir)
        self.assertEqual(len(pl.channels), 2)

    def test_url_invalida_value_error(self) -> None:
        with self.assertRaises(ValueError):
            load_url("ftp://ejemplo.com/lista.m3u", cache_dir=self.cache_dir)


if __name__ == "__main__":
    unittest.main()
