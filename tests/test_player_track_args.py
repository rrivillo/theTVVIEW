"""Tests de la traducción a argv por reproductor (plan F5b, SDD §46/§51).

Lo que se verifica:

- **lista blanca**: ningún valor del manifiesto llega al argv si no pasa el
  filtro (imprimible, sin ``-`` al principio, sin saltos, 120 caracteres);
- **el ``--`` no se mueve**: el argv termina siempre en ``["--", url]`` (gap
  B1), también con selección;
- **sólo lo que el reproductor sabe hacer**: nadie recibe opciones de calidad
  (verificado empíricamente: no hay ninguna que funcione);
- **sin proxy, índices del manifest original; con proxy, nada** (porque al
  reescribir el master los índices cambian).
"""

from __future__ import annotations

import unittest

from thetvview.models import Channel
from thetvview.player import PlayerError, command_for, track_args, track_warnings
from thetvview.player.capabilities import (
    KIND_AUDIO,
    KIND_QUALITY,
    KIND_SUBTITLES,
    hot_control,
    supports,
)
from thetvview.player.track_args import audio_index, subtitle_index
from thetvview.tracks.models import PROTO_DASH, PROTO_HLS
from thetvview.tracks.models import (
    AUDIO,
    TEXT,
    VIDEO,
    MediaCapabilities,
    MediaTrack,
    PlaybackSelection,
)

URL = "https://cdn.example.com/live/canal.m3u8"


def caps() -> MediaCapabilities:
    return MediaCapabilities(
        audio_tracks=[
            MediaTrack(id="a1", type=AUDIO, language="es", is_default=True,
                       channels="2", uri="https://cdn/a1.m3u8"),
            MediaTrack(id="a2", type=AUDIO, language="en", channels="6",
                       uri="https://cdn/a2.m3u8"),
        ],
        subtitle_tracks=[
            MediaTrack(id="s1", type=TEXT, language="es", uri="https://cdn/s1.vtt"),
            MediaTrack(id="s2", type=TEXT, language="en", is_forced=True,
                       uri="https://cdn/s2.vtt"),
        ],
        video_variants=[
            MediaTrack(id="v720", type=VIDEO, height=720, width=1280,
                       bitrate=3_800_000, uri="https://cdn/v720.m3u8"),
            MediaTrack(id="v1080", type=VIDEO, height=1080, width=1920,
                       bitrate=6_800_000, uri="https://cdn/v1080.m3u8"),
        ],
        adaptive_bitrate=True,
        protocol="hls",
    )


def sel(**kw) -> PlaybackSelection:
    base = {"audio_track_id": "a2", "subtitle_track_id": "s2", "subtitles_enabled": True}
    base.update(kw)
    return PlaybackSelection(**base)


class TestIndices(unittest.TestCase):
    def test_indice_de_audio_es_1_based(self) -> None:
        self.assertEqual(audio_index(caps(), "a1"), 1)
        self.assertEqual(audio_index(caps(), "a2"), 2)

    def test_indice_de_subtitulo(self) -> None:
        self.assertEqual(subtitle_index(caps(), "s2"), 2)

    def test_pista_inexistente_no_tiene_indice(self) -> None:
        self.assertIsNone(audio_index(caps(), "a9"))
        self.assertIsNone(audio_index(None, "a1"))
        self.assertIsNone(audio_index(caps(), None))


