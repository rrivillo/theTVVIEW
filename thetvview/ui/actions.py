"""Modelo de acciones contextuales (SDD §7, §8, §17, §21, §22, §44, §45, §63, §71).

**Define once, render everywhere.** Una acción se declara *una sola vez* en
`Screen.actions()` y de ella salen la barra inferior, y —a futuro— el manejo de
teclas. Y al revés también: **la barra nunca declara nada que la pantalla no
haga**. Si la definición se equivoca, la ayuda `?` y `Screen.handle_key()`
siguen siendo la verdad; la barra es una vista, no una fuente de autoridad
(§10, AC-03).

Este módulo es **puro**: sin `curses`, testeable sin terminal, y sin lógica de
render (§67 prohibe lógica en componentes visuales — por eso `fit_actions` vive
aquí y no en `FooterBar`).

Tres responsabilidades que el SDD mantiene separadas y este módulo no mezcla:

- ``priority``  **ordena** el catálogo (§17): qué se descarta primero cuando
  el ancho no da para todo.
- ``essential`` **garantiza supervivencia** (§45): marca las acciones que no
  deberían desaparecer nunca mientras haya espacio. Por eso `? Ayuda` tiene la
  prioridad *más baja* y ``essential=True`` *a la vez*: son responsabilidades
  distintas.
- ``enabled``   **explicita la disponibilidad** (§8, §36): la acción vive
  siempre en el catálogo; si ahora no está disponible se marca, no se borra.
  Así la barra no puede contradecir a la acción (R-04).
"""

from __future__ import annotations

from dataclasses import dataclass

from .textwidth import cell_width

__all__ = [
    "P",
    "V",
    "LABELS",
    "MAX_VISIBLE",
    "SEPARATOR",
    "LEADING",
    "Action",
    "fit_actions",
    "validate",
]


# =========================================================================
# Escala de prioridad (§17) — constantes, no números mágicos
# =========================================================================


class P:
    """Escala de prioridad de acciones (SDD §17)."""

    PRIMARIA = 100  # acción primaria (Enter Ver / Abrir)
    FRECUENTE = 90  # /= Buscar
    CONTEXTUAL = 80  # f Favorito/Quitar
    ORGANIZACION = 70  # g Grupos, r Recientes
    SECUNDARIA = 50  # e EPG, p Reproductor
    NAVEGACION = 20  # Esc Volver
    AYUDA = 10  # ? Ayuda


# =========================================================================
# Vocabulario cerrado de etiquetas (§21)
# =========================================================================


class V:
    """Vocabulario cerrado de etiquetas (SDD §21).

    Existe para terminar con la deriva que el §21 prohíbe: ``Ver`` /
    ``Reproducir`` / ``Play`` / ``Abrir canal`` para la misma operación. Si
    hace falta una etiqueta nueva, se añade **aquí** y en ningún otro sitio, y
    `validate()` obliga a usarla.
    """

    # --- núcleo (§21) ---
    VER = "Ver"
    ABRIR = "Abrir"
    BUSCAR = "Buscar"
    FAVORITO = "Favorito"
    QUITAR = "Quitar"
    EDITAR = "Editar"
    ACTUALIZAR = "Actualizar"
    HORARIO = "Horario"
    VOLVER = "Volver"
    AYUDA = "Ayuda"
    # --- las que exigen las 10 pantallas (§9, §34, §36) ---
    ARCHIVO = "Archivo"  # catch-up: Enter Ver|Archivo
    GRUPO = "Grupos"
    EPG = "EPG"
    RECARGAR = "Recargar"
    CONFIRMAR = "Confirmar"  # búsqueda: Enter confirmar
    VACIAR = "Vaciar"  # búsqueda: Ctrl-U borrar
    CANCELAR = "Cancelar"  # búsqueda: Esc cancelar
    ANADIR = "Añadir"
    BORRAR = "Borrar"
    ELIMINAR = "Eliminar"  # pie del modal de confirmar borrado
    GUARDAR = "Guardar"  # pie del modal de guardar un formulario
    CONTRASENA_X = "Contraseña X"  # catálogo: C (sólo Listas X)
    LIMPIAR = "Limpiar"  # Recientes: r
    ELEGIR = "Elegir"  # Reproductor/Calidad/Pistas: ↑↓ / ←→
    SECCION = "Sección"  # Pistas: Tab
    MARCAR = "Marcar"  # Pistas: Espacio
    CONTINUAR = "Continuar"  # Calidad: Enter
    REPRODUCTOR = "Reproductor"  # Pistas sin player elegido: Enter
    DETENER = "Detener"  # Reproduciendo: q
    DIAGNOSTICO = "Diagnóstico"  # Reproduciendo: d
    INFO = "Info"  # Reproduciendo: i
    #: Modales de error: R reintenta la operación que falló (§7 del SDD de
    #: errores). Existe **una sola vez** aquí porque `validate()` rechaza
    #: cualquier etiqueta fuera de este vocabulario, y sin ella el pie del
    #: modal de error no podría anunciar su propia acción.
    REINTENTAR = "Reintentar"


