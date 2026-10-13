"""Salud del canal: los cuatro estados lógicos y sus límites (FR-STA-01…10).

La capa que se comprueba aquí es **nueva y es pequeña**: `classify_health`
ya existía (nivel 0..5, barra, colores) y sus pruebas también. Lo que no
existía era la traducción a los cuatro estados que la UI puede decir en
palabras, y ahí está el riesgo real:

- que un canal **nunca comprobado** aparezca como online u offline
  (afirmaría algo que nadie sabe, FR-STA-02);
- que pintar el estado dispare **sondas nuevas** (una petición de red por
  repintado sería la peor forma de decorar la interfaz, T-STA-04);
- que el estado se comunicara **sólo con color** (NFR-06).

Sin red: todas las sondas son falsas.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from thetvview.channel_health import (
    ChannelHealthMonitor,
    DimensionEstado,
    Estado,
    HealthSnapshot,
    HealthStatus,
    classify_health,
    estado_de,
    fuente_de_mimetipo,
    health_label,
)
from thetvview.models import Channel
from thetvview.ui import colors
from thetvview.ui.widgets import estado_attr, estado_badge, estado_badge_text


def _estado(level: int, *, sondas: int = 1, medible: bool = True) -> HealthStatus:
    return HealthStatus(
        level=level,
        label="x",
        bar="○○○○○",
        color_pair=colors.PAIR_SUCCESS,
        summary="",
        detail="",
        success_rate=1.0,
        avg_latency_ms=None,
        last_latency_ms=None,
        jitter_ms=None,
        probe_count=sondas,
        success_count=sondas,
        medible=medible,
    )


def _snap(success: bool | None, latencia: float = 100.0) -> HealthSnapshot:
    ahora = datetime.now(timezone.utc)
    return HealthSnapshot(ahora, latencia, success, 200 if success else None, None)


class VentanaCaptura:
    def __init__(self, max_y: int = 24, max_x: int = 80) -> None:
        self.max_y = max_y
        self.max_x = max_x
        self.calls: list[tuple[int, int, str, int]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return (self.max_y, self.max_x)

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        assert 0 <= y < self.max_y
        assert 0 <= x < self.max_x
        self.calls.append((y, x, text, attr))

    def texto(self) -> str:
        return " ".join(c[2] for c in self.calls)


# ---------------------------------------------------------------------------
# 1. El mapeo de los seis niveles a los cuatro estados (FR-STA-01)
# ---------------------------------------------------------------------------


class TestMapeoDeEstados(unittest.TestCase):
    def test_los_seis_niveles_cubren_los_cuatro_estados(self):
        esperado = {
            5: Estado.ONLINE,
            4: Estado.ONLINE,
            3: Estado.AVISO,
            2: Estado.AVISO,
            1: Estado.OFFLINE,
            0: Estado.OFFLINE,
        }
        for nivel, estado in esperado.items():
            with self.subTest(nivel=nivel):
                self.assertIs(estado_de(_estado(nivel)), estado)

    def test_sin_sondas_es_desconocido(self):
        """T-STA-01: nunca comprobado no es online ni offline."""
        self.assertIs(estado_de(_estado(0, sondas=0)), Estado.DESCONOCIDO)
        # Aunque el nivel sea bueno: sin sondas no hay con quién comparar.
        self.assertIs(estado_de(_estado(5, sondas=0)), Estado.DESCONOCIDO)

    def test_sin_status_es_desconocido(self):
        self.assertIs(estado_de(None), Estado.DESCONOCIDO)

    def test_canal_no_medible_es_desconocido(self):
        """Un rtmp:// no se puede comprobar: no se pinta como caído."""
        self.assertIs(estado_de(_estado(3, medible=False)), Estado.DESCONOCIDO)

    def test_status_ilegible_no_lanza(self):
        self.assertIs(estado_de(object()), Estado.DESCONOCIDO)

    def test_reproductor_parado_es_offline(self):
        """Con Measurement y sin proceso, la reproducción no está: es OFFLINE."""
        status = classify_health([_snap(True)], player_alive=False)
        self.assertIs(estado_de(status), Estado.OFFLINE)

    def test_sin_sondas_la_etiqueta_no_dice_sin_senal(self):
        status = classify_health([], player_alive=True)
        self.assertNotIn("señal", status.label.lower())
        self.assertIs(estado_de(status), Estado.DESCONOCIDO)

    def test_error_temporal_es_aviso(self):
        """T-STA-03: un fallo aislado avisa, no declara muerto el canal."""
        status = classify_health([_snap(True), _snap(False)], player_alive=True)
        self.assertIs(estado_de(status), Estado.AVISO)

    def test_todo_fallando_es_offline(self):
        status = classify_health([_snap(False), _snap(False)], player_alive=True)
        self.assertIs(estado_de(status), Estado.OFFLINE)

    def test_protocolo_no_http_no_se_declara(self):
        status = classify_health([_snap(None)], player_alive=True)
        self.assertIs(estado_de(status), Estado.DESCONOCIDO)

    def test_health_label_sigue_siendo_una_funcion_del_nivel(self):
        self.assertEqual(health_label(5), "Excelente")
        self.assertEqual(health_label(0), "Sin señal")


