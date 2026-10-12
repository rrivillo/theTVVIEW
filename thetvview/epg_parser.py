"""Parser XMLTV (EPG) con soporte .gz, solo stdlib.

Contrato v1.1:
- parse_file acepta .xml/.xmltv y variantes .gz (se descomprimen con gzip).
- start/stop con offset tz se convierten a datetime consciente; sin offset,
  se toma como UTC **pero marcado** (FR-EPG-006): el programa lleva
  `tz_assumed=True`, `validar()` lo cuenta y la UI avisa una vez por fuente
  con `aviso_zona_pendiente`. La política no es «asumir en silencio»: es
  «asumir sin mentir» — se sigue haciendo para no cambiar el comportamiento
  de la app de un día para otro, pero el usuario se entera.
- programme sin stop: se rellena con el start del siguiente programa del
  mismo canal; el último queda con stop=None. Un stop imposible (anterior o
  igual al start) se descarta y entra en ese mismo relleno (FR-EPG-007).
- Canales indexados por id con su primer display-name (el canónico), el resto
  de display-name en aliases_by_id como alias de búsqueda (FR-EPG-002), y el
  primer icon (<icon src>).
- Programas con sub-title y categorías (en orden del feed).
- now_playing trata el último programa (sin stop) como abierto; next_programme
  da el fallback "a continuación" en consulta.
- Malformed: elementos inválidos se saltan sin romper el parseo.
- split_sources/resolve_source normalizan las fuentes EPG que declara una
  playlist (http, file://, path absoluto o relativo) en cualquier SO.

TODO(epg_parser) — diferidos deliberadamente:
- credits, episode-num, ratings y otros campos menores de XMLTV.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from bisect import bisect_right
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import Program
from .security.errors import IPTVError
from .security.limits import get_limits
from .security.local_files import gunzip_limited, read_limited_text
from .security.redaction import redact_text
from .security.safe_http import SafeHttpClient
from .security.url_policy import PURPOSE_METADATA
from .security.xml_safe import parse_xml

_FMT = "%Y%m%d%H%M%S %z"
_FMT_NO_TZ = "%Y%m%d%H%M%S"


#: Fuentes cuya guía ya se leyó y a las que ya se avisó de que sus horas
#: venía sin zona. Un aviso por fuente y por proceso (EPG-001): el aviso sirve
#: para que el usuario sepa que las horas pueden estar desplazadas, no para
#: repetirlo en cada refresco, que es como se deja de leer un aviso.
_AVISOS_TZ: set[str] = set()


def aviso_zona_pendiente(fuente: str = "") -> str:
    """Texto del aviso de zona horaria asumida, o "" si ya se avisó.

    Devolver el texto (y no abrir el modal aquí) es lo que mantiene este módulo
    **puro**: el aviso lo presenta la UI, que es quien sabe cómo hacerlo.
    """
    clave = str(fuente or "").strip() or "(sin fuente)"
    if clave in _AVISOS_TZ:
        return ""
    _AVISOS_TZ.add(clave)
    return (
        "Esta guía viene sin zona horaria, así que sus horas se están "
        "tomando como si fueran UTC. Si el proveedor usa la hora local, las "
        "horas se verán desplazadas."
    )


def reinicia_avisos_tz() -> None:
    """Olvida los avisos ya mostrados. Para tests y recargas completas."""
    _AVISOS_TZ.clear()


def _parse_ts(raw: str | None) -> tuple[datetime | None, bool]:
    """'YYYYMMDDHHMMSS ±ZZZZ' → datetime aware. Indica si hubo que asumir UTC.

    Política temporal (§10.2 del SDD de P1, FR-EPG-005/006)
    ------------------------------------------------------

    XMLTV admite dos formas y **no** son la misma cosa:

    1. ``20260825080000 +0200`` — el feed declara el desfase. Se respeta tal
       cual y se normaliza a UTC para comparar.
    2. ``20260825080000`` — el feed **no dice** nada.

    Antes, el caso 2 se convertía a UTC en silencio, lo que es afirmar que el
    proveedor publica en UTC sin ningún dato que lo sostenga. Ahora se sigue
    haciendo —para no cambiar el comportamiento de toda la app de un día para
    otro— pero **se marca**: el segundo valor de retorno dice que la zona se
    asumió y el :class:`~thetvview.models.Program` lo lleva en ``tz_assumed``.

    Aquí **no** se registra ningún aviso: hacerlo consumiría la única
    oportunidad de :func:`aviso_zona_pendiente` justo antes de que la UI pueda
    enseñarlo, y el usuario nunca vería el aviso. El registro lo hace la UI
    cuando lo muestra, una vez por fuente.

    Devolver un par en lugar de un `datetime` es lo que hace posible esa marca
    sin tener que volver a recorrer el XML.
    """
    if not raw:
        return None, False
    raw = raw.strip()
    try:
        if " " in raw:
            momento = datetime.strptime(raw, _FMT)
            return momento.astimezone(timezone.utc), False
        momento = datetime.strptime(raw[:14], _FMT_NO_TZ)
    except ValueError:
        return None, False
    return momento.replace(tzinfo=timezone.utc), True


def _normaliza_para_indice(momento: datetime) -> datetime:
    """Un instante comparable: siempre en UTC y siempre consciente.

    Dos motivos, y los dos importan:

    - un `Program` de un feed **con** offsets lleva su propia zona, y otro de un
      feed **sin** offsets llega como UTC. Compararlos con `<` lanzaría
      `TypeError`, así que todo pasa por aquí antes de compararse;
    - un reloj **naive** —el que llegue de la UI o de un test— se toma como UTC,
      que es exactamente la política de :func:`_parse_ts`. Tratarlo como
      «hora local» sería inventar una zona que nadie declaró.

    Normalizar en la frontera y no en el bucle es lo que mantiene el índice
    comparáble sin comprobaciones por comparación.
    """
    if momento.tzinfo is None:
        return momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc)


#: Sentinel de «más infinito» para el prefijo de paradas: un programa sin
#: `stop` sigue abierto, luego cubre cualquier instante posterior a su arranque.
_PARADA_ABIERTA = datetime.max.replace(tzinfo=timezone.utc)


def _stops_max_index(programs: dict[str, list[Program]]) -> dict[str, list[datetime]]:
    """Prefijo de paradas máximas por canal: `max[i]` = mayor `stop` de [0..i].

    Es lo que permite saber en O(1) si **algún** programa anterior al de la
    posición `i` sigue cubriendo un instante, sin recorrer el prefijo. Sin
    esta estructura, comprobar sólo el programa inmediatamente anterior —como
    hacía el primer intento— se equivoca en parrillas anidadas: un programa
    largo con dos cortos dentro hace que la bisección devuelva el corto
    equivocado **sin avisar**, que es justo el fallo silencioso que el plan
    marca como el más dañino (§5, «Equivalencia del índice EPG»).

    Un `stop is None` es un infinito: quien lo lleva nunca «termina antes».
    """
    indice: dict[str, list[datetime]] = {}
    for cid, programas in programs.items():
        maximos: list[datetime] = []
        actual: datetime | None = None
        for prog in programas:
            fin = (
                _PARADA_ABIERTA
                if prog.stop is None
                else _normaliza_para_indice(prog.stop)
            )
            actual = fin if actual is None else max(actual, fin)
            maximos.append(actual)
        indice[cid] = maximos
    return indice


def build_index(programs: dict[str, list[Program]]) -> dict[str, list[datetime]]:
    """Índice `channel_id` → inicios ordenados, para buscar por bisección.

    Es la estructura que convierte `now_playing` y `next_programme` de O(n) en
    O(log n) **por consulta** (FR-EPG-008/012). Se construye **una vez** en la
    carga, no en cada consulta: construirlo por consulta sería peor que el
    escaneo que sustituye.

    Los inicios van **ordenados**, porque `bisect_right` sólo sirve sobre una
    secuencia ordenada.

    Cuando la lista de programas **no** está ordenada por inicio, la bisección
    daría la posición equivocada, así que ese canal queda marcado en
    :attr:`Epg.unordered_channels` y sus consultas usan el escaneo lineal.
    :func:`parse_text` siempre ordena, así que en la práctica ese conjunto está
    vacío; el caso existe porque `Epg` es un dataclass público y alguien puede
    construirlo a mano. Lo detecta :meth:`Epg.__post_init__`, que es quien
    decide: esta función **no** guarda estado.
    """
    indice: dict[str, list[datetime]] = {}
    for cid, programas in programs.items():
        inicios = [_normaliza_para_indice(p.start) for p in programas]
        inicios.sort()
        indice[cid] = inicios
    return indice


@dataclass
class Epg:
    """EPG indexado por channel_id.

    Lleva dos índices: el de programas por canal (el de siempre) y el de
    inicios ordenados, que es lo que hace que buscar el programa actual sea
    una bisección en vez de un recorrido.
    """

    channels_by_id: dict[str, str]  # id -> display-name
    programs: dict[str, list[Program]]
    icons_by_id: dict[str, str] = field(default_factory=dict)  # id -> icon src
    #: id → etiquetas alternativas del mismo canal: los `<display-name>` que
    #: van **después** del primero (idomas, abreviaturas, HD/SD). Son los alias
    #: del fallback de FR-EPG-002; sólo los canales que traen más de uno
    #: aparecen aquí. El primer display-name es el canónico y vive en
    #: `channels_by_id`, que es la etiqueta que se **muestra**.
    aliases_by_id: dict[str, list[str]] = field(default_factory=dict)
    #: `channel_id` → inicios normalizados y ordenados. Ver :func:`build_index`.
    starts_by_channel: dict[str, list[datetime]] = field(default_factory=dict)
    #: `channel_id` → parada máxima acumulada por posición. Ver
    #: :func:`_stops_max_index`: es lo que hace que detectar un solape en el
    #: camino rápido sea O(1) y no un recorrido del prefijo.
    stops_max_by_channel: dict[str, list[datetime]] = field(default_factory=dict)
    #: Canales cuya lista de programas **no** está ordenada por inicio. Para
    #: ellos la búsqueda lineal sigue siendo la correcta: `parse_text` ordena
    #: siempre, así que en la práctica está vacío.
    unordered_channels: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.starts_by_channel:
            desordenados: set[str] = set()
            for cid, programas in self.programs.items():
                inicios = [_normaliza_para_indice(p.start) for p in programas]
                if inicios != sorted(inicios):
                    desordenados.add(cid)
                inicios.sort()
                self.starts_by_channel[cid] = inicios
            self.unordered_channels |= frozenset(desordenados)
        if not self.stops_max_by_channel:
            self.stops_max_by_channel = _stops_max_index(self.programs)

    def channel_name(self, channel_id: str) -> str | None:
        return self.channels_by_id.get(channel_id)

    def channel_icon(self, channel_id: str) -> str | None:
        """URL del primer <icon src> declarado para el canal, si hay."""
        return self.icons_by_id.get(channel_id)

    def programmes_for(self, channel_id: str) -> list[Program]:
        return self.programs.get(channel_id, [])

    def now_playing(self, channel_id: str, when: datetime) -> Program | None:
        """Programa en emisión en `when`, o None.

        Búsqueda por bisección: se localiza el último programa que **ya**
        empezó y, si el prefijo de paradas máximas dice que ninguno de los
        anteriores lo tapa, se comprueba sólo ese. El escaneo lineal haría lo
        mismo pero recorriendo la parrilla entera en cada consulta, y esa
        consulta se hace una vez por favorito y por reconstrucción de la
        lista (FR-EPG-008).
        """
        programas = self.programs.get(channel_id)
        if not programas:
            return None
        instante = _normaliza_para_indice(when)
        if channel_id in self.unordered_channels:
            for prog in programas:
                if _normaliza_para_indice(prog.start) <= instante and (
                    prog.stop is None
                    or instante < _normaliza_para_indice(prog.stop)
                ):
                    return prog
            return None
        inicios = self.starts_by_channel.get(channel_id)
        if inicios is None:  # Epg construido a mano sin pasar por __post_init__
            inicios = build_index({channel_id: programas})[channel_id]
        posicion = bisect_right(inicios, instante) - 1
        if posicion < 0:
            return None  # todavía no empezaba nada de este canal
        maximos = self.stops_max_by_channel.get(channel_id)
        if maximos is None:
            maximos = _stops_max_index({channel_id: programas})[channel_id]
        if posicion == 0 or maximos[posicion - 1] <= instante:
            # Camino rápido: ninguno de los anteriores seguía cubriendo el
            # instante, así que no hay solape y el último que empezó es **el**.
            candidato = programas[posicion]
            if candidato.stop is None or instante < _normaliza_para_indice(candidato.stop):
                return candidato
            return None
        # Camino de la **equivalencia exacta**: hay solapes, y el escaneo lineal
        # devolvía el primer programa de la lista que cumplía el intervalo, que
        # en una lista ordenada por inicio es el de arranque más temprano. Se
        # recorre el prefijo entero para devolver exactamente el mismo.
        #
        # Ocurre sólo con parrillas solapadas —un feed roto—; en una guía
        # correcta el camino de arriba siempre gana y la consulta es O(log n).
        for prog in programas[: posicion + 1]:
            if prog.stop is None or instante < _normaliza_para_indice(prog.stop):
                return prog
        return None

    def next_programme(self, channel_id: str, when: datetime) -> Program | None:
        """Primer programa que empieza estrictamente después de `when`.

        Fallback "a continuación" en consulta: también sirve para acotar un
        programa cuyo stop es None (el último de la parrilla).
        """
        programas = self.programs.get(channel_id)
        if not programas:
            return None
        instante = _normaliza_para_indice(when)
        if channel_id in self.unordered_channels:
            candidato: Program | None = None
            for prog in programas:
                inicio = _normaliza_para_indice(prog.start)
                if inicio <= instante:
                    continue
                if candidato is None or inicio < _normaliza_para_indice(candidato.start):
                    candidato = prog
            return candidato
        inicios = self.starts_by_channel.get(channel_id)
        if inicios is None:
            inicios = build_index({channel_id: programas})[channel_id]
        posicion = bisect_right(inicios, instante)
        if posicion >= len(inicios):
            return None
        # Lista ordenada por inicio: en esta posición vive el primer programa
        # que aún no ha empezado. Los inicios van normalizados, así que se
        # compara el normalizado del programa, no el crudo.
        objetivo = inicios[posicion]
        for prog in programas:
            if _normaliza_para_indice(prog.start) == objetivo:
                return prog
        return None  # pragma: no cover - el índice y la lista no pueden divergir

    @property
    def total_programas(self) -> int:
        """Cuántos programas hay en total. Para la validación de integridad."""
        return sum(len(v) for v in self.programs.values())

    def validar(self, *, min_canales: int = 1, min_programas: int = 1):
        """¿Esta guía sirve para sustituir a la activa? (FR-EPG-009/010)

        Devuelve una :class:`~thetvview.epg_parser.EpgValidation` con el
        veredicto y el motivo. **No** cambia nada: decidir es de quien llama.

        Dos rechazos y sólo dos, porque son los que hacen que una pantalla de
        Favoritos pase de mostrar programas a no mostrar nada:

        - **0 canales** o **0 programas**: una guía vacía no es una guía;
        - **muchos menos que la anterior** (por debajo de ``min_ratio``): una
          descarga a medio servir.

        Y una condición que **no** es un rechazo pero sí se informa: que alguna
        fuente trajera horas sin zona. La app las sigue usando —cambiar eso de
        golpe sería peor—, pero el usuario tiene que saberlo (FR-EPG-006).
        """
        canales = len(self.channels_by_id)
        programas = self.total_programas
        if canales < min_canales or programas < min_programas:
            return EpgValidation(
                ok=False,
                motivo=(
                    f"La guía descargada viene vacía ({canales} canales, "
                    f"{programas} programas). Se conserva la guía que ya tenías."
                ),
                canales=canales,
                programas=programas,
                vacia=True,
            )
        con_zona_asumida = sum(
            1
            for lista in self.programs.values()
            for prog in lista
            if prog.tz_assumed
        )
        avisos: list[str] = []
        if con_zona_asumida:
            avisos.append(
                "Una parte de la guía viene sin zona horaria y se está tomando "
                "como UTC; si el proveedor usa otra zona, esas horas se verán "
                "desplazadas."
            )
        return EpgValidation(
            ok=True,
            canales=canales,
            programas=programas,
            con_zona_asumida=con_zona_asumida,
            avisos=avisos,
        )


@dataclass
class EpgValidation:
    """Veredicto de una guía recién descargada, antes de sustituir a la activa."""

    ok: bool = True
    motivo: str = ""
    canales: int = 0
    programas: int = 0
    #: True si la guía no trae nada utilizable.
    vacia: bool = False
    #: Programas cuyo instante se tomó como UTC sin que el feed lo dijera.
    con_zona_asumida: int = 0
    #: Avisos que la UI debe enseñar aunque la guía sea válida.
    avisos: list[str] = field(default_factory=list)

    def para_modal(self) -> str:
        if not self.ok:
            return self.motivo
        base = f"La guía trae {self.programas} programas de {self.canales} canales."
        return base + ("\n\n" + "\n".join(self.avisos) if self.avisos else "")

    @staticmethod
    def contra_creciente(
        nueva: "EpgValidation",
        anterior_canales: int,
        anterior_programas: int = 0,
        min_ratio: float = 0.3,
    ) -> "EpgValidation":
        """Segunda vuelta de la validación: contra la guía que ya está activa.

        Una guía «no vacía» puede estar igual de rota: 30 programas de 20 000
        canales es una descarga cortada que **sí** pasa el primer filtro. Por eso
        se compara con la anterior, que es la que hay que proteger.

        Se comparan **las dos magnitudes**, canales y programas, y basta con que
        una caiga por debajo del umbral para rechazar. Mirar sólo los canales no
        detecta el caso real: un proveedor que sigue mandando los 20 000 canales
        pero con la parrilla vacía para la mitad.
        """
        if not nueva.ok or (anterior_canales <= 0 and anterior_programas <= 0):
            return nueva
        falta_canales = (
            anterior_canales > 0 and nueva.canales < anterior_canales * min_ratio
        )
        falta_programas = (
            anterior_programas > 0
            and nueva.programas < anterior_programas * min_ratio
        )
        if not falta_canales and not falta_programas:
            return nueva
        detalle = []
        if falta_canales:
            detalle.append(
                f"{nueva.canales} canales de los {anterior_canales} que había"
            )
        if falta_programas:
            detalle.append(
                f"{nueva.programas} programas de los {anterior_programas} que había"
            )
        return EpgValidation(
            ok=False,
            motivo=(
                f"La guía nueva trae {' y '.join(detalle)}: parece incompleta. "
                "Se conserva la guía que ya tenías; si de verdad quieres "
                "cambiarla, dilo."
            ),
            canales=nueva.canales,
            programas=nueva.programas,
            avisos=nueva.avisos,
        )


def _text_of(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    text = (el.text or "").strip()
    return text or None


def parse_text(text: str) -> Epg:
    """Parsea contenido XMLTV desde memoria.

    Usa :func:`thetvview.security.xml_safe.parse_xml`, que rechaza
    ``<!DOCTYPE>``/``<!ENTITY>`` y acota profundidad y número de nodos
    (gap B6). Un XML mal formado sigue lanzando ``ValueError`` con el
    mensaje de siempre.

    De cada canal se guardan **todos** los ``<display-name>``: el primero es
    el canónico (la etiqueta que se muestra) y el resto van a
    ``Epg.aliases_by_id`` como alias de búsqueda (FR-EPG-002). El tratamiento
    de intervalos —stops ausentes, imposibles o de duración cero— queda
    documentado en el bloque de relleno de más abajo (FR-EPG-007).
    """
    try:
        root = parse_xml(text)
    except ET.ParseError as exc:
        raise ValueError(f"XMLTV inválido: {exc}") from exc

    channels: dict[str, str] = {}
    icons: dict[str, str] = {}
    aliasines: dict[str, list[str]] = {}
    for ch in root.iter("channel"):
        cid = ch.get("id")
        if not cid:
            continue
        nombres = [n for n in (_text_of(el) for el in ch.findall("display-name")) if n]
        if cid not in channels and nombres:
            channels[cid] = nombres.pop(0)  # el primero es el canónico
        if nombres:
            # El resto son alias (FR-EPG-002): se guardan sin repetir y sin
            # repetir el canónico, porque un feed que repite `<channel id>`
            # con el mismo texto no está aportando nada. Si al final no queda
            # ninguno, el canal no entra en el dict.
            actuales = aliasines.get(cid, [])
            for nombre in nombres:
                if nombre != channels.get(cid) and nombre not in actuales:
                    actuales.append(nombre)
            if actuales:
                aliasines[cid] = actuales
        if cid not in icons:
            icon_el = ch.find("icon")
            src = icon_el.get("src", "").strip() if icon_el is not None else ""
            if src:
                icons[cid] = src

    by_channel: dict[str, list[Program]] = {}
    for node in root.iter("programme"):
        cid = node.get("channel") or ""
        start, asumido = _parse_ts(node.get("start"))
        if not cid or start is None:
            continue  # sin canal o sin fecha válida: entrada inservible
        title = _text_of(node.find("title")) or "(sin título)"
        categories = [
            cat for cat in (_text_of(el) for el in node.findall("category")) if cat
        ]
        stop, _ = _parse_ts(node.get("stop"))
        by_channel.setdefault(cid, []).append(
            Program(
                channel_id=cid,
                title=title,
                start=start,
                stop=stop,
                desc=_text_of(node.find("desc")),
                sub_title=_text_of(node.find("sub-title")),
                categories=categories,
                tz_assumed=asumido,
            )
        )

    # Tratamiento de intervalos (FR-EPG-007), en un solo sitio:
    #
    # - `stop` ausente → se acota con el arranque del siguiente programa (de
    #   siempre; es lo que hace que un feed sin stops no deje huecos);
    # - `stop` **anterior o igual** al arranque → intervalo imposible, feed
    #   corrupto. Se descarta el stop —el programa queda «abierto»— y el mismo
    #   relleno de abajo le asigna el arranque del siguiente. Así la parrilla
    #   sigue contigua y en pantalla nunca aparece un fin anterior al inicio.
    #   Si es el último del canal, queda abierto: la política de «último sin
    #   stop» que ya existía.
    # - duración cero **con** siguiente → se acota igual que un stop ausente.
    #
    # Los solapes no se tocan: dos programas pueden convivir (§10.2) y quien
    # decide cuál está en el aire es la consulta, que devuelve el primero de
    # la lista —el de arranque más temprano—, igual que el escaneo lineal.
    for progs in by_channel.values():
        progs.sort(key=lambda p: p.start)
        for prog in progs:
            if prog.stop is not None and prog.stop <= prog.start:
                prog.stop = None
        for prev, nxt in zip(progs, progs[1:]):
            if prev.stop is None:
                prev.stop = nxt.start

    return Epg(
        channels_by_id=channels,
        programs=by_channel,
        icons_by_id=icons,
        aliases_by_id=aliasines,
    )


def parse_file(path: str | Path) -> Epg:
    """Lee un archivo XMLTV local (.xml/.xmltv, opcionalmente .gz).

    La lectura pasa por :func:`thetvview.security.local_files.read_limited_text`:
    fichero regular, sin symlinks y con límite de tamaño tanto comprimido
    como descomprimido (gap B11). Los fallos salen como ``OSError`` con
    mensaje ya amable (incluye la ruta).
    """
    return parse_text(read_limited_text(Path(path)))


# --- Referencias EPG declaradas en la cabecera de una playlist -------------

# Atributos de #EXTM3U que traen el EPG asociado (alias reales en el campo).
SOURCE_ATTRS: frozenset[str] = frozenset(
    {"x-tvg-url", "url-tvg", "tvg-url", "x-url-tvg", "x-epg-url", "epg-url"}
)

# Separadores admitidos entre varias URLs EPG dentro del mismo atributo.
_SPLIT_RE = re.compile(r"[\s|;]+")

# Cadenas que pueden ser una fuente EPG (para no trocear URLs con comas).
_PATH_ENDINGS = (".xml", ".xmltv", ".gz", ".xz", ".bz2", ".zip", ".json")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
# /C:/ruta (file:///C:/ruta ya pasado por url2pathname en un SO POSIX).
_FILE_WIN_ABS_RE = re.compile(r"^/[A-Za-z]:[\\/]")


def _looks_like_source(part: str) -> bool:
    """True si `part` tiene pinta de path/URL de EPG (no un fragmento suelto)."""
    low = part.lower()
    if low.startswith(("http://", "https://", "file://", "//")):
        return True
    if _WINDOWS_ABS_RE.match(part):
        return True
    path = low.split("?", 1)[0].split("#", 1)[0]
    return path.endswith(_PATH_ENDINGS) or "/" in part or "\\" in part


def split_sources(value: str) -> list[str]:
    """Trocea el valor de un atributo EPG en varias fuentes.

    Admite separación por espacio, `|`, `;` y `,`. Una coma solo trocea si
    todas las piezas siguen pareciendo fuentes: así no se rompen URLs con
    comas dentro de la query (p. ej. `guia.xml?ids=a,b`).
    """
    refs: list[str] = []
    for chunk in (c for c in _SPLIT_RE.split((value or "").strip()) if c):
        chunk = chunk.strip(",")
        pieces = [p for p in chunk.split(",") if p]
        if len(pieces) > 1 and all(_looks_like_source(p) for p in pieces):
            refs.extend(pieces)
        elif pieces:
            refs.append(chunk)
    return refs


def resolve_source(source: str | None, *, base: str | None = None) -> str | None:
    """Normaliza una referencia EPG a algo que `parse_file`/`load_url` entiende.

    Multiplataforma (sin dependencias del SO en uso):
    - `http(s)://`  → se devuelve tal cual (descarga con cache TTL).
    - `file:///C:/guia.xml` → path local del SO (percent-decoding + UNC).
    - Absoluta POSIX (`/opt/guia.xml`), Windows (`C:\\guia.xml`) o UNC
      (`\\\\srv\\guia.xml`) → tal cual, aunque el SO actual sea otro.
    - Relativa (`guia.xml.gz`) → se resuelve contra `base`:
      directorio del fichero de la playlist, o `urljoin` si la playlist
      es remota. Las barras invertidas se normalizan a `/` para que una
      lista creada en Windows abra también en Linux/macOS.

    Devuelve None si no hay referencia que usar.
    """
    ref = (source or "").strip().strip("\"'").strip()
    if not ref:
        return None
    low = ref.lower()
    if low.startswith(("http://", "https://")):
        return ref
    if low.startswith("file://"):
        parsed = urllib.parse.urlparse(ref)
        path = urllib.request.url2pathname(parsed.path)
        # file:///C:/guia.xml en un SO POSIX deja la barra inicial de más.
        if _FILE_WIN_ABS_RE.match(path):
            path = path[1:]
        host = parsed.netloc
        if host and host.lower() != "localhost":
            # file://servidor/compartido/x.xml → UNC //servidor/compartido/x.xml
            path = f"//{host}{path}"
        return path or None
    if ref.startswith(("/", "\\\\", "//")) or _WINDOWS_ABS_RE.match(ref):
        return ref  # absoluta (otro SO o la propia): no se toca
    if not base:
        return ref
    base = base.strip()
    if base.lower().startswith(("http://", "https://")):
        return urllib.parse.urljoin(base, ref.replace("\\", "/"))
    # `\` → `/` (aceptado por Windows) para que una lista creada en un SO
    # con separadores distintos resuelva igual en el actual.
    joined = Path(base).expanduser().parent / ref.replace("\\", "/")
    try:
        return os.path.normpath(str(joined))
    except (OSError, ValueError):
        return str(joined)


# --- Descarga por URL con cache TTL ---------------------------------------

DEFAULT_TIMEOUT: float = 15.0
DEFAULT_TTL_HOURS: float = 12.0

_GZIP_MAGIC = b"\x1f\x8b"


def _cache_path_for(url: str, cache_dir: Path) -> Path:
    """Nombre seguro y determinista para la entrada de cache de una URL."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    suffix = ".gz" if url.split("?", 1)[0].lower().endswith(".gz") else ".xml"
    return cache_dir / f"epg_{digest}{suffix}"


