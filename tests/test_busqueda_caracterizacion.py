"""Caracterización de la **búsqueda actual** (AUD-02, Fase 0 del plan P2).

Estas pruebas congelan el comportamiento que existe **hoy** en
`ChannelsScreen._apply_filter` y company, antes de que la Fase 3 (P2.1 búsqueda
avanzada) añada el modelo de consulta tipado, filtros por `tvg-id`/fuente, estados
`epg_status`/`catchup_status`, calidad y búsqueda por título de programa.

No comprueban que el buscador sea bueno: comprueban que **cambiarlo** no rompa
nada que un usuario o un test ya dependa. Y dejan por escrito, como<constante>,
las limitaciones que la Fase 3 va a cerrar (acentos, espacios repetidos, campos
no indexados), para que la comparación antes/después sea posible.

Reglas de esta fase:

1. **Sin red, sin reproductor.** El filtro es una pasada en memoria; además se
   comprueba con un centinela que no pide nada por red (REQ-PERF-002).
2. **Sin `sleep` ni RELOJ.** Los tamaños grandes (100k) se miden por *comportamiento*
   (las cachés no se rehacen), no por tiempo: un umbral de milisegundos en la
   suite sería ruido, no señal.
3. Si alguna prueba **falla** al escribirse, es un bug preexistente y se
   documenta en `docs/sdd/p2-gap-analysis.md`; no se "arregla" aquí.

Casos cubiertos (los que pide el plan):

- Unicode y acentos (`casefold` sí, normalización de diacríticos **no**).
- Espacios repetidos (no se colapsan).
- Mayúsculas en la consulta y en el catálogo.
- 100 000 canales con el índice ya construido.
- Cambio de playlist con el índice ya construido (invalidación por identidad).
- Lo que el buscador **no** mira: `tvg_id`, `attrs` (calidad, catch-up), `url`.
- El filtro no hace red.
"""

from __future__ import annotations

import unittest
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.ui import screens as ui_screens


# ---------------------------------------------------------------------------
# Dobles y utilidades
# ---------------------------------------------------------------------------


class _Favoritos:
    def is_favorite(self, channel):  # noqa: ANN001, ANN201
        return False

    def favorite_urls(self):  # noqa: ANN201
        return set()


class _App:
    def __init__(self) -> None:
        self.favorites = _Favoritos()

    def body_height(self) -> int:
        return 20


def _pantalla(canales: list[Channel]) -> ui_screens.ChannelsScreen:
    return ui_screens.ChannelsScreen(_App(), Playlist(name="Test", channels=canales))


def _buscar(pantalla: ui_screens.ChannelsScreen, q: str) -> list[str]:
    """Aplica `q` como lo hace el usuario y devuelve los nombres visibles."""
    pantalla.query = q
    pantalla.searching = bool(q)
    pantalla._apply_filter()
    return [pantalla.channels[i].name for i in pantalla.visible_idx]