# ---------------------------------------------------------------------------
# 2. Pintar el estado no abre la red (FR-STA-04/05/06, T-STA-04)
# ---------------------------------------------------------------------------


class TestSinSondasNuevas(unittest.TestCase):
    def test_repintados_no_disparan_sondas(self):
        canal = Channel(name="La 1", url="http://a.test/1.m3u8")
        monitor = ChannelHealthMonitor(canal, auto_start=False)
        with mock.patch(
            "thetvview.channel_health.probe_channel_url",
            return_value=_snap(True),
        ) as sonda:
            for _ in range(5):
                monitor.status(player_alive=True)
            self.assertEqual(sonda.call_count, 0,
                             "pintar el estado no puede abrir la red")

    def test_una_sonda_explicita_si_cuenta(self):
        canal = Channel(name="La 1", url="http://a.test/1.m3u8")
        monitor = ChannelHealthMonitor(canal, auto_start=False)
        with mock.patch(
            "thetvview.channel_health.probe_channel_url",
            return_value=_snap(True),
        ) as sonda:
            monitor.probe_once()
            sonda.assert_called_once()


# ---------------------------------------------------------------------------
# 3. El badge: con palabra y con reserva ASCII (FR-STA-03/09, NFR-06)
# ---------------------------------------------------------------------------


class TestBadgeDeEstado(unittest.TestCase):
    def test_cada_estado_tiene_glifo_y_palabra(self):
        for estado in Estado:
            with self.subTest(estado=estado):
                texto = estado_badge_text(estado)
                self.assertTrue(any(c.isalpha() for c in texto),
                                "el estado debe nombrarse, no sólo pintarse")

    def test_no_usa_solo_color(self):
        """El texto es lo que comunica; el color sólo lo acompaña."""
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            for i, estado in enumerate(Estado):
                estado_badge(ventana, 1 + i, 0, estado, 40)
        texto = ventana.texto()
        for palabra in ("En línea", "Con avisos", "Sin conexión", "Sin comprobar"):
            self.assertIn(palabra, texto)
        # Además, el atributo no es lo único que se pinta: hay texto.
        self.assertTrue(all(len(c[2].strip()) > 0 for c in ventana.calls))

    def test_el_atributo_incluye_negrita(self):
        """En una terminal de dos colores la negrita es lo que distingue."""
        import curses

        with mock.patch.object(colors, "pair", return_value=0):
            for estado in Estado:
                self.assertTrue(estado_attr(estado) & curses.A_BOLD)

    def test_reserva_ascii_no_usa_unicode(self):
        for estado in Estado:
            texto = estado_badge_text(estado, ascii_only=True)
            self.assertTrue(all(ord(c) < 128 for c in texto), texto)
            self.assertTrue(any(c.isalpha() for c in texto))

    def test_estado_desconocido_es_el_que_se_pinta_por_defecto(self):
        self.assertIn("Sin comprobar", estado_badge_text("inventado"))

    def test_badge_roto_por_anchura(self):
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            estado_badge(ventana, 1, 0, Estado.ONLINE, 0)    # sin espacio
            estado_badge(ventana, 1, 0, Estado.ONLINE, -5)   # ancho imposible
            estado_badge(ventana, 999, 0, Estado.ONLINE, 40)  # fila fuera
        self.assertEqual(ventana.calls, [])

    def test_badge_recortado_no_desborda(self):
        ventana = VentanaCaptura()
        with mock.patch.object(colors, "pair", return_value=0):
            estado_badge(ventana, 1, 0, Estado.DESCONOCIDO, 4)
        self.assertTrue(all(len(c[2]) <= 4 for c in ventana.calls))


# ---------------------------------------------------------------------------
# 4. Las tres dimensiones del §11.1 (DIAG-003)
# ---------------------------------------------------------------------------


def _snap_ctype(
    success: bool | None = True, content_type: str | None = None
) -> HealthSnapshot:
    ahora = datetime.now(timezone.utc)
    return HealthSnapshot(
        ahora,
        100.0,
        success,
        200 if success else None,
        None,
        content_type=content_type,
    )


