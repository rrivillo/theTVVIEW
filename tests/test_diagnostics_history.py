"""Tests de DIAG-004: historial acotado y fallo nuevo vs. recurrente.

Garantías (FR-DIAG-005 y FR-DIAG-010):

- el historial distingue un fallo **nuevo** de uno **recurrente** sin
  ir a la red;
- está acotado por **cantidad, edad y tamaño total**, los tres;
- un fichero ilegible, ausente o corrupto se comporta como vacío: nada
  aquí puede romper un diagnóstico;
- el fichero nace 0600 y las claves de canal pasan por redacción.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from thetvview.diagnostics import construir_informe
from thetvview.diagnostics.history import (
    HistorialDiagnostico,
    RegistroHistorial,
    default_historial,
)


class TestRegistro(unittest.TestCase):
    def test_entradas_ilegitimas_se_salten(self) -> None:
        self.assertIsNone(RegistroHistorial.de_dict("no soy un dict"))
        self.assertIsNone(RegistroHistorial.de_dict({"momento": "x", "canal": "a"}))
        self.assertIsNone(RegistroHistorial.de_dict({"canal": "a"}))
        registro = RegistroHistorial.de_dict(
            {"momento": 10.0, "canal": "a", "exito": True}
        )
        self.assertIsNotNone(registro)
        assert registro is not None
        self.assertEqual(registro.canal, "a")
        self.assertTrue(registro.exito)


class TestHistorial(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ruta = self.dir / "diagnostics.json"
        # Base realista: un relector con reloj por defecto no debe purgar
        # lo que un reloj de juguete (epoch 1000) habría dejado «viejo».
        self.reloj = [time.time()]

        def ahora() -> float:
            return self.reloj[0]

        self.hist = HistorialDiagnostico(self.ruta, ahora=ahora)

    def test_registrar_y_consultar(self) -> None:
        self.hist.registrar("Canal Uno", True)
        self.hist.registrar("Canal Uno", False, motivo="http_403")
        self.assertEqual(self.hist.contar("Canal Uno"), (1, 1))
        self.assertEqual(len(self.hist.entradas()), 2)
        # Orden cronológico.
        self.assertTrue(self.hist.entradas()[0].exito)

    def test_fallo_nuevo_vs_recurrente(self) -> None:
        # Sin historial: no es recurrente.
        self.assertFalse(self.hist.es_recurrente("Canal"))
        self.hist.registrar("Canal", False, motivo="http_403")
        # El fallo de hoy aún no está registrado al consultar: es
        # «recurrente» porque ya hubo uno antes.
        self.assertTrue(self.hist.es_recurrente("Canal"))
        # Con motivo distinto no cuenta como el mismo fallo.
        self.assertFalse(self.hist.es_recurrente("Canal", motivo="timeout"))
        self.assertTrue(self.hist.es_recurrente("Canal", motivo="http_403"))
        # Un éxito previo no hace recurrente a un fallo.
        self.assertFalse(self.hist.es_recurrente("Otro"))

    def test_acotado_por_cantidad(self) -> None:
        hist = HistorialDiagnostico(self.ruta, max_entradas=3)
        for i in range(6):
            hist.registrar(f"canal{i}", True)
        entradas = hist.entradas()
        self.assertEqual(len(entradas), 3)
        # Se quedan las más nuevas.
        self.assertEqual([e.canal for e in entradas], ["canal3", "canal4", "canal5"])

    def test_acotado_por_edad(self) -> None:
        reloj = [1_000.0]
        hist = HistorialDiagnostico(
            self.ruta, max_dias=1.0, ahora=lambda: reloj[0]
        )
        hist.registrar("viejo", False)
        reloj[0] += 3 * 86_400.0  # tres días después
        hist.registrar("nuevo", True)
        entradas = hist.entradas()
        self.assertEqual([e.canal for e in entradas], ["nuevo"])

    def test_acotado_por_tamano_total(self) -> None:
        hist = HistorialDiagnostico(
            self.ruta, max_entradas=100, max_bytes=256
        )
        for i in range(20):
            hist.registrar(f"canal-{i}-algo-largo", False, motivo="x" * 50)
        self.assertLessEqual(self.ruta.stat().st_size, 256)
        # Y lo que queda sigue siendo coherente al releer.
        releido = HistorialDiagnostico(self.ruta, max_bytes=256)
        self.assertGreater(len(releido.entradas()), 0)

    def test_releer_persiste(self) -> None:
        self.hist.registrar("Canal", False, motivo="timeout", fase="diagnose")
        releido = HistorialDiagnostico(self.ruta)
        entradas = releido.entradas()
        self.assertEqual(len(entradas), 1)
        self.assertEqual(entradas[0].motivo, "timeout")
        self.assertEqual(entradas[0].fase, "diagnose")

    def test_fichero_corrupto_es_vacio(self) -> None:
        self.ruta.write_text("{{{ no es json", encoding="utf-8")
        self.assertEqual(HistorialDiagnostico(self.ruta).entradas(), [])

    def test_fichero_inexistente_es_vacio(self) -> None:
        self.assertEqual(HistorialDiagnostico(self.ruta).entradas(), [])

    def test_json_ajeno_malformado_no_tira_el_resto(self) -> None:
        self.ruta.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entradas": [
                        "x",
                        {"momento": time.time(), "canal": "b", "exito": True},
                    ],
                }
            ),
            encoding="utf-8",
        )
        entradas = HistorialDiagnostico(self.ruta).entradas()
        self.assertEqual([e.canal for e in entradas], ["b"])

    def test_archivo_privado(self) -> None:
        self.hist.registrar("Canal", True)
        if os.name != "nt":
            self.assertEqual(self.ruta.stat().st_mode & 0o777, 0o600)

    def test_redacta_la_clave_del_canal(self) -> None:
        self.hist.registrar(
            "http://usuario:clave@proveedor.example/live/canal.m3u8", False
        )
        registro = self.hist.entradas()[0]
        self.assertNotIn("clave", registro.canal)

    def test_no_puede_escribir_no_lanza(self) -> None:
        with mock.patch(
            "thetvview.diagnostics.history.os.open", side_effect=OSError("sin disco")
        ):
            registro = self.hist.registrar("Canal", False)
        self.assertFalse(registro.exito)
        # Y la entrada sigue en memoria: el historial no se pierde.
        self.assertEqual(len(self.hist.entradas()), 1)

    def test_default_usa_la_ruta_de_config(self) -> None:
        hist = default_historial()
        self.assertEqual(hist.ruta.name, "diagnostics.json")


class TestInformeConHistorial(unittest.TestCase):
    def test_nota_del_historial_entra_en_el_informe(self) -> None:
        texto = construir_informe(
            canal="Canal",
            historial="Fallo recurrente: este canal ya fallaba antes.",
        )
        self.assertIn("Historial local", texto)
        self.assertIn("Fallo recurrente", texto)

    def test_sin_nota_no_hay_seccion(self) -> None:
        self.assertNotIn("Historial local", construir_informe())


if __name__ == "__main__":
    unittest.main()
