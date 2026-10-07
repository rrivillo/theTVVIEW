"""Tests de la cabecera consistente (SDD Cabecera Consistente, pasos 5-7).

El principio que esta suite vigila es uno solo y va antes que todos los demás:
**la pantalla dice QUÉ contexto se ve; el `HeaderBar` decide CÓMO se dibuja**.

De ahí sale la invariante central, comprobada contra el *código fuente* y no
contra una expectativa escrita a mano: `HeaderBar` no puede mencionar pantallas
con `isinstance`, ni tocar playlists, EPG ni favoritas. Si algún día alguien lo
hace "sólo para un caso raro", este test falla en el CI y no en la revisión.

Todo se mide con `cell_width()`, nunca con `len()`: `★ ▶ ◈` son *East Asian
Ambiguous* y un emoji ocupa 2 celdas, así que la aserción clave —la que verifica
que la fila 0 no desborda— sería falsa con `len()`.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.ui import app as ui_app
from thetvview.ui import colors, icons
from thetvview.ui.screens import (
    ChannelsScreen,
    EpgScreen,
    FavoritesScreen,
    GroupsScreen,
    NowPlayingScreen,
    PlayerScreen,
    PlaylistsScreen,
    RecentsScreen,
    ResolutionScreen,
    Screen,
    TrackOptionsScreen,
)
from thetvview.ui.textwidth import cell_width
from thetvview.ui.widgets import HeaderBar


# ---------------------------------------------------------------------------
# Dobles de prueba
# ---------------------------------------------------------------------------

class _FakeStdscr:
    """Doble de ventana curses con **buffer real**: lo que se ve, no lo que se pidió.

    Importa que `addstr` *pise* en vez de acumular: la cabecera pinta en tres
    pasadas (banda, logo encima, tema encima), así que concatenar las llamadas
    daría una línea de 136 columnas donde la pantalla real tiene 80.

    Además registra `overflow`: si un `addstr` se pasa del borde, no lanza
    `curses.error` —marcaría la bandera y recortaría— para que el test pueda
    afirmar sobre el desbordamiento en vez de dejarlo pasar en silencio.
    """

    def __init__(self, max_y: int = 24, max_x: int = 80) -> None:
        self._max_y = max_y
        self._max_x = max_x
        self.calls: list[tuple[int, int, str, int]] = []
        self.overflow = False
        self._filas = [[" "] * max_x for _ in range(max_y)]

    def getmaxyx(self) -> tuple[int, int]:
        return self._max_y, self._max_x

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        self.calls.append((y, x, text, attr))
        if not (0 <= y < self._max_y) or x < 0:
            self.overflow = True
            return
        col = x
        for ch in text:
            w = cell_width(ch)
            if w == 0:
                continue
            if col + w > self._max_x:
                self.overflow = True
                break
            self._filas[y][col] = ch
            for k in range(1, w):
                self._filas[y][col + k] = ""  # celdacontinuación
            col += w

    def row0(self) -> str:
        """Fila 0 tal y como queda en pantalla."""
        return "".join(self._filas[0])

    def erase(self) -> None:
        self._filas = [[" "] * self._max_x for _ in range(self._max_y)]

    def refresh(self) -> None:
        pass

    def getch(self) -> int:
        return -1


class _RecordingHeader:
    """Doble de `HeaderBar` que **graba** el contexto que le pasan (§29)."""

    def __init__(self) -> None:
        self.contexts: list[str] = []

    def render(self, stdscr, title, stack_depth=1) -> None:  # noqa: ANN001
        self.contexts.append(title)


class _Fav:
    def __init__(self, favs=()):
        self.favs = set(favs)

    def is_favorite(self, channel) -> bool:
        return channel.name in self.favs

    def favorite_urls(self, channels):
        return {c.url for c in channels if c.name in self.favs}


class _Recents:
    def load(self):
        return []


def _ch(name: str, group: str | None = None, radio: bool = False) -> Channel:
    return Channel(name=name, url=f"http://x/{name}", group=group, radio=radio)


def _playlist() -> Playlist:
    return Playlist(
        name="Test",
        channels=[
            _ch("La 1", "Noticias"),
            _ch("La 2", "Noticias"),
            _ch("Radio Pop", "Música"),
        ],
    )


class _StubApp:
    """App mínima para pantallas que sólo necesitan unas pocas cosas."""

    def __init__(self) -> None:
        self.favorites = _Fav()
        self.recents = _Recents()
        self.epg = None

    def body_height(self) -> int:
        return 10

    def ensure_epg(self, channel, url_hint=None):
        return []


# ---------------------------------------------------------------------------
# 1. Contrato de `Screen.header_context()` (paso 1)
# ---------------------------------------------------------------------------

class _HerederaDeScreen(Screen):
    """Pantalla doblada que NO sobrescribe nada (el caso H10 de los tests)."""

    title = "Pantalla doblada"


class TestContratoHeaderContext(unittest.TestCase):
    def test_por_defecto_devuelve_el_titulo(self):
        """§28.1: comportamiento por defecto = `title` (compatibilidad)."""
        screen = _HerederaDeScreen(app=None)
        self.assertEqual(screen.header_context(), screen.title)

    def test_todas_las_pantallas_lo_declaran(self):
        """El contrato existe en la base: heredar es siempre suficiente."""
        for clase in (
            ChannelsScreen, EpgScreen, FavoritesScreen, GroupsScreen,
            NowPlayingScreen, PlayerScreen, PlaylistsScreen, RecentsScreen,
            ResolutionScreen, TrackOptionsScreen,
        ):
            self.assertTrue(
                issubclass(clase, Screen),
                f"{clase.__name__} debería heredar de Screen",
            )

    def test_title_y_header_context_pueden_ser_distintos(self):
        """§12: `title` es la ficha, el contexto es la vista.

        El valor por defecto coincide con `title` —eso es la compatibilidad—,
        pero una pantalla que lo sobrescriba demuestra que son dos salidas
        distintas y no un renombrado del mismo dato.
        """
        class _ConContexto(Screen):
            title = "EPG · CNN"

            def header_context(self) -> str:
                return "Guía TV"

        screen = _ConContexto(app=None)
        self.assertEqual(screen.title, "EPG · CNN")
        self.assertEqual(screen.header_context(), "Guía TV")


# ---------------------------------------------------------------------------
# 2. Contexto por pantalla (paso 2 / §28.2 y §28.3)
# ---------------------------------------------------------------------------

class TestContextoPorPantalla(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _StubApp()
        self.pl = _playlist()

    def test_playlists(self):
        s = PlaylistsScreen.__new__(PlaylistsScreen)  # sin tocar disco
        s.app = self.app
        self.assertEqual(s.header_context(), f"{icons.ICON_LIST} Listas")

    def test_channels(self):
        s = ChannelsScreen(self.app, self.pl)
        self.assertEqual(
            s.header_context(), f"{icons.ICON_TV} Canales · {len(self.pl.channels)}"
        )

    def test_favorites(self):
        s = FavoritesScreen.__new__(FavoritesScreen)
        s.app = self.app
        self.assertEqual(s.header_context(), f"{icons.ICON_STAR} Favoritos")

    def test_recents(self):
        s = RecentsScreen(self.app)
        self.assertEqual(s.header_context(), f"{icons.ICON_ARCHIVE} Recientes")

    def test_groups(self):
        s = GroupsScreen.__new__(GroupsScreen)
        s.app = self.app
        self.assertEqual(s.header_context(), f"{icons.ICON_GROUP} Grupos")

    def test_resolution(self):
        """§11.7: "Resolución · <canal>" NO se traslada a la cabecera."""
        s = ResolutionScreen(self.app, _ch("La 1"), [_ch("La 1"), _ch("HD")])
        self.assertEqual(s.header_context(), "Calidad")
        self.assertIn("La 1", s.title)  # el canal sigue en su sitio (§11.3)

    def test_track_options(self):
        s = TrackOptionsScreen.__new__(TrackOptionsScreen)
        s.app = self.app
        s.channel = _ch("La 1")
        s.title = "Audio y calidad · La 1"
        self.assertEqual(s.header_context(), "Audio y calidad")

    def test_player(self):
        s = PlayerScreen.__new__(PlayerScreen)
        s.app = self.app
        self.assertEqual(s.header_context(), f"{icons.ICON_PLAY} Reproductor")

    def test_now_playing(self):
        """§11.6: en la cabecera manda la sección, no el canal."""
        proc = mock.Mock()
        proc.poll.return_value = None
        s = NowPlayingScreen(self.app, _ch("La 1"), player_name="mpv", proc=proc)
        self.assertEqual(s.header_context(), f"{icons.ICON_PLAY} Reproduciendo")
        self.assertIn("La 1", s.title)

    def test_epg(self):
        """§12: `EpgScreen.title` sigue siendo `EPG · <canal>`."""
        s = EpgScreen(self.app, _ch("La 1"))
        self.assertEqual(s.header_context(), f"{icons.ICON_EPG} Guía TV")
        self.assertEqual(s.title, "EPG · La 1")
        self.assertIn("La 1", s.title)

    def test_todos_los_iconos_salen_de_icons_py(self):
        """Ningún glifo suelto: el vocabulario vive en un solo sitio."""
        esperados = [
            PlaylistsScreen, ChannelsScreen, FavoritesScreen, GroupsScreen,
            RecentsScreen, PlayerScreen, NowPlayingScreen, EpgScreen,
        ]
        for clase in esperados:
            fuente = inspect.getsource(clase.header_context)
            self.assertIn("icons.", fuente, f"{clase.__name__} debería usar icons.py")


# ---------------------------------------------------------------------------
# 3. El contador de canales (paso 2 / §28.4, §19)
# ---------------------------------------------------------------------------

class TestContadorCanales(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _StubApp()
        self.screen = ChannelsScreen(self.app, _playlist())

    def _numero(self) -> str:
        return self.screen.header_context().rsplit("· ", 1)[-1]

    def test_sin_filtro(self):
        self.assertEqual(self._numero(), "3")

    def test_con_query(self):
        self.screen.query = "noticias"
        self.screen._apply_filter()
        self.assertEqual(len(self.screen.visible_idx), 2)  # "La 1" y "La 2"
        self.assertEqual(self._numero(), "2")

    def test_con_filtro_de_grupo(self):
        filtrada = ChannelsScreen(self.app, _playlist(), group="Música")
        self.assertEqual(filtrada.header_context(), f"{icons.ICON_TV} Canales · 1")

    def test_es_o1_sin_io(self):
        """§19: el contador no lee nada del dominio, sólo `len()` de una lista.

        Los dobles de app **lanzan** si se toca `playlists` o `epg`: así el test
        falla si alguien cambia el contador por algo que lea de disco o red.
        """

        class _Prohibido:
            def __getattr__(self, name):
                raise AssertionError(
                    f"header_context() no debe acceder a app.{name} (§19)"
                )

        screen = ChannelsScreen.__new__(ChannelsScreen)
        screen.channels = [_ch("A"), _ch("B")]
        screen.visible_idx = [0, 1]
        screen.app = _Prohibido()
        self.assertEqual(screen.header_context(), f"{icons.ICON_TV} Canales · 2")


# ---------------------------------------------------------------------------
# 4. Render: responsive, Unicode y truncado (paso 6)
# ---------------------------------------------------------------------------

class TestHeaderRender(unittest.TestCase):
    ANCHOS = (40, 50, 60, 80, 120)

    def _render(self, ctx: str, max_x: int) -> str:
        stdscr = _FakeStdscr(24, max_x)
        header = HeaderBar(app=None)
        with mock.patch.object(colors, "pair", return_value=0):
            header.render(stdscr, ctx, stack_depth=3)
        # Desbordarse es un fallo aunque el widget lo capture con try/except.
        self.assertFalse(
            stdscr.overflow,
            f"la cabecera se pasa de {max_x} columnas con {ctx!r}",
        )
        return stdscr.row0()

    def test_no_desborda_a_cualquier_ancho(self):
        """§15: el ancho mínimo del proyecto es 40 columnas."""
        for max_x in self.ANCHOS:
            for ctx in ("", "Canales", "◉ Canales · 1284", "◈ Guía TV"):
                with self.subTest(max_x=max_x, ctx=ctx):
                    fila = self._render(ctx, max_x)
                    self.assertLessEqual(
                        cell_width(fila), max_x,
                        f"la fila 0 se sale a {max_x} columnas",
                    )

    def test_no_lanza_en_anchos_extremos(self):
        """Ni a 12 (mínimo) ni a 200 (muy ancho): nunca `curses.error`."""
        for max_x in (12, 13, 14, 15, 200):
            with self.subTest(max_x=max_x):
                self._render("◉ Canales · 1284", max_x)

    def test_contexto_alineado_a_la_derecha_a_cualquier_ancho(self):
        """§28.6 + §10: los glifos `◉ ★ ◈ ▶ ▣ ▤ ☀` no desalinean la línea.

        Que el contexto esté *pegado a la derecha* significa, exactamente:

        - el logo empieza siempre en la columna 0;
        - el indicador de tema termina siempre en la última columna;
        - el contexto termina justo donde empieza el tema.

        Las tres cosas se miden en **celdas**, no en caracteres: `★` y `◈` son
        *Ambiguous*, y medirlos con `len()` haría pasar un logo desalineado.
        """
        tema = f" {icons.ICON_THEME_LIGHT} "
        for ctx in ("◉ Canales", "★ Favoritos", "◈ Guía TV", "▶ Reproductor",
                    "▣ Grupos", "▤ Listas", "☀"):
            for max_x in self.ANCHOS:
                with self.subTest(ctx=ctx, max_x=max_x):
                    fila = self._render(ctx, max_x)
                    fin_ctx = fila.index(ctx) + cell_width(ctx)
                    # " ◉ theTVVIEW " → el nombre arranca tras 3 celdas.
                    self.assertEqual(fila.index("theTVVIEW"), 3)
                    self.assertEqual(
                        fin_ctx, max_x - cell_width(tema),
                        "el contexto debe acabar donde empieza el tema",
                    )
                    self.assertEqual(
                        cell_width(fila), max_x,
                        "la línea debe ocupar la pantalla entera, sin huecos",
                    )

    def test_identidad_siempre_presente(self):
        """§16: por estrecho que sea, el logo no se recorta a media palabra."""
        for max_x in (20, 30, 40, 80):
            with self.subTest(max_x=max_x):
                fila = self._render("◉ Canales · 1284", max_x)
                self.assertIn("theTVVIEW", fila)

    def test_truncado_por_el_final(self):
        """§18: 200 caracteres → `…` al final, nunca un glifo partido."""
        largo = "◉ Canales · " + "X" * 200
        for max_x in self.ANCHOS:
            with self.subTest(max_x=max_x):
                fila = self._render(largo, max_x)
                self.assertIn("…", fila)
                self.assertIn("theTVVIEW", fila)
                # El corte no parte el logo por la mitad.
                self.assertIn("theTVVIEW", fila)
                self.assertNotIn("theTV", fila.replace("theTVVIEW", ""))

    def test_sin_contexto_solo_identidad(self):
        fila = self._render("", 80)
        self.assertIn("theTVVIEW", fila)
        self.assertLessEqual(cell_width(fila), 80)

    def test_tema_indicado_a_la_derecha(self):
        for nombre, icono in (("light", icons.ICON_THEME_LIGHT),
                              ("dark", icons.ICON_THEME_DARK)):
            with self.subTest(tema=nombre):
                app = mock.Mock()
                app.theme_name = nombre
                stdscr = _FakeStdscr(24, 80)
                with mock.patch.object(colors, "pair", return_value=0):
                    HeaderBar(app).render(stdscr, "Canales", 1)
                self.assertIn(icono, stdscr.row0())

    def test_contexto_derecha_no_centrado(self):
        """§10: el contexto se alinea a la derecha, no queda centrado."""
        fila = self._render("Canales", 80)
        inicio = fila.index("Canales")
        fin = inicio + cell_width("Canales")
        # A 80 columnas el hueco libre a la izquierda es mucho mayor que el de
        # la derecha: eso es "pegado a la derecha", y es lo contrario de centred.
        self.assertGreater(fila.index("theTVVIEW"), 0)
        self.assertLessEqual(fin, 80)
        self.assertGreater(fin, 80 // 2)


# ---------------------------------------------------------------------------
# 5. La invariante arquitectónica: el widget no sabe de pantallas (paso 6)
# ---------------------------------------------------------------------------

class TestHeaderBarIgnoraElDominio(unittest.TestCase):
    """§31: cero `isinstance` de pantallas, cero acceso al dominio.

    Se comprueba contra `inspect.getsource()` en vez de contra una expectativa:
    es la forma de que la invariante sobreviva a los próximos refactors sin
    depender de que alguien se acuerde de revisarla.
    """

    PROHIBIDAS = (
        "FavoritesScreen", "ChannelsScreen", "EpgScreen", "GroupsScreen",
        "PlaylistsScreen", "RecentsScreen", "ResolutionScreen",
        "TrackOptionsScreen", "PlayerScreen", "NowPlayingScreen",
        "isinstance", "playlist", "epg", "favorites",
    )

    def test_fuente_sin_pantallas_ni_dominio(self):
        fuente = inspect.getsource(HeaderBar)
        for palabra in self.PROHIBIDAS:
            with self.subTest(palabra=palabra):
                self.assertNotIn(
                    palabra, fuente,
                    f"HeaderBar no debe mencionar {palabra!r} (§31)",
                )

    def test_una_sola_clase_de_cabecera(self):
        """La cabecera sigue siendo **una** clase, no una jerarquía."""
        from thetvview.ui import widgets

        clases = [
            n for n, o in vars(widgets).items()
            if isinstance(o, type) and issubclass(o, HeaderBar)
        ]
        self.assertEqual(clases, ["HeaderBar"])

    def test_render_sigue_teniendo_la_firma_original(self):
        """D5: la firma no cambia, y por eso los dobles de test no se tocan."""
        firma = inspect.signature(HeaderBar.render)
        self.assertEqual(
            list(firma.parameters), ["self", "stdscr", "title", "stack_depth"]
        )
        self.assertEqual(firma.parameters["stack_depth"].default, 1)


# ---------------------------------------------------------------------------
# 6. Integración: `App` recoge el contexto (paso 3 / paso 7)
# ---------------------------------------------------------------------------

class TestAppHeaderContext(unittest.TestCase):
    """`App` no sabe qué pantalla es: sólo recoge su contexto."""

    def _app(self, test):
        tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(ui_app.config, "PLAYLISTS_JSON", tmp / "p.json"),
            mock.patch.object(ui_app.config, "FAVORITES_JSON", tmp / "f.json"),
            mock.patch.object(ui_app.config, "PREFS_JSON", tmp / "pr.json"),
            mock.patch.object(ui_app.config, "RECENTS_JSON", tmp / "r.json"),
            mock.patch.object(ui_app.config, "THEME_JSON", tmp / "t.json"),
            mock.patch("thetvview.config.ensure_dirs", lambda: None),
            mock.patch.object(colors, "pair", return_value=0),
        ]
        for p in patches:
            p.start()
            test.addCleanup(p.stop)
        return ui_app.App(_FakeStdscr(24, 80))

    def _con_pantalla(self, screen) -> None:
        """Coloca `screen` en el stack (`App.screen` es sólo lectura)."""
        self.app.stack = [screen]

    def setUp(self) -> None:
        self.app = self._app(self)
        self.app.header = _RecordingHeader()

    def test_recibe_header_context_de_la_pantalla(self):
        self.app.epg = None
        self.app.ensure_epg = mock.Mock(return_value=[])
        self._con_pantalla(EpgScreen(self.app, _ch("La 1")))
        self.assertEqual(
            self.app._header_context(), f"{icons.ICON_EPG} Guía TV"
        )

    def test_cae_al_title_si_la_pantalla_no_lo_tiene(self):
        """Una pantalla doblada sin el método no tumba la cabecera."""
        class _Muda:
            title = "Playlists"

        self._con_pantalla(_Muda())
        self.assertEqual(self.app._header_context(), "Playlists")

    def test_cae_al_title_si_el_metodo_falla(self):
        class _Explota:
            title = "Grupos"

            def header_context(self):
                raise RuntimeError("boom")

        self._con_pantalla(_Explota())
        self.assertEqual(self.app._header_context(), "Grupos")

    def test_sin_pantalla_devuelve_cadena_vacia(self):
        self.app.stack = []
        self.assertEqual(self.app._header_context(), "")

    def test_todas_las_pantallas_emiten_su_contexto(self):
        """§29: recorrido por el stack real, con la cabecera escuchando."""
        pl = _playlist()
        app = self.app
        # `EpgScreen` dispara un prompt de carga si no hay EPG; aquí no.
        app.epg = None
        app.ensure_epg = mock.Mock(return_value=[])
        pantallas = [
            PlaylistsScreen(app),
            GroupsScreen(app, pl),
            ChannelsScreen(app, pl),
            FavoritesScreen(app),
            EpgScreen(app, _ch("La 1")),
            ResolutionScreen(app, _ch("La 1"), [_ch("La 1"), _ch("HD")]),
        ]
        with mock.patch("thetvview.config.detect_players",
                        return_value={"mpv": "/usr/bin/mpv"}):
            pantallas.append(PlayerScreen(app, _ch("La 1")))
        proc = mock.Mock()
        proc.poll.return_value = None
        pantallas.append(
            NowPlayingScreen(app, _ch("La 1"), player_name="mpv", proc=proc)
        )
        pantallas.append(RecentsScreen(app))
        for s in pantallas:
            app.push(s)
            with mock.patch("thetvview.config.detect_players",
                            return_value={"mpv": "/usr/bin/mpv"}):
                app.header.render(
                    _FakeStdscr(24, 80), app._header_context(), len(app.stack)
                )

        self.assertEqual(
            self.app.header.contexts,
            [
                f"{icons.ICON_LIST} Listas",
                f"{icons.ICON_GROUP} Grupos",
                f"{icons.ICON_TV} Canales · 3",
                f"{icons.ICON_STAR} Favoritos",
                f"{icons.ICON_EPG} Guía TV",
                "Calidad",
                f"{icons.ICON_PLAY} Reproductor",
                f"{icons.ICON_PLAY} Reproduciendo",
                f"{icons.ICON_ARCHIVE} Recientes",
            ],
        )


if __name__ == "__main__":
    unittest.main()
