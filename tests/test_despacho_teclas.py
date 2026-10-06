"""Tests del **despacho** de teclas: quién atiende cada pulsación.

Existe porque aquí estaba el agujero que dejó vivo un bug: todos los tests
probaban `Screen.handle_key` —que estaba bien escrito— y **nadie probaba quién
lo llama**. `App.run` interceptaba `r` antes de delegar, así que en Recientes
`r Limpiar` no limpiaba nada: en su lugar se apilaba otra pantalla Recientes
(cada una releyendo `recents.json`), y la tecla quedaba engañosa.

Lo que se fija aquí es el **reparto**, no las pantallas:

1. una tecla global cede si la pantalla la declara en `actions()` (§10);
2. las globales que ya funcionaban siguen funcionando igual;
3. mientras se escribe, las globales van al campo de texto;
4. ninguna tecla puede hacer que la app se quede sin pantalla ni con dos.

La invariante que ata (1) con el resto del modelo —`keys(actions()) ⊆
handle_key()`— está en `tests/test_actions_por_pantalla.py`; aquí se comprueba
que el reparto **usa** ese catálogo y no una lista de pantallas escrita a mano.
"""

from __future__ import annotations

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview import config
from thetvview.epg_parser import Epg
from thetvview.models import Channel, Playlist
from thetvview.recents import RecentItem
from thetvview.ui import screens as ui_screens
from thetvview.ui.app import App


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeStdscr:
    def __init__(self, h=24, w=80) -> None:
        self.h = h
        self.w = w
        self.addstr_calls: list[tuple[int, int, str, int]] = []

    def getmaxyx(self):
        return (self.h, self.w)

    def addstr(self, y, x, text, attr=0):
        room = self.w - 1 if x == 0 else self.w - x
        if room > 0 and len(text) > room:
            text = text[:room]
        self.addstr_calls.append((y, x, text, attr))

    def erase(self):
        self.addstr_calls.clear()

    def refresh(self):
        pass

    def timeout(self, ms):
        pass


class FavoritesFalsos:
    def __init__(self, favs=()) -> None:
        self.favs = set(favs)

    def is_favorite(self, channel):
        return channel.name in self.favs

    def favorite_urls(self):
        return {"http://x/" + n for n in self.favs}

    def load(self):
        return [Channel(name=n, url="http://x/" + n) for n in sorted(self.favs)]

    def toggle(self, channel):
        if channel.name in self.favs:
            self.favs.discard(channel.name)
            return False
        self.favs.add(channel.name)
        return True


class RecentsFalsos:
    """Doble de `RecentsManager`: cuenta `clear()` sin tocar disco."""

    def __init__(self, items=()) -> None:
        self.items = list(items)
        self.limpiadas = 0

    def load(self):
        return self.items

    def clear(self):
        self.items = []
        self.limpiadas += 1


def item_reciente(nombre="La 1") -> RecentItem:
    return RecentItem(name=nombre, url="http://x/" + nombre, group="N", player="mpv")


def playlist_canales() -> Playlist:
    return Playlist(
        name="Test",
        channels=[
            Channel(name="La 1", url="http://x/La 1", group="N"),
            Channel(name="La 2", url="http://x/La 2", group="D"),
        ],
    )


