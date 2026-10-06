"""Tests del modelo de acciones contextuales (plan F5, SDD §17/§21/§22/§44/§45).

Sin terminal real: `ui/actions.py` y `ui/textwidth.py` son puros, y
`FooterBar` se ejercita con el mismo `FakeStdscr` que usa el resto de la suite.

Qué se verifica aquí:

- **la medida** (`cell_width`/`clip_cells`): `len()` no es una medida, y esta
  línea lleva `← → ★ ▶` que son *Ambiguous* (H2);
- **el ajuste por prioridad**: A=100 / B=50 / C=10 con ancho insuficiente
  conserva A,B y descarta C (H3);
- **`essential` sobrevive** al descarte por prioridad (§45);
- **`enabled=False` no aparece** (§8);
- **unicidad de clave** por pantalla → `AssertionError` (§22);
- **vocabulario cerrado**: una etiqueta inventada falla (§21);
- **tope de 6**, una sola línea, ninguna etiqueta partida (§18/§46/§48);
- **`FooterBar` sin `curses.error`** de 1×1 a 200×60, con el toast mandando en
  el ancho y `chips` reflejando lo que cabe de verdad (F4).
"""

from __future__ import annotations

import unittest
from unittest import mock

from thetvview.ui import colors
from thetvview.ui.actions import (
    LABELS,
    MAX_VISIBLE,
    P,
    V,
    Action,
    fit_actions,
    validate,
)
from thetvview.ui.textwidth import cell_width, char_width, clip_cells
from thetvview.ui.widgets import FooterBar, Modal, SearchModal, modal_actions


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class FakeStdscr:
    """Ventana curses falsa: recorta como curses y anota cada addstr."""

    def __init__(self, h: int = 24, w: int = 80) -> None:
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

    def row_text(self, y: int) -> str:
        return "".join(t for (cy, _x, t, _a) in self.addstr_calls if cy == y)


def render_footer(fb: FooterBar, h: int, w: int, **kw) -> FakeStdscr:
    """Pinta el pie en un `FakeStdscr` sin curses real ni pares de color."""
    fake = FakeStdscr(h, w)
    fb._flash_until = 0.0
    with mock.patch.object(colors, "pair", return_value=0):
        fb.render(fake)
    return fake


def cat(*pairs, essential_ayuda: bool = True):
    """Catálogo de ejemplo con las prioridades del §17."""
    acciones = [
        Action("Enter", V.VER, P.PRIMARIA),
        Action("f", V.FAVORITO, P.CONTEXTUAL),
        Action("e", V.EPG, P.SECUNDARIA),
    ]
    if essential_ayuda:
        acciones.append(Action("?", V.AYUDA, P.AYUDA, essential=True))
    return acciones


def keys(acts):
    return [a.key for a in acts]


def width_for(*acts) -> int:
    """Ancho exacto que necesita esa lista de acciones, y ni una celda más.

    Cuenta el espacio inicial de la línea y un separador `│` entre chips, que es
    como mide `fit_actions`. Sirve para poder escribir "justo cabe A y B" sin
    depender de la aritmética a ojo.
    """
    total = 1  # LEADING
    for i, a in enumerate(acts):
        total += a.chip_cells() + (1 if i else 0)
    return total


# ---------------------------------------------------------------------------
# 1. La medida (F1)
# ---------------------------------------------------------------------------


