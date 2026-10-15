"""Fase 8 · U-04b — El contexto que `valor_campo` lee y nadie construía.

U-04 dejó escrito el hallazgo: `valor_campo` saca cinco campos de un
`contexto` que le pasa quien evalúa, y la app **nunca se lo pasaba**. Para
`favorites`, `epg_status` y `last_played` eso significaba «no favorito»,
«sin guía» y «nunca visto» **para todos los canales, siempre**. Una colección
«mis favoritos» salía con cero canales y ningún motivo.

Este archivo fija lo que sí se puede responder con lo que la app ya tiene, y
—tan importante— lo que **no**: los dos campos sin origen de datos no se
ofrecen como si fueran utilizables.

El reparto, y por qué:

- ``favorites`` — sí: `FavoritesManager` ya está en memoria.
- ``epg_status`` — sí: `App.epg` ya está cargado cuando el usuario lo cargó.
- ``last_played`` — sí: `RecentsManager` es la lista de lo que se ha visto.
- ``recent_failure_count`` — **no**: nada escribe el historial de fallos de
  reproducción desde la UI. `HistorialDiagnostico` existe y se persiste, pero
  sólo lo rellenan los tests y `security/check.py`.
- ``playback_health`` — **no**: el estado de salud se sondea por pantalla y se
  apaga al salir; no queda nada consultable.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.channel_identity import channel_identity
from thetvview.collections.rules import valor_campo, evaluar_coleccion
from thetvview.collections.schema import Coleccion
from thetvview.favorites import FavoritesManager
from thetvview.models import Channel, Playlist
from thetvview.recents import RecentItem, RecentsManager
from thetvview.ui import colors, rule_text
from thetvview.ui.app import App
from thetvview.ui.screens import CollectionsScreen


def canal(nombre="Canal", grupo="Noticias", url=None, tvg_id=""):
    return Channel(name=nombre, url=url or f"http://ejemplo.test/{nombre}",
                   group=grupo, tvg_id=tvg_id)


def hoja(campo, operador, valor):
    return {"field": campo, "operator": operador, "value": valor}


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


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeStdscr(24, 80)
        tmp = Path(tempfile.mkdtemp())
        import thetvview.config as cfg

        self.tmp = tmp
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
        self.la1 = canal("La 1", "General", tvg_id="espn.la1")
        self.la2 = canal("La 2", "General", tvg_id="espn.la2")
        self.dep = canal("Dep 1", "Deportes", tvg_id="espn.dep1")
        self.catalogo = [self.la1, self.la2, self.dep]

    def cargar(self, *canales):
        canales = list(canales) or self.catalogo
        self.app.playlist_cache["t"] = Playlist(name="T", channels=canales)
        return canales

    def favoritos(self, *canales):
        for c in canales:
            self.app.favorites.toggle(c)
        self.app.favorites.invalidate()


# ---------------------------------------------------------------------------
# 1. El contexto que se construye
# ---------------------------------------------------------------------------


class TestContextoDeReglas(Base):
    def test_trae_las_piezas_del_contexto(self) -> None:
        ctx = self.app.contexto_de_reglas()
        self.assertIsInstance(ctx, dict)
        for clave in ("favorites", "last_played", "epg", "indice_epg"):
            with self.subTest(clave=clave):
                self.assertIn(clave, ctx)

    def test_sin_favoritos_el_conjunto_viene_vacío_pero_existe(self) -> None:
        ctx = self.app.contexto_de_reglas()
        self.assertIsInstance(ctx["favorites"], set)
        self.assertEqual(ctx["favorites"], set())

    def test_un_favorito_aparece_por_identidad_y_por_url(self) -> None:
        """`valor_campo` busca por identidad y, si falla, por URL.

        Sólo con una de las dos, un canal se contaría como favorito o no según
        de dónde venga: el mismo canal con la URL girada.
        """
        self.favoritos(self.dep)
        favs = self.app.contexto_de_reglas()["favorites"]
        self.assertIn(str(channel_identity(self.dep)), favs)
        self.assertIn(self.dep.url, favs)

    def test_valor_campo_dice_que_son_favoritos(self) -> None:
        self.favoritos(self.dep)
        ctx = self.app.contexto_de_reglas()
        self.assertEqual(valor_campo(self.dep, "favorites", ctx), "true")
        self.assertEqual(valor_campo(self.la1, "favorites", ctx), "false")

    def test_el_ultimo_visto_sale_de_los_recientes(self) -> None:
        self.app.recents.push(self.dep, "mpv")
        ctx = self.app.contexto_de_reglas()
        valor = valor_campo(self.dep, "last_played_at", ctx)
        self.assertTrue(valor)
        # Debe poder compararse: `within_last` no puede convertir un `None`.
        from thetvview.collections.rules import evaluar_condicion

        self.assertTrue(evaluar_condicion(
            self.dep, hoja("last_played_at", "within_last", 1), ctx))

    def test_un_canal_nunca_visto_no_tiene_fecha(self) -> None:
        self.app.recents.push(self.dep, "mpv")
        ctx = self.app.contexto_de_reglas()
        self.assertIsNone(valor_campo(self.la1, "last_played_at", ctx))

    def test_sin_recientes_el_mapa_viene_vacío_pero_existe(self) -> None:
        ctx = self.app.contexto_de_reglas()
        self.assertIsInstance(ctx["last_played"], dict)
        self.assertEqual(ctx["last_played"], {})

    def test_sin_guia_cargada_la_clave_es_none(self) -> None:
        self.app.epg = None
        ctx = self.app.contexto_de_reglas()
        self.assertIsNone(ctx["epg"])

    def test_la_guia_cargada_pasa_tal_cual(self) -> None:
        self.app.epg = "una-guia-falsa"
        ctx = self.app.contexto_de_reglas()
        self.assertEqual(ctx["epg"], "una-guia-falsa")

    def test_construirlo_nunca_revienta(self) -> None:
        """El contexto se pide en plein pantalla; reventar aquí tumba la app."""
        self.app.favorites = None
        self.app.recents = None
        self.assertIsInstance(self.app.contexto_de_reglas(), dict)

    def test_construirlo_con_un_reciente_corrupto_no_revienta(self) -> None:
        self.app.recents = mock.Mock(load=mock.Mock(side_effect=OSError))
        self.assertIsInstance(self.app.contexto_de_reglas(), dict)


class TestElContextoNoGuardaCredenciales(Base):
    """El contexto lleva URLs —`valor_campo` las busca por URL—, pero nunca
    con login y contraseña dentro. Un `favorites.json` se redacta al escribir;
    lo que está en memoria sale de ahí."""

    def test_un_favorito_con_credenciales_no_arrastra_la_contrasena(self) -> None:
        self.favoritos(Channel(name="Privado",
                               url="http://usuario:clave@ejemplo.test/live/1",
                               group="X"))
        import json

        ctx = self.app.contexto_de_reglas()
        texto = json.dumps(sorted(ctx["favorites"]))
        self.assertNotIn("clave", texto)

    def test_construir_el_contexto_no_escribe_nada(self) -> None:
        """Se pide en pleno render: si escribiera, cada repintado tocaría disco."""
        self.favoritos(self.dep)
        self.app.recents.push(self.dep, "mpv")
        antes = {p.name: p.read_bytes() for p in self.tmp.iterdir()}
        self.app.contexto_de_reglas()
        self.app.contexto_de_reglas()
        despues = {p.name: p.read_bytes() for p in self.tmp.iterdir()}
        self.assertEqual(antes, despues)


# ---------------------------------------------------------------------------
# 2. Lo que estaba roto: «mis favoritos» salía con cero
# ---------------------------------------------------------------------------


class TestLoQueEstabaRoto(Base):
    def test_sin_contexto_un_favorito_parecia_que_no_lo_es(self) -> None:
        """El bug, con su número exacto, para que no se pueda olvidar."""
        self.favoritos(self.dep)
        self.cargar()
        col = Coleccion(id="x", name="x", type="smart",
                        rules=hoja("favorites", "equals", "true"))
        self.assertEqual(len(evaluar_coleccion(col, self.catalogo, {})), 0)

    def test_con_el_contexto_de_la_app_entra_el_favorito(self) -> None:
        self.favoritos(self.dep)
        self.cargar()
        col = Coleccion(id="x", name="x", type="smart",
                        rules=hoja("favorites", "equals", "true"))
        ctx = self.app.contexto_de_reglas()
        self.assertEqual(len(evaluar_coleccion(col, self.catalogo, ctx)), 1)

    def test_la_coleccion_creada_tiene_los_canales_correctos(self) -> None:
        self.favoritos(self.dep)
        self.cargar()
        cid = self.app.collections.crear(
            "Mis favoritos", tipo="smart",
            rules=hoja("favorites", "equals", "true")).id
        # El catálogo se pasa como en la pantalla (U-03 lo deja fijo en el
        # constructor): sin listas cargadas no hay nada que contar.
        self.assertEqual(
            len(self.app.collections.miembros(cid, self.catalogo)), 1)


class TestElGestorUsaElContexto(Base):
    def test_el_gestor_recibe_el_contexto_de_la_app(self) -> None:
        """Si el proveedor no está conectado, el arreglo no sirve de nada."""
        self.assertIsNotNone(
            getattr(self.app.collections, "context_provider", None))

    def test_marcar_un_favorito_cambia_el_conteo_sin_recargar(self) -> None:
        """El contexto cambia sin que cambie la lista, así que un resultado
        cacheado con el contexto viejo sería un número congelado."""
        self.favoritos(self.dep)
        self.cargar()
        cid = self.app.collections.crear(
            "Favoritos", tipo="smart",
            rules=hoja("favorites", "equals", "true")).id
        antes = len(self.app.collections.miembros(cid, self.catalogo))
        self.favoritos(self.la1)
        despues = len(self.app.collections.miembros(cid, self.catalogo))
        self.assertNotEqual(antes, despues)
        self.assertEqual(despues, 2)

    def test_una_coleccion_manual_no_cambia_de_comportamiento(self) -> None:
        cid = self.app.collections.crear(
            "Mía", tipo="manual",
            members=[str(channel_identity(self.dep))]).id
        self.cargar()
        self.assertEqual(
            [c.name for c in self.app.collections.miembros(cid, self.catalogo)],
            ["Dep 1"])
        # Y la caché sigue sirviendo: sin contexto no hay nada que refrescar.
        self.app.collections.miembros(cid)
        self.assertTrue(self.app.collections._cache_result)


class TestElContadorDeLaPantalla(Base):
    def test_el_contador_de_una_coleccion_de_favoritos_es_el_real(self) -> None:
        self.favoritos(self.dep)
        self.cargar()
        cid = self.app.collections.crear(
            "Mis favoritos", tipo="smart",
            rules=hoja("favorites", "equals", "true")).id
        pantalla = CollectionsScreen(self.app, self.catalogo)
        self.assertIn("1", pantalla._detalles[cid][0])


# ---------------------------------------------------------------------------
# 3. Lo que sigue sin poder responderse
# ---------------------------------------------------------------------------


class TestLoQueNoHay(Base):
    def test_los_campos_sin_datos_se_dicen(self) -> None:
        self.assertEqual(
            sorted(rule_text.campos_sin_contexto(
                hoja("recent_failure_count", "gt", 3))),
            ["Fallos recientes"],
        )

    def test_los_campos_que_ya_saben_no_se_declaran_sin_contexto(self) -> None:
        """Favoritos, guía y última vez visto ya se responden: seguír
        marcándolos como imposibles sería hacer perder la regla al usuario."""
        for campo, op, val in (("favorites", "equals", "true"),
                               ("epg_status", "equals", "available"),
                               ("last_played_at", "after", "2026-01-01"),
                               ("group", "contains", "x")):
            with self.subTest(campo=campo):
                self.assertEqual(rule_text.campos_sin_contexto(
                    hoja(campo, op, val)), [])

    def test_la_salud_no_se_declara_respondible(self) -> None:
        """La salud se sondea por pantalla y se apaga al salir: no queda
        nada que un constructor pueda leer."""
        self.assertEqual(
            rule_text.campos_sin_contexto(hoja("playback_health", "equals", "x")),
            ["Salud al abrirse"])

    def test_el_constructor_marca_los_no_disponibles(self) -> None:
        """Aparecen con su motivo al lado, no desaparecen.

        Un campo que sale de la lista sin explicación parece un fallo; uno que
        sale con «la app todavía no guarda esto» es una respuesta (la misma
        regla que fijó U-03b).
        """
        items = {i.key: i for i in rule_text.items_de_campo()}
        for clave in ("recent_failure_count", "playback_health"):
            with self.subTest(campo=clave):
                self.assertFalse(items[clave].selectable)
                self.assertTrue(items[clave].note)

    def test_el_motivo_dice_que_falta(self) -> None:
        items = {i.key: i for i in rule_text.items_de_campo()}
        nota = items["recent_failure_count"].note
        self.assertTrue(nota)
        self.assertNotIn("regla", nota.lower())

    def test_los_demas_campos_son_elegibles(self) -> None:
        items = rule_text.items_de_campo()
        no_elegibles = {i.key for i in items if not i.selectable}
        self.assertEqual(no_elegibles, {"recent_failure_count",
                                        "playback_health"})

    def test_la_explicacion_de_un_campo_no_elegible_no_promete_una_explicacion(self) -> None:
        """Los elegibles llevan «qué mira»; los que no, «por qué no». Una
        explicación de uso en un campo que no se puede usar es contradictoria."""
        items = {i.key: i for i in rule_text.items_de_campo()}
        for clave in ("recent_failure_count", "playback_health"):
            with self.subTest(campo=clave):
                nota = items[clave].note.lower()
                self.assertNotIn("cuántas veces", nota)


# ---------------------------------------------------------------------------
# 4. La vista previa
# ---------------------------------------------------------------------------


class TestVistaPrevia(Base):
    def test_una_regla_de_favoritos_ya_se_cuenta(self) -> None:
        self.favoritos(self.dep)
        self.cargar()
        self.assertEqual(
            self.app._cuenta_de_regla(hoja("favorites", "equals", "true")), 1)

    def test_el_mensaje_dice_el_numero_y_no_que_no_se_puede(self) -> None:
        self.favoritos(self.dep)
        self.cargar()
        _titulo, mensaje = self.app._mensaje_de_vista_previa(
            hoja("favorites", "equals", "true"))
        self.assertIn("1 canal", mensaje)
        self.assertNotIn("No se puede contar", mensaje)

    def test_sin_favoritos_una_regla_de_favoritos_da_cero_de_verdad(self) -> None:
        """Aquí el cero sí es verdad: la app sí lo sabe y no hay ninguno."""
        self.cargar()
        _titulo, mensaje = self.app._mensaje_de_vista_previa(
            hoja("favorites", "equals", "true"))
        self.assertIn("Ningún canal", mensaje)

    def test_un_campo_sin_datos_sigue_sin_contarse(self) -> None:
        self.cargar()
        self.assertIsNone(self.app._cuenta_de_regla(
            hoja("playback_health", "equals", "online")))
        _titulo, mensaje = self.app._mensaje_de_vista_previa(
            hoja("playback_health", "equals", "online"))
        self.assertIn("Salud al abrirse", mensaje)


# ---------------------------------------------------------------------------
# 5. La navegación salta lo que no se puede elegir
# ---------------------------------------------------------------------------


class TestLaPistaSaltaLosCamposMuertos(Base):
    def _modal(self):
        from thetvview.ui.widgets import ChecklistModal, radio_actions

        return ChecklistModal("Campos", rule_text.items_de_campo(),
                              actions=radio_actions(), radio=True)

    def test_arranca_en_un_campo_elegible(self) -> None:
        modal = self._modal()
        self.assertTrue(modal.items[modal.selected].selectable)

    def test_bajar_desde_el_ultimo_no_vuelve_al_principio(self) -> None:
        import curses

        modal = self._modal()
        for _ in range(len(modal.items) + 3):
            modal.handle_key(curses.KEY_DOWN)
        elegible = [i.selectable for i in modal.items]
        self.assertTrue(elegible[modal.selected])

    def test_no_se_puede_elegir_un_campo_muerto(self) -> None:
        modal = self._modal()
        for i, item in enumerate(modal.items):
            if not item.selectable:
                modal.selected = i
                with self.subTest(campo=item.key):
                    self.assertIsNone(modal.handle_key(10))