"""Tests de DIAG-001: eventos §3.3 y EventBus con redacción a la entrada.

Tres garantías se comprueban aquí:

- el modelo **omite** los campos desconocidos y no inventa mediciones
  (un ``duration_ms`` no medido no aparece);
- el bus **redacta al añadir** y nunca al leer: lo que está guardado ya
  está limpio y leer no cambia nada;
- el búfer está **acotado** por construcción.
"""

from __future__ import annotations

import time
import unittest

from thetvview.diagnostics import (
    DEFAULT_MAX_EVENTS,
    Event,
    EventBus,
    Severity,
    nuevo_correlation_id,
)


class TestEvent(unittest.TestCase):
    def test_event_type_es_obligatorio(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Event("   ")
        self.assertIn("event_type", str(ctx.exception))

    def test_timestamp_por_defecto_es_ahora(self) -> None:
        antes = time.time()
        evento = Event("prueba.ok")
        self.assertGreaterEqual(evento.timestamp, antes)
        self.assertLess(evento.timestamp, time.time() + 1)

    def test_campos_desconocidos_se_omiten(self) -> None:
        datos = Event("probe.ok", component="probe").to_dict()
        self.assertEqual(datos["event_type"], "probe.ok")
        self.assertIn("timestamp", datos)
        # No aplican → no aparecen; y jamás con un valor inventado.
        for clave in (
            "correlation_id",
            "session_id",
            "channel_id",
            "phase",
            "reason_code",
            "retryable",
            "duration_ms",
            "details",
        ):
            self.assertNotIn(clave, datos)

    def test_no_se_inventan_mediciones(self) -> None:
        evento = Event("playback.started")
        self.assertIsNone(evento.duration_ms)
        self.assertNotIn("duration_ms", evento.to_dict())
        # En cambio, si se midió, sale.
        medido = Event("playback.started", duration_ms=12.5)
        self.assertEqual(medido.to_dict()["duration_ms"], 12.5)

    def test_duration_negativa_no_es_medicion(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Event("x", duration_ms=-1)
        self.assertIn("mediciones", str(ctx.exception))

    def test_severity_como_texto_se_normaliza(self) -> None:
        evento = Event("fallo.red", severity="error")
        self.assertIs(evento.severity, Severity.ERROR)
        self.assertEqual(evento.to_dict()["severity"], "error")

    def test_severity_desconocida_rechaza(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Event("x", severity="critico")
        self.assertIn("Gravedad desconocida", str(ctx.exception))

    def test_details_es_inmutable(self) -> None:
        evento = Event("x", details={"intentos": 3})
        with self.assertRaises(TypeError):
            evento.details["intentos"] = 4  # type: ignore[index]

    def test_details_vacio_se_omite(self) -> None:
        self.assertNotIn("details", Event("x").to_dict())
        self.assertEqual(Event("x", details={"a": 1}).to_dict()["details"], {"a": 1})


class TestEventBusRedaccion(unittest.TestCase):
    """FR-DIAG-008: la redacción ocurre al añadir, no al leer."""

    def test_redacta_al_emitir(self) -> None:
        bus = EventBus()
        guardado = bus.emit(
            Event(
                "probe.failed",
                channel_id="http://usuario:clave@proveedor.example/live/canal.m3u8",
                details={
                    "password": "secreta123",
                    "motivo": "401 con Authorization: Bearer abcdef123456",
                    "intentos": 2,
                },
            )
        )
        self.assertNotIn("clave", str(guardado.channel_id))
        self.assertNotIn("secreta123", str(guardado.details["password"]))
        self.assertNotIn("abcdef123456", str(guardado.details["motivo"]))
        # Lo no sensible se conserva: redactar no es borrar.
        self.assertEqual(guardado.details["intentos"], 2)

    def test_leer_no_vuelve_a_redactar_ni_cambiar(self) -> None:
        bus = EventBus()
        emitido = bus.emit(Event("a.b", details={"x": "sin secretos"}))
        leidos = bus.events()
        self.assertEqual(len(leidos), 1)
        # El guardado es el objeto devuelto: no hay copias divergentes.
        self.assertIs(leidos[0], emitido)

    def test_emisor_ve_el_evento_limpio(self) -> None:
        bus = EventBus()
        devuelto = bus.emit(Event("a.b", channel_id="http://u:p@h/x"))
        self.assertIs(devuelto, bus.events()[0])
        self.assertNotIn("p@", str(devuelto.channel_id))


class TestEventBusBuzon(unittest.TestCase):
    def test_buffer_acotado(self) -> None:
        bus = EventBus(maxlen=3)
        for i in range(5):
            bus.emit(Event(f"evt{i}"))
        self.assertEqual(len(bus), 3)
        self.assertEqual([e.event_type for e in bus.events()], ["evt2", "evt3", "evt4"])
        self.assertEqual(bus.maxlen, 3)

    def test_maxlen_no_positivo_rechaza(self) -> None:
        with self.assertRaises(ValueError):
            EventBus(maxlen=0)

    def test_buscar_por_correlacion_y_sesion(self) -> None:
        bus = EventBus()
        bus.emit(Event("a", correlation_id="c1", session_id="s1"))
        bus.emit(Event("b", correlation_id="c1", session_id="s2"))
        bus.emit(Event("a", correlation_id="c2", session_id="s1"))
        self.assertEqual(len(bus.buscar(correlation_id="c1")), 2)
        self.assertEqual(len(bus.buscar(session_id="s1")), 2)
        # Los criterios combinan en AND…
        self.assertEqual(
            [e.event_type for e in bus.buscar(correlation_id="c1", session_id="s1")],
            ["a"],
        )
        # …y sin criterios devuelve todo.
        self.assertEqual(len(bus.buscar()), 3)

    def test_limpiar_e_iterar(self) -> None:
        bus = EventBus()
        bus.emit(Event("a"))
        self.assertEqual([e.event_type for e in bus], ["a"])
        bus.clear()
        self.assertEqual(len(bus), 0)

    def test_default_acotado(self) -> None:
        self.assertGreater(DEFAULT_MAX_EVENTS, 0)
        self.assertEqual(EventBus().maxlen, DEFAULT_MAX_EVENTS)

    def test_correlation_ids_distintos(self) -> None:
        self.assertNotEqual(nuevo_correlation_id(), nuevo_correlation_id())
        self.assertEqual(len(nuevo_correlation_id()), 32)


if __name__ == "__main__":
    unittest.main()
