"""Gramática validada y evaluador de reglas de colecciones (C-02, C-03)."""

from __future__ import annotations

from dataclasses import is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from thetvview.channel_health import Estado as _EstadoSalud
from thetvview.channel_identity import channel_identity
from thetvview.models import Channel
from thetvview.search import Consulta, estado_catchup, estado_epg, calidad_de

__all__ = [
    "MAX_DEPTH",
    "MAX_NODES",
    "FIELD_TYPES",
    "ALLOWED_OPERATORS",
    "ReglaInvalida",
    "validar_regla",
    "validar_coleccion",
    "normalizar_regla",
    "es_compuesta",
    "evaluar_nodo",
    "evaluar_condicion",
    "evaluar_coleccion",
    "miembros_de_coleccion",
    "valor_campo",
]

MAX_DEPTH = 3
MAX_NODES = 200

# Tipos por campo.
FIELD_TYPES: dict[str, str] = {
    "name": "text",
    "nombre": "text",
    "group": "text",
    "grupo": "text",
    "tvg_id": "text",
    "source_id": "text",
    "fuente": "text",
    "epg_status": "enum",
    "estado_epg": "enum",
    "catchup_status": "enum",
    "estado_catchup": "enum",
    "quality": "enum",
    "calidad": "enum",
    "last_played_at": "date",
    "playback_health": "enum",
    "recent_failure_count": "number",
    "favorites": "enum",
}

TEXT_FIELDS = {"name", "nombre", "group", "grupo", "tvg_id", "source_id", "fuente"}
ENUM_FIELDS = {"epg_status", "estado_epg", "catchup_status", "estado_catchup",
               "quality", "calidad", "playback_health", "favorites"}
DATE_FIELDS = {"last_played_at"}
NUMBER_FIELDS = {"recent_failure_count"}

ALLOWED_OPERATORS: dict[str, set[str]] = {
    "text": {"equals", "contains", "starts_with", "ends_with"},
    "enum": {"equals", "in", "not_equals"},
    "date": {"equals", "before", "after", "within_last"},
    "number": {"equals", "lt", "gt", "le", "ge", "in"},
}


class ReglaInvalida(ValueError):
    pass


def _fail(msg: str) -> None:
    raise ReglaInvalida(msg)


def normalizar_nodo(nodo: Any) -> Any:
    """Normaliza dataclass -> dict."""
    if is_dataclass(nodo):
        # Regla
        if hasattr(nodo, "field"):
            return {"field": nodo.field, "operator": nodo.operator, "value": getattr(nodo, "value", None)}
        if hasattr(nodo, "op"):
            return {nodo.op: getattr(nodo, "rules", [])}
        # Coleccion handled separately
        return dict(getattr(nodo, "__dict__", {}))
    if isinstance(nodo, Mapping):
        return dict(nodo)
    if isinstance(nodo, (list, tuple)):
        return list(nodo)
    return nodo


def es_compuesta(nodo: Any) -> bool:
    n = normalizar_nodo(nodo)
    if isinstance(n, dict):
        return any(k in n for k in ("all", "any", "not"))
    return False


def _is_leaf(nodo: Any) -> bool:
    n = normalizar_nodo(nodo)
    return isinstance(n, dict) and ("field" in n or "campo" in n)


