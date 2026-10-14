"""Fuentes alternativas y canales lógicos (Fase 5A)."""

from .model import (
    ConfianzaCandidata,
    CandidateConfidence,
    SourceCandidate,
    Association,
    EstadoAsociacion,
    LogicalChannel,
)
from .migration import (
    hacer_backup,
    migrar_favoritos,
    migrar_recientes,
    migrar_identidad_logica,
    migrate,
    migrate_to_logical_channels,
    LogicalMigrationResult,
)
from .catalog import (
    SourceListSpec,
    SourceCatalogEntry,
    MultiSourceCatalog,
)

__all__ = [
    "ConfianzaCandidata",
    "CandidateConfidence",
    "SourceCandidate",
    "Association",
    "EstadoAsociacion",
    "LogicalChannel",
    "hacer_backup",
    "migrar_favoritos",
    "migrar_recientes",
    "migrar_identidad_logica",
    "migrate",
    "migrate_to_logical_channels",
    "LogicalMigrationResult",
    "SourceListSpec",
    "SourceCatalogEntry",
    "MultiSourceCatalog",
]

# English aliases.
LogicalChannelAlias = LogicalChannel
Source = SourceCandidate
Candidate = SourceCandidate
AssociationAlias = Association
LogicalChannelModel = LogicalChannel
SourceCandidateModel = SourceCandidate
AssociationModel = Association
