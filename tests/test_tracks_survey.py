"""Tests de la encuesta de listas (plan: ¿la app puede ofrecer pistas de verdad?).

Sin red: se levanta un servidor local que sirve un master con alternativas,
otro con una sola pista, un 403 y un 404, y se comprueba que la encuesta los
clasifica bien. Lo que importa aquí es que **cada motivo por el que el menú no
aparece tiene su propio veredicto**: es lo que permite distinguir "el
proveedor no publica pistas" de "no supe leer el manifiesto".
"""

from __future__ import annotations

import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from thetvview.models import Channel, Playlist
from thetvview.tracks.models import MediaCapabilities, PlaybackSelection
from thetvview.tracks.survey import (
    HTTP_FORBIDDEN,
    diversificar_por_host,
    host_de,
    prioriza_canales,
    HTTP_MISSING,
    NETWORK,
    NOT_HTTP,
    OK_MENU,
    OK_NO_MENU,
    OK_SINGLE,
    TS,
    UNKNOWN_BODY,
    VERDICT_LABELS,
    load_playlist,
    survey_channel,
    survey_playlist,
)

FIXTURES = Path(__file__).parent / "fixtures" / "streams"
MASTER = (FIXTURES / "master_full.m3u8").read_bytes()
MEDIA = (FIXTURES / "media_simple.m3u8").read_bytes()
MPD = (FIXTURES / "full.mpd").read_bytes()
HTML = (FIXTURES / "403.html").read_bytes()

#: Cuatro paquetes MPEG-TS: el detector exige 3 seguidos.
TS_BODY = b"".join(
    bytes([0x47]) + b"\x00" * 187 for _ in range(4)
)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        ruta = self.path.split("?", 1)[0]
        if ruta == "/master.m3u8":
            self._send(200, MASTER, "application/vnd.apple.mpegurl")
        elif ruta == "/media.m3u8":
            self._send(200, MEDIA, "application/vnd.apple.mpegurl")
        elif ruta == "/manifest.mpd":
            self._send(200, MPD, "application/dash+xml")
        elif ruta == "/ts.ts":
            self._send(200, TS_BODY, "video/mp2t")
        elif ruta == "/html":
            self._send(200, HTML, "text/html")
        elif ruta == "/prohibido":
            self._send(403, b"no", "text/html")
        elif ruta == "/ausente":
            self._send(404, b"no", "text/html")
        else:
            self._send(404, b"", "text/plain")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def canal(self, ruta: str) -> Channel:
        return Channel(name=ruta.strip("/"), url=self.base + ruta)


class TestVeredictos(_Base):
    def test_master_con_alternativas_da_menu(self) -> None:
        veredicto = survey_channel(self.canal("/master.m3u8"), allow_private=True)
        self.assertEqual(veredicto.code, OK_MENU)
        self.assertTrue(veredicto.menu_audio)
        self.assertTrue(veredicto.menu_subtitles)
        self.assertTrue(veredicto.menu_quality)
        self.assertEqual(veredicto.variants, 3)
        self.assertEqual(veredicto.audio_tracks, 2)
        self.assertEqual(veredicto.protocol, "hls")

    def test_media_playlist_es_una_pista(self) -> None:
        veredicto = survey_channel(self.canal("/media.m3u8"), allow_private=True)
        self.assertEqual(veredicto.code, OK_SINGLE)
        self.assertFalse(veredicto.has_menu)

    def test_mpegts_es_pista_unica(self) -> None:
        veredicto = survey_channel(self.canal("/ts.ts"), allow_private=True)
        self.assertEqual(veredicto.code, TS)
        self.assertEqual(veredicto.protocol, "mpegts")
        self.assertFalse(veredicto.has_menu)

    def test_dash_con_alternativas_da_menu(self) -> None:
        veredicto = survey_channel(self.canal("/manifest.mpd"), allow_private=True)
        self.assertEqual(veredicto.code, OK_MENU)
        self.assertTrue(veredicto.menu_audio)
        self.assertTrue(veredicto.menu_subtitles)

    def test_dash_no_ofrece_calidad_fija(self) -> None:
        # En DASH no hay mecanismo de fijado: no se ofrece (fuera de alcance).
        veredicto = survey_channel(self.canal("/manifest.mpd"), allow_private=True)
        self.assertFalse(veredicto.menu_quality)

    def test_html_no_es_manifiesto(self) -> None:
        veredicto = survey_channel(self.canal("/html"), allow_private=True)
        self.assertEqual(veredicto.code, UNKNOWN_BODY)

    def test_403_y_404_se_distinguen(self) -> None:
        self.assertEqual(
            survey_channel(self.canal("/prohibido"), allow_private=True).code,
            HTTP_FORBIDDEN,
        )
        self.assertEqual(
            survey_channel(self.canal("/ausente"), allow_private=True).code,
            HTTP_MISSING,
        )

    def test_no_http(self) -> None:
        veredicto = survey_channel(
            Channel(name="rtmp", url="rtmp://servidor/live"), allow_private=True
        )
        self.assertEqual(veredicto.code, NOT_HTTP)

    def test_sin_url(self) -> None:
        veredicto = survey_channel(Channel(name="vacio", url=""))
        self.assertEqual(veredicto.code, "sin_url")

    def test_error_de_red_no_rompe(self) -> None:
        veredicto = survey_channel(
            Channel(name="caido", url="http://no.existe.invalido/x.m3u8"),
            allow_private=True,
            timeout=2.0,
        )
        self.assertEqual(veredicto.code, NETWORK)
        self.assertTrue(veredicto.detail)

    def test_red_privada_bloqueada_sin_permiso(self) -> None:
        veredicto = survey_channel(self.canal("/master.m3u8"), allow_private=False)
        self.assertNotEqual(veredicto.code, OK_MENU)