#: Conjunto cerrado de etiquetas permitidas (§21). `validate()` lo comprueba.
LABELS: frozenset[str] = frozenset(
    value for name, value in vars(V).items() if not name.startswith("_")
)

# =========================================================================
# Geometría del chip (una sola definición: la miden y la pintan con la misma)
# =========================================================================

#: Máximo de acciones visibles a la vez (§18, R-03): hace la barra breve de verdad.
MAX_VISIBLE = 6

#: Separador entre chips (§23).
SEPARATOR = "│"

#: Espacio inicial de la línea del pie.
LEADING = 1


# =========================================================================
# El modelo
# =========================================================================


@dataclass(frozen=True, slots=True)
class Action:
    """Una acción contextual declarada por una pantalla.

    - ``key``: case exacto, nunca normalizado ("Enter", "?", "←→").
    - ``label``: vocabulario cerrado (§21), una constante de `V`.
    - ``priority``: escala de `P`; ordena el catálogo.
    - ``essential``: no desaparece mientras haya espacio (§45).
    - ``enabled``: declarada pero no disponible ahora (§8).
    """

    key: str
    label: str
    priority: int = P.SECUNDARIA
    essential: bool = False
    enabled: bool = True

    def chip(self) -> str:
        """Texto del chip sin espaciado exterior: ``"Enter Ver"``."""
        return f"{self.key} {self.label}"

    def chip_cells(self) -> int:
        """Ancho en celdas del chip con su espaciado exterior."""
        return cell_width(self.chip()) + 2


# =========================================================================
# Unicidad de clave (§22) y vocabulario cerrado (§21)
# =========================================================================


def validate(actions: list[Action]) -> None:
    """Comprueba las invariantes del catálogo de una pantalla.

    Dos ``Action`` no pueden compartir ``key`` en la misma pantalla (§22): un
    footer con ``f Favorito`` y ``F Favorito`` a la vez es un error de
    desarrollo, no un footer que se dibuja. Fallar es preferible a pintar una
    ambigüedad.

    Falla con ``AssertionError`` para que se vea en la suite, no en producción.
    """
    seen: dict[str, Action] = {}
    for action in actions:
        prev = seen.get(action.key)
        if prev is not None:
            raise AssertionError(
                f"clave de acción duplicada en la misma pantalla: {action.key!r} "
                f"({prev.label!r} y {action.label!r}) — SDD §22"
            )
        seen[action.key] = action
        if action.label not in LABELS:
            raise AssertionError(
                f"etiqueta fuera del vocabulario cerrado (§21): {action.label!r}; "
                f"añádela como constante en ui.actions.V"
            )
        if not action.key:
            raise AssertionError("acción sin tecla: no se puede anunciar (§8)")


# =========================================================================
# El ajuste por prioridad (§44, §45, §46, §48)
# =========================================================================


