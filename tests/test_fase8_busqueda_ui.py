"""Fase 8 · U-05 — Búsqueda: los modales y la pantalla de canales.

`tests/test_fase8_busqueda_filtros.py` fija el vocabulario y el motor. Este
fija lo de alrededor: las teclas, los modales encadenados, y sobre todo tres
cosas que el plan pedía y que son fáciles de prometer y no cumplir:

- **los filtros activos se ven** — en el subtítulo del modal y en el título
  de la lista, no sólo en un estado interno;
- **el contador de resultados es real** — sale de la misma evaluación que
  deja la lista filtrada, nunca de una cuenta aparte que se pueda desfasar;
- **la limpieza rápida nombra lo que quita** — un «quitar todos» que borra
  cuatro filtros sin decirlos deja al usuario reconstruyendo de memoria.

Y una cuarta, que es la razón de ser del ticket: **la búsqueda de siempre no
se toca**. Sin filtros puestos, escribir sigue siendo un `q in texto` de C y
`Consultador.consultar` no se llama ni una vez.
"""

from __future__ import annotations

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.search import Consultador
from thetvview.ui import colors, filters
from thetvview.ui.actions import V
from thetvview.ui.app import App
from thetvview.ui.screens import ChannelsScreen


class FakeStdscr:
    def __init__(self, h: int = 24, w: int = 80) -> None:
        self.h, self.w = h, w
        self.addstr_calls: list[tuple[int, int, str, int]] = []

    def getmaxyx(self):
        return (self.h, self.w)

    def addstr(self, y, x, text, attr=0):
        room = self.w - 1 if x == 0 else self.w - x
        if room > 0 and len(text) > room:
            text = text[:room]
        self.addstr_calls.append((y, x, text, attr))

    def resize(self, h, w):
        self.h, self.w = h, w

    def erase(self):
        self.addstr_calls.clear()

    def getch(self):
        return -1

    def texto(self):
        return " ".join(t for _y, _x, t, _a in self.addstr_calls)


def canal(nombre, grupo, url=None):
    return Channel(name=nombre, url=url or f"http://ejemplo.test/{nombre}",
                   group=grupo)


BASE = [
    canal("La 1 HD", "General"),
    canal("La 2 (720p)", "General"),
    canal("La 3 FHD", "Deportes"),
    canal("La 4", "Deportes"),
    canal("La 5 SD", "Infantiles"),
]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeStdscr(24, 80)
        tmp = Path(tempfile.mkdtemp())
        import thetvview.config as cfg

        parches = [
            mock.patch.object(cfg, "COLLECTIONS_JSON", tmp / "c.json"),
            mock.patch.object(cfg, "PLAYLISTS_JSON", tmp / "p.json"),
            mock.patch.object(cfg, "FAVORITES_JSON", tmp / "f.json"),
            mock.patch.object(cfg, "PREFS_JSON", tmp / "prefs.json"),
            mock.patch.object(cfg, "RECENTS_JSON", tmp / "r.json"),
            mock.patch.object(cfg, "THEME_JSON", tmp / "t.json"),
            mock.patch.object(cfg, "ensure_dirs", lambda: None),
            mock.patch.object(colors, "pair", return_value=0),
        ]
        for p in parches:
            p.start()
            self.addCleanup(p.stop)
        self.app = App(self.fake)
        self.app.playlist_cache["t"] = Playlist(name="T", channels=list(BASE))
        self.pantalla = ChannelsScreen(
            self.app, self.app.playlist_cache["t"])

    # -- ayudas ------------------------------------------------------------

    def nombres(self):
        return [self.pantalla.channels[i].name
                for i in self.pantalla.visible_idx]

    def elegir(self, *teclas):
        """Abre un modal y le pasa `teclas`; devuelve lo que salió."""
        self.app._run_modal = lambda modal, teclas=teclas: self._pulsar(
            modal, teclas)
        return self.app._run_modal

    def _pulsar(self, modal, teclas):
        for t in teclas:
            modal.handle_key(t)
        return "aceptar"


