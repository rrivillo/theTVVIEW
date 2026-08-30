"""Tests del tema vistoso: paleta dark violeta, NO_COLOR, focus/scrollbar."""

import os
import unittest
from unittest import mock

from thetvview.ui import colors


class TestDarkPaletteVioleta(unittest.TestCase):
    """Verifica que la paleta dark usa violeta en vez de cyan."""

    def test_dark_primary_es_violeta_141(self):
        from thetvview.ui.theme import _THEMES
        self.assertEqual(_THEMES["dark"]["primary"][0], 141)

    def test_dark_primary_hi_es_magenta_177(self):
        from thetvview.ui.theme import _THEMES
        self.assertEqual(_THEMES["dark"]["primary_hi"][0], 177)

    def test_dark_accent_es_amber_215(self):
        from thetvview.ui.theme import _THEMES
        self.assertEqual(_THEMES["dark"]["accent"][0], 215)

    def test_dark_title_bg_es_violeta_141(self):
        from thetvview.ui.theme import _THEMES
        self.assertEqual(_THEMES["dark"]["title_bg"][0], 141)

    def test_dark_selected_bg_es_violeta_141(self):
        from thetvview.ui.theme import _THEMES
        self.assertEqual(_THEMES["dark"]["selected_bg"][0], 141)

    def test_dark_border_es_240(self):
        from thetvview.ui.theme import _THEMES
        self.assertEqual(_THEMES["dark"]["border"][0], 240)

    def test_light_mantiene_blue_21(self):
        from thetvview.ui.theme import _THEMES
        self.assertEqual(_THEMES["light"]["primary"][0], 21)

    def test_dark_focus_existe(self):
        from thetvview.ui.theme import _THEMES
        self.assertIn("focus", _THEMES["dark"])

    def test_dark_scrollbar_existe(self):
        from thetvview.ui.theme import _THEMES
        self.assertIn("scrollbar", _THEMES["dark"])


class TestNoColor(unittest.TestCase):
    """Verifica que NO_COLOR desactiva colores."""

    def test_no_color_desactiva_init_pair(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            with mock.patch("curses.start_color") as sc:
                from thetvview.ui.theme import init_colors
                init_colors("dark")
                sc.assert_not_called()

    def test_no_color_no_crash(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            from thetvview.ui.theme import init_colors
            init_colors("light")


class TestPairIds(unittest.TestCase):
    """Verifica IDs de pares nuevos."""

    def test_pair_focus_es_22(self):
        self.assertEqual(colors.PAIR_FOCUS, 22)

    def test_pair_scrollbar_es_23(self):
        self.assertEqual(colors.PAIR_SCROLLBAR, 23)


if __name__ == "__main__":
    unittest.main()
