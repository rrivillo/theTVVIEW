"""Pantallas de la TUI theTVVIEW.

Cada pantalla implementa:
- handle_key(key) -> action | None  (acción para el App)
- render(stdscr)

Pantallas: catálogo de playlists, canales (con búsqueda incremental '/'),
favoritos ('f') y parrilla EPG por canal ('e').
"""

from __future__ import annotations

import curses
import time
from datetime import datetime

from thetvview import player, resolutions
from thetvview import config
from thetvview.channel_health import ChannelHealthMonitor
from thetvview.epg_parser import Epg, parse_file
from thetvview.groups import groups_of
from thetvview.models import Channel, Playlist
from thetvview.playlist_manager import PlaylistEntry, PlaylistError, PlaylistManager
from thetvview.recents import RecentsManager

from . import colors
from . import icons
from .widgets import ScrollableList, render_empty_message, render_separator


# Centinela: ChannelsScreen sin filtro de grupo (vs. filtro "sin grupo").
_UNSET: object = object()

# F5 para "actualizar lista" ('R' es el atajo principal; 'r' queda para
# Recientes). Fallback numérico por si curses no define KEY_F5.
_KEY_F5: int = getattr(curses, "KEY_F5", 269)


class Screen:
    """Base de pantalla: título y manejo mínimo de teclas."""

    title = ""

    def __init__(self, app) -> None:  # noqa: ANN001 - referencia circular evitada con typing diferido
        self.app = app

    def shortcuts(self) -> str:
        return "↑/↓ mover · Enter seleccionar · Esc volver · q salir"

    def handle_key(self, key: int) -> dict | None:
        return None

    def _render_title(self, stdscr: curses.window) -> None:
        pass  # Header lo gestiona App.run

    def render(self, stdscr: curses.window) -> None:
        raise NotImplementedError


def _epg_channel_id(epg: Epg, channel: Channel) -> str | None:
    """Resuelve el channel_id del EPG para un canal (tvg-id, luego nombre)."""
    if channel.tvg_id and channel.tvg_id in epg.programs:
        return channel.tvg_id
    want = (channel.tvg_name or channel.name).casefold()
    for cid, name in epg.channels_by_id.items():
        if cid in epg.programs and name.casefold() == want:
            return cid
    return None


def format_channel_name(ch: Channel, *, favorite: bool = False) -> str:
    """Etiqueta de canal para las listas: ★ favorito, ♪ radio."""
    fav = "★ " if favorite else ""
    radio = "♪ " if ch.radio else ""
    base = f"[{ch.group}] {ch.name}" if ch.group else ch.name
    return f"{fav}{radio}{base}"


