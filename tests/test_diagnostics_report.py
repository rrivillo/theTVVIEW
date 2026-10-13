"""Tests de DIAG-002: informe §11.3 puro, redactado y con archivo 0600.

Garantías que se comprueban:

- el informe se compone **sin red** con lo que ya se sabe (FR-DIAG-009);
- lo desconocido se marca, nunca se inventa (§11.4);
- no sale ni una URL ni un secreto (FR-DIAG-008);
- el archivo nace con permisos 0600.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from thetvview.diagnostics import DESCONOCIDO, Event, construir_informe, guardar_informe

_SESION = {
    "session_id": "abc-123",
    "correlation_id": "corr-1",
    "canal": "Canal Uno",
    "reproductor": "mpv",
    "estado": "ended",
    "fase": "failed",
    "marcas": {"creada": "2026-10-12T10:00:00.000", "fin": ""},
    "motivo_fin": "failed",
    "detalle_fin": "el reproductor devolvió 1",
    "intentos": [{"intento": 1, "error": "http_403"}],
}

_MOTOR = {
    "estado": "detenido",
    "intentos_del_ciclo": 2,
    "reconexiones": 1,
    "evidencia": "unknown",
    "evidencia_motivo": "sin comprobar",
    "ultimo_error": {"codigo": "http_403", "mensaje": "403"},
    "ultima_espera": {"segundos": 2.0, "acotada": True},
}


class TestConstruirInforme(unittest.TestCase):
    def test_vacio_dice_desconocido_y_advierte(self) -> None:
        texto = construir_informe()
        self.assertIn(DESCONOCIDO, texto)
        self.assertIn("theTVVIEW", texto)
        # La advertencia del §11.3 es parte del modelo, no un extra.
        self.assertIn("causa raíz", texto)

    def test_sesion_y_motor_cubren_el_modelo(self) -> None:
        texto = construir_informe(canal="Canal Uno", sesion=_SESION, motor=_MOTOR)
        self.assertIn("corr-1", texto)  # correlación
        self.assertIn("abc-123", texto)  # sesión
        self.assertIn("mpv", texto)  # motor
        self.assertIn("Tiempos medidos", texto)  # marcas
        self.assertIn("Historial de reintentos", texto)  # intentos
        self.assertIn("Motivo de finalización", texto)  # fin
        self.assertIn("http_403", texto)  # error normalizado
        self.assertIn("evidencia", texto)  # evidencia de reproducción

    def test_marca_la_marca_no_medida(self) -> None:
        texto = construir_informe(sesion=_SESION)
        # «fin» venía vacía: se marca, no se inventa un instante.
        self.assertIn(f"fin: {DESCONOCIDO}", texto)
        self.assertIn("creada: 2026-10-12T10:00:00.000", texto)

    def test_http_y_analisis_solo_si_se_miden(self) -> None:
        sin = construir_informe(sesion=_SESION)
        self.assertNotIn("HTTP:", sin)
        self.assertNotIn("Análisis de manifiesto:", sin)
        con = construir_informe(
            http_estado=200,
            content_type="application/dash+xml",
            analisis="HLS master con 3 variantes",
        )
        self.assertIn("HTTP: 200", con)
        self.assertIn("application/dash+xml", con)
        self.assertIn("3 variantes", con)

    def test_redacta_urls_y_secretos(self) -> None:
        texto = construir_informe(
            canal="http://usuario:clave@proveedor.example/live/canal.m3u8",
            sesion={"detalle_fin": "fallo con Authorization: Bearer abcdef123456"},
        )
        self.assertNotIn("clave", texto)
        self.assertNotIn("abcdef123456", texto)

    def test_sin_eventos_no_hay_seccion(self) -> None:
        self.assertNotIn("== Eventos", construir_informe())

    def test_con_eventos_sale_la_seccion(self) -> None:
        evento = Event(
            "probe.failed",
            timestamp=1_760_000_000.0,
            severity="error",
            reason_code="http_403",
        )
        texto = construir_informe(eventos=[evento])
        self.assertIn("probe.failed", texto)
        self.assertIn("http_403", texto)

    def test_no_hace_red(self) -> None:
        # FR-DIAG-009: componer el informe no toca ni un socket.
        with mock.patch("socket.socket", side_effect=AssertionError("¡red!")):
            texto = construir_informe(canal="X", sesion=_SESION, motor=_MOTOR)
        self.assertIn("X", texto)


class TestGuardarInforme(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_archivo_con_permisos_privados(self) -> None:
        ruta = guardar_informe("texto del informe", ruta=self.dir / "informe.txt")
        self.assertEqual(ruta.read_text(encoding="utf-8"), "texto del informe")
        if os.name != "nt":
            self.assertEqual(ruta.stat().st_mode & 0o777, 0o600)

    def test_crea_el_directorio_si_no_existe(self) -> None:
        ruta = guardar_informe("x", ruta=self.dir / "nuevo" / "informe.txt")
        self.assertTrue(ruta.exists())

    def test_nombre_por_defecto_y_colisiones(self) -> None:
        with mock.patch("thetvview.config.DIAGNOSTICS_DIR", self.dir):
            uno = guardar_informe("a")
            dos = guardar_informe("b")
        self.assertNotEqual(uno.name, dos.name)
        self.assertEqual(len(list(self.dir.iterdir())), 2)
        if os.name != "nt":
            self.assertEqual(uno.stat().st_mode & 0o777, 0o600)
            self.assertEqual(dos.stat().st_mode & 0o777, 0o600)

    def test_no_puede_escribir_no_explota(self) -> None:
        with mock.patch(
            "thetvview.diagnostics.report.os.open", side_effect=OSError("sin disco")
        ):
            with self.assertRaises(OSError):
                guardar_informe("x", ruta=self.dir / "i.txt")


class TestCableadoEnApp(unittest.TestCase):
    """`show_diagnose` guarda el informe sin romper el modal (DIAG-002)."""

    def _app_doble(self):
        class Doble:
            _sessions = SimpleNamespace(
                actual=SimpleNamespace(para_informe=lambda: dict(_SESION))
            )
            screen = SimpleNamespace(supervisor=None)
            eventos = None

        return Doble()

    def test_guarda_desde_los_datos_de_app(self) -> None:
        from thetvview.ui.app import App

        doble = self._app_doble()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("thetvview.config.DIAGNOSTICS_DIR", Path(tmp)):
                ruta = App._guardar_informe_sesion(
                    doble, SimpleNamespace(name="Canal Uno"), []
                )
            self.assertIsNotNone(ruta)
            texto = Path(ruta).read_text(encoding="utf-8")
        self.assertIn("corr-1", texto)
        self.assertIn("Canal Uno", texto)

    def test_fallo_al_guardar_devuelve_none(self) -> None:
        from thetvview.ui.app import App

        doble = self._app_doble()
        with mock.patch(
            "thetvview.diagnostics.report.guardar_informe",
            side_effect=OSError("sin sitio"),
        ):
            ruta = App._guardar_informe_sesion(
                doble, SimpleNamespace(name="X"), []
            )
        self.assertIsNone(ruta)

    def test_registrar_diagnose_distingue_nuevo_y_recurrente(self) -> None:
        """FR-DIAG-005: con historial, el fallo de hoy sabe si ya pasó."""
        from thetvview.diagnostics.history import HistorialDiagnostico
        from thetvview.ui.app import App

        with tempfile.TemporaryDirectory() as tmp:
            doble = SimpleNamespace(
                diagnosticos=HistorialDiagnostico(Path(tmp) / "d.json")
            )
            fallo = SimpleNamespace(ok=False, verdict="NOT READY")
            canal = SimpleNamespace(name="Canal", tvg_id="")
            self.assertIn("nuevo", App._registrar_diagnose(doble, canal, fallo))
            self.assertIn(
                "recurrente", App._registrar_diagnose(doble, canal, fallo)
            )
            self.assertEqual(doble.diagnosticos.contar("Canal"), (0, 2))

    def test_registrar_diagnose_sin_historial_no_opina(self) -> None:
        from thetvview.ui.app import App

        doble = SimpleNamespace()  # App sin `run()`: sin historial
        nota = App._registrar_diagnose(
            doble, SimpleNamespace(), SimpleNamespace(ok=False, verdict="X")
        )
        self.assertIsNone(nota)


if __name__ == "__main__":
    unittest.main()
