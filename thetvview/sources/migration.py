"""Migración a identidad lógica de fuentes (A-01, A-02). Solo stdlib."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from thetvview import config
from thetvview.models import Channel
from thetvview.storage import json_store

from .model import LogicalChannel, SourceCandidate

__all__ = [
    "hacer_backup",
    "migrar_favoritos",
    "migrar_recientes",
    "migrar_identidad_logica",
    "migrate",
    "migrate_to_logical_channels",
    "LogicalMigrationResult",
]


class LogicalMigrationResult:
    def __init__(self, favorites=None, recents=None, prefs_preserved=True):
        self.favorites = [] if favorites is None else favorites
        self.recents = [] if recents is None else recents
        self.prefs_preserved = prefs_preserved


def hacer_backup(path: Path | str) -> Path | None:
    path = Path(path)
    if not path.exists():
        return None
    stamp = int(time.time())
    dst = path.with_name(path.name + f".bak-{stamp}")
    shutil.copy2(path, dst)
    return dst


def _read_json(path: Path) -> Any:
    try:
        return json_store.read_json_with_recovery(path, schema_version=None)
    except Exception:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None


def _channel_from_mapping(item: dict[str, Any]) -> Channel:
    known = set(Channel.__dataclass_fields__)
    payload = {k: v for k, v in item.items() if k in known}
    try:
        return Channel(**payload)
    except TypeError:
        return Channel(name=str(item.get("name", "")), url=str(item.get("url", "")))


def migrar_favoritos(path: Path | str, backup: bool = True) -> list[LogicalChannel]:
    path = Path(path)
    if backup:
        hacer_backup(path)
    data = _read_json(path)
    out: list[LogicalChannel] = []
    items = data if isinstance(data, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        canal = _channel_from_mapping(item)
        out.append(LogicalChannel.from_channel(canal))
    return out


def migrar_recientes(path: Path | str, backup: bool = True) -> list[LogicalChannel]:
    path = Path(path)
    if backup:
        hacer_backup(path)
    data = _read_json(path)
    out: list[LogicalChannel] = []
    items = data if isinstance(data, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        canal = _channel_from_mapping(item)
        out.append(LogicalChannel.from_channel(canal))
    seen: set[str] = set()
    dedup: list[LogicalChannel] = []
    for lc in out:
        if lc.logical_id in seen:
            continue
        seen.add(lc.logical_id)
        dedup.append(lc)
    return dedup


def migrar_identidad_logica(
    directorio: Path | str,
    *,
    favoritos=None,
    recientes=None,
    prefs=None,
    backup: bool = True,
) -> dict[str, Any]:
    d = Path(directorio)
    if favoritos is None:
        f_path = d / "favorites.json"
    else:
        f_path = Path(favoritos)
    if recientes is None:
        r_path = d / "recents.json"
    else:
        r_path = Path(recientes)
    if prefs is None:
        p_path = d / "prefs.json"
    else:
        p_path = Path(prefs)

    # Fallback to config default only when caller did not provide temp/data dirs and no local files.
    if favoritos is None and not f_path.exists():
        try:
            from thetvview import config as _config
            if hasattr(_config, "FAVORITES_JSON"):
                cfg = Path(_config.FAVORITES_JSON)
                if cfg.exists():
                    f_path = cfg
        except Exception:
            pass
    if recientes is None and not r_path.exists():
        try:
            from thetvview import config as _config
            if hasattr(_config, "RECENTS_JSON"):
                cfg = Path(_config.RECENTS_JSON)
                if cfg.exists():
                    r_path = cfg
        except Exception:
            pass

    res_favs = []
    res_recs = []
    if f_path.exists():
        res_favs = migrar_favoritos(f_path, backup=backup)
    if r_path.exists():
        res_recs = migrar_recientes(r_path, backup=backup)
    if p_path.exists() and backup:
        hacer_backup(p_path)
    destino = d / "logical_candidates.json"
    payload = {
        "logical_channels": [
            {
                "logical_id": lc.logical_id,
                "name": lc.name,
                "tvg_id": lc.tvg_id,
                "source_ids": [c.source_id for c in lc.candidates],
                "rejected_candidate_ids": sorted(lc.rejected_candidate_ids),
            }
            for lc in (res_favs + res_recs)
        ]
    }
    json_store.atomic_write_json(destino, payload, schema_version=None)
    return {
        "favorites": res_favs,
        "recents": res_recs,
        "prefs_preserved": True,
    }


def migrate(*args, **kwargs):
    return migrar_identidad_logica(*args, **kwargs)


def migrate_to_logical_channels(*args, **kwargs):
    return migrar_identidad_logica(*args, **kwargs)
