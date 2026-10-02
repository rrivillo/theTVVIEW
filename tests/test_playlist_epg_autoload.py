"""Tests del EPG incluido en la playlist (cabecera #EXTM3U).

Cubre:
- aliases de atributo (x-tvg-url, url-tvg, tvg-url...), varias fuentes y
  paths relativos resueltos contra el origen de la lista;
- epg_parser.resolve_source/split_sources multiplataforma (file://, rutas
  Windows/UNC en cualquier SO, urljoin para listas remotas);
- App.auto_load_playlist_epg: carga en segundo plano al abrir la lista,
  espera desde ensure_epg y fallos sin romper la TUI.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.epg_parser import resolve_source, split_sources
from thetvview.m3u_parser import parse_file, parse_text
from thetvview.models import Channel, Playlist
from thetvview.playlist_manager import PlaylistEntry
from thetvview.ui import app as ui_app
from thetvview.ui.screens import open_playlist

FIXTURES = Path(__file__).parent / "fixtures"


def ch(name: str = "Canal Uno HD", tvg_id: str | None = "canal1.es") -> Channel:
    return Channel(name=name, url=f"http://stream/{name}", tvg_id=tvg_id)


# ---------------------------------------------------------------------------
# 1. Cabecera #EXTM3U
# ---------------------------------------------------------------------------


class TestHeaderEpg(unittest.TestCase):
    def _pl(self, header: str, source: str | None = None) -> Playlist:
        return parse_text(f"#EXTM3U {header}\n#EXTINF:-1,A\nhttp://a\n", source=source)

    def test_alias_url_tvg_sin_comillas(self) -> None:
        pl = self._pl("url-tvg=http://epg.example.com/guia.xml")
        self.assertEqual(pl.epg_url, "http://epg.example.com/guia.xml")
        self.assertEqual(pl.epg_urls, ["http://epg.example.com/guia.xml"])

    def test_alias_tvg_url_con_comillas_simples(self) -> None:
        pl = self._pl("tvg-url='https://epg.example.com/g.xml.gz'")
        self.assertEqual(pl.epg_urls, ["https://epg.example.com/g.xml.gz"])

    def test_alias_x_tvg_url_sigue_funcionando(self) -> None:
        pl = self._pl('x-tvg-url="https://epg.example.com/guide.xml.gz"')
        self.assertEqual(pl.epg_url, "https://epg.example.com/guide.xml.gz")
        self.assertEqual(len(pl.epg_urls), 1)

    def test_varias_fuentes_en_un_atributo(self) -> None:
        pl = self._pl('x-tvg-url="https://a/g1.xml https://a/g2.xml|https://a/g3.xml"')
        self.assertEqual(
            pl.epg_urls,
            ["https://a/g1.xml", "https://a/g2.xml", "https://a/g3.xml"],
        )
        self.assertEqual(pl.epg_url, "https://a/g1.xml")

    def test_lista_remota_resuelve_relativas_con_urljoin(self) -> None:
        pl = self._pl('url-tvg="guia.xml"', source="http://panel.test:8080/listas/l.m3u")
        self.assertEqual(pl.epg_urls, ["http://panel.test:8080/listas/guia.xml"])

    def test_path_relativo_resuelve_contra_el_fichero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "listas" / "iptv.m3u"
            pl = self._pl('x-tvg-url="guia.xml"', source=str(base))
            expected = os.path.normpath(str(Path(tmp) / "listas" / "guia.xml"))
            self.assertEqual(pl.epg_urls, [expected])

    def test_path_relativo_con_barra_invertida_también_resuelve(self) -> None:
        # Lista creada en Windows, abierta en un SO con otro separador.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "iptv.m3u"
            pl = self._pl('url-tvg="epg\\guia.xml"', source=str(base))
            expected = os.path.normpath(str(Path(tmp) / "epg" / "guia.xml"))
            self.assertEqual(pl.epg_urls, [expected])

    def test_coma_dentro_de_query_no_trocea_la_url(self) -> None:
        pl = self._pl('x-tvg-url="https://a/g.xml?ids=1,2"')
        self.assertEqual(pl.epg_urls, ["https://a/g.xml?ids=1,2"])

    def test_url_con_coma_separa_varias_fuentes(self) -> None:
        pl = self._pl('x-tvg-url="one.xml,two.xml"  ', source="/tmp/listas/l.m3u")
        self.assertEqual(
            pl.epg_urls,
            [
                os.path.normpath("/tmp/listas/one.xml"),
                os.path.normpath("/tmp/listas/two.xml"),
            ],
        )

    def test_duplicados_se_descartan(self) -> None:
        pl = self._pl('x-tvg-url="https://a/g.xml" url-tvg="https://a/g.xml"')
        self.assertEqual(pl.epg_urls, ["https://a/g.xml"])

    def test_sin_epg_en_cabecera(self) -> None:
        pl = parse_text("#EXTM3U\n#EXTINF:-1,A\nhttp://a\n")
        self.assertIsNone(pl.epg_url)
        self.assertEqual(pl.epg_urls, [])

    def test_atributos_de_canal_no_se_confunden_con_epg(self) -> None:
        pl = parse_text(
            '#EXTM3U tvg-id="canal1.es"\n#EXTINF:-1,A\nhttp://a\n',
            source="/tmp/l.m3u",
        )
        self.assertIsNone(pl.epg_url)
        self.assertEqual(pl.epg_urls, [])

    def test_parse_file_de_una_playlist_con_epg_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lista.m3u"
            path.write_text(
                '#EXTM3U x-tvg-url="guia.xml.gz"\n#EXTINF:-1 tvg-id="canal1.es",A\n'
                "http://a\n",
                encoding="utf-8",
            )
            pl = parse_file(path)
            self.assertEqual(
                pl.epg_urls, [os.path.normpath(str(Path(tmp) / "guia.xml.gz"))]
            )


# ---------------------------------------------------------------------------
# 2. resolve_source / split_sources (multiplataforma)
# ---------------------------------------------------------------------------


class TestResolveSource(unittest.TestCase):
    def test_http_se_conserva(self) -> None:
        self.assertEqual(
            resolve_source("https://epg.example.com/g.xml.gz"),
            "https://epg.example.com/g.xml.gz",
        )

    def test_vacio_devuelve_none(self) -> None:
        self.assertIsNone(resolve_source(""))
        self.assertIsNone(resolve_source(None))
        self.assertIsNone(resolve_source('  ""  '))

    def test_file_url_de_unidad_windows_en_cualquier_so(self) -> None:
        # file:///C:/... parado por url2pathname en POSIX deja /C:/...
        got = resolve_source("file:///C:/Users/alicia/epg.xml.gz")
        self.assertEqual(got, os.path.normpath("C:/Users/alicia/epg.xml.gz"))

    def test_file_url_posix(self) -> None:
        got = resolve_source("file:///opt/epg/guia.xml") or ""
        self.assertTrue(got.replace("\\", "/").endswith("/opt/epg/guia.xml"))

    def test_file_url_con_host_es_unc(self) -> None:
        got = resolve_source("file://nas/share/guia.xml") or ""
        self.assertEqual(got.replace("\\", "/"), "//nas/share/guia.xml")

    def test_file_url_localhost_es_local(self) -> None:
        got = resolve_source("file://localhost/opt/g.xml") or ""
        self.assertEqual(got.replace("\\", "/"), "/opt/g.xml")

    def test_absoluta_posix(self) -> None:
        self.assertEqual(resolve_source("/opt/epg/guia.xml"), "/opt/epg/guia.xml")

    def test_absoluta_windows_se_conserva_aun_en_posix(self) -> None:
        self.assertEqual(resolve_source("C:\\epg\\guia.xml"), "C:\\epg\\guia.xml")

    def test_unc_se_conserva(self) -> None:
        self.assertEqual(resolve_source("\\\\nas\\epg\\guia.xml"), "\\\\nas\\epg\\guia.xml")

    def test_relativa_sin_base_se_conserva(self) -> None:
        self.assertEqual(resolve_source("guia.xml"), "guia.xml")

    def test_relativa_contra_fichero_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = str(Path(tmp) / "listas" / "iptv.m3u")
            got = resolve_source("../epg/guia.xml", base=base)
            self.assertEqual(got, os.path.normpath(str(Path(tmp) / "epg" / "guia.xml")))

    def test_relativa_contra_url_remota(self) -> None:
        self.assertEqual(
            resolve_source("guia.xml", base="http://panel.test:8080/get.php?x=1"),
            "http://panel.test:8080/guia.xml",
        )

    def test_comillas_y_espacios_se_limpian(self) -> None:
        self.assertEqual(resolve_source('  "https://a/g.xml"  '), "https://a/g.xml")


class TestSplitSources(unittest.TestCase):
    def test_espacios_pipe_y_coma(self) -> None:
        self.assertEqual(
            split_sources("a.xml b.xml|c.xml;d.xml"),
            ["a.xml", "b.xml", "c.xml", "d.xml"],
        )

    def test_coma_final_no_deja_basura(self) -> None:
        self.assertEqual(split_sources("one.xml, two.xml"), ["one.xml", "two.xml"])

    def test_url_con_query_con_comas_no_se_trocea(self) -> None:
        self.assertEqual(split_sources("https://a/g.xml?ids=1,2"), ["https://a/g.xml?ids=1,2"])

    def test_vacio(self) -> None:
        self.assertEqual(split_sources("   "), [])


# ---------------------------------------------------------------------------
# 3. Carga automática al abrir la lista
# ---------------------------------------------------------------------------


class _AppCase(unittest.TestCase):
    """App real con todos los JSON de datos dentro de un tmpdir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        for attr, name in (
            ("PLAYLISTS_JSON", "playlists.json"),
            ("FAVORITES_JSON", "favorites.json"),
            ("PREFS_JSON", "prefs.json"),
            ("RECENTS_JSON", "recents.json"),
            ("THEME_JSON", "theme.json"),
            ("EPG_CACHE_DIR", "epg_cache"),
            ("PLAYLIST_CACHE_DIR", "playlist_cache"),
        ):
            patcher = mock.patch.object(ui_app.config, attr, self.base / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def make_app(self) -> ui_app.App:
        return ui_app.App(None)  # noqa: ANN001 - no se renderiza

    def epg_fixture(self, name: str = "guia.xml") -> Path:
        target = self.base / name
        target.write_bytes((FIXTURES / "sample.xmltv").read_bytes())
        return target

    def playlist_with_epg(self, epg_ref: str, name: str = "ConEPG") -> Playlist:
        return Playlist(
            name=name,
            source=str(self.base / f"{name}.m3u"),
            channels=[ch()],
            epg_url=epg_ref,
            epg_urls=[epg_ref],
        )


class TestAutoLoadEpg(_AppCase):
    def test_carga_el_epg_en_segundo_plano(self) -> None:
        app = self.make_app()
        epg_path = self.epg_fixture()
        self.assertTrue(app.auto_load_playlist_epg(self.playlist_with_epg(str(epg_path))))
        self.assertTrue(app.wait_for_epg_load(timeout=10))
        self.assertIsNotNone(app.epg)
        self.assertEqual(app.epg_source, str(epg_path))
        self.assertIn("EPG cargado", app.status.message)
        self.assertEqual(app.epg.channel_name("canal1.es"), "Canal Uno")

    def test_resuelve_la_ruta_relativa_de_la_lista(self) -> None:
        app = self.make_app()
        self.epg_fixture()
        pl = self.playlist_with_epg("guia.xml")
        self.assertTrue(app.auto_load_playlist_epg(pl))
        self.assertTrue(app.wait_for_epg_load(timeout=10))
        self.assertEqual(app.epg_source, str(self.base / "guia.xml"))

    def test_sin_epg_en_la_lista_no_hace_nada(self) -> None:
        app = self.make_app()
        pl = Playlist(name="SinEPG", channels=[ch()], source=str(self.base / "x.m3u"))
        self.assertFalse(app.auto_load_playlist_epg(pl))
        self.assertIsNone(app.epg)
        self.assertIsNone(app._epg_load_thread)

    def test_playlist_none_no_explota(self) -> None:
        app = self.make_app()
        self.assertFalse(app.auto_load_playlist_epg(None))

    def test_no_relanza_si_ya_hay_epg(self) -> None:
        app = self.make_app()
        self.epg_fixture()
        pl = self.playlist_with_epg(str(self.base / "guia.xml"))
        self.assertTrue(app.auto_load_playlist_epg(pl))
        self.assertTrue(app.wait_for_epg_load(timeout=10))
        self.assertFalse(app.auto_load_playlist_epg(pl))

    def test_fuente_rota_no_pone_epg_y_no_martilla(self) -> None:
        app = self.make_app()
        pl = self.playlist_with_epg(str(self.base / "no-existe.xml"))
        self.assertTrue(app.auto_load_playlist_epg(pl))
        self.assertTrue(app.wait_for_epg_load(timeout=10))
        self.assertIsNone(app.epg)
        self.assertTrue(app.status.is_error)
        self.assertIn("No se pudo cargar el EPG", app.status.message)
        # Reintento inmediato bloqueado (mismo criterio que WARM_RETRY_S).
        self.assertFalse(app.auto_load_playlist_epg(pl))
        self.assertIsNone(app._epg_load_thread)

    def test_prueba_las_varias_fuentes_hasta_que_una_funciona(self) -> None:
        app = self.make_app()
        ok = str(self.epg_fixture("ok.xml"))
        bad = str(self.base / "rota.xml")
        pl = Playlist(
            name="Varias",
            source=str(self.base / "Varias.m3u"),
            channels=[ch()],
            epg_url=bad,
            epg_urls=[bad, ok],
        )
        self.assertTrue(app.auto_load_playlist_epg(pl))
        self.assertTrue(app.wait_for_epg_load(timeout=10))
        self.assertEqual(app.epg_source, ok)

    def test_cambia_de_fuente_si_la_siguiente_lista_declara_otra(self) -> None:
        app = self.make_app()
        primero = str(self.epg_fixture("guia1.xml"))
        segundo = str(self.epg_fixture("guia2.xml"))
        app.auto_load_playlist_epg(self.playlist_with_epg(primero, name="A"))
        app.wait_for_epg_load(timeout=10)
        self.assertEqual(app.epg_source, primero)

        self.assertTrue(
            app.auto_load_playlist_epg(self.playlist_with_epg(segundo, name="B"))
        )
        app.wait_for_epg_load(timeout=10)
        self.assertEqual(app.epg_source, segundo)

    def test_no_pisa_un_epg_elegido_a_mano(self) -> None:
        app = self.make_app()
        manual = str(self.epg_fixture("manual.xml"))
        app.epg = ui_app.parse_file(manual)
        app.epg_source = manual
        app._epg_auto_source = None

        otra = str(self.epg_fixture("otra.xml"))
        self.assertFalse(
            app.auto_load_playlist_epg(self.playlist_with_epg(otra, name="B"))
        )
        self.assertEqual(app.epg_source, manual)
        self.assertIsNone(app._epg_load_thread)

    def test_lista_que_declara_la_misma_fuente_no_recarga(self) -> None:
        app = self.make_app()
        fuente = str(self.epg_fixture("misma.xml"))
        app.auto_load_playlist_epg(self.playlist_with_epg(fuente, name="A"))
        self.assertTrue(app.wait_for_epg_load(timeout=10))
        self.assertFalse(
            app.auto_load_playlist_epg(self.playlist_with_epg(fuente, name="A2"))
        )
        self.assertIsNone(app._epg_load_thread)


class TestEnsureEpgEsperaAlHilo(_AppCase):
    def test_ensure_epg_sin_prompt_cuando_el_hilo_termina(self) -> None:
        app = self.make_app()
        epg_path = self.epg_fixture()
        app.auto_load_playlist_epg(self.playlist_with_epg(str(epg_path)))

        with mock.patch.object(app, "_prompt_form") as prompt:
            programs = app.ensure_epg(ch())
        prompt.assert_not_called()
        self.assertTrue(programs)
        self.assertEqual(programs[0].title, "Titulares")

    def test_ensure_epg_prompt_si_la_lista_no_trae_epg(self) -> None:
        app = self.make_app()
        with (
            mock.patch.object(app, "_prompt_form", return_value=None) as prompt,
            mock.patch(
                "thetvview.ui.app.load_epg_source", side_effect=OSError("sin red")
            ),
        ):
            self.assertEqual(app.ensure_epg(ch(), url_hint="https://a/g.xml"), [])
        self.assertEqual(
            prompt.call_args.kwargs.get("initial", {}).get("path"), "https://a/g.xml"
        )

    def test_ensure_epg_tras_fallo_de_la_lista_usa_el_hint(self) -> None:
        app = self.make_app()
        pl = self.playlist_with_epg(str(self.base / "no-existe.xml"))
        app.auto_load_playlist_epg(pl)
        self.assertTrue(app.wait_for_epg_load(timeout=10))
        with (
            mock.patch.object(app, "_prompt_form", return_value=None) as prompt,
            mock.patch(
                "thetvview.ui.app.load_epg_source", side_effect=OSError("sin red")
            ),
        ):
            self.assertEqual(app.ensure_epg(ch(), url_hint="https://a/g.xml"), [])
        self.assertEqual(
            prompt.call_args.kwargs.get("initial", {}).get("path"), "https://a/g.xml"
        )


class TestOpenPlaylistDisparaLaCarga(_AppCase):
    def test_abrir_lista_local_con_epg_carga_la_guia(self) -> None:
        app = self.make_app()
        self.epg_fixture()
        m3u = self.base / "ConEPG.m3u"
        m3u.write_text(
            '#EXTM3U x-tvg-url="guia.xml"\n'
            '#EXTINF:-1 tvg-id="canal1.es",Canal Uno HD\n'
            "http://stream/canal1\n",
            encoding="utf-8",
        )
        entry = PlaylistEntry(name="ConEPG", source=str(m3u))
        with mock.patch(
            "thetvview.ui.app.load_playlist_source",
            side_effect=lambda source, **kw: parse_file(source),
        ):
            open_playlist(app, entry)

        self.assertTrue(app.wait_for_epg_load(timeout=10))
        self.assertIsNotNone(app.epg)
        self.assertEqual(app.epg_source, str(self.base / "guia.xml"))
        self.assertEqual(len(app.stack), 2)  # catálogo + canales

    def test_abrir_lista_sin_epg_no_intenta_cargar_nada(self) -> None:
        app = self.make_app()
        m3u = self.base / "SinEPG.m3u"
        m3u.write_text("#EXTM3U\n#EXTINF:-1,A\nhttp://a\n", encoding="utf-8")
        entry = PlaylistEntry(name="SinEPG", source=str(m3u))
        with mock.patch(
            "thetvview.ui.app.load_playlist_source",
            side_effect=lambda source, **kw: parse_file(source),
        ):
            open_playlist(app, entry)
        self.assertIsNone(app._epg_load_thread)
        self.assertIsNone(app.epg)


if __name__ == "__main__":
    unittest.main()
