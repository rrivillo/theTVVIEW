from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import Channel


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

    def load(self) -> list[RecentItem]:
        out: list[RecentItem] = []
        for raw in self._read():
            try:
                out.append(
                    RecentItem(
                        name=str(raw["name"]),
                        url=str(raw["url"]),
                        group=raw.get("group"),
                        player=str(raw.get("player") or ""),
                        at_utc=str(raw.get("at_utc") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                )
            except KeyError:
                continue
        return out

    def push(self, channel: Channel, player: str) -> None:
        items = [i for i in self.load() if i.url != channel.url]
        items.insert(0, RecentItem(name=channel.name, url=channel.url, group=channel.group, player=player))
        self._write(items[: self.max_items])

    def clear(self) -> None:
        self._write([])
