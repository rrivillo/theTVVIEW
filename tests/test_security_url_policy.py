"""Tests de la política de URL (SDD §10, gaps B1/B2) — SEC-002."""

from __future__ import annotations

import unittest

from thetvview.security.errors import (
    InvalidSourceError,
    InvalidUrlError,
    IPTVError,
    ProviderError,
)
from thetvview.security.url_policy import (
    ALLOWED_SCHEMES,
    MAX_URL_LENGTH,
    PURPOSE_METADATA,
    PURPOSE_STREAM,
    REJECTED_SCHEMES,
    UrlParts,
    is_allowed_scheme,
    validate_url,
)
from thetvview.xtream_errors import friendly_message


class TestAllowlist(unittest.TestCase):
    def test_metadata_solo_http_https(self) -> None:
        self.assertEqual(ALLOWED_SCHEMES[PURPOSE_METADATA], frozenset({"http", "https"}))
        self.assertTrue(is_allowed_scheme("HTTP", PURPOSE_METADATA))
        self.assertTrue(is_allowed_scheme("https", PURPOSE_METADATA))
        self.assertFalse(is_allowed_scheme("ffmpeg", PURPOSE_METADATA))

    def test_stream_admite_ffmpeg(self) -> None:
        self.assertEqual(
            ALLOWED_SCHEMES[PURPOSE_STREAM], frozenset({"http", "https", "ffmpeg"})
        )
        self.assertTrue(is_allowed_scheme("ffmpeg", PURPOSE_STREAM))

    def test_proposito_desconocido_cae_a_metadata(self) -> None:
        self.assertEqual(validate_url("http://h/x", "otro").scheme, "http")

    def test_esquemas_rechazados_explicitos(self) -> None:
        for scheme in ("file", "smb", "ftp", "gopher", "data", "javascript",
                       "rtsp", "udp", "ws"):
            with self.subTest(scheme=scheme):
                self.assertIn(scheme, REJECTED_SCHEMES)


class TestRejectedSchemes(unittest.TestCase):
    def test_esquemas_peligrosos_stream(self) -> None:
        for url in (
            "file:///etc/passwd",
            "smb://server/share",
            "ftp://server/f",
            "gopher://server/",
            "data:text/html,<script>",
            "javascript:alert(1)",
            "rtsp://cam/stream",
            "udp://@239.0.0.1:1234",
            "ws://h/socket",
        ):
            with self.subTest(url=url):
                with self.assertRaises(InvalidUrlError):
                    validate_url(url, PURPOSE_STREAM)

    def test_esquemas_peligrosos_metadata(self) -> None:
        for url in ("file:///etc/passwd", "ftp://h/f", "javascript:x"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidUrlError):
                    validate_url(url, PURPOSE_METADATA)

    def test_rtsp_udp_no_entran_en_stream(self) -> None:
        # SDD §36: rtsp/udp se evalúan antes de añadirlos; hoy no entran.
        with self.assertRaises(InvalidUrlError):
            validate_url("rtsp://192.168.1.5/stream", PURPOSE_STREAM)
        with self.assertRaises(InvalidUrlError):
            validate_url("udp://@239.0.0.1:1234", PURPOSE_STREAM)

    def test_mensaje_dice_el_esquema(self) -> None:
        with self.assertRaises(InvalidUrlError) as ctx:
            validate_url("file:///etc/passwd", PURPOSE_STREAM)
        self.assertIn("file", str(ctx.exception))


