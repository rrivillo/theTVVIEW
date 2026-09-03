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
            type("E", (), {"name": "Test", "source": "http://x/a.m3u"})()
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
            type("E", (), {"name": "Test", "source": "http://x/a.m3u"})()
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
        screen.render(stdscr)


class TestFormatChannelName(unittest.TestCase):
    def test_favorito_icono(self):
        self.assertIn("★", format_channel_name(ch("X"), favorite=True))

    def test_radio_icono(self):
        self.assertIn("♪", format_channel_name(ch("X", radio=True)))


if __name__ == "__main__":
    unittest.main()
