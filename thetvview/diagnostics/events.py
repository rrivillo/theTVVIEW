"""Eventos estructurados (DIAG-001 · FR-DIAG-006 · contrato §3.3).

El contrato §3.3 pide eventos con `event_type`, `timestamp`,
`correlation_id`, `session_id`, `channel_id`, `component`, `phase`,
`severity`, `reason_code`, `retryable`, `duration_ms` y `details`. Aquí se
respeta **al pie de la letra**:

- Los campos desconocidos (no aplican a este evento) se **omitir** en
  :meth:`Event.to_dict`: no se rellenan con ceros ni con «desconocido»
  inventado. Un `duration_ms` que nadie midió no aparece — el SDD prohíbe
  inventar mediciones.
- :class:`EventBus` **redacta al añadir**, nunca al leer (FR-DIAG-008): lo
  que queda dentro del búfer ya está limpio, así que leerlo no puede
  colar un secreto «por si acaso» — y un evento leído es idéntico al que
  se emitió.
- El búfer es un ``deque(maxlen=…)``: acotado por construcción, no crece
  sin límite (el FR-DIAG-010 del historial persistente es aparte, DIAG-004).

El bus es un buzón en memoria: ``append`` de un ``deque`` es atómico en
CPython, suficiente para que el hilo de UI y los workers compartan eventos
sin bloquear. No es un bus de mensajes: nadie «escucha», el informe
(DIAG-002) lee cuando lo necesita.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from ..security.redaction import redact_mapping, redact_text

__all__ = [
    "DEFAULT_MAX_EVENTS",
    "Event",
    "EventBus",
    "Severity",
    "nuevo_correlation_id",
]

#: Tamaño del búfer en memoria. Acotado a mano: con ~500 eventos de texto
#: corto el informe tiene margen de sobra y el proceso no retiene nada.
DEFAULT_MAX_EVENTS: int = 500

#: Campos del contrato §3.3, en el orden en que los lista el SDD. Este
#: tuple es la única fuente de verdad de :meth:`Event.to_dict`.
_CAMPOS: tuple[str, ...] = (
    "event_type",
    "timestamp",
    "correlation_id",
    "session_id",
    "channel_id",
    "component",
    "phase",
    "severity",
    "reason_code",
    "retryable",
    "duration_ms",
    "details",
)


class Severity(str, Enum):
    """Gravedad del evento (§3.3; sin significado más profundo)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def nuevo_correlation_id() -> str:
    """Identificador de correlación nuevo (FR-DIAG-006).

    Los eventos de sesión, red, análisis y motor comparten uno para que el
    informe pueda enlazarlos.
    """
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Event:
    """Un evento estructurado del §3.3, inmutable.

    Sólo se rellena lo que se conoce. `duration_ms` sólo se pasa si se
    **midió**: el SDD prohíbe inventar mediciones y este modelo no las
    rellena por su cuenta.

    Raises:
        ValueError: `event_type` vacío, `severity` desconocido o
            `duration_ms` negativo (no es una medición posible).
    """

    #: Tipo del evento, p. ej. ``playback.started`` o ``probe.failed``.
    event_type: str
    #: Momento del suceso (epoch, segundos). Por defecto, ahora.
    timestamp: float = field(default_factory=time.time)
    #: Identificador de correlación del FR-DIAG-006.
    correlation_id: str | None = None
    #: Sesión de reproducción a la que pertenece, si aplica.
    session_id: str | None = None
    #: Canal estable; se redacta al emitir por si trae credenciales.
    channel_id: str | None = None
    #: Componente que emite (``probe``, ``player``, ``scheduler``…).
    component: str | None = None
    #: Fase (``PREPARING``, ``STARTING``…) cuando corresponda.
    phase: str | None = None
    severity: Severity = Severity.INFO
    #: Código corto de motivo (``http_403``, ``manifest_invalid``…).
    reason_code: str | None = None
    #: ``None`` si no se sabe si el fallo es reintentable.
    retryable: bool | None = None
    #: Milisegundos **medidos**; ausente si nadie midió.
    duration_ms: float | None = None
    #: Datos permitidos y redactados (ya limpios dentro del bus).
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.event_type or "").strip():
            raise ValueError("Un evento necesita un event_type (mensaje apto para modal).")
        if not isinstance(self.severity, Severity):
            try:
                object.__setattr__(self, "severity", Severity(str(self.severity)))
            except ValueError:
                raise ValueError(
                    f"Gravedad desconocida: {self.severity!r} (usa Severity.INFO/"
                    "WARNING/ERROR)."
                ) from None
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError(
                "duration_ms no puede ser negativo: no se inventan mediciones."
            )
        # Inmutable de verdad: ni el dict de details se toca tras guardarse.
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def to_dict(self) -> dict[str, Any]:
        """Versión serializable **omitiendo** los campos desconocidos (§3.3).

        Un campo ``None`` significa «no se conoce/no aplica»: no aparece en
        el diccionario. Sólo `event_type` y `timestamp` van siempre, porque
        un evento sin tipo ni momento no es un evento.
        """
        out: dict[str, Any] = {}
        for nombre in _CAMPOS:
            valor = getattr(self, nombre)
            if nombre == "details":
                if valor:  # vacío → también se omite
                    out[nombre] = dict(valor)
                continue
            if valor is None:
                continue
            if isinstance(valor, Severity):
                out[nombre] = valor.value
            else:
                out[nombre] = valor
        return out


