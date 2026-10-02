"""Tests de la UI de catch-up (SDD Catch-up §11, §12, §13, §22).

Comprueba los **cuatro caminos** que el plan exige recorrer a mano, sin
curses: se llama a `handle_key`/`handle_action` directamente y se mira qué
acción sale, qué modal se ensena y qué se lanza.

Los cuatro caminos (§22):

1. proveedor **sin** catch-up declarado → el programa pasado se ve pero no
   se reproduce, y el motivo sale en un modal;
2. proveedor **con** catch-up y programa dentro de la ventana → `▶` y
   `play_catchup`, que acaba delegando en `play_channel`;
3. proveedor **con** catch-up pero programa fuera de la ventana → modal
   con el motivo (no el error genérico de "no soportado");
4. la falla de reproducción se distingue de "el proveedor no lo soporta".

Además: la URL que sale hacia el reproductor es la referencia opaca
(`xtream-ts://`), nunca una URL con credenciales.
"""

from __future__ import annotations

import curses
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from thetvview import catchup
from thetvview.epg_parser import Epg
from thetvview.models import Channel, Program
from thetvview.stream_ref import TS_PREFIX, StreamRef
from thetvview.ui import app as ui_app
from thetvview.ui import icons
from thetvview.ui import screens as ui_screens

NOW = datetime.now().astimezone()


def xtream_channel(archive: int = 1, duration: int = 7) -> Channel:
    return Channel(
        name="ESPN",
        url=StreamRef("Panel", "live", "101").to_opaque(),
        tvg_id="espanol.espn",
        tvg_name="ESPN",
        attrs={
            "xtream_id": "101",
            "source_name": "Panel",
            "content_type": "live",
            "tv_archive": str(archive),
            "tv_archive_duration": str(duration),
        },
    )


def ref_opaca(inicio: datetime | None = None, segundos: int = 3600) -> str:
    """Referencia opaca de archivo bien formada (start= es obligatorio)."""
    return catchup.CatchupRef(
        "Panel", "101", int((inicio or (NOW - timedelta(hours=2))).timestamp()), segundos,
    ).to_opaque()


def prog_ago(*, hours: float = 2, minutes: float = 90) -> Program:
    start = NOW - timedelta(hours=hours)
    return Program("espanol.espn", "Película", start, start + timedelta(minutes=minutes))


def prog_current() -> Program:
    return Program("espanol.espn", "Ahora", NOW - timedelta(minutes=10),
                   NOW + timedelta(minutes=50))


def prog_ahead() -> Program:
    return Program("espanol.espn", "Después", NOW + timedelta(minutes=30),
                   NOW + timedelta(minutes=120))


class _Status:
    def __init__(self) -> None:
        self.last = ""
        self.error = False

    def show(self, message: str, error: bool = False) -> None:
        self.last = message
        self.error = error


class _FavoritosStub:
    def favorite_urls(self) -> set[str]:
        return set()

    def is_favorite(self, channel) -> bool:  # noqa: ANN001
        return False


class _Catalogo:
    def __init__(self, creds: dict | None = None) -> None:
        self.creds = creds or {}

    def get_credentials(self, name: str):
        return self.creds.get(name)


class _AppStub:
    """App mínima para las pantallas: ni curses ni red."""

    def __init__(self, programs: list[Program] | None = None) -> None:
        self.programs = programs or []
        self.status = _Status()
        self.epg = Epg(programs={}, channels_by_id={})
        self.avisos: list[str] = []
        self.stdscr = mock.Mock()
        self.favorites = _FavoritosStub()
        self.playlists = _Catalogo({"Panel": ("http://srv:8080", "u", "secreto")})
        self.prefs = mock.Mock()

    def ensure_epg(self, channel, url_hint=None, force_refresh=False):  # noqa: ANN001
        return self.programs

    def body_height(self) -> int:
        return 20

    def notify_warning(self, message: str) -> None:
        self.avisos.append(message)
        self.status.show(message)

    def notify_error(self, message: str) -> None:
        self.avisos.append(message)
        self.status.show(message, error=True)


