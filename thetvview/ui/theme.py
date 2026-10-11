"""Tres temas (light/dark/high_contrast) para la TUI — solo stdlib curses.

Define paletas por tema y expone init_colors(theme_name) + attr(role).
Fallback automático a 8 colores si 256 no están disponibles.
Respeto NO_COLOR=1 (https://no-color.org/).

La paleta se **añade**, no se reescribe: `light` y `dark` son lo que ya había y
sus valores están fijados por tests, así que el tercer tema es un bloque nuevo
y no una reinterpretación de los otros dos (FR-THE-01).
"""

from __future__ import annotations

import curses
import os
from typing import Any

from thetvview import config as _config

from . import colors


# --- Paletas semánticas por tema -------------------------------------------

#: `bg` es el fondo por defecto: -1 = el de la terminal. En `high_contrast` es
#: negro (0), porque el contraste alto tiene que valer aunque el fondo de la
#: terminal sea claro, y ese es justo el caso donde el otro tema falla.
_THEMES: dict[str, dict[str, Any]] = {
    "light": {
        "bg": -1,  # fondo de la terminal
        # roles -> (fg_256, fg_8_fallback)
        "text":       (235, curses.COLOR_BLACK),
        "muted":      (240, curses.COLOR_BLUE),   # más oscuro para contraste
        "secondary":  (25,  curses.COLOR_BLUE),   # texto de apoyo con color propio
        "border":     (238, curses.COLOR_WHITE),  # visible en light (antes 245 invisible)
        "surface":    (254, curses.COLOR_WHITE),
        "primary":    (21,  curses.COLOR_BLUE),     # azul sólido
        "primary_hi": (27,  curses.COLOR_BLUE),
        "success":    (28,  curses.COLOR_GREEN),
        "warning":    (172, curses.COLOR_YELLOW),
        "danger":     (160, curses.COLOR_RED),
        "accent":     (31,  curses.COLOR_CYAN),
        "title_fg":   (255, curses.COLOR_BLACK),
        "title_bg":   (21,  curses.COLOR_BLUE),
        "selected_fg": (255, curses.COLOR_BLACK),
        "selected_bg": (21,  curses.COLOR_BLUE),
        "error_fg":   (255, curses.COLOR_WHITE),
        "error_bg":   (160, curses.COLOR_RED),
        "search_fg":  (235, curses.COLOR_BLACK),
        "search_bg":  (252, curses.COLOR_YELLOW),
        "current_fg": (130, curses.COLOR_YELLOW), # más contrastado que 136
        "focus":      (21,  curses.COLOR_BLUE),
        "scrollbar":  (240, curses.COLOR_WHITE),
    },
    "dark": {
        "bg": -1,
        "text":       (252, curses.COLOR_WHITE),
        "muted":      (243, curses.COLOR_BLUE),
        "secondary":  (250, curses.COLOR_WHITE),
        "border":     (240, curses.COLOR_WHITE),
        "surface":    (236, curses.COLOR_BLACK),
        "primary":    (141, curses.COLOR_MAGENTA),
        "primary_hi": (177, curses.COLOR_MAGENTA),
        "success":    (114, curses.COLOR_GREEN),
        "warning":    (221, curses.COLOR_YELLOW),
        "danger":     (203, curses.COLOR_RED),
        "accent":     (215, curses.COLOR_YELLOW),
        "title_fg":   (232, curses.COLOR_BLACK),
        "title_bg":   (141, curses.COLOR_MAGENTA),
        "selected_fg": (232, curses.COLOR_BLACK),
        "selected_bg": (141, curses.COLOR_MAGENTA),
        "error_fg":   (232, curses.COLOR_BLACK),
        "error_bg":   (203, curses.COLOR_RED),
        "search_fg":  (232, curses.COLOR_BLACK),
        "search_bg":  (221, curses.COLOR_YELLOW),
        "current_fg": (221, curses.COLOR_YELLOW),
        "focus":      (141, curses.COLOR_MAGENTA),
        "scrollbar":  (240, curses.COLOR_WHITE),
    },
    "high_contrast": {
        # Blanco sobre negro, sin matices: lo que se busca es el mayor
        # contraste posible, no una paleta bonita. Los colores de estado son
        # los de alta visibilidad (verde/amarillo/rojo puros del kubo de 8) en
        # vez de los tonos 256 intermedios, que aquí serían casi indistinguibles
        # (FR-THE-05).
        "bg": 0,
        "text":       (255, curses.COLOR_WHITE),
        "muted":      (252, curses.COLOR_WHITE),
        "secondary":  (250, curses.COLOR_WHITE),
        "border":     (250, curses.COLOR_WHITE),
        "surface":    (0,   curses.COLOR_BLACK),
        "primary":    (231, curses.COLOR_WHITE),
        "primary_hi": (255, curses.COLOR_WHITE),
        "success":    (46,  curses.COLOR_GREEN),
        "warning":    (226, curses.COLOR_YELLOW),
        "danger":     (196, curses.COLOR_RED),
        "accent":     (255, curses.COLOR_WHITE),
        "title_fg":   (231, curses.COLOR_BLACK),
        "title_bg":   (255, curses.COLOR_WHITE),
        # Selección invertida y con el máximo contraste: aquí es lo que
        # distingue una fila de otra (FR-THE-06).
        "selected_fg": (0,   curses.COLOR_BLACK),
        "selected_bg": (255, curses.COLOR_WHITE),
        "error_fg":   (0,   curses.COLOR_BLACK),
        "error_bg":   (196, curses.COLOR_RED),
        "search_fg":  (0,   curses.COLOR_BLACK),
        "search_bg":  (255, curses.COLOR_WHITE),
        "current_fg": (226, curses.COLOR_YELLOW),
        "focus":      (255, curses.COLOR_WHITE),
        "scrollbar":  (255, curses.COLOR_WHITE),
    },
}


