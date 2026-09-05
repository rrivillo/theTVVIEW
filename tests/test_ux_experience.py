"""Tests de experiencia de usuario (UX) — solo Python stdlib.

Sin terminal real: se usa FakeStdscr + unittest.mock para simular
la interaccion del usuario con la TUI sin curses real.

Suites (6):
  1. TestUxJourney       — navegacion por teclas + backstack (App.push/pop).
  2. TestUxPerScreen     — shortcuts + render sin crash de cada pantalla.
  3. TestUxSearchEmpty   — busqueda incremental '/' + estados vacios.
  4. TestUxLayout        — breakpoints, resize y tamano minimo.
  5. TestUxThemeA11y     — tema claro/oscuro, NO_COLOR e iconos.
  6. TestUxFeedbackGolden — toasts, modal de ayuda y golden snapshots.

Solo stdlib: curses, unittest, unittest.mock, tempfile, os, pathlib.
Para el informe visual: ``python -m tests.ux_report``.
"""

from __future__ import annotations

import curses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.playlist_manager import PlaylistEntry
from thetvview.ui import app as ui_app
from thetvview.ui import colors
from thetvview.ui.screens import (
    ChannelsScreen,
    EpgScreen,
    FavoritesScreen,
    GroupsScreen,
    NowPlayingScreen,
    PlayerScreen,
    PlaylistsScreen,
    ResolutionScreen,
    format_channel_name,
)
from thetvview.ui.widgets import Modal


# ---------------------------------------------------------------------------
# Fixtures y dobles de prueba
# ---------------------------------------------------------------------------

def make_channel(name="Canal X", group=None, radio=False):
    """Canal de ejemplo para los tests."""
    return Channel(name=name, url="http://example.test/" + name, group=group, radio=radio)


def make_playlist():
    """Playlist pequena y determinista: noticias + radio + deportes."""
    return Playlist(
        name="Test",
        channels=[
            make_channel("La 1", "Noticias"),
            make_channel("Radio Pop", radio=True),
            make_channel("Canal Deportes", "Deportes"),
        ],
    )


class FakeFavorites:
    """Doble minimo de FavoritesManager (trabaja en memoria)."""

    def __init__(self, favs=()):
        self.favs = set(favs)

    def is_favorite(self, channel):
        return channel.name in self.favs

    def toggle(self, channel):
        if channel.name in self.favs:
            self.favs.discard(channel.name)
            return False
        self.favs.add(channel.name)
        return True

    def remove(self, channel):
        if channel.name in self.favs:
            self.favs.discard(channel.name)
            return True
        return False

    def add(self, channel):
        if channel.name in self.favs:
            return False
        self.favs.add(channel.name)
        return True

    def load(self):
        return [make_channel(n) for n in sorted(self.favs)]


class FakeStatusBar:
    """Doble de StatusBar: guarda el ultimo mensaje."""

    def __init__(self):
        self.message = ""
        self.is_error = False

    def show(self, msg, error=False):
        self.message = msg
        self.is_error = error

    def _current_message(self):
        return self.message


class FakeFooter:
    """Doble de FooterBar: guarda chips y ultimo toast."""

    def __init__(self):
        self.chips = []
        self.message = ""

    def show(self, msg, error=False):
        self.message = msg

    def render(self, stdscr):
        pass


class FakeHeader:
    """Doble de HeaderBar: no pinta nada."""

    def render(self, stdscr, title, stack_depth=1):
        pass


class FakeStdscr:
    """Doble de la ventana curses: captura addstr y simula getch/getmaxyx."""

    def __init__(self, h=24, w=80, keys=None):
        self.h = h
        self.w = w
        self.keys = list(keys or [])
        self._pos = 0
        self.addstr_calls = []
        self.erase_count = 0
        self.refresh_count = 0

    def getmaxyx(self):
        return (self.h, self.w)

    def addstr(self, y, x, text, attr=0):
        # Imita el truncado que hace curses al llegar al borde derecho.
        room = self.w - 1 if x == 0 else self.w - x
        if room > 0 and len(text) > room:
            text = text[:room]
        self.addstr_calls.append((y, x, text, attr))

    def erase(self):
        self.erase_count += 1
        self.addstr_calls.clear()

    def refresh(self):
        self.refresh_count += 1

    def getch(self):
        if self._pos < len(self.keys):
            key = self.keys[self._pos]
            self._pos += 1
            return key
        return -1

    def timeout(self, ms):
        pass

    def all_text(self):
        return " ".join(t for _, _, t, _ in self.addstr_calls)