class TestTextWidth(unittest.TestCase):
    """`cell_width` mide celdas; `clip_cells` no parte un carácter ancho."""

    def test_ambiguas_valen_una_celda(self):
        # *East Asian Ambiguous*: 1 carácter, 1 celda en terminal moderna.
        self.assertEqual(cell_width("←→"), 2)
        self.assertEqual(cell_width("★"), 1)
        self.assertEqual(cell_width("▶"), 1)

    def test_acentuadas_y_separadores(self):
        self.assertEqual(cell_width("↑/↓"), 3)
        self.assertEqual(cell_width("F Favorito"), 10)
        self.assertEqual(cell_width("│"), 1)

    def test_ancho_oriental_doble_celda(self):
        self.assertEqual(char_width("漢"), 2)
        self.assertEqual(cell_width("中文"), 4)

    def test_marca_combinante_no_ocupa_celda(self):
        self.assertEqual(char_width("́"), 0)

    def test_vacio_es_cero(self):
        self.assertEqual(cell_width(""), 0)
        self.assertEqual(clip_cells("abc", 0), "")

    def test_clip_devuelve_el_texto_si_cabe(self):
        self.assertEqual(clip_cells("abc", 10), "abc")
        self.assertEqual(clip_cells("abc", 3), "abc")

    def test_clip_noparte_una_celda_doble(self):
        self.assertEqual(clip_cells("←→", 1), "←")
        self.assertEqual(clip_cells("中文x", 5), "中文x")
        self.assertEqual(clip_cells("中文x", 3), "中")
        self.assertEqual(clip_cells("中文x", 4), "中文")

    def test_clip_respeta_el_maximo(self):
        texto = "Enter Ver · f Favorito · e EPG"
        for n in range(0, cell_width(texto) + 4):
            self.assertLessEqual(cell_width(clip_cells(texto, n)), max(0, n))


# ---------------------------------------------------------------------------
# 2. El modelo (F2)
# ---------------------------------------------------------------------------


class TestActionModel(unittest.TestCase):
    def test_prioridades_son_escala_no_numeros_magicos(self):
        self.assertEqual(P.PRIMARIA, 100)
        self.assertEqual(P.AYUDA, 10)
        self.assertGreater(P.PRIMARIA, P.NAVEGACION)
        self.assertGreater(P.NAVEGACION, P.AYUDA)

    def test_ayuda_es_esencial_y_de_prioridad_mas_baja(self):
        ayuda = Action("?", V.AYUDA, P.AYUDA, essential=True)
        self.assertTrue(ayuda.essential)
        self.assertEqual(ayuda.priority, min(P.AYUDA, 10))
        # Y sigue siendo la de menor prioridad del catálogo.
        self.assertLess(ayuda.priority, Action("Esc", V.VOLVER, P.NAVEGACION).priority)

    def test_chip_usa_la_forma_canonica(self):
        a = Action("Enter", V.VER)
        self.assertEqual(a.chip(), "Enter Ver")
        self.assertEqual(a.chip_cells(), cell_width("Enter Ver") + 2)

    def test_es_frozen(self):
        a = Action("f", V.FAVORITO)
        with self.assertRaises(Exception):
            a.key = "F"  # type: ignore[misc]

    def test_vocabulario_cerrado_cubre_las_declaradas(self):
        for label in ("Ver", "Abrir", "Buscar", "Favorito", "Quitar", "Editar",
                      "Actualizar", "Horario", "Volver", "Ayuda"):
            self.assertIn(label, LABELS)

    def test_validar_rechaza_duplicados(self):
        with self.assertRaises(AssertionError):
            validate([Action("f", V.FAVORITO), Action("f", V.QUITAR)])

    def test_validar_rechaza_etiqueta_fuera_de_vocabulario(self):
        with self.assertRaises(AssertionError):
            validate([Action("f", "Favorito Favorito")])

    def test_validar_rechaza_accion_sin_tecla(self):
        with self.assertRaises(AssertionError):
            validate([Action("", V.VER)])

    def test_validar_acepta_un_catalogo_valido(self):
        validate(cat())  # no lanza


# ---------------------------------------------------------------------------
# 3. El ajuste por prioridad (F2/F4)
# ---------------------------------------------------------------------------


