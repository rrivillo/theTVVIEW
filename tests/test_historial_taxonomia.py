"""H-02 — Una sola taxonomía: la del reproductor.

El historial técnico va a guardar un `reason_code` por evento y por entrada.
Ese código tiene que venir de **algún sitio único**. Si el módulo de
diagnóstico se inventara sus propias cadenas (`"fallo_de_red"`,
`"timeout"`, `"no_se_abre"`), aparecería un segundo vocabulario que nadie
mantiene y que nadie sabe traducir: el informe diría una cosa y el historial
otra, sin que nadie pueda detectar el desacuerdo.

Este archivo fija tres cosas:

1. **Los códigos son de :mod:`thetvview.player.errors`.** Los veinte y pico
   de ``StreamErrorCode``, más los ocho ``EndReason`` de ``player.session``
   (que no son errores, son motivos de cierre, pero son el otro vocabulario
   que el historial tiene que poder escribir).
2. **`recoverable` no es una política nueva.** Sale de
   ``StreamError.reintentable``. Si algún día cambia esa regla, cambia aquí
   sola: aquí no hay criterio propio que pueda contradecirlo.
3. **Lo observado y lo inferido no se mezclan.** ``UNKNOWN`` es una
   *interpretación*, no un hecho: el hecho es el ``returncode`` y el mensaje.
   Un historial que guarde sólo el código y tire el hecho no puede
   explicarse después.

El último test del archivo es el que de verdad protege la regla: recorre el
código de ``thetvview/diagnostics/`` buscando cadenas que seallen en un
``reason_code`` sin pasar por :mod:`thetvview.player.errors`. Hoy no hay
ninguna; el test existe para que no las haya mañana.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from thetvview.diagnostics import taxonomy
from thetvview.diagnostics.taxonomy import (
    CODES_CONOCIDOS,
    MOTIVOS_CONOCIDOS,
    Causa,
    causa_de,
    causa_de_codigo,
    codigo_de,
    es_razon_conocida,
    explicacion_de,
)
from thetvview.player.errors import (
    NO_REINTENTABLE,
    StreamError,
    StreamErrorCode,
)
from thetvview.player.session import EndReason

#: Rutas donde SÍ se permite nombrar códigos literales.
PERMITIDO = {"taxonomy.py"}


class TestLosCodigosSonLosDelReproductor(unittest.TestCase):
    """El vocabulario del historial es el del reproductor, punto."""

    def test_todos_los_codigos_de_error_estan_cubiertos(self):
        for codigo in StreamErrorCode:
            with self.subTest(codigo=codigo.value):
                self.assertIn(codigo.value, CODES_CONOCIDOS)
                self.assertIs(codigo_de(codigo.value), codigo)

    def test_todos_los_motivos_de_fin_estan_cubiertos(self):
        for motivo in EndReason:
            with self.subTest(motivo=motivo.value):
                self.assertIn(motivo.value, MOTIVOS_CONOCIDOS)

    def test_no_hay_codigos_de_mas(self):
        """El conjunto es exactamente la unión, ni un código inventado."""
        esperados = {c.value for c in StreamErrorCode} | {
            m.value for m in EndReason
        }
        self.assertEqual(set(MOTIVOS_CONOCIDOS), esperados)

    def test_codigo_de_rechaza_lo_que_no_conoce(self):
        for valor in ("fallo_de_red", "http_500", "", "   ", None, 42, object()):
            with self.subTest(valor=repr(valor)):
                self.assertIsNone(codigo_de(valor))  # type: ignore[arg-type]

    def test_es_razon_conocida_es_consulta_y_no_explota(self):
        self.assertTrue(es_razon_conocida("auth_failed"))
        self.assertTrue(es_razon_conocida("user_stopped"))
        self.assertFalse(es_razon_conocida("inventado"))
        self.assertFalse(es_razon_conocida(None))  # type: ignore[arg-type]


class TestRecuperableNoEsUnaPoliticaNueva(unittest.TestCase):
    """`recoverable` sale de `reintentable`. Aquí no hay criterio propio."""

    def test_coincide_con_la_politica_del_supervisor(self):
        for codigo in StreamErrorCode:
            with self.subTest(codigo=codigo.value):
                self.assertEqual(
                    causa_de_codigo(codigo).recoverable,
                    codigo.reintentable,
                    "si difieren, hay dos políticas y sólo una puede ser la buena",
                )

    def test_los_que_no_reintenta_son_ireintentables(self):
        for codigo in NO_REINTENTABLE:
            with self.subTest(codigo=codigo.value):
                self.assertFalse(causa_de_codigo(codigo).recoverable)

    def test_sin_codigo_no_se_inventa_la_recuperabilidad(self):
        """Sin motivo conocido la recuperabilidad se desconoce, no se supone."""
        causa = causa_de_codigo(None)
        self.assertIsNone(causa.recoverable)
        # Vacío, no "unknown": no afirmar nada no es afirmar "no se sabe".
        self.assertEqual(causa.reason_code, "")
        self.assertFalse(causa.causa_conocida)


class TestObservadoNoEsInferido(unittest.TestCase):
    """FR-REC-004 aplicado al historial: hecho e interpretación, separados."""

    def test_un_fallo_conocido_dice_su_causa(self):
        fallo = StreamError(
            code=StreamErrorCode.AUTH_FAILED,
            resumen="El canal rechazó las credenciales.",
            observado="HTTP 401",
            returncode=1,
            http_status=401,
        )
        causa = causa_de(fallo)
        self.assertEqual(causa.reason_code, "auth_failed")
        self.assertTrue(causa.causa_conocida)
        self.assertEqual(causa.observado, "HTTP 401")

    def test_unknown_no_afirma_una_causa(self):
        """`UNKNOWN` es una conjetura declarada como tal, no un hecho."""
        fallo = StreamError(
            code=StreamErrorCode.UNKNOWN,
            resumen="No se pudo reproducir el canal.",
            observado="el reproductor terminó con 1",
            returncode=1,
        )
        causa = causa_de(fallo)
        self.assertFalse(causa.causa_conocida)
        self.assertEqual(causa.confianza, "desconocida")
        # El hecho se conserva: sin él, el historial no se puede explicar.
        self.assertEqual(causa.observado, "el reproductor terminó con 1")

    def test_sin_fallo_no_hay_causa_ni_observado(self):
        causa = causa_de(None)
        self.assertEqual(causa, Causa())
        self.assertFalse(causa.causa_conocida)
        self.assertEqual(causa.observado, "")

    def test_causa_admite_un_observado_a_partir(self):
        """El observado se puede dar aunque el código no se sepa."""
        causa = Causa(reason_code="", recoverable=None, observado="HTTP 500")
        self.assertEqual(causa.observado, "HTTP 500")
        self.assertFalse(causa.causa_conocida)


class TestExplicacionParaModal(unittest.TestCase):
    """Un motivo sin explicación obliga al usuario a adivinar."""

    def test_todo_codigo_tiene_explicacion(self):
        for codigo in StreamErrorCode:
            with self.subTest(codigo=codigo.value):
                self.assertTrue(explicacion_de(codigo.value))

    def test_todo_motivo_de_fin_tiene_explicacion(self):
        for motivo in EndReason:
            with self.subTest(motivo=motivo.value):
                self.assertTrue(explicacion_de(motivo.value))

    def test_lo_desconocido_se_dice_desconocido(self):
        """Nunca una explicación inventada para un código que no existe."""
        texto = explicacion_de("inventado")
        self.assertIn("No se reconoce", texto)

    def test_lo_vacio_no_inventa(self):
        self.assertEqual(explicacion_de(""), "")


class TestNoHayTaxonomiaParalela(unittest.TestCase):
    """El test que protege la regla de verdad, buscado en el código real."""

    def setUp(self) -> None:
        self.paquete = Path(taxonomy.__file__).parent

    def _ficheros(self):
        for ruta in sorted(self.paquete.rglob("*.py")):
            if ruta.name in PERMITIDO:
                continue
            yield ruta

    def test_ningun_reason_code_literal(self):
        """Nadie escribe un motivo a mano fuera de la taxonomía."""
        ofensas: list[str] = []
        for ruta in self._ficheros():
            arbol = ast.parse(ruta.read_text(encoding="utf-8"), str(ruta))
            for nodo in ast.walk(arbol):
                if not isinstance(nodo, ast.Call):
                    continue
                nombre = getattr(nodo.func, "id", None) or getattr(
                    nodo.func, "attr", None
                )
                if nombre not in ("Event", "registrar"):
                    continue
                for palabra in nodo.keywords:
                    if palabra.arg != "reason_code":
                        continue
                    if not isinstance(palabra.value, ast.Constant):
                        continue  # una variable no es un código inventado
                    valor = palabra.value.value
                    if valor in (None, ""):
                        continue
                    ofensas.append(
                        f"{ruta.name}:{nodo.lineno} reason_code={valor!r}"
                    )
        self.assertEqual(ofensas, [], "\n".join(ofensas) or "sin ofensas")

    def test_el_paquete_no_importa_nada_de_ui(self):
        """El historial no depende de la interfaz: al revés tampoco."""
        for ruta in self._ficheros():
            texto = ruta.read_text(encoding="utf-8")
            with self.subTest(fichero=ruta.name):
                self.assertNotIn("from ..ui", texto)
                self.assertNotIn("import curses", texto)


if __name__ == "__main__":
    unittest.main()