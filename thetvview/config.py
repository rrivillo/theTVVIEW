"""Configuración de rutas y detección de reproductores para theTVVIEW.

Solo stdlib. Las rutas se derivan del directorio del proyecto (padre del
paquete) para que funcione tanto en desarrollo como desde el venv.

TODO(config):
- Permitir overrides por variables de entorno (THETVVIEW_DATA_DIR) o
  archivo de configuración de usuario (~/.config/thetvview/).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path

# --- Rutas base -----------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = PROJECT_ROOT / "data"
EPG_CACHE_DIR: Path = DATA_DIR / "epg_cache"
PLAYLIST_CACHE_DIR: Path = DATA_DIR / "playlist_cache"
PLAYLISTS_JSON: Path = DATA_DIR / "playlists.json"
FAVORITES_JSON: Path = DATA_DIR / "favorites.json"
THEME_JSON: Path = DATA_DIR / "theme.json"
PREFS_JSON: Path = DATA_DIR / "prefs.json"
RECENTS_JSON: Path = DATA_DIR / "recents.json"
XTREAM_CACHE_DIR: Path = DATA_DIR / "xtream_cache"

SUPPORTED_PLAYERS: tuple[str, ...] = ("mpv", "mplayer", "vlc")


def anonymize_path(path: str) -> str:
    """Oculta el directorio home del usuario en un path para no exponerlo en UI/logs.

    Ej: "/home/alicia/.local/bin/mpv" -> "~/.local/bin/mpv"
        "C:\\Users\\Alicia\\mpv\\mpv.exe" -> "~\\mpv\\mpv.exe"
    Si el path no contiene el home, se devuelve tal cual.
    """
    if not path:
        return path
    try:
        home = str(Path.home())
        if not home:
            return path
        # Normalizar para comparación (Windows case-insensitive)
        p_norm = path
        h_norm = home
        # En Windows comparar en minúsculas
        if os.name == "nt":
            if p_norm.lower().startswith(h_norm.lower()):
                return "~" + p_norm[len(h_norm):]
            return path
        # POSIX
        if p_norm.startswith(h_norm):
            return "~" + p_norm[len(h_norm):]
        # Fallback extra: ocultar cualquier "/home/<user>" o "/Users/<user>"
        # aunque no sea el home actual (p. ej. path de otro usuario visible).
        # Se reemplaza "/home/alguien" -> "/home/~"
        # Solo si parece ruta absoluta y contiene segmento de usuario.
        for prefix in ("/home/", "/Users/"):
            if p_norm.startswith(prefix):
                # "/home/alicia/foo" -> "/home/~/foo"
                rest = p_norm[len(prefix):]
                slash = rest.find("/")
                if slash != -1:
                    return prefix + "~" + rest[slash:]
                return prefix + "~"
    except Exception:
        pass
    return path


def _candidate_paths(name: str) -> list[Path]:
    """Rutas candidatas extra por plataforma si shutil.which no encuentra el binario.

    No depende de shell y cubre ubicaciones típicas fuera del PATH:
    - Windows: Program Files, AppData, Scoop, etc.
    - macOS: /Applications/*.app, Homebrew, MacPorts
    - Linux: /usr/local/bin, /snap/bin, Flatpak exports
    """
    system = platform.system()
    candidates: list[Path] = []
    exe_suffix = ".exe" if system == "Windows" else ""

    def _exe(base: str) -> str:
        return base + exe_suffix if not base.endswith(exe_suffix) else base

    if system == "Windows":
        prog_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        prog_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local_app = os.environ.get("LOCALAPPDATA", "")
        app_data = os.environ.get("APPDATA", "")
        home = Path.home()
        if name == "vlc":
            candidates.extend([
                Path(prog_files) / "VideoLAN" / "VLC" / _exe("vlc"),
                Path(prog_files_x86) / "VideoLAN" / "VLC" / _exe("vlc"),
            ])
        elif name == "mpv":
            candidates.extend([
                Path(prog_files) / "mpv" / _exe("mpv"),
                Path(prog_files_x86) / "mpv" / _exe("mpv"),
                Path(local_app) / "mpv" / _exe("mpv") if local_app else Path(),
                home / "scoop" / "apps" / "mpv" / "current" / _exe("mpv"),
                home / "AppData" / "Local" / "mpv" / _exe("mpv"),
            ])
        elif name == "mplayer":
            candidates.extend([
                Path(prog_files) / "MPlayer" / _exe("mplayer"),
                Path(prog_files_x86) / "MPlayer" / _exe("mplayer"),
                Path(prog_files) / "SMPlayer" / "mplayer" / _exe("mplayer"),
            ])
        # También PATH portable en la carpeta del proyecto/venv (por si acaso)
        candidates = [p for p in candidates if str(p) and p != Path()]
    elif system == "Darwin":
        # macOS
        if name == "vlc":
            candidates.extend([
                Path("/Applications/VLC.app/Contents/MacOS/VLC"),
                Path("/Applications/VLC.app/Contents/MacOS/clivlc"),
                Path("/opt/homebrew/bin") / _exe(name),
                Path("/usr/local/bin") / _exe(name),
                Path("/opt/local/bin") / _exe(name),
            ])
        elif name == "mpv":
            candidates.extend([
                Path("/opt/homebrew/bin") / _exe(name),
                Path("/usr/local/bin") / _exe(name),
                Path("/opt/local/bin") / _exe(name),
                Path("/Applications/mpv.app/Contents/MacOS/mpv"),
            ])
        elif name == "mplayer":
            candidates.extend([
                Path("/opt/homebrew/bin") / _exe(name),
                Path("/usr/local/bin") / _exe(name),
                Path("/opt/local/bin") / _exe(name),
            ])
    else:
        # Linux y otros POSIX (incluye LMDE 7)
        # shutil.which ya cubre PATH, aquí solo extras fuera de PATH
        if name == "vlc":
            candidates.extend([
                Path("/usr/bin") / name,
                Path("/usr/local/bin") / name,
                Path("/snap/bin") / name,
                Path("/var/lib/flatpak/exports/bin/org.videolan.VLC"),
                Path.home() / ".local" / "bin" / name,
                Path.home() / ".local" / "share" / "flatpak" / "exports" / "bin" / "org.videolan.VLC",
            ])
        elif name == "mpv":
            candidates.extend([
                Path("/usr/bin") / name,
                Path("/usr/local/bin") / name,
                Path("/snap/bin") / name,
                Path.home() / ".local" / "bin" / name,
            ])
        elif name == "mplayer":
            candidates.extend([
                Path("/usr/bin") / name,
                Path("/usr/local/bin") / name,
                Path.home() / ".local" / "bin" / name,
            ])
    # Filtrar entradas vacías
    return [p for p in candidates if str(p)]


def ensure_dirs() -> None:
    """Crea los directorios de datos si no existen (idempotente)."""
    EPG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PLAYLIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    XTREAM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PREFS_JSON.parent.mkdir(parents=True, exist_ok=True)
    RECENTS_JSON.parent.mkdir(parents=True, exist_ok=True)


def load_theme() -> str:
    """Lee el tema persistente de theme.json. Devuelve 'light' si falla."""
    try:
        data = json.loads(THEME_JSON.read_text(encoding="utf-8"))
        name = str(data.get("theme", "light")).strip().lower()
        if name in ("light", "dark"):
            return name
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return "light"


def save_theme(name: str) -> None:
    """Persiste el tema en theme.json (atomic write mínimo)."""
    if name not in ("light", "dark"):
        return
    try:
        THEME_JSON.parent.mkdir(parents=True, exist_ok=True)
        THEME_JSON.write_text(json.dumps({"theme": name}) + "\n", encoding="utf-8")
    except OSError:
        pass  # degrada silencioso


def find_player(name: str) -> str | None:
    """Devuelve la ruta absoluta del reproductor `name` si está instalado.

    Estrategia multiplataforma (solo stdlib, nunca shell):
    1. shutil.which (respeta PATH y PATHEXT en Windows).
    2. Si falla, revisa ubicaciones típicas por SO (Program Files en
       Windows, /Applications y Homebrew en macOS, /snap/flatpak en Linux).

    Devuelve None si no está disponible.
    """
    if name not in SUPPORTED_PLAYERS:
        return None
    found = shutil.which(name)
    if found:
        return found
    # Fallback: ubicaciones conocidas fuera de PATH
    for candidate in _candidate_paths(name):
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def detect_players() -> dict[str, str | None]:
    """Detecta todos los reproductores soportados.

    Returns:
        dict {nombre: ruta_absoluta_o_None} en orden de preferencia.
    """
    return {name: find_player(name) for name in SUPPORTED_PLAYERS}
