"""Etiquetas legibles de audio, subtítulos y vídeo (SDD §24/§25).

Este módulo decide **qué texto ve el usuario**; no decide qué pista se
selecciona ni cómo se lanza el reproductor. Prioridades fijas:

- audio: ``NAME`` del proveedor → idioma traducido → código ISO → ``Audio #N``;
- subtítulos: idioma → ``NAME`` → código (y el estado "Desactivados" lo
  pone la pantalla, no aquí);
- vídeo: ``1080p`` → ``6000 kbps`` → ``1080p — 6000 kbps`` →
  ``1080p — 6000 kbps — H.264``, degradando cuando falta un dato.

Nunca se inventa información: si el manifiesto no declara resolución, no
aparece una resolución inventada a partir del nombre del canal (eso es otra
concepto, ver :mod:`thetvview.resolutions`).
"""

from __future__ import annotations

from .language import NON_LINGUISTIC, language_label, normalize_language
from .models import MediaTrack

__all__ = [
    "audio_label",
    "subtitle_label",
    "video_label",
    "codec_label",
    "bitrate_kbps",
    "resolution_label",
    "SUBTITLES_OFF",
    "AUDIO_AUTO",
]

#: Textos de estado que la UI muestra; viven aquí para que las pruebas y las
#: pantallas no inventen literales distintos.
SUBTITLES_OFF: str = "Desactivados"
AUDIO_AUTO: str = "Automático"

#: Codec → nombre corto reconocible. Los `CODECS` de HLS y los `codecs` de
#: DASH vienen en minúsculas; el mapping es sólo de presentación.
_CODEC_NAMES: dict[str, str] = {
    "av1": "AV1",
    "avc1": "H.264",
    "avc3": "H.264",
    "dvh1": "HEVC",
    "dvhe": "HEVC",
    "h264": "H.264",
    "h265": "H.265",
    "hevc": "H.265",
    "mp4a": "AAC",
    "aac": "AAC",
    "ec-3": "E-AC3",
    "eac3": "E-AC3",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "vp8": "VP8",
    "vp9": "VP9",
    "mp4v": "MPEG-4",
    "mpeg2": "MPEG-2",
    "stpp": "TTML",
    "wvtt": "WebVTT",
}


def codec_label(codec: str | None) -> str | None:
    """Nombre corto de un códec, o el original si no está en la tabla."""
    if not codec:
        return None
    text = str(codec).strip()
    if not text:
        return None
    head = text.split(".", 1)[0].strip().lower()
    return _CODEC_NAMES.get(head, text)


def bitrate_kbps(bitrate: int | float | None) -> int | None:
    """Bits/s → kbps redondeados. ``None`` si no hay dato."""
    if bitrate is None:
        return None
    try:
        value = float(bitrate)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return int(round(value / 1000.0))


def resolution_label(track: MediaTrack) -> str | None:
    """``1920x1080`` → ``1080p``. ``None`` si no hay resolución.

    No se adivina el entrelazado a partir del ``FRAME-RATE``: 1080p30 y
    1080i50 declaran valores parecidos y una etiqueta equivocada en un
    selector de calidad es peor que no afinar el vídeo (SDD §25 sólo pide la
    altura).
    """
    if track is None:
        return None
    height = track.height
    if not height:
        return None
    height = int(height)
    if height <= 0:
        return None
    return f"{height}p"


def audio_label(track: MediaTrack, index: int = 1) -> str:
    """Etiqueta de audio con la prioridad del SDD §24.

    1. ``NAME`` del proveedor (``label``);
    2. idioma traducido al nombre nativo;
    3. código ISO tal cual lo dio el proveedor;
    4. ``Audio #N``.

    El índice es 1-based: se usa para "la segunda pista de audio sin
    nombre ni idioma", nunca para desambiguar pistas que sí se distinguen.
    """
    if track is None:
        return "Audio"
    name = _clean(track.label)
    if name:
        return name
    lang = track.language or track.original_language
    if lang:
        info = normalize_language(lang)
        if info is not None and info.label:
            return info.label
        return _clean(str(lang)) or ""
    return f"Audio #{max(1, int(index))}"


def subtitle_label(track: MediaTrack, index: int = 1) -> str:
    """Etiqueta de subtítulo.

    Aquí el idioma va **primero** (a diferencia del audio): el usuario
    busca "Español" y el ``NAME`` del proveedor es a menudo un código
    ("1") o una descripción del proveedor. Si no hay idioma, se cae al
    ``NAME`` y por último a ``Subtítulo #N``.
    """
    if track is None:
        return "Subtítulo"
    lang = track.language or track.original_language
    parts: list[str] = []
    if lang:
        info = normalize_language(lang)
        label = info.label if info is not None else None
        if not label and info is not None:
            label = NON_LINGUISTIC.get((info.code or "").lower()) or info.original_code
        if label:
            parts.append(str(label))
    name = _clean(track.label)
    if name and name not in parts:
        parts.append(name)
    if not parts:
        return f"Subtítulo #{max(1, int(index))}"
    return " · ".join(parts)


def video_label(track: MediaTrack) -> str:
    """``1080p — 6000 kbps — H.264``, degradando según los datos (SDD §25)."""
    if track is None:
        return "Automática"
    pieces: list[str] = []
    res = resolution_label(track)
    if res:
        pieces.append(res)
    kbps = bitrate_kbps(track.bitrate)
    if kbps:
        pieces.append(f"{kbps} kbps")
    codec = codec_label(track.codec)
    if codec:
        pieces.append(codec)
    if not pieces:
        return "Automática"
    return " — ".join(pieces)


def audio_role_suffix(track: MediaTrack) -> str:
    """Marca corta del rol declarado (``main``/``dub``/``commentary``…).

    Se añade a la etiqueta de audio **sólo** cuando el rol aporta algo que
    el idioma no dice: un "Español (dub)" o "English (commentary)".
    """
    if track is None or not track.role:
        return ""
    role = str(track.role).strip().lower()
    if role in ("", "main"):
        return ""
    return f" ({role})"


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(str(text).split())