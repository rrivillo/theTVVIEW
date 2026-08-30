"""Tests de detección y agrupación de resoluciones (thetvview.resolutions)."""

import unittest

from thetvview.models import Channel
from thetvview.resolutions import (
    base_name,
    detect,
    name_has_resolution,
    quality_rank,
    split_name,
    swappable_variants,
    variants_of,
)


def ch(name: str, **attrs: str) -> Channel:
    return Channel(name=name, url=f"http://x/{name}", attrs=dict(attrs))


class TestSplitName(unittest.TestCase):
    def test_sufijo_calidad(self):
        self.assertEqual(split_name("La 1 HD"), ("La 1", "HD"))
        self.assertEqual(split_name("La 1 FHD"), ("La 1", "FHD"))

    def test_sufijo_numerico(self):
        self.assertEqual(split_name("Canal Sur 1080p"), ("Canal Sur", "1080p"))
        self.assertEqual(split_name("Canal Sur 720P"), ("Canal Sur", "720p"))

    def test_parentesis_estilo_iptv_org(self):
        self.assertEqual(split_name("Canal X (720p)"), ("Canal X", "720p"))
        self.assertEqual(split_name("Canal X (1080i)"), ("Canal X", "1080i"))
        self.assertEqual(
            split_name("1+1 International HD (720p)"),
            ("1+1 International", "720p"),
        )

    def test_marcadores_corchete_se_ignoran(self):
        self.assertEqual(
            split_name("Canal X [Geo-blocked] (720p)"), ("Canal X", "720p")
        )
        self.assertEqual(
            split_name("Canal X [Not 24/7]"), ("Canal X [Not 24/7]", None)
        )

    def test_sin_sufijo(self):
        self.assertEqual(split_name("Teledeporte"), ("Teledeporte", None))
        # Palabra final que no es calidad: no se toca.
        self.assertEqual(split_name("Canal 24 Horas"), ("Canal 24 Horas", None))

    def test_nombre_de_una_palabra(self):
        self.assertEqual(split_name("HD"), ("HD", None))

    def test_normalizacion_minusculas(self):
        self.assertEqual(split_name("La 2 hd"), ("La 2", "HD"))
        self.assertEqual(split_name("La 2 4k"), ("La 2", "4K"))


class TestDetect(unittest.TestCase):
    def test_desde_atributo_res(self):
        c = ch("Canal X", res="1080")
        self.assertEqual(detect(c), "1080p")

    def test_desde_atributo_quality(self):
        c = ch("Canal X", quality="fhd")
        self.assertEqual(detect(c), "FHD")

    def test_atributo_tiene_prioridad_al_sufijo(self):
        c = ch("Canal X HD", quality="UHD")
        self.assertEqual(detect(c), "UHD")

    def test_desde_sufijo_del_nombre(self):
        self.assertEqual(detect(ch("Canal X 4K")), "4K")

    def test_desde_tvg_id_estilo_iptv_org(self):
        c = Channel(name="Canal X", url="http://x", tvg_id="canalx.es@HD")
        self.assertEqual(detect(c), "HD")

    def test_nombre_tiene_prioridad_sobre_tvg_id(self):
        c = Channel(name="Canal X (1080p)", url="http://x", tvg_id="canalx.es@SD")
        self.assertEqual(detect(c), "1080p")

    def test_sin_resolucion(self):
        self.assertIsNone(detect(ch("Canal X")))


class TestBaseName(unittest.TestCase):
    def test_quita_sufijo(self):
        self.assertEqual(base_name(ch("La 1 HD")), "La 1")

    def test_sin_sufijo_devuelve_nombre(self):
        self.assertEqual(base_name(ch("La 1")), "La 1")


class TestQualityRank(unittest.TestCase):
    def test_orden_simbolico(self):
        self.assertLess(quality_rank("SD"), quality_rank("HD"))
        self.assertLess(quality_rank("HD"), quality_rank("FHD"))
        self.assertLess(quality_rank("FHD"), quality_rank("4K"))

    def test_orden_numerico(self):
        self.assertLess(quality_rank("480p"), quality_rank("720p"))
        self.assertLess(quality_rank("720p"), quality_rank("1080p"))
        self.assertEqual(quality_rank("2160p"), quality_rank("4K"))

    def test_entrelazado_detras_de_progresivo(self):
        self.assertLess(quality_rank("1080i"), quality_rank("1080p"))
        self.assertGreater(quality_rank("1080i"), quality_rank("720p"))

    def test_desconocida_va_al_final(self):
        self.assertEqual(quality_rank("?"), 0)
        self.assertLess(quality_rank("?"), quality_rank("SD"))


