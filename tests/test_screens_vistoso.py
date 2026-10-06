"""Tests de pantallas vistosas: PlaylistsScreen, GroupsScreen, ResolutionScreen, etc."""

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.ui import app as ui_app, colors
from thetvview.ui.screens import (
    ChannelsScreen,
    EpgScreen,
    FavoritesScreen,
    GroupsScreen,
    PlayerScreen,
    PlaylistsScreen,
    ResolutionScreen,
    format_channel_name,
)


def ch(name: str = "Canal X", group: str | None = None, radio: bool = False) -> Channel:
    return Channel(name=name, url=f"http://x/{name}", group=group, radio=radio)


class _Fav:
    def __init__(self, favs=()):
        self.favs = set(favs)

    def is_favorite(self, channel: Channel) -> bool:
        return channel.name in self.favs


class _StubApp:
    def __init__(self, favs=(), max_y=24, max_x=80):
        self.favorites = _Fav(favs)
        self.status = type("S", (), {"show": lambda s, m, e=False: None, "is_error": False, "message": ""})()
        self.footer = type("F", (), {"show": lambda s, m, e=False: None})()
        self.stdscr = type("T", (), {"getmaxyx": lambda s: (max_y, max_x)})()
        self.theme_name = "light"
        self.epg = None
        self.playlist_cache = {}

    def body_height(self) -> int:
        return 10


class TestPlaylistsScreenCard(unittest.TestCase):
    def test_render_no_crash_en_10x40(self):
        stdscr = type("T", (), {
            "getmaxyx": lambda s: (10, 40),
            "addstr": lambda s, y, x, t, a=0: None,
        })()
        app = _StubApp(max_y=10, max_x=40)
        app.playlists = mock.Mock()
        app.playlists.load = mock.Mock(return_value=[
            type("E", (), {"name": "Test", "source": "http://x/a.m3u", "kind": "m3u"})()
        ])
        screen = PlaylistsScreen(app)
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)

    def test_render_no_crash_en_30x120(self):
        stdscr = type("T", (), {
            "getmaxyx": lambda s: (30, 120),
            "addstr": lambda s, y, x, t, a=0: None,
        })()
        app = _StubApp(max_y=30, max_x=120)
        app.playlists = mock.Mock()
        app.playlists.load = mock.Mock(return_value=[
            type("E", (), {"name": "Test", "source": "http://x/a.m3u", "kind": "m3u"})()
        ])
        screen = PlaylistsScreen(app)
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)


class TestGroupsScreenCard(unittest.TestCase):
    def test_render_no_crash(self):
        stdscr = type("T", (), {
            "getmaxyx": lambda s: (24, 80),
            "addstr": lambda s, y, x, t, a=0: None,
        })()
        app = _StubApp()
        pl = Playlist(name="P", channels=[
            ch("A", "Deportes"), ch("B", "Noticias"), ch("C", "Deportes"),
        ])
        screen = GroupsScreen(app, pl)
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)

    def test_render_con_search_active(self):
        stdscr = type("T", (), {
            "getmaxyx": lambda s: (24, 80),
            "addstr": lambda s, y, x, t, a=0: None,
        })()
        app = _StubApp()
        pl = Playlist(name="P", channels=[
            ch("A", "Deportes"), ch("B", "Noticias"), ch("C", "Deportes"),
        ])
        screen = GroupsScreen(app, pl)
        screen.searching = True
        screen.query = "dep"
        screen._apply_filter()
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)

    def test_render_con_query_sin_resultados(self):
        stdscr = type("T", (), {
            "getmaxyx": lambda s: (24, 80),
            "addstr": lambda s, y, x, t, a=0: None,
        })()
        app = _StubApp()
        pl = Playlist(name="P", channels=[
            ch("A", "Deportes"), ch("B", "Noticias"),
        ])
        screen = GroupsScreen(app, pl)
        screen.query = "xyz"
        screen._apply_filter()
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)

    def test_render_no_crash_10x40(self):
        stdscr = type("T", (), {
            "getmaxyx": lambda s: (10, 40),
            "addstr": lambda s, y, x, t, a=0: None,
        })()
        app = _StubApp(max_y=10, max_x=40)
        pl = Playlist(name="P", channels=[
            ch("A", "Deportes"), ch("B", "Noticias"),
        ])
        screen = GroupsScreen(app, pl)
        screen.searching = True
        screen.query = "dep"
        screen._apply_filter()
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)


class TestResolutionScreenPills(unittest.TestCase):
    def test_render_no_crash(self):
        stdscr = type("T", (), {
            "getmaxyx": lambda s: (24, 80),
            "addstr": lambda s, y, x, t, a=0: None,
        })()
        app = _StubApp()
        variants = [ch("Canal HD"), ch("Canal SD")]
        screen = ResolutionScreen(app, ch("Canal"), variants)
        screen.render(stdscr)


class _StdscrQueGuarda:
    """Doble de ventana que anota lo pintado, para leer la fila de las pills."""

    def __init__(self, h=24, w=80):
        self.h, self.w = h, w
        self.calls: list[tuple[int, int, str]] = []

    def getmaxyx(self):
        return (self.h, self.w)

    def addstr(self, y, x, text, attr=0):
        room = self.w - 1 if x == 0 else self.w - x
        if room > 0 and len(text) > room:
            text = text[:room]
        self.calls.append((y, x, text))

    def fila(self, y: int) -> str:
        partes = sorted((x, t) for cy, x, t in self.calls if cy == y)
        return "".join(t for _x, t in partes)

    @property
    def desbordado(self) -> bool:
        return any(x + len(t) > self.w - 1 for _y, x, t in self.calls)