def _colores_disponibles() -> int:
    """Cuántos colores da la terminal (8 si aún no se sabe).

    `curses.COLORS` sólo existe **después** de `initscr()`, y esta paleta se
    consulta en tests y en el arranque temprano. Un `getattr` con 8 de
    respaldo degrada a la paleta básica en vez de levantar `AttributeError`:
    justo lo que se quiere cuando todavía no se sabe qué soporta el terminal.
    """
    return int(getattr(curses, "COLORS", 0) or 0)


def _pares_disponibles() -> int:
    """Cuántos pares de color da la terminal (0 si aún no se sabe)."""
    return int(getattr(curses, "COLOR_PAIRS", 0) or 0)


def _color256(c256: int, c8: int) -> int:
    """Devuelve el color id: 256 si soportado, fallback 8."""
    if _colores_disponibles() >= 256:
        return c256
    return c8


def nombres() -> tuple[str, ...]:
    """Los tres temas, en el orden del alterno (`t`)."""
    return _config.THEMES


def nombre_legible(name: str) -> str:
    """«light» → «Claro». Para el modal de elección y los avisos."""
    return _LEGIBLES.get(_config.normalize_theme(name), _LEGIBLES[_config.DEFAULT_THEME])


#: Cómo se llama cada tema en español. Vive con la paleta porque es la misma
#: información: qué tema es, cómo se dice y cómo se ve.
_LEGIBLES: dict[str, str] = {
    "light": "Claro",
    "dark": "Oscuro",
    "high_contrast": "Alto contraste",
}


