"""Contenedores y códecs de audio y vídeo de cada reproductor (FR-CAP-001).

Dos dimensiones de la matriz de capacidades que no estaban en
:mod:`thetvview.player.protocols` (transportes) ni en
:mod:`thetvview.player.capabilities` (pistas): **qué contenedor desempaqueta**
y **qué códec decodifica**. Sin ellas, «mplayer abre este canal» sólo es
cierto si además el canal es MPEG-TS con H.264 —y eso hoy no lo pregunta
nadie—.

Sólo lo **medido** (plan Fase 5: «códecs y contenedores con lo ya medido»).
La evidencia es la misma campaña empírica que documenta
:mod:`thetvview.player.protocols` (2026-10-04, mpv 0.40.0, VLC 3.0.24,
mplayer 1.5) con los streams de laboratorio de ``tests/fixtures/streams``:
segmentos ``.ts`` (MPEG-TS) y variantes con ``CODECS="avc1…,mp4a…"``
(H.264 y AAC), cuya salida describía el códec que se estaba decodificando.

Lo que **no** se midió aquí —MP4, MKV, WebM, HEVC, AC-3, Opus…— no aparece
en la tabla: la app no promete lo que no ha comprobado, y preguntar por una
clave ausente funciona igual que preguntar por una fila con «no» (la
ausencia es un «no lo sabemos», y el diagnóstico lo dice sin inventarse una
causa). El nivel de confianza de cada clave es la regla de
:mod:`thetvview.player.confidence` (FR-CAP-002): hoy todo lo de aquí es
``DETECTED`` porque es lo que se midió, y ``VERIFIED`` exige evidencia
enlazada.
"""

from __future__ import annotations

from dataclasses import dataclass

from .confidence import Confidence, clave_de, nivel_confianza

__all__ = [
    "PLAYER_MEDIA_SUPPORT",
    "PlayerMediaSupport",
    "confianza_media",
    "media_support_for",
    "normalizar_codec",
    "normalizar_contenedor",
    "soporta",
]

#: Cómo se pide una clave en esta dimensión: o un contenedor («mpegts»)
#: o un códec, en forma RFC 6381 («avc1.4d401f») o corta («h264»).
KIND_VIDEO: str = "video"
KIND_AUDIO_CODEC: str = "audio"

#: RFC 6381 / nombre corto → clave canónica de códec. Los prefijos son los
#: que ya clasifica :mod:`thetvview.streams.hls` y los que muestra
#: :mod:`thetvview.tracks.labels`; aquí se normalizan a **una** clave para
#: que «avc1.640028» y «h264» sean la misma pregunta.
_CODEC_CANONICO: dict[str, str] = {
    # vídeo
    "avc1": "h264",
    "avc3": "h264",
    "h264": "h264",
    "hev1": "hevc",
    "hvc1": "hevc",
    "hevc": "hevc",
    "vp09": "vp9",
    "vp9": "vp9",
    "av01": "av1",
    "av1": "av1",
    # audio
    "mp4a": "aac",
    "aac": "aac",
    "ac-3": "ac3",
    "ac3": "ac3",
    "ec-3": "eac3",
    "eac3": "eac3",
    "mp3": "mp3",
    "opus": "opus",
    "vorbis": "vorbis",
    "flac": "flac",
    # subtítulos (para poder preguntar también por ellos sin sorpresas)
    "wvtt": "wvtt",
    "stpp": "stpp",
}

#: Formas usuales de nombrar un contenedor → clave canónica.
_CONTENEDOR_CANONICO: dict[str, str] = {
    "ts": "mpegts",
    "m2ts": "mpegts",
    "mts": "mpegts",
    "mpegts": "mpegts",
    "mp4": "mp4",
    "m4s": "mp4",
    "fmp4": "mp4",
    "mkv": "mkv",
    "mka": "mkv",
    "webm": "webm",
}


def normalizar_codec(codec: str | None) -> str:
    """Clave canónica de un códec («avc1.4d401f» → «h264»).

    Sólo mira el prefijo antes del primer punto, como hace la lista
    ``CODECS`` del HLS. Lo que no se reconoce vuelve en minúsculas, para
    que una pregunta rara siga siendo una pregunta (y pueda responder
    «no está en la tabla») en vez de un ``KeyError``.
    """
    crudo = str(codec or "").strip().lower()
    prefijo = crudo.split(".", 1)[0]
    return _CODEC_CANONICO.get(prefijo, prefijo)


def normalizar_contenedor(contenedor: str | None) -> str:
    """Clave canónica de un contenedor («.ts» → «mpegts»)."""
    crudo = str(contenedor or "").strip().lower().lstrip(".")
    return _CONTENEDOR_CANONICO.get(crudo, crudo)


