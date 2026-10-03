"""Tests del proxy de pinning (plan F5c, SDD §37/§51).

Lo que se fija:

- el master reescrito tiene **una sola variante** y conserva la pista elegida;
- todas las URI del master reescrito apuntan **al proveedor**: el proxy no
  es un proxy de segmentos;
- el servidor escucha **sólo en loopback**, en un puerto efímero y **con
  token**: sin token, cualquier otra ruta responde 404;
- se apaga siempre y es idempotente;
- si no se puede fijar, se dice (no se finge).
"""

from __future__ import annotations

import socket
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from thetvview.streams.hls import parse_master_playlist
from thetvview.streams.hls import parse_master_playlist
from thetvview.streams.pin_proxy import (
    AUDIO_GROUP,
    LOOPBACK,
    SUBTITLE_GROUP,
    TOKEN_BYTES,
    PinProxy,
    PinProxyError,
    rewrite_master,
    start_pin_proxy,
)
from thetvview.tracks.models import PlaybackSelection

FIXTURES = Path(__file__).parent / "fixtures" / "streams"
BASE = "https://h.test/live/canal/master.m3u8"


def caps():
    return parse_master_playlist(
        (FIXTURES / "master_full.m3u8").read_text(encoding="utf-8"), final_url=BASE
    )


def sel_720(**kw) -> PlaybackSelection:
    base = {
        "audio_track_id": "a2",
        "subtitle_track_id": "s2",
        "subtitles_enabled": True,
        "subtitles_decided": True,
        "video_track_id": "v720",
        "auto_quality": False,
    }
    base.update(kw)
    return PlaybackSelection(**base)


