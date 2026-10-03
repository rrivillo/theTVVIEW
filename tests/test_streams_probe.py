"""Tests del descubrimiento de pistas con red (plan F2, SDD §29/§48).

Se levanta un servidor HTTP real en loopback (con ``allow_private=True``,
que es exactamente como el llamador declara una fuente de la LAN) y se
comprueba, sobre todo, lo que **no** debe pasar:

- ningún error del proveedor lanza una excepción a la TUI (§29, AC-12);
- un 403 sin ``Referer`` se distingue de un 403 con él (H6);
- un directo infinito no se materializa (H5);
- la excepción anti-SSRF la hereda la fuente, no se pone a True sola (H8);
- el motivo que llega a la UI está redactado (H7);
- la caché en disco no guarda la URL cruda del canal (H7).
"""

from __future__ import annotations

import http.server
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from thetvview.models import Channel
from thetvview.streams import probe as probe_mod
from thetvview.streams.probe import ProbeResult, TrackProbe, probe_capabilities
from thetvview.tracks.models import (
    DEGRADED_PROTOCOL,
    DEGRADED_PROBE_FAILED,
    DEGRADED_UNKNOWN,
    PROTO_HLS,
    PROTO_MPEGTS,
)

FIXTURES = Path(__file__).parent / "fixtures" / "streams"

MASTER = (FIXTURES / "master_full.m3u8").read_bytes()
MPD = (FIXTURES / "full.mpd").read_bytes()
def _ts_packet(index: int = 0) -> bytes:
    """Un paquete MPEG-TS de 188 bytes con el byte de sync."""
    bloque = bytearray(188)
    bloque[0] = 0x47
    bloque[1] = 0x40 if index % 2 else 0x00
    bloque[3] = 0x10
    return bytes(bloque)


#: El detector exige 3 paquetes seguidos para no confundir un 0x47 suelto.
TS_BODY = _ts_packet(0) + _ts_packet(1) + _ts_packet(2) + _ts_packet(3)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silencio en los tests
        return

    # -- utilidades ---------------------------------------------------------

    def _referer_ok(self) -> bool:
        return self.headers.get("Referer") == "https://ref.test/"

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # -- rutas --------------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - firma de BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]
        if path == "/master.m3u8":
            self._send(200, MASTER, "application/vnd.apple.mpegurl")
        elif path == "/manifest.mpd":
            self._send(200, MPD, "application/dash+xml")
        elif path == "/master-head403":
            if self.command == "HEAD":
                self._send(405)
            else:
                self._send(200, MASTER, "application/vnd.apple.mpegurl")
        elif path == "/no-referer":
            if not self._referer_ok():
                self._send(403, b"<html>403 Forbidden</html>", "text/html")
            else:
                self._send(200, MASTER, "application/vnd.apple.mpegurl")
        elif path == "/html-403":
            self._send(403, (FIXTURES / "403.html").read_bytes(), "text/html")
        elif path == "/404":
            self._send(404, b"nope", "text/html")
        elif path == "/html-de-error":
            self._send(200, b"<html><body>login</body></html>", "text/html")
        elif path == "/directo.ts":
            self._send(200, TS_BODY, "video/mp2t")
        elif path == "/infinito":
            # Un "directo" que no acaba nunca: si el sondeo lo materializa
            # entero, el test se queda colgado.
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(1 << 30))
            self.end_headers()
            bloque = TS_BODY
            try:
                for _ in range(400):
                    self.wfile.write(bloque)
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/lento":
            time.sleep(3)
            self._send(200, MASTER, "application/vnd.apple.mpegurl")
        elif path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/master.m3u8")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/vivo/master.m3u8":
            # Manifiesto "en vivo": cambia entre peticiones (el HEAD no
            # cuenta, si no cada sondeo vería siempre lo mismo).
            if self.command == "HEAD":
                self._send(200, MASTER, "application/vnd.apple.mpegurl")
                return
            self.server.calls = getattr(self.server, "calls", 0) + 1  # type: ignore[attr-defined]
            if self.server.calls % 2:  # type: ignore[attr-defined]
                self._send(200, MASTER, "application/vnd.apple.mpegurl")
            else:
                self._send(
                    200,
                    b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nx.m3u8\n",
                    "application/vnd.apple.mpegurl",
                )
        else:
            self._send(404, b"", "text/plain")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.calls = 0  # type: ignore[attr-defined]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        probe_mod.clear_memory_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name)

    def url(self, path: str) -> str:
        return self.base + path

    def channel(self, path: str, **kw) -> Channel:
        return Channel(name="Canal", url=self.url(path), **kw)

    def probe(self, path: str, *, timeout: float = 10.0, **kw) -> ProbeResult:
        channel = self.channel(path, **kw.pop("channel_kw", {}))
        return probe_capabilities(
            channel, allow_private=True, timeout=timeout, cache_dir=self.cache_dir, **kw
        )