class TestDimensionesDeSalud(unittest.TestCase):
    """§11.1: tres dimensiones independientes; el nivel se deriva de ellas."""

    def test_servidor_ok_con_fuente_invalida_no_es_funcional(self):
        """El 200 con HTML no es reproducción: cae a «Mala», no a «Excelente»."""
        snaps = [_snap_ctype(True, "text/html; charset=utf-8")]
        status = classify_health(snaps, player_alive=True)
        self.assertIs(status.red, DimensionEstado.OK)
        self.assertIs(status.fuente, DimensionEstado.FALLA)
        self.assertLessEqual(status.level, 1)
        self.assertIs(estado_de(status), Estado.OFFLINE)
        # Y lo dice con palabras, no sólo con el número.
        self.assertIn("no parece reproducible", status.detail)

    def test_fuente_degradada_techoa_a_regular(self):
        status = classify_health(
            [_snap_ctype(True)], player_alive=True, fuente=DimensionEstado.DEGRADADO
        )
        self.assertLessEqual(status.level, 3)
        self.assertIn("no es el esperado", status.detail)

    def test_fuente_declarada_manda_sobre_el_mime(self):
        """Quien ya analizó el manifiesto sabe más que la cabecera."""
        status = classify_health(
            [_snap_ctype(True, "application/vnd.apple.mpegurl")],
            player_alive=True,
            fuente=DimensionEstado.FALLA,
        )
        self.assertIs(status.fuente, DimensionEstado.FALLA)

    def test_reproduccion_sin_medios_es_desconocida(self):
        """Proceso vivo sin confirmar medios no es «ok» (FR-REC-002)."""
        from thetvview.player.evidence import EvidenceLevel

        status = classify_health(
            [_snap_ctype(True)], player_alive=True, evidencia=EvidenceLevel.PROCESS_ALIVE
        )
        self.assertIs(status.red, DimensionEstado.OK)
        self.assertIs(status.reproduccion, DimensionEstado.DESCONOCIDO)

    def test_reproduccion_con_medios_es_ok(self):
        from thetvview.player.evidence import EvidenceLevel

        status = classify_health(
            [_snap_ctype(True)], player_alive=True, evidencia=EvidenceLevel.MEDIA_SEEN
        )
        self.assertIs(status.reproduccion, DimensionEstado.OK)

    def test_reproductor_parado_es_falla_de_reproduccion(self):
        status = classify_health([_snap_ctype(True)], player_alive=False)
        self.assertIs(status.reproduccion, DimensionEstado.FALLA)

    def test_sin_sondas_las_dimensiones_no_afirman(self):
        status = classify_health([], player_alive=True)
        self.assertIs(status.red, DimensionEstado.DESCONOCIDO)
        self.assertIs(status.fuente, DimensionEstado.DESCONOCIDO)
        self.assertIs(status.reproduccion, DimensionEstado.DESCONOCIDO)

    def test_red_en_falla_es_nivel_cero(self):
        snaps = [_snap_ctype(False, "application/vnd.apple.mpegurl")] * 3
        status = classify_health(snaps, player_alive=True)
        self.assertIs(status.red, DimensionEstado.FALLA)
        self.assertEqual(status.level, 0)

    def test_no_medible_no_se_puntua_como_falla(self):
        """Un rtmp:// no cae por dimensión ninguna: no se ha medido nada."""
        status = classify_health([_snap_ctype(None)], player_alive=True)
        self.assertIs(status.red, DimensionEstado.DESCONOCIDO)
        self.assertEqual(status.level, 3)

    def test_las_dimensiones_son_aditivas(self):
        """Los campos que la UI lee de siempre siguen intactos."""
        status = classify_health(
            [_snap_ctype(True, "video/mp2t")], player_alive=True
        )
        self.assertGreaterEqual(status.level, 4)
        self.assertTrue(status.label)
        self.assertTrue(status.bar)
        self.assertTrue(status.summary)

    def test_fuente_de_mimetipo(self):
        casos = [
            ("application/vnd.apple.mpegurl", DimensionEstado.OK),
            ("application/dash+xml; charset=utf-8", DimensionEstado.OK),
            ("video/mp2t", DimensionEstado.OK),
            ("text/html", DimensionEstado.FALLA),
            ("application/json", DimensionEstado.FALLA),
            ("text/plain", DimensionEstado.DESCONOCIDO),
            ("", DimensionEstado.DESCONOCIDO),
            (None, DimensionEstado.DESCONOCIDO),
        ]
        for mime, esperado in casos:
            with self.subTest(mime=mime):
                self.assertIs(fuente_de_mimetipo(mime), esperado)


if __name__ == "__main__":
    unittest.main()