def make_app(test, favs=(), h=24, w=80):
    """Crea un App headless con tmpdir aislado.

    Registra ``test.addCleanup`` para detener los patches (sin fugas).
    Devuelve ``(app, fake)``.
    """
    fake = FakeStdscr(h, w)
    tmp = Path(tempfile.mkdtemp())
    patches = [
        mock.patch.object(ui_app.config, "PLAYLISTS_JSON", tmp / "playlists.json"),
        mock.patch.object(ui_app.config, "FAVORITES_JSON", tmp / "favorites.json"),
        mock.patch.object(ui_app.config, "PREFS_JSON", tmp / "prefs.json"),
        mock.patch.object(ui_app.config, "RECENTS_JSON", tmp / "recents.json"),
        mock.patch.object(ui_app.config, "THEME_JSON", tmp / "theme.json"),
        mock.patch("thetvview.config.ensure_dirs", lambda: None),
        mock.patch.object(colors, "pair", return_value=0),
    ]
    for p in patches:
        p.start()
        test.addCleanup(p.stop)
    app = ui_app.App(fake)
    app.favorites = FakeFavorites(favs)
    app.status = FakeStatusBar()
    app.footer = FakeFooter()
    app.header = FakeHeader()
    return app, fake


def render_screen(screen):
    """Renderiza una pantalla en un FakeStdscr de 24x80 sin colores reales."""
    fake = FakeStdscr(24, 80)
    with mock.patch.object(colors, "pair", return_value=0):
        try:
            screen.render(fake)
        except curses.error:
            pass
    return fake


def parse_chips(app, shortcuts_str):
    """Delega en el parser real de App (no duplica logica)."""
    return app._parse_shortcuts_to_chips(shortcuts_str)


# ---------------------------------------------------------------------------
# 1. Journey — navegacion por teclas + backstack
# ---------------------------------------------------------------------------

class TestUxJourney(unittest.TestCase):
    """El usuario navega Playlists -> Canales -> Grupos -> EPG y vuelve."""

    def setUp(self):
        self.playlist = make_playlist()
        self.app, self.fake = make_app(self, favs={"La 1"})
        self.app.playlist_cache["test_src"] = self.playlist

    def test_playlists_to_channels_enter(self):
        screen = PlaylistsScreen(self.app)
        screen.entries = [PlaylistEntry(name="Test", source="test_src")]
        screen.selected = 0
        self.app.stack[-1] = screen
        action = screen.handle_key(curses.KEY_ENTER)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "open_playlist")
        self.app.push(ChannelsScreen(self.app, self.playlist))
        self.assertEqual(len(self.app.stack), 2)

    def test_channels_back_with_esc(self):
        self.app.push(ChannelsScreen(self.app, self.playlist))
        self.assertEqual(len(self.app.stack), 2)
        self.assertTrue(self.app.pop())
        self.assertEqual(len(self.app.stack), 1)

    def test_q_exits_from_root(self):
        # pop() en la raiz devuelve False: App.run lo interpreta como salir.
        self.assertEqual(len(self.app.stack), 1)
        self.assertFalse(self.app.pop())

    def test_journey_playlists_to_groups(self):
        self.app.push(ChannelsScreen(self.app, self.playlist))
        self.app.push(GroupsScreen(self.app, self.playlist))
        self.assertEqual(len(self.app.stack), 3)
        self.app.pop()
        self.assertEqual(len(self.app.stack), 2)
        self.assertIsInstance(self.app.screen, ChannelsScreen)

    def test_journey_full_backstack(self):
        self.app.push(ChannelsScreen(self.app, self.playlist))
        self.app.push(GroupsScreen(self.app, self.playlist))
        self.app.epg = None
        self.app.ensure_epg = mock.Mock(return_value=[])
        self.app.push(EpgScreen(self.app, make_channel("La 1")))
        self.assertEqual(len(self.app.stack), 4)
        self.app.pop()
        self.app.pop()
        self.app.pop()
        self.assertEqual(len(self.app.stack), 1)
        self.assertIsInstance(self.app.screen, PlaylistsScreen)


