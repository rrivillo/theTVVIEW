"""Tests de la normalización de idiomas (plan F0, SDD §23).

Se fija, sobre todo, el caso que da nombre a la sección: ``es``,
``es-ES``, ``spa``, ``Spanish`` y ``Español`` tienen que converger, **sin
perder** el valor original, y **sin** depender del locale del sistema.
"""

from __future__ import annotations

import unittest

from thetvview.tracks.language import (
    NON_LINGUISTIC,
    base_language,
    is_known_language,
    language_label,
    matches_language,
    normalize_code,
    normalize_language,
    same_language,
)


class TestNormalizacion(unittest.TestCase):
    def test_las_cinco_formas_de_espanol_convergen(self) -> None:
        for valor in ("es", "es-ES", "spa", "Spanish", "Español"):
            with self.subTest(valor=valor):
                info = normalize_language(valor)
                self.assertIsNotNone(info)
                self.assertEqual(info.code, "es")
                self.assertEqual(info.label, "Español")

    def test_no_se_destruye_el_valor_original(self) -> None:
        for valor in ("es-ES", "spa", "Spanish", "Español"):
            with self.subTest(valor=valor):
                info = normalize_language(valor)
                self.assertEqual(info.original_code, valor)

    def test_ingles_variantes(self) -> None:
        for valor in ("en", "en-GB", "eng", "English"):
            with self.subTest(valor=valor):
                self.assertEqual(normalize_code(valor), "en")

    def test_codigos_de_tres_letras_bibliografia_y_terminologia(self) -> None:
        self.assertEqual(normalize_code("deu"), "de")  # 639-2/B
        self.assertEqual(normalize_code("ger"), "de")  # 639-2/T
        self.assertEqual(normalize_code("fra"), "fr")
        self.assertEqual(normalize_code("fre"), "fr")
        self.assertEqual(normalize_code("zho"), "zh")
        self.assertEqual(normalize_code("chi"), "zh")
        self.assertEqual(normalize_code("gre"), "el")
        self.assertEqual(normalize_code("ell"), "el")

    def test_variante_regional_conserva_la_region(self) -> None:
        info = normalize_language("pt-BR")
        self.assertEqual(info.code, "pt")
        self.assertEqual(info.region, "BR")
        self.assertEqual(info.label, "Português (BR)")
        self.assertEqual(info.original_code, "pt-BR")

    def test_subrayo_y_espacio_also_son_variantes(self) -> None:
        for valor in ("pt_BR", "pt BR", "es-MX"):
            with self.subTest(valor=valor):
                info = normalize_language(valor)
                self.assertIsNotNone(info.code)
                self.assertEqual(info.original_code, valor)

    def test_acentos_y_mayusculas(self) -> None:
        self.assertEqual(normalize_code("ESPANOL"), "es")
        self.assertEqual(normalize_code("FRANÇAIS"), "fr")

    def test_nombre_con_sufijo_entre_parentesis(self) -> None:
        info = normalize_language("Español (España)")
        self.assertEqual(info.code, "es")

    def test_desconocido_no_se_inventa(self) -> None:
        info = normalize_language("Klingon")
        self.assertIsNone(info.code)
        self.assertEqual(info.original_code, "Klingon")
        self.assertIsNone(info.label)

    def test_etiqueta_texto_no_es_un_idioma(self) -> None:
        # SDD §23: "no asumir que una etiqueta textual siempre representa
        # correctamente el idioma". Un NAME del proveedor no se adivina.
        for valor in ("Commentary", "Dub", "Original", "SDH"):
            with self.subTest(valor=valor):
                self.assertIsNone(normalize_code(valor))

    def test_vacio_o_none(self) -> None:
        self.assertIsNone(normalize_language(None))
        self.assertIsNone(normalize_language(""))
        self.assertIsNone(normalize_language("   "))

    def test_codigos_no_linguisticos(self) -> None:
        info = normalize_language("und")
        self.assertIsNone(info.code)
        self.assertIn(info.original_code, NON_LINGUISTIC)

    def test_es_fallo_a_sedes_segun_tabla(self) -> None:
        self.assertTrue(is_known_language("es"))
        self.assertFalse(is_known_language("qqq"))

    def test_language_label_cae_al_original(self) -> None:
        self.assertEqual(language_label("spa"), "Español")
        self.assertEqual(language_label("Serranés"), "Serranés")
        self.assertIsNone(language_label(None))

    def test_str_de_language_info(self) -> None:
        self.assertEqual(str(normalize_language("spa")), "Español")
        self.assertEqual(str(normalize_language("Klingon")), "Klingon")
        # Sin entrada no hay información: se devuelve None, no un objeto
        # vacío que la UI tendría que tratar aparte.
        self.assertIsNone(normalize_language(None))


class TestBaseLanguage(unittest.TestCase):
    def test_base_de_variante(self) -> None:
        self.assertEqual(base_language("es-ES"), "es")
        self.assertEqual(base_language("es"), "es")
        self.assertEqual(base_language("spa"), "es")

    def test_base_none(self) -> None:
        self.assertIsNone(base_language(None))
        self.assertIsNone(base_language(""))

    def test_base_desconocido_conserva_lo_que_habia(self) -> None:
        self.assertEqual(base_language("klingon-XX"), "klingon")


class TestMatching(unittest.TestCase):
    def test_exacto_primero(self) -> None:
        self.assertTrue(same_language("es", "es"))
        self.assertTrue(same_language("spa", "Spanish"))
        self.assertFalse(same_language("es", "en"))

    def test_exigencia_de_region(self) -> None:
        self.assertFalse(same_language("es", "es-ES"))
        self.assertTrue(same_language("es-ES", "es-ES"))
        self.assertFalse(same_language("es-ES", "es-MX"))

    def test_base_cubre_la_variante(self) -> None:
        self.assertTrue(matches_language("es", "es-ES"))
        self.assertTrue(matches_language("es-ES", "es"))
        self.assertTrue(matches_language("spa", "Español"))
        self.assertFalse(matches_language("es", "en"))
        self.assertFalse(matches_language("en", "es-GB"))

    def test_none_no_encaja_con_nada(self) -> None:
        self.assertFalse(matches_language(None, "es"))
        self.assertFalse(matches_language("es", None))
        self.assertFalse(same_language(None, "es"))