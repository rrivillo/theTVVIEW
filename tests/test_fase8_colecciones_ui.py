"""Fase 8 · U-03 — Colecciones: pantalla y CRUD por modal.

El backend de la Fase 4 (`thetvview/collections/`) ya validaba, guardaba y
evaluaba. Esta prueba es sobre lo que **no** existe todavía: la pantalla.

Tres reglas del plan gobiernan los tests de aquí:

1. el **tipo** de cada colección (manual / inteligente / del sistema) se dice en
   la **fila**, no en el pie — el pie describe la acción, no la fila;
2. crear, renombrar y eliminar pasan por **modal** y cada uno dice **por qué**;
3. una referencia obsoleta **se avisa** y **no tumba** la colección (C-07).

Y una cuarta, que es la que más se olvida: una colección del sistema es una
**vista derivada** (Favoritos, Recientes). No tiene un número de canales
propio, así que la pantalla **no puede inventar** un «0 canales»: eso sería
mentirle al usuario sobre lo que está viendo.
"""

from __future__ import annotations

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.channel_identity import channel_identity
from thetvview.collections.manager import ColeccionesManager
from thetvview.models import Channel
from thetvview.ui import colors
from thetvview.ui.actions import V
from thetvview.ui.app import App
from thetvview.ui.screens import CollectionsScreen


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

    def row_text(self, y):
        return "".join(t for (cy, _x, t, _a) in self.addstr_calls if cy == y)


def canal(nombre="Canal X", grupo="Noticias") -> Channel:
    return Channel(
        name=nombre,
        url="http://example.test/" + nombre,
        group=grupo,
    )


