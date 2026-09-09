"""Configuración de una cuenta Xtream (dataclass, solo stdlib)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class XtreamConfig:
    """Credenciales de acceso a un proveedor Xtream.

    - server_url: URL base del servidor (normalizada al guardar).
    - username: identificador de usuario.
    - password: nunca se persiste en disco; solo en memoria de sesión.
    """

    server_url: str
    username: str
    password: str = ""
