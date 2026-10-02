"""Límites de red, tamaño y concurrencia (SDD §18/§19/§26, gap B14).

Solo stdlib. Todos los valores son configurables pero **siempre** se
normalizan con un rango: una prefs corrupta (o un 0, o un negativo, o un
string) nunca puede desactivar los límites de seguridad.

Uso típico::

    from thetvview.security.limits import get_limits

    lim = get_limits()
    # Nunca urlopen a pelo: pásalo por SafeHttpClient, que aplica
    # estos límites, la política de URL y el anti-SSRF.
    from thetvview.security.safe_http import SafeHttpClient
    SafeHttpClient(max_bytes=lim.max_file_bytes).get_bytes(url)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

__all__ = [
    "Limits",
    "DEFAULT_LIMITS",
    "get_limits",
    "set_limits",
    "reset_limits",
    "clamp_value",
    "human_size",
]


def human_size(num_bytes: int) -> str:
    """Tamaño legible para mensajes al usuario (12 KB, 25 MB, 512 bytes)."""
    try:
        value = int(num_bytes)
    except (TypeError, ValueError):  # pragma: no cover - caller ya valida
        return "límite"
    if value >= 1024 * 1024:
        return f"{max(1, value // (1024 * 1024))} MB"
    if value >= 1024:
        return f"{max(1, value // 1024)} KB"
    return f"{max(1, value)} bytes"


@dataclass(frozen=True)
class Limits:
    """Límites de la app (inmutables; cámbialos con `set_limits`)."""

    # --- timeouts (segundos) ------------------------------------------------
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    total_timeout: float = 60.0
    # --- tamaños ------------------------------------------------------------
    max_response_bytes: int = 25 * 1024 * 1024  # 25 MB (JSON/M3U/XMLTV)
    max_file_bytes: int = 64 * 1024 * 1024  # 64 MB para ficheros locales
    max_redirects: int = 3
    # --- concurrencia -------------------------------------------------------
    max_concurrent_requests: int = 8
    # --- volúmenes de datos -------------------------------------------------
    max_entries: int = 200_000  # entradas M3U o líneas de una playlist
    max_xml_depth: int = 64
    max_xml_nodes: int = 500_000

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "Limits":
        """Construye desde un dict (p. ej. `prefs.json`) normalizando cada valor.

        Claves desconocidas y valores no numéricos se ignoran (se queda el
        valor por defecto). Los números se recortan al rango permitido.
        """
        if not data:
            return cls()
        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            clamped = clamp_value(f.name, data[f.name])
            if clamped is not None:
                values[f.name] = clamped
        return cls(**values)


# Rangos admitidos por campo: (mínimo, máximo). Fuera de aquí se recorta.
_RANGES: dict[str, tuple[float, float]] = {
    "connect_timeout": (0.1, 300.0),
    "read_timeout": (0.1, 600.0),
    "total_timeout": (0.1, 1800.0),
    "max_response_bytes": (1024, 512 * 1024 * 1024),
    "max_file_bytes": (1024, 1024 * 1024 * 1024),
    "max_redirects": (0, 10),
    "max_concurrent_requests": (1, 64),
    "max_entries": (1, 5_000_000),
    "max_xml_depth": (1, 1000),
    "max_xml_nodes": (10, 5_000_000),
}

_DEFAULTS = Limits()
_FLOAT_FIELDS = frozenset({"connect_timeout", "read_timeout", "total_timeout"})


def clamp_value(name: str, raw: Any) -> float | int | None:
    """Normaliza `raw` para el campo `name`; devuelve None si es inutilizable."""
    if name not in _RANGES:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    low, high = _RANGES[name]
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
        return None
    value = min(max(value, low), high)
    if name in _FLOAT_FIELDS:
        return round(value, 3)
    return int(value)


DEFAULT_LIMITS: Limits = _DEFAULTS

_LIMITS: Limits = _DEFAULTS


def get_limits() -> Limits:
    """Límites activos de la aplicación (singleton)."""
    return _LIMITS


def set_limits(limits: Limits | Mapping[str, Any] | None) -> Limits:
    """Instala nuevos límites (dataclass o mapping) y los normaliza."""
    global _LIMITS
    if limits is None:
        _LIMITS = _DEFAULTS
    elif isinstance(limits, Limits):
        _LIMITS = limits
    else:
        _LIMITS = Limits.from_mapping(limits)
    return _LIMITS


def reset_limits() -> None:
    """Vuelve a los valores por defecto (tests)."""
    global _LIMITS
    _LIMITS = _DEFAULTS
