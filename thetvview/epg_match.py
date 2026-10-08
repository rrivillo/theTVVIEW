"""Asociación canal ↔ EPG en tiempo constante (dominio puro, sin curses).

El `Epg` ya viene indexado por `channel_id` (`Epg.programs`), así que buscar
por identidad estable es un acceso al diccionario. Lo que **no** venía indexado
era el nombre: `screens._epg_channel_id` comparaba el nombre del canal contra
los `display-name` de toda la guía en cada llamada, y esa función se llama una
vez por favorito y por reconstrucción de la lista. Con 40 favoritos y una guía
de 20 000 canales son cientos de miles de comparaciones por reconstruction
(§17 del SDD de Favoritos: "evitar búsquedas lineales repetidas").

Aquí la política de asociación queda **en un solo sitio** y en O(1):

1. ``tvg-id`` si ese identificador está en la guía (acceso directo);
2. si no, un índice de nombres normalizados → ``channel_id``;
3. si no hay correspondencia, ``None``. Nunca se inventa una.

La normalización es deliberadamente conservadora: `casefold()` y colapso de
espacios, nada más. El §19 del SDD prohíbe asumir que `CNN` es `CNN
International`, así que aquí no se añade ninguna comparación agresiva: lo que
se quita es el escaneo lineal, no se cambia el criterio.

Puro: no red, no disco, no curses. Testeable sin terminal.
"""

from __future__ import annotations

from .epg_parser import Epg
from .models import Channel

__all__ = ["normalize_key", "EpgIndex", "for_epg", "resolve_channel_id"]


def normalize_key(nombre: str | None) -> str:
    """Clave de comparación de un nombre de canal: minúsculas y sin espacios de más.

    `str.split()` sin argumentos recorta los espacios de los extremos y colapsa
    en uno los de en medio (incluidos los que no son ASCII), así que no hace
    falta una lista de "caracteres de espacio" que mantener al día.

    No hace nada más, y a propósito: sin acentos, sin prefijos, sin sufijos y
    sin similitud difusa. `CNN` no se convierte en `CNN International` porque
    "sí se parecen" — serían canales distintos (§19).
    """
    if not nombre:
        return ""
    return " ".join(nombre.casefold().split())


class EpgIndex:
    """Índice de nombres → `channel_id` para asociación O(1) por canal.

    Sólo se indexan los `channel_id` que **tienen programas**: un canal
    presente en la guía pero sin parrilla no puede devolver nada, así que
    meterlo en el índice sólo serviría para devolver un id inútil después.

    La ordenación de los `channel_id` con el mismo nombre normalizado es la de
    `channels_by_id`, la misma que recorría el escaneo lineal al que sustituye:
    con dos canales homónimos se sigue resolviendo el mismo de siempre.
    """

    __slots__ = ("_con_programas", "_por_nombre")

    def __init__(self, epg: Epg) -> None:
        programs = getattr(epg, "programs", None) or {}
        channels = getattr(epg, "channels_by_id", None) or {}
        con_programas = frozenset(programs)
        por_nombre: dict[str, list[str]] = {}
        for cid, nombre in channels.items():
            if cid not in con_programas:
                continue
            key = normalize_key(nombre)
            if not key:
                continue
            por_nombre.setdefault(key, []).append(cid)
        self._con_programas = con_programas
        self._por_nombre = por_nombre

    def resolve(self, canal: Channel) -> str | None:
        """`channel_id` de la guía para `canal`, o `None` si no hay."""
        tvg_id = getattr(canal, "tvg_id", None)
        if tvg_id and tvg_id in self._con_programas:
            return tvg_id
        want = normalize_key(getattr(canal, "tvg_name", None) or getattr(canal, "name", ""))
        if not want:
            return None
        candidatos = self._por_nombre.get(want)
        return candidatos[0] if candidatos else None

    def __len__(self) -> int:
        """Canales indexados (los que tienen programas **y** nombre)."""
        return len(self._por_nombre)


#: Una sola entrada: (epg, índice). Se sostiene la referencia al `Epg` para que
#: `id()` no pueda reciclarse bajo los pies de otro objeto —el clásico fallo de
#: cachear por identidad en un `dict`— y para no retener una guía que la app ya
#: no tiene en memoria.
_CACHE: tuple[Epg, EpgIndex] | None = None


def for_epg(epg: Epg | None) -> EpgIndex | None:
    """Índice de `epg`, reutilizado si es el mismo objeto.

    Devolver ``None`` para `epg=None` es deliberado: quien llama tiene que
    poder distinguir "no hay guía" de "hay guía sin este canal" sin capturar
    excepciones (es el caso que la pantalla de Favoritos trata distinto).
    """
    global _CACHE
    if epg is None:
        return None
    cached = _CACHE
    if cached is not None and cached[0] is epg:
        return cached[1]
    index = EpgIndex(epg)
    _CACHE = (epg, index)
    return index


def resolve_channel_id(
    epg: Epg | None,
    canal: Channel,
    index: EpgIndex | None = None,
) -> str | None:
    """Único lugar donde se decide qué canal de la guía es este canal.

    `index` es el parámetro de escape para no reconstruir el índice cuando el
    llamante ya lo tiene (una lista de 500 favoritos resolviendo uno a uno).
    """
    if epg is None:
        return None
    if index is None:
        index = for_epg(epg)
    if index is None:  # pragma: no cover - `for_epg` sólo devuelve None sin epg
        return None
    return index.resolve(canal)