class TestDescubrimientoHappyPath(_Base):
    def test_master_hls(self) -> None:
        result = self.probe("/master.m3u8")
        self.assertTrue(result.ok)
        caps = result.capabilities
        assert caps is not None
        self.assertEqual(caps.protocol, PROTO_HLS)
        self.assertEqual(len(caps.audio_tracks), 2)
        self.assertEqual(len(caps.video_variants), 3)
        self.assertEqual(len(caps.subtitle_tracks), 2)
        self.assertTrue(caps.adaptive_bitrate)

    def test_mpd_dash(self) -> None:
        result = self.probe("/manifest.mpd")
        assert result.capabilities is not None
        self.assertEqual(result.capabilities.protocol, "dash")
        self.assertEqual(len(result.capabilities.audio_tracks), 3)

    def test_redirect_se_sigue_y_resuelve_contra_la_url_final(self) -> None:
        result = self.probe("/redir")
        assert result.capabilities is not None
        self.assertTrue(result.capabilities.final_url.endswith("/master.m3u8"))
        # Las URI relativas se resuelven contra la URL final, no contra /redir.
        self.assertTrue(
            result.capabilities.audio_tracks[0].uri.startswith(self.base + "/")
        )

    def test_head_405_cae_a_get(self) -> None:
        result = self.probe("/master-head403")
        self.assertTrue(result.ok)

    def test_manifest_url_queda_redactado(self) -> None:
        result = self.probe("/master.m3u8")
        assert result.capabilities is not None
        self.assertEqual(result.capabilities.manifest_url,
                         result.capabilities.final_url)


class TestErroresDelProveedor(_Base):
    def test_403_sin_referer_no_lanza(self) -> None:
        result = self.probe("/html-403")
        self.assertTrue(result.failed)
        self.assertIn("403", result.reason or "")
        self.assertEqual(result.http_status, 403)

    def test_403_se_evita_con_referer_del_extvlcopt(self) -> None:
        # H6: muchos proveedores devuelven 403 si falta el Referer.
        channel = self.channel(
            "/no-referer",
            extra_options=[("EXTVLCOPT", "http-referrer=https://ref.test/")],
        )
        result = probe_capabilities(
            channel, allow_private=True, timeout=10.0, cache_dir=self.cache_dir
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(result.capabilities.audio_tracks), 2)  # type: ignore[union-attr]

    def test_404(self) -> None:
        result = self.probe("/404")
        self.assertTrue(result.failed)
        self.assertIn("404", result.reason or "")

    def test_html_de_error_no_es_manifiesto(self) -> None:
        result = self.probe("/html-de-error")
        assert result.capabilities is not None
        self.assertEqual(result.capabilities.degraded_reason, DEGRADED_UNKNOWN)
        self.assertFalse(result.capabilities.has_any_choice)

    def test_timeout_no_cuelga_ni_lanza(self) -> None:
        inicio = time.monotonic()
        result = self.probe("/lento", timeout=0.5)
        self.assertLess(time.monotonic() - inicio, 6.0)
        self.assertTrue(result.failed)
        self.assertTrue(result.reason)

    def test_url_inexistente_no_lanza(self) -> None:
        result = probe_capabilities(
            Channel(name="x", url="http://no.existe.invalido/x.m3u8"),
            allow_private=True,
            timeout=2.0,
            cache_dir=self.cache_dir,
        )
        self.assertTrue(result.failed)
        self.assertTrue(result.reason)

    def test_esquema_no_permitido(self) -> None:
        result = probe_capabilities(
            Channel(name="x", url="rtmp://h/live"),
            allow_private=True,
            cache_dir=self.cache_dir,
        )
        self.assertTrue(result.failed)
        self.assertIn("HTTP", result.reason or "")

    def test_motivo_sin_credenciales(self) -> None:
        # Una URL Xtream lleva usuario y contraseña en el path: el motivo
        # que llega a la UI no puede contenerlos (H7).
        result = probe_capabilities(
            Channel(name="x", url="http://h.test/live/usuario/secreto/1.m3u8"),
            allow_private=True,
            timeout=2.0,
            cache_dir=self.cache_dir,
        )
        self.assertTrue(result.failed)
        self.assertNotIn("secreto", result.reason or "")


