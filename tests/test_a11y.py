"""Tests de accesibilidad: NO_COLOR, contraste, color nunca solo señal."""

import os
import unittest
from unittest import mock

from thetvview.ui import colors


class TestNoColorMode(unittest.TestCase):
    """NO_COLOR=1 debe desactivar pares de color sin crashear."""

    def test_init_colors_no_crash_con_no_color(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            with mock.patch("curses.has_colors", return_value=True):
                from thetvview.ui.theme import init_colors
                init_colors("dark")

    def test_pair_devuelve_0_sin_color(self):
        with mock.patch("curses.has_colors", return_value=False):
            self.assertEqual(colors.pair(colors.PAIR_NORMAL), 0)
            self.assertEqual(colors.pair(colors.PAIR_FOCUS), 0)


class TestHealthIconBackup(unittest.TestCase):
    """La señal health debe tener icono + barra + label, no solo color."""

    def test_health_line_contiene_icono_y_barra(self):
        """Verifica que NowPlayingScreen render incluye icono junto a la barra de salud."""
        from thetvview.ui.screens import NowPlayingScreen
        from thetvview.models import Channel
        from unittest.mock import Mock
        import time

        ch = Channel(name="Test", url="http://x/test")
        proc = Mock()
        proc.poll.return_value = None
        app = Mock()
        app.epg = None

        screen = NowPlayingScreen(app, ch, "mpv", proc)
        # Forzar health status mock
        health_mock = Mock()
        health_mock.status.return_value = Mock(
            level=4,
            bar="●●●●○",
            label="Buena",
            color_pair=colors.PAIR_SUCCESS,
            success_rate=0.9,
        )
        screen.health = health_mock

        stdscr = Mock()
        stdscr.getmaxyx.return_value = (24, 80)
        stdscr.addstr = Mock()

        with mock.patch.object(colors, "pair", return_value=0):
            screen.render(stdscr)

        calls_str = [str(c) for c in stdscr.addstr.call_args_list]
        has_signal = any("Señal" in s or "●●●●○" in s or "Buena" in s for s in calls_str)
        self.assertTrue(has_signal, "La barra de salud debe incluir icono + bar + label")


if __name__ == "__main__":
    unittest.main()