class TestRedaccion(_Base):
    def test_la_url_con_credenciales_no_se_imprime(self) -> None:
        # URL al estilo Xtream: usuario y contraseña en el path.
        canala = Channel(name="Xtream", url="http://h/live/alice/secreto/1.m3u8")
        veredicto = survey_channel(canala, allow_private=True, timeout=2.0)
        texto = veredicto.describe()
        self.assertNotIn("alice", texto)
        self.assertNotIn("secreto", texto)

    def test_describe_incluye_el_veredicto(self) -> None:
        texto = survey_channel(self.canal("/master.m3u8"), allow_private=True).describe()
        self.assertIn("menu", texto)
        self.assertIn("calidad", texto)


class TestEncuestaDeLista(_Base):
    def _playlist(self) -> Playlist:
        canales = [
            self.canal("/master.m3u8"),
            self.canal("/media.m3u8"),
            self.canal("/ts.ts"),
            self.canal("/ausente"),
            self.canal("/prohibido"),
            self.canal("/manifest.mpd"),
        ]
        return Playlist(name="demo", channels=canales, source=self.base + "/lista.m3u")

    def test_recuento_por_veredicto(self) -> None:
        reporte = survey_playlist(self._playlist(), allow_private=True, workers=3)
        self.assertEqual(reporte.sondeados, 6)
        self.assertEqual(reporte.con_menu, 2)  # master + MPD
        # media playlist y MPEG-TS: se reproducen igual, sin nada que elegir
        self.assertEqual(reporte.sin_menu, 2)
        self.assertEqual(reporte.fallos, 2)  # 404 + 403
        self.assertEqual(reporte.total_canales, 6)
        # Las tres cifras tienen que sumar lo sondeado: si no, el informe
        # está contando mal y no sirve para diagnosticar.
        self.assertEqual(
            reporte.con_menu + reporte.sin_menu + reporte.fallos,
            reporte.sondeados,
        )

    def test_resumen_legible(self) -> None:
        reporte = survey_playlist(self._playlist(), allow_private=True, workers=3)
        texto = reporte.resumen()
        self.assertIn("SÍ hay menú de pistas: 2", texto)
        self.assertIn("menu", texto)
        self.assertIn(VERDICT_LABELS[OK_MENU], texto)
        self.assertIn(VERDICT_LABELS[HTTP_MISSING], texto)

    def test_ejemplos_por_veredicto(self) -> None:
        reporte = survey_playlist(self._playlist(), allow_private=True, workers=3)
        ejemplos = reporte.ejemplos(5)
        # Un ejemplo por veredicto, y los dos menús (HLS y DASH) aparecen.
        self.assertIn("master.m3u8", ejemplos)
        self.assertIn("manifest.mpd", ejemplos)
        self.assertIn("menu:", ejemplos)

    def test_el_tope_de_ejemplos_es_por_veredicto(self) -> None:
        import re as _re
        from collections import Counter

        reporte = survey_playlist(self._playlist(), allow_private=True, workers=3)
        for tope in (1, 2, 10):
            with self.subTest(tope=tope):
                lineas = reporte.ejemplos(tope).strip().splitlines()
                # El tope es por veredicto: "menu" aparece 2 veces (HLS y
                # DASH), así que el total puede exceder los veredictos.
                por_codigo = Counter(
                    _re.match(r"\s*- (\w+):", linea).group(1) for linea in lineas
                )
                self.assertTrue(por_codigo)
                self.assertEqual(set(por_codigo), {v.code for v in reporte.veredictos})
                self.assertLessEqual(max(por_codigo.values()), tope)

    def test_muestreo(self) -> None:
        playlist = self._playlist()
        for modo, esperados in (
            ("inicio", ["master.m3u8"]),
            ("fin", ["manifest.mpd"]),
        ):
            with self.subTest(modo=modo):
                reporte = survey_playlist(
                    playlist, limite=1, muestreo=modo, allow_private=True
                )
                self.assertEqual(reporte.sondeados, 1)
                self.assertEqual([v.name for v in reporte.veredictos], esperados)

    def test_muestreo_aleatorio(self) -> None:
        reporte = survey_playlist(
            self._playlist(), limite=3, muestreo="aleatorio", allow_private=True
        )
        self.assertEqual(reporte.sondeados, 3)

    def test_limite_cero_toma_todos(self) -> None:
        reporte = survey_playlist(self._playlist(), limite=0, allow_private=True)
        self.assertEqual(reporte.sondeados, 6)

    def test_lista_vacia(self) -> None:
        reporte = survey_playlist(Playlist(name="vacia", channels=[]), allow_private=True)
        self.assertEqual(reporte.sondeados, 0)
        self.assertEqual(reporte.con_menu, 0)
        self.assertIn("SÍ hay menú de pistas: 0", reporte.resumen())

    def test_progreso(self) -> None:
        vistos: list[tuple[int, int]] = []
        survey_playlist(
            self._playlist(), allow_private=True, workers=2,
            progreso=lambda h, t: vistos.append((h, t)),
        )
        self.assertEqual(vistos[-1], (6, 6))

    def test_orden_estable(self) -> None:
        reporte = survey_playlist(self._playlist(), allow_private=True, workers=4)
        codigos = [v.code for v in reporte.veredictos]
        self.assertEqual(codigos, sorted(codigos))

    def test_no_escribe_nada_en_disco(self) -> None:
        # Una encuesta no debe contaminar la caché de la app.
        with tempfile.TemporaryDirectory() as td:
            antes = set(Path(td).iterdir())
            survey_playlist(self._playlist(), allow_private=True)
            self.assertEqual(antes, set(Path(td).iterdir()))