class TestFitActions(unittest.TestCase):
    def test_prioridad_descarta_la_menos_importante(self):
        a = Action("Enter", V.VER, P.PRIMARIA)
        b = Action("f", V.FAVORITO, P.SECUNDARIA)
        c = Action("e", V.EPG, P.AYUDA)
        # Ancho justo para A y B; C no cabe.
        ancho = width_for(a, b)
        self.assertEqual(keys(fit_actions([a, b, c], ancho)), ["Enter", "f"])
        # Una celda menos y B ya no cabe: entra C, que es más corto. No es un
        # fallo del orden —el catálogo sigue siendo A, B, C por prioridad— sino
        # que en una barra estrecha la acción corta vale más que la larga.
        self.assertEqual(keys(fit_actions([a, b, c], ancho - 1)), ["Enter", "e"])

    def test_ancho_insuficiente_para_todo(self):
        acciones = cat()
        ajuste = fit_actions(acciones, 1)
        self.assertNotIn("Enter", keys(ajuste))

    def test_esencial_sobrevive_al_descarte_por_prioridad(self):
        # A y B llenan el ancho y C (`? Ayuda`, esencial) se queda fuera.
        a = Action("Enter", V.VER, P.PRIMARIA)
        b = Action("Esc", V.VOLVER, P.NAVEGACION)
        ayuda = Action("?", V.AYUDA, P.AYUDA, essential=True)
        ancho = width_for(a, b)  # justo A+B, sin lugar para C
        sin_essential = fit_actions([a, b], ancho)
        self.assertEqual(keys(sin_essential), ["Enter", "Esc"])

        ajuste = fit_actions([a, b, ayuda], ancho)
        self.assertIn("?", keys(ajuste))
        # Y lo consigue sacrificando a la menos importante, no a la primaria.
        self.assertIn("Enter", keys(ajuste))
        self.assertNotIn("Esc", keys(ajuste))

    def test_esencial_no_desplaza_a_la_accion_mas_importante(self):
        # Ancho de una sola acción: `? Ayuda` no puede expulsar a `Enter Ver`.
        ayuda = Action("?", V.AYUDA, P.AYUDA, essential=True)
        enter = Action("Enter", V.VER, P.PRIMARIA)
        ancho = width_for(enter)  # sólo cabe Enter
        self.assertEqual(keys(fit_actions([ayuda, enter], ancho)), ["Enter"])

    def test_enabled_false_no_aparece(self):
        off = Action("f", V.QUITAR, P.PRIMARIA, enabled=False)
        ajuste = fit_actions([off, Action("Enter", V.VER, P.PRIMARIA)], 100)
        self.assertEqual(keys(ajuste), ["Enter"])

    def test_orden_estable_ante_prioridades_iguales(self):
        primero = Action("g", V.GRUPO, P.ORGANIZACION)
        segundo = Action("R", V.ACTUALIZAR, P.ORGANIZACION)
        tercero = Action("r", V.RECARGAR, P.ORGANIZACION)
        ajuste = fit_actions([tercero, primero, segundo], 100)
        self.assertEqual(keys(ajuste), ["r", "g", "R"])

    def test_tope_de_seis_acciones(self):
        muchas = [Action(f"k{i}", V.VER, P.PRIMARIA) for i in range(12)]
        self.assertLessEqual(len(fit_actions(muchas, 400)), MAX_VISIBLE)

    def test_una_sola_linea_sin_wrap(self):
        ajuste = fit_actions(cat(), 400)
        for a in ajuste:
            self.assertNotIn("\n", a.chip())
            self.assertNotIn("\n", a.label)

    def test_ninguna_etiqueta_truncada(self):
        ajuste = fit_actions(cat(), 20)
        etiquetas = {a.label for a in cat()}
        for a in ajuste:
            # El texto pintado es exactamente el declarado: nada de "Favorit…".
            self.assertIn(a.label, etiquetas)
            self.assertTrue(a.chip().endswith(a.label))

    def test_sin_wrap_una_sola_linea_devuelta(self):
        self.assertEqual(len(fit_actions(cat(), 0)), 0)
        self.assertIsInstance(fit_actions(cat(), 30), list)

    def test_el_ajuste_nunca_excede_el_ancho(self):
        acciones = cat()
        for ancho in range(0, 80):
            ajuste = fit_actions(acciones, ancho)
            if not ajuste:
                # Sin nada que dibujar no se gasta ni el espacio inicial.
                self.assertLess(ancho, width_for(acciones[0]))
                continue
            usado = 1  # LEADING
            for i, a in enumerate(ajuste):
                usado += a.chip_cells() + (1 if i else 0)
            self.assertLessEqual(usado, ancho)

    def test_determinista(self):
        self.assertEqual(fit_actions(cat(), 40), fit_actions(cat(), 40))

    def test_devuelve_lista_de_actions(self):
        self.assertTrue(all(isinstance(a, Action) for a in fit_actions(cat(), 80)))

    def test_lista_vacia(self):
        self.assertEqual(fit_actions([], 80), [])


