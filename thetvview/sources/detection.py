"""Detector determinista de candidatas (A-06). Sin ML ni probabilidades."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from thetvview.models import Channel

__all__ = [
    "NivelConfianza",
    "DeteccionCandidata",
    "CandidateMatch",
    "detectar_candidatas",
    "detectar_candidaturas",
    "detect_candidates",
    "analizar_candidatas",
    "calcular_confianza",
]


@dataclass
class DeteccionCandidata:
    left: Any
    right: Any
    signals: list[str] = field(default_factory=list)
    confidence: str = "low"
    fusionable: bool = True

    @property
    def nivel(self) -> str:
        return self.confidence

    @property
    def signals_out(self) -> list[str]:
        return list(self.signals)


NivelConfianza = str
CandidateMatch = DeteccionCandidata


def _valor_canal(canal: Any, nombre: str) -> Any:
    if isinstance(canal, dict):
        return canal.get(nombre)
    return getattr(canal, nombre, None)


def _nombre_normalizado(canal: Any) -> str:
    return " ".join(str(_valor_canal(canal, "name") or "").casefold().split())


def _grupo(canal: Any) -> str | None:
    g = _valor_canal(canal, "group")
    return str(g).casefold() if g is not None else None


def _tvg(canal: Any) -> str | None:
    v = _valor_canal(canal, "tvg_id")
    return str(v).casefold() if v else None


def _calidad(canal: Any) -> str | None:
    attrs = _valor_canal(canal, "attrs")
    if isinstance(attrs, dict):
        for k in ("quality", "calidad", "resolucion"):
            if k in attrs:
                return str(attrs[k]).casefold()
    return None


def _idioma_region(canal: Any) -> str | None:
    for k in ("language", "idioma", "region"):
        attrs = _valor_canal(canal, "attrs")
        if isinstance(attrs, dict) and k in attrs:
            return str(attrs[k]).casefold()
    name = _nombre_normalizado(canal)
    # region hint from name suffix in brackets sometimes; keep simple.
    return None


def calcular_confianza(signals: list[str]) -> str:
    señales = set(signals)
    if "tvg-id" in señales or len(señales) >= 3:
        return "high"
    if len(señales) >= 2:
        return "medium"
    return "low"


def detectar_candidatas(
    canales: Iterable[Any],
    *,
    epg_index: Any = None,
    locale: Any = None,
) -> list[DeteccionCandidata]:
    lista = list(canales)
    # Build EPG availability set.
    epg_ids: set[str] = set()
    if epg_index is not None:
        if isinstance(epg_index, (set, list, tuple)):
            epg_ids = {str(x).casefold() for x in epg_index}
        elif isinstance(epg_index, dict):
            epg_ids = {str(x).casefold() for x in epg_index.keys()}
        elif hasattr(epg_index, "channels"):
            try:
                epg_ids = {str(x).casefold() for x in getattr(epg_index, "channels")}
            except Exception:
                epg_ids = set()

    resultado: list[DeteccionCandidata] = []
    for i in range(len(lista)):
        for j in range(i + 1, len(lista)):
            a = lista[i]
            b = lista[j]
            signals: list[str] = []

            ta = _tvg(a)
            tb = _tvg(b)
            if ta and tb and ta == tb:
                signals.append("tvg-id")

            na = _nombre_normalizado(a)
            nb = _nombre_normalizado(b)
            if na and na == nb:
                signals.append("name")

            ga = _grupo(a)
            gb = _grupo(b)
            if ga and ga == gb:
                signals.append("group")

            la = _idioma_region(a)
            lb = _idioma_region(b)
            if la and la == lb:
                signals.append("language")

            # EPG evidence: both channels known in EPG
            if epg_ids:
                # tvg id or normalized name
                ae = ta or na
                be = tb or nb
                if ae in epg_ids and be in epg_ids and ae == be:
                    signals.append("epg")

            # Quality distinct => no fusion.
            qa = _calidad(a)
            qb = _calidad(b)
            if qa and qb and qa != qb:
                # do not merge
                continue

            # names equal alone is not enough
            if signals == ["name"]:
                continue
            # no signals at all
            if not signals:
                continue

            # at least 2 signals unless tvg-id present
            if "tvg-id" not in signals and len(set(signals)) < 2:
                continue

            conf = calcular_confianza(signals)
            resultado.append(DeteccionCandidata(left=a, right=b, signals=sorted(set(signals)), confidence=conf))
    return resultado


def detectar_candidaturas(*args, **kwargs):
    return detectar_candidatas(*args, **kwargs)


def detect_candidates(*args, **kwargs):
    return detectar_candidatas(*args, **kwargs)


def analizar_candidatas(*args, **kwargs):
    return detectar_candidatas(*args, **kwargs)