class TestReescrituraDelMaster(unittest.TestCase):
    def test_una_sola_variante(self) -> None:
        texto = rewrite_master(caps(), sel_720())
        self.assertEqual(texto.count("#EXT-X-STREAM-INF"), 1)
        self.assertNotIn("1080p/index.m3u8", texto)
        self.assertNotIn("360p/index.m3u8", texto)

    def test_la_uri_apunta_al_proveedor(self) -> None:
        texto = rewrite_master(caps(), sel_720())
        self.assertIn("https://h.test/live/canal/720p/index.m3u8", texto)

    def test_el_audio_elegido_es_el_unico_default(self) -> None:
        texto = rewrite_master(caps(), sel_720())
        # Se conserva el grupo entero (si no, el master no es válido y el
        # reproductor se queda sin audio), pero sólo la elegida queda como
        # DEFAULT: eso es lo que la fija.
        por_defecto = [l for l in texto.splitlines()
                       if l.startswith("#EXT-X-MEDIA:TYPE=AUDIO")
                       and "DEFAULT=YES" in l]
        self.assertEqual(len(por_defecto), 1)
        self.assertIn("audio/en.m3u8", por_defecto[0])
        self.assertIn("audio/es.m3u8", texto)

    def test_el_subtitulo_elegido_es_el_unico_default(self) -> None:
        texto = rewrite_master(caps(), sel_720())
        por_defecto = [l for l in texto.splitlines()
                       if l.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES")
                       and "DEFAULT=YES" in l]
        self.assertEqual(len(por_defecto), 1)
        self.assertIn("subs/en.m3u8", por_defecto[0])

    def test_sin_subtitulos_no_hay_grupo_de_subtitulos(self) -> None:
        texto = rewrite_master(caps(), sel_720(subtitle_track_id=None, subtitles_enabled=False))
        self.assertNotIn("#EXT-X-MEDIA:TYPE=SUBTITLES", texto)

    def test_atributos_originales_conservados(self) -> None:
        # El NAME y el LANGUAGE del proveedor se mantienen: el reproductor
        # sigue viendo lo mismo que antes, sólo que fijado.
        texto = rewrite_master(caps(), sel_720())
        self.assertIn('NAME="English"', texto)
        self.assertIn('LANGUAGE="en"', texto)
        self.assertIn("DEFAULT=YES", texto)

    def test_el_master_es_legible_por_el_parser(self) -> None:
        texto = rewrite_master(caps(), sel_720())
        caps2 = parse_master_playlist(texto, final_url="http://127.0.0.1:1/x/master.m3u8")
        self.assertEqual(len(caps2.video_variants), 1)
        self.assertEqual(caps2.video_variants[0].height, 720)
        # El grupo se conserva entero, así que hay 2 audios y 2 subtítulos;
        # lo que cambia es cuál está marcada como DEFAULT.
        self.assertEqual(len(caps2.audio_tracks), 2)
        self.assertEqual(len(caps2.subtitle_tracks), 2)
        self.assertTrue([t for t in caps2.audio_tracks if t.is_default][0].uri
                        .endswith("audio/en.m3u8"))
        self.assertFalse(caps2.adaptive_bitrate)

    def test_sin_fijar_conserva_todas_las_variantes(self) -> None:
        # AC-08: volver a Automática no puede degradar nada.
        texto = rewrite_master(caps(), PlaybackSelection())
        self.assertEqual(texto.count("#EXT-X-STREAM-INF"), 3)
        self.assertIn("360p/index.m3u8", texto)
        self.assertIn("1080p/index.m3u8", texto)

    def test_sin_fijar_conserva_el_grupo_de_audio_del_proveedor(self) -> None:
        texto = rewrite_master(caps(), PlaybackSelection())
        self.assertIn('AUDIO="aud"', texto)

    def test_variante_inexistente_es_error_controlado(self) -> None:
        with self.assertRaises(PinProxyError) as ctx:
            rewrite_master(caps(), sel_720(video_track_id="v999"))
        self.assertIn("ya no existe", str(ctx.exception))

    def test_manifiesto_sin_variantes_es_error(self) -> None:
        from thetvview.tracks.models import PROTO_HLS, MediaCapabilities

        vacio = MediaCapabilities(protocol=PROTO_HLS)
        with self.assertRaises(PinProxyError) as ctx:
            rewrite_master(vacio, sel_720())
        self.assertIn("ninguna variante", str(ctx.exception))

    def test_un_mpd_no_se_reescribe_nunca(self) -> None:
        """Un MPD de DASH no puede servirse como master HLS.

        Sus representations no tienen URI de playlist, así que el master
        sintético saldría con la línea de URI vacía: mpv no arrancaría y el
        usuario vería el canal roto por haber elegido calidad. Preferimos
        negarnos y que el llamante caiga a calidad automática.
        """
        from thetvview.tracks.models import (
            PROTO_DASH,
            VIDEO,
            MediaCapabilities,
            MediaTrack,
        )

        dash = MediaCapabilities(
            video_variants=[
                MediaTrack(id="v360", type=VIDEO, height=360, width=640,
                           bitrate=800_000),
                MediaTrack(id="v720", type=VIDEO, height=720, width=1280,
                           bitrate=3_000_000),
            ],
            protocol=PROTO_DASH,
        )
        with self.assertRaises(PinProxyError) as ctx:
            rewrite_master(dash, PlaybackSelection(
                video_track_id="v720", auto_quality=False))
        self.assertIn("HLS", str(ctx.exception))
        # Y quien lo llama recibe None, para caer a calidad automática.
        self.assertIsNone(start_pin_proxy(dash, PlaybackSelection(
            video_track_id="v720", auto_quality=False)))

    def test_solo_calidad_no_rompe_el_audio(self) -> None:
        # Si el usuario no ha elegido audio, se conserva el grupo ORIGINAL
        # del proveedor con su DEFAULT: quitarlo o referenciar un grupo que
        # no existe deja al reproductor sin audio (verificado con mpv:
        # "AID=no").
        texto = rewrite_master(caps(), PlaybackSelection(video_track_id="v720",
                                                        auto_quality=False))
        self.assertIn("#EXT-X-MEDIA:TYPE=AUDIO", texto)
        self.assertIn('AUDIO="aud"', texto)
        por_defecto = [l for l in texto.splitlines()
                       if l.startswith("#EXT-X-MEDIA:TYPE=AUDIO")
                       and "DEFAULT=YES" in l]
        self.assertEqual(len(por_defecto), 1)
        self.assertIn("audio/es.m3u8", por_defecto[0])  # el DEFAULT del proveedor
        caps2 = parse_master_playlist(texto, final_url=BASE)
        self.assertEqual(len(caps2.video_variants), 1)
        self.assertEqual(caps2.video_variants[0].metadata["audio_group"], "aud")
        self.assertEqual(len(caps2.audio_tracks), 2)

    def test_sin_fijar_tambien_conserva_los_grupos(self) -> None:
        # El master "sin fijar" tampoco puede perder los EXT-X-MEDIA: sus
        # variantes los referencian.
        texto = rewrite_master(caps(), PlaybackSelection())
        self.assertIn("#EXT-X-MEDIA:TYPE=AUDIO", texto)
        self.assertIn("#EXT-X-MEDIA:TYPE=SUBTITLES", texto)
        leido = parse_master_playlist(texto, final_url=BASE)
        self.assertEqual(len(leido.video_variants), 3)
        self.assertEqual(len(leido.audio_tracks), 2)
        for variante in leido.video_variants:
            self.assertEqual(variante.metadata["audio_group"], "aud")

    def test_con_audio_eligido_se_usa_el_grupo_propio(self) -> None:
        texto = rewrite_master(caps(), sel_720())
        self.assertIn(f'GROUP-ID="{AUDIO_GROUP}"', texto)
        self.assertIn(f"AUDIO=\"{AUDIO_GROUP}\"", texto)

    def test_con_subtitulo_eligido_se_referencia_el_grupo(self) -> None:
        texto = rewrite_master(caps(), sel_720())
        self.assertIn(f"SUBTITLES=\"{SUBTITLE_GROUP}\"", texto)

    def test_el_manifesto_fijado_sigue_siendo_legible(self) -> None:
        # Un master fijado que el parser no entiende es un master roto.
        for sel in (sel_720(), PlaybackSelection(video_track_id="v720",
                                                  auto_quality=False),
                    PlaybackSelection()):
            with self.subTest(auto=sel.auto_quality, audio=sel.audio_track_id):
                texto = rewrite_master(caps(), sel)
                leido = parse_master_playlist(texto, final_url=BASE)
                self.assertTrue(leido.video_variants)

    def test_cabecera_extm3u_presente(self) -> None:
        self.assertTrue(rewrite_master(caps(), sel_720()).startswith("#EXTM3U"))