def _is_fresh(path: Path, ttl_seconds: float, now: float | None = None) -> bool:
    """True si el archivo de cache existe y su edad es menor que el TTL."""
    if not path.is_file():
        return False
    ref = time.time() if now is None else now
    try:
        return (ref - path.stat().st_mtime) < ttl_seconds
    except OSError:
        return False


def _decompress_and_decode(data: bytes, name_hint: str) -> str:
    """Descomprime gzip (por magia o sufijo .gz) y decodifica a texto.

    La descompresión va acotada por ``limits.max_file_bytes``: un EPG.gz
    servido como contenido (sin cabecera Content-Encoding) no puede
    expandirse en memoria sin tope.
    """
    looks_gz = name_hint.lower().split("?", 1)[0].endswith(".gz") or data[:2] == _GZIP_MAGIC
    if looks_gz:
        data = gunzip_limited(data, what="El EPG descargado")
    return data.decode("utf-8-sig", errors="replace")


def fetch_bytes(url: str, timeout: float = DEFAULT_TIMEOUT, *,
                allow_private: bool = False) -> bytes:
    """Descarga los bytes crudos del EPG (solo http/https, con timeout).

    Pasa por :class:`thetvview.security.safe_http.SafeHttpClient`: política
    de URL, anti-SSRF (B3), redirects revalidados, TLS estricto y lectura
    acotada a ``limits.max_file_bytes``. Es la fuente que elige la propia
    playlist (`x-tvg-url`), así que la validación es obligatoria.

    Lanza ValueError para URLs no soportadas u OSError con mensaje
    amigable ante fallos de red/HTTP.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(
            f"URL no soportada (solo http/https): '{redact_text(url)}'"
        )

    client = SafeHttpClient(
        allow_private=allow_private,
        max_bytes=get_limits().max_file_bytes,
        purpose=PURPOSE_METADATA,
    )
    try:
        return client.get_bytes(url, timeout=timeout)
    except (OSError, ValueError):
        raise
    except IPTVError as exc:
        # RateLimitError/ConnectionLimitError no heredan de OSError y el
        # contrato de esta función es "OSError amigable".
        raise OSError(str(exc)) from exc


def load_url(
    url: str,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    force_refresh: bool = False,
    cache_dir: Path | None = None,
    allow_private: bool = False,
) -> Epg:
    """Descarga un EPG por URL usando cache local con TTL.

    - Si hay copia en cache con menos de `ttl_hours`, se usa sin red.
    - Si no, se descarga (con timeout), se guarda en cache y se parsea.
    - Con `ttl_hours <= 0` o `force_refresh=True` siempre re-descarga.
    - `cache_dir` permite tests; por defecto es config.EPG_CACHE_DIR.
    - `allow_private` habilita red privada/loopback solo si la fuente lo
      tiene declarado (SDD §11); por defecto está bloqueado.

    Errores de red/HTTP se propagan como OSError con mensaje amigable;
    URLs no http(s) lanzan ValueError.
    """
    url = url.strip()
    directory = config.EPG_CACHE_DIR if cache_dir is None else Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = _cache_path_for(url, directory)

    fresh = ttl_hours > 0 and not force_refresh and _is_fresh(path, ttl_hours * 3600)
    if fresh:
        try:
            text = _decompress_and_decode(path.read_bytes(), path.name)
            return parse_text(text)
        except (OSError, ValueError):
            pass  # cache corrupta: re-descargar

    raw = fetch_bytes(url, timeout=timeout, allow_private=allow_private)

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(raw)
        tmp.replace(path)
    except OSError as exc:
        raise OSError(f"No se pudo guardar el cache del EPG '{path}': {exc}") from exc

    return parse_text(_decompress_and_decode(raw, url))
