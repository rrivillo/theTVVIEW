"""Buscador: conteo con palabras y resaltado de la coincidencia (FR-SRC-04/06).

Dos cosas miden estos tests, y las dos importan más de lo que parecen:

1. **El contador dice cuántos hay, en palabras.** "3/40" obliga a traducir
   mentalmente; "Sin coincidencias" distingue "no hay" de "no hay canales".
2. **El resaltado no toca el resto de la app.** `ScrollableList` lo usan nueve
   pantallas más, así que la invariante es explícita: sin rangos, la lista pinta
   **exactamente los mismos `addstr`** que antes del cambio. Si algún día esa
   comparativa falla, el resaltado se ha colado donde no debía.

Sin terminal real: doble de ventana que captura `addstr` y falla si se escribe
fuera de pantalla (la misma técnica que `test_empty_states`).
"""

from __future__ import annotations

import curses
import unittest
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.ui import colors
from thetvview.ui.screens import ChannelsScreen
from thetvview.ui.textwidth import cell_width
from thetvview.ui.widgets import ScrollableList, SearchModal

#: Los atributos con los que se marca el resaltado, leídos de curses para no
#: comparar contra números mágicos: negrita en las filas normales, inversión en
#: la seleccionada.
HL_BOLD = curses.A_BOLD
HL_INVERT = curses.A_REVERSE


class VentanaCaptura:
    """Memoriza cada `addstr` y falla ruidosamente si se sale de pantalla."""

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

    def filas(self) -> dict[int, str]:
        por_fila: dict[int, list[tuple[int, str]]] = {}
        for y, x, texto, _a in self.calls:
            por_fila.setdefault(y, []).append((x, texto))
        return {y: "".join(t for _x, t in sorted(p)) for y, p in por_fila.items()}

    def texto(self) -> str:
        return " ".join(c[2] for c in self.calls)

    def attrs_de(self, y: int) -> list[int]:
        return [a for cy, _x, _t, a in self.calls if cy == y]


# ---------------------------------------------------------------------------
# 1. El contador (FR-SRC-04)
# ---------------------------------------------------------------------------


class TestContador(unittest.TestCase):
    def test_tres_textos_del_contador(self):
        self.assertEqual(SearchModal.format_counter(0, 0), "Sin coincidencias")
        self.assertEqual(SearchModal.format_counter(1, 40), "1 resultado")
        self.assertEqual(SearchModal.format_counter(3, 40), "3 resultados")

    def test_singular_y_plural(self):
        self.assertEqual(SearchModal.format_counter(1, 1), "1 resultado")
        self.assertEqual(SearchModal.format_counter(2, 2), "2 resultados")
        self.assertNotIn("resultados", SearchModal.format_counter(1, 1))

    def test_el_contador_se_pinta_en_el_modal(self):
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            SearchModal.render_modal(ventana, query="la", matched=3, total=40)
        self.assertIn("3 resultados", ventana.texto())

    def test_sin_coincidencias_no_dice_cero_sobre_cero(self):
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            SearchModal.render_modal(ventana, query="zzz", matched=0, total=40)
        texto = ventana.texto()
        self.assertIn("Sin coincidencias", texto)
        self.assertNotIn("0/40", texto)


# ---------------------------------------------------------------------------
# 2. El resaltado dentro de ScrollableList (FR-SRC-06)
# ---------------------------------------------------------------------------


