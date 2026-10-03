"""Qué sabe hacer cada reproductor con las pistas (plan F5a, SDD §51).

Verificado **empíricamente** contra los binarios de esta máquina (mpv 0.40.0,
VLC 3.0.24, mplayer 1.5, ffmpeg 7.1.5). No es una tabla copiada de la
documentación: cada fila se comprobó ejecutando el reproductor contra un
master HLS de prueba servido en local, y el resultado —incluido lo que
**no** funciona— está aquí.

Resumen de la verificación (2026-10-02):

============================  ========  =========  =========  =========
reproductor                   audio     subtítulos calidad   en caliente
============================  ========  =========  =========  =========
mpv 0.40.0                    ✅        ✅         ❌        ✅ (IPC)
VLC 3.0.24                    ✅        ✅         ❌        ❌
mplayer 1.5                   ✅        ✅         ❌        ❌
============================  ========  =========  =========  =========

Los tres **`no` de calidad son deliberados**, no una falta de trabajo:

- **mpv**: ``--video-bitrate`` **no existe como opción** en 0.40.0
  (``Error parsing option video-bitrate (option not found)``). Existe como
  propiedad *de estadísticas* ``video-bitrate`` en **bits/s** y **sólo de
  lectura**: ``set_property`` responde ``error accessing property``. No hay
  forma de fijar una variante HLS por argv ni por IPC.
- **VLC**: ``--program=<int>`` es el selector de **programa de TV digital**
  (DVB), no de variante de un master HLS: usarlo aquí no fijaría calidad y
  además rompería la reproducción. Descartado.
- **mplayer**: no tiene ninguna opción de bitrate por pista.

Por eso la calidad manual se resuelve por el otro camino del plan (F5c): un
**proxy loopback** que reescribe el master con una sola variante. Funciona
igual en los tres y es verificable. Y en DASH no se ofrece en absoluto
(§34: no mostrar opciones que no existen).

Además, el IPC de mpv **no tiene autenticación**: por eso
:mod:`thetvview.player.mpv_ipc` lo ata a un socket en un directorio 0700
con nombre aleatorio, y :mod:`thetvview.streams.pin_proxy` pone un token de
128 bits en la ruta. Ver SECURITY.md.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "PlayerTrackSupport",
    "PLAYER_TRACK_SUPPORT",
    "KIND_AUDIO",
    "KIND_SUBTITLES",
    "KIND_QUALITY",
    "KINDS",
    "supports",
    "support_for",
    "can_pin_quality",
    "quality_unit_for",
    "hot_control",
]

KIND_AUDIO: str = "audio"
KIND_SUBTITLES: str = "subtitles"
KIND_QUALITY: str = "quality"
#: Tipos de pista, en el orden en que los muestra la UI.
KINDS: tuple[str, ...] = (KIND_AUDIO, KIND_SUBTITLES, KIND_QUALITY)

#: Ningún reproductor expresa la calidad en kbps. La propiedad ``video-bitrate``
#: de mpv (que no se puede escribir) va en **bits/s**, y por eso las
#: conversiones del módulo :mod:`thetvview.tracks.labels` son bps → kbps sólo
#: para mostrar texto. Este valor existe para que nadie confunda las unidades
#: al escribir el proxy de fijado o una opción nueva (plan F5a).
QUALITY_UNIT_BPS: str = "bps"


@dataclass(frozen=True)
class PlayerTrackSupport:
    """Capacidades de un reproductor, con su motivo si algo no puede hacer."""

    name: str
    #: Puede fijar el audio por índice (nuestro id de pista ordenado).
    audio_by_index: bool
    #: Puede fijar subtítulos por índice, incluido "ninguno".
    subtitles_by_index: bool
    #: Puede **fijar** una calidad concreta por argv/IPC.
    quality: bool
    #: Puede fijar audio por idioma (útil cuando el índice es frágil).
    audio_by_language: bool
    subtitles_by_language: bool
    #: Puede cambiar de pista **en caliente** sin reabrir el canal.
    hot_control: bool
    #: Cómo expresa el bitrate, si es que lo expresa de alguna forma.
    quality_unit: str = ""
    #: Por qué no puede lo que no puede. Va a los modales y a SECURITY.md.
    notes: str = ""

    def supports(self, kind: str) -> bool:
        if kind == KIND_AUDIO:
            return bool(self.audio_by_index or self.audio_by_language)
        if kind == KIND_SUBTITLES:
            return bool(self.subtitles_by_index or self.subtitles_by_language)
        if kind == KIND_QUALITY:
            return bool(self.quality)
        return False

    def uses_index(self, kind: str) -> bool:
        """¿Usa el índice de pista en lugar del idioma?"""
        if kind == KIND_AUDIO:
            return bool(self.audio_by_index)
        if kind == KIND_SUBTITLES:
            return bool(self.subtitles_by_index)
        return False


_MPV_NOTES = (
    "Audio y subtítulos por índice (--aid/--sid, verificado). "
    "La calidad no se puede fijar: --video-bitrate no existe como opción y la "
    "propiedad video-bitrate es de sólo lectura."
)
_VLC_NOTES = (
    "Audio y subtítulos por idioma (--audio-language/--sub-language) y por id "
    "de pista. --program es de TV digital, no de variantes HLS, así que no "
    "sirve para fijar calidad."
)
_MPLAYER_NOTES = (
    "Audio y subtítulos por idioma (-alang/-slang) o por índice (-aid/-sid). "
    "Sin control en caliente: los cambios sólo se aplican al reabrir."
)


PLAYER_TRACK_SUPPORT: dict[str, PlayerTrackSupport] = {
    "mpv": PlayerTrackSupport(
        name="mpv",
        audio_by_index=True,
        subtitles_by_index=True,
        quality=False,
        audio_by_language=False,
        subtitles_by_language=False,
        hot_control=True,
        quality_unit=QUALITY_UNIT_BPS,
        notes=_MPV_NOTES,
    ),
    "vlc": PlayerTrackSupport(
        name="vlc",
        audio_by_index=True,
        subtitles_by_index=True,
        quality=False,
        audio_by_language=True,
        subtitles_by_language=True,
        hot_control=False,
        notes=_VLC_NOTES,
    ),
    "mplayer": PlayerTrackSupport(
        name="mplayer",
        audio_by_index=True,
        subtitles_by_index=True,
        quality=False,
        audio_by_language=True,
        subtitles_by_language=True,
        hot_control=False,
        notes=_MPLAYER_NOTES,
    ),
}


def support_for(player_name: str | None) -> PlayerTrackSupport | None:
    """Capacidades de `player_name`, o None si no lo conocemos."""
    if not player_name:
        return None
    return PLAYER_TRACK_SUPPORT.get(str(player_name).strip().lower())


def supports(player_name: str | None, kind: str) -> bool:
    """¿Puede este reproductor cambiar este tipo de pista por argv?

    Un reproductor desconocido devuelve ``False``: si no sabemos lo que sabe,
    **no** le prometemos al usuario una opción que luego no se aplica (SDD §51).
    """
    soporte = support_for(player_name)
    return bool(soporte and soporte.supports(kind))


def can_pin_quality(player_name: str | None) -> bool:
    """¿Puede fijar calidad por argv? (nadie: va por el proxy)."""
    return supports(player_name, KIND_QUALITY)


def quality_unit_for(player_name: str | None) -> str:
    """Unidad con la que el reproductor expresa el bitrate ("" si no)."""
    soporte = support_for(player_name)
    return soporte.quality_unit if soporte else ""


def hot_control(player_name: str | None) -> bool:
    """¿Se puede cambiar de pista en caliente sin reabrir el canal?"""
    soporte = support_for(player_name)
    return bool(soporte and soporte.hot_control)


# ---------------------------------------------------------------------------
# Subtítulos como rendition: lo que el reproductor no ve (medido, no supuesto)
# ---------------------------------------------------------------------------

#: Reproductores cuyo demuxer HLS **no expone** las pistas de subtítulo que el
#: manifiesto declara aparte (`EXT-X-MEDIA` con `TYPE=SUBTITLES`).
#:
#: No es un fallo de configuración: el demuxer HLS de ffmpeg —el que usan mpv y
#: mplayer— lo dice literalmente al abrir el manifiesto
#: (``hls: Can't support the subtitle(uri: subs_es.m3u8)``), y el resultado es
#: que esas pistas no existen para él: pasan `--sid` o `--sub-track-id` y se
#: reproduce sin subtítulos. Comprobado también con un manifiesto escrito a
#: mano y servido en local, donde controlábamos cada línea.
#:
#: ``vlc`` **sí** las ve: trae su propio demuxer adaptativo y registra las
#: pistas (`adaptive demux: BaseAdaptationSet sub English`), así que con VLC la
#: elección sí se aplica.
#:
#: Y con subtítulos **incrustados en los segmentos** (pista normal, sin URI
#: aparte) funciona en todos: por eso el aviso depende también de que la pista
#: tenga URI.
SUBTITLE_RENDITION_BLIND: frozenset[str] = frozenset({"mpv", "mplayer"})


def subtitle_rendition_blind(player_name: str | None) -> bool:
    """¿Este reproductor ignora los subtítulos que el manifiesto declara aparte?

    Lo que devuelve ``True`` es que el reproductor **no va a poder** aplicar la
    elección. La app no lo oculta: avisa en un modal (SDD §51).
    """
    if not player_name:
        return False
    return str(player_name).strip().lower() in SUBTITLE_RENDITION_BLIND