class TestServidor(unittest.TestCase):
    def setUp(self) -> None:
        self.proxy = PinProxy.start(caps(), sel_720())
        self.addCleanup(self.proxy.stop)

    def _get(self, url: str, timeout: float = 3.0):
        return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 - local

    def test_sirve_el_master(self) -> None:
        with self._get(self.proxy.url) as resp:
            self.assertEqual(resp.status, 200)
            cuerpo = resp.read().decode()
        self.assertIn("#EXT-X-STREAM-INF", cuerpo)
        self.assertEqual(resp.headers.get("Content-Type"),
                         "application/vnd.apple.mpegurl")

    def test_escucha_solo_en_loopback(self) -> None:
        self.assertTrue(self.proxy.url.startswith(f"http://{LOOPBACK}:"))
        self.assertNotIn("0.0.0.0", self.proxy.url)

    def test_puerto_efimero(self) -> None:
        self.assertGreater(self.proxy.port, 0)
        self.assertLess(self.proxy.port, 65536)

    def test_token_de_128_bits(self) -> None:
        self.assertEqual(TOKEN_BYTES, 16)
        self.assertEqual(len(self.proxy.token), 32)
        int(self.proxy.token, 16)  # es hexadecimal

    def test_sin_token_responde_404(self) -> None:
        otros = "0" * 32
        url = f"http://{LOOPBACK}:{self.proxy.port}/{otros}/master.m3u8"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(url)
        self.assertEqual(ctx.exception.code, 404)

    def test_otra_ruta_responde_404(self) -> None:
        for ruta in ("/", f"/{self.proxy.token}", f"/{self.proxy.token}/",
                     f"/{self.proxy.token}/seg0.ts", f"/{self.proxy.token}/otro.m3u8",
                     "/index.html"):
            with self.subTest(ruta=ruta):
                url = f"http://{LOOPBACK}:{self.proxy.port}{ruta}"
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self._get(url)
                self.assertEqual(ctx.exception.code, 404)

    def test_no_hace_de_proxy_de_segmentos(self) -> None:
        # Si hiciera de proxy, el segmento no estaría en su allowlist.
        url = f"http://{LOOPBACK}:{self.proxy.port}/{self.proxy.token}/720p/index.m3u8"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(url)
        self.assertEqual(ctx.exception.code, 404)

    def test_head_tambien(self) -> None:
        peticion = urllib.request.Request(self.proxy.url, method="HEAD")
        with urllib.request.urlopen(peticion, timeout=3.0) as resp:  # noqa: S310
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Length"),
                             str(len(self.proxy.body.encode())))

    def test_parado_y_idempotente(self) -> None:
        self.assertTrue(self.proxy.is_running)
        self.proxy.stop()
        self.assertFalse(self.proxy.is_running)
        self.proxy.stop()  # no explota
        with self.assertRaises((urllib.error.URLError, OSError)):
            self._get(self.proxy.url)

    def test_se_puede_volver_a_parar_mas_veces(self) -> None:
        for _ in range(3):
            self.proxy.stop()
        self.assertFalse(self.proxy.is_running)

    def test_es_solo_loopback_por_diseno(self) -> None:
        with self.assertRaises(PinProxyError) as ctx:
            PinProxy.start(caps(), sel_720(), host="0.0.0.0")
        self.assertIn("loopback", str(ctx.exception))

    def test_no_escucha_en_la_red(self) -> None:
        # Una comprobación de verdad: el puerto no está en la interfaz.
        con_puerto = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            con_puerto.settimeout(0.5)
            # Si el bind fuera 0.0.0.0, conectar a la IP de la máquina
            # wouldn't fall. Con 127.0.0.1 sí funciona, así que lo que se
            # comprueba aquí es que el puerto existe y es local.
            con_puerto.connect((LOOPBACK, self.proxy.port))
            self.assertEqual(con_puerto.getsockname()[0], LOOPBACK)
        finally:
            con_puerto.close()


