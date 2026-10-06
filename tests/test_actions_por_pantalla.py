"""Tests de integración de las acciones contextuales por pantalla (plan F5).

Comprueba lo que sólo se puede ver con la pantalla delante — **la invariante
que sostiene el modelo**:

    keys(actions()) ⊆ keys(shortcuts()) ∧ ⊆ handle_key()

Es decir: **todo lo que la barra inferior anuncia, la pantalla ya lo hace**.
Si algún día alguien declara una acción cuya tecla no está en `shortcuts()` o
que `handle_key` ignora, la suite lo ve aquí — no el usuario en producción
(R-09, AC-03).

Además cubre:

- `? Ayuda` presente y `essential` en las 10 pantallas;
- etiqueta dinámica de favorito: `Favorito` ↔ `Quitar` según el estado real;
- EPG sin catch-up **no** anuncia «Archivo»;
- navegación Canales→Favoritos→EPG→Pistas→volver: el subconjunto visible
  cambia en cada transición (AC-02);
- resize 80→40→100: recalcula y no deja restos (§40).

Sin curses: `FakeStdscr` + dobles mínimos, igual que el resto de la suite.
"""

from __future__ import annotations

import curses
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.ui import colors
from thetvview.ui.actions import V, fit_actions
from thetvview.ui.app import App
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
from thetvview.ui.widgets import FooterBar


# ---------------------------------------------------------------------------
# Doubles y helpers
# ---------------------------------------------------------------------------


def canal(nombre="Canal X", grupo=None, **kw) -> Channel:
    return Channel(name=nombre, url="http://example.test/" + nombre, group=grupo, **kw)


def playlist_canales() -> Playlist:
    """Playlist con dos grupos: `g Grupos` tiene entonces algo que abrir."""
    return Playlist(
        name="Test",
        channels=[
            canal("La 1", "Noticias"),
            canal("La 2", "Noticias"),
            canal("Dep 1", "Deportes"),
        ],
    )


def canal_catchup() -> Channel:
    """Canal de proveedor que **sí** declara archivo (Xtream normalizado)."""
    from thetvview.stream_ref import StreamRef

    return Channel(
        name="ESPN",
        url=StreamRef("Panel", "live", "101").to_opaque(),
        tvg_id="espanol.espn",
        attrs={
            "xtream_id": "101",
            "tv_archive": "1",
            "tv_archive_duration": "7",
        },
    )


class FakeStdscr:
    def __init__(self, h=24, w=80) -> None:
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

    def row_text(self, y):
        return "".join(t for (cy, _x, t, _a) in self.addstr_calls if cy == y)


class _EntradaM3U:
    name = "M3U"
    source = "http://example.test/p.m3u"
    kind = "m3u"
    is_xtream = False


class _EntradaXtream:
    name = "Lista X"
    source = "xtream://user:pass@servidor:8080"
    kind = "xtream"
    is_xtream = True


class _FavoritosFalsos:
    def __init__(self, favs=()) -> None:
        self.favs = set(favs)

    def is_favorite(self, channel):
        return channel.name in self.favs

    def favorite_urls(self):
        return {canal(n).url for n in self.favs}

    def load(self):
        return [canal(n) for n in sorted(self.favs)]

    def toggle(self, channel):
        if channel.name in self.favs:
            self.favs.discard(channel.name)
            return False
        self.favs.add(channel.name)
        return True


class _RecientesFalsos:
    """Doble de `RecentsManager`: guarda `RecentItem` reales en memoria."""

    def __init__(self, items=()) -> None:
        from thetvview.recents import RecentItem

        self.items = [
            i if isinstance(i, RecentItem) else RecentItem(
                name=i.name, url=i.url, group=i.group, player="mpv",
            )
            for i in items
        ]

    def load(self):
        return self.items

    def clear(self):
        self.items = []


class _OpcionPista:
    """Opción mínima: identidad estable y sin etiqueta (aquí no se pinta)."""

    def __init__(self, kind: str, idx: int) -> None:
        self.kind = kind
        self.id = f"{kind}-{idx}"
        self.is_selected = False