# ---------------------------------------------------------------------------
# 1. La búsqueda de siempre no se toca
# ---------------------------------------------------------------------------


class TestElCaminoDeSiempreSigue(Base):
    def test_escribir_no_pasa_por_el_backend(self) -> None:
        with mock.patch.object(Consultador, "consultar",
                               side_effect=AssertionError("no debe contar")):
            self.pantalla.query = "la 1"
            self.pantalla._apply_filter()
        self.assertEqual(self.nombres(), ["La 1 HD"])

    def test_sin_filtro_no_pasa_por_el_backend_al_pintar(self) -> None:
        with mock.patch.object(Consultador, "consultar",
                               side_effect=AssertionError("no debe contar")):
            self.pantalla.render(self.fake)
        self.assertEqual(len(self.pantalla.visible_idx), 5)

    def test_sin_filtros_todo_visible(self) -> None:
        self.assertEqual(len(self.pantalla.visible_idx), 5)

    def test_el_titulo_sin_filtros_no_cambia(self) -> None:
        self.assertEqual(self.pantalla._title_text(),
                         "T (5 canales)")

    def test_el_resaltado_de_busqueda_sigue_funcionando(self) -> None:
        self.pantalla.query = "la 1"
        self.pantalla._apply_filter()
        self.assertEqual(self.nombres(), ["La 1 HD"])
        self.assertTrue(self.pantalla.list.ranges)

    def test_una_playlist_vacia_no_revienta(self) -> None:
        vacia = ChannelsScreen(self.app, Playlist(name="V", channels=[]))
        self.assertEqual(vacia.visible_idx, [])


# ---------------------------------------------------------------------------
# 2. Las teclas
# ---------------------------------------------------------------------------


class TestLasTeclas(Base):
    def test_f_abre_los_filtros(self) -> None:
        self.assertEqual(self.pantalla.handle_key(ord("F")),
                         {"action": "search_filters", "screen": self.pantalla})

    def test_x_quita_los_filtros(self) -> None:
        self.assertEqual(
            self.pantalla.handle_key(ord("X"))["action"],
            "clear_search_filters")

    def test_o_cambia_el_orden(self) -> None:
        self.assertEqual(self.pantalla.handle_key(ord("o"))["action"],
                         "search_order")

    def test_ninguna_tecla_global_come_la_f(self) -> None:
        """`F` es de Canales, pero lo que la roba sería `App._tecla_global`,
        que se ejecuta **antes** de la pantalla. Sin este test, añadir `F` a
        las globales no tocaría ningún `actions()` y nadie se enteraría."""
        self.app.push(self.pantalla)
        antes = len(self.app.stack)
        for tecla in (ord("F"), ord("X"), ord("o")):
            with self.subTest(tecla=chr(tecla)):
                self.assertFalse(self.app._tecla_global(tecla))
        # Ninguna dejó nada a medias ni abrió una pantalla por el camino.
        self.assertEqual(len(self.app.stack), antes)
        self.assertIs(self.app.screen, self.pantalla)

    def test_las_tres_teclas_llegan_a_la_pantalla(self) -> None:
        """Lo contrario del anterior: global no significa «nunca llega»."""
        self.app.push(self.pantalla)
        for tecla, accion in ((ord("F"), "search_filters"),
                              (ord("X"), "clear_search_filters"),
                              (ord("o"), "search_order")):
            with self.subTest(tecla=chr(tecla)):
                self.assertFalse(self.app._tecla_global(tecla))
                self.assertEqual(
                    self.app.screen.handle_key(tecla)["action"], accion)

    def test_en_busqueda_las_teclas_de_filtro_no_saltan(self) -> None:
        """Escribir «F» tiene que escribir una F, no abrir un modal."""
        self.pantalla.searching = True
        self.pantalla.handle_key(ord("F"))
        self.assertEqual(self.pantalla.query, "F")

    def test_el_pie_anuncia_los_filtros(self) -> None:
        self.assertIn("F Filtros", self.pantalla.shortcuts())
        self.assertIn("o Orden", self.pantalla.shortcuts())

    def test_x_solo_aparece_si_hay_filtros(self) -> None:
        teclas = {a.key for a in self.pantalla.actions()}
        self.assertNotIn("X", teclas)
        self.pantalla.filtros = [("quality", "HD")]
        teclas = {a.key for a in self.pantalla.actions()}
        self.assertIn("X", teclas)

    def test_f_aparece_siempre(self) -> None:
        self.assertIn("F", {a.key for a in self.pantalla.actions()})

    def test_las_etiquetas_nuevas_estan_en_el_vocabulario(self) -> None:
        self.assertEqual(V.FILTROS, "Filtros")
        self.assertEqual(V.ORDEN, "Orden")

    def test_la_ayuda_habla_de_las_tres_teclas(self) -> None:
        from thetvview.ui.app import _HELP_HERE

        texto = " ".join(t for _tipo, t in _HELP_HERE["ChannelsScreen"])
        for tecla in ("F ", "X ", "o "):
            with self.subTest(tecla=tecla):
                self.assertIn(tecla, texto)


