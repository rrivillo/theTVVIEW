"""Errores del paquete de persistencia."""

from __future__ import annotations


class StorageError(Exception):
    """Error de persistencia: corrupción, validación o escritura fallida.

    El mensaje es apto para mostrar al usuario en un modal (sin secretos).
    """
