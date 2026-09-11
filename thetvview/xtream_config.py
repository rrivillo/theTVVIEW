"""Configuración de una cuenta Xtream (dataclass, solo stdlib)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class XtreamConfig:
    """Credenciales de acceso a un proveedor Xtream.

    - server_url: URL base del servidor (normalizada al guardar).
    - username: identificador de usuario.
    - password: en runtime va aquí; PlaylistManager la persiste en
      playlists.json solo para listas Xtream (nunca para M3U).
    """

    server_url: str
    username: str
    password: str = ""