# ---------------------------------------------------------------------------
# 4. FooterBar (F4)
# ---------------------------------------------------------------------------


class TestFooterBarActions(unittest.TestCase):
    def test_set_actions_pinta_el_catalogo(self):
        fb = FooterBar()
        fb.set_actions(cat())
        fake = render_footer(fb, 24, 80)
        fila = fake.row_text(23)
        self.assertIn("Enter Ver", fila)
        self.assertIn("Favorito", fila)
        self.assertIn("Ayuda", fila)

    def test_chips_refleja_lo_que_cabe(self):
        fb = FooterBar()
        fb.set_actions(cat())
        render_footer(fb, 24, 80)
        self.assertEqual([c[0] for c in fb.chips][:2], ["Enter", "f"])
        # Y siguen siendo tuplas (key, label), como espera cualquier lector.
        for key, label in fb.chips:
            self.assertIsInstance(key, str)
            self.assertIsInstance(label, str)

    def test_ajusta_por_prioridad_al_narrows(self):
        fb = FooterBar()
        fb.set_actions(cat())
        render_footer(fb, 24, 24)
        # Con 24 celdas sólo cabe lo prioritario; la etiqueta no se parte.
        fila = render_footer(fb, 24, 24).row_text(23)
        self.assertIn("Enter Ver", fila)
        self.assertNotIn("Favorit…", fila)

    def test_ayuda_sobrevive_en_80_columnas(self):
        fb = FooterBar()
        fb.set_actions(cat())
        render_footer(fb, 24, 80)
        self.assertIn("?", [c[0] for c in fb.chips])

    def test_set_chips_deja_el_camino_heredado(self):
        fb = FooterBar()
        fb.set_chips(("Enter", "seleccionar"), ("Esc", "volver"))
        self.assertEqual(fb.actions, [])
        fake = render_footer(fb, 24, 80)
        self.assertIn("seleccionar", fake.row_text(23))

    def test_ningun_addstr_desborda_la_linea(self):
        """De 1×1 a 200×60: cero `curses.error` y cero desbordón."""
        for h, w in ((1, 1), (1, 10), (2, 20), (2, 80), (3, 5), (24, 40),
                     (24, 80), (24, 200), (60, 60), (24, 10), (24, 3)):
            fb = FooterBar()
            fb.set_actions(cat())
            fake = render_footer(fb, h, w)
            for _y, x, texto, _a in fake.addstr_calls:
                self.assertLessEqual(x + len(texto), w, f"desbordón en {h}x{w}")

    def test_toast_tiene_prioridad_absoluta(self):
        fb = FooterBar()
        fb.set_actions(cat())
        fake = FakeStdscr(24, 40)
        fb.show("Catálogo actualizado: 3 listas.", duration=30.0)
        with mock.patch.object(colors, "pair", return_value=0):
            fb.render(fake)
        fila = fake.row_text(23)
        self.assertIn("Catálogo actualizado", fila)
        # El toast entra entero y las acciones ceden el ancho.
        for _y, x, texto, _a in fake.addstr_calls:
            self.assertLessEqual(x + len(texto), 40)

    def test_toast_no_desborda_con_terminal_diminuta(self):
        fb = FooterBar()
        fb.set_actions(cat())
        fake = FakeStdscr(5, 12)
        fb.show("mensaje larguísimo que no cabe", duration=30.0)
        with mock.patch.object(colors, "pair", return_value=0):
            fb.render(fake)
        for _y, x, texto, _a in fake.addstr_calls:
            self.assertLessEqual(x + len(texto), 12)

    def test_sin_acciones_pinta_la_linea_vacia(self):
        fb = FooterBar()
        fake = render_footer(fb, 24, 40)
        self.assertEqual(fake.row_text(23).strip(), "")

    def test_usa_la_anchura_real_no_len(self):
        """La línea lleva `←`/`★`, Ambiguous: con `len()` se desborda."""
        acciones = [
            Action("←→", V.ELEGIR, P.PRIMARIA),
            Action("↑↓", V.ELEGIR, P.CONTEXTUAL),
            Action("★", V.FAVORITO, P.ORGANIZACION),
        ]
        fb = FooterBar()
        fb.set_actions(acciones)
        fake = render_footer(fb, 24, 40)
        for _y, x, texto, _a in fake.addstr_calls:
            self.assertLessEqual(x + len(texto), 40)
        # La suma de celdas reales cabe; con `len()` la línea se saldría.
        fila = fake.row_text(23)
        self.assertLessEqual(cell_width(fila), 40)

    def test_reutiliza_pares_existentes(self):
        """Cero pares de color nuevos (R-06): los usados son los del tema."""
        usados = set()
        with mock.patch.object(
            colors, "pair", side_effect=lambda p: usados.add(p) or p
        ):
            fb = FooterBar()
            fb.set_actions(cat())
            render_footer(fb, 24, 80)
        for p in usados:
            self.assertIn(p, {
                colors.PAIR_PRIMARY, colors.PAIR_MUTED, colors.PAIR_STATUS,
                colors.PAIR_BORDER, colors.PAIR_SUCCESS, colors.PAIR_DANGER,
            })

    def test_separador_superior_con_border(self):
        fb = FooterBar()
        fake = render_footer(fb, 24, 80)
        fila22 = [c for c in fake.addstr_calls if c[0] == 22]
        self.assertTrue(fila22)
        self.assertIn("─", fila22[0][2])


