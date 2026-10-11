"""Tests del tema vistoso: paleta dark violeta, NO_COLOR, focus/scrollbar."""

import curses
import os
import unittest
from unittest import mock

from thetvview.ui import colors, theme


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

    def test_pair_secondary_es_26(self):
        self.assertEqual(colors.PAIR_SECONDARY, 26)


# ---------------------------------------------------------------------------
# El tercer tema (FR-THE-01…09)
# ---------------------------------------------------------------------------


class TestTresTemas(unittest.TestCase):
    """Los tres perfiles existen, se degradan bien y no pisan los otros dos."""

    def test_existen_exactamente_tres(self):
        from thetvview.config import THEMES

        self.assertEqual(THEMES, ("light", "dark", "high_contrast"))

    def test_high_contrast_tiene_las_mismas_claves_que_los_otros(self):
        """`init_colors` indexa por rol: faltar una clave es un crash en runtime."""
        from thetvview.ui.theme import _THEMES

        base = set(_THEMES["light"])
        for nombre in _THEMES:
            with self.subTest(tema=nombre):
                self.assertEqual(set(_THEMES[nombre]), base)

    def test_todos_los_temas_inicializan_sin_excepcion(self):
        from thetvview.ui.theme import init_colors

        for nombre in ("light", "dark", "high_contrast"):
            for colores_256 in (8, 256):
                with self.subTest(tema=nombre, colores=colores_256):
                    with mock.patch("curses.has_colors", return_value=True), \
                         mock.patch("curses.start_color"), \
                         mock.patch("curses.use_default_colors"), \
                         mock.patch.object(theme, "_colores_disponibles",
                                           return_value=colores_256), \
                         mock.patch.object(theme, "_pares_disponibles",
                                           return_value=64), \
                         mock.patch("curses.init_pair"):
                        init_colors(nombre)

    def test_todos_los_temas_degradan_sin_color(self):
        from thetvview.ui.theme import init_colors

        for nombre in ("light", "dark", "high_contrast"):
            with self.subTest(tema=nombre):
                with mock.patch("curses.has_colors", return_value=False):
                    init_colors(nombre)
                with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
                    init_colors(nombre)

    def test_tema_desconocido_degrada_al_predeterminado(self):
        from thetvview.ui.theme import init_colors

        with mock.patch("curses.has_colors", return_value=False):
            init_colors("neon_underground")  # no lanza

    def test_sin_pares_suficientes_inicializa_los_que_caben(self):
        """T-THE-02: terminal de 8 pares → se inicializan los que caben y se sigue.

        `init_pair` lanza `curses.error` en cuanto se le pide uno de más, así que
        la pregunta no es «¿lanza?» sino «¿sigue funcionando?».
        """
        from thetvview.ui.theme import init_colors

        for nombre in ("light", "dark", "high_contrast"):
            with self.subTest(tema=nombre):
                with mock.patch("curses.has_colors", return_value=True), \
                     mock.patch("curses.start_color"), \
                     mock.patch("curses.use_default_colors"), \
                     mock.patch.object(theme, "_pares_disponibles",
                                       return_value=8), \
                     mock.patch("curses.init_pair") as init:
                    init_colors(nombre)
                init.assert_called()
                # Nunca pidió el par 8: se paró en el límite del terminal.
                pedidos = {c.args[0] for c in init.call_args_list}
                self.assertTrue(all(p <= 8 for p in pedidos), pedidos)

    def test_sin_conocer_los_pares_se_inicializa_el_primer_par(self):
        """Antes de `initscr` no se sabe cuántos hay: se inicializa lo básico."""
        from thetvview.ui.theme import init_colors

        with mock.patch("curses.has_colors", return_value=True), \
             mock.patch("curses.start_color"), \
             mock.patch("curses.use_default_colors"), \
             mock.patch.object(theme, "_pares_disponibles", return_value=0), \
             mock.patch("curses.init_pair"):
            init_colors("dark")  # no lanza

    def test_alto_contrase_apila_seleccion_invertida(self):
        """La selección tiene que verse sin depender del tono (FR-THE-06)."""
        from thetvview.ui.theme import _THEMES

        hc = _THEMES["high_contrast"]
        self.assertNotEqual(hc["selected_fg"], hc["selected_bg"])

    def test_light_y_dark_no_cambian(self):
        """El tercer tema se **añade**: los otros dos siguen exactamente igual."""
        from thetvview.ui.theme import _THEMES

        self.assertEqual(_THEMES["light"]["primary"][0], 21)
        self.assertEqual(_THEMES["light"]["bg"], -1)
        self.assertEqual(_THEMES["dark"]["primary"][0], 141)
        self.assertEqual(_THEMES["dark"]["primary_hi"][0], 177)
        self.assertEqual(_THEMES["dark"]["accent"][0], 215)
        self.assertEqual(_THEMES["dark"]["title_bg"][0], 141)
        self.assertEqual(_THEMES["dark"]["selected_bg"][0], 141)
        self.assertEqual(_THEMES["dark"]["border"][0], 240)

    def test_token_secondary_existe_en_los_tres(self):
        from thetvview.ui.theme import _THEMES

        for nombre in _THEMES:
            with self.subTest(tema=nombre):
                self.assertIn("secondary", _THEMES[nombre])

    def test_attr_reconoce_secondary(self):
        from thetvview.ui.theme import attr

        with mock.patch.object(curses, "color_pair", side_effect=lambda n: n or 0):
            self.assertEqual(attr("secondary"), colors.PAIR_SECONDARY)


