"""Persistencia JSON segura: escritura atómica, esquemas, migraciones y recovery.

Este paquete es el único punto por el que los escritores de JSON deben pasar.
Garantiza:

- **Escritura atómica** (STORE-001): validar → serializar → temporal único →
  flush + fsync → ``os.replace`` → fsync del directorio (POSIX).
- **Validación y versionado** (STORE-002): ``schema_version`` en cada formato
  persistente.
- **Migraciones** (STORE-003): v0→v1 idempotente, por escritura atómica.
- **Recuperación** (STORE-004): backup + preservación de archivos corruptos.
- **Límites** (STORE-007): tope de bytes y de entradas al leer.
"""

from __future__ import annotations

__all__: list[str] = []
