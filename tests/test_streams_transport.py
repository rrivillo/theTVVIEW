"""Tests del transporte: el **esquema**, que no es el contenido (SDD-M §4.1).

Lo que se verifica aquí es la regla arquitectónica del §36 del SDD-M: el tipo
de un stream **nunca** es igual al tipo de su URL. Eso se sostiene porque son
dos campos distintos, y estos tests son la prueba de que siguen siendo
distintos.
"""

from __future__ import annotations

import unittest

from thetvview.streams.transport import (
    DEFAULT_PORTS,
    STREAM_SCHEMES,
    Transport,
    describe,
    is_multicast_host,
    scheme_of,
    transport_from_scheme,
    transport_of,
)


class TestScheme(unittest.TestCase):
    def test_esquema_en_minusculas(self) -> None:
        self.assertEqual(scheme_of("HTTPS://x/a.m3u8"), "https")
        self.assertEqual(scheme_of("RTSP://cam/live"), "rtsp")
        self.assertEqual(scheme_of("  http://x/a  "), "http")

    def test_sin_esquema_devuelve_vacio(self) -> None:
        for url in ("", "   ", "x/a.m3u8", "/ruta/absoluta", None):
            with self.subTest(url=url):
                self.assertEqual(scheme_of(url), "")

    def test_no_texto_no_rompe(self) -> None:
        # El tipo se anota como ``str``, pero quien llama es un M3U hostil: si
        # llega un int, aquí no puede haber excepción.
        self.assertEqual(scheme_of(123), "")  # type: ignore[arg-type]
        self.assertEqual(scheme_of(["rtsp://x"]), "")  # type: ignore[arg-type]

    def test_url_malformada_no_rompe(self) -> None:
        # urlsplit lanza ValueError en algunos puertos raros: aquí no puede.
        self.assertEqual(scheme_of("http://[::1"), "")


class TestTransport(unittest.TestCase):
    def test_todo_esquema_conocido_tiene_transporte(self) -> None:
        for esquema in sorted(STREAM_SCHEMES):
            with self.subTest(esquema=esquema):
                self.assertIsNot(transport_from_scheme(esquema), Transport.UNKNOWN)

    def test_esquema_desconocido_es_unknown(self) -> None:
        for esquema in ("gopher", "smb", "ws", "", "no-existe"):
            with self.subTest(esquema=esquema):
                self.assertIs(transport_from_scheme(esquema), Transport.UNKNOWN)

    def test_transporte_de_url(self) -> None:
        self.assertIs(transport_of("rtmp://x/live/a"), Transport.RTMP)
        self.assertIs(transport_of("rtmps://x/live/a"), Transport.RTMPS)
        self.assertIs(transport_of("rtsp://cam:554/live"), Transport.RTSP)
        self.assertIs(transport_of("udp://239.1.1.1:5000"), Transport.UDP)
        self.assertIs(transport_of("https://x/a.m3u8"), Transport.HTTPS)

    def test_es_enum_de_texto(self) -> None:
        # Extensibilidad del §4.1: es un `str` Enum, así que un `Transport`
        # nuevo se serializa y se compara sin conversor. El valor es el que
        # viaja (`Transport.RTSP.value == "rtsp"`); se comprueba eso y no el
        # `str()` del enum, que en 3.12+ imprime `Transport.RTSP` y es
        # deliberadamente distinto para que nadie lo pinte por accidente.
        self.assertEqual(Transport.RTSP.value, "rtsp")
        self.assertEqual(Transport.RTSP, "rtsp")
        self.assertEqual(Transport.UNKNOWN.value, "unknown")

    def test_reconocido_no_es_lo_mismo_que_ofrecido(self) -> None:
        # SRT y RIST están en el enum y en la tabla (el vocabulario es
        # completo), pero **no** en STREAM_SCHEMES: el router no los ofrece.
        # Conocer la palabra y comprometerse a abrirla son dos cosas
        # distintas, y confundirlas es como se acaba prometiendo un protocolo
        # que nadie ha comprobado (SDD §51).
        from thetvview.streams import transport as modulo

        self.assertIs(modulo.transport_from_scheme("srt"), Transport.SRT)
        self.assertNotIn("srt", STREAM_SCHEMES)
        self.assertIn("srt", modulo.KNOWN_NOT_OFFERED)

    def test_puertos_por_defecto(self) -> None:
        self.assertEqual(DEFAULT_PORTS["rtmp"], 1935)
        self.assertEqual(DEFAULT_PORTS["rtsp"], 554)
        self.assertEqual(DEFAULT_PORTS["http"], 80)
        # udp no tiene puerto por defecto: es siempre explícito.
        self.assertIs(DEFAULT_PORTS.get("udp"), None)

    def test_describe_es_legible(self) -> None:
        self.assertEqual(describe(Transport.RTSP), "RTSP")
        self.assertEqual(describe(Transport.UNKNOWN), "desconocido")
        self.assertEqual(describe(Transport.FILE), "fichero local")


class TestMulticast(unittest.TestCase):
    def test_grupos_multicast(self) -> None:
        for host in ("239.255.0.1", "224.0.0.1", "239.0.0.250", "224.1.2.3"):
            with self.subTest(host=host):
                self.assertTrue(is_multicast_host(host))

    def test_no_multicast(self) -> None:
        for host in (
            "192.168.1.9",
            "10.0.0.1",
            "8.8.8.8",
            "223.255.255.255",  # el último justo antes del rango
            "240.0.0.1",  # reservado
            "",
            "no-es-una-ip",
            "1.2.3",
            "999.1.1.1",
        ):
            with self.subTest(host=host):
                self.assertFalse(is_multicast_host(host))

    def test_ipv6_entre_corchetes_no_es_multicast(self) -> None:
        # Un grupo IPv6 (ff02::1) es multicast, pero no es IPv4 multicast y el
        # predicado lo dice: no inventa una respuesta para algo que no es.
        self.assertFalse(is_multicast_host("[ff02::1]"))

    def test_espacios_ignorados(self) -> None:
        self.assertTrue(is_multicast_host("  239.255.0.1  "))


if __name__ == "__main__":
    unittest.main()