def _epg_screen(channel: Channel, programs: list[Program]) -> tuple[_AppStub, object]:
    app = _AppStub(programs)
    return app, ui_screens.EpgScreen(app, channel)


def _rows_markers(screen) -> dict[str, str]:  # noqa: ANN001
    """Título del programa -> marcador con el que lo pinta la pantalla."""
    filas = screen._rows()
    salida: dict[str, str] = {}
    for prog in screen.programs:
        for fila in filas:
            if prog.title in fila:
                salida[prog.title] = fila.strip()[0]
                break
    return salida


class TestMarcadoresEpg(unittest.TestCase):
    """§11: la guía dice visualmente si un programa se puede recuperar."""

    def setUp(self) -> None:
        self.pasado = prog_ago(hours=2)
        self.actual = prog_current()
        self.futuro = prog_ahead()
        self.programas = [self.pasado, self.actual, self.futuro]

    def test_con_archivo_declarado(self) -> None:
        _, screen = _epg_screen(xtream_channel(1, 7), self.programas)
        marcadores = _rows_markers(screen)
        self.assertEqual(marcadores["Película"], icons.ICON_PLAY)  # ▶
        self.assertEqual(marcadores["Ahora"], icons.ICON_LIVE)  # ●
        self.assertEqual(marcadores["Después"], icons.ICON_LIVE_OFF)  # ○

    def test_sin_archivo_declarado_todo_pasado_es_informativo(self) -> None:
        _, screen = _epg_screen(xtream_channel(0, 7), self.programas)
        marcadores = _rows_markers(screen)
        self.assertEqual(marcadores["Película"], icons.ICON_LIVE_OFF)  # ○
        self.assertEqual(marcadores["Ahora"], icons.ICON_LIVE)  # ●

    def test_programa_fuera_de_ventana_es_informativo_aunque_haya_archivo(self) -> None:
        viejo = prog_ago(hours=24 * 30, minutes=30)
        _, screen = _epg_screen(xtream_channel(1, 7), [viejo])
        self.assertEqual(_rows_markers(screen)["Película"], icons.ICON_LIVE_OFF)

    def test_la_comprobacion_no_depende_de_leer_los_attrs(self) -> None:
        """El marcador sale de `catchup.classify`, la invariante del dominio."""
        with mock.patch.object(ui_screens.catchup, "classify",
                               wraps=catchup.classify) as clasificar:
            _, screen = _epg_screen(xtream_channel(1, 7), self.programas)
            screen._rows()
        self.assertTrue(clasificar.called)

    def test_shortcuts_anuncia_el_modo_solo_si_hay_archivo(self) -> None:
        _, con = _epg_screen(xtream_channel(1, 7), self.programas)
        _, sin = _epg_screen(xtream_channel(0, 7), self.programas)
        self.assertIn("Archivo", con.shortcuts())
        self.assertNotIn("Archivo", sin.shortcuts())

    def test_sin_parrilla_sigue_ofreciendo_el_directo(self) -> None:
        _, screen = _epg_screen(xtream_channel(1, 7), [])
        self.assertEqual(screen._rows(), ["(sin datos de EPG para este canal)"])
        accion = screen.handle_key(curses.KEY_ENTER)
        self.assertEqual(accion["action"], "open_channel")


