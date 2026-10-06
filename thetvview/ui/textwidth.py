"""Medida de anchura en celdas de terminal (SDD §42, §43).

Existe porque `len()` **no** es una medida de anchura: una línea de la barra
inferior contiene `★ ▶ ↑ ↓ ← → …`, y todos ellos son *East Asian Ambiguous*
— 1 carácter de Python que ocupa 1 celda en una terminal moderna y 2 en una
terminal CJK. Medir con `len()` es, por tanto, incorrecto por construcción: en
el mejor caso desborda a la derecha (y curses lanza `curses.error` al tocar la
esquina inferior derecha), en el peor recorta una etiqueta a la mitad.

Regla de este módulo (§43, explícita y sin ambigüedad):

- *East Asian Width* ``W`` o ``F`` → **2 celdas**;
- *East Asian Width* ``A`` (*Ambiguous*: `★ ▶ ← → ↑ ↓ … │ ─`) → **1 celda**,
  que es lo que hacen las terminales modernas y lo que espera el resto del
  proyecto (sus tests asertan `★` y `▶`);
- marcas combinantes (``unicodedata.combining``) → **0 celdas**, porque se
  superponen a la letra anterior y no consumen columna;
- todo lo demás → **1 celda**.

Preferencia explícita del §42: **si ya hay equivalente ASCII en el proyecto se
prefiere**. `★ ▶` aparecen hoy en `Screen.shortcuts()` y se conservan porque los
tests los asertan; para arte *nuevo* de la barra se evalúa ASCII primero.

Puro, sin `curses`, testeable sin terminal.
"""

from __future__ import annotations

import unicodedata

__all__ = ["char_width", "cell_width", "clip_cells"]

# Anchuras que ocupan dos celdas (CJK, emoji de presentación ancha, Hangul…).
_WIDE_CLASSES = frozenset("WF")


def char_width(ch: str) -> int:
    """Ancho en celdas de un único carácter (0, 1 o 2)."""
    if not ch:
        return 0
    # Las marcas combinantes se dibujan encima del carácter anterior.
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in _WIDE_CLASSES:
        return 2
    # 'A' (Ambiguous), 'N' (Neutral), 'Na', 'H', 's' → 1 celda.
    return 1


def cell_width(s: str) -> int:
    """Ancho en celdas de terminal de ``s``.

    Estima, nunca miente por construcción: usa la misma regla para todos los
    caracteres, sin excepciones por símbolo.
    """
    return sum(char_width(ch) for ch in s)


def clip_cells(s: str, max_cells: int) -> str:
    """Trunca ``s`` a ``max_cells`` sin partir un carácter ancho.

    Devuelve el texto completo si ya cabe, y ``""`` si ``max_cells <= 0``.
    Nunca deja medio glifo: si el siguiente carácter no cabe entero, se para
    ahí (§46: una etiqueta no se parte por la mitad).
    """
    if max_cells <= 0:
        return ""
    total = 0
    out: list[str] = []
    for ch in s:
        w = char_width(ch)
        if total + w > max_cells:
            break
        out.append(ch)
        total += w
    return "".join(out)