from __future__ import annotations

from dataclasses import dataclass, asdict, field
import json
from pathlib import Path
from typing import Any

from . import config


@dataclass
class Prefs:
    last_player: str | None = None
    last_group_sort: str = "name"
    theme: str = "light"

    # --- Pistas (SDD §31, plan F4) --------------------------------------
    # Idioma de audio preferido, ya normalizado ("es") o tal cual lo escribió
    # el usuario. None = "usa el DEFAULT del stream".
    preferred_audio_language: str | None = None
    preferred_subtitle_language: str | None = None
    subtitles_enabled: bool = False
    #: "auto" para dejar el ABR al reproductor, o una altura ("720p") / un id
    #: de variante concreta.
    preferred_quality: str | None = None
    #: Si False, la app no propone el selector de pistas (sigue pudiendo
    #: cambiarse desde "Reproduciendo").
    ask_track_options: bool = True
    #: Preferencias por canal/proveedor. Las claves son ``tvg:...``,
    #: ``channel:<sha256 de la URL redactada>`` o ``provider:<host>``; los
    #: valores nunca contienen URLs ni credenciales (SDD §37).
    track_prefs: dict[str, dict[str, Any]] = field(default_factory=dict)


class PrefsManager:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else config.PREFS_JSON

    def load(self) -> Prefs:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return Prefs(
                last_player=str(data.get("last_player") or "") or None,
                last_group_sort=str(data.get("last_group_sort") or "name") or "name",
                theme=str(data.get("theme") or "light") or "light",
                preferred_audio_language=_opt_str(data.get("preferred_audio_language")),
                preferred_subtitle_language=_opt_str(
                    data.get("preferred_subtitle_language")
                ),
                subtitles_enabled=bool(data.get("subtitles_enabled")),
                preferred_quality=_opt_str(data.get("preferred_quality")),
                ask_track_options=bool(data.get("ask_track_options", True)),
                track_prefs=_scopes(data.get("track_prefs")),
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return Prefs()

    def save(self, prefs: Prefs) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(prefs), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def set_last_player(self, name: str | None) -> None:
        p = self.load()
        p.last_player = name
        self.save(p)

    def last_player(self) -> str | None:
        """Último reproductor elegido (o None si nunca se eligió uno).

        Lo usa también la reproducción de archivo para no abrir un
        reproductor distinto del que el usuario usa para el directo.
        """
        return self.load().last_player

    def set_last_group_sort(self, value: str) -> None:
        if value not in ("name", "count"):
            return
        p = self.load()
        p.last_group_sort = value
        self.save(p)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _scopes(value: Any) -> dict[str, dict[str, Any]]:
    """Sanea ``track_prefs``: sólo bloques ``dict`` con claves conocidas."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, bloque in value.items():
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(bloque, dict):
            continue
        limpio = {
            campo: bloque[campo]
            for campo in (
                "preferred_audio_language",
                "preferred_subtitle_language",
                "subtitles_enabled",
                "preferred_quality",
            )
            if campo in bloque
        }
        if limpio:
            out[key] = limpio
    return out