class TestEnterEnLaGuia(unittest.TestCase):
    """Los cuatro caminos de §22, medidos en lo que devuelve `handle_key`."""

    def test_1_sin_catchup_modal_y_sin_accion_de_archivo(self) -> None:
        app, screen = _epg_screen(xtream_channel(0, 7), [prog_ago(hours=2)])
        self.assertIsNone(screen.handle_key(curses.KEY_ENTER))
        self.assertEqual(len(app.avisos), 1)
        self.assertIn("no ofrece archivo", app.avisos[0])
        self.assertIn("directo", app.avisos[0])

    def test_2_con_catchup_devuelve_play_catchup(self) -> None:
        prog = prog_ago(hours=2)
        app, screen = _epg_screen(xtream_channel(1, 7), [prog])
        accion = screen.handle_key(curses.KEY_ENTER)
        self.assertEqual(accion["action"], "play_catchup")
        self.assertIs(accion["program"], prog)
        self.assertEqual(app.avisos, [])

    def test_3_fuera_de_la_ventana_modal_con_el_motivo(self) -> None:
        app, screen = _epg_screen(xtream_channel(1, 7),
                                  [prog_ago(hours=24 * 30, minutes=30)])
        self.assertIsNone(screen.handle_key(curses.KEY_ENTER))
        self.assertEqual(len(app.avisos), 1)
        self.assertIn("ya no está disponible", app.avisos[0])
        self.assertIn("7", app.avisos[0])  # dice cuántos días guarda

    def test_4_el_mensaje_fuera_de_ventana_no_es_el_de_no_soportado(self) -> None:
        app_sin, sin = _epg_screen(xtream_channel(0, 7), [prog_ago(hours=2)])
        app_fuera, fuera = _epg_screen(xtream_channel(1, 7),
                                       [prog_ago(hours=24 * 30)])
        sin.handle_key(curses.KEY_ENTER)
        fuera.handle_key(curses.KEY_ENTER)
        self.assertNotEqual(app_sin.avisos[0], app_fuera.avisos[0])

    def test_programa_en_directo_sigue_abriendo_el_canal(self) -> None:
        """§20.10: el directo es el camino por defecto, intacto."""
        app, screen = _epg_screen(xtream_channel(1, 7), [prog_current()])
        accion = screen.handle_key(curses.KEY_ENTER)
        self.assertEqual(accion["action"], "open_channel")
        self.assertEqual(app.avisos, [])

    def test_programa_futuro_tambien_abre_el_canal(self) -> None:
        _, screen = _epg_screen(xtream_channel(1, 7), [prog_ahead()])
        self.assertEqual(screen.handle_key(curses.KEY_ENTER)["action"], "open_channel")

    def test_sin_declared_no_se_emiten_avisos_de_peticion(self) -> None:
        """§20.9: cero peticiones históricas cuando no hay capacidad."""
        app, screen = _epg_screen(xtream_channel(0, 7), [prog_ago(hours=2)])
        with mock.patch.object(ui_app.catchup, "build_playback_request") as build:
            screen.handle_key(curses.KEY_ENTER)
        build.assert_not_called()


