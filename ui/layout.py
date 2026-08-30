"""Layout engine: cálculo de rectángulos de zona (header/sidebar/main/footer).

Stdlib-only. Se recalcula cada frame a partir de getmaxyx().
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    """Rectángulo de pantalla (y, x, height, width)."""
    y: int
    x: int
    h: int
    w: int

    @property
    def bottom(self) -> int:
        return self.y + self.h - 1

    @property
    def right(self) -> int:
        return self.x + self.w - 1


def header_rect(max_y: int, max_x: int) -> Rect:
    """Fila 0: barra de título con breadcrumbs."""
    return Rect(0, 0, 1, max_x)


def separator_rect(y: int, max_x: int) -> Rect:
    """Línea separadora de 1 fila."""
    return Rect(y, 0, 1, max_x)


def footer_rect(max_y: int, max_x: int) -> Rect:
    """Últimas 2 filas: separador + barra de estado."""
    if max_y < 3:
        return Rect(max(0, max_y - 1), 0, 1, max_x)
    return Rect(max_y - 2, 0, 2, max_x)


def main_rect(max_y: int, max_x: int) -> Rect:
    """Zona principal entre header (fila 0) y footer (últimas 2 filas).

    Header ocupa fila 0; la fila 1 se reserva para search bar o título
    de sección, pero se incluye en main para simplificar cálculos.
    """
    top = 1  # header
    bottom_reserved = 2  # separador + footer
    h = max(1, max_y - top - bottom_reserved)
    return Rect(top, 0, h, max_x)

def content_rect(max_y: int, max_x: int, has_search: bool = False) -> Rect:
    """Zona de contenido lista/cards dentro de main (descuenta search si existe)."""
    main = main_rect(max_y, max_x)
    if has_search:
        # search bar ocupa 1 fila en y=1, listas arrancan en y=2
        return Rect(main.y + 1, main.x, max(1, main.h - 1), main.w)
    return main


def sidebar_rect(max_y: int, max_x: int, ratio: float = 0.6) -> Rect:
    """Panel izquierdo en layout master-detail. Ancho mínimo 28 cols."""
    main = main_rect(max_y, max_x)
    sidebar_w = max(28, int(main.w * ratio))
    # Panel derecho (detalle) necesita al menos 28 cols
    if sidebar_w > main.w - 28:
        sidebar_w = max(28, main.w - 28)
    return Rect(main.y, main.x, main.h, sidebar_w)


def detail_rect(max_y: int, max_x: int, ratio: float = 0.6) -> Rect:
    """Panel derecho en layout master-detail."""
    main = main_rect(max_y, max_x)
    sb = sidebar_rect(max_y, max_x, ratio)
    detail_x = sb.x + sb.w + 1  # +1 para separador vertical
    detail_w = max(1, main.w - sb.w - 1)
    return Rect(main.y, detail_x, main.h, detail_w)
