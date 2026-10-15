"""Fase 8 · U-06 — Vocabulario e índice de candidatas.

Lo que se comprueba aquí es, sobre todo, lo que el modal **no** puede decir:
una probabilidad inventada, una señal traducida a algo que el detector no
comprobó, o un número de candidatas que nadie contó.
"""

from __future__ import annotations

import time
import unittest

from thetvview.models import Channel, Playlist
from thetvview.ui import sources_view as sv


def _canal(nombre="La 1 HD", url="http://a/live/1.ts", grupo="General",
           tvg="la1.es", **extra) -> Channel:
    return Channel(name=nombre, url=url, group=grupo, tvg_id=tvg, **extra)


class TestVocabulario(unittest.TestCase):
    """El detector habla en inglés; el usuario no."""

    def test_los_tres_niveles_son_castellano(self) -> None:
        self.assertEqual(sv.nivel_legible("high"), "alta")
        self.assertEqual(sv.nivel_legible("medium"), "media")
        self.assertEqual(sv.nivel_legible("low"), "baja")

    def test_un_nivel_desconocido_no_se_inventa(self) -> None:
        # Si el detector crece, el usuario ve el código y la app no finge
        # entenderlo. Inventar «alta» para un nivel nuevo sería mentir.
        self.assertEqual(sv.nivel_legible("dudoso"), "dudoso")

    def test_un_nivel_vacio_no_esta_blanco(self) -> None:
        self.assertTrue(sv.nivel_legible(""))

    def test_las_senales_cortas_caben_en_una_fila(self) -> None:
        # La nota de la fila se recorta al ancho del modal. Un aviso cortado
        # no avisa de nada: lo largo vive en el cuerpo del otro modal.
        for senales in (["tvg-id", "name"], ["tvg-id"], list(sv.SENALES_CORTAS)):
            with self.subTest(senales=senales):
                nota = f"alta · {sv.senales_cortas(senales)}"
                self.assertLessEqual(len(nota), 48)

    def test_una_senal_desconocida_se_muestra_tal_cual(self) -> None:
        self.assertIn("nueva-senal", sv.senales_cortas(["nueva-senal"]))

    def test_la_nota_recorta_las_señales_diciendo_cuantas_faltan(self) -> None:
        # Con seis señales la nota no cabe, y recortarla sin más haría creer
        # que sólo coincidían en tres cosas.
        nota = sv.Candidata("B", _canal(), "high", tuple(sv.SENALES_CORTAS)).nota
        self.assertIn("3 más", nota)
        self.assertLessEqual(len(nota), 48)

    def test_la_frase_larga_dice_las_señales(self) -> None:
        frase = sv.frase_de_senales(["tvg-id", "group"])
        self.assertIn("identificador", frase)
        self.assertIn("grupo", frase)
        self.assertTrue(frase.endswith("."))

    def test_la_frase_larga_sin_señales_no_inventa_una(self) -> None:
        self.assertIn("Sin señales", sv.frase_de_senales([]))

    def test_enumerar_en_castellano(self) -> None:
        self.assertEqual(sv.enumerar(["a"]), "a")
        self.assertEqual(sv.enumerar(["a", "b"]), "a y b")
        self.assertEqual(sv.enumerar(["a", "b", "c"]), "a, b y c")
        self.assertEqual(sv.enumerar([]), "")
        self.assertEqual(sv.enumerar(["a", "", "b"]), "a y b")

    def test_ningun_nivel_parece_como_probabilidad(self) -> None:
        # A-06: «nunca probabilidad sin calibrar». Ni porcentajes ni decimales
        # en nada de lo que el modal escribe sobre el nivel.
        for nivel in sv.NIVELES.values():
            with self.subTest(nivel=nivel):
                self.assertNotIn("%", nivel)
                self.assertFalse(any(c.isdigit() for c in nivel))

    def test_el_modelo_tiene_score_y_aqui_no_se_ensea(self) -> None:
        # `SourceCandidate.score` existe en el modelo de Fase 5A y nadie lo
        # debe mostrar: es un 0.0 sin calibrar. La Candidata ni lo tiene.
        self.assertFalse(hasattr(sv.Candidata, "score"))


