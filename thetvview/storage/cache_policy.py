"""Política única de cachés (RES-005, STORE-005).

Antes de este módulo cada caché tenía su propio TTL y ninguna tenía tope de
tamaño, así que un portátil con la ``data/`` llena de descargas viejas se
quedaba sin disco sin que nadie lo dijera. Aquí hay **una** política, con
reglas que se pueden auditar de un vistazo:

- **Tamaño máximo** por caché y **global**: al superar el tope se borra lo más
  antiguo primero (por ``mtime``), nunca lo más reciente.
- **Expiración** por caché, con los mismos TTL que usaba cada módulo para no
  cambiar el comportamiento de la app.
- **La limpieza no toca persistencia**: sólo entra en los directorios de
  caché que se declaran aquí. ``prefs.json``, ``playlists.json``,
  ``favorites.json`` y los backups no están en la lista y, por tanto, no se
  pueden borrar por error (STORE-005).
- **Es segura con ficheros en uso**: se ignora lo que no se puede borrar sin
   error, y nunca se lanza desde el arranque: una limpieza fallida no puede
  impedir abrir la app.

La app llama a :func:`limpiar_todo` al arrancar (ver ``config.ensure_dirs``).
Es idempotente y barata: sólo mira ``mtime`` y tamaños, sin abrir ficheros.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CachePolicy",
    "POLICIES",
    "MAX_TOTAL_BYTES",
    "limpiar_cache",
    "limpiar_todo",
    "espacio_usado",
]


@dataclass(frozen=True)
class CachePolicy:
    """Reglas de una caché concreta.

    Attributes:
        nombre: identificador legible, sólo para diagnóstico.
        directorio: dónde vive. **Nunca** un directorio de persistencia.
        ttl: segundos que vive una entrada sin tocarse.
        max_bytes: tope de tamaño de esta caché.
    """

    nombre: str
    directorio: Path
    ttl: float
    max_bytes: int


#: Tope conjunto de todas las cachés. Si la suma de sus topes individuales
#: supera este, se recorta proporcionalmente al borrar.
MAX_TOTAL_BYTES: int = 512 * 1024 * 1024  # 512 MB


def _policies() -> dict[str, CachePolicy]:
    """Construye las políticas desde :mod:`thetvview.config`.

    Se construye **suelo** (no al importar el módulo) para que los tests
    puedan cambiar ``config.DATA_DIR`` y tener las políticas al día.
    """
    from .. import config

    return {
        "playlist": CachePolicy(
            "playlist_cache",
            config.PLAYLIST_CACHE_DIR,
            ttl=6 * 3600.0,  # el mismo DEFAULT_TTL_HOURS de m3u_parser
            max_bytes=64 * 1024 * 1024,
        ),
        "epg": CachePolicy(
            "epg_cache",
            config.EPG_CACHE_DIR,
            ttl=12 * 3600.0,  # el mismo DEFAULT_TTL_HOURS de epg_parser
            max_bytes=256 * 1024 * 1024,
        ),
        "xtream": CachePolicy(
            "xtream_cache",
            config.XTREAM_CACHE_DIR,
            ttl=24 * 3600.0,  # el mismo CATEGORY_TTL de xtream_provider
            max_bytes=128 * 1024 * 1024,
        ),
        "track": CachePolicy(
            "track_cache",
            config.TRACK_CACHE_DIR,
            ttl=15 * 60.0,  # el mismo DISK_TTL de streams.probe
            max_bytes=32 * 1024 * 1024,
        ),
    }


#: Acceso perezoso a las políticas (se reconstruyen si cambia la config).
POLICIES = _policies


def _entries(directorio: Path) -> list[tuple[float, int, Path]]:
    """(mtime, tamaño, ruta) de los ficheros de una caché, de más viejo a más nuevo.

    Se ignoran los ficheros en uso y los que no son regulares: un directorio
    de caché no debería tenerlos, pero si aparece uno, borrarlo podría ser
    otra cosa y no compete a esta limpieza.
    """
    out: list[tuple[float, int, Path]] = []
    try:
        entradas = list(directorio.iterdir())
    except OSError:
        return out
    for ruta in entradas:
        try:
            stat = ruta.stat()
        except OSError:
            continue
        if not ruta.is_file() or ruta.is_symlink():
            continue
        out.append((stat.st_mtime, stat.st_size, ruta))
    out.sort(key=lambda item: item[0])
    return out


def espacio_usado(directorio: Path) -> int:
    """Bytes ocupados por una caché (0 si no existe)."""
    return sum(size for _mtime, size, _ruta in _entries(Path(directorio)))


def limpiar_cache(
    policy: CachePolicy,
    *,
    ahora: float | None = None,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Aplica la política a una caché: borra caducados y recorta por tamaño.

    Primero se va por lo caducado (más barato y más seguro), y sólo si aún así
    se pasa del tope se borra lo más antiguo que quede.

    Args:
        policy: reglas a aplicar.
        ahora: instante actual (parametrizable para tests deterministas).
        dry_run: si True, dice qué borraría sin borrar nada.

    Returns:
        ``(ficheros_considerados, bytes_borrados)``.
    """
    ahora = time.time() if ahora is None else float(ahora)
    entradas = _entries(policy.directorio)
    if not entradas:
        return 0, 0

    borrados = 0
    bytes_borrados = 0

    def _borrar(ruta: Path, tamano: int) -> None:
        nonlocal borrados, bytes_borrados
        if not dry_run:
            try:
                ruta.unlink()
            except OSError:
                return  # en uso o permiso: no insistir, no es un error
        borrados += 1
        bytes_borrados += tamano

    # (1) Caducados por TTL.
    supervivientes: list[tuple[float, int, Path]] = []
    for mtime, tamano, ruta in entradas:
        if ahora - mtime > policy.ttl:
            _borrar(ruta, tamano)
        else:
            supervivientes.append((mtime, tamano, ruta))

    # (2) Recorte por tamaño, del más antiguo al más reciente.
    total = sum(size for _m, size, _r in supervivientes)
    if total > policy.max_bytes:
        # `supervivientes` ya viene ordenado de más antiguo a más nuevo.
        for _mtime, tamano, ruta in supervivientes:
            if total <= policy.max_bytes:
                break
            _borrar(ruta, tamano)
            total -= tamano

    return len(entradas), bytes_borrados


