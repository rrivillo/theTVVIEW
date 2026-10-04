"""Tests de los errores de reproducción normalizados (SDD-M §20, AC-005).

El motivo por el que este módulo existe: los tres binarios usan el mismo
``returncode`` 1 para un 404, para un certificado inválido y para un stream
corrupto. Sin clasificar, la app dice «se cerró al instante» y el usuario no
tiene nada que hacer con esa frase. Estos tests comprueban que la clasificación
separa los casos que el §20 enumera, y que el §14 mantiene separados.
"""

from __future__ import annotations

import unittest

from thetvview.player.errors import (
    StreamError,
    StreamErrorCode,
    classify_message,
    classify_returncode,
    explain,
)
from thetvview.security.errors import IPTVError


class TestTaxonomia(unittest.TestCase):
    def test_los_codigos_del_sdd20_existen(self) -> None:
        # El §20 los enumera; si la app no puede distinguirlos, no cumple.
        for nombre in (
            "INVALID_URL",
            "UNSUPPORTED_PROTOCOL",
            "CONNECTION_FAILED",
            "CONNECTION_TIMEOUT",
            "AUTH_FAILED",
            "NOT_FOUND",
            "SERVER_ERROR",
            "DECODE_ERROR",
            "NETWORK_ERROR",
            "BUFFERING_TIMEOUT",
            "BACKEND_ERROR",
            "UNKNOWN",
        ):
            with self.subTest(codigo=nombre):
                self.assertTrue(hasattr(StreamErrorCode, nombre))
        # TLS no está en el §20 pero sí es un caso real de cámara IP, así que
        # se añade en vez de dejar que caiga en UNKNOWN.
        self.assertTrue(hasattr(StreamErrorCode, "TLS_FAILED"))

    def test_es_un_error_del_dominio(self) -> None:
        # La UI captura la raíz `IPTVError`: sin esto haría falta
        # `except Exception`.
        self.assertTrue(issubclass(StreamError, IPTVError))


class TestClasificacionPorTexto(unittest.TestCase):
    """El texto dice más que el código: los tres binarios usan 1 para todo."""

    def test_404_de_los_tres_binarios(self) -> None:
        for texto in (
            "Failed to open 'http://x/a.m3u8': 404 Not Found",
            "[rtmp @ 0x1] Server returned 404 (Not Found)",
            "VLC es incapaz de abrir el MRL: cannot be opened (404)",
        ):
            with self.subTest(texto=texto):
                self.assertIs(classify_message(texto), StreamErrorCode.NOT_FOUND)

    def test_401_y_403_son_credenciales(self) -> None:
        for texto in ("401 Unauthorized", "HTTP/1.1 403 Forbidden", "access denied"):
            with self.subTest(texto=texto):
                self.assertIs(classify_message(texto), StreamErrorCode.AUTH_FAILED)

    def test_certificado_invalido(self) -> None:
        for texto in (
            "SSL: certificate verify failed",
            "TLS handshake failed",
            "certificate has expired",
        ):
            with self.subTest(texto=texto):
                self.assertIs(classify_message(texto), StreamErrorCode.TLS_FAILED)

    def test_protocolo_no_soportado(self) -> None:
        # Es el mensaje de un reproductor al recibir algo que no habla. La app
        # no lo lanza hoy (el router filtra antes), pero el texto existe y hay
        # que saber interpretarlo si llega.
        self.assertIs(
            classify_message("Protocol not found"),
            StreamErrorCode.UNSUPPORTED_PROTOCOL,
        )

    def test_rechazado_por_red_no_por_protocolo(self) -> None:
        # §14: el caso que más se confunde. «Connection refused» sobre un
        # RTSP significa cámara apagada o firewall, **no** «no lo sé abrir».
        for texto in ("Connection refused", "No route to host", "network unreachable"):
            with self.subTest(texto=texto):
                self.assertIs(classify_message(texto), StreamErrorCode.NETWORK_ERROR)

    def test_timeout(self) -> None:
        self.assertIs(
            classify_message("Connection timed out"), StreamErrorCode.CONNECTION_TIMEOUT
        )

    def test_timeout_gana_a_conexion_generica(self) -> None:
        # El texto de FFmpeg dice «Connection timed out» y también «failed»:
        # el orden del clasificador pone lo específico antes que lo genérico.
        self.assertIs(
            classify_message("Failed to open: connection timed out after 30s"),
            StreamErrorCode.CONNECTION_TIMEOUT,
        )

    def test_datos_corruptos(self) -> None:
        self.assertIs(
            classify_message("Invalid data found when processing input"),
            StreamErrorCode.DECODE_ERROR,
        )

    def test_texto_vacio_no_inventa(self) -> None:
        self.assertIsNone(classify_message(""))
        self.assertIsNone(classify_message("   "))


class TestClasificacionPorCodigo(unittest.TestCase):
    def test_returncode_1_sin_texto_es_desconocido(self) -> None:
        # Preferimos «no se pudo reproducir» a un diagnóstico inventado.
        self.assertIs(
            classify_returncode(1, ""), StreamErrorCode.UNKNOWN
        )

    def test_126_y_127_son_binario_ausente(self) -> None:
        self.assertIs(classify_returncode(127, ""), StreamErrorCode.PLAYER_MISSING)
        self.assertIs(classify_returncode(126, ""), StreamErrorCode.PLAYER_MISSING)

    def test_el_texto_manda_sobre_el_codigo(self) -> None:
        # 1 + «404 Not Found» → 404, no «desconocido».
        self.assertIs(
            classify_returncode(1, "Server returned 404 Not Found"),
            StreamErrorCode.NOT_FOUND,
        )

    def test_none_no_significa_nada(self) -> None:
        self.assertIs(classify_returncode(None, ""), StreamErrorCode.UNKNOWN)


class TestMensajes(unittest.TestCase):
    def test_el_error_siempre_dice_algo(self) -> None:
        for codigo in StreamErrorCode:
            with self.subTest(codigo=codigo):
                err = StreamError(code=codigo, resumen="")
                self.assertTrue(str(err))

    def test_el_consejo_dice_que_revisar(self) -> None:
        # Un error sin consejo deja al usuario sin nada que hacer, que es lo
        # que §20 quiere evitar.
        err = explain(1, "Server returned 404 Not Found")
        self.assertEqual(err.code, StreamErrorCode.NOT_FOUND)
        self.assertIn("otro canal", str(err))

    def test_protocolo_no_soportado_dice_que_no_es_el_canal(self) -> None:
        # El §14: «el backend no abre el protocolo» es un problema distinto de
        # «el protocolo funciona y la red falló», y el consejo también.
        err = explain(
            1,
            "Protocol not found",
            transporte_nombre="RTSP",
        )
        self.assertIn("RTSP", str(err))
        self.assertIn("no es un fallo del canal", str(err).lower())

    def test_red_dice_revisar_la_red(self) -> None:
        err = explain(1, "Connection refused")
        self.assertIn("red", str(err))

    def test_para_modal_no_añade_nada(self) -> None:
        err = explain(1, "404")
        self.assertEqual(err.para_modal(), str(err))

    def test_el_detalle_se_conserva_para_el_informe(self) -> None:
        err = explain(1, "404 Not Found")
        self.assertEqual(err.detalle, "404 Not Found")