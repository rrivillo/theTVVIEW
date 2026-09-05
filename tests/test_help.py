"""La ayuda debe ser entendible: contextual, con pasos y cierre claro."""

import unittest

from thetvview.ui.app import build_help_lines, build_help_text


def _fake_screen(cls_name, title="Título"):
    screen = type(cls_name, (), {"title": title})()
    screen.__class__.__name__ = cls_name
    return screen


class TestHelpUnderstandable(unittest.TestCase):
    SCREENS = [
        "PlaylistsScreen", "ChannelsScreen", "GroupsScreen",
        "FavoritesScreen", "RecentsScreen", "EpgScreen",
        "ResolutionScreen", "PlayerScreen", "NowPlayingScreen",
    ]

    def test_help_is_contextual(self):
        """Cada pantalla dice dónde está el usuario y qué hacer allí."""
        for cls in self.SCREENS:
            lines = build_help_lines(_fake_screen(cls))
            sections = [t for s, t in lines if s == "section"]
            self.assertTrue(
                any("Estás en" in t for t in sections),
                f"{cls} no dice dónde está el usuario",
            )
            self.assertTrue(
                any(f"En esta pantalla" in t for t in sections),
                f"{cls} no detalla la pantalla actual",
            )

    def test_help_has_first_steps_and_close(self):
        """Siempre hay pasos iniciales y se explica cómo cerrar."""
        for cls in self.SCREENS + ["UnknownScreen"]:
            text = build_help_text(_fake_screen(cls)).lower()
            self.assertIn("enter", text)
            self.assertIn("esc", text)
            self.assertIn("cerrar", text)

    def test_help_mentions_common_fixes(self):
        """La ayuda orienta cuando algo falla (reproductor, guía...)."""
        text = build_help_text(None).lower()
        self.assertIn("mpv", text)
        self.assertIn("guía", text)

    def test_help_lines_fit_small_terminals(self):
        """Líneas cortas para que no se corten en ventanas estrechas."""
        for cls in self.SCREENS:
            for style, text in build_help_lines(_fake_screen(cls)):
                self.assertLessEqual(
                    len(text), 70,
                    f"línea demasiado larga en {cls}: {text!r}",
                )


if __name__ == "__main__":
    unittest.main()