class TestArgvPorReproductor(unittest.TestCase):
    def test_mpv_indice(self) -> None:
        self.assertEqual(track_args("mpv", sel(), caps()), ["--aid=2", "--sid=2"])

    def test_vlc_idioma_e_indice(self) -> None:
        self.assertEqual(
            track_args("vlc", sel(), caps()),
            [
                "--audio-language=en",
                "--audio-track-id=2",
                "--sub-language=en",
                "--sub-track-id=2",
            ],
        )

    def test_mplayer_indice(self) -> None:
        self.assertEqual(
            track_args("mplayer", sel(), caps()), ["-aid", "2", "-sid", "2"]
        )

    def test_mplayer_por_idioma_si_no_hay_indice(self) -> None:
        # Sin capacidades no hay índices: se cae al idioma, que sí lo entiende.
        self.assertEqual(track_args("mplayer", sel(), None), [])

    def test_subtitulos_desactivados(self) -> None:
        apagados = PlaybackSelection(subtitles_enabled=False, subtitles_decided=True)
        self.assertEqual(track_args("mpv", apagados, caps()), ["--sid=no"])
        self.assertEqual(track_args("vlc", apagados, caps()), ["--sub-track-id=-1"])
        # mplayer no tiene equivalente y no se le inventa uno.
        self.assertEqual(track_args("mplayer", apagados, caps()), [])

    def test_audio_none_no_pasa_nada(self) -> None:
        # None = "usa el DEFAULT del stream": el argv queda como siempre.
        vacia = PlaybackSelection()
        for nombre in ("mpv", "vlc", "mplayer"):
            with self.subTest(nombre=nombre):
                self.assertEqual(track_args(nombre, vacia, caps()), [])

    def test_calidad_nunca_llega_al_argv(self) -> None:
        # Verificado: mpv no tiene --video-bitrate, VLC --program es de TV.
        con_calidad = PlaybackSelection(video_track_id="v720", auto_quality=False)
        for nombre in ("mpv", "vlc", "mplayer"):
            with self.subTest(nombre=nombre):
                self.assertEqual(track_args(nombre, con_calidad, caps()), [])

    def test_reproductor_desconocido_no_anda(self) -> None:
        self.assertEqual(track_args("ffplay", sel(), caps()), [])
        self.assertEqual(track_args("", sel(), caps()), [])
        self.assertEqual(track_args(None, sel(), caps()), [])

    def test_seleccion_none(self) -> None:
        self.assertEqual(track_args("mpv", None, caps()), [])

    def test_pista_inexistente_no_traduce_nada(self) -> None:
        huerfana = PlaybackSelection(audio_track_id="a9", subtitles_enabled=True,
                                     subtitle_track_id="s9")
        self.assertEqual(track_args("mpv", huerfana, caps()), [])


class TestFiltroDeValores(unittest.TestCase):
    def _caps_malos(self, valor) -> MediaCapabilities:
        return MediaCapabilities(
            audio_tracks=[
                MediaTrack(id="a1", type=AUDIO, language=valor, uri="https://cdn/a1.m3u8"),
                MediaTrack(id="a2", type=AUDIO, language="en", uri="https://cdn/a2.m3u8"),
            ],
            video_variants=[],
            subtitle_tracks=[],
        )

    def test_valor_no_imprimible_se_descarta(self) -> None:
        for valor in ("es\n--script=/tmp/x", "es\x00", "es\rX"):
            with self.subTest(valor=valor):
                args = track_args("vlc", PlaybackSelection(audio_track_id="a1"),
                                  self._caps_malos(valor))
                # El idioma no aparece, pero el índice (numérico, Our sí es seguro).
                self.assertNotIn(f"--audio-language={valor}", args)
                for arg in args:
                    self.assertNotIn("\n", arg)
                    self.assertNotIn("\x00", arg)

    def test_valor_que_empieza_por_guion_no_llega(self) -> None:
        args = track_args(
            "mplayer", PlaybackSelection(audio_track_id="a1"),
            self._caps_malos("-alang"),
        )
        self.assertNotIn("-alang", args)

    def test_valor_largo_se_trunca(self) -> None:
        larguisimo = "x" * 400
        args = track_args("vlc", PlaybackSelection(audio_track_id="a1"),
                          self._caps_malos(larguisimo))
        for arg in args:
            if arg.startswith("--audio-language="):
                self.assertLess(len(arg), 140)


