"""Paleta de colores de la TUI, centralizada (solo curses stdlib).

Define IDs estables de pares de colores. Los colores reales los inicializa
theme.init_colors() según el tema activo (light/dark). Este módulo solo
expone los IDs y el helper pair().
"""

from __future__ import annotations

import curses

# --- Pares de colores (ids estables) ---

PAIR_NORMAL = 1
PAIR_TITLE = 2
PAIR_STATUS = 3
PAIR_SELECTED = 4
PAIR_ERROR = 5
PAIR_DIM = 6
PAIR_SEPARATOR = 7
PAIR_ACCENT = 8
PAIR_CURRENT = 9
PAIR_SEARCH = 10
PAIR_EMPTY = 11

# Pares extendidos (tema dual)
PAIR_PRIMARY = 12
PAIR_PRIMARY_HI = 13
PAIR_SUCCESS = 14
PAIR_WARNING = 15
PAIR_DANGER = 16
PAIR_SURFACE = 17
PAIR_MUTED = 18
PAIR_BORDER = 19
PAIR_MODAL_BG = 20
PAIR_MODAL_BORDER = 21
PAIR_FOCUS = 22
PAIR_SCROLLBAR = 23
PAIR_SELECTED_M3U = 24  # amarillo para playlists M3U/html
PAIR_SELECTED_XTREAM = 25  # púrpura para playlists Xtream


def selection_pair_for_kind(kind: str | None) -> int:
    """Par de selección según tipo de lista: xtream → púrpura, resto → amarillo."""
    return PAIR_SELECTED_XTREAM if (kind or "m3u") == "xtream" else PAIR_SELECTED_M3U


def pair(pair_id: int) -> int:
    """Devuelve el atributo color para usar en addstr."""
    return curses.color_pair(pair_id) if curses.has_colors() else 0
