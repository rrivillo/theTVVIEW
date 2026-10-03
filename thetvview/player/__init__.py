"""Traducción de la intención del usuario a lo que cada reproductor entiende.

Este paquete es el "backend" del SDD §46: conoce los reproductores, sus
listas blancas y sus límites, y la UI **no** habla mpv/vlc/mplayer. Aquí vive:

- :mod:`thetvview.player.capabilities` — qué sabe hacer cada uno (verificado
  empíricamente contra los binarios, plan F5a);
- :mod:`thetvview.player.track_args` — ``PlaybackSelection`` → argv;
- :mod:`thetvview.player.mpv_ipc` — control en caliente de mpv por su IPC.

La aplicación no usa :mod:`urllib.request`, no abre sockets de red y no
invoca ``shell``: el IPC es un socket **local** y el proxy de fijado vive en
:mod:`thetvview.streams.pin_proxy`.
"""

from __future__ import annotations

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
    PlayerError,
    _codec_h264_args,
    _headless_args,
    _option_args,
    _safe_value,
    _title_args,
    command_for,
    explain_early_exit,
    launch,
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
    "PLAYER_TRACK_SUPPORT",
    "SUBTITLE_RENDITION_BLIND",
    "MpvIpc",
    "MpvIpcError",
    "PlayerError",
    "PlayerTrackSupport",
    "command_for",
    "explain_early_exit",
    "hot_control",
    "ipc_path",
    "subtitle_rendition_blind",
    "launch",
    "quality_unit_for",
    "support_for",
    "supports",
    "track_args",
    "track_warnings",
]
