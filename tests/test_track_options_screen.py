"""Tests de la pantalla de pistas y del routing (plan F6, SDD §32/§33).

Sin curses: la pantalla se instancia con una sesión falsa que devuelve
capacidades reales, y lo que se comprueba es **qué ofrecería al usuario** y
**qué acciones devuelve** —que es donde una UI miente sobre lo que el
proveedor expone.

Reglas que se verifican aquí:

- con una sola pista **no hay menú** y la reproducción sigue igual (AC-09);
- con dos o más, `Enter` devuelve la acción con la selección;
- `0` vuelve a Automático/Desactivados;
- la tecla `i` de "Reproduciendo" sólo existe cuando hay algo que analizar; las
  de pista (`a`/`s`/`v`) **no existen**: ya se eligieron antes del reproductor;
"""

from __future__ import annotations

import curses
import unittest
from typing import Any
from unittest import mock

from thetvview.models import Channel
from thetvview.tracks.manager import QUICK_AUTO, TrackManager
from thetvview.tracks.models import (
    AUDIO,
    DEGRADED_NOT_EXPOSED,
    TEXT,
    VIDEO,
    MediaCapabilities,
    MediaTrack,
    PlaybackSelection,
)
from thetvview.ui import colors
from thetvview.ui.app import App, build_help_lines
from thetvview.ui.tracks import degraded_outcome
from thetvview.ui.screens import (
    NowPlayingScreen,
    TrackOptionsScreen,
    options_kinds,
)

ENTER = 10
UP = curses.KEY_UP
DOWN = curses.KEY_DOWN
LEFT = curses.KEY_LEFT
RIGHT = curses.KEY_RIGHT


def caps_multi() -> MediaCapabilities:
    return MediaCapabilities(
        audio_tracks=[
            MediaTrack(id="a1", type=AUDIO, language="es", is_default=True,
                       uri="https://h/a1.m3u8"),
            MediaTrack(id="a2", type=AUDIO, language="en",
                       uri="https://h/a2.m3u8"),
        ],
        subtitle_tracks=[
            MediaTrack(id="s1", type=TEXT, language="es", uri="https://h/s1.vtt"),
            MediaTrack(id="s2", type=TEXT, language="en", uri="https://h/s2.vtt"),
        ],
        video_variants=[
            MediaTrack(id="v720", type=VIDEO, height=720, uri="https://h/v720.m3u8"),
            MediaTrack(id="v1080", type=VIDEO, height=1080, uri="https://h/v1080.m3u8"),
        ],
        adaptive_bitrate=True,
        protocol="hls",
    )


def caps_single() -> MediaCapabilities:
    return MediaCapabilities(
        audio_tracks=[MediaTrack(id="a1", type=AUDIO, language="es",
                                 uri="https://h/a1.m3u8")],
        video_variants=[MediaTrack(id="v720", type=VIDEO, height=720,
                                   uri="https://h/v720.m3u8")],
        protocol="hls",
    )


def caps_media_playlist() -> MediaCapabilities:
    """Un solo tipo de pista declarado de verdad: una media playlist.

    Es un caso distinto de :func:`caps_single`: aquí el manifiesto **lo dice**
    (una sola pista), mientras que `caps_single` es un master leído bien que
    resulta tener una sola variante. Los dos son "nada que elegir", pero sólo
    el primero tiene un motivo que explicar.
    """
    return MediaCapabilities(
        audio_tracks=[MediaTrack(id="a1", type=AUDIO, language="es")],
        protocol="hls",
        degraded_reason=DEGRADED_NOT_EXPOSED,
        note="Una sola pista (media playlist, 6s por segmento).",
    )


class _ResultadoFalso:
    """Lo que `degraded_outcome` necesita: capacidades y motivo."""

    def __init__(self, capabilities, reason=None) -> None:
        self.capabilities = capabilities
        self.reason = reason


def _outcome(capabilities) -> tuple:
    return degraded_outcome(_ResultadoFalso(capabilities))


class _SesionFalsa:
    """Lo mínimo de TrackSession que la pantalla usa."""

    def __init__(self, caps, pendientes: bool = False) -> None:
        self.manager = TrackManager(caps, None)
        self.pending = pendientes
        self.last_avisos: list[str] = []
        self.remembered: list[bool] = []
        self.ipc_path = None
        self.degraded_shown = False
        self._avisos: list[str] = []
        self.channel = Channel(name="Canal", url="https://h/x.m3u8")

    def take_avisos(self) -> list[str]:
        avisos, self._avisos = self._avisos, []
        return avisos

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    # En los tests el valor lo fija cada caso; por defecto no se espera.
    wait_settles = False

    def degraded_outcome(self) -> tuple[str | None, bool]:
        """Como la sesión real: (texto, es_fallo).

        Sin capacidades todavía equivale a "el sondeo no ha dicho nada",
        no a "el sondeo falló": en la sesión real eso lo decide `pending` y
        `result`, y el camino de fallo se ejercita en TestDegradedOutcome.
        """
        caps = self.manager.capabilities
        if caps is None:
            return None, False
        return _outcome(caps)

    def degraded_message(self) -> str | None:
        return self.degraded_outcome()[0]

    @property
    def capabilities(self):
        return self.manager.capabilities

    def options(self, player_name=None):
        # Mismo criterio que TrackSession.options: la calidad se ofrece
        # porque se aplica por el proxy de pinning, no por argv.
        return self.manager.options(player_supports=lambda _kind: True)

    def has_menu(self, player_name=None) -> bool:
        return self.options(player_name).has_menu

    def kinds(self, player_name=None) -> list[str]:
        return self.options(player_name).selectable_kinds

    def select(self, kind, choice_id, *, enabled=None):
        return self.manager.select(kind, choice_id, enabled=enabled)

    @property
    def selection(self) -> PlaybackSelection:
        return self.manager.selection

    def summary(self) -> dict[str, str]:
        datos = {"audio": "", "subtitles": "", "quality": "", "resolution": "",
                 "codec": ""}
        caps = self.manager.capabilities
        if caps is None:
            return datos
        pistas = caps.audio_tracks or []
        if pistas:
            datos["audio"] = pistas[0].language or ""
        if caps.video_variants:
            v = caps.sorted_variants()[0]
            datos["quality"] = f"{v.height}p"
            datos["resolution"] = f"{v.width}x{v.height}"
        return datos

    def remember(self, *, for_provider: bool = False) -> bool:
        self.remembered.append(for_provider)
        return True


class _AppFalso:
    def __init__(self) -> None:
        self.avisos: list[tuple[str, bool]] = []

    def notify_error(self, message: str) -> None:
        self.avisos.append((message, True))

    def notify_warning(self, message: str) -> None:
        self.avisos.append((message, False))

    def notify(self, message: str, *, error: bool = False, title=None) -> None:
        self.avisos.append((message, error))


def _pantalla(caps, player="mpv", kind=None) -> tuple[TrackOptionsScreen, _AppFalso]:
    app = _AppFalso()
    canal = Channel(name="Canal", url="https://h/x.m3u8")
    sesion = _SesionFalsa(caps)
    return TrackOptionsScreen(app, canal, sesion, player, kind=kind), app


