"""Tests de navegación por grupos (punto 3).

Cubre groups_of() y el filtrado de ChannelsScreen por grupo (incluido
el caso "sin grupo") sin necesidad de curses.
"""

import curses
import unittest

from thetvview.groups import groups_of
from thetvview.models import Channel, Playlist
from thetvview.ui.screens import ChannelsScreen, GroupsScreen


def ch(name: str, group: str | None = None) -> Channel:
    return Channel(name=name, url=f"http://x/{name}", group=group)


class _Fav:
    def is_favorite(self, channel: Channel) -> bool:
        return False


class _Status:
    def show(self, message: str, error: bool = False) -> None:
        self.message = message


class _Footer:
    def show(self, message: str, error: bool = False) -> None:
        pass


class _StubApp:
    favorites = _Fav()
    status = _Status()
    footer = _Footer()
    theme_name = "light"

    def __init__(self, max_y: int = 24, max_x: int = 80) -> None:
        self.stdscr = type("T", (), {"getmaxyx": lambda s: (max_y, max_x)})()

    def body_height(self) -> int:
        return 10


class TestGroupsOf(unittest.TestCase):
    def test_agrupa_y_cuenta(self):
        channels = [ch("A", "Deportes"), ch("B", "Noticias"), ch("C", "Deportes")]
        groups = groups_of(channels)
        self.assertEqual(len(groups["Deportes"]), 2)
        self.assertEqual(len(groups["Noticias"]), 1)

    def test_orden_alfabetico_de_grupos(self):
        channels = [ch("A", "Zeta"), ch("B", "Alpha")]
        self.assertEqual(list(groups_of(channels)), ["Alpha", "Zeta"])

    def test_sin_grupo_va_al_final_con_clave_none(self):
        channels = [ch("Libre"), ch("A", "Alpha"), ch("Otro libre")]
        keys = list(groups_of(channels))
        self.assertEqual(keys[-1], None)
        self.assertEqual(keys[0], "Alpha")
        self.assertEqual(len(groups_of(channels)[None]), 2)

    def test_lista_vacia(self):
        self.assertEqual(groups_of([]), {})


class TestChannelsScreenGroupFilter(unittest.TestCase):
    def setUp(self):
        self.playlist = Playlist(
            name="Test",
            channels=[
                ch("Dep 1", "Deportes"),
                ch("Dep 2 HD", "Deportes"),
                ch("Nac 1", "Noticias"),
                ch("Sin grupo"),
            ],
        )

    def test_filtro_por_grupo(self):
        screen = ChannelsScreen(_StubApp(), self.playlist, group="Deportes")
        self.assertEqual(len(screen.channels), 2)
        self.assertTrue(all(c.group == "Deportes" for c in screen.channels))
        self.assertIn("[Deportes]", screen._title_text())

    def test_sin_grupo_muestra_todos_por_defecto(self):
        screen = ChannelsScreen(_StubApp(), self.playlist)
        self.assertEqual(len(screen.channels), 4)
        self.assertFalse(screen.group_filtered)

    def test_filtro_sin_grupo_solo_canales_libres(self):
        screen = ChannelsScreen(_StubApp(), self.playlist, group=None)
        self.assertTrue(screen.group_filtered)
        self.assertEqual([c.name for c in screen.channels], ["Sin grupo"])
        self.assertIn("sin grupo", screen._title_text().lower())

    def test_busqueda_dentro_del_grupo(self):
        screen = ChannelsScreen(_StubApp(), self.playlist, group="Deportes")
        screen.query = "hd"
        screen._apply_filter()
        self.assertEqual(screen.current_channel().name, "Dep 2 HD")

    def test_grupo_inexistente_queda_vacio_pero_no_explota(self):
        screen = ChannelsScreen(_StubApp(), self.playlist, group="Fantasma")
        self.assertEqual(screen.channels, [])
        self.assertIsNone(screen.current_channel())

    def test_tecla_g_avisa_si_la_playlist_no_tiene_grupos(self):
        from thetvview.models import Channel as Ch

        solo = Playlist(name="Sola", channels=[Ch(name="A", url="http://a")])
        screen = ChannelsScreen(_StubApp(), solo)
        action = screen.handle_key(ord("g"))
        self.assertIsNone(action)
        self.assertIn("no tiene grupos", screen.app.status.message)

    def test_tecla_g_devuelve_accion_con_grupos(self):
        screen = ChannelsScreen(_StubApp(), self.playlist)
        action = screen.handle_key(ord("g"))
        self.assertEqual(action["action"], "show_groups")
        self.assertIs(action["playlist"], self.playlist)