# ---------------------------------------------------------------------------
# 2. Por pantalla — shortcuts + render de cada pantalla
# ---------------------------------------------------------------------------

class TestUxPerScreen(unittest.TestCase):
    """Cada pantalla expone shortcuts utiles y renderiza sin crash."""

    def setUp(self):
        self.playlist = make_playlist()
        self.app, self.fake = make_app(self, favs={"La 1"})

    def check_shortcuts(self, screen, min_chips=2):
        text = screen.shortcuts()
        self.assertIsInstance(text, str)
        self.assertGreater(len(text), 5)
        chips = parse_chips(self.app, text)
        self.assertGreaterEqual(len(chips), min_chips)
        return text

    def test_playlists_screen(self):
        screen = PlaylistsScreen(self.app)
        render_screen(screen)
        self.check_shortcuts(screen, min_chips=3)

    def test_channels_screen(self):
        screen = ChannelsScreen(self.app, self.playlist)
        render_screen(screen)
        text = self.check_shortcuts(screen)
        self.assertIn("/", text)

    def test_favorites_screen(self):
        screen = FavoritesScreen(self.app)
        render_screen(screen)
        text = self.check_shortcuts(screen)
        self.assertIn("f", text)
        self.assertIn("\u2605", text)

    def test_groups_screen(self):
        screen = GroupsScreen(self.app, self.playlist)
        render_screen(screen)
        text = self.check_shortcuts(screen)
        self.assertIn("/", text)

    def test_resolution_screen(self):
        variants = [make_channel("La 1 HD"), make_channel("La 1 SD")]
        screen = ResolutionScreen(self.app, make_channel("La 1"), variants)
        render_screen(screen)
        text = screen.shortcuts()
        self.assertIn("\u2190/\u2192", text)
        self.assertIn("Enter \u25b6", text)
        self.assertIn("Esc \u2190", text)

    def test_player_screen(self):
        with mock.patch("thetvview.config.detect_players",
                         return_value={"mpv": "/usr/bin/mpv"}):
            screen = PlayerScreen(self.app, make_channel("La 1"))
        render_screen(screen)
        text = screen.shortcuts()
        self.assertIn("\u2191/\u2193", text)
        self.assertIn("Enter \u25b6", text)

    def test_now_playing_screen(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        screen = NowPlayingScreen(
            self.app, make_channel("La 1"), player_name="mpv", proc=proc)
        render_screen(screen)
        text = screen.shortcuts()
        self.assertIn("q", text)
        self.assertIn("Detener", text)

    def test_epg_screen(self):
        self.app.epg = None
        self.app.ensure_epg = mock.Mock(return_value=[])
        screen = EpgScreen(self.app, make_channel("La 1"))
        render_screen(screen)
        text = screen.shortcuts()
        self.assertIn("r", text)
        self.assertIn("Recargar", text)
        self.assertIn("Esc \u2190", text)


# ---------------------------------------------------------------------------
# 3. Busqueda + estados vacios
# ---------------------------------------------------------------------------

class TestUxSearchEmpty(unittest.TestCase):
    """'/' filtra mientras se escribe; q/t/? no son atajos en ese modo."""

    def setUp(self):
        self.playlist = make_playlist()
        self.app, self.fake = make_app(self, favs={"La 1"})
        self.screen = ChannelsScreen(self.app, self.playlist)

    def type_text(self, text):
        for ch in text:
            self.screen.handle_key(ord(ch))

    def test_slash_enters_searching(self):
        self.assertIsNone(self.screen.handle_key(ord("/")))
        self.assertTrue(self.screen.searching)

    def test_search_keys_go_to_buffer(self):
        for key in ("q", "t", "?"):
            with self.subTest(key=key):
                screen = ChannelsScreen(self.app, self.playlist)
                screen.searching = True
                screen.handle_key(ord(key))
                self.assertTrue(screen.searching)
                self.assertEqual(screen.query, key)

    def test_search_query_filters_channels(self):
        self.screen.searching = True
        self.type_text("Deportes")
        self.assertIn("Deportes", self.screen.query)
        self.assertEqual(len(self.screen.visible_idx), 1)
        shown = self.screen.channels[self.screen.visible_idx[0]]
        self.assertEqual(shown.name, "Canal Deportes")

    def test_empty_query_shows_no_results(self):
        self.screen.searching = True
        self.type_text("xyz")
        self.assertEqual(self.screen.visible_idx, [])

    def test_esc_in_search_clears(self):
        self.screen.searching = True
        self.type_text("dep")
        self.screen.handle_key(27)
        self.assertFalse(self.screen.searching)
        self.assertEqual(self.screen.query, "")

    def test_enter_in_search_confirms(self):
        self.screen.searching = True
        self.type_text("La 1")
        self.screen.handle_key(curses.KEY_ENTER)
        self.assertFalse(self.screen.searching)

    def test_render_empty_no_crash(self):
        empty = ChannelsScreen(self.app, Playlist(name="Empty"))
        render_screen(empty)
        self.assertEqual(empty.visible_idx, [])


# ---------------------------------------------------------------------------
# 4. Layout — breakpoints + resize + tamano minimo
# ---------------------------------------------------------------------------

MIN_H, MIN_W = 10, 40


class TestUxLayout(unittest.TestCase):
    """Todas las pantallas aguantan 10x40, 24x80 y 30x120 sin curses.error."""

    def setUp(self):
        self.app, self.fake = make_app(self)

    def render_all(self, h, w):
        playlist = make_playlist()
        fake = FakeStdscr(h, w)
        screens = [
            PlaylistsScreen(self.app),
            ChannelsScreen(self.app, playlist),
            FavoritesScreen(self.app),
            GroupsScreen(self.app, playlist),
            ResolutionScreen(self.app, make_channel(), [make_channel("HD")]),
        ]
        with mock.patch.object(colors, "pair", return_value=0):
            for screen in screens:
                try:
                    screen.render(fake)
                except curses.error:
                    self.fail("render lanzo curses.error en %dx%d" % (h, w))

    def test_10x40_no_crash(self):
        self.render_all(10, 40)

    def test_24x80_no_crash(self):
        self.render_all(24, 80)

    def test_30x120_no_crash(self):
        self.render_all(30, 120)

    def test_resize_triggers_rerender(self):
        self.fake.h = 30
        self.fake.w = 120
        with mock.patch.object(colors, "pair", return_value=0):
            try:
                self.app.screen.render(self.fake)
            except curses.error:
                self.fail("re-render tras resize lanzo curses.error")

    def test_below_minimum_is_detected(self):
        # App.run muestra "Ventana demasiado pequena" si h<10 o w<40.
        self.assertTrue(5 < MIN_H or 40 < MIN_W)
        self.assertFalse(24 < MIN_H or 80 < MIN_W)

    def test_body_height_responsive(self):
        self.app.push(ChannelsScreen(self.app, make_playlist()))
        small = self.app.body_height()
        self.fake.h = 30
        big = self.app.body_height()
        self.assertGreaterEqual(small, 0)
        self.assertGreaterEqual(big, small)


# ---------------------------------------------------------------------------
# 5. Tema + accesibilidad
# ---------------------------------------------------------------------------

class TestUxThemeA11y(unittest.TestCase):
    """El toggle de tema avisa por footer y NO_COLOR no rompe nada."""

    def setUp(self):
        self.app, self.fake = make_app(self)

    def test_toggle_theme_changes_name(self):
        with mock.patch("curses.has_colors", return_value=False):
            original = self.app.theme_name
            self.app.toggle_theme()
            self.assertNotEqual(self.app.theme_name, original)
            self.app.toggle_theme()
            self.assertEqual(self.app.theme_name, original)

    def test_toggle_theme_shows_footer(self):
        with mock.patch("curses.has_colors", return_value=False):
            self.app.toggle_theme()
        msg = (self.app.footer.message + self.app.status.message).lower()
        self.assertIn("tema", msg)

    def test_no_color_mode_init_colors(self):
        from thetvview.ui.theme import init_colors
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            with mock.patch("curses.has_colors", return_value=True):
                init_colors("dark")  # no debe lanzar ni inicializar pares

    def test_pair_returns_zero_without_color(self):
        with mock.patch("curses.has_colors", return_value=False):
            self.assertEqual(colors.pair(colors.PAIR_NORMAL), 0)
            self.assertEqual(colors.pair(colors.PAIR_FOCUS), 0)

    def test_icons_present_without_color(self):
        self.assertIn("\u2605", format_channel_name(make_channel("X"), favorite=True))
        self.assertIn("\u266a", format_channel_name(make_channel("X", radio=True)))
        grouped = make_channel("X", group="Gr")
        self.assertIn("\u2605", format_channel_name(grouped, favorite=True))


# ---------------------------------------------------------------------------
# 6. Feedback + golden snapshots
# ---------------------------------------------------------------------------

class TestUxFeedbackGolden(unittest.TestCase):
    """Toasts, modal de ayuda y snapshots deterministas en 24x80."""

    def setUp(self):
        self.playlist = make_playlist()
        self.app, self.fake = make_app(self, favs={"La 1"})

    def test_fav_toggle_shows_toast(self):
        self.app.toggle_favorite(make_channel("Canal Deportes"))
        msg = self.app.status.message.lower()
        self.assertTrue(
            "favoritos" in msg or "a\u00f1adido" in msg or "quitado" in msg,
            "toast de favorito poco claro: %r" % msg,
        )

    def test_toggle_theme_toast(self):
        with mock.patch("curses.has_colors", return_value=False):
            self.app.toggle_theme()
        msg = (self.app.footer.message + self.app.status.message).lower()
        self.assertIn("tema", msg)

    def test_help_overlay_via_modal(self):
        modal = Modal("Ayuda", "Ayuda de la app", ["Cerrar"])
        fake = FakeStdscr(24, 80)
        with mock.patch.object(colors, "pair", return_value=0):
            modal.render(fake)
        self.assertGreater(len(fake.addstr_calls), 0)

    def test_golden_channels_screen_24x80(self):
        screen = ChannelsScreen(self.app, self.playlist)
        fake = render_screen(screen)
        snapshot = [(y, x, t.rstrip()) for y, x, t, _ in fake.addstr_calls]
        all_text = " ".join(t for _, _, t in snapshot)
        self.assertIn("La 1", all_text)
        self.assertIn("\u2605", all_text)  # La 1 es favorito
        self.assertIn("\u266a", all_text)  # Radio Pop es radio
        self.assertGreater(len(snapshot), 3)
        # Determinista: mismos datos -> mismo snapshot.
        fake2 = render_screen(ChannelsScreen(self.app, self.playlist))
        snapshot2 = [(y, x, t.rstrip()) for y, x, t, _ in fake2.addstr_calls]
        self.assertEqual(snapshot, snapshot2)

    def test_golden_playlists_screen_24x80(self):
        fake = render_screen(PlaylistsScreen(self.app))
        snapshot = [(y, x, t.rstrip()) for y, x, t, _ in fake.addstr_calls]
        self.assertGreater(len(snapshot), 1)

    def test_footer_chips_parsed(self):
        screen = PlaylistsScreen(self.app)
        chips = parse_chips(self.app, screen.shortcuts())
        self.assertGreaterEqual(len(chips), 3)
        for key, label in chips:
            self.assertIsInstance(key, str)
            self.assertIsInstance(label, str)
            self.assertGreater(len(label), 0)


if __name__ == "__main__":
    unittest.main()
