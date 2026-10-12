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
2. si no, un índice de nombres normalizados → ``channel_id``. Las claves del
   índice son el ``<display-name>`` **canónico** de cada canal y también sus
   alias —los demás ``<display-name>`` del mismo canal, que es lo que pedía
   FR-EPG-002—; se prueban, en orden, la ``tvg-name`` del canal y su nombre;
3. si no hay correspondencia, ``None``. Nunca se inventa una.

Cada resolución devuelve un :class:`Match` que explica **por qué** casó:
identificador exacto, nombre normalizado o alias (FR-EPG-004), y si la clave
era ambigua. Las claves ambiguas **no se fusionan**: se resuelve el primero de
la lista —la ordenación de siempre, así que nadie cambia de canal por accidente—
y queda registrado que hubo que elegir (FR-EPG-003), tanto en el índice como en
el propio ``Match``.

La normalización es deliberadamente conservadora: `casefold()` y colapso de
espacios, nada más. El §19 del SDD prohíbe asumir que `CNN` es `CNN
International`, así que aquí no se añade ninguna comparación agresiva: lo que
se quita es el escaneo lineal, no se cambia el criterio. Los alias ayudan
porque son nombres que la **guía misma** declara para el mismo canal, no
deducciones nuestras.

Puro: no red, no disco, no curses. Testeable sin terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from .epg_parser import Epg
from .models import Channel

__all__ = [
    "normalize_key",
    "Match",
    "EpgAudit",
    "EpgIndex",
    "auditar",
    "for_epg",
    "resolve_channel_id",
    "METODO_IDENTIFICADOR",
    "METODO_ALIAS",
    "METODO_NOMBRE",
    "METODO_SIN",
]

#: Métodos de correspondencia (FR-EPG-004): van en el ``method`` de un
#: :class:`Match` y son lo que permite explicar por qué casó un canal con un
#: `channel_id` de la guía.
METODO_IDENTIFICADOR = "identificador"
METODO_ALIAS = "alias"
METODO_NOMBRE = "nombre"
METODO_SIN = "sin-correspondencia"


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


@dataclass(frozen=True)
class Match:
    """Resultado de asociar un canal con la guía (FR-EPG-003/004).

    - ``channel_id``: el id de la guía, o `None` si no hay correspondencia.
    - ``method``: por qué casó — :data:`METODO_IDENTIFICADOR` (el ``tvg-id``
      del canal existe en la guía), :data:`METODO_NOMBRE` (un nombre
      normalizado), :data:`METODO_ALIAS` (un ``<display-name>`` alternativo)
      o :data:`METODO_SIN`.
    - ``ambiguo`` / ``candidatos``: si la clave apuntaba a más de un canal de
      la guía, aquí está la lista completa. Se resuelve el primero —el de
      siempre— pero no se fusiona nada: quien recibe el ``Match`` sabe que
      hubo que elegir.
    - ``clave``: la clave normalizada que casó (vacía si no casó nada): es lo
      que identifica el aviso «este nombre coincide con varios canales».
    """

    channel_id: str | None
    method: str
    ambiguo: bool = False
    candidatos: tuple[str, ...] = ()
    clave: str = ""

    @property
    def ok(self) -> bool:
        """True si hay correspondencia."""
        return self.channel_id is not None


#: Correspondencia nula, para no repetir la constante en dos sitios.
SIN_MATCH = Match(channel_id=None, method=METODO_SIN)