def fit_actions(
    actions: list[Action],
    width: int,
    *,
    max_visible: int = MAX_VISIBLE,
) -> list[Action]:
    """Qué acciones caben en ``width`` celdas, por prioridad (§44).

    Algoritmo (§44, §45, §46, §48):

    1. descartar ``enabled=False`` (§8);
    2. ordenar por ``priority`` desc, **estable**: a igual prioridad desempata
       el orden de declaración;
    3. agregar mientras quepa según `cell_width` (§43, §44);
    4. 2ª pasada con sólo las ``essential``: si una no coló y sigue habiendo
       hueco se añade, y si no cabe **sacrifica la acción no esencial menos
       importante** ya presente (§45). Nunca desplaza a una más importante: un
       help que empuja a ``Enter Ver`` fuera sería peor que no tener help;
    5. nunca truncar una etiqueta a medias (§46): lo que no cabe, se descarta
       entero; lo que queda es siempre una línea, sin wrap (§48).

    Tope duro: ``max_visible`` acciones (§18, R-03), contando las esenciales:
    en un terminal de 80 columnas Canales no cabe todo y, entre ``Esc Volver``
    y ``? Ayuda``, el que sobrevive es el que el usuario puede descubrir.
    """
    validate(actions)
    if width <= 0:
        return []  # ni el espacio inicial cabe: no se dibuja ninguna (§44)
    # 1. descartar no disponibles + 2. ordenar estable por prioridad
    ordered = sorted(
        ((i, a) for i, a in enumerate(actions) if a.enabled),
        key=lambda item: (-item[1].priority, item[0]),
    )

    chosen: list[tuple[int, Action]] = []
    used = LEADING  # el espacio inicial de la línea

    def cost(action: Action) -> int:
        """Celdas que cuesta añadir ``action``: su chip y su separador.

        El separador `│` va *delante* de cada chip menos del primero, así que
        el ancho total de una lista de N acciones es
        ``LEADING + Σ chip_cells + (N - 1)`` celdas.
        """
        return action.chip_cells() + (1 if chosen else 0)

    # 3. primera pasada
    for decl, action in ordered:
        if len(chosen) >= max_visible:
            break
        need = cost(action)  # antes de añadir: `cost` depende de cuántas hay
        if used + need <= width:
            chosen.append((decl, action))
            used += need

    # 4. segunda pasada: sólo las essential que no colaron
    #    La más importante de las visibles nunca es víctima: sin ella la barra
    #    se quedaría anunciando sólo "Ayuda", que es peor que no tenerla (§45).
    protected = chosen[0] if chosen else None
    picked = {decl for decl, _ in chosen}
    for decl, action in ordered:
        if decl in picked or not action.essential:
            continue
        need = cost(action)
        if len(chosen) < max_visible and used + need <= width:
            chosen.append((decl, action))
            used += need
            picked.add(decl)
            continue
        victim = _least_important(chosen, protected)
        if victim is None:
            break  # no hay nada sacrificable: no se expulsa a nadie
        freed = victim[1].chip_cells() + 1
        if used - freed + need > width:
            break  # ni la víctima menos importante libera lo suficiente
        chosen.remove(victim)
        chosen.append((decl, action))
        used = used - freed + need
        picked.add(decl)

    # Orden final: prioridad desc, y a igualdad el orden de declaración.
    chosen.sort(key=lambda item: (-item[1].priority, item[0]))
    return [action for _, action in chosen]


def _least_important(
    chosen: list[tuple[int, Action]],
    protected: tuple[int, Action] | None = None,
) -> tuple[int, Action] | None:
    """La entrada no esencial menos importante de ``chosen``, o ``None``.

    "Menos importante" = menor ``priority``; a igualdad, la declarada más
    tarde. No son víctimas las esenciales — cambiarlas por otra esencial no
    mejora nada (§45) — ni la entrada ``protected``.
    """
    victims = [
        item
        for item in chosen
        if not item[1].essential and item != protected
    ]
    if not victims:
        return None
    return min(victims, key=lambda item: (item[1].priority, -item[0]))