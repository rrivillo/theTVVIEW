"""Parser HLS: master y media playlist → :class:`MediaCapabilities`.

Dos cosas que este parser hace **bien** y por eso no puede ser un
``split(",")``:

- **Lista de atributos**: ``CODECS="avc1.4d401f,mp4a.40.2"`` y
  ``CODECS="mp4a.40.5,avc1.640028,channels=2"`` traen comas dentro de
  comillas, y las comillas pueden estar escapadas (``NAME="Audio "5.1""``).
  El parser lleva estado de comillas y sólo divide fuera de ellas.
- **Asociación de grupos**: una ``EXT-X-STREAM-INF`` referencia sus grupos
  por ``AUDIO``/``SUBTITLES``/``VIDEO``. Cada variante guarda a qué grupos
  pertenece, para que el proxy de fijado (F5c) pueda reescribir el master
  sin perder la relación (SDD §9.1).

Las URI relativas se resuelven con ``urljoin`` contra la **URL final** tras
redirecciones, no contra la que el usuario escribió (SDD §6).

Ninguna pista se inventa: si el master no declara audios alternativos,
:func:`parse_master_playlist` deja **una sola** pista de audio —la que va
dentro de la variante— marcada como ``DEFAULT``, y el modelo no la
convierte en opción (SDD §33).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..tracks.language import normalize_language
from ..tracks.models import (
    AUDIO,
    DEGRADED_NOT_EXPOSED,
    PROTO_HLS,
    TEXT,
    VIDEO,
    MediaCapabilities,
    MediaTrack,
    audio_id,
    subtitle_id,
    video_id,
)

__all__ = [
    "HlsParseError",
    "parse_attribute_list",
    "parse_hls",
    "parse_master_playlist",
    "parse_media_playlist",
    "is_master_playlist",
    "split_codecs",
]


class HlsParseError(ValueError):
    """El texto no es un manifiesto HLS legible (mensaje apto para modal)."""


# ---------------------------------------------------------------------------
# Listas de atributos y CODECS
# ---------------------------------------------------------------------------


def parse_attribute_list(text: str) -> dict[str, str]:
    """Parsea ``KEY=VALUE,KEY="VALUE, WITH COMMA"`` en un dict.

    Respeta el estado de comillas, que es justo donde HLS se rompe con un
    ``split(",")``: los ``CODECS`` llevan comas dentro y los ``NAME``
    organizers con comillas dobles por dentro
    (``NAME="Audio ""5.1"" Mix"``), que se reconvergen en una comilla
    literal. La última clave repetida gana: los manifiestos reales no la
    repiten, y si lo hacen manda la última.

    >>> parse_attribute_list('BANDWIDTH=1200,CODECS="avc1.4d,mp4a"')
    {'BANDWIDTH': '1200', 'CODECS': 'avc1.4d,mp4a'}
    >>> parse_attribute_list('NAME="Audio ""5.1"" Mix"')
    {'NAME': 'Audio "5.1" Mix'}
    """
    out: dict[str, str] = {}
    if not text:
        return out

    index = 0
    length = len(text)
    while index < length:
        # Separadores entre atributos.
        while index < length and text[index] in ", \t\r\n":
            index += 1
        if index >= length:
            break
        # Clave: hasta el '=' (o hasta la coma si el token está roto).
        start = index
        while index < length and text[index] not in "=,\r\n":
            index += 1
        key = text[start:index].strip()
        if index < length and text[index] == "=":
            index += 1
            value, index = _read_value(text, index)
            if key:
                out[key] = value.strip()
        # Un token sin '=' se descarta: no aporta nada y suele ser un
        # manifiesto malformado.
    return out


def _read_value(text: str, index: int) -> tuple[str, int]:
    """Lee un valor de atributo desde `index`; devuelve ``(valor, índice)``.

    Fuera de comillas, el valor acaba en la primera coma. Dentro, las
    comas son parte del valor y una pareja de comillas es una comilla
    literal.
    """
    chars: list[str] = []
    length = len(text)
    in_quotes = False
    escaped = False

    while index < length:
        ch = text[index]
        if escaped:
            chars.append(ch)
            escaped = False
            index += 1
            continue
        if in_quotes and ch == "\\":
            escaped = True
            index += 1
            continue
        if ch == '"':
            if in_quotes and text[index + 1: index + 2] == '"':
                chars.append('"')
                index += 2
                continue
            in_quotes = not in_quotes
            index += 1
            continue
        if ch == "," and not in_quotes:
            break
        chars.append(ch)
        index += 1

    return "".join(chars), index


def split_codecs(value: str | None) -> list[str]:
    """Parte ``CODECS`` por comas **respetando** los valores entrecomillados.

    Un ``CODECS`` mal formado trae a veces parámetros entrecomillados
    (``mp4a.40.5,avc1.640028,channels=2``): partirlo por comas a pelo
    inventaría un códec llamado ``channels=2``.

    >>> split_codecs('mp4a.40.5,avc1.640028,channels=2')
    ['mp4a.40.5', 'avc1.640028', 'channels=2']
    """
    if not value:
        return []
    items: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    for ch in value:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if in_quotes and ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
            continue
        if ch == "," and not in_quotes:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
            continue
        current.append(ch)
    item = "".join(current).strip()
    if item:
        items.append(item)
    return items


def _is_video_codec(codec: str) -> bool:
    head = codec.strip().lower().split(".")[0]
    return head in {
        "avc1", "avc3", "h264", "hevc", "hev1", "hvc1", "dvh1", "dvhe",
        "av01", "vp8", "vp9", "mp4v", "theora",
    }


def _is_audio_codec(codec: str) -> bool:
    head = codec.strip().lower().split(".")[0]
    return head in {
        "mp4a", "aac", "ac-3", "ec-3", "opus", "vorbis", "flac", "mp3",
        "mp4a.40",
    }


def pick_codec(codecs: list[str], *, want: str) -> str | None:
    """Elige el códec de vídeo o de audio de una lista ``CODECS``."""
    match = _is_video_codec if want == "video" else _is_audio_codec
    for codec in codecs:
        if match(codec):
            return codec
    return codecs[0] if codecs else None


# ---------------------------------------------------------------------------
# Estructura interna
# ---------------------------------------------------------------------------


@dataclass
class _Media:
    """Una línea ``EXT-X-MEDIA`` (audio, subtítulo o CC) ya interpretada."""

    type: str
    group_id: str
    name: str | None
    language: str | None
    uri: str | None
    is_default: bool = False
    is_autoselect: bool = True
    is_forced: bool = False
    characteristics: str | None = None
    channels: str | None = None
    instream_id: str | None = None
    #: Atributos originales, para el proxy de fijado (F5c).
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class _Variant:
    """Una línea ``EXT-X-STREAM-INF`` con su URI."""

    uri: str
    bandwidth: int | None
    average_bandwidth: int | None
    width: int | None
    height: int | None
    fps: float | None
    codecs: list[str]
    audio_group: str | None
    subtitle_group: str | None
    video_group: str | None
    raw: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tokenización del manifiesto
# ---------------------------------------------------------------------------


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def is_master_playlist(text: str) -> bool:
    """True si el texto es un master playlist (tiene ``EXT-X-STREAM-INF``).

    Un media playlist lleva ``#EXTINF`` + segmentos; un master lleva
    ``EXT-X-STREAM-INF`` + URI de variante. Se mira la etiqueta que
    decide, no la extensión del fichero (SDD §6).
    """
    if not text:
        return False
    for line in _lines(text):
        if line.startswith("#EXT-X-STREAM-INF"):
            return True
    return False


def _attribute_text(line: str, tag: str) -> str | None:
    """Texto de atributos de una etiqueta con parámetros."""
    if not line.startswith(tag):
        return None
    rest = line[len(tag):]
    if rest and rest[0] not in (":", " "):
        return None
    rest = rest[1:] if rest.startswith(":") else rest
    return rest.strip()


# ---------------------------------------------------------------------------
# Master playlist
# ---------------------------------------------------------------------------


def parse_master_playlist(
    text: str,
    *,
    final_url: str = "",
    live: bool = True,
) -> MediaCapabilities:
    """Interpreta un master playlist HLS.

    Args:
        text: cuerpo del manifiesto.
        final_url: URL **final** tras redirecciones; contra ella se
            resuelven las URI relativas.
        live: si el directo está en marcha (por defecto sí: en IPTV casi
            siempre lo está; un master con ``EXT-X-ENDLIST`` es VOD).

    Returns:
        MediaCapabilities con ``adaptive_bitrate`` a True **sólo** si hay
        dos o más variantes (SDD §14/§15).

    Raises:
        HlsParseError: si no hay ni ``#EXTM3U`` ni ninguna variante.
    """
    if not text or not text.strip():
        raise HlsParseError("El manifiesto está vacío.")
    lines = _lines(text)
    if not any(line.startswith("#EXTM3U") for line in lines):
        raise HlsParseError(
            "El archivo no es una lista HLS: falta la cabecera #EXTM3U."
        )

    media_entries, variants = _scan(lines)
    if not variants and not media_entries:
        raise HlsParseError(
            "El manifiesto HLS no declara ninguna pista ni variante."
        )

    has_endlist = any(line.startswith("#EXT-X-ENDLIST") for line in lines)

    return _build(
        media_entries=media_entries,
        variants=variants,
        final_url=final_url,
        manifest_url=final_url,
        live=live and not has_endlist,
        degraded=DEGRADED_NOT_EXPOSED if not variants else "",
    )


def parse_media_playlist(
    text: str,
    *,
    final_url: str = "",
    live: bool = True,
) -> MediaCapabilities:
    """Interpreta un media playlist (una sola pista, sin alternativas).

    Un media playlist **no** ofrece opciones: por diseño devuelve una
    variante y un audio, y el modelo no muestra ningún selector (SDD §32).
    """
    if not text or not text.strip():
        raise HlsParseError("El manifiesto está vacío.")
    lines = _lines(text)
    if not any(line.startswith("#EXTM3U") for line in lines):
        raise HlsParseError(
            "El archivo no es una lista HLS: falta la cabecera #EXTM3U."
        )
    has_endlist = any(line.startswith("#EXT-X-ENDLIST") for line in lines)
    has_inf = any(line.startswith("#EXTINF") for line in lines)
    if not has_inf and not any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        raise HlsParseError(
            "El manifiesto HLS no declara segmentos (falta #EXTINF)."
        )
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        return parse_master_playlist(text, final_url=final_url, live=live)

    duration = _target_duration(lines)
    return MediaCapabilities(
        audio_tracks=[
            MediaTrack(
                id=audio_id(1),
                type=AUDIO,
                is_default=True,
                language=None,
            )
        ],
        video_variants=[
            MediaTrack(id=video_id(MediaTrack(id="", type=VIDEO)), type=VIDEO)
        ],
        adaptive_bitrate=False,
        protocol=PROTO_HLS,
        manifest_url=final_url or None,
        final_url=final_url or None,
        live=live and not has_endlist,
        probed_at=time.time(),
        degraded_reason=DEGRADED_NOT_EXPOSED,
        note=(
            f"Una sola pista (media playlist"
            f"{f', {duration}s por segmento' if duration else ''})."
        ),
    )


def parse_hls(
    text: str,
    *,
    final_url: str = "",
    live: bool = True,
) -> MediaCapabilities:
    """Detecta master vs media playlist y devuelve las capacidades."""
    if is_master_playlist(text):
        return parse_master_playlist(text, final_url=final_url, live=live)
    return parse_media_playlist(text, final_url=final_url, live=live)


# ---------------------------------------------------------------------------
# Construcción del modelo
# ---------------------------------------------------------------------------


def _scan(lines: list[str]) -> tuple[list[_Media], list[_Variant]]:
    media_entries: list[_Media] = []
    variants: list[_Variant] = []
    pending: dict[str, str] | None = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attribute_list(line[len("#EXT-X-MEDIA:"):])
            entry = _media_from(attrs)
            if entry is not None:
                media_entries.append(entry)
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attribute_list(line[len("#EXT-X-STREAM-INF:"):])
            continue
        if line.startswith("#"):
            continue
        if pending is not None:
            variants.append(_variant_from(pending, line))
            pending = None
    return media_entries, variants


def _media_from(attrs: dict[str, str]) -> _Media | None:
    kind = attrs.get("TYPE", "").strip().upper()
    if kind not in ("AUDIO", "SUBTITLES", "CLOSED-CAPTIONS", "VIDEO"):
        return None
    group = attrs.get("GROUP-ID", "").strip()
    language = attrs.get("LANGUAGE", "").strip() or None
    info = normalize_language(language) if language else None
    return _Media(
        type=kind,
        group_id=group,
        name=attrs.get("NAME", "").strip() or None,
        language=info.code if info and info.code else language,
        uri=attrs.get("URI", "").strip() or None,
        is_default=attrs.get("DEFAULT", "").strip().upper() == "YES",
        is_autoselect=attrs.get("AUTOSELECT", "YES").strip().upper() != "NO",
        is_forced=attrs.get("FORCED", "").strip().upper() == "YES",
        characteristics=attrs.get("CHARACTERISTICS", "").strip() or None,
        channels=attrs.get("CHANNELS", "").strip() or None,
        instream_id=attrs.get("INSTREAM-ID", "").strip() or None,
        raw=dict(attrs),
    )


def _variant_from(attrs: dict[str, str], uri: str) -> _Variant:
    resolution = attrs.get("RESOLUTION", "").strip()
    width = height = None
    if "x" in resolution:
        head, _, tail = resolution.partition("x")
        width = _int_or_none(head)
        height = _int_or_none(tail)
    return _Variant(
        uri=uri,
        bandwidth=_int_or_none(attrs.get("BANDWIDTH")),
        average_bandwidth=_int_or_none(attrs.get("AVERAGE-BANDWIDTH")),
        width=width,
        height=height,
        fps=_float_or_none(attrs.get("FRAME-RATE")),
        codecs=split_codecs(attrs.get("CODECS")),
        audio_group=attrs.get("AUDIO", "").strip() or None,
        subtitle_group=attrs.get("SUBTITLES", "").strip() or None,
        video_group=attrs.get("VIDEO", "").strip() or None,
        raw=dict(attrs),
    )


def _build(
    *,
    media_entries: list[_Media],
    variants: list[_Variant],
    final_url: str,
    manifest_url: str,
    live: bool,
    degraded: str,
) -> MediaCapabilities:
    audio_media = [m for m in media_entries if m.type == "AUDIO"]
    subtitle_media = [m for m in media_entries if m.type == "SUBTITLES"]
    cc_media = [m for m in media_entries if m.type == "CLOSED-CAPTIONS"]

    # --- vídeo: una pista por variante, en el orden del manifiesto ------
    video_tracks: list[MediaTrack] = []
    video_uris: dict[str, str] = {}
    for index, variant in enumerate(variants, start=1):
        codecs = variant.codecs
        track = MediaTrack(
            id="",
            type=VIDEO,
            codec=pick_codec(codecs, want="video"),
            bitrate=variant.bandwidth or variant.average_bandwidth,
            width=variant.width,
            height=variant.height,
            fps=variant.fps,
            group_id=variant.video_group,
            uri=_resolve(final_url, variant.uri),
            metadata={
                "average_bandwidth": variant.average_bandwidth,
                "audio_group": variant.audio_group,
                "subtitles_group": variant.subtitle_group,
                "video_group": variant.video_group,
                "codecs": codecs,
                "hls_attrs": variant.raw,
            },
        )
        track.id = video_id(track, fallback_index=index)
        video_tracks.append(track)
        if track.uri:
            video_uris[track.id] = track.uri

    # --- audio ----------------------------------------------------------
    audio_tracks: list[MediaTrack] = []
    audio_uris: dict[str, str] = {}
    for index, entry in enumerate(audio_media, start=1):
        track = _media_track(entry, audio_id(index), base=final_url)
        audio_tracks.append(track)
        if track.uri:
            audio_uris[track.id] = track.uri

    if not audio_tracks:
        # Sin EXT-X-MEDIA: la variante lleva su audio dentro. Se declara
        # UNA pista (la que existe de verdad), no una lista de opciones.
        audio_tracks.append(
            MediaTrack(id=audio_id(1), type=AUDIO, is_default=True)
        )

    # --- subtítulos ------------------------------------------------------
    subtitle_tracks: list[MediaTrack] = []
    subtitle_uris: dict[str, str] = {}
    for index, entry in enumerate(subtitle_media + cc_media, start=1):
        track = _media_track(entry, subtitle_id(index), base=final_url)
        if entry.type == "CLOSED-CAPTIONS":
            # Los CC viajan dentro del flujo: no tienen URI, pero sí un
            # identificador que el reproductor puede pedir (INSTREAM-ID).
            track.metadata["instream_id"] = entry.instream_id
        subtitle_tracks.append(track)
        if track.uri:
            subtitle_uris[track.id] = track.uri

    return MediaCapabilities(
        audio_tracks=audio_tracks,
        video_variants=video_tracks,
        subtitle_tracks=subtitle_tracks,
        # ABR sólo si hay algo entre lo que elegir (§14/§15).
        adaptive_bitrate=len(video_tracks) >= 2,
        protocol=PROTO_HLS,
        manifest_url=manifest_url or None,
        final_url=final_url or None,
        live=live,
        video_uri_by_id=video_uris,
        audio_uri_by_id=audio_uris,
        subtitle_uri_by_id=subtitle_uris,
        probed_at=time.time(),
        degraded_reason=degraded,
        note=_note(len(video_tracks), len(audio_tracks), len(subtitle_tracks)),
    )


def _media_track(entry: _Media, track_id: str, *, base: str = "") -> MediaTrack:
    kind = AUDIO if entry.type == "AUDIO" else TEXT
    track = MediaTrack(
        id=track_id,
        type=kind,
        language=entry.language,
        original_language=entry.raw.get("LANGUAGE", "").strip() or None,
        label=entry.name,
        role=entry.characteristics,
        is_default=entry.is_default,
        is_auto_select=entry.is_autoselect,
        is_forced=entry.is_forced,
        channels=entry.channels,
        uri=_resolve(base, entry.uri) if entry.uri else None,
        group_id=entry.group_id or None,
        metadata={
            "hls_type": entry.type,
            "hls_attrs": dict(entry.raw),
        },
    )
    return track


def _note(videos: int, audios: int, subtitles: int) -> str | None:
    if videos == 1 and audios <= 1 and subtitles == 0:
        return "Una sola pista: no hay nada que elegir."
    return None


def _resolve(base: str, uri: str) -> str:
    """Resuelve `uri` contra `base` (URL final tras redirecciones)."""
    if not uri:
        return ""
    if not base:
        return uri
    if uri.startswith(("http://", "https://")):
        return uri
    return urljoin(base, uri)


def _int_or_none(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float_or_none(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _target_duration(lines: list[str]) -> int | None:
    for line in lines:
        if line.startswith("#EXT-X-TARGETDURATION:"):
            return _int_or_none(line.partition(":")[2])
    return None
