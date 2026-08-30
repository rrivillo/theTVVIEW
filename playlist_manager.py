"""Gestión y persistencia del catálogo de playlists (playlists.json).

Solo stdlib. El archivo es una lista de entradas:
    [{"name": "...", "source": "path_o_url", "added": "ISO8601"}, ...]

TODO(playlist_manager):
- Validar que `source` exista si es ruta local al momento de usarla.
- Soportar reordenar/renombrar entradas.
- Deduplicar por `source` además de por nombre.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


class PlaylistError(Exception):
    """Error amigable de gestión de playlists."""


@dataclass
class PlaylistEntry:
    """Una playlist registrada en el catálogo."""

    name: str
    source: str  # ruta local o URL
    added: str = ""  # fecha ISO8601

    def __post_init__(self) -> None:
        if not self.added:
            self.added = datetime.now(timezone.utc).isoformat(timespec="seconds")


class PlaylistManager:
    """CRUD mínimo sobre playlists.json con escritura atómica."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

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
        payload = json.dumps([asdict(e) for e in entries], ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)  # atómico en POSIX

    # --- API pública --------------------------------------------------------

    def load(self) -> list[PlaylistEntry]:
        """Carga todas las entradas; lista vacía si el archivo no existe."""
        out: list[PlaylistEntry] = []
        for raw in self._read():
            try:
                out.append(
                    PlaylistEntry(
                        name=str(raw["name"]),
                        source=str(raw["source"]),
                        added=str(raw.get("added", "")),
                    )
                )
            except KeyError:
                continue  # entrada malformada: se ignora sin romper la app
        return out

    def add(self, name: str, source: str) -> PlaylistEntry:
        """Añade una playlist; falla amigablemente si el nombre ya existe."""
        name, source = name.strip(), source.strip()
        if not name or not source:
            raise PlaylistError("Nombre y origen son obligatorios.")
        entries = self.load()
        if any(e.name == name for e in entries):
            raise PlaylistError(f"Ya existe una playlist llamada '{name}'.")
        entry = PlaylistEntry(name=name, source=source)
        entries.append(entry)
        self._write(entries)
        return entry

    def remove(self, name: str) -> bool:
        """Elimina por nombre. Devuelve True si se eliminó algo."""
        entries = self.load()
        kept = [e for e in entries if e.name != name]
        if len(kept) == len(entries):
            return False
        self._write(kept)
        return True

    def get(self, name: str) -> PlaylistEntry | None:
        return next((e for e in self.load() if e.name == name), None)