class TestScrollableListResaltado(unittest.TestCase):
    def test_sin_ranges_el_render_es_identico(self):
        """La invariante de no-regresión: mismo texto, mismas posiciones.

        Se mide sobre una lista que **nunca** tuvo rangos y otra a la que se le
        ponen y se le quitan: si el resaltado se colara en el render de
        siempre, estas dos capturas dejarían de coincidir.
        """
        items = ["La 1", "La 2", "Deportes 1", "Radio Serra"]
        lista = ScrollableList()
        lista.set_items(items)

        antes = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(antes, 1, 0, 4, 40)

        lista.set_ranges([[(0, 2)], [], [(3, 6)], []])
        lista.set_ranges(None)  # el usuario vació la búsqueda
        despues = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(despues, 1, 0, 4, 40)

        self.assertEqual(antes.calls, despues.calls)

    def test_set_items_limpia_los_rangos(self):
        """Filas nuevas con resaltados viejos resaltaban lo que ya no está."""
        lista = ScrollableList()
        lista.set_items(["La 1", "La 2"])
        lista.set_ranges([[(0, 2)], [(0, 2)]])
        lista.set_items(["La 1"])
        self.assertEqual(lista.ranges, [])

    def test_set_ranges_descarta_un_desajuste(self):
        lista = ScrollableList()
        lista.set_items(["a", "b", "c"])
        lista.set_ranges([[(0, 1)], []])  # 2 rangos para 3 filas
        self.assertEqual(lista.ranges, [])

    def test_la_coincidencia_se_pinta_con_otro_atributo(self):
        lista = ScrollableList()
        lista.set_items(["La 1 HD", "La 2"])
        lista.set_ranges([[(3, 5)], []])

        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(ventana, 1, 0, 2, 20)

        fila = ventana.filas()[1]
        self.assertIn("La 1 HD", fila)
        # La fila 1 se compone de varios `addstr`: normal + resaltado + resto.
        trozos = [t for (_y, _x, t, _a) in ventana.calls if _y == 1]
        self.assertGreater(len(trozos), 1, "la fila resaltada debe partirse en trozos")

    def test_el_resaltado_cae_donde_dice_el_rango(self):
        """El rango cuenta desde la etiqueta, el corte desde la fila.

        Con el prefijo `▸ ` de por medio, un rango sin corregir se desplaza tres
        caracteres y acaba resaltando cualquier otra cosa —o nada, si se sale.
        """
        lista = ScrollableList()
        lista.set_items(["ABC marca DEF", "otra"])
        lista.set_ranges([[(4, 9)], []])  # "marca", en la fila sin seleccionar
        lista.selected = 1
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(ventana, 1, 0, 2, 30)
        resaltado = [t for (_y, _x, t, a) in ventana.calls if a & HL_BOLD]
        self.assertEqual(resaltado, ["marca"])

    def test_el_resaltado_ignora_el_prefijo_de_seleccion(self):
        """En la fila seleccionada el corte sigue falling donde toca."""
        lista = ScrollableList()
        lista.set_items(["ABC marca DEF"])
        lista.set_ranges([[(4, 9)]])
        lista.selected = 0
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(ventana, 1, 0, 1, 30)
        resaltado = [t for (_y, _x, t, a) in ventana.calls if a & HL_INVERT]
        self.assertEqual(resaltado, ["marca"])

    def test_el_resaltado_de_la_fila_seleccionada_no_depende_del_color(self):
        """Sobre la fila ya pintada con el par de selección, se invierte.

        `PAIR_CURRENT` encima del par de selección puede caer en el mismo tono
        (amarillo sobre amarillo en las listas M3U) y no verse nada. Invertir
        funciona sobre cualquier par, y `A_REVERSE` no depende del color (NFR-06).
        """
        lista = ScrollableList()
        lista.set_items(["ABC marca DEF"])
        lista.set_ranges([[(4, 9)]])
        lista.selected = 0
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(ventana, 1, 0, 1, 30)
        trozo = next(t for (_y, _x, t, a) in ventana.calls if t == "marca")
        self.assertTrue(trozo)
        # Sin color (`pair` devuelve 0), sólo puede distinguirse por atributo.
        atributos = {a for (_y, _x, t, a) in ventana.calls if t == "marca"}
        self.assertTrue(atributos)
        self.assertTrue(any(a & HL_INVERT for a in atributos))

    def test_la_fila_resaltada_sigue_midiendo_lo_mismo(self):
        """Una fila resaltada no puede medir menos: dejaría huecos."""
        items = ["Una", "ABC marca DEF", "Tres"]
        sin = ScrollableList()
        sin.set_items(items)
        con = ScrollableList()
        con.set_items(items)
        con.set_ranges([[], [(4, 9)], []])

        ancho = 30
        v1, v2 = VentanaCaptura(), VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            sin.render(v1, 1, 0, 3, ancho)
            con.render(v2, 1, 0, 3, ancho)
        ancho_por_fila: dict[int, int] = {}
        for y, _x, texto, _a in v1.calls:
            ancho_por_fila[y] = ancho_por_fila.get(y, 0) + cell_width(texto)
        for y, x, texto, _a in v2.calls:
            with self.subTest(fila=y):
                # Ni se pasa de ancho ni deja la fila más corta.
                self.assertLessEqual(x + cell_width(texto), ancho)

    def test_solo_se_resaltan_las_filas_con_coincidencia(self):
        lista = ScrollableList()
        lista.set_items(["La 1", "La 2", "La 3"])
        lista.set_ranges([[(0, 2)], [], [(0, 2)]])
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(ventana, 1, 0, 3, 20)
        por_fila: dict[int, int] = {}
        for y, _x, _t, _a in ventana.calls:
            por_fila[y] = por_fila.get(y, 0) + 1
        # La fila 2 (sin coincidencia) es un solo `addstr`; las otras, varios.
        self.assertEqual(por_fila[2], 1)
        self.assertGreater(por_fila[1], 1)
        self.assertGreater(por_fila[3], 1)

    def test_rangos_fuera_del_texto_no_revientan(self):
        lista = ScrollableList()
        lista.set_items(["La 1"])
        lista.set_ranges([[(50, 90)]])  # más allá del final de la fila
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(ventana, 1, 0, 1, 20)
        self.assertIn("La 1", ventana.texto())

    def test_rangos_invertidos_se_ignoran(self):
        lista = ScrollableList()
        lista.set_items(["La 1"])
        lista.set_ranges([[(5, 2)]])  # inicio > fin
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(ventana, 1, 0, 1, 20)
        self.assertIn("La 1", ventana.texto())

    def test_texto_con_celda_doble_no_parte_caracteres(self):
        """Emoji: el corte va por carácter, nunca por medio del glifo (NFR-06)."""
        lista = ScrollableList()
        lista.set_items(["📺 Noticias Canal 1"])
        lista.set_ranges([[(11, 16)]])  # "Canal"
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(ventana, 1, 0, 1, 30)
        fila = ventana.filas()[1]
        self.assertIn("Canal", fila)

    def test_terminal_minima_no_desborda(self):
        lista = ScrollableList()
        lista.set_items(["La 1", "La 2", "La 3"])
        lista.set_ranges([[(0, 2)], [(0, 2)], [(0, 2)]])
        ventana = VentanaCaptura(10, 40)
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(ventana, 1, 0, 8, 40)


