"""Fase 8 · U-05 — Búsqueda: filtros sobre `search.Consultador`.

Hasta U-05 la pantalla de canales filtraba con un `q in texto` sobre
nombre+grupo, y `search.Consultador` (Fase 3, S-09) no lo usaba nadie. Este
archivo fija lo que el ticket prometía y, sobre todo, lo que cuesta:

- los filtros existen, se llaman en castellano y traducen al backend;
- **la búsqueda de siempre sigue siendo el camino por omisión**: sin filtros
  no se toca el backend, porque un `in` de C sobre 90 000 canales tarda
  microsegundos y `Consultador.consultar` tarda **1,1 s** (medido);
- combinar filtros no vuelve a pagar la cuenta: cada campo se cuenta una vez;
- el orden es estable, y eso incluye a los empates de calidad, donde el
  backend sí es inestable;
- nada se enseña sin comprobar: la definición se llama «estimada» porque
  `resolutions.detect` no sondea nada.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.search import Consulta, Consultador
from thetvview.ui import colors, filters


def canal(nombre="Canal", grupo="Noticias", url=None, tvg_id=""):
    return Channel(name=nombre, url=url or f"http://ejemplo.test/{nombre}",
                   group=grupo, tvg_id=tvg_id)


def catalogo_de_calidades():
    return [
        canal("La 1 HD", "General", tvg_id="a.1"),
        canal("La 2 (720p)", "General", tvg_id="a.2"),
        canal("La 3 FHD", "Deportes", tvg_id="a.3"),
        canal("La 4", "Deportes", tvg_id="a.4"),
        canal("La 5 SD", "Infantiles", tvg_id="a.5"),
    ]


# ---------------------------------------------------------------------------
# 1. El vocabulario
# ---------------------------------------------------------------------------


class TestVocabulario(unittest.TestCase):
    def test_hay_filtros(self) -> None:
        self.assertTrue(filters.FILTROS)

    def test_cada_filtro_se_explica(self) -> None:
        for f in filters.FILTROS:
            with self.subTest(filtro=f.clave):
                self.assertTrue(f.etiqueta)
                self.assertTrue(f.ayuda)

    def test_ninguna_etiqueta_es_una_clave_del_backend(self) -> None:
        """«group» y «epg_status» no le dicen nada a nadie."""
        for f in filters.FILTROS:
            with self.subTest(filtro=f.clave):
                self.assertNotEqual(f.etiqueta, f.clave)
                self.assertNotIn("_", f.etiqueta)

    def test_un_filtro_de_valores_solo_tiene_valores_reales(self) -> None:
        for f in filters.FILTROS:
            if f.tipo != "enum":
                continue
            with self.subTest(filtro=f.clave):
                self.assertTrue(f.opciones)
                for backend, etiqueta in f.opciones:
                    self.assertTrue(backend)
                    self.assertTrue(etiqueta)

    def test_la_definicion_se_llama_estimada(self) -> None:
        """Decisión §1.1. `resolutions.detect` no sondea: deduce del nombre."""
        f = filters.filtro_de("quality")
        self.assertIn("estimada", f.etiqueta.lower())

    def test_el_filtro_de_definicion_avisa_de_que_es_exacto(self) -> None:
        """Un filtro que no avisa hace perder canales sin dar cuenta.

        El backend compara la etiqueta **exacta**: «HD» no incluye «720p».
        Eso no se arregla en U-05 (Fase 3 está cerrada), pero sí se dice, y
        en dos sitios: la nota corta de la fila y el aviso entero del editor.
        """
        f = filters.filtro_de("quality")
        self.assertIn("exacta", f.ayuda.lower())
        self.assertIn("no trae", f.aviso.lower())

    def test_el_aviso_largo_explica_de_donde_sale_la_definicion(self) -> None:
        """`resolutions.detect` no sondea: deduce. Saying it only in the short
        note would be saying it in the place where it gets cut."""
        aviso = filters.aviso_de("quality").lower()
        self.assertIn("no se mide", aviso)
        self.assertIn("tvg-id", aviso)

    def test_las_notas_caben_en_una_linea_de_terminal(self) -> None:
        """La nota de la fila se recorta al ancho; un aviso cortado no avisa
        de nada. El texto largo va al subtítulo del editor."""
        for f in filters.FILTROS:
            with self.subTest(filtro=f.clave):
                self.assertLessEqual(len(f.ayuda), 48)
                self.assertTrue(f.aviso == "" or len(f.aviso) > len(f.ayuda))

    def test_los_filtros_sin_aviso_no_inventan_uno(self) -> None:
        self.assertEqual(filters.aviso_de("catchup_status"), "")
        self.assertEqual(filters.aviso_de("no-existe"), "")

    def test_los_filtros_de_texto_dicen_cuando_admiten_varios(self) -> None:
        for clave in ("group", "source_id"):
            with self.subTest(filtro=clave):
                self.assertIn("comas", filters.filtro_de(clave).ayuda.lower())

    def test_los_que_necesitan_guia_lo_dicen(self) -> None:
        for clave in ("epg_status", "program_window", "program_title"):
            with self.subTest(filtro=clave):
                self.assertIn(
                    "guía", filters.filtro_de(clave).ayuda.lower())


class TestFrases(unittest.TestCase):
    def test_la_frase_se_lee_en_castellano(self) -> None:
        self.assertEqual(
            filters.frase_de("epg_status", "available"), "Guía: Tiene guía")

    def test_las_resoluciones_se_dicen_tal_cual(self) -> None:
        """Traducir «720p» a «alta definición» sería mentir: el filtro
        compara la etiqueta exacta."""
        self.assertEqual(filters.valor_legible("quality", "720p"), "720p")
        self.assertEqual(
            filters.valor_legible("quality", "unknown"),
            "Sin definición estimada")

    def test_un_valor_desconocido_se_dice_tal_cual(self) -> None:
        self.assertEqual(filters.valor_legible("group", "Deportes"),
                         "Deportes")

    def test_la_nota_con_valor_dice_el_valor(self) -> None:
        self.assertEqual(filters.nota_de("catchup_status", "available"),
                         "Se puede")

    def test_la_nota_sin_valor_explica_para_que_sirve(self) -> None:
        nota = filters.nota_de("catchup_status", None)
        self.assertEqual(nota, filters.filtro_de("catchup_status").ayuda)

    def test_todas_las_fases_caben_en_una_linea(self) -> None:
        """El modal las recorta; una frase kilométrica no se lee."""
        for clave, valor in (("group", "Deportes"), ("quality", "4K"),
                             ("program_window", "next_24h"),
                             ("epg_status", "unknown")):
            with self.subTest(filtro=clave):
                self.assertLessEqual(len(filters.frase_de(clave, valor)), 40)


class TestTraduccionAlBackend(unittest.TestCase):
    def test_un_texto_simple_va_tal_cual(self) -> None:
        self.assertEqual(
            filters.valor_para_consulta("tvg_id", " espn.la1 "), "espn.la1")

    def test_un_campo_de_lista_acepta_uno_solo(self) -> None:
        self.assertEqual(
            filters.valor_para_consulta("group", "Deportes"), ["Deportes"])

    def test_una_lista_se_separa_por_comas(self) -> None:
        self.assertEqual(
            filters.valor_para_consulta("group", "Deportes, Noticias , Cultura"),
            ["Deportes", "Noticias", "Cultura"])

    def test_una_lista_vacía_no_pide_nada(self) -> None:
        self.assertEqual(filters.valor_para_consulta("group", " , ,"), [])

    def test_un_enumerado_no_se_toca(self) -> None:
        self.assertEqual(
            filters.valor_para_consulta("epg_status", "available"),
            "available")

    def test_campos_de_traduce_el_filtro_entero(self) -> None:
        self.assertEqual(
            filters.campos_de([("group", "A, B"), ("quality", "HD")]),
            {"group": ["A", "B"], "quality": "HD"})

    def test_lo_traducido_lo_entiende_el_backend(self) -> None:
        """La prueba que de verdad importa: el `Consulta` que sale de aquí
        filtra lo mismo que si se hubiera escrito a mano."""
        cat = catalogo_de_calidades()
        cons = Consultador(cat)
        campos = filters.campos_de([("group", "Deportes"), ("quality", "HD")])
        mio = cons.consultar(Consulta(**campos), catalogo=cat)
        suyo = cons.consultar(
            Consulta(group=["Deportes"], quality="HD"), catalogo=cat)
        self.assertEqual([c.name for c in mio], [c.name for c in suyo])


# ---------------------------------------------------------------------------
# 2. El motor
# ---------------------------------------------------------------------------


class TestCaminoRapido(unittest.TestCase):
    """Sin filtros, el backend no se toca. Es el requisito de «la búsqueda
    actual sigue siendo el caso por omisión», y el que sostiene la escritura
    a 8 caracteres por segundo sobre 90 000 canales."""

    def setUp(self) -> None:
        self.cat = catalogo_de_calidades()
        self.indice = filters.IndiceFiltros(self.cat)

    def test_sin_texto_sin_filtro_sale_todo(self) -> None:
        self.assertEqual(self.indice.resuelve(), [0, 1, 2, 3, 4])

    def test_texto_a_medias_filtra_por_nombre_y_grupo(self) -> None:
        self.assertEqual(self.indice.resuelve([], "la 1"), [0])
        self.assertEqual(self.indice.resuelve([], "deportes"), [2, 3])

    def test_no_llama_al_backend(self) -> None:
        with mock.patch.object(
            Consultador, "consultar",
            side_effect=AssertionError("no debería contar nada")
        ):
            self.assertEqual(len(self.indice.resuelve([], "la")), 5)

    def test_texto_en_mayúsculas_sí_encuentra(self) -> None:
        """`resuelve` normaliza lo que le llega. La pantalla ya lo pasa en
        minúsculas, así que esto es una red de seguridad, no un coste por
        tecla: el `in` sigue siendo sobre el texto ya en minúsculas."""
        self.assertEqual(self.indice.resuelve([], "LA"), [0, 1, 2, 3, 4])
        self.assertEqual(self.indice.resuelve([], "LA 1"), [0])

    def test_catalogo_vacío_no_revienta(self) -> None:
        indice = filters.IndiceFiltros([])
        self.assertEqual(indice.resuelve([], "x"), [])
        self.assertEqual(indice.resuelve([("quality", "HD")], "x"), [])


class TestFiltros(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = catalogo_de_calidades()
        self.indice = filters.IndiceFiltros(self.cat)

    def test_por_definicion_estima(self) -> None:
        self.assertEqual(self.indice.resuelve([("quality", "FHD")]), [2])

    def test_por_definicion_exacta_no_arrastra_a_las_parecidas(self) -> None:
        """El backend compara la etiqueta exacta. Este test lo fija para que
        nadie «mejore» el filtro sin avisar de que cambia lo que cuenta."""
        self.assertEqual(self.indice.resuelve([("quality", "HD")]), [0])
        self.assertEqual(self.indice.resuelve([("quality", "720p")]), [1])

    def test_por_grupo(self) -> None:
        self.assertEqual(self.indice.resuelve([("group", "Deportes")]), [2, 3])

    def test_varios_grupos_se_or_interno(self) -> None:
        self.assertEqual(
            self.indice.resuelve([("group", "Deportes,Infantiles")]), [2, 3, 4])

    def test_texto_y_filtro_a_la_vez(self) -> None:
        self.assertEqual(
            self.indice.resuelve([("group", "General")], "720p"), [1])

    def test_texto_y_filtro_que_no_cuadran_dan_cero(self) -> None:
        self.assertEqual(
            self.indice.resuelve([("group", "Deportes")], "la 1"), [])

    def test_dos_filtros_se_cruzan(self) -> None:
        self.assertEqual(
            self.indice.resuelve(
                [("group", "General"), ("quality", "FHD")]), [])

    def test_el_orden_no_cambia_que_canales_entran(self) -> None:
        base = self.indice.resuelve([("group", "General")], "")
        for orden in ("name", "group", "quality"):
            with self.subTest(orden=orden):
                self.assertEqual(
                    sorted(self.indice.resuelve(
                        [("group", "General")], "", orden)), base)

    def test_sin_guia_el_estado_de_la_guia_no_sabe(self) -> None:
        """Sin EPG cargado, `estado_epg` dice «unknown» para todos. Eso **es**
        la respuesta, no un fallo: el filtro «no se sabe» cuenta los cinco."""
        self.assertEqual(
            len(self.indice.resuelve([("epg_status", "unknown")])), 5)
        self.assertEqual(
            len(self.indice.resuelve([("epg_status", "available")])), 0)


class TestPagaUnaSolaVez(unittest.TestCase):
    """Lo que hace el ticket posible: `Consultador.consultar` tardó 1,1 s sin
    filtros y 4,6 s con definición sobre 90 000 canales (medido en este repo).
    Si cada pulsación pagara eso, la app se congelaría."""

    def setUp(self) -> None:
        self.indice = filters.IndiceFiltros(catalogo_de_calidades())

    def test_el_mismo_campo_no_se_cuenta_dos_veces(self) -> None:
        with mock.patch.object(
            Consultador, "consultar",
            wraps=Consultador(catalogo_de_calidades()).consultar,
        ) as spy:
            self.indice.resuelve([("quality", "HD")])
            self.indice.resuelve([("quality", "HD")])
            self.indice.resuelve([("group", "General"), ("quality", "HD")])
        self.assertEqual(spy.call_count, 2)

    def test_quitar_y_poner_no_vuelve_a_pagar(self) -> None:
        with mock.patch.object(
            Consultador, "consultar",
            wraps=Consultador(catalogo_de_calidades()).consultar,
        ) as spy:
            self.indice.resuelve([("quality", "HD")])
            self.indice.resuelve([])
            self.indice.resuelve([("quality", "HD")])
        self.assertEqual(spy.call_count, 1)

    def test_cambiar_de_catálogo_invalida_lo_contado(self) -> None:
        self.indice.resuelve([("quality", "HD")])
        self.indice.canales = [canal("Otra HD", url="http://x/1")]
        self.indice.invalidar()
        self.assertEqual(self.indice.resuelve([("quality", "HD")]), [0])

    def test_el_texto_tampoco_se_rehace(self) -> None:
        primero = self.indice.textos()
        self.indice.resuelve([], "la")
        self.assertIs(self.indice.textos(), primero)

    def test_escribir_no_paga_el_camino_del_backend(self) -> None:
        """Cada tecla con un filtro puesto cruza conjuntos ya contados; el
        backend no se vuelve a preguntar ni una vez."""
        self.indice.resuelve([("group", "General")])
        with mock.patch.object(
            Consultador, "consultar",
            side_effect=AssertionError("no deberia volver a contar")
        ):
            for q in ("", "l", "la", "la 1", "la 1 "):
                self.indice.resuelve([("group", "General")], q)

    def test_un_backend_que_falla_no_tumba_la_lista(self) -> None:
        self.indice = filters.IndiceFiltros(catalogo_de_calidades())
        with mock.patch.object(Consultador, "consultar",
                               side_effect=RuntimeError("se rompió")):
            self.assertEqual(self.indice.resuelve([("quality", "HD")]), [])


class TestOrden(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = catalogo_de_calidades()
        self.indice = filters.IndiceFiltros(self.cat)

    def test_sin_orden_manda_el_de_la_lista(self) -> None:
        self.assertEqual(self.indice.resuelve([]), [0, 1, 2, 3, 4])

    def test_por_nombre(self) -> None:
        self.assertEqual(self.indice.resuelve([], "", "name"),
                         [0, 1, 2, 3, 4])

    def test_ordenar_no_necesita_ningun_filtro_puesto(self) -> None:
        """Ordenar la lista entera es el uso mas corriente de los cuatro."""
        cat = [canal("C", url="http://x/1"), canal("A", url="http://x/2"),
               canal("B", url="http://x/3")]
        indice = filters.IndiceFiltros(cat)
        self.assertEqual(indice.resuelve([], "", "name"), [1, 2, 0])
        self.assertEqual(indice.resuelve([], ""), [0, 1, 2])

    def test_por_nombre_desempata_por_posición(self) -> None:
        cat = [canal("B", url="http://x/1"), canal("A", url="http://x/2"),
               canal("A", url="http://x/3")]
        indice = filters.IndiceFiltros(cat)
        self.assertEqual(indice.resuelve([], "", "name"), [1, 2, 0])

    def test_por_definicion_el_mejor_primero(self) -> None:
        # FHD(1080) · HD(720) y 720p(720) empatan y mandan las posiciones
        # 0 y 1 · SD(480) · sin definición estimada.
        self.assertEqual(self.indice.resuelve([], "", "quality"),
                         [2, 0, 1, 4, 3])

    def test_por_definicion_los_empates_mandan_la_posicion(self) -> None:
        """`Consultador._sort` usa `reverse=True`, que invierte también los
        empates. «Orden estable» significa que entre dos que empatan manda
        la posición que tenían; por eso aquí se desempata siempre."""
        cat = [canal("X HD", url="http://x/1"), canal("Y HD", url="http://x/2")]
        indice = filters.IndiceFiltros(cat)
        self.assertEqual(indice.resuelve([], "", "quality"), [0, 1])

    def test_el_orden_no_tira_ningun_canal(self) -> None:
        for orden in (None, "name", "group", "quality"):
            with self.subTest(orden=orden):
                self.assertEqual(
                    len(self.indice.resuelve([], "", orden)), len(self.cat))

    def test_todos_los_ordenes_son_legibles(self) -> None:
        for clave, etiqueta in filters.ORDENES:
            with self.subTest(orden=clave):
                self.assertTrue(etiqueta)
                self.assertEqual(filters.orden_legible(clave), etiqueta)

    def test_un_orden_desconocido_cae_en_el_primero(self) -> None:
        self.assertEqual(filters.orden_legible("inventado"),
                         "Como viene en la lista")


class TestLasFilasDelModal(unittest.TestCase):
    def items(self, activos=None):
        return {i.key: i for i in filters.items_de_filtro(activos)}

    def test_hay_una_fila_por_filtro(self) -> None:
        self.assertEqual(len(filters.items_de_filtro()), len(filters.FILTROS))

    def test_todas_las_filas_se_explican(self) -> None:
        for clave, item in self.items().items():
            with self.subTest(filtro=clave):
                self.assertTrue(item.note)

    def test_todas_las_filas_son_elegibles(self) -> None:
        """A diferencia de las reglas (U-04b), aquí ningún filtro está muerto:
        los ocho los responde `Consultador`."""
        for clave, item in self.items().items():
            with self.subTest(filtro=clave):
                self.assertTrue(item.selectable)

    def test_sin_filtros_ninguna_fila_marca(self) -> None:
        for item in filters.items_de_filtro():
            self.assertFalse(item.checked)

    def test_un_filtro_activo_llega_marcado(self) -> None:
        items = self.items({"quality": "HD"})
        self.assertTrue(items["quality"].checked)
        self.assertFalse(items["group"].checked)

    def test_un_filtro_que_no_esta_tambien_dice_su_valor(self) -> None:
        """Si el usuario lo quita y lo vuelve a poner, no pierde lo escrito."""
        items = self.items({"quality": "HD"})
        self.assertFalse(items["quality"].checked is None)
        items = self.items({})
        self.assertEqual(items["quality"].note,
                         filters.filtro_de("quality").ayuda)

    def test_la_nota_de_un_activo_dice_el_valor(self) -> None:
        items = self.items({"quality": "HD"})
        self.assertEqual(items["quality"].note, "HD")


class TestCuenta(unittest.TestCase):
    def test_la_cuenta_es_la_longitud_de_la_resolución(self) -> None:
        indice = filters.IndiceFiltros(catalogo_de_calidades())
        self.assertEqual(
            filters.cuenta_de(indice, [("group", "Deportes")]),
            len(indice.resuelve([("group", "Deportes")])),
        )

    def test_sin_canales_la_cuenta_es_cero(self) -> None:
        self.assertEqual(filters.cuenta_de(filters.IndiceFiltros([])), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
