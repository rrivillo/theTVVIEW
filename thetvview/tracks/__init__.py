"""Selección dinámica de pistas (audio / subtítulos / vídeo), solo stdlib.

Este paquete es **la representación**, no el proveedor:

    manifest HLS/MPD  ──parse──►  MediaCapabilities  ──prefs──►  PlaybackSelection

El manifest es la entrada y :class:`~thetvview.tracks.models.MediaCapabilities`
el modelo interno; la UI nunca ve un ``EXT-X-MEDIA`` ni un ``AdaptationSet``
(SDD §4, §1.1 del plan de fases).

Reglas de oro que este paquete respeta siempre (SDD §49, §32):

- **no se inventa ni un solo track**: si el proveedor no lo declara, aquí no
  aparece;
- un stream simple (``.ts`` o un manifest de una sola pista) no genera
  opciones seleccionables;
- ninguna operación de red: los parsers viven en :mod:`thetvview.streams`.
"""

from __future__ import annotations

from .language import LanguageInfo, matches_language, normalize_language
from .labels import (
    AUDIO_AUTO,
    SUBTITLES_OFF,
    audio_label,
    audio_role_suffix,
    subtitle_label,
    video_label,
)
from .models import (
    AUDIO,
    CLOSED_CAPTIONS,
    DEGRADED_NOT_EXPOSED,
    DEGRADED_PROBE_FAILED,
    DEGRADED_PROTOCOL,
    DEGRADED_UNKNOWN,
    PROTO_DASH,
    PROTO_HLS,
    PROTO_MPEGTS,
    PROTO_UNKNOWN,
    TEXT,
    VIDEO,
    MediaCapabilities,
    MediaTrack,
    PlaybackSelection,
    TrackType,
)

__all__ = [
    "AUDIO",
    "AUDIO_AUTO",
    "CLOSED_CAPTIONS",
    "DEGRADED_NOT_EXPOSED",
    "DEGRADED_PROBE_FAILED",
    "DEGRADED_PROTOCOL",
    "DEGRADED_UNKNOWN",
    "PROTO_DASH",
    "PROTO_HLS",
    "PROTO_MPEGTS",
    "PROTO_UNKNOWN",
    "SUBTITLES_OFF",
    "TEXT",
    "VIDEO",
    "LanguageInfo",
    "MediaCapabilities",
    "MediaTrack",
    "PlaybackSelection",
    "TrackType",
    "audio_label",
    "audio_role_suffix",
    "matches_language",
    "normalize_language",
    "subtitle_label",
    "video_label",
]