class TestGroupsScreenSearch(unittest.TestCase):
    def setUp(self):
        self.playlist = Playlist(
            name="Test",
            channels=[
                ch("Dep 1", "Deportes"),
                ch("Dep 2 HD", "Deportes"),
                ch("Nac 1", "Noticias"),
                ch("Libre"),
            ],
        )
        self.app = _StubApp()

    def test_tecla_slash_activa_busqueda(self):
        screen = GroupsScreen(self.app, self.playlist)
        action = screen.handle_key(ord("/"))
        self.assertIsNone(action)
        self.assertTrue(screen.searching)

    def test_filtra_grupos_por_nombre(self):
        screen = GroupsScreen(self.app, self.playlist)
        screen.searching = True
        for c in "dep":
            screen.handle_key(ord(c))
        self.assertEqual(len(screen.visible_keys), 1)
        self.assertEqual(screen.visible_keys[0], "Deportes")

    def test_filtra_sin_grupo(self):
        screen = GroupsScreen(self.app, self.playlist)
        screen.searching = True
        for c in "sin":
            screen.handle_key(ord(c))
        self.assertEqual(len(screen.visible_keys), 1)
        self.assertIsNone(screen.visible_keys[0])

    def test_enter_confirma_sale_busqueda(self):
        screen = GroupsScreen(self.app, self.playlist)
        screen.searching = True
        for c in "dep":
            screen.handle_key(ord(c))
        screen.handle_key(curses.KEY_ENTER)
        self.assertFalse(screen.searching)
        self.assertEqual(len(screen.visible_keys), 1)

    def test_esc_limpia_y_sale(self):
        screen = GroupsScreen(self.app, self.playlist)
        screen.searching = True
        for c in "dep":
            screen.handle_key(ord(c))
        screen.handle_key(27)
        self.assertFalse(screen.searching)
        self.assertEqual(screen.query, "")
        self.assertEqual(len(screen.visible_keys), len(screen.keys))

    def test_backspace_borra(self):
        screen = GroupsScreen(self.app, self.playlist)
        screen.searching = True
        for c in "dep":
            screen.handle_key(ord(c))
        self.assertEqual(len(screen.visible_keys), 1)
        screen.handle_key(curses.KEY_BACKSPACE)
        self.assertEqual(screen.query, "de")
        self.assertEqual(len(screen.visible_keys), 1)
        screen.handle_key(curses.KEY_BACKSPACE)
        screen.handle_key(curses.KEY_BACKSPACE)
        self.assertEqual(screen.query, "")
        self.assertEqual(len(screen.visible_keys), len(screen.keys))

    def test_case_insensitive(self):
        screen = GroupsScreen(self.app, self.playlist)
        screen.searching = True
        for c in "DEPORTES":
            screen.handle_key(ord(c))
        self.assertEqual(len(screen.visible_keys), 1)
        self.assertEqual(screen.visible_keys[0], "Deportes")

    def test_shortcuts_cambia_con_searching(self):
        screen = GroupsScreen(self.app, self.playlist)
        self.assertIn("/ Buscar", screen.shortcuts())
        screen.searching = True
        self.assertIn("Escribir filtra", screen.shortcuts())

    def test_reload_respeta_filtro(self):
        screen = GroupsScreen(self.app, self.playlist)
        screen.query = "dep"
        screen.searching = True
        screen.reload()
        self.assertEqual(screen.query, "dep")
        self.assertEqual(len(screen.visible_keys), 1)

    def test_empty_result_no_explota(self):
        screen = GroupsScreen(self.app, self.playlist)
        screen.searching = True
        for c in "xyz":
            screen.handle_key(ord(c))
        self.assertEqual(len(screen.visible_keys), 0)
        self.assertIsNone(screen.current_group())

    def test_current_group_sobre_visible_keys(self):
        screen = GroupsScreen(self.app, self.playlist)
        screen.searching = True
        for c in "not":
            screen.handle_key(ord(c))
        self.assertEqual(screen.current_group(), "Noticias")

    def test_navegacion_grid_en_busqueda(self):
        """Teclas de dirección navegan sobre visible_keys."""
        screen = GroupsScreen(self.app, self.playlist)
        screen.handle_key(ord("/"))  # Activa searching=True
        # Sin filtro, hay 3 grupos: Deportes, Noticias, None
        screen.selected = 0
        screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(screen.selected, 1)
        screen.handle_key(curses.KEY_UP)
        self.assertEqual(screen.selected, 0)


if __name__ == "__main__":
    unittest.main()
