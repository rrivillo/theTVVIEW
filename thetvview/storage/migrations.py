"""Migraciones de formato persistente (STORE-003).

Los formatos que la app ha escrito hasta ahora **no** llevaban
``schema_version``: se consideran versión 0. La versión 1 añade el sello
``schema_version`` y normaliza los campos que el formato anterior dejaba
sueltos.

Reglas que no se negocian:

- **Idempotente**: migrar dos veces deja el mismo resultado. Un fichero ya en
  v1 no se vuelve a migrar.
- **Por escritura atómica**: la migración escribe con
  :func:`~thetvview.storage.json_store.atomic_write_json`, así que el
  fichero principal es siempre un JSON válido.
- **El original no se borra hasta validar el nuevo**: se escribe y se relee;
  sólo si la versión nueva valida, la migración se da por buena. Un backup
  ``.bak-*`` queda disponible por si acaso.
- **Sin secretos**: los mensajes nunca incluyen el contenido del fichero.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .errors import StorageError
from .json_store import atomic_write_json, make_backup, read_json_with_recovery
from .schemas import SCHEMA_VERSION, ensure_version, get_validator, stamped

__all__ = [
    "MIGRATIONS",
    "migrate_data",
    "migrate_file",
    "needs_migration",
    "MigrationResult",
]


class MigrationResult(dict):
    """Resultado de migrar: los datos nuevos + un informe de lo que pasó."""

    @property
    def changed(self) -> bool:
        return bool(self.get("changed"))

    @property
    def data(self) -> dict[str, Any] | None:
        """Los datos ya migrados (``None`` si no había fichero)."""
        return self.get("data")

    @property
    def from_version(self) -> int:
        return int(self.get("from_version", 0))

    @property
    def to_version(self) -> int:
        return int(self.get("to_version", 0))


def _v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
    """v0 → v1: sella ``schema_version`` y normaliza campos sueltos.

    No reordena ni renombra nada: el formato de los datos ya era válido, sólo
    le faltaba el sello. Los campos desconocidos **se conservan** para no
    perder preferencias de una versión más nueva.
    """
    out = dict(data)
    out["schema_version"] = SCHEMA_VERSION
    return out


#: Versión destino -> migración desde la anterior.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _v0_to_v1,
}


def needs_migration(data: Mapping[str, Any]) -> bool:
    """True si `data` no está ya en la versión actual."""
    return int(data.get("schema_version", 0) or 0) < SCHEMA_VERSION


def migrate_data(
    data: Any, *, nombre: str = "datos"
) -> MigrationResult:
    """Migra `data` en memoria hasta la versión actual.

    Es idempotente: un diccionario ya en la versión actual sale intacto (con
    ``changed=False``).

    Raises:
        StorageError: si los datos no son un objeto, o si una migración
            intermedia falla.
    """
    if not isinstance(data, dict):
        raise StorageError(f"{nombre} debe contener un objeto para poder migrarse.")

    origen = ensure_version(data, nombre=nombre)
    if origen >= SCHEMA_VERSION:
        return MigrationResult(
            {
                "data": dict(data),
                "changed": False,
                "from_version": origen,
                "to_version": origen,
            }
        )

    actual = dict(data)
    version = origen
    while version < SCHEMA_VERSION:
        paso = MIGRATIONS.get(version + 1)
        if paso is None:
            raise StorageError(
                f"no hay migración de la versión {version} a la {version + 1} "
                f"para {nombre}."
            )
        actual = paso(actual)
        version += 1

    actual["schema_version"] = SCHEMA_VERSION
    return MigrationResult(
        {
            "data": actual,
            "changed": True,
            "from_version": origen,
            "to_version": SCHEMA_VERSION,
        }
    )


def migrate_file(path, *, nombre: str | None = None) -> MigrationResult:
    """Migra el fichero `path` a la versión actual, por escritura atómica.

    El flujo es: leer → si no hay migración que hacer, devolver intacto →
    si hay, hacer backup → escribir atómicamente → **releer y validar** la
    versión nueva. Sólo si esa validación pasa la migración se considera
    buena; si falla, se lanza y el fichero original (el backup) sigue ahí.

    Idempotente: llamarla dos veces no cambia el resultado la segunda vez.
    """
    from pathlib import Path

    path = Path(path)
    nombre = nombre or path.stem
    validador = get_validator(nombre)

    data = read_json_with_recovery(path, schema_version=None)
    if data is None:
        # No existe: no hay nada que migrar.
        return MigrationResult(
            {"data": None, "changed": False, "from_version": 0, "to_version": 0}
        )
    if not isinstance(data, dict):
        raise StorageError(f"{nombre} debe contener un objeto para poder migrarse.")

    resultado = migrate_data(data, nombre=nombre)
    if not resultado.changed:
        return resultado

    # Backup del original antes de escribir (el original no se borra hasta
    # que la versión nueva esté escrita y validada).
    make_backup(path)

    atomic_write_json(path, resultado.data, schema_version=None)

    # Releer y validar la versión escrita: si algo salió mal, se dice.
    releido = read_json_with_recovery(path, schema_version=None)
    if not isinstance(releido, dict):
        raise StorageError(f"la migración de {nombre} escribió algo ilegible.")
    if ensure_version(releido, nombre=nombre) != SCHEMA_VERSION:
        raise StorageError(f"la migración de {nombre} no quedó en la versión actual.")
    if validador is not None:
        validador(releido)

    return resultado
