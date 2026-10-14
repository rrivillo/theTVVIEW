"""Reconciliación de preferencias con capacidades reales (A-09)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = ["PreferenciaNoDisponible", "ResultadoReconciliacion", "reconciliar_preferencias", "reconcile_preferences"]


@dataclass
class PreferenciaNoDisponible:
    preferencia: str
    motivo: str


@dataclass
class ResultadoReconciliacion:
    aplicadas: dict[str, Any] = field(default_factory=dict)
    no_disponibles: list[PreferenciaNoDisponible] = field(default_factory=list)
    confirmadas_por_reproductor: bool = False

    @property
    def modal_discreto(self) -> str:
        if not self.no_disponibles:
            return ""
        partes = [f"{p.preferencia}: {p.motivo}" for p in self.no_disponibles]
        return "Preferencias no disponibles en esta fuente: " + "; ".join(partes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "aplicadas": dict(self.aplicadas),
            "no_disponibles": [{"preferencia": p.preferencia, "motivo": p.motivo} for p in self.no_disponibles],
            "confirmadas_por_reproductor": self.confirmadas_por_reproductor,
        }



def reconciliar_preferencias(preferencias: Any, capacidades: Any, *, confirmado_por_reproductor: bool = False) -> ResultadoReconciliacion:
    prefs = preferencias if isinstance(preferencias, dict) else {}
    caps = capacidades if isinstance(capacidades, dict) else {}
    res = ResultadoReconciliacion(confirmadas_por_reproductor=bool(confirmado_por_reproductor))
    # audio
    audio = prefs.get("audio")
    if audio is not None:
        audios = caps.get("audios") or caps.get("audio_tracks") or []
        if audio in audios:
            res.aplicadas["audio"] = audio
        else:
            res.no_disponibles.append(PreferenciaNoDisponible("audio", "pista no disponible en esta fuente"))
    # subtitles
    subs = prefs.get("subtitles") or prefs.get("subtitulos")
    if subs is not None:
        sub_tracks = caps.get("subtitles") or caps.get("subtitles_tracks") or []
        if subs in sub_tracks:
            res.aplicadas["subtitles"] = subs
        else:
            res.no_disponibles.append(PreferenciaNoDisponible("subtitles", "pista no disponible en esta fuente"))
    # quality
    quality = prefs.get("quality") or prefs.get("calidad")
    if quality is not None:
        qualities = caps.get("qualities") or caps.get("quality") or []
        if quality in qualities:
            res.aplicadas["quality"] = quality
        else:
            res.no_disponibles.append(PreferenciaNoDisponible("quality", "calidad no disponible en esta fuente"))
    # generic booleans pass through when explicitly supported
    for k in ("audio_description", "subtitles_forced"):
        if k in prefs:
            if caps.get(k, False):
                res.aplicadas[k] = prefs[k]
            else:
                res.no_disponibles.append(PreferenciaNoDisponible(k, "no soportado por la fuente"))
    return res


def reconcile_preferences(*args, **kwargs):
    return reconciliar_preferencias(*args, **kwargs)
