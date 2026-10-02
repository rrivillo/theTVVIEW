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
    - allow_private_network: excepción anti-SSRF de ESTA cuenta (SDD §11).
      Los paneles autoalojados en 192.168.x.x o en localhost son
      frecuentes; la excepción es por fuente, nunca global, y se
      declara de forma explícita en la UI.
    """

    server_url: str
    username: str
    password: str = ""
    allow_private_network: bool = False
