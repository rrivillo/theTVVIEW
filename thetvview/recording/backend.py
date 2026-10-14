"""Backend de captura para grabación manual (sólo VLC)."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "RecordingState",
    "RecordingStatus",
    "RecordingCapabilities",
    "RecordingSession",
    "RecordingError",
    "can_record",
    "detect_recording_backend",
    "backend_reason",
    "validar_destino",
    "normalizar_nombre_archivo",
    "RecordingSupervisor",
    "RecordingStateMachine",
    "transiciones_validas",
    "guardar_sesion",
]


class RecordingState(str, Enum):
    PENDING_VALIDATION = "pending_validation"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"


RecordingStatus = RecordingState


TRANSICIONES_VALIDAS = {
    RecordingState.PENDING_VALIDATION: {RecordingState.STARTING, RecordingState.FAILED, RecordingState.CANCELLED},
    RecordingState.STARTING: {RecordingState.RECORDING, RecordingState.FAILED, RecordingState.CANCELLED},
    RecordingState.RECORDING: {RecordingState.STOPPING, RecordingState.FAILED, RecordingState.CANCELLED, RecordingState.INCOMPLETE},
    RecordingState.STOPPING: {RecordingState.COMPLETED, RecordingState.FAILED, RecordingState.CANCELLED, RecordingState.INCOMPLETE},
    RecordingState.COMPLETED: set(),
    RecordingState.FAILED: set(),
    RecordingState.CANCELLED: set(),
    RecordingState.INCOMPLETE: set(),
}


def transiciones_validas(origen: Any, destino: Any) -> bool:
    try:
        o = origen if isinstance(origen, RecordingState) else RecordingState(str(origen))
        d = destino if isinstance(destino, RecordingState) else RecordingState(str(destino))
    except ValueError:
        return False
    return d in TRANSICIONES_VALIDAS.get(o, set())


class RecordingStateMachine:
    @staticmethod
    def puede_transicionar(origen, destino) -> bool:
        return transiciones_validas(origen, destino)


class RecordingError(Exception):
    pass


@dataclass
class RecordingCapabilities:
    vlc_available: bool = False
    vlc_path: str | None = None
    mux: str = "mp4"

    def can_record(self) -> bool:
        return self.vlc_available and bool(self.vlc_path)


def detect_recording_backend() -> RecordingCapabilities:
    path = shutil.which("vlc")
    return RecordingCapabilities(vlc_available=path is not None, vlc_path=path)


def can_record() -> bool:
    return detect_recording_backend().can_record()


def backend_reason() -> str:
    caps = detect_recording_backend()
    if caps.can_record():
        return "VLC disponible."
    return "VLC no está instalado; la grabación manual está deshabilitada."


@dataclass
class RecordingSession:
    session_id: str = ""
    source_url: str = ""
    output_dir: Path | str = Path(".")
    requested_name: str = ""
    state: RecordingState = RecordingState.PENDING_VALIDATION
    provisional_path: Path | None = None
    final_path: Path | None = None
    process: Any = None
    pid: int | None = None
    error: str | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    limit_seconds: float | None = None
    cancelled: bool = False
    completed_condition: bool = False

    def transicionar(self, destino: RecordingState) -> None:
        if not transiciones_validas(self.state, destino):
            raise RecordingError(f"transición inválida: {self.state} -> {destino}")
        self.state = destino

    @property
    def done(self) -> bool:
        return self.state in {
            RecordingState.COMPLETED,
            RecordingState.FAILED,
            RecordingState.CANCELLED,
            RecordingState.INCOMPLETE,
        }


def normalizar_nombre_archivo(nombre: str, max_len: int = 128) -> str:
    texto = str(nombre or "").strip()
    texto = texto.replace("\\", "_").replace("/", "_")
    texto = texto.replace("\x00", "")
    base = Path(texto).name
    # Remove path separators definitively.
    base = base.replace(os.sep, "_")
    if os.altsep:
        base = base.replace(os.altsep, "_")
    # Only keep safe-ish chars.
    invalid = '<>:"|?*'
    for ch in invalid:
        base = base.replace(ch, "_")
    if base in ("", ".", ".."):
        raise RecordingError("nombre de archivo vacío o inválido")
    if len(base) > max_len:
        stem = Path(base).stem[: max(1, max_len - 5)]
        suffix = Path(base).suffix[:5]
        base = stem + suffix
    # anti traversal
    if ".." in base or "/" in base or "\\" in base:
        raise RecordingError("nombre de archivo contiene traversal")
    return base


def validar_destino(
    output_dir: Any,
    nombre: str,
    *,
    sobrescribir: bool = False,
    min_free_bytes: int = 1 << 20,
    max_name_len: int = 128,
) -> Path:
    d = Path(output_dir)
    # Ensure directory exists; creation only if parent exists.
    d.mkdir(parents=True, exist_ok=True)
    # Resolve without requiring existence of children; enforce no traversal.
    try:
        real_dir = d.resolve(strict=False)
    except Exception as exc:
        raise RecordingError("directorio inválido") from exc
    if not real_dir.is_absolute():
        raise RecordingError("directorio no absoluto")
    nombre_norm = normalizar_nombre_archivo(nombre, max_len=max_name_len)
    destino = real_dir / nombre_norm
    if not sobrescribir and destino.exists():
        raise RecordingError("el archivo ya existe")
    # Anti traversal check.
    try:
        destino.resolve(strict=False).relative_to(real_dir)
    except Exception as exc:
        raise RecordingError("ruta fuera del destino") from exc
    # free space check
    try:
        usage = shutil.disk_usage(str(real_dir))
        if usage.free < min_free_bytes:
            raise RecordingError("espacio libre insuficiente")
    except Exception as exc:
        raise RecordingError("no se pudo verificar espacio libre") from exc
    return destino


def _comando_vlc(session: RecordingSession, caps: RecordingCapabilities) -> list[str]:
    provisional = session.provisional_path
    finalish = str(provisional)
    return [
        str(caps.vlc_path),
        str(session.source_url),
        "--sout",
        f"#standard{{access=file,mux={caps.mux},dst={finalish}}}",
        "vlc://quit",
    ]


def guardar_sesion(session: RecordingSession) -> None:
    # Placeholder for persistence of metadata; not required by pure DoD.
    return None


class RecordingSupervisor:
    def __init__(self, *, task_manager: Any = None, backend: RecordingCapabilities | None = None, popen_factory: Callable[..., Any] = subprocess.Popen):
        self.task_manager = task_manager
        self.backend = backend or detect_recording_backend()
        self.popen_factory = popen_factory
        self._lock = threading.RLock()

    def prepare(self, session: RecordingSession, *, output_name: str | None = None) -> RecordingSession:
        session.transicionar(RecordingState.STARTING) if session.state == RecordingState.PENDING_VALIDATION else None
        # Actually validation outside; just ensure provisional.
        if session.state == RecordingState.PENDING_VALIDATION:
            session.transicionar(RecordingState.STARTING)
        if session.provisional_path is None:
            name = output_name or session.requested_name or "recording.part"
            if not name.endswith(".part"):
                name = normalizar_nombre_archivo(name) + ".part"
            else:
                name = normalizar_nombre_archivo(name.replace(".part", "")) + ".part"
            session.provisional_path = validar_destino(session.output_dir, name, sobrescribir=True)
        return session

    def start(self, session: RecordingSession, *, output_name: str | None = None) -> RecordingSession:
        if not self.backend.can_record():
            session.error = backend_reason()
            if session.state == RecordingState.PENDING_VALIDATION:
                session.transicionar(RecordingState.FAILED)
            raise RecordingError(session.error)
        if session.state == RecordingState.PENDING_VALIDATION:
            self.prepare(session, output_name=output_name)
        elif session.state == RecordingState.STARTING and session.provisional_path is None:
            self.prepare(session, output_name=output_name)
        if session.state != RecordingState.STARTING:
            raise RecordingError("sesión no está en starting")
        caps = self.backend
        if not caps.vlc_path:
            raise RecordingError("sin backend")
        cmd = _comando_vlc(session, caps)
        proc = self.popen_factory(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        session.process = proc
        try:
            session.pid = getattr(proc, "pid", None)
        except Exception:
            session.pid = None
        session.started_at = time.monotonic()
        session.transicionar(RecordingState.RECORDING)
        return session

    def stop(self, session: RecordingSession, *, timeout: float = 5.0, cancellation: bool = False) -> RecordingSession:
        if session.state not in (RecordingState.RECORDING, RecordingState.STARTING):
            # idempotent: if already terminal, return
            if session.done:
                return session
            raise RecordingError("no está grabando")
        session.transicionar(RecordingState.STOPPING)
        if session.cancelled or cancellation:
            session.cancelled = True
        proc = session.process
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=timeout)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
        session.stopped_at = time.monotonic()
        codigo = None
        if proc is not None:
            codigo = getattr(proc, "returncode", None)
        if session.cancelled:
            session.transicionar(RecordingState.CANCELLED)
            self._marcar_incompleto_o_limpiar(session, keep_part=False)
            return session
        # Condición de cierre: proceso terminó sin señal de fallo (código 0 o None)
        if codigo in (0, None):
            session.completed_condition = True
            if session.provisional_path is not None:
                final = session.provisional_path.with_suffix("")  # remove .part
                if final.exists():
                    final = final.with_name(final.name + ".completed")
                if session.provisional_path.exists():
                    try:
                        session.provisional_path.replace(final)
                    except Exception:
                        final = session.provisional_path
                session.final_path = final
            else:
                session.final_path = None
            session.transicionar(RecordingState.COMPLETED)
        else:
            session.error = f"proceso terminó con código {codigo}"
            session.completed_condition = False
            session.transicionar(RecordingState.INCOMPLETE)
            self._marcar_incompleto_o_limpiar(session, keep_part=True)
        return session

    def cancel(self, session: RecordingSession) -> RecordingSession:
        session.cancelled = True
        if session.state in (RecordingState.RECORDING, RecordingState.STARTING, RecordingState.STOPPING):
            try:
                session.transicionar(RecordingState.CANCELLED)
            except RecordingError:
                pass
        proc = session.process
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._marcar_incompleto_o_limpiar(session, keep_part=False)
        return session

    def _marcar_incompleto_o_limpiar(self, session: RecordingSession, keep_part: bool) -> None:
        part = session.provisional_path
        if part is None:
            return
        if not keep_part:
            try:
                if Path(part).exists():
                    Path(part).unlink()
            except Exception:
                pass
        # descriptors close
        proc = session.process
        if proc is not None:
            for attr in ("stdout", "stderr", "stdin"):
                handle = getattr(proc, attr, None)
                try:
                    if handle is not None and hasattr(handle, "close"):
                        handle.close()
                except Exception:
                    pass

    def cleanup(self, session: RecordingSession) -> None:
        self._marcar_incompleto_o_limpiar(session, keep_part=False)
