"""Diagnóstico de canales (P1.8 · SDD §11).

- :mod:`thetvview.diagnostics.events` → eventos estructurados del §3.3
  con redacción a la entrada (DIAG-001).
- El informe (DIAG-002) y el historial persistente (DIAG-004) viven aquí
  también; la salud por dimensiones (DIAG-003) sigue en
  :mod:`thetvview.channel_health`, que es quien la mide.

Regla de la casa: nada se inventa. Lo que no se conoce se marca como
desconocido, lo que no se midió no aparece, y generar un informe no hace
red (FR-DIAG-009).
"""

from __future__ import annotations

from .events import (
    DEFAULT_MAX_EVENTS,
    Event,
    EventBus,
    Severity,
    nuevo_correlation_id,
)
from .history import HistorialDiagnostico, RegistroHistorial, default_historial
from .report import DESCONOCIDO, construir_informe, guardar_informe
from .taxonomy import (
    CODES_CONOCIDOS,
    MOTIVOS_CONOCIDOS,
    Causa,
    causa_de,
    causa_de_codigo,
    es_razon_conocida,
    explicacion_de,
)

__all__ = [
    "CODES_CONOCIDOS",
    "DEFAULT_MAX_EVENTS",
    "DESCONOCIDO",
    "MOTIVOS_CONOCIDOS",
    "Causa",
    "Event",
    "EventBus",
    "HistorialDiagnostico",
    "RegistroHistorial",
    "Severity",
    "causa_de",
    "causa_de_codigo",
    "construir_informe",
    "default_historial",
    "es_razon_conocida",
    "explicacion_de",
    "guardar_informe",
    "nuevo_correlation_id",
]
