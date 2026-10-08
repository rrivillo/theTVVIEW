"""Widgets reutilizables de la TUI (ScrollableList, StatusBar, helpers visuales).

Extiende con widgets modernos: HeaderBar, FooterBar, ToastStack, SearchBar,
Modal, EmptyState, Timeline, TabBar.
"""

from __future__ import annotations

import curses
import time
from typing import Any

from . import colors
from . import icons
from .actions import LEADING, P, SEPARATOR, Action, V, fit_actions
from .textwidth import cell_width, clip_cells, pad_cells as _pad_cells


class ScrollableList:
    """Lista con scroll y selección; renderiza solo lo visible.

    Opcionalmente cada fila lleva un **texto secundario** paralelo (`subitems`):
    la columna del programa en Favoritos. Es opt-in a propósito —sin
    subelementos el render es exactamente el de siempre— porque el mismo widget
    lo usan nueve pantallas más y ninguna de ellas necesita dos columnas.
    """

    #: Prefijos de la fila: el de selección y el de hueco. Ambos miden 3 celdas.
    PREFIX_SELECTED = " ▸ "
    PREFIX_PLAIN = "   "

    def __init__(self, items: list[str] | None = None, selected_pair: int | None = None) -> None:
        self.items: list[str] = items or []
        self.subitems: list[str] = []
        self.selected = 0
        self.top = 0
        self.selected_pair = selected_pair if selected_pair is not None else colors.PAIR_SELECTED

    def set_selected_pair(self, pair_id: int) -> None:
        """Cambia el color de la fila seleccionada (p. ej. por tipo de lista)."""
        self.selected_pair = pair_id

    def set_items(
        self,
        items: list[str],
        subitems: list[str] | None = None,
        keep_selection: bool = False,
    ) -> None:
        """Fija las filas y, si se pasan, sus textos secundarios.

        `subitems` es *paralelo* a `items`: un texto por fila, en el mismo
        orden. Si no viene, o si no cuadra con el número de filas, la lista se
        dibuja a una columna: degradar es mejor que pintar la segunda columna
        descolocada o leer un índice que no existe.
        """
        self.items = items
        if subitems is not None and len(subitems) == len(items):
            self.subitems = list(subitems)
        else:
            self.subitems = []
        if not keep_selection or self.selected >= len(items):
            self.selected = 0
        self.top = 0
        self.clamp()

    def clamp(self, height: int | None = None) -> None:
        if not self.items:
            self.selected = 0
            self.top = 0
            return
        self.selected = max(0, min(self.selected, len(self.items) - 1))
        if height is None:
            # Sin viewport conocido: solo asegura que top esté en rango
            self.top = max(0, min(self.top, max(0, len(self.items) - 1)))
            if self.selected < self.top:
                self.top = self.selected
            return
        height = max(1, height)
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + height:
            self.top = self.selected - height + 1
        # clamp top a rango válido
        max_top = max(0, len(self.items) - height)
        self.top = max(0, min(self.top, max_top))

    def visible_rows(self, height: int) -> int:
        return max(0, height)

    def clamp_for(self, height: int) -> None:
        """Alias explícito para clamp con viewport."""
        self.clamp(height)

    def handle_key(self, key: int, rows: int) -> bool:
        """Procesa teclas de navegación. True si la tecla era suya."""
        if key in (curses.KEY_UP, ord("k")):
            self.selected -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected += 1
        elif key in (curses.KEY_PPAGE,):
            self.selected -= max(1, rows - 1)
        elif key in (curses.KEY_NPAGE,):
            self.selected += max(1, rows - 1)
        elif key in (curses.KEY_HOME, ord("g")):
            self.selected = 0
        elif key in (curses.KEY_END, ord("G")):
            self.selected = len(self.items) - 1
        else:
            return False
        self.clamp(rows)
        return True

    def _scrollbar_thumb(self, height: int) -> tuple[int, int] | None:
        """Calcula (top_offset, thumb_h) del scrollbar si hay overflow."""
        if not self.items or len(self.items) <= height or height <= 2:
            return None
        thumb_h = max(1, height * height // len(self.items))
        # clamp estético
        if thumb_h < 1:
            thumb_h = 1
        max_top = len(self.items) - height
        if max_top <= 0:
            return (0, thumb_h)
        thumb_top = int(self.top * (height - thumb_h) / max_top)
        return (thumb_top, thumb_h)

    def _two_columns(self) -> bool:
        """True si esta lista se dibuja con su columna secundaria.

        Sólo con subelementos no vacíos **y** uno por fila: es la bandera que
        deja intacto el render de las otras nueve pantallas.
        """
        return bool(self.subitems) and len(self.subitems) == len(self.items)

    def _render_scrollbar(self, stdscr: curses.window, line_y: int, scroll_x: int,
                          rows: int, i: int) -> None:
        """Dibuja el scrollbar en su columna reservada (una celda)."""
        thumb = self._scrollbar_thumb(rows)
        if thumb is None:
            stdscr.addstr(line_y, scroll_x, "│",
                          colors.pair(colors.PAIR_DIM) | curses.A_DIM)
            return
        t_top, t_h = thumb
        is_thumb = t_top <= i < t_top + t_h
        ch = "█" if is_thumb else "│"
        sa = (
            (colors.pair(colors.PAIR_SCROLLBAR) | curses.A_BOLD)
            if is_thumb
            else (colors.pair(colors.PAIR_DIM) | curses.A_DIM)
        )
        stdscr.addstr(line_y, scroll_x, ch, sa)

    def _render_row_two_columns(self, stdscr: curses.window, line_y: int, x: int,  # noqa: ANN001
                                content_w: int, idx: int, selected: bool,
                                has_scroll: bool, rows: int, i: int) -> None:
        """Pinta una fila en dos columnas: el canal y su texto secundario.

        El canal va en `PAIR_NORMAL` y lo secundario en `PAIR_DIM` —el dato
        complementario no debe competir con el nombre— y la fila seleccionada lo
        lleva entero en el par de selección: es lo que hace que se lea como una
        sola fila y no como dos textos sueltos.

        Todo se mide y se recorta en celdas (`clip_cells`), nunca con `len()`: un
        nombre con emoji no puede empujar la segunda columna ni desbordar la
        línea. La segunda columna empieza donde acaba la primera porque la
        primera llega ya rellenada con su hueco (`favorite_rows.layout_columns`):
        aquí no hace falta saber el ancho de ninguna columna.
        """
        prefix = self.PREFIX_SELECTED if selected else self.PREFIX_PLAIN
        attr = colors.pair(self.selected_pair if selected else colors.PAIR_NORMAL)
        sub_attr = attr if selected else colors.pair(colors.PAIR_DIM)

        principal = clip_cells(self.items[idx], max(0, content_w - cell_width(prefix)))
        cabeza = f"{prefix}{principal}"
        ancho_cabeza = cell_width(cabeza)
        secundario = ""
        if ancho_cabeza < content_w:
            secundario = clip_cells(self.subitems[idx], content_w - ancho_cabeza)
        stdscr.addstr(line_y, x, _pad_cells(cabeza, content_w), attr)
        if secundario:
            stdscr.addstr(line_y, x + ancho_cabeza,
                          _pad_cells(secundario, content_w - ancho_cabeza), sub_attr)
        if has_scroll:
            self._render_scrollbar(stdscr, line_y, x + content_w, rows, i)

    def _render_two_columns(self, stdscr: curses.window, y: int, x: int,  # noqa: ANN001
                            height: int, width: int) -> None:
        """Render de dos columnas: el de Favoritos con el programa actual."""
        rows = max(1, height)
        for i in range(rows):
            idx = self.top + i
            line_y = y + i
            if line_y >= stdscr.getmaxyx()[0]:
                break
            if idx >= len(self.items):
                continue
            # Dejar 1 col para scrollbar si hay overflow
            has_scroll = len(self.items) > rows and width > 8
            content_w = width - 1 - (1 if has_scroll else 0)
            try:
                self._render_row_two_columns(
                    stdscr, line_y, x, content_w, idx, idx == self.selected,
                    has_scroll, rows, i,
                )
            except curses.error:
                pass  # última celda de la esquina inferior derecha

    def render(self, stdscr: curses.window, y: int, x: int, height: int, width: int) -> None:
        self.clamp(height)
        if self._two_columns():
            self._render_two_columns(stdscr, y, x, height, width)
            return
        rows = max(1, height)
        for i in range(rows):
            idx = self.top + i
            line_y = y + i
            if line_y >= stdscr.getmaxyx()[0]:
                break
            if idx >= len(self.items):
                continue
            selected = idx == self.selected
            prefix = self.PREFIX_SELECTED if selected else self.PREFIX_PLAIN
            attr = colors.pair(self.selected_pair if selected else colors.PAIR_NORMAL)
            # Dejar 1 col para scrollbar si hay overflow
            has_scroll = len(self.items) > rows and width > 8
            content_w = width - 1 - (1 if has_scroll else 0)
            # Recorte y relleno en **celdas**, nunca en caracteres (§42, §43).
            # Contando caracteres, un nombre de Asia oriental o con emoji deja
            # la fila más ancha que la ventana: se come el scrollbar y, en la
            # esquina inferior derecha, `curses.error`. Para texto de una celda
            # por carácter —todo lo que había antes— el resultado es idéntico.
            text = clip_cells(self.items[idx], max(0, content_w - cell_width(prefix)))
            try:
                stdscr.addstr(line_y, x, _pad_cells(f"{prefix}{text}", content_w), attr)
                if has_scroll:
                    self._render_scrollbar(stdscr, line_y, x + content_w, rows, i)
            except curses.error:
                pass  # última celda de la esquina inferior derecha


def render_separator(stdscr: curses.window, y: int, width: int) -> None:
    """Dibuja una línea separadora horizontal con box-drawing chars."""
    try:
        line = "─" * max(0, width - 1)
        stdscr.addstr(y, 0, line, colors.pair(colors.PAIR_SEPARATOR))
    except curses.error:
        pass


def render_title_bar(stdscr: curses.window, title: str, stack_depth: int = 1) -> None:
    """Renderiza la barra de título con borde superior e indicador de profundidad."""
    max_y, max_x = stdscr.getmaxyx()
    # Indicador de profundidad del stack (breadcrumb visual)
    depth_prefix = ""
    if stack_depth > 1:
        depth_prefix = "◂ " * min(stack_depth - 1, 5) + " "
    full_title = f" {depth_prefix}{title} "
    # Rellenar con líneas de borde
    title_width = len(full_title)
    remaining = max_x - title_width
    left_border = "─" * max(0, remaining // 2)
    right_border = "─" * max(0, remaining - len(left_border))
    line = f"{left_border}{full_title}{right_border}"
    try:
        stdscr.addstr(0, 0, line[:max_x], colors.pair(colors.PAIR_TITLE))
    except curses.error:
        pass


class StatusBar:
    """Barra inferior: atajos contextuales a la izquierda, mensaje a la derecha."""

    def __init__(self) -> None:
        self.shortcuts = ""
        self.message = ""
        self.is_error = False
        self._flash_until = 0.0
        # Escalado de errores a modal (no negociable #1). Se inyecta desde
        # App; None fuera de la TUI o en tests.
        self.on_error: Any = None

    def set(self, shortcuts: str) -> None:
        self.shortcuts = shortcuts

    def show(self, message: str, error: bool = False, *, modal: bool = True) -> None:
        """Muestra `message`; si es error, escala a modal (no negociable #1).

        ``modal=False`` es para el **único** sitio que abre su propio modal de
        error: :meth:`thetvview.ui.app.App.show_error`. Escalar también ahí
        significaría abrir dos modales —el bueno, con `R`/`D`, y detrás un
        `Modal("Error", cuerpo)` genérico sin acciones—, y el segundo taparía al
        primero. El parámetro existe para que esa decisión sea explícita y no
        un efecto lateral: por defecto la barra sigue escalando sola, que es lo
        que hacen los ~15 ``status.show(…, error=True)`` de las pantallas.
        """
        import time

        from ..security.redaction import redact_text

        # Frontera de la UI: aunque un mensaje traiga una URL con
        # credenciales, aquí sale redactada (SDD §2.2, B10).
        message = redact_text(message)
        self.message = message
        self.is_error = error
        self._flash_until = time.monotonic() + (4.0 if message else 0.0)
        if error and modal and message and self.on_error is not None:
            self.on_error("Error", message)

    def _current_message(self) -> str:
        import time

        if time.monotonic() > self._flash_until:
            self.message = ""
            self.is_error = False
        return self.message

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if max_y < 2:
            return
        # Línea separadora arriba de la status bar
        separator_y = max_y - 2
        try:
            stdscr.addstr(separator_y, 0, "─" * (max_x - 1), colors.pair(colors.PAIR_SEPARATOR))
        except curses.error:
            pass
        # Barra de estado
        row = max_y - 1
        width = max_x - 1
        msg = f" {self._current_message()} " if self._current_message() else ""
        left = self.shortcuts[: max(0, width - len(msg))]
        text = f" {left}".ljust(width)
        if msg:
            text = text[: width - len(msg)] + msg
        attr = (
            colors.pair(colors.PAIR_ERROR)
            if self.is_error and self.message
            else colors.pair(colors.PAIR_STATUS)
        )
        try:
            stdscr.addstr(row, 0, text[:width], attr)
        except curses.error:
            pass


def render_status_bar(stdscr: curses.window, shortcuts: str, message: str = "", is_error: bool = False) -> None:
    """Renderiza la barra de estado con borde superior."""
    max_y, max_x = stdscr.getmaxyx()
    if max_y < 2:
        return
    separator_y = max_y - 2
    try:
        stdscr.addstr(separator_y, 0, "─" * (max_x - 1), colors.pair(colors.PAIR_SEPARATOR))
    except curses.error:
        pass
    row = max_y - 1
    width = max_x - 1
    msg = f" {message} " if message else ""
    left = shortcuts[: max(0, width - len(msg))]
    text = f" {left}".ljust(width)
    if msg:
        text = text[: width - len(msg)] + msg
    attr = (
        colors.pair(colors.PAIR_ERROR)
        if is_error and message
        else colors.pair(colors.PAIR_STATUS)
    )
    try:
        stdscr.addstr(row, 0, text[:width], attr)
    except curses.error:
        pass


# =========================================================================
# Widgets modernos (F1 — app chrome)
# =========================================================================


class HeaderBar:
    """Cabecera consistente: identidad a la izquierda, contexto a la derecha.

    La pantalla dice **qué** contexto se ve (`Screen.header_context()`); este
    widget decide **cómo** se dibuja y no sabe nada más: ni de qué pantalla
    viene, ni de qué datos hay cargados. Todo lo que necesita le llega como
    argumento, así que aquí dentro no hay ni una comparación de tipos ni una
    referencia al dominio (§31/§35); hay un test que lo vigila para siempre.

    Todo se mide con `cell_width()` de `textwidth`, nunca con `len()`: `★ ▶ ◈`
    son *East Asian Ambiguous* y un emoji ocupa 2 celdas, así que medir en
    caracteres desalinearía la línea entera (§17).

    Degradación cuando falta ancho, por este orden (§16, §18):

    1. si caben logo + contexto + tema → relleno `─` y contexto pegado a la
       derecha (**nunca centrado**, §10);
    2. si no → cae el indicador de tema (es lo último en caer: es secundario);
    3. si no → se trunca el contexto por el final con `…`;
    4. si no → sólo la identidad, recortada por celdas y nunca partida.
    """

    def __init__(self, app: Any = None) -> None:
        self.app = app

    @staticmethod
    def _pad_cells(s: str, max_x: int) -> str:
        """Rellena con espacios hasta `max_x` **celdas** (nunca con `len()`)."""
        return s + " " * max(0, max_x - cell_width(s))

    def render(self, stdscr: curses.window, title: str, stack_depth: int = 1) -> None:
        """Pinta la cabecera.

        `stack_depth` se conserva en la firma por compatibilidad con los dobles
        de test (`FakeHeader`) y con los call sites de `App`, pero ya no se usa
        para pintar breadcrumbs: la cabecera es un identificador de contexto y
        la profundidad de navegación ya se anuncia en el footer con `Esc` (§13).
        """
        max_y, max_x = stdscr.getmaxyx()
        if max_y < 1 or max_x < 12:
            return

        theme_name = getattr(self.app, "theme_name", "light") if self.app else "light"
        theme = (
            f" {icons.ICON_THEME_DARK} "
            if theme_name == "dark"
            else f" {icons.ICON_THEME_LIGHT} "
        )
        logo = f" {icons.ICON_TV} theTVVIEW "
        ctx = (title or "").strip()

        w_logo = cell_width(logo)
        w_theme = cell_width(theme)

        # --- ¿cabe todo? --------------------------------------------------
        with_theme = True
        avail = max_x - w_logo - w_theme
        if ctx and cell_width(ctx) > avail:
            # Segundo en caer: el tema (elemento secundario, §16).
            with_theme = False
            avail = max_x - w_logo
            if cell_width(ctx) > avail:
                # Tercero: el contexto, truncado por el final con "…" (§18).
                ctx = clip_cells(ctx, avail - 1) + "…"

        if not ctx:
            # Sin contexto (o sin ancho para él): la identidad manda y el
            # espacio sobrante se rellena, nunca se recorta el logo (§16).
            line = self._pad_cells(clip_cells(logo, max_x), max_x)
            try:
                stdscr.addstr(0, 0, line, colors.pair(colors.PAIR_TITLE) | curses.A_BOLD)
                stdscr.addstr(0, 0, clip_cells(logo, max_x), colors.pair(colors.PAIR_ACCENT) | curses.A_BOLD)
            except curses.error:
                pass
            return

        used = cell_width(ctx) + (w_theme if with_theme else 0)
        fill = "─" * max(0, max_x - w_logo - used)
        line = f"{logo}{fill}{ctx}" + (theme if with_theme else "")
        # La suma de celdas da exactamente `max_x` en el camino normal; el
        # `clip_cells` es la red de seguridad para anchos extremos (§18).
        line = self._pad_cells(clip_cells(line, max_x), max_x)

        try:
            # Banda sólida: PAIR_TITLE ocupa toda la fila con BOLD.
            stdscr.addstr(0, 0, line, colors.pair(colors.PAIR_TITLE) | curses.A_BOLD)
            # Logo resaltado por encima en accent.
            stdscr.addstr(0, 0, clip_cells(logo, max_x), colors.pair(colors.PAIR_ACCENT) | curses.A_BOLD)
            if with_theme:
                theme_x = max_x - w_theme
                if 0 <= theme_x < max_x:
                    stdscr.addstr(0, theme_x, theme, colors.pair(colors.PAIR_DIM))
        except curses.error:
            pass


class FooterBar:
    """Barra inferior moderna con chips de atajos contextuales.

    **Fuente única**: el catálogo de `Action` de la pantalla entra por
    `set_actions` y el ajuste por prioridad/anchoraje lo decide
    `actions.fit_actions`. Este widget sólo traduce a texto y pinta; no decide
    qué se muestra (§67: nada de lógica de presentación en el componente).

    `chips` sigue siendo la lista `(key, label)` que leen los lectores
    actuales, y se recalcula en cada `render` a partir de las acciones que
    caben de verdad en el ancho disponible.
    """

    def __init__(self) -> None:
        self.chips: list[tuple[str, str]] = []  # (key, label)
        self.message: str = ""
        self.is_error: bool = False
        self._flash_until: float = 0.0
        self._flash_duration: float = 4.0
        #: Catálogo declarado por la pantalla (SDD §10). Vacío = usar `chips`.
        self.actions: list[Action] = []
        #: Acciones habilitadas que no cupieron: se marca con "…" (§45).
        self.overflow: bool = False

    def set_chips(self, *chips: tuple[str, str]) -> None:
        self.chips = list(chips)
        self.actions = []

    def set_actions(self, actions: list[Action]) -> None:
        """Declara el catálogo de la pantalla (§63).

        No ajusta aquí: el ancho real sólo se conoce en `render`. A partir de
        este momento `chips` refleja lo que *de verdad* cabe, porque se
        recalcula en cada frame.
        """
        self.actions = list(actions)

    def show(self, message: str, error: bool = False, duration: float = 4.0) -> None:
        self.message = message
        self.is_error = error
        self._flash_until = time.monotonic() + (duration if message else 0.0)

    def _current_message(self) -> str:
        if time.monotonic() > self._flash_until:
            self.message = ""
            self.is_error = False
        return self.message

    # -- pieces of the line ------------------------------------------------
    # Todo se mide con `cell_width` (§43): `len()` no es una medida, y esta
    # línea lleva `★ ▶ ← →` que son *Ambiguous*.

    def _fit_actions_for(self, avail: int) -> list[Action]:
        """Qué acciones caben en ``avail`` celdas, y deja `chips` al día."""
        if not self.actions:
            return []
        habilitadas = [a for a in self.actions if a.enabled]
        ajustadas = fit_actions(self.actions, avail)
        self.chips = [(a.key, a.label) for a in ajustadas]
        self.overflow = len(ajustadas) < len(habilitadas)
        return ajustadas

    def _fit_legacy_chips(self, avail: int) -> list[str]:
        """Chips heredados (``chips`` a pelo), medidos con `cell_width`.

        Se mantiene el camino viejo para las pantallas que aún no tienen
        catálogo y para los dobles de test. Igual que antes, primer-que-llega
        y sin partir etiquetas: si el primer chip no cabe, no se dibuja ninguno
        (§46).
        """
        partes: list[str] = []
        usado = LEADING
        overflow = False
        for key, label in self.chips:
            chunk = f" {key} {label} "
            need = cell_width(chunk) + (cell_width(SEPARATOR) if partes else 0)
            if usado + need <= avail:
                partes.append(chunk)
                usado += need
            else:
                overflow = True
                break
        self.overflow = overflow and bool(partes)
        return partes

    def _segments(self, fitted: list[Action], avail: int) -> list[tuple[str, int]]:
        """Segmentos `(texto, attr)` de la línea, sin el mensaje.

        Clave en `PAIR_PRIMARY|A_BOLD` y etiqueta en `PAIR_MUTED` (§24): el
        contraste entre la tecla y su etiqueta es lo que hace legible la barra
        sin gastar un solo par de color nuevo (R-06).
        """
        attr_key = colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD
        attr_label = colors.pair(colors.PAIR_MUTED)
        attr_sep = colors.pair(colors.PAIR_MUTED)
        attr_filler = colors.pair(colors.PAIR_STATUS)

        segs: list[tuple[str, int]] = [(" ", attr_filler)]
        usado = LEADING
        for i, action in enumerate(fitted):
            if i:
                segs.append((SEPARATOR, attr_sep))
                usado += cell_width(SEPARATOR)
            segs.append((f" {action.key}", attr_key))
            usado += cell_width(f" {action.key}")
            segs.append((f" {action.label} ", attr_label))
            usado += cell_width(f" {action.label} ")
        # "…" marca que quedan acciones fuera (§45): nunca una etiqueta partida.
        self._cierre(segs, usado, avail)
        return segs

    def _cierre(self, segs: list[tuple[str, int]], usado: int, avail: int) -> None:
        """Cierra la línea: "…" si sobran acciones y relleno hasta el ancho."""
        if self.overflow and usado + 1 <= avail:
            segs.append(("…", colors.pair(colors.PAIR_MUTED)))
            usado += 1
        if usado < avail:
            segs.append((" " * (avail - usado), colors.pair(colors.PAIR_STATUS)))

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if max_y < 2:
            return
        sep_y = max_y - 2
        row = max_y - 1
        width = max_x - 1
        if width <= 0:
            return

        # Separador superior
        try:
            stdscr.addstr(sep_y, 0, "─" * width, colors.pair(colors.PAIR_BORDER))
        except curses.error:
            pass

        msg = self._current_message()
        msg_str = f" {msg} " if msg else ""
        # El toast conserva su prioridad absoluta: se reserva su ancho real.
        msg_cells = cell_width(msg_str)
        avail = max(0, width - msg_cells)

        if self.actions:
            segs = self._segments(self._fit_actions_for(avail), avail)
        elif self.chips:
            attr_chip = colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD
            partes = self._fit_legacy_chips(avail)
            segs = [(" ", colors.pair(colors.PAIR_STATUS))]
            segs += [(parte, attr_chip) for parte in partes]
            self._cierre(segs, LEADING + sum(cell_width(p) for p in partes), avail)
        else:
            segs = [(" " * avail, colors.pair(colors.PAIR_STATUS))]

        attr_msg = (
            colors.pair(colors.PAIR_DANGER)
            if (msg and self.is_error)
            else colors.pair(colors.PAIR_SUCCESS)
        )
        x = 0
        for texto, attr in segs:
            if x >= width:
                break
            troceado = clip_cells(texto, width - x)
            if not troceado:
                break  # no queda hueco: nada que escribir (§39)
            try:
                stdscr.addstr(row, x, troceado, attr)
            except curses.error:
                return
            x += cell_width(troceado)
        if msg_str and x < width:
            try:
                stdscr.addstr(row, x, clip_cells(msg_str, width - x), attr_msg)
            except curses.error:
                pass


class ToastStack:
    """Toasts apilados (notificaciones temporales) en la esquina superior derecha."""

    def __init__(self, max_visible: int = 3) -> None:
        self.max_visible = max_visible
        self._toasts: list[tuple[str, str, float]] = []  # (msg, level, until)

    def push(self, message: str, level: str = "info", duration: float = 4.0) -> None:
        """level: 'info', 'success', 'warning', 'error'."""
        until = time.monotonic() + duration
        self._toasts.append((message, level, until))

    def _cleanup(self) -> None:
        now = time.monotonic()
        self._toasts = [(m, l, u) for m, l, u in self._toasts if now < u]

    def render(self, stdscr: curses.window) -> None:
        self._cleanup()
        max_y, max_x = stdscr.getmaxyx()
        visible = self._toasts[-self.max_visible:]
        for i, (msg, level, _) in enumerate(visible):
            y = 1 + i
            if y >= max_y - 2:
                break
            # Icono por nivel
            icon = {"info": "●", "success": "✔", "warning": "⚠", "error": "✘"}.get(level, "●")
            text = f" {icon} {msg} "
            x = max(0, max_x - len(text) - 1)
            # Fondo del toast
            pair_map = {
                "info": colors.PAIR_ACCENT,
                "success": colors.PAIR_SUCCESS,
                "warning": colors.PAIR_WARNING,
                "error": colors.PAIR_DANGER,
            }
            attr = colors.pair(pair_map.get(level, colors.PAIR_ACCENT))
            try:
                stdscr.addstr(y, x, text[:max(0, max_x - x - 1)], attr)
            except curses.error:
                pass


class SearchBar:
    """Barra de búsqueda persistente con icono y contador."""

    def __init__(self) -> None:
        self.query: str = ""
        self.active: bool = False
        self.total: int = 0
        self.matched: int = 0

    def set_counts(self, total: int, matched: int) -> None:
        self.total = total
        self.matched = matched

    def render(self, stdscr: curses.window, y: int, width: int) -> None:
        if not self.active and not self.query:
            return
        # Icono + query + cursor
        cursor = "▌" if self.active else ""
        icon = f" {icons.ICON_SEARCH} "
        counter = f" {self.matched}/{self.total} " if self.query else ""
        inner_w = max(0, width - len(icon) - len(counter) - 2)
        query_part = self.query[:inner_w] + cursor
        line = f"{icon}{query_part}{' ' * max(0, inner_w - len(query_part) + len(query_part))}{counter}"

        pair = colors.PAIR_SEARCH if self.active else colors.PAIR_STATUS
        attr = colors.pair(pair)
        try:
            stdscr.addstr(y, 0, line[:width], attr)
        except curses.error:
            pass


class SearchModal:
    """Modal centrado para búsqueda incremental (canales y grupos).

    Sustituye a la antigua barra superior: al pulsar ``/`` el campo de
    texto aparece en un diálogo centrado con borde doble + sombra, con
    contador live ``coincidencias/total`` y pista de teclas.

    La lógica de filtrado sigue viviendo en la pantalla
    (``query``/``searching``/``_apply_filter``); este widget solo dibuja.
    Uso::

        SearchModal.render(stdscr, title="Buscar canal",
                           query=screen.query, matched=len(screen.visible_idx),
                           total=len(screen.channels))

    Todo addstr está protegido contra curses.error y terminales pequeñas.
    """

    #: Texto de la pista cuando se pasa `hint` a mano. Antes era también el
    #: valor por defecto; ahora lo es `actions()`, para no tener dos caminos.
    DEFAULT_HINT = "Enter confirmar · Esc limpiar · Ctrl-U borrar"

    def __init__(self, title: str = "Buscar", hint: str | None = None) -> None:
        self.title = title
        #: `hint` explícito del caller. Si no, la pista sale de `actions()`.
        self.hint = hint
        #: Mismo `Action` que el pie de `Modal` (§35): un solo camino, no dos.
        self.actions: list[Action] = [
            Action("Enter", V.CONFIRMAR, P.PRIMARIA, essential=True),
            Action("Ctrl-U", V.VACIAR, P.FRECUENTE),
            Action("Esc", V.LIMPIAR, P.NAVEGACION, essential=True),
        ]

    def hint_line(self, avail: int = 10_000) -> str:
        """La pista de teclas: el `hint` del caller, o la derivada de `actions()`.

        Antes era una constante de texto; ahora sale del mismo catálogo que el
        pie de `Modal` —misma tecla, mismo vocabulario cerrado, mismo ajuste por
        anchura— porque mantener las dos versiones es exactamente la deriva que
        el modelo de acciones viene a cerrar.
        """
        if self.hint is not None:
            return self.hint
        ajustadas = fit_actions(self.actions, avail)
        if not ajustadas:
            return ""
        return " · ".join(a.chip() for a in ajustadas)

    @staticmethod
    def visible_query(query: str, avail: int) -> str:
        """Cola visible del query cuando excede el ancho (pura, testeable)."""
        if avail <= 0:
            return ""
        if len(query) <= avail:
            return query
        return query[-avail:]

    @staticmethod
    def format_counter(matched: int, total: int) -> str:
        """Texto del contador live (puro, testeable)."""
        return f" {matched}/{total} "

    def calc_rect(self, max_y: int, max_x: int) -> tuple[int, int, int, int]:
        """Calcula (y, x, h, w) del modal centrado."""
        w = max(30, min(max_x - 4, 56))
        h = 7
        # Si la terminal es muy baja, compactar a 6 filas.
        if max_y < 12:
            h = 6
        y = max(1, (max_y - h) // 2)
        x = max(0, (max_x - w) // 2)
        return y, x, h, w

    def cursor_pos(self, max_y: int, max_x: int, query: str) -> tuple[int, int] | None:
        """Posición del cursor hardware dentro del campo (o None)."""
        y, x, h, w = self.calc_rect(max_y, max_x)
        field_w = max(4, w - 8)
        visible = self.visible_query(query, field_w)
        row = y + 2
        col = x + 4 + len(f"{icons.ICON_SEARCH} ") + len(visible)
        if row >= max_y - 1 or col >= max_x - 1:
            return None
        return row, col

    def render(
        self,
        stdscr: curses.window,
        query: str = "",
        matched: int = 0,
        total: int = 0,
    ) -> tuple[int, int] | None:
        """Dibuja el modal y devuelve la posición del cursor hardware."""
        max_y, max_x = stdscr.getmaxyx()
        if max_y < 8 or max_x < 30:
            return None
        y, x, h, w = self.calc_rect(max_y, max_x)

        # Sombra (1 fila/col offset, dim) — mismo lenguaje que Modal/FormModal.
        try:
            for sy in range(y + 1, min(y + h + 1, max_y - 1)):
                stdscr.addstr(sy, min(x + 1, max_x - 1),
                              " " * max(0, min(w, max_x - x - 2)),
                              colors.pair(colors.PAIR_MUTED) | curses.A_DIM)
        except curses.error:
            pass

        # Borde doble.
        border_attr = colors.pair(colors.PAIR_MODAL_BORDER)
        try:
            stdscr.addstr(y, x, icons.BOX_D_TL + icons.BOX_D_H * (w - 2) + icons.BOX_D_TR, border_attr)
            for row in range(1, h - 1):
                stdscr.addstr(y + row, x, icons.BOX_D_V, border_attr)
                stdscr.addstr(y + row, x + w - 1, icons.BOX_D_V, border_attr)
            stdscr.addstr(y + h - 1, x, icons.BOX_D_BL + icons.BOX_D_H * (w - 2) + icons.BOX_D_BR, border_attr)
        except curses.error:
            pass

        # Título centrado en el borde superior.
        title_str = f" {self.title} "
        tx = x + max(0, (w - len(title_str)) // 2)
        try:
            stdscr.addstr(y, tx, title_str[:max(0, w - 2)],
                          colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
        except curses.error:
            pass

        # Campo de texto: " ⌕ <query>▌ ".
        field_w = max(4, w - 8)
        visible = self.visible_query(query, field_w)
        cursor = "▌"
        icon = f"{icons.ICON_SEARCH} "
        line = f"{icon}{visible}{cursor}"
        try:
            stdscr.addstr(y + 2, x + 3, line[:max(0, w - 6)],
                          colors.pair(colors.PAIR_SEARCH) | curses.A_BOLD)
        except curses.error:
            pass

        # Contador live alineado a la derecha de la fila siguiente.
        counter = self.format_counter(matched, total)
        crow = y + 3
        if crow < y + h - 1:
            try:
                stdscr.addstr(crow, x + max(2, w - len(counter) - 2),
                              counter[:max(0, w - 4)],
                              colors.pair(colors.PAIR_STATUS))
            except curses.error:
                pass

        # Pista de teclas (tenue). Viene de `actions()`, medido con cell_width;
        # en terminal compacta se omite si no cabe (§46: nunca se parte).
        hrow = y + 4
        if hrow < y + h - 1:
            pista = self.hint_line()
            try:
                stdscr.addstr(hrow, x + 2, clip_cells(pista, max(0, w - 4)),
                              colors.pair(colors.PAIR_DIM) | curses.A_DIM)
            except curses.error:
                pass

        return self.cursor_pos(max_y, max_x, query)

    @staticmethod
    def render_modal(
        stdscr: curses.window,
        *,
        title: str = "Buscar",
        query: str = "",
        matched: int = 0,
        total: int = 0,
        hint: str | None = None,
    ) -> tuple[int, int] | None:
        """Atajo funcional sin instanciar (para pantallas y tests)."""
        modal = SearchModal(title=title, hint=hint)
        return modal.render(stdscr, query=query, matched=matched, total=total)


def modal_actions(
    *,
    confirmar: str = V.CONFIRMAR,
    cancelar: str = V.CANCELAR,
) -> list[Action]:
    """`Enter <confirmar> · Esc <cancelar>`: el pie por defecto de un modal (§35).

    Vive fuera de `Modal` a propósito —y no como método estático— porque quien
    lo usa tiene el `Modal` delante (o un doble de test que no tiene por qué
    saber de esta API). El verbo se pasa porque «Enter Guardar» y «Enter
    Eliminar» no son la misma acción, pero la tecla y la forma del pie sí.
    """
    return [
        Action("Enter", confirmar, P.PRIMARIA, essential=True),
        Action("Esc", cancelar, P.NAVEGACION, essential=True),
    ]


class Modal:
    """Diálogo modal centrado con borde doble, sombra, botones y pie de acciones.

    El **pie de acciones** (`actions`) pinta `Enter Confirmar · Esc Cancelar` en
    el borde inferior (SDD §35): mientras hay un modal abierto la barra de la
    pantalla padre queda tapada y el usuario necesita saber qué teclas valen
    *aquí dentro*, no en la pantalla de detrás. Es además el sitio donde vive
    la configuración de la app, que son ajustes y modales, no una pantalla.

    El catálogo es el mismo `Action` que usa el pie (`ui.actions`), así que no
    hay un segundo camino: la etiqueta sale del vocabulario cerrado y el ancho
    se mide con `cell_width`.

    Sin campos de texto: para escribir algo está `FormModal`, que es lo que se
    usa de verdad. Aquí una caja de input sería una segunda forma de lo mismo,
    y una que además nadie ha activado nunca.

    **Dos extensiones opt-in para el modal de error** (SDD de errores §6, §12,
    §13): ``wrap`` para que un mensaje largo se parta en vez de recortarse, y
    ``keys`` para que un atajo anunciado en el pie tenga una tecla real detrás.
    Ambas con default de «no hacer nada»: los veinte modales existentes se
    comportan exactamente igual que antes de que existieran.
    """

    def __init__(
        self,
        title: str,
        message: str,
        buttons: list[str] | None = None,
        actions: list[Action] | None = None,
        *,
        wrap: bool = False,
        keys: dict[int, str] | None = None,
    ) -> None:
        """
        Args:
            wrap: envuelve el mensaje al ancho disponible en vez de recortarlo
                con `clip_cells`. **Opt-in** (por defecto ``False``): hay veinte
                modales que dependen del comportamiento actual y un recorte
                distinto en alguno de ellos sería un fallo de maquetación, no una
                mejora.
            keys: atajos adicionales ``{ord("R"): "retry"}``. Se consultan
                **antes** que la navegación de botones, y es lo que hace que un
                atajo anunciado en el pie tenga una tecla real detrás (§13):
                sin este diccionario el pie podría mentir.
        """
        self.title = title
        self.message = message
        self.buttons = buttons or ["Aceptar"]
        self.selected_button: int = 0
        #: Pie de acciones (§35). Vacío = sin pie (comportamiento anterior).
        self.actions: list[Action] = list(actions or [])
        #: Opt-in: ver el docstring del parámetro ``wrap``.
        self.wrap = bool(wrap)
        #: Opt-in: ver el docstring del parámetro ``keys``.
        self.keys: dict[int, str] = dict(keys or {})

    def _lines(self, inner_w: int | None = None) -> list[str]:
        """El mensaje tal y como se dibuja, ya envuelto si toca.

        Con ``wrap=False`` es un ``split("\\n")`` y por tanto **exactamente** lo
        que se dibujaba antes: es el camino que usan los veinte modales
        existentes. Con ``wrap=True`` delega en :func:`wrap_block`, que ya
        existe y ya mide con `cell_width` —no se escribe un segundo envolvedor.
        """
        if not self.wrap:
            return self.message.split("\n")
        return wrap_block(self.message, max(1, (inner_w or self._natural_w()) - 4))

    def _natural_w(self) -> int:
        """Ancho que tendría el modal sin envolver: el de antes."""
        lines = self.message.split("\n")
        btn_line = "  ".join(
            f"{'>' if i == self.selected_button else ' '} {b} "
            for i, b in enumerate(self.buttons)
        )
        return max(
            cell_width(self.title) + 4,
            max((cell_width(line) for line in lines), default=0) + 4,
            cell_width(btn_line) + 4,
            30,
        )

    def _layout(self, max_y: int, max_x: int) -> tuple[list[str], int]:
        """``(líneas del mensaje, ancho interior)`` para una ventana dada.

        Vive aquí y no en :meth:`_calc_rect` porque **los dos** necesitan la
        misma respuesta: el que calcula el rectángulo (para saber cuántas filas
        reservar) y el que dibuja (para saber qué pintar en ellas). Calcularlo
        dos veces era la forma natural de que dejaran de cuadrar.

        Sin ancho fijo (§6): el ancho sale del contenido. Con ``wrap=True`` sale
        del bloque ya envuelto contra el ancho **disponible** —si se envolviera
        contra el ancho natural, el mensaje se partiría al tamaño que ya era
        demasiado grande y el modal acabaría más ancho que la terminal.
        """
        inner_w = self._natural_w()
        if not self.wrap:
            return self.message.split("\n"), inner_w
        disponible = max(12, int(max_x) - 8)
        lines = self._lines(min(inner_w, disponible))
        ancho = max((cell_width(line) for line in lines), default=0) + 4
        return lines, min(max(inner_w, ancho), disponible)

    def _actions_line(self, avail: int) -> str:
        """Texto del pie de acciones, ajustado al ancho real (`cell_width`).

        Nunca parte una etiqueta: si no cabe, se cae la menos prioritaria y, si
        aun así no cabe ninguna entera, no se pinta nada (§46).
        """
        if not self.actions or avail <= 0:
            return ""
        ajustadas = fit_actions(self.actions, avail)
        if not ajustadas:
            return ""
        return SEPARATOR.join(a.chip() for a in ajustadas)

    def _calc_rect(self, max_y: int, max_x: int) -> tuple[int, int, int, int]:
        """Calcula (y, x, h, w) del modal centrado.

        Sin ancho fijo (§6) y **sin alto fijo tampoco**: si el bloque envuelto
        no cabe en la ventana, el modal se queda en lo que le cabe y lo que
        sobra se deja de pintar, en vez de dibujar un borde por debajo de la
        pantalla. Sólo el ``wrap=True`` recorta —la terminal estrecha es el
        caso que el §12 exige)— y siempre por el final, que es donde el
        ``render`` tiene el botón y el pie: el título y las primeras líneas
        sobreviven, y las acciones nunca son lo que se pierde (§12).
        """
        lines, inner_w = self._layout(max_y, max_x)
        # El pie de acciones es contenido: el modal crece para que quepa entero
        # en vez de recortar «Enter Confirmar · Esc Cancelar» a media etiqueta
        # (§46). Si la terminal es más estrecha, ya lo ajustará `_actions_line`.
        if self.actions:
            pie_ancho = sum(a.chip_cells() + 1 for a in self.actions)
            inner_w = max(inner_w, pie_ancho + 4)
        inner_w = min(inner_w, max_x - 4)
        # +1 fila si hay pie de acciones: es una fila más que dibujar.
        filas_extra = 1 if self.actions else 0
        lineas = len(lines)
        if self.wrap:
            # Alto máximo **dentro de la ventana**: `y >= 1` siempre, así que
            # la última fila dibujable es `max_y - 1` y el alto no puede pasar
            # de `max_y - 1`; descontando los dos bordes, la fila del título (que
            # se pinta sobre el borde superior) y las de botones y pie queda
            # lo que ve. Sin esto, en una ventana baja el modal dibujaba su
            # borde por debajo de la última fila y el `addstr` de la esquina
            # lanzaba `curses.error` (§39).
            caben = max(1, int(max_y) - 6 - filas_extra)
            lineas = min(lineas, caben)
        inner_h = lineas + 3 + filas_extra
        total_h = inner_h + 2  # borders
        total_w = inner_w + 2
        y = max(1, (max_y - total_h) // 2)
        x = max(1, (max_x - total_w) // 2)
        return y, x, total_h, total_w

    def handle_key(self, key: int) -> str | None:
        """Devuelve el nombre del botón presionado o None.

        Los atajos de ``keys`` se consultan **primero**: una tecla que anuncia
        una acción del pie tiene que ejecutar esa acción, y no mueve el cursor
        entre botones ni se pierde en el ``None`` de «nada que hacer aquí»
        (§13). Por eso el orden no es un detalle: es la garantía.
        """
        if self.keys:
            accion = self.keys.get(key)
            if accion is not None:
                return accion
        # Navegación de botones
        if key in (curses.KEY_LEFT, ord("\t")):
            self.selected_button = (self.selected_button - 1) % len(self.buttons)
        elif key in (curses.KEY_RIGHT, ord("\t")):
            self.selected_button = (self.selected_button + 1) % len(self.buttons)
        elif key in (curses.KEY_ENTER, 10, 13):
            return self.buttons[self.selected_button].strip().lower()
        elif key in (27,):
            return "cancel"
        return None

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        # Ventana demasiado pequeña para un diálogo con borde: no se dibuja
        # nada. Pisar el borde con el título o los botones se ve peor que un
        # modal ausente, y además el `addstr` de la esquina lanzaría
        # `curses.error` (§39).
        if max_y < 7 or max_x < 20:
            return
        y, x, h, w = self._calc_rect(max_y, max_x)

        # Sombra (1 fila/col offset, dim)
        try:
            for sy in range(y + 1, min(y + h + 1, max_y - 1)):
                stdscr.addstr(sy, min(x + 1, max_x - 1),
                              " " * max(0, min(w, max_x - x - 2)),
                              colors.pair(colors.PAIR_MUTED) | curses.A_DIM)
        except curses.error:
            pass

        # Borde doble
        border_attr = colors.pair(colors.PAIR_MODAL_BORDER)
        try:
            stdscr.addstr(y, x, icons.BOX_D_TL + icons.BOX_D_H * (w - 2) + icons.BOX_D_TR, border_attr)
            for row in range(1, h - 1):
                stdscr.addstr(y + row, x, icons.BOX_D_V, border_attr)
                stdscr.addstr(y + row, x + w - 1, icons.BOX_D_V, border_attr)
            stdscr.addstr(y + h - 1, x, icons.BOX_D_BL + icons.BOX_D_H * (w - 2) + icons.BOX_D_BR, border_attr)
        except curses.error:
            pass

        # Título
        title_str = f" {self.title} "
        tx = x + max(0, (w - len(title_str)) // 2)
        try:
            stdscr.addstr(y, tx, title_str[:max(0, w - 2)], colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
        except curses.error:
            pass

        # Filas reservadas abajo: borde, botones y —si hay— pie de acciones.
        # El pie va justo debajo de los botones, y sólo se pinta si cabe entero
        # antes del borde inferior (§35, §39: terminal diminuta).
        fila_botones = min(y + h - 3 if self.actions else y + h - 2, max_y - 2)
        hay_fila_pie = self.actions and (y + h - 2) < max_y - 1
        limite = fila_botones

        # Mensaje
        lines, _inner = self._layout(max_y, max_x)
        for i, line in enumerate(lines):
            row = y + 1 + i
            if row >= limite:
                break
            lx = x + 2
            try:
                stdscr.addstr(row, lx, clip_cells(line, max(0, w - 4)),
                              colors.pair(colors.PAIR_NORMAL))
            except curses.error:
                pass

        # Botones — selected con BOLD|REVERSE, resto PRIMARY|BOLD
        btn_line = "  ".join(
            f"[{b}]" for b in self.buttons
        )
        bx = x + max(0, (w - cell_width(btn_line)) // 2)
        by = fila_botones
        try:
            offset = bx
            for i, b in enumerate(self.buttons):
                if offset >= max_x:
                    break  # ventana diminuta: no se escribe fuera (§39)
                txt = f"[{b}]"
                sep = "  " if i < len(self.buttons) - 1 else ""
                if i == self.selected_button:
                    attr = colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD | curses.A_REVERSE
                else:
                    attr = colors.pair(colors.PAIR_PRIMARY)
                stdscr.addstr(by, offset, clip_cells(txt + sep, max(0, w - (offset - x))), attr)
                offset += cell_width(txt) + cell_width(sep)
        except curses.error:
            pass

        # Pie de acciones (§35): qué teclas valen *aquí dentro*.
        if hay_fila_pie:
            pie = self._actions_line(max(0, w - 4))
            if pie:
                px = x + max(2, (w - cell_width(pie)) // 2)
                if px < max_x:
                    try:
                        stdscr.addstr(y + h - 2, px,
                                      clip_cells(pie, max(0, min(w - 4, max_x - 1 - px))),
                                      colors.pair(colors.PAIR_MUTED))
                    except curses.error:
                        pass


class FormModal:
    """Modal con varios campos de texto + botones (p. ej. Añadir playlist).

    Uso::

        modal = FormModal("Añadir playlist",
                          [("name", "Nombre"), ("source", "Ruta o URL")])
        # en el bucle de App:
        modal.render(stdscr)
        res = modal.handle_key(key)  # "ok" | "cancel" | None
        if res == "ok":
            data = modal.data()  # {"name": ..., "source": ...}

    - ``fields``: lista de ``(key, label)`` o ``(key, label, secret)``.
      ``secret`` enmascara con ``*`` (contraseñas).
    - ``initial``: valores precargados por key.
    - Navegación: ↑/↓ o Tab cambia de campo, ←/→ en botones,
      Enter en campo salta al siguiente (o a Guardar si es el último),
      Enter en [Guardar] devuelve "ok", Esc cancela.
    - Lógica pura en ``handle_key`` (testeable sin curses iniciado).
    """

    def __init__(
        self,
        title: str,
        fields: list[tuple] | None = None,
        buttons: list[str] | None = None,
        initial: dict[str, str] | None = None,
    ) -> None:
        self.title = title
        self.fields: list[tuple[str, str, bool]] = []
        for spec in fields or []:
            if len(spec) == 3:
                key, label, secret = spec
            else:
                key, label = spec[0], spec[1]
                secret = False
            self.fields.append((str(key), str(label), bool(secret)))
        self.buttons = buttons or ["Guardar", "Cancelar"]
        self.values: dict[str, str] = {
            key: str((initial or {}).get(key, "")) for key, _, _ in self.fields
        }
        self.focus: int = 0  # 0..len(fields)-1 campos, resto botones
        self.error: str = ""
        self._maxlen = 240

    @property
    def _total(self) -> int:
        return len(self.fields) + len(self.buttons)

    def _focus_is_field(self) -> bool:
        return 0 <= self.focus < len(self.fields)

    def _button_index(self) -> int:
        return max(0, self.focus - len(self.fields))

    def data(self) -> dict[str, str]:
        """Valores actuales: strip en normales, sin strip en secretas."""
        secrets = {key for key, _, secret in self.fields if secret}
        return {k: (v if k in secrets else v.strip()) for k, v in self.values.items()}

    def _secret_key(self) -> str | None:
        for key, _, secret in self.fields:
            if secret:
                return key
        return None

    def raw_data(self) -> dict[str, str]:
        return dict(self.values)

    def handle_key(self, key: int) -> str | None:
        """Procesa una tecla. Devuelve "ok", "cancel" o None."""
        nfields = len(self.fields)
        total = self._total
        if total <= 0:
            return None
        # Cancelar siempre con Esc
        if key == 27:
            return "cancel"
        # Tab / Shift-Tab (curses envía 9 y 353)
        if key in (9,):
            self.focus = (self.focus + 1) % total
            self.error = ""
            return None
        if key == curses.KEY_BTAB:
            self.focus = (self.focus - 1) % total
            self.error = ""
            return None
        if key == curses.KEY_UP:
            self.focus = (self.focus - 1) % total
            self.error = ""
            return None
        if key == curses.KEY_DOWN:
            self.focus = (self.focus + 1) % total
            self.error = ""
            return None
        if key in (curses.KEY_LEFT,):
            if not self._focus_is_field():
                self._move_button(-1)
                return None
            return None
        if key in (curses.KEY_RIGHT,):
            if not self._focus_is_field():
                self._move_button(1)
                return None
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            if self._focus_is_field():
                # Enter en campo: avanza al siguiente foco; si es el último
                # campo, salta al botón de guardar (índice 0 de botones).
                if self.focus == nfields - 1:
                    self.focus = nfields  # primer botón (Guardar)
                else:
                    self.focus += 1
                return None
            # Foco en botón
            label = self.buttons[self._button_index()].strip().lower()
            if label.startswith("guardar") or label.startswith("aceptar") or label == "ok":
                return "ok"
            return "cancel"
        # Edición solo en campos
        if self._focus_is_field():
            fkey = self.fields[self.focus][0]
            cur = self.values[fkey]
            if key in (curses.KEY_BACKSPACE, 127, 8):
                self.values[fkey] = cur[:-1]
                self.error = ""
                return None
            if key == curses.KEY_DC:  # Supr: limpiar campo
                self.values[fkey] = ""
                self.error = ""
                return None
            if 32 <= key < 127 or key > 160:
                try:
                    ch = chr(key)
                except (ValueError, OverflowError):
                    return None
                if len(cur) < self._maxlen:
                    self.values[fkey] = cur + ch
                    self.error = ""
                return None
        return None

    def _move_button(self, delta: int) -> None:
        nfields = len(self.fields)
        nbuttons = len(self.buttons)
        idx = (self._button_index() + delta) % nbuttons
        self.focus = nfields + idx
        self.error = ""

    def _calc_rect(self, max_y: int, max_x: int) -> tuple[int, int, int, int]:
        """Calcula (y, x, h, w) del modal centrado."""
        label_w = max((len(lbl) for _, lbl, _ in self.fields), default=0)
        field_w = min(40, max(20, max_x - label_w - 12))
        inner_w = max(len(self.title) + 6, label_w + field_w + 6, 44)
        btn_line = "  ".join(f"[{b}]" for b in self.buttons)
        inner_w = max(inner_w, len(btn_line) + 6)
        if self.error:
            inner_w = max(inner_w, len(self.error) + 6)
        inner_w = min(inner_w, max(20, max_x - 4))
        # alto: campos + error opcional + botones + huecos
        inner_h = len(self.fields) + 4 + (1 if self.error else 0)
        total_h = inner_h + 2
        total_w = inner_w + 2
        y = max(0, (max_y - total_h) // 2)
        x = max(0, (max_x - total_w) // 2)
        return y, x, total_h, total_w

    def cursor_pos(self, max_y: int, max_x: int) -> tuple[int, int] | None:
        """Posición (y, x) del cursor hardware si el foco es un campo."""
        if not self._focus_is_field():
            return None
        y, x, _, w = self._calc_rect(max_y, max_x)
        label_w = max((len(lbl) for _, lbl, _ in self.fields), default=0)
        field_w = w - label_w - 7
        fkey = self.fields[self.focus][0]
        val = self.values[fkey]
        avail = max(4, field_w)
        visible = val[-avail:] if len(val) > avail else val
        row = y + 2 + self.focus
        col = x + 3 + label_w + 2 + len(visible)
        if row >= max_y - 1 or col >= max_x - 1:
            return None
        return row, col

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if max_y < 8 or max_x < 30:
            return
        y, x, h, w = self._calc_rect(max_y, max_x)

        # Sombra
        try:
            for sy in range(y + 1, min(y + h + 1, max_y - 1)):
                stdscr.addstr(sy, min(x + 1, max_x - 1),
                              " " * max(0, min(w, max_x - x - 2)),
                              colors.pair(colors.PAIR_MUTED) | curses.A_DIM)
        except curses.error:
            pass

        # Borde doble
        border_attr = colors.pair(colors.PAIR_MODAL_BORDER)
        try:
            stdscr.addstr(y, x, icons.BOX_D_TL + icons.BOX_D_H * (w - 2) + icons.BOX_D_TR, border_attr)
            for row in range(1, h - 1):
                stdscr.addstr(y + row, x, icons.BOX_D_V, border_attr)
                stdscr.addstr(y + row, x + w - 1, icons.BOX_D_V, border_attr)
            stdscr.addstr(y + h - 1, x, icons.BOX_D_BL + icons.BOX_D_H * (w - 2) + icons.BOX_D_BR, border_attr)
        except curses.error:
            pass

        # Título
        title_str = f" {self.title} "
        tx = x + max(0, (w - len(title_str)) // 2)
        try:
            stdscr.addstr(y, tx, title_str[:max(0, w - 2)],
                          colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
        except curses.error:
            pass

        # Campos: etiqueta + caja de edición
        label_w = max((len(lbl) for _, lbl, _ in self.fields), default=0)
        field_w = w - label_w - 7
        for i, (fkey, label, secret) in enumerate(self.fields):
            row = y + 2 + i
            if row >= y + h - 2:
                break
            active = (self.focus == i)
            try:
                stdscr.addstr(row, x + 2, f" {label}:".ljust(label_w + 2)[:label_w + 2],
                              (colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
                              if active else colors.pair(colors.PAIR_NORMAL))
            except curses.error:
                pass
            val = self.values[fkey]
            shown = ("*" * len(val)) if secret else val
            avail = max(4, field_w)
            visible = shown[-avail:] if len(shown) > avail else shown
            cursor = "▌" if active else " "
            box = f"{visible}{cursor}".ljust(avail)[:avail]
            try:
                stdscr.addstr(row, x + 3 + label_w + 2, box,
                              colors.pair(colors.PAIR_SEARCH) | curses.A_BOLD
                              if active else colors.pair(colors.PAIR_STATUS))
            except curses.error:
                pass

        # Error inline (si hay)
        brow = y + 2 + len(self.fields)
        if self.error:
            if brow < y + h - 1:
                try:
                    stdscr.addstr(brow, x + 2, self.error[:max(0, w - 4)],
                                  colors.pair(colors.PAIR_DANGER) | curses.A_BOLD)
                except curses.error:
                    pass
            brow += 1
        else:
            # Pista de navegación
            if brow < y + h - 1:
                try:
                    stdscr.addstr(brow, x + 2, "Tab/↑↓ campo · Enter siguiente · Esc cancela"[:max(0, w - 4)],
                                  colors.pair(colors.PAIR_DIM) | curses.A_DIM)
                except curses.error:
                    pass
            brow += 1

        # Botones
        by = y + h - 2
        if by <= y or by >= max_y - 1:
            return
        try:
            offset = x + max(2, (w - (sum(len(f"[{b}]") for b in self.buttons) + 2 * (len(self.buttons) - 1))) // 2)
            for i, b in enumerate(self.buttons):
                txt = f"[{b}]"
                sep = "  " if i < len(self.buttons) - 1 else ""
                focused = (not self._focus_is_field() and self._button_index() == i)
                attr = (colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD | curses.A_REVERSE) if focused \
                    else colors.pair(colors.PAIR_PRIMARY)
                stdscr.addstr(by, offset, (txt + sep)[:max(0, w - (offset - x))], attr)
                offset += len(txt) + len(sep)
        except curses.error:
            pass


def wrap_block(text: str, width: int) -> list[str]:
    """Parte `text` en líneas de como mucho `width` celdas, por palabras.

    Utilidad **pura** (sin curses): la comparten los widgets que necesitan
    envolver texto largo al ancho disponible en vez de truncarlo
    (`EmptyState`, modales). Reglas:

    * respeta los espacios existentes: nunca parte una palabra salvo que esa
      palabra, sola, sea más ancha que la línea, y en ese caso se recorta a
      `width` sin partir ningún glifo (§46);
    * `\n` fuerza salto de línea (párrafos);
    * una línea vacía se conserva: es respiración, no ruido.

    Mide con `cell_width` (§43), nunca con `len()`: `◈ ─ ━` ocupan 1 celda en
    una terminal moderna pero `len()` no lo sabe, y centrar con `len()`
    descentra el bloque justo cuando aparece un símbolo.
    """
    if width <= 0:
        return []
    out: list[str] = []
    for para in (text or "").split("\n"):
        palabras = para.split()
        if not palabras:
            out.append("")
            continue
        actual = palabras[0]
        for palabra in palabras[1:]:
            if cell_width(actual) + 1 + cell_width(palabra) <= width:
                actual = f"{actual} {palabra}"
            else:
                out.append(actual)
                actual = palabra
        out.append(actual)
    return [clip_cells(linea, width) for linea in out]


#: Orden en que `EmptyState` renuncia elementos cuando la ventana es demasiado
#: baja (SDD §8.3). Es el inverso de la jerarquía visual: primero se cae lo
#: decorativo, y el **título no se renuncia nunca**.
_DEGRADACION = ("arte", "icono", "cta", "mensaje")


def _degradar(secciones: list[tuple[str, list[tuple[str, int]]]]) -> bool:
    """Sacrifica un elemento de `secciones` según §8.3. True si renunció algo.

    Trabaja sobre una lista de ``(clave, lineas)`` para poder quitar la sección
    entera sin importar cuál sea su posición: el arte está arriba y el CTA
    abajo, pero lo que manda no es *dónde* está sino *cuánto pesa*.
    """
    # Primero se recorta el mensaje línea a línea; sólo cuando queda una sola
    # se renuncia el bloque entero.
    for _clave, lineas in secciones:
        if _clave == "mensaje" and len(lineas) > 1:
            del lineas[-1]
            return True
    for clave_huerfano in _DEGRADACION:
        for i, (clave, _lineas) in enumerate(secciones):
            if clave == clave_huerfano:
                del secciones[i]
                return True
    return False


class EmptyState:
    """Estado vacío **estandarizado**: icono + título + mensaje + CTA opcional.

    Un único camino para todo "aquí no hay nada" de la TUI (SDD estados vacíos
    §6, §8). Antes cada pantalla se inventaba su propio `addstr` y sus propias
    comillas; ahora todas comparten bloque, centrado y jerarquía visual.

    Jerarquía (SDD §19) — y por tanto **orden de recorte** si no cabe
    (§8.3: título > mensaje > CTA > icono > decoración):

    ===============  ==================  =======================================
    Elemento         Par de colores      Papel
    ===============  ==================  =======================================
    arte ASCII       ``PAIR_DIM``        decoración, se omite si no cabe
    icono            ``PAIR_ACCENT``    acento de la categoría
    título           ``PAIR_EMPTY``     qué ocurre
    mensaje          ``PAIR_DIM``       por qué / cómo llegar
    CTA              ``PAIR_SELECTED``  qué hacer ahora (``A_BOLD``)
    ===============  ==================  =======================================

    **Responsabilidad (SDD §7).** Este widget sólo hace layout, centrado,
    wrapping, estilos y dibujo. No captura teclas, no ejecuta acciones, no
    conoce ninguna pantalla, no llama a ``App`` y no guarda estado. La CTA la
    compone quien llama, y sólo si la tecla existe de verdad: *toda CTA
    dibujada debe tener una tecla real detrás* (§4 R-4, §23).

    **Taxonomía (SDD §17).** Distingue *contenido vacío* de *búsqueda sin
    resultados* y de *loading* por el texto que recibe, no por su estado
    interno. Los *errores* no llegan aquí: van por `_warn`/`_error`.

    Nunca lanza: cada `addstr` va protegido, para que ni el 40×10 mínimo ni
    un resize a media escritura taquen de `curses.error`.
    """

    #: Antena de TV. 7 líneas de 16 celdas como mucho; decoración pura, y lo
    #: primero que se sacrifica cuando la ventana es estrecha.
    ART: tuple[str, ...] = (
        "    ┌──────────┐",
        "    │  ◉  ◉    │",
        "    │    ▬     │",
        "    │  ────    │",
        "    └────┬─────┘",
        "         │",
        "    ═════╧═════",
    )

    #: Por debajo de este ancho el arte estorba: se dibuja el bloque sin él y
    #: el icono pasa a ser el elemento visual principal.
    MIN_ART_WIDTH = 46

    @staticmethod
    def render(
        stdscr: curses.window,
        y: int,
        width: int,
        icon: str,
        title: str,
        message: str = "",
        cta: str = "",
    ) -> None:
        """Dibuja el bloque centrado verticalmente sobre `y`.

        `width` es la anchura **disponible** (no la de la ventana): quien lo
        llama en una columna estrecha —el sidebar de Canales con panel de
        detalle— pasa el ancho de su columna, y el bloque no invade a la
        vecina.
        """
        max_y = stdscr.getmaxyx()[0]
        if width <= 0 or max_y <= 0:
            return

        # Secciones del bloque, en orden de lectura, cada una ya medida y con su
        # attr. El arte es decoración pura: sólo entra si el ancho lo permite.
        secciones: list[tuple[str, list[tuple[str, int]]]] = []
        if width >= EmptyState.MIN_ART_WIDTH:
            attr_arte = colors.pair(colors.PAIR_DIM)
            secciones.append(("arte", [(linea, attr_arte) for linea in EmptyState.ART]))
        if icon:
            secciones.append(("icono", [(icon, colors.pair(colors.PAIR_ACCENT))]))
        secciones.append(("titulo", [(title, colors.pair(colors.PAIR_EMPTY))]))
        lineas_mensaje = wrap_block(message, max(1, width - 2))
        if lineas_mensaje:
            attr_msg = colors.pair(colors.PAIR_DIM)
            secciones.append(("mensaje", [(linea, attr_msg) for linea in lineas_mensaje]))
        if cta:
            attr_cta = colors.pair(colors.PAIR_SELECTED) | curses.A_BOLD
            secciones.append(("cta", [(f" {cta} ", attr_cta)]))

        # Degradación por altura (§8.3) y centrado vertical en `y`.
        # `limite` = cuántas filas puede ocupar el bloque: se dejan intactas
        # las dos de abajo (status y footer), y la 0 arriba es la barra de
        # título, así que el bloque prefiere empezar en la 1.
        limite = max_y - 2
        while True:
            # Cada sección cuenta sus líneas, no una: un mensaje de tres
            # líneas ocupa tres filas, y centrar sin contarlas descentraba el
            # bloque y empujaba la CTA fuera de pantalla.
            alto = (sum(len(lineas) for _clave, lineas in secciones)
                    + max(0, len(secciones) - 1))
            inicio = max(1, min(y - alto // 2, max(0, limite - alto)))
            if inicio + alto <= limite:
                break
            if not _degradar(secciones):
                # Sólo queda el título y aun así no cabe (terminal de 1-3
                # filas): se dibuja lo que se pueda y se sale.
                break

        fila = inicio
        for i, (_clave, lineas) in enumerate(secciones):
            if i:
                fila += 1  # separador en blanco
            for texto, attr in lineas:
                if fila >= limite:
                    break
                x = max(0, (width - cell_width(texto)) // 2)
                try:
                    stdscr.addstr(fila, x, clip_cells(texto, max(0, width - 1)), attr)
                except curses.error:
                    pass
                fila += 1


class Timeline:
    """Barra de timeline EPG: ━━━●━━━━ HH:MM-HH:MM Título."""

    @staticmethod
    def render(stdscr: curses.window, y: int, x: int, width: int,
               start: str, stop: str, title: str, is_current: bool = False) -> None:
        """Renderiza una línea de timeline inline."""
        time_str = f"{start}-{stop}"
        bar_w = max(0, width - len(time_str) - len(title) - 6)
        if bar_w < 4:
            # Formato compacto
            line = f" {time_str} {title}"
        else:
            # Barra visual: ● en progreso si es actual
            if is_current:
                filled = bar_w // 2
                bar = icons.LINE_FULL * filled + icons.LINE_DOT + icons.LINE_FULL * (bar_w - filled - 1)
            else:
                bar = icons.LINE_FULL * bar_w
            line = f" {bar} {time_str} {title}"

        attr = colors.pair(colors.PAIR_CURRENT if is_current else colors.PAIR_DIM)
        if is_current:
            attr |= curses.A_BOLD
        try:
            stdscr.addstr(y, x, line[:max(0, width)], attr)
        except curses.error:
            pass


class TabBar:
    """Barra de pestañas horizontal."""

    def __init__(self, tabs: list[str], active: int = 0) -> None:
        self.tabs = tabs
        self.active = active

    def handle_key(self, key: int) -> int | None:
        """Devuelve el índice de la pestaña seleccionada o None."""
        if key in (curses.KEY_LEFT,):
            self.active = (self.active - 1) % len(self.tabs)
            return self.active
        if key in (curses.KEY_RIGHT,):
            self.active = (self.active + 1) % len(self.tabs)
            return self.active
        # Atajos numéricos 1-9
        if ord("1") <= key <= ord("9"):
            idx = key - ord("1")
            if idx < len(self.tabs):
                self.active = idx
                return self.active
        return None

    def render(self, stdscr: curses.window, y: int, width: int) -> None:
        parts: list[str] = []
        for i, tab in enumerate(self.tabs):
            if i == self.active:
                parts.append(f" {icons.ICON_PLAY}{tab} ")
            else:
                parts.append(f"  {tab} ")
        tab_str = "│".join(parts)
        # Centrar
        x = max(0, (width - len(tab_str)) // 2)
        try:
            for i, tab in enumerate(self.tabs):
                label = f"{'▶' if i == self.active else ' '}{tab} "
                sep = "│" if i < len(self.tabs) - 1 else ""
                segment = f"{label}{sep}"
                attr = (colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD
                        if i == self.active else colors.pair(colors.PAIR_DIM))
                stdscr.addstr(y, x, segment[:max(0, width - x)], attr)
                x += len(segment)
        except curses.error:
            pass


class LoadingOverlay:
    """Pantalla de 'Cargando…' para esperas de red/disco.

    Uso típico: dibujar justo antes de una operación bloqueante
    (re-parsear una playlist remota) y hacer refresh(), de modo que
    el usuario vea feedback aunque la espera sea de segundos::

        LoadingOverlay.render(stdscr, "Actualizando lista…", sub=source)
        stdscr.refresh()
        playlist = load_playlist_source(source)  # bloqueante con timeout

    Solo stdlib + curses. Nunca lanza: todo addstr está protegido.
    `frame` selecciona el spinner (0..3 -> | / - \\).
    """

    FRAMES = ("|", "/", "-", "\\")

    @staticmethod
    def render(
        stdscr: curses.window,
        message: str = "Cargando…",
        sub: str = "",
        frame: int = 0,
    ) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if max_y < 5 or max_x < 20:
            # Ventana diminuta: una línea basta, sin box.
            try:
                spin = LoadingOverlay.FRAMES[frame % len(LoadingOverlay.FRAMES)]
                stdscr.addstr(0, 0, f" {spin} {message}"[: max(0, max_x - 1)],
                              colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
            except curses.error:
                pass
            return
        spin = LoadingOverlay.FRAMES[frame % len(LoadingOverlay.FRAMES)]
        title = f" {spin} {message} "
        sub = (sub or "").strip()
        inner_w = max(len(title), len(sub)) + 4
        box_w = min(max_x - 4, max(30, inner_w), 64)
        box_h = 5 if sub else 4
        by = max(1, (max_y - box_h) // 2)
        bx = max(0, (max_x - box_w) // 2)

        border = colors.pair(colors.PAIR_MODAL_BORDER)
        try:
            stdscr.addstr(by, bx,
                          icons.BOX_D_TL + icons.BOX_D_H * (box_w - 2) + icons.BOX_D_TR,
                          border)
            for r in range(1, box_h - 1):
                stdscr.addstr(by + r, bx, icons.BOX_D_V, border)
                stdscr.addstr(by + r, bx + box_w - 1, icons.BOX_D_V, border)
            stdscr.addstr(by + box_h - 1, bx,
                          icons.BOX_D_BL + icons.BOX_D_H * (box_w - 2) + icons.BOX_D_BR,
                          border)
        except curses.error:
            pass
        try:
            tx = bx + max(1, (box_w - len(title)) // 2)
            stdscr.addstr(by + 1, tx, title[: max(0, box_w - 2)],
                          colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
        except curses.error:
            pass
        if sub:
            try:
                sx = bx + max(1, (box_w - len(sub)) // 2)
                stdscr.addstr(by + 2, sx, sub[: max(0, box_w - 2)],
                              colors.pair(colors.PAIR_DIM) | curses.A_DIM)
            except curses.error:
                pass
        # Barra animada simple (progreso indeterminado).
        try:
            bar_w = max(8, box_w - 8)
            pos = frame % bar_w
            bar = "─" * pos + "●" + "─" * max(0, bar_w - pos - 1)
            bxx = bx + max(1, (box_w - bar_w) // 2)
            stdscr.addstr(by + box_h - 2, bxx, bar[:bar_w],
                          colors.pair(colors.PAIR_ACCENT))
        except curses.error:
            pass


def render_loading(
    stdscr: curses.window,
    message: str = "Cargando…",
    sub: str = "",
    frame: int = 0,
) -> None:
    """Atajo funcional sobre LoadingOverlay.render (para tests y App)."""
    LoadingOverlay.render(stdscr, message, sub=sub, frame=frame)