@dataclass(frozen=True)
class PlayerMediaSupport:
    """Lo que un reproductor desempaqueta y decodifica, con su confianza."""

    name: str
    #: Contenedores que sabe abrir (claves canónicas).
    containers: frozenset[str]
    #: Códecs de vídeo que decodifica (claves canónicas).
    codecs_video: frozenset[str]
    #: Códecs de audio que decodifica (claves canónicas).
    codecs_audio: frozenset[str]
    #: Claves comprobadas en esta máquina. None = «sin medir» → DECLARED.
    medido: frozenset[str] | None = None
    #: Matiz del propio reproductor, si lo tiene (va al diagnóstico).
    notes: str = ""

    @property
    def claves(self) -> frozenset[str]:
        """Todas las claves que esta fila afirma."""
        return self.containers | self.codecs_video | self.codecs_audio

    def declara(self, clave: str) -> bool:
        """¿Esta fila afirma algo sobre `clave`? (normaliza antes de mirar)."""
        return normalizar_codec(clave) in self.claves or (
            normalizar_contenedor(clave) in self.containers
        )

    def confianza(self, clave: str) -> Confidence | None:
        """Nivel de confianza de `clave` (FR-CAP-002), o None si no se afirma."""
        canonica = self._canonica(clave)
        if canonica is None:
            return None
        medido = self.medido is not None and canonica in self.medido
        return nivel_confianza(medido, clave_de(self.name, canonica))

    def _canonica(self, clave: str) -> str | None:
        """Clave canónica de `clave` dentro de esta fila, o None."""
        crudo = str(clave or "").strip().lower()
        if not crudo:
            return None
        como_codec = normalizar_codec(crudo)
        if como_codec in self.claves:
            return como_codec
        como_contenedor = normalizar_contenedor(crudo)
        if como_contenedor in self.containers:
            return como_contenedor
        return None


#: Tabla: sólo lo medido el 2026-10-04 (ver docstring del módulo). Mismo
#: set para los tres porque la medición de MPEG-TS con H.264/AAC salió bien
#: en los tres; las diferencias entre reproductores viven en
#: ``protocols``/``capabilities``, no aquí.
_MPEGTS = frozenset({"mpegts"})
_H264 = frozenset({"h264"})
_AAC = frozenset({"aac"})
_MEDIDO = _MPEGTS | _H264 | _AAC

PLAYER_MEDIA_SUPPORT: dict[str, PlayerMediaSupport] = {
    "mpv": PlayerMediaSupport(
        name="mpv",
        containers=_MPEGTS,
        codecs_video=_H264,
        codecs_audio=_AAC,
        medido=_MEDIDO,
        notes="Único con control en caliente (IPC); decodifica H.264/AAC de MPEG-TS.",
    ),
    "vlc": PlayerMediaSupport(
        name="vlc",
        containers=_MPEGTS,
        codecs_video=_H264,
        codecs_audio=_AAC,
        medido=_MEDIDO,
        notes="Demuxer adaptativo propio; H.264/AAC en MPEG-TS medido el 2026-10-04.",
    ),
    "mplayer": PlayerMediaSupport(
        name="mplayer",
        containers=_MPEGTS,
        codecs_video=_H264,
        codecs_audio=_AAC,
        medido=_MEDIDO,
        notes=(
            "Decodifica H.264/AAC en MPEG-TS; en HLS sólo con URL `.m3u8` "
            "(su libavformat no detecta manifiestos de otra forma)."
        ),
    ),
}


def media_support_for(player_name: str | None) -> PlayerMediaSupport | None:
    """Contenedores/códecs de `player_name`, o None si no lo conocemos."""
    if not player_name:
        return None
    return PLAYER_MEDIA_SUPPORT.get(str(player_name).strip().lower())


def soporta(player_name: str | None, clave: str) -> bool:
    """¿Decodifica/desempaqueta este reproductor `clave`?

    `clave` acepta forma RFC 6381 (``avc1.4d401f``), corta (``h264``) o de
    contenedor con o sin punto (``.ts``). Un reproductor desconocido o una
    clave ausente devuelven False: no se promete lo que no está en la tabla.
    """
    soporte = media_support_for(player_name)
    return bool(soporte and soporte.declara(clave))


def confianza_media(player_name: str | None, clave: str) -> Confidence | None:
    """Nivel de confianza de `clave` para `player_name` (FR-CAP-002)."""
    soporte = media_support_for(player_name)
    return soporte.confianza(clave) if soporte else None