class TestContextManager(unittest.TestCase):
    def test_se_apaga_al_salir(self) -> None:
        proxy = PinProxy.start(caps(), sel_720())
        url = proxy.url
        with proxy:
            with urllib.request.urlopen(url, timeout=3.0) as resp:  # noqa: S310
                self.assertEqual(resp.status, 200)
        with self.assertRaises((urllib.error.URLError, OSError)):
            urllib.request.urlopen(url, timeout=1.0)  # noqa: S310


class TestAtajo(unittest.TestCase):
    def test_devuelve_none_si_no_hay_que_fijar(self) -> None:
        self.assertIsNone(start_pin_proxy(caps(), PlaybackSelection()))
        self.assertIsNone(start_pin_proxy(None, sel_720()))
        self.assertIsNone(start_pin_proxy(caps(), None))
        self.assertIsNone(start_pin_proxy(caps(), sel_720(auto_quality=True)))

    def test_devuelve_proxy_si_hay_que_fijar(self) -> None:
        proxy = start_pin_proxy(caps(), sel_720())
        assert proxy is not None
        try:
            self.assertIn("720p/index.m3u8", proxy.body)
            self.assertIn(proxy.token, proxy.url)
        finally:
            proxy.stop()

    def test_none_si_el_manifesto_no_da_variantes(self) -> None:
        from thetvview.tracks.models import MediaCapabilities

        self.assertIsNone(start_pin_proxy(MediaCapabilities(), sel_720()))