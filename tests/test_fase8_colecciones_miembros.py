"""Fase 8 · U-03b — Meter y sacar canales de una colección manual.

U-03 dejó las colecciones manuales **vivas pero inertes**: se creaban, se
renombraban y se borraban, pero no había ninguna forma de meter un canal
dentro. Una colección vacía no es una colección, así que este ticket cierra
el hueco antes de U-04 (que es de reglas, no de membresía).

Dos caminos, porque el usuario está en dos sitios distintos:

- desde **Canales**, `C` elige en qué colecciones está el canal de abajo;
- desde **Colecciones**, `m` elige qué canales se quedan en la de arriba.

Y dos reglas que gobiernan el widget nuevo (`ChecklistModal`):

1. Una casilla **no** se marca hasta que el cambio está guardado. Si el gestor
   rechaza, la casilla se queda como estaba y se enseña el motivo: una casilla
   que dice «sí» sobre un cambio que no ocurrió es la peor clase de mentira de
   una interfaz.
2. Lo que **no** se puede tocar aparece con su motivo al lado, no desaparece.
   Una colección inteligente o del sistema que no aparece en la lista parece
   un fallo; una que aparece marcada «la decide su regla» es una respuesta.
"""

from __future__ import annotations

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.channel_identity import channel_identity
from thetvview.collections.manager import ColeccionesManager, ColeccionesManagerError
from thetvview.models import Channel, Playlist
from thetvview.ui import colors
from thetvview.ui.app import App
from thetvview.ui.screens import ChannelsScreen, CollectionsScreen
from thetvview.ui.widgets import ChecklistItem, ChecklistModal, checklist_actions


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeStdscr:
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

    def resize(self, h, w):
        self.h = h
        self.w = w

    def erase(self):
        self.addstr_calls.clear()

    def getch(self):
        return -1

    def texto(self):
        return " ".join(t for _y, _x, t, _a in self.addstr_calls)


def canal(nombre="Canal X", grupo="Noticias") -> Channel:
    return Channel(name=nombre, url="http://example.test/" + nombre, group=grupo)


def identidad(canal: Channel) -> str:
    return str(channel_identity(canal))


