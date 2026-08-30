"""Persistencia de canales favoritos (data/favorites.json).

Solo stdlib. El archivo es una lista de canales serializados:
    [{"name": ..., "url": ..., "tvg_id": ..., ...}, ...]

La identidad de un favorito es su `url`: toggle añade/quita por url.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import Channel


class FavoritesError(Exception):
    """Error amigable de gestión de favoritos."""


class FavoritesManager:
    """CRUD mínimo sobre favorites.json con escritura atómica."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # --- Persistencia -------------------------------------------------------

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FavoritesError(
                f"No se pudo leer {self.path}: {exc.__class__.__name__}. "
                "El archivo puede estar corrupto."
            ) from exc
        if not isinstance(data, list):
            raise FavoritesError(f"Formato inesperado en {self.path} (se esperaba lista).")
        return [e for e in data if isinstance(e, dict)]

    def _write(self, channels: list[Channel]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps([asdict(c) for c in channels], ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)  # atómico en POSIX

    # --- API pública ----------------------------------------------------------

    def load(self) -> list[Channel]:
        """Carga todos los favoritos; lista vacía si el archivo no existe."""
        out: list[Channel] = []
        for raw in self._read():
            try:
                raw["extra_options"] = [tuple(p) for p in raw.get("extra_options", [])]
                out.append(Channel(**raw))
            except (KeyError, TypeError):
                continue  # entrada malformada: se ignora sin romper la app
        return out

    def is_favorite(self, channel: Channel) -> bool:
        return any(c.url == channel.url for c in self.load())

    def toggle(self, channel: Channel) -> bool:
        """Añade o quita el canal. True si quedó como favorito."""
        entries = self.load()
        kept = [c for c in entries if c.url != channel.url]
        if len(kept) != len(entries):
            self._write(kept)
            return False
        entries.append(channel)
        self._write(entries)
        return True

    def remove(self, channel: Channel) -> bool:
        """Quita por url. Devuelve True si se eliminó algo."""
        entries = self.load()
        kept = [c for c in entries if c.url != channel.url]
        if len(kept) == len(entries):
            return False
        self._write(kept)
        return True
