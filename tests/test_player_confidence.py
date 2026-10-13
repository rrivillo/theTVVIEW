"""Tests de la matriz de capacidades con niveles de confianza (Fase 5).

Cubre FR-CAP-001 (la dimensión nueva de contenedores y códecs) y FR-CAP-002
(el nivel ``DECLARED``/``DETECTED``/``VERIFIED`` de cada capacidad), más la
regla que el plan marca como innegociable: **nada ``VERIFIED`` sin una prueba
reproducible enlazada**. Todo es puro: aquí no se lanza ni un binario.
"""

from __future__ import annotations

import unittest
from unittest import mock

from thetvview import config
from thetvview.player import (
    Confidence,
    PLAYER_MEDIA_SUPPORT,
    PlayerProtocolSupport,
    PlayerTrackSupport,
    confianza_media,
    media_support_for,
    normalizar_codec,
    normalizar_contenedor,
    soporta,
)
from thetvview.player import confidence as conf_mod
from thetvview.player.capabilities import KINDS, PLAYER_TRACK_SUPPORT
from thetvview.player.protocols import PLAYER_PROTOCOL_SUPPORT


class TestReglaDeConfianza(unittest.TestCase):
    """FR-CAP-002: tres niveles, y el tercero sólo con evidencia."""

    def test_los_tres_niveles_del_sdd_en_el_orden(self) -> None:
        self.assertEqual(
            [c.value for c in Confidence],
            ["DECLARED", "DETECTED", "VERIFIED"],
        )
        self.assertLess(Confidence.DECLARED.orden, Confidence.DETECTED.orden)
        self.assertLess(Confidence.DETECTED.orden, Confidence.VERIFIED.orden)

    def test_declarado_detectado_segun_la_medicion(self) -> None:
        self.assertEqual(conf_mod.nivel_confianza(False), Confidence.DECLARED)
        self.assertEqual(conf_mod.nivel_confianza(True), Confidence.DETECTED)

    def test_verified_exige_evidencia_enlazada(self) -> None:
        # Sin clave no hay forma de llegar a VERIFIED…
        self.assertEqual(conf_mod.nivel_confianza(True), Confidence.DETECTED)
        # …y con clave pero fuera del registro, tampoco.
        self.assertEqual(
            conf_mod.nivel_confianza(True, "mpv:http"), Confidence.DETECTED
        )
        with mock.patch.dict(
            conf_mod.EVIDENCIAS_VERIFICADAS,
            {"mpv:http": "tests.test_player_confidence"},
        ):
            self.assertEqual(
                conf_mod.nivel_confianza(True, "mpv:http"), Confidence.VERIFIED
            )
            self.assertEqual(
                conf_mod.evidencia("mpv:http"), "tests.test_player_confidence"
            )
        # Fuera del patch, el registro vuelve a estar vacío.
        self.assertEqual(conf_mod.EVIDENCIAS_VERIFICADAS, {})

    def test_una_evidencia_rota_se_detecta_no_se_calla(self) -> None:
        with mock.patch.dict(
            conf_mod.EVIDENCIAS_VERIFICADAS,
            {
                "mpv:http": "tests.test_player_confidence",  # existe
                "mpv:udp": "tests.test_no_existe",  # módulo fantasma
                "vlc:rtsp": "tests.test_player_confidence::test_que_no_hay",  # attr
            },
        ):
            invalidas = conf_mod.referencias_invalidas()
        self.assertEqual(invalidas, ["mpv:udp", "vlc:rtsp"])

    def test_clave_de_normaliza_jugador_y_capacidad(self) -> None:
        self.assertEqual(conf_mod.clave_de(" MPV ", "HTTP"), "mpv:http")