def limpiar_todo(*, ahora: float | None = None, dry_run: bool = False) -> dict[str, int]:
    """Aplica la política a todas las cachés y recorta el total global.

    Returns:
        Un dict ``nombre_caché -> bytes borrados``, sólo con las que cambiaron.
    """
    resultado: dict[str, int] = {}
    politicas = _policies()
    for nombre, policy in politicas.items():
        try:
            _considerados, borrados = limpiar_cache(policy, ahora=ahora, dry_run=dry_run)
        except Exception:
            # Una caché con permiso raro no puede impedir la limpieza de las
            # demás ni la apertura de la app.
            continue
        if borrados:
            resultado[nombre] = borrados

    if not dry_run:
        _recortar_total(politicas, ahora=ahora)
    return resultado


def _recortar_total(
    politicas: dict[str, CachePolicy], *, ahora: float
) -> None:
    """Si la suma supera :data:`MAX_TOTAL_BYTES`, borra proporcionalmente.

    La idea es que el usuario no se quede sin disco por tener cuatro cachés
    «razonables» por separado. Se empieza por la que más ocupa, que es la que
    más aporta al recorte.
    """
    def _total() -> dict[str, int]:
        return {n: espacio_usado(p.directorio) for n, p in politicas.items()}

    usados = _total()
    suma = sum(usados.values())
    if suma <= MAX_TOTAL_BYTES:
        return

    for nombre in sorted(usados, key=lambda n: usados[n], reverse=True):
        if suma <= MAX_TOTAL_BYTES:
            break
        policy = politicas[nombre]
        if usados[nombre] <= 0:
            continue
        # Se recorta esta caché hasta la mitad de su parte: un solo paso,
        # y se vuelve a medir. Evita borrar de más si el total ya bajó.
        entradas = _entries(policy.directorio)
        objetivo = usados[nombre] // 2
        borrado_hasta = 0
        for _mtime, tamano, ruta in entradas:  # de más antiguo a más nuevo
            if borrado_hasta >= objetivo:
                break
            try:
                ruta.unlink()
            except OSError:
                continue
            borrado_hasta += tamano
        suma -= borrado_hasta
