"""CRUD y persistencia atómica de colecciones (C-04, C-05, C-07)."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

from thetvview.channel_identity import channel_identity
from thetvview.models import Channel
from thetvview.storage import json_store
from thetvview.storage.schemas import validate_collections

from .rules import ReglaInvalida, evaluar_coleccion, validar_coleccion, validar_regla
from .schema import Coleccion, SCHEMA_VERSION

__all__ = [
    "ColeccionesManager",
    "CollectionsManager",
    "CollectionManager",
    "ColeccionesManagerError",
]


class ColeccionesManagerError(Exception):
    pass


def _read_collections(path: Path) -> list[dict]:
    data = json_store.read_json_with_recovery(path, schema_version=None)
    if data is None:
        return []
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        # allow {"collections": [...]} or {schema_version, collections}
        for key in ("collections", "colecciones", "items"):
            if isinstance(data.get(key), list):
                return [e for e in data[key] if isinstance(e, dict)]
    raise ColeccionesManagerError("formato inesperado (se esperaba lista)")


def _dump_collection(col: Coleccion) -> dict:
    return {
        "schema_version": int(getattr(col, "schema_version", SCHEMA_VERSION)),
        "id": col.id,
        "name": col.name,
        "type": col.type,
        "rules": col.rules,
        "members": list(col.members or []),
        "sort": col.sort,
        "metadata": dict(col.metadata or {}),
    }


def _load_collection(raw: dict) -> Coleccion:
    return Coleccion(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        type=str(raw.get("type") or "manual"),
        rules=raw.get("rules"),
        members=list(raw.get("members") or []),
        sort=raw.get("sort"),
        metadata=dict(raw.get("metadata") or {}),
        schema_version=int(raw.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION),
    )


class ColeccionesManager:
    def __init__(
        self,
        path: str | Path,
        *,
        identity_fn: Callable[[Channel], str] = channel_identity,
        catalog_provider: Callable[[], Any] | None = None,
        context_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.path = Path(path)
        self.identity_fn = identity_fn
        self.catalog_provider = catalog_provider
        #: Callable que devuelve el contexto de evaluación (U-04b).
        self.context_provider = context_provider
        self._cache: list[Coleccion] | None = None
        self._cache_mtime = None
        self._cache_result: dict[str, list[Channel]] = {}
        self._invalidated = True
        self._last_catalog_id = None

    # ----- IO -----

    def _read(self) -> list[Coleccion]:
        raw_items = _read_collections(self.path)
        out = []
        for raw in raw_items:
            try:
                col = _load_collection(raw)
                validar_coleccion(col)
                if col.rules is not None:
                    validar_regla(col.rules)
                out.append(col)
            except (ReglaInvalida, Exception):
                # Saltar entrada inválida sin romper la app.
                continue
        return out

    def load(self) -> list[Coleccion]:
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            mtime = None
        if self._cache is not None and mtime == self._cache_mtime:
            return [self._clone(c) for c in self._cache]
        cols = self._read()
        self._cache = cols
        self._cache_mtime = mtime
        self._invalidated = True
        return [self._clone(c) for c in cols]

    def _clone(self, c: Coleccion) -> Coleccion:
        return Coleccion(
            id=c.id,
            name=c.name,
            type=c.type,
            rules=(None if c.rules is None else dict(c.rules)),
            members=list(c.members or []),
            sort=(None if c.sort is None else dict(c.sort)),
            metadata=dict(c.metadata or {}),
            schema_version=c.schema_version,
        )

    def _write(self, cols: list[Coleccion]) -> None:
        json_store.make_backup(self.path)
        json_store.atomic_write_json(
            self.path,
            [_dump_collection(c) for c in cols],
            schema_version=None,
            validate=validate_collections,
        )
        self._cache = list(cols)
        try:
            self._cache_mtime = self.path.stat().st_mtime
        except OSError:
            self._cache_mtime = None
        self.invalidate()

    def save(self) -> None:
        self._write(self.load())

    # ----- CRUD -----

    def listar(self) -> list[Coleccion]:
        return self.load()

    def list(self) -> list[Coleccion]:
        return self.listar()

    def obtener(self, cid: str) -> Coleccion | None:
        for c in self.load():
            if c.id == cid:
                return self._clone(c)
        return None

    def get(self, cid: str) -> Coleccion | None:
        return self.obtener(cid)

    def crear(
        self,
        name: str,
        *,
        tipo: str = "manual",
        type: str | None = None,
        reglas=None,
        rules=None,
        members=None,
        sort=None,
        cid: str | None = None,
        metadata=None,
    ) -> Coleccion:
        if type is not None:
            tipo = type
        if not isinstance(name, str) or not name.strip():
            raise ColeccionesManagerError("nombre vacío")
        if tipo not in ("manual", "smart", "system"):
            raise ColeccionesManagerError("tipo inválido")
        rid = cid or f"col-{uuid.uuid4().hex[:12]}"
        rules_val = reglas if reglas is not None else rules
        if rules_val is not None:
            validar_regla(rules_val)
        col = Coleccion(
            id=rid,
            name=name.strip(),
            type=tipo,
            rules=rules_val,
            members=list(members or []),
            sort=sort,
            metadata=dict(metadata or {}),
        )
        validar_coleccion(col)
        cols = self.load()
        if any(c.id == rid for c in cols):
            raise ColeccionesManagerError("id duplicado")
        cols.append(col)
        self._write(cols)
        return self._clone(col)

    def create(self, *a, **k):
        return self.crear(*a, **k)

    def renombrar(self, cid: str, nuevo_nombre: str) -> Coleccion:
        if not isinstance(nuevo_nombre, str) or not nuevo_nombre.strip():
            raise ColeccionesManagerError("nombre vacío")
        cols = self.load()
        found = None
        for c in cols:
            if c.id == cid:
                c.name = nuevo_nombre.strip()
                found = c
        if found is None:
            raise ColeccionesManagerError("colección no encontrada")
        self._write(cols)
        return self._clone(found)

    def rename(self, *a, **k):
        return self.renombrar(*a, **k)

    def editar(
        self,
        cid: str,
        *,
        name=None,
        tipo=None,
        type=None,
        rules=None,
        reglas=None,
        members=None,
        sort=None,
        metadata=None,
    ) -> Coleccion:
        cols = self.load()
        found = None
        for c in cols:
            if c.id == cid:
                if name is not None:
                    if not str(name).strip():
                        raise ColeccionesManagerError("nombre vacío")
                    c.name = str(name).strip()
                if tipo is not None or type is not None:
                    nt = type if type is not None else tipo
                    if nt not in ("manual", "smart", "system"):
                        raise ColeccionesManagerError("tipo inválido")
                    c.type = nt
                new_rules = reglas if reglas is not None else rules
                if new_rules is not None or rules is not None or reglas is not None:
                    if new_rules is not None:
                        validar_regla(new_rules)
                        c.rules = new_rules
                if members is not None:
                    if any(not isinstance(m, str) for m in members):
                        raise ColeccionesManagerError("members inválido")
                    c.members = list(members)
                if sort is not None:
                    c.sort = sort
                if metadata is not None:
                    c.metadata = dict(metadata)
                validar_coleccion(c)
                if c.rules is not None:
                    validar_regla(c.rules)
                found = c
        if found is None:
            raise ColeccionesManagerError("colección no encontrada")
        self._write(cols)
        return self._clone(found)

    def edit(self, *a, **k):
        return self.editar(*a, **k)

    def eliminar(self, cid: str) -> bool:
        cols = self.load()
        before = len(cols)
        cols = [c for c in cols if c.id != cid]
        if len(cols) == before:
            return False
        self._write(cols)
        self._cache_result.pop(cid, None)
        return True

    def delete(self, *a, **k):
        return self.eliminar(*a, **k)

    def remove(self, *a, **k):
        return self.eliminar(*a, **k)

    # ----- Manual membership -----

    def identidad(self, canal: Channel) -> str:
        return str(self.identity_fn(canal))

    def añadir(self, cid: str, canal: Channel) -> Coleccion:
        col = self.obtener(cid)
        if col is None or col.type != "manual":
            raise ColeccionesManagerError("colección no manual")
        key = self.identidad(canal)
        if key not in col.members:
            col.members.append(key)
        return self.editar(cid, members=col.members)

    def add(self, *a, **k):
        return self.añadir(*a, **k)

    def add_channel(self, *a, **k):
        return self.añadir(*a, **k)

    def quitar(self, cid: str, canal: Channel | str) -> Coleccion:
        col = self.obtener(cid)
        if col is None or col.type != "manual":
            raise ColeccionesManagerError("colección no manual")
        key = canal if isinstance(canal, str) else self.identidad(canal)
        col.members = [m for m in col.members if m != key]
        return self.editar(cid, members=col.members)

    def remove_channel(self, *a, **k):
        return self.quitar(*a, **k)

    def toggle(self, cid: str, canal: Channel) -> Coleccion:
        col = self.obtener(cid)
        if col is None or col.type != "manual":
            raise ColeccionesManagerError("colección no manual")
        key = self.identidad(canal)
        if key in col.members:
            return self.quitar(cid, key)
        return self.añadir(cid, canal)

    # ----- Evaluation -----

    def invalidar(self, motivo: Any = None) -> None:
        self._invalidated = True
        self._cache_result.clear()

    def invalidate(self, *a, **k):
        return self.invalidar(*a, **k)

    def _catalogo_actual(self, catalogo: Any = None) -> Any:
        if catalogo is not None:
            return catalogo
        if self.catalog_provider is not None:
            try:
                return self.catalog_provider()
            except Exception:
                return None
        return None

    def _catalog_key(self, catalogo: Any) -> int:
        if isinstance(catalogo, (list, tuple)):
            return id(catalogo)
        chans = getattr(catalogo, "channels", None)
        if isinstance(chans, (list, tuple)):
            return id(chans)
        return id(catalogo)

    def _contexto_actual(self, contexto: Any = None) -> Any:
        """El contexto a usar: el que le pasan, o el que aporta la app.

        Un `context_provider` que revienta no puede tumbar la evaluación: se
        degrada a `None`, que es lo que pasaba antes de que existiera y es
        peor que nada —pero no es una mentira, es una ausencia.
        """
        if contexto is not None:
            return contexto
        if self.context_provider is not None:
            try:
                return self.context_provider()
            except Exception:
                return None
        return None

    def miembros(
        self,
        cid: str,
        catalogo: Any = None,
        contexto: Mapping[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> list[Channel]:
        catalogo = self._catalogo_actual(catalogo)
        col = self.obtener(cid)
        if col is None:
            return []
        contexto = self._contexto_actual(contexto)
        ck = self._catalog_key(catalogo)
        cache_key = (cid, ck, id(contexto))
        # Una **manual** resuelve sus `members` contra el catálogo y ya: el
        # contexto no interviene, así que su caché sigue igual que siempre.
        # En cualquier otro tipo el contexto manda, y el contexto cambia sin
        # que cambie la lista (el usuario marca un favorito y el conteo tiene
        # que moverse), mientras que la clave de caché sólo mira la identidad
        # del diccionario. Cachearlo dejaría un número congelado en pantalla,
        # que es justo lo que U-04b viene a arreglar.
        depende_del_contexto = contexto is not None and col.type != "manual"
        if not depende_del_contexto:
            if (not force and not self._invalidated
                    and cache_key in self._cache_result
                    and self._last_catalog_id == ck):
                return list(self._cache_result[cache_key])
        try:
            res = evaluar_coleccion(col, catalogo, contexto)
        except Exception:
            res = []
        # referencias obsoletas para manuales
        if col.type == "manual":
            self._referencias_obsoletas(cid, col, catalogo)
        if not depende_del_contexto:
            self._cache_result[cache_key] = list(res)
            self._last_catalog_id = ck
            self._invalidated = False
        return list(res)

    def members(self, *a, **k):
        return self.miembros(*a, **k)

    def evaluar(self, *a, **k):
        return self.miembros(*a, **k)

    def evaluate(self, *a, **k):
        return self.miembros(*a, **k)

    def resolver_miembros(self, *a, **k):
        return self.miembros(*a, **k)

    def _referencias_obsoletas(self, cid: str, col: Coleccion, catalogo: Any) -> list[str]:
        if isinstance(catalogo, (list, tuple)):
            channels = list(catalogo)
        else:
            chans = getattr(catalogo, "channels", None)
            channels = list(chans) if isinstance(chans, (list, tuple)) else list(catalogo or [])
        ident_map = {str(channel_identity(c)): c for c in channels}
        url_map = {c.url: c for c in channels if getattr(c, "url", None)}
        missing = [m for m in (col.members or []) if m not in ident_map and m not in url_map]
        if missing:
            col.metadata = dict(col.metadata or {})
            col.metadata["obsolete_refs"] = missing
            # persist quietly? avoid rewriting every render: store in cache only; write on next explicit save.
        return missing

    def obsolete_refs(self, cid: str, catalogo: Any = None) -> list[str]:
        col = self.obtener(cid)
        if col is None:
            return []
        catalogo = self._catalogo_actual(catalogo)
        return self._referencias_obsoletas(cid, col, catalogo)


# Aliases
CollectionsManager = ColeccionesManager
CollectionManager = ColeccionesManager

# Minimal validate_collections already registered? Add now.