class TestConfianzaEnProtocolos(unittest.TestCase):
    """Las filas de protocols.py: todo lo de su ``medido`` es DETECTED."""

    def test_lo_medido_es_detected_y_lo_ausente_es_none(self) -> None:
        for nombre, fila in PLAYER_PROTOCOL_SUPPORT.items():
            for transporte in fila.transports:
                with self.subTest(reproductor=nombre, transporte=transporte):
                    esperado = (
                        Confidence.DETECTED
                        if fila.medido and transporte in fila.medido
                        else Confidence.DECLARED
                    )
                    self.assertEqual(fila.confianza(transporte), esperado)

    def test_ningun_transporte_de_la_tabla_esta_verified(self) -> None:
        # La medición fue manual (2026-10-04), no un test reproducible:
        # por eso nada es VERIFIED. Si algún día cambia, la evidencia
        # enlazada tiene que existir antes que el nivel.
        for nombre, fila in PLAYER_PROTOCOL_SUPPORT.items():
            for transporte in fila.transports:
                with self.subTest(reproductor=nombre, transporte=transporte):
                    nivel = fila.confianza(transporte)
                    self.assertIsNot(nivel, Confidence.VERIFIED)
                    self.assertIsNotNone(nivel)

    def test_sin_afirmacion_no_hay_confianza(self) -> None:
        # rtsps no está en ninguna tabla (no se pudo medir) y un texto
        # raro no es un transporte: None = «no afirmamos nada».
        for fila in PLAYER_PROTOCOL_SUPPORT.values():
            self.assertIsNone(fila.confianza("rtsps"))
            self.assertIsNone(fila.confianza("esto-no-es-una-url"))

    def test_medido_none_es_declared_pero_can_play_lo_ofrezca(self) -> None:
        fila = PlayerProtocolSupport(
            name="x", transports=frozenset({"http"}), medido=None
        )
        self.assertTrue(fila.can_play("http"))  # compatibilidad: sigue ofreciéndose
        self.assertEqual(fila.confianza("http"), Confidence.DECLARED)


class TestConfianzaEnPistas(unittest.TestCase):
    """Las filas de capabilities.py: medidas el 2026-10-02."""

    def test_los_kinds_de_cada_fila_son_detected(self) -> None:
        for nombre, fila in PLAYER_TRACK_SUPPORT.items():
            for kind in KINDS:
                with self.subTest(reproductor=nombre, kind=kind):
                    self.assertEqual(
                        fila.confianza(kind), Confidence.DETECTED
                    )

    def test_un_kind_fuera_de_la_tabla_no_esta_afirmado(self) -> None:
        fila = PLAYER_TRACK_SUPPORT["mpv"]
        self.assertIsNone(fila.confianza("vidrio"))
        self.assertIsNone(fila.confianza(""))

    def test_fila_sin_medir_es_declared(self) -> None:
        fila = PlayerTrackSupport(
            name="nuevo",
            audio_by_index=True,
            subtitles_by_index=False,
            quality=False,
            audio_by_language=False,
            subtitles_by_language=False,
            hot_control=False,
        )
        self.assertEqual(fila.confianza("audio"), Confidence.DECLARED)

    def test_verified_llega_con_su_evidencia(self) -> None:
        with mock.patch.dict(
            conf_mod.EVIDENCIAS_VERIFICADAS, {"mpv:audio": "tests.test_x"}
        ):
            self.assertEqual(
                PLAYER_TRACK_SUPPORT["mpv"].confianza("audio"),
                Confidence.VERIFIED,
            )


