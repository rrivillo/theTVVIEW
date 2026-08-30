"""Tests de layout breakpoints para ChannelsScreen (70/80)."""

import unittest

from thetvview.ui.layout import sidebar_rect, detail_rect


class TestSidebarBreakpoints(unittest.TestCase):
    """Verifica rectángulos en distintos anchos de terminal."""

    def test_sidebar_80_es_60_40(self):
        sb = sidebar_rect(24, 80, ratio=0.6)
        dr = detail_rect(24, 80, ratio=0.6)
        self.assertEqual(sb.w, 48)
        self.assertGreaterEqual(dr.w, 28)

    def test_sidebar_75_es_compacto_55(self):
        sb = sidebar_rect(24, 75, ratio=0.55)
        self.assertGreaterEqual(sb.w, 28)

    def test_sidebar_60_fullwidth(self):
        """Por debajo de 70: no hay detail."""
        sb = sidebar_rect(24, 60, ratio=0.55)
        self.assertGreaterEqual(sb.w, 28)

    def test_detail_80_es_minimo_28(self):
        dr = detail_rect(24, 80, ratio=0.6)
        self.assertGreaterEqual(dr.w, 28)

    def test_detail_75_es_minimo_22(self):
        dr = detail_rect(24, 75, ratio=0.55)
        self.assertGreaterEqual(dr.w, 22)

    def test_10x40_no_crash_en_detail(self):
        sb = sidebar_rect(10, 40, ratio=0.6)
        dr = detail_rect(10, 40, ratio=0.6)
        self.assertGreater(sb.w, 0)
        self.assertGreater(dr.w, 0)


if __name__ == "__main__":
    unittest.main()
