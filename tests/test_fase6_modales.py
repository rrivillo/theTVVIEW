"""Tests de Fase 6: errores, avisos e interacciones van por modal.

No negociable #1. Fuera de curses (`App(None)`, tests, hilos) todo se
degrada a la barra de estado para no colgar la TUI; con curses real el
mismo código abre un `Modal`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.ui import app as ui_app
from thetvview.ui.app import App, _is_plain_http, _is_private_source
from thetvview.ui.widgets import StatusBar


class _AppCase(unittest.TestCase):
    """App(None) aislada de data/ real (el de la máquina del desarrollador)."""

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
        self.app = App(None)  # noqa: ANN001 - sin terminal


class TestStatusBarEscalaAModal(unittest.TestCase):
    def test_error_llama_al_callback(self) -> None:
        bar = StatusBar()
        seen: list[tuple[str, str]] = []
        bar.on_error = lambda title, message: seen.append((title, message))
        bar.show("algo falló", error=True)
        self.assertEqual(seen, [("Error", "algo falló")])
        self.assertEqual(bar.message, "algo falló")
        self.assertTrue(bar.is_error)

    def test_no_error_no_abre_modal(self) -> None:
        bar = StatusBar()
        seen: list = []
        bar.on_error = lambda *a: seen.append(a)
        bar.show("todo bien")
        self.assertEqual(seen, [])

    def test_sin_callback_no_falla(self) -> None:
        bar = StatusBar()
        bar.show("algo falló", error=True)
        self.assertTrue(bar.is_error)


class TestDegradaSinCurses(_AppCase):
    def test_notify_error_queda_en_la_barra(self) -> None:
        self.app.notify_error("No se pudo cargar la lista.")
        self.assertEqual(self.app.status.message, "No se pudo cargar la lista.")
        self.assertTrue(self.app.status.is_error)
        self.assertFalse(self.app._in_modal)

    def test_notify_warning_queda_en_la_barra(self) -> None:
        self.app.notify_warning("Esto es un aviso.")
        self.assertEqual(self.app.status.message, "Esto es un aviso.")
        self.assertFalse(self.app.status.is_error)

    def test_confirm_sin_ui_devuelve_false(self) -> None:
        self.assertFalse(self.app._confirm("Título", "¿Seguro?"))

    def test_modal_no_se_anida(self) -> None:
        self.app._in_modal = True
        self.app._show_notice("Error", "interno")
        self.assertIsNone(self.app._run_modal(mock.Mock()))

    def test_en_hilo_no_toca_curses(self) -> None:
        import threading

        box: list = []

        def worker() -> None:
            app = App(None)  # noqa: ANN001
            app.status.on_error = app._show_notice
            app.notify_error("desde el hilo")
            box.append(app.status.message)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(box, ["desde el hilo"])


class TestDeteccionDeDestino(unittest.TestCase):
    def test_red_privada_sin_dns(self) -> None:
        for url in (
            "http://127.0.0.1/x.m3u",
            "http://localhost/x.m3u",
            "https://192.168.1.10:8080/lista.m3u",
            "http://10.0.0.1/x.m3u",
            "http://172.16.5.5/x.m3u",
            "http://[::1]/x.m3u",
            "https://169.254.169.254/latest/meta-data/",
            "https://metadata.google.internal/computeMetadata/",
        ):
            with self.subTest(url=url):
                self.assertTrue(_is_private_source(url))

    def test_destinos_publicos(self) -> None:
        for url in (
            "https://example.com/lista.m3u",
            "http://cdn.example.org/live.ts",
            "http://8.8.8.8/x.m3u",
        ):
            with self.subTest(url=url):
                self.assertFalse(_is_private_source(url))

    def test_rutas_locales_no_son_destino_de_red(self) -> None:
        self.assertFalse(_is_private_source("/home/u/lista.m3u"))
        self.assertFalse(_is_private_source("C:\\listas\\a.m3u"))
        self.assertFalse(_is_private_source(""))

    def test_url_malformada_no_pide_permiso(self) -> None:
        self.assertFalse(_is_private_source("http://"))

    def test_plain_http(self) -> None:
        self.assertTrue(_is_plain_http("http://example.com/x.m3u"))
        self.assertFalse(_is_plain_http("https://example.com/x.m3u"))
        self.assertFalse(_is_plain_http("/ruta/local.m3u"))


class TestGuardSourcePolicy(_AppCase):
    def test_red_privada_se_cancela_sin_confirmacion(self) -> None:
        ok = self.app._guard_source_policy("http://192.168.0.5/lista.m3u")
        self.assertFalse(ok)
        self.assertTrue(self.app.status.is_error)
        self.assertIn("privada", self.app.status.message)

    def test_http_publico_solo_avisa(self) -> None:
        self.assertTrue(self.app._guard_source_policy("http://example.com/a.m3u"))
        self.assertFalse(self.app.status.is_error)
        self.assertIn("sin cifrar", self.app.status.message)

    def test_https_publico_no_molesta(self) -> None:
        self.assertTrue(self.app._guard_source_policy("https://example.com/a.m3u"))
        self.assertEqual(self.app.status.message, "")

    def test_ruta_local_no_molesta(self) -> None:
        self.assertTrue(self.app._guard_source_policy("/home/u/a.m3u"))
        self.assertEqual(self.app.status.message, "")


class TestAddM3uRespetaLaPolitica(_AppCase):
    def test_fuente_privada_no_se_guarda(self) -> None:
        with mock.patch.object(
            self.app, "_prompt_form",
            return_value={"name": "NAS", "source": "http://127.0.0.1/x.m3u"},
        ), mock.patch.object(self.app.playlists, "add") as add:
            self.app._add_m3u_playlist()
        add.assert_not_called()
        self.assertTrue(self.app.status.is_error)

    def test_fuente_publica_se_guarda_con_excepcion_apagada(self) -> None:
        with mock.patch.object(
            self.app, "_prompt_form",
            return_value={"name": "Pública", "source": "https://example.com/x.m3u"},
        ), mock.patch.object(self.app.playlists, "add") as add, \
                mock.patch.object(self.app.screen, "reload"):
            self.app._add_m3u_playlist()
        add.assert_called_once_with("Pública", "https://example.com/x.m3u",
                                    allow_private_network=False)


class TestAddXtreamRespetaLaPolitica(_AppCase):
    def test_servidor_privado_no_llega_a_autenticar(self) -> None:
        with mock.patch.object(
            self.app, "_prompt_form",
            return_value={
                "name": "NAS",
                "server": "http://192.168.1.2:8080",
                "user": "u",
                "password": "p",
            },
        ), mock.patch(
            "thetvview.xtream_provider.authenticate"
        ) as auth:
            self.app._add_xtream_source()
        auth.assert_not_called()
        self.assertTrue(self.app.status.is_error)

    def test_servidor_publico_autentica_sin_excepcion(self) -> None:
        from thetvview.xtream_config import XtreamConfig

        with mock.patch.object(
            self.app, "_prompt_form",
            return_value={
                "name": "Panel",
                "server": "https://panel.example.com:8080",
                "user": "u",
                "password": "p",
            },
        ), mock.patch(
            "thetvview.xtream_provider.authenticate",
            return_value={"user_info": {"status": "Active"}},
        ) as auth, mock.patch.object(
            self.app.playlists, "add_xtream"
        ) as add, mock.patch.object(self.app.screen, "reload"):
            self.app._add_xtream_source()
        auth.assert_called_once()
        cfg: XtreamConfig = auth.call_args.args[0]
        self.assertFalse(cfg.allow_private_network)
        add.assert_called_once_with(
            "Panel", "https://panel.example.com:8080", "u", "p",
            allow_private_network=False,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