class TestPersistenciaDelTema(unittest.TestCase):
    """`theme.json` con los tres, y vuelta a leerlo (FR-THE-08, NFR-07)."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from thetvview import config

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ruta = Path(self.dir.name) / "theme.json"
        p = mock.patch.object(config, "THEME_JSON", self.ruta)
        p.start()
        self.addCleanup(p.stop)
        self.config = config

    def test_alto_contraste_se_guarda_y_se_relee(self):
        self.config.save_theme("high_contrast")
        self.assertEqual(self.config.load_theme(), "high_contrast")

    def test_los_tres_sobreviven_al_viaje_de_ida_y_vuelta(self):
        for nombre in self.config.THEMES:
            with self.subTest(tema=nombre):
                self.config.save_theme(nombre)
                self.assertEqual(self.config.load_theme(), nombre)

    def test_un_tema_inventado_no_se_persiste(self):
        self.config.save_theme("neon")
        self.assertFalse(self.ruta.exists())
        self.assertEqual(self.config.load_theme(), "light")

    def test_tema_inventado_al_leer_degrada(self):
        self.ruta.write_text('{"theme": "neon"}', encoding="utf-8")
        self.assertEqual(self.config.load_theme(), "light")

    def test_mayusculas_y_espacios_se_normalizan(self):
        self.ruta.write_text('{"theme": "  HIGH_CONTRAST "}', encoding="utf-8")
        self.assertEqual(self.config.load_theme(), "high_contrast")


class TestCicloDeTema(unittest.TestCase):
    """`t` recorre los tres y persiste; `T` ofrece elegir (D12)."""

    def _app(self, test):
        import tempfile
        from pathlib import Path

        from thetvview import config
        from thetvview.ui.app import App

        tmp = Path(tempfile.mkdtemp())
        test.addCleanup(lambda: None)
        for nombre in ("PLAYLISTS_JSON", "FAVORITES_JSON", "PREFS_JSON",
                       "RECENTS_JSON", "THEME_JSON"):
            p = mock.patch.object(config, nombre, tmp / f"{nombre.lower()}.json")
            p.start()
            test.addCleanup(p.stop)
        p = mock.patch("thetvview.config.ensure_dirs", lambda: None)
        p.start()
        test.addCleanup(p.stop)
        return App(None), config  # type: ignore[arg-type]

    def test_el_ciclo_recorre_los_tres_y_vuelve(self):
        app, _config = self._app(self)
        inicio = app.theme_name
        vistos = []
        with mock.patch("curses.has_colors", return_value=False):
            for _ in range(3):
                app.toggle_theme()
                vistos.append(app.theme_name)
        self.assertEqual(len(set(vistos)), 3, vistos)
        self.assertEqual(app.theme_name, inicio)

    def test_el_tema_elegido_se_persiste_en_los_dos_sitios(self):
        app, config = self._app(self)
        with mock.patch("curses.has_colors", return_value=False):
            app.set_theme("high_contrast", avisar=False)
        self.assertEqual(app.theme_name, "high_contrast")
        self.assertEqual(config.load_theme(), "high_contrast")
        self.assertEqual(app.prefs.load().theme, "high_contrast")

    def test_el_aviso_del_tema_es_modal(self):
        """No negociable: cambiar el tema se avisa, y se avisa en un modal."""
        app, _config = self._app(self)
        with mock.patch("curses.has_colors", return_value=False), \
             mock.patch.object(app, "_show_notice") as aviso:
            app.toggle_theme()
        aviso.assert_called_once()
        self.assertIn("tema", aviso.call_args.args[1].lower())

    def test_el_modal_de_elegir_ofrece_los_tres(self):
        from thetvview.ui import theme
        from thetvview.ui.widgets import Modal

        app, _config = self._app(self)
        with mock.patch.object(app, "_run_modal", return_value=None) as runner:
            app.elegir_tema()
        modal = runner.call_args.args[0]
        self.assertIsInstance(modal, Modal)
        self.assertEqual(len(modal.buttons), 3)
        self.assertEqual(
            {theme.nombre_legible(n) for n in theme.nombres()},
            set(modal.buttons),
        )

    def test_una_eleccion_valida_se_aplica(self):
        from thetvview.ui import theme

        app, _config = self._app(self)
        elegido = theme.nombre_legible("high_contrast")
        with mock.patch("curses.has_colors", return_value=False), \
             mock.patch.object(app, "_run_modal", return_value=elegido.lower()):
            app.elegir_tema()
        self.assertEqual(app.theme_name, "high_contrast")


if __name__ == "__main__":
    unittest.main()