def _redactar(evento: Event) -> Event:
    """Copia del evento con `details` y `channel_id` redactados (FR-DIAG-008)."""
    detalles = redact_mapping(dict(evento.details)) if evento.details else {}
    canal = redact_text(evento.channel_id) if evento.channel_id else evento.channel_id
    return replace(evento, details=detalles, channel_id=canal)


class EventBus:
    """Búfer acotado de eventos que redacta **al añadir**, nunca al leer.

    La redacción en la entrada es la única garantía de que nada legible
    dentro ya sea un secreto (FR-DIAG-008); redactar al leer convertiría
    cada acceso en una oportunidad para olvidar algo.
    """

    def __init__(self, maxlen: int = DEFAULT_MAX_EVENTS) -> None:
        """`maxlen` es el tope del búfer: al llenarse sale el más antiguo."""
        if maxlen <= 0:
            raise ValueError("EventBus necesita un maxlen mayor que cero.")
        self._eventos: deque[Event] = deque(maxlen=maxlen)

    @property
    def maxlen(self) -> int:
        """Cuántos eventos retiene como máximo."""
        return self._eventos.maxlen or 0

    def emit(self, event: Event) -> Event:
        """Redacta, guarda y devuelve el evento **tal y como quedó**.

        Devolverlo es deliberado: el emisor puede ver que su secreto fue
        redactado, y el búfer nunca guarda otra copia distinta.
        """
        limpio = _redactar(event)
        self._eventos.append(limpio)
        return limpio

    def buscar(
        self,
        *,
        correlation_id: str | None = None,
        session_id: str | None = None,
        event_type: str | None = None,
    ) -> list[Event]:
        """Eventos que coinciden con todos los criterios indicados (AND)."""
        out: list[Event] = []
        for evento in self._eventos:
            if correlation_id is not None and evento.correlation_id != correlation_id:
                continue
            if session_id is not None and evento.session_id != session_id:
                continue
            if event_type is not None and evento.event_type != event_type:
                continue
            out.append(evento)
        return out

    def events(self) -> list[Event]:
        """Copia en orden (el más antiguo primero)."""
        return list(self._eventos)

    def clear(self) -> None:
        """Vacía el búfer (p. ej. al empezar una sesión nueva)."""
        self._eventos.clear()

    def __len__(self) -> int:
        return len(self._eventos)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._eventos)