def _catalogo(n: int = 200) -> list[Channel]:
    grupos = ("Noticias", "Deportes", "Cine", "Infantil")
    return [
        Channel(
            name=f"Canal {i} HD",
            url=f"http://a.test/{i}",
            tvg_id=f"canal.{i}.es",
            group=grupos[i % len(grupos)],
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. Unicode, acentos y mayúsculas
# ---------------------------------------------------------------------------


class TestUnicodeYAcentos(unittest.TestCase):
    """`casefold` es el único normalizador que existe hoy."""

    def test_las_mayusculas_de_la_consulta_no_importan(self):
        pantalla = _pantalla(_catalogo(10))
        self.assertEqual(_buscar(pantalla, "CANAL 7"), _buscar(pantalla, "canal 7"))

    def test_el_nombre_del_catalogo_tambien_se_ignora_en_mayusculas(self):
        pantalla = _pantalla([Channel(name="TELEVISION", url="http://a.test/1")])
        self.assertEqual(_buscar(pantalla, "television"), ["TELEVISION"])

    def test_los_acentos_no_se_normalizan(self):
        """Congelado: «Tele» **no** encuentra «Télé» (gap S-01)."""
        pantalla = _pantalla([Channel(name="Télé Noticias", url="http://a.test/1")])
        self.assertEqual(_buscar(pantalla, "tele"), [])
        self.assertEqual(_buscar(pantalla, "télé"), ["Télé Noticias"])

    def test_los_espacios_repetidos_no_se_colapsan(self):
        """Congelado: la consulta es literal (gap S-01)."""
        pantalla = _pantalla([Channel(name="Canal  Deportivo", url="http://a.test/1")])
        self.assertEqual(_buscar(pantalla, "canal deportivo"), [])
        self.assertEqual(_buscar(pantalla, "canal  deportivo"), ["Canal  Deportivo"])

    def test_casefold_que_cambia_la_longitud_no_rompe_la_busqueda(self):
        """`ß` se despliega en `ss` al minusculizar: casa igual, sin crashear."""
        pantalla = _pantalla([Channel(name="Straße 9", url="http://a.test/1")])
        self.assertEqual(_buscar(pantalla, "strasse"), ["Straße 9"])

    def test_emoji_y_nombres_vacios(self):
        pantalla = _pantalla(
            [
                Channel(name="📺 Noticias", url="http://a.test/1"),
                Channel(name="", url="http://a.test/2", group="Cine"),
            ]
        )
        self.assertEqual(_buscar(pantalla, "📺"), ["📺 Noticias"])
        # Una consulta vacía por nombre no filtra lo que sí trae grupo.
        self.assertEqual(_buscar(pantalla, "cine"), [""])

    def test_el_grupo_si_entra_en_la_busqueda(self):
        pantalla = _pantalla(_catalogo(8))
        self.assertEqual(len(_buscar(pantalla, "deportes")), 2)


# ---------------------------------------------------------------------------
# 2. Qué campos se indexan y cuáles no
# ---------------------------------------------------------------------------


class TestCamposIndexados(unittest.TestCase):
    """El texto indexado es exactamente `name + "\\n" + group`."""

    def setUp(self) -> None:
        self.pantalla = _pantalla(
            [
                Channel(
                    name="Canal Uno",
                    url="http://a.test/token-secreto",
                    tvg_id="identificador.unico",
                    tvg_name="Nombre Alternativo",
                    group="Noticias",
                    attrs={"res": "1080", "catchup": "shift"},
                )
            ]
        )

    def test_el_tvg_id_no_se_busca(self):
        """Congelado: `tvg_id` no es buscable (gap S-02)."""
        self.assertEqual(_buscar(self.pantalla, "identificador.unico"), [])

    def test_el_tvg_name_no_se_busca(self):
        self.assertEqual(_buscar(self.pantalla, "nombre alternativo"), [])

    def test_los_atributos_no_se_buscan(self):
        """La calidad y el catch-up viven en `attrs`, fuera del índice."""
        self.assertEqual(_buscar(self.pantalla, "1080"), [])
        self.assertEqual(_buscar(self.pantalla, "shift"), [])

    def test_la_url_no_se_busca(self):
        self.assertEqual(_buscar(self.pantalla, "token-secreto"), [])

    def test_la_url_tampoco_se_busca_a_traves_de_la_etiqueta(self):
        """La etiqueta visible tampoco incluye la URL (y no debe: es un secreto)."""
        self.assertNotIn("token-secreto", "".join(self.pantalla.list.items))

    def test_nombre_y_grupo_si_se_buscan(self):
        self.assertEqual(_buscar(self.pantalla, "canal uno"), ["Canal Uno"])
        self.assertEqual(_buscar(self.pantalla, "noticias"), ["Canal Uno"])


# ---------------------------------------------------------------------------
# 3. Orden estable y caché
# ---------------------------------------------------------------------------


class TestOrdenEstableYCaches(unittest.TestCase):
    def setUp(self) -> None:
        self.canales = _catalogo(500)
        self.pantalla = _pantalla(self.canales)
        # Una primera búsqueda con texto: es la que construye `_search_texts`.
        self.assertTrue(_buscar(self.pantalla, "canal 40"))

    def test_el_orden_es_el_del_playlist(self):
        """Congelado: no hay ordenación; se respeta el del M3U."""
        vistos = _buscar(self.pantalla, "canal 1")
        esperados = [
            c.name for c in self.canales if "canal 1" in (c.name + c.group).casefold()
        ]
        self.assertEqual(vistos, esperados)
        self.assertEqual(vistos[:2], ["Canal 1 HD", "Canal 10 HD"])

    def test_filtrar_no_rehace_las_etiquetas_ni_los_textos(self):
        etiquetas = self.pantalla._labels
        textos = self.pantalla._search_texts
        self.assertIsNotNone(textos, "la primera búsqueda sí debe indexar")
        for q in ("canal", "canal 4", "zzz", ""):
            _buscar(self.pantalla, q)
        self.assertIs(self.pantalla._labels, etiquetas)
        self.assertIs(self.pantalla._search_texts, textos)

    def test_volver_a_sin_query_muestra_todo(self):
        self.assertEqual(len(_buscar(self.pantalla, "canal 40")), 11)
        self.assertEqual(len(_buscar(self.pantalla, "")), 500)

    def test_la_seleccion_sobrevive_si_el_canal_sigue_visible(self):
        self.pantalla.list.selected = 2
        elegido = self.pantalla.current_channel()
        self.pantalla._apply_filter(keep_selection=True)
        self.assertEqual(self.pantalla.current_channel().name, elegido.name)

    def test_sin_keep_selection_se_conserva_la_posicion_no_el_canal(self):
        """Congelado: `set_items(..., keep_selection=True)` mantiene la fila.

        Sin `keep_selection` la *posición* se conserva igual; lo que se pierde es
        la identidad del canal, porque la lista puede haber cambiado.
        """
        self.pantalla.list.selected = 2
        self.pantalla._apply_filter()
        self.assertEqual(self.pantalla.list.selected, 2)

    def test_el_resaltado_apunta_a_la_etiqueta_visible(self):
        self.pantalla.query = "noticias"
        self.pantalla._apply_filter()
        for etiqueta, rangos in zip(self.pantalla.list.items, self.pantalla.list.ranges):
            self.assertTrue(rangos)
            for ini, fin in rangos:
                self.assertIn("Noticias", etiqueta[ini:fin])

    def test_como_mucho_diez_resaltados_por_fila(self):
        for i in range(30):
            self.canales[i].name = "Canal " + " ".join(["x"] * 5)
        pantalla = _pantalla(self.canales)
        pantalla.query = "x"
        pantalla._apply_filter()
        for rangos in pantalla.list.ranges:
            self.assertLessEqual(len(rangos), ui_screens.ChannelsScreen.MAX_RESALTADOS)


# ---------------------------------------------------------------------------
# 4. Cambio de playlist con el índice ya construido
# ---------------------------------------------------------------------------


class TestCambioDePlaylist(unittest.TestCase):
    """La caché se invalida por identidad de la lista, no por contenido."""

    def setUp(self) -> None:
        self.pantalla = _pantalla(_catalogo(50))
        self.pantalla.query = "canal 4"
        self.pantalla._apply_filter()
        self.vistos = len(self.pantalla.list.items)
        self.assertEqual(self.vistos, 11)

    def test_la_lista_fresca_reconstruye_las_caches(self):
        etiquetas = self.pantalla._labels
        textos = self.pantalla._search_texts
        fresca = Playlist(name="Test", channels=_catalogo(80))
        self.pantalla.refresh_from_playlist(fresca)
        self.assertIsNot(self.pantalla._labels, etiquetas)
        self.assertIsNot(self.pantalla._search_texts, textos)
        self.assertEqual(len(self.pantalla.list.items), self.vistos)
        self.assertEqual(len(self.pantalla.channels), 80)

    def test_la_query_se_conserva_al_actualizar(self):
        self.pantalla.refresh_from_playlist(Playlist(name="Test", channels=_catalogo(50)))
        self.assertEqual(self.pantalla.query, "canal 4")
        self.assertTrue(self.pantalla.list.items)

    def test_una_lista_vacia_no_deja_indice_enganoso(self):
        self.pantalla.refresh_from_playlist(Playlist(name="Test", channels=[]))
        self.assertEqual(self.pantalla.list.items, [])
        self.assertIsNone(self.pantalla.current_channel())


# ---------------------------------------------------------------------------
# 5. Volumen
# ---------------------------------------------------------------------------


class TestCienMilCanales(unittest.TestCase):
    """El filtro escala linealmente y las cachés se construyen una sola vez."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.canales = _catalogo(100_000)

    def test_cien_mil_canales_se_filtran_y_se_recuperan(self):
        pantalla = _pantalla(self.canales)
        self.assertEqual(len(_buscar(pantalla, "canal 99999")), 1)
        self.assertEqual(len(_buscar(pantalla, "deportes")), 25_000)
        self.assertEqual(len(_buscar(pantalla, "canal")), 100_000)

    def test_no_se_rehacen_las_caches_en_cada_tecla(self):
        pantalla = _pantalla(self.canales)
        _buscar(pantalla, "c")
        etiquetas = pantalla._labels
        textos = pantalla._search_texts
        for q in ("ca", "can", "cana", "canal", "canal "):
            _buscar(pantalla, q)
        self.assertIs(pantalla._labels, etiquetas)
        self.assertIs(pantalla._search_texts, textos)

    def test_el_grupo_se_filtra_ademas_de_la_busqueda(self):
        pantalla = ui_screens.ChannelsScreen(
            _App(), Playlist(name="Test", channels=self.canales), group="Cine"
        )
        self.assertEqual(len(pantalla.channels), 25_000)
        pantalla.query = "canal 7"
        pantalla._apply_filter()
        self.assertTrue(
            all(pantalla.channels[i].group == "Cine" for i in pantalla.visible_idx)
        )


# ---------------------------------------------------------------------------
# 6. El filtro no hace red (REQ-PERF-002)
# ---------------------------------------------------------------------------


class TestSinRed(unittest.TestCase):
    def test_filtrar_no_abre_ninguna_conexion(self):
        import urllib.request

        pantalla = _pantalla(_catalogo(500))
        with mock.patch.object(
            urllib.request, "urlopen", side_effect=AssertionError("red en el filtro")
        ) as urlopen, mock.patch("socket.socket") as sock:
            for q in ("canal", "deportes", "zzz", ""):
                _buscar(pantalla, q)
        urlopen.assert_not_called()
        sock.assert_not_called()


if __name__ == "__main__":
    unittest.main()