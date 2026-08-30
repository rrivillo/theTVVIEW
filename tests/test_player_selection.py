"""Tests de la selección de reproductor antes de reproducir (punto 2).

Cubre:
- PlayerScreen: lista solo reproductores instalados, en orden de preferencia.
- Enter -> acción 'play_with' con el jugador seleccionado.
- App: 'select_player' empuja la pantalla (o avisa si no hay ninguno) y
  'play_with' delega en player.launch con el reproductor elegido.
"""

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel
from thetvview.ui import app as ui_app
from thetvview.ui.screens import PlayerScreen, play_channel


def ch(name: str = "Canal X") -> Channel:
    return Channel(name=name, url=f"http://x/{name}")


class _Status:
    def __init__(self) -> None:
        self.last: str = ""
        self.was_error: bool = False

    def show(self, message: str, error: bool = False) -> None:
        self.last = message
        self.was_error = error


class _StubApp:
    """Lo mínimo que PlayerScreen/play_channel necesitan sin curses."""

    def __init__(self) -> None:
        self.status = _Status()
        self.stack: list = []
        self.epg = None  # sin EPG cargado

    def body_height(self) -> int:
        return 10

    def push(self, screen) -> None:
        self.stack.append(screen)


ENTER = 10


class TestPlayerScreen(unittest.TestCase):
    def test_lista_solo_instalados_en_orden(self):
        installed = {"mpv": "/usr/bin/mpv", "mplayer": None, "vlc": "/usr/bin/vlc"}
        with mock.patch("thetvview.config.detect_players", return_value=installed):
            screen = PlayerScreen(_StubApp(), ch())
        self.assertEqual([n for n, _ in screen.players], ["mpv", "vlc"])

    def test_enter_devuelve_play_con_reproductor_seleccionado(self):
        installed = {"mpv": "/usr/bin/mpv", "mplayer": None, "vlc": "/usr/bin/vlc"}
        with mock.patch("thetvview.config.detect_players", return_value=installed):
            screen = PlayerScreen(_StubApp(), ch())
        action = screen.handle_key(ENTER)
        self.assertEqual(
            action,
            {"action": "play_with", "channel": action["channel"], "player_name": "mpv"},
        )
        self.assertEqual(action["channel"].name, "Canal X")

    def test_navegar_y_elegir_segundo(self):
        installed = {n: f"/usr/bin/{n}" for n in ("mpv", "mplayer", "vlc")}
        with mock.patch("thetvview.config.detect_players", return_value=installed):
            screen = PlayerScreen(_StubApp(), ch())
        screen.handle_key(curses.KEY_DOWN)  # solo navega, devuelve None
        action = screen.handle_key(ENTER)
        self.assertEqual(action["player_name"], "mplayer")

    def test_sin_reproductores_lista_vacia_y_enter_inofensivo(self):
        with mock.patch("thetvview.config.detect_players", return_value={"mpv": None}):
            screen = PlayerScreen(_StubApp(), ch())
        self.assertEqual(screen.players, [])
        self.assertIsNone(screen.handle_key(ENTER))


class TestPlayChannel(unittest.TestCase):
    def test_reenvia_player_name_a_launch(self):
        stub = _StubApp()
        fake_proc = mock.Mock(pid=4242)
        fake_proc.poll.return_value = None
        with mock.patch("thetvview.player.launch", return_value=fake_proc) as launch:
            play_channel(stub, ch(), player_name="vlc")
        launch.assert_called_once()
        args, kwargs = launch.call_args
        self.assertEqual(kwargs.get("player_name"), "vlc")
        self.assertIn("VLC", stub.status.last.upper())
        self.assertIn("4242", stub.status.last)
        # Ahora hace push de NowPlayingScreen
        self.assertEqual(len(stub.stack), 1)
        self.assertEqual(stub.stack[0].channel.name, "Canal X")
        self.assertTrue(stub.stack[0].is_alive())

    def test_sin_player_name_lanza_por_preferencia(self):
        stub = _StubApp()
        fake_proc = mock.Mock(pid=1)
        fake_proc.poll.return_value = None
        with mock.patch("thetvview.player.launch", return_value=fake_proc) as launch:
            play_channel(stub, ch())
        self.assertIsNone(launch.call_args.kwargs.get("player_name"))
        self.assertEqual(len(stub.stack), 1)


class TestAppActions(unittest.TestCase):
    def setUp(self):
        # Aísla el estado en disco del catálogo/favoritos.
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        patches = [
            mock.patch.object(ui_app.config, "PLAYLISTS_JSON", base / "playlists.json"),
            mock.patch.object(ui_app.config, "FAVORITES_JSON", base / "favorites.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _app(self) -> ui_app.App:
        return ui_app.App(None)  # noqa: ANN001 - no se renderiza

    def test_select_player_empuja_pantalla_si_hay_reproductores(self):
        app = self._app()
        installed = {"mpv": "/usr/bin/mpv", "mplayer": None, "vlc": None}
        with mock.patch("thetvview.config.detect_players", return_value=installed):
            app.handle_action({"action": "select_player", "channel": ch()})
        self.assertIsInstance(app.screen, PlayerScreen)
        self.assertEqual(len(app.stack), 2)

    def test_select_player_avisa_si_no_hay_ninguno(self):
        app = self._app()
        with mock.patch("thetvview.config.detect_players", return_value={"mpv": None}):
            app.handle_action({"action": "select_player", "channel": ch()})
        self.assertEqual(len(app.stack), 1)
        self.assertTrue(app.status.is_error)
        self.assertIn("No hay reproductor", app.status.message)

    def test_play_with_lanza_el_reproductor_elegido(self):
        app = self._app()
        fake_proc = mock.Mock(pid=7)
        fake_proc.poll.return_value = None
        canal = ch()
        with mock.patch("thetvview.player.launch", return_value=fake_proc) as launch:
            app.handle_action(
                {"action": "play_with", "channel": canal, "player_name": "mplayer"}
            )
        launch.assert_called_once_with(canal, player_name="mplayer")
        # Ahora se hace push de NowPlayingScreen
        from thetvview.ui.screens import NowPlayingScreen

        self.assertEqual(len(app.stack), 2)
        self.assertIsInstance(app.screen, NowPlayingScreen)


if __name__ == "__main__":
    unittest.main()
