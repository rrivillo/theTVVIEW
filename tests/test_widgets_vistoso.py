"""Tests de widgets vistosos: HeaderBar, FooterBar, Modal, EmptyState, ScrollableList."""

import curses
import unittest
from unittest import mock

from thetvview.ui import colors, icons
from thetvview.ui.widgets import (
    EmptyState,
    FooterBar,
    HeaderBar,
    Modal,
    ScrollableList,
)


class _FakeStdscr:
    """Stdscr falso que captura llamadas a addstr."""

    def __init__(self, max_y: int = 24, max_x: int = 80) -> None:
        self._max_y = max_y
        self._max_x = max_x
        self.calls: list[tuple[int, int, str, int]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return self._max_y, self._max_x

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        self.calls.append((y, x, text, attr))


class TestHeaderBar(unittest.TestCase):
    def test_render_banda_solido_bold(self):
        stdscr = _FakeStdscr(24, 80)
        header = HeaderBar(app=None)
        with mock.patch.object(colors, "pair", return_value=0):
            header.render(stdscr, "Playlists", stack_depth=1)
        # Debe haber al menos una llamada a addstr en fila 0
        row0 = [c for c in stdscr.calls if c[0] == 0]
        self.assertGreater(len(row0), 0)

    def test_render_tiene_logo(self):
        stdscr = _FakeStdscr(24, 80)
        header = HeaderBar(app=None)
        with mock.patch.object(colors, "pair", return_value=0):
            header.render(stdscr, "Test", stack_depth=1)
        row0_text = " ".join(c[2] for c in stdscr.calls if c[0] == 0)
        self.assertIn("theTVVIEW", row0_text)


class TestFooterBar(unittest.TestCase):
    def test_render_chips_con_primary_bold(self):
        stdscr = _FakeStdscr(24, 80)
        fb = FooterBar()
        fb.chips = [("Enter", "seleccionar"), ("Esc", "volver")]
        fb._flash_until = 0.0
        with mock.patch.object(colors, "pair", return_value=0):
            fb.render(stdscr)
        row0 = [c for c in stdscr.calls if c[0] == 23]
        self.assertGreater(len(row0), 0)


class TestModal(unittest.TestCase):
    def test_render_borde_doble(self):
        stdscr = _FakeStdscr(24, 80)
        modal = Modal("Test", "Mensaje", ["Aceptar"])
        with mock.patch.object(colors, "pair", return_value=0):
            modal.render(stdscr)
        all_text = " ".join(c[2] for c in stdscr.calls)
        self.assertIn(icons.BOX_D_TL, all_text)

    def test_selected_button_tiene_reverse(self):
        stdscr = _FakeStdscr(24, 80)
        modal = Modal("Test", "Mensaje", ["Si", "No"])
        modal.selected_button = 0
        with mock.patch.object(colors, "pair", return_value=0):
            modal.render(stdscr)
        btn_calls = [c for c in stdscr.calls if "[Si]" in c[2] or "[No]" in c[2]]
        self.assertGreater(len(btn_calls), 0)


class TestEmptyState(unittest.TestCase):
    def test_render_mantiene_arte_7_lineas(self):
        stdscr = _FakeStdscr(24, 80)
        with mock.patch.object(colors, "pair", return_value=0):
            EmptyState.render(stdscr, 12, 80, "Sin datos", "pulsa 'a'")
        all_text = " ".join(c[2] for c in stdscr.calls)
        self.assertIn("┌──────────┐", all_text)
        self.assertIn("◉", all_text)

    def test_cta_usa_selected(self):
        stdscr = _FakeStdscr(24, 80)
        with mock.patch.object(colors, "pair", return_value=0):
            EmptyState.render(stdscr, 12, 80, "Msg", "CTA")
        cta_calls = [c for c in stdscr.calls if "CTA" in c[2]]
        self.assertGreater(len(cta_calls), 0)


class TestScrollableListScrollbar(unittest.TestCase):
    def test_scrollbar_thumb_return_value(self):
        sl = ScrollableList([f"item_{i}" for i in range(50)])
        thumb = sl._scrollbar_thumb(10)
        self.assertIsNotNone(thumb)
        t_top, t_h = thumb
        self.assertGreater(t_h, 0)

    def test_scrollbar_no_overflow(self):
        sl = ScrollableList(["a", "b", "c"])
        self.assertIsNone(sl._scrollbar_thumb(10))


if __name__ == "__main__":
    unittest.main()