def identidad(canal: Channel) -> str:
    """La clave con la que el gestor guarda un canal en ``members``.

    Es ``str(channel_identity(c))`` y no ``channel_identity(c)``: el gestor
    serializa la identidad a texto, y ``validar_coleccion`` rechaza cualquier
    otra cosa como miembro.
    """
    return str(channel_identity(canal))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeStdscr(24, 80)
        tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(
                __import__("thetvview.config", fromlist=["x"]),
                "COLLECTIONS_JSON",
                tmp / "collections.json",
            ),
            mock.patch.object(
                __import__("thetvview.config", fromlist=["x"]),
                "PLAYLISTS_JSON",
                tmp / "p.json",
            ),
            mock.patch.object(
                __import__("thetvview.config", fromlist=["x"]),
                "FAVORITES_JSON",
                tmp / "f.json",
            ),
            mock.patch.object(
                __import__("thetvview.config", fromlist=["x"]),
                "PREFS_JSON",
                tmp / "prefs.json",
            ),
            mock.patch.object(
                __import__("thetvview.config", fromlist=["x"]),
                "RECENTS_JSON",
                tmp / "r.json",
            ),
            mock.patch.object(
                __import__("thetvview.config", fromlist=["x"]),
                "THEME_JSON",
                tmp / "theme.json",
            ),
            mock.patch("thetvview.config.ensure_dirs", lambda: None),
            mock.patch.object(colors, "pair", return_value=0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = App(self.fake)
        self.manager = self.app.collections

    # -- ayudas ------------------------------------------------------------
    def catalogo(self, *canales: Channel) -> list[Channel]:
        return list(canales) or [canal("La 1"), canal("La 2", "Deportes")]

    def pantalla(self, catalogo=None) -> CollectionsScreen:
        """Pantalla sobre un catálogo fijo (nada de `playlist_cache`)."""
        return CollectionsScreen(self.app, catalogo or self.catalogo())

    def creada(self, nombre="Deportes", tipo="manual", members=()) -> str:
        col = self.manager.crear(nombre, tipo=tipo, members=list(members))
        return col.id


# ---------------------------------------------------------------------------
# 1. La pantalla existe y se entra en ella
# ---------------------------------------------------------------------------


class TestLaPantallaExiste(Base):
    def test_etiquetas_nuevas_en_el_vocabulario_cerrado(self) -> None:
        """U-03 añadió tres; sin ellas `validate()` revienta el primer render."""
        from thetvview.ui.actions import LABELS

        for etiqueta in (V.DETALLE, V.NUEVA, V.RENOMBRAR):
            with self.subTest(etiqueta=etiqueta):
                self.assertIn(etiqueta, LABELS)

    def test_app_tiene_gestor_de_colecciones(self) -> None:
        """Sin gestor en `App`, la pantalla sería una lista de mentira."""
        self.assertIsInstance(self.app.collections, ColeccionesManager)

    def test_el_gestor_usa_la_ruta_de_config(self) -> None:
        import thetvview.config as cfg

        self.assertEqual(self.app.collections.path, cfg.COLLECTIONS_JSON)

    def test_la_accion_de_entrar_lleva_a_la_pantalla(self) -> None:
        app = App(None)
        app.push = mock.Mock()
        app.handle_action({"action": "show_collections"})
        (screen,) = app.push.call_args[0]
        self.assertIsInstance(screen, CollectionsScreen)

    def test_la_ayuda_la_conoce(self) -> None:
        """`?` en una pantalla sin `_HELP_WHERE` explica «la app», no la pantalla."""
        from thetvview.ui.app import _HELP_WHERE, _HELP_HERE

        self.assertIn("CollectionsScreen", _HELP_WHERE)
        self.assertIn("CollectionsScreen", _HELP_HERE)
        self.assertTrue(_HELP_HERE["CollectionsScreen"])


# ---------------------------------------------------------------------------
# 2. El tipo se dice en la fila, no en el pie
# ---------------------------------------------------------------------------


class TestElTipoVaEnLaFila(Base):
    def test_los_tres_tipos_se_dicen_en_la_fila(self) -> None:
        self.creada("Aficionados", "manual")
        self.manager.crear(
            "Documentales",
            tipo="smart",
            rules={"all": [{"field": "group", "operator": "contains", "value": "Doc"}]},
        )
        self.manager.crear("Favoritos", tipo="system")

        pantalla = self.pantalla()
        filas = " ".join(pantalla.list.items)
        for etiqueta in ("manual", "inteligente", "del sistema"):
            with self.subTest(tipo=etiqueta):
                self.assertIn(etiqueta, filas)

    def test_el_tipo_no_solo_vive_en_el_pie(self) -> None:
        """El pie describe la acción; la fila describe la fila."""
        self.manager.crear("Favoritos", tipo="system")
        pantalla = self.pantalla()
        self.assertIn("del sistema", pantalla.list.items[0])
        for accion in pantalla.actions():
            with self.subTest(accion=accion.key):
                self.assertNotIn("del sistema", accion.label)

    def test_la_cabecera_lleva_el_contador(self) -> None:
        self.creada("Una")
        self.creada("Dos")
        self.assertIn("2", self.pantalla().header_context())

    def test_una_coleccion_del_sistema_no_inventa_un_cero(self) -> None:
        """«0 canales» sobre una vista derivada es una mentira, no un dato.

        `evaluar_coleccion` devuelve vacío para `system` sin reglas, así que un
        contador ingenuo enseñaría «0» en Favoritos. La pantalla dice que es
        derivada y no inventa la cifra.
        """
        self.manager.crear("Favoritos", tipo="system")
        pantalla = self.pantalla()
        self.assertNotIn("0 canales", " ".join(pantalla.list.subitems))
        self.assertIn("derivada", " ".join(pantalla.list.subitems))

    def test_el_contador_de_una_manual_es_el_de_sus_miembros(self) -> None:
        canales = [canal("La 1"), canal("La 2", "Deportes"), canal("La 3")]
        self.manager.crear("Otra", tipo="manual",
                           members=[identidad(c) for c in canales])
        pantalla = self.pantalla(canales)
        self.assertIn("3 canales", " ".join(pantalla.list.subitems))

    def test_un_canal_se_cuenta_una_vez_por_mas_que_este_repetido(self) -> None:
        """La identidad es la clave: el mismo canal dos veces sigue siendo uno."""
        c = canal("La 1")
        self.manager.crear("Con duplicados", tipo="manual",
                           members=[identidad(c), identidad(c)])
        pantalla = self.pantalla([c, c])
        self.assertIn("1 canal", " ".join(pantalla.list.subitems))


# ---------------------------------------------------------------------------
# 3. Invariantes de acciones (R-09)
# ---------------------------------------------------------------------------


class TestInvariantesDeAcciones(Base):
    #: Las dos teclas que `App.run` atiende antes de delegar en la pantalla
    #: (mismo criterio que `tests/test_actions_por_pantalla.py`).
    GLOBAL = {"Esc", "?"}
    KEYCODES = {"Enter": curses.KEY_ENTER}

    def test_toda_tecla_anunciada_esta_en_shortcuts_y_se_atiende(self) -> None:
        for construir in (
            lambda: self.pantalla(),
            lambda: (self.creada("Vacia"), self.pantalla())[1],
        ):
            pantalla = construir()
            with self.subTest(pantalla=pantalla.list.items):
                atajo = pantalla.shortcuts()
                for accion in pantalla.actions():
                    if accion.key in self.GLOBAL:
                        continue
                    with self.subTest(tecla=accion.key):
                        self.assertIn(accion.key, atajo)
                        codigo = (
                            self.KEYCODES[accion.key]
                            if accion.key in self.KEYCODES
                            else ord(accion.key)
                        )
                        res = pantalla.handle_key(codigo)
                        self.assertIsNotNone(
                            res, f"{pantalla.title}: `{accion.key}` no hace nada"
                        )

    def test_ayuda_esencial_y_de_ultima_prioridad(self) -> None:
        pantalla = self.pantalla()
        ayuda = [a for a in pantalla.actions() if a.key == "?"]
        self.assertEqual(len(ayuda), 1)
        self.assertTrue(ayuda[0].essential)
        self.assertEqual(ayuda[0].label, V.AYUDA)

    def test_ayuda_sobrevive_a_40_columnas(self) -> None:
        from thetvview.ui.actions import fit_actions

        self.creada("Una")
        visibles = [a.key for a in fit_actions(self.pantalla().actions(), 40)]
        self.assertIn("?", visibles)

    def test_el_pie_y_la_pista_dicen_lo_mismo(self) -> None:
        """Si el pie dice «Editar» y la pista «Renombrar», uno miente (§10).

        Se comparan las palabras, no las teclas: la suite compartida ya vigila
        que toda tecla de la barra esté en `shortcuts()`, pero no vigila que las
        dos frases describan lo mismo.
        """
        self.creada("Una")
        pantalla = self.pantalla()
        pista = pantalla.shortcuts()
        for accion in pantalla.actions():
            if accion.key in self.GLOBAL:
                continue
            with self.subTest(tecla=accion.key):
                self.assertIn(accion.label, pista)


# ---------------------------------------------------------------------------
# 4. El CRUD pasa por modal y dice por qué
# ---------------------------------------------------------------------------


class TestCrudPorModal(Base):
    def _tecla(self, pantalla, letra):
        return pantalla.handle_key(ord(letra))

    def test_a_pide_crear(self) -> None:
        self.assertEqual(
            self._tecla(self.pantalla(), "a"), {"action": "add_collection"}
        )

    def test_e_pide_renombrar_la_seleccionada(self) -> None:
        cid = self.creada("Vieja")
        pantalla = self.pantalla()
        pantalla.list.selected = 0
        res = self._tecla(pantalla, "e")
        self.assertEqual(res, {"action": "rename_collection", "cid": cid})

    def test_d_pide_borrar_la_seleccionada(self) -> None:
        cid = self.creada("Sacrificada")
        pantalla = self.pantalla()
        pantalla.list.selected = 0
        res = self._tecla(pantalla, "d")
        self.assertEqual(res, {"action": "delete_collection", "cid": cid})

    def test_sin_colecciones_las_teclas_no_inventan_trabajo(self) -> None:
        """Avisar es obligatorio; fingir que se borró algo, no."""
        pantalla = self.pantalla()
        for letra in ("e", "d"):
            with self.subTest(tecla=letra):
                self.assertIsNone(self._tecla(pantalla, letra))
        self.assertTrue(self.manager.listar() == [])

    def test_una_coleccion_del_sistema_no_se_edita_ni_se_borra(self) -> None:
        """Son vistas derivadas: editarlas sería mentir sobre lo que son."""
        self.manager.crear("Favoritos", tipo="system")
        pantalla = self.pantalla()
        pantalla.list.selected = 0
        for letra in ("e", "d"):
            with self.subTest(tecla=letra):
                self.assertIsNone(self._tecla(pantalla, letra))

    def test_el_pie_no_anuncia_editar_una_vista_derivada(self) -> None:
        self.manager.crear("Favoritos", tipo="system")
        self.manager.crear("Mía", tipo="manual")
        pantalla = self.pantalla()
        pantalla.list.selected = 0  # Favoritos
        etiquetas = {a.key: a for a in pantalla.actions()}
        self.assertFalse(etiquetas["e"].enabled)
        self.assertFalse(etiquetas["d"].enabled)
        pantalla.list.selected = 1  # Mía
        etiquetas = {a.key: a for a in pantalla.actions()}
        self.assertTrue(etiquetas["e"].enabled)


class TestAltaPorModal(Base):
    def test_el_modal_de_alta_explica_que_es_una_coleccion(self) -> None:
        self.app._prompt_form = mock.Mock(return_value=None)
        self.app.create_collection()
        titulo, campos = self.app._prompt_form.call_args[0][:2]
        self.assertTrue(titulo)
        self.assertTrue(
            any("Nombre" in etiqueta for _clave, etiqueta in campos),
            campos,
        )

    def test_cancelar_no_crea_nada(self) -> None:
        self.app._prompt_form = mock.Mock(return_value=None)
        self.app.create_collection()
        self.assertEqual(self.manager.listar(), [])

    def test_el_nombre_vacio_se_rechaza_con_mensaje(self) -> None:
        """El backend dice «nombre vacío»; la UI lo enseña, no lo traga."""
        self.app._prompt_form = mock.Mock(return_value={"name": "   "})
        avisos: list[str] = []
        self.app.notify_warning = avisos.append
        self.app.create_collection()
        self.assertEqual(self.manager.listar(), [])
        self.assertTrue(any("nombre" in a.lower() for a in avisos), avisos)

    def test_crear_deja_la_coleccion_en_pantalla(self) -> None:
        self.app._prompt_form = mock.Mock(return_value={"name": "Fútbol"})
        pantalla = CollectionsScreen(self.app, [])
        self.app.push(pantalla)
        self.app.create_collection()
        self.assertEqual([c.name for c in self.manager.listar()], ["Fútbol"])
        self.assertIn("Fútbol", " ".join(pantalla.list.items))


class TestRenombrarYBorrarPorModal(Base):
    def test_renombrar_usa_el_nuevo_nombre(self) -> None:
        cid = self.creada("Vieja")
        self.app._prompt_form = mock.Mock(return_value={"name": "Nueva"})
        self.app.rename_collection(cid)
        self.assertEqual(self.manager.obtener(cid).name, "Nueva")

    def test_renombrar_cancelado_no_toca_nada(self) -> None:
        cid = self.creada("Intacta")
        self.app._prompt_form = mock.Mock(return_value=None)
        self.app.rename_collection(cid)
        self.assertEqual(self.manager.obtener(cid).name, "Intacta")

    def test_renombrar_deja_el_mismo_nombre_no_es_un_cambio(self) -> None:
        cid = self.creada("Igual")
        self.app._prompt_form = mock.Mock(return_value={"name": "Igual"})
        avisos: list[str] = []
        self.app.notify_warning = avisos.append
        self.app.rename_collection(cid)
        self.assertEqual(self.manager.obtener(cid).name, "Igual")
        self.assertTrue(avisos)

    def test_borrar_pide_confirmacion_y_dice_que_se_borra(self) -> None:
        cid = self.creada("Con tres", "manual", members=["a", "b", "c"])
        confirmaciones: list[tuple[str, str]] = []

        def _confirma(titulo, mensaje):
            confirmaciones.append((titulo, mensaje))
            return True

        self.app._confirm = _confirma
        self.app.delete_collection(cid)
        self.assertEqual(self.manager.listar(), [])
        titulo, mensaje = confirmaciones[0]
        self.assertIn("Con tres", f"{titulo} {mensaje}")

    def test_borrar_cancelado_no_borra(self) -> None:
        cid = self.creada("Se queda")
        self.app._confirm = mock.Mock(return_value=False)
        self.app.delete_collection(cid)
        self.assertEqual([c.name for c in self.manager.listar()], ["Se queda"])

    def test_borrar_una_coleccion_inexistente_no_revienta(self) -> None:
        self.app._confirm = mock.Mock(return_value=True)
        avisos: list[str] = []
        self.app.notify_warning = avisos.append
        self.app.delete_collection("col-no-existe")
        self.assertTrue(avisos)


# ---------------------------------------------------------------------------
# 5. Referencias obsoletas: se avisan, no tumban (C-07)
# ---------------------------------------------------------------------------


class TestReferenciasObsoletas(Base):
    #: Dos canales que sí existen y una referencia que no existe en ninguna lista.
    VIVOS = (canal("La 1"), canal("La 2", "Deportes"))
    FANTASMA = "|url|http://example.test/Canal-que-ya-no-esta"

    def _obsoleta(self) -> str:
        col = self.manager.crear(
            "Con huecos",
            tipo="manual",
            members=[identidad(c) for c in self.VIVOS] + [self.FANTASMA],
        )
        return col.id

    def test_la_fila_avisa_del_hueco(self) -> None:
        self._obsoleta()
        pantalla = self.pantalla(list(self.VIVOS))
        self.assertIn("obsolet", " ".join(pantalla.list.subitems).lower())

    def test_el_aviso_dice_cuantas_y_no_solo_que_hay(self) -> None:
        self._obsoleta()
        pantalla = self.pantalla(list(self.VIVOS))
        self.assertIn("1 referencia obsoleta", " ".join(pantalla.list.subitems))

    def test_los_canales_que_si_existen_se_cuentan_igual(self) -> None:
        """Un hueco no puede contar como cero: eso sería tirar el trabajo."""
        self._obsoleta()
        pantalla = self.pantalla(list(self.VIVOS))
        self.assertIn("2 canales", " ".join(pantalla.list.subitems))

    def test_la_coleccion_sobrevive_a_las_referencias_obsoletas(self) -> None:
        self._obsoleta()
        pantalla = self.pantalla(list(self.VIVOS))
        # Ni un raise al construir, ni al contar, ni al pintar.
        pantalla.reload()
        pantalla.render(self.fake)
        self.assertEqual([c.name for c in self.manager.listar()], ["Con huecos"])

    def test_renombrar_una_coleccion_con_huecos_sigue_funcionando(self) -> None:
        cid = self._obsoleta()
        self.app._prompt_form = mock.Mock(return_value={"name": "Renombrada"})
        self.app.rename_collection(cid)
        self.assertEqual(self.manager.obtener(cid).name, "Renombrada")

    def test_el_aviso_no_es_falso_si_todo_resuelve(self) -> None:
        canales = list(self.VIVOS)
        self.manager.crear("Sana", tipo="manual",
                           members=[identidad(c) for c in canales])
        pantalla = self.pantalla(canales)
        self.assertNotIn("obsolet", " ".join(pantalla.list.subitems).lower())

    def test_sin_catalogo_todo_manual_parece_obsoleto(self) -> None:
        """Con la app recién abierta no hay nada en memoria: se dice, no se finge.

        Un aviso de «2 referencias obsoletas» cuando en realidad lo que aún no
        ha ocurrido es que no se ha cargado ninguna lista sería el mismo error
        al revés: informar de algo que el usuario puede resolver abriendo una
        lista. Por eso, sin catálogo, la pantalla no cuenta nada.
        """
        self.manager.crear("Sana", tipo="manual",
                           members=[identidad(c) for c in self.VIVOS])
        pantalla = self.pantalla([])
        self.assertNotIn("obsolet", " ".join(pantalla.list.subitems).lower())


# ---------------------------------------------------------------------------
# 6. Estados vacíos y terminales estrechas
# ---------------------------------------------------------------------------


class TestEstadosVaciosYRender(Base):
    def test_sin_colecciones_dice_como_empieza(self) -> None:
        pantalla = self.pantalla()
        pantalla.render(self.fake)
        texto = " ".join(t for _y, _x, t, _a in self.fake.addstr_calls)
        self.assertIn("coleccion", texto.lower())

    def test_render_en_terminal_diminuta_no_revienta(self) -> None:
        self.creada("Una", "manual", members=["a"])
        self.manager.crear("Derivada", tipo="system")
        for alto, ancho in ((10, 40), (24, 80), (30, 120)):
            with self.subTest(terminal=f"{alto}x{ancho}"):
                self.fake.resize(alto, ancho)
                pantalla = self.pantalla()
                pantalla.render(self.fake)
                self.fake.erase()
                pantalla = self.pantalla()
                pantalla.render(self.fake)

    def test_render_de_lista_vacia_no_revienta(self) -> None:
        for alto, ancho in ((10, 40), (24, 80), (30, 120)):
            with self.subTest(terminal=f"{alto}x{ancho}"):
                self.fake.resize(alto, ancho)
                self.pantalla().render(self.fake)
                self.fake.erase()

    def test_scroll_entre_colecciones(self) -> None:
        for i in range(30):
            self.creada(f"C{i}")
        pantalla = self.pantalla()
        self.fake.resize(10, 40)
        for _ in range(40):
            pantalla.handle_key(curses.KEY_DOWN)
        pantalla.render(self.fake)
        self.assertLess(pantalla.list.selected, 30)


# ---------------------------------------------------------------------------
# 7. Ver el detalle de una colección (Enter) — y no prometer una lista
# ---------------------------------------------------------------------------


class TestDetalle(Base):
    def test_enter_abre_el_detalle(self) -> None:
        cid = self.creada("Deportes", "manual", members=[])
        pantalla = self.pantalla()
        res = pantalla.handle_key(curses.KEY_ENTER)
        self.assertEqual(res, {"action": "show_collection", "cid": cid})

    def test_enter_en_una_coleccion_del_sistema_igual_abre_el_detalle(self) -> None:
        self.manager.crear("Favoritos", tipo="system")
        pantalla = self.pantalla()
        res = pantalla.handle_key(curses.KEY_ENTER)
        self.assertEqual(res["action"], "show_collection")

    def test_el_detalle_explica_tipo_y_contenido(self) -> None:
        cid = self.creada("Deportes", "manual")
        vistos: list[tuple[str, str]] = []

        def _modal(titulo, mensaje, *a, **k):
            vistos.append((titulo, mensaje))

        self.app._mostrar_modal = _modal
        self.app.show_collection(cid)
        titulo, mensaje = vistos[0]
        self.assertIn("Deportes", f"{titulo} {mensaje}")
        self.assertIn("manual", mensaje.lower())

    def test_el_detalle_de_una_inexistente_no_revienta(self) -> None:
        avisos: list[str] = []
        self.app.notify_warning = avisos.append
        self.app.show_collection("col-fantasma")
        self.assertTrue(avisos)