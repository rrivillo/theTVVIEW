"""Failover limitado y clasificación de errores recuperables (A-07, A-08)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from thetvview.player.errors import StreamErrorCode, NO_REINTENTABLE
except Exception:  # pragma: no cover
    StreamErrorCode = None
    NO_REINTENTABLE = frozenset()

__all__ = [
    "ErrorRecuperable",
    "ErrorNoRecuperable",
    "clasificar_error_recuperable",
    "es_error_recuperable",
    "FailoverLimits",
    "FailoverController",
    "FailoverPlan",
]


# Errores NO recuperables: seguridad, auth, formato no compatible, cancelación.
_NO_RECOVERABLE_CODES = {
    "invalid_url",
    "unsupported_protocol",
    "auth_failed",
    "player_missing",
    "cancelled",
    "codec_unsupported",
    "url_rechazada",
    "autenticacion",
}
# Errores recuperables.
_RECOVERABLE_CODES = {
    "connection_failed",
    "connection_timeout",
    "network_error",
    "buffering_timeout",
    "server_error",
    "timeout",
    "connection_reset",
    "dns",
}


def _code_value(err: Any) -> str:
    if hasattr(err, "code"):
        code = getattr(err, "code", None)
        if hasattr(code, "value"):
            return str(code.value)
        if code is not None:
            return str(code)
    if hasattr(err, "value"):
        return str(err.value)
    return str(err).lower()


def es_error_recuperable(error: Any) -> bool:
    code = _code_value(error)
    low = code.lower()
    if low in _NO_RECOVERABLE_CODES:
        return False
    if low in _RECOVERABLE_CODES:
        return True
    # Security errors never recoverable.
    name = type(error).__name__.lower()
    if "security" in name or "auth" in name:
        return False
    if StreamErrorCode is not None:
        try:
            se = StreamErrorCode(low)
            # delegate to library if code exists
            if se in NO_REINTENTABLE:
                return False
            return True
        except Exception:
            pass
    # timeout/reconnect keywords
    if "timeout" in low or "connect" in low or "reconect" in low:
        return True
    return False


def clasificar_error_recuperable(error: Any) -> bool:
    return es_error_recuperable(error)


class ErrorRecuperable(Exception):
    pass


class ErrorNoRecuperable(Exception):
    pass


@dataclass
class FailoverLimits:
    max_attempts: int = 3
    total_budget_seconds: float = 30.0
    per_attempt_timeout: float = 5.0


@dataclass
class FailoverController:
    limits: FailoverLimits = field(default_factory=FailoverLimits)
    cancelled: bool = False
    attempts: int = 0
    used_sources: set[str] = field(default_factory=set)
    started_at: float = field(default_factory=time.monotonic)
    last_source: str | None = None

    def cancel(self) -> None:
        self.cancelled = True

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def budget_exhausted(self) -> bool:
        return self.elapsed() >= self.limits.total_budget_seconds

    @property
    def attempts_exhausted(self) -> bool:
        return self.attempts >= self.limits.max_attempts

    def can_try(self, source_id: str) -> bool:
        if self.cancelled:
            return False
        if self.attempts_exhausted:
            return False
        if self.budget_exhausted():
            return False
        if source_id in self.used_sources:
            return False
        return True

    def begin_attempt(self, source_id: str) -> None:
        self.attempts += 1
        self.used_sources.add(source_id)
        self.last_source = source_id

    def reset_session(self) -> None:
        self.attempts = 0
        self.used_sources.clear()
        self.started_at = time.monotonic()
        self.cancelled = False


@dataclass
class FailoverPlan:
    controller: FailoverController
    candidates: list[Any] = field(default_factory=list)

    def next_source(self) -> Any | None:
        if self.controller.cancelled:
            return None
        for c in self.candidates:
            sid = getattr(c, "source_id", None) or getattr(c, "id", None) or str(c)
            if self.controller.can_try(str(sid)):
                return c
        return None