class _OpcionesPistas:
    """Lo que `TrackOptionsScreen` pide a la sesión de pistas."""

    def __init__(self, kinds=("audio", "video")) -> None:
        self._kinds = tuple(kinds)
        # Objetos reales y estables: `_section_of` compara por identidad, así
        # que `choices_for` tiene que devolver SIEMPRE los mismos.
        self._choices = {k: [_OpcionPista(k, i) for i in range(2)] for k in self._kinds}

    @property
    def selectable_kinds(self):
        return list(self._kinds)

    def choices_for(self, kind):
        return list(self._choices.get(kind, []))

    def selected_id(self, kind):
        for c in self._choices.get(kind, []):
            if c.is_selected:
                return c.id
        return None


class _SesionPistas:
    def __init__(self, kinds=("audio", "video")) -> None:
        self.kinds = tuple(kinds)
        self.options_obj = _OpcionesPistas(self.kinds)
        self.pending = False
        self.channel = canal("La 1")

    def options(self, player_name=None):
        return self.options_obj

    def select(self, kind, choice_id, *, enabled=None):
        for c in self.options_obj.choices_for(kind):
            c.is_selected = c.id == choice_id

    @property
    def selection(self):
        return None

    def remember(self, *, for_provider: bool = False) -> bool:
        return True


