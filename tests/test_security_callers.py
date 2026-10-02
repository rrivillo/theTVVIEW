"""Fase 4: los cuatro call sites de red pasan por `safe_http` (B3/B4/B5).

Antes cada uno usaba `urllib.request.urlopen` a pelo: sin política de URL,
sin anti-SSRF, sin redirects revalidados y con lectura sin tope. Aquí se
comprueba que TODOS quedan cubiertos por el cliente seguro y que la
excepción por fuente (`allow_private`) sigue funcionando.
"""

from __future__ import annotations

import gzip
import unittest

from thetvview.channel_health import probe_channel_url
from thetvview.epg_parser import fetch_bytes as epg_fetch_bytes
from thetvview.m3u_parser import fetch_bytes as m3u_fetch_bytes, parse_url
from thetvview.models import Channel
from thetvview.security.errors import NetworkError, SSRFBlockedError
from thetvview.security.local_files import gunzip_limited

# Nada escucha en el puerto 9: si la validación fallara, la conexión daría
# "rechazada" y el test seguiría pasando por otro motivo. Por eso se
# comprueba el mensaje concreto del bloqueo.
LOOPBACK = "http://127.0.0.1:9/x"
LAN = "http://192.168.1.10:8080/player_api.php?username=u&password=p&action=auth"
METADATA = "http://169.254.169.254/latest/meta-data/"