class TestPlayCatchupEnApp(unittest.TestCase):
    """`App.handle_action` valida, delega en `play_channel` y explica fallos."""

    def setUp(self) -> None:
        self.app = mock.Mock()
        self.app.prefs.last_player.return_value = "mpv"
        self.channel = xtream_channel(1, 7)

    def _programa(self, horas: float = 2) -> Program:
        return prog_ago(hours=horas)

    def test_lanza_con_la_url_opaca(self) -> None:
        with mock.patch.object(ui_app, "play_channel") as play:
            ui_app.App.play_catchup(self.app, self.channel, self._programa())
        play.assert_called_once()
        kwargs = play.call_args.kwargs
        canal = play.call_args.args[1]
        self.assertTrue(canal.url.startswith(TS_PREFIX))
        self.assertNotIn("password", canal.url)
        self.assertEqual(kwargs["player_name"], "mpv")
        self.assertIsNotNone(kwargs["catchup_playback"])
        self.assertIsNotNone(kwargs["catchup_program"])

    def test_no_muta_el_canal_original(self) -> None:
        original = self.channel.url
        with mock.patch.object(ui_app, "play_channel"):
            ui_app.App.play_catchup(self.app, self.channel, self._programa())
        self.assertEqual(self.channel.url, original)

    def test_sin_capacidad_modal_y_sin_play(self) -> None:
        self.app.notify_warning.reset_mock()
        with mock.patch.object(ui_app, "play_channel") as play:
            ui_app.App.play_catchup(self.app, xtream_channel(0, 7), self._programa())
        play.assert_not_called()
        self.app.notify_warning.assert_called_once()
        self.app.notify_error.assert_not_called()

    def test_fuera_de_ventana_modal_y_sin_play(self) -> None:
        self.app.notify_warning.reset_mock()
        with mock.patch.object(ui_app, "play_channel") as play:
            ui_app.App.play_catchup(self.app, self.channel,
                                    self._programa(horas=24 * 30))
        play.assert_not_called()
        (mensaje,), _ = self.app.notify_warning.call_args
        self.assertIn("ya no está disponible", mensaje)

    def test_plantilla_inutilizable_es_error_no_advertencia(self) -> None:
        """§20.13: "no lo soporta" y "falló la reproducción" se distinguen."""
        self.app.notify_warning.reset_mock()
        self.app.notify_error.reset_mock()
        canal = Channel(
            name="M3U", url="http://p/1.ts", tvg_id="c.1",
            attrs={"catchup": "append", "catchup_days": "7",
                   "catchup_source": "http://p/ts?ep={episodio}"},
        )
        with mock.patch.object(ui_app, "play_channel") as play:
            ui_app.App.play_catchup(self.app, canal, self._programa())
        play.assert_not_called()
        self.app.notify_error.assert_called_once()
        self.app.notify_warning.assert_not_called()

    def test_programa_sin_stop_usa_una_hora_por_defecto(self) -> None:
        sin_stop = Program("espanol.espn", "Indefinido", NOW - timedelta(hours=2), None)
        with mock.patch.object(ui_app, "play_channel") as play:
            ui_app.App.play_catchup(self.app, self.channel, sin_stop)
        playback = play.call_args.kwargs["catchup_playback"]
        self.assertEqual(playback.duration, 3600)


class TestNowPlayingConArchivo(unittest.TestCase):
    """La tarjeta informational distingue "Archivo" de "En vivo" (§11)."""

    def _screen(self, **kwargs) -> object:  # noqa: ANN003
        app = mock.Mock()
        app.epg = None
        proc = mock.Mock(pid=1)
        proc.poll.return_value = None
        with mock.patch.object(ui_screens, "ChannelHealthMonitor", return_value=mock.Mock()):
            return ui_screens.NowPlayingScreen(app, xtream_channel(1, 7), "mpv", proc,
                                              **kwargs)

    def test_directo_sigue_diciendo_en_vivo(self) -> None:
        self.assertFalse(self._screen().is_archive)
        self.assertNotIn("Archivo", self._screen().title)

    def test_archivo_se_anuncia_como_archivo(self) -> None:
        playback = catchup.PlaybackRequest(
            channel=xtream_channel(1, 7), url=ref_opaca(),
            start=NOW - timedelta(hours=2), duration=3600,
        )
        screen = self._screen(catchup_playback=playback,
                              catchup_program=prog_ago(hours=2))
        self.assertTrue(screen.is_archive)
        self.assertIn("Archivo", screen.title)

    def test_muestra_el_programa_elegido_no_el_actual(self) -> None:
        elegido = prog_ago(hours=2)
        playback = catchup.PlaybackRequest(
            channel=xtream_channel(1, 7), url=ref_opaca(),
            start=elegido.start, duration=5400,
        )
        app = mock.Mock()
        app.epg = Epg(programs={}, channels_by_id={})
        proc = mock.Mock(pid=1)
        proc.poll.return_value = None
        with mock.patch.object(ui_screens, "ChannelHealthMonitor", return_value=mock.Mock()):
            screen = ui_screens.NowPlayingScreen(app, xtream_channel(1, 7), "mpv", proc,
                                                 catchup_playback=playback,
                                                 catchup_program=elegido)
        self.assertIs(screen._current_program(), elegido)

    def test_el_lanzamiento_sigue_siendo_el_mismo_motor(self) -> None:
        """§20.11 / §14: catch-up pasa por `player.launch`, sin capa extra."""
        with mock.patch("thetvview.player.launch") as launch:
            proc = mock.Mock(pid=3)
            proc.poll.return_value = None
            launch.return_value = proc
            app = mock.Mock()
            app.epg = None
            app.playlists = _Catalogo({"Panel": ("http://srv:8080", "u", "secreto")})
            canal = catchup.playback_channel(catchup.PlaybackRequest(
                channel=xtream_channel(1, 7), url=ref_opaca(),
                start=NOW - timedelta(hours=2), duration=3600,
            ))
            with mock.patch.object(ui_screens, "ChannelHealthMonitor",
                                   return_value=mock.Mock()):
                ui_screens.play_channel(app, canal, player_name="mpv")
        launch.assert_called_once()
        # Lo que llega al motor es la URL resuelta (con credenciales, en
        # caliente): el reproductor no conoce el modo, sólo la URL.
        self.assertIn("timeshift.php", launch.call_args.args[0].url)


