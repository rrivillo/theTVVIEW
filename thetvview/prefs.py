from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from pathlib import Path

from . import config


@dataclass
class Prefs:
    last_player: str | None = None
    last_group_sort: str = "name"
    theme: str = "light"


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

    def set_last_group_sort(self, value: str) -> None:
        if value not in ("name", "count"):
            return
        p = self.load()
        p.last_group_sort = value
        self.save(p)