class TestM3uCaller(unittest.TestCase):
    def test_fetch_bytes_bloquea_loopback_por_defecto(self) -> None:
        with self.assertRaises(OSError) as ctx:
            m3u_fetch_bytes(LOOPBACK, timeout=0.5)
        self.assertIn("bloqueado", str(ctx.exception))

    def test_es_ssrf_y_tambien_oserror(self) -> None:
        # Contrato legacy: los parsers y la TUI capturan (OSError, ValueError).
        self.assertTrue(issubclass(SSRFBlockedError, OSError))
        with self.assertRaises(SSRFBlockedError):
            m3u_fetch_bytes(LOOPBACK, timeout=0.5)

    def test_parse_url_tambien_queda_cubierto(self) -> None:
        with self.assertRaises(OSError) as ctx:
            parse_url(LOOPBACK, timeout=0.5)
        self.assertIn("bloqueado", str(ctx.exception))

    def test_sigue_dando_value_error_para_esquemas_no_soportados(self) -> None:
        with self.assertRaises(ValueError):
            m3u_fetch_bytes("ftp://example.com/lista.m3u")
        with self.assertRaises(ValueError):
            parse_url("file:///etc/passwd")

    def test_red_privada_tambien_bloqueada(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            m3u_fetch_bytes(LAN, timeout=0.5)

    def test_metadata_tambien_bloqueada(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            m3u_fetch_bytes(METADATA, timeout=0.5)


class TestEpgCaller(unittest.TestCase):
    """La `x-tvg-url` la elige la playlist: es el vector clásico de SSRF."""

    def test_fetch_bytes_bloquea_loopback(self) -> None:
        with self.assertRaises(SSRFBlockedError) as ctx:
            epg_fetch_bytes(LOOPBACK, timeout=0.5)
        self.assertIn("bloqueado", str(ctx.exception))
        self.assertIsInstance(ctx.exception, OSError)

    def test_fetch_bytes_bloquea_red_privada(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            epg_fetch_bytes("http://10.0.0.5/epg.xml", timeout=0.5)

    def test_esquema_no_soportado_sigue_siendo_value_error(self) -> None:
        with self.assertRaises(ValueError):
            epg_fetch_bytes("ftp://example.com/epg.xml")


class TestXtreamCaller(unittest.TestCase):
    def test_api_call_bloquea_panel_en_lan(self) -> None:
        from thetvview.xtream_client import api_call

        with self.assertRaises(NetworkError) as ctx:
            api_call(LAN, timeout=0.5)
        self.assertIn("bloqueado", str(ctx.exception))

    def test_api_call_bloquea_metadata(self) -> None:
        from thetvview.xtream_client import api_call

        with self.assertRaises(NetworkError):
            api_call(METADATA, timeout=0.5)

    def test_los_errores_se_mapean_al_dominio_xtream(self) -> None:
        # Un status inesperado llega como ProviderError, no como crudo de urllib.
        from unittest import mock

        from thetvview.security.errors import InvalidSourceError
        from thetvview.xtream_client import api_call

        class _Resp:
            status = 200
            reason = "OK"
            body = b"esto no es json"

        with mock.patch(
            "thetvview.xtream_client.SafeHttpClient.request", return_value=_Resp()
        ):
            with self.assertRaises(InvalidSourceError):
                api_call("http://panel/player_api.php?x=1")


class TestSondaDeSalud(unittest.TestCase):
    def test_no_sondea_loopback_sin_excepcion(self) -> None:
        snap = probe_channel_url(
            Channel(name="C", url="http://127.0.0.1:9/live.ts"), timeout=0.5
        )
        self.assertFalse(snap.success)
        self.assertEqual(snap.error, "URL bloqueada")
        self.assertIsNotNone(snap.raw_error)
        self.assertIn("bloqueado", snap.raw_error or "")

    def test_no_sondea_direcciones_de_metadata(self) -> None:
        snap = probe_channel_url(Channel(name="C", url=METADATA), timeout=0.5)
        self.assertFalse(snap.success)
        self.assertEqual(snap.error, "URL bloqueada")

    def test_protocolo_no_http_sigue_sin_hacer_red(self) -> None:
        snap = probe_channel_url(Channel(name="C", url="rtmp://ejemplo/live"))
        self.assertIsNone(snap.success)
        self.assertEqual(snap.error, "Protocolo no HTTP")

    def test_url_vacia_no_explota(self) -> None:
        snap = probe_channel_url(Channel(name="C", url="  "))
        self.assertFalse(snap.success)
        self.assertEqual(snap.error, "URL vacía")


class TestGzipDeDescarga(unittest.TestCase):
    def test_bomba_gzip_no_expande_en_el_camino_de_descarga(self) -> None:
        blob = gzip.compress(b"\x00" * (2 * 1024 * 1024))
        limit = len(blob) + 64
        with self.assertRaises(OSError) as ctx:
            gunzip_limited(blob, max_bytes=limit, what="La playlist descargada")
        self.assertIn("límite", str(ctx.exception))

    def test_gzip_truncado_se_detecta(self) -> None:
        blob = gzip.compress(b"contenido " * 100)
        with self.assertRaises(OSError) as ctx:
            gunzip_limited(blob[: len(blob) // 2], what="El EPG descargado")
        self.assertIn("dañado", str(ctx.exception))

    def test_gzip_valido_pasa(self) -> None:
        self.assertEqual(gunzip_limited(gzip.compress(b"hola")), b"hola")


class TestExcepcionPorFuente(unittest.TestCase):
    """La excepción anti-SSRF se busca por `source`, que es lo que tienen
    a mano `load_playlist_source` y la carga del EPG de la lista."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from thetvview.playlist_manager import PlaylistManager

        self._tmp = tempfile.TemporaryDirectory()
        self.mgr = PlaylistManager(Path(self._tmp.name) / "playlists.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_se_encuentra_por_source(self) -> None:
        self.mgr.add("L", "http://192.168.1.5/lista.m3u", allow_private_network=True)
        self.mgr.add("P", "http://ejemplo.test/otra.m3u")
        self.assertTrue(self.mgr.allow_private_for("http://192.168.1.5/lista.m3u"))
        self.assertFalse(self.mgr.allow_private_for("http://ejemplo.test/otra.m3u"))

    def test_fuente_desconocida_o_vacia_es_false(self) -> None:
        self.assertFalse(self.mgr.allow_private_for("http://desconocida.test/x.m3u"))
        self.assertFalse(self.mgr.allow_private_for(""))
        self.assertFalse(self.mgr.allow_private_for(None))  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