class PlaylistsScreen(Screen):
    """Catálogo de playlists registradas (playlists.json) — vista cards."""

    title = "Playlists"

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
            return "a Añadir · r Recientes · t Tema · ? Ayuda · q Salir"
        return "↑/↓/←/→ · Enter · a Añadir · d Borrar · R Actualizar · r Recientes · f ★ · t Tema · ? Ayuda · q Salir"

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
        elif key == ord("d"):
            entry = self.current_entry()
            if entry is None:
                self.app.footer.show("No hay playlists que borrar.")
                return None
            return {"action": "remove_playlist", "name": entry.name}
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
        border_attr = colors.pair(colors.PAIR_PRIMARY if selected else colors.PAIR_BORDER)
        text_attr = colors.pair(colors.PAIR_SELECTED if selected else colors.PAIR_NORMAL)
        dim_attr = colors.pair(colors.PAIR_DIM)

        card_h = 5
        if card_y + card_h > max_y - 2:
            return

        # Fondo sólido para selected
        if selected:
            try:
                for r in range(card_h):
                    stdscr.addstr(card_y + r, card_x, " " * card_w, colors.pair(colors.PAIR_SELECTED))
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
            from .widgets import EmptyState
            EmptyState.render(stdscr, max_y // 2, max_x,
                              "No hay playlists registradas.",
                              "pulsa 'a' para añadir")
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
        self.list = ScrollableList()
        self.query = ""
        self.searching = False
        # Índices (sobre self.channels) de los canales visibles tras filtrar.
        self.visible_idx: list[int] = []
        # Snapshot de favoritas + flag de estrella del título: se calculan
        # en _apply_filter (una sola pasada en memoria) para no hacer IO
        # por canal ni por frame con listas grandes.
        self._fav_urls: set[str] = set()
        self._has_fav: bool = False
        self._apply_filter()

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

    def _apply_filter(self, keep_selection: bool = False) -> None:
        prev = self.current_channel() if keep_selection else None
        channels = self.channels
        fav_urls = self._refresh_fav_snapshot(channels)
        q = self.query.casefold()
        if not q:
            self.visible_idx = list(range(len(channels)))
            labels = [
                format_channel_name(ch, favorite=ch.url in fav_urls)
                for ch in channels
            ]
        else:
            visible: list[int] = []
            labels: list[str] = []
            append_v = visible.append
            append_l = labels.append
            for i, ch in enumerate(channels):
                name_cf = ch.name.casefold()
                grp = ch.group
                if q in name_cf or (grp and q in grp.casefold()):
                    append_v(i)
                    append_l(format_channel_name(ch, favorite=ch.url in fav_urls))
            self.visible_idx = visible
        has_fav = False
        if fav_urls:
            for ch in channels:
                if ch.url in fav_urls:
                    has_fav = True
                    break
        self._has_fav = has_fav
        self.list.set_items(labels, keep_selection=True)
        if prev is not None:
            for pos, i in enumerate(self.visible_idx):
                if self.channels[i] is prev:
                    self.list.selected = pos
                    break
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
        if self.group_filtered:
            self.channels = [c for c in fresh.channels if (c.group or None) == self.group]
        else:
            self.channels = list(fresh.channels)
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
            "↑/↓ · Enter ▶ · / Buscar · g Grupos · "
            "f ★ · e EPG · R Actualizar · r Recientes · p Reproductor · ? Ayuda · t Tema · Esc ←"
        )

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
        if key == ord("R") or key == _KEY_F5:
            # 'r' queda para Recientes (global); 'R'/F5 recarga la fuente
            # porque los canales de una URL pueden cambiar sin aviso.
            return {"action": "reload_playlist"}
        if key == ord("f"):
            channel = self.current_channel()
            if channel is None:
                self.app.status.show("No hay canales que marcar.")
                return None
            return {"action": "toggle_favorite", "channel": channel}
        if key == ord("e"):
            channel = self.current_channel()
            if channel is None:
                self.app.status.show("Selecciona un canal para ver su EPG.")
                return None
            return {
                "action": "show_epg",
                "channel": channel,
                "epg_url": self.playlist.epg_url,
            }
        if key in (curses.KEY_ENTER, 10, 13):
            channel = self.current_channel()
            if channel is None:
                self.app.status.show("La playlist no tiene canales (o el filtro no coincide).")
                return None
            return {"action": "open_channel", "channel": channel}
        return None

    # --- Render -----------------------------------------------------------------

    def _title_text(self) -> str:
        total = len(self.channels)
        shown = len(self.visible_idx)
        suffix = f" · {shown}/{total}" if self.query else f" ({total} canales)"
        grp = ""
        if self.group_filtered:
            grp = f" · [{self.group}]" if self.group else " · [sin grupo]"
        star = " ★" if self._has_fav else ""
        return f"{self.playlist.name}{grp}{suffix}{star}"

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

        # Lista de canales (izquierda)
        if not self.visible_idx and not self.query:
            if show_detail:
                render_empty_message(stdscr, list_start_y + body_h // 2, sidebar_w, "La playlist no tiene canales.")
            else:
                render_empty_message(stdscr, list_start_y + body_h // 2, max_x, "La playlist no tiene canales.")
        elif not self.visible_idx and self.query:
            if show_detail:
                render_empty_message(stdscr, list_start_y + body_h // 2, sidebar_w, f"Sin resultados para '{self.query}'")
            else:
                render_empty_message(stdscr, list_start_y + body_h // 2, max_x, f"Sin resultados para '{self.query}'")
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
    """Canales marcados como favoritos (favorites.json)."""

    title = "Favoritos"

    def __init__(self, app) -> None:
        super().__init__(app)
        self.list = ScrollableList()
        self.channels: list[Channel] = []
        self.reload()

    def reload(self) -> None:
        self.channels = self.app.favorites.load()
        self.list.set_items([format_channel_name(c, favorite=True) for c in self.channels])

    def current_channel(self) -> Channel | None:
        if not self.channels:
            return None
        return self.channels[self.list.selected]

    def shortcuts(self) -> str:
        return "↑/↓ · Enter ▶ · f ★ · p Reproductor · ? Ayuda · t Tema · Esc ←"

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
        if not self.channels:
            render_empty_message(stdscr, max_y // 2, max_x, "Sin favoritos. Marca canales con 'f'.")
        else:
            self.list.render(stdscr, 1, 0, self.app.body_height(), max_x)


class RecentsScreen(Screen):
    title = "Recientes"

    def __init__(self, app) -> None:  # noqa: ANN001
        super().__init__(app)
        self.list = ScrollableList()
        self.items = app.recents.load()
        self.list.set_items([f"{i.name} ({i.player})" for i in self.items])

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
                self.app.status.show("No hay reciente para marcar.")
                return None
            return {"action": "toggle_favorite", "channel": ch}
        if key in (curses.KEY_ENTER, 10, 13):
            ch = self.current_channel()
            if ch is None:
                self.app.status.show("No hay reciente para abrir.")
                return None
            return {"action": "open_channel", "channel": ch}
        return None

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.items:
            render_empty_message(stdscr, max_y // 2, max_x, "Sin recientes. reproduce algo primero.")
        else:
            self.list.render(stdscr, 1, 0, self.app.body_height(), max_x)


class GroupsScreen(Screen):
    """Grupos (group-title) de una playlist — vista cards.

    '/' abre el modal de búsqueda: filtra mientras se escribe; Esc limpia,
    Enter confirma la consulta y vuelve a navegación normal.
    """

    title = "Grupos"

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
        border_attr = colors.pair(colors.PAIR_PRIMARY if selected else colors.PAIR_BORDER)
        text_attr = colors.pair(colors.PAIR_SELECTED if selected else colors.PAIR_NORMAL)
        accent_attr = colors.pair(colors.PAIR_ACCENT)

        card_h = 4
        if card_y + card_h > max_y - 2:
            return

        # Fondo sólido para selected
        if selected:
            try:
                for r in range(card_h):
                    stdscr.addstr(card_y + r, card_x, " " * card_w, colors.pair(colors.PAIR_SELECTED))
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
            from .widgets import EmptyState
            EmptyState.render(stdscr, max_y // 2, max_x,
                              "No hay grupos en esta playlist.", "")
            return

        if not self.visible_keys and self.query:
            msg = f"Sin resultados para '{self.query}'"
            mx = max(0, (max_x - len(msg)) // 2)
            try:
                stdscr.addstr(max_y // 2, mx, msg[:max(0, max_x - 1)],
                              colors.pair(colors.PAIR_EMPTY))
            except curses.error:
                pass
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
        self.selected: int = 0

    def shortcuts(self) -> str:
        return "←/→ · Enter ▶ · ? Ayuda · t Tema · Esc ←"

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

        # Pills de resolución con borde redondeado
        pills: list[str] = []
        pill_widths: list[int] = []
        for i, v in enumerate(self.variants):
            res = resolutions.detect(v) or "Auto"
            pill = f" {res} "
            pills.append(pill)
            pill_widths.append(len(pill))

        total_w = sum(pill_widths) + 2 * (len(pills) - 1)
        pill_x = max(0, (max_x - total_w) // 2)
        pill_y = max(2, (max_y - 3) // 2)

        try:
            x = pill_x
            for i, pill in enumerate(pills):
                pw = pill_widths[i]
                if i == self.selected:
                    # Selected: fondo sólido + BOLD + REVERSE sutil
                    fill = " " * pw
                    stdscr.addstr(pill_y, x, fill, colors.pair(colors.PAIR_SELECTED))
                    stdscr.addstr(pill_y, x, pill, colors.pair(colors.PAIR_SELECTED) | curses.A_BOLD | curses.A_REVERSE)
                else:
                    stdscr.addstr(pill_y, x, pill, colors.pair(colors.PAIR_DIM))
                x += pw + 2
        except curses.error:
            pass

        sel = self.variants[self.selected]
        name = sel.name
        try:
            nx = max(0, (max_x - len(name)) // 2)
            stdscr.addstr(pill_y + 2, nx, name[:max(0, max_x - 1)], colors.pair(colors.PAIR_NORMAL))
        except curses.error:
            pass


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

    def shortcuts(self) -> str:
        return "↑/↓ · Enter ▶ · ? Ayuda · t Tema · Esc ←"

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
            from .widgets import EmptyState
            EmptyState.render(stdscr, max_y // 2, max_x,
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
    """

    def __init__(  # noqa: ANN001
        self,
        app,
        channel: Channel,
        player_name: str,
        proc,  # noqa: ANN001 - Popen sin tipar para evitar import circular
        health_monitor: ChannelHealthMonitor | None = None,
    ) -> None:
        super().__init__(app)
        self.channel = channel
        self.player_name = player_name
        self.proc = proc
        self.player_path: str = config.find_player(player_name) or "?"
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
        # Salud del canal en tiempo real (hilo daemon no bloqueante)
        if health_monitor is not None:
            self.health = health_monitor
        else:
            try:
                self.health = ChannelHealthMonitor(channel, interval=3.0, timeout=3.0, auto_start=True)
            except Exception:
                # degradar silencioso si no se puede crear el monitor
                self.health = ChannelHealthMonitor(channel, interval=10.0, timeout=2.0, auto_start=False)

    def shortcuts(self) -> str:
        return "q Detener · ? Ayuda"

    def is_alive(self) -> bool:
        try:
            return self.proc.poll() is None
        except Exception:
            return False

    def exit_summary(self) -> str:
        """Mensaje de cierre según cómo murió el reproductor.

        Si murió al instante con código != 0, explica la causa probable
        (stream que no abrió: proveedor/URL) en vez del "finalizado"
        genérico que ocultaba el problema.
        """
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
        return None

    def _current_program(self):  # type: ignore[no-untyped-def]
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
        return "Reproduciendo" if alive else "Detenido"

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
        bar_label = "En vivo" if alive else "Parado"
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
        # Si hay programa EPG en curso, añadir % del programa y minutos restantes en misma línea si cabe
        prog = self._current_program()
        extra_prog = ""
        if prog is not None and prog.stop is not None:
            try:
                now2 = datetime.now().astimezone()
                total2 = (prog.stop - prog.start).total_seconds()
                if total2 > 0:
                    frac2 = max(0.0, min(1.0, (now2 - prog.start).total_seconds() / total2))
                    rem2 = int(total2 - (now2 - prog.start).total_seconds())
                    if rem2 < 0:
                        rem2 = 0
                    pct2 = int(frac2 * 100)
                    rem_human = self._fmt_human(rem2)
                    extra_prog = f" · {pct2}% del programa, quedan {rem_human}"
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
                health_icon = icons.ICON_OK if hstat.level >= 4 else icons.ICON_WARN if hstat.level >= 2 else icons.ICON_ERR if hstat.level == 1 else icons.ICON_LIVE_OFF
                hl_attr = colors.pair(hstat.color_pair)
                # Frase ultra simple: "Señal: [●●●●○] Buena"
                lines.append((f" Señal: [{hstat.bar}] {hstat.label} {health_icon}", hl_attr))
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
            # La mostramos siempre, es útil y familiar (es progreso del programa, no del medidor)
            if prog.stop:
                try:
                    now = datetime.now().astimezone()
                    total = (prog.stop - prog.start).total_seconds()
                    if total > 0:
                        frac = max(0.0, min(1.0, (now - prog.start).total_seconds() / total))
                        bar_w2 = max(8, min(24, max_x - 14))
                        filled = int(bar_w2 * frac)
                        bar2 = "━" * filled + "●" + "─" * max(0, bar_w2 - filled - 1)
                        lines.append((f"    {bar2} {int(frac*100)}%", colors.pair(colors.PAIR_CURRENT) | curses.A_DIM))
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




class EpgScreen(Screen):
    """Parrilla de programas del canal seleccionado."""

    def __init__(self, app, channel: Channel, epg_url: str | None = None) -> None:
        super().__init__(app)
        self.channel = channel
        self.epg_url = epg_url
        self.title = f"EPG · {channel.name}"
        self._list = ScrollableList()
        self.programs: list = []
        self.refresh_programs()

    def refresh_programs(self) -> None:
        self.programs = self.app.ensure_epg(self.channel, url_hint=self.epg_url)
        self._list.set_items(self._rows())

    def shortcuts(self) -> str:
        return "↑/↓ · Enter ▶ · r Recargar · ? Ayuda · t Tema · Esc ←"

    def _rows(self) -> list[str]:
        if not self.programs:
            return ["(sin datos de EPG para este canal)"]
        now = datetime.now().astimezone()
        rows: list[str] = []
        for prog in self.programs:
            start = prog.start.astimezone().strftime("%H:%M")
            stop = prog.stop.astimezone().strftime("%H:%M") if prog.stop else "--:--"
            is_current = prog.start <= now and (prog.stop is None or now < prog.stop)
            marker = "●" if is_current else "○"
            label = f"{prog.title}: {prog.sub_title}" if prog.sub_title else prog.title
            desc = f" — {prog.desc}" if prog.desc else ""
            cats = f" ({', '.join(prog.categories)})" if prog.categories else ""
            rows.append(f" {marker} {start}-{stop}  {label}{desc}{cats}")
        return rows

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
            return {"action": "open_channel", "channel": self.channel}
        return None

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

    def render(self, stdscr: curses.window) -> None:
        max_y, max_x = stdscr.getmaxyx()
        if not self.programs:
            render_empty_message(stdscr, max_y // 2, max_x, "Sin datos de EPG para este canal.")
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
                if idx < len(self.programs):
                    prog = self.programs[idx]
                    is_current = prog.start <= now and (prog.stop is None or now < prog.stop)
                if idx == self._list.selected:
                    prefix = " ▸ "
                    if is_current:
                        attr = colors.pair(colors.PAIR_CURRENT) | curses.A_BOLD | curses.A_REVERSE
                    else:
                        attr = colors.pair(colors.PAIR_SELECTED) | curses.A_BOLD
                else:
                    prefix = "   "
                    if is_current:
                        attr = colors.pair(colors.PAIR_CURRENT) | curses.A_BOLD
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
    """
    if entry.kind == "xtream":
        _open_xtream_playlist(app, entry)
        return

    from .app import load_playlist_source

    source = entry.source.strip()
    try:
        playlist = load_playlist_source(source)
    except (OSError, ValueError) as exc:
        app.status.show(str(exc), error=True)
        return
    app.playlist_cache[source] = playlist
    if not playlist.channels:
        app.status.show(f"'{entry.name}' no contiene canales.", error=True)
        return
    app.push(ChannelsScreen(app, playlist))


def _open_xtream_playlist(app, entry: PlaylistEntry) -> None:
    """Abre una fuente Xtream: auth + carga de canales + normalización."""
    # Obtener credenciales (password en memoria)
    creds = app.playlists.get_credentials(entry.name)
    if creds is None:
        # Pedir password con modal centrado (campo secreto).
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

    cfg = XtreamConfig(server_url=server_url, username=username, password=password)
    app.show_loading("Conectando…", sub=server_url)
    try:
        authenticate(cfg)
    except Exception as exc:
        from thetvview.xtream_errors import friendly_message
        app.status.show(
            f"No se pudo abrir '{entry.name}': {friendly_message(exc)}",
            error=True,
        )
        return

    # Cargar categorías y canales
    try:
        categories = get_live_categories(cfg)
        streams = get_live_streams(cfg)
    except Exception as exc:
        from thetvview.xtream_errors import friendly_message
        app.status.show(
            f"No se pudo cargar '{entry.name}': {friendly_message(exc)}",
            error=True,
        )
        return

    cat_map = build_category_map(categories)

    # Normalizar a Channel
    channels = []
    for s in streams:
        ch = normalize_live_stream(s, server_url, username, password)
        cat_name = cat_map.get(str(s.category_id))
        if cat_name:
            ch.group = cat_name
        channels.append(ch)

    if not channels:
        app.status.show(f"'{entry.name}' no contiene canales.", error=True)
        return

    playlist = Playlist(
        name=entry.name,
        channels=channels,
        source=entry.source,
    )
    source_key = entry.source.strip()
    app.playlist_cache[source_key] = playlist
    app.push(ChannelsScreen(app, playlist))


def play_channel(app, channel, player_name: str | None = None) -> None:  # noqa: ANN001
    try:
        proc = player.launch(channel, player_name=player_name)
    except player.PlayerError as exc:
        app.status.show(str(exc), error=True)
        return
    # Push de pantalla informativa; auto-pop cuando el proceso termina (ver App.run).
    # Si player_name venía None (no debería desde PlayerScreen), inferirlo del binario.
    effective_name = player_name or "mpv"
    try:
        # Intentar deducir nombre real si launch eligió el primero disponible
        if player_name is None:
            for cand in config.SUPPORTED_PLAYERS:
                if config.find_player(cand):
                    effective_name = cand
                    break
    except Exception:
        pass
    app.push(NowPlayingScreen(app, channel, effective_name, proc))
    app.prefs.set_last_player(effective_name)
    app.status.show(f"Reproduciendo '{channel.name}' con {effective_name.upper()} (pid {proc.pid}).")
