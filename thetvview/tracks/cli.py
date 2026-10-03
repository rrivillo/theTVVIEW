"""Overrides de pistas de una sesión (SDD §43, opcional).

    python -m thetvview --audio es --subtitles off --quality 720p

Son **sólo de esta ejecución**: no tocan `prefs.json` ni ningún flujo
existente, así que se pueden usar para probar un canal sin cambiar la
configuración del usuario. El valor por defecto (sin opciones) es
exactamente el comportamiento anterior.

Un valor no reconocido se explica y se sale con código 2: ignorar una opción
mal escrita en silencio sería peor que no tenerla.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "SessionOverrides",
    "OVERRIDE_OPTIONS",
    "parse_overrides",
    "set_session_overrides",
    "get_session_overrides",
    "clear_session_overrides",
]


@dataclass(frozen=True)
class SessionOverrides:
    """Lo que el usuario pidió en la línea de órdenes, si algo pidió."""

    audio_language: str | None = None
    subtitle_language: str | None = None
    #: True = subtítulos sí; False = subtítulos no; None = sin cambio.
    subtitles_enabled: bool | None = None
    #: "auto" para dejar el ABR al reproductor, o una altura/id de variante.
    quality: str | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.audio_language is None
            and self.subtitle_language is None
            and self.subtitles_enabled is None
            and self.quality is None
        )

    def merged_with(self, prefs) -> object:
        """Devuelve unas preferencias con los overrides por encima.

        Se apoya en :class:`~thetvview.tracks.prefs.TrackPreferences`, que
        tiene exactamente los nombres que espera
        :mod:`thetvview.tracks.manager`.
        """
        from .prefs import TrackPreferences

        base = TrackPreferences(
            preferred_audio_language=getattr(prefs, "preferred_audio_language", None),
            preferred_subtitle_language=getattr(
                prefs, "preferred_subtitle_language", None
            ),
            subtitles_enabled=bool(getattr(prefs, "subtitles_enabled", False)),
            preferred_quality=getattr(prefs, "preferred_quality", None),
        )
        if self.audio_language:
            base.preferred_audio_language = self.audio_language
        if self.subtitle_language:
            base.preferred_subtitle_language = self.subtitle_language
        if self.subtitles_enabled is not None:
            base.subtitles_enabled = self.subtitles_enabled
        if self.quality:
            base.preferred_quality = self.quality
        return base


#: Opciones aceptadas -> True si llevan valor detrás.
OVERRIDE_OPTIONS: dict[str, bool] = {
    "--audio": True,
    "--subtitles": True,
    "--quality": True,
}

_OFF = {"off", "no", "0", "false", "disabled"}
_ON = {"on", "yes", "1", "true", "enabled"}


def parse_overrides(argv: list[str]) -> tuple[SessionOverrides, str | None]:
    """Lee los overrides de un argv. Devuelve ``(overrides, error)``.

    Sólo reconoce las opciones de :data:`OVERRIDE_OPTIONS`; cualquier otro
    argumento devuelve un error explicando qué se esperaba. Es deliberado:
    la app no tiene otras opciones de línea de órdenes, y adivinar sería peor
    que explicar.
    """
    audio = subs = quality = None
    subs_on: bool | None = None
    indice = 0
    while indice < len(argv):
        arg = argv[indice]
        if arg not in OVERRIDE_OPTIONS:
            return SessionOverrides(), (
                f"No se reconoce la opción '{arg}'."
            )
        if indice + 1 >= len(argv):
            return SessionOverrides(), f"Falta el valor de '{arg}'."
        valor = argv[indice + 1].strip()
        indice += 2
        if not valor:
            return SessionOverrides(), f"El valor de '{arg}' está vacío."
        if arg == "--audio":
            audio = valor
        elif arg == "--quality":
            quality = valor
        else:
            bajo = valor.lower()
            if bajo in _OFF:
                subs_on = False
                subs = None
            elif bajo in _ON:
                subs_on = True
            else:
                # Un idioma: subtítulos de ese idioma, activados.
                subs = valor
                subs_on = True
    return SessionOverrides(
        audio_language=audio,
        subtitle_language=subs,
        subtitles_enabled=subs_on,
        quality=quality,
    ), None


_ACTIVE: SessionOverrides | None = None


def set_session_overrides(overrides: SessionOverrides) -> None:
    """Los deja activos para este proceso."""
    global _ACTIVE
    _ACTIVE = None if overrides.is_empty else overrides


def get_session_overrides() -> SessionOverrides | None:
    """Overrides activos, o None si no se pasó ninguno."""
    return _ACTIVE


def clear_session_overrides() -> None:
    """Los borra (tests)."""
    global _ACTIVE
    _ACTIVE = None
