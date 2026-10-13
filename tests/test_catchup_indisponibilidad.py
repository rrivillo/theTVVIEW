"""CU-04 — Estados de indisponibilidad separados.

Hoy el módulo tiene **un** estado para todo lo que falla: ``CatchupState.ERROR``.
Eso obliga al modal a decir «la reproducción falló», que es cierto y no dice
nada: el usuario no sabe si reintentar, recargar credenciales, esperar a que
vuelva la red, o rendirse.

La regla del proyecto es explícita: *ningún dato perdido en silencio* y
*«desconocido» no es «falso»*. Este ticket separa las causas que **el proyecto
ya sabe distinguir** sin inventar ninguna:

============================  =========================================
Causa                        Cómo se sabe
============================  =========================================
``NOT_AVAILABLE``             El proveedor aceptó la URL y no devolvió el
                              archivo. Es el «no está», no el «no sé».
``AUTH_FAILED``               401/403: las credenciales están malas o caducaron.
``NETWORK``                   No se pudo ni conectar: DNS, refused, timeout.
``UNKNOWN``                   Algo falló y no se puede clasificar.
============================  =========================================

Lo que **no** se hace, a propósito:

- **No se deducen estas causas sin evidencia.** Si el error no viene
  clasificado, el estado es ``UNKNOWN``, nunca un ``NETWORK`` inventado.
- **No se duplica la taxonomía de errores.** Se traduce desde
  :class:`thetvview.player.errors.StreamErrorCode`, que es la única del proyecto.
- **No se inventan códigos de red.** Distinguir «contenido eliminado» de
  «fuera de ventana» exigiría sondear el panel, y el plan prohíbe el sondeo
  implícito (REQ-SRC-004).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from thetvview import catchup
from thetvview.catchup import (
    CatchupError,
    CatchupState,
    describe_unavailable,
    texto_indisponibilidad,
)
from thetvview.player.errors import StreamErrorCode

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Traducción desde la taxonomía que ya existe
# ---------------------------------------------------------------------------


class TestTraduceDesdeLaTaxonomiaDelProyecto(unittest.TestCase):
    """Un solo origen de verdad: `player.errors.StreamErrorCode`."""

    def test_es_la_misma_taxonomia(self):
        """Nada de códigos paralelos: se importan, no se redefinen."""
        self.assertIs(catchup.StreamErrorCode, StreamErrorCode)

    def test_autenticacion_se_reconoce(self):
        self.assertIs(
            describe_unavailable(StreamErrorCode.AUTH_FAILED),
            CatchupState.AUTH_FAILED,
        )

    def test_red_se_reconoce(self):
        for codigo in (
            StreamErrorCode.CONNECTION_FAILED,
            StreamErrorCode.CONNECTION_TIMEOUT,
            StreamErrorCode.DNS,
            StreamErrorCode.NETWORK_ERROR,
            StreamErrorCode.TLS_FAILED,
        ):
            with self.subTest(codigo=codigo.name):
                self.assertIs(
                    describe_unavailable(codigo), CatchupState.NETWORK
                )

    def test_no_disponible_se_reconoce(self):
        for codigo in (
            StreamErrorCode.NOT_FOUND,
            StreamErrorCode.SERVER_ERROR,
            StreamErrorCode.MANIFEST_INVALID,
            StreamErrorCode.MEDIA_DEMUX,
        ):
            with self.subTest(codigo=codigo.name):
                self.assertIs(
                    describe_unavailable(codigo), CatchupState.NOT_AVAILABLE
                )

    def test_lo_desconocido_no_se_adivina(self):
        """Sin evidencia suficiente el estado es desconocido, no «red»."""
        for codigo in (
            StreamErrorCode.UNKNOWN,
            StreamErrorCode.BACKEND_ERROR,
            StreamErrorCode.DECODE_ERROR,
        ):
            with self.subTest(codigo=codigo.name):
                self.assertIs(
                    describe_unavailable(codigo), CatchupState.UNKNOWN
                )

    def test_la_cancelacion_del_usuario_no_es_un_fallo_del_proveedor(self):
        """Cancelar no es «no disponible»: el archivo puede estar perfectamente."""
        self.assertIs(
            describe_unavailable(StreamErrorCode.CANCELLED),
            CatchupState.CANCELLED,
        )

    def test_un_error_de_reproductor_no_es_un_fallo_del_proveedor(self):
        """Si el motor no arranca, el archivo puede estar bien."""
        for codigo in (
            StreamErrorCode.PLAYER_MISSING,
            StreamErrorCode.PLAYER_STARTUP,
            StreamErrorCode.PLAYER_EXIT,
            StreamErrorCode.CODEC_UNSUPPORTED,
            StreamErrorCode.INVALID_URL,
            StreamErrorCode.UNSUPPORTED_PROTOCOL,
        ):
            with self.subTest(codigo=codigo.name):
                self.assertIs(
                    describe_unavailable(codigo), CatchupState.PLAYER
                )

    def test_sin_clasificar_no_se_inventa_causa(self):
        self.assertIs(describe_unavailable(None), CatchupState.UNKNOWN)

    def test_un_texto_libre_no_se_convierte_en_causa(self):
        """Si no viene de la taxonomía, es desconocido."""
        self.assertIs(
            describe_unavailable("algo raro que dice el motor"),
            CatchupState.UNKNOWN,
        )


# ---------------------------------------------------------------------------
# 2. Los estados existen y son distintos entre sí
# ---------------------------------------------------------------------------


class TestEstadosSeparados(unittest.TestCase):
    ESTADOS_DE_INDISPONIBILIDAD = (
        CatchupState.NOT_AVAILABLE,
        CatchupState.AUTH_FAILED,
        CatchupState.NETWORK,
        CatchupState.UNKNOWN,
        CatchupState.CANCELLED,
        CatchupState.PLAYER,
    )

    def test_existen_y_son_distintos(self):
        valores = {e.value for e in self.ESTADOS_DE_INDISPONIBILIDAD}
        self.assertEqual(len(valores), len(self.ESTADOS_DE_INDISPONIBILIDAD))

    def test_no_reutilizan_el_error_generico(self):
        """`ERROR` sigue existiendo para lo que no se clasifica, pero ya no es
        la única respuesta posible."""
        self.assertNotIn(CatchupState.ERROR, self.ESTADOS_DE_INDISPONIBILIDAD)

    def test_el_estado_desconocido_es_explicito(self):
        """Se puede decir «no sé» sin mentir."""
        self.assertIsNot(CatchupState.UNKNOWN, CatchupState.NOT_AVAILABLE)


# ---------------------------------------------------------------------------
# 3. El modal explica qué hacer en cada caso
# ---------------------------------------------------------------------------


class TestElModalExplicaQueHacer(unittest.TestCase):
    """Cada estado lleva un consejo distinto y accionable."""

    def test_cada_estado_tiene_texto_propio(self):
        textos = {
            estado: texto_indisponibilidad(estado)
            for estado in CatchupState
            if estado.value in {
                "not_available", "auth_failed", "network", "unknown",
                "cancelled", "player",
            }
        }
        for estado in (
            CatchupState.NOT_AVAILABLE,
            CatchupState.AUTH_FAILED,
            CatchupState.NETWORK,
            CatchupState.UNKNOWN,
            CatchupState.CANCELLED,
            CatchupState.PLAYER,
        ):
            with self.subTest(estado=estado.name):
                self.assertIsInstance(textos[estado], str)
                self.assertTrue(textos[estado].strip())

    def test_el_texto_no_repite_un_error_generico(self):
        """Nada de «la reproducción falló» para todo."""
        generico = "la reproducción falló"
        for estado in (
            CatchupState.NOT_AVAILABLE,
            CatchupState.AUTH_FAILED,
            CatchupState.NETWORK,
        ):
            with self.subTest(estado=estado.name):
                self.assertNotIn(generico, texto_indisponibilidad(estado))

    def test_el_texto_no_trae_tecnica_cruda(self):
        for estado in (
            CatchupState.NOT_AVAILABLE,
            CatchupState.AUTH_FAILED,
            CatchupState.NETWORK,
            CatchupState.UNKNOWN,
            CatchupState.PLAYER,
            CatchupState.CANCELLED,
        ):
            with self.subTest(estado=estado.name):
                texto = texto_indisponibilidad(estado)
                for prohibido in ("Traceback", "StreamErrorCode", "None", "0x"):
                    self.assertNotIn(prohibido, texto)

    def test_describir_desconocido_lo_dice_explicitamente(self):
        """«No se sabe» es una respuesta válida; ocultarlo no lo es."""
        texto = texto_indisponibilidad(CatchupState.UNKNOWN).lower()
        self.assertTrue(
            "no se sabe" in texto or "no se puede" in texto,
            f"el texto debe admitir la ignorancia, dice: {texto!r}",
        )

    def test_el_mensaje_es_usable_en_un_modal(self):
        """`explicar_estado` devuelve un `CatchupError`: lo que la UI muestra."""
        for estado in (CatchupState.NETWORK, CatchupState.AUTH_FAILED):
            with self.subTest(estado=estado.name):
                error = catchup.explicar_estado(estado)
                self.assertIsInstance(error, CatchupError)
                self.assertTrue(str(error).strip())

    def test_un_estado_sin_texto_propio_cae_en_el_generico(self):
        """`OUTSIDE_ARCHIVE_WINDOW` lo explica `describe_state`, no este mapa."""
        self.assertEqual(
            texto_indisponibilidad(CatchupState.OUTSIDE_ARCHIVE_WINDOW),
            catchup.describe_state(CatchupState.OUTSIDE_ARCHIVE_WINDOW),
        )


if __name__ == "__main__":
    unittest.main()