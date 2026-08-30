"""Íconos Unicode centralizados para la TUI (stdlib-only).

Evita emojis (no todos los terminales los soportan). Usa box-drawing y
símbolos Unicode seguros en la mayoría de terminales modernos (xterm-256color,
kitty, alacritty, wezterm, windows terminal).
"""

from __future__ import annotations

# --- Canal / reproducción -------------------------------------------------
ICON_TV = "◉"
ICON_RADIO = "♪"
ICON_PLAY = "▶"
ICON_LIVE = "●"
ICON_LIVE_OFF = "○"

# --- Favoritos -------------------------------------------------------------
ICON_STAR = "★"
ICON_STAR_OFF = "☆"

# --- Grupos ----------------------------------------------------------------
ICON_GROUP = "▣"

# --- EPG / timeline --------------------------------------------------------
ICON_EPG = "◈"
ICON_TIME = "◷"

# --- UI / navegación -------------------------------------------------------
ICON_ARROW_UP = "▲"
ICON_ARROW_DOWN = "▼"
ICON_ARROW_LEFT = "◀"
ICON_ARROW_RIGHT = "▶"
ICON_CHEVRON = "▸"
ICON_CHEVRON_DOWN = "▾"

# --- Box-drawing (bordes) --------------------------------------------------
BOX_H = "─"
BOX_V = "│"
BOX_TL = "┌"
BOX_TR = "┐"
BOX_BL = "└"
BOX_BR = "┘"
BOX_T = "┬"
BOX_B = "┴"
BOX_L = "├"
BOX_R = "┤"
BOX_CROSS = "┼"

# --- Bordes redondeados (simulados) ----------------------------------------
BOX_R_TL = "╭"
BOX_R_TR = "╮"
BOX_R_BL = "╰"
BOX_R_BR = "╯"

# --- Bordes dobles (para modales) ------------------------------------------
BOX_D_H = "═"
BOX_D_V = "║"
BOX_D_TL = "╔"
BOX_D_TR = "╗"
BOX_D_BL = "╚"
BOX_D_BR = "╝"
BOX_D_T = "╦"
BOX_D_B = "╩"
BOX_D_L = "╠"
BOX_D_R = "╣"

# --- Líneas de progreso / timeline -----------------------------------------
LINE_FULL = "━"
LINE_EMPTY = "━"
LINE_DOT = "●"
LINE_CIRCLE = "○"

# --- Bloques fraccionales (barras sub-celda) --------------------------------
BLOCK_FULL = "█"
BLOCK_7_8 = "▇"
BLOCK_3_4 = "▆"
BLOCK_5_8 = "▅"
BLOCK_1_2 = "▄"
BLOCK_3_8 = "▃"
BLOCK_1_4 = "▂"
BLOCK_1_8 = "▁"
BLOCK_R_8 = "▐"
BLOCK_L_8 = "▌"

# --- Búsqueda --------------------------------------------------------------
ICON_SEARCH = "⌕"
ICON_CLEAR = "×"

# --- Ayuda -----------------------------------------------------------------
ICON_HELP = "?"

# --- Tema ------------------------------------------------------------------
ICON_THEME_LIGHT = "☀"
ICON_THEME_DARK = "☽"

# --- Toast / estado --------------------------------------------------------
ICON_OK = "✔"
ICON_WARN = "⚠"
ICON_ERR = "✘"
ICON_INFO = "●"