class TestResolutionScreenNoConfunde(unittest.TestCase):
    """El selector de calidad no puede ofrecer dos botones iguales.

    Caso real de una lista publicada por el usuario: el mismo canal aparece
    tres veces (HD, SD y SD alterno) y las dos SD se pintaban idénticas. Aquí se
    fija que (a) las etiquetas se distinguen y (b) el cursor abre sobre la
    variante que el usuario eligió, no sobre la primera de la lista.
    """

    NOMBRES = [
        "[Chile] La Red HD (Nacional)[Geo-Blocked]",
        "[Chile] La Red SD (Nacional)[Opc.2]",
        "[Chile] La Red SD (Nacional)[Opc.3]",
    ]

    def setUp(self):
        self.app = _StubApp()
        self.variantes = [ch(n) for n in self.NOMBRES]

    def pintar(self, elegida, w=80):
        screen = ResolutionScreen(self.app, elegida, self.variantes)
        stdscr = _StdscrQueGuarda(24, w)
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)
        return screen, stdscr

    def test_las_pills_no_se_repeten(self):
        _screen, stdscr = self.pintar(self.variantes[0])
        fila = stdscr.fila(10)
        for marca in ("[Opc.2]", "[Opc.3]"):
            self.assertIn(marca, fila, "las dos SD tienen que decir cuál es cuál")

    def test_el_nombre_de_abajo_coincide_con_el_boton_resaltado(self):
        """El texto grande es la verdad: si dice HD, el resaltado es el HD."""
        for elegida in self.variantes:
            with self.subTest(canal=elegida.name):
                screen, stdscr = self.pintar(elegida)
                resaltado = stdscr.fila(10)
                # El botón resaltado se pinta con un relleno de espacios.
                self.assertIn(elegida.name, stdscr.fila(12))
                self.assertEqual(screen.selected, self.variantes.index(elegida))

    def test_enter_reproduce_la_variante_elegida(self):
        """Enter sobre la variante elegida tiene que dar esa misma variante."""
        for elegida in self.variantes:
            with self.subTest(canal=elegida.name):
                screen = ResolutionScreen(self.app, elegida, self.variantes)
                accion = screen.handle_key(curses.KEY_ENTER)
                self.assertEqual(accion["channel"].name, elegida.name)

    def test_enter_tras_no_mover_no_cambia_de_canal(self):
        """El caso que sí dolía: abrir y pulsar Enter salía con otra calidad."""
        elegida = self.variantes[2]  # el HD, el último de la lista
        screen = ResolutionScreen(self.app, elegida, self.variantes)
        self.assertEqual(screen.handle_key(curses.KEY_ENTER)["channel"], elegida)

    def test_un_canal_fuera_de_las_variantes_abre_en_la_primera(self):
        otro = ch("La Red HD")
        screen = ResolutionScreen(self.app, otro, self.variantes)
        self.assertEqual(screen.selected, 0)

    def test_terminal_estrecha_no_desborda(self):
        for w in (80, 40, 30, 24, 20, 12):
            with self.subTest(ancho=w):
                _screen, stdscr = self.pintar(self.variantes[0], w)
                self.assertFalse(stdscr.desbordado, f"desbordó con {w} columnas")


class TestPlayerScreenSubtitle(unittest.TestCase):
    def test_render_muestra_nombre_reproductor(self):
        calls = []
        stdscr = type("T", (), {
            "getmaxyx": lambda s: (24, 80),
            "addstr": lambda s, y, x, t, a=0: calls.append((y, x, t, a)),
        })()
        app = _StubApp()
        with mock.patch("thetvview.config.detect_players", return_value={"mpv": "/usr/bin/mpv"}):
            screen = PlayerScreen(app, ch())
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)
        all_text = " ".join(c[2] for c in calls if len(c) > 2 and isinstance(c[2], str))
        self.assertIn("MPV", all_text)


class TestEpgScreenCurrent(unittest.TestCase):
    def test_render_no_crash(self):
        stdscr = type("T", (), {
            "getmaxyx": lambda s: (24, 80),
            "addstr": lambda s, y, x, t, a=0: None,
        })()
        app = _StubApp()
        app.epg = None
        app.ensure_epg = mock.Mock(return_value=[])
        screen = EpgScreen(app, ch())
        # `colors.pair` necesita `initscr()`; sin terminal hay que neutralizarlo
        # (como en el resto de tests de este archivo). El `EmptyState` calcula
        # sus atributos **antes** de dibujar, así que aquí no lo traga ningún
        # `except curses.error` — y no debe: tragarse un fallo de color sería
        # dibujar en el par equivocado sin que nadie se entere.
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)


class TestFormatChannelName(unittest.TestCase):
    def test_favorito_icono(self):
        self.assertIn("★", format_channel_name(ch("X"), favorite=True))

    def test_radio_icono(self):
        self.assertIn("♪", format_channel_name(ch("X", radio=True)))


if __name__ == "__main__":
    unittest.main()