class TestDimensionNuevosContenedoresCodecs(unittest.TestCase):
    """FR-CAP-001: la dimensión nueva, sólo con lo medido."""

    def test_los_tres_reproductores_tienen_fila(self) -> None:
        for nombre in config.SUPPORTED_PLAYERS:
            with self.subTest(reproductor=nombre):
                self.assertIn(nombre, PLAYER_MEDIA_SUPPORT)
                fila = PLAYER_MEDIA_SUPPORT[nombre]
                self.assertEqual(fila.containers, frozenset({"mpegts"}))
                self.assertEqual(fila.codecs_video, frozenset({"h264"}))
                self.assertEqual(fila.codecs_audio, frozenset({"aac"}))

    def test_lo_medido_es_detected(self) -> None:
        for clave in ("mpegts", ".ts", "h264", "avc1.4d401f", "aac", "mp4a.40.2"):
            with self.subTest(clave=clave):
                self.assertEqual(
                    confianza_media("mpv", clave), Confidence.DETECTED
                )

    def test_lo_no_medido_no_se_ofrece(self) -> None:
        # MP4, HEVC y Opus no se midieron aquí: ni se afirma ni se
        # promete («la app no ofrece lo que no ha comprobado»).
        for clave in ("mp4", ".mp4", "hevc", "hvc1.1.6.L120.90", "opus"):
            with self.subTest(clave=clave):
                self.assertFalse(soporta("mpv", clave))
                self.assertIsNone(confianza_media("mpv", clave))

    def test_reproductor_desconocido_no_afirma_nada(self) -> None:
        self.assertIsNone(media_support_for("otro"))
        self.assertIsNone(confianza_media("otro", "mpegts"))
        self.assertFalse(soporta("otro", "mpegts"))
        self.assertFalse(soporta(None, "mpegts"))

    def test_normalizar_codec_como_en_el_hls(self) -> None:
        self.assertEqual(normalizar_codec("avc1.4d401f"), "h264")
        self.assertEqual(normalizar_codec("avc1.640028"), "h264")
        self.assertEqual(normalizar_codec("MP4A.40.2"), "aac")
        self.assertEqual(normalizar_codec("h264"), "h264")
        self.assertEqual(normalizar_codec("wvtt"), "wvtt")
        # Lo desconocido vuelve en minúsculas: pregunta válida, respuesta
        # «no está en la tabla», nunca un KeyError.
        self.assertEqual(normalizar_codec("xx9.zzz"), "xx9")
        self.assertEqual(normalizar_codec(None), "")

    def test_normalizar_contenedor(self) -> None:
        self.assertEqual(normalizar_contenedor(".ts"), "mpegts")
        self.assertEqual(normalizar_contenedor("TS"), "mpegts")
        self.assertEqual(normalizar_contenedor("mpegts"), "mpegts")
        self.assertEqual(normalizar_contenedor("mp4"), "mp4")
        self.assertEqual(normalizar_contenedor(None), "")


class TestInvarianteSinVerifiedSinPrueba(unittest.TestCase):
    """La regla innegociable del plan, sobre las tres tablas."""

    def test_nada_es_verified_sin_evidencia_que_resuelva(self) -> None:
        self.assertEqual(conf_mod.referencias_invalidas(), [])
        for nombre, fila in PLAYER_PROTOCOL_SUPPORT.items():
            for transporte in fila.transports:
                nivel = fila.confianza(transporte)
                if nivel is Confidence.VERIFIED:
                    self.assertIsNotNone(
                        conf_mod.evidencia(conf_mod.clave_de(nombre, transporte))
                    )
        for nombre, fila in PLAYER_TRACK_SUPPORT.items():
            for kind in KINDS:
                if fila.confianza(kind) is Confidence.VERIFIED:
                    self.assertIsNotNone(
                        conf_mod.evidencia(conf_mod.clave_de(nombre, kind))
                    )
        for nombre, fila in PLAYER_MEDIA_SUPPORT.items():
            for clave in fila.claves:
                if fila.confianza(clave) is Confidence.VERIFIED:
                    self.assertIsNotNone(
                        conf_mod.evidencia(conf_mod.clave_de(nombre, clave))
                    )

    def test_hoy_nada_esta_verified(self) -> None:
        # Documenta el estado real: la medición fue manual, no un test.
        self.assertEqual(conf_mod.EVIDENCIAS_VERIFICADAS, {})


if __name__ == "__main__":
    unittest.main()
