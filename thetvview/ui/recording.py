"""Fase 8 U-08 helper module: grabación (autorización y formulario)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from thetvview.recording import backend as recording_backend

__all__ = [
    "puede_grabar",
    "can_record",
    "es_grabacion_permitida",
    "autorizacion_mensaje",
    "autorizacion",
    "primer_modal_grabacion_mensaje",
    "sin_grabacion_mensaje",
    "deshabilitado_mensaje",
    "backend_reason",
    "formulario_mensaje",
    "formulario_grabacion",
    "validar_formulario",
    "validar_destino",
    "normalizar_nombre_archivo",
    "FormularioGrabacion",
    "RecordingState",
    "RecordingStatus",
    "RecordingCapabilities",
    "RecordingSession",
    "RecordingError",
    "RecordingSupervisor",
    "RecordingStateMachine",
    "transiciones_validas",
    "detect_recording_backend",
    "can_record_backend",
    "normalizar_nombre",
    "validar",
    "form",
]


# Re-export backend primitives (single source of truth).
RecordingState = recording_backend.RecordingState
RecordingStatus = recording_backend.RecordingStatus
RecordingCapabilities = recording_backend.RecordingCapabilities
RecordingSession = recording_backend.RecordingSession
RecordingError = recording_backend.RecordingError
RecordingSupervisor = recording_backend.RecordingSupervisor
RecordingStateMachine = recording_backend.RecordingStateMachine
validar_destino = recording_backend.validar_destino
normalizar_nombre_archivo = recording_backend.normalizar_nombre_archivo
transiciones_validas = recording_backend.transiciones_validas
detect_recording_backend = recording_backend.detect_recording_backend
backend_reason = recording_backend.backend_reason

TEXTO_AUTORIZACION = (
    "La grabación captura únicamente las fuentes que el usuario ya abre con su "
    "suscripción.\n"
    "No hay soporte de contenido protegido (DRM)."
)


def puede_grabar() -> bool:
    try:
        return bool(recording_backend.can_record())
    except Exception:
        try:
            return bool(recording_backend.detect_recording_backend().can_record())
        except Exception:
            return False


def can_record() -> bool:
    return puede_grabar()


def es_grabacion_permitida(*args, **kwargs) -> bool:
    return puede_grabar()


def autorizacion_mensaje() -> str:
    return TEXTO_AUTORIZACION


def autorizacion() -> str:
    return autorizacion_mensaje()


def primer_modal_grabacion_mensaje() -> str:
    return autorizacion_mensaje()


def sin_grabacion_mensaje(motivo: str = "") -> str:
    motivo = str(motivo or "").strip() or backend_reason()
    return "La grabación no está disponible: " + motivo


def deshabilitado_mensaje(motivo: str = "") -> str:
    return sin_grabacion_mensaje(motivo)


def can_record_backend() -> bool:
    return puede_grabar()


def formulario_mensaje(destino=None, nombre="", *, duracion=None, **kwargs) -> str:
    partes = ["Destino, nombre y duración de la grabación."]
    if destino is not None:
        partes.append("Destino: " + str(destino))
    if nombre:
        partes.append("Nombre: " + str(nombre))
    if duracion is not None:
        partes.append("Duración: " + str(duracion))
    partes.append("La validación del backend decide si el destino es válido.")
    return "\n".join(partes)


def normalizar_nombre(nombre, *args, **kwargs) -> str:
    return normalizar_nombre_archivo(str(nombre or ""), *args, **kwargs)


class FormularioGrabacion:
    def __init__(self, destino=None, nombre="", duracion=None, sobrescribir=False):
        self.destino = destino
        self.nombre = nombre
        self.duracion = duracion
        self.sobrescribir = bool(sobrescribir)

    @property
    def path(self):
        return self.validar()

    def validar(self):
        if self.duracion is not None:
            try:
                valor = float(self.duracion)
            except (TypeError, ValueError) as exc:
                raise RecordingError("duración inválida") from exc
            if valor <= 0:
                raise RecordingError("duración inválida")
        return validar_destino(self.destino, self.nombre, sobrescribir=self.sobrescribir)

    def to_path(self):
        return self.validar()


def validar_formulario(destino=None, nombre="", *, duracion=None, sobrescribir=False,
                      min_free_bytes=1 << 20, max_name_len=128, **kwargs) -> Any:
    # Aceptar firma flexible: primer arg puede ser objeto con attrs.
    if destino is None and kwargs:
        destino = kwargs.get("output_dir", kwargs.get("dir"))
    if not nombre and kwargs.get("name"):
        nombre = kwargs["name"]
    if duracion is None:
        duracion = kwargs.get("duracion", kwargs.get("duration"))
    # Duración: si viene, debe ser número positivo.
    if duracion is not None:
        try:
            valor = float(duracion)
        except (TypeError, ValueError) as exc:
            raise RecordingError("duración inválida") from exc
        if valor <= 0:
            raise RecordingError("duración inválida")
    return validar_destino(
        destino,
        nombre,
        sobrescribir=bool(sobrescribir or kwargs.get("overwrite", False)),
        min_free_bytes=int(kwargs.get("min_free_bytes", min_free_bytes)),
        max_name_len=int(kwargs.get("max_name_len", max_name_len)),
    )


def validar(*args, **kwargs) -> Any:
    return validar_formulario(*args, **kwargs)


def form(*args, **kwargs) -> Any:
    return validar_formulario(*args, **kwargs)


def formulario_grabacion(*args, **kwargs) -> Any:
    return validar_formulario(*args, **kwargs)
