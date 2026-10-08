"""Filas de Favoritos con el programa que se está emitiendo (puro, sin curses).

Mismo criterio que `ui/actions.py` y `ui/textwidth.py`: aquí vive la lógica de
la lista —qué programa corresponde a cada favorito y cómo se reparten las dos
columnas— y **no** en la pantalla. La pantalla sólo pinta.

Tres reglas del SDD de Favoritos que este módulo concentrates:

- el favorito sigue siendo **un canal** (§4, §26): el programa se resuelve al
  construir las filas y no se persiste en ningún sitio;
- la resolución es **una vez por fila**, sobre el `Epg` que ya está en memoria:
  ni red ni disco, ni una petición por favorito (§10, CA-08);
- **el nombre del canal nunca se sacrifica** por información del EPG (§15):
  si no caben las dos columnas, se dibuja sólo la primera.

Nada aquí lanza por datos incompletos: un canal sin `tvg-id`, un EPG sin
`channel-id`, un programa sin título o sin horario son datos ausentes, no
errores (§18).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from thetvview import epg_match
from thetvview.epg_parser import Epg
from thetvview.models import Channel, Program

from . import icons
from .textwidth import cell_width, clip_cells

__all__ = [
    "RowState",
    "FavoriteRow",
    "build_rows",
    "channel_label",
    "program_text",
    "two_columns_fit",
    "layout_columns",
    "SIN_INFO",
    "CARGANDO",
    "MIN_TWO_COLUMN_CELLS",
]

#: Placeholder de "este canal no tiene nada que enseñar ahora mismo". Un
#: indicador discreto, no un mensaje de error (§11.4).
SIN_INFO = "—"

#: La guía se está descargando en segundo plano: el canal se ve igual y la
#: segunda columna dice lo que está pasando (§11.5).
CARGANDO = "Cargando…"

#: Por debajo de este ancho no se dibuja la segunda columna. Es el caso más
#: común —80 columnas con el prefijo, el scrollbar y nombres largos— y el §15
#: es explícito: primero el nombre del canal, luego el programa.
MIN_TWO_COLUMN_CELLS = 34

#: Separación entre las dos columnas.
COLUMN_GAP = 2

#: Celdas reservadas al prefijo de selección que dibuja el `ScrollableList`
#: (" ▸ " / "   "). Se descuentan aquí para no prometer un ancho que el widget
#: no puede pintar.
PREFIX_CELLS = 3

#: Celdas que se reservan al scrollbar cuando la lista desborda.
SCROLLBAR_CELLS = 1

#: Suelo de cada columna: por debajo de esto no hay información útil, así que
#: es mejor no dibujar esa columna que dibujarla inservible.
MIN_CHANNEL_CELLS = 12
MIN_PROGRAM_CELLS = 12


class RowState(StrEnum):
    """Qué se sabe de un favorito en este momento.

    Los cuatro estados existen porque son cuatro hechos distintos —"no tengo
    guía", "la guía está llegando", "tengo guía pero este canal no emite" y
    "esto es lo que están dando"— y un solo texto no los distingue. La
    pantalla decide qué se pinta de cada uno; aquí sólo se nombran.
    """

    SIN_EPG = "sin_epg"      # no hay guía: la lista se ve como siempre (§11.2)
    CARGANDO = "cargando"    # la guía se está descargando (§11.5)
    SIN_EMISION = "sin_emision"  # hay guía, este canal no emite ahora (§11.4)
    AL_AIRE = "al_aire"      # hay un programa en emisión (§11.3)


@dataclass(frozen=True, slots=True)
class FavoriteRow:
    """Un favorito ya resuelto contra la guía, listo para pintar.

    `next_change` es el instante en que este texto dejaría de ser cierto (el
    `stop` del programa, o el arranque del siguiente si no hay nada en
    emisión). **No se persiste**: existe para que la entrega con refresco por
    tiempo sepa cuándo hay que volver a preguntar, sin tener que releer toda
    la parrilla.
    """

    channel: Channel
    program: Program | None
    state: RowState
    next_change: datetime | None = None


def _aware(now: datetime) -> datetime:
    """`now` con zona horaria: sin ella no se puede comparar con la parrilla.

    Un `datetime` ingenuo se interpreta en hora local, que es lo que la app
    usa en el resto del código (`datetime.now().astimezone()`), en vez de
    reventar la comparación entre instantes (TypeError) dentro del
    `now_playing` (§22).
    """
    return now if now.tzinfo is not None else now.astimezone()


def _next_change(epg: Epg, cid: str, now: datetime, prog: Program | None) -> datetime | None:
    """Cuándo deja de ser cierto lo que esta fila muestra.

    Con un programa en emisión: su `stop`. Sin nada en emisión: el arranque
    del siguiente. Si no hay ninguno de los dos, no hay cambio conocido.
    """
    try:
        if prog is not None and prog.stop is not None:
            return prog.stop
        siguiente = epg.next_programme(cid, now)
        return siguiente.start if siguiente is not None else None
    except Exception:  # noqa: BLE001 - un dato raro no puede tumbar la lista
        return None


def build_rows(
    canales: list[Channel],
    epg: Epg | None,
    now: datetime,
    *,
    epg_loading: bool = False,
    index: epg_match.EpgIndex | None = None,
) -> list[FavoriteRow]:
    """Una fila por favorito, con su programa actual resuelto.

    Un recorrido O(n) sobre los favoritos. `epg` es el que **ya** está cargado
    en memoria: esta función no pide nada, no lee disco y no abre red, ni
    aunque `epg` sea `None` (que es el caso "sin guía", y se responde
    igualmente con `SIN_EPG`).

    `index` evita reconstruir el índice de nombres cuando el llamante ya lo
    tiene; sin él se construye (o se reutiliza) una vez por llamada.
    """
    if epg is None:
        estado = RowState.CARGANDO if epg_loading else RowState.SIN_EPG
        return [FavoriteRow(channel=c, program=None, state=estado) for c in canales]

    momento = _aware(now)
    indice = epg_match.for_epg(epg) if index is None else index
    filas: list[FavoriteRow] = []
    for canal in canales:
        cid = indice.resolve(canal) if indice is not None else None
        if cid is None:
            # Hay guía pero este canal no está en ella: es un dato ausente, no
            # un problema. El favorito sigue ahí y se sigue pudiendo ver (§18).
            filas.append(
                FavoriteRow(channel=canal, program=None, state=RowState.SIN_EMISION)
            )
            continue
        prog: Program | None = None
        try:
            # El intervalo (`start <= now < stop`) ya lo decide `Epg`: aquí no
            # se reimplementa la comparación, que es lo que más fácil se
            # equivoca con las zonas horarias.
            prog = epg.now_playing(cid, momento)
        except Exception:  # noqa: BLE001 - datos inconsistentes: no hay programa
            prog = None
        filas.append(
            FavoriteRow(
                channel=canal,
                program=prog,
                state=RowState.AL_AIRE if prog is not None else RowState.SIN_EMISION,
                next_change=_next_change(epg, cid, momento, prog),
            )
        )
    return filas


# --- Las dos columnas ------------------------------------------------------


def channel_label(canal: Channel) -> str:
    """Primera columna: el nombre del canal y, si es radio, su distintivo.

    Sin `★` por fila —en Favoritos todo lo que hay es favorito por definición,
    así que el asterisco sólo consume ancho— y sin el prefijo de grupo, para
    que la columna quede limpia y alineada.
    """
    if getattr(canal, "radio", False):
        return f"{icons.ICON_RADIO} {canal.name}"
    return canal.name


def program_text(fila: FavoriteRow) -> str:
    """Segunda columna: el programa actual, o por qué no hay ninguno."""
    if fila.state is RowState.AL_AIRE and fila.program is not None:
        titulo = (fila.program.title or "").strip()
        return titulo or SIN_INFO
    if fila.state is RowState.CARGANDO:
        return CARGANDO
    return SIN_INFO


def _available(width: int) -> int:
    """Celdas que la fila puede usar de verdad, descontando el cromado."""
    return max(0, width - PREFIX_CELLS - SCROLLBAR_CELLS)


def two_columns_fit(width: int) -> bool:
    """¿Cabe la segunda columna en `width` sin comerse el nombre del canal?

    `width` es el ancho de la ventana, no el de la fila: el prefijo de selección
    y el scrollbar se descuentan aquí para no prometer más de lo que hay. No
    depende de cuántas filas haya, sólo del sitio que queda.
    """
    if width < MIN_TWO_COLUMN_CELLS:
        return False
    disponible = _available(width)
    return disponible >= MIN_CHANNEL_CELLS + COLUMN_GAP + MIN_PROGRAM_CELLS


def layout_columns(
    rows: list[FavoriteRow],
    width: int,
) -> list[tuple[str, str]]:
    """Reparte cada fila en `(columna del canal, columna del programa)`.

    Las anchuras se miden en **celdas** (`cell_width`), nunca con `len()`: un
    nombre con emoji o con coreano y cirílico ocupa dos celdas por carácter, y
    cortarlo por caracteres deja media fila corrida (§42, §43).

    La primera columna se rellena con el nombre más largo que quepa, y la
    segunda recibe lo que quede. Si no caben las dos, la segunda viene vacía:
    el nombre del canal es lo último que se sacrifica (§15).
    """
    etiquetas = [channel_label(fila.channel) for fila in rows]
    programas = [program_text(fila) for fila in rows]
    disponible = _available(width)

    if not two_columns_fit(width):
        return [(clip_cells(etiqueta, disponible), "") for etiqueta in etiquetas]

    primera = min(
        max((cell_width(etiqueta) for etiqueta in etiquetas), default=0),
        disponible - COLUMN_GAP - MIN_PROGRAM_CELLS,
    )
    primera = max(primera, MIN_CHANNEL_CELLS)

    salida: list[tuple[str, str]] = []
    for etiqueta, programa in zip(etiquetas, programas):
        canal = clip_cells(etiqueta, primera)
        canal += " " * (primera - cell_width(canal))
        # El hueco va al final de la primera columna para que el `ScrollableList`
        # sepa dónde empieza la segunda sin tener que saber el ancho de nada.
        canal += " " * COLUMN_GAP
        salida.append((canal, clip_cells(programa, max(0, disponible - primera - COLUMN_GAP))))
    return salida