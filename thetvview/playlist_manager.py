"""Gestión y persistencia del catálogo de playlists (playlists.json).

Solo stdlib. El archivo es una lista de entradas:
    [{"name": "...", "source": "path_o_url", "added": "ISO8601",
      "kind": "m3u", "server_url": null, "username": null}, ...]

Soporta playlists M3U (kind="m3u") y fuentes Xtream (kind="xtream").
Para Xtream: server_url, username y password se persisten (solo Xtream).
Para M3U: nunca se guarda password.
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
    - server_url, username, password: solo para kind="xtream", se persisten.
      Para kind="m3u" nunca se guarda password.
    """

    name: str
    source: str  # ruta local, URL, o "xtream://<server_url>" para kind=xtream
    added: str = ""  # fecha ISO8601
    kind: str = "m3u"  # "m3u" | "xtream"
    server_url: str = ""  # solo para xtream
    username: str = ""  # solo para xtream
    # password: solo se persiste para kind="xtream". En memoria para la sesión.
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
        """Serializa a dict para JSON.

        - kind="xtream": incluye server_url, username y password.
        - kind="m3u": nunca incluye password (ni server_url/username).
        """
        d = asdict(self)
        if self.kind == "m3u":
            d.pop("server_url", None)
            d.pop("username", None)
            d.pop("password", None)
        return d


class PlaylistManager:
    """CRUD mínimo sobre playlists.json con escritura atómica."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # Passwords en memoria (cache de sesión). key=nombre.
        # Se sincroniza con el password persistido en disco (solo Xtream).
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
        # playlists.json puede contener passwords Xtream: restringir a 0o600.
        try:
            import os

            os.chmod(self.path, 0o600)
        except (OSError, AttributeError):
            pass  # Windows o FS sin permisos: degrada sin romper

    # --- API pública --------------------------------------------------------

    def load(self) -> list[PlaylistEntry]:
        """Carga todas las entradas; lista vacía si el archivo no existe.

        Entradas viejas sin 'kind' se cargan como kind="m3u" (migración invisible).
        El password solo se restaura para kind="xtream"; en M3U se ignora.
        """
        out: list[PlaylistEntry] = []
        for raw in self._read():
            try:
                kind = str(raw.get("kind", "m3u"))
                password = ""
                if kind == "xtream":
                    password = str(raw.get("password", ""))
                entry = PlaylistEntry(
                    name=str(raw["name"]),
                    source=str(raw["source"]),
                    added=str(raw.get("added", "")),
                    kind=kind,
                    server_url=str(raw.get("server_url", "")),
                    username=str(raw.get("username", "")),
                    password=password,
                )
                out.append(entry)
                # Sincronizar cache de sesión con lo persistido.
                if entry.is_xtream and password:
                    self._passwords[entry.name] = password
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
        """Añade una fuente Xtream. Password se persiste (solo Xtream)."""
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
            password=password or "",
        )
        entries.append(entry)
        self._write(entries)
        # Cache de sesión sincronizada con disco.
        self._passwords[name] = password or ""
        return entry

    def get_credentials(self, name: str) -> tuple[str, str, str] | None:
        """Devuelve (server_url, username, password) de una entrada Xtream.

        Usa la cache de sesión y, si falta, el password persistido en disco.
        Solo Xtream: para M3U siempre devuelve None.
        Si no hay password configurado, devuelve None (hay que pedirlo).
        """
        entry = self.get(name)
        if entry is None or not entry.is_xtream:
            return None
        password = self._passwords.get(name, "") or entry.password or ""
        if not password:
            return None
        # Mantener cache sincronizada.
        self._passwords[name] = password
        return entry.server_url, entry.username, password

    def set_password(self, name: str, password: str) -> bool:
        """Establece/cambia la password de una entrada Xtream y la persiste.

        Solo Xtream: para M3U devuelve False y no hace nada.
        Devuelve True si la entrada existía y era Xtream.
        """
        return self.update_password(name, password)

    def update_password(self, name: str, new_password: str) -> bool:
        """Cambia la contraseña de una lista Xtream (persistida en disco).

        Solo aplica a kind="xtream". Invalida la cache de auth del proveedor
        para que la próxima apertura re-autentique con la nueva contraseña.
        Devuelve True si se actualizó, False si no existe o no es Xtream.
        """
        entries = self.load()
        found = False
        target: PlaylistEntry | None = None
        for e in entries:
            if e.name == name and e.is_xtream:
                e.password = new_password or ""
                found = True
                target = e
                break
        if not found:
            return False
        self._write(entries)
        self._passwords[name] = new_password or ""
        # Invalidar cache de auth: la password vieja ya no vale.
        if target is not None and target.server_url and target.username:
            try:
                from .xtream_provider import _clear_cache

                _clear_cache(target.server_url, target.username)
            except Exception:
                pass
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