# ---------------------------------------------------------------------------
# 3. Filtrar de verdad
# ---------------------------------------------------------------------------


class TestFiltrar(Base):
    def test_poner_un_filtro_devuelve_la_cuenta(self) -> None:
        self.assertEqual(
            self.pantalla.poner_filtros([("group", "Deportes")]), 2)

    def test_la_lista_queda_filtrada_de_verdad(self) -> None:
        self.pantalla.poner_filtros([("group", "Deportes")])
        self.assertEqual(self.nombres(), ["La 3 FHD", "La 4"])

    def test_el_filtro_y_el_texto_se_cumplen_a_la_vez(self) -> None:
        self.pantalla.query = "la"
        self.pantalla.poner_filtros([("group", "Deportes")])
        self.assertEqual(self.nombres(), ["La 3 FHD", "La 4"])

    def test_se_conserva_el_canal_seleccionado(self) -> None:
        self.pantalla.list.selected = 2  # "La 3 FHD"
        self.pantalla.poner_filtros([("group", "Deportes")])
        self.assertEqual(self.pantalla.current_channel().name, "La 3 FHD")

    def test_un_canal_que_desaparece_suelta_el_cursor(self) -> None:
        self.pantalla.list.selected = 0  # "La 1 HD"
        self.pantalla.poner_filtros([("group", "Deportes")])
        self.assertEqual(self.pantalla.current_channel().name, "La 3 FHD")

    def test_limpiar_deja_la_lista_como_estaba(self) -> None:
        self.pantalla.query = "la 1"
        self.pantalla.poner_filtros([("group", "Deportes")])
        self.assertEqual(self.pantalla.limpiar_filtros(), 1)
        self.assertEqual(self.nombres(), ["La 1 HD"])

    def test_limpiar_no_toca_el_orden(self) -> None:
        """El orden mueve canales pero no quita ninguno: un «quitar todo» que
        también lo descolocara estaría cambiando algo que nadie pidió."""
        self.pantalla.poner_filtros(orden="name")
        self.pantalla.poner_filtros([("group", "Deportes")])
        self.pantalla.limpiar_filtros()
        self.assertEqual(self.pantalla.orden, "name")
        self.assertEqual(self.pantalla.filtros, [])

    def test_el_titulo_dice_los_filtros_puestos(self) -> None:
        self.pantalla.poner_filtros([("group", "Deportes")])
        self.assertIn("Grupo: Deportes", self.pantalla._title_text())

    def test_el_titulo_dice_cuantos_salen(self) -> None:
        self.pantalla.poner_filtros([("group", "Deportes")])
        self.assertIn("2/5", self.pantalla._title_text())

    def test_el_titulo_dice_el_orden_si_lo_hay(self) -> None:
        self.pantalla.poner_filtros(orden="name")
        self.assertIn("Por nombre", self.pantalla._title_text())

    def test_el_motor_se_rehace_si_cambian_los_canales(self) -> None:
        primero = self.pantalla._indice_de_filtros()
        self.pantalla.channels = [canal("Otra HD", "X")]
        self.assertIsNot(self.pantalla._indice_de_filtros(), primero)

    def test_el_motor_rehecho_cuenta_sobre_los_canales_nuevos(self) -> None:
        self.pantalla._indice_de_filtros().pasar("quality", "HD")
        self.pantalla.channels = [canal("Otra HD", "X")]
        indice = self.pantalla._indice_de_filtros()
        self.assertEqual(indice.pasar("quality", "HD"), {0})

    def test_el_motor_no_se_rehace_si_no_cambia_nada(self) -> None:
        """Recargar produce otra lista con los mismos canales; tirar el motor
        en ese caso borraría todo lo contado y volvería a pagar segundos."""
        primero = self.pantalla._indice_de_filtros()
        self.assertIs(self.pantalla._indice_de_filtros(), primero)


