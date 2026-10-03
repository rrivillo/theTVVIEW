"""Descubrimiento del contenido de un stream (parsers puros, cero red).

    detector  →  "¿qué es esto?"   (SDD §6)
    hls       →  master/media playlist  → MediaCapabilities  (SDD §9)
    dash      →  MPD                    → MediaCapabilities  (SDD §10)

Nada en este paquete abre un socket: reciben **texto ya descargado** y
devuelven el modelo común. El módulo que sí baja el manifiesto es
:mod:`thetvview.streams.probe`, y lo hace por
:mod:`thetvview.security.safe_http`.

Regla que gobierna los tres: **no se inventa ni una pista**. Lo que el
proveedor no declara, aquí no aparece; y cuando sólo hay una alternativa,
:mod:`thetvview.tracks.models` no la considera una opción.
"""

from __future__ import annotations

from .dash import parse_dash
from .detector import (
    Detection,
    detect_protocol,
    looks_like_dash,
    looks_like_hls,
)
from .hls import (
    HlsParseError,
    is_master_playlist,
    parse_attribute_list,
    parse_hls,
    parse_master_playlist,
    parse_media_playlist,
    split_codecs,
)

__all__ = [
    "Detection",
    "HlsParseError",
    "detect_protocol",
    "looks_like_dash",
    "looks_like_hls",
    "is_master_playlist",
    "parse_attribute_list",
    "parse_dash",
    "parse_hls",
    "parse_master_playlist",
    "parse_media_playlist",
    "split_codecs",
]