class TestSeparadorGapB1(unittest.TestCase):
    def _argv(self, **kw) -> list[str]:
        canal = Channel(name="Canal", url=URL)
        return command_for(canal, "mpv", player_path="/usr/bin/true", **kw)

    def test_sin_seleccion_termina_en_dos_guiones(self) -> None:
        self.assertEqual(self._argv()[-2:], ["--", URL])

    def test_con_seleccion_termina_en_dos_guiones(self) -> None:
        argv = self._argv(selection=sel(), capabilities=caps())
        self.assertEqual(argv[-2:], ["--", URL])

    def test_con_proxy_termina_en_dos_guiones(self) -> None:
        proxy = "http://127.0.0.1:45001/token123/master.m3u8"
        argv = self._argv(selection=sel(), capabilities=caps(), proxy_url=proxy)
        self.assertEqual(argv[-2:], ["--", proxy])

    def test_ipc_tambien_antes_del_separador(self) -> None:
        argv = self._argv(ipc_path="/tmp/data/ipc/mpv-1-abc.sock")
        self.assertEqual(argv[-2:], ["--", URL])
        self.assertIn("--input-ipc-server=/tmp/data/ipc/mpv-1-abc.sock", argv)
        self.assertLess(argv.index("--input-ipc-server=/tmp/data/ipc/mpv-1-abc.sock"),
                        len(argv) - 2)

    def test_url_hostil_no_altera_el_argv(self) -> None:
        hostil = Channel(name="x", url="https://cdn.example.com/a.ts?q=1;$(id)")
        argv = command_for(hostil, "mpv", player_path="/usr/bin/true",
                           selection=sel(), capabilities=caps())
        self.assertEqual(argv[-2:], ["--", hostil.url])


class TestProxyEnElArgv(unittest.TestCase):
    def test_con_proxy_no_se_pasan_indices(self) -> None:
        # Comprobado con mpv real: reescribir el master renumera las pistas.
        self.assertEqual(
            track_args("mpv", sel(), caps(), pinned_via_proxy=True), []
        )

    def test_proxy_url_tolera_la_politica_de_url(self) -> None:
        with self.assertRaises(PlayerError):
            command_for(Channel(name="x", url=URL), "mpv", player_path="/usr/bin/true",
                        proxy_url="file:///etc/passwd")

    def test_el_proxy_solo_escucha_en_loopback(self) -> None:
        # La garantía está en PinProxy.start, que rechaza cualquier host que
        # no sea 127.0.0.1 (test en test_pin_proxy).
        from thetvview.streams.pin_proxy import PinProxy, PinProxyError

        with self.assertRaises(PinProxyError):
            PinProxy.start(caps(), sel(video_track_id="v720", auto_quality=False),
                           host="0.0.0.0")

    def test_ipc_solo_para_mpv(self) -> None:
        for nombre in ("vlc", "mplayer"):
            with self.subTest(nombre=nombre):
                argv = command_for(Channel(name="x", url=URL), nombre,
                                   player_path="/usr/bin/true", ipc_path="/tmp/x.sock")
                self.assertFalse(any(a.startswith("--input-ipc-server") for a in argv))

    def test_ipc_hostil_se_ignora(self) -> None:
        argv = command_for(Channel(name="x", url=URL), "mpv",
                           player_path="/usr/bin/true", ipc_path="--inyeccion")
        self.assertFalse(any("inyeccion" in a for a in argv))


class TestTablaDeCapacidades(unittest.TestCase):
    def test_audio_y_subtitulos_en_los_tres(self) -> None:
        for nombre in ("mpv", "vlc", "mplayer"):
            self.assertTrue(supports(nombre, KIND_AUDIO), nombre)
            self.assertTrue(supports(nombre, KIND_SUBTITLES), nombre)

    def test_calidad_en_ninguno(self) -> None:
        for nombre in ("mpv", "vlc", "mplayer"):
            self.assertFalse(supports(nombre, KIND_QUALITY), nombre)

    def test_desconocido_no_promete_nada(self) -> None:
        self.assertFalse(supports("ffplay", KIND_AUDIO))
        self.assertFalse(supports(None, KIND_AUDIO))

    def test_control_en_caliente_solo_mpv(self) -> None:
        self.assertTrue(hot_control("mpv"))
        self.assertFalse(hot_control("vlc"))
        self.assertFalse(hot_control("mplayer"))
        self.assertFalse(hot_control(None))


