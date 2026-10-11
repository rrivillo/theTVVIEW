"""Gestor de tareas en segundo plano (P0.3).

Un solo lugar donde nacen las tareas de la app, con ciclo de vida común:
identificador y generación, cancelación cooperativa, límites de concurrencia,
publicación por cola y cierre acotado. Ver :mod:`thetvview.tasks.manager`.
"""

from __future__ import annotations

from .manager import (
    CancellationToken,
    TaskCancelled,
    TaskHandle,
    TaskManager,
    TaskResult,
    TaskStatus,
)

__all__ = [
    "CancellationToken",
    "TaskCancelled",
    "TaskHandle",
    "TaskManager",
    "TaskResult",
    "TaskStatus",
]
