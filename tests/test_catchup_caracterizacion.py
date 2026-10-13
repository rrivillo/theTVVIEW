"""Caracterización del catch-up **actual** (AUD-02, Fase 0 del plan P2).

Estas pruebas congelan el comportamiento que existe **hoy** en
`thetvview/catchup.py`, antes de que la Fase 1 (P2.5-A catch-up robusto) añada
`validar_intervalo()`, límites de duración máxima, límites por parámetro de
plantilla y estados de indisponibilidad separados.

El foco está en los tres casos que el plan declara rotos o dangerous:

- **intervalo invertido** (fin anterior al inicio): `duration_seconds` usa
  ``max(1, …)``, así que se **normaliza en silencio** y se construye una URL de
  1 segundo que el proveedor nunca pidió;
- **inicio igual a fin**: mismo camino, misma normalización;
- **plantilla de terceros**: ya hay lista blanca, pero sin límites por valor.

Ninguno se "arregla" aquí: se congela y se documenta en
`docs/sdd/p2-gap-analysis.md`. El arreglo pertenece a CU-02.

Reglas de esta fase: sin red, sin reproductor, sin `sleep`; reloj fijo por
construcción (`NOW` es una constante) para que los resultados sean deterministas.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from thetvview import catchup
from thetvview.catchup import (
    CatchupCapability,
    CatchupError,
    CatchupRef,
    CatchupRequest,
    CatchupState,
    build_playback_request,
    capability_for,
    classify_request,
    is_within_archive_window,
    render_source_template,
)
from thetvview.models import Channel

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
DIA = 86400

PLANTILLA = (
    "http://a.test/streaming/timeshift.php?username=u&password=p"
    "&stream=101&start={start}&duration={duration}"
)


def cap_m3u(**extra) -> CatchupCapability:
    base = {
        "enabled": True,
        "provider_declared": True,
        "archive_duration_seconds": 7 * DIA,
        "protocol": catchup.PROTOCOL_M3U,
        "source_template": PLANTILLA,
    }
    base.update(extra)
    return CatchupCapability(**base)


def canal_m3u(**attrs) -> Channel:
    base = {
        "catchup": "shift",
        "catchup_days": "7",
        "catchup_source": PLANTILLA,
    }
    base.update(attrs)
    return Channel(name="Canal", url="http://a.test/live/101", attrs=base)


def req(inicio: datetime, fin: datetime) -> CatchupRequest:
    return CatchupRequest(channel_id="101", start=inicio, end=fin)


# ---------------------------------------------------------------------------
# 1. Intervalos: el fallo silencioso que la Fase 1 cierra
# ---------------------------------------------------------------------------


class TestIntervaloInvertido(unittest.TestCase):
    """Fin anterior al inicio: hoy se normaliza a 1 s sin avisar."""

    def setUp(self) -> None:
        self.invertida = req(NOW - timedelta(hours=3), NOW - timedelta(hours=4))

    def test_la_duracion_se_normaliza_a_un_segundo(self):
        self.assertEqual(self.invertida.duration_seconds, 1)

    def test_se_considera_dentro_de_la_ventana(self):
        cap = cap_m3u()
        self.assertTrue(is_within_archive_window(self.invertida.to_program(), cap, NOW))
        self.assertIs(classify_request(cap, self.invertida, NOW), CatchupState.AVAILABLE)

    def test_se_construye_la_url_sin_error(self):
        canal = canal_m3u()
        pedido = build_playback_request(
            canal, capability_for(canal), self.invertida, now=NOW
        )
        self.assertEqual(pedido.duration, 1)
        # El proveedor recibe un intervalo de 1 s que el usuario nunca pidió.
        self.assertIn("duration=1", pedido.url)
        self.assertIn(f"start={int(self.invertida.start.timestamp())}", pedido.url)

    def test_inicio_igual_a_fin_tambien_se_normaliza(self):
        igual = req(NOW - timedelta(hours=1), NOW - timedelta(hours=1))
        self.assertEqual(igual.duration_seconds, 1)
        self.assertIs(
            classify_request(cap_m3u(), igual, NOW), CatchupState.AVAILABLE
        )

    def test_la_redaccion_no_tapa_el_intervalo_invertido(self):
        """No hay ningún camino que rechace el intervalo: `describe_state` sólo
        habla de ventana, y un intervalo invertido **sí** está en la ventana."""
        cap = cap_m3u()
        estado = classify_request(cap, self.invertida, NOW)
        self.assertIs(estado, CatchupState.AVAILABLE)
        self.assertEqual(
            catchup.describe_state(estado, cap),
            "Estado de archivo del proveedor sin cambios.",
        )

    def test_no_hay_duracion_maxima_por_ningun_lado(self):
        """Congelado: una petición de 400 días se acepta si la ventana alcanza."""
        enorme = req(NOW - timedelta(days=400), NOW)
        cap = cap_m3u(archive_duration_seconds=500 * DIA)
        self.assertEqual(enorme.duration_seconds, 400 * DIA)
        self.assertIs(classify_request(cap, enorme, NOW), CatchupState.AVAILABLE)


# ---------------------------------------------------------------------------
# 2. Duraciones válidas
# ---------------------------------------------------------------------------


class TestDuraciones(unittest.TestCase):
    def test_una_hora_exacta(self):
        self.assertEqual(
            req(NOW - timedelta(hours=1), NOW).duration_seconds, 3600
        )

    def test_el_redondeo_es_al_segundo(self):
        self.assertEqual(
            req(NOW - timedelta(seconds=90, microseconds=400_000), NOW).duration_seconds,
            90,
        )

    def test_menos_de_un_segundo_también_es_uno(self):
        self.assertEqual(
            req(NOW - timedelta(milliseconds=10), NOW).duration_seconds, 1
        )

    def test_naive_y_aware_dan_el_mismo_resultado(self):
        naive = CatchupRequest(
            channel_id="101",
            start=(NOW - timedelta(hours=2)).replace(tzinfo=None),
            end=NOW.replace(tzinfo=None),
        )
        self.assertEqual(naive.duration_seconds, 7200)

    def test_otro_rhuso_temporal_no_cambia_el_segundo(self):
        """Se usa UTC para el epoch: dos zonas del mismo instante coinciden."""
        en_madrid = req(
            (NOW - timedelta(hours=1)).astimezone(timezone(timedelta(hours=2))), NOW
        )
        self.assertEqual(en_madrid.duration_seconds, 3600)


# ---------------------------------------------------------------------------
# 3. Ventana de archivo
# ---------------------------------------------------------------------------


class TestVentanaDeArchivo(unittest.TestCase):
    def test_dentro_de_la_ventana(self):
        cap = cap_m3u()
        dentro = req(NOW - timedelta(hours=2), NOW - timedelta(hours=1))
        self.assertIs(classify_request(cap, dentro, NOW), CatchupState.AVAILABLE)

    def test_fuera_de_la_ventana(self):
        cap = cap_m3u(archive_duration_seconds=DIA)
        viejo = req(NOW - timedelta(days=3), NOW - timedelta(days=2))
        self.assertIs(
            classify_request(cap, viejo, NOW), CatchupState.OUTSIDE_ARCHIVE_WINDOW
        )

    def test_sin_declaracion_no_hay_archivo_aunque_la_plantilla_exista(self):
        cap = catchup.DISABLED
        peticion = req(NOW - timedelta(hours=1), NOW)
        self.assertIs(classify_request(cap, peticion, NOW), CatchupState.DISABLED_BY_PROVIDER)
        with self.assertRaises(catchup.CatchupNotAvailableError):
            build_playback_request(canal_m3u(), cap, peticion, now=NOW)

    def test_una_fuente_sin_catchup_sigue_teniendo_directo(self):
        canal = Channel(name="Canal", url="http://a.test/live/1")
        cap = capability_for(canal)
        self.assertFalse(catchup.can_use_catchup(cap))
        self.assertEqual(canal.url, "http://a.test/live/1")

    def test_no_se_adivina_catchup_por_el_nombre(self):
        canal = Channel(name="Canal Timeshift", url="http://a.test/live/1")
        self.assertIs(
            classify_request(capability_for(canal), req(NOW - timedelta(hours=1), NOW), NOW),
            CatchupState.DISABLED_BY_PROVIDER,
        )


# ---------------------------------------------------------------------------
# 4. Plantillas: lo que ya está y lo que no
# ---------------------------------------------------------------------------


class TestPlantillaDeTerceros(unittest.TestCase):
    """La lista blanca ya existe (CU-03 la endurece, no la crea)."""

    def test_los_cuatro_marcadores_se_reconocen(self):
        peticion = req(NOW - timedelta(hours=2), NOW)
        url = render_source_template(
            "http://a.test/x?s={start}&e={end}&d={duration}&u={utc}", peticion
        )
        self.assertIn(f"s={int(NOW.timestamp()) - 7200}", url)
        self.assertIn(f"e={int(NOW.timestamp())}", url)
        self.assertIn("d=7200", url)
        self.assertIn("u=2026-03-02:", url)

    def test_un_marcador_desconocido_se_rechaza(self):
        with self.assertRaises(CatchupError):
            render_source_template("http://a.test/x?s={inicio}", req(NOW, NOW))

    def test_un_especificador_de_formato_se_rechaza(self):
        with self.assertRaises(CatchupError):
            render_source_template("http://a.test/x?s={start:04d}", req(NOW, NOW))

    def test_una_plantilla_sin_marcadores_se_rechaza(self):
        with self.assertRaises(CatchupError):
            render_source_template("http://a.test/x/fijo.m3u8", req(NOW, NOW))

    def test_una_plantilla_vacia_se_rechaza(self):
        for plantilla in ("", "   ", None):
            with self.assertRaises(CatchupError):
                render_source_template(plantilla, req(NOW, NOW))  # type: ignore[arg-type]

    def test_la_url_renderizada_pasa_la_politica_de_stream(self):
        peticion = req(NOW - timedelta(hours=2), NOW)
        canal = canal_m3u()
        pedido = build_playback_request(
            canal, capability_for(canal), peticion, now=NOW
        )
        self.assertEqual(
            pedido.url,
            f"http://a.test/streaming/timeshift.php?username=u&password=p&stream=101"
            f"&start={int(NOW.timestamp()) - 7200}&duration=7200",
        )

    def test_una_plantilla_con_esquema_prohibido_se_rechaza(self):
        canal = canal_m3u(catchup_source="file:///etc/passwd?start={start}")
        with self.assertRaises(CatchupError):
            build_playback_request(
                canal, capability_for(canal), req(NOW - timedelta(hours=1), NOW), now=NOW
            )

    def test_una_plantilla_con_nueva_linea_se_rechaza(self):
        """Congelado: `validate_url` la rechaza (gap CU-03)."""
        canal = canal_m3u(
            catchup_source="http://a.test/x?start={start}\nX-Inyectado: 1"
        )
        with self.assertRaises(CatchupError):
            build_playback_request(
                canal, capability_for(canal), req(NOW - timedelta(hours=1), NOW), now=NOW
            )


# ---------------------------------------------------------------------------
# 5. Referencia opaca
# ---------------------------------------------------------------------------


class TestReferenciaOpaca(unittest.TestCase):
    def test_no_lleva_credenciales(self):
        ref = CatchupRef("Panel", "101", 1700000000, 3600)
        self.assertEqual(
            ref.to_opaque(),
            "xtream-ts://Panel/101.ts?start=1700000000&dur=3600",
        )
        self.assertNotIn("@", ref.to_opaque())

    def test_al_parsear_una_duracion_negativa_se_acota_a_uno(self):
        ref = CatchupRef.parse("xtream-ts://P/1.ts?start=10&dur=-5")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.duration_seconds, 5)

    def test_al_generar_una_duracion_negativa_no_se_acota(self):
        """Asimetría real: `to_opaque` escribe `dur=-5` (gap CU-02)."""
        self.assertEqual(
            CatchupRef("P", "1", 1, -5).to_opaque(),
            "xtream-ts://P/1.ts?start=1&dur=-5",
        )

    def test_ida_y_vuelta_conserva_los_datos(self):
        original = CatchupRef("Panel Xtream", "a/b", 1700000000, 90, "mp4")
        otra = CatchupRef.parse(original.to_opaque())
        self.assertEqual(otra, original)

    def test_un_esquema_desconocido_no_se_parsea(self):
        self.assertIsNone(CatchupRef.parse("http://a.test/live/1.m3u8"))


if __name__ == "__main__":
    unittest.main()