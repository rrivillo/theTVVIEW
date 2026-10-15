"""Pantallas de la TUI theTVVIEW.

Cada pantalla implementa:
- handle_key(key) -> action | None  (acción para el App)
- render(stdscr)

Pantallas: catálogo de playlists, canales (con búsqueda incremental '/'),
favoritos ('f') y parrilla EPG por canal ('e').
"""

from __future__ import annotations

import curses
import os
import time
from dataclasses import replace
from datetime import datetime, timezone

from thetvview import player, resolutions
from thetvview import catchup
from thetvview import config
from thetvview.channel_health import ChannelHealthMonitor, estado_de
from thetvview import epg_match
from thetvview.epg_parser import Epg, parse_file
from thetvview.groups import groups_of
from thetvview.models import Channel, Playlist
from thetvview.playlist_manager import PlaylistEntry, PlaylistError, PlaylistManager
from thetvview.recents import RecentsManager, relative_time
from thetvview.cam_ref import MissingCamCredentialsError
from thetvview.stream_ref import MissingCredentialsError, resolve_channel_url
from thetvview.tracks.labels import audio_label, subtitle_label, video_label
from thetvview.tracks.manager import QUICK_AUTO, SelectTrackError
from thetvview.tracks.models import MediaCapabilities, PlaybackSelection
from thetvview.player.capabilities import KIND_SUBTITLES
from thetvview.player.router import NoCompatibleBackend
from thetvview.player.session import EndReason, SessionRegistry

from . import icons
from .tracks import SECTION_LABELS

from . import colors
from . import filters as filters_mod
from . import icons
from .actions import Action, P, V
from .errormsg import (
    describe_playback_error,
    empty_playlist_error,
)
from .favorite_rows import FavoriteRow, build_rows, layout_columns, two_columns_fit
from .textwidth import cell_width, clip_cells
from .widgets import EmptyState, ScrollableList, estado_badge_text, render_separator


# Centinela: ChannelsScreen sin filtro de grupo (vs. filtro "sin grupo").
_UNSET: object = object()

# Tab como código de carácter (el terminal lo entrega así, no como KEY_TAB,
# que curses no define en todas las plataformas).
_KEY_TAB: int = 9

# F5 para "actualizar lista" ('R' es el atajo principal; 'r' queda para
# Recientes). Fallback numérico por si curses no define KEY_F5.
_KEY_F5: int = getattr(curses, "KEY_F5", 269)


def _favorite_label(fav_urls: set[str], channel: Channel | None) -> str:
    """Etiqueta dinámica de `f`: `Favorito` o `Quitar` según el estado (§9, R-04).

    Hoy la barra dice siempre «Favorito» sobre una tecla que a veces quita. Con
    `enabled` y etiqueta derivada del estado, la definición nunca desaparece:
    sólo cambia lo que dice. Un solo lugar donde equivocarse, y el test lo ve.
    """
    if channel is None:
        return V.FAVORITO
    return V.QUITAR if channel.url in fav_urls else V.FAVORITO


def _is_favorite(app, channel: Channel | None) -> bool:  # noqa: ANN001
    """¿Es favorito `channel`? Degrada a False si el doble no lo soporta."""
    if channel is None:
        return False
    fn = getattr(getattr(app, "favorites", None), "is_favorite", None)
    if not callable(fn):
        return False
    try:
        return bool(fn(channel))
    except Exception:
        return False


class Screen:
    """Base de pantalla: título y manejo mínimo de teclas."""

    title = ""

    def __init__(self, app) -> None:  # noqa: ANN001 - referencia circular evitada con typing diferido
        self.app = app

    def shortcuts(self) -> str:
        return "↑/↓ mover · Enter seleccionar · Esc volver · q salir"

    def actions(self) -> list[Action]:
        """Catálogo de acciones contextuales de esta pantalla (§10, §63).

        Definición **única**: de esta lista salen la barra inferior y —a
        futuro— el manejo de teclas. La barra no declara nada que la pantalla
        no haga: toda ``key`` de aquí debe existir también en ``shortcuts()``
        y en ``handle_key()`` (invariante comprobada en la suite).

        La base devuelve lista vacía a propósito: ``App._render_footer`` cae a
        ``shortcuts()`` cuando no hay catálogo, así que una pantalla sin
        migrar no rompe la app.

        Dos formas de "no disponible", y la diferencia importa:

        - **no se declara** la acción cuando su tecla ni siquiera aparece en
          ``shortcuts()`` en ese estado (p. ej. `i Info` sin pistas);
        - se declara con ``enabled=False`` cuando la tecla se anuncia pero
          ahora mismo no hay nada sobre lo que actuar (p. ej. `f Favorito` sin
          canales).
        """
        return []

    def header_context(self) -> str:
        """Contexto corto de la cabecera (SDD §12), por defecto el título.

        `title` es el título *funcional* y puede ser largo y detallado
        ("EPG · CNN", "Resolución · CNN"). Lo que va a la cabecera es otra cosa:
        responder "¿dónde estoy?" en un vistazo (§3.3). Por eso son dos métodos
        y no uno: ésta es la vista, aquél la ficha.
        """
        return self.title

    def handle_key(self, key: int) -> dict | None:
        return None

    def _render_title(self, stdscr: curses.window) -> None:
        pass  # Header lo gestiona App.run

    def render(self, stdscr: curses.window) -> None:
        raise NotImplementedError


def _epg_channel_id(epg: Epg | None, channel: Channel) -> str | None:
    """Resuelve el channel_id del EPG para un canal (tvg-id, luego nombre).

    No decide nada: la política vive en :mod:`thetvview.epg_match`, que además
    la resuelve en tiempo constante. Canales, Grupos, la guía y Favoritos pasan
    por aquí, así que los cuatro comparten una sola asociación y un solo índice.
    """
    return epg_match.resolve_channel_id(epg, channel)


def _warn(app, message: str) -> None:  # noqa: ANN001
    """Aviso en barra de estado + modal (no negociable #1). Degrada en tests."""
    fn = getattr(app, "notify_warning", None)
    if callable(fn):
        fn(message)
    else:
        app.status.show(message)


def _error(app, message: str) -> None:  # noqa: ANN001
    """Error en barra de estado + modal (no negociable #1). Degrada en tests."""
    fn = getattr(app, "notify_error", None)
    if callable(fn):
        fn(message)
    else:
        app.status.show(message, error=True)


def _mostrar_error(app, err, *, retry=None, diagnose=None) -> None:  # noqa: ANN001
    """Presenta un :class:`UiError` con las acciones que existen (F4/F5).

    Un único camino para los errores de playlist y de reproducción, y con
    **degradación** para las apps de prueba: si el doble no tiene
    ``show_error``, el mensaje va a la barra como antes. Así el call site no
    necesita saber si quien lo llama es la `App` real o un doble, que es lo
    que hacía imposible probar el flujo sin levantar curses.
    """
    fn = getattr(app, "show_error", None)
    if callable(fn):
        fn(err, retry=retry, diagnose=diagnose)
        return
    app.status.show(err.cuerpo(), error=True)


def _error_dominio(app, exc: BaseException, *, retry=None, diagnose=None) -> None:  # noqa: ANN001
    """Presenta un fallo de backend (Fase 8 · U-02) con las acciones que existen.

    Las Fases 3 a 7 validan y rechazan con su propio vocabulario, y ninguna
    pantalla debería traducir eso a mano: este es el único camino. Delega en
    :func:`~thetvview.ui.domain_errors.describe_domain_error` y degrada igual que
    :func:`_mostrar_error`, para que un doble de prueba sin ``show_error`` siga
    viendo el mensaje en la barra en vez de romperse.
    """
    from .domain_errors import describe_domain_error

    _mostrar_error(app, describe_domain_error(exc), retry=retry, diagnose=diagnose)


def _error_xtream(name: str, que: str, exc: BaseException):
    """`:class:`UiError` de un fallo Xtream, con el cuerpo de `friendly_message`.

    El **título** es contextual («No se pudo cargar la lista») y el **mensaje**
    es el que ya produce :func:`thetvview.xtream_errors.friendly_message`: esa
    función sabe distinguir cuenta caducada, credenciales, límite de peticiones y
    servidor caído, y garantiza que ninguno de esos textos lleve un código HTTP
    ni una URL. Reutilizarla en vez de traducir con `describe_playlist_error` es
    lo que evita tener dos versiones que se contradigan: aquí el error no viene de
    `safe_http` sino del proveedor Xtream, que tiene su propia taxonomía.
    """
    from thetvview.xtream_errors import friendly_message

    from .errormsg import ErrorKind, UiError

    quien = f"'{str(name).strip()}' " if str(name or "").strip() else ""
    return UiError(
        title=f"La lista {quien}{que}.",
        message=friendly_message(exc),
        detail=None,
        kind=ErrorKind.PLAYLIST,
    )


def format_channel_name(ch: Channel, *, favorite: bool = False) -> str:
    """Etiqueta de canal para las listas: ★ favorito, ♪ radio."""
    fav = "★ " if favorite else ""
    radio = "♪ " if ch.radio else ""
    base = f"[{ch.group}] {ch.name}" if ch.group else ch.name
    return f"{fav}{radio}{base}"


def _render_no_results(stdscr: curses.window, y: int, width: int,  # noqa: ANN001
                       query: str, kind: str = "canales") -> None:
    """Estado vacío de **búsqueda**, categoría propia y distinta (§17).

    "He obtenido los datos, pero el filtro no encuentra nada" no es lo mismo
    que "aquí no hay nada": mezclarlos hacía que una lista llena pareciera
    vacía. Por eso tiene su propio camino, con la consulta a la vista para
    que el usuario vea *qué* está buscando sin tener que adivinarlo.

    La CTA es real: `Esc` limpia la consulta y sale de búsqueda
    (`ChannelsScreen._feed_search` / `GroupsScreen._feed_search`), y `Ctrl-U`
    la vacía sin cerrar el modal. No se anuncia ninguna tecla que no exista.
    """
    EmptyState.render(
        stdscr, y, width, icons.ICON_SEARCH,
        f"No se encontraron {kind}",
        f'No hay coincidencias para "{query}".',
        "[Esc] Limpiar búsqueda",
    )


