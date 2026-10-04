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
    detect_transport,
    looks_like_hls,
    looks_like_mpegts,
)
from thetvview.streams.transport import Transport

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


class TestMatrizDelSddM29(unittest.TestCase):
    """La matriz del detector del §29 del SDD-M, verbatim.

    Es la tabla que el SDD-M da como ejemplo. Aquí es un contrato: cada fila es
    un caso que la app tiene que clasificar, y la última fila —«URL sin
    extensión»— es la que de verdad importa, porque es la que ocurre en un
    proveedor real y la que antes caía a ``unknown``.
    """

    #: (entrada, protocolo esperado)
    MATRIZ: tuple[tuple[str, str], ...] = (
        ("https://x/a.m3u8", "hls"),
        ("http://x/a.ts", "mpegts"),
        ("rtmp://x/live/a", "rtmp"),
        ("rtmps://x/live/a", "rtmps"),
        ("rtsp://x/live", "rtsp"),
        ("udp://239.1.1.1:5000", "udp"),
    )

    def test_cada_fila_de_la_matriz(self) -> None:
        for url, esperado in self.MATRIZ:
            with self.subTest(url=url):
                det = detect_protocol(b"", url=url)
                self.assertEqual(det.protocol, esperado)

    def test_url_sin_extension_es_unknown_sin_mime(self) -> None:
        # La última fila del §29: sin extensión **ni** MIME no hay información
        # suficiente. Lo dice `unknown`, no HLS a palos (SDD-M D-003).
        det = detect_protocol(b"", url="https://x/live/canal")
        self.assertEqual(det.protocol, "unknown")
        self.assertFalse(det.confident)

    def test_url_sin_extension_con_mime_si_se_resuelve(self) -> None:
        # El caso real que faltaba (§9.3): el servidor declara el tipo y la
        # ruta no tiene extensión. Antes esto acababa en `unknown`.
        det = detect_protocol(b"", url="https://x/live/canal", content_type="video/mp2t")
        self.assertEqual(det.protocol, "mpegts")
        self.assertTrue(det.confident)


class TestEsquemaEsTransporte(unittest.TestCase):
    """El §36 del SDD-M: el tipo de stream nunca es el tipo de URL."""

    def test_esquema_y_protocolo_son_campos_distintos(self) -> None:
        # https con HLS: el esquema NO es el protocolo. Es la regla entera.
        det = detect_protocol(b"", url="https://x/a.m3u8")
        self.assertEqual(det.scheme, "https")
        self.assertEqual(det.protocol, "hls")
        self.assertIs(det.transport, Transport.HTTPS)

    def test_http_sin_extension_es_mpegts_por_mime(self) -> None:
        det = detect_protocol(b"", url="http://x/live/canal", content_type="video/mp2t")
        self.assertIs(det.transport, Transport.HTTP)
        self.assertEqual(det.protocol, "mpegts")

    def test_mp4_y_flv_van_a_su_protocolo(self) -> None:
        # Los dos MIME que faltaban del §9.3. Antes: `unknown` con
        # `confident=True`, que la UI traducía como «no pude determinarlo»
        # sobre un canal que sí sabía qué era.
        for mime, esperado in (("video/mp4", "mp4"), ("video/x-flv", "flv")):
            with self.subTest(mime=mime):
                det = detect_protocol(b"", url="https://x/canal", content_type=mime)
                self.assertEqual(det.protocol, esperado)
                self.assertTrue(det.confident)
                self.assertFalse(det.is_selectable)

    def test_transportes_de_conversacion_no_seleccionables(self) -> None:
        # RTMP/RTSP/UDP son conexiones: no hay manifiesto con pistas.
        for url in ("rtmp://x/live/a", "rtsp://x/live", "udp://239.1.1.1:5000"):
            with self.subTest(url=url):
                det = detect_protocol(b"", url=url)
                self.assertTrue(det.is_streaming_transport)
                self.assertFalse(det.is_selectable)

    def test_hls_sobre_rtmp_no_puede_ser(self) -> None:
        # `rtmp://x/a.m3u8` no es HLS aunque el nombre lo diga: el esquema
        # manda porque no hay cuerpo que inspeccionar. El nombre del fichero
        # es la señal **más débil** y no resucita un veredicto.
        det = detect_protocol(b"", url="rtmp://x/live/a.m3u8")
        self.assertEqual(det.protocol, "rtmp")
        self.assertTrue(det.confident)

    def test_rtsp_solo_es_rtsp_aunque_diga_m3u(self) -> None:
        det = detect_protocol(b"", url="rtsp://x/live/stream.m3u8")
        self.assertEqual(det.protocol, "rtsp")

    def test_esquema_se_guarda_siempre(self) -> None:
        for url, esquema in (
            ("http://x/a.m3u8", "http"),
            ("https://x/a", "https"),
            ("rtmp://x/a", "rtmp"),
            ("rtsps://x/a", "rtsps"),
            ("udp://239.1.1.1:5000", "udp"),
            ("sin-esquema", ""),
        ):
            with self.subTest(url=url):
                self.assertEqual(detect_protocol(b"", url=url).scheme, esquema)

    def test_mime_queda_registrado_como_senal(self) -> None:
        # El MIME es una señal, no el veredicto: el informe (§21) tiene que
        # poder enseñarlo aunque el cuerpo haya gaineado.
        det = detect_protocol(
            b"#EXTM3U\n#EXT-X-VERSION:3\n",
            url="https://x/canal.mpd",
            content_type="video/mp2t",
        )
        self.assertEqual(det.protocol, "hls")  # el cuerpo manda
        self.assertEqual(det.mime, "video/mp2t")  # pero la señal se conserva

    def test_describe_para_modales(self) -> None:
        self.assertIn("RTMP", detect_protocol(b"", url="rtmp://x/a").describe())
        self.assertTrue(detect_protocol(b"", url="https://x/a").describe())

    def test_detect_transport_es_atajo(self) -> None:
        # Quien sólo necesita el transporte (router, diagnóstico) no paga la
        # detección completa.
        self.assertIs(detect_transport("rtsp://x/live"), Transport.RTSP)
        self.assertIs(detect_transport("no-es-url"), Transport.UNKNOWN)


class TestFixtureVacio(unittest.TestCase):
    def test_empty_bin_no_detecta_nada(self) -> None:
        det = detect_protocol(fixture("empty.bin"), url="http://h/x.m3u8")
        # Sin cuerpo y sólo extensión: el veredicto es débil y por eso la
        # UI no debe alarmarse.
        self.assertTrue(det.is_hls)
        self.assertFalse(det.confident)