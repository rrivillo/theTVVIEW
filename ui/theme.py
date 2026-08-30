"""Dual-theme (light/dark) para la TUI — solo stdlib curses.

Define paletas por tema y expone init_colors(theme_name) + attr(role).
Fallback automático a 8 colores si 256 no están disponibles.
Respeto NO_COLOR=1 (https://no-color.org/).
"""

from __future__ import annotations

import curses
import os
from typing import Any

from . import colors


# --- Paletas semánticas por tema -------------------------------------------

_THEMES: dict[str, dict[str, Any]] = {
    "light": {
        "bg": -1,  # fondo de la terminal
        # roles -> (fg_256, fg_8_fallback)
        "text":       (235, curses.COLOR_BLACK),
        "muted":      (240, curses.COLOR_BLUE),   # más oscuro para contraste
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
}


def _color256(c256: int, c8: int) -> int:
    """Devuelve el color id: 256 si soportado, fallback 8."""
    if curses.COLORS >= 256:
        return c256
    return c8


def init_colors(theme_name: str = "light") -> None:
    """Inicializa curses pairs según el tema activo.

    Si NO_COLOR=1 está en el entorno, desactiva colores (solo atributos).
    """
    # Respetar https://no-color.org/
    if os.environ.get("NO_COLOR"):
        return
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()

    t = _THEMES.get(theme_name, _THEMES["light"])
    bg = t["bg"]

    def _fg(role: str) -> int:
        c = t[role]
        return _color256(c[0], c[1])

    curses.init_pair(colors.PAIR_NORMAL,    _fg("text"), bg)
    curses.init_pair(colors.PAIR_TITLE,     _fg("title_fg"), _fg("title_bg"))
    curses.init_pair(colors.PAIR_STATUS,    _fg("text"), _fg("surface"))
    curses.init_pair(colors.PAIR_SELECTED,  _fg("selected_fg"), _fg("selected_bg"))
    curses.init_pair(colors.PAIR_ERROR,     _fg("error_fg"), _fg("error_bg"))
    curses.init_pair(colors.PAIR_DIM,       _fg("muted"), bg)
    curses.init_pair(colors.PAIR_SEPARATOR, _fg("border"), bg)
    curses.init_pair(colors.PAIR_ACCENT,    _fg("accent"), bg)
    curses.init_pair(colors.PAIR_CURRENT,   _fg("current_fg"), bg)
    curses.init_pair(colors.PAIR_SEARCH,    _fg("search_fg"), _fg("search_bg"))
    curses.init_pair(colors.PAIR_EMPTY,     _fg("muted"), bg)

    # Pares extendidos (nuevos roles)
    curses.init_pair(colors.PAIR_PRIMARY,     _fg("primary"), bg)
    curses.init_pair(colors.PAIR_PRIMARY_HI,  _fg("primary_hi"), bg)
    curses.init_pair(colors.PAIR_SUCCESS,     _fg("success"), bg)
    curses.init_pair(colors.PAIR_WARNING,     _fg("warning"), bg)
    curses.init_pair(colors.PAIR_DANGER,      _fg("danger"), bg)
    curses.init_pair(colors.PAIR_SURFACE,     _fg("text"), _fg("surface"))
    curses.init_pair(colors.PAIR_MUTED,       _fg("muted"), bg)
    curses.init_pair(colors.PAIR_BORDER,      _fg("border"), bg)

    # Modales: fondo deSurface invertido
    curses.init_pair(colors.PAIR_MODAL_BG,    _fg("text"), _fg("surface"))
    curses.init_pair(colors.PAIR_MODAL_BORDER, _fg("primary"), bg)

    # Focus y scrollbar
    curses.init_pair(colors.PAIR_FOCUS,     _fg("focus"), bg)
    curses.init_pair(colors.PAIR_SCROLLBAR, _fg("scrollbar"), bg)


def attr(role: str, *modifiers: str) -> int:
    """Devuelve el atributo curses para un rol semántico.

    Roles: text, muted, border, primary, success, warning, danger, accent,
           title_fg, selected_fg, current_fg, error_fg, search_fg.
    Modifiers: bold, dim, reverse, underline.
    """
    role_to_pair = {
        "text":        colors.PAIR_NORMAL,
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
