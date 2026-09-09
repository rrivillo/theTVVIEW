"""Proveedor Xtream: auth, categorías, canales, VOD, series, short_epg.

Solo stdlib. Toda la lógica Xtream vive aquí; el dominio existente
(Channel, etc.) se consume después vía el normalizador (xtream_models).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .xtream_client import api_call
from .xtream_config import XtreamConfig
from .xtream_errors import AuthenticationError, InvalidSourceError, ProviderError
from .xtream_security import build_api_url, build_stream_url, normalize_server_url


# --- TTLs de cache (en segundos) -------------------------------------------

CATEGORY_TTL: float = 24 * 3600   # 24 h
CHANNEL_TTL: float = 6 * 3600     # 6 h
EPG_TTL: float = 2 * 3600         # 2 h


# --- Modelos de respuesta de la API Xtream ----------------------------------

@dataclass
class XtreamCategory:
    """Categoría del proveedor (live, vod o series)."""

    category_id: str
    name: str
    parent_id: int = 0


@dataclass
class XtreamStream:
    """Elemento de stream (live, vod o series) tal cual responde la API."""

    num: int | str = ""
    name: str = ""
    stream_type: str = ""
    stream_id: int | str = ""
    stream_icon: str = ""
    epg_channel_id: str | None = None
    added: str = ""
    category_id: str = ""
    category_ids: list[int] = field(default_factory=list)
    custom_sid: str = ""
    tv_archive: int = 0
    direct_source: str = ""
    tv_archive_duration: int = 0
    # Campos adicionales que la API pueda traer
    rating: str | None = None
    rating_5based: float | None = None
    backdrop_path: list[str] = field(default_factory=list)
    plot: str | None = None
    cast: str | None = None
    director: str | None = None
    genre: str | None = None
    releaseDate: str | None = None
    duration_secs: int | None = None
    duration: str | None = None


@dataclass
class XtreamSeriesInfo:
    """Info extendida de una serie (get_series_info)."""

    info: dict = field(default_factory=dict)
    seasons: list[dict] = field(default_factory=list)
    episodes: dict = field(default_factory=dict)


# --- Funciones de cache local -----------------------------------------------

def _cache_dir() -> Path:
    d = config.XTREAM_CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(server_url: str, username: str) -> str:
    raw = f"{normalize_server_url(server_url)}:{username}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_path(key: str, kind: str) -> Path:
    return _cache_dir() / f"{key}_{kind}.json"


def _is_fresh(path: Path, ttl: float) -> bool:
    if not path.is_file():
        return False
    try:
        return (time.time() - path.stat().st_mtime) < ttl
    except OSError:
        return False


def _read_cache(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, data: dict | list) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _clear_cache(server_url: str, username: str) -> None:
    """Elimina toda la cache de un proveedor (al borrar la entrada)."""
    key = _cache_key(server_url, username)
    for kind in ("auth", "live_cat", "live", "vod_cat", "vod", "series_cat", "series"):
        p = _cache_path(key, kind)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


# --- API Xtream -------------------------------------------------------------

def authenticate(cfg: XtreamConfig, *, force_refresh: bool = False) -> dict:
    """Autentica y devuelve la info del servidor/usuario.

    Cache: 24h (forzable con force_refresh).
    Lanza AuthenticationError si las credenciales son inválidas.
    """
    key = _cache_key(cfg.server_url, cfg.username)
    cp = _cache_path(key, "auth")

    if not force_refresh and _is_fresh(cp, CATEGORY_TTL):
        cached = _read_cache(cp)
        if cached is not None:
            return cached

    url = build_api_url(cfg.server_url, cfg.username, cfg.password, "auth")
    data = api_call(url)

    # Validar respuesta: Xtream devuelve {"user_info": {...}, "server_info": {...}}
    if not isinstance(data, dict):
        raise InvalidSourceError("Respuesta inesperada del servidor (no es un objeto JSON).")
    # Algunos paneles devuelven {"login":false,"message":"invalid user, pass."} con 200.
    if data.get("login") is False:
        raise AuthenticationError(
            "Usuario o contraseña inválidos (el servidor rechazó el login)."
        )
    user_info = data.get("user_info")
    if not isinstance(user_info, dict):
        raise AuthenticationError("Credenciales inválidas o servidor no Xtream.")

    # Xtream estándar: user_info.auth == 0 => login incorrecto/expirado.
    try:
        auth_flag = user_info.get("auth")
        if auth_flag is not None and int(auth_flag) == 0:
            raise AuthenticationError(
                "Usuario o contraseña inválidos (el servidor rechazó el login)."
            )
    except (TypeError, ValueError):
        pass

    status = str(user_info.get("status", "")).lower()
    if status in ("disabled", "banned", "inactive"):
        raise AuthenticationError(
            f"Tu cuenta está {status}. Contacta al proveedor."
        )

    _write_cache(cp, data)
    return data


def get_live_categories(cfg: XtreamConfig, *, force_refresh: bool = False) -> list[XtreamCategory]:
    """Obtiene las categorías de canales en vivo."""
    key = _cache_key(cfg.server_url, cfg.username)
    cp = _cache_path(key, "live_cat")

    if not force_refresh and _is_fresh(cp, CATEGORY_TTL):
        cached = _read_cache(cp)
        if cached is not None:
            return [XtreamCategory(**c) for c in cached]

    url = build_api_url(cfg.server_url, cfg.username, cfg.password, "get_live_categories")
    data = api_call(url)
    if not isinstance(data, list):
        return []

    cats = [XtreamCategory(**c) for c in data if isinstance(c, dict)]
    _write_cache(cp, [c.__dict__ for c in cats])
    return cats


def get_live_streams(
    cfg: XtreamConfig,
    category_id: int | str | None = None,
    *,
    force_refresh: bool = False,
) -> list[XtreamStream]:
    """Obtiene la lista de canales en vivo, opcionalmente filtrado por categoría."""
    key = _cache_key(cfg.server_url, cfg.username)
    cp = _cache_path(key, "live")

    if not force_refresh and _is_fresh(cp, CHANNEL_TTL):
        cached = _read_cache(cp)
        if cached is not None:
            streams = [XtreamStream(**s) for s in cached if isinstance(s, dict)]
            if category_id is not None:
                cat_str = str(category_id)
                streams = [s for s in streams if str(s.category_id) == cat_str]
            return streams

    suffix = f"&category_id={category_id}" if category_id is not None else ""
    url = build_api_url(cfg.server_url, cfg.username, cfg.password, "get_live_streams") + suffix
    data = api_call(url)
    if not isinstance(data, list):
        return []

    streams = [XtreamStream(**s) for s in data if isinstance(s, dict)]
    _write_cache(cp, [s.__dict__ for s in streams])
    return streams


def get_vod_categories(cfg: XtreamConfig, *, force_refresh: bool = False) -> list[XtreamCategory]:
    """Obtiene las categorías de VOD (películas)."""
    key = _cache_key(cfg.server_url, cfg.username)
    cp = _cache_path(key, "vod_cat")

    if not force_refresh and _is_fresh(cp, CATEGORY_TTL):
        cached = _read_cache(cp)
        if cached is not None:
            return [XtreamCategory(**c) for c in cached]

    url = build_api_url(cfg.server_url, cfg.username, cfg.password, "get_vod_categories")
    data = api_call(url)
    if not isinstance(data, list):
        return []

    cats = [XtreamCategory(**c) for c in data if isinstance(c, dict)]
    _write_cache(cp, [c.__dict__ for c in cats])
    return cats


def get_vod_streams(
    cfg: XtreamConfig,
    category_id: int | str | None = None,
    *,
    force_refresh: bool = False,
) -> list[XtreamStream]:
    """Obtiene la lista de películas VOD."""
    key = _cache_key(cfg.server_url, cfg.username)
    cp = _cache_path(key, "vod")

    if not force_refresh and _is_fresh(cp, CHANNEL_TTL):
        cached = _read_cache(cp)
        if cached is not None:
            streams = [XtreamStream(**s) for s in cached if isinstance(s, dict)]
            if category_id is not None:
                cat_str = str(category_id)
                streams = [s for s in streams if str(s.category_id) == cat_str]
            return streams

    suffix = f"&category_id={category_id}" if category_id is not None else ""
    url = build_api_url(cfg.server_url, cfg.username, cfg.password, "get_vod_streams") + suffix
    data = api_call(url)
    if not isinstance(data, list):
        return []

    streams = [XtreamStream(**s) for s in data if isinstance(s, dict)]
    _write_cache(cp, [s.__dict__ for s in streams])
    return streams


def get_series_categories(cfg: XtreamConfig, *, force_refresh: bool = False) -> list[XtreamCategory]:
    """Obtiene las categorías de series."""
    key = _cache_key(cfg.server_url, cfg.username)
    cp = _cache_path(key, "series_cat")

    if not force_refresh and _is_fresh(cp, CATEGORY_TTL):
        cached = _read_cache(cp)
        if cached is not None:
            return [XtreamCategory(**c) for c in cached]

    url = build_api_url(cfg.server_url, cfg.username, cfg.password, "get_series_categories")
    data = api_call(url)
    if not isinstance(data, list):
        return []

    cats = [XtreamCategory(**c) for c in data if isinstance(c, dict)]
    _write_cache(cp, [c.__dict__ for c in cats])
    return cats


def get_series(
    cfg: XtreamConfig,
    category_id: int | str | None = None,
    *,
    force_refresh: bool = False,
) -> list[XtreamStream]:
    """Obtiene la lista de series."""
    key = _cache_key(cfg.server_url, cfg.username)
    cp = _cache_path(key, "series")

    if not force_refresh and _is_fresh(cp, CHANNEL_TTL):
        cached = _read_cache(cp)
        if cached is not None:
            streams = [XtreamStream(**s) for s in cached if isinstance(s, dict)]
            if category_id is not None:
                cat_str = str(category_id)
                streams = [s for s in streams if str(s.category_id) == cat_str]
            return streams

    suffix = f"&category_id={category_id}" if category_id is not None else ""
    url = build_api_url(cfg.server_url, cfg.username, cfg.password, "get_series") + suffix
    data = api_call(url)
    if not isinstance(data, list):
        return []

    streams = [XtreamStream(**s) for s in data if isinstance(s, dict)]
    _write_cache(cp, [s.__dict__ for s in streams])
    return streams


def get_series_info(cfg: XtreamConfig, series_id: int | str) -> XtreamSeriesInfo:
    """Obtiene info detallada de una serie (temporadas, episodios)."""
    url = build_api_url(cfg.server_url, cfg.username, cfg.password, "get_series_info")
    url += f"&series_id={series_id}"
    data = api_call(url)
    if not isinstance(data, dict):
        return XtreamSeriesInfo()
    return XtreamSeriesInfo(
        info=data.get("info", {}),
        seasons=data.get("seasons", []),
        episodes=data.get("episodes", {}),
    )


def get_short_epg(cfg: XtreamConfig, channel_id: str, limit: int = 8) -> list[dict]:
    """Obtiene EPG corto (short_epg) de un canal específico."""
    url = build_api_url(cfg.server_url, cfg.username, cfg.password, "get_short_epg")
    url += f"&stream_id={channel_id}&limit={limit}"
    data = api_call(url)
    if not isinstance(data, dict):
        return []
    epg_listings = data.get("epg_listings", [])
    if not isinstance(epg_listings, list):
        return []
    return epg_listings


def make_stream_url(
    cfg: XtreamConfig,
    stream_id: int | str,
    content_type: str,
    extension: str = "ts",
) -> str:
    """Construye la URL de reproducción de un stream."""
    return build_stream_url(
        cfg.server_url, cfg.username, cfg.password,
        stream_id, content_type, extension,
    )
