"""La pantalla de bienvenida (FR-HOM-01…10).

La regla que gobierna esta pantalla es corta y no se negocia: **sólo aparece
cuando no hay listas**. Con listas configuradas, la raíz es el catálogo, como
siempre; meter una pantalla intermedia obligaría a quien ya sabe lo que hace a
pulsar «Enter» para ver lo mismo (FR-HOM-06, D13).

Y lo que promete la pantalla tiene que ser verdad: cada acción que anuncia se
puede ejecutar de verdad (FR-HOM-02), y no enseña ninguna cifra que no pueda
calcular (FR-HOM-04).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.playlist_manager import PlaylistEntry
from thetvview.ui import colors
from thetvview.ui.actions import V
from thetvview.ui.app import App, build_help_lines
from thetvview.ui.screens import HomeScreen, PlaylistsScreen
from thetvview.ui.textwidth import cell_width


class VentanaCaptura:
    """Falla ruidosamente si se escribe fuera de la pantalla (AC-13)."""

    def __init__(self, max_y: int = 24, max_x: int = 80) -> None:
        self.max_y = max_y
        self.max_x = max_x
        self.calls: list[tuple[int, int, str, int]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return (self.max_y, self.max_x)

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        assert 0 <= y < self.max_y, f"fila {y} fuera de {self.max_y}"
        assert 0 <= x < self.max_x, f"columna {x} fuera de {self.max_x}"
        assert x + cell_width(text) <= self.max_x, (
            f"desborde: {x}+{cell_width(text)} > {self.max_x}: {text!r}"
        )
        self.calls.append((y, x, text, attr))

    def erase(self) -> None:
        self.calls.clear()

    def texto(self) -> str:
        return " ".join(c[2] for c in self.calls)


class BaseApp(unittest.TestCase):
    def setUp(self) -> None:
        self.ventana = VentanaCaptura()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        tmp = Path(self.dir.name)
        for nombre in ("PLAYLISTS_JSON", "FAVORITES_JSON", "PREFS_JSON",
                       "RECENTS_JSON", "THEME_JSON"):
            p = mock.patch.object(__import__("thetvview.config", fromlist=["x"]),
                                  nombre, tmp / f"{nombre.lower()}.json")
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("thetvview.config.ensure_dirs", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        self.app = App(self.ventana)  # type: ignore[arg-type]


class TestRaizSegunEstadoDelCatalogo(BaseApp):
    def test_sin_listas_la_raiz_es_la_bienvenida(self):
        self.assertIsInstance(self.app.screen, HomeScreen)
        self.assertEqual(len(self.app.stack), 1)

    def test_con_listas_la_raiz_es_el_catalogo(self):
        self.app.playlists.add("Test", "http://example.test/p.m3u")
        app = App(self.ventana)  # type: ignore[arg-type]
        self.assertIsInstance(app.screen, PlaylistsScreen)

    def test_un_catalogo_roto_no_tumba_el_arranque(self):
        self.app.playlists.load = mock.Mock(side_effect=OSError("disco roto"))
        app = App(self.ventana)  # type: ignore[arg-type]
        self.assertIsInstance(app.screen, HomeScreen)

    def test_reload_cambia_a_catalogo_cuando_aparece_una_lista(self):
        """Añadir la primera lista desde la bienvenida lleva al sitio útil."""
        inicio = self.app.screen
        self.assertIsInstance(inicio, HomeScreen)
        self.app.playlists.add("Test", "http://example.test/p.m3u")
        inicio.reload()
        self.assertIsInstance(self.app.screen, PlaylistsScreen)
        self.assertEqual(len(self.app.stack), 1)

    def test_reload_sin_listas_no_inventa_nada(self):
        self.app.screen.reload()
        self.assertIsInstance(self.app.screen, HomeScreen)

    def test_esc_en_la_raiz_no_saca_de_la_app(self):
        self.assertFalse(self.app.pop())


class TestAccionesEjecutables(BaseApp):
    #: `?` es global: la atiende `App._tecla_global` antes que cualquier
    #: pantalla, y eso es correcto —no es una acción de la pantalla.
    GLOBALES = {"?"}

    def test_solo_anuncia_acciones_que_se_pueden_ejecutar(self):
        self.app.favorites.load = lambda: []
        self.app.recents.load = lambda: []
        pantalla = HomeScreen(self.app)
        for accion in pantalla.actions():
            if accion.key in self.GLOBALES:
                continue
            with self.subTest(tecla=accion.key):
                resultado = pantalla.handle_key(ord(accion.key))
                self.assertIsNotNone(resultado,
                                     f"{accion.key} se anuncia pero no hace nada")

    def test_la_ayuda_global_atiende_el_signo_de_interrogacion(self):
        with mock.patch.object(self.app, "_show_help") as ayuda:
            self.app._despachar_tecla(ord("?"))
            ayuda.assert_called_once()

    def test_a_va_al_alta_de_lista_de_verdad(self):
        """Un solo camino de alta: el mismo que usa el catálogo."""
        self.app.favorites.load = lambda: []
        self.app.recents.load = lambda: []
        accion = HomeScreen(self.app).handle_key(ord("a"))
        self.assertEqual(accion, {"action": "add_playlist"})
        with mock.patch.object(self.app, "add_playlist") as alta:
            self.app.handle_action(accion)
            alta.assert_called_once()

    def test_q_pide_confirmar_la_salida(self):
        with mock.patch.object(self.app, "confirm_quit", return_value=False) as salir:
            self.app.handle_action({"action": "confirm_quit"})
            salir.assert_called_once()
            self.assertTrue(self.app._salir_pendiente is False)

    def test_el_catalogo_de_acciones_es_el_del_vocabulario_cerrado(self):
        self.app.favorites.load = lambda: []
        self.app.recents.load = lambda: []
        etiquetas = {a.label for a in HomeScreen(self.app).actions()}
        self.assertTrue(etiquetas <= {v for v in vars(V).values() if isinstance(v, str)})

    def test_la_ayuda_sabe_donde_esta(self):
        lineas = build_help_lines(HomeScreen(self.app))
        secciones = [t for s, t in lineas if s == "section"]
        self.assertTrue(any("Estás en" in t for t in secciones))
        self.assertTrue(any("Bienvenida" in t for t in secciones))

    def test_la_ayuda_no_tiene_lineas_largas(self):
        for estilo, texto in build_help_lines(HomeScreen(self.app)):
            self.assertLessEqual(len(texto), 70, f"línea demasiado larga: {texto!r}")


class TestPresentacionDeLaBienvenida(BaseApp):
    def _pintar(self, alto: int = 24, ancho: int = 80) -> VentanaCaptura:
        ventana = VentanaCaptura(alto, ancho)
        self.app.favorites.load = lambda: []
        self.app.recents.load = lambda: []
        pantalla = HomeScreen(self.app)
        with mock.patch.object(colors, "pair", return_value=0):
            pantalla.render(ventana)
        return ventana

    def test_dice_quien_es(self):
        texto = self._pintar().texto()
        self.assertIn("theTVVIEW", texto)

    def test_explica_que_pasar(self):
        texto = self._pintar().texto()
        self.assertIn("Añadir", texto)

    def test_no_inventa_cifras_de_ejemplo(self):
        """FR-HOM-04: sin datos, no hay números; con datos, los reales."""
        texto = self._pintar().texto()
        for cifra in ("1000", "10 000", "500", "3000"):
            self.assertNotIn(cifra, texto, f"cifra de ejemplo en pantalla: {cifra}")

    def test_terminal_estrecha_no_lanza(self):
        for alto, ancho in ((40, 40), (24, 40), (10, 40), (20, 60), (10, 20)):
            with self.subTest(tam=(alto, ancho)):
                ventana = self._pintar(alto, ancho)
                self.assertTrue(ventana.calls, f"no se pintó nada en {alto}x{ancho}")

    def test_terminal_diminuta_no_lanza(self):
        for alto, ancho in ((1, 1), (2, 5), (3, 12)):
            with self.subTest(tam=(alto, ancho)):
                self._pintar(alto, ancho)

    def test_no_toca_red_ni_disco_al_pintar(self):
        """FR-HOM-10: la falta de listas es un estado normal, no un error."""
        with mock.patch.object(
            self.app.playlists, "load", side_effect=AssertionError("no debe releer")
        ), mock.patch(
            "thetvview.epg_parser.parse_file", side_effect=AssertionError("no red")
        ):
            self._pintar()


class TestCatalogoSinListasSigueIgual(BaseApp):
    """La otra mitad de D13: el catálogo vacío no se ha tocado."""

    def test_el_catalogo_vacio_sigue_siendo_el_catalogo(self):
        pantalla = PlaylistsScreen(self.app)
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            pantalla.render(ventana)
        self.assertIn("No hay playlists", ventana.texto())

    def test_una_lista_entra_por_el_camino_de_siempre(self):
        self.app.playlists.add("Test", "http://example.test/p.m3u")
        app = App(self.ventana)  # type: ignore[arg-type]
        pantalla = app.screen
        self.assertIsInstance(pantalla, PlaylistsScreen)
        entradas = pantalla.load() if hasattr(pantalla, "load") else pantalla.entries
        self.assertEqual([e.name for e in entradas], ["Test"])


class TestHelpSinPantalla(unittest.TestCase):
    def test_la_ayuda_global_sigue_explicando_los_tres_temas(self):
        from thetvview.ui.app import build_help_text

        texto = build_help_text(None).lower()
        self.assertIn("alto contraste", texto)


if __name__ == "__main__":
    unittest.main()