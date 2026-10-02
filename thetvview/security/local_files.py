"""Permisos restrictivos y lectura acotada de ficheros locales (B11/B12).

Solo stdlib. En POSIX se aplica `chmod`; en Windows el bit no existe y la
función degrada en silencio (devuelve False) sin romper la aplicación.

Además ofrece :func:`read_limited_text`, la única puerta de entrada para
leer playlists/EPG del disco:

- exige un fichero regular (no un fifo, directorio ni dispositivo);
- rechaza symlinks salvo ``allow_symlinks=True`` (y lo hace con
  ``O_NOFOLLOW`` en los SO que lo soportan);
- acota el tamaño **leído** y, si el contenido es gzip, también el
  **descomprimido** (una bomba gzip de 1 KB no puede expandirse a 10 GB);
- decodifica ``utf-8-sig`` con ``errors="replace"``.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
import zlib
from pathlib import Path

from .limits import get_limits, human_size

#: Solo el usuario puede leer/escribir el fichero (0600).
PRIVATE_FILE_MODE = 0o600
#: Solo el usuario puede entrar en el directorio (0700).
PRIVATE_DIR_MODE = 0o700

#: Tamaño de los trozos al leer/descomprimir.
_CHUNK: int = 64 * 1024
#: Identificación mágica de gzip.
_GZIP_MAGIC: bytes = b"\x1f\x8b"

# Errnos con los que O_NOFOLLOW rechaza un symlink (ELOOP en POSIX).
_SYMLINK_ERRNOS = frozenset(
    code for code in (getattr(errno, "ELOOP", None), getattr(errno, "EMLINK", None))
    if isinstance(code, int)
)

__all__ = [
    "PRIVATE_FILE_MODE",
    "PRIVATE_DIR_MODE",
    "chmod_private",
    "chmod_private_tree",
    "gunzip_limited",
    "read_limited_bytes",
    "read_limited_text",
]


def chmod_private(path: str | Path, *, directory: bool = False) -> bool:
    """Fija permisos privados a un fichero (o directorio).

    Devuelve True si se aplicaron, False si el SO/FS no lo permite
    (Windows, FAT, sistemas sin soporte de permisos).
    """
    if os.name == "nt":
        return False
    try:
        os.chmod(path, PRIVATE_DIR_MODE if directory else PRIVATE_FILE_MODE)
        return True
    except (OSError, ValueError, NotImplementedError):
        return False


def chmod_private_tree(path: str | Path) -> bool:
    """Aplica permisos privados a un directorio y a todos sus ficheros."""
    root = Path(path)
    if not root.exists():
        return False
    ok = chmod_private(root, directory=True)
    try:
        for child in root.iterdir():
            if child.is_file():
                ok = chmod_private(child) or ok
    except OSError:
        pass
    return ok


# ---------------------------------------------------------------------------
# Lectura acotada (gap B11)
# ---------------------------------------------------------------------------


def _limit_or_default(max_bytes: int | None) -> int:
    if max_bytes is None:
        return get_limits().max_file_bytes
    return int(max_bytes)


def _err_too_big(path: Path, max_bytes: int) -> OSError:
    return OSError(
        f"El fichero '{path}' supera el límite de {human_size(int(max_bytes))}; "
        "no se lee entero."
    )


def _err_too_big_what(what: str, max_bytes: int) -> OSError:
    return OSError(
        f"{what} supera el límite de {human_size(int(max_bytes))}; "
        "no se lee entero."
    )


def _err_symlink(path: Path) -> OSError:
    return OSError(
        f"'{path}' es un enlace simbólico y no se lee por seguridad."
    )


def _open_read_only(path: Path, allow_symlinks: bool) -> int:
    """Abre `path` en lectura, bloqueando symlinks si no se permiten."""
    if not allow_symlinks and path.is_symlink():
        raise _err_symlink(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if not allow_symlinks:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        # O_NOFOLLOW convierte el symlink en ELOOP: mismo mensaje amigable.
        if not allow_symlinks and getattr(exc, "errno", None) in _SYMLINK_ERRNOS:
            raise _err_symlink(path) from exc
        raise OSError(f"No se pudo abrir '{path}': {exc.strerror or exc}.") from exc


def _read_capped(path: Path, max_bytes: int, *, allow_symlinks: bool) -> bytes:
    """Lee como mucho `max_bytes` bytes; nunca sigue leyendo por encima."""
    fd = _open_read_only(path, allow_symlinks)
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise OSError(f"'{path}' no es un fichero normal; no se lee.")
        if st.st_size > max_bytes:
            raise _err_too_big(path, max_bytes)
        out = bytearray()
        while True:
            piece = os.read(fd, min(_CHUNK, max_bytes + 1 - len(out)))
            if not piece:
                break
            out.extend(piece)
            if len(out) > max_bytes:
                raise _err_too_big(path, max_bytes)
        return bytes(out)
    except OSError:
        raise
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _gunzip_limited(data: bytes, max_bytes: int, what: str) -> bytes:
    """Descomprime gzip sin poder superar `max_bytes` (bomba gzip).

    ``what`` es el sujeto del mensaje (p. ej. ``"El fichero '/x'"``).
    """
    dco = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    pos = 0
    try:
        while pos < len(data):
            piece = data[pos:pos + _CHUNK]
            pos += _CHUNK
            room = max_bytes + 1 - len(out)
            if room <= 0:
                raise _err_too_big_what(what, max_bytes)
            # `max_length` acota la salida de golpe: 1 KB comprimido no
            # puede materializarse en GB antes de que lo detectemos.
            out += dco.decompress(piece, room)
            if len(out) > max_bytes:
                raise _err_too_big_what(what, max_bytes)
        # flush() no valida el fin de stream: `eof` sí. Un gzip cortado a
        # la mitad devuelve datos parciales en silencio si no comprobamos.
        if not dco.eof:
            raise OSError(f"{what} (gzip) está dañado o incompleto.")
        out += dco.flush()
    except zlib.error as exc:
        raise OSError(f"{what} (gzip) está dañado o incompleto.") from exc
    if len(out) > max_bytes:
        raise _err_too_big_what(what, max_bytes)
    return bytes(out)


def _gunzip_capped(data: bytes, max_bytes: int, path: Path) -> bytes:
    """Descomprime el contenido de ``path`` con el tope indicado."""
    return _gunzip_limited(data, max_bytes, f"El fichero '{path}'")


def gunzip_limited(
    data: bytes,
    *,
    max_bytes: int | None = None,
    what: str = "El contenido",
) -> bytes:
    """Descomprime gzip acotando el **resultado** (bomba gzip).

    Contraparte de :func:`_gunzip_capped` para el camino de **descarga**,
    donde no hay una ruta de fichero: ``what`` describe el origen en el
    mensaje de error (p. ej. ``"La playlist descargada"``).

    Args:
        data: bytes ya leídos (comprimidos o no).
        max_bytes: tope del resultado (``limits.max_file_bytes`` por defecto).
        what: sujeto del mensaje de error.

    Raises:
        OSError: si el resultado supera el límite o el gzip está dañado.
    """
    return _gunzip_limited(data, _limit_or_default(max_bytes), what)


def read_limited_bytes(
    path: str | Path,
    *,
    max_bytes: int | None = None,
    allow_symlinks: bool = False,
) -> bytes:
    """Lee un fichero como bytes sin pasar nunca de `max_bytes`.

    Args:
        path: fichero a leer.
        max_bytes: tope (por defecto ``limits.max_file_bytes``).
        allow_symlinks: si True se permiten enlaces simbólicos.

    Raises:
        OSError: no existe, no es regular, es un symlink prohibido, supera
            el límite o no se puede abrir. El mensaje es apto para el
            usuario e incluye la ruta.
    """
    limit = _limit_or_default(max_bytes)
    p = Path(path)
    return _read_capped(p, limit, allow_symlinks=allow_symlinks)


def read_limited_text(
    path: str | Path,
    *,
    max_bytes: int | None = None,
    allow_symlinks: bool = False,
    encoding: str = "utf-8-sig",
) -> str:
    """Lee un fichero de texto acotado; gzip se descomprime con el mismo tope.

    El límite se aplica tanto al fichero comprimido como al texto final,
    así que un gzip de 2 KB no puede convertirse en un XMLTV de 10 GB.

    Args:
        path: fichero a leer (.gz se descomprime).
        max_bytes: tope de bytes comprimidos **y** descomprimidos
            (por defecto ``limits.max_file_bytes``).
        allow_symlinks: si True se permiten enlaces simbólicos.
        encoding: codificación de destino (``utf-8-sig`` por defecto).

    Raises:
        OSError: igual que :func:`read_limited_bytes`, o si el gzip está
            dañado/incompleto.
    """
    limit = _limit_or_default(max_bytes)
    p = Path(path)
    raw = _read_capped(p, limit, allow_symlinks=allow_symlinks)
    if p.suffix.lower() == ".gz" or raw[:2] == _GZIP_MAGIC:
        raw = _gunzip_capped(raw, limit, p)
    return raw.decode(encoding, errors="replace")