class TestFormValidation(unittest.TestCase):
    def test_url_vacia(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("")
        with self.assertRaises(InvalidUrlError):
            validate_url("   ")

    def test_sin_esquema(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("example.com/list.m3u")

    def test_host_vacio(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("http:///ruta", PURPOSE_METADATA)

    def test_userinfo_rechazado(self) -> None:
        for url in ("http://user:pass@host/x", "http://user@host/x",
                    "https://u:p@[::1]/x"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidUrlError):
                    validate_url(url, PURPOSE_METADATA)

    def test_fragmento_rechazado(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("http://host/lista.m3u8#frag", PURPOSE_STREAM)

    def test_puerto_fuera_de_rango(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("http://host:99999/")

    def test_puerto_no_numerico(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("http://host:abc/")

    def test_puerto_cero(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("http://host:0/")

    def test_puerto_valido_se_conserva(self) -> None:
        parts = validate_url("http://host:8080/x", PURPOSE_METADATA)
        self.assertEqual(parts.port, 8080)
        self.assertEqual(parts.netloc, "host:8080")

    def test_caracteres_de_control(self) -> None:
        for raw in ("http://h/\x00", "http://h/\nSet-Cookie: x", "http://h/\x7f"):
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidUrlError):
                    validate_url(raw)

    def test_caracteres_escapados(self) -> None:
        for raw in ("http://h/%00", "http://h/%0a", "http://h/%1F", "http://h/%7f"):
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidUrlError):
                    validate_url(raw)

    def test_espacios_internos(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("http://h/mi lista.m3u")

    def test_espacios_de_borde_se_quitan(self) -> None:
        parts = validate_url("  http://h/x  ")
        self.assertEqual(parts.host, "h")
        self.assertEqual(parts.raw, "http://h/x")

    def test_longitud_maxima(self) -> None:
        long_url = "http://h/" + "a" * (MAX_URL_LENGTH + 1)
        with self.assertRaises(InvalidUrlError):
            validate_url(long_url)
        ok_url = "http://h/" + "a" * (MAX_URL_LENGTH - 11)
        self.assertEqual(validate_url(ok_url).host, "h")

    def test_no_string(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url(None)  # type: ignore[arg-type]


class TestStreamArgvInjection(unittest.TestCase):
    def test_stream_no_puede_empezar_por_guion(self) -> None:
        for url in ("--script=/tmp/evil.lua", "- --vo=null", "-dummy"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidUrlError) as ctx:
                    validate_url(url, PURPOSE_STREAM)
                self.assertIn("«-»", str(ctx.exception))

    def test_stream_guion_inicial_manda_sobre_el_esquema(self) -> None:
        # El chequeo de argv va antes que el de esquema: el mensaje tiene que
        # explicar el motivo real (inyección de argumentos).
        with self.assertRaises(InvalidUrlError) as ctx:
            validate_url("--script=/tmp/evil.lua", PURPOSE_STREAM)
        self.assertIn("«-»", str(ctx.exception))

    def test_metadata_sin_esquema_tambien_se_rechaza(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("-archivo", PURPOSE_METADATA)


class TestFfmpegWrapper(unittest.TestCase):
    def test_ffmpeg_envuelve_http(self) -> None:
        parts = validate_url("ffmpeg://http://host/lista.m3u8", PURPOSE_STREAM)
        self.assertEqual(parts.scheme, "http")
        self.assertEqual(parts.host, "host")
        self.assertEqual(parts.wrapper, "ffmpeg")
        self.assertEqual(parts.raw, "ffmpeg://http://host/lista.m3u8")

    def test_ffmpeg_ruta_relativa(self) -> None:
        parts = validate_url("ffmpeg://rel/x.m3u8", PURPOSE_STREAM)
        self.assertEqual(parts.scheme, "ffmpeg")
        self.assertEqual(parts.host, "")
        self.assertEqual(parts.wrapper, "ffmpeg")

    def test_ffmpeg_con_file_dentro_se_rechaza(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("ffmpeg://file:///etc/passwd", PURPOSE_STREAM)

    def test_ffmpeg_con_rtsp_dentro_se_rechaza(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("ffmpeg://rtsp://cam/1", PURPOSE_STREAM)

    def test_ffmpeg_anidado_se_rechaza(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("ffmpeg://ffmpeg://http://h/x", PURPOSE_STREAM)

    def test_ffmpeg_vacio_se_rechaza(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("ffmpeg://", PURPOSE_STREAM)

    def test_ffmpeg_no_sirve_para_descargas(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("ffmpeg://http://h/x", PURPOSE_METADATA)

    def test_ffmpeg_rechaza_userinfo_interior(self) -> None:
        with self.assertRaises(InvalidUrlError):
            validate_url("ffmpeg://http://u:p@h/x", PURPOSE_STREAM)


class TestUrlParts(unittest.TestCase):
    def test_propiedades(self) -> None:
        https = validate_url("https://h:8443/x?a=b")
        self.assertTrue(https.is_https)
        self.assertFalse(https.is_http)
        self.assertEqual(https.netloc, "h:8443")
        self.assertEqual(https.query, "a=b")
        self.assertEqual(https.normalized, "https://h:8443/x?a=b")

    def test_puerto_por_defecto_no_se_pinta(self) -> None:
        self.assertEqual(validate_url("http://h:80/x").netloc, "h")
        self.assertEqual(validate_url("https://h:443/x").netloc, "h")

    def test_ipv6_entre_corchetes(self) -> None:
        parts = validate_url("http://[2001:db8::1]:8080/x")
        self.assertEqual(parts.host, "2001:db8::1")
        self.assertEqual(parts.netloc, "[2001:db8::1]:8080")

    def test_host_se_normaliza_a_minusculas(self) -> None:
        self.assertEqual(validate_url("HTTP://MiHost/x").host, "mihost")

    def test_ffmpeg_normalized_reconstruye(self) -> None:
        parts = validate_url("ffmpeg://http://h/x.m3u8", PURPOSE_STREAM)
        self.assertEqual(parts.normalized, "ffmpeg://http://h/x.m3u8")


class TestErrorsYMensajes(unittest.TestCase):
    def test_jerarquia_de_excepciones(self) -> None:
        with self.assertRaises(InvalidUrlError) as ctx:
            validate_url("file:///x")
        self.assertIsInstance(ctx.exception, InvalidSourceError)
        self.assertIsInstance(ctx.exception, ProviderError)
        self.assertIsInstance(ctx.exception, IPTVError)

    def test_mensajes_nunca_contienen_la_url(self) -> None:
        # El mensaje acaba en la barra de estado: jamás ecoa la URL ni las
        # credenciales que pueda llevar dentro.
        casos = [
            "http://user1:supersecret@host/vivo/x.ts",
            "http://host:99999/live/user1/supersecret/1.ts",
            "http://host/" + "a" * 3000,
            "http://host/live/user1/supersecret/1.ts\nX-Evil: 1",
        ]
        for url in casos:
            with self.subTest(url=url[:40]):
                with self.assertRaises(InvalidUrlError) as ctx:
                    validate_url(url, PURPOSE_STREAM)
                message = str(ctx.exception)
                self.assertNotIn("supersecret", message)
                self.assertNotIn("user1", message)
                self.assertNotIn("http://host", message)

    def test_friendly_message_es_accionable(self) -> None:
        with self.assertRaises(InvalidUrlError) as ctx:
            validate_url("file:///etc/passwd", PURPOSE_STREAM)
        text = friendly_message(ctx.exception)
        self.assertNotIn("file:///", text)
        self.assertIn("Corrige la dirección", text)

    def test_friendly_message_sin_detalle(self) -> None:
        self.assertIn("Corrige la dirección", friendly_message(InvalidUrlError("")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
