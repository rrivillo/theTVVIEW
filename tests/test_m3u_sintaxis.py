"""M3U-002 · FR-M3U-002/003: separador de coma y directivas.

El separador entre atributos y nombre es la decisión que más se nota: si
se corta en el sitio equivocado, un canal se queda sin ``tvg-id`` o
pierde la mitad del nombre. Aquí se fija el comportamiento esperado y se
blindan las propiedades (round-trip) sobre las fixtures reales del repo.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from thetvview.m3u_parser import _split_extinf, parse_text

FIXTURES = Path(__file__).parent / "fixtures"


def _linea(rest: str) -> tuple[dict[str, str], str]:
    return _split_extinf(rest)


class TestSeparadorDeComa(unittest.TestCase):
    def test_una_sola_coma(self) -> None:
        self.assertEqual(_linea('-1 tvg-id="c1",Canal'), ({"tvg_id": "c1"}, "Canal"))

    def test_sin_coma_no_hay_nombre(self) -> None:
        self.assertEqual(_linea("-1"), ({}, ""))
        self.assertEqual(_linea(""), ({}, ""))

    def test_nombre_con_comas_no_se_trunca(self) -> None:
        """El fallo que arregla el escaneo: «Canal, La 1» se quedaba en «La 1»."""
        attrs, nombre = _linea('-1 tvg-id="c1",Canal, La 1')
        self.assertEqual(attrs, {"tvg_id": "c1"})
        self.assertEqual(nombre, "Canal, La 1")

    def test_coma_dentro_de_un_valor_entrecomillado(self) -> None:
        attrs, nombre = _linea('-1 tvg-name="Simpson, Los" custom-tag="xyz",Los Simpson')
        self.assertEqual(attrs["tvg_name"], "Simpson, Los")
        self.assertEqual(attrs["custom_tag"], "xyz")
        self.assertEqual(nombre, "Los Simpson")

    def test_comas_en_atributo_y_en_nombre_a_la_vez(self) -> None:
        attrs, nombre = _linea('-1 group-title="A,B" tvg-id="z",Canal, HD')
        self.assertEqual(attrs["group_title"], "A,B")
        self.assertEqual(attrs["tvg_id"], "z")
        self.assertEqual(nombre, "Canal, HD")

    def test_comillas_sin_cerrar_no_tiran(self) -> None:
        # Línea rota: se lee lo que se puede, sin excepción (FR-M3U-005).
        attrs, nombre = _linea('-1 tvg-id="roto,Canal')
        self.assertIsInstance(attrs, dict)
        self.assertTrue(nombre or nombre == "")

    def test_comillas_impares_al_final(self) -> None:
        attrs, nombre = _linea('-1 tvg-id="c1",Canal "raro"')
        self.assertEqual(attrs.get("tvg_id"), "c1")
        self.assertIsInstance(nombre, str)


class TestReglasDeDirectivas(unittest.TestCase):
    def test_duplicado_gana_el_ultimo(self) -> None:
        attrs, _ = _linea('-1 tvg-id="a" tvg-id="b",X')
        self.assertEqual(attrs["tvg_id"], "b")

    def test_desconocido_se_conserva(self) -> None:
        pl = parse_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="c1" x-cheap="sí" yotube="z",Uno\nhttp://x/1.ts\n'
        )
        canal = pl.channels[0]
        self.assertEqual(canal.tvg_id, "c1")
        self.assertEqual(canal.attrs["x_cheap"], "sí")
        self.assertEqual(canal.attrs["yotube"], "z")

    def test_atributo_vacio_no_inventa_valor(self) -> None:
        pl = parse_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="" tvg-logo="" ,Uno\nhttp://x/1.ts\n'
        )
        canal = pl.channels[0]
        self.assertIsNone(canal.tvg_id)
        self.assertIsNone(canal.tvg_logo)

    def test_sin_comillas_no_es_atributo(self) -> None:
        """``key=valor`` sin comillas en EXTINF se ignora: si no, se come la URL."""
        pl = parse_text(
            "#EXTM3U\n#EXTINF:-1 tvg-id=abc,Uno\nhttp://x/1.ts\n"
        )
        self.assertIsNone(pl.channels[0].tvg_id)
        self.assertEqual(pl.channels[0].name, "Uno")

    def test_extinf_sin_url_se_descarta(self) -> None:
        pl = parse_text('#EXTM3U\n#EXTINF:-1,Colgado\n#EXTINF:-1,Otro\nhttp://x/1.ts\n')
        self.assertEqual([c.name for c in pl.channels], ["Otro"])


class TestFixtureDeCasosQueEncontraronBugs(unittest.TestCase):
    """M3U-004: cada entrada que encontró un bug queda aquí para siempre.

    El fichero `fixtures/malformadas.m3u` enumera los casos; lo que se
    comprueba es la **política** (nombres con comas intactos, duplicados
    con el último ganando, colgados descartados, URL suelta), no que el
    número de canales sea bonito.
    """

    def setUp(self) -> None:
        self.texto = (FIXTURES / "malformadas.m3u").read_text(encoding="utf-8")
        self.playlist = parse_text(self.texto, source=str(FIXTURES / "malformadas.m3u"))
        self.por_nombre = {c.name: c for c in self.playlist.channels}

    def test_nombre_con_comas_no_se_trunca(self) -> None:
        canal = self.por_nombre["Canal, La 1"]
        self.assertEqual(canal.tvg_id, "c1")

    def test_coma_en_grupo_y_en_nombre_a_la_vez(self) -> None:
        canal = self.por_nombre["Canal, HD"]
        self.assertEqual(canal.tvg_id, "c2")
        self.assertEqual(canal.group, "Deportes, Locales")

    def test_placeholder_de_xtream_no_es_grupo(self) -> None:
        self.assertIsNone(self.por_nombre["Todos"].group)

    def test_duplicado_gana_el_ultimo(self) -> None:
        self.assertEqual(self.por_nombre["Duplicado"].tvg_id, "bueno")

    def test_tvg_logo_con_prefijo_residual(self) -> None:
        self.assertEqual(
            self.por_nombre["Con Logo Roto"].tvg_logo, "http://logo.example/x.png"
        )

    def test_extinf_sin_url_se_descarta(self) -> None:
        self.assertNotIn("Colgado", self.por_nombre)
        self.assertNotIn("Se Queda Colgada Al Final", self.por_nombre)

    def test_larga_linea_no_corta_el_nombre(self) -> None:
        self.assertIn("Larguisimo", self.por_nombre)
        self.assertGreater(len(self.por_nombre["Larguisimo"].tvg_name or ""), 100)

    def test_sin_comillas_no_se_convierten_en_url(self) -> None:
        canal = self.por_nombre["Sin Comillas"]
        self.assertIsNone(canal.tvg_id)
        self.assertTrue(canal.url.endswith("/8.ts"))

    def test_unicode_en_el_nombre(self) -> None:
        self.assertIn("Ñandú 📺 Émoji", self.por_nombre)

    def test_todo_canal_tiene_nombre_y_url(self) -> None:
        for canal in self.playlist.channels:
            self.assertTrue(canal.name.strip(), repr(canal))
            self.assertTrue(canal.url.strip(), repr(canal))


class TestPropiedadRoundTrip(unittest.TestCase):
    """`(attrs, name)` reconstruido y vuelto a partir debe ser el mismo."""

    def _casos(self) -> list[tuple[dict[str, str], str]]:
        valores = ["c1", "canal.es", "Canal, La 1", "A,B,C", "Ñandú", "con espacios"]
        grupos = ["Noticias", "Deportes, Locales", "-"]
        casos: list[tuple[dict[str, str], str]] = []
        for i, vid in enumerate(valores):
            for j, grupo in enumerate(grupos):
                nombre = valores[(i + j) % len(valores)] + f" {i}{j}"
                casos.append(
                    (
                        {"tvg-id": vid, "group-title": grupo, "tvg-name": nombre},
                        nombre,
                    )
                )
        return casos

    def test_round_trip_de_atributos_y_nombre(self) -> None:
        for attrs, nombre in self._casos():
            with self.subTest(attrs=attrs, nombre=nombre):
                partes = [f'-1 {k}="{v}"' for k, v in attrs.items()]
                texto = " ".join(partes) + "," + nombre
                leidos, leido_nombre = _linea(texto)
                # El parser normaliza las claves (`tvg-id` → `tvg_id`).
                self.assertEqual(
                    leidos, {k.replace("-", "_"): v for k, v in attrs.items()}
                )
                self.assertEqual(leido_nombre, nombre)
                # Y el mismo texto devuelve lo mismo otra vez.
                self.assertEqual(_linea(texto), (leidos, nombre))

    def test_round_trip_sobre_las_fixtures_reales(self) -> None:
        archivos = sorted(FIXTURES.glob("*.m3u")) + sorted(FIXTURES.glob("*.m3u8"))
        self.assertTrue(archivos, "sin fixtures no hay nada que round-trippear")
        lineas = 0
        for ruta in archivos:
            texto = ruta.read_text(encoding="utf-8", errors="replace")
            for linea in texto.splitlines():
                if not linea.upper().startswith("#EXTINF:"):
                    continue
                lineas += 1
                with self.subTest(fichero=ruta.name, linea=linea[:60]):
                    attrs, nombre = _linea(linea[len("#EXTINF:") :])
                    self.assertIsInstance(attrs, dict)
                    self.assertIsInstance(nombre, str)
                    for clave in attrs:
                        self.assertTrue(clave.replace("_", "").isalnum(), clave)
                    # Determinista: dos veces lo mismo.
                    self.assertEqual(_linea(linea[len("#EXTINF:") :]), (attrs, nombre))
        self.assertGreater(lineas, 0)


if __name__ == "__main__":
    unittest.main()
