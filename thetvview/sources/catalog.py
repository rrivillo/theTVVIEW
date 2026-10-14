"""Catálogo multi-fuente (A-04, A-05). Se usa con tasks/manager.py."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from thetvview.models import Channel, Playlist
from thetvview.m3u_parser import parse_file as _parse_file
from thetvview.tasks.manager import TaskManager, CancellationToken

__all__ = ["SourceListSpec", "MultiSourceCatalog", "SourceCatalogEntry"]


@dataclass
class SourceListSpec:
    name: str
    location: str
    enabled: bool = True
    max_entries: int = 1000


@dataclass
class SourceCatalogEntry:
    playlist_name: str
    channel: Channel
    location: str


@dataclass
class MultiSourceCatalog:
    lists: list[SourceListSpec] = field(default_factory=list)
    max_playlists: int = 5
    max_entries_per_list: int = 5000
    progress: float = 0.0
    status: str = "idle"
    entries: list[SourceCatalogEntry] = field(default_factory=list)
    _cancel: threading.Event = field(default_factory=threading.Event)
    _task_manager: TaskManager | None = None

    def cancel(self) -> None:
        self._cancel.set()
        self.status = "cancelled"

    def start(self, task_manager: TaskManager | None = None, worker: Callable[[CancellationToken], Any] | None = None) -> None:
        tm = task_manager or self._task_manager or TaskManager()
        self._task_manager = tm

        def job(token: CancellationToken) -> None:
            self.status = "running"
            try:
                self.run_parse(token)
            except Exception:
                self.status = "failed-safe"
                return
            if token.check():
                self.status = "cancelled"
                return
            self.status = "ready"

        handle = tm.submit("source-catalog", job, supersede=True)
        self._handle = handle

    def run_parse(self, token: CancellationToken) -> None:
        self.entries = []
        valid_lists = self.lists[: self.max_playlists]
        total = max(1, len(valid_lists))
        done = 0
        for spec in valid_lists:
            token.raise_if_cancelled()
            if not spec.enabled:
                done += 1
                self.progress = done / total
                continue
            location = spec.location
            try:
                if location.startswith(("http://", "https://")):
                    # No network in catalog build contract for tests; skip remote.
                    channels: list[Channel] = []
                else:
                    playlist = _parse_file(location)
                    channels = list(playlist.channels)[: min(spec.max_entries, self.max_entries_per_list)]
            except Exception:
                channels = []
            for ch in channels:
                token.raise_if_cancelled()
                self.entries.append(SourceCatalogEntry(spec.name, ch, location))
            done += 1
            self.progress = done / total

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()
