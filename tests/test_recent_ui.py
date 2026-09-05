"""Tests de la UI de Recents y shortcuts 'r'/'p'/'?'."""

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.ui import app as ui_app
from thetvview.ui import colors
from thetvview.ui.screens import ChannelsScreen, PlaylistsScreen, RecentsScreen


def ch(name: str = "Canal X") -> Channel:
    return Channel(name=name, url=f"http://x/{name}", group="G")


class FakeStdscr:
    def __init__(self, h=24, w=80):
        self.h = h
        self.w = w
        self.calls = []

    def getmaxyx(self):
        return self.h, self.w

    def addstr(self, y, x, text, attr=0):
        self.calls.append((y, x, text, attr))

    def erase(self):
        self.calls.clear()

    def refresh(self):
        pass


class TestRecentsScreen(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        patches = [
            mock.patch.object(ui_app.config, "PLAYLISTS_JSON", base / "playlists.json"),
            mock.patch.object(ui_app.config, "FAVORITES_JSON", base / "favorites.json"),
            mock.patch.object(ui_app.config, "PREFS_JSON", base / "prefs.json"),
            mock.patch.object(ui_app.config, "RECENTS_JSON", base / "recents.json"),
            mock.patch("thetvview.config.ensure_dirs", lambda: None),
            mock.patch.object(colors, "pair", return_value=0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = ui_app.App(FakeStdscr())

    def test_shortcuts_contienen_r_y_esc(self):
        screen = RecentsScreen(self.app)
        text = screen.shortcuts()
        self.assertIn("r", text)
        self.assertIn("Esc", text)

    def test_render_no_crash_con_vacio(self):
        screen = RecentsScreen(self.app)
        fake = FakeStdscr()
        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(fake)
        self.assertGreater(len(fake.calls), 0)

    def test_playlists_screen_shortcuts_contienen_r(self):
        screen = PlaylistsScreen(self.app)
        text = screen.shortcuts()
        self.assertIn("r", text)


if __name__ == "__main__":
    unittest.main()
