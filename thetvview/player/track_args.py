"""Traducción de :class:`PlaybackSelection` a argv, por reproductor.

Filosofía idéntica a la de ``player.py``: **lista blanca**. No se traduce
"lo que el manifiesto diga" sino lo que esta tabla reconoce, y todo valor
pasa por el mismo filtro que las cabeceras HTTP (imprimible, sin empezar por
``-``, sin saltos de línea, 120 caracteres como mucho).

El argv **sigue terminando en ``-- <url>``**: ``--`` no se mueve nunca (gap
B1). Aquí sólo se añaden argumentos **antes** de él.

Traducción por reproductor (verificada contra los binarios, F5a):

- **mpv**: ``--aid=<n>`` / ``--sid=<n|no>``. Índices 1-based en el orden en
  que las pistas aparecen en el manifiesto, que es el orden que usa mpv.
- **mplayer**: ``-aid <n>`` / ``-sid <n>`` y ``-slang <código>``. mplayer no
  entiende "sin subtítulos" con un índice: no se le pasa nada.
- **VLC**: ``--audio-track-id=<n>`` / ``--sub-track-id=<n>`` y
  ``--audio-language=<código>`` / ``--sub-language=<código>``. Para
  desactivar subtítulos se usa ``--no-sub-track``... que no existe; VLC se
  resuelve con ``--sub-track=-1`` (documentado como "sin subtítulos").

**La calidad no se traduce** a argv en ningún reproductor: la tabla de
capacidades dice que ninguno sabe, y quien la resuelve es
:mod:`thetvview.streams.pin_proxy` (F5c).
"""

from __future__ import annotations

from ..tracks.labels import language_label
from ..tracks.models import (
    MediaCapabilities,
    PlaybackSelection,
    puede_fijar_calidad,
)
from .capabilities import (
    KIND_AUDIO,
    KIND_QUALITY,
    KIND_SUBTITLES,
    support_for,
)

__all__ = [
    "track_args",
    "track_warnings",
    "audio_index",
    "subtitle_index",
    "MAX_VALUE_LENGTH",
]

#: Tope de longitud de un valor pasado al reproductor (mismo criterio que
#: ``player._title_args``): un idioma no ocupa 120 caracteres y si lo ocupa,
#: algo va mal.
MAX_VALUE_LENGTH: int = 120


def _safe_value(value: object) -> str | None:
    """``None`` si el valor no es razonable como argumento único."""
    if value is None:
        return None
    text = str(value)
    if not text or not text.isprintable() or "\n" in text or "\r" in text:
        return None
    if text.startswith("-"):
        return None
    if len(text) > MAX_VALUE_LENGTH:
        text = text[: MAX_VALUE_LENGTH - 1] + "…"
    return text


def audio_index(caps: MediaCapabilities | None, track_id: str | None) -> int | None:
    """Índice 1-based de una pista de audio en el orden del manifiesto."""
    return _index(caps, KIND_AUDIO, track_id)


def subtitle_index(caps: MediaCapabilities | None, track_id: str | None) -> int | None:
    """Índice 1-based de una pista de subtítulo en el orden del manifiesto."""
    return _index(caps, KIND_SUBTITLES, track_id)


def _index(
    caps: MediaCapabilities | None, kind: str, track_id: str | None
) -> int | None:
    if caps is None or not track_id:
        return None
    bucket = (
        caps.audio_tracks if kind == KIND_AUDIO
        else caps.subtitle_tracks if kind == KIND_SUBTITLES
        else caps.video_variants
    )
    for index, track in enumerate(bucket, start=1):
        if track.id == track_id:
            return index
    return None


def _language(track) -> str | None:
    if track is None:
        return None
    return track.language or language_label(track.original_language)


def track_args(
    player_name: str,
    selection: PlaybackSelection | None,
    caps: MediaCapabilities | None = None,
    *,
    pinned_via_proxy: bool = False,
) -> list[str]:
    """Argumentos de pistas para `player_name`, en su lista blanca.

    Args:
        player_name: "mpv", "mplayer" o "vlc".
        selection: intención del usuario, o None.
        caps: capacidades del manifest **original**.
        pinned_via_proxy: True cuando la URL que va a reproducir es el master
            reescrito por :mod:`thetvview.streams.pin_proxy`. En ese caso
            **no** se pasa nada: la elección ya va escrita en el manifiesto.

            No es una manía, es un hecho medido (F5): al reescribir el
            master, el manifest queda con **una** pista de audio, así que sus
            índices empiezan otra vez en 1. Con la verificación real de mpv,
            un master fijado se reproducía correctamente sin opciones y con
            ``--aid=2`` (el índice del manifest original) mpv se quedaba **sin
            audio** ("AID=no"), porque esa pista ya no existe en el manifiesto
            servido.

    Devuelve una lista vacía si no hay selección, si el reproductor no
    soporta ese tipo de pista o si el valor no pasa el filtro. Nunca lanza y
    nunca incluye algo que el reproductor vaya a interpretar como otra cosa.
    """
    if selection is None or pinned_via_proxy:
        return []
    soporte = support_for(player_name)
    if soporte is None:
        return []

    args: list[str] = []
    args += _audio_args(player_name, soporte, selection, caps)
    args += _subtitle_args(player_name, soporte, selection, caps)
    # La calidad no se pasa por argv: ver la cabecera del módulo y
    # `player/capabilities.py` (verificado contra los binarios).
    return args


