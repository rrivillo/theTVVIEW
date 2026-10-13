"""Colecciones inteligentes (Fase 4)."""

from .manager import ColeccionesManager, CollectionsManager, CollectionManager
from .rules import (
    MAX_DEPTH,
    ReglaInvalida,
    es_compuesta,
    evaluar_coleccion,
    evaluar_condicion,
    evaluar_nodo,
    miembros_de_coleccion,
    normalizar_nodo,
    validar_coleccion,
    validar_regla,
    valor_campo,
)
from .schema import Coleccion, Collection, Regla, ReglaCompuesta, Rule, SCHEMA_VERSION

__all__ = [
    "ColeccionesManager",
    "CollectionsManager",
    "CollectionManager",
    "Coleccion",
    "Collection",
    "Regla",
    "Rule",
    "ReglaCompuesta",
    "SCHEMA_VERSION",
    "MAX_DEPTH",
    "ReglaInvalida",
    "validar_regla",
    "validar_coleccion",
    "evaluar_coleccion",
    "evaluar_nodo",
    "evaluar_condicion",
    "valor_campo",
    "es_compuesta",
    "miembros_de_coleccion",
    "normalizar_nodo",
]

# Alias names for English naming expectations.
Collections = Coleccion
CollectionAlias = Coleccion
RuleAlias = Regla
