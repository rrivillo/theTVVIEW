"""Tests de `thetvview.stream_ref` (SDD §37, gap B8).

Contrato: por el dominio sólo circulan URLs opacas `xtream://…` sin
usuario ni contraseña; las credenciales se recuperan del catálogo en el
instante de lanzar el reproductor y nunca se persisten en `data/`.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.favorites import FavoritesManager
from thetvview.models import Channel
from thetvview.recents import RecentsManager
from thetvview.stream_ref import (
    PREFIX,
    MissingCredentialsError,
    StreamRef,
    resolve_channel_url,
)
from thetvview.ui import screens as ui_screens
from thetvview.ui.screens import play_channel


class _Catalogo:
    """Duck-typing de PlaylistManager.get_credentials()."""

    def __init__(self, creds: dict | None = None) -> None:
        self.creds = creds or {}

    def get_credentials(self, name: str):
        return self.creds.get(name)


class _Status:
    def __init__(self) -> None:
        self.last: str = ""
        self.was_error: bool = False

    def show(self, message: str, error: bool = False) -> None:
        self.last = message
        self.was_error = error


class _PrefsStub:
    def set_last_player(self, name: str | None) -> None:
        pass


class _StubApp:
    def __init__(self, playlists=None) -> None:
        self.status = _Status()
        self.prefs = _PrefsStub()
        self.stack: list = []
        self.epg = None
        self.playlists = playlists

    def push(self, screen) -> None:
        self.stack.append(screen)


class TestOpaca(unittest.TestCase):
    def test_roundtrip(self) -> None:
        ref = StreamRef("Mi Panel", "live", "101", "ts")
        url = ref.to_opaque()
        self.assertEqual(url, "xtream://Mi%20Panel/live/101.ts")
        back = StreamRef.parse(url)
        self.assertEqual(back, ref)

    def test_nombre_con_slash_no_rompe_el_parse(self) -> None:
        ref = StreamRef("liga/2026", "movie", "201", "mp4")
        url = ref.to_opaque()
        self.assertNotIn("liga/2026", url)
        self.assertEqual(StreamRef.parse(url), ref)

    def test_url_sin_esquema_real(self) -> None:
        ref = StreamRef("Panel", "live", "101")
        self.assertTrue(ref.to_opaque().startswith(PREFIX))
        self.assertIsNone(StreamRef.parse("http://srv/live/u/p/101.ts"))
        self.assertIsNone(StreamRef.parse("xtream://Panel/live"))
        self.assertIsNone(StreamRef.parse("xtream://Panel/live/.ts"))
        self.assertIsNone(StreamRef.parse(None))  # type: ignore[arg-type]

    def test_url_opaca_es_rechazada_por_el_reproductor(self) -> None:
        from thetvview.security.url_policy import (
            PURPOSE_STREAM,
            InvalidUrlError,
            validate_url,
        )

        with self.assertRaises(InvalidUrlError):
            validate_url(StreamRef("Panel", "live", "101").to_opaque(), PURPOSE_STREAM)


class TestResolve(unittest.TestCase):
    def test_resuelve_con_credenciales_del_catalogo(self) -> None:
        mgr = _Catalogo({"Panel": ("http://srv:8080", "u", "p")})
        ref = StreamRef("Panel", "live", "101")
        self.assertEqual(ref.resolve(mgr), "http://srv:8080/live/u/p/101.ts")

    def test_sin_credenciales_lanza_error_amigable(self) -> None:
        ref = StreamRef("Panel", "live", "101")
        with self.assertRaises(MissingCredentialsError) as cm:
            ref.resolve(_Catalogo())
        self.assertIn("Panel", str(cm.exception))
        self.assertNotIn("p ", str(cm.exception))

    def test_resolve_channel_url_pasa_las_que_no_son_opacas(self) -> None:
        mgr = _Catalogo()
        self.assertEqual(resolve_channel_url("http://x/1.ts", mgr), "http://x/1.ts")
        self.assertEqual(resolve_channel_url("", mgr), "")

    def test_resolve_channel_url_opaca_corrupta_se_niega(self) -> None:
        with self.assertRaises(MissingCredentialsError):
            resolve_channel_url(PREFIX + "rota", _Catalogo({"x": (1, 2, 3)}))

    def test_opaca_sin_catalogo_tambien_se_niega(self) -> None:
        with self.assertRaises(MissingCredentialsError):
            resolve_channel_url(StreamRef("Panel", "live", "101").to_opaque(), None)


class TestPlayChannel(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = _Catalogo({"Panel": ("http://srv:8080", "usuario", "secreto")})
        self.opaca = StreamRef("Panel", "live", "101").to_opaque()
        self.channel = Channel(name="ESPN", url=self.opaca)

    def _launch(self, app):
        fake = mock.Mock(pid=7)
        fake.poll.return_value = None
        with mock.patch("thetvview.player.launch", return_value=fake) as launch, \
                mock.patch.object(ui_screens, "ChannelHealthMonitor", return_value=mock.Mock()):
            play_channel(app, self.channel, player_name="mpv")
        return launch

    def test_lanza_con_credenciales_sin_mutar_el_canal(self) -> None:
        app = _StubApp(self.mgr)
        launch = self._launch(app)
        launch.assert_called_once()
        lanzado = launch.call_args.args[0]
        self.assertEqual(lanzado.url, "http://srv:8080/live/usuario/secreto/101.ts")
        self.assertEqual(self.channel.url, self.opaca)
        self.assertEqual(app.stack[0].channel.url, lanzado.url)
        self.assertNotIn("secreto", app.status.last)

    def test_sin_catalogo_no_lanza_y_avisa(self) -> None:
        app = _StubApp(None)
        launch = self._launch(app)
        launch.assert_not_called()
        self.assertTrue(app.status.was_error)
        self.assertIn("Panel", app.status.last)
        self.assertEqual(app.stack, [])

    def test_urls_normales_no_se_tocan(self) -> None:
        canal = Channel(name="X", url="http://x/1.ts")
        app = _StubApp(self.mgr)
        fake = mock.Mock(pid=1)
        fake.poll.return_value = None
        with mock.patch("thetvview.player.launch", return_value=fake) as launch, \
                mock.patch.object(ui_screens, "ChannelHealthMonitor", return_value=mock.Mock()):
            play_channel(app, canal, player_name="mpv")
        launch.assert_called_once_with(canal, player_name="mpv")


class TestMigracionRecents(unittest.TestCase):
    _AT = "2024-01-01T00:00:00+00:00"

    def test_login_embebido_se_redacta_y_reescribe(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "recents.json"
            path.write_text(
                json.dumps([
                    {"name": "A", "url": "http://srv:8080/live/usuario/secreto/101.ts",
                     "group": None, "player": "mpv", "at_utc": self._AT},
                    {"name": "B", "url": "http://h/x.m3u8?token=tok123",
                     "group": None, "player": "mpv", "at_utc": self._AT},
                ]),
                encoding="utf-8",
            )
            items = RecentsManager(path).load()
            self.assertNotIn("secreto", items[0].url)
            self.assertNotIn("usuario", items[0].url)
            self.assertEqual(items[1].url, "http://h/x.m3u8?token=tok123")
            en_disco = path.read_text(encoding="utf-8")
            self.assertNotIn("secreto", en_disco)
            self.assertIn("tok123", en_disco)

    def test_url_opaca_no_se_toca(self) -> None:
        opaca = StreamRef("Panel", "live", "101").to_opaque()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "recents.json"
            path.write_text(
                json.dumps([{"name": "A", "url": opaca, "group": None,
                             "player": "mpv", "at_utc": self._AT}]),
                encoding="utf-8",
            )
            items = RecentsManager(path).load()
            self.assertEqual(items[0].url, opaca)

    def test_push_no_escribe_credenciales(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "recents.json"
            mgr = RecentsManager(path)
            mgr.push(
                Channel(name="A", url="http://srv/live/user/pass/1.ts"),
                "mpv",
            )
            self.assertNotIn("pass", path.read_text(encoding="utf-8"))


class TestMigracionFavoritos(unittest.TestCase):
    def test_login_embebido_se_redacta_al_leer(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "favorites.json"
            path.write_text(
                json.dumps([
                    {"name": "A", "url": "http://srv/live/user/pass/1.ts"},
                    {"name": "B", "url": "http://h/x.m3u8?token=tok123"},
                ]),
                encoding="utf-8",
            )
            favs = FavoritesManager(path)
            items = favs.load()
            self.assertNotIn("pass", items[0].url)
            self.assertEqual(items[1].url, "http://h/x.m3u8?token=tok123")
            self.assertNotIn("pass", path.read_text(encoding="utf-8"))

    def test_url_opaca_sirve_para_identidad(self) -> None:
        opaca = StreamRef("Panel", "live", "101").to_opaque()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "favorites.json"
            favs = FavoritesManager(path)
            favs.toggle(Channel(name="A", url=opaca))
            self.assertTrue(favs.is_favorite(Channel(name="A", url=opaca)))
            self.assertNotIn("pass", path.read_text(encoding="utf-8"))


class TestNormalizeGeneraOpaca(unittest.TestCase):
    def test_no_hay_ruta_de_codigos_que_embeba_password(self) -> None:
        from thetvview.xtream_models import normalize_live_stream
        from thetvview.xtream_provider import XtreamStream

        stream = XtreamStream(num=1, name="ESPN", stream_id=101,
                              stream_type="live", category_id="1")
        ch = normalize_live_stream(
            stream, "http://srv:8080", "usuario", "secreto",
            source_name="Panel",
        )
        self.assertEqual(ch.url, "xtream://Panel/live/101.ts")
        self.assertNotIn("secreto", ch.url)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
