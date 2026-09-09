"""Tests de carga de EPG por URL desde la UI (punto 4).

Cubre el despacho de load_epg_source (path vs URL, force_refresh) y el
reciclaje del EPG ya cargado en App.ensure_epg.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.epg_parser import Epg
from thetvview.models import Channel
from thetvview.ui import app as ui_app


def ch() -> Channel:
    return Channel(name="Canal X", url="http://x/canalx")


class TestLoadEpgSource(unittest.TestCase):
    def test_path_local_usa_parse_file(self):
        with mock.patch("thetvview.ui.app.parse_file") as pf:
            ui_app.load_epg_source("/tmp/guia.xml")
        pf.assert_called_once_with("/tmp/guia.xml")

    def test_url_http_usa_load_url(self):
        with mock.patch("thetvview.ui.app.load_url") as lu:
            ui_app.load_epg_source("https://epg.example.com/guias/guia.xml.gz")
        lu.assert_called_once_with(
            "https://epg.example.com/guias/guia.xml.gz", force_refresh=False
        )

    def test_force_refresh_se_propaga(self):
        with mock.patch("thetvview.ui.app.load_url") as lu:
            ui_app.load_epg_source("http://epg.example.com/x.gz", force_refresh=True)
        self.assertTrue(lu.call_args.kwargs["force_refresh"])

    def test_errores_se_propagan_sin_capturar(self):
        with mock.patch("thetvview.ui.app.load_url", side_effect=OSError("red")):
            with self.assertRaises(OSError):
                ui_app.load_epg_source("https://epg.example.com/x.gz")

    def test_devuelve_el_epg_del_despacho(self):
        fake = Epg(programs={}, channels_by_id={})
        with mock.patch("thetvview.ui.app.parse_file", return_value=fake) as pf:
            self.assertIs(ui_app.load_epg_source("guia.xml"), fake)


class TestEnsureEpgRecycle(unittest.TestCase):
    """Con EPG ya cargado: sin prompt y con recarga forzada opcional."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        patches = [
            mock.patch.object(ui_app.config, "PLAYLISTS_JSON", base / "playlists.json"),
            mock.patch.object(ui_app.config, "FAVORITES_JSON", base / "favorites.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = ui_app.App(None)  # noqa: ANN001 - no se renderiza

        # EPG falso ya cargado con correspondencia para 'canalx'.
        self.fake_epg = mock.Mock(spec=Epg)
        self.fake_epg.programs = {"canalx": []}
        self.fake_epg.channels_by_id = {"canalx": "Canal X"}
        self.fake_epg.programmes_for.return_value = []
        self.app.epg = self.fake_epg

    def tearDown(self):
        self._tmp.cleanup()

    def test_epg_ya_cargado_no_pide_nada(self):
        progs = self.app.ensure_epg(ch())
        self.assertEqual(progs, [])
        self.assertIs(self.app.epg, self.fake_epg)

    def test_url_hint_se_usa_como_defecto_si_no_hay_fuente_previa(self):
        self.app.epg = None
        self.app.epg_source = None
        # El modal precarga el default sugerido en el campo.
        with mock.patch.object(
            self.app, "_prompt_form", return_value=None
        ) as prompt:
            self.app.ensure_epg(ch(), url_hint="https://epg.example.com/g.xml.gz")
        self.assertEqual(
            prompt.call_args.kwargs.get("initial", {}).get("path"),
            "https://epg.example.com/g.xml.gz",
        )

    def test_force_refresh_recarga_con_la_misma_fuente(self):
        self.app.epg_source = "https://epg.example.com/guia.xml"
        recargado = mock.Mock(spec=Epg)
        recargado.programs = {"canalx": []}
        recargado.channels_by_id = {"canalx": "Canal X"}
        recargado.programmes_for.return_value = []
        with mock.patch(
            "thetvview.ui.app.load_epg_source", return_value=recargado
        ) as loader:
            self.app.ensure_epg(ch(), force_refresh=True)
        loader.assert_called_once_with("https://epg.example.com/guia.xml", force_refresh=True)
        self.assertIs(self.app.epg, recargado)

    def test_force_refresh_con_error_conserva_epg_anterior(self):
        self.app.epg_source = "/ruta/local.xml"
        with mock.patch(
            "thetvview.ui.app.load_epg_source", side_effect=OSError("sin red")
        ):
            progs = self.app.ensure_epg(ch(), force_refresh=True)
        self.assertIs(self.app.epg, self.fake_epg)
        self.assertTrue(self.app.status.is_error)

    def test_error_de_matching_informa_amigable(self):
        self.app.epg = mock.Mock(spec=Epg, programs={}, channels_by_id={})
        progs = self.app.ensure_epg(ch())
        self.assertEqual(progs, [])
        self.assertTrue(self.app.status.is_error)


if __name__ == "__main__":
    unittest.main()
