"""Tests del enrutado común open_channel (punto: resolución desde favoritos/EPG).

Cubre App.variants_for (cache de sesión + escaneo del catálogo) y el
enrutado: con variantes -> ResolutionScreen; sin ellas -> PlayerScreen.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.ui import app as ui_app
from thetvview.ui.screens import PlayerScreen, ResolutionScreen


def ch(name: str, group: str | None = None, **attrs: str) -> Channel:
    return Channel(name=name, url=f"http://x/{name}", group=group, attrs=dict(attrs))


class AppCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        patches = [
            mock.patch.object(ui_app.config, "PLAYLISTS_JSON", base / "playlists.json"),
            mock.patch.object(ui_app.config, "FAVORITES_JSON", base / "favorites.json"),
            mock.patch.object(ui_app.config, "PREFS_JSON", base / "prefs.json"),
            mock.patch.object(ui_app.config, "RECENTS_JSON", base / "recents.json"),
            mock.patch.object(ui_app.config, "THEME_JSON", base / "theme.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = ui_app.App(None)  # noqa: ANN001 - no se renderiza

    def tearDown(self):
        self._tmp.cleanup()


class TestVariantsFor(AppCase):
    def test_desde_cache_de_sesion(self):
        pl = Playlist(name="P", channels=[ch("La 1 HD"), ch("La 1 SD")])
        self.app.playlist_cache["/p.m3u"] = pl
        got = self.app.variants_for(ch("La 1 HD"))
        self.assertEqual([c.name for c in got], ["La 1 SD", "La 1 HD"])

    def test_escanea_el_catalogo_si_no_esta_en_cache(self):
        entry = mock.Mock(source=" /catalogo.m3u ")
        self.app.playlists.load = mock.Mock(return_value=[entry])
        pl = Playlist(
            name="Cat", source="/catalogo.m3u",
            channels=[ch("Dep FHD"), ch("Dep SD"), ch("Otro")],
        )
        with mock.patch(
            "thetvview.ui.app.load_playlist_source", return_value=pl
        ) as loader:
            got = self.app.variants_for(ch("Dep FHD"))
        loader.assert_called_once_with("/catalogo.m3u")
        self.assertEqual([c.name for c in got], ["Dep SD", "Dep FHD"])
        # Y queda cacheado: segunda llamada no re-parsea.
        with mock.patch("thetvview.ui.app.load_playlist_source") as loader2:
            self.app.variants_for(ch("Dep FHD"))
        loader2.assert_not_called()

    def test_fuentes_rotas_se_saltan_sin_explotar(self):
        entry = mock.Mock(source="http://rota.example.com/x.m3u")
        self.app.playlists.load = mock.Mock(return_value=[entry])
        with mock.patch(
            "thetvview.ui.app.load_playlist_source", side_effect=OSError("red caída")
        ):
            self.assertEqual(self.app.variants_for(ch("Canal")), [])

    def test_sin_variantes_devuelve_vacio(self):
        pl = Playlist(name="P", channels=[ch("Unica")])
        self.app.playlist_cache["/p.m3u"] = pl
        self.assertEqual(self.app.variants_for(ch("Unica")), [])


class TestRoutingOpenChannel(AppCase):
    def test_con_etiqueta_y_variantes_va_a_resolution(self):
        """Aunque el título diga 'HD', si hay variantes con distinta resolución, muestra selector."""
        pl = Playlist(name="P", channels=[ch("La 1 HD"), ch("La 1 SD")])
        self.app.playlist_cache["/p.m3u"] = pl
        self.app.handle_action({"action": "open_channel", "channel": ch("La 1 HD")})
        self.assertIsInstance(self.app.screen, ResolutionScreen)

    def test_con_etiqueta_y_variantes_iguales_va_a_player(self):
        """Si todas las variantes tienen la misma resolución, no hay selector."""
        pl = Playlist(name="P", channels=[ch("TVN HD"), ch("TVN HD")])
        self.app.playlist_cache["/p.m3u"] = pl
        installed = {"mpv": "/usr/bin/mpv", "mplayer": None, "vlc": None}
        with mock.patch("thetvview.config.detect_players", return_value=installed):
            self.app.handle_action({"action": "open_channel", "channel": ch("TVN HD")})
        self.assertIsInstance(self.app.screen, PlayerScreen)

    def test_sin_etiqueta_con_variantes_empuja_resolution_screen(self):
        pl = Playlist(name="P", channels=[ch("Deportes"), ch("Deportes HD"), ch("Deportes SD")])
        self.app.playlist_cache["/p.m3u"] = pl
        self.app.handle_action({"action": "open_channel", "channel": ch("Deportes")})
        self.assertIsInstance(self.app.screen, ResolutionScreen)

    def test_res_en_attrs_y_variantes_distintas_va_a_resolution(self):
        """res='1080' en attrs, pero hay variante HD -> selector."""
        pl = Playlist(name="P", channels=[ch("Canal X", res="1080"), ch("Canal X HD")])
        self.app.playlist_cache["/p.m3u"] = pl
        self.app.handle_action({"action": "open_channel", "channel": ch("Canal X", res="1080")})
        self.assertIsInstance(self.app.screen, ResolutionScreen)

    def test_sin_variantes_empuja_player_screen(self):
        pl = Playlist(name="P", channels=[ch("Unica")])
        self.app.playlist_cache["/p.m3u"] = pl
        installed = {"mpv": "/usr/bin/mpv", "mplayer": None, "vlc": None}
        with mock.patch("thetvview.config.detect_players", return_value=installed):
            self.app.handle_action({"action": "open_channel", "channel": ch("Unica")})
        self.assertIsInstance(self.app.screen, PlayerScreen)

    def test_sin_variantes_ni_reproductor_avisa_y_no_empuja(self):
        pl = Playlist(name="P", channels=[ch("Unica")])
        self.app.playlist_cache["/p.m3u"] = pl
        with mock.patch("thetvview.config.detect_players", return_value={"mpv": None}):
            self.app.handle_action({"action": "open_channel", "channel": ch("Unica")})
        self.assertEqual(len(self.app.stack), 1)
        self.assertTrue(self.app.status.is_error)

    def test_desde_resolution_screen_va_directo_a_reproductor(self):
        canal = ch("La 1 HD")
        variants = [ch("La 1 SD"), canal]
        self.app.push(ResolutionScreen(self.app, canal, variants))
        installed = {"mpv": "/usr/bin/mpv", "mplayer": None, "vlc": None}
        with mock.patch("thetvview.config.detect_players", return_value=installed):
            self.app.handle_action({"action": "select_player", "channel": canal})
        self.assertIsInstance(self.app.screen, PlayerScreen)


if __name__ == "__main__":
    unittest.main()