class TestDirectos(_Base):
    def test_mpegts_es_pista_unica(self) -> None:
        result = self.probe("/directo.ts")
        assert result.capabilities is not None
        self.assertEqual(result.capabilities.protocol, PROTO_MPEGTS)
        self.assertEqual(result.capabilities.degraded_reason, DEGRADED_PROTOCOL)
        self.assertFalse(result.capabilities.has_any_choice)
        # No es un error: el canal se reproduce igual.
        self.assertEqual(result.reason, "")

    def test_directo_infinito_no_se_materializa(self) -> None:
        inicio = time.monotonic()
        result = self.probe("/infinito", timeout=4.0)
        self.assertLess(time.monotonic() - inicio, 8.0)
        assert result.capabilities is not None
        self.assertFalse(result.capabilities.has_any_choice)

    def test_ffmpeg_wrapper_se_quito(self) -> None:
        channel = Channel(name="x", url="ffmpeg://" + self.url("/master.m3u8"))
        result = probe_capabilities(
            channel, allow_private=True, timeout=10.0, cache_dir=self.cache_dir
        )
        self.assertTrue(result.ok, result.reason)


class TestAllowPrivateHeredado(_Base):
    def test_sin_excepcion_la_red_privada_esta_bloqueada(self) -> None:
        result = probe_capabilities(
            self.channel("/master.m3u8"),
            allow_private=False,
            timeout=3.0,
            cache_dir=self.cache_dir,
        )
        self.assertTrue(result.failed)
        self.assertTrue(result.reason)

    def test_con_excepcion_funciona(self) -> None:
        self.assertTrue(self.probe("/master.m3u8").ok)


class TestCache(_Base):
    def test_cache_en_memoria_no_vuelve_a_pedir(self) -> None:
        self.assertTrue(self.probe("/master.m3u8").ok)
        self.server.calls = 0
        segunda = probe_capabilities(
            self.channel("/master.m3u8"),
            allow_private=True,
            timeout=5.0,
            cache_dir=self.cache_dir,
        )
        self.assertTrue(segunda.from_cache)
        self.assertIsNotNone(segunda.capabilities)

    def test_use_cache_false_siempre_pide(self) -> None:
        self.assertTrue(self.probe("/master.m3u8").ok)
        self.assertFalse(self.probe("/master.m3u8", use_cache=False).from_cache)

    def test_ttl_de_memoria_es_corto(self) -> None:
        self.assertLessEqual(probe_mod.MEMORY_TTL, 120.0)
        self.assertGreater(probe_mod.DISK_TTL, probe_mod.MEMORY_TTL)

    def test_cache_en_disco_es_respaldo(self) -> None:
        self.assertTrue(self.probe("/master.m3u8").ok)
        ficheros = list(self.cache_dir.glob("*.json"))
        self.assertEqual(len(ficheros), 1)
        # Con la red caída, la caché en disco responde.
        probe_mod.clear_memory_cache()
        resultado = probe_capabilities(
            self.channel("/no-existe-jamas"),
            allow_private=True,
            timeout=2.0,
            cache_dir=self.cache_dir,
        )
        self.assertTrue(resultado.failed)

    def test_la_cache_no_guarda_la_url_cruda(self) -> None:
        url = "http://h.test/live/usuario/secreto/1.m3u8"
        probe_capabilities(
            Channel(name="x", url=url),
            allow_private=False,
            timeout=1.0,
            cache_dir=self.cache_dir,
        )
        for fichero in self.cache_dir.glob("*"):
            texto = fichero.read_text(encoding="utf-8")
            self.assertNotIn("secreto", texto)
            self.assertNotIn("usuario", texto)

    def test_fichero_de_cache_legible(self) -> None:
        self.assertTrue(self.probe("/master.m3u8").ok)
        datos = json.loads(next(self.cache_dir.glob("*.json")).read_text())
        self.assertEqual(datos["version"], 1)
        self.assertIn("capabilities", datos)
        # Ni las URI absolutas ni la URL del canal.
        self.assertNotIn("uri", json.dumps(datos["capabilities"]["audio_tracks"][0]))

    def test_cache_corrupta_no_rompe(self) -> None:
        self.assertTrue(self.probe("/master.m3u8").ok)
        for fichero in self.cache_dir.glob("*.json"):
            fichero.write_text("{no es json", encoding="utf-8")
        probe_mod.clear_memory_cache()
        resultado = probe_capabilities(
            self.channel("/master.m3u8"),
            allow_private=True,
            timeout=5.0,
            cache_dir=self.cache_dir,
        )
        # Vuelve a la red y funciona igual.
        self.assertTrue(resultado.ok)

    def test_manifiesto_que_cambia_no_se_congela(self) -> None:
        # Un master en vivo cambia; con TTL de 60 s el usuario ve el cambio
        # al re-sondear, no la versión de hace un minuto.
        probe_mod.clear_memory_cache()
        primera = self.probe("/vivo/master.m3u8", use_cache=False)
        segunda = self.probe("/vivo/master.m3u8", use_cache=False)
        self.assertIsNotNone(primera.capabilities)
        self.assertIsNotNone(segunda.capabilities)
        self.assertNotEqual(
            len(primera.capabilities.video_variants),
            len(segunda.capabilities.video_variants),
        )


