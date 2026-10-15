"""Fuentes alternativas y canales lógicos (Fase 5A)."""

from .detection import *  # noqa: F401,F403
from .failover import *  # noqa: F401,F403
from .preferences import *  # noqa: F401,F403
from .associations import (
    AsociacionesError,
    Asociacion,
    AssociationStore,
    SourcesStore,
    MAX_RECHAZOS_POR_CANAL,
    clave_logica,
    clave_candidata,
)
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
    "AsociacionesError",
    "Asociacion",
    "AssociationStore",
    "SourcesStore",
    "MAX_RECHAZOS_POR_CANAL",
    "clave_logica",
    "clave_candidata",
]

# English aliases.
LogicalChannelAlias = LogicalChannel
Source = SourceCandidate
Candidate = SourceCandidate
AssociationAlias = Association
LogicalChannelModel = LogicalChannel
SourceCandidateModel = SourceCandidate
AssociationModel = Association


# Ensure all public names from submodules are available.
try:
    from .detection import __all__ as _a1
except Exception:
    _a1 = []
try:
    from .failover import __all__ as _a2
except Exception:
    _a2 = []
try:
    from .preferences import __all__ as _a3
except Exception:
    _a3 = []
__all__ = list(dict.fromkeys(list(globals().get("__all__", [])) + _a1 + _a2 + _a3))