class TestCargaDeListas(unittest.TestCase):
    def test_fichero_local(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ruta = Path(td) / "mini.m3u"
            ruta.write_text(
                "#EXTM3U\n"
                '#EXTINF:-1 tvg-id="a",Canal A\n'
                "http://ejemplo.test/a.ts\n",
                encoding="utf-8",
            )
            playlist = load_playlist(str(ruta))
            self.assertEqual(len(playlist.channels), 1)
            self.assertEqual(playlist.channels[0].name, "Canal A")

    def test_fuente_vacia(self) -> None:
        with self.assertRaises(ValueError):
            load_playlist("   ")


class TestPoliticaDeSeleccion(_Base):
    def test_lo_que_veredicto_muestra_es_lo_que_abriria_la_app(self) -> None:
        # La encuesta pregunta al TrackManager, el mismo que usa la TUI:
        # si divergieran, la encuesta mentiría.
        from thetvview.streams.probe import probe_capabilities
        from thetvview.tracks.manager import TrackManager

        canal = self.canal("/master.m3u8")
        veredicto = survey_channel(canal, allow_private=True)
        caps = probe_capabilities(
            canal, allow_private=True, use_cache=False, cache_dir=None
        ).capabilities
        opciones = TrackManager(caps, None).options()
        self.assertEqual(veredicto.menu_audio, opciones.audio_selectable)
        self.assertEqual(veredicto.menu_subtitles, opciones.subtitles_selectable)

    def test_seleccion_vacia_no_es_menu(self) -> None:
        sel = PlaybackSelection()
        self.assertTrue(sel.is_default)


class _Local:
    """Servidor local con los mismos seis recursos que :class:`_Base`."""

    def __init__(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def canal(self, ruta: str) -> Channel:
        return Channel(name=ruta.strip("/"), url=self.base + ruta)


class TestSeleccionRapida(unittest.TestCase):
    """No demorarse: elegir rápido los canales que de verdad sirven."""

    def test_los_primeros_son_de_hosts_distintos(self) -> None:
        # 20 hosts con 9 canales cada uno, como la lista real (59 canales de un
        # host, 1810 en total).
        canales = [
            Channel(name=f"c{i}", url=f"http://host{i % 20}.test/x.m3u8")
            for i in range(180)
        ]
        orden = diversificar_por_host(canales)
        primeros = [host_de(c) for c in orden[:20]]
        # 20 sondeos distintos, 20 proveedores distintos.
        self.assertEqual(len(set(primeros)), 20)
        # Y ningún canal de un host aparece dos veces seguidas.
        for a, b in zip(primeros, primeros[1:]):
            self.assertNotEqual(a, b)

    def test_no_se_pierde_ningun_canal(self) -> None:
        canales = [
            Channel(name=f"c{i}", url=f"http://host{i % 7}.test/x.m3u8")
            for i in range(50)
        ] + [Channel(name="raro", url="rtmp://otro/live")]
        orden = diversificar_por_host(canales)
        self.assertEqual(len(orden), len(canales))
        self.assertEqual(
            sorted(c.name for c in orden), sorted(c.name for c in canales)
        )

    def test_el_host_sin_esquema_no_agrupa(self) -> None:
        canales = [
            Channel(name="a", url="rtmp://h1/live"),
            Channel(name="b", url="rtmp://h2/live"),
            Channel(name="c", url="http://h3/a.ts"),
        ]
        self.assertEqual(len(diversificar_por_host(canales)), 3)

    def test_prioriza_master_primero(self) -> None:
        canales = [
            Channel(name="ts", url="http://h/live.ts"),
            Channel(name="media", url="http://h/live/canal.m3u8"),
            Channel(name="master", url="http://h/live/master.m3u8"),
        ]
        orden = [c.name for c in prioriza_canales(canales)]
        self.assertEqual(orden, ["master", "media", "ts"])

    def test_prioriza_timeshift_antes_que_m3u8_generico(self) -> None:
        canales = [
            Channel(name="generico", url="http://h/play/a.m3u8"),
            Channel(name="timeshift", url="http://h/play/a/index.m3u8"),
        ]
        self.assertEqual(
            [c.name for c in prioriza_canales(canales)],
            ["timeshift", "generico"],
        )

    def test_priorizar_no_pierde_canales(self) -> None:
        canales = [Channel(name=str(i), url=f"http://h{i}.test/a.m3u8")
                   for i in range(30)]
        self.assertEqual(len(prioriza_canales(canales)), 30)


class TestParadaTemprana(unittest.TestCase):
    def setUp(self) -> None:
        self.local = _Local()
        self.addCleanup(self.local.close)

    def _playlist(self, n: int = 40) -> Playlist:
        # El primero es el master (sirve); el resto son 404 (no sirven). Con
        # objetivo=1 debe parar tras el primer lote y no mirar los 40.
        canales = [self.local.canal("/master.m3u8")]
        canales += [self.local.canal(f"/ausente{i}.m3u8") for i in range(n)]
        return Playlist(name="larga", channels=canales, source=self.local.base)

    def test_se_para_en_cuando_tiene_el_objetivo(self) -> None:
        reporte = survey_playlist(self._playlist(), objetivo=1, workers=2,
                                  allow_private=True)
        self.assertTrue(reporte.paro_antes)
        self.assertEqual(reporte.con_menu, 1)
        # El lote mínimo es max(2*workers, 4) = 4, así que no llega a 40.
        self.assertLess(reporte.sondeados, 40)
        self.assertLess(reporte.segundos, 30)

    def test_sin_objetivo_mide_la_lista_entera(self) -> None:
        reporte = survey_playlist(self._playlist(), limite=0, workers=4,
                                  allow_private=True)
        self.assertFalse(reporte.paro_antes)
        self.assertEqual(reporte.sondeados, 41)

    def test_el_informe_avisa_de_que_no_es_medicion_completa(self) -> None:
        reporte = survey_playlist(self._playlist(), objetivo=1, workers=2,
                                  allow_private=True)
        texto = reporte.resumen()
        self.assertIn("parado", texto)
        self.assertIn("no** es el porcentaje", texto)

    def test_objetivo_mayor_que_lo_que_hay_no_se_para(self) -> None:
        # Sólo un canal sirve y se piden 5: se agota la lista sin marcar paro.
        reporte = survey_playlist(self._playlist(), objetivo=5, workers=4,
                                  allow_private=True)
        self.assertFalse(reporte.paro_antes)
        self.assertEqual(reporte.con_menu, 1)

    def test_limite_tras_priorizar_recorta_por_prioridad(self) -> None:
        # -n 2 con prioridad debe devolver los 2 primeros por prioridad, no
        # los dos primeros de la lista.
        playlist = Playlist(name="p", channels=[
            Channel(name="ts", url="http://h/a.ts"),
            Channel(name="gen", url="http://h/b.m3u8"),
            Channel(name="master", url="http://h/master.m3u8"),
        ])
        reporte = survey_playlist(playlist, limite=2, workers=1,
                                  allow_private=True, timeout=0.5)
        self.assertEqual(reporte.sondeados, 2)
        nombres = {v.name for v in reporte.veredictos}
        self.assertIn("master", nombres)
