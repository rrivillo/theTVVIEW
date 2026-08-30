"""Tests mínimos para epg_parser (XMLTV plano y .gz)."""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thetvview.epg_parser import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def _load() -> object:  # typing lo afina cada test
    return parse_file(FIXTURES / "sample.xmltv")


class TestEpgParserPlain(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.epg = parse_file(FIXTURES / "sample.xmltv")

    def test_canales_indexados(self) -> None:
        self.assertEqual(self.epg.channel_name("canal1.es"), "Canal Uno")
        self.assertIsNone(self.epg.channel_name("no.existe"))

    def test_iconos_de_canal(self) -> None:
        self.assertEqual(
            self.epg.channel_icon("canal1.es"),
            "https://example.com/logos/canal1.png",
        )
        # icon sin src útil no se indexa
        self.assertIsNone(self.epg.channel_icon("radio.pop"))
        self.assertIsNone(self.epg.channel_icon("no.existe"))

    def test_sub_title_opcional(self) -> None:
        titulares = self.epg.programmes_for("canal1.es")[0]
        self.assertEqual(titulares.sub_title, "Edición matinal")
        deportes = self.epg.programmes_for("canal1.es")[1]
        self.assertIsNone(deportes.sub_title)

    def test_categorias_en_orden_y_vacias(self) -> None:
        titulares, deportes = self.epg.programmes_for("canal1.es")
        self.assertEqual(titulares.categories, ["Noticias", "Actualidad"])
        self.assertEqual(deportes.categories, ["Deportes"])
        cancion = self.epg.programmes_for("radio.pop")[0]
        self.assertEqual(cancion.categories, [])

    def test_programas_por_canal_ordenados(self) -> None:
        progs = self.epg.programmes_for("canal1.es")
        self.assertEqual([p.title for p in progs], ["Titulares", "Deportes"])
        starts = [p.start for p in progs]
        self.assertEqual(starts, sorted(starts))

    def test_start_con_timezone(self) -> None:
        prog = self.epg.programmes_for("canal1.es")[0]
        tz = timezone(timedelta(hours=2))
        self.assertEqual(prog.start, datetime(2026, 8, 25, 8, 0, 0, tzinfo=tz))

    def test_stop_ausente_se_rellena_con_next_start(self) -> None:
        titulares, deportes = self.epg.programmes_for("canal1.es")
        self.assertEqual(titulares.stop, deportes.start)
        self.assertIsNone(deportes.stop)

    def test_desc_opcional(self) -> None:
        prog = self.epg.programmes_for("canal1.es")[0]
        self.assertEqual(prog.desc, "Noticias de la mañana.")
        self.assertIsNone(self.epg.programmes_for("canal1.es")[1].desc)

    def test_now_playing(self) -> None:
        tz = timezone(timedelta(hours=2))
        dentro = datetime(2026, 8, 25, 9, 30, tzinfo=tz)
        antes = datetime(2026, 8, 25, 7, 0, tzinfo=tz)
        prog = self.epg.now_playing("canal1.es", dentro)
        assert prog is not None
        self.assertEqual(prog.title, "Deportes")
        self.assertIsNone(self.epg.now_playing("canal1.es", antes))

    def test_canal_desconocido_devuelve_vacio(self) -> None:
        self.assertEqual(self.epg.programmes_for("fantasma"), [])

    def test_next_programme(self) -> None:
        tz = timezone(timedelta(hours=2))
        titulares, deportes = self.epg.programmes_for("canal1.es")
        # Entre ambos programas: el siguiente es Deportes.
        entre = datetime(2026, 8, 25, 8, 30, tzinfo=tz)
        self.assertIs(self.epg.next_programme("canal1.es", entre), deportes)
        # Justo en el start: no cuenta (es estrictamente posterior).
        self.assertIs(self.epg.next_programme("canal1.es", titulares.start), deportes)
        # Tras el último programa: None (parrilla agotada).
        fin = datetime(2026, 8, 26, 0, 0, tzinfo=tz)
        self.assertIsNone(self.epg.next_programme("canal1.es", fin))
        # Canal desconocido: None.
        self.assertIsNone(self.epg.next_programme("fantasma", entre))

    def test_now_playing_ultimo_sin_stop_es_abierto(self) -> None:
        """Fallback: el último programa (stop=None) cubre hasta el fin de parrilla."""
        tz = timezone(timedelta(hours=2))
        muy_tarde = datetime(2026, 8, 25, 23, 59, tzinfo=tz)
        prog = self.epg.now_playing("canal1.es", muy_tarde)
        assert prog is not None
        self.assertEqual(prog.title, "Deportes")


class TestEpgParserGzip(unittest.TestCase):
    def test_gz_coincide_con_plano(self) -> None:
        plain = parse_file(FIXTURES / "sample.xmltv")
        gz = parse_file(FIXTURES / "sample.xmltv.gz")
        self.assertEqual(
            [(p.channel_id, p.title, p.start) for p in plain.programmes_for("canal1.es")],
            [(p.channel_id, p.title, p.start) for p in gz.programmes_for("canal1.es")],
        )


if __name__ == "__main__":
    unittest.main()