def caps_con_subs(*, uri="https://h/sub.m3u8", protocol="hls"):
    """Audio + 2 subtítulos + 2 calidades: las tres secciones con menú."""
    return MediaCapabilities(
        audio_tracks=[
            MediaTrack(id="a1", type=AUDIO, language="es", is_default=True,
                       uri="https://h/a1.m3u8"),
            MediaTrack(id="a2", type=AUDIO, language="en",
                       uri="https://h/a2.m3u8"),
        ],
        subtitle_tracks=[
            MediaTrack(id="s1", type=TEXT, language="es", uri=uri),
            MediaTrack(id="s2", type=TEXT, language="en", uri=uri),
        ],
        video_variants=[
            MediaTrack(id="v360", type=VIDEO, height=360, width=640,
                       bitrate=500_000, uri="https://h/v360.m3u8"),
            MediaTrack(id="v720", type=VIDEO, height=720, width=1280,
                       bitrate=3_000_000, uri="https://h/v720.m3u8"),
        ],
        protocol=protocol,
        adaptive_bitrate=True,
    )


class TestPoliticaDeLaPantalla(unittest.TestCase):
    def test_sin_opciones_no_abre_menu(self) -> None:
        pantalla, _ = _pantalla(caps_single())
        self.assertEqual(pantalla.kinds, [])
        self.assertFalse(pantalla.options.has_menu)

    def test_tres_secciones_con_alternativas(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        self.assertEqual(pantalla.kinds, ["audio", "subtitles", "quality"])

    def test_una_seccion_concreta(self) -> None:
        pantalla, _ = _pantalla(caps_multi(), kind="audio")
        self.assertEqual(pantalla.kinds, ["audio"])
        self.assertTrue(all(c.kind == "audio" for c in pantalla.options.audio))

    def test_seccion_sin_alternativas_abre_vacia(self) -> None:
        pantalla, _ = _pantalla(caps_multi(), kind="subtitles")
        self.assertEqual(pantalla.kinds, ["subtitles"])

    def test_el_foco_empieza_en_lo_que_se_va_a_aplicar(self) -> None:
        # Sin preferencias aplicadas, lo que se va a ver es el "Automático":
        # el audio que marque el proveedor. Ofrecer otra marca sería mentir.
        pantalla, _ = _pantalla(caps_multi())
        self.assertEqual(pantalla.selected_choice.id, QUICK_AUTO)

    def test_tras_aplicar_preferencia_el_foco_sigue_ahi(self) -> None:
        app = _AppFalso()
        canal = Channel(name="C", url="https://h/x.m3u8")
        manager = TrackManager(caps_multi(), None)
        manager.select("audio", "a2")
        sesion = _SesionFalsa(caps_multi())
        sesion.manager = manager
        pantalla = TrackOptionsScreen(app, canal, sesion, "mpv")
        self.assertEqual(pantalla.selected_choice.id, "a2")


class TestNavegacion(unittest.TestCase):
    def test_abajo_y_arriba(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        primero = pantalla.selected
        pantalla.handle_key(DOWN)
        self.assertEqual(pantalla.selected, primero + 1)
        pantalla.handle_key(UP)
        self.assertEqual(pantalla.selected, primero)

    def test_mover_el_cursor_aplica_la_seleccion(self) -> None:
        # El cursor ES la selección: lo marcado es lo que se va a lanzar.
        pantalla, _ = _pantalla(caps_multi())
        self.assertIsNone(pantalla.selection.audio_track_id)
        pantalla.handle_key(DOWN)
        self.assertEqual(pantalla.selection.audio_track_id, "a1")
        pantalla.handle_key(DOWN)
        self.assertEqual(pantalla.selection.audio_track_id, "a2")
        pantalla.handle_key(UP)
        self.assertEqual(pantalla.selection.audio_track_id, "a1")

    def test_no_se_sale_por_arriba_ni_por_abajo(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        for _ in range(50):
            pantalla.handle_key(UP)
        self.assertEqual(pantalla.selected, 0)
        for _ in range(50):
            pantalla.handle_key(DOWN)
        self.assertEqual(pantalla.selected_choice is not None, True)

    def test_el_cursor_no_salta_de_seccion_al_elegir(self) -> None:
        # Elegir calidad no debe devolver el cursor al audio: el foco se
        # queda donde el usuario está trabajando.
        pantalla, _ = _pantalla(caps_multi())
        pantalla.handle_key(RIGHT)
        pantalla.handle_key(RIGHT)
        self.assertEqual(pantalla.selected_kind, "quality")
        pantalla.handle_key(DOWN)
        self.assertEqual(pantalla.selected_kind, "quality")
        self.assertEqual(pantalla.selection.video_track_id, "v720")

    def test_saltar_entre_secciones(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        pantalla.handle_key(DOWN)
        self.assertEqual(pantalla.selected_kind, "audio")
        pantalla.handle_key(RIGHT)
        self.assertEqual(pantalla.selected_kind, "subtitles")
        pantalla.handle_key(RIGHT)
        self.assertEqual(pantalla.selected_kind, "quality")
        pantalla.handle_key(LEFT)
        self.assertEqual(pantalla.selected_kind, "subtitles")

    def test_el_salto_da_la_vuelta(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        pantalla.handle_key(LEFT)
        self.assertEqual(pantalla.selected_kind, "quality")


class TestAcciones(unittest.TestCase):
    def test_enter_confirma_y_sigue_al_reproductor(self) -> None:
        # Orden pedido: pistas primero, reproductor después. Enter devuelve
        # el control al App, que ya tiene la selección aplicada y todavía no
        # sabe con qué reproductor se va a ver: por eso no viaja ninguno.
        pantalla, _ = _pantalla(caps_multi(), player=None)
        pantalla.handle_key(DOWN)  # Español (el primero)
        pantalla.handle_key(DOWN)  # English
        accion = pantalla.handle_key(ENTER)
        self.assertIsNotNone(accion)
        self.assertEqual(accion["action"], "tracks_choose_player")
        self.assertEqual(accion["selection"].audio_track_id, "a2")
        self.assertIsNone(accion["player_name"])

    def test_enter_conserva_el_reproductor_ya_elegido(self) -> None:
        # Esta pantalla también se abre *después* del reproductor, cuando el
        # sondeo del canal llegó tarde. Si el Enter no lleva el reproductor
        # consigo, el App no tiene más remedio que volver a preguntarlo: el
        # usuario elige dos veces lo mismo.
        pantalla, _ = _pantalla(caps_multi(), player="mpv")
        accion = pantalla.handle_key(ENTER)
        self.assertEqual(accion["action"], "tracks_choose_player")
        self.assertEqual(accion["player_name"], "mpv")

    def test_tecla_cero_vuelve_a_automatico(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        pantalla.handle_key(DOWN)  # Español
        self.assertEqual(pantalla.selection.audio_track_id, "a1")
        pantalla.handle_key(ord("0"))
        self.assertIsNone(pantalla.selection.audio_track_id)

    def test_cero_en_calidad_vuelve_a_abr(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        pantalla.handle_key(RIGHT)
        pantalla.handle_key(RIGHT)
        self.assertEqual(pantalla.selected_kind, "quality")
        pantalla.handle_key(DOWN)
        pantalla.handle_key(ord("0"))
        self.assertTrue(pantalla.selection.auto_quality)
        self.assertIsNone(pantalla.selection.video_track_id)

    def test_m_pide_recordar(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        accion = pantalla.handle_key(ord("m"))
        self.assertEqual(accion["action"], "remember_track_prefs")
        self.assertIs(accion["session"], pantalla.session)

    def test_tecla_desconocida_no_hace_nada(self) -> None:
        pantalla, app = _pantalla(caps_multi())
        self.assertIsNone(pantalla.handle_key(ord("z")))
        self.assertEqual(app.avisos, [])


class TestConUnaSolaOpcionNoHayMenu(unittest.TestCase):
    def test_no_se_pinta_un_menu_de_una_opcion(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[MediaTrack(id="a1", type=AUDIO, language="es",
                                     uri="https://h/a1.m3u8")],
            subtitle_tracks=[MediaTrack(id="s1", type=TEXT, language="es",
                                        uri="https://h/s1.vtt")],
            video_variants=[MediaTrack(id="v720", type=VIDEO, height=720,
                                       uri="https://h/v720.m3u8")],
            protocol="hls",
        )
        pantalla, _ = _pantalla(caps)
        # Sólo los subtítulos tienen "algo" que elegir (encender/apagar).
        self.assertEqual(pantalla.kinds, ["subtitles"])
        self.assertEqual(pantalla.options.audio, [])
        self.assertEqual(pantalla.options.quality, [])
        self.assertTrue(pantalla.options.has_menu)


class TestNowPlayingSinAtajosDePista(unittest.TestCase):
    """Con el canal abierto no se cambia de pista: se eligió antes del reproductor.

    La regla es de una sola dirección y se comprueba por los tres lados: la
    tecla no hace nada, no está en la toolbar y la tarjeta no la anuncia. Si
    volviera una de las tres, la pantalla estaría mintiendo.
    """

    def _screen(self, sesion=None) -> NowPlayingScreen:
        app = mock.Mock()
        app.epg = None
        proc = mock.Mock()
        proc.poll.return_value = None
        # Sin terminal, `colors.pair()` lanza: se sustituye por 0 porque lo
        # que se prueba aquí es la decisión (qué filas y qué etiquetas), no
        # el color.
        parche = mock.patch(
            "thetvview.ui.screens.colors.pair", return_value=0
        )
        parche.start()
        self.addCleanup(parche.stop)
        return NowPlayingScreen(app, Channel(name="C", url="https://h/x.m3u8"),
                                "mpv", proc, track_session=sesion)

    def test_sin_sesion_las_teclas_no_hacen_nada(self) -> None:
        pantalla = self._screen(None)
        self.assertIsNone(pantalla.handle_key(ord("a")))
        self.assertIsNone(pantalla.handle_key(ord("s")))

    def test_las_teclas_de_pista_no_abren_el_selector(self) -> None:
        # Ni con sesión multi-pista: `a`/`s`/`v` no hacen nada aquí.
        pantalla = self._screen(_SesionFalsa(caps_multi()))
        for tecla in ("a", "s", "v"):
            with self.subTest(tecla=tecla):
                self.assertIsNone(pantalla.handle_key(ord(tecla)))

    def test_i_pide_reanalizar(self) -> None:
        pantalla = self._screen(_SesionFalsa(caps_multi()))
        accion = pantalla.handle_key(ord("i"))
        self.assertEqual(accion["action"], "recheck_tracks")

    def test_q_sigue_deteniendo(self) -> None:
        pantalla = self._screen(_SesionFalsa(caps_multi()))
        self.assertEqual(pantalla.handle_key(ord("q"))["action"], "stop_playback")

    def test_shortcuts_solo_con_quedan_i_y_q_y_diagnostico(self) -> None:
        # `d` (diagnóstico) aparece siempre, con pistas o sin ellas: es lo que
        # contesta «¿por qué no abre?» cuando un canal muere al instante, y en
        # ese momento las pistas son lo último que importa (SDD-M §21).
        con = self._screen(_SesionFalsa(caps_multi())).shortcuts()
        self.assertEqual(con, "q Detener · d Diagnóstico · i Info · ? Ayuda")
        sin = self._screen(None).shortcuts()
        self.assertEqual(sin, "q Detener · d Diagnóstico · ? Ayuda")

    def test_d_abre_el_diagnostico(self) -> None:
        accion = self._screen(None).handle_key(ord("d"))
        assert accion is not None
        self.assertEqual(accion["action"], "diagnose_channel")
        # Mayúscula también: es una tecla que se documenta en mayúsculas.
        accion_mayus = self._screen(None).handle_key(ord("D"))
        assert accion_mayus is not None
        self.assertEqual(accion_mayus["action"], "diagnose_channel")

    def test_tarjeta_muestra_las_pistas_sin_tecla(self) -> None:
        pantalla = self._screen(_SesionFalsa(caps_multi()))
        filas = pantalla._track_lines(80)
        texto = "\n".join(f[0] for f in filas)
        self.assertIn("Audio", texto)
        self.assertIn("es", texto)
        self.assertIn("Calidad", texto)
        # Informativa, no accionable: ninguna fila lleva la letra de un atajo.
        self.assertNotIn("a Audio", texto)
        self.assertNotIn("s Subt", texto)
        self.assertNotIn("v Calidad", texto)

    def test_tarjeta_analizando(self) -> None:
        sesion = _SesionFalsa(None, pendientes=True)
        filas = self._screen(sesion)._track_lines(80)
        self.assertIn("Analizando", filas[0][0])

    def test_tarjeta_vacia_si_no_hay_pistas(self) -> None:
        filas = self._screen(_SesionFalsa(None))._track_lines(80)
        self.assertEqual(filas, [])

    def test_stop_tracks_apaga_todo(self) -> None:
        sesion = _SesionFalsa(caps_multi())
        sesion.stop = mock.Mock()
        pantalla = self._screen(sesion)
        proxy = mock.Mock()
        pantalla.pin_proxy = proxy
        pantalla.stop_tracks()
        sesion.stop.assert_called_once()
        proxy.stop.assert_called_once()
        self.assertIsNone(pantalla.pin_proxy)

    def test_stop_tracks_no_lanza_si_falla(self) -> None:
        sesion = _SesionFalsa(caps_multi())
        sesion.stop = mock.Mock(side_effect=RuntimeError)
        self._screen(sesion).stop_tracks()


class _AppRutas(App):
    """App con los métodos de routing reales y sin curses.

    Se hereda de :class:`App` a propósito: `handle_action` llama a
    `self.maybe_open_track_options`, `self.track_selection`, etc., y un
    `Mock` genérico no los tendría. Lo que se sustituye es lo que de verdad
    importa: curses, disco y red.
    """

    def __init__(self) -> None:  # noqa: D107 - deliberadamente no App.__init__
        self.track_session = None
        self.stack = []
        self.status = mock.Mock()
        self.pushed: list = []
        self.notified: list[str] = []

    def push(self, screen) -> None:
        self.pushed.append(screen)

    def notify_warning(self, message: str) -> None:
        self.notified.append(message)

    def notify(self, message: str, *, error: bool = False, title=None) -> None:
        self.notified.append(message)

    def notify_error(self, message: str) -> None:
        self.notified.append(message)

    def _confirm(self, title: str, message: str) -> bool:
        return True


class TestRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _AppRutas()

    def test_play_with_sin_menu_reproduce(self) -> None:
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_single())
        sesion.channel = canal
        self.app.track_session = sesion
        with mock.patch("thetvview.ui.app.play_channel") as play:
            App.handle_action(self.app, {"action": "play_with", "channel": canal,
                                         "player_name": "mpv"})
        self.assertTrue(play.called)
        self.assertEqual(self.app.pushed, [])

    def test_play_with_con_menu_abre_el_selector(self) -> None:
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        self.app.track_session = sesion
        with mock.patch("thetvview.ui.app.play_channel") as play:
            App.handle_action(self.app, {"action": "play_with", "channel": canal,
                                         "player_name": "mpv"})
        self.assertFalse(play.called)
        self.assertEqual(len(self.app.pushed), 1)
        self.assertIsInstance(self.app.pushed[0], TrackOptionsScreen)

    def test_play_with_sin_sesion_reproduce(self) -> None:
        canal = Channel(name="C", url="https://h/x.m3u8")
        self.app.track_session = None
        with mock.patch("thetvview.ui.app.play_channel") as play:
            App.handle_action(self.app, {"action": "play_with", "channel": canal,
                                         "player_name": "mpv"})
        self.assertTrue(play.called)

    def test_sesion_de_otro_canal_no_abre_menu(self) -> None:
        otro = Channel(name="Otro", url="https://h/y.m3u8")
        sesion = _SesionFalsa(caps_multi())
        self.app.track_session = sesion
        with mock.patch("thetvview.ui.app.play_channel") as play:
            App.handle_action(self.app, {"action": "play_with", "channel": otro,
                                         "player_name": "mpv"})
        self.assertTrue(play.called)

    def test_play_with_selection_pasa_la_seleccion(self) -> None:
        canal = Channel(name="C", url="https://h/x.m3u8")
        sel = PlaybackSelection(audio_track_id="a2")
        self.app.track_session = _SesionFalsa(caps_multi())
        with mock.patch("thetvview.ui.app.play_channel") as play:
            App.handle_action(self.app, {"action": "play_with_selection",
                                         "channel": canal, "player_name": "mpv",
                                         "selection": sel})
        self.assertEqual(play.call_args.kwargs["selection"], sel)

    def test_open_track_kind_sin_menu_avisa(self) -> None:
        app = _AppRutas()
        app.track_session = _SesionFalsa(caps_single())
        App.open_track_options(app, Channel(name="C", url="u"), "mplayer",
                               kind="audio")
        self.assertTrue(app.notified)
        self.assertEqual(app.pushed, [])

    def test_open_track_kind_con_menu_abre(self) -> None:
        app = _AppRutas()
        app.track_session = _SesionFalsa(caps_multi())
        App.open_track_options(app, Channel(name="C", url="https://h/x.m3u8"),
                               "mpv", kind="audio")
        self.assertEqual(len(app.pushed), 1)
        self.assertEqual(app.pushed[0].kinds, ["audio"])


class TestDegradacionVisible(unittest.TestCase):
    """Un modal es una interrupción: sólo se gasta cuando hay un fallo.

    "El canal declara una sola pista" **no** es un fallo: el canal se
    reproduce bien. Interrumpir ahí, antes incluso de elegir reproductor,
    hace creer que algo va mal. Eso va a la barra de estado.
    """

    def test_mensaje_de_unica_pista(self) -> None:
        sesion = _SesionFalsa(caps_media_playlist())
        self.assertIsNotNone(sesion.degraded_message())

    def test_master_con_una_sola_variante_no_dice_nada(self) -> None:
        # Se leyó bien y no hay nada que ofrecer: tampoco hay nada que
        # explicar. El canal funciona y el usuario no ha pedido nada.
        sesion = _SesionFalsa(caps_single())
        self.assertIsNone(sesion.degraded_message())

    def test_sin_sondeo_aun_no_dice_nada(self) -> None:
        sesion = _SesionFalsa(None)
        self.assertIsNone(sesion.degraded_message())
        self.assertEqual(sesion.degraded_outcome(), (None, False))

    def test_nada_que_elegir_no_es_fallo(self) -> None:
        mensaje, fallo = _SesionFalsa(caps_media_playlist()).degraded_outcome()
        self.assertIsNotNone(mensaje)
        self.assertFalse(fallo)

    def test_solo_se_explica_una_vez_en_la_barra(self) -> None:
        app = _AppRutas()
        sesion = _SesionFalsa(caps_media_playlist())
        App._explain_degraded(app, sesion)
        App._explain_degraded(app, sesion)
        # Ni un modal: la barra lo dice una vez y no molesta.
        self.assertEqual(app.notified, [])
        self.assertEqual(app.status.show.call_count, 1)

    def test_un_fallo_si_abre_modal(self) -> None:
        app = _AppRutas()
        sesion = _SesionFalsa(caps_media_playlist())
        sesion.degraded_outcome = lambda: (
            "No se pudo consultar el manifiesto del canal.", True
        )
        App._explain_degraded(app, sesion)
        App._explain_degraded(app, sesion)
        self.assertEqual(len(app.notified), 1)

    def test_si_hay_menu_no_se_explica_nada(self) -> None:
        app = _AppRutas()
        sesion = _SesionFalsa(caps_multi())
        App._explain_degraded(app, sesion)
        self.assertEqual(app.notified, [])
        app.status.show.assert_not_called()


class TestDegradedOutcome(unittest.TestCase):
    """La función pura decide qué merece interrupting y qué no."""

    def _resultado(self, capabilities, reason=None):
        return _ResultadoFalso(capabilities, reason)

    def test_canales_sin_opciones_no_molestan(self) -> None:
        mensaje, fallo = degraded_outcome(_ResultadoFalso(caps_media_playlist()))
        self.assertIsNotNone(mensaje)
        self.assertFalse(fallo)

    def test_un_master_de_una_sola_variante_no_interrumpe(self) -> None:
        # Leído bien, nada que ofrecer y nada que explicar: silencio total.
        self.assertEqual(degraded_outcome(_ResultadoFalso(caps_single())),
                         (None, False))

    def test_no_se_pudo_consultar_es_fallo(self) -> None:
        texto, fallo = degraded_outcome(_ResultadoFalso(None, "HTTP 403"))
        self.assertEqual(texto, "HTTP 403")
        self.assertTrue(fallo)

    def test_sin_motivo_usa_un_texto_util(self) -> None:
        texto, fallo = degraded_outcome(_ResultadoFalso(None, None))
        self.assertIn("No se pudo consultar", texto)
        self.assertTrue(fallo)

    def test_sondeo_pendiente_no_dice_nada(self) -> None:
        self.assertEqual(degraded_outcome(None), (None, False))

    def test_con_alternativas_no_dice_nada(self) -> None:
        self.assertEqual(degraded_outcome(_ResultadoFalso(caps_multi())),
                         (None, False))


class TestAyuda(unittest.TestCase):
    def test_la_pantalla_tiene_ayuda(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        textos = [t for _s, t in build_help_lines(pantalla)]
        self.assertIn("Estás en: Audio, subtítulos y calidad", textos)
        claves = [t for s, t in build_help_lines(pantalla) if s == "key"]
        self.assertTrue(any("0  volver a Automático" in t for t in claves))
        self.assertTrue(any("m  recordar" in t for t in claves))

    def test_shortcuts_menciona_recordar(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        self.assertIn("Recordar", pantalla.shortcuts())


class TestOpcionesKinds(unittest.TestCase):
    def test_filtra_a_la_seccion_pedida_si_existe(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        self.assertEqual(options_kinds(pantalla.options, "audio"), ["audio"])

    def test_devuelve_vacio_si_no_existe(self) -> None:
        pantalla, _ = _pantalla(caps_single())
        self.assertEqual(options_kinds(pantalla.options, "audio"), [])

    def test_orden_estable(self) -> None:
        pantalla, _ = _pantalla(caps_multi())
        self.assertEqual(options_kinds(pantalla.options),
                         ["audio", "subtitles", "quality"])


class TestDegradadoEnLaTarjeta(unittest.TestCase):
    def test_el_motivo_propagado(self) -> None:
        caps = caps_single()
        caps.degraded_reason = DEGRADED_NOT_EXPOSED
        sesion = _SesionFalsa(caps)
        self.assertEqual(sesion.manager.capabilities.degraded_reason,
                         DEGRADED_NOT_EXPOSED)
        self.assertEqual(pantalla_opciones(sesion), [])
        self.assertIsNotNone(sesion.degraded_message())


def pantalla_opciones(sesion) -> list:
    return [c for c in sesion.options("mpv").audio + sesion.options("mpv").quality]


class TestSeleccionPorDefecto(unittest.TestCase):
    def test_sin_tocar_nada_la_seleccion_esta_vacia(self) -> None:
        sesion = _SesionFalsa(caps_multi())
        self.assertTrue(sesion.selection.is_default)

class TestReanalisisNoBloquea(unittest.TestCase):
    """`i` vuelve a analizar el canal: nunca puede parar la TUI."""

    def _sesion_real(self, url: str, app=None) -> Any:
        from thetvview.ui.tracks import TrackSession

        return TrackSession(app or _AppRutas(), Channel(name="C", url=url))

    def test_recheck_devuelve_seguida_aunque_el_servidor_tarde(self) -> None:
        # Servidor que tarda 1,5 s: la llamada tiene que volver antes.
        import http.server
        import threading
        import time as _time

        class Lento(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                return

            def do_HEAD(self):
                _time.sleep(1.5)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_HEAD

        servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Lento)
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
        self.addCleanup(servidor.server_close)
        self.addCleanup(servidor.shutdown)
        base = f"http://127.0.0.1:{servidor.server_address[1]}"
        app = _AppRutas()
        app._tracks_no_disk_cache = True

        sesion = self._sesion_real(base + "/lento.m3u8", app)
        inicio = _time.monotonic()
        sesion.recheck()
        elapsed = _time.monotonic() - inicio
        self.assertLess(elapsed, 1.0, "recheck bloqueó el hilo de la interfaz")
        sesion.stop()

    def test_los_avisos_se_sacan_una_sola_vez(self) -> None:
        from thetvview.ui.tracks import TrackSession

        sesion = TrackSession(_AppRutas(), Channel(name="C", url="https://h/x.m3u8"))
        sesion._avisos.extend(["uno", "dos"])
        self.assertEqual(sesion.take_avisos(), ["uno", "dos"])
        self.assertEqual(sesion.take_avisos(), [])

    def test_el_bucle_drena_los_avisos(self) -> None:
        app = _AppRutas()
        sesion = _SesionFalsa(caps_multi())
        sesion._avisos.append("El audio ya no está en este stream.")
        app.track_session = sesion
        App._drain_track_avisos(app)
        self.assertEqual(len(app.notified), 1)
        self.assertIn("ya no está", app.notified[0])
        App._drain_track_avisos(app)
        self.assertEqual(len(app.notified), 1)

    def test_sin_sesion_no_falla(self) -> None:
        app = _AppRutas()
        App._drain_track_avisos(app)
        self.assertEqual(app.notified, [])

    def test_recheck_sin_sesion_avisa(self) -> None:
        app = _AppRutas()
        app.track_session = None
        App.recheck_tracks(app, Channel(name="C", url="https://h/x.m3u8"))
        self.assertTrue(app.notified)


class _SesionTarde(_SesionFalsa):
    """Sondeo que responde **después** de la primera espera.

    Reproduce lo que pasaba en Windows con un proveedor lento: al elegir el
    canal, la espera inicial no da tiempo a que llegue el manifiesto, se
    ofrece el selector de reproductor, y el resultado aparece un momento
    después. Hasta que responde, la sesión no tiene nada que ofrecer, igual
    que la real.
    """

    def __init__(self, caps) -> None:
        super().__init__(None)  # manager vacío: el manifiesto aún no ha vuelto
        self._caps = caps
        self.answered = False
        self._pending = True

    @property
    def pending(self) -> bool:
        return self._pending

    @pending.setter
    def pending(self, value: bool) -> None:
        self._pending = bool(value)

    def responder_en(self, segundos: float) -> None:
        """Contesta pasado un rato, como el hilo del sondeo real."""
        import threading
        import time as _time

        def _contestar() -> None:
            _time.sleep(segundos)
            self.manager = TrackManager(self._caps, None)
            self._pending = False
            self.answered = True

        threading.Thread(target=_contestar, daemon=True).start()

    @property
    def wait_settles(self) -> bool:
        # El caso que hay que cubrir: en cuanto **responde**, la espera
        # pendiente es cero pero la propiedad debe seguir siendo True, porque
        # es lo que le concede al flujo el margen de cortesía.
        from thetvview.ui.tracks import _url_may_be_manifest

        return self.answered or _url_may_be_manifest(self.channel.url)


class TestOrdenPistasAntesDelReproductor(unittest.TestCase):
    """El orden que se pidió: pistas primero, reproductor después.

    Antes el selector de pistas salía **después** de elegir reproductor, y
    como el sondeo soltaba volando en segundo plano a menudo ya no quedaba
    nada que preguntar. Ahora el orden es el natural y la espera está acotada.
    """

    def _app(self, sesion) -> Any:
        app = _AppRutas()
        app.track_session = sesion
        app.player_screen = 0
        return app

    def test_con_pistas_abre_pistas_y_no_reproductor(self) -> None:
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        app = self._app(sesion)
        App._track_options_then_player(app, canal, wait=0.0)
        self.assertEqual(len(app.pushed), 1)
        self.assertIsInstance(app.pushed[0], TrackOptionsScreen)

    def test_sin_pistas_abre_reproductor_directo(self) -> None:
        canal = Channel(name="C", url="https://h/x.ts")
        sesion = _SesionFalsa(caps_single())
        sesion.channel = canal
        app = self._app(sesion)
        App._track_options_then_player(app, canal, wait=0.0)
        self.assertEqual(len(app.pushed), 1)
        self.assertNotIsInstance(app.pushed[0], TrackOptionsScreen)

    def test_sin_reproductor_instalado_no_empuja_nada(self) -> None:
        # Sin reproductor no tiene sentido enseñar pistas: el aviso sale
        # igual que siempre, sin menús de por medio.
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        app = self._app(sesion)
        with mock.patch("thetvview.ui.app.PlayerScreen") as pantalla:
            pantalla.return_value = mock.Mock(players=[])
            App._track_options_then_player(app, canal, wait=0.0)
        self.assertEqual(app.pushed, [])
        self.assertTrue(app.status.show.called)

    def test_enter_en_pistas_marca_elegido_y_sigue(self) -> None:
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        app = self._app(sesion)
        App._track_options_then_player(app, canal, wait=0.0)
        pantalla = app.pushed[0]
        pantalla.handle_key(DOWN)
        pantalla.handle_key(ENTER)
        App.handle_action(app, pantalla and app.pushed[0] and {
            "action": "tracks_choose_player", "channel": canal,
            "selection": pantalla.selection,
        })
        self.assertTrue(sesion.chosen)
        # La última pantalla empujada es la de reproductor, no las pistas.
        self.assertIsInstance(app.pushed[-1], __import__(
            "thetvview.ui.screens", fromlist=["PlayerScreen"]
        ).PlayerScreen)

    def test_sondeo_tardio_no_pregunta_dos_veces_el_reproductor(self) -> None:
        # El bug que se vio en Windows: el manifiesto llegaba después de la
        # primera espera, así que el orden se invertía (reproductor → pistas)
        # y al confirmar las pistas se volvía a pedir el reproductor. Con el
        # reproductor ya elegido, confirmar reproduce directamente.
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        app = self._app(sesion)
        with mock.patch("thetvview.ui.app.play_channel") as play:
            # 1) El usuario elige reproductor y el sondeo aún no había
            #    terminado: se le ofrecen las pistas antes de lanzar.
            App.handle_action(app, {"action": "play_with", "channel": canal,
                                    "player_name": "mpv"})
            self.assertFalse(play.called)
            pistas = app.pushed[-1]
            self.assertIsInstance(pistas, TrackOptionsScreen)
            # 2) Confirma las pistas: se reproduce con el reproductor que ya
            #    eligió, sin preguntar otra vez.
            accion = pistas.handle_key(ENTER)
            self.assertEqual(accion["player_name"], "mpv")
            App.handle_action(app, accion)
        self.assertTrue(play.called, "no se reprodujo tras elegir las pistas")
        self.assertEqual(play.call_args.kwargs["player_name"], "mpv")
        self.assertEqual(len(app.pushed), 1, "se preguntó el reproductor dos veces")

    def test_mientras_se_ve_no_se_relanza_una_segunda_vez(self) -> None:
        # El mismo selector de "Reproduciendo" (por ejemplo, si el sondeo llegó
        # tarde y se abre con `p`) llega con un reproductor ya elegido, pero ahí
        # el canal **ya está abierto**: confirmar no puede lanzar un segundo
        # reproductor.
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        app = self._app(sesion)
        app.stack = [mock.Mock(spec=NowPlayingScreen)]
        with mock.patch("thetvview.ui.app.play_channel") as play:
            App.handle_action(app, {"action": "tracks_choose_player",
                                    "channel": canal, "player_name": "mpv"})
        self.assertFalse(play.called, "relanzó el canal que ya se está viendo")
        self.assertEqual(len(app.pushed), 1)

    def test_sondeo_en_marcha_espera_un_margen(self) -> None:
        # Si el sondeo sigue vivo al confirmar el reproductor, se le concede
        # un margen corto y acotado antes de renunciar a las pistas: es lo que
        # mantiene el orden en un equipo lento.
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        sesion.pending = True
        sesion.wait_settles = True
        app = self._app(sesion)
        with mock.patch.object(App, "_await_tracks") as espera, \
                mock.patch("thetvview.ui.app.play_channel"):
            App.handle_action(app, {"action": "play_with", "channel": canal,
                                    "player_name": "mpv"})
        self.assertTrue(espera.called, "no esperó al sondeo que seguía en marcha")
        from thetvview.ui import app as ui_app

        self.assertEqual(espera.call_args[0][1], ui_app.TRACK_LATE_WAIT_SECONDS)

    def test_si_ya_respondio_no_se_espera_nada(self) -> None:
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        sesion.pending = False
        app = self._app(sesion)
        with mock.patch.object(App, "_await_tracks") as espera, \
                mock.patch("thetvview.ui.app.play_channel"):
            App.handle_action(app, {"action": "play_with", "channel": canal,
                                    "player_name": "mpv"})
        self.assertFalse(espera.called)

    def test_el_margen_esta_acotado(self) -> None:
        # Ni la primera espera ni el margen de cortesía pueden convertirse en
        # una espera sin fin: los dos son valores cortos y fijos.
        from thetvview.ui import app as ui_app

        self.assertGreater(ui_app.TRACK_LATE_WAIT_SECONDS, 0)
        self.assertLessEqual(ui_app.TRACK_LATE_WAIT_SECONDS, 4.0)

    def test_responde_tarde_aun_así_se_pregunta_por_las_pistas(self) -> None:
        # El caso límite que rompía el orden: el sondeo termina **justo
        # después** de la primera espera. `wait_settles` debe seguir siendo
        # True (ya respondió) para que el margen de cortesía se conceda y las
        # pistas se ofrezcan antes de lanzar, no después.
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionTarde(caps_multi())
        sesion.channel = canal
        sesion.responder_en(0.1)
        app = self._app(sesion)
        with mock.patch("thetvview.ui.app.play_channel") as play:
            App.handle_action(app, {"action": "play_with", "channel": canal,
                                    "player_name": "mpv"})
        self.assertFalse(play.called)
        self.assertIsInstance(app.pushed[-1], TrackOptionsScreen)

    def test_la_espera_inicial_tampoco_se_salta_si_ya_respondio(self) -> None:
        # La otra mitad de lo mismo: si el sondeo ya terminó cuando el canal
        # se abre, `wait_settles` no puede depender de que la URL "parezca" un
        # manifiesto. Con una dirección opaca (una referencia Xtream, por
        # ejemplo) la espera se saltaría y las pistas llegarían tarde otra vez.
        from thetvview.streams.probe import ProbeResult
        from thetvview.ui.tracks import TrackSession

        sesion = TrackSession(
            _AppRutas(), Channel(name="C", url="xtream-ts://panel/live/u/p/42")
        )
        sesion.result = ProbeResult(capabilities=caps_multi())
        self.assertTrue(sesion.wait_settles)

    def test_con_elegido_ya_no_se_pregunta_al_lanzar(self) -> None:
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        sesion.chosen = True
        app = self._app(sesion)
        with mock.patch("thetvview.ui.app.play_channel") as play:
            App.handle_action(app, {"action": "play_with", "channel": canal,
                                    "player_name": "mpv"})
        self.assertTrue(play.called)
        self.assertEqual(len(app.pushed), 0)

    def test_sin_elegir_todavia_se_ofrece_al_lanzar(self) -> None:
        # Sondeo que llegó tarde: la única oportunidad es al elegir
        # reproductor, y ahí se ofrece antes de abrir el canal.
        canal = Channel(name="C", url="https://h/x.m3u8")
        sesion = _SesionFalsa(caps_multi())
        sesion.channel = canal
        app = self._app(sesion)
        with mock.patch("thetvview.ui.app.play_channel") as play:
            App.handle_action(app, {"action": "play_with", "channel": canal,
                                    "player_name": "mpv"})
        self.assertFalse(play.called)
        self.assertIsInstance(app.pushed[-1], TrackOptionsScreen)


class TestEsperaAcotada(unittest.TestCase):
    """Esperar debe ser corto, sólo cuando tiene sentido, y con aviso."""

    def test_un_ts_no_espera(self) -> None:
        from thetvview.ui.tracks import _url_may_be_manifest

        self.assertFalse(_url_may_be_manifest("http://h/live/123.ts"))
        self.assertFalse(_url_may_be_manifest("http://h/live/u/p/123.ts"))
        self.assertFalse(_url_may_be_manifest("rtmp://h/live"))
        self.assertFalse(_url_may_be_manifest(""))

    def test_un_manifesto_sí_espera(self) -> None:
        from thetvview.ui.tracks import _url_may_be_manifest

        for url in (
            "http://h/live/canal.m3u8",
            "http://h/live/canal.m3u8?token=x",
            "http://h/manifest.mpd",
            "http://h/player_api.php?action=get_live_streams&type=m3u8",
            "http://h/live/123.ts?type=m3u8",
        ):
            with self.subTest(url=url):
                self.assertTrue(_url_may_be_manifest(url))

    def test_el_tope_es_corto(self) -> None:
        from thetvview.ui import app as ui_app

        self.assertGreater(ui_app.TRACK_WAIT_SECONDS, 0)
        self.assertLessEqual(ui_app.TRACK_WAIT_SECONDS, 4.0)

    def test_no_espera_si_ya_esta_respondido(self) -> None:
        app = _AppRutas()
        sesion = _SesionFalsa(caps_multi())  # pending=False
        with mock.patch.object(App, "show_loading") as espera:
            App._await_tracks(app, sesion, wait=5.0)
        self.assertFalse(espera.called)

    def test_espera_lo_prometido_y_sigue_vivo(self) -> None:
        import threading
        import time as _time

        app = _AppRutas()
        app.stdscr = mock.Mock()
        sesion = _SesionFalsa(caps_multi())
        sesion.wait_settles = True
        sesion.pending = True

        # Termina a los ~0,3 s: la espera debe cortarse en cuanto contesta.
        def _responde():
            _time.sleep(0.3)
            sesion.pending = False

        threading.Thread(target=_responde, daemon=True).start()
        inicio = _time.monotonic()
        with mock.patch.object(App, "show_loading") as espera:
            App._await_tracks(app, sesion, wait=5.0)
        elapsed = _time.monotonic() - inicio
        # Corta en cuanto contesta (0,3 s), sin agotar los 5 s.
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 4.0, "no respetó el fin del sondeo")
        self.assertTrue(espera.called, "no avisó de que estaba esperando")
        self.assertIn("Analizando", espera.call_args[0][0])

    def test_no_pasa_del_tope(self) -> None:
        import time as _time

        app = _AppRutas()
        app.stdscr = mock.Mock()
        sesion = _SesionFalsa(caps_multi())
        sesion.wait_settles = True
        sesion.pending = True  # nunca responde
        inicio = _time.monotonic()
        App._await_tracks(app, sesion, wait=0.3)
        self.assertLess(_time.monotonic() - inicio, 2.0)

    def test_sin_espera_no_pinta_nada(self) -> None:
        app = _AppRutas()
        sesion = _SesionFalsa(caps_multi())
        sesion.wait_settles = True
        sesion.pending = True
        with mock.patch.object(App, "show_loading") as espera:
            App._await_tracks(app, sesion, wait=0.0)
        self.assertFalse(espera.called)


class _Ventana:
    """Doble de stdscr: guarda lo que se pintaría, sin abrir curses.

    ``colors.pair`` llama a ``curses.color_pair``, que exige ``initscr()`` y
    lanza ``curses.error`` —que las pantallas se tragan con un ``pass``, y sin
    registro de llamadas. Por eso se sustituye por un ``pair`` neutro: lo que
    se comprueba aquí es **el texto**, no el color.
    """

    def __init__(self, h: int = 40, w: int = 100) -> None:
        self.h, self.w = h, w
        self.calls: list[tuple[int, int, str, int]] = []

    def getmaxyx(self):
        return self.h, self.w

    def addstr(self, y, x, text, attr=0):
        self.calls.append((y, x, text, attr))

    def erase(self):
        self.calls.clear()

    def refresh(self):
        pass


def _pintar(pantalla) -> list[str]:
    """Pinta la pantalla en una ventana falsa y devuelve los textos, en orden.

    Sustituye ``colors.pair`` por un valor neutro: llama a
    ``curses.color_pair``, que exige ``initscr()`` y lanza ``curses.error`` —
    error que las pantallas se tragan con un ``pass``, dejando la ventana en
    blanco. Aquí se mira el texto, no el color.
    """
    ventana = _Ventana()
    with mock.patch.object(colors, "pair", return_value=0):
        pantalla.render(ventana)
    return [texto for _y, _x, texto, _attr in ventana.calls]


class TestNotaDeSubtitulos(unittest.TestCase):
    """Lo de los subtítulos se dice **junto a la lista**, no en un modal.

    Un modal posterior interrumpía con la decisión ya tomada y el reproductor
    sin elegir, que es justo cuando el aviso menos puede cambiar algo.
    """

    def test_sin_reproductor_elegido_nombra_los_dos_casos(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs(), player=None)
        nota = pantalla._nota_subtitulos()
        self.assertIsNotNone(nota)
        self.assertIn("VLC", nota)
        self.assertIn("MPV", nota)

    def test_con_mpv_avisa_que_no_se_aplican(self) -> None:
        for reproductor in ("mpv", "mplayer"):
            with self.subTest(reproductor=reproductor):
                pantalla, _ = _pantalla(caps_con_subs(), player=reproductor)
                nota = pantalla._nota_subtitulos()
                self.assertIsNotNone(nota)
                self.assertIn("no aplica", nota.lower())
                self.assertIn("VLC", nota)

    def test_con_vlc_no_dice_nada(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs(), player="vlc")
        self.assertIsNone(pantalla._nota_subtitulos())

    def test_incrustados_en_el_segmento_no_avisa(self) -> None:
        # Sin URI son una pista normal: `--sid` los alcanza en todos.
        pantalla, _ = _pantalla(caps_con_subs(uri=None), player="mpv")
        self.assertIsNone(pantalla._nota_subtitulos())

    def test_sin_subtitulos_no_avisa(self) -> None:
        pantalla, _ = _pantalla(caps_single(), player="mpv")
        self.assertIsNone(pantalla._nota_subtitulos())

    def test_incrustados_con_otros_idiomas_tampoco(self) -> None:
        # El criterio es "trae URI o no", no el idioma: `caps_multi` trae dos
        # subtítulos con URI y sí es un caso de rendition.
        pantalla, _ = _pantalla(caps_multi(), player="mpv")
        self.assertIsNotNone(pantalla._nota_subtitulos())

    def test_la_nota_no_depende_del_idioma(self) -> None:
        caps = caps_con_subs()
        pantalla, _ = _pantalla(caps, player="mpv")
        self.assertIsNotNone(pantalla._nota_subtitulos())


class TestMarcarConEspacio(unittest.TestCase):
    """Poder elegir varias cosas y revisarlas antes de `Enter`.

    El cursor ya aplica al moverse, pero eso deja sin distinguir "lo elegí yo"
    de "lo que ya venía". `Espacio` marca la opción con un asterisco, así que
    se pueden elegir subtítulos Y una calidad y ver las dos antes de confirmar.
    """

    def test_espacio_marca_la_opcion_del_cursor(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs())
        pantalla.handle_key(ord(" "))
        self.assertEqual(len(pantalla.marcados), 1)
        kind, id_ = next(iter(pantalla.marcados))
        self.assertEqual(id_, pantalla.selected_choice.id)

    def test_se_pueden_marcar_subtitulos_y_calidad(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs())
        # Audio ->向下 hasta subtítulos, marcar; luego calidad, marcar.
        pantalla._focus_first("subtitles")
        pantalla.handle_key(ord(" "))
        self.assertEqual({k for k, _ in pantalla.marcados}, {"subtitles"})
        pantalla._focus_first("quality")
        pantalla.handle_key(ord(" "))
        self.assertEqual({k for k, _ in pantalla.marcados},
                         {"subtitles", "quality"})

    def test_marcar_otra_traslada_el_asterisco(self) -> None:
        """Es un radio: marcar otra deja a la primera sin asterisk.

        Es lo que pedía el ejemplo: 360p marcada, luego 720p con Espacio ->
        360p se desmarca y 720p queda. No pueden convivir dos en una lista.
        """
        pantalla, _ = _pantalla(caps_con_subs())
        pantalla._focus_first("quality")
        pantalla.handle_key(curses.KEY_DOWN)      # 360p
        pantalla.handle_key(ord(" "))
        self.assertEqual(pantalla.marcas_de("quality"), ["v360"])
        pantalla.handle_key(curses.KEY_DOWN)      # 720p
        pantalla.handle_key(ord(" "))
        self.assertEqual(pantalla.marcas_de("quality"), ["v720"])
        # Ni una más de las que había antes.
        self.assertEqual(len(pantalla.marcas_de("quality")), 1)
        # Y el dibujo muestra un único asterisco en toda la sección.
        marcados = [t for t in _pintar(pantalla) if t.rstrip().endswith("*")]
        self.assertEqual(len([m for m in marcados if "360p" in m]), 0, marcados)
        self.assertEqual(len([m for m in marcados if "720p" in m]), 1, marcados)

    def test_marcar_dos_veces_la_misma_no_crea_duplicados(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs())
        pantalla._focus_first("quality")
        pantalla.handle_key(ord(" "))
        primera = set(pantalla.marcados)
        pantalla.handle_key(ord(" "))
        self.assertEqual(pantalla.marcados, primera)

    def test_cada_lista_mantiene_su_propia_marca(self) -> None:
        # Una por lista: elegir subtítulo y calidad deja una marca en cada una,
        # sin que la segunda pise a la primera.
        pantalla, _ = _pantalla(caps_con_subs())
        pantalla._focus_first("subtitles")
        pantalla.handle_key(curses.KEY_DOWN)
        pantalla.handle_key(ord(" "))
        pantalla._focus_first("quality")
        pantalla.handle_key(ord(" "))
        self.assertEqual(len(pantalla.marcas_de("subtitles")), 1)
        self.assertEqual(len(pantalla.marcas_de("quality")), 1)

    def test_el_subtitulo_no_desaparece_al_marcar_calidad(self) -> None:
        """Regla exacta: no se eligen varias opciones **por categoría**.

        Marcar el subtítulo y luego la calidad deja las dos marcas; lo único
        que no puede pasar es que una categoría tenga dos marcados. Por eso
        cambiar de calidad mueve la marca de calidad y deja la del subtítulo
        intacta.
        """
        pantalla, _ = _pantalla(caps_con_subs())
        pantalla._focus_first("subtitles")
        pantalla.handle_key(curses.KEY_DOWN)      # Español
        pantalla.handle_key(ord(" "))
        self.assertEqual(pantalla.marcas_de("subtitles"), ["s1"])

        pantalla._focus_first("quality")
        pantalla.handle_key(curses.KEY_DOWN)      # 360p
        pantalla.handle_key(ord(" "))
        # El subtítulo sigue ahí.
        self.assertEqual(pantalla.marcas_de("subtitles"), ["s1"])
        self.assertEqual(pantalla.marcas_de("quality"), ["v360"])

        pantalla.handle_key(curses.KEY_DOWN)      # 720p
        pantalla.handle_key(ord(" "))
        # Sólo se mueve la de calidad.
        self.assertEqual(pantalla.marcas_de("subtitles"), ["s1"])
        self.assertEqual(pantalla.marcas_de("quality"), ["v720"])

    def test_las_tres_categorias_pueden_estar_marcadas(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs())
        for kind in ("audio", "subtitles", "quality"):
            pantalla._focus_first(kind)
            pantalla.handle_key(ord(" "))
        self.assertEqual(
            sorted(pantalla.marcados),
            [("audio", "auto"), ("quality", "auto"), ("subtitles", "auto")],
        )

    def test_una_categoria_nunca_tiene_dos_marcas(self) -> None:
        """Invariante general, sea cual sea el camino que se llegue."""
        pantalla, _ = _pantalla(caps_con_subs())
        for kind in ("audio", "subtitles", "quality"):
            pantalla._focus_first(kind)
            for _ in range(len(pantalla.options.choices_for(kind))):
                pantalla.handle_key(ord(" "))
                for otra in ("audio", "subtitles", "quality"):
                    with self.subTest(categoria=otra):
                        self.assertLessEqual(len(pantalla.marcas_de(otra)), 1)

    def test_cero_quita_la_marca_de_la_seccion(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs())
        pantalla._focus_first("subtitles")
        pantalla.handle_key(ord(" "))
        pantalla._focus_first("quality")
        pantalla.handle_key(ord(" "))
        pantalla._focus_first("subtitles")
        pantalla.handle_key(ord("0"))
        self.assertEqual({k for k, _ in pantalla.marcados}, {"quality"})

    def test_enter_confirma_pase_lo_que_pase(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs())
        pantalla._focus_first("quality")
        pantalla.handle_key(ord(" "))
        accion = pantalla.handle_key(10)
        self.assertIsNotNone(accion)
        self.assertEqual(accion["action"], "tracks_choose_player")

    def test_el_asterisco_se_dibuja_en_la_opcion_marcada(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs())
        pantalla._focus_first("subtitles")
        pantalla.handle_key(ord(" "))
        marcados = [t for t in _pintar(pantalla) if t.rstrip().endswith("*")]
        self.assertTrue(marcados, "ninguna opción quedó marcada con *")

    def test_sin_marcar_no_hay_asteriscos(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs())
        self.assertFalse([t for t in _pintar(pantalla)
                          if t.rstrip().endswith("*")])

    def test_la_nota_se_dibuja_junto_a_los_subtitulos(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs(), player="mpv")
        textos = _pintar(pantalla)
        notas = [t for t in textos if "▸" in t]
        self.assertTrue(notas, "no se dibujó la nota de subtítulos")
        self.assertIn("VLC", notas[0])
        # Y va después de la última opción de subtítulos, no al final del todo.
        indice_ultima_sub = max(
            i for i, t in enumerate(textos)
            if t.startswith("Español") or t.startswith("English")
            or t == "Desactivados"
        )
        indice_nota = textos.index(notas[0])
        self.assertGreater(indice_nota, indice_ultima_sub)

    def test_las_teclas_anuncian_el_espacio(self) -> None:
        pantalla, _ = _pantalla(caps_con_subs())
        self.assertIn("Espacio", pantalla.shortcuts())
        self.assertIn("*", pantalla.shortcuts())
