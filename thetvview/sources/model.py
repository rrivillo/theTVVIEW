"""Modelo de fuentes alternativas (Fase 5A · P2.3)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from thetvview.channel_identity import ChannelIdentity
from thetvview.models import Channel

__all__ = [
    "ConfianzaCandidata",
    "SourceCandidate",
    "Association",
    "LogicalChannel",
    "EstadoAsociacion",
]


class ConfianzaCandidata:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CandidateConfidence:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class SourceCandidate:
    """Candidata de fuente para un canal lógico."""

    candidate_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    channel: Channel | None = None
    url: str = ""
    source_id: str = ""
    playlist: str = ""
    group: str | None = None
    tvg_id: str | None = None
    confidence: str = "low"  # low | medium | high
    signals: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | accepted | rejected
    provider_name: str = ""
    score: float = 0.0

    @classmethod
    def desde_canal(cls, channel: Channel, *, source_id: str = "", playlist: str = "") -> "SourceCandidate":
        return cls(
            channel=channel,
            url=getattr(channel, "url", "") or "",
            source_id=source_id or getattr(channel, "source_id", "") or "",
            playlist=playlist,
            group=getattr(channel, "group", None),
            tvg_id=getattr(channel, "tvg_id", None),
        )


@dataclass
class Association:
    """Asociación entre un canal lógico y una candidata."""

    association_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    logical_id: str = ""
    candidate_id: str = ""
    source_id: str = ""
    accepted: bool = False
    rejected: bool = False
    reverted: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    reasons: list[str] = field(default_factory=list)

    @property
    def undo_ready(self) -> bool:
        return self.accepted or self.rejected

    def accept(self) -> None:
        self.accepted = True
        self.rejected = False
        self.reverted = False

    def reject(self) -> None:
        self.accepted = False
        self.rejected = True
        self.reverted = False

    def undo(self) -> None:
        self.accepted = False
        self.rejected = False
        self.reverted = True


class EstadoAsociacion:
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNDONE = "undone"


@dataclass
class LogicalChannel:
    """Canal lógico independiente de su fuente técnica."""

    logical_id: str = ""
    identity: ChannelIdentity | None = None
    name: str = ""
    group: str | None = None
    tvg_id: str | None = None
    channels: list[Channel] = field(default_factory=list)
    candidates: list[SourceCandidate] = field(default_factory=list)
    association: Association | None = None
    rejected_candidate_ids: set[str] = field(default_factory=set)
    selected_source_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def from_channel(cls, channel: Channel, source_id: str = "") -> "LogicalChannel":
        ident = None
        try:
            from thetvview.channel_identity import channel_identity

            try:
                ident = channel_identity(channel, source_id=source_id)
            except TypeError:
                ident = channel_identity(channel)
        except Exception:
            ident = None
        lc = cls(
            logical_id=str(ident) if ident is not None else f"{getattr(channel, 'url', '') or getattr(channel, 'name', '')}",
            identity=ident,
            name=getattr(channel, "name", ""),
            group=getattr(channel, "group", None),
            tvg_id=getattr(channel, "tvg_id", None),
            channels=[channel],
            candidates=[SourceCandidate.desde_canal(channel, source_id=source_id)],
        )
        return lc

    def add_candidate(self, candidate: SourceCandidate) -> None:
        if candidate.candidate_id in self.rejected_candidate_ids:
            return
        if candidate.candidate_id not in {c.candidate_id for c in self.candidates}:
            self.candidates.append(candidate)
        if candidate.channel is not None and candidate.channel not in self.channels:
            self.channels.append(candidate.channel)

    def reject_candidate(self, candidate_id: str) -> Association:
        self.rejected_candidate_ids.add(candidate_id)
        assoc = Association(logical_id=self.logical_id, candidate_id=candidate_id)
        assoc.reject()
        self.association = assoc
        for c in self.candidates:
            if c.candidate_id == candidate_id:
                c.status = "rejected"
        return assoc

    def accept_candidate(self, candidate_id: str) -> Association:
        cand = next((c for c in self.candidates if c.candidate_id == candidate_id), None)
        if cand is None:
            raise ValueError("candidata desconocida")
        assoc = Association(logical_id=self.logical_id, candidate_id=candidate_id, source_id=cand.source_id)
        assoc.accept()
        self.association = assoc
        self.selected_source_id = cand.source_id
        cand.status = "accepted"
        return assoc

    def undo_association(self) -> None:
        if self.association is not None:
            self.association.undo()
            self.selected_source_id = None
            for c in self.candidates:
                c.status = "pending"

    @property
    def id(self) -> str:
        return self.logical_id

    @property
    def label(self) -> str:
        return self.name
