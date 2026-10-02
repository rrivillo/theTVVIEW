"""Tests de apertura de listas: caché de sesión, vigencia y feedback.

Regresión del problema de lentitud: reabrir una lista ya parseada no
vuelve a leer la fuente (red/fichero) ni la re-parsea, mientras que la
caché se invalida cuando toca (fichero local modificado, TTL de URL
vencido, contraseña Xtream cambiada). También cubre el refresco de las
etiquetas de ChannelsScreen (cacheadas por tecla de búsqueda).
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.playlist_manager import PlaylistEntry
from thetvview.ui import app as ui_app
from thetvview.ui.screens import ChannelsScreen, open_playlist


# ---------------------------------------------------------------------------
# Dobles mínimos (sin curses real)
# ---------------------------------------------------------------------------

class _Status:
    def __init__(self) -> None:
        self.message = ""
        self.is_error = False

    def show(self, message: str, error: bool = False) -> None:
        self.message = message
        self.is_error = error


class _Fav:
    def __init__(self, urls=()) -> None:
        self.urls = set(urls)

    def favorite_urls(self) -> set[str]:
        return set(self.urls)


class _StubApp:
    """App mínima con la API (y la caché) que open_playlist toca."""

    # Mismos helpers de caché que la App real (doble ligero para tests).
    cached_playlist = ui_app.App.cached_playlist
    remember_playlist = ui_app.App.remember_playlist
    _session_cache_valid = ui_app.App._session_cache_valid
    _is_remote = staticmethod(ui_app.App._is_remote)

    def __init__(self) -> None:
        self.status = _Status()
        self.footer = type("F", (), {"show": lambda self, *a, **k: None})()
        self.favorites = _Fav()
        self.stack: list = []
        self.loading: list[str] = []
        self.playlist_cache: dict = {}
        self.playlist_cache_meta: dict = {}
        self._base_indexes: dict = {}

    @property
    def screen(self):
        return self.stack[-1]

    def push(self, screen) -> None:
        self.stack.append(screen)

    def show_loading(self, message: str = "Cargando…", sub: str = "") -> None:
        self.loading.append(message)

    def body_height(self) -> int:
        return 20


class _PlainApp(_StubApp):
    """Doble SIN helpers de caché: comprueba que se degrada sin romper."""

    cached_playlist = None  # type: ignore[assignment]
    remember_playlist = None  # type: ignore[assignment]


def _ch(name: str, group: str | None = None) -> Channel:
    return Channel(name=name, url=f"http://x/{name}", group=group)


def _playlist(*channels: Channel, kind: str = "m3u") -> Playlist:
    return Playlist(name="P", channels=list(channels), kind=kind)


def _parse_real(_path: Path | None = None):
    """Loader real de ficheros locales (side_effect de load_playlist_source)."""
    from thetvview import m3u_parser

    return lambda source, **_kw: m3u_parser.parse_file(source)


class _AppCase(unittest.TestCase):
    """App real con todos los JSON de datos dentro de un tmpdir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        for attr, name in (
            ("PLAYLISTS_JSON", "playlists.json"),
            ("FAVORITES_JSON", "favorites.json"),
            ("PREFS_JSON", "prefs.json"),
            ("RECENTS_JSON", "recents.json"),
            ("THEME_JSON", "theme.json"),
        ):
            patcher = mock.patch.object(ui_app.config, attr, base / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def make_app(self) -> ui_app.App:
        return ui_app.App(None)  # noqa: ANN001 - no se renderiza


# ---------------------------------------------------------------------------
# 1. Caché de sesión al abrir
# ---------------------------------------------------------------------------

class TestCacheSesionAlAbrir(_AppCase):
    def test_reabrir_no_vuelve_a_cargar_la_fuente(self) -> None:
        """La 2ª apertura reutiliza la caché: loader no se llama otra vez."""
        app = self.make_app()
        entry = PlaylistEntry(name="Remota", source="http://ejemplo.test/lista.m3u")
        cargada = _playlist(_ch("Uno"))

        with mock.patch(
            "thetvview.ui.app.load_playlist_source", return_value=cargada
        ) as loader:
            open_playlist(app, entry)
            loader.assert_called_once()
            open_playlist(app, entry)
            loader.assert_called_once()  # sigue sin recargar

        # PlaylistsScreen inicial + una pantalla por apertura.
        self.assertEqual(len(app.stack), 3)
        self.assertTrue(all(isinstance(s, ChannelsScreen) for s in app.stack[1:]))

    def test_primera_apertura_pide_pantalla_de_carga(self) -> None:
        """Sin caché se pinta 'Cargando…' antes de bloquear en la fuente."""
        app = _StubApp()
        entry = PlaylistEntry(name="Remota", source="http://ejemplo.test/l.m3u")
        with mock.patch(
            "thetvview.ui.app.load_playlist_source",
            return_value=_playlist(_ch("Uno")),
        ):
            open_playlist(app, entry)
            open_playlist(app, entry)
        self.assertEqual(len(app.loading), 1)  # solo la primera vez

    def test_app_sin_helpers_de_cache_sigue_abriendo(self) -> None:
        """Doble sin cached_playlist/remember_playlist (tests viejos): degrada."""
        app = _PlainApp()
        entry = PlaylistEntry(name="Remota", source="http://ejemplo.test/l.m3u")
        with mock.patch(
            "thetvview.ui.app.load_playlist_source",
            return_value=_playlist(_ch("Uno")),
        ):
            open_playlist(app, entry)
        self.assertEqual(app.playlist_cache[entry.source].channels[0].name, "Uno")

    def test_fichero_local_modificado_invalida_la_cache(self) -> None:
        """Si el .m3u local cambia, la 2ª apertura lo vuelve a leer."""
        app = self.make_app()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lista.m3u"
            path.write_text("#EXTM3U\n#EXTINF:-1,Uno\nhttp://u/1\n", encoding="utf-8")
            entry = PlaylistEntry(name="Local", source=str(path))

            with mock.patch(
                "thetvview.ui.app.load_playlist_source",
                side_effect=_parse_real(path),
            ) as loader:
                open_playlist(app, entry)
                open_playlist(app, entry)
                loader.assert_called_once()

                # Reescribir con mtime garantizadamente distinto.
                path.write_text("#EXTM3U\n#EXTINF:-1,Dos\nhttp://u/2\n", encoding="utf-8")
                st = path.stat()
                os.utime(path, (st.st_atime, st.st_mtime + 5))

                open_playlist(app, entry)
                self.assertEqual(loader.call_count, 2)

    def test_url_cacheada_vence_con_el_ttl(self) -> None:
        app = self.make_app()
        source = "http://ejemplo.test/l.m3u"
        pl = _playlist(_ch("Uno"))
        app.playlist_cache[source] = pl

        app.playlist_cache_meta[source] = {"at": time.monotonic()}
        self.assertIs(app.cached_playlist(source), pl)

        app.playlist_cache_meta[source] = {"at": time.monotonic() - 7 * 3600}
        self.assertIsNone(app.cached_playlist(source))
        self.assertNotIn(source, app.playlist_cache)  # se descarta

    def test_fichero_borrado_invalida_la_cache(self) -> None:
        app = self.make_app()
        source = "/tmp/lista-que-no-existe.m3u"
        app.playlist_cache[source] = _playlist(_ch("Uno"))
        app.playlist_cache_meta[source] = {"at": time.monotonic(), "mtime": 1.0}
        self.assertIsNone(app.cached_playlist(source))

    def test_forget_playlist_descarta_entradas(self) -> None:
        app = self.make_app()
        app.remember_playlist("http://ejemplo.test/l.m3u", _playlist(_ch("Uno")))
        self.assertIsNotNone(app.cached_playlist("http://ejemplo.test/l.m3u"))
        app.forget_playlist("http://ejemplo.test/l.m3u")
        self.assertIsNone(app.cached_playlist("http://ejemplo.test/l.m3u"))
        self.assertEqual(app.playlist_cache, {})
        self.assertEqual(app.playlist_cache_meta, {})


# ---------------------------------------------------------------------------
# 2. Xtream: sin red en reabrir
# ---------------------------------------------------------------------------

class TestXtreamReabrir(_AppCase):
    def test_reabrir_xtream_no_pide_password_ni_autentica(self) -> None:
        app = self.make_app()
        entry = PlaylistEntry(
            name="Panel",
            source="xtream://http://panel.example",
            server_url="http://panel.example",
            username="user",
            password="secret",
        )
        app.remember_playlist(entry.source, _playlist(_ch("Canal A"), kind="xtream"))
        # Si se llegara a tocar credenciales/red, el test revienta.
        app.playlists.get_credentials = mock.Mock(
            side_effect=AssertionError("no debe pedir credenciales")
        )

        with mock.patch("thetvview.xtream_provider.authenticate") as auth:
            open_playlist(app, entry)
            auth.assert_not_called()

        app.playlists.get_credentials.assert_not_called()
        self.assertIsInstance(app.screen, ChannelsScreen)
        self.assertEqual(app.screen.playlist.kind, "xtream")
        self.assertEqual(len(app.screen.playlist.channels), 1)

    def test_cache_xtream_es_remota_y_respeta_ttl(self) -> None:
        """'xtream://' no tiene mtime: se refresca por TTL, no por fichero."""
        app = self.make_app()
        source = "xtream://http://panel.example"
        app.playlist_cache[source] = _playlist(_ch("Canal A"))
        app.playlist_cache_meta[source] = {"at": time.monotonic()}
        self.assertIsNotNone(app.cached_playlist(source))


# ---------------------------------------------------------------------------
# 3. Etiquetas de ChannelsScreen cacheadas
# ---------------------------------------------------------------------------

class TestCacheEtiquetas(unittest.TestCase):
    def _screen(self, favs=()) -> ChannelsScreen:
        app = _StubApp()
        app.favorites = _Fav(favs)
        pl = _playlist(
            _ch("Canal Uno HD", "Deportes"),
            _ch("Canal Dos", "Noticias"),
            _ch("Radio Cabal", "Radio"),
        )
        return ChannelsScreen(app, pl)

    def test_etiquetas_reutilizadas_entre_filtros(self) -> None:
        screen = self._screen()
        first = screen._labels
        self.assertIsNotNone(first)

        screen.query = "canal"
        screen._apply_filter(keep_selection=True)
        self.assertIs(screen._labels, first)  # no se reformatea

        screen.query = ""
        screen._apply_filter(keep_selection=True)
        self.assertIs(screen._labels, first)

    def test_etiquetas_se_reconstruyen_si_cambian_las_favoritas(self) -> None:
        screen = self._screen()
        antes = screen._labels
        screen.app.favorites.urls = {"http://x/Canal Uno HD"}

        screen._apply_filter(keep_selection=True)
        self.assertIsNot(screen._labels, antes)
        self.assertTrue(screen._labels[0].startswith("★ "))
        self.assertTrue(screen._title_text().endswith("★"))

    def test_busqueda_filtra_por_nombre_y_por_grupo(self) -> None:
        screen = self._screen()
        screen.query = "uno"
        screen._apply_filter()
        self.assertEqual(len(screen.visible_idx), 1)
        self.assertEqual(screen.channels[screen.visible_idx[0]].name, "Canal Uno HD")

        screen.query = "deportes"  # solo está en el group-title
        screen._apply_filter()
        self.assertEqual(len(screen.visible_idx), 1)
        self.assertEqual(screen.channels[screen.visible_idx[0]].group, "Deportes")

        screen.query = "zzz"
        screen._apply_filter()
        self.assertEqual(screen.visible_idx, [])
        self.assertEqual(screen.list.items, [])

    def test_busqueda_conserva_la_seleccion_si_sigue_visible(self) -> None:
        screen = self._screen()
        screen.list.selected = 1  # "Canal Dos"
        screen.query = "canal"
        screen._apply_filter(keep_selection=True)
        self.assertEqual(screen.current_channel().name, "Canal Dos")

        # Ya no coincide: la selección se recoloca sin romper.
        screen.query = "radio"
        screen._apply_filter(keep_selection=True)
        self.assertEqual(screen.current_channel().name, "Radio Cabal")


# ---------------------------------------------------------------------------
# 4. Borrar una playlist descarta su caché de sesión
# ---------------------------------------------------------------------------

class _FakeModal:
    """Modal de confirmación que responde 'Eliminar' sin tocar curses."""

    def __init__(self, *args, **kwargs) -> None:
        self.selected_button = 0

    def render(self, stdscr) -> None:
        pass

    def handle_key(self, key: int) -> str:
        return "eliminar"


class _FakeScr:
    def erase(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def getch(self) -> int:
        return ord("\n")


class TestForgetAlEliminar(_AppCase):
    def test_eliminar_playlist_borra_su_cache_de_sesion(self) -> None:
        """El flujo de confirmación no revienta y olvida la fuente."""
        app = self.make_app()
        source = "http://ejemplo.test/borrar.m3u"
        entry = app.playlists.add("Borrar", source)
        app.playlist_cache[entry.source] = _playlist(_ch("Uno"))
        app.playlist_cache_meta[entry.source] = {"at": time.monotonic()}
        self.assertIsNotNone(app.cached_playlist(entry.source))

        app.stdscr = _FakeScr()  # type: ignore[assignment]
        with (
            mock.patch("thetvview.ui.widgets.Modal", _FakeModal),
            mock.patch.object(app.header, "render", lambda *a, **k: None),
            mock.patch.object(app.screen, "render", lambda *a, **k: None),
            mock.patch.object(app.screen, "reload", lambda *a, **k: None),
            mock.patch.object(app, "_render_footer", lambda *a, **k: None),
        ):
            app.remove_playlist("Borrar")

        self.assertIsNone(app.playlists.get("Borrar"))
        self.assertIsNone(app.cached_playlist(entry.source))
        self.assertNotIn(entry.source, app.playlist_cache_meta)
        self.assertIn("eliminada", app.status.message)

    def test_cancelar_no_borra_nada(self) -> None:
        """Si el usuario cancela, la playlist y su caché siguen ahí."""
        app = self.make_app()
        source = "http://ejemplo.test/quedar.m3u"
        entry = app.playlists.add("Quedar", source)
        app.playlist_cache[entry.source] = _playlist(_ch("Uno"))
        app.playlist_cache_meta[entry.source] = {"at": time.monotonic()}

        class _Cancelar(_FakeModal):
            def handle_key(self, key: int) -> str:
                return "cancelar"

        app.stdscr = _FakeScr()  # type: ignore[assignment]
        with (
            mock.patch("thetvview.ui.widgets.Modal", _Cancelar),
            mock.patch.object(app.header, "render", lambda *a, **k: None),
            mock.patch.object(app.screen, "render", lambda *a, **k: None),
            mock.patch.object(app.screen, "reload", lambda *a, **k: None),
            mock.patch.object(app, "_render_footer", lambda *a, **k: None),
        ):
            app.remove_playlist("Quedar")

        self.assertIsNotNone(app.playlists.get("Quedar"))
        self.assertIsNotNone(app.cached_playlist(entry.source))


if __name__ == "__main__":
    unittest.main()
