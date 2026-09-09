"""Modelos Xtream y normalización al dominio existente (solo stdlib).

- ContentType enum para live/vod/series.
- Movie y Series dataclasses mínimos (extienden el dominio sin romperlo).
- normalize_live_stream: XtreamStream → Channel (reutiliza groups/favorites/player).
- normalize_vod_stream: XtreamStream → Movie.
- normalize_series_stream: XtreamStream → Series.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import Channel
from .xtream_provider import XtreamCategory, XtreamStream


class ContentType(Enum):
    """Tipo de contenido Xtream."""

    LIVE = "live"
    VOD = "movie"
    SERIES = "series"


@dataclass
class Movie:
    """Película VOD normalizada al dominio theTVVIEW."""

    name: str
    stream_id: int | str
    url: str  # URL de reproducción completa
    category: str | None = None
    category_id: str | None = None
    logo: str | None = None
    rating: str | None = None
    plot: str | None = None
    cast: str | None = None
    director: str | None = None
    genre: str | None = None
    release_date: str | None = None
    duration: str | None = None
    backdrop: str | None = None
    content_type: ContentType = ContentType.VOD


@dataclass
class Series:
    """Serie normalizada al dominio theTVVIEW."""

    name: str
    series_id: int | str
    category: str | None = None
    category_id: str | None = None
    logo: str | None = None
    plot: str | None = None
    cast: str | None = None
    director: str | None = None
    genre: str | None = None
    release_date: str | None = None
    rating: str | None = None
    content_type: ContentType = ContentType.SERIES


@dataclass
class XtreamPlaylist:
    """Playlist normalizada de Xtream: canales live + VOD + series."""

    name: str
    channels: list[Channel] = field(default_factory=list)
    movies: list[Movie] = field(default_factory=list)
    series_list: list[Series] = field(default_factory=list)
    categories_live: list[XtreamCategory] = field(default_factory=list)
    categories_vod: list[XtreamCategory] = field(default_factory=list)
    categories_series: list[XtreamCategory] = field(default_factory=list)
    server_url: str = ""
    username: str = ""


# --- Funciones de normalización ----------------------------------------------

def normalize_live_stream(
    stream: XtreamStream,
    server_url: str,
    username: str,
    password: str,
) -> Channel:
    """Convierte un XtreamStream de tipo live a Channel del dominio.

    - category → group
    - stream_icon → tvg_logo
    - "xtream:<id>" → tvg_id (para matching EPG)
    - attrs guarda metadata Xtream para uso futuro
    """
    from .xtream_security import build_stream_url

    url = build_stream_url(
        server_url, username, password,
        stream.stream_id, "live", "ts",
    )

    return Channel(
        name=stream.name or f"Channel {stream.num}",
        url=url,
        tvg_id=f"xtream:{stream.stream_id}" if stream.epg_channel_id is None else stream.epg_channel_id,
        tvg_name=stream.name or None,
        tvg_logo=stream.stream_icon or None,
        group=None,  # se asigna después desde la categoría
        radio=False,
        attrs={
            "xtream_id": str(stream.stream_id),
            "category_id": str(stream.category_id),
            "content_type": "live",
        },
        extra_options=[],
    )


def normalize_vod_stream(
    stream: XtreamStream,
    server_url: str,
    username: str,
    password: str,
    category_name: str | None = None,
) -> Movie:
    """Convierte un XtreamStream de tipo movie a Movie normalizado."""
    from .xtream_security import build_stream_url

    # Determinar extensión (por defecto ts para movies también funciona)
    url = build_stream_url(
        server_url, username, password,
        stream.stream_id, "movie", "mp4",
    )

    return Movie(
        name=stream.name or f"Movie {stream.num}",
        stream_id=stream.stream_id,
        url=url,
        category=category_name,
        category_id=str(stream.category_id),
        logo=stream.stream_icon or None,
        rating=stream.rating,
        plot=stream.plot,
        cast=stream.cast,
        director=stream.director,
        genre=stream.genre,
        release_date=stream.releaseDate,
        duration=stream.duration,
        backdrop=stream.backdrop_path[0] if stream.backdrop_path else None,
    )


def normalize_series_stream(
    stream: XtreamStream,
    category_name: str | None = None,
) -> Series:
    """Convierte un XtreamStream de tipo series a Series normalizado."""
    return Series(
        name=stream.name or f"Series {stream.num}",
        series_id=stream.stream_id,
        category=category_name,
        category_id=str(stream.category_id),
        logo=stream.stream_icon or None,
        plot=stream.plot,
        cast=stream.cast,
        director=stream.director,
        genre=stream.genre,
        release_date=stream.releaseDate,
        rating=stream.rating,
    )


def build_category_map(categories: list[XtreamCategory]) -> dict[str, str]:
    """Construye mapa {category_id: name} para lookup rápido."""
    return {str(c.category_id): c.name for c in categories}