class BaseDespacho(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeStdscr()
        tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(config, "PLAYLISTS_JSON", tmp / "p.json"),
            mock.patch.object(config, "FAVORITES_JSON", tmp / "f.json"),
            mock.patch.object(config, "PREFS_JSON", tmp / "prefs.json"),
            mock.patch.object(config, "RECENTS_JSON", tmp / "r.json"),
            mock.patch.object(config, "THEME_JSON", tmp / "theme.json"),
            mock.patch.object(config, "ensure_dirs", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = App(self.fake)
        self.app.favorites = FavoritesFalsos({"La 1"})
        self.app.recents = RecentsFalsos([item_reciente(), item_reciente("La 2")])

    def pulsar(self, key: str | int) -> bool:
        """Pulsa una tecla por **el mismo camino que el usuario**.

        No reimplementa nada: llama a `_despachar_tecla`, que es lo que
        invoca `App.run`. Devuelve si la app pidió cerrar.
        """
        code = ord(key) if isinstance(key, str) and len(key) == 1 else key
        return self.app._despachar_tecla(code)

    def en_raiz(self, screen) -> None:
        """Coloca `screen` en el top del stack (como tras un `push`)."""
        self.app.stack[-1] = screen


# ---------------------------------------------------------------------------
# 1. El bug: `r Limpiar` en Recientes
# ---------------------------------------------------------------------------


class TestDespachoReal(BaseDespacho):
    def test_r_en_recientes_limpia_y_no_apila(self):
        """`r` en Recientes limpia el historial y NO apila otra pantalla.

        Antes: `r` se interceptaba como global, se empujaba otro
        `RecentsScreen` y el historial nunca se limpiaba.
        """
        recents = ui_screens.RecentsScreen(self.app)
        self.en_raiz(recents)
        self.assertEqual(len(self.app.stack), 1)

        self.pulsar("r")

        self.assertEqual(self.app.recents.limpiadas, 1,
                         "r Limpiar tiene que llegar a RecentsScreen")
        self.assertEqual(self.app.recents.items, [])
        self.assertEqual(len(self.app.stack), 1,
                         "r no debe apilar pantallas: se acumulaban sin fin")
        self.assertIs(self.app.screen, recents)

    def test_r_repetido_no_hace_nada_mas(self):
        recents = ui_screens.RecentsScreen(self.app)
        self.en_raiz(recents)
        for _ in range(5):
            self.pulsar("r")
        self.assertEqual(len(self.app.stack), 1)
        self.assertEqual(self.app.recents.items, [])

    def test_r_en_recientes_vacio_no_apila(self):
        self.app.recents.items = []
        recents = ui_screens.RecentsScreen(self.app)
        self.en_raiz(recents)
        self.pulsar("r")
        self.assertEqual(len(self.app.stack), 1)

    def test_r_en_otra_pantalla_sigue_abriendo_recientes(self):
        """Lo que sí funcionaba no se toca: fuera de Recientes, `r` es global."""
        for nombre, screen in (
            ("Canales", ui_screens.ChannelsScreen(self.app, playlist_canales())),
            ("Grupos", ui_screens.GroupsScreen(self.app, playlist_canales())),
            ("Favoritos", ui_screens.FavoritesScreen(self.app)),
        ):
            with self.subTest(pantalla=nombre):
                self.app.stack = [screen]
                self.pulsar("r")
                self.assertIsInstance(self.app.screen, ui_screens.RecentsScreen)
                self.assertEqual(self.app.recents.limpiadas, 0,
                                 "r fuera de Recientes no limpia nada")

    def test_r_en_guia_recarga_y_no_abre_recientes(self):
        """La guía reclama `r` por catálogo; el `isinstance` especial ya no hace falta."""
        self.app.epg = Epg(programs={}, channels_by_id={})
        llamadas = []

        def ensure_epg(channel, *, force_refresh=False, url_hint=None):
            llamadas.append(force_refresh)
            return []

        self.app.ensure_epg = ensure_epg  # type: ignore[method-assign]
        guia = ui_screens.EpgScreen(self.app, Channel(name="La 1", url="http://x/La 1"))
        self.en_raiz(guia)

        self.pulsar("r")

        self.assertTrue(llamadas, "r en la guía tiene que recargar el EPG")
        self.assertNotIsInstance(self.app.screen, ui_screens.RecentsScreen)


# ---------------------------------------------------------------------------
# 2. El reparto se decide por catálogo, no por una lista de pantallas
# ---------------------------------------------------------------------------


class TestRepartoPorCatalogo(BaseDespacho):
    class _ReclamaR(ui_screens.Screen):
        """Pantalla que declara `r` pero **no** lo atiende (debería fallar)."""

        title = "X"

        def actions(self):
            from thetvview.ui.actions import Action, P, V

            return [Action("r", V.LIMPIAR, P.ORGANIZACION)]

        def handle_key(self, key):
            return None

        def render(self, stdscr):
            pass

    def test_el_reparto_usa_actions_no_isinstance(self):
        """El criterio es el catálogo, no el nombre de la clase."""
        pantalla = self._ReclamaR(self.app)
        self.en_raiz(pantalla)
        self.assertTrue(self.app._tecla_es_de_la_pantalla("r"))

        # Y una pantalla que no declara `r` no lo reclama, sea la que sea.
        canales = ui_screens.ChannelsScreen(self.app, playlist_canales())
        self.en_raiz(canales)
        self.assertFalse(self.app._tecla_es_de_la_pantalla("r"))

    def test_una_reclamacion_falsa_no_pasa_el_despacho(self):
        """Una pantalla que declara `r` pero no lo atiende se traga la tecla.

        No se le encaja la global a la fuerza: si la pantalla dice que `r` es
        suya, la global no la pisa. Por eso la otra mitad de la Guarantee —que
        una pantalla no pueda declarar lo que no hace— **no** es opcional:
        vive en `tests/test_actions_por_pantalla.py` (`keys ⊆ handle_key`) y
        es la que hace este caso imposible en producción.
        """
        pantalla = self._ReclamaR(self.app)
        self.en_raiz(pantalla)
        self.pulsar("r")
        self.assertEqual(len(self.app.stack), 1,
                         "la global no debe pisar a quien reclama la tecla")
        self.assertIs(self.app.screen, pantalla)

    def test_toda_tecla_declarada_llega_a_la_pantalla(self):
        """Cruce de las dos invariantes: declarada ⇒ despachada a la pantalla.

        Es la versión ejecutable del bug que acabamos de cerrar: se espía
        `handle_key` y se comprueba que la tecla que la pantalla declara llega
        hasta ella. Con el reparto viejo, `r` en Recientes nunca llegaba.
        """
        pantallas = {
            "Canales": ui_screens.ChannelsScreen(self.app, playlist_canales()),
            "Grupos": ui_screens.GroupsScreen(self.app, playlist_canales()),
            "Favoritos": ui_screens.FavoritesScreen(self.app),
            "Recientes": ui_screens.RecentsScreen(self.app),
        }
        for nombre, screen in pantallas.items():
            with self.subTest(pantalla=nombre):
                self.en_raiz(screen)
                with mock.patch.object(
                    screen, "handle_key", wraps=screen.handle_key
                ) as atiende:
                    self.pulsar("r")
                if screen.actions() and any(a.key == "r" for a in screen.actions()):
                    self.assertEqual(
                        atiende.call_count, 1,
                        f"{nombre} declara `r` pero la tecla no le llegó",
                    )


# ---------------------------------------------------------------------------
# 3. Las globales que ya funcionaban, siguen igual
# ---------------------------------------------------------------------------


class TestGlobalesIntactas(BaseDespacho):
    def test_t_cambia_el_tema(self):
        # El color se inicializa dentro de curses.wrapper, así que aquí lo que
        # se comprueba es el reparto: `t` es global y la llega a la app.
        with mock.patch.object(self.app, "toggle_theme") as cambiar:
            self.pulsar("t")
        cambiar.assert_called_once()

    def test_interrogante_abre_ayuda(self):
        with mock.patch.object(self.app, "_show_help") as ayuda:
            self.pulsar("?")
        ayuda.assert_called_once()

    def test_escape_sube_en_el_stack(self):
        canales = ui_screens.ChannelsScreen(self.app, playlist_canales())
        self.app.push(canales)
        self.assertEqual(len(self.app.stack), 2)
        self.pulsar(27)
        self.assertEqual(len(self.app.stack), 1)

    def test_escape_en_raiz_no_rompe(self):
        self.pulsar(27)
        self.assertEqual(len(self.app.stack), 1)

    def test_u_deshace(self):
        self.app._undo_stack.append({"action": "show_recents"})
        self.pulsar("u")
        self.assertEqual(self.app._undo_stack, [])
        self.assertIsInstance(self.app.screen, ui_screens.RecentsScreen)

    def test_u_sin_historial_no_rompe(self):
        self.pulsar("u")
        self.assertIn("deshacer", self.app.footer.message.lower())

    def test_exclamacion_pasa_el_security_check(self):
        with mock.patch.object(self.app, "run_security_check") as sc:
            self.pulsar("!")
        sc.assert_called_once()

    def test_q_en_raiz_pide_confirmacion_de_salida(self):
        with mock.patch.object(self.app, "confirm_quit", return_value=False):
            self.assertFalse(self.pulsar("q"))
        with mock.patch.object(self.app, "confirm_quit", return_value=True):
            self.assertTrue(self.pulsar("q"), "q confirmado debe cerrar la app")

    def test_q_en_reproduciendo_detiene_en_vez_de_salir(self):
        with mock.patch.object(self.app, "confirm_quit") as salir:
            pantalla = mock.Mock()
            pantalla.handle_key.return_value = {"action": "stop_playback"}
            pantalla.is_alive.return_value = True
            self.app.stack[-1] = pantalla
            self.pulsar("q")
        salir.assert_not_called()

    def test_una_tecla_sin_global_llega_a_la_pantalla(self):
        self.app._undo_stack.append({"action": "show_recents"})
        self.app.stack = [ui_screens.PlaylistsScreen(self.app)]
        self.pulsar("u")
        self.assertEqual(len(self.app.stack), 2)

    def test_tecla_rara_no_rompe(self):
        self.assertFalse(self.pulsar("z"))


# ---------------------------------------------------------------------------
# 4. Mientras se escribe, las globales van al campo
# ---------------------------------------------------------------------------


class TestBusquedaNoSeComeLasGlobales(BaseDespacho):
    def test_r_durante_la_busqueda_no_abre_recientes(self):
        canales = ui_screens.ChannelsScreen(self.app, playlist_canales())
        canales.searching = True
        self.en_raiz(canales)
        self.pulsar("r")
        self.assertEqual(len(self.app.stack), 1)
        self.assertEqual(canales.query, "r", "la tecla tiene que ir al campo")

    def test_t_durante_la_busqueda_no_llega_a_la_app(self):
        canales = ui_screens.ChannelsScreen(self.app, playlist_canales())
        canales.searching = True
        self.en_raiz(canales)
        with mock.patch.object(self.app, "toggle_theme") as cambiar:
            self.pulsar("t")
        cambiar.assert_not_called()
        self.assertEqual(canales.query, "t")

    def test_escape_durante_la_busqueda_sale_de_la_busqueda(self):
        """`Esc` sí sale de búsqueda: es lo que la propia pantalla atiende."""
        canales = ui_screens.ChannelsScreen(self.app, playlist_canales())
        canales.searching = True
        canales.query = "la 1"
        self.app.push(canales)
        self.pulsar(27)
        self.assertFalse(canales.searching)
        self.assertEqual(canales.query, "")
        self.assertEqual(len(self.app.stack), 2, "no debe cerrar la app desde la búsqueda")


if __name__ == "__main__":
    unittest.main()