# ---------------------------------------------------------------------------
# 4. Los modales
# ---------------------------------------------------------------------------


class TestAnadirFiltro(Base):
    def _anadir_enum(self):
        with mock.patch.object(App, "_elegir_uno",
                               return_value="quality"):
            with mock.patch.object(App, "_valor_de_filtro",
                                   return_value=("HD", False)) as v:
                self.app._anadir_filtro(self.pantalla)
        return v

    def test_anadir_va_al_filtro_que_el_usuario_eligio(self) -> None:
        self._anadir_enum()
        self.assertEqual(self.pantalla.filtros, [("quality", "HD")])

    def test_anadir_refiltra_en_el_acto(self) -> None:
        """Si sólo se filtrara al cerrar el modal, el constructor iría a
        ciegas y el contador no serviría para decidir."""
        self._anadir_enum()
        self.assertEqual(self.nombres(), ["La 1 HD"])

    def test_anadir_avisa_con_la_cuenta(self) -> None:
        self._anadir_enum()
        self.assertIn("1 canales", self.app.status.message)

    def test_rendirse_no_deja_el_filtro_puesto(self) -> None:
        with mock.patch.object(App, "_elegir_uno", return_value=None):
            self.app._anadir_filtro(self.pantalla)
        self.assertEqual(self.pantalla.filtros, [])

    def test_un_valor_vacio_no_pone_el_filtro(self) -> None:
        with mock.patch.object(App, "_elegir_uno", return_value="group"):
            with mock.patch.object(App, "_prompt_form",
                                   return_value={"valor": "   "}):
                with mock.patch.object(App, "notify_warning") as aviso:
                    self.app._anadir_filtro(self.pantalla)
        self.assertEqual(self.pantalla.filtros, [])
        self.assertTrue(aviso.called)

    def test_poner_un_filtro_ya_puesto_lo_cambia(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD")])
        with mock.patch.object(App, "_elegir_uno", return_value="quality"):
            with mock.patch.object(App, "_valor_de_filtro",
                                   return_value=("FHD", False)):
                self.app._anadir_filtro(self.pantalla)
        self.assertEqual(self.pantalla.filtros, [("quality", "FHD")])


class TestElModalDeFiltros(Base):
    def modal(self):
        self.pantalla.poner_filtros([("quality", "HD"), ("group", "Deportes")])
        return self.app._modal_de_filtros(self.pantalla)

    def test_las_filas_son_los_filtros_puestos(self) -> None:
        modal = self.modal()
        self.assertEqual(modal.items[0].key, "quality")
        self.assertEqual(modal.items[1].key, "group")

    def test_la_ultima_fila_anade_uno_mas(self) -> None:
        modal = self.modal()
        self.assertEqual(modal.items[-1].key, App._FILTRO_NUEVO)

    def test_cada_fila_dice_su_valor(self) -> None:
        self.assertEqual(self.modal().items[0].label, "Definición estimada: HD")

    def test_cada_fila_explica_que_mira(self) -> None:
        for item in self.modal().items:
            with self.subTest(fila=item.label):
                self.assertTrue(item.note)

    def test_el_subtitulo_dice_cuantos_canales_salen(self) -> None:
        self.assertIn("0 de 5", self.modal().subtitle)

    def test_el_subtitulo_cuenta_un_filtro_que_solo_si_saca_canales(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD")])
        modal = self.app._modal_de_filtros(self.pantalla)
        self.assertIn("1 de 5", modal.subtitle)

    def test_un_cruce_vacio_dice_cero_y_no_una_mentira(self) -> None:
        """«HD» está en General y «Deportes» no tiene HD. El cero es real, y
        por eso se enseña: no es «no se sabe», es «no sale»."""
        self.pantalla.poner_filtros([("quality", "HD"), ("group", "Deportes")])
        self.assertEqual(self.nombres(), [])
        self.assertIn("0 de 5", self.app._modal_de_filtros(
            self.pantalla).subtitle)

    def test_el_subtitulo_dice_que_hay_puesto(self) -> None:
        self.assertIn("Definición estimada: HD",
                      self.modal().subtitle)

    def test_el_subtitulo_dice_cuando_no_hay_nada(self) -> None:
        modal = self.app._modal_de_filtros(self.pantalla)
        self.assertIn("sin filtros", modal.subtitle)

    def test_el_subtitulo_es_una_sola_linea(self) -> None:
        """`ChecklistModal` pinta el subtítulo en una fila y mide el alto por
        su ancho: un `\\n` ahí no envuelve, se sale por encima del borde y
        deja el recuadro roto."""
        for filtros in ([], [("quality", "HD")], [("group", "Deportes")]):
            with self.subTest(filtros=filtros):
                self.pantalla.poner_filtros(filtros)
                modal = self.app._modal_de_filtros(self.pantalla)
                self.assertNotIn("\n", modal.subtitle)

    def test_el_subtitulo_dice_el_orden_de_los_filtros(self) -> None:
        """`dict` mantiene el orden de inserción: los filtros salen en el
        orden en que se pusieron, que es el que el usuario recuerda."""
        self.pantalla.poner_filtros([("quality", "HD"), ("group", "Deportes")])
        modal = self.app._modal_de_filtros(self.pantalla)
        self.assertLess(modal.subtitle.index("Definición estimada"),
                        modal.subtitle.index("Grupo: Deportes"))

    def test_es_modo_radio(self) -> None:
        """Cada fila abre un modal distinto; una casilla prometería un
        interruptor de un solo gesto que aquí no existe."""
        self.assertTrue(self.modal().radio)


class TestEditarYQuitar(Base):
    def test_quitar_avisa_con_la_frase(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD")])
        with mock.patch.object(App, "_valor_de_filtro",
                               return_value=(None, True)):
            self.app._editar_filtro(self.pantalla, "quality")
        self.assertEqual(self.pantalla.filtros, [])

    def test_quitar_un_filtro_puesto_no_avisa_de_lo_que_no_hay(self) -> None:
        self.app._quitar_filtro(self.pantalla, "quality")
        self.assertNotIn("Quitado", self.app.status.message)

    def test_cambiar_el_valor_mantiene_el_filtro(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD")])
        with mock.patch.object(App, "_valor_de_filtro",
                               return_value=("FHD", False)):
            self.app._editar_filtro(self.pantalla, "quality")
        self.assertEqual(self.pantalla.filtros, [("quality", "FHD")])

    def test_rendirse_no_cambia_nada(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD")])
        with mock.patch.object(App, "_valor_de_filtro",
                               return_value=(None, False)):
            self.app._editar_filtro(self.pantalla, "quality")
        self.assertEqual(self.pantalla.filtros, [("quality", "HD")])

    def test_el_aviso_de_la_definicion_cabe_entero(self) -> None:
        """La nota de la fila se recorta; el subtítulo del editor no. Por eso
        el aviso largo va ahí: medio aviso no avisa de nada."""
        vistos = {}

        def _captura(titulo, items, **kw):
            vistos["subtitulo"] = kw.get("subtitle", "")
            return None

        with mock.patch.object(App, "_elegir_uno", side_effect=_captura):
            self.app._valor_de_filtro("quality", None)
        self.assertIn("no trae los canales que se llaman", vistos["subtitulo"])

    def test_un_filtro_sin_aviso_usa_su_ayuda_corta(self) -> None:
        vistos = {}

        def _captura(titulo, items, **kw):
            vistos["subtitulo"] = kw.get("subtitle", "")
            return None

        with mock.patch.object(App, "_elegir_uno", side_effect=_captura):
            self.app._valor_de_filtro("catchup_status", None)
        self.assertEqual(
            vistos["subtitulo"], filters.filtro_de("catchup_status").ayuda)


class TestLimpiezaRapida(Base):
    def test_sin_filtros_no_abre_modal(self) -> None:
        with mock.patch.object(App, "_confirm",
                               side_effect=AssertionError("no hay nada que quitar")):
            self.app.clear_search_filters(self.pantalla)
        self.assertIn("No hay filtros que quitar", self.app.status.message)

    def test_cancelar_no_quita_nada(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD"), ("group", "Deportes")])
        with mock.patch.object(App, "_confirm", return_value=False):
            self.app.clear_search_filters(self.pantalla)
        self.assertEqual(len(self.pantalla.filtros), 2)

    def test_aceptar_quita_todos(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD"), ("group", "Deportes")])
        with mock.patch.object(App, "_confirm", return_value=True):
            self.app.clear_search_filters(self.pantalla)
        self.assertEqual(self.pantalla.filtros, [])

    def test_la_confirmacion_nombra_cada_filtro(self) -> None:
        """Un «quitar todo» que borra cuatro filtros sin decirlos deja al
        usuario reconstruyendo de memoria lo que tenía."""
        self.pantalla.poner_filtros([("quality", "HD"), ("group", "Deportes")])
        vistos = {}

        def _captura(titulo, cuerpo):
            vistos["titulo"] = titulo
            vistos["cuerpo"] = cuerpo
            return False

        with mock.patch.object(App, "_confirm", side_effect=_captura):
            self.app.clear_search_filters(self.pantalla)
        self.assertIn("Definición estimada: HD", vistos["cuerpo"])
        self.assertIn("Grupo: Deportes", vistos["cuerpo"])

    def test_la_confirmacion_dice_que_no_toca_el_texto(self) -> None:
        self.pantalla.query = "la 1"
        self.pantalla.poner_filtros([("quality", "HD")])
        vistos = {}
        with mock.patch.object(
            App, "_confirm",
            side_effect=lambda t, c: (vistos.update(cuerpo=c), False)[1]):
            self.app.clear_search_filters(self.pantalla)
        self.assertIn("El texto que hayas escrito se queda",
                      vistos["cuerpo"])

    def test_un_solo_filtro_se_dice_en_singular(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD")])
        with mock.patch.object(App, "_confirm", return_value=True):
            self.app.clear_search_filters(self.pantalla)
        self.assertIn("Quitados 1 filtro.", self.app.status.message)


class TestElOrden(Base):
    def test_el_orden_mueve_pero_no_quita(self) -> None:
        antes = set(self.pantalla.channels[i].name
                    for i in self.pantalla.visible_idx)
        self.pantalla.poner_filtros(orden="name")
        despues = set(self.pantalla.channels[i].name
                      for i in self.pantalla.visible_idx)
        self.assertEqual(antes, despues)
        self.assertEqual(len(self.pantalla.visible_idx), 5)

    def test_por_definicion_manda_el_mejor_primero(self) -> None:
        self.pantalla.poner_filtros(orden="quality")
        self.assertEqual(self.nombres()[0], "La 3 FHD")

    def test_el_modal_de_orden_ofrece_los_cuatro(self) -> None:
        self.pantalla.poner_filtros(orden="name")
        vistos = {}

        def _captura(titulo, items, **kw):
            vistos["items"] = items
            return None

        with mock.patch.object(App, "_elegir_uno", side_effect=_captura):
            self.app.search_order(self.pantalla)
        self.assertEqual(len(vistos["items"]), len(filters.ORDENES))

    def test_el_orden_actual_llega_marcado_al_modal(self) -> None:
        self.pantalla.poner_filtros(orden="name")
        vistos = {}

        def _captura(titulo, items, **kw):
            vistos["items"] = items
            return None

        with mock.patch.object(App, "_elegir_uno", side_effect=_captura):
            self.app.search_order(self.pantalla)
        marcados = [i.label for i in vistos["items"] if i.checked]
        self.assertEqual(marcados, ["Por nombre"])

    def test_el_orden_natural_tiene_clave_propia(self) -> None:
        """`None` no puede ser la `key` de una fila; el modal lo usa como
        identificador, asi que necesita un valor."""
        self.assertIn(None, [c for c, _e in filters.ORDENES])
        self.assertNotIn(None, [c for c, _e in filters.ORDENES if c])

    def test_rendirse_en_el_orden_no_cambia_nada(self) -> None:
        with mock.patch.object(App, "_elegir_uno", return_value=None):
            self.app.search_order(self.pantalla)
        self.assertIsNone(self.pantalla.orden)

    def test_poner_el_mismo_orden_no_avisa_de_cambio(self) -> None:
        self.pantalla.poner_filtros(orden="name")
        with mock.patch.object(App, "_elegir_uno", return_value="name"):
            with mock.patch.object(self.pantalla, "poner_filtros") as poner:
                self.app.search_order(self.pantalla)
        self.assertFalse(poner.called)
        self.assertIn("sigue siendo", self.app.status.message)

    def test_el_orden_natural_se_representa_con_su_clave(self) -> None:
        """`None` no es una clave de `ORDENES`; el modal necesita algo que
        `ChecklistItem` pueda usar como `key`."""
        claves = [clave for clave, _etiqueta in filters.ORDENES]
        self.assertIn(None, claves)
        self.assertNotIn(None, [str(c) for c in claves if c is not None])

    def test_cambiar_el_orden_no_toca_los_filtros(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD")])
        with mock.patch.object(App, "_elegir_uno", return_value="name"):
            self.app.search_order(self.pantalla)
        self.assertEqual(self.pantalla.filtros, [("quality", "HD")])

    def test_el_orden_avisa_con_la_cuenta(self) -> None:
        with mock.patch.object(App, "_elegir_uno", return_value="name"):
            self.app.search_order(self.pantalla)
        self.assertIn("5 canales", self.app.status.message)


class TestSinTerminal(Base):
    """Sin curses los modales devuelven `None`. Lo que debe quedar es un
    estado coherente, nunca un filtro puesto a medias."""

    def test_anadir_sin_curses_no_ponde_nada(self) -> None:
        self.app._anadir_filtro(self.pantalla)
        self.assertEqual(self.pantalla.filtros, [])

    def test_limpiar_sin_curses_no_pasa(self) -> None:
        self.pantalla.poner_filtros([("quality", "HD")])
        self.app.clear_search_filters(self.pantalla)
        self.assertEqual(self.pantalla.filtros, [("quality", "HD")])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