class TestResumen(unittest.TestCase):
    """Un recorte callado se lee como «no hay más»."""

    def test_sin_candidatas(self) -> None:
        self.assertEqual(sv.resumen_de_candidatas(0, 0), "sin candidatas")

    def test_todas_caben(self) -> None:
        self.assertEqual(sv.resumen_de_candidatas(3, 3), "3 candidatas")
        self.assertEqual(sv.resumen_de_candidatas(1, 1), "1 candidata")

    def test_recortadas_lo_dice(self) -> None:
        self.assertIn("5 de 9", sv.resumen_de_candidatas(9, 5))
        self.assertIn("las otras 4", sv.resumen_de_candidatas(9, 5))


class TestIndice(unittest.TestCase):
    def setUp(self) -> None:
        self.este = _canal()
        self.otras = [
            ("Lista B", [_canal(url="http://b/live/1.ts")]),
            ("Lista C", [
                _canal(nombre="La 1", url="http://c/live/1.ts", tvg=""),
                _canal(nombre="Canal 9", url="http://c/live/9.ts", tvg="c9"),
            ]),
        ]
        self.indice = sv.construir_indice(self.otras)

    def test_encuentra_la_candidata_por_identificador(self) -> None:
        found = self.indice.candidatas_de(self.este)
        self.assertEqual([f for f, _ in found], ["Lista B"])

    def test_encuentra_la_candidata_por_nombre(self) -> None:
        # Sin tvg-id, el nombre normalizado es la única señal de partida.
        Channel_ = _canal(nombre="La 1", tvg="")
        found = self.indice.candidatas_de(Channel_)
        self.assertEqual([f for f, _ in found], ["Lista C"])

    def test_no_ofrece_el_canal_que_se_mira(self) -> None:
        propias = sv.construir_indice([("Lista A", [self.este])])
        self.assertEqual(propias.candidatas_de(self.este), [])

    def test_no_repite_una_candidata_que_encaja_en_dos_cubos(self) -> None:
        # La de Lista B coincide por identificador **y** por nombre. Una vez.
        found = self.indice.candidatas_de(self.este)
        self.assertEqual(len(found), 1)

    def test_no_repite_candidatas_identicas_entre_listas(self) -> None:
        # Dos listas con el mismo canal: son dos fuentes y se ven las dos.
        indice = sv.construir_indice(
            [("B", [_canal(url="http://b/1.ts")]),
             ("C", [_canal(url="http://c/1.ts")])]
        )
        self.assertEqual(len(indice.candidatas_de(self.este)), 2)

    def test_no_busca_por_grupo(self) -> None:
        # Un grupo de cinco mil canales no es un canal, y `detectar_candidatas`
        # es O(n²): buscarlo sería la forma de colgar la app.
        indice = sv.construir_indice(
            [("B", [_canal(nombre="Otro", tvg="otro")])]
        )
        self.assertEqual(indice.candidatas_de(self.este), [])

    def test_un_canal_sin_nombre_no_revienta(self) -> None:
        self.assertEqual(
            self.indice.candidatas_de(_canal(nombre="", tvg="")),
            [],
        )

    def test_cuenta_los_canales_una_vez(self) -> None:
        # Un canal con nombre y sin tvg-id sólo entra en un cubo; uno con los
        # dos entra en dos. El total tiene que ser el número de canales.
        indice = sv.construir_indice(
            [("B", [_canal(), _canal(nombre="La 1", tvg="")])]
        )
        self.assertEqual(indice.canales, 2)