class BaseApp(unittest.TestCase):
    """App real sobre tmpdir aislado, con favoritos/recientes y EPG falsos."""

    def setUp(self) -> None:
        self.fake = FakeStdscr(24, 80)
        tmp = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(ui_app_config(), "PLAYLISTS_JSON", tmp / "p.json"),
            mock.patch.object(ui_app_config(), "FAVORITES_JSON", tmp / "f.json"),
            mock.patch.object(ui_app_config(), "PREFS_JSON", tmp / "prefs.json"),
            mock.patch.object(ui_app_config(), "RECENTS_JSON", tmp / "r.json"),
            mock.patch.object(ui_app_config(), "THEME_JSON", tmp / "theme.json"),
            mock.patch("thetvview.config.ensure_dirs", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = App(self.fake)
        self.app.favorites = _FavoritosFalsos({"La 1"})
        self.app.recents = _RecientesFalsos([canal("La 1", "Noticias")])
        self.app.playlists.load = lambda: []  # type: ignore[method-assign]
        # EPG ya cargado: sin esto `EpgScreen` pediría la ruta por un modal, que
        # es justo lo que este test no quiere medir.
        self.programa_actual()
        self.addCleanup(self._parar_epg)

    def programa_actual(self, titulo="Ahora") -> None:
        """Deja un EPG en memoria con un programa en directo para "La 1"."""
        from thetvview.epg_parser import Epg, Program

        now = datetime.now().astimezone()
        self.app.epg = Epg(
            programs={"la1": [Program("la1", titulo, now - timedelta(minutes=5),
                                      now + timedelta(minutes=5))]},
            channels_by_id={"la1": "La 1"},
        )
        self.app.epg_source = "memoria"

    def _parar_epg(self) -> None:
        thread = getattr(self.app, "_epg_load_thread", None)
        if thread is not None and hasattr(thread, "is_alive") and thread.is_alive():
            thread.join(timeout=0.5)

    # -- pantallas ----------------------------------------------------------
    def pantallas(self) -> dict:
        app = self.app
        pl = playlist_canales()
        playlists = PlaylistsScreen(app)
        playlists.entries = [_EntradaM3U()]
        playlists.selected = 0

        canales = ChannelsScreen(app, pl)
        canales.list.selected = 0

        grupos = GroupsScreen(app, pl)

        reproductor = PlayerScreen(app, canal("La 1"))
        reproductor.players = [("mpv", "/usr/bin/mpv")]

        pistas = TrackOptionsScreen(app, canal("La 1"), _SesionPistas())

        return {
            "Playlists": playlists,
            "Channels": canales,
            "Favorites": FavoritesScreen(app),
            "Recents": RecentsScreen(app),
            "Groups": grupos,
            "Resolution": ResolutionScreen(app, canal("La 1"),
                                        [canal("La 1 HD"), canal("La 1 SD")]),
            "Player": reproductor,
            "Pistas": pistas,
            "NowPlaying": NowPlayingScreen(app, canal("La 1"), "mpv", mock.Mock()),
            "Epg": self.epg_pantalla(),
        }

    def epg_pantalla(self, screen_channel=None) -> EpgScreen:
        """EPG real (con el EPG en memoria de `programa_actual`)."""
        return EpgScreen(self.app, screen_channel or canal("La 1"))


def ui_app_config():
    """Módulo de configuración (import diferido: `App` lo usa al construirse)."""
    from thetvview import config

    return config


# ---------------------------------------------------------------------------
# Teclas: cómo se mide la invariante
# ---------------------------------------------------------------------------

#: Tecla del catálogo → código curses, para llamar a `handle_key` de verdad.
KEYCODES = {
    "Enter": curses.KEY_ENTER,
    "Esc": 27,
    "Ctrl-U": 21,
    "Espacio": ord(" "),
    "Tab": 9,
    "↑↓": curses.KEY_DOWN,
    "←→": curses.KEY_RIGHT,
}

#: Las únicas dos teclas que la barra anuncia y atiende `App.run` **antes** de
#: delegar en la pantalla. Todo lo demás —incluidas las flechas y `Ctrl-U`— lo
#: responde la propia pantalla, así que también se comprueba.
GLOBAL_KEYS = {"Esc", "?"}

#: Códigos que consume `ScrollableList.handle_key` (mover cursor, ir a extremos).
LIST_KEYS = {
    curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE,
    curses.KEY_HOME, curses.KEY_END, ord("k"), ord("j"), ord("g"), ord("G"),
}


def keycodes_for(key: str) -> list[int]:
    if key in KEYCODES:
        return [KEYCODES[key]]
    if len(key) == 1:
        return [ord(key)]
    raise AssertionError(f"tecla sin código conocido en el test: {key!r}")


def shortcut_keys(shortcuts: str) -> set[str]:
    """Teclas que `shortcuts()` anuncia, con el parser real de la App.

    Se usa `App._parse_shortcuts_to_chips` —no una copia— para que el test
    mida exactamente lo que el pie mide. Cuando el chip no trae etiqueta
    («←/→» a secas, sin palabra detrás), el parser deja la tecla vacía y
    guarda el texto en la etiqueta: se lee de ahí.
    """
    app = object.__new__(App)
    teclas: set[str] = set()
    for key, label in App._parse_shortcuts_to_chips(app, shortcuts):
        token = (key or label.split(None, 1)[0]).strip()
        # Sólo las flechas se agrupan con «/» («↑/↓»). La tecla `/` de Buscar
        # también usa «/», pero ahí es una tecla, no un separador.
        if token[:1] in ("↑", "←"):
            teclas.update(p for p in token.split("/") if p)
        elif token:
            teclas.add(token)
    return teclas


def busca(screen, key: str):
    return next((a for a in screen.actions() if a.key == key), None)


def label_de(screen, key: str) -> str | None:
    accion = busca(screen, key)
    return accion.label if accion else None


def ficha(screen) -> list[tuple[str, str]]:
    """(key, label) de todo lo que la barra puede mostrar en esta pantalla."""
    return [(a.key, a.label) for a in fit_actions(screen.actions(), 10_000)]


# ---------------------------------------------------------------------------
# 1. La invariante fundamental
# ---------------------------------------------------------------------------


def tecla_declarada(accion_key: str, declaradas: set[str]) -> bool:
    """¿`shortcuts()` anuncia esa tecla?

    Las teclas de navegación se declaran **sueltas** y separadas por `/`
    («↑/↓ elegir»), así que `Action(key="↑↓")` se considera declarada si sus
    dos teclas están. Para todo lo demás basta con la coincidencia exacta: no
    se perdona nada por ser «parecida».
    """
    if accion_key in declaradas:
        return True
    if accion_key in ("↑↓", "←→"):
        return all(tecla in declaradas for tecla in accion_key)
    return False


class TestInvarianteTeclas(BaseApp):
    """`keys(actions()) ⊆ shortcuts()` y `handle_key` responde de verdad."""

    def _comprobar_shortcuts(self, screen) -> None:
        """Falla si la barra anuncia una tecla que `shortcuts()` no declara.

        Vive en un método (y no suelto en el test) para que los tests de
        «el test muerde» puedan invocarla contra una pantalla falsa.
        """
        declaradas = shortcut_keys(screen.shortcuts())
        for accion in screen.actions():
            self.assertTrue(
                tecla_declarada(accion.key, declaradas),
                f"{type(screen).__name__}: la barra anuncia {accion.key!r} y "
                f"shortcuts() no la declara ({screen.shortcuts()!r})",
            )

    def _comprobar_handle_key(self, screen) -> None:
        """Falla si `handle_key` no responde a alguna tecla anunciada."""
        for accion in screen.actions():
            if accion.key in GLOBAL_KEYS:
                continue
            for code in keycodes_for(accion.key):
                self._pulsa_y_mira(screen, code)

    def test_toda_tecla_anunciada_esta_en_shortcuts(self):
        """R-09 (1ª mitad): la barra no anuncia teclas que `shortcuts()` no
        declara en ese estado."""
        for nombre, screen in self.pantallas().items():
            with self.subTest(pantalla=nombre):
                self._comprobar_shortcuts(screen)

    def test_tecla_de_nav_siempre_en_shortcuts(self):
        """«↑↓» y «←→» se declaran como teclas sueltas en `shortcuts()`."""
        for nombre, screen in self.pantallas().items():
            declaradas = shortcut_keys(screen.shortcuts())
            for accion in screen.actions():
                if accion.key in ("↑↓", "←→"):
                    with self.subTest(pantalla=nombre, key=accion.key):
                        for parte in accion.key:
                            self.assertIn(parte, declaradas)

    def test_handle_key_responde_a_lo_anunciado(self):
        """R-09 (2ª mitad): `handle_key` **hace algo** con esa tecla.

        Observacional a propósito: muchas ramas sólo mueven el cursor y
        devuelven `None`, así que se mide si cambió el estado observable.
        """
        for nombre, screen in self.pantallas().items():
            with self.subTest(pantalla=nombre):
                self._comprobar_handle_key(screen)

    def _estado(self, screen) -> str:
        partes = [repr(getattr(screen, "selected", None)),
                  repr(getattr(screen, "searching", None)),
                  repr(getattr(screen, "query", None)),
                  repr(getattr(screen, "marcados", None)),
                  repr(getattr(screen, "selected_button", None)),
                  self.app.status.message]
        lista = getattr(screen, "list", None) or getattr(screen, "_list", None)
        if lista is not None:
            partes.append(repr(lista.selected))
        return "|".join(partes)

    def _pulsa_y_mira(self, screen, code: int) -> None:
        antes_estado = self._estado(screen)
        antes_chips = list(getattr(self.app.footer, "chips", []))
        resultado = screen.handle_key(code)
        cambio = (
            resultado is not None
            or self._estado(screen) != antes_estado
            or list(getattr(self.app.footer, "chips", [])) != antes_chips
        )
        self.assertTrue(
            cambio or code in LIST_KEYS,
            f"{type(screen).__name__}.handle_key({code!r}) no hizo nada: "
            f"una tecla que no hace nada no se anuncia en la barra",
        )

    def test_actions_de_la_base_devuelve_lista_vacia(self):
        """Pantalla sin catálogo → `[]`, y la app cae a `shortcuts()`."""
        self.assertEqual(Screen(self.app).actions(), [])

    def test_una_accion_inexistente_se_detecta(self):
        """El test **muerde**: una acción falsa lo hace fallar.

        Sin esto, «la invariante se cumple» no significaría nada. Se declara
        una acción con una tecla que `shortcuts()` no anuncia y se comprueba
        que la comprobación la rechaza de verdad.
        """
        class _Mentirosa(Screen):
            title = "X"

            def shortcuts(self):
                return "Enter abrir · Esc volver"

            def actions(self):
                from thetvview.ui.actions import Action, P

                return [Action("z", V.VER, P.PRIMARIA)]

            def handle_key(self, key):
                return None

            def render(self, stdscr):
                pass

        with self.assertRaises(AssertionError):
            self._comprobar_shortcuts(_Mentirosa(self.app))

    def test_una_tecla_muda_se_detecta(self):
        """El test también muerde por el lado de `handle_key`.

        Una tecla que `handle_key` ignora por completo no puede anunciarse: es
        la mitad del R-09 que `shortcuts()` no puede cubrir.
        """
        class _Muda(Screen):
            title = "X"

            def shortcuts(self):
                return "z hace algo"

            def actions(self):
                from thetvview.ui.actions import Action, P

                return [Action("z", V.VER, P.PRIMARIA)]

            def handle_key(self, key):
                return None  # no hace nada

            def render(self, stdscr):
                pass

        with self.assertRaises(AssertionError):
            self._comprobar_handle_key(_Muda(self.app))


# ---------------------------------------------------------------------------
# 2. `? Ayuda` presente y esencial
# ---------------------------------------------------------------------------


class TestAyudaEsencial(BaseApp):
    def test_ayuda_presente_y_esencial_en_las_diez(self):
        pantallas = self.pantallas()
        self.assertEqual(len(pantallas), 10)
        for nombre, screen in pantallas.items():
            with self.subTest(pantalla=nombre):
                ayuda = [a for a in screen.actions() if a.key == "?"]
                self.assertEqual(len(ayuda), 1, f"{nombre}: falta o duplica `?`")
                self.assertTrue(ayuda[0].essential, f"{nombre}: `?` debe ser essential")
                self.assertEqual(ayuda[0].label, V.AYUDA)

    def test_ayuda_es_la_de_menor_prioridad_del_catalogo(self):
        for nombre, screen in self.pantallas().items():
            with self.subTest(pantalla=nombre):
                ayuda = next(a for a in screen.actions() if a.key == "?")
                for otra in screen.actions():
                    if otra.key != "?":
                        self.assertLessEqual(ayuda.priority, otra.priority)

    def test_ayuda_sobrevive_a_la_barra_estrecha(self):
        """Con 40 columnas, `? Ayuda` sigue visible aunque se caiga `Esc Volver`."""
        for nombre, screen in self.pantallas().items():
            if nombre == "NowPlaying":
                continue  # no declara Esc Volver: su barra es más corta
            with self.subTest(pantalla=nombre):
                visibles = [a.key for a in fit_actions(screen.actions(), 40)]
                self.assertIn("?", visibles, f"{nombre}: `?` no sobrevive a 40 columnas")


# ---------------------------------------------------------------------------
# 3. Etiquetas dinámicas
# ---------------------------------------------------------------------------


class TestEtiquetasDinamicas(BaseApp):
    def test_favorito_depende_del_estado_real(self):
        screen = ChannelsScreen(self.app, playlist_canales())
        screen.list.selected = 0  # "La 1" sí es favorita
        self.assertEqual(label_de(screen, "f"), V.QUITAR)

        self.app.favorites.favs.clear()
        screen2 = ChannelsScreen(self.app, playlist_canales())
        screen2.list.selected = 0
        self.assertEqual(label_de(screen2, "f"), V.FAVORITO)

    def test_favoritos_siempre_dice_quitar(self):
        self.assertEqual(label_de(FavoritesScreen(self.app), "f"), V.QUITAR)

    def test_favoritos_vacio_no_miente(self):
        vacio = FavoritesScreen(self.app)
        vacio.channels = []
        vacio.list.set_items([])
        accion = busca(vacio, "f")
        self.assertIsNotNone(accion)
        self.assertFalse(accion.enabled, "sin canales, `f` no está disponible")

    def test_canales_vacio_no_miente(self):
        vacio = ChannelsScreen(self.app, Playlist(name="Vacia", channels=[]))
        for key in ("Enter", "f", "e"):
            with self.subTest(key=key):
                self.assertFalse(busca(vacio, key).enabled)

    def test_recientes_refleja_el_estado(self):
        from thetvview.recents import RecentItem

        self.app.favorites.favs.clear()
        self.app.recents.items = [
            RecentItem(name="La 2", url="http://example.test/La 2",
                       group="Noticias", player="mpv")
        ]
        self.assertEqual(label_de(RecentsScreen(self.app), "f"), V.FAVORITO)

        self.app.favorites.favs.add("La 2")
        self.assertEqual(label_de(RecentsScreen(self.app), "f"), V.QUITAR)

    def test_contrasena_x_solo_con_xtream(self):
        screen = PlaylistsScreen(self.app)
        screen.entries = [_EntradaM3U()]
        self.assertIsNone(busca(screen, "C"), "una lista M3U no cambia la contraseña")

        screen.entries = [_EntradaXtream()]
        accion = busca(screen, "C")
        self.assertIsNotNone(accion, "una Lista X sí puede cambiar la contraseña")

    def test_catalogo_vacio_solo_anuncia_a_y_ayuda(self):
        screen = PlaylistsScreen(self.app)
        screen.entries = []
        self.assertEqual({a.key for a in screen.actions()}, {"a", "?"})

    def test_epg_sin_catchup_no_anuncia_archivo(self):
        screen = EpgScreen(self.app, canal("La 1"))
        self.assertFalse(screen.has_catchup)
        self.assertEqual(label_de(screen, "Enter"), V.VER)
        fila = " ".join(a.chip() for a in screen.actions())
        self.assertNotIn(V.ARCHIVO, fila)

    def test_epg_con_catchup_anuncia_archivo(self):
        screen = EpgScreen(self.app, canal_catchup())
        self.assertTrue(screen.has_catchup)
        self.assertEqual(label_de(screen, "Enter"), V.ARCHIVO)

    def test_pistas_enter_depende_del_player_ya_elegido(self):
        session = _SesionPistas()
        sin_elegir = TrackOptionsScreen(self.app, canal("La 1"), session, player_name=None)
        self.assertEqual(label_de(sin_elegir, "Enter"), V.REPRODUCTOR)

        elegido = TrackOptionsScreen(self.app, canal("La 1"), session, player_name="mpv")
        self.assertEqual(label_de(elegido, "Enter"), V.VER)

    def test_pistas_sin_secciones_no_anuncia_marcar(self):
        vacia = TrackOptionsScreen(self.app, canal("La 1"), _SesionPistas(kinds=()))
        for key in ("↑↓", "Espacio", "Tab"):
            with self.subTest(key=key):
                self.assertFalse(busca(vacia, key).enabled)

    def test_pistas_con_una_seccion_no_anuncia_saltar(self):
        """Con una sola sección `Tab` no lleva a ninguna parte."""
        una = TrackOptionsScreen(self.app, canal("La 1"), _SesionPistas(kinds=("audio",)))
        self.assertFalse(busca(una, "Tab").enabled)
        self.assertTrue(busca(una, "Espacio").enabled)

    def test_busqueda_cambia_el_juego_de_teclas(self):
        screen = ChannelsScreen(self.app, playlist_canales())
        normal = {a.key for a in screen.actions()}
        screen.searching = True
        self.assertEqual({a.key for a in screen.actions()}, {"Enter", "Ctrl-U", "Esc"})
        self.assertNotEqual(normal, {"Enter", "Ctrl-U", "Esc"})

    def test_grupos_busqueda_igual_que_canales(self):
        screen = GroupsScreen(self.app, playlist_canales())
        screen.searching = True
        self.assertEqual({a.key for a in screen.actions()}, {"Enter", "Ctrl-U", "Esc"})

    def test_reproductor_sin_pistas_no_anuncia_info(self):
        screen = NowPlayingScreen(self.app, canal("La 1"), "mpv", mock.Mock())
        self.assertIsNone(screen.tracks)
        self.assertIsNone(busca(screen, "i"))

    def test_reproductor_con_pistas_anuncia_info(self):
        screen = NowPlayingScreen(self.app, canal("La 1"), "mpv", mock.Mock(),
                                  track_session=mock.Mock())
        self.assertIsNotNone(busca(screen, "i"))

    def test_reproductor_sin_players_no_anuncia_elegir(self):
        screen = PlayerScreen(self.app, canal("La 1"))
        screen.players = []
        for key in ("Enter", "↑↓"):
            with self.subTest(key=key):
                self.assertFalse(busca(screen, key).enabled)

    def test_calidad_sin_variantes_no_anuncia_elegir(self):
        screen = ResolutionScreen(self.app, canal("La 1"), [])
        for key in ("Enter", "←→"):
            with self.subTest(key=key):
                self.assertFalse(busca(screen, key).enabled)


# ---------------------------------------------------------------------------
# 4. El pie real: navegación y resize
# ---------------------------------------------------------------------------


class TestPieEnNavegacion(BaseApp):
    """El pie refleja la pantalla actual y recalcula al cambiar de ancho."""

    def _pintar_pie(self, screen=None) -> str:
        """Pinta el pie de verdad y devuelve la fila del último `addstr`.

        Se limpia el doble antes de cada pintado: si no, las llamadas de un
        ancho anterior se mezclan con las del nuevo y el test de resize mide
        filas acumuladas en vez de la línea actual.
        """
        if screen is not None:
            # `App.screen` es el top del stack: se cambia ahí, no por setter.
            self.app.stack[-1] = screen
        self.fake.erase()
        self.app.footer = FooterBar()
        with mock.patch.object(colors, "pair", return_value=0):
            self.app.footer._flash_until = 0.0
            self.app._render_footer(self.fake)
        return self.fake.row_text(self.fake.getmaxyx()[0] - 1)

    def test_el_pie_cambia_en_cada_transicion(self):
        """Canales → Favoritos → EPG → Pistas: subconjunto visible distinto."""
        pl = playlist_canales()
        browsing = [
            ChannelsScreen(self.app, pl),
            FavoritesScreen(self.app),
            EpgScreen(self.app, canal("La 1")),
            TrackOptionsScreen(self.app, canal("La 1"), _SesionPistas()),
        ]
        fichas = [set(ficha(s)) for s in browsing]
        for i in range(len(fichas) - 1):
            with self.subTest(de=browsing[i].__class__.__name__,
                              a=browsing[i + 1].__class__.__name__):
                self.assertNotEqual(fichas[i], fichas[i + 1])

    def test_el_pie_real_cambia_al_cambiar_de_pantalla(self):
        pl = playlist_canales()
        primera = self._pintar_pie(ChannelsScreen(self.app, pl))
        self.fake.addstr_calls.clear()
        segunda = self._pintar_pie(FavoritesScreen(self.app))
        self.assertNotEqual(primera, segunda)
        self.assertIn("Enter Ver", primera)
        self.assertIn("Quitar", segunda)

    def test_ayuda_se_pinta_en_cada_pantalla(self):
        pl = playlist_canales()
        for screen in self.pantallas().values():
            with self.subTest(pantalla=type(screen).__name__):
                fila = self._pintar_pie(screen)
                self.assertIn("Ayuda", fila)

    def test_resize_recalcula_y_no_deja_restos(self):
        pl = playlist_canales()
        self.app.stack[-1] = ChannelsScreen(self.app, pl)
        filas: dict[int, str] = {}
        for w in (80, 40, 100, 30, 80):
            self.fake.resize(24, w)
            filas[w] = self._pintar_pie()
            for _y, x, texto, _a in self.fake.addstr_calls:
                self.assertLessEqual(x + len(texto), w, f"residuo en {w} columnas")
        # Se compara el contenido, no el relleno: a 80 y a 100 columnas cabe
        # lo mismo y sólo cambia el espacio en blanco de la derecha.
        self.assertNotEqual(filas[80].rstrip(), filas[40].rstrip(),
                            "80 y 40 deben dar subconjuntos distintos")
        self.assertEqual(filas[80].rstrip(), filas[100].rstrip(),
                         "sin recorte, más ancho no puede cambiar el subconjunto")
        self.assertLess(len(filas[40].rstrip()), len(filas[80].rstrip()))

    def test_terminal_diminuto_no_rompe(self):
        self.app.stack[-1] = ChannelsScreen(self.app, playlist_canales())
        for h, w in ((2, 10), (3, 20), (24, 12), (24, 3)):
            with self.subTest(tam=f"{h}x{w}"):
                self.fake.resize(h, w)
                self._pintar_pie()
                for _y, x, texto, _a in self.fake.addstr_calls:
                    self.assertLessEqual(x + len(texto), w)

    def test_pie_sin_catalogo_cae_a_shortcuts(self):
        """Pantalla sin `actions()`: la app no se rompe y usa `shortcuts()`."""

        class _SinCatalogo(Screen):
            title = "X"

            def shortcuts(self):
                return "Enter abrir · Esc volver"

            def render(self, stdscr):
                pass

        fila = self._pintar_pie(_SinCatalogo(self.app))
        self.assertIn("abrir", fila)

    def test_actions_que_falla_no_rompe_la_app(self):
        """Un `actions()` con error degrada a `shortcuts()`, nunca a un traceback."""

        class _Rota(Screen):
            title = "X"

            def shortcuts(self):
                return "Enter abrir"

            def actions(self):
                raise RuntimeError("boom")

            def render(self, stdscr):
                pass

        fila = self._pintar_pie(_Rota(self.app))
        self.assertIn("abrir", fila)

    def test_chips_reflejan_lo_que_cabe(self):
        pl = playlist_canales()
        self.app.stack[-1] = ChannelsScreen(self.app, pl)
        self._pintar_pie()
        self.assertEqual([k for k, _ in self.app.footer.chips][0], "Enter")


if __name__ == "__main__":
    unittest.main()