def playlist(*canales) -> Playlist:
    return Playlist(name="Test", channels=list(canales) or [canal("La 1")])


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeStdscr(24, 80)
        tmp = Path(tempfile.mkdtemp())
        import thetvview.config as cfg

        patches = [
            mock.patch.object(cfg, "COLLECTIONS_JSON", tmp / "collections.json"),
            mock.patch.object(cfg, "PLAYLISTS_JSON", tmp / "p.json"),
            mock.patch.object(cfg, "FAVORITES_JSON", tmp / "f.json"),
            mock.patch.object(cfg, "PREFS_JSON", tmp / "prefs.json"),
            mock.patch.object(cfg, "RECENTS_JSON", tmp / "r.json"),
            mock.patch.object(cfg, "THEME_JSON", tmp / "theme.json"),
            mock.patch.object(cfg, "ensure_dirs", lambda: None),
            mock.patch.object(colors, "pair", return_value=0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = App(self.fake)
        self.manager = self.app.collections
        self.la1 = canal("La 1")
        self.la2 = canal("La 2", "Deportes")
        self.dep = canal("Dep 1", "Deportes")

    def catalogo(self, *canales) -> list[Channel]:
        """Pone las listas en memoria: es lo que hace `App.catalogo_canales`."""
        canales = list(canales) or [self.la1, self.la2, self.dep]
        self.app.playlist_cache["test"] = playlist(*canales)
        return canales

    def manual(self, nombre="Mía", members=()) -> str:
        return self.manager.crear(nombre, tipo="manual",
                                 members=list(members)).id

    def inteligente(self, nombre="Inteligente") -> str:
        return self.manager.crear(
            nombre, tipo="smart",
            rules={"all": [{"field": "group", "operator": "contains",
                            "value": "Dep"}]},
        ).id

    def sistema(self, nombre="Favoritos") -> str:
        return self.manager.crear(nombre, tipo="system").id


# ---------------------------------------------------------------------------
# 1. El widget: lógica pura, sin curses
# ---------------------------------------------------------------------------


class TestChecklistModal(unittest.TestCase):
    def setUp(self) -> None:
        # `colors.pair` llama a `curses.has_colors()`, que necesita initscr().
        patch = mock.patch.object(colors, "pair", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)
        self.fake = FakeStdscr()
        self.items = [
            ChecklistItem("a", "Primera", checked=True),
            ChecklistItem("b", "Segunda"),
            ChecklistItem("c", "Tercera", note="la decide una regla",
                          selectable=False),
        ]
        self.modal = ChecklistModal("Elegir", self.items)

    def test_la_seleccion_empieza_en_la_primera_marcable(self) -> None:
        self.assertEqual(self.modal.selected, 0)

    def test_bajar_sube_y_se_queda_dentro(self) -> None:
        self.modal.handle_key(curses.KEY_DOWN)
        self.assertEqual(self.modal.selected, 1)
        self.modal.handle_key(curses.KEY_DOWN)
        # Salta la no marcable (índice 2) y se queda en el final.
        self.assertEqual(self.modal.selected, 1)
        self.modal.handle_key(curses.KEY_UP)
        self.assertEqual(self.modal.selected, 0)
        self.modal.handle_key(curses.KEY_UP)
        self.assertEqual(self.modal.selected, 0)

    def test_espacio_marca_y_devuelve_el_nuevo_estado(self) -> None:
        self.assertEqual(self.modal.handle_key(ord(" ")), ("toggle", 0, False))
        # El segundo Espacio devuelve lo mismo: el widget no se auto-marca, y
        # quien mueve la casilla es quien guardó el cambio.
        self.modal.set_checked(0, False)
        self.assertEqual(self.modal.handle_key(ord(" ")), ("toggle", 0, True))

    def test_enter_tambien_marca(self) -> None:
        self.assertEqual(
            self.modal.handle_key(curses.KEY_ENTER), ("toggle", 0, False)
        )

    def test_una_fila_no_marcable_no_se_puede_marcar(self) -> None:
        """`C` no puede añadir a una colección inteligente: no miente ni falla."""
        self.modal.selected = 2
        self.assertIsNone(self.modal.handle_key(ord(" ")))
        self.assertFalse(self.modal.items[2].checked)

    def test_esc_cierra(self) -> None:
        self.assertEqual(self.modal.handle_key(27), "cerrar")

    def test_una_tecla_sin_nada_devuelve_none(self) -> None:
        for tecla in (ord("z"), curses.KEY_F1, -1):
            with self.subTest(tecla=tecla):
                self.assertIsNone(self.modal.handle_key(tecla))

    def test_sin_elementos_nada_revienta(self) -> None:
        vacio = ChecklistModal("Vacío", [])
        for tecla in (ord(" "), curses.KEY_ENTER, curses.KEY_DOWN,
                      curses.KEY_UP, 27):
            with self.subTest(tecla=tecla):
                vacio.handle_key(tecla)
        vacio.render(self.fake)

    def test_solo_elementos_no_marcables_nada_revienta(self) -> None:
        solo = ChecklistModal(
            "Todos no",
            [ChecklistItem("a", "A", selectable=False)],
        )
        self.assertEqual(solo.handle_key(curses.KEY_DOWN), None)
        self.assertIsNone(solo.handle_key(ord(" ")))

    def test_el_estado_solo_cambia_cuando_se_lo_piden(self) -> None:
        """El widget no se auto-marca: quien guarda es `App`.

        Si el gestor rechaza el cambio, la casilla tiene que quedarse como
        estaba. Marcar antes de guardar sería enseñar un «sí» que no ocurrió.
        """
        modal = ChecklistModal("X", [ChecklistItem("a", "A")])
        modal.handle_key(ord(" "))
        self.assertFalse(modal.items[0].checked)
        modal.set_checked(0, True)
        self.assertTrue(modal.items[0].checked)

    def test_set_checked_sobre_un_indice_inexistente_no_revienta(self) -> None:
        self.modal.set_checked(99, True)
        self.modal.set_checked(-5, True)

    def test_el_error_se_enseña_y_se_borra_al_mover(self) -> None:
        self.modal.set_error("No se pudo guardar.")
        self.assertIn("No se pudo guardar.", self.modal.text())
        self.modal.handle_key(curses.KEY_DOWN)
        self.assertNotIn("No se pudo guardar.", self.modal.text())

    def test_el_texto_dice_marcado_y_no_marcado(self) -> None:
        texto = self.modal.text()
        self.assertIn("✓", texto)
        self.assertIn("Primera", texto)
        self.assertIn("la decide una regla", texto)


class TestChecklistModalRender(unittest.TestCase):
    def setUp(self) -> None:
        # `colors.pair` llama a `curses.has_colors()`, que necesita initscr().
        patch = mock.patch.object(colors, "pair", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)
        self.fake = FakeStdscr()
        self.modal = ChecklistModal(
            "Colecciones de «La 1»",
            [ChecklistItem(str(i), f"Canal {i}", checked=i % 2 == 0)
             for i in range(30)],
            subtitle="Marca las que quieras",
        )

    def test_no_revienta_en_terminales_diminutas(self) -> None:
        for alto, ancho in ((10, 40), (24, 80), (30, 120), (7, 20), (5, 10)):
            with self.subTest(terminal=f"{alto}x{ancho}"):
                self.fake.resize(alto, ancho)
                self.modal.render(self.fake)
                self.fake.erase()

    def test_una_lista_larga_cabe_en_la_alta_disponible(self) -> None:
        """30 elementos en 10 filas: se recortan, no se desbordan."""
        self.fake.resize(10, 40)
        self.modal.render(self.fake)
        filas = {y for y, _x, _t, _a in self.fake.addstr_calls}
        self.assertTrue(filas)
        self.assertLessEqual(max(filas), 10)

    def test_el_subtitulo_se_pinta(self) -> None:
        self.fake.resize(24, 80)
        self.modal.render(self.fake)
        self.assertIn("Marca las que quieras", self.fake.texto())

    def test_sin_nada_marcable_el_pie_no_anuncia_espacio(self) -> None:
        """Un pie que anuncia una tecla que no hace nada es lo que el pie no
        puede hacer (§10)."""
        for titulo, items in (
            ("Vacío", []),
            ("Todo bloqueado", [ChecklistItem("a", "A", selectable=False)]),
        ):
            with self.subTest(caso=titulo):
                modal = ChecklistModal(titulo, items, actions=checklist_actions(),
                                       empty="No hay nada que marcar.")
                fake = FakeStdscr(24, 80)
                modal.render(fake)
                self.assertNotIn("Marcar", fake.texto())
                self.assertNotIn("Cerrar", fake.texto())

    def test_con_algo_marcable_el_pie_sigue_diciendolo(self) -> None:
        modal = ChecklistModal("Con algo", [ChecklistItem("a", "A")],
                               actions=checklist_actions())
        fake = FakeStdscr(24, 80)
        modal.render(fake)
        self.assertIn("Marcar", fake.texto())
        self.assertIn("Cerrar", fake.texto())

    def test_el_texto_vacio_con_saltos_no_se_derrama_sobre_el_borde(self) -> None:
        modal = ChecklistModal("Vacío", [], empty="Primera frase.\nSegunda frase.")
        self.fake.resize(24, 80)
        modal.render(self.fake)
        self.assertIn("Primera frase.", self.fake.texto())
        self.assertIn("Segunda frase.", self.fake.texto())


# ---------------------------------------------------------------------------
# 2. Canales `C`: en qué colecciones está este canal
# ---------------------------------------------------------------------------


class TestElegirColeccionesDeUnCanal(Base):
    def _modal(self, canal_=None):
        canal_ = canal_ or self.la1
        return self.app._checklist_de_canal(canal_)

    def test_las_colecciones_manuales_son_marcables(self) -> None:
        self.manual("Fútbol")
        self.manual("_series")
        modal = self._modal()
        self.assertTrue(all(i.selectable for i in modal.items))
        self.assertEqual(len(modal.items), 2)

    def test_una_coleccion_que_ya_lo_tiene_aparece_marcada(self) -> None:
        cid = self.manual("Fútbol", [identidad(self.la1)])
        modal = self._modal()
        marcadas = {i.key for i in modal.items if i.checked}
        self.assertEqual(marcadas, {cid})

    def test_una_coleccion_que_no_lo_tiene_aparece_desmarcada(self) -> None:
        self.manual("Fútbol", [identidad(self.la2)])
        modal = self._modal()
        self.assertTrue(modal.items)
        self.assertFalse(any(i.checked for i in modal.items))

    def test_el_titulo_dice_de_quien_es_el_canal(self) -> None:
        self.manual("Fútbol")
        self.assertIn("La 1", self._modal().title)

    def test_una_coleccion_inteligente_no_es_marcable_y_explica_por_que(self) -> None:
        self.inteligente()
        modal = self._modal()
        (item,) = modal.items
        self.assertFalse(item.selectable)
        self.assertIn("regla", item.note.lower())

    def test_una_coleccion_del_sistema_no_es_marcable_y_explica_por_que(self) -> None:
        self.sistema("Favoritos")
        (item,) = self._modal().items
        self.assertFalse(item.selectable)
        self.assertTrue(item.note)

    def test_sin_colecciones_no_hay_que_elegir(self) -> None:
        modal = self._modal()
        self.assertEqual(modal.items, [])
        self.assertTrue(modal.empty)

    def test_no_se_necesita_catalogo_para_saber_si_dentro_estaba(self) -> None:
        """La clave es la identidad del canal, no la lista de canales."""
        self.manual("Fútbol", [identidad(self.la1)])
        self.assertTrue(self._modal().items[0].checked)


class TestGuardarEnColeccion(Base):
    def test_el_ciclo_completo_marca_guarda_y_se_ve_al_reabrir(self) -> None:
        """El contrato de `_run_checklist`, driving a mano.

        No hay terminal en los tests, así que el bucle se recorre a mano
        como lo recorre `_run_checklist`: la casilla sólo se mueve **después**
        de que el gestor aceptó.
        """
        self.manual("Fútbol")
        modal = self.app._checklist_de_canal(self.la1)
        self.assertFalse(modal.items[0].checked)
        _marca, idx, marcado = modal.handle_key(ord(" "))
        error = self.app._guardar_en_coleccion(modal.items[idx].key, self.la1,
                                                marcado)
        self.assertIsNone(error)
        modal.set_checked(idx, marcado)
        self.assertTrue(modal.items[0].checked)
        # Reabrir lee del gestor, no del modal anterior.
        self.assertTrue(self.app._checklist_de_canal(self.la1).items[0].checked)

    def test_si_el_gestor_rechaza_la_casilla_no_se_mueve(self) -> None:
        """Una casilla marcada sobre un cambio que no ocurrió es una mentira."""
        self.manual("Fútbol")
        modal = self.app._checklist_de_canal(self.la1)
        with mock.patch.object(
            self.manager, "añadir",
            side_effect=ColeccionesManagerError("disco lleno"),
        ):
            _marca, idx, marcado = modal.handle_key(ord(" "))
            error = self.app._guardar_en_coleccion(
                modal.items[idx].key, self.la1, marcado
            )
        self.assertTrue(error)
        modal.set_error(error)
        self.assertFalse(modal.items[idx].checked)
        self.assertIn("disco lleno", modal.text())

    def test_la_nota_y_el_avatar_dicen_lo_mismo(self) -> None:
        """Una sola explicación para el mismo «no», venga de donde venga."""
        from thetvview.ui.screens import _miembros_editables

        for tipo in ("smart", "system"):
            with self.subTest(tipo=tipo):
                col = (self.inteligente() if tipo == "smart"
                       else self.sistema())
                col = self.manager.obtener(col)
                (item,) = [i for i in self.app._checklist_de_canal(self.la1).items
                           if i.key == col.id]
                self.assertEqual(item.note, _miembros_editables(col))

    def test_marcar_mete_el_canal_de_verdad(self) -> None:
        cid = self.manual("Fútbol")
        error = self.app._guardar_en_coleccion(cid, self.la1, True)
        self.assertIsNone(error)
        self.assertIn(identidad(self.la1), self.manager.obtener(cid).members)

    def test_desmarcar_saca_el_canal_de_verdad(self) -> None:
        cid = self.manual("Fútbol", [identidad(self.la1)])
        error = self.app._guardar_en_coleccion(cid, self.la1, False)
        self.assertIsNone(error)
        self.assertNotIn(identidad(self.la1), self.manager.obtener(cid).members)

    def test_marcar_y_desmarcar_es_volver_al_mismo_punto(self) -> None:
        cid = self.manual("Fútbol")
        self.app._guardar_en_coleccion(cid, self.la1, True)
        self.app._guardar_en_coleccion(cid, self.la1, False)
        self.assertEqual(self.manager.obtener(cid).members, [])

    def test_no_se_mete_dos_veces_el_mismo_canal(self) -> None:
        cid = self.manual("Fútbol")
        self.app._guardar_en_coleccion(cid, self.la1, True)
        self.app._guardar_en_coleccion(cid, self.la1, True)
        self.assertEqual(self.manager.obtener(cid).members.count(
            identidad(self.la1)), 1)

    def test_una_coleccion_inexistente_devuelve_mensaje_y_no_revienta(self) -> None:
        error = self.app._guardar_en_coleccion("col-fantasma", self.la1, True)
        self.assertTrue(error)

    def test_una_coleccion_inteligente_no_acepta_canales(self) -> None:
        """El gestor dice «no es manual»; la UI lo enseña y no cambia nada."""
        cid = self.inteligente()
        error = self.app._guardar_en_coleccion(cid, self.la1, True)
        self.assertTrue(error)
        self.assertEqual(self.manager.obtener(cid).members, [])

    def test_el_error_del_gestor_no_muestra_la_url_del_canal(self) -> None:
        """El mensaje es para el usuario, no un volcado de la excepción."""
        con_url = canal("Rare", grupo="").__class__(
            name="Canal raro",
            url="http://usuario:clave@ejemplo.test/live/1.m3u8",
            group="X",
        )
        self.sistema("Favoritos")
        error = self.app._guardar_en_coleccion("no-existe", con_url, True)
        self.assertNotIn("clave", error)
        self.assertNotIn("ejemplo.test", error)


# ---------------------------------------------------------------------------
# 3. Colecciones `m`: qué canales se quedan
# ---------------------------------------------------------------------------


class TestMiembrosDeUnaColeccion(Base):
    def _modal(self, cid):
        return self.app._checklist_de_miembros(cid)

    def test_los_miembros_aparecen_marcados_con_su_nombre(self) -> None:
        self.catalogo()
        cid = self.manual("Fútbol", [identidad(self.la1), identidad(self.dep)])
        modal = self._modal(cid)
        etiquetas = {i.label for i in modal.items}
        self.assertEqual(etiquetas, {"La 1", "Dep 1"})
        self.assertTrue(all(i.checked for i in modal.items))

    def test_una_coleccion_vacia_no_inventa_filas(self) -> None:
        cid = self.manual("Vacía")
        self.assertEqual(self._modal(cid).items, [])

    def test_una_referencia_obsoleta_aparece_y_es_sacable(self) -> None:
        """Un canal que salió de tus listas debe poder quitarse igual.

        Si la lista de miembros fuera sólo los canales que **resuelven**, la
        referencia rota no tendría por dónde quitarse y sólo quedaría editar
        `collections.json` a mano.
        """
        self.catalogo()
        fantasma = "|url|http://example.test/Ya-no-existe"
        cid = self.manual("Con hueco",
                          [identidad(self.la1), fantasma])
        modal = self._modal(cid)
        (hueleco,) = [i for i in modal.items if i.key == fantasma]
        self.assertTrue(hueleco.selectable)
        self.assertIn("obsolet", hueleco.note.lower())

    def test_sin_catalogo_no_se_inventa_el_nombre_ni_el_hueco(self) -> None:
        """Sin listas en memoria no se sabe ni el nombre ni si caducó."""
        cid = self.manual("Fútbol", [identidad(self.la1)])
        (item,) = self._modal(cid).items
        self.assertNotIn("obsolet", item.note.lower())
        self.assertTrue(item.label)

    def test_un_miembro_repetido_aparece_una_sola_vez(self) -> None:
        self.catalogo()
        clave = identidad(self.la1)
        cid = self.manual("Repetida", [clave, clave])
        self.assertEqual(len(self._modal(cid).items), 1)

    def test_una_coleccion_inteligente_no_deja_editar_sus_miembros(self) -> None:
        self.catalogo()
        cid = self.inteligente()
        error = self.app._checklist_de_miembros_error(cid)
        self.assertTrue(error)


class TestQuitarDeColeccion(Base):
    def test_desmarcar_saca_el_canal(self) -> None:
        cid = self.manual("Fútbol", [identidad(self.la1), identidad(self.la2)])
        error = self.app._quitar_de_coleccion(cid, identidad(self.la1))
        self.assertIsNone(error)
        self.assertEqual(self.manager.obtener(cid).members, [identidad(self.la2)])

    def test_desmarcar_el_ultimo_no_borra_la_coleccion(self) -> None:
        """Quitar el último canal deja una colección vacía, no desaparece la fila."""
        cid = self.manual("Fútbol", [identidad(self.la1)])
        self.app._quitar_de_coleccion(cid, identidad(self.la1))
        col = self.manager.obtener(cid)
        self.assertIsNotNone(col)
        self.assertEqual(col.members, [])

    def test_una_referencia_obsoleta_tambien_se_saca(self) -> None:
        fantasma = "|url|http://example.test/Ya-no-existe"
        cid = self.manual("Con hueco", [fantasma])
        self.assertIsNone(self.app._quitar_de_coleccion(cid, fantasma))
        self.assertEqual(self.manager.obtener(cid).members, [])

    def test_sacar_un_miembro_que_no_esta_no_revienta(self) -> None:
        cid = self.manual("Fútbol")
        self.assertIsNone(
            self.app._quitar_de_coleccion(cid, "|url|http://example.test/X")
        )

    def test_una_coleccion_inexistente_devuelve_mensaje(self) -> None:
        self.assertTrue(
            self.app._quitar_de_coleccion("col-fantasma", "cualquiera")
        )


# ---------------------------------------------------------------------------
# 4. Las teclas: declaradas, anunciada y atendidas
# ---------------------------------------------------------------------------


class TestTeclaCEnCanales(Base):
    def pantalla(self) -> ChannelsScreen:
        return ChannelsScreen(self.app, playlist(self.la1, self.la2))

    def test_c_mayuscula_abre_las_colecciones_del_canal(self) -> None:
        pantalla = self.pantalla()
        res = pantalla.handle_key(ord("C"))
        self.assertEqual(res["action"], "channel_collections")
        self.assertIs(res["channel"], self.la1)

    def test_c_mayuscula_esta_declarada_y_anunciada(self) -> None:
        pantalla = self.pantalla()
        etiqueta = next(a for a in pantalla.actions() if a.key == "C")
        self.assertEqual(etiqueta.label, "Colecciones")
        self.assertIn("C", pantalla.shortcuts())

    def test_sin_canal_seleccionado_c_no_hace_nada(self) -> None:
        pantalla = self.pantalla()
        pantalla.channels = []
        pantalla.visible_idx = []
        self.assertIsNone(pantalla.handle_key(ord("C")))

    def test_sin_canal_el_pie_no_anuncia_c(self) -> None:
        pantalla = self.pantalla()
        pantalla.channels = []
        pantalla.visible_idx = []
        etiqueta = next(a for a in pantalla.actions() if a.key == "C")
        self.assertFalse(etiqueta.enabled)

    def test_c_no_entra_durante_la_busqueda(self) -> None:
        """En búsqueda el juego de teclas es otro (§34)."""
        pantalla = self.pantalla()
        pantalla.searching = True
        claves = {a.key for a in pantalla.actions()}
        self.assertEqual(claves, {"Enter", "Ctrl-U", "Esc"})

    def test_c_y_C_conviven_sin_confundirse(self) -> None:
        """`c` abre la pantalla y `C` elige colecciones: dos teclas, dos cosas."""
        self.manual("Fútbol")
        pantalla = self.pantalla()
        self.assertEqual(pantalla.handle_key(ord("c"))["action"],
                         "show_collections")
        self.assertEqual(pantalla.handle_key(ord("C"))["action"],
                         "channel_collections")


class TestTeclaMEnColecciones(Base):
    def pantalla(self, catalogo=None) -> CollectionsScreen:
        return CollectionsScreen(self.app, catalogo or [self.la1])

    def test_m_pide_gestionar_los_miembros(self) -> None:
        cid = self.manual("Fútbol")
        pantalla = self.pantalla()
        pantalla.list.selected = 0
        self.assertEqual(
            pantalla.handle_key(ord("m")),
            {"action": "collection_members", "cid": cid},
        )

    def test_m_esta_declarada_y_anunciada(self) -> None:
        self.manual("Fútbol")
        pantalla = self.pantalla()
        etiqueta = next(a for a in pantalla.actions() if a.key == "m")
        self.assertEqual(etiqueta.label, "Miembros")
        self.assertIn("m", pantalla.shortcuts())

    def test_una_inteligente_no_anuncia_miembros(self) -> None:
        """Sus canales los decide la regla: no hay nada que quitar a mano."""
        self.inteligente()
        pantalla = self.pantalla()
        self.assertFalse(
            next(a for a in pantalla.actions() if a.key == "m").enabled
        )

    def test_una_del_sistema_no_anuncia_miembros(self) -> None:
        self.sistema("Favoritos")
        pantalla = self.pantalla()
        self.assertFalse(
            next(a for a in pantalla.actions() if a.key == "m").enabled
        )

    def test_pulsar_m_en_una_inteligente_explica_por_que(self) -> None:
        self.inteligente("Deportes")
        pantalla = self.pantalla()
        avisos: list[str] = []
        with mock.patch.object(self.app, "notify_warning", avisos.append):
            res = pantalla.handle_key(ord("m"))
        self.assertIsNone(res)
        self.assertTrue(any("regla" in a.lower() for a in avisos), avisos)

    def test_pulsar_m_sin_coleccion_no_inventa_trabajo(self) -> None:
        pantalla = self.pantalla()
        avisos: list[str] = []
        with mock.patch.object(self.app, "notify_warning", avisos.append):
            self.assertIsNone(pantalla.handle_key(ord("m")))
        self.assertTrue(avisos)

    def test_sin_colecciones_el_pie_no_declara_m(self) -> None:
        """Sin filas, `shortcuts()` no anuncia `m`: la barra no miente (§10)."""
        from tests.test_actions_por_pantalla import shortcut_keys

        pantalla = self.pantalla()
        claves = {a.key for a in pantalla.actions()}
        self.assertNotIn("m", claves)
        # Se usa el parser real de la app: buscar «m» con `in` daría un falso
        # positivo con «Tema».
        self.assertNotIn("m", shortcut_keys(pantalla.shortcuts()))


# ---------------------------------------------------------------------------
# 5. El gestor es de verdad el que manda
# ---------------------------------------------------------------------------


class TestElBackendManda(Base):
    def test_el_gestor_es_el_que_rechaza_lo_que_no_puede(self) -> None:
        """La UI no reimplementa la regla: la pide al gestor y translate."""
        from thetvview.collections.manager import ColeccionesManagerError

        cid = self.sistema("Favoritos")
        with self.assertRaises(ColeccionesManagerError):
            self.manager.añadir(cid, self.la1)

    def test_el_app_no_escribe_members_a_sus_manos(self) -> None:
        """Mandar es `toggle`/`quitar` del gestor, no editar el dataclass.

        Si la UI construyera la lista herself, se saltaría la escritura
        atómica y la validación del esquema.
        """
        cid = self.manual("Fútbol")
        with mock.patch.object(
            self.manager, "quitar", wraps=self.manager.quitar
        ) as spy:
            self.app._guardar_en_coleccion(cid, self.la1, False)
        # `toggle` es quien llama a `quitar` cuando el canal ya estaba dentro.
        self.manager.toggle(cid, self.la1)
        self.assertTrue(spy.called)


class TestLaAyudaLoExplica(Base):
    """`?` tiene que explicar las teclas nuevas: si no, no hay forma de
    descubrirlas."""

    def _entrada(self, pantalla: str, tecla: str) -> str:
        from thetvview.ui.app import _HELP_HERE

        textos = [t for _clase, t in _HELP_HERE[pantalla]]
        return "\n".join(t for t in textos if t.startswith(tecla))

    def test_canales_explica_c_y_C(self) -> None:
        self.assertTrue(self._entrada("ChannelsScreen", "c ").strip())
        self.assertTrue(self._entrada("ChannelsScreen", "C ").strip())

    def test_canales_distingue_las_dos_teclas_de_colecciones(self) -> None:
        """`c` y `C` se parecen mucho; la ayuda dice qué hace cada una."""
        minuscula = self._entrada("ChannelsScreen", "c ")
        mayuscula = self._entrada("ChannelsScreen", "C ")
        self.assertNotEqual(minuscula, mayuscula)

    def test_colecciones_explica_m(self) -> None:
        self.assertTrue(self._entrada("CollectionsScreen", "m ").strip())


class TestSinInterfaz(Base):
    """Sin terminal real los caminos modales no cran — y no hacen nada."""

    def test_sin_colecciones_el_aviso_usa_el_texto_del_modal(self) -> None:
        """Una sola versión del texto: el modal y el aviso no se contradicen."""
        modal = self.app._checklist_de_canal(self.la1)
        avisos: list[str] = []
        self.app.notify_warning = avisos.append
        self.app.choose_channel_collections(self.la1)
        self.assertEqual(avisos, [modal.empty])
        self.assertTrue(modal.empty)

    def test_una_fila_no_marcable_no_enseña_una_casilla(self) -> None:
        """Una casilla que no responde sería una mentira dibujada."""
        self.sistema("Favoritos")
        (linea,) = [
            ln for ln in self.app._checklist_de_canal(self.la1).text().splitlines()
            if "Favoritos" in ln
        ]
        self.assertNotIn("✓", linea)
        self.assertNotIn("[ ]", linea)

    def test_el_modal_de_canal_no_hace_nada_sin_ui(self) -> None:
        self.manual("Fútbol")
        app = App(None)
        app.choose_channel_collections(self.la1)

    def test_el_modal_de_miembros_no_hace_nada_sin_ui(self) -> None:
        cid = self.manual("Fútbol", [identidad(self.la1)])
        app = App(None)
        app.manage_collection_members(cid)

    def test_el_reintento_de_error_no_abre_un_segundo_modal(self) -> None:
        """`_run_modal` no se anida: el fallo se queda en la barra."""
        app = App(None)
        self.assertIsNone(app._run_checklist(
            ChecklistModal("X", [ChecklistItem("a", "A")]),
            lambda item, marcado: None,
        ))