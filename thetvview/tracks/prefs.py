"""Preferencias de pistas por canal, por proveedor y globales (plan F4).

Precedencia (SDD §31), y se lee de arriba abajo hasta encontrar un valor:

    preferencia del canal  →  del proveedor  →  global  →  DEFAULT del stream

Dos decisiones de seguridad, no de gusto:

- **La URL cruda del canal nunca se persiste** (H7). Las URLs de Xtream
  llevan usuario y contraseña en el *path*, así que la clave de un canal sin
  ``tvg-id`` es el ``sha256`` de su URL **ya redactada**. Es un identificador
  estable, no un secreto y no una credencial.
- **El JSON de pistas no lleva URLs**, ni siquiera redactadas: sólo
  preferencias (idiomas, calidad). Hay un test explícito de esto.

También es retrocompatible por construcción: las claves nuevas son opcionales
y ``PrefsManager.load()`` ya ignoraba lo que no conocía (H11).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlsplit

from ..models import Channel
from ..prefs import Prefs, PrefsManager
from ..security.redaction import redact_text
from .models import QUICK_AUTO

__all__ = [
    "TrackPreferences",
    "GLOBAL_KEY",
    "PROVIDER_PREFIX",
    "CHANNEL_PREFIX",
    "TVG_PREFIX",
    "channel_key",
    "provider_key",
    "empty_preferences",
    "resolve_preferences",
    "preferences_from_selection",
    "load_scopes",
    "remember_for_channel",
    "remember_for_provider",
    "read_scope",
    "write_scope",
]

#: Clave del bloque global dentro de ``track_prefs``. Existe para que el
#: global y el resto compartan el mismo diccionario, pero el global **también**
#: vive en las claves de primer nivel de ``prefs.json`` (retrocompatible).
GLOBAL_KEY: str = "__global__"

PROVIDER_PREFIX: str = "provider:"
CHANNEL_PREFIX: str = "channel:"
TVG_PREFIX: str = "tvg:"

#: Cuántos caracteres del hash se conservan: suficiente para no colisionar
#: entre canales, insuficiente para poder hacer fuerza bruta sobre el
#: preimagen (que además ya viene redactada).
_HASH_CHARS: int = 32


@dataclass
class TrackPreferences:
    """Vista resuelta de las preferencias de pistas.

    Los nombres de los campos son los que espera
    :mod:`thetvview.tracks.manager`, así que el resultado se le pasa tal cual.
    """

    preferred_audio_language: str | None = None
    preferred_subtitle_language: str | None = None
    subtitles_enabled: bool = False
    preferred_quality: str | None = None
    #: Ámbito del que salió cada valor, para explicarlo si hace falta.
    scope: str = "stream"

    def to_dict(self) -> dict[str, Any]:
        return {
            "preferred_audio_language": self.preferred_audio_language,
            "preferred_subtitle_language": self.preferred_subtitle_language,
            "subtitles_enabled": self.subtitles_enabled,
            "preferred_quality": self.preferred_quality,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "TrackPreferences":
        if not isinstance(data, Mapping):
            return cls()
        calidad = _opt_str(data.get("preferred_quality"))
        return cls(
            preferred_audio_language=_opt_str(data.get("preferred_audio_language")),
            preferred_subtitle_language=_opt_str(
                data.get("preferred_subtitle_language")
            ),
            subtitles_enabled=bool(data.get("subtitles_enabled")),
            preferred_quality=calidad,
        )

    def is_empty(self) -> bool:
        return (
            self.preferred_audio_language is None
            and self.preferred_subtitle_language is None
            and not self.subtitles_enabled
            and self.preferred_quality is None
        )


def empty_preferences() -> TrackPreferences:
    """Preferencias vacías: no expresa nada, manda el stream."""
    return TrackPreferences()


# ---------------------------------------------------------------------------
# Claves
# ---------------------------------------------------------------------------


def channel_key(channel: Channel | None) -> str | None:
    """Clave estable de un canal: ``tvg-id`` si existe, si no hash de la URL.

    Nunca contiene la URL ni parte de ella (H7), y un hash de algo que lleva
    credenciales sería atacable por diccionario, así que se hashea la URL
    **ya redactada**.

    Limitación aceptada a conciencia: dos canales cuyas URLs sólo se
    diferencian en el usuario/contraseña (``/live/alice/…`` frente a
    ``/live/bob/…``) reciben la misma clave y comparten preferencias. Es un
    caso raro —las cuentas Xtream ya llegan aquí como referencia opaca, sin
    credenciales en la URL (SDD §37)— y el coste de evitarlo sería meter en
    el JSON material del que no se puede deshacer un secreto.
    """
    if channel is None:
        return None
    tvg_id = str(getattr(channel, "tvg_id", "") or "").strip()
    if tvg_id:
        return f"{TVG_PREFIX}{tvg_id}"
    url = str(getattr(channel, "url", "") or "").strip()
    if not url:
        return None
    digest = hashlib.sha256(redact_text(url).encode("utf-8")).hexdigest()
    return f"{CHANNEL_PREFIX}{digest[:_HASH_CHARS]}"


def provider_key(source: str | None) -> str | None:
    """Clave de proveedor: el host de la fuente.

    Sólo el host, que no es un secreto; las credenciales viven en otro sitio
    y en el SecretStore del sistema (SDD §15).
    """
    text = str(source or "").strip()
    if not text:
        return None
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    return f"{PROVIDER_PREFIX}{host.lower()}" if host else None


# ---------------------------------------------------------------------------
# Lectura / escritura de un ámbito
# ---------------------------------------------------------------------------


def read_scope(data: Mapping[str, Any] | None, key: str | None) -> TrackPreferences:
    """Preferencias guardadas para una clave concreta."""
    if not isinstance(data, Mapping) or not key:
        return TrackPreferences()
    bloque = data.get(key)
    return TrackPreferences.from_dict(bloque if isinstance(bloque, Mapping) else None)


def write_scope(data: MutableMapping[str, Any], key: str, prefs: TrackPreferences) -> None:
    """Guarda las preferencias de una clave (sobrescribe el bloque)."""
    if not key:
        return
    if prefs.is_empty():
        data.pop(key, None)
        return
    data[key] = prefs.to_dict()


# ---------------------------------------------------------------------------
# Resolución con precedencia
# ---------------------------------------------------------------------------


def _global_from(prefs: Prefs | None) -> TrackPreferences:
    if prefs is None:
        return TrackPreferences()
    return TrackPreferences(
        preferred_audio_language=_opt_str(prefs.preferred_audio_language),
        preferred_subtitle_language=_opt_str(prefs.preferred_subtitle_language),
        subtitles_enabled=bool(prefs.subtitles_enabled),
        preferred_quality=_opt_str(prefs.preferred_quality),
    )


def resolve_preferences(
    channel: Channel | None = None,
    source: str | None = None,
    *,
    prefs: Prefs | None = None,
    scopes: Mapping[str, Any] | None = None,
) -> TrackPreferences:
    """Resuelve la preferencia efectiva de un canal.

    Args:
        channel: canal cuyo audio/subtítulo/calidad se quiere fijar.
        source: fuente (URL o ruta) a la que pertenece el canal; da el
            ámbito "proveedor".
        prefs: preferencias globales ya cargadas.
        scopes: bloque ``track_prefs`` de ``prefs.json``. Si no se pasa, se
            usa ``{}`` (así la función es pura y testeable).

    Returns:
        La preferencia efectiva, campo a campo, canal → proveedor → global.
    """
    bloques = scopes if isinstance(scopes, Mapping) else {}
    niveles: list[tuple[str, TrackPreferences]] = []
    clave_canal = channel_key(channel)
    if clave_canal:
        niveles.append(("canal", read_scope(bloques, clave_canal)))
    clave_proveedor = provider_key(source)
    if clave_proveedor:
        niveles.append(("proveedor", read_scope(bloques, clave_proveedor)))
    niveles.append(("global", read_scope(bloques, GLOBAL_KEY)))
    niveles.append(("global", _global_from(prefs)))

    resultado = TrackPreferences()
    ámbito = "stream"
    for campo in (
        "preferred_audio_language",
        "preferred_subtitle_language",
        "preferred_quality",
    ):
        for nombre, bloque in niveles:
            valor = getattr(bloque, campo)
            if valor is not None:
                setattr(resultado, campo, valor)
                if campo == "preferred_audio_language":
                    ámbito = nombre
                break
    for nombre, bloque in niveles:
        if bloque.subtitles_enabled:
            resultado.subtitles_enabled = True
            if ámbito == "stream":
                ámbito = nombre
            break
    resultado.scope = ámbito
    return resultado


# ---------------------------------------------------------------------------
# "Recordar para este canal / este proveedor"
# ---------------------------------------------------------------------------


def remember_for_channel(
    prefs_manager: PrefsManager,
    channel: Channel,
    preferences: TrackPreferences,
) -> str | None:
    """Guarda las preferencias para **este canal** y persiste.

    Devuelve la clave usada, o None si el canal no tiene forma de tener clave
    (ni ``tvg-id`` ni URL). No lanza: si el disco falla, se devuelve None.
    """
    clave = channel_key(channel)
    if clave is None:
        return None
    prefs = prefs_manager.load()
    scopes = dict(getattr(prefs, "track_prefs", None) or {})
    write_scope(scopes, clave, preferences)
    prefs.track_prefs = scopes
    _save(prefs_manager, prefs)
    return clave


def remember_for_provider(
    prefs_manager: PrefsManager,
    source: str,
    preferences: TrackPreferences,
) -> str | None:
    """Guarda las preferencias para **toda la fuente** (proveedor)."""
    clave = provider_key(source)
    if clave is None:
        return None
    prefs = prefs_manager.load()
    scopes = dict(getattr(prefs, "track_prefs", None) or {})
    write_scope(scopes, clave, preferences)
    prefs.track_prefs = scopes
    _save(prefs_manager, prefs)
    return clave


def preferences_from_selection(
    capabilities, selection
) -> TrackPreferences:
    """Traduce una :class:`PlaybackSelection` a preferencias guardables.

    Un id de pista **nuestro** (``a2``, ``v720``) no significa nada fuera de
    este canal, así que sólo se guardan lo que es interpretable en cualquier
    sitio: el idioma y la altura. La calidad se guarda como altura
    (``720p``), que es estable aunque el manifiesto reordene variantes.
    """
    from .labels import language_label

    prefs = TrackPreferences()
    if selection is None:
        return prefs
    pista_audio = capabilities.audio_by_id(selection.audio_track_id) if capabilities else None
    if pista_audio is not None:
        prefs.preferred_audio_language = (
            pista_audio.language
            or language_label(pista_audio.original_language)
        )
    prefs.subtitles_enabled = bool(getattr(selection, "subtitles_enabled", False))
    pista_sub = capabilities.subtitle_by_id(selection.subtitle_track_id) if capabilities else None
    if pista_sub is not None:
        prefs.preferred_subtitle_language = (
            pista_sub.language or language_label(pista_sub.original_language)
        )
    if not getattr(selection, "auto_quality", True):
        variante = capabilities.variant_by_id(selection.video_track_id) if capabilities else None
        if variante is not None:
            prefs.preferred_quality = (
                f"{variante.height}p" if variante.height else QUICK_AUTO
            )
        else:
            prefs.preferred_quality = QUICK_AUTO
    return prefs


def _save(prefs_manager: PrefsManager, prefs: Prefs) -> None:
    try:
        prefs_manager.save(prefs)
    except OSError:
        # Preferir no recordar antes que romper la reproducción.
        return


def load_scopes(prefs_manager: PrefsManager) -> dict[str, Any]:
    """Bloque ``track_prefs`` del ``prefs.json``, o ``{}``."""
    prefs = prefs_manager.load()
    scopes = getattr(prefs, "track_prefs", None)
    return dict(scopes) if isinstance(scopes, Mapping) else {}


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None