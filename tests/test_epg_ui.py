"""Tests de carga de EPG por URL desde la UI (punto 4).

Cubre el despacho de load_epg_source (path vs URL, force_refresh) y el
reciclaje del EPG ya cargado en App.ensure_epg.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from thetvview.epg_parser import Epg
from thetvview.models import Channel, Program
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
            "https://epg.example.com/guias/guia.xml.gz",
            force_refresh=False,
            allow_private=False,
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

        # Guía previa ya cargada, con correspondencia para 'canalx'. Es un
        # `Epg` de verdad y no un doble: desde la Fase 4 `ensure_epg` **compara
        # la nueva contra la activa** (FR-EPG-009) y un doble no sabe cuántos
        # programas tiene.
        self.fake_epg = Epg(
            channels_by_id={"canalx": "Canal X"},
            programs={"canalx": []},
        )
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
        # Un `Epg` de verdad, no un doble: desde la Fase 4 la app **valida** la
        # guía antes de sustituir a la activa (FR-EPG-009), y un doble no puede
        # contestar eso con sentido.
        recargado = Epg(
            channels_by_id={"canalx": "Canal X"},
            programs={
                "canalx": [
                    Program(
                        channel_id="canalx",
                        title="Algo",
                        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        stop=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
                    )
                ]
            },
        )
        with mock.patch(
            "thetvview.ui.app.load_epg_source", return_value=recargado
        ) as loader:
            self.app.ensure_epg(ch(), force_refresh=True)
        loader.assert_called_once_with(
            "https://epg.example.com/guia.xml", force_refresh=True, allow_private=False
        )
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


class TestAvisoDeAmbiguedad(unittest.TestCase):
    """FR-EPG-003: si un nombre casa con varios canales, se avisa una vez.

    No se fusiona nada —el canal funciona y se resuelve el primero—, pero el
    usuario merece saber que el horario que ve puede ser el de un homónimo.
    El aviso es un modal (regla de `AGENTS.md`) y se emite **una vez por
    clave**: repetirlo en cada apertura deja de leerse.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        for target in ("PLAYLISTS_JSON", "FAVORITES_JSON"):
            p = mock.patch.object(ui_app.config, target, base / target.lower())
            p.start()
            self.addCleanup(p.stop)
        self.app = ui_app.App(None)  # noqa: ANN001 - no se renderiza
        cuando = datetime(2026, 1, 1, tzinfo=timezone.utc)
        luego = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)

        def prog(cid):
            return [Program(channel_id=cid, title="Algo", start=cuando, stop=luego)]

        self.app.epg = Epg(
            channels_by_id={
                "canal.sd": "Canal X",
                "canal.hd": "Canal X",
                "otro.a": "Alfa",
                "otro.b": "Alfa",
            },
            programs={cid: prog(cid) for cid in ("canal.sd", "canal.hd", "otro.a", "otro.b")},
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_un_nombre_ambiguo_avisa_una_sola_vez(self):
        with mock.patch.object(self.app, "_show_notice") as notice:
            progs = self.app.ensure_epg(ch())
            # El canal se sirve igual: la ambigüedad informa, no bloquea.
            self.assertEqual(len(progs), 1)
            self.assertEqual(notice.call_count, 1)
            titulo, mensaje = notice.call_args.args
            self.assertIn("llaman igual", titulo)
            self.assertIn("Canal X", mensaje)
            self.assertIn("tvg-id", mensaje)
            self.app.ensure_epg(ch())
            self.assertEqual(notice.call_count, 1)  # una vez por clave

    def test_un_canal_sin_ambiguedad_no_abre_modal(self):
        self.app.epg = Epg(
            channels_by_id={"canalx": "Canal X"},
            programs={"canalx": []},
        )
        with mock.patch.object(self.app, "_show_notice") as notice:
            self.app.ensure_epg(ch())
        notice.assert_not_called()

    def test_dos_nombres_ambiguos_avisan_cada_uno(self):
        with mock.patch.object(self.app, "_show_notice") as notice:
            self.app.ensure_epg(ch())
            self.app.ensure_epg(Channel(name="Alfa", url="http/x/alfa"))
            self.assertEqual(notice.call_count, 2)


if __name__ == "__main__":
    unittest.main()
