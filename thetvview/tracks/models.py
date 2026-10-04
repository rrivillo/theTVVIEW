"""Modelo común de pistas y de capacidades (SDD §7, §8, §26).

Nada aquí lee red ni escribe disco: son ``dataclasses`` puras. Los
identificadores de pista son **deterministas** (``a1``, ``a2``, ``s1``,
``v1080``) y no índices de posición en el manifest, porque son la clave de
las preferencias por canal y de la reconciliación cuando un master en vivo
cambia (SDD §28, plan F0).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "TrackType",
    "MediaTrack",
    "MediaCapabilities",
    "PlaybackSelection",
    "Protocol",
    "AUDIO",
    "VIDEO",
    "TEXT",
    "CLOSED_CAPTIONS",
    "PROTO_HLS",
    "PROTO_DASH",
    "PROTO_MPEGTS",
    "PROTO_RTMP",
    "PROTO_RTMPS",
    "PROTO_RTSP",
    "PROTO_UDP",
    "PROTO_MP4",
    "PROTO_FLV",
    "PROTO_UNKNOWN",
    "DEGRADED_NONE",
    "DEGRADED_NOT_EXPOSED",
    "DEGRADED_PROBE_FAILED",
    "DEGRADED_PROTOCOL",
    "DEGRADED_UNKNOWN",
    "audio_id",
    "subtitle_id",
    "video_id",
    "QUICK_AUTO",
]


class TrackType(str, Enum):
    """Tipo de pista. Los valores coinciden con los de HLS ``TYPE``."""

    AUDIO = "audio"
    VIDEO = "video"
    TEXT = "text"
    CLOSED_CAPTIONS = "closed_captions"


#: Alias de módulo, para no repetir `TrackType.X` en todo el código.
AUDIO: TrackType = TrackType.AUDIO
VIDEO: TrackType = TrackType.VIDEO
TEXT: TrackType = TrackType.TEXT
CLOSED_CAPTIONS: TrackType = TrackType.CLOSED_CAPTIONS


class Protocol(str, Enum):
    """Protocolo del **contenido** de un canal (SDD §6, SDD-M §4.1).

    Deliberadamente **no** incluye los esquemas: ``rtsp`` y ``rtmp`` son
    transportes (ver :mod:`thetvview.streams.transport`), no formatos de
    contenedor. Un mismo «directo» viaja por HTTP, RTMP o RTSP y el formato
    del contenido no cambia, así que mezclar ambos en un enum obligaría a
    decir «RTSP o HLS» cuando la pregunta correcta es «¿RTSP? ¿y qué formato
    lleva dentro?».

    Los valores de HLS/DASH/MPEG-TS/UNKNOWN son los de siempre; los nuevos no
    son tipos de contenido sino **el scheme**, y llegan aquí sólo porque el
    scheme es la única señal disponible cuando no se ha descargado nada: sin
    body ni ``Content-Type`` no hay con qué distinguirlos.
    """

    HLS = "hls"
    DASH = "dash"
    MPEGTS = "mpegts"
    #: Transporte identificado y **suficiente**: para RTMP/RTSP/UDP el
    #: reproductor negocia el formato con el servidor, y la app no lo sabe.
    RTMP = "rtmp"
    RTMPS = "rtmps"
    RTSP = "rtsp"
    UDP = "udp"
    #: MP4 y FLV servidos tal cual: directo de pista única, no seleccionable.
    MP4 = "mp4"
    FLV = "flv"
    UNKNOWN = "unknown"


PROTO_HLS: str = Protocol.HLS.value
PROTO_DASH: str = Protocol.DASH.value
PROTO_MPEGTS: str = Protocol.MPEGTS.value
PROTO_RTMP: str = Protocol.RTMP.value
PROTO_RTMPS: str = Protocol.RTMPS.value
PROTO_RTSP: str = Protocol.RTSP.value
PROTO_UDP: str = Protocol.UDP.value
PROTO_MP4: str = Protocol.MP4.value
PROTO_FLV: str = Protocol.FLV.value
PROTO_UNKNOWN: str = Protocol.UNKNOWN.value


#: Razones de degradación. Distinguen dos cosas que la UI explica distinto
#: (SDD §29/§48): "el proveedor no expone pistas" frente a "no se pudo
#: averiguar". Nunca se inventan: una de estas sólo aparece si el sondeo lo
#: pudo comprobar.
DEGRADED_NONE: str = ""
#: El manifiesto se leyó bien y no declara alternativas: pista única.
DEGRADED_NOT_EXPOSED: str = "not_exposed"
#: El manifiesto no se pudo leer (403/404/timeout/HTML): no sabemos.
DEGRADED_PROBE_FAILED: str = "probe_failed"
#: El protocolo no es seleccionable (MPEG-TS, directo): pista única por diseño.
DEGRADED_PROTOCOL: str = "protocol_not_selectable"
#: Ni siquiera se pudo clasificar la URL.
DEGRADED_UNKNOWN: str = "unknown"


#: Id de la opción "Automática (ABR)" y de "usa el DEFAULT del stream". No
#: es una pista: por eso vive fuera de los ids ``a*``/``s*``/``v*``.
QUICK_AUTO: str = "auto"


#: Rol de la pista tal y como lo declara el proveedor (``ContentRole`` /
#: ``CHARACTERISTICS`` de HLS).
ROLE_MAIN: str = "main"
ROLE_ALTERNATE: str = "alternate"
ROLE_COMMENTARY: str = "commentary"
ROLE_SUBTITLE: str = "subtitle"
ROLE_SIGN: str = "sign"


@dataclass
class MediaTrack:
    """Una pista (audio, texto) o una variante de vídeo de un mismo stream.

    ``bitrate`` va en **bits por segundo**, como los ``BANDWIDTH`` de HLS y
    los ``bandwidth`` de DASH. La conversión a kbps es cosa de
    :mod:`thetvview.tracks.labels` y de la tabla de unidades del reproductor
    (plan F5a), nunca de este campo.
    """

    id: str
    type: TrackType

    #: Código de idioma **ya normalizado** ("es") o None si el proveedor no lo
    #: declara. El valor crudo del proveedor se conserva en
    #: ``original_language`` (SDD §23: no destruir lo que vino).
    language: str | None = None
    original_language: str | None = None

    #: ``NAME`` del proveedor (HLS) o ``label`` de la plataforma.
    label: str | None = None

    codec: str | None = None
    #: Bits por segundo.
    bitrate: int | None = None

    width: int | None = None
    height: int | None = None
    fps: float | None = None

    role: str | None = None
    is_default: bool = False
    is_auto_select: bool = True
    #: Subtítulo forzado (HLS ``FORCED=YES``).
    is_forced: bool = False
    #: Canales de audio ("2", "6") tal cual los declara el proveedor.
    channels: str | None = None

    #: URI absoluta de la pista/variante cuando el manifiesto la publica.
    uri: str | None = None
    #: ``GROUP-ID`` de HLS: agrupa variantes con un mismo juego de pistas.
    group_id: str | None = None

    #: Datos extra que ni la UI ni el reproductor necesitan, para depurar.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def resolution(self) -> tuple[int, int] | None:
        """``(width, height)`` si hay los dos, o None."""
        if self.width and self.height:
            return int(self.width), int(self.height)
        return None

    @property
    def language_label(self) -> str | None:
        """Etiqueta nativa del idioma normalizado (o el crudo si no se sabe)."""
        from .language import normalize_language

        info = normalize_language(self.language or self.original_language)
        if info is not None and info.label:
            return info.label
        return self.original_language or self.language or None

    def to_dict(self, *, include_uri: bool = True) -> dict[str, Any]:
        """Diccionario plano y apto para JSON.

        ``include_uri=False`` omite la URI: es lo que usa la caché en disco,
        donde una URI de Xtream lleva usuario y contraseña en el *path*
        (SDD §37). El id basta para reconciliar y para pedir la pista.
        """
        data: dict[str, Any] = {
            "id": self.id,
            "type": self.type.value,
            "language": self.language,
            "original_language": self.original_language,
            "label": self.label,
            "codec": self.codec,
            "bitrate": self.bitrate,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "role": self.role,
            "is_default": self.is_default,
            "is_auto_select": self.is_auto_select,
            "is_forced": self.is_forced,
            "channels": self.channels,
            "group_id": self.group_id,
        }
        if include_uri:
            data["uri"] = self.uri
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MediaTrack":
        raw_type = str(data.get("type") or TrackType.AUDIO.value)
        try:
            track_type = TrackType(raw_type)
        except ValueError:
            track_type = TrackType.AUDIO
        return cls(
            id=str(data.get("id") or ""),
            type=track_type,
            language=_opt_str(data.get("language")),
            original_language=_opt_str(data.get("original_language")),
            label=_opt_str(data.get("label")),
            codec=_opt_str(data.get("codec")),
            bitrate=_opt_int(data.get("bitrate")),
            width=_opt_int(data.get("width")),
            height=_opt_int(data.get("height")),
            fps=_opt_float(data.get("fps")),
            role=_opt_str(data.get("role")),
            is_default=bool(data.get("is_default")),
            is_auto_select=bool(data.get("is_auto_select", True)),
            is_forced=bool(data.get("is_forced")),
            channels=_opt_str(data.get("channels")),
            uri=_opt_str(data.get("uri")),
            group_id=_opt_str(data.get("group_id")),
        )


@dataclass
class MediaCapabilities:
    """Lo que el proveedor **realmente** expone para un stream (SDD §8).

    ``adaptive_bitrate`` sólo es True si el manifiesto declara **dos o más**
    variantes: con una sola, el "ABR" sería una mentira (plan F3, §14/§15).
    """

    audio_tracks: list[MediaTrack] = field(default_factory=list)
    video_variants: list[MediaTrack] = field(default_factory=list)
    subtitle_tracks: list[MediaTrack] = field(default_factory=list)

    adaptive_bitrate: bool = False

    protocol: str = PROTO_UNKNOWN
    manifest_url: str | None = None
    #: URL final tras redirecciones: contra ella se resuelven las URI
    #: relativas del manifiesto (SDD §6).
    final_url: str | None = None
    live: bool = False

    video_uri_by_id: dict[str, str] = field(default_factory=dict)
    audio_uri_by_id: dict[str, str] = field(default_factory=dict)
    subtitle_uri_by_id: dict[str, str] = field(default_factory=dict)

    probed_at: float | None = None
    #: Ver :data:`DEGRADED_NOT_EXPOSED` y familia.
    degraded_reason: str = DEGRADED_NONE
    #: El master declara ``EXT-X-MEDIA``/``AdaptationSet`` pero el proveedor
    #: no hailed de forma utilizable (sólo informativo).
    note: str | None = None

    # -- consultas de ayuda (la UI y el gestor no deciden "a ojo") ---------

    @property
    def has_selectable_audio(self) -> bool:
        """True sólo si hay ≥2 audios (SDD §33: uno no abre selector)."""
        return len(self.audio_tracks) >= 2

    @property
    def has_selectable_subtitles(self) -> bool:
        """True sólo si hay ≥2 subtítulos (incluido el estado "desactivados")."""
        return len(self.subtitle_tracks) >= 2

    @property
    def has_selectable_quality(self) -> bool:
        """True sólo si hay ≥2 variantes de vídeo (SDD §34)."""
        return len(self.video_variants) >= 2

    @property
    def has_any_choice(self) -> bool:
        """True si hay algo que el usuario pueda cambiar."""
        return (
            self.has_selectable_audio
            or self.has_selectable_subtitles
            or self.has_selectable_quality
        )

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded_reason)

    def track_by_id(self, track_id: str | None) -> MediaTrack | None:
        """Busca una pista por id en cualquiera de las tres listas."""
        if not track_id:
            return None
        for bucket in (
            self.audio_tracks,
            self.video_variants,
            self.subtitle_tracks,
        ):
            for track in bucket:
                if track.id == track_id:
                    return track
        return None

    def audio_by_id(self, track_id: str | None) -> MediaTrack | None:
        return self._pick(self.audio_tracks, track_id)

    def subtitle_by_id(self, track_id: str | None) -> MediaTrack | None:
        return self._pick(self.subtitle_tracks, track_id)

    def variant_by_id(self, track_id: str | None) -> MediaTrack | None:
        return self._pick(self.video_variants, track_id)

    @staticmethod
    def _pick(
        bucket: list[MediaTrack], track_id: str | None
    ) -> MediaTrack | None:
        if not track_id:
            return None
        for track in bucket:
            if track.id == track_id:
                return track
        return None

    def uri_for(self, track_id: str | None) -> str | None:
        """URI absoluta de una pista, en cualquiera de los tres mapas."""
        if not track_id:
            return None
        for mapping in (
            self.video_uri_by_id,
            self.audio_uri_by_id,
            self.subtitle_uri_by_id,
        ):
            found = mapping.get(track_id)
            if found:
                return found
        track = self.track_by_id(track_id)
        return track.uri if track is not None else None

    def default_audio(self) -> MediaTrack | None:
        """Audio ``DEFAULT`` del manifiesto, o el primero si no hay ninguno."""
        return _default_of(self.audio_tracks)

    def default_subtitle(self) -> MediaTrack | None:
        return _default_of(self.subtitle_tracks)

    def sorted_variants(self) -> list[MediaTrack]:
        """Variantes de peor a mejor (altura, luego bitrate, luego id)."""
        def key(track: MediaTrack) -> tuple[int, int, str]:
            return (
                int(track.height or 0),
                int(track.bitrate or 0),
                track.id,
            )

        return sorted(self.video_variants, key=key)

    def to_dict(self) -> dict[str, Any]:
        """Representación plana para la caché en disco (sin secretos).

        Las URI absolutas **no** se guardan: en Xtream son
        ``/live/usuario/contraseña/id.m3u8`` y esta caché acaba en
        ``data/`` (SDD §37). Al releer, las pistas conservan su id, así que
        las preferencias siguen reconciliando sin necesitar la URL.
        """
        return {
            "audio_tracks": [t.to_dict(include_uri=False) for t in self.audio_tracks],
            "video_variants": [t.to_dict(include_uri=False) for t in self.video_variants],
            "subtitle_tracks": [
                t.to_dict(include_uri=False) for t in self.subtitle_tracks
            ],
            "adaptive_bitrate": self.adaptive_bitrate,
            "protocol": self.protocol,
            "live": self.live,
            "probed_at": self.probed_at,
            "degraded_reason": self.degraded_reason,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MediaCapabilities":
        def tracks(key: str) -> list[MediaTrack]:
            raw = data.get(key)
            if not isinstance(raw, list):
                return []
            out: list[MediaTrack] = []
            for item in raw:
                if isinstance(item, Mapping):
                    out.append(MediaTrack.from_dict(item))
            return out

        return cls(
            audio_tracks=tracks("audio_tracks"),
            video_variants=tracks("video_variants"),
            subtitle_tracks=tracks("subtitle_tracks"),
            adaptive_bitrate=bool(data.get("adaptive_bitrate")),
            protocol=str(data.get("protocol") or PROTO_UNKNOWN),
            live=bool(data.get("live")),
            probed_at=_opt_float(data.get("probed_at")),
            degraded_reason=str(data.get("degraded_reason") or DEGRADED_NONE),
            note=_opt_str(data.get("note")),
        )


@dataclass
class PlaybackSelection:
    """La intención del usuario (SDD §26); no es un comando del reproductor.

    - ``audio_track_id``/``subtitle_track_id``/``video_track_id`` son ids
      **nuestros** (no índices del manifest).
    - ``subtitle_track_id is None`` significa subtítulos desactivados.
    - ``audio_track_id is None`` significa "usa el default del backend".
    - ``auto_quality=True`` significa que no se fuerza ninguna variante.

    ``subtitles_decided`` no está en el SDD §26 y existe por una razón
    concreta: distingue **"el usuario no ha tocado los subtítulos"** de
    **"el usuario los ha apagado"**. Sin esa diferencia no se puede pasar
    ``--sid=no`` sólo en el segundo caso, y pasarlo siempre cambiaría el
    comportamiento de los streams que no tienen subtítulos.
    """

    audio_track_id: str | None = None
    subtitle_track_id: str | None = None
    video_track_id: str | None = None

    subtitles_enabled: bool = False
    subtitles_decided: bool = False
    auto_quality: bool = True

    @property
    def is_default(self) -> bool:
        """True si no se ha tocado nada (reproducción idéntica a hoy)."""
        return (
            self.audio_track_id is None
            and self.subtitle_track_id is None
            and self.video_track_id is None
            and not self.subtitles_enabled
            and not self.subtitles_decided
            and self.auto_quality
        )

    def copy(self) -> "PlaybackSelection":
        return replace(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "audio_track_id": self.audio_track_id,
            "subtitle_track_id": self.subtitle_track_id,
            "video_track_id": self.video_track_id,
            "subtitles_enabled": self.subtitles_enabled,
            "subtitles_decided": self.subtitles_decided,
            "auto_quality": self.auto_quality,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlaybackSelection":
        return cls(
            audio_track_id=_opt_str(data.get("audio_track_id")),
            subtitle_track_id=_opt_str(data.get("subtitle_track_id")),
            video_track_id=_opt_str(data.get("video_track_id")),
            subtitles_enabled=bool(data.get("subtitles_enabled")),
            subtitles_decided=bool(data.get("subtitles_decided")),
            auto_quality=bool(data.get("auto_quality", True)),
        )


# ---------------------------------------------------------------------------
# Ids deterministas (plan F0)
# ---------------------------------------------------------------------------


def audio_id(index: int) -> str:
    """Id estable de audio por orden de aparición: ``a1``, ``a2``…."""
    return f"a{max(1, int(index))}"


def subtitle_id(index: int) -> str:
    """Id estable de subtítulo por orden de aparición: ``s1``, ``s2``…."""
    return f"s{max(1, int(index))}"


def video_id(track: MediaTrack, fallback_index: int = 1) -> str:
    """Id estable de variante: ``v1080`` por altura, o ``v{bitrate}``.

    Se prefiere la altura porque es lo que el usuario reconoce; si el
    manifiesto no declara resolución, se usa el bitrate y, si tampoco, el
    índice de aparición.
    """
    if track is not None and track.height:
        return f"v{int(track.height)}"
    if track is not None and track.bitrate:
        return f"v{int(track.bitrate)}"
    return f"v{max(1, int(fallback_index))}"


def audio_uri_map(caps: MediaCapabilities) -> dict[str, str]:
    """Mapa id -> URI de audio, construido desde las pistas."""
    return {
        t.id: t.uri
        for t in caps.audio_tracks
        if t.uri and t.id
    }


def subtitle_uri_map(caps: MediaCapabilities) -> dict[str, str]:
    return {
        t.id: t.uri
        for t in caps.subtitle_tracks
        if t.uri and t.id
    }


def video_uri_map(caps: MediaCapabilities) -> dict[str, str]:
    return {
        t.id: t.uri
        for t in caps.video_variants
        if t.uri and t.id
    }


def puede_fijar_calidad(caps: MediaCapabilities | None) -> bool:
    """¿Existe alguna forma de **forzar** una variante concreta?

    Sólo en HLS, y sólo por el proxy de :mod:`thetvview.streams.pin_proxy`,
    que reescribe el master dejando una única variante. Verificado con los
    binarios: ningún reproductor sabe pedir una variante HLS concreta por
    argv, y en DASH no hay master que reescribir (sus representations no
    tienen URI de playlist, así que un master sintético saldría con la URI
    vacía y no reproduciría nada).

    Vive aquí, y no en el proxy, porque **los dos** la necesitan y este módulo
    no importa nada: si el gestor de la UI y la encuesta decidieran por su
    cuenta, acabarían discrepando —que es exactamente lo que pasó: la
    encuesta filtraba por HLS y la app no, de modo que un canal DASH
    ofrecía un menú de calidad que rompía la reproducción.
    """
    return bool(caps is not None and caps.protocol == PROTO_HLS)


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------


def _default_of(bucket: list[MediaTrack]) -> MediaTrack | None:
    if not bucket:
        return None
    for track in bucket:
        if track.is_default:
            return track
    return bucket[0]


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None