class TestAvisos(unittest.TestCase):
    def test_sin_seleccion_no_hay_avisos(self) -> None:
        self.assertEqual(track_warnings("mpv", PlaybackSelection(), caps()), [])

    def test_calidad_fija_en_hls_no_avisa(self) -> None:
        """La calidad elegida **sí** se aplica (por el proxy de pinzado).

        Avisar aquí era un ruido que además sonaba a avería: el usuario
        elegía 720p, se reproducía en 720p y la app le decía "no se pide al
        reproductor…". Un aviso describe lo que no va a funcionar.
        """
        con_calidad = PlaybackSelection(video_track_id="v720", auto_quality=False)
        self.assertEqual(track_warnings("mpv", con_calidad, caps()), [])

    def test_calidad_que_no_se_puede_fijar_si_avisa(self) -> None:
        # DASH: no hay master que reescribir, así que la elección se pierde
        # de verdad y el usuario tiene que saberlo.
        dash = MediaCapabilities(
            video_variants=[
                MediaTrack(id="v720", type=VIDEO, height=720, width=1280,
                           bitrate=3_800_000),
                MediaTrack(id="v1080", type=VIDEO, height=1080, width=1920,
                           bitrate=6_800_000),
            ],
            protocol=PROTO_DASH,
        )
        con_calidad = PlaybackSelection(video_track_id="v720", auto_quality=False)
        avisos = track_warnings("mpv", con_calidad, dash)
        self.assertEqual(len(avisos), 1)
        self.assertIn("calidad automática", avisos[0])

    def test_reproductor_desconocido_avisa(self) -> None:
        avisos = track_warnings("ffplay", sel(), caps())
        self.assertEqual(len(avisos), 1)
        self.assertIn("conocido", avisos[0])

    def test_todo_aplicable_no_avisa(self) -> None:
        # mpv con audio/subtítulo por argv no tiene nada que explicar.
        self.assertEqual(track_warnings("mpv", sel(), caps()), [])

class TestSubtitulosSinModal(unittest.TestCase):
    """Lo de los subtítulos ya **no** es un modal.

    Se cuenta junto a la lista de idiomas, mientras el usuario elige
    (``TrackOptionsScreen._nota_subtitulos``). Un modal posterior le
    interrumpía con la decisión ya tomada y el reproductor sin elegir, que es
    justo cuando el aviso menos puede cambiar algo.

    Aquí se comprueba lo que sí importa: que ``track_warnings`` **no** dice
    nada sobre los subtítulos en ningún reproductor, para que no se cuele por
    la otra puerta.
    """

    def _caps(self, *, uri: str | None = "https://cdn/es.m3u8") -> MediaCapabilities:
        return MediaCapabilities(
            audio_tracks=[MediaTrack(id="a1", type=AUDIO, language="es")],
            subtitle_tracks=[
                MediaTrack(id="s1", type=TEXT, language="es", uri=uri),
                MediaTrack(id="s2", type=TEXT, language="en", uri=uri),
            ],
            video_variants=[MediaTrack(id="v720", type=VIDEO, height=720)],
            protocol=PROTO_HLS,
        )

    def _sel(self, **kw) -> PlaybackSelection:
        base = {"subtitle_track_id": "s2", "subtitles_enabled": True,
                "subtitles_decided": True}
        base.update(kw)
        return PlaybackSelection(**base)

    def test_ningun_reproductor_conocido_avisa(self) -> None:
        caps = self._caps()
        for reproductor in ("mpv", "mplayer", "vlc"):
            with self.subTest(reproductor=reproductor):
                self.assertEqual(
                    track_warnings(reproductor, self._sel(), caps), []
                )

    def test_un_reproductor_desconocido_sigue_avisando_de_ello(self) -> None:
        # Eso no es cosa de los subtítulos: es no saber qué sabe el programa.
        avisos = track_warnings("ffplay", self._sel(), self._caps())
        self.assertEqual(len(avisos), 1)
        self.assertIn("conocido", avisos[0])

    def test_tampoco_con_subtitulos_incrustados(self) -> None:
        caps = self._caps(uri=None)
        self.assertEqual(track_warnings("mpv", self._sel(), caps), [])

    def test_el_argv_sigue_siendo_el_de_siempre(self) -> None:
        # Quitar el aviso no quita la opción ni cambia lo que se manda.
        self.assertEqual(track_args("mpv", self._sel(), self._caps()), ["--sid=2"])
