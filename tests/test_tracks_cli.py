"""Tests de los overrides de línea de órdenes (plan F6 §43, opcional).

Lo importante aquí no es que las opciones existan, sino que **no cambien
nada** cuando no se usan: sin opciones, el comportamiento es exactamente el
de antes.
"""

from __future__ import annotations

import unittest

from thetvview import __main__ as cli_main
from thetvview.tracks.cli import (
    SessionOverrides,
    clear_session_overrides,
    get_session_overrides,
    parse_overrides,
    set_session_overrides,
)
from thetvview.tracks.prefs import TrackPreferences


class TestParseo(unittest.TestCase):
    def tearDown(self) -> None:
        clear_session_overrides()

    def test_sin_opciones(self) -> None:
        overrides, error = parse_overrides([])
        self.assertIsNone(error)
        self.assertTrue(overrides.is_empty)

    def test_audio(self) -> None:
        overrides, error = parse_overrides(["--audio", "es"])
        self.assertIsNone(error)
        self.assertEqual(overrides.audio_language, "es")

    def test_calidad(self) -> None:
        overrides, error = parse_overrides(["--quality", "720p"])
        self.assertEqual(overrides.quality, "720p")

    def test_calidad_auto(self) -> None:
        overrides, _error = parse_overrides(["--quality", "auto"])
        self.assertEqual(overrides.quality, "auto")

    def test_subtitulos_off(self) -> None:
        for valor in ("off", "no", "0", "false"):
            with self.subTest(valor=valor):
                overrides, _error = parse_overrides(["--subtitles", valor])
                self.assertFalse(overrides.subtitles_enabled)
                self.assertIsNone(overrides.subtitle_language)

    def test_subtitulos_idioma(self) -> None:
        overrides, _error = parse_overrides(["--subtitles", "eng"])
        self.assertTrue(overrides.subtitles_enabled)
        self.assertEqual(overrides.subtitle_language, "eng")

    def test_subtitulos_on(self) -> None:
        overrides, _error = parse_overrides(["--subtitles", "on"])
        self.assertTrue(overrides.subtitles_enabled)
        self.assertIsNone(overrides.subtitle_language)

    def test_varias_juntas(self) -> None:
        overrides, error = parse_overrides(
            ["--audio", "es", "--subtitles", "off", "--quality", "1080p"]
        )
        self.assertIsNone(error)
        self.assertEqual(overrides.audio_language, "es")
        self.assertFalse(overrides.subtitles_enabled)
        self.assertEqual(overrides.quality, "1080p")

    def test_opcion_desconocida_explica(self) -> None:
        overrides, error = parse_overrides(["--color", "azul"])
        self.assertIsNotNone(error)
        self.assertIn("--color", error)

    def test_valor_que_falta_explica(self) -> None:
        overrides, error = parse_overrides(["--audio"])
        self.assertIsNotNone(error)
        self.assertIn("--audio", error)

    def test_valor_vacio_explica(self) -> None:
        _overrides, error = parse_overrides(["--audio", "   "])
        self.assertIsNotNone(error)


class TestSobrePreferencias(unittest.TestCase):
    def test_gana_el_override(self) -> None:
        guardadas = TrackPreferences(
            preferred_audio_language="ca", preferred_quality="720p"
        )
        overrides = SessionOverrides(audio_language="en", quality="1080p")
        merged = overrides.merged_with(guardadas)
        self.assertEqual(merged.preferred_audio_language, "en")
        self.assertEqual(merged.preferred_quality, "1080p")

    def test_lo_no_pisado_se_conserva(self) -> None:
        guardadas = TrackPreferences(
            preferred_audio_language="ca", preferred_subtitle_language="fr",
            subtitles_enabled=True,
        )
        merged = SessionOverrides(audio_language="en").merged_with(guardadas)
        self.assertEqual(merged.preferred_subtitle_language, "fr")
        self.assertTrue(merged.subtitles_enabled)

    def test_subtitles_off_pisa_al_idioma(self) -> None:
        guardadas = TrackPreferences(
            preferred_subtitle_language="es", subtitles_enabled=True
        )
        merged = SessionOverrides(subtitles_enabled=False).merged_with(guardadas)
        self.assertFalse(merged.subtitles_enabled)

    def test_sin_preferencias(self) -> None:
        merged = SessionOverrides(audio_language="es").merged_with(None)
        self.assertEqual(merged.preferred_audio_language, "es")


class TestEstadoDelProceso(unittest.TestCase):
    def tearDown(self) -> None:
        clear_session_overrides()

    def test_activar_y_leer(self) -> None:
        self.assertIsNone(get_session_overrides())
        set_session_overrides(SessionOverrides(audio_language="es"))
        self.assertEqual(get_session_overrides().audio_language, "es")

    def test_vacios_no_se_activan(self) -> None:
        set_session_overrides(SessionOverrides())
        self.assertIsNone(get_session_overrides())

    def test_limpiar(self) -> None:
        set_session_overrides(SessionOverrides(audio_language="es"))
        clear_session_overrides()
        self.assertIsNone(get_session_overrides())


class TestArranque(unittest.TestCase):
    def test_opcion_mala_sale_con_codigo_2(self) -> None:
        # No debe llegar a lanzar curses: se explica y se sale.
        self.assertEqual(cli_main.run(["--inventada", "x"]), 2)

    def test_sin_opciones_no_devuelve_2(self) -> None:  # noqa: D102
        import unittest.mock as mock

        with mock.patch.object(cli_main, "_launch"):
            with mock.patch.object(cli_main, "curses_problem", return_value=""):
                with mock.patch.object(cli_main, "resolve_os", return_value="Linux"):
                    with mock.patch.object(cli_main, "prepare_console"):
                        self.assertEqual(cli_main.run([]), 0)
        clear_session_overrides()