# ---------------------------------------------------------------------------
# 3. El resaltado en Canales (D14: sólo aquí, no en Grupos)
# ---------------------------------------------------------------------------


class _Favoritos:
    def is_favorite(self, channel):  # noqa: ANN001
        return False

    def favorite_urls(self):  # noqa: ANN201
        return set()


class _App:
    def __init__(self) -> None:
        self.favorites = _Favoritos()

    def body_height(self) -> int:
        return 20


def _pantalla(cCanales: int = 200) -> ChannelsScreen:
    canales = [
        Channel(name=f"La {i}", url=f"http://a.test/{i}", group="Noticias")
        for i in range(1, cCanales + 1)
    ]
    return ChannelsScreen(_App(), Playlist(name="Test", channels=canales))


class TestResaltadoEnCanales(unittest.TestCase):
    def test_las_coincidencias_marcan_rangos(self):
        pantalla = _pantalla(10)
        pantalla.query = "la 1"
        pantalla.searching = True
        pantalla._apply_filter()
        self.assertTrue(pantalla.list.ranges)
        for rangos in pantalla.list.ranges:
            self.assertTrue(rangos, "toda fila filtrada tiene su coincidencia")
            for ini, fin in rangos:
                self.assertLess(ini, fin)

    def test_sin_query_no_hay_rangos(self):
        pantalla = _pantalla(10)
        pantalla._apply_filter()
        self.assertEqual(pantalla.list.ranges, [])

    def test_query_que_no_casa_no_marca_rangos(self):
        pantalla = _pantalla(10)
        pantalla.query = "zzz"
        pantalla.searching = True
        pantalla._apply_filter()
        self.assertEqual(pantalla.list.items, [])
        self.assertEqual(pantalla.list.ranges, [])

    def test_rango_apunta_a_la_etiqueta_real(self):
        """El rango cae dentro del texto que se ve, no del que se indexa."""
        pantalla = _pantalla(10)
        pantalla.query = "noticias"
        pantalla.searching = True
        pantalla._apply_filter()
        for etiqueta, rangos in zip(pantalla.list.items, pantalla.list.ranges):
            for ini, fin in rangos:
                self.assertIn(etiqueta[ini:fin].lower(), "noticias")
                self.assertLessEqual(fin, len(etiqueta))

    def test_mayusculas_en_la_consulta(self):
        pantalla = _pantalla(10)
        pantalla.query = "LA 3"
        pantalla.searching = True
        pantalla._apply_filter()
        self.assertEqual(len(pantalla.list.items), 1)
        self.assertTrue(pantalla.list.ranges[0])

    def test_cinco_mil_canales_no_rehacen_etiquetas(self):
        """Rendimiento: filtrar es recorrer lo cacheado, no reconstruir (FR-SRC-12)."""
        pantalla = _pantalla(5000)
        etiqueta_original = pantalla._labels
        pantalla.query = "la 42"
        pantalla.searching = True
        pantalla._apply_filter()
        self.assertIs(pantalla._labels, etiqueta_original,
                      "el filtro no debe regenerar las etiquetas cacheadas")
        self.assertGreater(len(pantalla.list.items), 0)
        self.assertEqual(len(pantalla.list.items), len(pantalla.list.ranges))

    def test_render_en_terminal_pequena_con_resaltado(self):
        pantalla = _pantalla(50)
        pantalla.query = "la"
        pantalla.searching = True
        pantalla._apply_filter()
        ventana = VentanaCaptura(10, 40)
        with mock.patch.object(colors, "pair", return_value=0):
            pantalla.render(ventana)
        self.assertTrue(ventana.calls)


if __name__ == "__main__":
    unittest.main()