class TestVariantsOf(unittest.TestCase):
    def setUp(self):
        self.channels = [
            ch("La 1 HD"),
            ch("La 1 SD"),
            ch("La 1 4K"),
            ch("Otro Canal"),
            ch("La 2 HD"),
        ]

    def test_variantes_ordenadas_peor_a_mejor(self):
        target = self.channels[0]  # La 1 HD
        got = variants_of(self.channels, target)
        labels = [detect(c) for c in got]
        self.assertEqual(labels, ["SD", "HD", "4K"])

    def test_canal_sin_variante_devuelve_vacio(self):
        self.assertEqual(variants_of(self.channels, ch("Otro Canal")), [])

    def test_canal_sin_resolucion_devuelve_vacio(self):
        solo = [ch("Canal Solo")]
        self.assertEqual(variants_of(solo, solo[0]), [])

    def test_unica_variante_no_es_selector(self):
        dos = [ch("Canal A HD"), ch("Canal B")]
        self.assertEqual(variants_of(dos, dos[0]), [])

    def test_mezcla_atributos_y_sufijos(self):
        mezcla = [ch("Dep HD"), ch("Dep", res="2160"), ch("Dep SD")]
        got = variants_of(mezcla, mezcla[2])
        self.assertEqual([detect(c) for c in got], ["SD", "HD", "2160p"])

    def test_variantes_reales_iptv_org(self):
        canales = [
            ch("1+1 International"),
            ch("1+1 International (720p)"),
            Channel(name="1+1 International (1080p)", url="http://x/3",
                    tvg_id="1plus1.int@SD"),
        ]
        got = variants_of(canales, canales[2])
        self.assertEqual([detect(c) for c in got], ["720p", "1080p"])
        # La entrada sin calidad no entra en el grupo de variantes.
        self.assertNotIn(canales[0], got)
        # Y su base agrupa igual aunque el tvg-id diga otra cosa.
        self.assertEqual(
            [base_name(c) for c in canales], ["1+1 International"] * 3
        )


class TestNameHasResolution(unittest.TestCase):
    def test_con_sufijo_calidad(self):
        self.assertTrue(name_has_resolution(ch("La 1 HD")))
        self.assertTrue(name_has_resolution(ch("Canal X 4K")))
        self.assertTrue(name_has_resolution(ch("Canal X (720p)")))

    def test_con_sufijo_numerico(self):
        self.assertTrue(name_has_resolution(ch("Canal Sur 1080p")))
        self.assertTrue(name_has_resolution(ch("Canal Sur 720P")))

    def test_sin_sufijo(self):
        self.assertFalse(name_has_resolution(ch("Teledeporte")))
        self.assertFalse(name_has_resolution(ch("Canal 24 Horas")))

    def test_etiqueta_solo_en_attrs_no_cuenta(self):
        self.assertFalse(name_has_resolution(ch("Canal X", res="1080")))

    def test_etiqueta_solo_en_tvg_id_no_cuenta(self):
        c = Channel(name="Canal X", url="http://x", tvg_id="canalx.es@HD")
        self.assertFalse(name_has_resolution(c))


class TestSwappableVariants(unittest.TestCase):
    def test_con_variante_sin_etiqueta(self):
        canales = [ch("Deportes"), ch("Deportes HD"), ch("Deportes SD")]
        got = swappable_variants(canales, canales[0])
        self.assertEqual([detect(c) for c in got], ["SD", "HD"])

    def test_con_etiqueta_y_variante(self):
        canales = [ch("La 1 HD"), ch("La 1 SD"), ch("La 1 4K")]
        got = swappable_variants(canales, canales[0])
        self.assertEqual([detect(c) for c in got], ["SD", "HD", "4K"])

    def test_unica_variante_devuelve_vacio(self):
        canales = [ch("Canal A HD"), ch("Canal B")]
        self.assertEqual(swappable_variants(canales, canales[0]), [])

    def test_sin_variante_devuelve_vacio(self):
        canales = [ch("Canal Solo")]
        self.assertEqual(swappable_variants(canales, canales[0]), [])

    def test_excluye_canales_de_otro_base(self):
        canales = [
            ch("La 1 HD"), ch("La 1 SD"),
            ch("La 2 HD"), ch("La 2 SD"),
        ]
        got = swappable_variants(canales, canales[0])
        bases = [base_name(c) for c in got]
        self.assertTrue(all(b == "La 1" for b in bases))


if __name__ == "__main__":
    unittest.main()
