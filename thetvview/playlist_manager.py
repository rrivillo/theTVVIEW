"""Gestión y persistencia del catálogo de playlists (playlists.json).

Solo stdlib. El archivo es una lista de entradas:
    [{"name": "...", "source": "path_o_url", "added": "ISO8601",
      "kind": "m3u", "server_url": null, "username": null}, ...]

Soporta playlists M3U (kind="m3u") y fuentes Xtream (kind="xtream").
Para Xtream: server_url y username se persisten; password NUNCA en disco.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class PlaylistError(Exception):
    """Error amigable de gestión de playlists."""


@dataclass
class PlaylistEntry:
    """Una playlist registrada en el catálogo.

    - kind: "m3u" (default) o "xtream"
    - server_url, username: solo para kind="xtream", se persisten.
    - password: NUNCA se persiste; se pide en sesión cada vez.
    """

    name: str
    source: str  # ruta local, URL, o "xtream://<server_url>" para kind=xtream
    added: str = ""  # fecha ISO8601
    kind: str = "m3u"  # "m3u" | "xtream"
    server_url: str = ""  # solo para xtream
    username: str = ""  # solo para xtream
    # password NO se persiste: campo temporal de sesión
    password: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.added:
            self.added = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Normalizar kind
        if self.kind not in ("m3u", "xtream"):
            self.kind = "m3u"

    @property
    def is_xtream(self) -> bool:
        return self.kind == "xtream"

    def to_dict(self) -> dict:
        """Serializa a dict para JSON, excluyendo password."""
        d = asdict(self)
        d.pop("password", None)
        # Limpiar campos vacíos para entradas M3U
        if self.kind == "m3u":
            d.pop("server_url", None)
            d.pop("username", None)
        return d


class PlaylistManager:
    """CRUD mínimo sobre playlists.json con escritura atómica."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # Passwords en memoria (nunca persistidas). key=nombre.
        self._passwords: dict[str, str] = {}

    # --- Persistencia -----------------------------------------------------

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlaylistError(
                f"No se pudo leer {self.path}: {exc.__class__.__name__}. "
                "El archivo puede estar corrupto."
            ) from exc
        if not isinstance(data, list):
            raise PlaylistError(f"Formato inesperado en {self.path} (se esperaba lista).")
        return [e for e in data if isinstance(e, dict)]

    def _write(self, entries: list[PlaylistEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(
            [e.to_dict() for e in entries],
            ensure_ascii=False,
            indent=2,
        )
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)  # atómico en POSIX

    # --- API pública --------------------------------------------------------

    def load(self) -> list[PlaylistEntry]:
        """Carga todas las entradas; lista vacía si el archivo no existe.

        Entradas viejas sin 'kind' se cargan como kind="m3u" (migración invisible).
        """
        out: list[PlaylistEntry] = []
        for raw in self._read():
            try:
                out.append(
                    PlaylistEntry(
                        name=str(raw["name"]),
                        source=str(raw["source"]),
                        added=str(raw.get("added", "")),
                        kind=str(raw.get("kind", "m3u")),
                        server_url=str(raw.get("server_url", "")),
                        username=str(raw.get("username", "")),
                    )
                )
            except KeyError:
                continue  # entrada malformada: se ignora sin romper la app
        return out

    def add(self, name: str, source: str) -> PlaylistEntry:
        """Añade una playlist IPTV (.m3u/.m3u8/.ts); falla amigablemente si el nombre ya existe."""
        name, source = name.strip(), source.strip()
        if not name or not source:
            raise PlaylistError("Nombre y origen son obligatorios.")
        entries = self.load()
        if any(e.name == name for e in entries):
            raise PlaylistError(f"Ya existe una playlist llamada '{name}'.")
        entry = PlaylistEntry(name=name, source=source, kind="m3u")
        entries.append(entry)
        self._write(entries)
        return entry

    def add_xtream(
        self,
        name: str,
        server_url: str,
        username: str,
        password: str,
    ) -> PlaylistEntry:
        """Añade una fuente Xtream. Password se guarda solo en memoria."""
        name = name.strip()
        server_url = server_url.strip()
        username = username.strip()
        if not name or not server_url or not username:
            raise PlaylistError("Nombre, servidor y usuario son obligatorios.")
        entries = self.load()
        if any(e.name == name for e in entries):
            raise PlaylistError(f"Ya existe una playlist llamada '{name}'.")
        # Source sintético para backward compat
        source = f"xtream://{server_url}"
        entry = PlaylistEntry(
            name=name,
            source=source,
            kind="xtream",
            server_url=server_url,
            username=username,
        )
        entries.append(entry)
        self._write(entries)
        # Password solo en memoria
        self._passwords[name] = password
        return entry

    def get_credentials(self, name: str) -> tuple[str, str, str] | None:
        """Devuelve (server_url, username, password) de una entrada Xtream.

        Password se almacena solo en memoria; si no está configurado, devuelve None.
        """
        entry = self.get(name)
        if entry is None or not entry.is_xtream:
            return None
        password = self._passwords.get(name, "")
        if not password:
            return None
        return entry.server_url, entry.username, password

    def set_password(self, name: str, password: str) -> bool:
        """Establece la password en memoria para una entrada Xtream.

        No persiste: se usa durante la sesión actual.
        Devuelve True si la entrada existía y era Xtream.
        """
        entry = self.get(name)
        if entry is None or not entry.is_xtream:
            return False
        self._passwords[name] = password
        return True

    def remove(self, name: str) -> bool:
        """Elimina por nombre. Limpia cache Xtream si aplica.

        Devuelve True si se eliminó algo.
        """
        entry = self.get(name)
        entries = self.load()
        kept = [e for e in entries if e.name != name]
        if len(kept) == len(entries):
            return False
        self._write(kept)
        # Limpiar cache Xtream si la entrada era Xtream
        if entry is not None and entry.is_xtream and entry.server_url and entry.username:
            try:
                from .xtream_provider import _clear_cache
                _clear_cache(entry.server_url, entry.username)
            except Exception:
                pass  # fallo silencioso: no afecta al usuario
        return True

    def get(self, name: str) -> PlaylistEntry | None:
        return next((e for e in self.load() if e.name == name), None)