class PlaylistsScreen(Screen):
    """Catálogo de playlists registradas (playlists.json) — vista cards."""

    title = "Playlists"

    def header_context(self) -> str:
        return f"{icons.ICON_LIST} Listas"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.entries: list[PlaylistEntry] = []
        self.selected: int = 0
        self._first_row: int = 0
        self.reload()

    def reload(self) -> None:
        self.entries = self.app.playlists.load()
        if self.selected >= len(self.entries):
            self.selected = max(0, len(self.entries) - 1)
        self._clamp_scroll()

    def current_entry(self) -> PlaylistEntry | None:
        if not self.entries:
            return None
        return self.entries[self.selected]

    def can_change_password(self) -> bool:
        """Solo las listas Xtream API (source con prefijo 'xtream://')."""
        entry = self.current_entry()
        return entry is not None and entry.is_xtream

    def _cols(self, max_x: int) -> int:
        """Número de columnas de cards según ancho."""
        if max_x >= 150:
            return 3
        return 2 if max_x >= 100 else 1

    def _card_w(self, max_x: int, cols: int) -> int:
        """Ancho de cada card."""
        gap = 2  # espacio entre columnas
        return max(28, (max_x - gap * (cols - 1)) // cols)

    def _visible_rows(self, max_y: int) -> int:
        card_h = 5
        gap = 1
        start_y = 1
        available = max_y - start_y - 2
        if available <= 0:
            return 1
        return max(1, available // (card_h + gap))

    def _clamp_scroll(self) -> None:
        if not self.entries:
            self._first_row = 0
            return
        max_y, max_x = self.app.stdscr.getmaxyx()
        cols = self._cols(max_x)
        rows = self._visible_rows(max_y)
        sel_row = self.selected // cols
        if sel_row < self._first_row:
            self._first_row = sel_row
        elif sel_row >= self._first_row + rows:
            self._first_row = sel_row - rows + 1
        max_row = max(0, -(-len(self.entries) // cols) - rows)
        self._first_row = max(0, min(self._first_row, max_row))

    def shortcuts(self) -> str:
        if not self.entries:
            return "a Añadir · T Tema · r Recientes · t Tema · ? Ayuda · q Salir"
        base = "↑/↓/←/→ · Enter · a Añadir · d Borrar"
        if self.can_change_password():
            base += " · C Contraseña Xtream"
        return base + " · R Actualizar · T Tema · r Recientes · f ★ · t Tema · ? Ayuda · q Salir"

    def actions(self) -> list[Action]:
        """Catálogo del catálogo de listas (§3, Catálogo).

        Catálogo vacío: `shortcuts()` anuncia otras teclas, y la barra sólo
        puede declarar lo que la pantalla anuncia (§10). Por eso aquí sólo hay
        «a Añadir», «T Tema» y «? Ayuda».
        """
        if not self.entries:
            return [
                Action("a", V.ANADIR, P.FRECUENTE),
                Action("T", V.TEMA, P.ORGANIZACION),
                Action("?", V.AYUDA, P.AYUDA, essential=True),
            ]
        acciones = [
            Action("Enter", V.ABRIR, P.PRIMARIA),
            Action("a", V.ANADIR, P.FRECUENTE),
            Action("d", V.BORRAR, P.ORGANIZACION),
            Action("T", V.TEMA, P.ORGANIZACION),
        ]
        # `C` sólo existe para las Listas X: `shortcuts()` tampoco la anuncia
        # en el resto, y la barra no puede anunciar más que la pantalla (§10).
        if self.can_change_password():
            acciones.append(Action("C", V.CONTRASENA_X, P.ORGANIZACION))
        acciones.append(Action("?", V.AYUDA, P.AYUDA, essential=True))
        return acciones

    def handle_mouse(self, mx: int, my: int, screen) -> bool:  # noqa: ANN001
        if not self.entries:
            return False
        max_y, max_x = self.app.stdscr.getmaxyx()
        cols = self._cols(max_x)
        card_w = self._card_w(max_x, cols)
        gap = 2
        total_w = cols * card_w + (cols - 1) * gap
        start_x = max(0, (max_x - total_w) // 2)
        card_h = 5
        start_y = 1
        for i, _ in enumerate(self.entries):
            row = i // cols
            if row < self._first_row:
                continue
            col = i % cols
            cx = start_x + col * (card_w + gap)
            cy = start_y + (row - self._first_row) * (card_h + 1)
            if cy <= my < cy + card_h and cx <= mx < cx + card_w:
                self.selected = i
                self._clamp_scroll()
                return True
        return False

    def handle_key(self, key: int) -> dict | None:
        if not self.entries:
            if key == ord("a"):
                return {"action": "add_playlist"}
            if key == ord("T"):
                return {"action": "choose_theme"}
            return None
        if key == ord("R") or key == _KEY_F5:
            # El catálogo es un JSON local: relectura instantánea.
            self.reload()
            try:
                self.app.footer.show(f"Catálogo actualizado: {len(self.entries)} listas.")
            except Exception:
                pass
            return None
        max_y, max_x = self.app.stdscr.getmaxyx()
        cols = self._cols(max_x)

        if key == curses.KEY_UP:
            self.selected = max(0, self.selected - cols)
        elif key == curses.KEY_DOWN:
            self.selected = min(len(self.entries) - 1, self.selected + cols)
        elif key == curses.KEY_LEFT:
            self.selected = max(0, self.selected - 1)
        elif key == curses.KEY_RIGHT:
            self.selected = min(len(self.entries) - 1, self.selected + 1)
        elif key == ord("k"):
            self.selected = max(0, self.selected - cols)
        elif key == ord("j"):
            self.selected = min(len(self.entries) - 1, self.selected + cols)
        elif key == ord("g"):
            self.selected = 0
        elif key == ord("G"):
            self.selected = len(self.entries) - 1
        elif key == ord("a"):
            return {"action": "add_playlist"}
        elif key == ord("T"):
            # `T` elige el tema en un modal; `t` lo alterna (D12).
            return {"action": "choose_theme"}
        elif key == ord("d"):
            entry = self.current_entry()
            if entry is None:
                self.app.footer.show("No hay playlists que borrar.")
                return None
            return {"action": "remove_playlist", "name": entry.name}
        elif key in (ord("c"), ord("C")):
            entry = self.current_entry()
            if entry is None:
                return None
            if not self.can_change_password():
                try:
                    self.app.footer.show("Solo las listas Xtream tienen contraseña.")
                except Exception:
                    pass
                return None
            return {"action": "change_password", "name": entry.name}
        elif key == ord("f"):
            return {"action": "show_favorites"}
        elif key in (curses.KEY_ENTER, 10, 13):
            entry = self.current_entry()
            if entry is None:
                self.app.footer.show("Catálogo vacío: pulsa 'a' para añadir una playlist.")
                return None
            return {"action": "open_playlist", "entry": entry}
        else:
            return None
        self._clamp_scroll()
        return None

    def _render_card(self, stdscr: curses.window, entry: PlaylistEntry,
                     card_x: int, card_y: int, card_w: int, selected: bool) -> None:
        """Renderiza una card individual de playlist."""
        max_y = stdscr.getmaxyx()[0]
        # Amarillo para M3U/html, púrpura para Xtream.
        sel_pair = colors.selection_pair_for_kind(getattr(entry, "kind", "m3u"))
        border_attr = colors.pair(colors.PAIR_PRIMARY if selected else colors.PAIR_BORDER)
        text_attr = colors.pair(sel_pair if selected else colors.PAIR_NORMAL)
        dim_attr = colors.pair(colors.PAIR_DIM)

        card_h = 5
        if card_y + card_h > max_y - 2:
            return

        # Fondo sólido para selected
        if selected:
            try:
                for r in range(card_h):
                    stdscr.addstr(card_y + r, card_x, " " * card_w, colors.pair(sel_pair))
            except curses.error:
                pass

        # Bordes — rounded con PRIMARY|BOLD para selected
        top = f"{icons.BOX_R_TL}{icons.BOX_H * (card_w - 2)}{icons.BOX_R_TR}"
        bot = f"{icons.BOX_R_BL}{icons.BOX_H * (card_w - 2)}{icons.BOX_R_BR}"
        bdr = border_attr | curses.A_BOLD if selected else border_attr
        try:
            stdscr.addstr(card_y, card_x, top[:max(0, card_w)], bdr)
            stdscr.addstr(card_y + card_h - 1, card_x, bot[:max(0, card_w)], bdr)
            for r in range(1, card_h - 1):
                stdscr.addstr(card_y + r, card_x, icons.BOX_V, bdr)
                stdscr.addstr(card_y + r, card_x + card_w - 1, icons.BOX_V, bdr)
        except curses.error:
            pass

        inner_w = max(0, card_w - 4)
        name = entry.name[:inner_w]
        icon = icons.ICON_XTREAM if entry.kind == "xtream" else icons.ICON_TV
        name_line = f" {icon} {name}"
        try:
            stdscr.addstr(card_y + 1, card_x + 1, name_line[:inner_w], text_attr | curses.A_BOLD)
        except curses.error:
            pass

        src = entry.source
        if len(src) > inner_w - 2:
            src = src[:inner_w - 5] + "..."
        try:
            stdscr.addstr(card_y + 2, card_x + 1, f" {src}", dim_attr)
        except curses.error:
            pass

        try:
            stdscr.addstr(card_y + 3, card_x + 1,
                          f"{'─' * (card_w - 2)}", colors.pair(colors.PAIR_BORDER) | curses.A_DIM)
        except curses.error:
            pass

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.entries:
            # Sin crear nada aquí: la CTA anuncia `a`, que ya existe y ya crea
            # la lista por su propio modal (no se duplica esa lógica, §12).
            EmptyState.render(
                stdscr, max_y // 2, max_x,
                icons.ICON_LIST,
                "No hay playlists",
                "Añade una lista M3U o Xtream para empezar a ver canales.",
                "[A] Añadir lista",
            )
            return

        cols = self._cols(max_x)
        card_w = self._card_w(max_x, cols)
        gap = 2
        total_w = cols * card_w + (cols - 1) * gap
        start_x = max(0, (max_x - total_w) // 2)

        card_h = 5
        start_y = 1  # después del header
        visible_rows = self._visible_rows(max_y)

        for i, entry in enumerate(self.entries):
            row = i // cols
            if row < self._first_row:
                continue
            if row >= self._first_row + visible_rows:
                break
            col = i % cols
            cx = start_x + col * (card_w + gap)
            cy = start_y + (row - self._first_row) * (card_h + 1)
            if cy + card_h > max_y - 2:
                break
            self._render_card(stdscr, entry, cx, cy, card_w, i == self.selected)
        # Indicador de paginación
        total_rows = -(-len(self.entries) // cols)
        if total_rows > visible_rows:
            info = f" {self._first_row + 1}-{min(total_rows, self._first_row + visible_rows)}/{total_rows} "
            try:
                px = max(0, (max_x - len(info)) // 2)
                py = max_y - 3
                if py >= 1:
                    stdscr.addstr(py, px, info, colors.pair(colors.PAIR_DIM) | curses.A_DIM)
            except curses.error:
                pass


class HomeScreen(Screen):
    """Bienvenida: lo primero que ve quien aún no tiene ninguna lista.

    Es una pantalla de **una sola situación**: sin listas. Quien ya tiene
    listas configuradas nunca la ve, porque la raíz sigue siendo el catálogo
    (`App._pantalla_raiz`): meterla en medio del camino de quien ya sabe lo
    que hace sería hacerle pulsar «Enter» de más para ver lo mismo
    (FR-HOM-06, D13).

    Todo lo que ofrece es ejecutable de verdad —«a» entra en el mismo modal de
    alta que usa el catálogo— y nada de lo que enseña es un número de ejemplo:
    si no hay listas, no hay cifras que inventar (FR-HOM-04).
    """

    title = "theTVVIEW"

    #: Qué hace la app, en una frase. Sin cifras de ejemplo.
    DESC = "Tu televisión en la terminal: listas de canales, guía y favoritos."

    def header_context(self) -> str:
        return f"{icons.ICON_TV} Bienvenida"

    def shortcuts(self) -> str:
        return "a Añadir · ? Ayuda · t Tema · q Salir"

    def actions(self) -> list[Action]:
        return [
            Action("a", V.ANADIR, P.PRIMARIA),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
            Action("q", V.SALIR, P.NAVEGACION),
        ]

    def handle_key(self, key: int) -> dict | None:
        if key == ord("a"):
            # El mismo camino que el catálogo: un único alta, no dos.
            return {"action": "add_playlist"}
        if key == ord("q"):
            # `q` lo atiende la App antes que la pantalla; si algún día llegara
            # aquí, la intención del usuario es la misma: la salida se confirma.
            return {"action": "confirm_quit"}
        return None

    def reload(self) -> None:
        """Reactiva el catálogo en cuanto hay su primera lista.

        `add_playlist`, `remove_playlist` y `restore_playlist` recargan la
        pantalla actual cuando terminan. Aquí eso significa algo concreto: si
        ya hay listas, la bienvenida **deja de tener razón de ser** y la raíz
        pasa a ser el catálogo —el sitio donde de verdad se abre un canal—, en
        vez de dejar al usuario con un «vacío» que ya no lo es (FR-HOM-07).

        Si sigue sin haber listas, no hace nada: recargar no inventa.
        """
        app = self.app
        hay = getattr(getattr(app, "playlists", None), "load", None)
        try:
            entradas = hay() if callable(hay) else []
        except Exception:
            return
        if not entradas:
            return
        stack = getattr(app, "stack", None)
        if isinstance(stack, list) and stack and stack[0] is self:
            stack[0] = PlaylistsScreen(app)
            app.status.show("Listas añadidas. Elige una con Enter.")

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        # Identidad primero (FR-HOM-09): el nombre y una frase. En terminal
        # estrecha el bloque se recorta solo; nunca se lanza.
        titulo = f"{icons.ICON_TV} theTVVIEW"
        y = max(2, max_y // 2 - 4)
        if max_y > 3 and max_x > len(titulo):
            try:
                stdscr.addstr(y, max(0, (max_x - cell_width(titulo)) // 2),
                              clip_cells(titulo, max_x),
                              colors.pair(colors.PAIR_TITLE) | curses.A_BOLD)
            except curses.error:
                pass
            y += 1
            desc = clip_cells(self.DESC, max(0, max_x - 2))
            try:
                stdscr.addstr(y, max(0, (max_x - cell_width(desc)) // 2), desc,
                              colors.pair(colors.PAIR_DIM))
            except curses.error:
                pass
            y += 2
        # El bloque estándar explica qué pasa y qué hacer, y `EmptyState` ya
        # sabe recortarse y degradarse en terminal estrecha (FR-HOM-08).
        EmptyState.render(
            stdscr, y, max_x,
            icons.ICON_LIST,
            "Todavía no hay ninguna lista",
            "Añade una lista de canales M3U o tu cuenta de Lists X para empezar.",
            "[A] Añadir lista",
        )


class ChannelsScreen(Screen):
    """Lista de canales de una playlist abierta.

    '/' abre el modal de búsqueda: filtra mientras se escribe; Esc limpia,
    Enter confirma la consulta y vuelve a navegación normal.
    """

    def __init__(
        self, app, playlist: Playlist, group: str | None | object = _UNSET
    ) -> None:  # noqa: ANN001
        super().__init__(app)
        self.playlist = playlist
        # group=_UNSET: todos los canales; group=str: ese grupo;
        # group=None: solo canales sin group-title.
        self.group_filtered = group is not _UNSET
        self.group = group if group is not _UNSET else None
        if self.group_filtered:
            self.channels = [c for c in playlist.channels if (c.group or None) == self.group]
        else:
            self.channels = list(playlist.channels)
        self.list = ScrollableList(
            selected_pair=colors.selection_pair_for_kind(getattr(playlist, "kind", "m3u"))
        )
        self.query = ""
        self.searching = False
        # Índices (sobre self.channels) de los canales visibles tras filtrar.
        self.visible_idx: list[int] = []
        # Snapshot de favoritas + flag de estrella del título: se calculan
        # en _apply_filter (una sola pasada en memoria) para no hacer IO
        # por canal ni por frame con listas grandes.
        self._fav_urls: set[str] = set()
        self._has_fav: bool = False
        # Cachés en memoria de las etiquetas y de los textos de búsqueda,
        # válidas mientras `self.channels` no cambie (y las favoritas, en el
        # caso de las etiquetas). Evita reformatear 90k+ cadenas en cada
        # tecla al filtrar.
        self._labels: list[str] | None = None
        self._labels_src: list[Channel] | None = None
        self._labels_favs: set[str] = set()
        self._search_texts: list[str] | None = None
        self._search_src: list[Channel] | None = None
        # --- Filtros de búsqueda (U-05) ------------------------------------
        #
        # `filtros` es una lista de `(campo, valor)` tal como los escribe
        # `ui.filters`; `orden` es `None` (como viene en la lista) o una clave
        # de `filters.ORDENES`. Están **vacíos por omisión**: la búsqueda de
        # siempre —`q in texto`— sigue siendo lo que pasa si el usuario no
        # toca nada, y por eso va por su propio camino, sin preguntar al
        # backend.
        self.filtros: list[tuple[str, str]] = []
        self.orden: str | None = None
        self._indice: filters_mod.IndiceFiltros | None = None
        self._apply_filter()

    # --- Filtros (U-05) ------------------------------------------------------

    def filtros_activos(self) -> dict[str, str]:
        """`{campo: valor}` de lo que está puesto, para las filas del modal."""
        return {clave: valor for clave, valor in self.filtros}

    def filtros_legibles(self) -> list[str]:
        """Las frases de los filtros activos, para confirmarlos por su nombre."""
        return [filters_mod.frase_de(clave, valor)
                for clave, valor in self.filtros]

    def _indice_de_filtros(self) -> filters_mod.IndiceFiltros:
        """El motor, que se reconstruye si cambian los canales o la guía.

        Un `IndiceFiltros` con la lista de la pantalla de antes contaría sobre
        canales que ya no están, y daría un número que no corresponde con lo
        que hay en pantalla. Se compara por **identidad**, no por contenido:
        recargar la lista produce otra lista con los mismos canales, y
        reconstruirse en ese caso tiraría a la basura todo lo contado.
        """
        ctx = (self.app.contexto_de_reglas()
               if hasattr(self.app, "contexto_de_reglas") else {})
        epg = ctx.get("epg")
        indice_epg = ctx.get("indice_epg")
        if (self._indice is None
                or self._indice.fuente is not self.channels
                or self._indice.epg is not epg):
            self._indice = filters_mod.IndiceFiltros(
                self.channels, epg=epg, indice_epg=indice_epg)
        else:
            self._indice.indice_epg = indice_epg
        return self._indice

    def poner_filtros(self, filtros=None, orden=...,  # noqa: ANN001
                      ) -> int:
        """Cambia los filtros o el orden y vuelve a filtrar. Devuelve la cuenta.

        El canal seleccionado se conserva si sigue entrando en el filtro: al
        cambiar un filtro, saltar al primer resultado es lo que más molesta,
        porque el cursor se va justo de la fila que estabas mirando.
        """
        if filtros is not None:
            self.filtros = list(filtros)
        if orden is not ...:
            self.orden = orden
        self._apply_filter(keep_selection=True)
        return len(self.visible_idx)

    def limpiar_filtros(self) -> int:
        """Quita todos los filtros y deja el orden como estaba.

        El **orden no es un filtro**: no quita canales, sólo los mueve, y
        mezclarlo con «quitar todos los filtros» haría que un `X` cambiara
        algo que el usuario no pidió.
        """
        self.filtros = []
        self._apply_filter(keep_selection=True)
        return len(self.visible_idx)

    def header_context(self) -> str:
        return f"{icons.ICON_TV} Canales · {len(self.visible_idx)}"

    # --- Filtrado -------------------------------------------------------------

    def _refresh_fav_snapshot(self, channels: list[Channel] | None = None) -> set[str]:
        """Un solo acceso cacheado a favoritas (un stat como mucho).

        Si el manager no expone `favorite_urls` (dobles de test con solo
        `is_favorite`), se construye el set por canal.
        """
        mgr = self.app.favorites
        get_urls = getattr(mgr, "favorite_urls", None)
        if callable(get_urls):
            try:
                urls = set(get_urls())
            except Exception:
                urls = set()
        else:
            urls = set()
            pool = channels if channels is not None else self.channels
            is_fav = getattr(mgr, "is_favorite", None)
            if callable(is_fav):
                try:
                    urls = {ch.url for ch in pool if is_fav(ch)}
                except Exception:
                    urls = set()
        self._fav_urls = urls
        return urls

    def _label_for(self, idx: int) -> str:
        ch = self.channels[idx]
        return format_channel_name(ch, favorite=ch.url in self._fav_urls)

    def _labels_for(self, channels: list[Channel], fav_urls: set[str]) -> list[str]:
        """Etiquetas de todas las filas, cacheadas por (canales, favoritas).

        Reformatear 90k+ canales costaba ~100 ms por tecla al buscar; con la
        caché solo se hace el primer filtro (y al cambiar las favoritas).
        """
        if (
            self._labels is not None
            and self._labels_src is channels
            and self._labels_favs == fav_urls
        ):
            return self._labels
        labels: list[str] = []
        append = labels.append
        has_fav = False
        for ch in channels:
            fav = ch.url in fav_urls
            if fav:
                has_fav = True
            append(format_channel_name(ch, favorite=fav))
        self._labels = labels
        self._labels_src = channels
        self._labels_favs = set(fav_urls)
        self._has_fav = has_fav
        return labels

    def _search_texts_for(self, channels: list[Channel]) -> list[str]:
        """Texto en minúsculas (nombre + grupo) cacheado para filtrar.

        Un `q in texto` por canal es una búsqueda C; hacer `.casefold()`
        por canal en cada tecla era el cuello de botella al escribir.
        """
        if self._search_texts is not None and self._search_src is channels:
            return self._search_texts
        texts = [
            (ch.name + "\n" + (ch.group or "")).casefold() for ch in channels
        ]
        self._search_texts = texts
        self._search_src = channels
        return texts

    #: Tope de coincidencias resaltadas por fila. Diez es de sobra para leer
    #: de un vistazo y evita que un nombre kilométrico painted en 400 trozos
    #: convierta el resaltado en ruido (y en trabajo de más por tecla).
    MAX_RESALTADOS = 10

    @classmethod
    def _coincidencias(cls, texto: str, q: str) -> list[tuple[int, int]]:
        """Rangos `(inicio, fin)` de cada aparición de `q` en `texto`.

        Índices de **carácter**, que es lo que corta `ScrollableList`. Primero
        se busca tal cual —caso normal, y un `find` en C— y sólo si eso falla
        se cae a `casefold`, que es el caso caro: escribir en mayúsculas no
        debe costarle un `casefold` a cada canal de la lista.

        Si `casefold()` cambia la longitud del texto (`ß` → `ss`), los índices
        ya no valen sobre el original: en ese caso no se resalta nada. Es
        preferible a resaltar el trozo equivocado.
        """
        if not texto or not q:
            return []
        rango = max(1, cls.MAX_RESALTADOS)
        bajo = texto.casefold()
        if len(bajo) != len(texto):
            bajo = q if q in texto else ""
        if not bajo:
            return []
        salta = len(q)
        rangos: list[tuple[int, int]] = []
        pos = bajo.find(q)
        while pos != -1 and len(rangos) < rango:
            rangos.append((pos, pos + salta))
            pos = bajo.find(q, pos + salta)
        return rangos

    def _visibles_por_texto(self, q: str) -> list[int]:
        """Índices cuyo texto indexado contiene `q`. El camino de siempre.

        Un `q in texto` por canal es una búsqueda en C sobre una cadena ya en
        minúsculas: con 90 000 canales son microsegundos. Es lo que hace que
        escribir sea fluido, y por eso **no se pregunta al backend** cuando no
        hay filtros puestos (U-05).
        """
        if not q:
            return list(range(len(self.channels)))
        textos = self._search_texts_for(self.channels)
        return [i for i, text in enumerate(textos) if q in text]

    def _apply_filter(self, keep_selection: bool = False) -> None:
        prev_pos = self.list.selected if keep_selection else -1
        prev_idx = (
            self.visible_idx[prev_pos]
            if 0 <= prev_pos < len(self.visible_idx)
            else None
        )
        channels = self.channels
        fav_urls = self._refresh_fav_snapshot(channels)
        all_labels = self._labels_for(channels, fav_urls)
        q = self.query.casefold()

        if self.filtros or self.orden is not None:
            # Con filtros —o con un orden elegido— el motor es quien dice qué
            # canales salen. El texto se le pasa como filtro más, así que las
            # dos cosas se cumplen a la vez y no hay que elegir una.
            #
            # El orden entra aquí y no en el camino rápido porque ordenar la
            # lista entera **sin** ningún filtro puesto es el uso más corriente
            # de los cuatro: si sólo se aplicara con filtros, `o` no haría nada
            # en la mitad de las veces.
            visibles = self._indice_de_filtros().resuelve(
                self.filtros, q, self.orden)
        else:
            visibles = self._visibles_por_texto(q)

        # Copia defensiva: `set_items` no debe compartir la lista con la caché.
        self.visible_idx = visibles
        self.list.set_items([all_labels[i] for i in visibles],
                            keep_selection=True)
        if not q:
            self.list.set_ranges(None)
        else:
            # La coincidencia se resalta en **la etiqueta que se ve**, no en
            # el texto indexado (nombre + grupo): los índices de aquél no
            # valen sobre ésta (FR-SRC-06).
            self.list.set_ranges(
                [self._coincidencias(all_labels[i], q) for i in visibles])
        if prev_idx is not None:
            try:
                # index() es C-level y evita el bucle Python por tecla.
                self.list.selected = self.visible_idx.index(prev_idx)
            except ValueError:
                pass  # el canal anterior quedó fuera del filtro
        self.list.clamp()

    def current_channel(self) -> Channel | None:
        if not self.visible_idx:
            return None
        return self.channels[self.visible_idx[self.list.selected]]

    def refresh_from_playlist(self, fresh: Playlist) -> None:
        """Sustituye el contenido por un re-parseo fresco de la misma fuente.

        Preserva filtro de grupo, query de búsqueda y (si sigue
        existiendo) el canal seleccionado por identidad (nombre+url).
        No toca curses: testeable con unittest.
        """
        cur = self.current_channel()
        ident = (cur.name, cur.url) if cur is not None else None
        self.playlist = fresh
        self.list.set_selected_pair(
            colors.selection_pair_for_kind(getattr(fresh, "kind", "m3u"))
        )
        if self.group_filtered:
            self.channels = [c for c in fresh.channels if (c.group or None) == self.group]
        else:
            self.channels = list(fresh.channels)
        # `canales` es una lista nueva, así que el motor de filtros queda con
        # la de antes: se suelta para que no cuente sobre canales que ya no
        # están (U-05).
        self._indice = None
        # Re-etiquetar conservando la query actual.
        self._apply_filter()
        if ident is not None:
            for pos, i in enumerate(self.visible_idx):
                c = self.channels[i]
                if (c.name, c.url) == ident:
                    self.list.selected = pos
                    break
        self.list.clamp()

    # --- Teclas ---------------------------------------------------------------

    def shortcuts(self) -> str:
        if self.searching:
            return "Escribir filtra · Enter confirmar · Ctrl-U limpiar · Esc salir"
        return (
            "↑/↓ · Enter ▶ · / Buscar · F Filtros · s Fuentes · "
            "o Orden · g Grupos · f ★ · e EPG · c Colecciones · "
            "C Colecciones · R Actualizar · r Recientes · p Reproductor · "
            "? Ayuda · t Tema · Esc ←"
        )

    def actions(self) -> list[Action]:
        """Catálogo de la lista de canales (Canales).

        En búsqueda el juego de teclas es otro (§34): sólo confirmar, vaciar y
        cancelar. Fuera de búsqueda, `f` dice Favorito o Quitar según el canal
        de verdad bajo el cursor (§9, R-04).
        """
        if self.searching:
            return [
                Action("Enter", V.CONFIRMAR, P.PRIMARIA),
                Action("Ctrl-U", V.VACIAR, P.FRECUENTE),
                Action("Esc", V.CANCELAR, P.NAVEGACION),
            ]
        channel = self.current_channel()
        hay = channel is not None
        acciones = [
            Action("Enter", V.VER, P.PRIMARIA, enabled=hay),
            Action("/", V.BUSCAR, P.FRECUENTE),
            Action("f", _favorite_label(self._fav_urls, channel), P.CONTEXTUAL, enabled=hay),
            Action("g", V.GRUPO, P.ORGANIZACION),
            Action("e", V.EPG, P.SECUNDARIA, enabled=hay),
            Action("c", V.COLECCIONES, P.SECUNDARIA),
            Action("C", V.COLECCIONES, P.CONTEXTUAL, enabled=hay),
            Action("s", V.FUENTES, P.CONTEXTUAL, enabled=hay),
            Action("Esc", V.VOLVER, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]
        # `F` y `o` siempre: abrir el constructor sin filtros puestos y poner
        # el primero es el camino normal, no una excepción. `X` sólo con
        # filtros: anunciar «quitar filtros» sobre una lista sin filtros es
        # un botón que no hace nada (§35).
        extras = [Action("F", V.FILTROS, P.FRECUENTE)]
        if self.filtros:
            extras.append(Action("X", V.LIMPIAR, P.CONTEXTUAL))
        extras.append(Action("o", V.ORDEN, P.CONTEXTUAL))
        return acciones[:2] + extras + acciones[2:]

    def handle_mouse(self, mx: int, my: int, screen) -> bool:  # noqa: ANN001
        # Solo en modo lista (sidebar). Calcular si click está en lista
        max_y, max_x = self.app.stdscr.getmaxyx()
        show_detail = max_x >= 80
        sidebar_w = max(28, int(max_x * 0.6)) if show_detail else max_x
        list_start_y = 1
        body_h = max(1, max_y - list_start_y - 2)
        if my < list_start_y or my >= list_start_y + body_h:
            return False
        if mx >= sidebar_w:
            return False
        idx = self.list.top + (my - list_start_y)
        if 0 <= idx < len(self.list.items):
            self.list.selected = idx
            return True
        return False

    def _feed_search(self, key: int) -> dict | None:
        if key in (curses.KEY_ENTER, 10, 13):
            self.searching = False
            self.app.footer.show("")
            return None
        if key in (27,):  # Esc: limpia la consulta y sale de búsqueda
            self.searching = False
            self.query = ""
            self._apply_filter()
            msg = "Búsqueda limpiada."
            try:
                self.app.status.show(msg)
            except Exception:
                pass
            try:
                self.app.footer.show(msg)
            except Exception:
                pass
            return None
        if key in (21,):  # Ctrl-U: borrar el contenido del modal sin cerrarlo
            self.query = ""
            self._apply_filter(keep_selection=True)
            return None
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.query = self.query[:-1]
            self._apply_filter(keep_selection=True)
            return None
        if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE,
                   curses.KEY_HOME, curses.KEY_END):
            self.list.handle_key(key, self.app.body_height())
            return None
        if 32 <= key < 127 or key > 160:
            self.query += chr(key)
            self._apply_filter(keep_selection=True)
            return None
        return None

    def handle_key(self, key: int) -> dict | None:
        if self.searching:
            return self._feed_search(key)
        # 'g' abre grupos antes de que la lista lo interprete como "inicio".
        # Solo si hay grupos; si no, deja que ScrollableList maneje 'g' como HOME
        if key == ord("g"):
            if not self.playlist.channels:
                msg = "La playlist no tiene canales."
                try:
                    self.app.status.show(msg)
                except Exception:
                    pass
                try:
                    self.app.footer.show(msg)
                except Exception:
                    pass
                return None
            if len(groups_of(self.playlist.channels)) < 2:
                msg = "Esta playlist no tiene grupos."
                try:
                    self.app.status.show(msg)
                except Exception:
                    pass
                try:
                    self.app.footer.show(msg)
                except Exception:
                    pass
                return None
            return {"action": "show_groups", "playlist": self.playlist}
        rows = self.app.body_height()
        if self.list.handle_key(key, rows):
            return None
        if key == ord("/"):
            self.searching = True
            return None
        if key == ord("F"):
            # El constructor de filtros. Sin filtros puestos entra directo a
            # añadir el primero (App.search_filters).
            return {"action": "search_filters", "screen": self}
        if key == ord("X"):
            return {"action": "clear_search_filters", "screen": self}
        if key == ord("o"):
            return {"action": "search_order", "screen": self}
        if key == ord("R") or key == _KEY_F5:
            # 'r' queda para Recientes (global); 'R'/F5 recarga la fuente
            # porque los canales de una URL pueden cambiar sin aviso.
            return {"action": "reload_playlist"}
        if key == ord("f"):
            channel = self.current_channel()
            if channel is None:
                _warn(self.app, "No hay canales que marcar.")
                return None
            return {"action": "toggle_favorite", "channel": channel}
        if key == ord("e"):
            channel = self.current_channel()
            if channel is None:
                _warn(self.app, "Selecciona un canal para ver su EPG.")
                return None
            return {
                "action": "show_epg",
                "channel": channel,
                "epg_url": self.playlist.epg_url,
            }
        if key == ord("c"):
            # Las colecciones agrupan canales, así que se abren desde aquí:
            # es la pantalla donde el usuario está mirando justo los canales
            # que quiere agrupar.
            return {"action": "show_collections"}
        if key == ord("C"):
            # Mayúscula: no «abre la lista», sino «elige en cuáles está este
            # canal». Con `c` minúscula al lado, la diferencia tiene que
            # poder leerse en el pie, no sólo reconocerse en la tecla.
            canal = self.current_channel()
            if canal is None:
                _warn(self.app, "No hay canal debajo del cursor.")
                return None
            return {"action": "channel_collections", "channel": canal}
        if key == ord("s"):
            # Fuentes del canal (U-06): qué otra lista lo tiene y cuál se usa.
            canal = self.current_channel()
            if canal is None:
                _warn(self.app, "No hay canal debajo del cursor.")
                return None
            return {"action": "channel_sources", "channel": canal}
        if key == ord("P"):
            # Programar grabación (U-10): abrir el planificador.
            return {"action": "show_scheduler"}
        if key in (curses.KEY_ENTER, 10, 13):
            channel = self.current_channel()
            if channel is None:
                _warn(self.app, "La playlist no tiene canales (o el filtro no coincide).")
                return None
            return {"action": "open_channel", "channel": channel}
        return None

    # --- Render -----------------------------------------------------------------

    def _title_text(self) -> str:
        total = len(self.channels)
        shown = len(self.visible_idx)
        # Con filtros puestos el «N/M» solo no dice bastante: hay que ver
        # *por qué* la lista ha encogido, o el usuario no sabe si está
        # buscando lo que cree.
        if self.query or self.filtros:
            suffix = f" · {shown}/{total}"
        else:
            suffix = f" ({total} canales)"
        grp = ""
        if self.group_filtered:
            grp = f" · [{self.group}]" if self.group else " · [sin grupo]"
        filtros = ""
        if self.filtros:
            frases = self.filtros_legibles()
            filtros = " · " + " · ".join(frases)
        orden = ""
        if self.orden is not None:
            orden = f" · {filters_mod.orden_legible(self.orden)}"
        star = " ★" if self._has_fav else ""
        return (f"{self.playlist.name}{grp}{suffix}{filtros}{orden}{star}")

    def _render_detail_panel(self, stdscr: curses.window, x: int, y: int, w: int, h: int) -> None:
        """Panel derecho: info del canal seleccionado + EPG preview."""
        ch = self.current_channel()
        if ch is None:
            try:
                stdscr.addstr(y + h // 2, x, "Selecciona un canal".center(w)[:w],
                              colors.pair(colors.PAIR_DIM))
            except curses.error:
                pass
            return

        row = y
        max_y = y + h

        # Nombre del canal
        icon = icons.ICON_RADIO if ch.radio else icons.ICON_TV
        fav = " ★" if ch.url in self._fav_urls else ""
        name_line = f" {icon} {ch.name}{fav}"
        try:
            stdscr.addstr(row, x, name_line[:w], colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
        except curses.error:
            pass
        row += 1

        # Grupo
        if ch.group:
            try:
                stdscr.addstr(row, x, f" {icons.ICON_GROUP} {ch.group}", colors.pair(colors.PAIR_DIM))
            except curses.error:
                pass
            row += 1

        # Separador
        try:
            stdscr.addstr(row, x, f"{'─' * (w - 1)}", colors.pair(colors.PAIR_BORDER))
        except curses.error:
            pass
        row += 1

        # URL (truncada)
        url = ch.url
        avail = max(0, w - 2)
        if len(url) > avail:
            url = url[:avail - 3] + "..."
        try:
            stdscr.addstr(row, x, f" {url}", colors.pair(colors.PAIR_DIM))
        except curses.error:
            pass
        row += 1

        # Separador
        try:
            stdscr.addstr(row, x, f"{'─' * (w - 1)}", colors.pair(colors.PAIR_BORDER))
        except curses.error:
            pass
        row += 1

        # Archivo (catch-up): sólo si el proveedor lo declaró (FR-002). Un
        # canal sin declaración no muestra ninguna pista de que exista la
        # función: eso es justo lo que pide el SDD §20.1.
        if row < max_y - 1:
            cap = catchup.capability_for(ch)
            if catchup.can_use_catchup(cap):
                dias = cap.archive_duration_days
                plural = "día" if dias == 1 else "días"
                try:
                    stdscr.addstr(
                        row, x, f" {icons.ICON_ARCHIVE} Archivo: {dias} {plural}",
                        colors.pair(colors.PAIR_PRIMARY),
                    )
                except curses.error:
                    pass
                row += 1

        # EPG: programa actual si hay datos cargados
        epg = getattr(self.app, "epg", None)
        if epg is not None and row < max_y - 1:
            from .screens import _epg_channel_id
            cid = _epg_channel_id(epg, ch)
            if cid is not None:
                try:
                    progs = epg.programmes_for(cid)
                except Exception:
                    progs = []
                now = datetime.now().astimezone()
                current = None
                for p in progs:
                    if p.start <= now and (p.stop is None or now < p.stop):
                        current = p
                        break
                if current:
                    start = current.start.astimezone().strftime("%H:%M")
                    stop = current.stop.astimezone().strftime("%H:%M") if current.stop else "--:--"
                    try:
                        stdscr.addstr(row, x, f" {icons.ICON_LIVE} Ahora", colors.pair(colors.PAIR_CURRENT) | curses.A_BOLD)
                    except curses.error:
                        pass
                    row += 1
                    if row < max_y:
                        # Usar Timeline visual si hay ancho
                        from .widgets import Timeline
                        title = current.title[:max(0, w - 14)]
                        # Timeline inline si cabe, sino fallback texto
                        if w >= 20:
                            Timeline.render(stdscr, row, x, w - 1, start, stop, title, is_current=True)
                        else:
                            try:
                                stdscr.addstr(row, x, f"  {start}-{stop}  {title}",
                                              colors.pair(colors.PAIR_NORMAL))
                            except curses.error:
                                pass
                        row += 1
                    if current.sub_title and row < max_y:
                        try:
                            stdscr.addstr(row, x, f"  {current.sub_title[:max(0, w - 2)]}",
                                          colors.pair(colors.PAIR_DIM))
                        except curses.error:
                            pass
                        row += 1
                    # Barra de progreso si hay stop
                    if current.stop and row < max_y and w >= 16:
                        try:
                            total = (current.stop - current.start).total_seconds()
                            elapsed = (now - current.start).total_seconds()
                            if total > 0:
                                frac = max(0.0, min(1.0, elapsed / total))
                                bar_w = max(4, w - 4)
                                filled = int(bar_w * frac)
                                bar = "━" * filled + "○" + "─" * max(0, bar_w - filled - 1)
                                stdscr.addstr(row, x, f"  {bar}", colors.pair(colors.PAIR_CURRENT) | curses.A_DIM)
                                row += 1
                        except Exception:
                            pass
                elif progs and row < max_y - 1:
                    try:
                        stdscr.addstr(row, x, f" {icons.ICON_LIVE_OFF} Sin emisión ahora",
                                      colors.pair(colors.PAIR_EMPTY))
                    except curses.error:
                        pass
                    row += 1
                    # Próximo programa con timeline tenue
                    nxt = next((p for p in progs if p.start > now), None)
                    if nxt and row < max_y:
                        start = nxt.start.astimezone().strftime("%H:%M")
                        from .widgets import Timeline
                        if w >= 20:
                            Timeline.render(stdscr, row, x, w - 1, start, nxt.stop.astimezone().strftime("%H:%M") if nxt.stop else "--:--", nxt.title[:max(0, w - 14)], is_current=False)
                        else:
                            try:
                                stdscr.addstr(row, x, f"  Próximo: {start}  {nxt.title[:max(0, w - 18)]}",
                                              colors.pair(colors.PAIR_DIM))
                            except curses.error:
                                pass
        elif row < max_y - 1 and epg is None:
            try:
                stdscr.addstr(row, x, f" {icons.ICON_EPG} Sin EPG cargado", colors.pair(colors.PAIR_EMPTY))
            except curses.error:
                pass

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        # Si las favoritas cambiaron fuera (p. ej. al volver de
        # FavoritesScreen), re-etiquetar una vez; en memoria y barato.
        get_urls = getattr(self.app.favorites, "favorite_urls", None)
        urls = self._fav_urls
        if callable(get_urls):
            try:
                urls = set(get_urls())
            except Exception:
                urls = self._fav_urls
        if urls != self._fav_urls:
            self._apply_filter(keep_selection=True)
        self.title = self._title_text()
        from .layout import sidebar_rect, detail_rect

        # Breakpoints: >=80 → 60/40, 70-79 → compacto 55/45, <70 → full-width
        COMPACT_BREAKPOINT = 70
        if max_x >= 80:
            show_detail = True
            sb = sidebar_rect(max_y, max_x, ratio=0.6)
            dr = detail_rect(max_y, max_x, ratio=0.6)
            sidebar_w = sb.w
            detail_x = dr.x
            detail_w = dr.w
            list_start_y = 1
            body_h = max(1, max_y - list_start_y - 2)
            sidebar_w = min(sidebar_w, max_x)
            detail_w = max(1, max_x - sidebar_w - 1)
        elif max_x >= COMPACT_BREAKPOINT:
            show_detail = True
            sb = sidebar_rect(max_y, max_x, ratio=0.55)
            dr = detail_rect(max_y, max_x, ratio=0.55)
            sidebar_w = sb.w
            detail_x = dr.x
            detail_w = max(22, dr.w)  # min 22 en compacto
            list_start_y = 1
            body_h = max(1, max_y - list_start_y - 2)
            sidebar_w = min(sidebar_w, max_x)
            detail_w = max(22, max_x - sidebar_w - 1)
        else:
            show_detail = False
            list_start_y = 1
            body_h = max(1, max_y - list_start_y - 2)
            sidebar_w = max_x

        # Lista de canales (izquierda). Las dos ramas vacías son
        # **mutuamente excluyentes** y de categoría distinta (§17):
        # `not visible_idx and not query` → no hay contenido; `and query` → la
        # búsqueda no encuentra nada. No se fusionan, porque un usuario con
        # 400 canales filtrados no debe leer "no hay canales".
        if not self.visible_idx and not self.query:
            ancho = sidebar_w if show_detail else max_x
            EmptyState.render(
                stdscr, list_start_y + body_h // 2, ancho,
                icons.ICON_TV,
                "La playlist no tiene canales.",
                "Vuelve a la lista de playlists para abrir otra.",
            )
        elif not self.visible_idx and self.query:
            ancho = sidebar_w if show_detail else max_x
            _render_no_results(stdscr, list_start_y + body_h // 2, ancho,
                               self.query, "canales")
        else:
            self.list.render(stdscr, list_start_y, 0, body_h, sidebar_w)
            # Contador flotante de posición cuando hay muchos
            if len(self.visible_idx) > body_h:
                pos = f" {self.list.selected + 1}/{len(self.visible_idx)} "
                try:
                    px = sidebar_w - len(pos) - 1
                    py = list_start_y + body_h - 1
                    if px >= 0 and py < max_y - 2:
                        stdscr.addstr(py, px, pos, colors.pair(colors.PAIR_DIM) | curses.A_DIM)
                except curses.error:
                    pass

        # Panel de detalle (derecha)
        if show_detail:
            # Separador vertical
            try:
                for sy in range(list_start_y, list_start_y + body_h):
                    stdscr.addstr(sy, sidebar_w, icons.BOX_V, colors.pair(colors.PAIR_BORDER))
            except curses.error:
                pass
            self._render_detail_panel(stdscr, detail_x, list_start_y, detail_w, body_h)

        # Modal de búsqueda (overlay centrado, filtra en vivo el fondo).
        if self.searching:
            from .widgets import SearchModal
            SearchModal.render_modal(
                stdscr,
                title="Buscar canal",
                query=self.query,
                matched=len(self.visible_idx),
                total=len(self.channels),
            )


class FavoritesScreen(Screen):
    """Canales marcados como favoritos (favorites.json).

    Cada fila es un canal y, si hay guía, lo que están dando ahora mismo. El
    programa se **resuelve al pintar** y no se guarda en ningún sitio: el
    favorito sigue siendo un canal, y `favorites.json` no cambia de formato
    (§4, §26, CA-09). Lo único que se guarda en memoria es la fila ya
    resuelta, que se tira en cuanto la guía cambia.

    La guía es un adorno, nunca un requisito: sin `Epg` la lista se dibuja
    exactamente como antes de esta mejora, y un canal sin correspondencia o sin
    emisión se marca con `—` en vez de desaparecer (§11.2, §11.4, §11.6).
    """

    title = "Favoritos"

    def header_context(self) -> str:
        return f"{icons.ICON_STAR} Favoritos"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.list = ScrollableList()
        self.channels: list[Channel] = []
        self.rows: list[FavoriteRow] = []
        # Identidad de la guía con la que se construyeron las filas, y si había
        # una carga en marcha. Es lo que decide cuándo hay que rehacerlas
        # (§16): no un temporizador, sino un cambio de datos.
        self._epg_visto: Epg | None = None
        self._epg_cargando_visto = False
        # ¿Las filas llevan programa? Sin guía no se reserva la segunda columna.
        self._hay_programas = False
        self.reload()

    def reload(self) -> None:
        """Relee los favoritos de `favorites.json` y vuelve a resolverlos.

        La lista en pantalla se actualiza en el render siguiente, que es
        inmediatamente después: aquí sólo se releen los datos.
        """
        self.channels = self.app.favorites.load()
        self.rebuild_rows()

    # --- Filas: la guía sólo se lee, nunca se pide ---------------------------

    def _epg(self) -> Epg | None:
        """La guía ya cargada en memoria, o `None`. Nunca pide una."""
        return getattr(self.app, "epg", None)

    def _epg_cargando(self) -> bool:
        """¿Hay una carga de EPG en marcha en segundo plano?

        Se lee con `getattr` a propósito: los dobles de test (y una app que no
        tenga el atributo) no tienen por qué saber de hilos de carga, y esta
        pantalla tiene que funcionar igual sin él.
        """
        hilo = getattr(self.app, "_epg_load_thread", None)
        if hilo is None:
            return False
        vivo = getattr(hilo, "is_alive", None)
        return bool(vivo()) if callable(vivo) else True

    def filas_al_dia(self) -> bool:
        """¿Las filas se construyeron con los datos que hay ahora?

        Se compara **identidad**, no contenido: cambiar de guía es un hecho, y
        recorrer 20 000 canales de la parrilla en cada frame para comprobar si
        es la misma es justo lo que el §17 prohíbe. Es la regla de la entrega
        sin temporizador: se rehace al cargar la guía y al volver de ella.
        """
        return (
            self._epg() is not self._epg_visto
            or self._epg_cargando() != self._epg_cargando_visto
        )

    def rebuild_rows(self, now: datetime | None = None) -> None:
        """Resuelve los favoritos contra la guía que ya está en memoria.

        Nunca llama a `ensure_epg()`: pedir la guía es trabajo de la pantalla
        de la guía, con su modal y su hilo. Aquí sólo se lee `app.epg`, que es
        lo que garantiza que mostrar Favoritos no genere ni una petición por
        favorito (CA-08).

        No toca la lista ni el cursor: el reparto en columnas depende del ancho
        y lo hace `render()`.
        """
        epg = self._epg()
        cargando = self._epg_cargando()
        self._epg_visto = epg
        self._epg_cargando_visto = cargando
        # ¿Hay algo que decir en la segunda columna? Sin guía y sin carga en
        # marcha, no: la lista se dibuja exactamente como antes de esta mejora
        # (§11.6). Con la guía a medio camino sí, y dice "Cargando…" (§11.5).
        self._hay_programas = epg is not None or cargando
        self.rows = build_rows(
            self.channels,
            epg,
            now if now is not None else datetime.now().astimezone(),
            epg_loading=cargando,
        )

    def _aplicar_columnas(self, width: int) -> None:
        """Vuelca las filas en la lista, en dos columnas si caben.

        Sólo se toca la lista si los textos han cambiado de verdad. Es lo que
        permite refrescar en cada frame sin que el usuario pierda el scroll a
        media lista cada vez que llega una tecla.
        """
        if not self._hay_programas:
            items = [format_channel_name(c, favorite=True) for c in self.channels]
            subitems: list[str] = []
        else:
            pares = layout_columns(self.rows, width)
            items = [canal for canal, _ in pares]
            subitems = (
                [programa for _, programa in pares]
                if two_columns_fit(width)
                else []
            )
        if items == self.list.items and subitems == self.list.subitems:
            return  # nada cambió: ni la lista ni su cursor se mueven
        # `set_items` deja el scroll arriba; recargar la misma lista no puede
        # saltar al primer favorito mientras el usuario está mirando el quince.
        top, selected = self.list.top, self.list.selected
        self.list.set_items(items, subitems=subitems or None, keep_selection=True)
        self.list.selected = selected
        self.list.top = top

    def current_channel(self) -> Channel | None:
        if not self.channels:
            return None
        return self.channels[self.list.selected]

    def shortcuts(self) -> str:
        return (
            "↑/↓ · Enter ▶ · f ★ · e EPG · p Reproductor · "
            "? Ayuda · t Tema · Esc ←"
        )

    def actions(self) -> list[Action]:
        """Catálogo de Favoritos.

        Todo lo que hay aquí es favorito por definición, así que `f` siempre
        dice «Quitar»: anunciar «Favorito» sobre una tecla que quita sería
        mentir (§9, R-04).

        `e EPG` es la misma acción que en Canales y con el mismo contrato: abre
        la guía que ya existe en lugar de inventar una segunda guía dentro de
        esta pantalla (§3).
        """
        hay = self.current_channel() is not None
        return [
            Action("Enter", V.VER, P.PRIMARIA, enabled=hay),
            Action("f", V.QUITAR, P.CONTEXTUAL, enabled=hay),
            Action("e", V.EPG, P.SECUNDARIA, enabled=hay),
            Action("Esc", V.VOLVER, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]

    def handle_mouse(self, mx: int, my: int, screen) -> bool:  # noqa: ANN001
        max_y, max_x = self.app.stdscr.getmaxyx()
        body_h = self.app.body_height()
        if my < 1 or my >= 1 + body_h:
            return False
        idx = self.list.top + (my - 1)
        if 0 <= idx < len(self.list.items):
            self.list.selected = idx
            return True
        return False

    def handle_key(self, key: int) -> dict | None:
        rows = self.app.body_height()
        if self.list.handle_key(key, rows):
            return None
        if key == ord("f"):
            channel = self.current_channel()
            if channel is None:
                msg = "No hay favoritos que quitar."
                try:
                    self.app.status.show(msg)
                except Exception:
                    pass
                try:
                    self.app.footer.show(msg)
                except Exception:
                    pass
                return None
            return {"action": "unfavorite", "channel": channel}
        if key == ord("e"):
            channel = self.current_channel()
            if channel is None:
                _warn(self.app, "No hay favoritos: marca canales con 'f' para ver su guía.")
                return None
            # Sin `epg_url`: un favorito puede venir de cualquier lista y la app
            # no tiene una "lista actual" única. La guía sabe esperar al hilo de
            # carga y, si no hay guía, abre su propio prompt.
            return {"action": "show_epg", "channel": channel}
        if key in (curses.KEY_ENTER, 10, 13):
            channel = self.current_channel()
            if channel is None:
                msg = "Sin favoritos: marca canales con 'f' en la lista de canales."
                try:
                    self.app.status.show(msg)
                except Exception:
                    pass
                try:
                    self.app.footer.show(msg)
                except Exception:
                    pass
                return None
            return {"action": "open_channel", "channel": channel}
        return None

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if self.filas_al_dia():
            self.rebuild_rows()
        self._aplicar_columnas(max_x)
        if not self.channels:
            # **Sin CTA, a propósito.** En esta pantalla `f` *quita* el
            # favorito, no lo añade (`actions()` lo dice: todo lo que hay aquí
            # es favorito por definición). Anunciar "[F] Favorito" sería una
            # promesa falsa: una tecla dibujada sin acción detrás (R-04). El
            # mensaje explica cómo llegar, que es lo que faltaba.
            EmptyState.render(
                stdscr, max_y // 2, max_x,
                icons.ICON_STAR_OFF,
                "No tienes favoritos",
                "Pulsa F sobre un canal para añadirlo aquí.",
            )
        else:
            self.list.render(stdscr, 1, 0, self.app.body_height(), max_x)


class RecentsScreen(Screen):
    title = "Recientes"

    def header_context(self) -> str:
        return f"{icons.ICON_ARCHIVE} Recientes"

    def __init__(self, app) -> None:  # noqa: ANN001
        super().__init__(app)
        self.list = ScrollableList()
        self.items = app.recents.load()
        self.list.set_items(self._filas(), self._fechas())

    def _fechas(self) -> list[str]:
        """Segunda columna: cuándo se reprodujo cada canal (FR-REC-03).

        Es la columna de texto secundario que `ScrollableList` ya tenía para
        Favoritos, así que aquí no hay widget nuevo ni ruta de render nueva.
        """
        ahora = datetime.now(timezone.utc)
        return [relative_time(i.at_utc, ahora) for i in self.items]

    def _filas(self) -> list[str]:
        return [f"{i.name} ({i.player})" for i in self.items]

    def current_item(self):
        if not self.items:
            return None
        return self.items[self.list.selected]

    def current_channel(self) -> Channel | None:
        item = self.current_item()
        if item is None:
            return None
        return Channel(name=item.name, url=item.url, group=item.group)

    def shortcuts(self) -> str:
        return "↑/↓ · Enter ▶ · f ★ · r Limpiar · ? Ayuda · t Tema · Esc ←"

    def actions(self) -> list[Action]:
        """Catálogo de Recientes.

        `f` alterna, así que su etiqueta sigue al estado real del reciente
        (§9, R-04) — igual que en Canales, porque es la misma tecla.
        """
        channel = self.current_channel()
        hay = channel is not None
        etiqueta = V.QUITAR if _is_favorite(self.app, channel) else V.FAVORITO
        return [
            Action("Enter", V.VER, P.PRIMARIA, enabled=hay),
            Action("f", etiqueta, P.CONTEXTUAL, enabled=hay),
            Action("r", V.LIMPIAR, P.ORGANIZACION, enabled=bool(self.items)),
            Action("Esc", V.VOLVER, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]

    def handle_key(self, key: int) -> dict | None:
        rows = self.app.body_height()
        if self.list.handle_key(key, rows):
            return None
        if key == ord("r"):
            self.app.recents.clear()
            self.items = []
            self.list.set_items([])
            self.app.status.show("Recientes limpiados.")
            return None
        if key == ord("f"):
            ch = self.current_channel()
            if ch is None:
                _warn(self.app, "No hay reciente para marcar.")
                return None
            return {"action": "toggle_favorite", "channel": ch}
        if key in (curses.KEY_ENTER, 10, 13):
            ch = self.current_channel()
            if ch is None:
                _warn(self.app, "No hay reciente para abrir.")
                return None
            return {"action": "open_channel", "channel": ch}
        return None

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.items:
            # Sin CTA: "Reproducir canal" sería una acción que esta pantalla
            # no tiene con la lista vacía (aquí `Enter` abre *un* reciente, y
            # no hay ninguno). Se dice de dónde saldrán, no se promete.
            EmptyState.render(
                stdscr, max_y // 2, max_x,
                icons.ICON_TIME,
                "No hay canales recientes",
                "Los canales que reproduzcas aparecerán aquí.",
            )
        else:
            self.list.render(stdscr, 1, 0, self.app.body_height(), max_x)


def _miembros_editables(col) -> str | None:  # noqa: ANN001
    """Por qué no se pueden tocar los canales de `col`; `None` si sí se puede.

    Vive aquí y no en la pantalla ni en `App` porque **los dos** necesitan
    decir exactamente lo mismo: la nota de la lista de casillas y el aviso al
    pulsar la tecla. Si cada uno escribiera su frase, el usuario leería dos
    explicaciones distintas para el mismo «no».

    La razón de fondo no es un permiso, es una consecuencia: los canales de una
    colección inteligente los decide su regla, así que quitar uno a mano sería
    deshacerlo hasta la siguiente evaluación.
    """
    tipo = getattr(col, "type", "")
    if tipo == "manual":
        return None
    nombre = getattr(col, "name", "") or "(sin nombre)"
    if tipo == "smart":
        return (f"«{nombre}» la decide una regla: sus canales no se eligen a mano.")
    return f"«{nombre}» es una vista de la app, no una colección tuya."


def _catalogo_de(app) -> list[Channel]:  # noqa: ANN001
    """Canales que la pantalla de colecciones puede considerar existentes.

    Lo que hay en memoria, no lo que hay en disco: una colección guarda
    *referencias* a canales, y resolverlas necesita los canales. Sin lista
    cargada se devuelve una lista vacía, que es exactamente lo que la pantalla
    puede afirmar con honestidad.
    """
    fn = getattr(app, "catalogo_canales", None)
    if not callable(fn):
        return []
    try:
        return list(fn() or [])
    except Exception:
        return []


class CollectionsScreen(Screen):
    """Colecciones (Fase 4 · C-07) — la lista de lo que el usuario agrupó.

    Lo único que esta pantalla tiene que resolver bien es una pregunta que el
    pie no puede contestar: **¿qué es esto?** Una colección puede ser un montón
    de canales que él eligió a mano, el resultado de una regla que se recalcula
    sola, o una vista de la app (Favoritos, Recientes) que no contiene nada
    propio. Por eso el tipo va **en la fila** (§ plan U-03) y no en el pie: el
    pie describe la acción que se puede hacer, la fila describe la fila.

    De ahí salen las dos decisiones que parecen arbitrarias y no lo son:

    - Las colecciones **del sistema no se editan ni se borran**. No es un
      capricho: son vistas derivadas, y renombrar «Favoritos» no renombra
      nada, promete algo que no ocurre.
    - Su contador dice **«vista derivada»** y no «0 canales». `evaluar_coleccion`
      devuelve vacío para `system` sin reglas, así que un contador ingenuo
      enseñaría un cero que es mentira: no tiene cero canales, tiene los que
      tenga la vista.

    Las **referencias obsoletas** (un canal que ya no está en ninguna lista) se
    avisan y se siguen mostrando. Una colección no se cae porque un canal
    desapareció: si se cayera, el usuario perdería el resto por un hueco (C-07).
    """

    title = "Colecciones"

    #: Cómo se dice cada tipo **en la fila**. Es el dato que decide si el
    #: usuario puede esperar que la lista cambie sola, y por eso tiene icono y
    #: palabra: no basta con una letra.
    TIPOS: dict[str, tuple[str, str]] = {
        "manual": ("manual", icons.ICON_STAR_OFF),
        "smart": ("inteligente", icons.ICON_SEARCH),
        "system": ("del sistema", icons.ICON_STAR),
    }

    #: Un tipo desconocido (un `collections.json` editado a mano, por ejemplo)
    #: no puede borrarse de la lista: se muestra como es y se avisa, en vez de
    #: inventar un cuarto tipo o esconder la fila.
    TIPO_DESCONOCIDO = ("tipo desconocido", icons.ICON_CHEVRON)

    def header_context(self) -> str:
        total = len(self.collections)
        plural = "colección" if total == 1 else "colecciones"
        return f"{icons.ICON_LIST} Colecciones · {total} {plural}"

    def __init__(self, app, catalogo: list[Channel] | None = None) -> None:  # noqa: ANN001
        super().__init__(app)
        self.list = ScrollableList()
        #: Catálogo **fijo** mientras la pantalla viva. No se pide a `App` en
        #: cada tecla: el gestor de colecciones cachea por `id()` de la lista,
        #: y una lista nueva en cada llamada no cachea nada (evaluaría la
        #: colección entera del catálogo por cada tecla).
        self.catalogo: list[Channel] = (
            list(catalogo) if catalogo is not None else _catalogo_de(app)
        )
        self.collections: list = []
        #: cid → (texto de la segunda columna, referencias obsoletas).
        self._detalles: dict[str, tuple[str, int]] = {}
        self.reload()

    # -- datos --------------------------------------------------------------
    def reload(self) -> None:
        """Relee las colecciones y vuelve a contar. Idempotente."""
        try:
            self.collections = list(self.manager.listar())
        except Exception as exc:
            # Un `collections.json` ilegible no puede impedir abrir la app:
            # se dice y se sigue con la lista vacía (degradar > tumbar).
            self.collections = []
            _warn(self.app, f"No se pudieron leer las colecciones: {exc}")
        self._detalles = {c.id: self._detalle(c) for c in self.collections}
        self.list.set_items(self._filas(), self._contadores())

    @property
    def manager(self):
        return getattr(self.app, "collections", None)

    def current(self):
        if not self.collections:
            return None
        return self.collections[self.list.selected]

    def _tipo_de(self, col) -> tuple[str, str]:  # noqa: ANN001
        return self.TIPOS.get(getattr(col, "type", ""), self.TIPO_DESCONOCIDO)

    def _detalle(self, col) -> tuple[str, int]:  # noqa: ANN001
        """Segunda columna y número de referencias obsoletas de una colección.

        Todo lo que aquí puede fallar (regla corrupta, canal que ya no está,
        catálogo que aún no se ha cargado) se convierte en un texto que explica
        el estado, nunca en una excepción: la lista tiene que poder pintarse
        aunque la última colección guardada sea inválida.
        """
        manager = self.manager
        tipo = getattr(col, "type", "")
        if tipo == "system":
            # Sin catálogo no hay nada que contar, y con él tampoco: lo que
            # hay es la vista, no sus canales.
            return ("vista derivada", 0)
        if manager is None:
            return ("—", 0)
        try:
            miembros = manager.miembros(col.id, self.catalogo)
        except Exception:
            miembros = []
        obsoletas = 0
        if tipo == "manual" and self.catalogo:
            # **Sólo con catálogo.** Sin ninguna lista en memoria, todas las
            # referencias parecen obsoletas, y avisar de eso sería inventar un
            # problema que el usuario resuelve abriendo su lista. Aquí no se
            # sabe nada, y la pantalla no dice que sí (C-07 avisa de hechos,
            # no de sospechas).
            try:
                obsoletas = len(manager.obsolete_refs(col.id, self.catalogo))
            except Exception:
                obsoletas = 0
        n = len(miembros)
        texto = "1 canal" if n == 1 else f"{n} canales"
        if obsoletas:
            # En singular y plural, porque «1 referencia obsoleta» y «2
            # referencias obsoletas» no son el mismo aviso.
            sufijo = "referencia obsoleta" if obsoletas == 1 else "referencias obsoletas"
            texto = f"{texto} · {obsoletas} {sufijo}"
        return (texto, obsoletas)

    def _filas(self) -> list[str]:
        filas = []
        for col in self.collections:
            tipo, icono = self._tipo_de(col)
            filas.append(f"{icono} {col.name} · {tipo}")
        return filas

    def _contadores(self) -> list[str]:
        return [self._detalles.get(c.id, ("—", 0))[0] for c in self.collections]

    # -- catálogo de acciones ----------------------------------------------
    def shortcuts(self) -> str:
        if not self.collections:
            # Sin filas no hay nada que abrir, renombrar ni borrar, y `shortcuts`
            # es la verdad: anunciar teclas que no hacen nada es exactamente lo
            # que la barra no puede hacer (§10). `Esc` y `?` sí se anuncian, y
            # `Esc` sí se declara: salir siempre tiene que estar a mano.
            return "a Nueva · ? Ayuda · t Tema · Esc Volver · q Salir"
        return (
            "↑/↓ mover · Enter Detalle · a Nueva · e Renombrar · d Borrar"
            " · m Miembros · R Regla · ? Ayuda · t Tema · Esc Volver"
            " · q Salir"
        )

    def actions(self) -> list[Action]:
        col = self.current()
        if not self.collections:
            return [
                Action("a", V.NUEVA, P.FRECUENTE),
                Action("Esc", V.VOLVER, P.NAVEGACION),
                Action("?", V.AYUDA, P.AYUDA, essential=True),
            ]
        # Una vista derivada no se renombra ni se borra. La tecla **no**
        # desaparece: se marca `enabled=False`, que es la forma de decir «aquí
        # no hay nada sobre lo que actuar» sin mentir sobre la tecla (§8).
        editable = col is not None and getattr(col, "type", "") != "system"
        # Quitar canales es otra cosa: sólo lo admite una colección **manual**.
        # En una inteligente los canales los decide la regla, así que `m`
        # ofrecería sacar un canal que volvería en la siguiente evaluación.
        manual = col is not None and getattr(col, "type", "") == "manual"
        return [
            Action("Enter", V.DETALLE, P.PRIMARIA),
            Action("a", V.NUEVA, P.FRECUENTE),
            Action("e", V.RENOMBRAR, P.ORGANIZACION, enabled=editable),
            Action("d", V.BORRAR, P.ORGANIZACION, enabled=editable),
            Action("m", V.MIEMBROS, P.ORGANIZACION, enabled=manual),
            # `R` convierte una manual en inteligente y cambia la regla de una
            # inteligente. No se ofrece en una vista derivada: no hay nada que
            # decidir en algo que ya está decidido por la app.
            Action("R", V.REGLA, P.ORGANIZACION, enabled=editable),
            Action("Esc", V.VOLVER, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]

    def handle_key(self, key: int) -> dict | None:
        rows = self.app.body_height()
        if self.list.handle_key(key, rows):
            return None
        col = self.current()
        if key == ord("a"):
            return {"action": "add_collection"}
        if key == ord("e"):
            if col is None:
                _warn(self.app, "No hay colección que renombrar.")
                return None
            if getattr(col, "type", "") == "system":
                # No es un veto silencioso: se explica por qué no se puede.
                _warn(
                    self.app,
                    f"«{col.name}» es una vista de la app, no una colección tuya. "
                    "No se renombra.",
                )
                return None
            return {"action": "rename_collection", "cid": col.id}
        if key == ord("d"):
            if col is None:
                _warn(self.app, "No hay colección que borrar.")
                return None
            if getattr(col, "type", "") == "system":
                _warn(
                    self.app,
                    f"«{col.name}» es una vista de la app, no una colección tuya. "
                    "No se borra.",
                )
                return None
            return {"action": "delete_collection", "cid": col.id}
        if key == ord("m"):
            if col is None:
                _warn(self.app, "No hay colección cuyos miembros gestionar.")
                return None
            bloqueo = _miembros_editables(col)
            if bloqueo:
                _warn(self.app, bloqueo)
                return None
            return {"action": "collection_members", "cid": col.id}
        if key == ord("R"):
            if col is None:
                _warn(self.app, "No hay colección a la que darle una regla.")
                return None
            if getattr(col, "type", "") == "system":
                _warn(
                    self.app,
                    f"«{col.name}» es una vista de la app, no una colección tuya. "
                    "No se le pone regla: sus canales ya estándecided por la app.",
                )
                return None
            return {"action": "collection_rule", "cid": col.id}
        if key in (curses.KEY_ENTER, 10, 13):
            if col is None:
                _warn(self.app, "No hay colección que abrir.")
                return None
            return {"action": "show_collection", "cid": col.id}
        return None

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.collections:
            # La CTA anuncia `a`, que existe y abre el modal de alta (§23).
            EmptyState.render(
                stdscr, max_y // 2, max_x,
                icons.ICON_LIST,
                "No hay colecciones",
                "Agrupa canales en una colección para verlos juntos.",
                "[A] Nueva colección",
            )
            return
        self.list.render(stdscr, 1, 0, self.app.body_height(), max_x)


class GroupsScreen(Screen):
    """Grupos (group-title) de una playlist — vista cards.

    '/' abre el modal de búsqueda: filtra mientras se escribe; Esc limpia,
    Enter confirma la consulta y vuelve a navegación normal.
    """

    title = "Grupos"

    def header_context(self) -> str:
        return f"{icons.ICON_GROUP} Grupos"

    def __init__(self, app, playlist: Playlist) -> None:
        super().__init__(app)
        self.playlist = playlist
        self.keys: list[str | None] = []
        self.counts: dict[str | None, int] = {}
        self.selected: int = 0
        self._first_row: int = 0
        self.query: str = ""
        self.searching: bool = False
        self.visible_keys: list[str | None] = []
        self.reload()

    def reload(self) -> None:
        groups = groups_of(self.playlist.channels)
        self.keys = list(groups)
        self.counts = {k: len(v) for k, v in groups.items()}
        self._apply_filter()

    def refresh_from_playlist(self, fresh: Playlist) -> None:
        """Sustituye el contenido por un re-parseo fresco de la misma fuente.

        Preserva query y grupo seleccionado (si sigue existiendo).
        """
        self.playlist = fresh
        groups = groups_of(fresh.channels)
        self.keys = list(groups)
        self.counts = {k: len(v) for k, v in groups.items()}
        self._apply_filter(keep_selection=True)

    def _apply_filter(self, keep_selection: bool = False) -> None:
        prev = self.current_group() if keep_selection else None
        q = self.query.casefold()
        self.visible_keys = [
            k for k in self.keys
            if not q or q in (k or "(sin grupo)").casefold()
        ]
        if prev is not None:
            for pos, k in enumerate(self.visible_keys):
                if k is prev:
                    self.selected = pos
                    self._clamp_scroll()
                    return
        if self.selected >= len(self.visible_keys):
            self.selected = max(0, len(self.visible_keys) - 1)
        self._clamp_scroll()

    def _visible_rows(self, max_y: int) -> int:
        """Cuántas filas de cards caben en pantalla."""
        card_h = 4
        gap = 1
        start_y = 1
        available = max_y - start_y - 2  # reservar para header/footer
        if available <= 0:
            return 0
        return max(1, available // (card_h + gap))

    def _clamp_scroll(self) -> None:
        """Ajusta _first_row para que selected siempre sea visible."""
        max_y, max_x = self.app.stdscr.getmaxyx()
        cols = 2 if max_x >= 100 else 1
        rows = self._visible_rows(max_y)
        max_sel = max(0, len(self.visible_keys) - 1)
        self.selected = max(0, min(self.selected, max_sel))
        sel_row = self.selected // cols
        if sel_row < self._first_row:
            self._first_row = sel_row
        elif sel_row >= self._first_row + rows:
            self._first_row = sel_row - rows + 1
        self._first_row = max(0, self._first_row)

    def current_group(self) -> str | None:
        if not self.visible_keys:
            return None
        return self.visible_keys[self.selected]

    def shortcuts(self) -> str:
        if self.searching:
            return "Escribir filtra · Enter confirmar · Ctrl-U limpiar · Esc salir"
        return "↑/↓/←/→ · Enter abrir · / Buscar · R Actualizar · r Recientes · ? Ayuda · t Tema · Esc ←"

    def actions(self) -> list[Action]:
        """Catálogo de Grupos.

        En búsqueda, el juego de teclas es el de Canales (§34): confirmar,
        vaciar, cancelar.
        """
        if self.searching:
            return [
                Action("Enter", V.CONFIRMAR, P.PRIMARIA),
                Action("Ctrl-U", V.VACIAR, P.FRECUENTE),
                Action("Esc", V.CANCELAR, P.NAVEGACION),
            ]
        return [
            Action("Enter", V.ABRIR, P.PRIMARIA, enabled=self.current_group() is not None),
            Action("/", V.BUSCAR, P.FRECUENTE),
            Action("R", V.ACTUALIZAR, P.ORGANIZACION),
            Action("Esc", V.VOLVER, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]

    def handle_mouse(self, mx: int, my: int, screen) -> bool:  # noqa: ANN001
        if not self.visible_keys:
            return False
        max_y, max_x = self.app.stdscr.getmaxyx()
        cols = 2 if max_x >= 100 else 1
        card_w = max(28, (max_x - 2 * (cols - 1)) // cols)
        gap = 2
        total_w = cols * card_w + (cols - 1) * gap
        start_x = max(0, (max_x - total_w) // 2)
        card_h = 4
        start_y = 1
        for i, _ in enumerate(self.visible_keys):
            row = i // cols
            if row < self._first_row:
                continue
            col = i % cols
            cx = start_x + col * (card_w + gap)
            cy = start_y + (row - self._first_row) * (card_h + 1)
            if cy <= my < cy + card_h and cx <= mx < cx + card_w:
                self.selected = i
                self._clamp_scroll()
                return True
        return False

    def _feed_search(self, key: int) -> dict | None:
        if key in (curses.KEY_ENTER, 10, 13):
            self.searching = False
            self.app.footer.show("")
            return None
        if key in (27,):  # Esc: limpia la consulta y sale de búsqueda
            self.searching = False
            self.query = ""
            self._apply_filter()
            self.app.status.show("Búsqueda limpiada.")
            self.app.footer.show("Búsqueda limpiada.")
            return None
        if key in (21,):  # Ctrl-U: borrar el contenido del modal sin cerrarlo
            self.query = ""
            self._apply_filter(keep_selection=True)
            return None
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.query = self.query[:-1]
            self._apply_filter(keep_selection=True)
            return None
        if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT,
                   curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            cols = 2 if self.app.stdscr.getmaxyx()[1] >= 100 else 1
            max_sel = max(0, len(self.visible_keys) - 1)
            if key == curses.KEY_UP:
                self.selected = max(0, self.selected - cols)
            elif key == curses.KEY_DOWN:
                self.selected = min(max_sel, self.selected + cols)
            elif key == curses.KEY_LEFT:
                self.selected = max(0, self.selected - 1)
            elif key == curses.KEY_RIGHT:
                self.selected = min(max_sel, self.selected + 1)
            elif key == curses.KEY_PPAGE:
                rows = self._visible_rows(self.app.stdscr.getmaxyx()[0])
                self.selected = max(0, self.selected - max(1, rows - 1) * cols)
            elif key == curses.KEY_NPAGE:
                rows = self._visible_rows(self.app.stdscr.getmaxyx()[0])
                self.selected = min(max_sel, self.selected + max(1, rows - 1) * cols)
            elif key == curses.KEY_HOME:
                self.selected = 0
            elif key == curses.KEY_END:
                self.selected = max_sel
            self._clamp_scroll()
            return None
        if 32 <= key < 127 or key > 160:
            self.query += chr(key)
            self._apply_filter(keep_selection=True)
            return None
        return None

    def handle_key(self, key: int) -> dict | None:
        if self.searching:
            return self._feed_search(key)
        if key == ord("R") or key == _KEY_F5:
            return {"action": "reload_playlist"}
        if not self.visible_keys:
            return None
        max_y, max_x = self.app.stdscr.getmaxyx()
        cols = 2 if max_x >= 100 else 1
        if key == curses.KEY_UP:
            self.selected = max(0, self.selected - cols)
        elif key == curses.KEY_DOWN:
            self.selected = min(len(self.visible_keys) - 1, self.selected + cols)
        elif key == curses.KEY_LEFT:
            self.selected = max(0, self.selected - 1)
        elif key == curses.KEY_RIGHT:
            self.selected = min(len(self.visible_keys) - 1, self.selected + 1)
        elif key == ord("/"):
            self.searching = True
            return None
        elif key in (curses.KEY_ENTER, 10, 13):
            return {
                "action": "open_group",
                "playlist": self.playlist,
                "group": self.current_group(),
            }
        else:
            return None
        self._clamp_scroll()
        return None

    def _render_card(self, stdscr: curses.window, name: str | None, count: int,
                     card_x: int, card_y: int, card_w: int, selected: bool) -> None:
        max_y = stdscr.getmaxyx()[0]
        sel_pair = colors.selection_pair_for_kind(getattr(self.playlist, "kind", "m3u"))
        border_attr = colors.pair(colors.PAIR_PRIMARY if selected else colors.PAIR_BORDER)
        text_attr = colors.pair(sel_pair if selected else colors.PAIR_NORMAL)
        accent_attr = colors.pair(colors.PAIR_ACCENT)

        card_h = 4
        if card_y + card_h > max_y - 2:
            return

        # Fondo sólido para selected
        if selected:
            try:
                for r in range(card_h):
                    stdscr.addstr(card_y + r, card_x, " " * card_w, colors.pair(sel_pair))
            except curses.error:
                pass

        bdr = border_attr | curses.A_BOLD if selected else border_attr
        try:
            stdscr.addstr(card_y, card_x,
                          f"{icons.BOX_R_TL}{icons.BOX_H * (card_w - 2)}{icons.BOX_R_TR}",
                          bdr)
            stdscr.addstr(card_y + card_h - 1, card_x,
                          f"{icons.BOX_R_BL}{icons.BOX_H * (card_w - 2)}{icons.BOX_R_BR}",
                          bdr)
            for r in range(1, card_h - 1):
                stdscr.addstr(card_y + r, card_x, icons.BOX_V, bdr)
                stdscr.addstr(card_y + r, card_x + card_w - 1, icons.BOX_V, bdr)
        except curses.error:
            pass

        inner_w = max(0, card_w - 4)
        display_name = name or "(sin grupo)"
        try:
            stdscr.addstr(card_y + 1, card_x + 1,
                          f" {icons.ICON_GROUP} {display_name[:inner_w]}",
                          text_attr | curses.A_BOLD)
        except curses.error:
            pass

        label = "canal" if count == 1 else "canales"
        try:
            stdscr.addstr(card_y + 2, card_x + 1,
                          f"   {count} {label}",
                          accent_attr)
        except curses.error:
            pass

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.keys:
            EmptyState.render(stdscr, max_y // 2, max_x,
                              icons.ICON_GROUP,
                              "No hay grupos en esta playlist.", "")
            return

        if not self.visible_keys and self.query:
            # Búsqueda sin resultados: categoría propia (§17), no "no hay
            # grupos". Antes esto era un `addstr` a mano, distinto del de
            # Canales; ahora los dos comparten el mismo bloque.
            _render_no_results(stdscr, max_y // 2, max_x, self.query, "grupos")
            if self.searching:
                from .widgets import SearchModal
                SearchModal.render_modal(
                    stdscr,
                    title="Buscar grupo",
                    query=self.query,
                    matched=len(self.visible_keys),
                    total=len(self.keys),
                )
            return

        cols = 2 if max_x >= 100 else 1
        card_w = max(28, (max_x - 2 * (cols - 1)) // cols)
        gap = 2
        total_w = cols * card_w + (cols - 1) * gap
        start_x = max(0, (max_x - total_w) // 2)
        card_h = 4
        start_y = 1
        visible_rows = self._visible_rows(max_y)

        for i, key in enumerate(self.visible_keys):
            row = i // cols
            if row < self._first_row:
                continue
            if row >= self._first_row + visible_rows:
                break
            col = i % cols
            cx = start_x + col * (card_w + gap)
            cy = start_y + (row - self._first_row) * (card_h + 1)
            if cy + card_h > max_y - 2:
                break
            self._render_card(stdscr, key, self.counts[key], cx, cy, card_w, i == self.selected)
        # Indicador de paginación
        total_rows = -(-len(self.visible_keys) // cols) if self.visible_keys else 0
        if total_rows > visible_rows:
            info = f" {self._first_row + 1}-{min(total_rows, self._first_row + visible_rows)}/{total_rows} "
            try:
                px = max(0, (max_x - len(info)) // 2)
                py = max_y - 3
                if py >= 1:
                    stdscr.addstr(py, px, info, colors.pair(colors.PAIR_DIM) | curses.A_DIM)
            except curses.error:
                pass
        # Modal de búsqueda (overlay centrado, filtra en vivo el fondo).
        if self.searching:
            from .widgets import SearchModal
            SearchModal.render_modal(
                stdscr,
                title="Buscar grupo",
                query=self.query,
                matched=len(self.visible_keys),
                total=len(self.keys),
            )


class ResolutionScreen(Screen):
    """Selector de resolución — vista pills/buttons."""

    def __init__(self, app, channel: Channel, variants: list[Channel]) -> None:
        super().__init__(app)
        self.channel = channel
        self.variants = variants
        self.title = f"Resolución · {resolutions.base_name(channel)}"
        # Abrir **sobre la variante elegida**: las variantes llegan ordenadas de
        # peor a mejor, así que empezar en 0 hacía que pulsar Enter aquí
        # reprodujera otra calidad sin que se notara — en la lista real, 2 de
        # cada 3 aperturas. Si el canal no está en la lista, la primera.
        self.selected = next(
            (i for i, v in enumerate(variants) if v is channel), 0
        )

    def header_context(self) -> str:
        return "Calidad"

    def shortcuts(self) -> str:
        return "←/→ · Enter ▶ · ? Ayuda · t Tema · Esc ←"

    def actions(self) -> list[Action]:
        """Catálogo del selector de calidad (Calidad)."""
        hay = bool(self.variants)
        return [
            Action("Enter", V.CONTINUAR, P.PRIMARIA, enabled=hay),
            Action("←→", V.ELEGIR, P.FRECUENTE, enabled=hay),
            Action("Esc", V.VOLVER, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]

    def handle_key(self, key: int) -> dict | None:
        if key == curses.KEY_LEFT:
            self.selected = max(0, self.selected - 1)
        elif key == curses.KEY_RIGHT:
            self.selected = min(len(self.variants) - 1, self.selected + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            return {"action": "select_player", "channel": self.variants[self.selected]}
        return None

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.variants:
            return

        ch = self.channel
        icon = icons.ICON_RADIO if ch.radio else icons.ICON_TV
        try:
            stdscr.addstr(1, 0, f" {icon} {ch.name}", colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
        except curses.error:
            pass

        # Pills de resolución con borde redondeado. La etiqueta sale de
        # `variant_labels`, no de `detect`: dos entradas pueden ser las dos SD
        # (o las dos HD) y dos botones iguales no son un botón.
        pills: list[str] = []
        pill_widths: list[int] = []
        for etiqueta in resolutions.variant_labels(self.variants):
            pill = f" {etiqueta} "
            pills.append(pill)
            pill_widths.append(cell_width(pill))

        total_w = sum(pill_widths) + 2 * (len(pills) - 1)
        pill_x = max(0, (max_x - total_w) // 2)
        pill_y = max(2, (max_y - 3) // 2)

        try:
            x = pill_x
            for i, pill in enumerate(pills):
                # Una etiqueta larga puede no caber en una terminal estrecha:
                # se recorta por celdas y se deja de pintar el resto, en vez
                # de desbordar la fila (§39, §43).
                room = max(0, max_x - 1 - x)
                visible = clip_cells(pill, room)
                if not visible:
                    break
                if i == self.selected:
                    # Selected: fondo sólido + BOLD + REVERSE sutil
                    fill = " " * cell_width(visible)
                    stdscr.addstr(pill_y, x, fill, colors.pair(colors.PAIR_SELECTED))
                    stdscr.addstr(pill_y, x, visible,
                                  colors.pair(colors.PAIR_SELECTED) | curses.A_BOLD | curses.A_REVERSE)
                else:
                    stdscr.addstr(pill_y, x, visible, colors.pair(colors.PAIR_DIM))
                x += cell_width(visible) + 2
        except curses.error:
            pass

        sel = self.variants[self.selected]
        name = sel.name
        try:
            nx = max(0, (max_x - cell_width(name)) // 2)
            stdscr.addstr(pill_y + 2, nx, clip_cells(name, max(0, max_x - 1)),
                          colors.pair(colors.PAIR_NORMAL))
        except curses.error:
            pass


class TrackOptionsScreen(Screen):
    """Selector de audio / subtítulos / calidad de un mismo canal.

    Con el estilo visual de ``PlayerScreen``/``ResolutionScreen``: secciones,
    marca de lo seleccionado y atajos. Tres reglas la gobiernan y ninguna es
    negociable (SDD §33/§34/§49, plan F6):

    - **sólo se pintan las secciones con 2 o más opciones**: con una pista
      única no hay nada que elegir, y ofrecer un menú de una opción sería
      inventar una decisión que el usuario no tiene;
    - **lo que va a aplicarse va marcado**: se entra viendo ya qué se va a
      reproducir, porque las preferencias se resuelven antes de abrir;
    - **``0`` devuelve a Automático/Desactivados** de la sección actual;
      ``Enter`` confirma y devuelve el control al App, que a continuación
      muestra el selector de reproductor (pistas primero, reproductor
      después). Si el reproductor **ya estaba elegido** —este selector se
      abrió después, porque el sondeo del canal llegó tarde—, ``Enter``
      reproduce directamente con él: preguntarlo otra vez obligaba al
      usuario a elegir dos veces lo mismo.
    """

    def __init__(  # noqa: ANN001
        self,
        app,
        channel: Channel,
        session,  # noqa: ANN001 - TrackSession
        player_name: str | None = None,
        kind: str | None = None,
    ) -> None:
        super().__init__(app)
        self.channel = channel
        self.session = session
        self.player_name = player_name
        self.title = f"Audio y calidad · {channel.name}"
        self.options = session.options(player_name)
        self.kinds: list[str] = options_kinds(self.options, kind)
        # Índice global de la opción enfocada, sobre la lista aplanada.
        self.selected: int = 0
        #: Opciones que el usuario ha marcado con `Espacio`: (sección, id).
        #: Es un ``set`` a propósito — elegir dos veces lo mismo no cuenta como
        #: dos elecciones — y vive en la pantalla, no en la sesión: se borra al
        #: salir y no se guarda como preferencia.
        self.marcados: set[tuple[str, str]] = set()
        self._focus()

    def header_context(self) -> str:
        return "Audio y calidad"

    # -- navegación ---------------------------------------------------------

    def _rows(self) -> list[tuple[str | None, object]]:
        """[(sección, opción)] aplanado, sólo con las secciones visibles."""
        rows: list[tuple[str | None, object]] = []
        for kind in self.kinds:
            rows.append((SECTION_LABELS.get(kind, kind), None))
            for choice in self.options.choices_for(kind):
                rows.append((None, choice))
        return rows

    def _flatten(self) -> list[object]:
        return [choice for _section, choice in self._rows() if choice is not None]

    def _section_of(self, index: int) -> str | None:
        opciones = self._flatten()
        if not opciones or index < 0 or index >= len(opciones):
            return None
        actual = opciones[index]
        for kind in self.kinds:
            if actual in self.options.choices_for(kind):
                return kind
        return None

    def _focus(self, prefer_kind: str | None = None) -> None:
        """Deja el foco sobre la opción que va a aplicarse.

        Si `prefer_kind` viene dado y tiene opción marcada, se queda en esa
        sección: cambiar la calidad no debe saltar el cursor al audio.
        """
        opciones = self._flatten()
        if not opciones:
            self.selected = 0
            return
        marcadas = [i for i, c in enumerate(opciones) if c.is_selected]
        if prefer_kind:
            for indice in marcadas:
                if opciones[indice] in self.options.choices_for(prefer_kind):
                    self.selected = indice
                    return
        self.selected = marcadas[0] if marcadas else 0

    def _move(self, delta: int) -> None:
        """Mueve el cursor y aplica lo que queda marcado.

        El cursor **es** la selección (como en ``PlayerScreen``): así lo que
        se ve marcado es exactamente lo que se va a lanzar, y `Enter` no
        puede confirmar otra cosa por sorpresa.
        """
        total = len(self._flatten())
        if not total:
            return
        destino = max(0, min(total - 1, self.selected + delta))
        if destino == self.selected:
            return
        self.selected = destino
        self._aplicar_cursor()

    def _jump(self, delta: int) -> None:
        """Salta de sección con ←/→ (o Tab)."""
        if not self.kinds:
            return
        kind = self._section_of(self.selected)
        if kind is None:
            self.selected = 0
            return
        indice = self.kinds.index(kind)
        destino = self.kinds[(indice + delta) % len(self.kinds)]
        self._focus_first(destino)

    def _focus_first(self, kind: str) -> None:
        opciones = self._flatten()
        for indice, choice in enumerate(opciones):
            if choice in self.options.choices_for(kind):
                self.selected = indice
                self._aplicar_cursor()
                return
        self.selected = 0

    def _aplicar_cursor(self) -> None:
        """Aplica la opción enfocada a la selección."""
        kind = self._section_of(self.selected)
        choice = self.selected_choice
        if kind is None or choice is None:
            return
        if choice.id == self.options.selected_id(kind):
            return
        self._aplicar(kind, choice.id)

    # -- acciones -----------------------------------------------------------

    def _aplicar(self, kind: str, choice_id: str | None) -> None:
        try:
            self.session.select(kind, choice_id)
        except SelectTrackError as exc:
            _error(self.app, exc.message)
            return
        self.options = self.session.options(self.player_name)
        self._focus(prefer_kind=kind)

    def _marcar(self) -> None:
        """Marca **una sola** opción por lista: la del cursor.

        El asterisco es una elección única, como en un selector de archivos:
        dentro de una lista no puede haber dos cosas marcadas a la vez. Si
        marcas 360p y luego 720p, el asterisco **se mueve**: 360p lo pierde y
        720p lo gana, porque lo que has dicho es "720p", no "360p y 720p".

        El motivo de que el asterisco exista, siendo que el cursor ya aplica al
        moverse: al recorrer la lista se va aplicando todo lo que pasa por
        debajo, y sin una marca fija no hay forma de distinguir "esto lo elegí
        yo" de "esto es lo que ya venía". `Espacio` es el gesto de elegir a
        conciencia; el asterisco dice dónde quedó la decisión.

        Que sea **una por lista** y no una en toda la pantalla es a propósito:
        cada sección es una lista con su propia pregunta (audio, subtítulos,
        calidad) y se pueden contestar varias —elegir un subtítulo *y* una
        calidad— sin que una marca pise a la otra.

        `0` quita la marca de la sección al volver a Automático/Desactivados:
        esa opción no es una elección del usuario, es el valor por defecto.
        """
        kind = self._section_of(self.selected)
        choice = self.selected_choice
        if kind is None or choice is None:
            return
        self._aplicar(kind, choice.id)
        # Radio: se cae lo que hubiera en ESTA lista antes de poner lo nuevo.
        self.marcados = {(k, c) for k, c in self.marcados if k != kind}
        self.marcados.add((kind, choice.id))

    def marcas_de(self, kind: str) -> list[str]:
        """Ids marcados de una sección. Nunca más de uno: es un radio."""
        return [c for k, c in self.marcados if k == kind]

    def _ultima_de_seccion(self, indice: int) -> bool:
        """True si la opción `indice` es la última de su sección.

        Se usa para colgar una nota justo debajo de su lista, sin tener que
        recorrer las filas y comparar con la siguiente.
        """
        kind = self._section_of(indice - 1)
        if kind is None:
            return True
        return self._section_of(indice) != kind

    def _nota_subtitulos(self) -> str | None:
        """Texto junto a la lista de subtítulos, o None si no hace falta.

        Los subtítulos no siempre se pueden aplicar, y depende del
        reproductor: medido, el demuxer HLS de ffmpeg (mpv y mplayer) **no
        expone** las pistas que el manifiesto declara aparte (``EXT-X-MEDIA``
        con ``TYPE=SUBTITLES``) y dice literalmente ``hls: Can't support the
        subtitle(...)``; VLC sí las ve porque trae su propio demuxer
        adaptativo.

        Aquí se dice **antes** de elegir, que es cuando sirve: el usuario ve la
        limitación junto a la decisión y puede cambiar de reproductor después.
        Un modal posterior le interrumpía ya con la elección hecha.

        No se dice nada (y es correcto callar) cuando:

        - los subtítulos van incrustados en el segmento: entonces son una
          pista normal y ``--sid`` sí los alcanza en todos los reproductores;
        - el reproductor ya es VLC, que sí los aplica;
        - no hay subtítulos que ofrecer.
        """
        if not self.options.subtitles:
            return None
        capabilities = getattr(self.session, "capabilities", None)
        if capabilities is None:
            return None
        hay_renditions = any(pista.uri for pista in capabilities.subtitle_tracks)
        if not hay_renditions:
            # Incrustados en el segmento: funcionan en todos.
            return None
        reproductor = (self.player_name or "").strip().lower()
        if reproductor == "vlc":
            return None
        if not reproductor:
            # Todavía no se ha elegido reproductor (el orden es pistas primero):
            # hay que nombrar los dos casos sin dar por hecho nada.
            return (
                "según el reproductor: VLC los aplica; con MPV o MPLAYER sólo "
                "si van dentro del vídeo"
            )
        return (
            f"{reproductor.upper()} no aplica los subtítulos de este canal "
            "(los publica como pista aparte). Con VLC sí funcionan."
        )

    def _confirmar(self) -> dict:
        # Se devuelve al App con la selección ya aplicada en la sesión; a
        # partir de aquí sigue el selector de reproductor (pistas primero,
        # reproductor después).
        #
        # `player_name` viaja en la acción a propósito: si esta pantalla se
        # abrió con un reproductor ya elegido (el sondeo llegó tarde y las
        # pistas se ofrecen al confirmar el reproductor), el App reproduce
        # con él en vez de volver a preguntar. Sin esto, el usuario elegía
        # reproductor, veía las pistas y tenía que elegir reproductor otra
        # vez para que la elección contara.
        return {
            "action": "tracks_choose_player",
            "channel": self.channel,
            "player_name": self.player_name,
            "selection": self.session.selection,
        }

    def shortcuts(self) -> str:
        confirmar = "Enter ▶ ver" if self.player_name else "Enter ▶ reproductor"
        return ("↑/↓ elegir · Tab sección · Espacio marcar * · 0 Automático · "
                f"{confirmar} · m Recordar · ? Ayuda · Esc ←")

    def actions(self) -> list[Action]:
        """Catálogo del selector de pistas (Pistas).

        `Enter` cambia de significado según el estado: si el reproductor ya
        estaba elegido, `Enter` reproduce con él; si no, `Enter` lleva al
        selector de reproductor (§36). Anunciar siempre una de las dos sería
        exactamente el bug que `enabled` + etiqueta dinámica evitan (R-04).

        La sección se salta con `Tab` —que es lo que anuncia `shortcuts()`— y
        también con ←/→, que no se anuncian: se declara la tecla que la
        pantalla dice, no la que quedaría más bonita. Con una sola sección no
        hay a dónde saltar, y `Tab` se marca como no disponible.
        """
        secciones = len(self.kinds)
        hay = secciones > 0
        return [
            Action("Enter", V.VER if self.player_name else V.REPRODUCTOR, P.PRIMARIA),
            Action("↑↓", V.ELEGIR, P.FRECUENTE, enabled=hay),
            Action("Tab", V.SECCION, P.CONTEXTUAL, enabled=secciones > 1),
            Action("Espacio", V.MARCAR, P.CONTEXTUAL, enabled=hay),
            Action("Esc", V.VOLVER, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]

    def handle_key(self, key: int) -> dict | None:
        kind = self._section_of(self.selected)
        if key in (curses.KEY_UP, ord("k")):
            self._move(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self._move(1)
        elif key in (curses.KEY_LEFT, curses.KEY_BTAB):
            self._jump(-1)
        elif key in (curses.KEY_RIGHT, _KEY_TAB):
            self._jump(1)
        elif key == ord("0"):
            if kind:
                # Volver a Automático/Desactivados no es elegir: quita la marca
                # de esa sección para que el asterisco siga significando
                # "esto lo he elegido yo".
                self.marcados = {
                    (k, c) for k, c in self.marcados if k != kind
                }
                self._aplicar(kind, QUICK_AUTO)
        elif key == ord(" "):
            self._marcar()
        elif key in (curses.KEY_ENTER, 10, 13):
            return self._confirmar()
        elif key in (ord("m"), ord("M")):
            return {"action": "remember_track_prefs", "channel": self.channel,
                    "session": self.session}
        return None

    # -- dibujo -------------------------------------------------------------

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if max_y < 6 or max_x < 30:
            return
        ch = self.channel
        icon = icons.ICON_RADIO if ch.radio else icons.ICON_TV
        try:
            stdscr.addstr(1, 0, f" {icon} {ch.name}",
                          colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
        except curses.error:
            pass

        pendientes = self.session.pending
        if pendientes:
            texto = "Analizando pistas del canal… (puedes empezar a ver igualmente)"
            try:
                stdscr.addstr(2, 2, texto[: max(0, max_x - 3)],
                              colors.pair(colors.PAIR_DIM))
            except curses.error:
                pass

        if not self.kinds:
            if pendientes:
                # Sin esto se diría "no hay nada que elegir" mientras el
                # manifiesto sigue en camino: es exactamente la confusión que
                # hace que un canal lento parezca un canal sin pistas. Loading
                # y vacío son categorías distintas (§17) y por eso son dos
                # ramas del mismo bloque, no dos widgets.
                EmptyState.render(
                    stdscr, max_y // 2, max_x,
                    icons.ICON_TIME,
                    "Analizando las pistas del canal…",
                    "En cuanto el proveedor conteste verás aquí el audio, los "
                    "subtítulos y la calidad que publica",
                )
                return
            EmptyState.render(
                stdscr, max_y // 2, max_x,
                icon,
                "Este canal no ofrece pistas que elegir.",
                "Se reproduce con el audio y la calidad por defecto del proveedor",
            )
            return

        filas = self._rows()
        y = 4
        x_seccion = 2
        x_opcion = max(8, min(14, max_x // 3))
        indice = 0
        for seccion, choice in filas:
            if y >= max_y - 2:
                break
            if seccion is not None:
                try:
                    stdscr.addstr(y, x_seccion, seccion,
                                  colors.pair(colors.PAIR_ACCENT) | curses.A_BOLD)
                    stdscr.addstr(y, x_seccion + len(seccion) + 1,
                                  "─" * max(0, max_x - x_seccion - len(seccion) - 2),
                                  colors.pair(colors.PAIR_SEPARATOR))
                except curses.error:
                    pass
                y += 1
                continue
            elegido = indice == self.selected
            kind_actual = self._section_of(indice)
            marcada = (
                kind_actual is not None
                and (kind_actual, choice.id) in self.marcados
            )
            marca = icons.ICON_PLAY if elegido else " "
            etiqueta = choice.label
            # El asterisco va al final: dice "lo he elegido yo" y sobrevive a
            # que el cursor se mueva a otra sección, que es justo cuando hace
            # falta recordarlo.
            if marcada:
                etiqueta = f"{etiqueta} *"
            try:
                if elegido:
                    stdscr.addstr(y, x_opcion - 2, marca,
                                  colors.pair(colors.PAIR_SELECTED) | curses.A_BOLD)
                stdscr.addstr(y, x_opcion, etiqueta[: max(0, max_x - x_opcion - 1)],
                              colors.pair(colors.PAIR_SELECTED if elegido
                                          else (colors.PAIR_ACCENT if marcada
                                                else colors.PAIR_NORMAL))
                              | (curses.A_BOLD if elegido else 0))
                if elegido:
                    stdscr.addstr(y, x_seccion, " ", colors.pair(colors.PAIR_SELECTED))
                    stdscr.addstr(y, x_seccion + 1, " ", colors.pair(colors.PAIR_SELECTED))
            except curses.error:
                pass
            indice += 1
            y += 1
            # Si esta era la última opción de subtítulos, se pone aquí la nota:
            # pegada a la lista donde se está eligiendo, no al final de la
            # pantalla que es donde nadie lo lee.
            if kind_actual == KIND_SUBTITLES and self._ultima_de_seccion(indice):
                nota = self._nota_subtitulos()
                if nota and y < max_y - 2:
                    try:
                        stdscr.addstr(y, x_seccion,
                                      f" ▸ {nota}"[: max(0, max_x - x_seccion - 1)],
                                      colors.pair(colors.PAIR_DIM))
                    except curses.error:
                        pass
                    y += 1

        # Información de lo que no tiene menú: informar no es ofrecer elegir.
        y += 1
        if self.options.audio_info and not self.options.audio_selectable:
            try:
                stdscr.addstr(y, x_seccion,
                              f" Audio · {self.options.audio_info}"
                              f" (única pista)", colors.pair(colors.PAIR_DIM))
            except curses.error:
                pass
            y += 1
        if self.options.quality_info and not self.options.quality_selectable:
            try:
                stdscr.addstr(y, x_seccion,
                              f" Calidad · {self.options.quality_info}"
                              f" (única)", colors.pair(colors.PAIR_DIM))
            except curses.error:
                pass

    @property
    def selection(self):
        """Selección vigente (la misma que se devolverá al reproducir)."""
        return self.session.selection

    # -- tests ---------------------------------------------------------------

    @property
    def selected_kind(self) -> str | None:
        return self._section_of(self.selected)

    @property
    def selected_choice(self):
        opciones = self._flatten()
        if 0 <= self.selected < len(opciones):
            return opciones[self.selected]
        return None



class PlayerScreen(Screen):
    """Selector de reproductor — vista cards con icono."""

    def __init__(self, app, channel: Channel) -> None:
        super().__init__(app)
        self.channel = channel
        self.title = f"Reproductor · {channel.name}"
        self.players: list[tuple[str, str]] = [
            (name, path)
            for name, path in config.detect_players().items()
            if path
        ]
        self.selected: int = 0

    def header_context(self) -> str:
        return f"{icons.ICON_PLAY} Reproductor"

    def shortcuts(self) -> str:
        return "↑/↓ · Enter ▶ · ? Ayuda · t Tema · Esc ←"

    def actions(self) -> list[Action]:
        """Catálogo del selector de reproductor (Reproductor)."""
        hay = bool(self.players)
        return [
            Action("Enter", V.ABRIR, P.PRIMARIA, enabled=hay),
            Action("↑↓", V.ELEGIR, P.FRECUENTE, enabled=hay),
            Action("Esc", V.VOLVER, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]

    def handle_key(self, key: int) -> dict | None:
        if not self.players:
            return None
        if key == curses.KEY_UP:
            self.selected = max(0, self.selected - 1)
        elif key == curses.KEY_DOWN:
            self.selected = min(len(self.players) - 1, self.selected + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            name, _ = self.players[self.selected]
            return {"action": "play_with", "channel": self.channel, "player_name": name}
        return None

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.players:
            EmptyState.render(stdscr, max_y // 2, max_x,
                              icons.ICON_PLAY,
                              "No hay reproductor disponible.",
                              "Instala: mpv, mplayer o vlc")
            return

        # Título del canal
        ch = self.channel
        icon = icons.ICON_RADIO if ch.radio else icons.ICON_TV
        try:
            stdscr.addstr(1, 0, f" {icon} {ch.name}", colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
        except curses.error:
            pass

        # Cards de reproductores
        card_h = 3
        start_y = max(3, (max_y - len(self.players) * (card_h + 1)) // 2)
        card_w = max(28, min(max(1, max_x - 4), 50))
        card_x = max(0, (max_x - card_w) // 2)

        for i, (name, path) in enumerate(self.players):
            cy = start_y + i * (card_h + 1)
            if cy + card_h > max_y - 2:
                break
            selected = i == self.selected
            border_attr = colors.pair(colors.PAIR_PRIMARY if selected else colors.PAIR_BORDER)
            text_attr = colors.pair(colors.PAIR_SELECTED if selected else colors.PAIR_NORMAL)

            # Bordes
            try:
                stdscr.addstr(cy, card_x,
                              f"{icons.BOX_R_TL}{icons.BOX_H * (card_w - 2)}{icons.BOX_R_TR}",
                              border_attr)
                stdscr.addstr(cy + card_h - 1, card_x,
                              f"{icons.BOX_R_BL}{icons.BOX_H * (card_w - 2)}{icons.BOX_R_BR}",
                              border_attr)
                for r in range(1, card_h - 1):
                    stdscr.addstr(cy + r, card_x, icons.BOX_V, border_attr)
                    stdscr.addstr(cy + r, card_x + card_w - 1, icons.BOX_V, border_attr)
            except curses.error:
                pass

            # Icono de play + nombre
            inner_w = max(0, card_w - 4)
            play_icon = icons.ICON_PLAY if selected else " "
            try:
                stdscr.addstr(cy + 1, card_x + 1,
                              f" {play_icon} {name.upper()}",
                              text_attr | curses.A_BOLD)
            except curses.error:
                pass

            # Estado
            display = "✓ Disponible"
            if len(display) > inner_w - 2:
                display = display[:inner_w - 5] + "..."
            try:
                stdscr.addstr(cy + 2, card_x + 1,
                              f"   {display}",
                              colors.pair(colors.PAIR_DIM))
            except curses.error:
                pass


class NowPlayingScreen(Screen):
    """Pantalla informativa mientras un canal se reproduce en reproductor externo.

    Se hace push al iniciar la reproducción y pop automático cuando el
    proceso termina (polling desde App.run). 'q' mata el reproductor.
    Muestra info del canal y, si hay EPG ya cargado, el programa actual.

    Con catch-up (SDD Catch-up §11) la tarjeta cambia de "En vivo" a
    "Archivo" y muestra **el programa y la hora que el usuario eligió**, no
    el que se esté emitiendo. El medidor y la salud del canal quedan
    intactos: el motor multimedia es el mismo (§14).
    """

    def __init__(  # noqa: ANN001
        self,
        app,
        channel: Channel,
        player_name: str,
        proc,  # noqa: ANN001 - Popen sin tipar para evitar import circular
        health_monitor: ChannelHealthMonitor | None = None,
        catchup_playback=None,  # noqa: ANN001 - PlaybackRequest | None
        catchup_program=None,  # noqa: ANN001 - Program | None
        track_session=None,  # noqa: ANN001 - TrackSession | None
        pin_proxy=None,  # noqa: ANN001 - PinProxy | None
        failures=None,  # noqa: ANN001 - set[str] | None
        relanzar=None,  # noqa: ANN001 - Callable[[], Popen] | None
        intentos_maximos: int = 5,
        session=None,  # noqa: ANN001 - PlaybackSession | None
        es_vigente=None,  # noqa: ANN001 - Callable[[], bool] | None
        al_medir=None,  # noqa: ANN001 - Callable[[Evidence], None] | None
    ) -> None:
        super().__init__(app)
        self.channel = channel
        self.player_name = player_name
        self.proc = proc
        self.player_path: str = config.find_player(player_name) or "?"
        # Marca de catch-up. Si llega la petición, la reproducción es de
        # archivo aunque el `channel` traiga la referencia live.
        self.catchup_playback = catchup_playback
        self.catchup_program = catchup_program
        self.is_archive = catchup_playback is not None
        if self.is_archive:
            self.title = f"\u25b6 {channel.name} · Archivo"
        else:
            self.title = f"\u25b6 {channel.name}"
        self._spin_chars = ["|", "/", "-", "\\"]
        self._spin_idx = 0
        # Medidor de tiempo real: inicio de esta reproducción.
        self._start_mono: float = time.monotonic()
        self._start_wall: datetime = datetime.now().astimezone()
        # Cachear programas si EPG ya está cargado (no dispara prompt).
        self._programs: list = []
        epg = getattr(app, "epg", None)
        if epg is not None:
            cid = _epg_channel_id(epg, channel)
            if cid is not None:
                try:
                    self._programs = epg.programmes_for(cid)
                except Exception:
                    self._programs = []
        # Pistas del canal (audio/subtítulos/calidad). Puede llegar None:
        # un .ts o un canal sin manifiesto se reproduce igual que antes.
        self.tracks = track_session
        self.pin_proxy = pin_proxy
        # Supervisor de reconexión (SDD-M §17). **No** reproduce: observa el
        # proceso, publica un estado y llama a `relanzar` cuando toca. La
        # pantalla sólo pinta ese estado (igual que hace con la salud del
        # canal), nunca decide cuándo reconectar: si lo hiciera, el
        # comportamiento dependería de si hay alguien mirando la pantalla.
        self._failures: set[str] = set(failures or ())
        self.supervisor = None
        # Sesión de reproducción (SDD §4). Es **opcional**: sin ella, todo lo
        # que hay debajo es exactamente lo de siempre. Con ella, esta pantalla
        # deja de ser dueña a medias de lo que abrió: el proxy, la sesión de
        # pistas y el socket de control quedan registrados en su ResourceScope
        # y se liberan en bloque, una sola vez y en orden (PLAY-003).
        self.session = session
        if self.session is not None:
            self.session.launched()
            self._registrar_recursos()
        # Fuente de evidencia de reproducción (FR-REC-001/002). Sólo se crea
        # para mpv **con** canal de control abierto, que es el único caso en que
        # hay algo que mirar. En los demás, la fuente dice `UNKNOWN` y la barra
        # lo enseña: es preferible «sin comprobar» a un «reproduciendo» que no
        # nadie ha comprobado.
        self.evidencia = self._crear_evidencia(player_name, track_session)
        if relanzar is not None:
            try:
                from thetvview.player.supervisor import (
                    ReconnectPolicy,
                    ReconnectSupervisor,
                )

                self.supervisor = ReconnectSupervisor(
                    relanzar=relanzar,
                    policy=ReconnectPolicy(max_attempts=intentos_maximos),
                    session=self.session,
                    is_vigente=es_vigente,
                    evidencia=self.evidencia,
                    # H-01: la medición llega al observador de la sesión. Sin
                    # él, «inicio confirmado» no puede ocurrir nunca, que es
                    # preferible a que ocurra por otra razón.
                    al_medir=al_medir,
                )
                self.supervisor.start(proc)
            except Exception:
                self.supervisor = None  # sin supervisor, el resto igual
        # Salud del canal en tiempo real (hilo daemon no bloqueante)
        if health_monitor is not None:
            self.health = health_monitor
        else:
            # El gestor de tareas de la app: así el monitor entra en el
            # cierre acotado y no es un hilo suelto que la app no alcanza.
            # `getattr` porque los tests montan la pantalla con dobles.
            tasks = getattr(app, "_tasks", None)
            try:
                self.health = ChannelHealthMonitor(
                    channel, interval=3.0, timeout=3.0, auto_start=True,
                    task_manager=tasks,
                )
            except Exception:
                # degradar silencioso si no se puede crear el monitor
                self.health = ChannelHealthMonitor(
                    channel, interval=10.0, timeout=2.0, auto_start=False,
                    task_manager=tasks,
                )

    def header_context(self) -> str:
        return f"{icons.ICON_PLAY} Reproduciendo"

    def shortcuts(self) -> str:
        # Audio, subtítulos y calidad ya se eligieron antes de elegir
        # reproductor (ver TrackOptionsScreen): aquí no se ofrecen porque con
        # el canal abierto no se pueden cambiar (una pista fija se aplica al
        # lanzar). La toolbar queda con lo que sí tiene sentido aquí.
        extra = " · i Info" if self.tracks else ""
        # `d` diagnóstico siempre: es lo que contesta «¿por qué no abre?» sin
        # tener que adivinar (SDD-M §21), y es la única ayuda cuando un canal
        # muere al instante.
        return f"q Detener · d Diagnóstico{extra} · ? Ayuda"

    def actions(self) -> list[Action]:
        """Catálogo de Reproduciendo.

        `i Info` sólo se declara cuando hay sesión de pistas viva, porque es
        justo cuando `shortcuts()` lo anuncia: la barra no puede decir más que
        la pantalla (§10, R-09).
        """
        acciones = [
            Action("q", V.DETENER, P.PRIMARIA),
            Action("d", V.DIAGNOSTICO, P.CONTEXTUAL),
        ]
        if self.tracks:
            acciones.append(Action("i", V.INFO, P.SECUNDARIA))
        acciones.append(Action("?", V.AYUDA, P.AYUDA, essential=True))
        return acciones

    def is_alive(self) -> bool:
        # El proceso vivo es el del supervisor si lo hay: tras un corte, el
        # `proc` original sigue muerto y la pantalla se cerraría en el primer
        # tick mientras el canal ya se está reabriendo.
        proceso = self.proc
        supervisor = getattr(self, "supervisor", None)
        if supervisor is not None and getattr(supervisor, "proceso", None) is not None:
            proceso = supervisor.proceso
        try:
            return proceso.poll() is None
        except Exception:
            return False

    def playback_state(self) -> object:  # noqa: ANN001 - PlaybackState | None
        """Estado de reproducción para pintarlo (§17).

        Devuelve ``None`` si no hay supervisor: la pantalla entonces se
        comporta exactamente como antes, que es lo que el §32 del SDD de pistas
        garantiza para un stream sin extras.
        """
        supervisor = getattr(self, "supervisor", None)
        return getattr(supervisor, "estado", None) if supervisor else None

    def exit_summary(self) -> str:
        """Mensaje de cierre según cómo murió el reproductor.

        Si murió al instante con código != 0, explica la causa probable
        (stream que no abrió: proveedor/URL) en vez del "finalizado"
        genérico que ocultaba el problema.

        Y si hubo reconexiones, dice cuántas: un canal que se cortó tres veces
        y acabó-engregando no es un canal que «se ha terminado», y el usuario
        necesita esa diferencia para decidir si insistir.
        """
        intentos = int(getattr(getattr(self, "supervisor", None), "intentos", 0) or 0)
        if intentos:
            plural = "vez" if intentos == 1 else "veces"
            return (
                f"'{self.channel.name}' se cortó y se reconectó {intentos} "
                f"{plural} sin llegar a reproducirse del todo."
            )
        sesion = getattr(self, "session", None)
        if sesion is not None and sesion.ended:
            return self._resumen_con_motivo(sesion)
        try:
            returncode = self.proc.poll()
        except Exception:
            returncode = None
        try:
            hint = player.explain_early_exit(returncode, self.elapsed_seconds())
        except Exception:
            hint = None
        if hint:
            return f"'{self.channel.name}' no abrió. {hint}"
        return f"'{self.channel.name}' finalizado."

    def _resumen_con_motivo(self, sesion) -> str:  # noqa: ANN001 - PlaybackSession
        """Resumen de salida construido desde el ``end_reason`` de la sesión.

        El motivo de la sesión manda sobre el texto genérico porque sabe cosas
        que el código de salida no dice: si fue el usuario, si la app se cerró,
        si se agotaron los reintentos. Y cuando el proceso murió al instante se
        conserva el texto de `explain_early_exit`, que es el que el usuario ya
        conoce.
        """
        from thetvview.player.session import EndReason

        motivo = sesion.end_reason
        nombre = self.channel.name
        if motivo in (EndReason.USER_STOPPED, EndReason.CANCELLED):
            return f"'{nombre}' detenido."
        if motivo is EndReason.APP_SHUTDOWN:
            return f"'{nombre}' se detuvo al cerrar la aplicación."
        try:
            returncode = self.proc.poll()
        except Exception:
            returncode = None
        try:
            hint = player.explain_early_exit(returncode, self.elapsed_seconds())
        except Exception:
            hint = None
        if hint:
            return f"'{nombre}' no abrió. {hint}"
        if motivo is EndReason.RETRIES_EXHAUSTED:
            return (
                f"'{nombre}' se cortó y no volvió tras varios intentos. "
                "Prueba otro canal o revisa tu conexión."
            )
        if motivo is EndReason.INIT_FAILED:
            return f"'{nombre}' no se pudo abrir."
        if sesion.first_media_at is None:
            # Proceso abierto, ninguna evidencia de medios: decirlo es el punto
            # de todo el modelo de evidencia (FR-REC-001). Va **antes** que el
            # motivo genérico de «falló»: decir «falló» cuando no llegó a haber
            # imagen es exactamente la mentira que este modelo elimina.
            return (
                f"'{nombre}' se abrió pero no llegó a reproducir nada "
                "antes de terminar."
            )
        if motivo is EndReason.PLAYBACK_ERROR:
            return f"'{nombre}' falló mientras se veía."
        return f"'{nombre}' finalizado."

    def stop_health(self) -> None:
        """Detiene el hilo de sonda de salud (idempotente, no bloquea)."""
        try:
            h = getattr(self, "health", None)
            if h is not None and hasattr(h, "stop"):
                h.stop()
        except Exception:
            pass

    def handle_key(self, key: int) -> dict | None:
        if key in (ord("q"), ord("Q"), 27, curses.KEY_LEFT, curses.KEY_BACKSPACE):
            return {"action": "stop_playback"}
        if key in (ord("d"), ord("D")):
            # Diagnóstico **sin** conexión: es puro y no espera. La conexión de
            # verdad va en el modal, con su propia tecla, porque abre red.
            return {"action": "diagnose_channel", "channel": self.channel}
        if self.tracks is None:
            return None
        # Sólo queda `i`. Las pistas (audio, subtítulos y calidad) **no**
        # tienen atajo aquí y no por descuido: se eligen en
        # TrackOptionsScreen, antes de elegir reproductor, porque el
        # reproductor no cambia qué pistas publica el canal. Con el canal ya
        # abierto, `a`, `s` y `v` no hacen nada: una pista fija se aplica al
        # lanzar. Para volver a verlas hay que reabrir el canal (`p`).
        if key in (ord("i"), ord("I")):
            return {"action": "recheck_tracks", "channel": self.channel}
        return None

    def stop_tracks(self) -> None:
        """Apaga todo lo que esta pantalla abrió: sondeo, proxy y socket IPC.

        Se llama al parar la reproducción, al saltar hacia atrás y al salir
        de la app. Es idempotente y nunca lanza.

        **Con sesión**, la liberación pasa por su :class:`ResourceScope`: una sola
        vez, en orden inverso al de adquisición y aunque un recurso falle. **Sin
        sesión**, se hace exactamente lo de siempre, que es lo que garantiza que
        un stream sin extras no cambie de camino.
        """
        supervisor = getattr(self, "supervisor", None)
        if supervisor is not None:
            try:
                supervisor.stop()
            except Exception:
                pass
            self.supervisor = None
        sesion = getattr(self, "session", None)
        if sesion is not None:
            # Los recursos se registraron en el `__init__`; aquí sólo se sueltan.
            sesion.close_resources()
            self.pin_proxy = None
            return
        sesion_pistas = getattr(self, "tracks", None)
        if sesion_pistas is not None:
            try:
                sesion_pistas.stop()
            except Exception:
                pass
        proxy = getattr(self, "pin_proxy", None)
        if proxy is not None:
            try:
                proxy.stop()
            except Exception:
                pass
            self.pin_proxy = None
        self._remove_ipc_socket()

    def _crear_evidencia(self, player_name: str, track_session) -> object:  # noqa: ANN001
        """Fuente de señal de reproducción para este motor (FR-REC-002).

        mpv es el único con canal de control, así que es el único del que se
        puede sacar una respuesta real. Para los demás se devuelve
        :class:`~thetvview.player.evidence.UnknownSignalSource`, que **siempre**
        dice ``UNKNOWN``: un ``PROCESS_ALIVE`` ahí sería el éxito ficticio que
        el requisito prohíbe.

        Sin canal de control tampoco hay nada que mirar en mpv, y por eso
        ``track_session`` se mira: sin sesión de pistas, `play_channel` no abre el
        socket.
        """
        from thetvview.player.evidence import signal_source_for

        ipc = None
        if str(player_name or "").strip().lower() == "mpv" and track_session is not None:
            ruta = getattr(track_session, "ipc_path", "") or ""
            if ruta:
                try:
                    from thetvview.player.mpv_ipc import MpvIpc

                    ipc = MpvIpc(ruta)
                except Exception:
                    ipc = None
        return signal_source_for(player_name, ipc=ipc)

    def _registrar_recursos(self) -> None:
        """Da dueño a todo lo que esta pantalla abrió (FR-PLAY-007).

        Sólo se llama cuando hay sesión. El orden de alta es el inverso del de
        liberación, que es el correcto para recursos anidados: el socket de
        control se abre **después** del proxy, así que se suelta **antes**.
        """
        sesion = getattr(self, "session", None)
        if sesion is None:  # pragma: no cover - el caller ya lo comprobó
            return
        if self.pin_proxy is not None:
            proxy = self.pin_proxy
            sesion.add_resource("proxy-calidad", lambda: _parar(proxy))
        pistas = getattr(self, "tracks", None)
        if pistas is not None:
            sesion.add_resource("pistas", lambda: _parar(pistas))
        ruta = getattr(pistas, "ipc_path", None) if pistas is not None else None
        if ruta:
            sesion.add_resource("socket-control", lambda: _borrar_socket(ruta))

    def _remove_ipc_socket(self) -> None:
        """Borra el socket del IPC si sigue ahí.

        mpv crea el fichero y no lo borra al salir. Vive en un directorio
        0700, así que no es un problema de seguridad, pero dejarlo puesto
        acumula basura en ``data/ipc/``.
        """
        sesion = getattr(self, "tracks", None)
        ruta = getattr(sesion, "ipc_path", None) if sesion is not None else None
        if not ruta:
            return
        _borrar_socket(ruta)


    def _current_program(self):  # type: ignore[no-untyped-def]
        """Programa que se está viendo.

        Con catch-up es **el que eligió el usuario** (el pedido), no el que
        se esté emitiendo ahora: mezclar los dos haría creer que se ve la
        directo mientras se ve el archivo.
        """
        if self.catchup_program is not None:
            return self.catchup_program
        if not self._programs:
            return None
        now = datetime.now().astimezone()
        for prog in self._programs:
            if prog.start <= now and (prog.stop is None or now < prog.stop):
                return prog
        return None

    # --- Medidor minimalista (solo stdlib, fácil de entender) -----------------

    def elapsed_seconds(self) -> int:
        """Segundos viendo este canal (tiempo real desde que se lanzó)."""
        try:
            return max(0, int(time.monotonic() - self._start_mono))
        except Exception:
            return 0

    @staticmethod
    def _fmt_hms(seconds: int) -> str:
        """Formato reloj minimalista: MM:SS si <1h, si no HH:MM:SS."""
        seconds = max(0, int(seconds))
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _fmt_human(seconds: int) -> str:
        """Duración humana ultra-minimal: '3 min', '1 h 5 min', '45 s'."""
        seconds = max(0, int(seconds))
        h = seconds // 3600
        m = (seconds % 3600) // 60
        if h > 0:
            return f"{h} h {m} min" if m else f"{h} h"
        if m > 0:
            return f"{m} min"
        return f"{seconds} s"

    # --- Medidor simple basado en reproductor (fácil de entender) -----------
    # Cada reproductor tiene etiqueta humana.
    _PLAYER_META: dict[str, tuple[str, str]] = {
        "mpv": ("MPV", ""),
        "mplayer": ("MPlayer", ""),
        "vlc": ("VLC", ""),
    }

    def _player_display(self) -> tuple[str, str]:
        """Devuelve (etiqueta, pista) según el reproductor elegido."""
        key = (self.player_name or "").strip().lower()
        return self._PLAYER_META.get(key, (self.player_name.upper() if self.player_name else "?", ""))

    def _meter_bar(self, alive: bool, width: int) -> str:
        """Barra con bloques fraccionales: animada si reproduce, vacía si detenido.

        - Vivo:  [▏▎▍▌▋▊▉█▏▎▍]  con progreso animado.
        - Parado: [○○○○○○○○○○○○○○]  fácil de entender.
        width se clamped a 10..24 para legibilidad.
        """
        w = max(10, min(24, int(width)))
        if not alive:
            return "[" + "○" * w + "]"
        blocks = ["▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]
        pos = self._spin_idx % w
        inner = ""
        for i in range(w):
            if i < pos:
                inner += "█"
            elif i == pos:
                inner += blocks[self._spin_idx % len(blocks)]
            else:
                inner += "▁"
        return "[" + inner + "]"

    def _meter_state_text(self, alive: bool) -> str:
        """Texto del medidor, con el estado del supervisor si lo hay (§17).

        Sin supervisor, exactamente lo de siempre: «Reproduciendo» o
        «Detenido». Con supervisor, el estado real —«Reconectando», «Cargando»—
        que es la información que hace falta cuando algo va mal, y que sólo él
        sabe. La pantalla **no decide** nada aquí: lee.
        """
        supervisor = getattr(self, "supervisor", None)
        if supervisor is not None:
            estado = getattr(supervisor, "estado", None)
            if estado is not None and alive:
                return str(getattr(estado, "texto", "Reproduciendo"))
        return "Reproduciendo" if alive else "Detenido"

    # --- Pistas: sólo lo que el manifiesto declaró de verdad -----------------

    def _track_lines(self, max_x: int) -> list[tuple[str, int]]:
        """Filas `Audio/Subtítulos/Calidad/Resolución/Códec` de la tarjeta.

        Se omiten las vacías: sin pistas analizadas la tarjeta queda como
        estaba, que es lo que exige no romper los streams simples (§32).

        Ninguna fila lleva tecla: con el canal ya abierto no se cambia de
        pista, así que aquí sólo se informa de lo que se está viendo.
        """
        sesion = getattr(self, "tracks", None)
        if sesion is None:
            return []
        if sesion.pending and sesion.capabilities is None:
            return [(" Analizando pistas del canal…", colors.pair(colors.PAIR_DIM) | curses.A_DIM)]
        datos = sesion.summary()
        if not any(datos.values()):
            return []
        etiquetas = (
            ("Audio", "audio"),
            ("Subtítulos", "subtitles"),
            ("Calidad", "quality"),
            ("Resolución", "resolution"),
            ("Códec", "codec"),
        )
        lineas: list[tuple[str, int]] = []
        for etiqueta, clave in etiquetas:
            valor = datos.get(clave) or ""
            if not valor:
                continue
            # Todo es información: aquí ya no se cambia ninguna pista, así que
            # ninguna fila lleva tecla. Audio, subtítulos y calidad se eligieron
            # antes de abrir el reproductor.
            lineas.append((f" {etiqueta}:".ljust(17), colors.pair(colors.PAIR_DIM)))
            lineas.append((f"{valor}"[: max(0, max_x - 4)], colors.pair(colors.PAIR_NORMAL)))
        return lineas

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        spin = self._spin_chars[self._spin_idx % len(self._spin_chars)]
        self._spin_idx = (self._spin_idx + 1) % len(self._spin_chars)

        lines: list[tuple[str, int]] = []
        alive = self.is_alive()

        # -- Datos básicos del canal (siempre visibles) --
        grp = self.channel.group or "-"
        safe_path = config.anonymize_path(self.player_path) if self.player_path != "?" else "?"
        player_label, player_hint = self._player_display()
        hint_txt = f" · {player_hint}" if player_hint else ""
        # Cabecera simple de estado
        state_txt = self._meter_state_text(alive)
        state_icon = icons.ICON_LIVE if alive else icons.ICON_LIVE_OFF
        # Color del estado según vivo/parado (simple: verde / rojo)
        state_attr = (colors.pair(colors.PAIR_SUCCESS) | curses.A_BOLD) if alive else (colors.pair(colors.PAIR_DANGER) | curses.A_BOLD)
        # Mostrar también spin para sensación de actividad
        lines.append((f" {spin} {state_icon} {state_txt} · {self.channel.name}", state_attr))
        lines.append(("", colors.pair(colors.PAIR_NORMAL)))

        lines.append((f" Canal:      {self.channel.name}", colors.pair(colors.PAIR_NORMAL) | curses.A_BOLD))
        lines.append((f" Grupo:      {grp}", colors.pair(colors.PAIR_DIM)))
        lines.append((f" Reproductor: {player_label}", colors.pair(colors.PAIR_NORMAL)))
        lines += self._track_lines(max_x)
        if self.is_archive:
            lines.append((f" Fuente:     {icons.ICON_ARCHIVE} Archivo (catch-up)",
                          colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD))
        lines.append(("", colors.pair(colors.PAIR_NORMAL)))

        # ── MEDIDOR SIMPLE basado en reproductor (fácil de entender) ──
        # Título del medidor
        lines.append((f" ── Medidor · {player_label} ──", colors.pair(colors.PAIR_BORDER) | curses.A_DIM))
        # Barra + estado + tiempo: 3 líneas como máximo, lenguaje natural
        # 1) Barra visual animada
        # Calcular ancho de barra en función del ancho de terminal (simple, legible)
        bar_w = max(10, min(24, max_x - 14))
        # Si max_x muy pequeño, reducir
        if max_x < 50:
            bar_w = max(8, max_x - 20)
        bar_str = self._meter_bar(alive, bar_w)
        # Con catch-up la etiqueta es "Archivo", no "En vivo": el SDD §11
        # exige que la UI distinga el modo de reproducción, y el mismo motor
        # multimedia sirve para los dos (§14).
        bar_label = ("Archivo" if self.is_archive else "En vivo") if alive else "Parado"
        bar_icon = icons.ICON_LIVE if alive else icons.ICON_LIVE_OFF
        # Color de barra: verde si vivo, tenue si parado
        bar_attr = colors.pair(colors.PAIR_SUCCESS) if alive else colors.pair(colors.PAIR_DIM)
        # Línea única fácil: [──▶──]  En vivo  ●
        lines.append((f" {bar_str}  {bar_icon} {bar_label}", bar_attr | curses.A_BOLD))
        # 2) Tiempo viendo (reloj simple)
        elapsed = self.elapsed_seconds()
        elapsed_str = self._fmt_hms(elapsed)
        started_str = self._start_wall.strftime("%H:%M")
        # Texto humano: "⏱ 02:15 viendo · desde 14:32"
        time_line = f" ⏱ {elapsed_str} viendo · desde {started_str}"
        # El progreso del programa se mide contra el pedido de catch-up si
        # lo hay (el reloj no sirve: lo que corre es el archivo, no la
        # emission actual). Con directo, contra el programa en emisión.
        prog = self._current_program()
        extra_prog = ""
        if prog is not None:
            try:
                if self.is_archive and self.catchup_playback is not None:
                    ref = self.catchup_playback.start
                    total2 = float(max(1, int(self.catchup_playback.duration)))
                    etiqueta = "del archivo"
                else:
                    ref = datetime.now().astimezone()
                    total2 = (prog.stop - prog.start).total_seconds() if prog.stop else 0
                    etiqueta = "del programa"
                if total2 > 0:
                    frac2 = max(0.0, min(1.0, (ref - prog.start).total_seconds() / total2))
                    rem2 = max(0, int(total2 - (ref - prog.start).total_seconds()))
                    pct2 = int(frac2 * 100)
                    extra_prog = (f" · {pct2}% {etiqueta}, quedan "
                                  f"{self._fmt_human(rem2)}")
            except Exception:
                pass
        # Solo añadir extra_prog si no hace la línea demasiado larga (> max_x-4)
        if extra_prog and len(time_line + extra_prog) < max_x - 4:
            time_line += extra_prog
        lines.append((time_line, (colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD) if alive else colors.pair(colors.PAIR_DIM)))
        # 3) Salud simplificada a UNA línea fácil (no tecnicismos)
        try:
            hlth = getattr(self, "health", None)
            if hlth is not None:
                hstat = hlth.status(player_alive=alive)  # type: ignore[attr-defined]
                # Solo mostrar barra + etiqueta, sin jitter/ms técnicos
                # Nivel 0-5 -> texto ya es humano ("Excelente" etc) y bar "●●●●○"
                # La barra dice **qué tan bien va**; el badge dice **qué se
                # sabe de él**: sin sondas, un canal nunca comprobado sale
                # "Sin comprobar" y no como online ni como caído (FR-STA-01).
                # Glifo + palabra, nunca sólo color (FR-STA-03).
                estado = estado_de(hstat)
                hl_attr = colors.pair(hstat.color_pair)
                # Frase ultra simple: "Señal: [●●●●○] Buena ● En línea"
                lines.append((
                    f" Señal: [{hstat.bar}] {hstat.label} "
                    f"{estado_badge_text(estado)}",
                    hl_attr,
                ))
                # Solo si nivel <=2 y vivo, un tip corto accionable en lenguaje natural
                if alive and hstat.level <= 2:
                    tip = "Consejo: si se corta, prueba otra resolución o revisa Wi-Fi."
                    if hstat.success_rate < 0.5:
                        tip = "Consejo: sin señal, revisa internet o prueba otro canal."
                    # Truncar tip si ancho pequeño
                    max_tip = max(0, max_x - 6)
                    if len(tip) > max_tip and max_tip > 10:
                        tip = tip[: max_tip - 3] + "..."
                    lines.append((f"   ↳ {tip}", colors.pair(colors.PAIR_DIM) | curses.A_DIM))
        except Exception:
            pass

        lines.append(("", colors.pair(colors.PAIR_NORMAL)))

        # ── Info del programa actual (EPG) simplificada ──
        # Mantener 1-2 líneas como antes pero sin duplicar barra si ya hay medidor
        # Reusar prog ya obtenido
        if prog is not None:
            start = prog.start.astimezone().strftime("%H:%M")
            stop = prog.stop.astimezone().strftime("%H:%M") if prog.stop else "--:--"
            if self.is_archive:
                # Con catch-up el programa elegido ya no está "en emisión":
                # se anuncia como archivo, con la hora por la que se entra.
                entra = self.catchup_playback.start.astimezone().strftime("%H:%M")
                lines.append((
                    f" {icons.ICON_ARCHIVE} Archivo: {start}-{stop}  {prog.title}",
                    colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD,
                ))
                lines.append((
                    f"    entras en {entra} · {self._fmt_human(int(self.catchup_playback.duration))}",
                    colors.pair(colors.PAIR_DIM),
                ))
            else:
                lines.append((f" ● Ahora: {start}-{stop}  {prog.title}", colors.pair(colors.PAIR_CURRENT) | curses.A_BOLD))
            if prog.sub_title:
                lines.append((f"    {prog.sub_title}", colors.pair(colors.PAIR_NORMAL)))
            if prog.desc:
                desc = prog.desc.strip().replace("\n", " ")
                max_desc = max(0, max_x - 10)
                if len(desc) > max_desc and max_desc > 10:
                    desc = desc[: max_desc - 3] + "..."
                lines.append((f"    {desc}", colors.pair(colors.PAIR_DIM)))
            if prog.categories:
                cats = ", ".join(prog.categories)
                # Truncar categorías si necesario
                max_cats = max(0, max_x - 10)
                if len(cats) > max_cats and max_cats > 10:
                    cats = cats[: max_cats - 3] + "..."
                lines.append((f"    ({cats})", colors.pair(colors.PAIR_DIM)))
            # Barra de progreso EPG solo si no se mostró ya el % en el medidor (evitar duplicado)
            # La mostramos siempre, es útil y familiar. Con catch-up se mide
            # contra el pedido, no contra la hora actual (el archivo ya pasó).
            if prog.stop:
                try:
                    if self.is_archive and self.catchup_playback is not None:
                        ref = self.catchup_playback.start
                    else:
                        ref = datetime.now().astimezone()
                    total = (prog.stop - prog.start).total_seconds()
                    if total > 0:
                        frac = max(0.0, min(1.0, (ref - prog.start).total_seconds() / total))
                        bar_w2 = max(8, min(24, max_x - 14))
                        filled = int(bar_w2 * frac)
                        bar2 = "━" * filled + "●" + "─" * max(0, bar_w2 - filled - 1)
                        lines.append((f"    {bar2} {int(frac*100)}%", colors.pair(colors.PAIR_PRIMARY) | curses.A_DIM))
                except Exception:
                    pass
        elif self._programs:
            lines.append((" Sin programa en emisión ahora.", colors.pair(colors.PAIR_EMPTY)))
            now = datetime.now().astimezone()
            nxt = next((p for p in self._programs if p.start > now), None)
            if nxt is not None:
                start = nxt.start.astimezone().strftime("%H:%M")
                lines.append((f" Siguiente: {start}  {nxt.title}", colors.pair(colors.PAIR_DIM)))
        else:
            if getattr(self.app, "epg", None) is None:
                lines.append((" Sin datos EPG (pulsa 'e' en la lista para cargar).", colors.pair(colors.PAIR_EMPTY)))
            else:
                lines.append((" Sin datos EPG para este canal.", colors.pair(colors.PAIR_EMPTY)))

        content_h = len(lines)
        # Card centrada con borde redondeado
        card_w = min(max_x - 4, max(40, max(len(t) for t, _ in lines) + 4) if lines else 40)
        card_w = max(36, min(card_w, max_x - 2))
        card_h = content_h + 2
        card_x = max(0, (max_x - card_w) // 2)
        start_y = max(1, (max_y - card_h) // 2)
        # Borde
        try:
            stdscr.addstr(start_y, card_x, f"{icons.BOX_R_TL}{icons.BOX_H * (card_w - 2)}{icons.BOX_R_TR}", colors.pair(colors.PAIR_BORDER))
            stdscr.addstr(start_y + card_h - 1, card_x, f"{icons.BOX_R_BL}{icons.BOX_H * (card_w - 2)}{icons.BOX_R_BR}", colors.pair(colors.PAIR_BORDER))
            for r in range(1, card_h - 1):
                stdscr.addstr(start_y + r, card_x, icons.BOX_V, colors.pair(colors.PAIR_BORDER))
                stdscr.addstr(start_y + r, card_x + card_w - 1, icons.BOX_V, colors.pair(colors.PAIR_BORDER))
        except curses.error:
            pass
        inner_x = card_x + 2
        inner_w = card_w - 4
        for idx, (text, attr) in enumerate(lines):
            y = start_y + 1 + idx
            if y >= start_y + card_h - 1 or y >= max_y - 2:
                break
            if not text.strip():
                continue
            clipped = text.strip()[:inner_w]
            try:
                stdscr.addstr(y, inner_x, clipped, attr)
            except curses.error:
                pass





def _borrar_socket(ruta: str) -> None:
    """Borra el fichero del socket de control. Nunca lanza.

    mpv crea el socket al arrancar y no lo borra al salir. Vive en un directorio
    0700, así que no es un problema de seguridad, pero dejarlo puesto acumula
    basura en ``data/ipc/``.
    """
    try:
        os.unlink(ruta)
    except (OSError, TypeError, ValueError):
        pass


def _parar(objeto) -> None:  # noqa: ANN001 - PinProxy | TrackSession | None
    """Llama a ``objeto.stop()`` tragándose el fallo.

    Es una función suelta, y no un ``lambda`` que capture ``self``, por una
    razón concreta: el liberador que se registra en el
    :class:`~thetvview.player.session.ResourceScope` viviría más que la sesión, y
    retener la pantalla entera impide que se recolecte.
    """
    parar = getattr(objeto, "stop", None)
    if callable(parar):
        parar()


class EpgScreen(Screen):
    """Parrilla de programas del canal seleccionado.

    Distingue tres situaciones con el marcador de cada fila (SDD Catch-up
    §11, que aquí no es cosmético: es la **información de si la acción
    existe**):

    - ``●`` en directo (el programa que se está emitiendo);
    - ``▶`` programa pasado **con** catch-up declarado por el proveedor:
      se puede pedir al archivo;
    - ``○`` programa pasado **sin** catch-up: se ve, no se reproduce.

    El marcador no lo decide la pantalla leyendo `tv_archive` ni
    `catchup*`: pasa por `catchup.classify`, que es donde vive la única
    invariante del dominio (§7/§21).
    """

    def __init__(self, app, channel: Channel, epg_url: str | None = None) -> None:
        super().__init__(app)
        self.channel = channel
        self.epg_url = epg_url
        self.title = f"EPG · {channel.name}"
        self._list = ScrollableList()
        self.programs: list = []
        # Capacidad derivada una vez por carga, no por fila ni por tecla:
        # es O(1) leer dos claves de `attrs`, pero 500 filas por frame ya
        # es trabajo de más. Nunca se persiste.
        try:
            self.catchup = catchup.capability_for(channel)
        except Exception:  # noqa: BLE001 - sin catch-up, directo intacto
            self.catchup = catchup.DISABLED
        self.refresh_programs()

    def header_context(self) -> str:
        return f"{icons.ICON_EPG} Guía TV"

    @property
    def has_catchup(self) -> bool:
        """True si el proveedor declaró archivo utilizable para este canal."""
        return catchup.can_use_catchup(self.catchup)

    @property
    def plantilla_no_soportada(self) -> bool:
        """CU-07: declaró archivo, pero no sabemos escribir su dirección.

        En ese caso la guía **no** muestra el icono de «disponible»: ofrecer
        algo que después falla es peor que no ofrecerlo. Lo que sí se ofrece es
        el intento bajo riesgo propio, con su aviso.
        """
        return bool(
            catchup.can_use_catchup(self.catchup)
            and not self.catchup.plantilla_soportada
        )

    def refresh_programs(self) -> None:
        self.programs = self.app.ensure_epg(self.channel, url_hint=self.epg_url)
        self._list.set_items(self._rows())

    def shortcuts(self) -> str:
        if self.has_catchup:
            return ("↑/↓ · Enter ▶ Archivo · Enter ● Directo · r Recargar · "
                    "? Ayuda · t Tema · Esc ←")
        return "↑/↓ · Enter ● Directo · r Recargar · ? Ayuda · t Tema · Esc ←"

    def actions(self) -> list[Action]:
        """Catálogo de la parrilla EPG.

        `Enter` anuncia «Archivo» **sólo** si el proveedor declaró catch-up
        utilizable: sin esa declaración la acción no existe y anunciarla sería
        una promesa falsa (§20.1, R-04). La tecla es `r` y no `R`: es la que
        el `handle_key` de esta pantalla atiende.

        El plan (F3) preveía además `←→ Horario`, pero `EpgScreen.handle_key`
        **no** atiende `KEY_LEFT`/`KEY_RIGHT` y `shortcuts()` tampoco los
        anuncia. Declararla aquí sería el bug R-09 exacto que este sistema
        existe para cerrar, así que queda fuera hasta que la pantalla la
        implemente (fuera de alcance: no se toca `handle_key`).
        """
        return [
            Action("Enter", V.ARCHIVO if self.has_catchup else V.VER, P.PRIMARIA),
            Action("r", V.RECARGAR, P.ORGANIZACION),
            Action("Esc", V.VOLVER, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]

    def _state_of(self, prog, now: datetime) -> catchup.CatchupState:  # noqa: ANN001
        """Estado catch-up de un programa (delegado al dominio, §7)."""
        if not self.has_catchup:
            return catchup.CatchupState.DISABLED_BY_PROVIDER
        return catchup.classify(self.channel, prog, now)

    def _is_current(self, prog, now: datetime) -> bool:  # noqa: ANN001
        return prog.start <= now and (prog.stop is None or now < prog.stop)

    def _rows(self) -> list[str]:
        if not self.programs:
            # Sin fila fantasma: "aquí no hay nada" lo dice el `EmptyState`,
            # y una fila de texto dentro de la lista se contaría como programa
            # (rompería el contador y el cursor). Lista vacía de verdad.
            return []
        now = datetime.now().astimezone()
        rows: list[str] = []
        for prog in self.programs:
            start = prog.start.astimezone().strftime("%H:%M")
            stop = prog.stop.astimezone().strftime("%H:%M") if prog.stop else "--:--"
            if self._is_current(prog, now):
                marker = icons.ICON_LIVE
            elif self._state_of(prog, now) is catchup.CatchupState.AVAILABLE:
                marker = icons.ICON_PLAY
            elif self._state_of(prog, now) is (
                catchup.CatchupState.UNSUPPORTED_TEMPLATE
            ):
                # Hay archivo, pero no sabemos pedirlo: se marca distinto del
                # «no disponible» sin mentir (CU-07).
                marker = icons.ICON_LIVE_OFF
            else:
                marker = icons.ICON_LIVE_OFF
            label = f"{prog.title}: {prog.sub_title}" if prog.sub_title else prog.title
            desc = f" — {prog.desc}" if prog.desc else ""
            cats = f" ({', '.join(prog.categories)})" if prog.categories else ""
            rows.append(f" {marker} {start}-{stop}  {label}{desc}{cats}")
        return rows

    def _selected_program(self):  # type: ignore[no-untyped-def]
        """Programa bajo el cursor, o None si no hay parrilla."""
        idx = self._list.selected
        if 0 <= idx < len(self.programs):
            return self.programs[idx]
        return None

    def handle_key(self, key: int) -> dict | None:
        rows = self.app.body_height()
        if self._list.handle_key(key, rows):
            return None
        if key == ord("r") or key == ord("R") or key == _KEY_F5:
            # Re-carga: para URLs fuerza re-descarga; para paths re-lee fichero.
            self.app.ensure_epg(self.channel, force_refresh=True)
            self.refresh_programs()
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            prog = self._selected_program()
            if prog is None:
                # Sin parrilla: la acción disponible es el directo, intacto.
                return {"action": "open_channel", "channel": self.channel}
            now = datetime.now().astimezone()
            if prog.start > now:
                # Aún no se ha emitido: no es una situación de archivo. Se
                # ofrece el directo, que es el camino por defecto (§20.10).
                return {"action": "open_channel", "channel": self.channel}
            if self._is_current(prog, now):
                return {"action": "open_channel", "channel": self.channel}
            state = self._state_of(prog, now)
            if state is catchup.CatchupState.AVAILABLE:
                return {
                    "action": "play_catchup",
                    "channel": self.channel,
                    "program": prog,
                }
            if state is catchup.CatchupState.UNSUPPORTED_TEMPLATE:
                # La lista declara archivo pero su plantilla usa marcadores que
                # no reconocemos. No se promete: se explica y se ofrece el
                # intento bajo riesgo propio, con su aviso (CU-07).
                return self._ofrecer_intento(prog, state)
            # Sin catch-up declarado, o fuera de la ventana: se explica por
            # qué en un modal, con el motivo exacto (§13). No se ofrece una
            # acción que no existe.
            _warn(self.app, catchup.describe_state(state, self.catchup))
        return None

    def _ofrecer_intento(self, prog, state) -> dict | None:  # noqa: ANN001
        """Modal de riesgo propio y, si el usuario acepta, el intento.

        El aviso va **antes** de la pregunta, no después: quien acepta ya
        tiene que saber lo que está aceptando. Cancelar no cuesta nada y
        deja el directo disponible, que es la salida por defecto.
        """
        motivo = catchup.describe_state(state, self.catchup)
        aviso = catchup.aviso_riesgo_propio(self.catchup.source_template)
        fn = getattr(self.app, "_confirm_risk", None) or getattr(
            self.app, "_confirm", None
        )
        if not callable(fn):
            # Sin UI (tests, dobles): se explica y no se ofrece nada.
            _warn(self.app, motivo)
            return None
        acepta = fn("Pedir el archivo bajo tu propio riesgo", f"{motivo}\n\n{aviso}")
        if not acepta:
            return {"action": "open_channel", "channel": self.channel}
        return {
            "action": "play_catchup",
            "channel": self.channel,
            "program": prog,
            "permitir_no_soportada": True,
        }

    def handle_mouse(self, mx: int, my: int, screen) -> bool:  # noqa: ANN001
        max_y, max_x = self.app.stdscr.getmaxyx()
        body_h = self.app.body_height()
        if my < 1 or my >= 1 + body_h:
            return False
        idx = self._list.top + (my - 1)
        if 0 <= idx < len(self._list.items):
            self._list.selected = idx
            return True
        return False

    def _epg_is_loading(self) -> bool:
        """¿Sigue el hilo de EPG de la App descargando la guía?

        Loading y vacío son categorías distintas (§17) y aquí **no** hay carga
        propia que anunciar: `refresh_programs()` es síncrono y se llama en
        `__init__`. Lo único que puede estar en curso es el hilo de fondo que
        la App ya lanza al abrir la playlist, así que se **deriva de él** en
        lugar de inventar un flag que nunca se vería en pantalla (estado
        muerto).

        Se lee con `getattr` defensivo: los tests usan dobles de `App` que no
        tienen ese atributo, y un `AttributeError` en `render` sería una
        pantalla rota por un detalle de implementación. Si algún día `App`
        expone el estado como propiedad pública, esto cambia en una línea.
        """
        return getattr(self.app, "_epg_load_thread", None) is not None

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.programs:
            if self._epg_is_loading():
                # Loading real: se está obteniendo la guía. Decir "no hay
                # programación" aquí mentiría (§11) y haría que recargar con
                # `r` no arreglara nada.
                EmptyState.render(stdscr, max_y // 2, max_x, icons.ICON_TIME,
                                  "Cargando programación…",
                                  "Espera un momento.")
            else:
                # Vacío real. La CTA apunta a `r`, que esta pantalla ya atiende
                # (`handle_key`) y ya anuncia en `actions()`/`shortcuts()`: no
                # es una acción nueva, es una tecla que ya existía.
                EmptyState.render(stdscr, max_y // 2, max_x, icons.ICON_EPG,
                                  "No hay programación disponible",
                                  "La guía no contiene programas para este canal.",
                                  "[R] Actualizar guía")
        else:
            rows = max(1, self.app.body_height())
            self._list.clamp(rows)
            now = datetime.now().astimezone()
            for i in range(rows):
                idx = self._list.top + i
                line_y = 1 + i
                if line_y >= max_y - 2:
                    break
                if idx >= len(self._list.items):
                    continue
                text = self._list.items[idx][: max(0, max_x - 2)]
                is_current = False
                is_archive = False
                if idx < len(self.programs):
                    prog = self.programs[idx]
                    is_current = self._is_current(prog, now)
                    is_archive = (not is_current
                                  and self._state_of(prog, now)
                                  is catchup.CatchupState.AVAILABLE)
                if idx == self._list.selected:
                    prefix = " ▸ "
                    if is_current:
                        attr = colors.pair(colors.PAIR_CURRENT) | curses.A_BOLD | curses.A_REVERSE
                    elif is_archive:
                        attr = colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD | curses.A_REVERSE
                    else:
                        attr = colors.pair(colors.PAIR_SELECTED) | curses.A_BOLD
                else:
                    prefix = "   "
                    if is_current:
                        attr = colors.pair(colors.PAIR_CURRENT) | curses.A_BOLD
                    elif is_archive:
                        attr = colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD
                    else:
                        attr = colors.pair(colors.PAIR_NORMAL)
                full_line = f"{prefix}{text}"
                try:
                    stdscr.addstr(line_y, 0, full_line.ljust(max_x - 1)[: max_x - 1], attr)
                except curses.error:
                    pass


def open_playlist(app, entry: PlaylistEntry) -> None:
    """Carga y parsea una entrada del catálogo, empujando ChannelsScreen.

    Para fuentes Xtream: pide password si no está en memoria, autentica,
    descarga canales y los normaliza a Channel.

    Rendimiento: si la misma fuente ya se abrió en esta sesión (y sigue
    vigente: mtime local / TTL de URL) se reutiliza la caché de sesión y
    la apertura es instantánea; solo la primera vez se parsea, y mientras
    tanto se pinta la pantalla de "Cargando…" para que no parezca colgada.
    """
    if entry.kind == "xtream":
        _open_xtream_playlist(app, entry)
        return

    from .app import load_playlist_source
    from .errormsg import describe_playlist_error, empty_playlist_error

    source = entry.source.strip()
    get_cached = getattr(app, "cached_playlist", None)
    playlist = get_cached(source) if callable(get_cached) else None
    if playlist is None:
        try:
            app.show_loading(f"Abriendo '{entry.name}'…", sub=source)
        except Exception:
            pass  # sin curses (tests) o terminal rota: sigue sin cargar
        try:
            playlist = load_playlist_source(
                source, allow_private=entry.allow_private_network
            )
        except (OSError, ValueError) as exc:
            _mostrar_error(
                app,
                describe_playlist_error(exc),
                # El retry **es** la operación que falló, no un segundo intento
                # con otro camino: se llama a sí misma con la misma entrada
                # (§7). `load_playlist_source` no cachea un fallo, así que
                # repetirlo vuelve a pedir la lista.
                retry=lambda: open_playlist(app, entry),
            )
            return
        remember = getattr(app, "remember_playlist", None)
        if callable(remember):
            remember(source, playlist)
        else:  # app de tests sin caché de sesión
            try:
                app.playlist_cache[source] = playlist
            except Exception:
                pass
    playlist.kind = entry.kind
    if not playlist.channels:
        # **Sin** `retry`: recargar una lista vacía no la va a llenar, y el
        # §13 no permite anunciar una tecla que no repara nada.
        _mostrar_error(app, empty_playlist_error(entry.name))
        return
    _explicar_camaras(app, playlist)
    auto_epg = getattr(app, "auto_load_playlist_epg", None)
    if callable(auto_epg):
        # La lista puede traer su EPG en la cabecera: se carga en segundo
        # plano (sin bloquear) y los canales muestran parrilla en cuanto
        # termina. Si no trae EPG, no hace nada.
        try:
            auto_epg(playlist)
        except Exception:
            pass
    app.push(ChannelsScreen(app, playlist))


def _explicar_camaras(app, playlist) -> None:  # noqa: ANN001
    """Explica, una vez por sesión, el flujo de credenciales de las cámaras (H11).

    Es el sitio donde el usuario se entera de que su M3U traía líneas
    ``rtsp://`` con usuario y contraseña, y de que a partir de ahora esa
    contraseña **no está en la lista**: está en el almacén del sistema y el
    canal lleva una referencia. Sin este aviso, el primer favorito de una
    cámara parece un canal normal, y cuando alguien mira el M3U ve una URL
    distinta a la que escribió y no sabe por qué.

    Una vez por sesión y por fuente: un aviso que aparece cada vez que se abre
    la lista deja de leerse, y uno que no se lee no informa de nada. Cambiar de
    lista vuelve a explicar, porque es otra situación.

    Nunca lanza: si la UI no está (tests) o el modal no se puede pintar, se
    degrada en silencio. Explicar es informativo; romper la apertura, no.
    """
    from thetvview.cam_ref import camera_names, explain_camera_flow

    try:
        nombres = camera_names(getattr(playlist, "channels", None) or ())
    except Exception:  # noqa: BLE001 - un aviso nunca impide abrir la lista
        return
    if not nombres:
        return
    vistos = getattr(app, "_camaras_explicadas", None)
    if vistos is None:
        vistos = set()
        try:
            app._camaras_explicadas = vistos
        except Exception:  # noqa: BLE001 - app de pruebas sin atributos
            return
    fuente = str(getattr(playlist, "name", "") or "")
    if fuente in vistos:
        return
    vistos.add(fuente)
    aviso = getattr(app, "_show_notice", None)
    if not callable(aviso):
        return
    try:
        aviso("Cámaras IP en la lista", explain_camera_flow(len(nombres)))
    except Exception:  # noqa: BLE001 - igual que arriba
        return


def _open_xtream_playlist(app, entry: PlaylistEntry) -> None:
    """Abre una fuente Xtream: auth + carga de canales + normalización.

    La contraseña está guardada en disco (solo Xtream); solo se pide
    si falta (listas viejas sin password). Si falla el auth, usa 'C'
    en el catálogo para cambiarla.

    Si la lista ya se abrió en esta sesión (y la caché sigue vigente) se
    reutiliza: sin llamadas a red ni re-normalización de decenas de miles
    de canales.
    """
    get_cached = getattr(app, "cached_playlist", None)
    cached = get_cached(entry.source) if callable(get_cached) else None
    if cached is not None and cached.channels:
        cached.kind = "xtream"
        app.push(ChannelsScreen(app, cached))
        return

    # Obtener credenciales (password guardado en disco, solo Xtream)
    creds = app.playlists.get_credentials(entry.name)
    if creds is None:
        # Solo listas viejas sin password: pedir una vez y guardar.
        data = app._prompt_form(
            "Xtream · contraseña",
            [("password", f"Contraseña ({entry.name})", True)],
        )
        password = (data or {}).get("password", "")
        if not password:
            app.status.show("Cancelado: se necesita contraseña.", error=True)
            return
        app.playlists.set_password(entry.name, password)
        creds = app.playlists.get_credentials(entry.name)
        if creds is None:
            app.status.show("Error al configurar credenciales.", error=True)
            return

    server_url, username, password = creds

    # Autenticar y cargar
    from thetvview.xtream_config import XtreamConfig
    from thetvview.xtream_provider import (
        authenticate,
        get_live_categories,
        get_live_streams,
    )
    from thetvview.xtream_models import normalize_live_stream, build_category_map

    cfg = XtreamConfig(
        server_url=server_url,
        username=username,
        password=password,
        allow_private_network=entry.allow_private_network,
    )
    app.show_loading("Conectando…", sub=server_url)
    # El cuerpo del mensaje sale de `friendly_message`, que ya existe y ya
    # garantiza que ningún texto para el usuario lleve un código HTTP
    # (`xtream_errors.py`). Lo que cambia es el **título**: en vez del
    # genérico «Error» —que no dice qué pasó— pasa a ser el de la lista, que sí
    # lo dice. El `detail` se deja deliberadamente vacío: `friendly_message` no
    # da status y la lista Xtream se reintenta cambiando la configuración, no
    # pulsando «reintentar» sobre lo mismo.
    reintentar = lambda: _open_xtream_playlist(app, entry)
    try:
        authenticate(cfg)
    except Exception as exc:
        _mostrar_error(
            app,
            _error_xtream(entry.name, "no se pudo abrir", exc),
            retry=reintentar,
        )
        return

    # Cargar categorías y canales
    try:
        categories = get_live_categories(cfg)
        streams = get_live_streams(cfg)
    except Exception as exc:
        _mostrar_error(
            app,
            _error_xtream(entry.name, "no se pudo cargar", exc),
            retry=reintentar,
        )
        return

    cat_map = build_category_map(categories)

    # Normalizar a Channel
    channels = []
    for s in streams:
        ch = normalize_live_stream(
            s, server_url, username, password, source_name=entry.name,
        )
        cat_name = cat_map.get(str(s.category_id))
        if cat_name:
            ch.group = cat_name
        channels.append(ch)

    if not channels:
        # Sin `retry`: la autenticación fue bien y la lista llegó vacía. Volver
        # a pedirla no la va a llenar, así que `R` no se anuncia (§13).
        _mostrar_error(app, empty_playlist_error(entry.name))
        return

    playlist = Playlist(
        name=entry.name,
        channels=channels,
        source=entry.source,
        kind="xtream",
    )
    source_key = entry.source.strip()
    remember = getattr(app, "remember_playlist", None)
    if callable(remember):
        remember(source_key, playlist)
    else:
        app.playlist_cache[source_key] = playlist
    app.push(ChannelsScreen(app, playlist))


class _ResolutorCredenciales:
    """Une el catálogo de playlists y el almacén del SO para resolver ``url``.

    Hay dos mecanismos de referencia opaca y cada uno vive en su sitio:

    - ``xtream://`` → :class:`~thetvview.playlist_manager.PlaylistManager`, que
      expone ``get_credentials(fuente)``;
    - ``ipcam://`` → :mod:`thetvview.security.secrets`, que expone
      ``get_password(clave)``.

    :func:`resolve_channel_url` habla con un único objeto, así que se le pasa
    uno que reúna las dos interfaces. Se hace aquí y no dentro de
    ``resolve_channel_url`` para que el parser M3U siga siendo una función pura
    que no toca disco, y para que los tests puedan pasar un catálogo falso.
    """

    __slots__ = ("_catalogo", "_store")

    def __init__(self, catalogo: object, store: object) -> None:
        self._catalogo = catalogo
        self._store = store

    def get_credentials(self, name: str):  # type: ignore[no-untyped-def]
        getter = getattr(self._catalogo, "get_credentials", None)
        return getter(name) if callable(getter) else None

    def get_password(self, key: str) -> str | None:
        getter = getattr(self._store, "get_password", None)
        return getter(key) if callable(getter) else None


def _clave_canal(channel) -> str:  # noqa: ANN001 - Channel
    """Identidad legible del canal para la sesión (sin URL ni secretos).

    Es la **clave**, no la identidad: el `tvg-id` si existe, y si no el nombre.
    No se pone la URL a propósito —el informe de la sesión puede acabar en un
    archivo que el usuario comparte— y con esto basta para saber de qué canal
    se trata.
    """
    return str(
        getattr(channel, "tvg_id", "") or getattr(channel, "name", "") or ""
    ).strip()


def _registro_sesiones(app) -> SessionRegistry:  # noqa: ANN001
    """Registro de sesiones de `app`, o uno propio si el doble no lo tiene.

    Los tests montan la pantalla con dobles que no son `App`; en vez de exigir el
    atributo (y romperlos), se crea un registro local. La app real **sí** lo
    tiene, y es el que importa: es el que sabe si la sesión sigue vigente.
    """
    registro = getattr(app, "_sessions", None)
    if isinstance(registro, SessionRegistry):
        return registro
    try:
        app._sessions = SessionRegistry()
        return app._sessions
    except Exception:  # pragma: no cover - doble que no admite atributos
        return SessionRegistry()


def _observar_sesion(app, sesion) -> None:  # noqa: ANN001
    """Ata un :class:`ObservadorDeSesion` al bus de `app`, si lo hay (H-01).

    Devuelve el observador para poder seguir dándole señales, sobre todo la
    evidencia de :mod:`player.evidence`: sin ella, «inicio confirmado» no
    puede ocurrir, porque ese suceso existe **precisamente** para no declarar
    que algo suena sin comprobarlo.

    Nunca lanza. Un bus que no se puede construir deja la reproducción igual,
    que es lo que pasaba antes de este ticket.
    """
    bus = getattr(app, "eventos", None)
    if bus is None:
        return None
    try:
        from thetvview.diagnostics.session_events import observar

        return observar(sesion, bus)
    except Exception:  # noqa: BLE001 - diagnosticar no puede impedir ver
        return None


def _nada():  # noqa: ANN201 - context manager vacío, para el `with` condicional
    """Context manager que no hace nada.

    Existe para poder escribir ``with A if cond else _nada():`` sin duplicar la
    llamada. Un `nullcontext` del stdlib haría lo mismo, pero esto no obliga a
    a importar contextlib para un solo uso en una línea.
    """
    return _NADA


class _Nada:  # noqa: N801 - es un context manager privado
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        # `None` y no `False`: los dos significan «no capturo la excepción», pero
        # sólo `None` es un tipo honesto para «no hago nada». Anotar `bool`
        # aquí es lo que hace que un context manager pueda tragarse
        # excepciones sin querer.
        return None


_NADA = _Nada()


def _almacen_de(app) -> object:  # noqa: ANN001
    """Resolutor de credenciales para referencias opacas.

    Las referencias ``ipcam://…`` se resuelven contra el almacén del SO, que
    es donde la contraseña existe en claro y el SO la cifra; las de Xtream,
    contra el catálogo de playlists, como hasta ahora.
    """
    from thetvview.security.secrets import get_store

    return _ResolutorCredenciales(getattr(app, "playlists", None), get_store())


def _recordar_reciente(app, channel, player_name: str) -> None:  # noqa: ANN001
    """Deja el canal en Recientes. Es un historial, no un requisito (FR-REC-01).

    Se llama con el canal **original** (sin credenciales resueltas) y con el
    reproductor que acabó ABRiéndose de verdad, no con el pedido: así Recientes
    dice la verdad aunque el router haya recurrido a otro motor.

    Nunca propaga fallos: un disco lleno o un `recents.json` con permisos
    raros no puede impedir que se vea el canal. Peor: el aviso iría por el
    mismo camino que warn/error, así que avisar desde aquí exigiría abrir un
    modal *encima* de la pantalla de reproducción. Se degrada en silencio, que
    es la degradación honesta para un dato secundario.
    """
    manager = getattr(app, "recents", None)
    push = getattr(manager, "push", None)
    if not callable(push):
        return
    try:
        push(channel, player_name)
    except Exception:
        pass


def play_channel(  # noqa: ANN001
    app,
    channel,
    player_name: str | None = None,
    *,
    catchup_playback=None,
    catchup_program=None,
    selection=None,
    capabilities=None,
    track_session=None,
) -> None:
    """Abre `channel` en el reproductor elegido.

    Cuando hay :class:`PlaybackSelection` y capacidades, se hace lo que el
    plan F5 manda, en este orden:

    1. se resuelve la URL (las referencias Xtream son opacas);
    2. si la calidad está fijada y el reproductor no la sabe elegir, se
       levanta el **proxy de pinning** y se reproduce su master; si el proxy
       no puede, se reproduce el manifest original y se explica en un modal;
    3. se abre el canal de control de mpv (siempre, cuesta cero) para poder
       cambiar de pista en caliente;
    4. se lanzan los argumentos de pistas que el reproductor sí soporta.

    El proxy vive **mientras dure la reproducción**: se lo pasa a
    :class:`NowPlayingScreen`, que lo apaga al cerrar (plan F5c: apagado
    siempre, en el ``finally``).

    Y con el multi-stream (SDD-M §8/§17/§26) el reproductor lo elige
    :mod:`thetvview.player.router` por **capacidad**, no por ser el primero que
    se encuentre; si el elegido muere al instante se prueban los siguientes
    (como mucho dos, §26) y, una vez en marcha, un
    :mod:`thetvview.player.supervisor` vigila el proceso y reconecta **el
    mismo** motor si se corta.
    """
    # Las URLs Xtream son opacas en el dominio: aquí (y sólo aquí) se
    # recuperan las credenciales. El objeto original no se toca, para que
    # la caché, favoritos y recientes sigan sin secretos (SDD §37).
    #
    # Con catch-up entra la referencia opaca `xtream-ts://…`, que se
    # resuelve por el mismo camino: el reproductor no distingue los dos
    # modos y el motor multimedia es el mismo (SDD Catch-up §14).
    try:
        resolved = resolve_channel_url(channel.url, _almacen_de(app))
    except (MissingCredentialsError, MissingCamCredentialsError) as exc:
        # **Sin** `retry`: no hay contraseña, y repetir la resolución daría
        # exactamente el mismo error. Lo que sí tiene sentido es el diagnóstico,
        # porque la credencial puede estar en el almacén del sistema y que el
        # usuario lo ignore (F5).
        _mostrar_error(
            app,
            _error_credenciales(channel.name, str(exc)),
            diagnose=lambda: _diagnosticar(app, channel),
        )
        return
    es_camara = resolved != channel.url
    # El canal **tal como entró**, sin la URL resuelta: es el único que se
    # escribe en Recientes, porque `resolved` lleva las credenciales que se
    # reconstruyeron (SDD §37). A disco nunca van credenciales (H7).
    canal_original = channel
    if es_camara:
        channel = replace(channel, url=resolved)

    proxy = None
    proxy_url: str | None = None
    avisos: list[str] = []

    # Preferencias de multi-stream (§33). Se leen aquí, una vez, y se pasan
    # explícitas: `play_channel` no debe ir a buscar el fichero de preferencias
    # por su cuenta, porque los tests lo llaman con una app que no tiene `prefs`
    # de disco.
    preferred_backend = ""
    reconnect_max_attempts = 5
    gestor = getattr(app, "prefs", None)
    if gestor is not None:
        try:
            ajustes = gestor.load()
            preferred_backend = ajustes.preferred_backend or ""
            reconnect_max_attempts = ajustes.reconnect_max_attempts
        except Exception:
            # Una preferencia corrupta no impide reproducir: se reproduce con
            # los valores conocidos, que es el comportamiento de siempre.
            preferred_backend = ""
            reconnect_max_attempts = 5
    if selection is not None and capabilities is not None:
        proxy, avisos = _start_pin(capabilities, selection, player_name)
        if proxy is not None:
            proxy_url = proxy.url

    ipc_path: str | None = None
    # El canal de control sólo se abre cuando hay una sesión de pistas
    # viva: si el canal no expone alternativas, no hay nada que cambiar en
    # caliente y el socket sería trabajo inútil. Con esto, una reproducción
    # sin pistas es byte a byte la de siempre (§32).
    if track_session is not None and (player_name or "").strip().lower() == "mpv":
        try:
            from thetvview.player.mpv_ipc import ipc_path as _ipc_path

            ipc_path = _ipc_path()
        except Exception:
            avisos.append(
                "No se pudo abrir el canal de control del reproductor: "
                "los cambios de pista habrá que hacerlos al reabrir el canal."
            )
            ipc_path = None

    # Los kwargs de pistas sólo se pasan si hay algo que decir. Con la
    # llamada vacía, `player.launch()` se invoca exactamente como antes de
    # esta funcionalidad: es la garantía de que un stream simple no cambia
    # de camino (SDD §32).
    extra: dict = {}
    if selection is not None:
        extra["selection"] = selection
        extra["capabilities"] = capabilities
    if proxy_url:
        extra["proxy_url"] = proxy_url
    if ipc_path:
        extra["ipc_path"] = ipc_path
    if preferred_backend:
        # Criterio 1 del §27: el reproductor que el usuario eligió por
        # preferencia. No es lo mismo que `last_player` (el último usado), y por
        # eso viaja en `prefs.preferred_backend` y no aquí.
        extra["preferred"] = preferred_backend
    # Criterio FR-CAP-007: sólo si **no** hay preferencia del usuario y no
    # pidió motor explícito, el router puede apoyarse en una combinación
    # que ya funcionó para este transporte. Viaja como `preferred` (el
    # criterio 1) y no dentro de `extra`, para no duplicar el kwarg en el
    # fallback de `_lanzar_con_router`. `combinaciones` lo crea App.run():
    # en tests no existe y aquí no se toca disco.
    cache = getattr(app, "combinaciones", None)
    if cache is not None and not preferred_backend and not player_name:
        ganador = _combinacion_exitosa(cache, channel.url)
        if ganador is not None:
            preferred_backend = ganador.reproductor
    fallos: set[str] = set()
    # Sesión de reproducción (SDD §4). Se abre **antes** del lanzamiento: si el
    # lanzamiento falla, la sesión termina con `init_failed` y su motivo queda
    # registrado, en vez de ser un `return` sin rastro (FR-PLAY-010).
    registro = _registro_sesiones(app)
    sesion = registro.start(_clave_canal(channel), player_name or "")
    # H-01: la sesión nace **atada** al bus de eventos. Se ata aquí, y no más
    # tarde, para que el fallo de un lanzamiento que ni siquiera ocurre
    # deje rastro igual que el de uno que sí ocurrió.
    #
    # `getattr` defensivo: los tests montan pantallas con dobles que no son
    # `App`, y sin bus no hay eventos — que es exactamente lo que se quiere
    # cuando no hay bus.
    observador = _observar_sesion(app, sesion)
    sesion.start()
    try:
        # Si el canal venía como referencia opaca de cámara, `resolved` lleva
        # la credencial que se reconstruyó desde el keyring. Se marca como
        # «reconstruida por la app» sólo durante la construcción del argv:
        # `validate_url` rechaza el userinfo en todos los esquemas, y esa regla
        # protege las URLs que vienen de una lista, así que la única excepción
        # posible es aquí y se declara, no se deduce.
        with player.url_de_camara_resuelta(resolved) if es_camara else _nada():
            proc, effective_name = _lanzar_con_router(
                channel, player_name, preferred_backend, fallos, extra
            )
    except player.PlayerError as exc:
        # La sesión se sella **antes** de tocar el proxy a mano: el motivo real
        # es `init_failed`, y un fallo al apagar el proxy no puede pisarlo.
        sesion.end(
            EndReason.INIT_FAILED,
            "" if isinstance(exc, NoCompatibleBackend) else str(exc),
        )
        registro.finish(sesion, EndReason.INIT_FAILED)
        if proxy is not None:
            proxy.stop()
        # El socket de control se reservó **antes** del lanzamiento; si el
        # lanzamiento falla, el fichero existe aunque mpv nunca llegara a
        # abrirlo, así que se limpia aquí (FR-PLAY-010: la ruta de limpieza se
        # ejecuta aunque el lanzamiento no tuviera éxito).
        if ipc_path:
            _borrar_socket(ipc_path)
            if track_session is not None:
                track_session.ipc_path = ""
        # Un `NoCompatibleBackend` no es «falló la reproducción» sino «este
        # canal no se puede abrir aquí» (§14 del SDD-M). Se preserva tal cual,
        # con su modal de un botón y sin capa de presentación encima: el
        # diagnóstico no añadiría nada (el problema es del entorno, no del
        # canal) y el retry volvería a elegir los mismos reproductores.
        if isinstance(exc, NoCompatibleBackend):
            _error(app, str(exc))
            return
        # El retry es un **cierre sobre los mismos argumentos** con los que se
        # llegó aquí (F5, H10): no se reconstruye el router ni se toca
        # `player/router.py` ni el supervisor (§17). `extra` ya tiene el
        # `proxy_url` resuelto, y `proxy` se ha parado justo antes, así que un
        # reintento rebuilding construye uno nuevo.
        _mostrar_error(
            app,
            describe_playback_error(exc),
            retry=lambda: play_channel(
                app,
                channel,
                player_name,
                catchup_playback=catchup_playback,
                catchup_program=catchup_program,
                selection=selection,
                capabilities=capabilities,
                track_session=track_session,
            ),
            diagnose=lambda: _diagnosticar(app, channel),
        )
        return
    if track_session is not None:
        track_session.ipc_path = ipc_path
    app.push(NowPlayingScreen(
        app, channel, effective_name, proc,
        catchup_playback=catchup_playback,
        catchup_program=catchup_program,
        track_session=track_session,
        pin_proxy=proxy,
        failures=fallos,
        relanzar=(
            (lambda: _relanzar_mismo_motor(
                channel, effective_name, extra, app
            ))
            if player_name is None
            else None
        ),
        intentos_maximos=reconnect_max_attempts,
        session=sesion,
        es_vigente=lambda: registro.is_current(sesion.session_id),
        # H-01: la evidencia llega al observador, que es quien decide si lo
        # medido basta para decir «está sonando». El supervisor no sabe qué
        # hay al otro lado, y así `player` no depende de `diagnostics`.
        al_medir=(observador.evidencia if observador is not None else None),
    ))
    app.prefs.set_last_player(effective_name)
    _recordar_reciente(app, canal_original, effective_name)
    if cache is not None and not player_name:
        # FR-CAP-007: sólo la ruta del router deja combinación en la
        # caché; un lanzamiento explícito ya queda en `prefs` y además la
        # preferencia del usuario manda sobre la caché.
        _guardar_combinacion(cache, channel.url, effective_name)
    for aviso in _avisos_sin_repetir(app, channel, effective_name, avisos):
        app.notify_warning(aviso)
    if catchup_playback is not None:
        arch = (catchup_playback.start.astimezone().strftime("%H:%M")
                if catchup_playback.start else "?")
        app.status.show(
            f"Archivo: '{channel.name}' desde las {arch} con "
            f"{effective_name.upper()} (pid {proc.pid})."
        )
        return
    app.status.show(f"Reproduzco '{channel.name}' con {effective_name.upper()} (pid {proc.pid}).")


def _diagnosticar(app, channel) -> None:  # noqa: ANN001
    """Abre el diagnóstico **existente** con comprobación de conexión (F5, H9).

    Es un reenvío a ``App.show_diagnose``, no un segundo mecanismo: el informe,
    la redacción y la pantalla son las que ya había (SDD §11). Lo único que se
    añade es ``con_conexion=True``, porque el §11 lo pide —«puedes revisar el
    diagnóstico» sólo significa algo si el diagnóstico comprueba de verdad— y
    ese modo ya estaba cableado en ``handle_action`` desde antes.
    """
    fn = getattr(app, "show_diagnose", None)
    if callable(fn):
        fn(channel, con_conexion=True)


def _transporte_clave(url: str) -> str | None:
    """Clave de caché para una URL: su transporte, **nunca** la URL.

    Las URLs de IPTV pueden llevar token en la query o userinfo
    reconstruido, así que a disco no va la URL (misma regla que para
    Recientes, SDD §37). El transporte es además lo único sobre lo que
    decide el router, así que la granularidad es exacta (FR-CAP-007).
    """
    from thetvview.streams.detector import detect_protocol
    from thetvview.streams.transport import Transport

    try:
        transporte = detect_protocol(url=url).transport
    except Exception:
        return None
    if transporte is Transport.UNKNOWN:
        return None
    return transporte.value


def _combinacion_exitosa(cache, url: str):  # noqa: ANN001
    """La combinación que ya abrió este transporte, o None (FR-CAP-007)."""
    clave = _transporte_clave(url)
    if not clave:
        return None
    try:
        return cache.buscar(clave)
    except Exception:
        # Una caché ilegible no cambia el comportamiento de siempre: el
        # router decide solo.
        return None


def _guardar_combinacion(cache, url: str, reproductor: str) -> None:  # noqa: ANN001
    """Anota el motor que acaba de abrir este transporte (sin la URL)."""
    clave = _transporte_clave(url)
    if not clave or not reproductor:
        return
    try:
        cache.recordar(clave, reproductor)
    except Exception:
        # Una caché que falla no puede romper una reproducción.
        pass


def _error_credenciales(nombre: str, detalle: str):
    """`:class:`UiError` de una credencial ausente, sin `retry` (E6).

    El cuerpo conserva el texto del dominio (``MissingCredentialsError`` /
    ``MissingCamCredentialsError`` ya están escritos para el usuario y no llevan
    ni la URL ni el usuario), pero **redactado**: no se copia sin pasar por
    `redact_text`, porque el contrato de §2.4 no admite excepciones por «el
    mensaje ya venía bien».
    """
    from thetvview.security.redaction import redact_text

    from .errormsg import ErrorKind, UiError

    return UiError(
        title="No se pudo reproducir este canal",
        message=redact_text(str(detalle)),
        detail=None,
        kind=ErrorKind.PLAYBACK,
    )


def _lanzar_con_router(channel, player_name, preferred, fallos, extra):  # noqa: ANN001
    """Lanza el reproductor y devuelve ``(proceso, nombre)``.

    Con ``player_name`` explícito es un ``launch`` de toda la vida. Sin él, el
    router decide por capacidad y, si el que eligió falla **inmediatamente**,
    se prueban los siguientes hasta el tope del §26 — lo que también evita el
    ciclo mpv→vlc→mpv→vlc porque ``fallos`` recuerda lo ya probado.
    """
    from thetvview.player.router import MAX_BACKEND_ATTEMPTS, NoCompatibleBackend

    if player_name:
        return player.launch(channel, player_name=player_name, **extra), player_name

    from thetvview.player.router import attempts

    candidatos = attempts(
        channel.url, preferred=preferred, failures=fallos
    )
    if not candidatos:
        # Sin candidatos viables no se lanza nada: se delega el motivo al
        # router, que distingue «no instalado» de «no lo abre nadie».
        return player.launch(
            channel, player_name=None, preferred=preferred, **extra
        )
    ultimo: Exception | None = None
    for indice, cand in enumerate(candidatos[:MAX_BACKEND_ATTEMPTS]):
        try:
            proc = player.launch(channel, player_name=cand.name, **extra)
        except player.PlayerError as exc:
            fallos.add(cand.name)
            ultimo = exc
            continue
        return proc, cand.name
    if ultimo is not None:
        raise ultimo
    raise NoCompatibleBackend(  # pragma: no cover - `candidatos` no está vacío
        "No se pudo abrir el canal con ningún reproductor instalado."
    )


def _relanzar_mismo_motor(channel, player_name, extra, app):  # noqa: ANN001
    """Relanza el mismo reproductor (lo usa el supervisor tras una caída).

    Se relanza **el mismo** y no otro: el §26 trata el cambio de reproductor
    como una decisión distinta, con su propio tope, y mezclarla aquí haría que
    un corte de red acabara abriendo el canal con otro motor sin que nadie lo
    pidiera.
    """
    try:
        proc = player.launch(channel, player_name=player_name, **extra)
    except player.PlayerError:
        return None
    _warn(app, "El canal se cortó; reintentando con el mismo reproductor.")
    return proc


def _avisos_sin_repetir(app, channel, player_name, avisos):  # noqa: ANN001
    """Quita los avisos ya mostrados para este canal con este reproductor.

    Los avisos de pistas son **informativos**, no preguntas: explican que una
    elección no se va a aplicar (por ejemplo, subtítulos que el reproductor no
    ve). Si el usuario reabre el canal con «s» y vuelve a confirmar, no hace
    falta volver a decírselo: un aviso que se repite en cada pulsación deja
    de leerse, y un aviso que no se lee no informa de nada.

    El recuerdo es por sesión y por (canal, reproductor): cambiar de canal o
    de reproductor vuelve a explicar, porque la situación es otra.
    """
    if not avisos:
        return avisos
    vistos = getattr(app, "_avisos_mostrados", None)
    if vistos is None:
        vistos = set()
        try:
            app._avisos_mostrados = vistos
        except Exception:  # noqa: BLE001 - app de pruebas sin atributos
            return avisos
    clave_canal = (
        getattr(channel, "tvg_id", "") or getattr(channel, "name", "") or ""
    )
    clave = (clave_canal, str(player_name or ""))
    nuevos = [aviso for aviso in avisos if (clave, aviso) not in vistos]
    vistos.update((clave, aviso) for aviso in avisos)
    return nuevos


def _start_pin(capabilities, selection, player_name):  # noqa: ANN001
    """Levanta el proxy de calidad si hace falta. Devuelve (proxy, avisos).

    Si la calidad es automática no hay nada que fijar y no se abre ningún
    puerto (AC-08). Si el proxy no puede arrancar, se devuelve None y un
    aviso: el canal se reproduce con calidad automática, que es mejor que
    no reproducir (SDD §48).
    """
    from thetvview.player.track_args import track_warnings
    from thetvview.streams.pin_proxy import PinProxyError, start_pin_proxy

    avisos: list[str] = []
    if selection is None or selection.is_default:
        return None, avisos
    avisos.extend(track_warnings(player_name or "", selection, capabilities))
    if selection.auto_quality or not selection.video_track_id:
        return None, avisos
    try:
        proxy = start_pin_proxy(capabilities, selection)
    except PinProxyError as exc:
        return None, avisos + [str(exc)]
    if proxy is None:
        # `start_pin_proxy` no levanta por dos motivos muy distintos y ya está
        # explicado `track_warnings` cuando el formato no admite fijado (sólo
        # HLS). Decirlo dos veces sería puro ruido, así que aquí sólo se avisa
        # del otro: que el puerto no se pudo abrir.
        from thetvview.tracks.models import puede_fijar_calidad

        if puede_fijar_calidad(capabilities):
            avisos.append(
                "No se pudo fijar la calidad elegida; se reproduce con calidad "
                "automática, que es lo que hace el reproductor por defecto."
            )
        return None, avisos
    return proxy, avisos


def options_kinds(options, kind: str | None = None) -> list[str]:  # noqa: ANN001
    """Secciones visibles del selector, en orden estable.

    Si `kind` viene dado (una tecla como ``a`` o ``s`` desde la pantalla de
    reproducción), se queda sólo con esa sección: así la pantalla explica
    *sólo* lo que el usuario ha pedido cambiar.
    """
    if kind:
        disponibles = options.selectable_kinds
        return [kind] if kind in disponibles else []
    return list(options.selectable_kinds)


class SchedulerScreen(Screen):
    """Pantalla del planificador de grabaciones (U-10).

    Muestra las tareas programadas, permite crear nuevas (con formulario),
    cancelar futuras y borrar completadas/canceladas. El scheduler vive en
    ``app._scheduler`` y delega la captura en el grabador de Fase 6.
    """

    title = "Planificador"

    def __init__(self, app) -> None:  # noqa: ANN001
        super().__init__(app)
        self.tasks: list = []
        self.selected: int = 0
        self._first_row: int = 0
        self.reload()

    def reload(self) -> None:
        scheduler = getattr(self.app, "_scheduler", None)
        if scheduler is None:
            self.tasks = []
        else:
            try:
                self.tasks = scheduler.tasks_list()
            except Exception:
                self.tasks = []
        if self.selected >= len(self.tasks):
            self.selected = max(0, len(self.tasks) - 1)
        self._clamp_scroll()

    def current_task(self):
        if not self.tasks:
            return None
        return self.tasks[self.selected]

    def _visible_rows(self, max_y: int) -> int:
        row_h = 4
        start_y = 1
        available = max_y - start_y - 2
        if available <= 0:
            return 1
        return max(1, available // row_h)

    def _clamp_scroll(self) -> None:
        if not self.tasks:
            self._first_row = 0
            return
        try:
            max_y, _ = self.app.stdscr.getmaxyx()
        except Exception:
            return
        rows = self._visible_rows(max_y)
        if self.selected < self._first_row:
            self._first_row = self.selected
        elif self.selected >= self._first_row + rows:
            self._first_row = self.selected - rows + 1
        max_row = max(0, len(self.tasks) - rows)
        self._first_row = max(0, min(self._first_row, max_row))

    def header_context(self) -> str:
        return f"{icons.ICON_TIME} Planificador · {len(self.tasks)}"

    def shortcuts(self) -> str:
        if not self.tasks:
            return "a Añadir · r Recientes · ? Ayuda · t Tema · q Salir"
        return "↑/↓ · Enter Ver · a Añadir · c Cancelar · d Borrar · r Recientes · ? Ayuda · t Tema · q Salir"

    def actions(self) -> list:
        if not self.tasks:
            return [
                Action("a", V.ANADIR, P.FRECUENTE),
                Action("?", V.AYUDA, P.AYUDA, essential=True),
            ]
        return [
            Action("Enter", V.VER, P.PRIMARIA),
            Action("a", V.ANADIR, P.FRECUENTE),
            Action("c", V.CANCELAR, P.CONTEXTUAL),
            Action("d", V.BORRAR, P.CONTEXTUAL),
            Action("r", V.RECIENTES, P.NAVEGACION),
            Action("?", V.AYUDA, P.AYUDA, essential=True),
        ]

    def handle_key(self, key: int) -> dict | None:
        if not self.tasks:
            if key == ord("a"):
                return {"action": "add_scheduled_task"}
            if key == ord("r"):
                return {"action": "show_recents"}
            return None

        rows = self.app.body_height()
        max_y, _ = self.app.stdscr.getmaxyx()

        if key == curses.KEY_UP:
            self.selected = max(0, self.selected - 1)
        elif key == curses.KEY_DOWN:
            self.selected = min(len(self.tasks) - 1, self.selected + 1)
        elif key == ord("a"):
            return {"action": "add_scheduled_task"}
        elif key == ord("c"):
            task = self.current_task()
            if task is None:
                return None
            return {"action": "cancel_scheduled_task", "task_id": task.task_id}
        elif key == ord("d"):
            task = self.current_task()
            if task is None:
                return None
            return {"action": "remove_scheduled_task", "task_id": task.task_id}
        elif key == ord("r"):
            return {"action": "show_recents"}
        elif key in (curses.KEY_ENTER, 10, 13):
            task = self.current_task()
            if task is None:
                return None
            return {"action": "show_scheduled_task", "task_id": task.task_id}
        else:
            return None

        self._clamp_scroll()
        return None

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.tasks:
            EmptyState.render(
                stdscr, max_y // 2, max_x,
                icons.ICON_TIME,
                "No hay grabaciones programadas",
                "Programa una grabación para que se inicie automáticamente.",
                "[A] Añadir grabación programada",
            )
            return

        row_h = 4
        start_y = 1
        visible_rows = self._visible_rows(max_y)

        for i, task in enumerate(self.tasks):
            row = i // 1
            if row < self._first_row:
                continue
            if row >= self._first_row + visible_rows:
                break

            y = start_y + (row - self._first_row) * row_h
            if y + row_h > max_y - 2:
                break

            selected = i == self.selected

            # Badge de estado
            status = getattr(task, "status", None)
            status_text = getattr(status, "value", str(status)) if status else "?"

            # Hora de inicio
            start_iso = getattr(task, "presentation_time", None) or ""
            if start_iso:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(start_iso)
                    start_str = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    start_str = start_iso[:16]
            else:
                start_str = "--"

            # Duración
            duration = int(getattr(task, "duration_seconds", 0) or 0)
            if duration >= 3600:
                dur_str = f"{duration // 3600}h {(duration % 3600) // 60}min"
            elif duration >= 60:
                dur_str = f"{duration // 60}min"
            else:
                dur_str = f"{duration}s"

            # Canal / fuente
            channel_id = getattr(task, "channel_id", None) or getattr(task, "source", None) or "?"

            # Colores según estado
            if selected:
                attr = colors.pair(colors.PAIR_SELECTED) | curses.A_BOLD
                status_attr = colors.pair(colors.PAIR_SELECTED) | curses.A_BOLD
            elif status_text == "running":
                attr = colors.pair(colors.PAIR_SUCCESS) | curses.A_BOLD
                status_attr = colors.pair(colors.PAIR_SUCCESS) | curses.A_BOLD
            elif status_text in ("completed",):
                attr = colors.pair(colors.PAIR_NORMAL)
                status_attr = colors.pair(colors.PAIR_DIM)
            elif status_text in ("failed", "missed"):
                attr = colors.pair(colors.PAIR_DANGER)
                status_attr = colors.pair(colors.PAIR_DANGER) | curses.A_BOLD
            elif status_text in ("cancelled", "skipped"):
                attr = colors.pair(colors.PAIR_DIM)
                status_attr = colors.pair(colors.PAIR_DIM)
            else:
                attr = colors.pair(colors.PAIR_NORMAL)
                status_attr = colors.pair(colors.PAIR_PRIMARY)

            # Fondo si está seleccionado
            if selected:
                try:
                    for r in range(row_h):
                        if y + r < max_y - 2:
                            stdscr.addstr(y + r, 0, " " * max_x, colors.pair(colors.PAIR_SELECTED))
                except curses.error:
                    pass

            # Badge de estado
            badge = f"[{status_text.upper()}]"
            try:
                stdscr.addstr(y, 2, badge, status_attr)
                stdscr.addstr(y, 2 + len(badge) + 1, f" {start_str}", attr)
                stdscr.addstr(y, max_x - len(dur_str) - 3, dur_str, attr | curses.A_DIM)
            except curses.error:
                pass

            # Canal / fuente
            try:
                stdscr.addstr(y + 1, 4, f" {channel_id}"[:max(0, max_x - 5)], attr | curses.A_DIM)
            except curses.error:
                pass

            # Mensaje si hay uno
            message = getattr(task, "message", None)
            if message:
                try:
                    stdscr.addstr(y + 2, 4, f" {message}"[:max(0, max_x - 5)],
                                  colors.pair(colors.PAIR_DIM) if not selected else colors.pair(colors.PAIR_SELECTED) | curses.A_DIM)
                except curses.error:
                    pass

            # Separador
            try:
                stdscr.addstr(y + row_h - 1, 2, "─" * max(0, max_x - 4),
                              colors.pair(colors.PAIR_BORDER) | curses.A_DIM)
            except curses.error:
                pass