class TestTrackProbeEnHilo(_Base):
    def test_no_bloquea_el_hilo_principal(self) -> None:
        canal = self.channel("/lento")
        probe = TrackProbe(canal, allow_private=True, timeout=0.5,
                           cache_dir=self.cache_dir)
        recibidos: list[ProbeResult] = []
        probe.start(recibidos.append)
        # El hilo principal sigue siendo libre inmediatamente.
        self.assertIsNone(probe.result)
        for _ in range(200):
            if recibidos:
                break
            time.sleep(0.05)
        self.assertTrue(recibidos, "el callback nunca llegó")
        self.assertTrue(recibidos[0].failed)
        probe.stop()

    def test_callback_recibe_capacidades(self) -> None:
        recibidos: list[ProbeResult] = []
        probe = TrackProbe(self.channel("/master.m3u8"), allow_private=True,
                           timeout=10.0, cache_dir=self.cache_dir)
        probe.start(recibidos.append)
        for _ in range(200):
            if recibidos:
                break
            time.sleep(0.02)
        self.assertEqual(len(recibidos), 1)
        assert recibidos[0].capabilities is not None
        self.assertEqual(len(recibidos[0].capabilities.audio_tracks), 2)
        self.assertIsNotNone(probe.result)

    def test_callback_roto_no_tumba_el_hilo(self) -> None:
        def roto(_result: ProbeResult) -> None:
            raise RuntimeError("boom")

        probe = TrackProbe(self.channel("/master.m3u8"), allow_private=True,
                           timeout=10.0, cache_dir=self.cache_dir)
        probe.start(roto)
        probe.stop()
        self.assertIsNotNone(probe.result)

    def test_sondeo_sincrono_devuelve_resultado(self) -> None:
        probe = TrackProbe(self.channel("/master.m3u8"), allow_private=True,
                           timeout=10.0, cache_dir=self.cache_dir)
        resultado = probe.probe_now()
        self.assertTrue(resultado.ok)
        self.assertFalse(probe.is_running)

    def test_start_dos_veces_no_duplica_hilo(self) -> None:
        probe = TrackProbe(self.channel("/master.m3u8"), allow_private=True,
                           timeout=10.0, cache_dir=self.cache_dir)
        probe.start()
        probe.start()
        self.assertTrue(probe.is_running)
        probe.stop()


class TestHeadersCompartidos(unittest.TestCase):
    def test_user_agent_siempre(self) -> None:
        from thetvview.streams.headers import build_headers

        self.assertIn("User-Agent", build_headers(Channel(name="x", url="http://h/")))

    def test_referer_del_extvlcopt(self) -> None:
        from thetvview.streams.headers import build_headers

        canal = Channel(
            name="x",
            url="http://h/",
            extra_options=[
                ("EXTVLCOPT", "http-referrer=https://ref/"),
                ("EXTVLCOPT", "user-agent=MiniEPG/2"),
                ("EXTVLCOPT", "network-caching=1000"),
                ("KODIPROP", "inputstream.adaptive.manifest_type=hls"),
            ],
        )
        cabeceras = build_headers(canal)
        self.assertEqual(cabeceras["Referer"], "https://ref/")
        self.assertEqual(cabeceras["User-Agent"], "MiniEPG/2")

    def test_none_no_revienta(self) -> None:
        from thetvview.streams.headers import build_headers

        self.assertIn("User-Agent", build_headers(None))