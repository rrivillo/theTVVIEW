"""Esquema versionado de colecciones inteligentes (C-01)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

TIPOS_COLECCION = ("manual", "smart", "system")


@dataclass
class Regla:
    """Condición hoja: field + operator + value."""

    field: str
    operator: str
    value: Any = None


@dataclass
class ReglaCompuesta:
    """Nodo all/any/not."""

    op: str  # "all" | "any" | "not"
    rules: list[Any] = field(default_factory=list)


@dataclass
class Coleccion:
    """Colección versionada. Guarda reglas o referencias, never copias."""

    id: str
    name: str
    type: str = "manual"
    rules: dict[str, Any] | None = None
    members: list[str] = field(default_factory=list)
    sort: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


# Aliases
Collection = Coleccion
Rule = Regla
ComposedRule = ReglaCompuesta

__all__ = [
    "SCHEMA_VERSION",
    "TIPOS_COLECCION",
    "Coleccion",
    "Collection",
    "Regla",
    "Rule",
    "ReglaCompuesta",
    "ComposedRule",
]