def validar_regla(nodo: Any, *, profundidad: int = 0, contador: list[int] | None = None) -> None:
    if contador is None:
        contador = [0]
    contador[0] += 1
    if contador[0] > MAX_NODES:
        _fail("la regla supera el número máximo de nodos")

    if profundidad > MAX_DEPTH:
        _fail("la regla supera la profundidad máxima")

    n = normalizar_nodo(nodo)

    if es_compuesta(n):
        # identify op
        op = None
        hijos = None
        for key in ("all", "any", "not"):
            if key in n:
                op = key
                val = n[key]
                if key == "not":
                    hijos = [val]
                else:
                    if not isinstance(val, (list, tuple)):
                        _fail(f"{key} debe ser una lista")
                    hijos = list(val)
                break
        if op is None:
            _fail("nodo compuesto inválido")
        if not isinstance(hijos, list) or not hijos:
            _fail(f"{op} requiere al menos un nodo")
        # not single
        if op == "not" and len(hijos) != 1:
            _fail("not requiere exactamente un nodo")
        for h in hijos:
            validar_regla(h, profundidad=profundidad + 1, contador=contador)
        return

    if _is_leaf(n):
        field = n.get("field") if "field" in n else n.get("campo")
        operator = n.get("operator") if "operator" in n else n.get("operador")
        if "field" not in n and "campo" not in n:
            _fail("condición sin campo")
        if "operator" not in n and "operador" not in n:
            _fail("condición sin operador")
        if not isinstance(field, str) or not field:
            _fail("field debe ser texto")
        if field not in FIELD_TYPES:
            _fail(f"campo no permitido: {field}")
        tipo = FIELD_TYPES[field]
        allowed = ALLOWED_OPERATORS.get(tipo, set())
        if operator not in allowed:
            _fail(f"operador no permitido para {field}: {operator}")
        value = n.get("value") if "value" in n else n.get("valor")
        # type checks
        if tipo == "text":
            if not isinstance(value, str):
                _fail("value debe ser texto")
        elif tipo == "enum":
            if operator == "in":
                if not isinstance(value, (list, tuple)):
                    _fail("value de in debe ser lista")
                for v in value:
                    if not isinstance(v, str):
                        _fail("value de in debe ser lista de texto")
            else:
                if operator != "not_equals":
                    if not isinstance(value, str):
                        _fail("value debe ser texto")
        elif tipo == "date":
            if operator == "within_last":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    _fail("within_last requiere número")
            else:
                # date value can be str/datetime/number; just accept convertible later
                pass
        elif tipo == "number":
            if operator == "in":
                if not isinstance(value, (list, tuple)):
                    _fail("value de in debe ser lista")
            else:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    _fail("value debe ser número")
        return

    # unknown node
    _fail("nodo desconocido")


def validar_coleccion(col: Any) -> None:
    c = col
    if hasattr(c, "__dict__") and not isinstance(c, dict):
        # dataclass-ish
        pass
    # accept dict or object
    def get(key):
        if isinstance(c, dict):
            return c.get(key)
        return getattr(c, key, None)

    cid = get("id")
    name = get("name")
    ctype = get("type")
    if not isinstance(cid, str) or not cid.strip():
        raise ReglaInvalida("id vacío")
    if not isinstance(name, str) or not name.strip():
        raise ReglaInvalida("nombre vacío")
    if ctype not in ("manual", "smart", "system"):
        raise ReglaInvalida("tipo de colección inválido")
    rules = get("rules")
    if rules is not None:
        if ctype != "smart":
            raise ReglaInvalida("sólo smart puede tener reglas")
        validar_regla(rules)
    members = get("members")
    if members is not None:
        if not isinstance(members, (list, tuple)):
            raise ReglaInvalida("members debe ser lista")
        if any(not isinstance(m, str) for m in members):
            raise ReglaInvalida("members deben ser texto")
    # store unknown fields? allowed


def normalizar_regla(regla: Any) -> Any:
    return normalizar_nodo(regla)


def _str(v: Any) -> str:
    return "" if v is None else str(v)


