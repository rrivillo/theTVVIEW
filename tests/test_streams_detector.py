"""Tests de la detección de protocolo (plan F1, SDD §6).

Lo que importa aquí es la **prioridad de las señales**: un `.m3u8` que
devuelve HTML es HTML, y un MPEG-TS no puede confundirse con un manifiesto.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from thetvview.streams.detector import (
    TS_PACKET_SIZE,
    detect_protocol,
    looks_like_hls,
    looks_like_mpegts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "streams"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def ts_body(packets: int = 4) -> bytes:
    """Cuerpo con patrón de sync TS cada 188 bytes."""
    out = bytearray()
    for index in range(packets):
        block = bytearray(TS_PACKET_SIZE)
        block[0] = 0x47
        block[1] = 0x40 if index % 2 else 0x00
        block[3] = 0x10
        out += block
    return bytes(out)


class TestSenalesDelCuerpo(unittest.TestCase):
    def test_hls_por_cuerpo_manda_sobre_la_extension(self) -> None:
        body = b"#EXTM3U\n#EXT-X-VERSION:3\n"
        det = detect_protocol(body, url="http://h/canal.mpd")
        self.assertTrue(det.is_hls)
        self.assertTrue(det.confident)

    def test_dash_por_cuerpo(self) -> None:
        body = fixture("full.mpd")
        det = detect_protocol(body, url="http://h/canal.m3u8")
        self.assertTrue(det.is_dash)
        self.assertTrue(det.confident)

    def test_mpegts_por_patron_de_sync(self) -> None:
        det = detect_protocol(ts_body(), url="http://h/canal")
        self.assertTrue(det.is_mpegts)
        self.assertTrue(det.confident)

    def test_byte_suelto_0x47_no_es_mpegts(self) -> None:
        # Un solo 0x47 no es un flujo TS: sin patrón repetido, unknown.
        det = detect_protocol(b"\x47algo\x00\x01\x02", url="http://h/canal")
        self.assertEqual(det.protocol, "unknown")

    def test_html_de_error_no_es_hls_aunque_diga_m3u8(self) -> None:
        det = detect_protocol(fixture("403.html"), url="http://h/canal.m3u8")
        self.assertEqual(det.protocol, "unknown")
        self.assertTrue(det.confident)
        self.assertIn("no es HLS", det.reason)

    def test_html_con_espacio_antes_de_extm3u(self) -> None:
        self.assertTrue(looks_like_hls(b"  \r\n#EXTM3U\n"))
        self.assertTrue(looks_like_hls(b"\xef\xbb\xbf#EXTM3U\n"))

    def test_vacio_no_es_nada(self) -> None:
        self.assertEqual(looks_like_hls(b""), False)
        self.assertFalse(looks_like_mpegts(b""))
        self.assertFalse(looks_like_mpegts(b"\x47" * 10))


class TestContentTypeYExtension(unittest.TestCase):
    def test_content_type_hls(self) -> None:
        det = detect_protocol(b"", url="", content_type="application/vnd.apple.mpegurl")
        self.assertTrue(det.is_hls)
        self.assertFalse(det.confident)

    def test_content_type_hls_con_charset(self) -> None:
        det = detect_protocol(b"", content_type="application/x-mpegURL; charset=utf-8")
        self.assertTrue(det.is_hls)

    def test_content_type_dash(self) -> None:
        det = detect_protocol(b"", content_type="application/dash+xml")
        self.assertTrue(det.is_dash)

    def test_extension_es_la_senal_mas_debil(self) -> None:
        det = detect_protocol(b"", url="http://h/live/canal.m3u8?token=abc")
        self.assertTrue(det.is_hls)
        self.assertIn(".m3u8", det.reason)

    def test_extension_ts(self) -> None:
        det = detect_protocol(b"", url="http://h/live/123.ts")
        self.assertTrue(det.is_mpegts)

    def test_extension_con_query_y_fragmento(self) -> None:
        det = detect_protocol(b"", url="http://h/canal.M3U8?t=1#x")
        self.assertTrue(det.is_hls)

    def test_extension_percent_encoded(self) -> None:
        det = detect_protocol(b"", url="http://h/canal%2Em3u8")
        self.assertTrue(det.is_hls)

    def test_sin_nada_es_unknown_pero_no_error(self) -> None:
        det = detect_protocol(b"")
        self.assertEqual(det.protocol, "unknown")
        self.assertFalse(det.confident)
        self.assertFalse(det.is_selectable)
        self.assertTrue(det.reason)

    def test_url_basura_no_revienta(self) -> None:
        det = detect_protocol(b"", url="no-es-una-url")
        self.assertEqual(det.protocol, "unknown")

    def test_solo_son_seleccionables_hls_y_dash(self) -> None:
        self.assertTrue(detect_protocol(b"#EXTM3U\n#EXT-X-VERSION:3\n").is_selectable)
        self.assertFalse(detect_protocol(ts_body()).is_selectable)
        self.assertFalse(detect_protocol(b"").is_selectable)

    def test_cuerpo_muy_corto_no_juzga(self) -> None:
        # Sonda con read_body=False: 2 bytes no dicen nada, así que manda
        # el Content-Type y, si no hay, la extensión (sin confident).
        det = detect_protocol(b"#E", url="http://h/canal.ts")
        self.assertFalse(det.confident)
        self.assertTrue(det.is_mpegts)


class TestFixtureVacio(unittest.TestCase):
    def test_empty_bin_no_detecta_nada(self) -> None:
        det = detect_protocol(fixture("empty.bin"), url="http://h/x.m3u8")
        # Sin cuerpo y sólo extensión: el veredicto es débil y por eso la
        # UI no debe alarmarse.
        self.assertTrue(det.is_hls)
        self.assertFalse(det.confident)