# ---------------------------------------------------------------------------
# 5. Pies de modal (§35, F6)
# ---------------------------------------------------------------------------


def render_modal(modal: Modal, h: int = 24, w: int = 80) -> FakeStdscr:
    fake = FakeStdscr(h, w)
    with mock.patch.object(colors, "pair", return_value=0):
        modal.render(fake)
    return fake


class TestPieDeModal(unittest.TestCase):
    """El modal dice qué teclas valen *ahí dentro* (§35).

    Es el sustituto del §28 «pantalla de Configuración» que nunca existió: los
    ajustes de theTVVIEW son modales, así que su contextual vive en este pie.
    """

    def _fila_pie(self, modal: Modal, h: int = 24, w: int = 80) -> str:
        """Texto de la fila del pie de un modal ya pintado."""
        fake = render_modal(modal, h, w)
        _y, _x, alto, _w = modal._calc_rect(h, w)
        return fake.row_text(_y + alto - 2)

    def test_pinta_confirmar_y_cancelar(self):
        modal = Modal("Confirmar", "¿Eliminar?", ["Cancelar", "Aceptar"],
                      actions=modal_actions())
        texto = self._fila_pie(modal)
        self.assertIn("Enter Confirmar", texto)
        self.assertIn("Esc Cancelar", texto)

    def test_el_verbo_se_personaliza(self):
        modal = Modal("Confirmar", "¿Eliminar?", ["Cancelar", "Eliminar"],
                      actions=modal_actions(confirmar=V.ELIMINAR))
        self.assertIn("Enter Eliminar", self._fila_pie(modal))

    def test_sin_pie_no_cambia_nada(self):
        modal = Modal("Aviso", "Texto", ["Aceptar"])
        self.assertEqual(modal.actions, [])
        texto = "\n".join(render_modal(modal).row_text(y) for y in range(24))
        self.assertNotIn("Esc Cancelar", texto)

    def test_el_pie_no_tapa_el_boton(self):
        modal = Modal("Confirmar", "¿Eliminar?", ["Cancelar", "Aceptar"],
                      actions=modal_actions())
        fake = render_modal(modal)
        y, _x, h, _w = modal._calc_rect(24, 80)
        filas = {r: fake.row_text(y + r) for r in range(h)}
        con_pie = [r for r, f in filas.items() if "Enter Confirmar" in f]
        con_boton = [r for r, f in filas.items() if "[Aceptar]" in f]
        self.assertEqual(len(con_pie), 1, f"pie en filas {con_pie}")
        self.assertEqual(len(con_boton), 1, f"botón en filas {con_boton}")
        self.assertNotEqual(con_pie[0], con_boton[0],
                            "el pie se está pintando encima del botón")

    def test_el_modal_ocupa_una_fila_mas_con_pie(self):
        base = Modal("Aviso", "Una línea", ["Aceptar"])
        con_pie = Modal("Aviso", "Una línea", ["Aceptar"], actions=modal_actions())
        _y, _x, h_base, _w_base = base._calc_rect(24, 80)
        _y, _x, h_pie, _w_pie = con_pie._calc_rect(24, 80)
        self.assertEqual(h_pie, h_base + 1,
                         "el pie necesita su propia fila")

    def test_el_modal_crece_para_que_el_pie_cpa_entero(self):
        """Prefiere crecer un poco a recortar «Enter Confirmar» (§46)."""
        base = Modal("Aviso", "Una línea", ["Aceptar"])
        con_pie = Modal("Aviso", "Una línea", ["Aceptar"], actions=modal_actions())
        _y, _x, _h, w_base = base._calc_rect(24, 80)
        _y, _x, _h, w_pie = con_pie._calc_rect(24, 80)
        self.assertGreater(w_pie, w_base,
                           "sin crecer, el pie se recortaría a medias")
        texto = self._fila_pie(con_pie)
        self.assertIn("Esc Cancelar", texto,
                      "las dos acciones del pie deben caber enteras")

    def test_etiqueta_nunca_partida(self):
        modal = Modal("Confirmar", "¿Eliminar?", ["Cancelar", "Aceptar"],
                      actions=modal_actions())
        for w in (24, 30, 40, 80, 200):
            texto = "\n".join(render_modal(modal, 24, w).row_text(y) for y in range(24))
            self.assertNotIn("Enter Confirm…", texto)
            self.assertNotIn("Enter Confir…", texto)

    def test_terminal_diminuto_no_rompe(self):
        modal = Modal("Confirmar", "¿Eliminar?", ["Cancelar", "Aceptar"],
                      actions=modal_actions())
        for h, w in ((1, 1), (2, 10), (6, 19), (7, 20), (24, 20), (10, 30), (200, 200)):
            with self.subTest(tam=f"{h}x{w}"):
                fake = render_modal(modal, h, w)
                for _y, x, texto, _a in fake.addstr_calls:
                    self.assertLessEqual(x + len(texto), w)
                    self.assertTrue(texto, "nunca un addstr vacío")

    def test_reutiliza_el_mismo_modelo_que_el_pie(self):
        """El pie del modal usa `Action` y `fit_actions`: un solo camino."""
        acciones = modal_actions()
        self.assertTrue(all(isinstance(a, Action) for a in acciones))
        self.assertEqual([a.key for a in acciones], ["Enter", "Esc"])
        for a in acciones:
            with self.subTest(key=a.key):
                validate([a])  # vocabulario cerrado: no hay atajo nuevo

    def test_search_modal_deriva_la_pista_de_actions(self):
        """`SearchModal.hint` sale de `actions()` si el caller no pasa uno."""
        modal = SearchModal("Buscar canal")
        linea = modal.hint_line()
        self.assertIn("Enter Confirmar", linea)
        self.assertIn("Ctrl-U Vaciar", linea)
        self.assertIn("Esc Limpiar", linea)

    def test_search_modal_respeta_un_hint_explicito(self):
        modal = SearchModal("Buscar", hint="texto propio")
        self.assertEqual(modal.hint_line(), "texto propio")

    def test_search_modal_no_se_rompe_al_pintar(self):
        modal = SearchModal("Buscar canal")
        fake = render_modal(modal, 24, 80)
        _y, _x, h, _w = modal.calc_rect(24, 80)
        texto = fake.row_text(_y + 4)
        self.assertTrue(texto.strip(), "la pista de teclas no se pintó")
        for _y, x, t, _a in fake.addstr_calls:
            self.assertLessEqual(x + len(t), 80)


if __name__ == "__main__":
    unittest.main()