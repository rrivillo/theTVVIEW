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
    """CRUD mínimo sobre favorites.json con escritura atómica.

    Cache en memoria: `is_favorite()`/`favorite_urls()` no tocan disco
    en cada llamada (antes hacían `load()`+JSON parse por canal, lo que
    colgaba la TUI con listas de decenas de miles de canales: N
    lecturas de disco por frame/tecla). La cache se invalida al
    escribir y se refresca si el archivo cambia por fuera (mtime).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache: list[Channel] | None = None
        self._cache_mtime: float | None = None

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
        self._cache = list(channels)
        try:
            self._cache_mtime = self.path.stat().st_mtime
        except OSError:
            self._cache_mtime = None

    def _cache_valid(self) -> bool:
        """True si la cache en memoria sigue vigente (mtime sin cambios)."""
        if self._cache is None:
            return False
        try:
            return self.path.stat().st_mtime == self._cache_mtime
        except OSError:
            # Archivo borrado o ilegible: solo vale la cache si era vacía.
            return not self._cache and self._cache_mtime is None

    def invalidate(self) -> None:
        """Olvida la cache en memoria (forzar re-lectura en el próximo uso)."""
        self._cache = None
        self._cache_mtime = None

    # --- API pública ----------------------------------------------------------

    def load(self) -> list[Channel]:
        """Carga todos los favoritos; lista vacía si el archivo no existe.

        Usa cache en memoria: solo relee el JSON si cambió en disco.
        Devuelve una copia para que el llamante no mute la cache.
        """
        if self._cache_valid():
            return list(self._cache or [])
        out: list[Channel] = []
        for raw in self._read():
            try:
                raw["extra_options"] = [tuple(p) for p in raw.get("extra_options", [])]
                out.append(Channel(**raw))
            except (KeyError, TypeError):
                continue  # entrada malformada: se ignora sin romper la app
        self._cache = list(out)
        try:
            self._cache_mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            self._cache_mtime = None
        return list(out)

    def favorite_urls(self) -> set[str]:
        """Set de urls favoritas (O(1) por consulta, sin IO repetido)."""
        if self._cache_valid():
            return {c.url for c in (self._cache or [])}
        return {c.url for c in self.load()}

    def is_favorite(self, channel: Channel) -> bool:
        return channel.url in self.favorite_urls()

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
