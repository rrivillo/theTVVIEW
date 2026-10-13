"""Traducción de la intención del usuario a lo que cada reproductor entiende.

Este paquete es el "backend" del SDD §46: conoce los reproductores, sus
listas blancas y sus límites, y la UI **no** habla mpv/vlc/mplayer. Aquí vive:

- :mod:`thetvview.player.capabilities` — qué **pistas** sabe hacer cada uno
  (verificado empíricamente contra los binarios, plan F5a);
- :mod:`thetvview.player.protocols` — qué **transportes** abre cada uno, que es
  otra pregunta y por eso es otra tabla (SDD-M Fase 0);
- :mod:`thetvview.player.media` — qué **contenedores y códecs** decodifica
  cada uno (FR-CAP-001, plan Fase 5), y :mod:`thetvview.player.confidence` —
  con qué evidencia lo sabemos: ``DECLARED``/``DETECTED``/``VERIFIED``
  (FR-CAP-002);
- :mod:`thetvview.player.success_cache` — la caché de combinaciones
  exitosas del router, con fecha, versión, ámbito y caducidad
  (FR-CAP-007);
- :mod:`thetvview.player.router` — elige reproductor por capacidad, no por el
  primero que encuentre (SDD-M §8);
- :mod:`thetvview.player.errors` — errores de reproducción normalizados
  (SDD-M §20);
- :mod:`thetvview.player.track_args` — ``PlaybackSelection`` → argv;
- :mod:`thetvview.player.mpv_ipc` — control en caliente de mpv por su IPC.

Dos tablas de capacidad, no una, porque son dos preguntas: un reproductor puede
fijar la pista de audio por índice y a la vez no abrir ``rtsp://``. Si se
mezclaran, el signo de una celda taparía al de la otra.

La aplicación no usa :mod:`urllib.request`, no abre sockets de red y no
invoca ``shell``: el IPC es un socket **local** y el proxy de fijado vive en
:mod:`thetvview.streams.pin_proxy`.
"""

from __future__ import annotations

from .errors import (
    PlayerError,
    StreamError,
    StreamErrorCode,
    classify_message,
    classify_returncode,
)
from .confidence import Confidence
from .media import (
    PLAYER_MEDIA_SUPPORT,
    PlayerMediaSupport,
    confianza_media,
    media_support_for,
    normalizar_codec,
    normalizar_contenedor,
    soporta,
)
from .protocols import (
    PLAYER_PROTOCOL_SUPPORT,
    TRANSPORTS,
    PlayerProtocolSupport,
    candidates as protocol_candidates,
    support_for as protocol_support_for,
    supports as protocol_supports,
    verified as protocol_verified,
)
from .router import (
    MAX_BACKEND_ATTEMPTS,
    Candidate,
    NoCompatibleBackend,
    explain_no_backend,
    order_candidates,
    select_backend,
)
from .capabilities import (
    KIND_AUDIO,
    KIND_QUALITY,
    KIND_SUBTITLES,
    KINDS,
    PlayerTrackSupport,
    PLAYER_TRACK_SUPPORT,
    SUBTITLE_RENDITION_BLIND,
    hot_control,
    quality_unit_for,
    subtitle_rendition_blind,
    support_for,
    supports,
)
from .core import (
    EARLY_EXIT_SECONDS,
    _codec_h264_args,
    _headless_args,
    _option_args,
    _safe_value,
    _title_args,
    command_for,
    diagnose_early_exit,
    explain_early_exit,
    launch,
    url_de_camara_resuelta,
)
from .mpv_ipc import MpvIpc, MpvIpcError, ipc_path
from .track_args import track_args, track_warnings

#: Alias del módulo de configuración y del módulo ``subprocess``, con los que
#: trabaja :mod:`thetvview.player.core`. Se exponen aquí para que
#: ``mock.patch.object(thetvview.player.subprocess, "Popen")`` y
#: ``player.config.find_player = ...`` sigan viendo los mismos objetos que antes,
#: cuando ``player`` era un módulo y no un paquete (compatibilidad, tests).
from .. import config  # noqa: E402  (re-export intencional)
from .core import subprocess  # noqa: E402  (re-export intencional)

__all__ = [
    "EARLY_EXIT_SECONDS",
    "KIND_AUDIO",
    "KIND_QUALITY",
    "KIND_SUBTITLES",
    "KINDS",
    "MAX_BACKEND_ATTEMPTS",
    "PLAYER_MEDIA_SUPPORT",
    "PLAYER_PROTOCOL_SUPPORT",
    "PLAYER_TRACK_SUPPORT",
    "SUBTITLE_RENDITION_BLIND",
    "TRANSPORTS",
    "Candidate",
    "Confidence",
    "MpvIpc",
    "MpvIpcError",
    "NoCompatibleBackend",
    "PlayerError",
    "PlayerMediaSupport",
    "PlayerProtocolSupport",
    "PlayerTrackSupport",
    "StreamError",
    "StreamErrorCode",
    "classify_message",
    "classify_returncode",
    "command_for",
    "confianza_media",
    "diagnose_early_exit",
    "explain_early_exit",
    "explain_no_backend",
    "hot_control",
    "ipc_path",
    "launch",
    "media_support_for",
    "normalizar_codec",
    "normalizar_contenedor",
    "order_candidates",
    "protocol_candidates",
    "protocol_support_for",
    "protocol_supports",
    "protocol_verified",
    "quality_unit_for",
    "select_backend",
    "soporta",
    "subtitle_rendition_blind",
    "support_for",
    "supports",
    "track_args",
    "track_warnings",
    "url_de_camara_resuelta",
]
