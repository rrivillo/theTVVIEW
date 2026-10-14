"""Planificador (P2.5-C) con integración exclusiva al grabador manual.

No introduce una segunda ruta de captura: el planificador decide *cuándo* y
delega *cómo* en :mod:`thetvview.recording.backend` (Fase 6), el único módulo
autorizado a lanzar procesos de captura.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "TaskState",
    "ScheduledState",
    "TaskStatus",
    "ScheduledTask",
    "ScheduledRecording",
    "SchedulerTask",
    "RetryPolicy",
    "SchedulerConfig",
    "SchedulerError",
    "PolicyMissed",
    "TaskStore",
    "RecordingRunner",
    "Scheduler",
    "Planificador",
    "SchedulerService",
    "validar_tarea",
    "crear_tarea",
    "make_task",
    "tick",
    "evaluate",
    "process_due",
    "run_scheduler",
]


class TaskState(str, Enum):
    """Estados de una tarea programada (P-01)."""

    PENDING = "pending"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MISSED = "missed"
    CONFLICT = "conflict"
    SKIPPED = "skipped"


ScheduledState = TaskState
TaskStatus = TaskState


class SchedulerError(Exception):
    pass


class PolicyMissed(str, Enum):
    """Qué hacer con una tarea cuyo instante ya pasó (P-03)."""

    MARK_MISSED = "mark_missed"
    RUN_IF_SAFE = "run_if_safe"
    SKIP = "skip"


@dataclass
class RetryPolicy:
    """Política de reintentos declarativa (P-01)."""

    max_retries: int = 0
    retry_delay_seconds: int = 60
    missed_grace_seconds: int = 30
    missed_policy: PolicyMissed = PolicyMissed.MARK_MISSED


@dataclass
class SchedulerConfig:
    """Límites globales del planificador (P-04)."""

    max_concurrent: int = 1
    grace_seconds: int = 30
    version: int = 1


@dataclass
class ScheduledTask:
    """Tarea versionada de grabación programada (P-01)."""

    task_id: str = ""
    source: str = ""
    channel_id: str | None = None
    start_at: float | None = None  # epoch seconds (segundo más Simple)
    start_iso: str | None = None
    duration_seconds: int = 0
    end_at: float | None = None
    timezone: str = "UTC"
    tz_offset_minutes: int = 0
    destination: str | None = None
    status: TaskState = TaskState.PENDING
    conflicts: list[str] = field(default_factory=list)
    conflict_detected_at: float | None = None
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    retries_used: int = 0
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    message: str | None = None
    file_path: str | None = None
    recording_session_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = str(uuid.uuid4())
        if self.start_at is None and self.start_iso:
            try:
                self.start_at = datetime.fromisoformat(
                    str(self.start_iso).replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                self.start_at = None
        if self.end_at is None and self.start_at is not None:
            self.end_at = float(self.start_at) + max(0, int(self.duration_seconds or 0))
        if isinstance(self.status, str):
            try:
                self.status = TaskState(self.status)
            except ValueError:
                self.status = TaskState.PENDING
        if isinstance(self.retry_policy, dict):
            known = {k: v for k, v in self.retry_policy.items() if k in RetryPolicy.__dataclass_fields__}
            self.retry_policy = RetryPolicy(**known)

    # --- ayuda de presentación -------------------------------------------
    @property
    def tz(self) -> timezone:
        try:
            return timezone(timedelta(minutes=int(self.tz_offset_minutes)))
        except Exception:
            return timezone.utc

    @property
    def presentation_time(self) -> str | None:
        """Instante en la zona horaria de presentación, no en UTC."""
        if self.start_at is None:
            return None
        return datetime.fromtimestamp(float(self.start_at), self.tz).isoformat()

    def end_time_iso(self) -> str | None:
        if self.end_at is None:
            return None
        return datetime.fromtimestamp(float(self.end_at), self.tz).isoformat()

    def definition(self) -> dict[str, Any]:
        """Identidad de la tarea: qué se va a grabar, cuándo y dónde.

        No incluye estado ni marcas de tiempo: reintentar o recargar la misma
        tarea es idempotente aunque su estado haya cambiado (P-06).
        """
        return {
            "task_id": self.task_id,
            "source": self.source,
            "channel_id": self.channel_id,
            "start_at": self.start_at,
            "duration_seconds": self.duration_seconds,
            "end_at": self.end_at,
            "timezone": self.timezone,
            "tz_offset_minutes": self.tz_offset_minutes,
            "destination": self.destination,
            "version": self.version,
        }

    # --- serialización ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value if isinstance(self.status, TaskState) else str(self.status)
        if isinstance(self.retry_policy, RetryPolicy):
            rp = asdict(self.retry_policy)
            rp["missed_policy"] = (
                self.retry_policy.missed_policy.value
                if isinstance(self.retry_policy.missed_policy, PolicyMissed)
                else str(self.retry_policy.missed_policy)
            )
            data["retry_policy"] = rp
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "ScheduledTask":
        if not isinstance(data, dict):
            raise ValueError("la tarea debe ser un objeto")
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


ScheduledRecording = ScheduledTask
SchedulerTask = ScheduledTask


def crear_tarea(*args, **kwargs) -> ScheduledTask:
    return ScheduledTask(*args, **kwargs)


make_task = crear_tarea


def validar_tarea(task: Any) -> list[str]:
    """Errores de validación (P-02). Lista vacía = tarea válida."""
    errors: list[str] = []
    if not isinstance(task, ScheduledTask):
        return ["la tarea debe ser ScheduledTask"]
    if not str(task.source or "").strip():
        errors.append("source es obligatorio")
    if task.start_at is None:
        errors.append("start_at es obligatorio")
    elif float(task.start_at) < 0:
        errors.append("start_at no puede ser negativo")
    if int(task.duration_seconds or 0) < 0:
        errors.append("duration_seconds no puede ser negativo")
    if task.end_at is not None and task.start_at is not None:
        if float(task.end_at) < float(task.start_at):
            errors.append("end_at es anterior a start_at")
    if int(task.version or 0) < 1:
        errors.append("version debe ser >= 1")
    if task.retry_policy is not None and int(getattr(task.retry_policy, "max_retries", 0) or 0) < 0:
        errors.append("max_retries no puede ser negativo")
    return errors


class TaskStore:
    """Persistencia atómica y tolerante a corrupción (P-02)."""

    def __init__(self, path: Any = None, *, version: int = 1):
        self.path = Path(path) if path is not None else None
        self.version = version

    def load(self) -> tuple[list[ScheduledTask], list[str]]:
        tasks: list[ScheduledTask] = []
        errors: list[str] = []
        if self.path is None or not self.path.exists():
            return tasks, errors
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            return [], [f"no se pudo leer el store: {exc}"]

        if isinstance(raw, list):  # formato legado sin versionar
            items = raw
        elif isinstance(raw, dict):
            if int(raw.get("version", 1) or 1) > self.version:
                errors.append("store con versión futura: se lee igual pero no se sobrescribe")
            items = raw.get("tasks")
        else:
            return [], ["el store debe ser una lista u objeto"]

        if not isinstance(items, list):
            return [], ["tasks debe ser una lista"]

        seen: set[str] = set()
        for index, item in enumerate(items):
            try:
                task = ScheduledTask.from_dict(item)
            except Exception as exc:
                errors.append(f"tarea[{index}] ilegible: {exc}")
                continue
            problems = validar_tarea(task)
            if problems:
                errors.append(f"tarea[{index}] inválida: {'; '.join(problems)}")
                continue
            if task.task_id in seen:  # idempotencia: nunca duplicados
                errors.append(f"tarea[{index}] duplicada ignorada: {task.task_id}")
                continue
            seen.add(task.task_id)
            tasks.append(task)
        return tasks, errors

    def save(self, tasks: Iterable[ScheduledTask]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self.version, "tasks": [t.to_dict() for t in tasks]}
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        try:
            os.replace(tmp, self.path)
        except Exception:
            self.path.write_text(data, encoding="utf-8")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def recover(self, tasks: Iterable[ScheduledTask]) -> list[ScheduledTask]:
        """Recuperación tras arranque (P-03): nada quedó grabando solo."""
        recovered: list[ScheduledTask] = []
        for task in tasks:
            if task.status == TaskState.RUNNING:
                task.status = TaskState.MISSED
                task.message = "la aplicación estaba cerrada durante la grabación"
                task.updated_at = time.time()
            recovered.append(task)
        return recovered


class RecordingRunner:
    """Puente exclusivo hacia el grabador manual de Fase 6 (P-05)."""

    def __init__(self, supervisor: Any | None = None, *, destination: str | None = None):
        self._supervisor = supervisor
        self.destination = destination

    @property
    def supervisor(self) -> Any:
        if self._supervisor is None:
            from thetvview.recording.backend import RecordingSupervisor

            self._supervisor = RecordingSupervisor()
        return self._supervisor

    def __call__(self, task: ScheduledTask) -> Any:
        from thetvview.recording.backend import RecordingError, RecordingSession

        session = RecordingSession(
            session_id=task.task_id,
            source_url=task.source,
            output_dir=self.destination or (task.destination or "."),
            requested_name=(task.metadata or {}).get("filename", ""),
        )
        try:
            self.supervisor.start(session)
        except RecordingError as exc:
            raise SchedulerError(str(exc)) from exc
        return session


class Scheduler:
    """Planificador: decide el instante y delega la captura (P-03..P-06)."""

    def __init__(
        self,
        store: TaskStore | None = None,
        recorder_runner: Callable[[ScheduledTask], Any] | None = None,
        config: SchedulerConfig | None = None,
    ):
        self.store = store
        self.recorder_runner = recorder_runner
        self.config = config or SchedulerConfig()
        self.tasks: dict[str, ScheduledTask] = {}
        self.active: set[str] = set()
        self.load_errors: list[str] = []
        if self.store is not None:
            self.reload()

    # --- persistencia -----------------------------------------------------
    def reload(self) -> list[str]:
        if self.store is None:
            return []
        loaded, errors = self.store.load()
        loaded = self.store.recover(loaded)
        self.load_errors = list(errors)
        for task in loaded:
            self.tasks[task.task_id] = task
        return list(self.load_errors)

    def save(self) -> None:
        if self.store is not None:
            self.store.save(self.tasks.values())

    def tasks_list(self) -> list[ScheduledTask]:
        return sorted(self.tasks.values(), key=lambda t: (t.start_at or 0, t.task_id))

    # --- ciclo de vida de la tarea ---------------------------------------
    def add(self, task: ScheduledTask) -> ScheduledTask:
        errors = validar_tarea(task)
        if errors:
            raise SchedulerError("; ".join(errors))
        existing = self.tasks.get(task.task_id)
        if existing is not None:
            if existing.definition() == task.definition():
                return existing  # idempotente: mismo ID y misma definición
            raise SchedulerError(f"ya existe otra tarea con el id {task.task_id}")
        if task.status == TaskState.PENDING:
            task.status = TaskState.SCHEDULED
        self.tasks[task.task_id] = task
        self.save()
        return task

    def get(self, task_id: str) -> ScheduledTask:
        if task_id not in self.tasks:
            raise SchedulerError(f"tarea no encontrada: {task_id}")
        return self.tasks[task_id]

    def remove(self, task_id: str, *, delete_file: bool = False) -> ScheduledTask:
        """Borrar la tarea no borra el archivo salvo acción explícita (P-06)."""
        task = self.get(task_id)
        if task.status == TaskState.RUNNING:
            raise SchedulerError("no se puede borrar una grabación en curso")
        self.tasks.pop(task_id, None)
        self.active.discard(task_id)
        self.save()
        if delete_file and task.file_path:
            try:
                Path(task.file_path).unlink(missing_ok=True)
            except Exception:
                pass
        return task

    def cancel(self, task_id: str, *, force: bool = False) -> ScheduledTask:
        """Cancelar una tarea futura no toca una grabación activa (P-06)."""
        task = self.get(task_id)
        if task.status == TaskState.RUNNING and not force:
            raise SchedulerError("la grabación está en curso: usa force para detenerla")
        if task.status in (TaskState.COMPLETED, TaskState.CANCELLED, TaskState.MISSED):
            return task
        task.status = TaskState.CANCELLED
        task.message = "cancelada por el usuario"
        task.updated_at = time.time()
        self.active.discard(task_id)
        self.save()
        return task

    def complete(self, task_id: str, *, file_path: str | None = None, success: bool = True) -> ScheduledTask:
        task = self.get(task_id)
        if task.status != TaskState.RUNNING:
            raise SchedulerError("la tarea no está en curso")
        self.active.discard(task_id)
        task.status = TaskState.COMPLETED if success else TaskState.FAILED
        task.message = "grabación completada" if success else "la grabación falló"
        if file_path:
            task.file_path = str(file_path)
        task.updated_at = time.time()
        self.save()
        return task

    # --- solapamientos (P-04) -------------------------------------------
    def conflicts_for(self, task: ScheduledTask) -> list[str]:
        if task.start_at is None or task.end_at is None:
            return []
        result: list[str] = []
        for other_id, other in self.tasks.items():
            if other_id == task.task_id:
                continue
            if other.status not in (TaskState.PENDING, TaskState.SCHEDULED, TaskState.RUNNING, TaskState.CONFLICT):
                continue
            if other.start_at is None or other.end_at is None:
                continue
            if not (float(task.end_at) <= float(other.start_at) or float(other.end_at) <= float(task.start_at)):
                result.append(other_id)
        return result

    def has_conflict(self, task: ScheduledTask) -> bool:
        return bool(self.conflicts_for(task))

    def detect_conflicts(self) -> list[str]:
        """Detecta solapamientos y avisa, sin tocar hora ni fuente (P-04).

        La tarea conserva su estado programado: se marca el aviso y se queda en
        la cola, de modo que la programación nunca se pierde ni se reprograma a
        escondidas. Si al llegar su turno no hay hueco, ``tick`` la deja en
        espera con un mensaje visible.
        """
        changed: list[str] = []
        for task_id, task in self.tasks.items():
            if task.status not in (TaskState.PENDING, TaskState.SCHEDULED, TaskState.CONFLICT):
                continue
            conflicts = self.conflicts_for(task)
            if not conflicts:
                if task.conflicts:
                    task.conflicts = []
                    changed.append(task_id)
                continue
            if task.conflicts != sorted(conflicts):
                changed.append(task_id)
            task.conflicts = sorted(conflicts)
            task.conflict_detected_at = time.time()
            task.message = "se solapa con: " + ", ".join(sorted(conflicts))
            if task.status == TaskState.PENDING:
                task.status = TaskState.SCHEDULED
        if changed:
            self.save()
        return changed

    # --- reloj y tareas perdidas (P-03) ----------------------------------
    def due_tasks(self, now: float | None = None) -> list[ScheduledTask]:
        now = time.time() if now is None else now
        return [
            task
            for task in self.tasks_list()
            if task.status in (TaskState.SCHEDULED, TaskState.PENDING)
            and task.start_at is not None
            and float(task.start_at) <= now + self.config.grace_seconds
        ]

    def missed_tasks(self, now: float | None = None) -> list[ScheduledTask]:
        now = time.time() if now is None else now
        missed: list[ScheduledTask] = []
        for task in self.tasks_list():
            if task.status not in (TaskState.SCHEDULED, TaskState.PENDING, TaskState.CONFLICT):
                continue
            if task.start_at is None:
                continue
            grace = max(self.config.grace_seconds, int(getattr(task.retry_policy, "missed_grace_seconds", 0) or 0))
            if float(task.start_at) + grace < now:
                missed.append(task)
        return missed

    def mark_missed(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        missed_ids: list[str] = []
        for task in self.missed_tasks(now):
            policy = getattr(task.retry_policy, "missed_policy", PolicyMissed.MARK_MISSED)
            if isinstance(policy, str):
                policy = PolicyMissed(policy)
            if policy == PolicyMissed.SKIP:
                task.status = TaskState.SKIPPED
                task.message = "omitida: su instante ya pasó"
            else:
                task.status = TaskState.MISSED
                task.message = "perdida: la aplicación estaba cerrada en su momento"
            task.updated_at = now
            missed_ids.append(task.task_id)
        if missed_ids:
            self.save()
        return missed_ids

    # --- ciclo de ejecución ----------------------------------------------
    def tick(self, now: float | None = None, *, run: bool = True) -> dict[str, Any]:
        """Evalúa el reloj: conflictos, perdidas y lanzamientos. Idempotente."""
        now = time.time() if now is None else now
        events: dict[str, list] = {
            "conflicts": [],
            "missed": [],
            "skipped": [],
            "started": [],
            "failed": [],
        }
        events["conflicts"] = self.detect_conflicts()
        events["missed"] = self.mark_missed(now)
        if not run:
            self.save()
            return events

        for task in self.due_tasks(now):
            if len(self.active) >= max(1, int(self.config.max_concurrent)):
                # Una sola grabación simultánea. La tarea NO se reprograma: queda
                # en espera con el motivo visible y arranca cuando haya hueco.
                task.message = "en espera: ya hay una grabación en curso"
                events["skipped"].append(task.task_id)
                continue
            policy = getattr(task.retry_policy, "missed_policy", PolicyMissed.MARK_MISSED)
            if isinstance(policy, str):
                policy = PolicyMissed(policy)
            try:
                if self.recorder_runner is None:
                    raise SchedulerError("no hay grabador configurado")
                session = self.recorder_runner(task)
            except Exception as exc:
                if task.retries_used < int(getattr(task.retry_policy, "max_retries", 0) or 0):
                    task.retries_used += 1
                    task.message = f"reintento {task.retries_used} programado: {exc}"
                    task.updated_at = now
                    events["skipped"].append(task.task_id)
                else:
                    task.status = TaskState.FAILED
                    task.message = f"falló al iniciar: {exc}"
                    task.updated_at = now
                    events["failed"].append(task.task_id)
                continue
            task.status = TaskState.RUNNING
            task.recording_session_id = (
                getattr(session, "session_id", None) or getattr(session, "task_id", None) or task.task_id
            )
            task.message = "grabación iniciada"
            task.updated_at = now
            self.active.add(task.task_id)
            events["started"].append(task.task_id)

        self.save()
        return events


def tick(scheduler: Scheduler, now: float | None = None, **kwargs: Any) -> dict[str, Any]:
    return scheduler.tick(now, **kwargs)


evaluate = tick
process_due = tick


def run_scheduler(
    store: TaskStore | None = None,
    recorder_runner: Callable[[ScheduledTask], Any] | None = None,
    now: float | None = None,
    **kwargs: Any,
) -> Scheduler:
    scheduler = Scheduler(store=store, recorder_runner=recorder_runner, **kwargs)
    if now is not None:
        scheduler.tick(now)
    return scheduler


Planificador = Scheduler
SchedulerService = Scheduler