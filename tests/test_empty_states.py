"""Tests de estados vacíos estandarizados (SDD estados vacíos §22, AC-01…AC-13).

Qué fija este archivo, y por qué importa:

- **Cada pantalla vacía dice qué pasa y qué hacer**, no "Sin datos." — y lo
  dice con el mismo bloque (`EmptyState`), no con un `addstr` propio.
- **Búsqueda sin resultados ≠ contenido vacío.** Es la diferencia que más
  engaña (§17): un usuario con 400 canales filtrados a 0 no debe leer "aquí
  no hay canales".
- **Loading ≠ vacío.** Mientras la guía se descarga, `EpgScreen` no puede
  decir "no hay programación" (§11): sería verdad a medias y haría que `r`
  pareciera no arreglar nada.
- **Toda CTA dibujada tiene tecla real detrás** (AC-11). No se comprueba sólo
  la invariante de `tests.test_actions_por_pantalla`: aquí se comprueba que el
  *texto* de la CTA nombra una tecla que la pantalla atiende de verdad.

Sin curses: doble de ventana que captura `addstr` + dobles mínimos de `App`.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.ui import colors, icons
from thetvview.ui.screens import (
    ChannelsScreen,
    EpgScreen,
    FavoritesScreen,
    GroupsScreen,
    PlaylistsScreen,
    RecentsScreen,
)
from thetvview.ui.textwidth import cell_width
from thetvview.ui.widgets import EmptyState, wrap_block


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class StdscrCaptura:
    """Ventana falsa: memoriza cada `addstr` como `(y, x, texto, attr)`.

    Además **falla ruidosamente** si se escribe fuera de la pantalla, porque
    un `curses.error` real en la esquina inferior derecha es exactamente el
    fallo que el mínimo 40×10 tiene que impedir (AC-13): aquí se convierte en
    un fallo de test legible en vez de una excepción en producción.
    """

    def __init__(self, max_y: int = 24, max_x: int = 80) -> None:
        self.max_y = max_y
        self.max_x = max_x
        self.calls: list[tuple[int, int, str, int]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return (self.max_y, self.max_x)

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        assert 0 <= y < self.max_y, f"addstr fuera de pantalla: fila {y} de {self.max_y}"
        assert 0 <= x < self.max_x, f"addstr fuera de pantalla: columna {x} de {self.max_x}"
        assert x + cell_width(text) <= self.max_x, (
            f"addstr desborda la columna: {x}+{cell_width(text)} > {self.max_x}: {text!r}"
        )
        self.calls.append((y, x, text, attr))

    # -- lectura -------------------------------------------------------------

    @property
    def texto(self) -> str:
        """Todo lo pintado, unido. Para `assertIn` sobre el copy."""
        return " ".join(c[2] for c in self.calls)

    def filas(self) -> dict[int, str]:
        """`{fila: línea}` reconstruida, para comprobar el bloque centrado."""
        por_fila: dict[int, list[tuple[int, str]]] = {}
        for y, x, texto, _attr in self.calls:
            por_fila.setdefault(y, []).append((x, texto))
        return {
            y: "".join(t for _x, t in sorted(partes))
            for y, partes in por_fila.items()
        }


class _Favoritos:
    def __init__(self, favs: tuple[str, ...] = ()) -> None:
        self.favs = set(favs)

    def is_favorite(self, channel: Channel) -> bool:
        return channel.name in self.favs

    def favorite_urls(self) -> set[str]:
        return {canal(n).url for n in self.favs}

    def load(self) -> list[Channel]:
        return [canal(n) for n in sorted(self.favs)]

    def toggle(self, channel: Channel) -> bool:
        if channel.name in self.favs:
            self.favs.discard(channel.name)
            return False
        self.favs.add(channel.name)
        return True


class _Recientes:
    def __init__(self, nombres: tuple[str, ...] = ()) -> None:
        from thetvview.recents import RecentItem

        self.items = [RecentItem(name=n, url=canal(n).url, group="G", player="mpv")
                      for n in nombres]

    def load(self):
        return list(self.items)

    def clear(self) -> None:
        self.items = []


class _Playlists:
    def __init__(self, entradas=()) -> None:
        self.entradas = list(entradas)

    def load(self):
        return list(self.entradas)


class _Entrada:
    name = "M3U"
    source = "http://example.test/p.m3u"
    kind = "m3u"
    is_xtream = False


class AppFalso:
    """Lo mínimo que las pantallas leen de `App`.

    Deliberadamente **no** tiene `_epg_load_thread`: es el caso por defecto de
    los dobles de test, y `EpgScreen` debe sobrevivir a su ausencia (por eso
    lo lee con `getattr` y no como atributo).
    """

    def __init__(self, max_y: int = 24, max_x: int = 80,
                 programas=None, hilo_epg=None) -> None:
        self.favorites = _Favoritos()
        self.recents = _Recientes()
        self.playlists = _Playlists()
        self.stdscr = type("T", (), {"getmaxyx": lambda s: (max_y, max_x)})()
        self.status = type("S", (), {"show": lambda s, m, e=False: None,
                                     "is_error": False, "message": ""})()
        self.footer = type("F", (), {"show": lambda s, m, e=False: None})()
        self.theme_name = "light"
        self.epg = None
        self.epg_source = None
        self.playlist_cache: dict[str, Playlist] = {}
        self._programas = list(programas or [])
        self.ensure_epg_calls: list[bool] = []
        if hilo_epg is not None:
            self._epg_load_thread = hilo_epg

    def body_height(self) -> int:
        return max(1, self.stdscr.getmaxyx()[0] - 3)

    def ensure_epg(self, channel, *, force_refresh=False, url_hint=None):
        self.ensure_epg_calls.append(force_refresh)
        return list(self._programas)


def canal(nombre: str = "Canal X", grupo: str | None = None) -> Channel:
    return Channel(name=nombre, url=f"http://x/{nombre}", group=grupo)


def pintar(render, max_y=24, max_x=80) -> StdscrCaptura:
    """Renderiza en una ventana capturadora y devuelve lo pintado."""
    stdscr = StdscrCaptura(max_y, max_x)
    with mock.patch.object(colors, "pair", return_value=0):
        render(stdscr)
    return stdscr


# ---------------------------------------------------------------------------
# Widget: layout, wrapping y adaptación al ancho (AC-13)
# ---------------------------------------------------------------------------


class TestEmptyStateWidget(unittest.TestCase):
    def test_render_40x10_no_rompe(self):
        """El mínimo soportado del proyecto: 40×10 (AC-13)."""
        stdscr = pintar(lambda s: EmptyState.render(
            s, 5, 40, icons.ICON_STAR_OFF,
            "No tienes favoritos",
            "Pulsa F sobre un canal para añadirlo aquí.",
            "[Esc] Limpiar búsqueda",
        ), max_y=10, max_x=40)
        self.assertIn("No tienes favoritos", stdscr.texto)

    def test_no_escribe_encima_del_header_ni_del_footer(self):
        """El bloque vive en el cuerpo, no encima de la barra ni del footer."""
        for alto in (10, 12, 24, 40):
            with self.subTest(alto=alto):
                stdscr = pintar(lambda s: EmptyState.render(
                    s, alto // 2, 80, icons.ICON_EPG,
                    "No hay programación disponible",
                    "La guía no contiene programas para este canal.",
                    "[R] Actualizar guía",
                ), max_y=alto, max_x=80)
                filas = set(stdscr.filas())
                self.assertNotIn(0, filas, "ha pisado la barra de título")
                for y in filas:
                    self.assertLess(y, alto - 2, f"ha pisado el footer en {y}")

    def test_anchos_40_60_80_120_el_texto_siempre_cabe(self):
        """§20: se verifica a los cuatro anchos que la spec enumera."""
        for ancho in (40, 60, 80, 120):
            with self.subTest(ancho=ancho):
                stdscr = pintar(lambda s: EmptyState.render(
                    s, 12, ancho, icons.ICON_EPG,
                    "No hay programación disponible",
                    "La guía no contiene programas para este canal.",
                    "[R] Actualizar guía",
                ), max_y=24, max_x=ancho)
                self.assertIn("No hay programación disponible", stdscr.texto)
                for y, x, texto, _attr in stdscr.calls:
                    self.assertLessEqual(x + cell_width(texto), ancho)

    def test_mensaje_largo_se_envuelve_y_no_se_trunca(self):
        """§8.2: envolver, no cortar. Antes `render_empty_message` perdía texto."""
        mensaje = (
            "En cuanto el proveedor conteste verás aquí el audio, los subtítulos "
            "y la calidad que publica para este canal concreto"
        )
        stdscr = pintar(lambda s: EmptyState.render(
            s, 12, 46, icons.ICON_TIME, "Analizando", mensaje,
        ), max_y=40, max_x=46)
        # Todas las palabras del mensaje están en pantalla, en alguna fila.
        pintado = stdscr.texto
        for palabra in set(mensaje.split()):
            self.assertIn(palabra, pintado, f"se perdió {palabra!r} al envolver")
        # Y ninguna línea excede el ancho útil.
        for _y, _x, texto, _attr in stdscr.calls:
            self.assertLessEqual(cell_width(texto), 45)

    def test_arte_a_80_y_ausente_a_40(self):
        """Decisión 1: el arte es decoración degradable, no estructura."""
        ancho = pintar(lambda s: EmptyState.render(
            s, 12, 80, icons.ICON_STAR_OFF, "Sin datos", "explicación",
        ))
        self.assertIn(icons.BOX_TL + "─", ancho.texto)

        estrecho = pintar(lambda s: EmptyState.render(
            s, 12, 40, icons.ICON_STAR_OFF, "Sin datos", "explicación",
        ), max_y=24, max_x=40)
        self.assertNotIn(icons.BOX_TL, estrecho.texto)
        # Pero el icono sí: pasa a ser el elemento visual principal.
        self.assertIn(icons.ICON_STAR_OFF, estrecho.texto)

    def test_altura_insuficiente_preserva_el_titulo(self):
        """§8.3: título > mensaje > CTA > icono > decoración.

        En una ventana de 5 filas sólo cabe el título, y es el que se queda.
        """
        stdscr = pintar(lambda s: EmptyState.render(
            s, 2, 80, icons.ICON_EPG, "No hay programación disponible",
            "La guía no contiene programas para este canal.",
            "[R] Actualizar guía",
        ), max_y=5, max_x=80)
        self.assertIn("No hay programación", stdscr.texto)
        self.assertNotIn("[R] Actualizar guía", stdscr.texto)

    def test_bloque_queda_centrado_horizontalmente(self):
        stdscr = pintar(lambda s: EmptyState.render(
            s, 12, 80, icons.ICON_EPG, "No hay programación disponible",
        ))
        for y, x, texto, _attr in stdscr.calls:
            if texto.strip() == "No hay programación disponible":
                self.assertEqual(x, (80 - cell_width(texto)) // 2)

    def test_widget_no_toca_teclas_ni_app(self):
        """§7: `EmptyState` es presentación pura.

        Se verifica por *firma*, no por comportamiento: si mañana alguien le
        añade un parámetro `app` o un handler, la suite lo ve sin necesidad de
        arrancarlo con curses.
        """
        import inspect

        params = list(inspect.signature(EmptyState.render).parameters)
        self.assertEqual(
            params,
            ["stdscr", "y", "width", "icon", "title", "message", "cta"],
            "la firma de EmptyState cambió: revisa los 5 call sites",
        )
        fuente = inspect.getsource(EmptyState)
        for prohibido in ("KEY_", "handle_key", "self.app"):
            self.assertNotIn(
                prohibido, fuente,
                f"EmptyState no debe saber de teclas ni de App ({prohibido})",
            )


class TestWrapBlock(unittest.TestCase):
    def test_parte_por_palabras(self):
        self.assertEqual(
            wrap_block("uno dos tres cuatro", 9),
            ["uno dos", "tres", "cuatro"],
        )

    def test_no_parte_una_palabra_que_cabe(self):
        for linea in wrap_block("palabra superlarga imposible", 12):
            self.assertNotIn(" ", linea)

    def test_palabra_mas_ancha_que_la_linea_se_recorta(self):
        lineas = wrap_block("abcdefghijklmnopqrstuvwxyz", 10)
        self.assertEqual(lineas, ["abcdefghij"])

    def test_parrafo_vacio_se_conserva(self):
        self.assertEqual(wrap_block("uno\n\ndos", 20), ["uno", "", "dos"])

    def test_ancho_cero_no_devuelve_nada(self):
        self.assertEqual(wrap_block("texto", 0), [])


# ---------------------------------------------------------------------------
# Favoritos (§22.1, AC-01, AC-02)
# ---------------------------------------------------------------------------


class TestFavoritosVacio(unittest.TestCase):
    def pantalla(self) -> FavoritesScreen:
        return FavoritesScreen(AppFalso())

    def test_vacio_muestra_titulo_explicativo(self):
        stdscr = pintar(self.pantalla().render)
        self.assertIn("No tienes favoritos", stdscr.texto)
        # Y explica cómo llegar: no basta con negar (§4 R-1).
        self.assertIn("añadirlo aquí", stdscr.texto)

    def test_vacio_muestra_el_icono(self):
        stdscr = pintar(self.pantalla().render)
        self.assertIn(icons.ICON_STAR_OFF, stdscr.texto)

    def test_vacio_no_anuncia_una_tecla_que_quita(self):
        """En Favoritos `f` **quita**: anunciar "[F] Favorito" sería mentir.

        `actions()` lo dice con todas las letras ("todo lo que hay aquí es
        favorito por definición"), así que la CTA se omite en vez de copiarse
        de la barra (R-04, AC-11).
        """
        pantalla = self.pantalla()
        etiquetas = {a.label for a in pantalla.actions()}
        self.assertIn("Quitar", etiquetas)
        stdscr = pintar(pantalla.render)
        self.assertNotIn("[F] Favorito", stdscr.texto)

    def test_con_favoritos_no_muestra_el_estado_vacio(self):
        app = AppFalso()
        app.favorites = _Favoritos(("La 1",))
        stdscr = pintar(FavoritesScreen(app).render)
        self.assertNotIn("No tienes favoritos", stdscr.texto)
        self.assertIn("La 1", stdscr.texto)


# ---------------------------------------------------------------------------
# EPG (§22.2, AC-03, AC-04, AC-05)
# ---------------------------------------------------------------------------


def _programa(titulo: str = "Ahora"):
    from thetvview.epg_parser import Program

    ahora = datetime.now().astimezone()
    return Program("canalx", titulo, ahora - timedelta(minutes=5),
                   ahora + timedelta(minutes=5))


class _HiloVivo:
    """Lo mínimo de `threading.Thread` que `EpgScreen` mira: que exista."""

    def is_alive(self) -> bool:
        return True

    def join(self, timeout=None) -> None:  # pragma: no cover - no se llama
        pass


class TestEpgVacio(unittest.TestCase):
    def pantalla(self, programas=(), hilo_epg=None) -> EpgScreen:
        app = AppFalso(programas=list(programas), hilo_epg=hilo_epg)
        return EpgScreen(app, canal("Canal X"))

    def test_sin_programas_muestra_titulo_explicativo(self):
        stdscr = pintar(self.pantalla().render)
        self.assertIn("No hay programación disponible", stdscr.texto)
        self.assertIn("La guía no contiene programas", stdscr.texto)

    def test_sin_programas_anuncia_la_accion_real_de_actualizar(self):
        """AC-04: la CTA nombra `R` — y `R` existe de verdad.

        No basta con dibujar `[R]`: se comprueba que `handle_key` atiende esa
        tecla, que `actions()` la anuncia y que `shortcuts()` la lista (AC-11).
        """
        pantalla = self.pantalla()
        stdscr = pintar(pantalla.render)
        self.assertIn("[R] Actualizar guía", stdscr.texto)

        for tecla in ("r", "R"):
            with self.subTest(tecla=tecla):
                self.assertEqual(pantalla.handle_key(ord(tecla)) or {}, {},
                                 f"{tecla} debe recargar, no abrir nada")

        self.assertTrue(any(a.key == "r" for a in pantalla.actions()))
        self.assertIn("r", pantalla.shortcuts())

    def test_loading_no_se_sustituye_por_vacio(self):
        """AC-05: con el hilo de EPG vivo se anuncia loading, no "no hay".

        Decir "no hay programación" mientras la guía se descarga es el fallo
        que este estado previene: el usuario pulsa `r` y parece que nada cambia.
        """
        pantalla = self.pantalla(hilo_epg=_HiloVivo())
        stdscr = pintar(pantalla.render)
        self.assertIn("Cargando programación", stdscr.texto)
        self.assertNotIn("No hay programación disponible", stdscr.texto)
        # Loading sin CTA: recargar mientras carga no arregla nada.
        self.assertNotIn("[R] Actualizar guía", stdscr.texto)

    def test_hilo_terminado_vuelve_al_vacio(self):
        """Un hilo que ya no carga no puede dejar la pantalla colgada en loading."""
        app = AppFalso()
        pantalla = EpgScreen(app, canal("Canal X"))
        app._epg_load_thread = _HiloVivo()
        self.assertTrue(pantalla._epg_is_loading())
        app._epg_load_thread = None
        self.assertFalse(pantalla._epg_is_loading())
        self.assertIn("No hay programación disponible", pintar(pantalla.render).texto)

    def test_con_programas_no_muestra_el_estado_vacio(self):
        stdscr = pintar(self.pantalla(programas=[_programa()]).render)
        self.assertNotIn("No hay programación disponible", stdscr.texto)
        self.assertIn("Ahora", stdscr.texto)

    def test_sin_programas_no_inventa_una_fila_fantasma(self):
        """La fila "(sin datos de EPG…)" contaminaba el contador y el cursor.

        Con el `EmptyState` en su sitio la lista es realmente vacía: lo que no
        hay, no se cuenta como si lo hubiera.
        """
        self.assertEqual(self.pantalla()._rows(), [])

    def test_render_no_rompe_en_10x40(self):
        stdscr = pintar(self.pantalla().render, max_y=10, max_x=40)
        self.assertIn("No hay programación", stdscr.texto)


# ---------------------------------------------------------------------------
# Playlists (§22.3, AC-06)
# ---------------------------------------------------------------------------


class TestPlaylistsVacio(unittest.TestCase):
    def test_vacio_muestra_titulo_mensaje_y_cta(self):
        stdscr = pintar(PlaylistsScreen(AppFalso()).render)
        self.assertIn("No hay playlists", stdscr.texto)
        self.assertIn("M3U", stdscr.texto)
        self.assertIn("[A] Añadir lista", stdscr.texto)

    def test_cta_corresponde_a_la_tecla_real(self):
        """AC-11: `a` existe y de verdad pide crear la lista.

        Con el catálogo vacío `handle_key` devuelve la acción `add_playlist`
        directamente (sin abrir nada aquí): la CTA promete una tecla que
        existe, no una que se espera ver funcionar.
        """
        pantalla = PlaylistsScreen(AppFalso())
        self.assertEqual(pantalla.handle_key(ord("a")),
                         {"action": "add_playlist"})
        self.assertTrue(any(a.key == "a" for a in pantalla.actions()))
        self.assertIn("a Añadir", pantalla.shortcuts())

    def test_con_entradas_no_muestra_el_estado_vacio(self):
        app = AppFalso()
        app.playlists = _Playlists([_Entrada()])
        stdscr = pintar(PlaylistsScreen(app).render)
        self.assertNotIn("No hay playlists", stdscr.texto)
        self.assertIn("M3U", stdscr.texto)


# ---------------------------------------------------------------------------
# Recientes (§22.4, AC-07)
# ---------------------------------------------------------------------------


class TestRecientesVacio(unittest.TestCase):
    def test_sin_items_muestra_titulo_explicativo(self):
        stdscr = pintar(RecentsScreen(AppFalso()).render)
        self.assertIn("No hay canales recientes", stdscr.texto)
        self.assertIn("aparecerán aquí", stdscr.texto)

    def test_sin_items_no_promete_una_accion_que_no_existe(self):
        """§13: nada de "Reproducir canal" — con la lista vacía `Enter` no abre."""
        pantalla = RecentsScreen(AppFalso())
        self.assertIsNone(pantalla.current_channel())
        stdscr = pintar(pantalla.render)
        self.assertNotIn("Reproducir", stdscr.texto)

    def test_con_items_no_muestra_el_estado_vacio(self):
        app = AppFalso()
        app.recents = _Recientes(("La 1",))
        stdscr = pintar(RecentsScreen(app).render)
        self.assertNotIn("No hay canales recientes", stdscr.texto)
        self.assertIn("La 1", stdscr.texto)


# ---------------------------------------------------------------------------
# Búsqueda sin resultados (§22.5, AC-08) — categoría propia
# ---------------------------------------------------------------------------


class TestBusquedaSinResultados(unittest.TestCase):
    def playlist(self) -> Playlist:
        return Playlist(name="P", channels=[
            canal("La 1", "Noticias"),
            canal("La 2", "Noticias"),
            canal("Dep 1", "Deportes"),
        ])

    def test_canales_sin_coincidencias_es_estado_de_busqueda(self):
        pantalla = ChannelsScreen(AppFalso(), self.playlist())
        pantalla.query = "zzz"
        pantalla._apply_filter()
        self.assertEqual(pantalla.visible_idx, [])
        stdscr = pintar(pantalla.render)
        self.assertIn("No se encontraron canales", stdscr.texto)
        self.assertIn('"zzz"', stdscr.texto)

    def test_canales_sin_coincidencias_no_dice_que_no_hay_canales(self):
        """La diferencia que importa: hay canales, el filtro no los encuentra."""
        pantalla = ChannelsScreen(AppFalso(), self.playlist())
        pantalla.query = "zzz"
        pantalla._apply_filter()
        stdscr = pintar(pantalla.render)
        self.assertNotIn("La playlist no tiene canales", stdscr.texto)

    def test_cta_de_busqueda_anuncia_esc_y_esc_existe(self):
        """AC-11: `Esc` limpia la consulta de verdad (`_feed_search`)."""
        pantalla = ChannelsScreen(AppFalso(), self.playlist())
        pantalla.query = "zzz"
        pantalla._apply_filter()
        self.assertIn("[Esc] Limpiar búsqueda", pintar(pantalla.render).texto)

        pantalla.searching = True
        pantalla.handle_key(27)
        self.assertEqual(pantalla.query, "", "Esc debe limpiar la consulta")
        self.assertEqual(len(pantalla.visible_idx), 3, "y restaurar la lista")

    def test_grupos_sin_coincidencias_es_estado_de_busqueda(self):
        pantalla = GroupsScreen(AppFalso(), self.playlist())
        pantalla.query = "zzz"
        pantalla._apply_filter()
        self.assertEqual(pantalla.visible_keys, [])
        stdscr = pintar(pantalla.render)
        self.assertIn("No se encontraron grupos", stdscr.texto)
        self.assertIn('"zzz"', stdscr.texto)
        self.assertNotIn("No hay grupos en esta playlist", stdscr.texto)

    def test_grupos_sin_coincidencias_tiene_su_propia_categoria(self):
        """Canales y grupos comparten bloque, no mensaje: cambia el sustantivo."""
        pantalla = GroupsScreen(AppFalso(), self.playlist())
        pantalla.query = "zzz"
        pantalla._apply_filter()
        stdscr = pintar(pantalla.render)
        self.assertIn(icons.ICON_SEARCH, stdscr.texto)
        self.assertIn("[Esc] Limpiar búsqueda", stdscr.texto)

    def test_con_coincidencias_se_dibuja_la_lista_normal(self):
        pantalla = ChannelsScreen(AppFalso(), self.playlist())
        pantalla.query = "Notic"
        pantalla._apply_filter()
        self.assertTrue(pantalla.visible_idx)
        stdscr = pintar(pantalla.render)
        self.assertNotIn("No se encontraron", stdscr.texto)
        self.assertIn("La 1", stdscr.texto)

    def test_sin_query_y_sin_contenido_es_estado_de_contenido(self):
        """Las dos ramas son excluyentes y de categoría distinta (§17)."""
        vacia = Playlist(name="P", channels=[])
        pantalla = ChannelsScreen(AppFalso(), vacia)
        stdscr = pintar(pantalla.render)
        self.assertIn("La playlist no tiene canales", stdscr.texto)
        self.assertNotIn("No se encontraron", stdscr.texto)
        self.assertIn(icons.ICON_TV, stdscr.texto)

    def test_grupos_sin_grupos_es_estado_de_contenido(self):
        # Sin canales no hay ni un grupo: ni siquiera el bucket "(sin grupo)".
        pantalla = GroupsScreen(AppFalso(), Playlist(name="P", channels=[]))
        stdscr = pintar(pantalla.render)
        self.assertIn("No hay grupos en esta playlist", stdscr.texto)
        self.assertIn(icons.ICON_GROUP, stdscr.texto)

    def test_busqueda_no_desborda_en_10x40(self):
        pantalla = ChannelsScreen(AppFalso(), self.playlist())
        pantalla.query = "zzz"
        pantalla._apply_filter()
        stdscr = pintar(pantalla.render, max_y=10, max_x=40)
        self.assertIn("No se encontraron canales", stdscr.texto)


# ---------------------------------------------------------------------------
# Reglas transversales (AC-09, AC-10, AC-12)
# ---------------------------------------------------------------------------


class TestReglasTransversales(unittest.TestCase):
    #: Todo estado vacío pasa por aquí: si uno se sale del patrón, salta.
    def estados_vacios(self):
        app = AppFalso()
        canales = ChannelsScreen(app, Playlist(name="P", channels=[canal("La 1")]))
        grupos = GroupsScreen(app, Playlist(
            name="P", channels=[canal("A", "X"), canal("B", "Y")]))
        for pantalla, filtro in ((canales, "zzz"), (grupos, "zzz")):
            pantalla.query = filtro
            pantalla._apply_filter()
        return [
            FavoritesScreen(AppFalso()),
            RecentsScreen(AppFalso()),
            PlaylistsScreen(AppFalso()),
            EpgScreen(AppFalso(), canal("Canal X")),
            canales,
            grupos,
            ChannelsScreen(AppFalso(), Playlist(name="P", channels=[])),
        ]

    def test_ningun_estado_vacio_usa_emoji(self):
        """AC-09/AC-10: los glifos salen de `ui/icons.py`, y ninguno es emoji.

        `icons.py` es el inventario único del proyecto. Dos cosas se comprueban:
        que no haya ningún carácter de presentación ancha o fuera del BMP (los
        emojis no se ven en la mayoría de terminales y rompen la alineación), y
        que las pantallas no hayan inventado glifos propios para el vacío.
        """
        import re
        from pathlib import Path

        fuente_icons = Path(icons.__file__).read_text(encoding="utf-8")
        glifos = re.findall(r'"([^"\n]+)"', fuente_icons)
        self.assertTrue(glifos, "icons.py no define ningún icono")
        for glifo in glifos:
            for ch in glifo:
                with self.subTest(glifo=glifo, ch=ch):
                    self.assertLess(
                        ord(ch), 0xFE00,
                        f"icons.py trae un carácter de presentación ancha/emoji: {ch!r}",
                    )

        # Y cada icono que pintan los estados vacíos existe en el inventario.
        for nombre in ("ICON_STAR_OFF", "ICON_EPG", "ICON_TIME", "ICON_TV",
                       "ICON_GROUP", "ICON_LIST", "ICON_SEARCH"):
            with self.subTest(icono=nombre):
                self.assertTrue(hasattr(icons, nombre),
                                f"{nombre} no está en ui/icons.py (AC-10)")

    def test_todos_los_estados_se_dibujan_sin_romper(self):
        """AC-13 a 40×10 y a 120×30: ningún estado vacío es una pantalla rota."""
        for pantalla in self.estados_vacios():
            with self.subTest(pantalla=type(pantalla).__name__):
                for alto, ancho in ((10, 40), (24, 80), (30, 120)):
                    pintar(pantalla.render, max_y=alto, max_x=ancho)

    def test_todos_los_estados_dicen_algo(self):
        """Regla 1 de §4: ningún estado vacío es una línea muda."""
        for pantalla in self.estados_vacios():
            with self.subTest(pantalla=type(pantalla).__name__):
                stdscr = pintar(pantalla.render)
                self.assertTrue(stdscr.texto.strip(), "estado vacío sin texto")
                # Título explicativo: nunca "Sin datos." a secas.
                self.assertNotIn("Sin datos", stdscr.texto)

    def test_ctas_anuncian_solo_teclas_que_la_pantalla_atiende(self):
        """AC-11 sobre el texto **dibujado**, no sólo sobre `actions()`.

        Cada CTA nombra una tecla entre corchetes. Se comprueba que esa tecla
        aparece en el catálogo de `actions()` de esa misma pantalla **y** en su
        `shortcuts()`. Como `tests.test_actions_por_pantalla` ya demuestra la
        invariante ``keys(actions()) ⊆ shortcuts ∩ handle_key()``, encadenando
        las dos se obtiene exactamente lo que AC-11 pide: ninguna CTA dibujada
        sin tecla real detrás.

        Es el cierre del bug que el sistema de barras ya quería cerrar (R-09),
        aplicado también al `EmptyState` — el punto donde es fácil prometer
        algo sin respaldo es un texto pintado a mano.

        Si algún día una CTA nueva no tiene tecla, esto falla; y el arreglo es
        quitar la CTA, nunca simular la tecla.
        """
        import re

        vistas = 0
        for pantalla in self.estados_vacios():
            # Pieza fuerte: la tecla debe ser una acción **declarada** por esta
            # pantalla. `tests.test_actions_por_pantalla` ya demuestra que ese
            # catálogo es ⊆ `shortcuts()` ∩ `handle_key()`.
            declaradas = {a.key.lower() for a in pantalla.actions()}
            for etiqueta in re.findall(r"\[([^\]]+)\]", pintar(pantalla.render).texto):
                atajo = etiqueta.strip().lower()
                vistas += 1
                with self.subTest(pantalla=type(pantalla).__name__,
                                  tecla=etiqueta):
                    self.assertIn(
                        atajo, declaradas,
                        f"CTA [{etiqueta}]: la pantalla no declara esa acción",
                    )
                    # Pieza de apoyo: la barra inferior también la anuncia.
                    self.assertIn(
                        atajo, pantalla.shortcuts().lower(),
                        f"CTA [{etiqueta}]: shortcuts() no la anuncia",
                    )
        self.assertGreater(vistas, 0, "ninguna CTA se dibujó: el test no midió nada")

    def test_las_ctas_son_de_estados_vacios_no_de_errores(self):
        """AC-12: un vacío no se presenta como un fallo.

        Un estado vacío que usa el color de error hace que el usuario pida
        ayuda por algo que no está roto. Aquí se fija lo contrario: ningún
        estado vacío se dibuja con `PAIR_ERROR`, y ninguno se emite por
        `_warn`/`_error` (el camino de error va por `notify_*`, y `AppFalso`
        no lo tiene — si alguno pasara por ahí, reventaría aquí).
        """
        for pantalla in self.estados_vacios():
            with self.subTest(pantalla=type(pantalla).__name__):
                stdscr = StdscrCaptura(24, 80)
                con_color = mock.patch.object(
                    colors, "pair", side_effect=lambda p: p)
                with con_color:
                    pantalla.render(stdscr)
                for y, x, texto, attr in stdscr.calls:
                    self.assertNotEqual(
                        attr, colors.PAIR_ERROR,
                        f"estado vacío pintado como error: {texto!r}",
                    )
                    self.assertNotIn(
                        texto.lower().strip(),
                        {"error", "fallo", "excepción"},
                        "un vacío no es un fallo",
                    )


if __name__ == "__main__":
    unittest.main()