def _audio_args(player_name, soporte, selection, caps) -> list[str]:
    if not soporte.supports(KIND_AUDIO):
        return []
    track_id = selection.audio_track_id
    if not track_id:
        # None = "usa el DEFAULT del stream": no se toca nada, que es
        # exactamente lo que quiere decir (SDD §26).
        return []
    track = caps.audio_by_id(track_id) if caps is not None else None
    if track is None:
        return []
    indice = audio_index(caps, track_id)
    idioma = _safe_value(_language(track))

    if player_name == "mpv":
        if indice is None:
            return []
        return [f"--aid={indice}"]
    if player_name == "mplayer":
        if indice is not None:
            return ["-aid", str(indice)]
        if idioma:
            return ["-alang", idioma]
        return []
    if player_name == "vlc":
        args: list[str] = []
        if idioma:
            args.append(f"--audio-language={idioma}")
        if indice is not None:
            args.append(f"--audio-track-id={indice}")
        return args
    return []


def _subtitle_args(player_name, soporte, selection, caps) -> list[str]:
    if not soporte.supports(KIND_SUBTITLES):
        return []
    track_id = selection.subtitle_track_id
    habilitado = bool(selection.subtitles_enabled)

    if not habilitado or not track_id:
        if not _subtitles_decided(selection):
            # El usuario no ha tocado los subtítulos: el argv queda tal como
            # estaba, que es lo que hace que un stream simple se reproduzca
            # exactamente igual que antes (SDD §32).
            return []
        return _subtitles_off_args(player_name)

    track = caps.subtitle_by_id(track_id) if caps is not None else None
    if track is None:
        return []
    indice = subtitle_index(caps, track_id)
    idioma = _safe_value(_language(track))

    if player_name == "mpv":
        if indice is None:
            return []
        return [f"--sid={indice}"]
    if player_name == "mplayer":
        # mplayer no tiene "sin subtítulos" por índice: si están apagados, no
        # se le pasa nada y el reproductor decide.
        if not habilitado:
            return []
        if indice is not None:
            return ["-sid", str(indice)]
        if idioma:
            return ["-slang", idioma]
        return []
    if player_name == "vlc":
        args = list(_subtitles_off_args(player_name) if not habilitado else [])
        if habilitado and idioma:
            args.append(f"--sub-language={idioma}")
        if habilitado and indice is not None:
            args.append(f"--sub-track-id={indice}")
        return args
    return []


def _subtitles_decided(selection) -> bool:
    """True si el usuario tocó los subtítulos (elegirlos **o** apagarlos)."""
    return bool(getattr(selection, "subtitles_decided", False))


def _subtitles_off_args(player_name: str) -> list[str]:
    """Argumentos para dejar los subtítulos apagados de forma explícita.

    Que un reproductor no sepa apagarlos no es motivo para no intentarlo: lo
    que no se aplica se explica en un modal (AGENTS), no se aplica en
    silencio.
    """
    if player_name == "mpv":
        return ["--sid=no"]
    if player_name == "vlc":
        # VLC usa -1 como "sin pista de subtítulos".
        return ["--sub-track-id=-1"]
    # mplayer no tiene equivalente: no se le pasa nada.
    return []


def track_warnings(
    player_name: str,
    selection: PlaybackSelection | None,
    caps: MediaCapabilities | None = None,
) -> list[str]:
    """Avisos de lo que el reproductor **no** va a aplicar.

    La regla es que la UI nunca deja sin explicar una selección que el backend
    no va a aplicar (SDD §51): aquí se devuelve el texto y la pantalla lo
    enseña en un modal.

    Y su requisito espejo, que es igual de importante: **no avisar cuando todo
    va a funcionar**. Un aviso que describe el mecanismo ("se sirve un master
    fijado por un proxy local") suena a avería, llega por modal y le roba la
    atención al usuario para informarle de que su elección se aplicó. Los
    avisos son para lo que no se va a aplicar, no para explicar cómo se
    aplica lo que sí.

    Y su otro requisito: **el sitio de un aviso es donde se decide, no después
    de decidir**. Lo de los subtítulos se explica ahora en la propia pantalla
    de pistas, junto a la lista de idiomas, mientras el usuario elige
    (``TrackOptionsScreen._nota_subtitulos``). Interrumpir con un modal después
    de haber elegido era interrumpir demasiado tarde y a lo peor.
    """
    if selection is None or selection.is_default:
        return []
    soporte = support_for(player_name)
    if soporte is None:
        return [
            f"'{player_name}' no es un reproductor conocido: no se le pasan "
            "opciones de audio, subtítulos ni calidad."
        ]
    avisos: list[str] = []
    if selection.audio_track_id and not soporte.supports(KIND_AUDIO):
        avisos.append(
            f"'{soporte.name}' no puede cambiar de audio en caliente; "
            "se aplicará al reabrir el canal."
        )
    if (
        selection.subtitle_track_id
        and not soporte.hot_control
        and not soporte.supports(KIND_SUBTITLES)
    ):
        avisos.append(
            f"'{soporte.name}' no puede cambiar de subtítulo; se aplicará al "
            "reabrir el canal."
        )
    if selection.video_track_id and caps is not None and not puede_fijar_calidad(caps):
        # La calidad elegida **no se va a poder aplicar**. Sólo aquí hay algo
        # que avisar: en HLS sí se aplica (el proxy de pinzado sirve un master
        # con esa variante) y no hay por qué molestar al usuario con un
        # detalle de implementación que además suena a problema.
        avisos.append(
            "La calidad elegida no se puede fijar en este canal: sólo es "
            "posible en streams HLS y este usa otro formato. Se reproducirá "
            "con calidad automática."
        )
    return avisos
