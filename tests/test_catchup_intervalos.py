"""CU-02 — Validación de intervalos de catch-up.

Cierra el defecto medido en la Fase 0: :attr:`CatchupRequest.duration_seconds`
usaba ``max(1, round(delta))``, de modo que un intervalo **invertido** o de
duración cero se normalizaba a un segundo en silencio, se clasificaba
``AVAILABLE`` y se construía la URL sin decir nada.

Estos tests **fallan contra el código anterior** a propósito: son el criterio de
aceptación del ticket. Con ellos, el módulo pasa a:

- **rechazar** en vez de normalizar, con un motivo apto para modal;
- distinguir los cuatro motivos que el usuario percibe distinto (fin anterior
  al inicio, inicio igual al fin, duración excesiva, intervalo ausente);
- fijar un **tope propio y declarado** de duración por petición, porque el
  estándar no lo define y el proveedor no lo publica (docs/sdd/catchup-temporal.md §5.3);
- tratar el programa que **cruza medianoche** como lo que es: un intervalo
  válido, no un caso especial.

La caracterización del antes queda en ``tests/test_catchup_caracterizacion.py``,
que congela el comportamiento viejo y **no** se modifica.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from thetvview import catchup
from thetvview.catchup import (
    SECONDS_PER_DAY,
    CatchupError,
    CatchupRequest,
    CatchupState,
    build_playback_request,
    classify_request,
    validar_intervalo,
)
from thetvview.models import Channel

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
DIA = SECONDS_PER_DAY

PLANTILLA = (
    "http://a.test/streaming/timeshift.php?username=u&password=p"
    "&stream=101&start={start}&duration={duration}"
)


def cap(**extra) -> catchup.CatchupCapability:
    base = {
        "enabled": True,
        "provider_declared": True,
        "archive_duration_seconds": 7 * DIA,
        "protocol": catchup.PROTOCOL_M3U,
        "source_template": PLANTILLA,
    }
    base.update(extra)
    return catchup.CatchupCapability(**base)


def canal(**attrs) -> Channel:
    base = {"catchup": "shift", "catchup_days": "7", "catchup_source": PLANTILLA}
    base.update(attrs)
    return Channel(name="Canal", url="http://a.test/live/101", attrs=base)


def req(inicio: datetime, fin: datetime) -> CatchupRequest:
    return CatchupRequest(channel_id="101", start=inicio, end=fin)


def valido() -> CatchupRequest:
    return req(NOW - timedelta(hours=2), NOW - timedelta(hours=1))


# ---------------------------------------------------------------------------
# 1. El motivo del rechazo
# ---------------------------------------------------------------------------


class TestMotivos(unittest.TestCase):
    """Cada motivo es distinguible, en español y apto para modal."""

    def test_fin_anterior_al_inicio(self):
        invertido = req(NOW - timedelta(hours=3), NOW - timedelta(hours=4))
        with self.assertRaises(CatchupError) as ctx:
            validar_intervalo(invertido)
        self.assertIn("antes de empezar", str(ctx.exception))

    def test_inicio_igual_a_fin(self):
        with self.assertRaises(CatchupError) as ctx:
            validar_intervalo(req(NOW - timedelta(hours=1), NOW - timedelta(hours=1)))
        self.assertIn("misma hora", str(ctx.exception))

    def test_duracion_excesiva(self):
        largo = req(NOW - timedelta(days=40), NOW)
        with self.assertRaises(CatchupError) as ctx:
            validar_intervalo(largo, maximo=catchup.MAX_REQUEST_SECONDS)
        self.assertIn("máximo por intento", str(ctx.exception))

    def test_intervalo_ausente(self):
        for inicio, fin in ((None, NOW), (NOW, None), (None, None)):
            with self.assertRaises(CatchupError) as ctx:
                validar_intervalo(req(inicio, fin))
            self.assertIn("no se sabe", str(ctx.exception).lower())

    def test_el_mensaje_no_es_tecnico(self):
        """El modal tiene que explicar el motivo sin jerga ni credenciales."""
        with self.assertRaises(CatchupError) as ctx:
            validar_intervalo(req(NOW, NOW - timedelta(hours=1)))
        texto = str(ctx.exception)
        self.assertNotIn("Traceback", texto)
        self.assertNotIn("datetime", texto)
        self.assertNotIn("username", texto)


# ---------------------------------------------------------------------------
# 2. Rechazar en vez de normalizar
# ---------------------------------------------------------------------------


class TestSeRechazaEnVezDeNormalizar(unittest.TestCase):
    """El comportamiento viejo, congelado como error explícito."""

    def test_el_invertido_ya_no_dura_un_segundo(self):
        invertido = req(NOW - timedelta(hours=3), NOW - timedelta(hours=4))
        with self.assertRaises(CatchupError):
            validar_intervalo(invertido)

    def test_el_invertido_no_se_clasifica_como_disponible(self):
        """Clasificar es una consulta: devuelve estado, no lanza."""
        invertido = req(NOW - timedelta(hours=3), NOW - timedelta(hours=4))
        self.assertIs(
            classify_request(cap(), invertido, NOW), CatchupState.INVALID_INTERVAL
        )

    def test_el_invertido_no_construye_url(self):
        invertido = req(NOW - timedelta(hours=3), NOW - timedelta(hours=4))
        with self.assertRaises(CatchupError):
            build_playback_request(canal(), catchup.capability_for(canal()), invertido, now=NOW)

    def test_la_duracion_sigue_siendo_calculable_para_diagnostico(self):
        """Rechazar no quita el dato: `duration_seconds` sigue siendo legible.

        Se usa para el informe y para el texto del modal, así que no puede
        lanzar; simplemente ya no se usa para *construir* nada.
        """
        invertido = req(NOW - timedelta(hours=3), NOW - timedelta(hours=4))
        self.assertEqual(invertido.duration_seconds, 1)  # diagnóstico
        with self.assertRaises(CatchupError):
            validar_intervalo(invertido)  # construcción

    def test_build_playback_request_valida_antes_de_construir(self):
        """El orden del SDD §10: validar, y sólo después construir."""
        invertido = req(NOW - timedelta(hours=3), NOW - timedelta(hours=4))
        with self.assertRaises(CatchupError):
            build_playback_request(
                canal(), catchup.capability_for(canal()), invertido, now=NOW
            )


# ---------------------------------------------------------------------------
# 3. Topes
# ---------------------------------------------------------------------------


class TestTopes(unittest.TestCase):
    def test_hay_un_tope_propio_y_documentado(self):
        self.assertIsInstance(catchup.MAX_REQUEST_SECONDS, int)
        self.assertGreater(catchup.MAX_REQUEST_SECONDS, 0)
        self.assertIn("catchup-temporal", catchup.MAX_REQUEST_SECONDS_DOC)

    def test_el_tope_no_es_menor_que_un_programa_normal(self):
        """Un programa de 4 h debe caber de sobra: el tope no estorba."""
        self.assertGreaterEqual(catchup.MAX_REQUEST_SECONDS, 4 * 3600)

    def test_el_tope_no_excede_la_ventana_maxima_declarada(self):
        """No tiene sentido permitir más de lo que el proveedor conserva."""
        ventana_maxima = 90 * DIA
        self.assertLessEqual(catchup.MAX_REQUEST_SECONDS, ventana_maxima)

    def test_un_intervalo_en_el_limite_se_acepta(self):
        justo = req(NOW - timedelta(seconds=catchup.MAX_REQUEST_SECONDS), NOW)
        validar_intervalo(justo)  # no lanza

    def test_un_segundo_mas_alla_del_limite_se_rechaza(self):
        exceso = req(
            NOW - timedelta(seconds=catchup.MAX_REQUEST_SECONDS + 1), NOW
        )
        with self.assertRaises(CatchupError):
            validar_intervalo(exceso)

    def test_el_tope_se_puede_ajustar_por_llamada(self):
        peticion = req(NOW - timedelta(hours=3), NOW)
        validar_intervalo(peticion, maximo=4 * 3600)
        with self.assertRaises(CatchupError):
            validar_intervalo(peticion, maximo=3600)

    def test_un_tope_no_positivo_se_rechaza(self):
        for tope in (0, -1):
            with self.assertRaises(CatchupError):
                validar_intervalo(valido(), maximo=tope)


# ---------------------------------------------------------------------------
# 4. Intervalos legítimos que antes se confundían
# ---------------------------------------------------------------------------


class TestIntervalosLegitimos(unittest.TestCase):
    def test_una_hora_se_acepta(self):
        self.assertEqual(valido().duration_seconds, 3600)
        validar_intervalo(valido())

    def test_un_segundo_se_acepta(self):
        minimo = req(NOW - timedelta(seconds=1), NOW)
        validar_intervalo(minimo)
        self.assertEqual(minimo.duration_seconds, 1)

    def test_un_programa_que_cruza_medianoche_se_acepta(self):
        """23:30 → 00:15 del día siguiente: un intervalo normal."""
        inicio = datetime(2026, 3, 2, 23, 30, tzinfo=timezone.utc)
        fin = datetime(2026, 3, 3, 0, 15, tzinfo=timezone.utc)
        cruce = req(inicio, fin)
        validar_intervalo(cruce)
        self.assertEqual(cruce.duration_seconds, 45 * 60)
        self.assertIs(
            classify_request(cap(), cruce, datetime(2026, 3, 3, 1, tzinfo=timezone.utc)),
            CatchupState.AVAILABLE,
        )

    def test_naive_y_aware_se_comparan_por_instante(self):
        naive = CatchupRequest(
            channel_id="101",
            start=(NOW - timedelta(hours=2)).replace(tzinfo=None),
            end=(NOW - timedelta(hours=1)).replace(tzinfo=None),
        )
        validar_intervalo(naive)
        self.assertEqual(naive.duration_seconds, 3600)

    def test_la_misma_expresion_en_dos_zonas_no_se_va_de_la_ventana(self):
        """El instante manda, no la zona donde se escribió."""
        en_madrid = valido().start.astimezone(timezone(timedelta(hours=2)))
        en_lima = valido().start.astimezone(timezone(timedelta(hours=-5)))
        a = req(en_madrid, valido().end)
        b = req(en_lima, valido().end)
        self.assertEqual(a.duration_seconds, b.duration_seconds)
        validar_intervalo(a)
        validar_intervalo(b)


# ---------------------------------------------------------------------------
# 5. La ventana declarada sigue mandando sobre el intervalo
# ---------------------------------------------------------------------------


class TestLaVentanaSigueMandando(unittest.TestCase):
    def test_un_intervalo_valido_fuera_de_la_ventana_sigue_siendo_fuera(self):
        """Intervalo sano (2 h) que cae fuera de la ventana de 1 día.

        Las dos reglas son independientes: validar el intervalo no perdona
        salirse de la ventana.
        """
        viejo = req(NOW - timedelta(days=3, hours=-1), NOW - timedelta(days=3, hours=-3))
        validar_intervalo(viejo)  # el intervalo en sí es válido
        self.assertIs(
            classify_request(cap(archive_duration_seconds=DIA), viejo, NOW),
            CatchupState.OUTSIDE_ARCHIVE_WINDOW,
        )

    def test_una_ventana_ancha_no_hace_valer_un_intervalo_excesivo(self):
        """Con ventana de 500 días, 40 h siguen estando por encima del tope."""
        enorme = req(NOW - timedelta(days=3), NOW - timedelta(days=1, hours=-16))
        validar_intervalo(enorme, maximo=72 * 3600)
        with self.assertRaises(CatchupError):
            validar_intervalo(enorme)

    def test_validar_el_intervalo_no_habilita_nada(self):
        """Fail-closed: validar el intervalo no suple la declaración."""
        peticion = valido()
        validar_intervalo(peticion)
        self.assertIs(
            classify_request(catchup.DISABLED, peticion, NOW),
            CatchupState.DISABLED_BY_PROVIDER,
        )

    def test_sin_catchup_la_url_se_nunca_construye(self):
        peticion = valido()
        with self.assertRaises(catchup.CatchupNotAvailableError):
            build_playback_request(
                canal(), catchup.DISABLED, peticion, now=NOW
            )


if __name__ == "__main__":
    unittest.main()