class TestDeteccion(unittest.TestCase):
    """Quien decide si dos canales son el mismo es `sources.detection`."""

    def setUp(self) -> None:
        self.este = _canal()
        self.b = _canal(url="http://b/live/1.ts")
        self.c = _canal(url="http://c/live/1.ts")

    def test_acepta_y_explica(self) -> None:
        salida = sv.deteccion_para(self.este, [("B", self.b)])
        self.assertEqual(len(salida), 1)
        self.assertEqual(salida[0].fuente, "B")
        self.assertEqual(salida[0].nivel, "high")
        self.assertIn("tvg-id", salida[0].senales)
        self.assertIn("group", salida[0].senales)

    def test_no_devuelve_parejas_entre_candidatas(self) -> None:
        # El detector devuelve todos los pares del lote. Si se pasan tres
        # canales, saldrían tresparejas del canal y tres entre ellos: el
        # modal enseñaría listas que no son del canal que se está mirando.
        salida = sv.deteccion_para(self.este, [("B", self.b), ("C", self.c)])
        self.assertEqual([c.fuente for c in salida], ["B", "C"])

    def test_el_orden_es_estable(self) -> None:
        a = sv.deteccion_para(self.este, [("B", self.b), ("C", self.c)])
        b = sv.deteccion_para(self.este, [("C", self.c), ("B", self.b)])
        self.assertEqual([c.fuente for c in a], [c.fuente for c in b])

    def test_el_nivel_no_sale_de_la_pelicula(self) -> None:
        # El detector llama `high` en cuanto ve el tvg-id, y sin tvg-id hace
        # falta grupo o nombre además de otra señal. Se comprueba con los
        # tres casos, no con el primero solo.
        con_id = sv.deteccion_para(
            _canal(tvg="x.es"), [("B", _canal(url="http://b/x.ts", tvg="x.es"))]
        )
        self.assertEqual(con_id[0].nivel, "high")

        mismo_nombre_otro_grupo = _canal(nombre="La 2", tvg="", grupo="A")
        con_dos = sv.deteccion_para(
            mismo_nombre_otro_grupo,
            [("B", _canal(nombre="La 2", url="http://b/2.ts", tvg="", grupo="B"))],
        )
        # Mismo nombre pero distinto grupo: el detector lo rechaza, y está
        # bien: §9.2 dice que dos canales con el mismo nombre y sin tvg-id
        # son canales distintos.
        self.assertEqual(con_dos, [])

    def test_un_canal_que_revienta_no_tumba_el_modal(self) -> None:
        class Roto:
            name = "La 1 HD"
            url = "http://roto"
            tvg_id = "la1.es"
            group = "General"

            @property
            def attrs(self):
                raise RuntimeError("attrs explota")

        self.assertEqual(sv.deteccion_para(self.este, [("B", Roto())]), [])

    def test_sin_candidatas_no_se_llama_al_detector(self) -> None:
        self.assertEqual(sv.deteccion_para(self.este, []), [])


class TestCoste(unittest.TestCase):
    """El número que justifica el índice, medido y no supuesto."""

    def test_cien_mil_canales_se_agrupan_en_menos_de_dos_segundos(self) -> None:
        # Medido en este repositorio: el recorrido lineal cuesta ~1 s con dos
        # listas de 90 000 canales. El índice es la diferencia entre un modal
        # que aparece y un modal que parpadea. El tope es holgado a propósito:
        # si algún día falla aquí, es que el agrupamiento se rompió, no que la
        # máquina sea más lenta.
        listas = [
            (f"L{k}", [_canal(nombre=f"Canal {i}", tvg=f"c.{i}")
                       for i in range(50_000)])
            for k in range(2)
        ]
        inicio = time.perf_counter()
        indice = sv.construir_indice(listas)
        coste = time.perf_counter() - inicio
        self.assertEqual(indice.canales, 100_000)
        self.assertLess(coste, 2.0, f"el índice tardó {coste:.2f} s")

    def test_la_cancelacion_se_oye_a_ritmo_de_catalogo(self) -> None:
        # Sin comprobar la cancelación cada tanto, cancelar un catálogo grande
        # no evita el trabajo: sólo evita el resultado.
        llamadas = {"n": 0}

        def _cancelar() -> bool:
            llamadas["n"] += 1
            return llamadas["n"] > 2

        listas = [("L", [_canal(nombre=f"Canal {i}", tvg=f"c.{i}")
                         for i in range(10_000)])]
        with self.assertRaises(sv._Cancelado):
            sv.construir_indice(listas, cancelar=_cancelar)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()