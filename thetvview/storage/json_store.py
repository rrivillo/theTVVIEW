"""Escritura atómica JSON común (STORE-001), solo stdlib.

Todos los escritores de JSON de la app pasan por aquí. Garantiza:

1. **Validar** el objeto antes de persistirlo.
2. **Serializar** completamente el contenido antes de reemplazar el archivo.
3. Crear un **temporal único** en el mismo sistema de ficheros que el destino.
4. Escribir el contenido completo.
5. ``flush`` + ``fsync`` del fichero.
6. Cerrar el temporal.
7. **Reemplazar** el destino mediante ``os.replace`` (atómico en POSIX y Windows).
8. ``fsync`` del directorio en POSIX (Windows: no aplicable, se documenta).
9. Gestionar y eliminar **temporales abandonados** sin borrar el último estado válido.

Diferencias por plataforma:
- **Linux/macOS**: ``fsync`` del fichero y del directorio.
- **Windows**: ``os.replace`` es atómico; ``fsync`` del directorio no aplica.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .errors import StorageError

__all__ = [
    "atomic_write_json",
    "read_json_with_recovery",
    "cleanup_orphans",
    "corrupt_path",
    "make_backup",
    "file_lock",
    "DEFAULT_SCHEMA_VERSION",
]

#: Versión de esquema por defecto para los formatos persistentes.
DEFAULT_SCHEMA_VERSION: int = 1

#: Tope de bytes al leer un JSON persistente, por defecto (32 MB).
#:
#: No es arbitrario: un `prefs.json` con preferencias de pistas ocupa unos
#: pocos KiB, y un `favorites.json` con 50 000 canales unas 12 MB. Un tope de
#: 10 MB (el primer valor) rechazaba un historial de favoritos legítimo, y por
#: eso ahora el tope **depende del formato** (:data:`MAX_BYTES_POR_FORMATO`):
#: el mismo límite para todos sería o demasiado justo para los historiales o
#: demasiado flojo para la configuración.
MAX_JSON_BYTES: int = 32 * 1024 * 1024

#: Tope por formato. Los archivos que guardan preferencias —que son los que
#: un usuario puede editar a mano y los que se corrompen con más facilidad—
#: llevan un tope mucho más bajo: si un `prefs.json` pasa de 2 MB, algo va
#: mal y leerlo entero no va a arreglarlo.
MAX_BYTES_POR_FORMATO: dict[str, int] = {
    "prefs": 2 * 1024 * 1024,
    "theme": 64 * 1024,
    "playlists": 8 * 1024 * 1024,
    "favorites": 128 * 1024 * 1024,
    "recents": 8 * 1024 * 1024,
}


def max_bytes_para(nombre: str | None, por_defecto: int = MAX_JSON_BYTES) -> int:
    """Tope de bytes para el formato `nombre`, o `por_defecto` si no lo tiene."""
    return MAX_BYTES_POR_FORMATO.get(str(nombre or "").strip().lower(), por_defecto)


#: Tope de entradas en una lista JSON. Por encima de 500 000 entradas el
#: modelo en memoria es el problema, no el disco.
MAX_JSON_ENTRIES: int = 500_000

#: Número máximo de archivos ``.corrupt-*`` que se conservan por fichero.
MAX_CORRUPT_COPIES: int = 3

#: Número máximo de backups (``.bak-*``) que se conservan por fichero.
MAX_BACKUPS: int = 1


#: Un candado por fichero, para los pares leer-modificar-escribir.
#:
#: ``prefs.set_last_player()`` es leer → cambiar un campo → escribir. Sin
#: candado, dos hilos (o el hilo principal y un worker) pueden leer el mismo
#: estado, cambiar campos distintos y escribir: el segundo `os.replace` se
#: lleva por delante el cambio del primero, y ese ajuste desaparece sin que
#: nadie lo note.
#:
#: El candado es **por proceso**: dos procesos distintos (la app y, por
#: ejemplo, un script del usuario) no se coordinan. Es la misma frontera que
#: documenta STORE-006, y no se intenta disimular con un lock de fichero: un
#: fichero de bloqueo entre procesos exige ``fcntl`` en POSIX y el trabajo con
#: msvcrt en Windows, y para el uso real de la app el candado en memoria es
#: suficiente y no añade un fichero que limpiar.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def file_lock(path: Path | str) -> threading.RLock:
    """Candado reentrante asociado a `path` (el mismo objeto para el mismo fichero)."""
    clave = str(Path(path).resolve())
    with _LOCKS_GUARD:
        candado = _LOCKS.get(clave)
        if candado is None:
            candado = threading.RLock()
            _LOCKS[clave] = candado
        return candado


def _fsync_dir(path: Path) -> None:
    """Hace ``fsync`` del directorio que contiene `path` (solo POSIX).

    En Windows no se hace nada: ``os.replace`` ya es atómico y no hay
    ``fsync`` de directorio portátil.
    """
    if os.name != "posix":
        return
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _unique_tmp(path: Path) -> Path:
    """Ruta de temporal única en el mismo directorio que `path`.

    Formato: ``.<nombre>.<pid>.<timestamp_ns>.tmp``. El pid evita colisiones
    entre procesos; el timestamp nanosegundo evita colisiones rápidas.
    """
    stem = path.name
    pid = os.getpid()
    ns = time.time_ns()
    return path.parent / f".{stem}.{pid}.{ns}.tmp"


def cleanup_orphans(path: Path, *, max_age_seconds: float = 3600) -> int:
    """Elimina temporales huérfanos de `path` que tengan más de `max_age_seconds`.

    Los temporales huérfanos son ficheros ``.<nombre>.*.tmp`` que quedaron
    atrás por un fallo (process kill, excepción). Se eliminan solo si tienen
    más de 1 hora: un proceso que está escribiendo ahora no se toca.

    Returns:
        Número de ficheros eliminados.
    """
    path = Path(path)
    prefix = f".{path.name}."
    suffix = ".tmp"
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        for entry in path.parent.iterdir():
            if not entry.name.startswith(prefix) or not entry.name.endswith(suffix):
                continue
            try:
                stat = entry.stat()
                if stat.st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def corrupt_path(path: Path) -> Path:
    """Ruta para guardar una copia corrupta: ``.<nombre>.corrupt-<timestamp>``."""
    path = Path(path)
    ts = time.strftime("%Y%m%d%H%M%S")
    return path.parent / f".{path.name}.corrupt-{ts}"


def atomic_write_json(
    path: Path | str,
    data: Any,
    *,
    schema_version: int | None = DEFAULT_SCHEMA_VERSION,
    validate: Callable[[Any], None] | None = None,
    indent: int | None = 2,
) -> None:
    """Escribe `data` como JSON de forma atómica (STORE-001).

    El fichero principal **siempre** es el estado antiguo válido o el nuevo
    válido, nunca JSON parcial.

    Args:
        path: destino del fichero JSON.
        data: objeto serializable a JSON.
        schema_version: si no es None, se añade ``schema_version`` al JSON.
        validate: callable opcional que valida `data`` antes de escribir.
            Debe lanzar :class:`StorageError` si los datos no son válidos.
        indent: indentación del JSON (``None`` para compacto).

    Raises:
        StorageError: si falla la validación o la escritura.
    """
    path = Path(path)
    if validate is not None:
        try:
            validate(data)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"validación falló: {exc}") from exc

    # 1-2. Serializar completamente antes de tocar el fichero
    try:
        if schema_version is not None:
            if not isinstance(data, dict):
                raise StorageError("schema_version solo es válido para objetos dict")
            payload = dict(data)
            payload["schema_version"] = int(schema_version)
        else:
            payload = data
        text = json.dumps(payload, ensure_ascii=False, indent=indent)
    except (TypeError, ValueError) as exc:
        raise StorageError(f"no se pudo serializar a JSON: {exc}") from exc

    # 3. Mismo directorio que el destino
    path.parent.mkdir(parents=True, exist_ok=True)

    # 4-6. Temporal único + flush + fsync
    tmp = _unique_tmp(path)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # 7. Reemplazo atómico
        os.replace(tmp, path)
        # 8. fsync del directorio (solo POSIX)
        _fsync_dir(path)
    except OSError as exc:
        # Limpiar el temporal si quedó
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise StorageError(f"no se pudo escribir {path}: {exc}") from exc


def read_json_with_recovery(
    path: Path | str,
    *,
    schema_version: int | None = None,
    validate: Callable[[Any], None] | None = None,
    max_bytes: int | None = None,
) -> Any:
    """Lee un JSON con recuperación (STORE-004).

    Si el fichero está corrupto:
    1. Conserva una copia como ``.<nombre>.corrupt-<timestamp>`` (con permisos
       privados).
    2. Intenta restaurar desde el backup más reciente (``.bak-*``).
    3. Si no hay backup válido, lanza :class:`StorageError` con un mensaje
       recuperable.

    Si el fichero no existe, devuelve ``None``.

    Args:
        path: ruta del fichero JSON.
        schema_version: si no es None, valida que el JSON tenga esa versión.
        validate: callable opcional que valida los datos leídos.
        max_bytes: tope de bytes del fichero. Si es None, se deduce del nombre
            del formato (:func:`max_bytes_para`), de modo que un historial
            grande tiene más margen que una configuración.

    Returns:
        Los datos JSON, o ``None`` si el fichero no existe.

    Raises:
        StorageError: si el fichero está corrupto y no hay backup válido.
    """
    path = Path(path)
    if max_bytes is None:
        max_bytes = max_bytes_para(path.stem)
    if not path.exists():
        return None

    # Tamaño del fichero
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise StorageError(f"no se pudo leer {path}: {exc}") from exc
    if size > max_bytes:
        raise StorageError(
            f"el fichero {path} es demasiado grande ({size} bytes, "
            f"máximo {max_bytes}). Puede estar corrupto."
        )

    # Leer y parsear
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Corrupto: conservar copia y restaurar desde backup
        backup = _restore_from_backup(path)
        if backup is not None:
            return backup
        _preserve_corrupt(path, raw)
        raise StorageError(
            f"el fichero {path.name} está corrupto y no hay copia de "
            "recuperación válida. Se ha conservado una copia para diagnóstico."
        ) from exc

    # Validar schema_version
    if schema_version is not None:
        if not isinstance(data, dict):
            raise StorageError(
                f"el fichero {path.name} no contiene un objeto JSON"
            )
        version = data.get("schema_version")
        if version is None:
            raise StorageError(
                f"el fichero {path.name} no tiene schema_version"
            )
        if int(version) != schema_version:
            raise StorageError(
                f"el fichero {path.name} tiene schema_version={version}, "
                f"se esperaba {schema_version}"
            )

    # Validación semántica
    if validate is not None:
        try:
            validate(data)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"validación falló: {exc}") from exc

    # Validar límites de entradas
    if isinstance(data, list) and len(data) > MAX_JSON_ENTRIES:
        raise StorageError(
            f"el fichero {path.name} tiene demasiadas entradas "
            f"({len(data)}, máximo {MAX_JSON_ENTRIES})"
        )

    return data


def _restore_from_backup(path: Path) -> Any | None:
    """Intenta restaurar el backup más reciente de `path`.

    Los backups son ficheros ``.<nombre>.bak-<timestamp>``. Se intenta el más
    reciente primero.
    """
    prefix = f".{path.name}.bak-"
    candidates: list[tuple[float, Path]] = []
    try:
        for entry in path.parent.iterdir():
            if entry.name.startswith(prefix) and entry.is_file():
                try:
                    mtime = entry.stat().st_mtime
                    candidates.append((mtime, entry))
                except OSError:
                    continue
    except OSError:
        pass
    candidates.sort(reverse=True)  # más reciente primero
    for _, backup_path in candidates:
        try:
            data = json.loads(backup_path.read_bytes().decode("utf-8"))
            # Restaurar: escribir de vuelta al path original
            atomic_write_json(path, data)
            return data
        except Exception:
            continue
    return None


def _preserve_corrupt(path: Path, raw: bytes) -> None:
    """Guarda una copia del fichero corrupto con permisos privados."""
    dest = corrupt_path(path)
    try:
        dest.write_bytes(raw)
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        # Limitar el número de copias corruptas
        _limit_corrupt_copies(path)
    except OSError:
        pass


def _limit_corrupt_copies(path: Path) -> None:
    """Elimina copias corruptas antiguas, conservando las más recientes."""
    prefix = f".{path.name}.corrupt-"
    entries: list[tuple[float, Path]] = []
    try:
        for entry in path.parent.iterdir():
            if entry.name.startswith(prefix) and entry.is_file():
                try:
                    entries.append((entry.stat().st_mtime, entry))
                except OSError:
                    continue
    except OSError:
        return
    if len(entries) <= MAX_CORRUPT_COPIES:
        return
    entries.sort()
    for _, old in entries[: -MAX_CORRUPT_COPIES]:
        try:
            old.unlink()
        except OSError:
            pass


def make_backup(path: Path | str) -> Path | None:
    """Crea un backup del fichero `path` como ``.<nombre>.bak-<timestamp>``.

    Se conserva solo el backup más reciente (MAX_BACKUPS=1).

    Returns:
        La ruta del backup creado, o None si no existía el fichero.
    """
    path = Path(path)
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d%H%M%S")
    backup = path.parent / f".{path.name}.bak-{ts}"
    try:
        backup.write_bytes(path.read_bytes())
        try:
            os.chmod(backup, 0o600)
        except OSError:
            pass
        # Limitar backups
        _limit_backups(path)
        return backup
    except OSError:
        return None


def _limit_backups(path: Path) -> None:
    """Elimina backups antiguos, conservando el más reciente."""
    prefix = f".{path.name}.bak-"
    entries: list[tuple[float, Path]] = []
    try:
        for entry in path.parent.iterdir():
            if entry.name.startswith(prefix) and entry.is_file():
                try:
                    entries.append((entry.stat().st_mtime, entry))
                except OSError:
                    continue
    except OSError:
        return
    if len(entries) <= MAX_BACKUPS:
        return
    entries.sort()
    for _, old in entries[: -MAX_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
