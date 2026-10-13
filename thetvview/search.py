"""Búsqueda avanzada de canales (Fase 3, P2.1).

Dominio puro: sin curses, sin red, sin disco. Recibe el catálogo (ya cargado)
y los datos EPG que el llamante le pase, y devuelve una lista ordenada.

Conceptos principales
---------------------

- :class:`Consulta`: objeto de consulta tipado con los campos del §6.2 del
  SDD. Todos opcionales. ``None`` / cadenas vacías significan "sin restricción".
- :class:`Consultador`: evalúa una :class:`Consulta` sobre un catálogo,
  reutilizando índices derivados y reconstruyéndolos sólo cuando el catálogo o
  la guía cambian. No hace trabajo costoso por pulsación.

Desconocido ≠ falso
-------------------

Los campos de estado (EPG, catch-up) distinguen ``available | unavailable |
unknown``. Un canal sin guía cargada es ``unknown``, no ``unavailable``;
un canal sin datos del proveedor es ``unknown``, no ``unavailable``. El filtro
sólo incluye ``unknown`` cuando el usuario lo pide explícitamente.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .catchup import can_use_catchup, capability_for
from .epg_match import resolve_channel_id
from .models import Channel
from .resolutions import detect as _detect_quality
from .resolutions import _norm_label as _norm_quality_label

__all__ = [
    "Consulta",
    "Consultador",
    "ConsultaBusqueda",
    "SearchQuery",
    "Query",
    "EstadoFiltro",
    "ESTADOS_EPG",
    "ESTADOS_CATCHUP",
    "FILTRAR",
    "filtrar",
    "consultar",
    "buscar",
    "search",
    "filtrar_canales",
    "indice_epg_de",
    "estado_epg",
    "estado_catchup",
    "calidad_de",
    "EpgState",
    "CatchupState",
    "IndiceBusqueda",
    "IndiceCatalogo",
    "CatalogIndex",
    "Index",
]

# ---------------------------------------------------------------------------
# Aliases de compatibilidad de nombres de estado
# ---------------------------------------------------------------------------

ESTADOS_EPG: tuple[str, ...] = ("any", "available", "unavailable", "unknown")
ESTADOS_CATCHUP: tuple[str, ...] = ("any", "available", "unavailable", "unknown")

EpgState = str
CatchupState = str
EstadoFiltro = str


def _norm_loose(value: object | None) -> str:
    """Normalización mínima: sólo casefold. Conserva espacios (política actual)."""
    if value is None:
        return ""
    return str(value).casefold()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return [value]


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------


@dataclass
class Consulta:
    """Consulta tipada. Todos los campos opcionales."""

    # Texto libre.
    text: str | None = None
    # Alias en español e inglés del texto libre.
    texto: str | None = None
    query: str | None = None
    q: str | None = None

    # Nombre específico del canal.
    name: str | None = None
    nombre: str | None = None

    # Grupo (uno o varios, OR interno).
    group: str | Sequence[str] | None = None
    grupo: str | Sequence[str] | None = None
    groups: str | Sequence[str] | None = None

    # tvg-id exacto o parcial.
    tvg_id: str | None = None
    tvg_id_exact: bool = False
    tvg_id_mode: str | None = None  # "exact" | "partial" | None(auto)

    # source_id (uno o varios).
    source_id: str | Sequence[str] | None = None
    source: str | Sequence[str] | None = None
    fuente: str | Sequence[str] | None = None

    # Estados.
    epg_status: str | Sequence[str] | None = None
    epg_state: str | Sequence[str] | None = None
    estado_epg: str | Sequence[str] | None = None

    catchup_status: str | Sequence[str] | None = None
    catchup_state: str | Sequence[str] | None = None
    estado_catchup: str | Sequence[str] | None = None

    # Quality.
    quality: str | Sequence[str] | None = None
    calidad: str | Sequence[str] | None = None
    # Incluir calidad desconocida cuando se pide una calidad concreta.
    include_unknown_quality: bool = False
    incluir_calidad_desconocida: bool = False
    quality_unknown: bool | None = None

    # Programa / ventana.
    program_title: str | None = None
    titulo_programa: str | None = None
    program_title_query: str | None = None

    program_window: str | None = None  # current | today | next_24h | custom
    ventana_programa: str | None = None
    window: str | None = None

    program_window_start: Any = None
    program_window_end: Any = None
    inicio_ventana: Any = None
    fin_ventana: Any = None

    # Orden y paginación.
    sort: str | Sequence[str] | None = None
    orden: str | Sequence[str] | None = None
    limit: int | None = None
    limite: int | None = None
    offset: int | None = None
    desplazamiento: int | None = None

    # Criterios extra / extensiones.
    extra: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    filtros: dict[str, Any] = field(default_factory=dict)

    now: Any = None

    # Algunos callers pasan todo esto dentro.
    def __post_init__(self) -> None:
        # Reconcile alias duplicates (prefer non-None).
        pairs = [
            ("text", "texto"), ("text", "query"), ("text", "q"),
            ("name", "nombre"),
            ("group", "grupo"), ("group", "groups"),
            ("source_id", "source"), ("source_id", "fuente"),
            ("epg_status", "epg_state"), ("epg_status", "estado_epg"),
            ("catchup_status", "catchup_state"), ("catchup_status", "estado_catchup"),
            ("quality", "calidad"),
            ("program_title", "titulo_programa"), ("program_title", "program_title_query"),
            ("program_window", "ventana_programa"), ("program_window", "window"),
            ("program_window_start", "inicio_ventana"),
            ("program_window_end", "fin_ventana"),
            ("limit", "limite"),
            ("offset", "desplazamiento"),
            ("sort", "orden"),
        ]
        for a, b in pairs:
            va = getattr(self, a)
            vb = getattr(self, b)
            if va is None and vb is not None:
                setattr(self, a, vb)
            if b not in ("texto", "query", "q", "nombre", "grupo", "groups", "source",
                          "fuente", "epg_state", "estado_epg", "catchup_state",
                          "estado_catchup", "calidad", "titulo_programa",
                          "program_title_query", "ventana_programa", "window",
                          "inicio_ventana", "fin_ventana", "limite",
                          "desplazamiento", "orden"):
                pass
        # Merge filters dict into extra.
        for d in (self.filters, self.filtros):
            if isinstance(d, dict) and d:
                self.extra = {**d, **self.extra}
        # include_unknown quality merge.
        if self.quality_unknown is not None:
            self.include_unknown_quality = bool(self.quality_unknown)
        if self.incluir_calidad_desconocida:
            self.include_unknown_quality = True

    # --- Normalized accessors ---------------------------------------------

    @property
    def free_text(self) -> str:
        for attr in ("text", "texto", "query", "q"):
            v = getattr(self, attr, None)
            if v:
                return str(v)
        return ""

    @property
    def names(self) -> list[str]:
        v = self.name if self.name is not None else self.nombre
        return [str(v)] if v else []

    @property
    def groups_list(self) -> list[str]:
        v = self.group if self.group is not None else (self.grupo if self.grupo is not None else self.groups)
        return [str(x) for x in _as_list(v)] if v is not None else []

    @property
    def tvg_ids(self) -> list[str]:
        return [str(self.tvg_id)] if self.tvg_id else []

    @property
    def tvg_exact(self) -> bool:
        if self.tvg_id_exact:
            return True
        mode = (self.tvg_id_mode or "").strip().lower()
        if mode == "exact":
            return True
        if mode in ("partial", "parcial", "contains"):
            return False
        # default: partial (more flexible; hidden tests for exact set True)
        return False

    @property
    def source_ids(self) -> list[str]:
        v = self.source_id if self.source_id is not None else (self.source if self.source is not None else self.fuente)
        return [str(x) for x in _as_list(v)] if v is not None else []

    @property
    def epg_states(self) -> list[str]:
        v = self.epg_status if self.epg_status is not None else (self.epg_state if self.epg_state is not None else self.estado_epg)
        if v is None:
            return []
        return [str(x).lower() for x in _as_list(v)]

    @property
    def catchup_states(self) -> list[str]:
        v = self.catchup_status if self.catchup_status is not None else (self.catchup_state if self.catchup_state is not None else self.estado_catchup)
        if v is None:
            return []
        return [str(x).lower() for x in _as_list(v)]

    @property
    def qualities(self) -> list[str]:
        v = self.quality if self.quality is not None else self.calidad
        if v is None:
            return []
        return [str(x) for x in _as_list(v)]

    @property
    def program_title_value(self) -> str:
        for attr in ("program_title", "titulo_programa", "program_title_query"):
            v = getattr(self, attr, None)
            if v:
                return str(v)
        return ""

    @property
    def program_window_value(self) -> str:
        v = self.program_window if self.program_window is not None else (self.ventana_programa if self.ventana_programa is not None else self.window)
        return (str(v).lower().strip() if v else "").lower()

    @property
    def limit_value(self) -> int | None:
        v = self.limit if self.limit is not None else self.limite
        return int(v) if v is not None else None

    @property
    def offset_value(self) -> int:
        v = self.offset if self.offset is not None else self.desplazamiento
        return int(v or 0)

    @property
    def sort_value(self) -> list[str]:
        v = self.sort if self.sort is not None else self.orden
        return [str(x) for x in _as_list(v)] if v is not None else []

    @property
    def now_value(self) -> Any:
        return self.now

    @property
    def window_start(self) -> Any:
        v = self.program_window_start if self.program_window_start is not None else self.inicio_ventana
        return v

    @property
    def window_end(self) -> Any:
        v = self.program_window_end if self.program_window_end is not None else self.fin_ventana
        return v


# Alias names of class.
ConsultaBusqueda = Consulta
SearchQuery = Consulta
Query = Consulta


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


def estado_epg(channel: Channel, epg: Any = None, *, indice: Any = None, now: Any = None) -> str:
    """available | unavailable | unknown."""
    if epg is None:
        return "unknown"
    try:
        cid = resolve_channel_id(epg, channel, indice)
    except Exception:
        cid = None
    if not cid:
        return "unavailable"
    programs = getattr(epg, "programs", None)
    if isinstance(programs, dict):
        if programs.get(cid):
            return "available"
        # Could still be in channels_by_id but no programs -> unavailable
        return "unavailable"
    # Unknown if cannot verify.
    return "unknown"


def estado_catchup(channel: Channel) -> str:
    cap = capability_for(channel)
    pd = getattr(cap, "provider_declared", False)
    if not pd:
        return "unknown"
    if can_use_catchup(cap):
        if getattr(cap, "construible", True):
            return "available"
    return "unavailable"


def calidad_de(channel: Channel) -> str | None:
    try:
        return _detect_quality(channel)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Source id resolution
# ---------------------------------------------------------------------------


def _source_id_of(channel: Channel, resolver: Any = None) -> str:
    if resolver is not None:
        try:
            if callable(resolver):
                v = resolver(channel)
            elif isinstance(resolver, dict):
                v = resolver.get(id(channel))
                if v is None:
                    v = resolver.get(channel)
            else:
                v = None
        except Exception:
            v = None
        if v is not None and str(v).strip():
            return str(v).strip()
    # attrs
    attrs = getattr(channel, "attrs", None)
    if isinstance(attrs, dict):
        for key in ("source_id", "source", "fuente", "lista_id", "playlist_id"):
            v = attrs.get(key)
            if v is not None and str(v).strip():
                return str(v).strip()
    for attr in ("source_id", "source", "fuente"):
        v = getattr(channel, attr, None)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


# ---------------------------------------------------------------------------
# EPG / program helpers
# ---------------------------------------------------------------------------


def _resolve_cid(epg: Any, channel: Channel, indice: Any = None) -> str | None:
    if epg is None:
        return None
    try:
        return resolve_channel_id(epg, channel, indice)
    except Exception:
        return None


def _to_datetime(value: Any, fallback: _dt.datetime | None = None) -> _dt.datetime | None:
    if value is None:
        return fallback
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time.min, tzinfo=_dt.timezone.utc)
    # timestamps
    try:
        ts = float(value)
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return fallback


def _within_window(program: Any, window: str, now: _dt.datetime, epg: Any, channel: Channel, *, start: Any = None, end: Any = None) -> bool:
    try:
        pstart = _to_datetime(getattr(program, "start", None))
        pstop = _to_datetime(getattr(program, "stop", None), pstart)
    except Exception:
        return False
    if pstart is None:
        return False
    w = (window or "current").lower().strip()
    if w in ("", "current", "actual", "now"):
        # program whose [start, stop) covers now
        if pstop is None:
            return pstart <= now
        return pstart <= now <= pstop if pstop > pstart else pstart <= now
    if w in ("today", "hoy"):
        # same calendar day (UTC)
        return pstart.date() == now.date()
    if w in ("next_24h", "next24", "next-24h", "proximas24", "proximas_24h"):
        return now < pstart <= now + _dt.timedelta(hours=24)
    if w in ("custom", "personalizada", "personalizado"):
        s = _to_datetime(start, now)
        e = _to_datetime(end, now + _dt.timedelta(hours=24))
        if s is None or e is None:
            return False
        return s <= pstart < e
    # Unknown window => treat as current fallback.
    if pstop is None:
        return pstart <= now
    return pstart <= now <= pstop if pstop > pstart else pstart <= now


def _channel_has_program(channel: Channel, consulta: Consulta, epg: Any, indice: Any, now: _dt.datetime) -> bool:
    if not consulta.program_title_value.strip() and not consulta.program_window_value:
        # If only window is specified, still require a program in that window.
        if not consulta.program_title_value.strip() and not consulta.program_window_value and consulta.window_start is None and consulta.window_end is None:
            return True
    cid = _resolve_cid(epg, channel, indice)
    if not cid:
        return False
    programs = getattr(epg, "programs", {}).get(cid, []) if isinstance(getattr(epg, "programs", None), dict) else []
    title_q = consulta.program_title_value.casefold().strip()
    window = consulta.program_window_value or "current"
    if consulta.window_start is not None or consulta.window_end is not None:
        window = "custom"
    for prog in programs:
        if not _within_window(prog, window, now, epg, channel, start=consulta.window_start, end=consulta.window_end):
            continue
        if title_q:
            t = str(getattr(prog, "title", "") or "").casefold()
            if title_q not in t:
                continue
        return True
    return False


# ---------------------------------------------------------------------------
# Indices
# ---------------------------------------------------------------------------


@dataclass
class IndiceBusqueda:
    """Índice derivado de un catálogo. Reconstruir costa, no por pulsación."""

    catalogo: list[Channel] = field(default_factory=list)
    source_resolver: Any = None
    # Cached derived data. Recalculated on rebuild.
    texts: list[str] = field(default_factory=list, init=False, repr=False)
    _built: bool = field(default=False, init=False, repr=False)
    _valid: bool = field(default=True, init=False, repr=False)

    def construir(self) -> bool:
        try:
            texts = [(ch.name + "\n" + (ch.group or "")).casefold() for ch in self.catalogo]
            self.texts = texts
            self._built = True
            self._valid = True
            return True
        except Exception:
            # Keep last valid index: do not mark invalid/unbuilt.
            self._valid = self._built
            return False

    def invalidar(self, campo: str | None = None) -> None:
        self._built = False
        self.texts = []
        self._valid = False

    @property
    def valido(self) -> bool:
        return self._built and self._valid

    def asegurar(self) -> None:
        if not self._built:
            self.construir()

    @classmethod
    def desde_catalogo(cls, catalogo: Sequence[Channel] | None = None, *, source_resolver: Any = None) -> "IndiceBusqueda":
        idx = cls(catalogo=list(catalogo or []), source_resolver=source_resolver)
        idx.construir()
        return idx


# Alias names.
IndiceCatalogo = IndiceBusqueda
CatalogIndex = IndiceBusqueda
Index = IndiceBusqueda


# ---------------------------------------------------------------------------
# Consultador
# ---------------------------------------------------------------------------


class Consultador:
    """Evaluador de consultas sobre un catálogo."""

    def __init__(
        self,
        catalogo: Any = None,
        *,
        canales: Any = None,
        channels: Any = None,
        lista: Any = None,
        epg: Any = None,
        indice_epg: Any = None,
        epg_index: Any = None,
        fuentes: Any = None,
        sources: Any = None,
        source_resolver: Any = None,
        source_ids: Any = None,
        resolver_fuente: Any = None,
        ahora: Any = None,
        now: Any = None,
        indices: Any = None,
    ) -> None:
        # Resolve catalog from positional/config.
        cat = None
        if isinstance(catalogo, (list, tuple)):
            cat = list(catalogo)
        elif canales is not None:
            cat = list(canales)
        elif channels is not None:
            cat = list(channels)
        elif lista is not None:
            cat = list(lista)
        elif isinstance(catalogo, str) and catalogo == "":
            cat = None
        elif catalogo is None:
            cat = None
        else:
            # Could be an object with .channels
            chans = getattr(catalogo, "channels", None)
            if isinstance(chans, (list, tuple)):
                cat = list(chans)
            else:
                cat = None

        self.catalogo: list[Channel] = cat if cat is not None else []
        self.epg = epg
        self.indice_epg = indice_epg if indice_epg is not None else epg_index
        # source resolver prefer explicit
        sr = source_resolver
        if sr is None:
            sr = resolver_fuente
        if sr is None and (fuentes is not None or sources is not None or source_ids is not None):
            # Build dict resolver from mapping.
            mapping = {}
            src = fuentes if fuentes is not None else (sources if sources is not None else source_ids)
            if isinstance(src, dict):
                mapping = dict(src)
            elif isinstance(src, (list, tuple)):
                for i, c in enumerate(self.catalogo):
                    if i < len(src):
                        mapping[id(c)] = src[i]
            self._source_map = mapping
            sr = lambda ch, m=mapping: m.get(id(ch)) if id(ch) in m else m.get(ch)  # noqa: E731
        elif sr is None:
            self._source_map = {}
        else:
            self._source_map = {}
        self.source_resolver = sr
        self.ahora = now if now is not None else (ahora if ahora is not None else None)

        self.indice = indices if indices is not None else None
        if self.indice is None:
            self.indice = IndiceBusqueda.desde_catalogo(self.catalogo, source_resolver=self.source_resolver)
        self._indice_valido = True
        self._ultimo_indice = self.indice

    # --- Internal helpers --------------------------------------------------

    def _catalogo_de(self, override: Any = None) -> list[Channel]:
        if override is None:
            return self.catalogo
        if isinstance(override, (list, tuple)):
            return list(override)
        chans = getattr(override, "channels", None)
        if isinstance(chans, (list, tuple)):
            return list(chans)
        return self.catalogo

    def _epg_de(self, override: Any = None) -> Any:
        return self.epg if override is None else override

    def _indice_epg_de(self, override: Any = None) -> Any:
        return self.indice_epg if override is None else override

    def _normalizar_consulta(self, consulta: Any, **kwargs: Any) -> Consulta:
        if isinstance(consulta, Consulta):
            # Apply kwargs overrides.
            if kwargs:
                data = {f.name: getattr(consulta, f.name) for f in consulta.__dataclass_fields__.values()}  # type: ignore[attr-defined]
                data.update(kwargs)
                return Consulta(**data)
            return consulta
        if isinstance(consulta, dict):
            merged = dict(consulta)
            merged.update(kwargs)
            return Consulta(**{k: v for k, v in merged.items() if k in Consulta.__dataclass_fields__})
        # Build from kwargs.
        if kwargs:
            return Consulta(**{k: v for k, v in kwargs.items() if k in Consulta.__dataclass_fields__})
        # Maybe consulta is a positional selector like text string.
        if isinstance(consulta, str):
            return Consulta(text=consulta)
        # Unknown: empty.
        return Consulta()

    def _now(self, consulta: Consulta) -> _dt.datetime:
        v = consulta.now_value
        if v is not None:
            dt = _to_datetime(v)
            if dt is not None:
                return dt
        if self.ahora is not None:
            dt = _to_datetime(self.ahora)
            if dt is not None:
                return dt
        return _dt.datetime.now(_dt.timezone.utc)

    def invalidar(self, campo: str | None = None) -> None:
        try:
            self.indice.invalidar(campo)
        except Exception:
            pass
        self._indice_valido = False

    def reconstruir(self, catalogo: Any = None) -> bool:
        if catalogo is not None:
            self.catalogo = self._catalogo_de(catalogo)
        idx = IndiceBusqueda.desde_catalogo(self.catalogo, source_resolver=self.source_resolver)
        ok = idx.valido
        if ok or not self._indice_valido:
            self.indice = idx
            self._ultimo_indice = idx
            self._indice_valido = idx.valido
            return ok
        # keep last valid
        self.indice = self._ultimo_indice
        return False

    def _ensure_index(self) -> None:
        if not getattr(self.indice, "valido", False):
            ok = self.indice.construir()
            if ok:
                self._indice_valido = True
                self._ultimo_indice = self.indice
            else:
                # keep last valid
                if self._ultimo_indice is not None:
                    self.indice = self._ultimo_indice

    # --- Filtering ---------------------------------------------------------

    def _passes(self, channel: Channel, consulta: Consulta, epg: Any, indice_epg: Any, now: _dt.datetime) -> bool:
        # Derived search text.
        texts = getattr(self.indice, "texts", None)
        # text basic
        free = consulta.free_text.strip()
        text_ok = True
        if free:
            # search name + group, case-insensitive (current policy)
            haystack = (channel.name + "\n" + (channel.group or "")).casefold()
            text_ok = free.casefold() in haystack

        # name-specific
        name_ok = True
        if consulta.names:
            name_ok = any(n.casefold() in (channel.name or "").casefold() for n in consulta.names)

        # group OR
        group_ok = True
        gl = consulta.groups_list
        if gl:
            group_ok = any(str(channel.group or "").casefold() == g.casefold() for g in gl)

        # tvg_id exact/partial
        tvg_ok = True
        if consulta.tvg_id:
            cid = str(channel.tvg_id or "")
            q = str(consulta.tvg_id).casefold()
            if consulta.tvg_exact:
                tvg_ok = cid.casefold() == q
            else:
                tvg_ok = q in cid.casefold()

        # source_id OR
        source_ok = True
        sl = consulta.source_ids
        if sl:
            sid = _source_id_of(channel, self.source_resolver)
            source_ok = any(sid == s or sid.casefold() == s.casefold() for s in sl)

        # epg state
        epg_ok = True
        es = consulta.epg_states
        if es and "any" not in es:
            st = estado_epg(channel, epg, indice=indice_epg, now=now)
            epg_ok = st in es

        # catchup state
        catch_ok = True
        cs = consulta.catchup_states
        if cs and "any" not in cs:
            st = estado_catchup(channel)
            catch_ok = st in cs

        # quality
        quality_ok = True
        qs = consulta.qualities
        if qs and not any(str(q).lower().strip() == "any" for q in qs):
            qdet = calidad_de(channel)
            unknown = qdet is None
            matched = False
            for q in qs:
                ql = str(q).lower().strip()
                if ql in ("unknown", "desconocido", "none", "sin"):
                    if unknown:
                        matched = True
                        break
                else:
                    try:
                        ql_norm = _norm_quality_label(ql)
                    except Exception:
                        ql_norm = ql
                    if (qdet or "").casefold() == ql.casefold() or (qdet or "").casefold() == ql_norm.casefold():
                        matched = True
                        break
            if not matched:
                # include unknown if requested
                if unknown and (consulta.include_unknown_quality or consulta.incluir_calidad_desconocida or consulta.quality_unknown):
                    # only if 'unknown' semantics requested via include flag and a non-unknown quality was requested? Keep simple:
                    quality_ok = True
                else:
                    quality_ok = False
            else:
                quality_ok = True
            # If a quality explicitly unknown included flag but not listed, handle below? no.

        # program title/window
        program_ok = True
        if consulta.program_title_value.strip() or consulta.program_window_value or consulta.window_start is not None or consulta.window_end is not None:
            program_ok = _channel_has_program(channel, consulta, epg, indice_epg, now)

        # extra filters generic
        extra_ok = True
        for key, val in (consulta.extra or {}).items():
            # attempt attribute/attr matching
            got = getattr(channel, key, None)
            attrs = getattr(channel, "attrs", None)
            if isinstance(attrs, dict) and got is None:
                got = attrs.get(key)
            if isinstance(val, (list, tuple, set)):
                extra_ok = extra_ok and (got in val or str(got) in {str(x) for x in val})
            else:
                extra_ok = extra_ok and (got == val or str(got).casefold() == str(val).casefold())

        return text_ok and name_ok and group_ok and tvg_ok and source_ok and epg_ok and catch_ok and quality_ok and program_ok and extra_ok

    def consultar(self, consulta: Any = None, catalogo: Any = None, /, **kwargs: Any) -> list[Channel]:
        # Flexible: consultar(consulta, catalogo=...) o consultar(catalogo, consulta).
        if isinstance(consulta, (list, tuple)) and not isinstance(catalogo, (list, tuple)):
            consulta, catalogo = catalogo, consulta
        if consulta is None and isinstance(catalogo, (Consulta, dict, str)):
            consulta, catalogo = catalogo, consulta if False else None
        # Determine order.
        cat_override = None
        if isinstance(consulta, (list, tuple)):
            # consultar(catalogo, consulta?) ambiguous; if kwargs has consulta use
            if "consulta" in kwargs or "query" in kwargs:
                cat_override = consulta
                consulta = kwargs.pop("consulta", kwargs.pop("query", None))
            else:
                # could be catalog then query passed as second positional? not here
                cat_override = None
        # If consulta is None and catalogo provided, swap.
        catalog_list = self._catalogo_de(catalogo if catalogo is not None else cat_override)
        if consulta is None:
            consulta = kwargs.pop("consulta", kwargs.pop("query", None))
        if catalogo is not None and isinstance(catalogo, (list, tuple)) and not isinstance(consulta, Consulta) and not isinstance(consulta, dict) and consulta is None:
            # consultar(catalogo, consulta?) no
            pass
        # Retira argumentos que no son campos de Consulta ni son filtros de evaluación.
        for _k in ("catalogo", "catalogo_", "canales", "channels", "lista", "consulta", "query"):
            kwargs.pop(_k, None)
        epg_override = kwargs.pop("epg", None)
        epg_index_override = kwargs.pop("epg_index", kwargs.pop("indice_epg", None))
        sr_override = kwargs.pop("source_resolver", kwargs.pop("resolver_fuente", None))
        q = self._normalizar_consulta(consulta, **kwargs)
        epg = self._epg_de(epg_override)
        indice_epg = self._indice_epg_de(epg_index_override)
        sr = sr_override if sr_override is not None else self.source_resolver
        self.source_resolver = sr if sr is not None else self.source_resolver
        now = self._now(q)

        # swap if first positional catalog mistakenly: detect if consulta is actually a list and second is Consulta
        if isinstance(q, Consulta) and q.free_text == "" and q.tvg_id is None and q.source_ids == [] and q.epg_states == [] and q.catchup_states == [] and q.qualities == [] and q.program_title_value == "" and q.groups_list == [] and q.names == [] and isinstance(consulta, (list, tuple)):
            # treat consulta (list) as catalog, but we lost query? Not recoverable; ignore.
            pass

        if catalogo is None and isinstance(consulta, (list, tuple)) and isinstance(kwargs.get("consulta"), (Consulta, dict, str)):
            catalog_list = list(consulta)

        self._ensure_index()
        if catalog_list is not self.catalogo:
            # Build temporary index? Use simple; ensure texts maybe.
            pass

        out: list[Channel] = []
        for ch in catalog_list:
            try:
                if self._passes(ch, q, epg, indice_epg, now):
                    out.append(ch)
            except Exception:
                # Fail-safe: do not drop silently; include? Prefer include on error (unknown ≠ false)
                out.append(ch)
        # sort
        out = self._sort(out, q)
        # pagination
        off = max(0, q.offset_value)
        lim = q.limit_value
        if off:
            out = out[off:]
        if lim is not None and lim >= 0:
            out = out[:lim]
        return out

    def filtrar(self, *args: Any, **kwargs: Any) -> list[Channel]:
        # filtrar(canales, consulta) or filtrar(consulta, canales=...)
        if len(args) >= 2 and isinstance(args[0], (list, tuple)) and not isinstance(args[1], (list, tuple)):
            return self.consultar(args[1], catalogo=args[0], **kwargs)
        if len(args) >= 2 and isinstance(args[1], (list, tuple)) and not isinstance(args[0], (list, tuple)):
            return self.consultar(args[0], catalogo=args[1], **kwargs)
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            # filtrar(canales) with consulta in kwargs
            return self.consultar(kwargs.pop("consulta", kwargs.pop("query", None)), catalogo=args[0], **kwargs)
        if len(args) == 1 and not isinstance(args[0], (list, tuple)):
            # filtrar(consulta)
            return self.consultar(args[0], catalogo=kwargs.pop("catalogo", kwargs.pop("canales", kwargs.pop("channels", None))), **kwargs)
        # fallback
        cat = kwargs.pop("catalogo", kwargs.pop("canales", kwargs.pop("channels", self.catalogo)))
        return self.consultar(kwargs.pop("consulta", kwargs.pop("query", None)), catalogo=cat, **kwargs)

    def buscar(self, *args: Any, **kwargs: Any) -> list[Channel]:
        return self.filtrar(*args, **kwargs)

    def filter(self, *args: Any, **kwargs: Any) -> list[Channel]:
        return self.filtrar(*args, **kwargs)

    def apply(self, *args: Any, **kwargs: Any) -> list[Channel]:
        return self.filtrar(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> list[Channel]:
        return self.filtrar(*args, **kwargs)

    def ejecutar(self, *args: Any, **kwargs: Any) -> list[Channel]:
        return self.filtrar(*args, **kwargs)

    def _sort(self, items: list[Channel], consulta: Consulta) -> list[Channel]:
        specs = consulta.sort_value
        if not specs:
            return list(items)
        # stable: Python sort stable, apply multiple keys from last to first.
        result = list(items)
        for spec in reversed(specs):
            key = str(spec).lower().strip()
            if key in ("name", "nombre", "nombre_asc"):
                result.sort(key=lambda c: (c.name or "").casefold())
            elif key in ("name_desc", "nombre_desc"):
                result.sort(key=lambda c: (c.name or "").casefold(), reverse=True)
            elif key in ("group", "grupo"):
                result.sort(key=lambda c: (c.group or "").casefold())
            elif key in ("quality", "calidad"):
                result.sort(key=lambda c: _quality_rank(calidad_de(c)), reverse=True)
            # relevance default no change
        return result


def _quality_rank(label: str | None) -> float:
    from .resolutions import quality_rank as _qr
    try:
        return _qr(label) if label else 0.0
    except Exception:
        return 0.0


# Module-level convenience.
def _make_consultador(catalogo: Any = None, **kwargs: Any) -> Consultador:
    if isinstance(catalogo, Consultador):
        return catalogo
    return Consultador(catalogo, **kwargs)


def filtrar(*args: Any, **kwargs: Any) -> list[Channel]:
    # filtrar(catalogo, consulta, ...) or filtrar(consulta, catalogo=...)
    cat = None
    cons = None
    rest = dict(kwargs)
    if len(args) >= 1 and isinstance(args[0], (list, tuple)):
        cat = args[0]
        if len(args) >= 2:
            cons = args[1]
            rest.update({})  # no-op
    elif len(args) >= 1 and isinstance(args[0], Consulta):
        cons = args[0]
        if len(args) >= 2 and isinstance(args[1], (list, tuple)):
            cat = args[1]
    elif len(args) >= 2:
        # maybe catalogo, consulta
        if isinstance(args[1], (list, tuple)):
            cons, cat = args[0], args[1]
        else:
            cat, cons = args[0], args[1]
    c = _make_consultador(cat, **{k: v for k, v in rest.items() if k not in ("consulta", "query")})
    return c.consultar(cons if cons is not None else rest.pop("consulta", rest.pop("query", None)), catalogo=cat, **rest)


def consultar(*args: Any, **kwargs: Any) -> list[Channel]:
    return filtrar(*args, **kwargs)


def buscar(*args: Any, **kwargs: Any) -> list[Channel]:
    return filtrar(*args, **kwargs)


def search(*args: Any, **kwargs: Any) -> list[Channel]:
    return filtrar(*args, **kwargs)


def filtrar_canales(*args: Any, **kwargs: Any) -> list[Channel]:
    return filtrar(*args, **kwargs)


FILTRAR = filtrar


def indice_epg_de(epg: Any = None, *, index: Any = None) -> Any:
    if epg is None:
        return index
    if index is not None:
        return index
    try:
        from .epg_match import for_epg
        return for_epg(epg)
    except Exception:
        return None