class TestPanelDeDetalles(unittest.TestCase):
    """La línea «Archivo: N días» sólo aparece con capacidad declarada."""

    def _texto_del_panel(self, channel: Channel) -> list[str]:
        """Todo lo que el panel pinta (curses simulado: 0 = sin color)."""
        from thetvview.models import Playlist

        pantalla = ui_screens.ChannelsScreen(_AppStub(), Playlist(name="p",
                                                                   channels=[channel]))
        stdscr = mock.Mock()
        with mock.patch.object(ui_screens.colors, "pair", return_value=0):
            pantalla._render_detail_panel(stdscr, 0, 0, 40, 30)
        # addstr(y, x, texto, attr): el texto es el 3º argumento. Timeline
        # pinta con addstr(y, x) sin texto, así que se filtran.
        return [c.args[2] for c in stdscr.addstr.call_args_list
                if len(c.args) > 2 and isinstance(c.args[2], str)]

    def test_no_aparece_sin_declaracion(self) -> None:
        escritos = self._texto_del_panel(xtream_channel(0, 7))
        self.assertTrue(escritos, "el panel no pintó nada (test sin valor)")
        ninguno = all("Archivo" not in t for t in escritos)
        self.assertTrue(ninguno, f"apareció Archivo sin declaración: {escritos}")

    def test_aparece_con_declaracion(self) -> None:
        escritos = self._texto_del_panel(xtream_channel(1, 7))
        self.assertTrue(any(f"{icons.ICON_ARCHIVE} Archivo: 7 días" in t for t in escritos),
                        f"no apareció la línea de archivo: {escritos}")

    def test_singular_si_un_dia(self) -> None:
        escritos = self._texto_del_panel(xtream_channel(1, 1))
        self.assertTrue(any("Archivo: 1 día" in t for t in escritos), escritos)


class TestIconoYTeclas(unittest.TestCase):
    def test_hay_icono_de_archivo_y_no_es_emoji(self) -> None:
        self.assertTrue(icons.ICON_ARCHIVE)
        self.assertFalse(any(ord(ch) > 0xFFFF for ch in icons.ICON_ARCHIVE))
        self.assertNotIn(icons.ICON_ARCHIVE, ("", "�"))

    def test_la_ayuda_explica_los_tres_marcadores(self) -> None:
        pantalla = type("EpgScreen", (), {"title": "EPG"})()
        texto = ui_app.build_help_text(pantalla)
        self.assertIn("archivo", texto.lower())
        self.assertIn("proveedor", texto.lower())
        for glifo in ("\u25cf", "\u25b6", "\u25cb"):  # ● ▶ ○
            self.assertIn(glifo, texto)
        lineas = ui_app.build_help_lines(pantalla)
        largos = [t for _, t in lineas if len(t) > 70]
        self.assertEqual(largos, [], f"líneas de ayuda demasiado largas: {largos}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()