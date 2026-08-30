"""Modelos de dominio de theTVVIEW (dataclasses, solo stdlib).

Interfaz validada con iptv-epg-expert:
- Channel soporta búsqueda por nombre, grupos, favoritos, EPG matching
  (tvg_id/tvg_name) y opciones extra para player-launching.
- Playlist agrupa canales y conserva el origen (path o URL).
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Channel:
    """Un canal IPTV de una playlist M3U."""

    name: str
    url: str
    tvg_id: str | None = None
    tvg_name: str | None = None
    tvg_logo: str | None = None
    group: str | None = None  # group-title
    radio: bool = False
    # Atributos EXTINF no reconocidos, tal cual (claves con guion, valores str).
    attrs: dict[str, str] = field(default_factory=dict)
    # Pares (tag, valor) de #EXTVLCOPT/#KODIPROP asociados a la entrada.
    # Lista ordenada porque puede haber claves repetidas y el orden importa.
    extra_options: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Program:
    """Un programa de la parrilla XMLTV (EPG)."""

    channel_id: str
    title: str
    start: datetime  # consciente de zona horaria cuando el feed trae offset
    stop: datetime | None = None  # None si el feed no trae stop y no hay siguiente
    desc: str | None = None
    sub_title: str | None = None  # <sub-title> (episodio, sección...)
    categories: list[str] = field(default_factory=list)  # <category>, en orden del feed


@dataclass
class Playlist:
    """Resultado del parseo de un M3U/M3U8."""

    name: str
    channels: list[Channel] = field(default_factory=list)
    source: str | None = None  # path o URL de origen
    epg_url: str | None = None  # x-tvg-url de la cabecera #EXTM3U