def _to_date(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def valor_campo(canal: Channel, field: str, contexto: Mapping[str, Any] | None = None) -> Any:
    contexto = contexto or {}
    f = field
    if f in ("name", "nombre"):
        return canal.name
    if f in ("group", "grupo"):
        return canal.group
    if f == "tvg_id":
        return canal.tvg_id
    if f in ("source_id", "fuente"):
        attrs = getattr(canal, "attrs", None)
        if isinstance(attrs, dict):
            for k in ("source_id", "source", "fuente"):
                if k in attrs and attrs.get(k) is not None:
                    return attrs.get(k)
        return getattr(canal, "source_id", None)
    if f in ("epg_status", "estado_epg"):
        epg = contexto.get("epg")
        idx = contexto.get("indice_epg")
        return estado_epg(canal, epg, indice=idx)
    if f in ("catchup_status", "estado_catchup"):
        return estado_catchup(canal)
    if f in ("quality", "calidad"):
        return calidad_de(canal)
    if f == "last_played_at":
        last = contexto.get("last_played")
        if isinstance(last, Mapping):
            ident = str(channel_identity(canal))
            v = last.get(ident)
            if v is None:
                v = last.get(canal.url)
            return v
        return None
    if f == "recent_failure_count":
        failures = contexto.get("failures")
        if isinstance(failures, Mapping):
            ident = str(channel_identity(canal))
            return int(failures.get(ident, failures.get(canal.url, 0)) or 0)
        return 0
    if f == "playback_health":
        health = contexto.get("health")
        if isinstance(health, Mapping):
            ident = str(channel_identity(canal))
            st = health.get(ident, health.get(canal.url))
            if st is None:
                return "desconocido"
            return str(st).lower()
        return "desconocido"
    if f == "favorites":
        favs = contexto.get("favorites")
        if isinstance(favs, set):
            ident = str(channel_identity(canal))
            return "true" if (ident in favs or canal.url in favs) else "false"
        return "false"
    # fallback attrs
    attrs = getattr(canal, "attrs", None)
    if isinstance(attrs, dict) and f in attrs:
        return attrs.get(f)
    return getattr(canal, f, None)


def evaluar_condicion(canal: Channel, cond: Any, contexto: Mapping[str, Any] | None = None) -> bool:
    n = normalizar_nodo(cond)
    if not isinstance(n, dict):
        return False
    field = n.get("field", n.get("campo"))
    operator = n.get("operator", n.get("operador"))
    value = n.get("value", n.get("valor"))
    actual = valor_campo(canal, str(field), contexto)

    op = operator
    if op in ("equals", "igual"):
        if isinstance(actual, str) and isinstance(value, str):
            return actual.casefold() == value.casefold()
        return actual == value
    if op == "not_equals":
        if isinstance(actual, str) and isinstance(value, str):
            return actual.casefold() != value.casefold()
        return actual != value
    if op == "contains":
        return str(value).casefold() in _str(actual).casefold()
    if op == "starts_with":
        return _str(actual).casefold().startswith(str(value).casefold())
    if op == "ends_with":
        return _str(actual).casefold().endswith(str(value).casefold())
    if op == "in":
        vals = [str(v).casefold() if isinstance(v, str) else v for v in (value or [])]
        if isinstance(actual, str):
            return actual.casefold() in vals
        return actual in (value or [])
    if op == "before":
        d1 = _to_date(actual)
        d2 = _to_date(value)
        return d1 is not None and d2 is not None and d1 < d2
    if op == "after":
        d1 = _to_date(actual)
        d2 = _to_date(value)
        return d1 is not None and d2 is not None and d1 > d2
    if op == "within_last":
        d1 = _to_date(actual)
        if d1 is None:
            return False
        try:
            delta = timedelta(days=float(value))
        except Exception:
            delta = timedelta(days=0)
        now = (contexto or {}).get("now")
        nowdt = _to_date(now) if now is not None else datetime.now(timezone.utc)
        return nowdt - delta <= d1 <= nowdt
    if op in ("lt", "gt", "le", "ge"):
        try:
            a = float(actual)
            b = float(value)
        except (TypeError, ValueError):
            return False
        if op == "lt":
            return a < b
        if op == "gt":
            return a > b
        if op == "le":
            return a <= b
        if op == "ge":
            return a >= b
    return False


def evaluar_nodo(canal: Channel, nodo: Any, contexto: Mapping[str, Any] | None = None) -> bool:
    n = normalizar_nodo(nodo)
    if es_compuesta(n):
        if "all" in n:
            hijos = n["all"]
            return all(evaluacion_simple(canal, h, contexto) for h in hijos) if hijos else True
        if "any" in n:
            hijos = n["any"]
            return any(evaluacion_simple(canal, h, contexto) for h in hijos) if hijos else False
        if "not" in n:
            return not evaluacion_simple(canal, n["not"], contexto)
    # leaf
    if isinstance(n, dict) and ("field" in n or "campo" in n):
        return evaluar_condicion(canal, n, contexto)
    return False


def evaluacion_simple(canal: Channel, nodo: Any, contexto: Mapping[str, Any] | None) -> bool:
    return evaluar_nodo(canal, nodo, contexto)


def _norm_sort(sort: Any) -> tuple[str, str]:
    if isinstance(sort, Mapping):
        field = sort.get("field") or sort.get("campo") or "name"
        direction = str(sort.get("direction") or sort.get("direccion") or "asc").lower()
    elif isinstance(sort, (list, tuple)) and sort:
        field = sort[0]
        direction = str(sort[1]).lower() if len(sort) > 1 else "asc"
    else:
        field = "name"
        direction = "asc"
    return str(field), direction


def _compare_key(canal: Channel, field: str, direction: str) -> Any:
    # For deterministic ordering; missing values sort last.
    if field in ("name", "nombre"):
        raw = canal.name
    elif field in ("group", "grupo"):
        raw = canal.group or ""
    elif field == "tvg_id":
        raw = canal.tvg_id or ""
    else:
        raw = getattr(canal, field, None)
    if isinstance(raw, str):
        return (0 if direction == "asc" else 1, raw.casefold())
    if raw is None:
        return (1, "")
    return (0, raw)


def evaluar_coleccion(coleccion: Any, catalogo: Any, contexto: Mapping[str, Any] | None = None) -> list[Channel]:
    contexto = contexto or {}
    # resolve catalog
    if isinstance(catalogo, (list, tuple)):
        channels = list(catalogo)
    else:
        chans = getattr(catalogo, "channels", None)
        channels = list(chans) if isinstance(chans, (list, tuple)) else list(catalogo or [])

    def get(key):
        if isinstance(coleccion, dict):
            return coleccion.get(key)
        return getattr(coleccion, key, None)

    ctype = get("type")
    # system/manual override: handle separately in manager; here rules or members
    rules = get("rules")
    members = get("members")
    # Determine type of collection
    result: list[Channel] = []

    if ctype == "smart" and rules is not None:
        for ch in channels:
            try:
                if evaluar_nodo(ch, rules, contexto):
                    result.append(ch)
            except Exception:
                # Rechazar silenciosamente canal? Better skip to avoid duplicates issues
                continue
    elif ctype == "manual" and members:
        ident_map = {str(channel_identity(c)): c for c in channels}
        url_map = {c.url: c for c in channels if getattr(c, "url", None)}
        seen = set()
        for ref in members:
            ch = ident_map.get(ref) or url_map.get(ref)
            if ch is not None and id(ch) not in seen:
                seen.add(id(ch))
                result.append(ch)
    else:
        # system: if rules given use; else empty (manager decorates)
        if rules is not None:
            for ch in channels:
                try:
                    if evaluar_nodo(ch, rules, contexto):
                        result.append(ch)
                except Exception:
                    continue
        # system recentes/favoritos are handled by manager context injection of rules; if no rules leave empty.

    # dedup by identity (no duplicate channels)
    deduped: list[Channel] = []
    seen_keys: set[str] = set()
    for ch in result:
        key = str(channel_identity(ch))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(ch)

    # sort
    sort = get("sort")
    if sort:
        field, direction = _norm_sort(sort)
        reverse = direction in ("desc", "descending", "reverse")
        # Python sort stable; we sort ascending keys then reverse if needed
        deduped.sort(key=lambda c: _compare_key(c, field, "asc"), reverse=False)
        if reverse:
            deduped.reverse()
    return deduped


def miembros_de_coleccion(*args, **kwargs):
    return evaluar_coleccion(*args, **kwargs)
