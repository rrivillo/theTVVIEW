"""La opción 'Cambiar contraseña' solo vale para listas Xtream API.

Criterio: source con prefijo 'xtream://' (kind queda normalizado a xtream).
"""

import tempfile
import unittest
from pathlib import Path

from thetvview.playlist_manager import PlaylistEntry, PlaylistManager
from thetvview.ui.app import build_help_lines
from thetvview.ui.screens import PlaylistsScreen


class _FakeFooter:
    def __init__(self) -> None:
        self.message = ""

    def show(self, msg, error=False) -> None:
        self.message = msg


class _FakeStdscr:
    def getmaxyx(self):
        return (24, 80)


class _FakeApp:
    def __init__(self, mgr: PlaylistManager) -> None:
        self.playlists = mgr
        self.footer = _FakeFooter()
        self.stdscr = _FakeStdscr()


def _screen_with(test: unittest.TestCase, entries: list[PlaylistEntry]) -> PlaylistsScreen:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    mgr = PlaylistManager(Path(tmp.name) / "playlists.json")
    mgr._write(entries)  # escritura directa del fixture
    return PlaylistsScreen(_FakeApp(mgr))


class TestChangePasswordOnlyXtream(unittest.TestCase):
    def _m3u_screen(self) -> PlaylistsScreen:
        return _screen_with(self, [PlaylistEntry(name="M3U", source="/tmp/a.m3u")])

    def _xtream_screen(self) -> PlaylistsScreen:
        return _screen_with(self, [
            PlaylistEntry(name="M3U", source="/tmp/a.m3u"),
            PlaylistEntry(
                name="API",
                source="xtream://http://host:8080",
                kind="m3u",  # el prefijo debe mandar
                username="u",
                password="p",
            ),
        ])

    def test_prefijo_xtream_normaliza_kind_y_server_url(self) -> None:
        entry = PlaylistEntry(name="X", source="xtream://http://h:8080")
        self.assertEqual(entry.kind, "xtream")
        self.assertTrue(entry.is_xtream)
        self.assertEqual(entry.server_url, "http://h:8080")

    def test_m3u_no_tiene_opcion_en_shortcuts(self) -> None:
        screen = self._m3u_screen()
        self.assertNotIn("Contraseña", screen.shortcuts())

    def test_xtream_si_tiene_opcion_en_shortcuts(self) -> None:
        screen = self._xtream_screen()
        screen.selected = 1
        self.assertIn("C Contraseña Xtream", screen.shortcuts())

    def test_tecla_c_en_m3u_no_lanza_accion(self) -> None:
        screen = self._m3u_screen()
        self.assertIsNone(screen.handle_key(ord("C")))

    def test_tecla_c_en_xtream_lanza_accion(self) -> None:
        screen = self._xtream_screen()
        screen.selected = 1
        action = screen.handle_key(ord("C"))
        self.assertEqual(action, {"action": "change_password", "name": "API"})

    def test_ayuda_oculta_C_si_no_es_xtream(self) -> None:
        screen = self._m3u_screen()
        lines = [t for s, t in build_help_lines(screen) if s == "key"]
        self.assertFalse([t for t in lines if t.startswith("C  ")])

    def test_ayuda_muestra_C_si_es_xtream(self) -> None:
        screen = self._xtream_screen()
        screen.selected = 1
        lines = [t for s, t in build_help_lines(screen) if s == "key"]
        self.assertTrue([t for t in lines if t.startswith("C  ")])


if __name__ == "__main__":
    unittest.main()