class EpgIndex:
    """Índice de nombres → `channel_id` para asociación O(1) por canal.

    Sólo se indexan los `channel_id` que **tienen programas**: un canal
    presente en la guía pero sin parrilla no puede devolver nada, así que
    meterlo en el índice sólo serviría para devolver un id inútil después.

    Cada clave guarda de qué vino cada candidato —nombre canónico o alias—
    porque el método de la correspondencia forma parte del contrato
    (FR-EPG-004). La ordenación de los `channel_id` con la misma clave es la
    de `channels_by_id` (cada canal aporta su canónico y después sus alias),
    la misma que recorría el escaneo lineal al que sustituye: con dos canales
    homónimos se sigue resolviendo el mismo de siempre.

    Al construirse registran las claves ambiguas (FR-EPG-003): si dos canales
    de la guía comparten clave no se fusionan; se resuelve el primero y la
    clave queda en `ambiguos` para poder revisarse.
    """

    __slots__ = ("_con_programas", "_por_nombre", "_ambiguos")

    def __init__(self, epg: Epg) -> None:
        programs = getattr(epg, "programs", None) or {}
        channels = getattr(epg, "channels_by_id", None) or {}
        aliases = getattr(epg, "aliases_by_id", None) or {}
        con_programas = frozenset(programs)
        por_nombre: dict[str, list[tuple[str, str]]] = {}
        for cid, nombre in channels.items():
            if cid not in con_programas:
                continue
            etiquetas = [(nombre, METODO_NOMBRE)]
            etiquetas += [(alias, METODO_ALIAS) for alias in aliases.get(cid, ())]
            for etiqueta, via in etiquetas:
                key = normalize_key(etiqueta)
                if not key:
                    continue
                por_nombre.setdefault(key, []).append((cid, via))
        self._con_programas = con_programas
        self._por_nombre = por_nombre
        # Registro de ambigüedad (FR-EPG-003). Se calcula una vez, al
        # construir: una clave con más de un canal **distinto** (que un mismo
        # canal repita su nombre como alias no es ambigüedad, es redundancia).
        self._ambiguos: dict[str, tuple[str, ...]] = {
            key: tuple(dict.fromkeys(cid for cid, _ in entradas))
            for key, entradas in por_nombre.items()
            if len({cid for cid, _ in entradas}) > 1
        }

    def resolve_match(self, canal: Channel) -> Match:
        """Asocia `canal` con la guía y explica por qué lo hizo así.

        El orden de las claves —`tvg-name` y después nombre— es una mejora
        sobre el `tvg_name or name` anterior: si la `tvg-name` declarada por
        la lista está desactualizada y no existe en la guía, el nombre del
        canal sigue siendo una clave **exacta** suya y merece probarse. En
        cuanto una clave tiene candidatos se para: el resto sólo se mira para
        medir la ambigüedad de *esa* clave, no de otras.
        """
        tvg_id = getattr(canal, "tvg_id", None)
        if tvg_id and tvg_id in self._con_programas:
            return Match(tvg_id, METODO_IDENTIFICADOR)
        for clave in self._claves_de(canal):
            entradas = self._por_nombre.get(clave)
            if not entradas:
                continue
            cids = tuple(dict.fromkeys(cid for cid, _ in entradas))
            return Match(
                cids[0],
                entradas[0][1],
                ambiguo=len(cids) > 1,
                candidatos=cids,
                clave=clave,
            )
        return SIN_MATCH

    def resolve(self, canal: Channel) -> str | None:
        """`channel_id` de la guía para `canal`, o `None` si no hay.

        La firma vieja, para quien sólo necesita el id y nada más
        (`favorite_rows`, las pantallas).
        """
        return self.resolve_match(canal).channel_id

    @staticmethod
    def _claves_de(canal: Channel) -> list[str]:
        """Claves candidatas, en orden: la `tvg-name` del canal y su nombre.

        Sólo claves **exactas** del propio canal (normalizadas). La
        `tvg-name` es el nombre que la lista declara para el canal y el
        nombre es el que ve el usuario; si la primera no está en la guía se
        prueban la segunda y ya.
        """
        claves: list[str] = []
        for valor in (getattr(canal, "tvg_name", None), getattr(canal, "name", None)):
            clave = normalize_key(valor)
            if clave and clave not in claves:
                claves.append(clave)
        return claves

    @property
    def ambiguos(self) -> Mapping[str, tuple[str, ...]]:
        """Claves con más de un canal de la guía: el registro de ambigüedad.

        Sólo lectura por fuera; los valores son tuplas, no listas.
        """
        return MappingProxyType(self._ambiguos)

    def __len__(self) -> int:
        """Claves indexadas (canales con programas **y** nombre, alias incluidos)."""
        return len(self._por_nombre)


@dataclass(frozen=True)
class EpgAudit:
    """Retrato de la correspondencia guía ↔ canales dados (FR-EPG-003).

    - ``ambiguos``: las claves ambiguas que **algún** canal usó de verdad,
      con sus candidatos. Una ambigüedad que no usó nadie no afecta a nadie.
    - ``sin_epg``: etiquetas de los canales que no encontraron guía. Es lo
      que conviene revisar en la lista: o no están en el XMLTV o su nombre
      cambió.
    - ``epg_sin_canal``: `channel_id` de la guía (con programas) al que no
      apunta ninguno de los canales dados: la guía trae parrilla para canales
      que esta lista no tiene, que también es información.
    """

    ambiguos: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sin_epg: tuple[str, ...] = ()
    epg_sin_canal: tuple[str, ...] = ()


def auditar(epg: Epg | None, canales: Iterable[Channel]) -> EpgAudit:
    """Registra ambigüedades, canales sin EPG y entradas de guía sin canal.

    La parte «detectar y registrar» de FR-EPG-003, en memoria y en tiempo
    lineal en el número de canales dados (cada resolución es O(1)). Con
    `epg=None` no hay nada que auditar y se devuelve el retrato vacío, nunca
    un error: «todavía no hay guía» es un estado normal, no un fallo.
    """
    if epg is None:
        return EpgAudit()
    indice = for_epg(epg)
    if indice is None:  # pragma: no cover - `for_epg` sólo devuelve None sin epg
        return EpgAudit()
    usados: set[str] = set()
    sin_epg: list[str] = []
    ambiguos: dict[str, tuple[str, ...]] = {}
    for canal in canales:
        match = indice.resolve_match(canal)
        if match.channel_id is None:
            sin_epg.append(getattr(canal, "name", "") or getattr(canal, "url", ""))
            continue
        usados.add(match.channel_id)
        if match.ambiguo:
            ambiguos.setdefault(match.clave, match.candidatos)
    # `_con_programas` es privado pero estamos en el mismo módulo: no se
    # expone un alias público para un solo uso interno.
    return EpgAudit(
        ambiguos=ambiguos,
        sin_epg=tuple(sin_epg),
        epg_sin_canal=tuple(sorted(indice._con_programas - usados)),
    )


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
