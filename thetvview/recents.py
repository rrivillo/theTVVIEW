from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import Channel
from .security.local_files import chmod_private
from .security.redaction import needs_redaction_for_storage, redact_text
from .stream_ref import is_opaque_ref


@dataclass
class RecentItem:
    name: str
    url: str
    group: str | None
    player: str
    at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class RecentsManager:
    def __init__(self, path: Path | str | None = None, *, max_items: int = 20) -> None:
        self.path = Path(path) if path is not None else config.RECENTS_JSON
        self.max_items = max(1, max_items)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [e for e in data if isinstance(e, dict)]

    def _write(self, items: list[RecentItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        chmod_private(self.path)

    @staticmethod
    def _safe_url(url: str) -> tuple[str, bool]:
        """URL sin login embebido y flag de si hubo que tocarla (SDD §37)."""
        # Dos casos se dejan intactos: las referencias opacas
        # (xtream:// y xtream-ts://, que no llevan credenciales aunque un
        # nombre de fuente contenga '@'), y el `?token=` de un M3U, sin el
        # cual la lista no se puede reproducir. Todo lo demás que lleve
        # `password=` en la query se redacta antes de tocar disco.
        if is_opaque_ref(url) or not needs_redaction_for_storage(url):
            return url, False
        return redact_text(url), True

    def load(self) -> list[RecentItem]:
        out: list[RecentItem] = []
        migrated = False
        for raw in self._read():
            try:
                url = str(raw["url"])
            except KeyError:
                continue
            url, touched = self._safe_url(url)
            migrated = migrated or touched
            try:
                out.append(
                    RecentItem(
                        name=str(raw["name"]),
                        url=url,
                        group=raw.get("group"),
                        player=str(raw.get("player") or ""),
                        at_utc=str(raw.get("at_utc") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                )
            except KeyError:
                continue
        # Entradas antiguas con `/live/u/p/…` se limpian en disco al leerlas.
        if migrated:
            try:
                self._write(out)
            except OSError:
                pass
        return out

    def push(self, channel: Channel, player: str) -> None:
        url, _ = self._safe_url(channel.url)
        items = [i for i in self.load() if i.url != url]
        items.insert(0, RecentItem(name=channel.name, url=url, group=channel.group, player=player))
        self._write(items[: self.max_items])

    def clear(self) -> None:
        self._write([])