def init_colors(theme_name: str = "light") -> None:
    """Inicializa curses pairs según el tema activo.

    Si NO_COLOR=1 está en el entorno, desactiva colores (solo atributos).
    Un tema desconocido degrada al predeterminado en vez de fallar.
    """
    # Respetar https://no-color.org/
    if os.environ.get("NO_COLOR"):
        return
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()

    t = _THEMES.get(_config.normalize_theme(theme_name), _THEMES[_config.DEFAULT_THEME])
    bg = t["bg"]

    def _fg(role: str) -> int:
        c = t[role]
        return _color256(c[0], c[1])

    # Los pares van en orden de "cada vez menos imprescindible": el texto y la
    # selección se inicializan primero, y un terminal que sólo da 8 pares los
    # recibe. Recorrer una lista —en vez de veinte llamadas sueltas— es lo que
    # hace que la degradación sea una lista y no veinte `try/except`:
    # `init_pair` lanza `curses.error` en cuanto se le pide uno de más.
    pares: tuple[tuple[int, int, int], ...] = (
        (colors.PAIR_NORMAL,        _fg("text"), bg),
        (colors.PAIR_TITLE,         _fg("title_fg"), _fg("title_bg")),
        (colors.PAIR_STATUS,        _fg("text"), _fg("surface")),
        (colors.PAIR_SELECTED,      _fg("selected_fg"), _fg("selected_bg")),
        (colors.PAIR_ERROR,         _fg("error_fg"), _fg("error_bg")),
        (colors.PAIR_DIM,           _fg("muted"), bg),
        (colors.PAIR_SEPARATOR,     _fg("border"), bg),
        (colors.PAIR_ACCENT,        _fg("accent"), bg),
        (colors.PAIR_CURRENT,       _fg("current_fg"), bg),
        (colors.PAIR_SEARCH,        _fg("search_fg"), _fg("search_bg")),
        (colors.PAIR_EMPTY,         _fg("muted"), bg),
        # Pares extendidos.
        (colors.PAIR_PRIMARY,       _fg("primary"), bg),
        (colors.PAIR_PRIMARY_HI,    _fg("primary_hi"), bg),
        (colors.PAIR_SUCCESS,       _fg("success"), bg),
        (colors.PAIR_WARNING,       _fg("warning"), bg),
        (colors.PAIR_DANGER,        _fg("danger"), bg),
        (colors.PAIR_SURFACE,       _fg("text"), _fg("surface")),
        (colors.PAIR_MUTED,         _fg("muted"), bg),
        (colors.PAIR_BORDER,        _fg("border"), bg),
        # Texto de apoyo con color propio: el token `SECONDARY` del §4.4 del
        # SDD, que antes no existía y que `muted` no cubría (aquí `muted` ya
        # es el gris de apoyo).
        (colors.PAIR_SECONDARY,     _fg("secondary"), bg),
        # Modales: fondo de superficie invertido.
        (colors.PAIR_MODAL_BG,      _fg("text"), _fg("surface")),
        (colors.PAIR_MODAL_BORDER,  _fg("primary"), bg),
        # Focus y scrollbar.
        (colors.PAIR_FOCUS,         _fg("focus"), bg),
        (colors.PAIR_SCROLLBAR,     _fg("scrollbar"), bg),
        # Selección por tipo de lista: amarillo (M3U/html) y púrpura (Xtream).
        # El fg hereda selected_fg del tema para mantener contraste; el bg es
        # fijo por tipo para que no cambie con el tema elegido.
        (colors.PAIR_SELECTED_M3U,   _fg("selected_fg"),
         _color256(220, curses.COLOR_YELLOW)),
        (colors.PAIR_SELECTED_XTREAM, _fg("selected_fg"),
         _color256(135, curses.COLOR_MAGENTA)),
    )
    disponibles = _pares_disponibles()
    for par_id, fg, fondo in pares:
        if par_id > disponibles:
            break  # el terminal no llega a este par: los siguientes tampoco
        try:
            curses.init_pair(par_id, fg, fondo)
        except curses.error:
            break


def attr(role: str, *modifiers: str) -> int:
    """Devuelve el atributo curses para un rol semántico.

    Roles: text, secondary, muted, border, primary, success, warning, danger,
           accent, title_fg, selected_fg, current_fg, error_fg, search_fg.
    Modifiers: bold, dim, reverse, underline.
    """
    role_to_pair = {
        "text":        colors.PAIR_NORMAL,
        "secondary":   colors.PAIR_SECONDARY,
        "muted":       colors.PAIR_MUTED,
        "border":      colors.PAIR_BORDER,
        "primary":     colors.PAIR_PRIMARY,
        "primary_hi":  colors.PAIR_PRIMARY_HI,
        "success":     colors.PAIR_SUCCESS,
        "warning":     colors.PAIR_WARNING,
        "danger":      colors.PAIR_DANGER,
        "accent":      colors.PAIR_ACCENT,
        "title_fg":    colors.PAIR_TITLE,
        "selected":    colors.PAIR_SELECTED,
        "selected_m3u": colors.PAIR_SELECTED_M3U,
        "selected_xtream": colors.PAIR_SELECTED_XTREAM,
        "current":     colors.PAIR_CURRENT,
        "error":       colors.PAIR_ERROR,
        "search":      colors.PAIR_SEARCH,
        "empty":       colors.PAIR_EMPTY,
        "surface":     colors.PAIR_SURFACE,
        "separator":   colors.PAIR_SEPARATOR,
        "modal_bg":    colors.PAIR_MODAL_BG,
        "modal_border": colors.PAIR_MODAL_BORDER,
        "focus":       colors.PAIR_FOCUS,
        "scrollbar":   colors.PAIR_SCROLLBAR,
    }
    result = curses.color_pair(role_to_pair.get(role, colors.PAIR_NORMAL))
    for mod in modifiers:
        if mod == "bold":
            result |= curses.A_BOLD
        elif mod == "dim":
            result |= curses.A_DIM
        elif mod == "reverse":
            result |= curses.A_REVERSE
        elif mod == "underline":
            result |= curses.A_UNDERLINE
    return result
