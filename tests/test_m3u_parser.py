"""Tests mínimos para m3u_parser (fixture tests/fixtures/sample.m3u)."""

import unittest
from pathlib import Path

from thetvview.m3u_parser import parse_file, parse_text

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playlist = parse_file(FIXTURES / "sample.m3u")

    def test_cuenta_canales(self) -> None:
        # 3 con URL; el EXTINF final sin URL se descarta.
        self.assertEqual(len(self.playlist.channels), 3)

    def test_metadatos_canal1(self) -> None:
        ch = self.playlist.channels[0]
        self.assertEqual(ch.name, "Canal Uno HD")
        self.assertEqual(ch.tvg_id, "canal1.es")
        self.assertEqual(ch.tvg_name, "Canal Uno")
        self.assertEqual(ch.tvg_logo, "http://logo.example.com/1.png")
        self.assertEqual(ch.group, "Noticias")

    def test_extra_options_ordenados(self) -> None:
        ch = self.playlist.channels[0]
        self.assertEqual(
            ch.extra_options,
            [
                ("EXTVLCOPT", "http-user-agent=theTVVIEW/1.0"),
                ("KODIPROP", "inputstream=inputstream.adaptive"),
            ],
        )

    def test_radio_y_grupo(self) -> None:
        ch = self.playlist.channels[1]
        self.assertTrue(ch.radio)
        self.assertEqual(ch.group, "Música")
        self.assertFalse(self.playlist.channels[0].radio)

    def test_coma_en_nombre(self) -> None:
        ch = self.playlist.channels[2]
        self.assertEqual(ch.name, "Los Simpson")
        self.assertEqual(ch.tvg_name, "Simpson, Los")
        self.assertEqual(ch.attrs.get("custom_tag"), "xyz")

    def test_opciones_huerfanas_ignoradas(self) -> None:
        # El EXTVLCOPT inicial (sin EXTINF previo) no aparece en ningún canal.
        all_opts = [opt for ch in self.playlist.channels for opt in ch.extra_options]
        self.assertNotIn(("EXTVLCOPT", "option_sin_extinf=1"), all_opts)


class TestParseText(unittest.TestCase):
    def test_vacio_devuelve_playlist_sin_canales(self) -> None:
        pl = parse_text("")
        self.assertEqual(pl.channels, [])

    def test_solo_comentarios(self) -> None:
        pl = parse_text("#EXTM3U\n# solo un comentario\n")
        self.assertEqual(pl.channels, [])

    def test_x_tvg_url_de_cabecera(self) -> None:
        pl = parse_text(
            '#EXTM3U x-tvg-url="https://epg.example.com/guide.xml.gz"\n'
            '#EXTINF:-1 tvg-id="a.es",Canal A\n'
            "http://stream/a\n"
        )
        self.assertEqual(pl.epg_url, "https://epg.example.com/guide.xml.gz")
        self.assertEqual(len(pl.channels), 1)

    def test_sin_x_tvg_url_da_none(self) -> None:
        pl = parse_text("#EXTM3U\n#EXTINF:-1,A\nhttp://a\n")
        self.assertIsNone(pl.epg_url)

    def test_group_title_placeholder_xtream_es_sin_grupo(self) -> None:
        # Portales Xtream: group-title="-" significa sin grupo.
        pl = parse_text(
            '#EXTM3U\n#EXTINF:-1 group-title="-",Canal X\nhttp://x\n'
            '#EXTINF:-1 group-title="",Canal Y\nhttp://y\n'
            '#EXTINF:-1 group-title="Deportes",Canal Z\nhttp://z\n'
        )
        self.assertIsNone(pl.channels[0].group)
        self.assertIsNone(pl.channels[1].group)
        self.assertEqual(pl.channels[2].group, "Deportes")


class TestParseTextTags(unittest.TestCase):
    """Tags malformados/comentarios: el parseo optimizado no cambia de criterio."""

    def test_logo_anidado_sin_espacio(self) -> None:
        # Bug real de IPTVSV.m3u: tvg-logo="tvg-logo="https://...
        pl = parse_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="a" tvg-logo="tvg-logo="https://l.png",Canal\n'
            "http://u\n"
        )
        self.assertEqual(pl.channels[0].tvg_logo, "https://l.png")

    def test_logo_anidado_con_espacio(self) -> None:
        # Variante con espacio: solo la puede arreglar el regex de respaldo.
        pl = parse_text(
            '#EXTM3U\n#EXTINF:-1 tvg-logo=" tvg-logo="https://l.png",Canal\n'
            "http://u\n"
        )
        self.assertEqual(pl.channels[0].tvg_logo, "https://l.png")

    def test_logo_normal_intacto(self) -> None:
        pl = parse_text(
            '#EXTM3U\n#EXTINF:-1 tvg-logo="https://l.png" tvg-id="a",Canal\n'
            "http://u\n"
        )
        ch = pl.channels[0]
        self.assertEqual(ch.tvg_logo, "https://l.png")
        self.assertEqual(ch.tvg_id, "a")

    def test_extinf_sin_comillas_no_da_atributos(self) -> None:
        pl = parse_text("#EXTM3U\n#EXTINF:-1 tvg-id=a,Canal\nhttp://u\n")
        ch = pl.channels[0]
        self.assertEqual(ch.name, "Canal")
        self.assertIsNone(ch.tvg_id)
        self.assertEqual(ch.attrs, {})

    def test_tags_en_minusculas_se_reconocen(self) -> None:
        pl = parse_text(
            "#EXTM3U\n"
            "#extinf:-1 group-title=\"Deportes\",Canal\n"
            "#extvlcopt: http-user-agent=UA\n"
            "http://u\n"
        )
        ch = pl.channels[0]
        self.assertEqual(ch.group, "Deportes")
        self.assertEqual(ch.extra_options, [("EXTVLCOPT", " http-user-agent=UA")])

    def test_comentario_desconocido_entre_extinf_y_url(self) -> None:
        pl = parse_text(
            "#EXTM3U\n#EXTINF:-1,Canal\n#EXTGRP:Ignorada\nhttp://u\n"
        )
        ch = pl.channels[0]
        self.assertEqual(ch.name, "Canal")
        self.assertEqual(ch.url, "http://u")
        self.assertIsNone(ch.group)


if __name__ == "__main__":
    unittest.main()
