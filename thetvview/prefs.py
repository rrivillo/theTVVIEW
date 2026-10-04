from __future__ import annotations

from dataclasses import dataclass, asdict, field
import json
from pathlib import Path
from typing import Any, overload

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
    #: Si False, la app no propone el selector de pistas. Con el canal ya
    #: abierto ya no se cambian (se eligieron antes del reproductor): para
    #: verlas hay que volver a elegir reproductor con `p`.
    ask_track_options: bool = True
    #: Preferencias por canal/proveedor. Las claves son ``tvg:...``,
    #: ``channel:<sha256 de la URL redactada>`` o ``provider:<host>``; los
    #: valores nunca contienen URLs ni credenciales (SDD §37).
    track_prefs: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- Multi-stream (SDD-M §33, plan Fase 7) -----------------------------
    # El §33 del SDD-M propone un TOML con [player], [playback], [reconnect]
    # y [diagnostics]. Aquí vive en `prefs.json`, que es el formato que el
    # repo ya usa, con su único cargador y sus tests (decisión D6): un segundo
    # formato de configuración sería un segundo sitio donde cambiar una opción.
    #
    #: Reproductor preferido. None = "el que la tabla diga que puede abrir
    #: este transporte", no "el último usado": son cosas distintas y
    #: confundirlas haría que un RTSP acabara en el binario que el usuario usó
    #: para un HLS. `player.router` lo trata como el criterio 1 del §27, por
    #: encima de la prioridad, y sólo si puede abrir el transporte.
    preferred_backend: str | None = None
    #: Segundos para abrir la conexión antes de darla por perdida (§18).
    #: `None` = el valor del reproductor. Fijarlo aquí tiene sentido sobre todo
    #: para RTSP y UDP, donde una cámara apagada se queda esperando en silencio
    #: mucho más que un canal HTTP.
    connect_timeout: int | None = None
    #: Segundos para que el medio empiece a llegar (§18).
    startup_timeout: int | None = None
    #: Intentos de reconexión como máximo (§17). El tope de **cambio de
    #: reproductor** es otro y vive en `player.router.MAX_BACKEND_ATTEMPTS`
    #: (§26): confundirlos convertiría un canal malo en veinte esperas.
    reconnect_max_attempts: int = 5
    #: Perfil de red (§19). "balanced" = el de siempre; "low_latency" y
    #: "stable" se traducen a banderas reales del reproductor, no a un búfer en
    #: este proceso.
    playback_profile: str = "balanced"
    #: Si False, el menú de diagnóstico no propone comprobar la conexión (que
    #: sale a la red). El informe **sin** conexión siempre está disponible:
    #: es puro y no cuesta nada.
    diagnostics_enabled: bool = True


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
                preferred_backend=_opt_backend(data.get("preferred_backend")),
                connect_timeout=_opt_int(data.get("connect_timeout")),
                startup_timeout=_opt_int(data.get("startup_timeout")),
                reconnect_max_attempts=_opt_int(
                    data.get("reconnect_max_attempts"), default=5, minimum=0, maximum=20
                ),
                playback_profile=_perfil(data.get("playback_profile")),
                diagnostics_enabled=bool(
                    data.get("diagnostics_enabled", True)
                ),
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


def _opt_backend(value: Any) -> str | None:
    """Nombre de reproductor, o None si no es un nombre.

    A diferencia de :func:`_opt_str`, **no** convierte cualquier cosa a texto:
    un ``42`` en ``prefs.json`` no es el nombre de un reproductor, y
    aceptarlo haría que el router creyera que el usuario eligió uno que no
    existe. Mejor ningún preferido que un preferido inventado.
    """
    if not isinstance(value, str):
        return None
    texto = value.strip().lower()
    return texto or None


@overload
def _opt_int(
    value: Any, *, minimum: int = ..., maximum: int = ...
) -> int | None: ...


@overload
def _opt_int(
    value: Any, *, default: int, minimum: int = ..., maximum: int = ...
) -> int: ...


def _opt_int(
    value: Any,
    *,
    default: int | None = None,
    minimum: int = 0,
    maximum: int = 3600,
) -> int | None:
    """Entero de `prefs.json`, acotado. Basura → `default`.

    Acotar no es cosmético: `reconnect_max_attempts` viene de un fichero que
    el usuario puede editar a mano, y un 100000 ahí significa una app que no
    responde. Fuera de rango se usa el valor por defecto, que es lo
    conocido-bueno.

    Las sobrecargas existen para que el compilador sepa que, si se pasa
    `default`, el resultado **siempre** es un entero. Sin ellas el tipo es
    `int | None` y el que llama a un campo que no admite `None` recibe un
    error… o, peor, un `None` colándose en un `Prefs`.
    """
    if value is None:
        return default
    try:
        numero = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if numero < minimum or numero > maximum:
        return default
    return numero


def _perfil(value: Any) -> str:
    """Perfil de red (§19), normalizado. Desconocido → "balanced"."""
    texto = _opt_str(value) or "balanced"
    return texto if texto in ("low_latency", "balanced", "stable") else "balanced"


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