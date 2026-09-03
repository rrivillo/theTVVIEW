"""Núcleo de la TUI: bucle principal, stack de pantallas y entrada de texto.

Nunca dejar la terminal rota: la app se ejecuta siempre vía curses.wrapper
(ver main()). KEY_RESIZE se captura y provoca re-layout sin crash.
"""

from __future__ import annotations

import curses
import locale

from thetvview import config
from thetvview import resolutions
from thetvview.epg_parser import Epg, load_url, parse_file
from thetvview.favorites import FavoritesError, FavoritesManager
from thetvview.models import Channel, Playlist, Program
from thetvview.playlist_manager import PlaylistError, PlaylistManager

from . import colors, theme
from .widgets import FooterBar, HeaderBar, StatusBar
from .screens import (
    ChannelsScreen,
    EpgScreen,
    FavoritesScreen,
    GroupsScreen,
    NowPlayingScreen,
    PlaylistsScreen,
    PlayerScreen,
    ResolutionScreen,
    _epg_channel_id,
    open_playlist,
    play_channel,
)


def load_epg_source(source: str, *, force_refresh: bool = False) -> Epg:
    """Carga un XMLTV desde path local o URL http(s) (cache TTL)."""
    if source.lower().startswith(("http://", "https://")):
        return load_url(source, force_refresh=force_refresh)
    return parse_file(source)


def load_playlist_source(source: str) -> Playlist:
    """Parsea una fuente M3U (path local o URL http(s))."""
    from thetvview import m3u_parser

    if source.lower().startswith(("http://", "https://")):
        return m3u_parser.parse_url(source)
    return m3u_parser.parse_file(source)


def prompt_text(stdscr: curses.window, status: StatusBar, label: str) -> str | None:
    """Input modal simple en una línea reservada. None si se cancela (Esc).

    Usa la fila del footer para no pisar el contenido, trunca según ancho,
    soporta KEY_RESIZE y limita el buffer al ancho visible.
    """
    buffer = ""
    curses.curs_set(1)
    try:
        while True:
            max_y, max_x = stdscr.getmaxyx()
            # Footer ocupa las 2 últimas filas; usamos la última como input.
            row = max(0, max_y - 1)
            # Calcular ancho disponible tras label
            prefix = f" {label} "
            avail = max(4, max_x - len(prefix) - 2)
            # Buffer visible truncado por la izquierda si excede
            visible = buffer[-avail:] if len(buffer) > avail else buffer
            cursor = "▌"
            line = f"{prefix}{visible}{cursor}".ljust(max_x - 1)[: max_x - 1]
            try:
                stdscr.addstr(row, 0, line, colors.pair(colors.PAIR_SEARCH) | curses.A_BOLD)
                # Limpiar separador de arriba para dar sensación de modal
                if max_y >= 3:
                    stdscr.addstr(max_y - 2, 0, "─" * (max_x - 1), colors.pair(colors.PAIR_BORDER))
            except curses.error:
                pass
            stdscr.refresh()
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            if key in (27,):  # Esc
                return None
            if key in (curses.KEY_ENTER, 10, 13):
                return buffer.strip()
            if key in (curses.KEY_BACKSPACE, 127, 8):
                buffer = buffer[:-1]
            elif 32 <= key < 127 or key > 160:
                # Limitar longitud total para no desbordar
                if len(buffer) < 240:
                    buffer += chr(key)
    finally:
        curses.curs_set(0)
        status.show("")


class App:
    """Estado global + stack de pantallas + bucle de eventos."""

    def __init__(self, stdscr: curses.window) -> None:
        self.stdscr = stdscr
        self.status = StatusBar()
        self.header = HeaderBar(app=self)
        self.footer = FooterBar()
        self.playlists = PlaylistManager(config.PLAYLISTS_JSON)
        self.favorites = FavoritesManager(config.FAVORITES_JSON)
        self.epg: Epg | None = None
        self.epg_source: str | None = None
        self.theme_name: str = config.load_theme()
        self.playlist_cache: dict[str, Playlist] = {}
        self.stack: list[PlaylistsScreen] = []
        self.stack.append(PlaylistsScreen(self))

    # --- Stack --------------------------------------------------------------

    @property
    def screen(self):
        return self.stack[-1]

    def push(self, screen) -> None:
        self.stack.append(screen)

    def pop(self) -> bool:
        if len(self.stack) <= 1:
            return False
        top = self.stack[-1]
        try:
            if isinstance(top, NowPlayingScreen):
                top.stop_health()
        except Exception:
            pass
        self.stack.pop()
        return True

    # --- Layout ---------------------------------------------------------------

    def body_height(self) -> int:
        from .layout import main_rect, content_rect
        max_y, max_x = self.stdscr.getmaxyx()
        # Si la pantalla actual necesita search bar, descontar una fila
        has_search = bool(getattr(self.screen, "searching", False) or getattr(self.screen, "query", ""))
        if has_search and hasattr(self.screen, "visible_keys"):
            return content_rect(max_y, max_x, has_search=True).h
        if has_search and hasattr(self.screen, "visible_idx"):
            return content_rect(max_y, max_x, has_search=True).h
        return main_rect(max_y, max_x).h

    # --- Resoluciones ---------------------------------------------------------

    def variants_for(self, channel: Channel) -> list[Channel]:
        """Variantes de resolución del canal buscando en todas las playlists.

        Devuelve variantes swappable (el canal objetivo puede o no tener
        etiqueta de resolución; se buscan entradas con distinta resolución).

        Orden de búsqueda:
        1. Playlists ya abiertas/cacheadas en esta sesión.
        2. Resto de fuentes del catálogo (se parsean una vez y se cachean;
           las que fallen se saltan en silencio).
        """
        base = resolutions.base_name(channel)

        def search(pool_channels: list[Channel]) -> list[Channel]:
            if any(resolutions.base_name(c) == base for c in pool_channels):
                return resolutions.swappable_variants(pool_channels, channel)
            return []

        for playlist in self.playlist_cache.values():
            found = search(playlist.channels)
            if found:
                return found

        for entry in self.playlists.load():
            source = entry.source.strip()
            if source in self.playlist_cache:
                continue
            try:
                playlist = load_playlist_source(source)
            except (OSError, ValueError):
                continue  # fuente caída/inaccesible: no bloquea la búsqueda
            self.playlist_cache[source] = playlist
            found = search(playlist.channels)
            if found:
                return found
        return []

    def toggle_theme(self) -> None:
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        config.save_theme(self.theme_name)
        theme.init_colors(self.theme_name)
        label = "oscuro" if self.theme_name == "dark" else "claro"
        self.footer.show(f"Tema {label} activado.")

    # --- Acciones -----------------------------------------------------------

    def add_playlist(self) -> None:
        name = prompt_text(self.stdscr, self.status, "Nombre:")
        if not name:
            return
        source = prompt_text(self.stdscr, self.status, "Ruta o URL:")
        if not source:
            return
        try:
            self.playlists.add(name, source)
        except PlaylistError as exc:
            self.status.show(str(exc), error=True)
            return
        self.screen.reload()
        self.status.show(f"Playlist '{name}' añadida.")

    def remove_playlist(self, name: str) -> None:
        # Confirmación vía modal centrada para evitar borrados accidentales.
        self._confirm_remove_playlist(name)

    def _confirm_remove_playlist(self, name: str) -> None:
        from .widgets import Modal

        modal = Modal("Confirmar", f"¿Eliminar playlist '{name}'?", ["Cancelar", "Eliminar"])
        modal.selected_button = 0
        stdscr = self.stdscr
        # Render loop del modal
        while True:
            stdscr.erase()
            self.header.render(stdscr, self.screen.title, len(self.stack))
            self.screen.render(stdscr)
            self._render_footer(stdscr)
            modal.render(stdscr)
            stdscr.refresh()
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            res = modal.handle_key(key)
            # Modal devuelve lower de botón o "cancel"
            if res in ("cancelar", "cancel", None):
                if key in (27, ord("q"), curses.KEY_LEFT, curses.KEY_BACKSPACE):
                    if res is not None:
                        self.footer.show("Cancelado.")
                        return
                    # Si fue navegación de botones, continuar
                    if res is None and key in (27,):
                        self.footer.show("Cancelado.")
                        return
                    continue
                if res is None:
                    continue
                self.footer.show("Cancelado.")
                return
            if res == "eliminar":
                break
            if res == "cancelar":
                self.footer.show("Cancelado.")
                return
        try:
            removed = self.playlists.remove(name)
        except PlaylistError as exc:
            self.status.show(str(exc), error=True)
            return
        self.screen.reload()
        if removed:
            self.status.show(f"Playlist '{name}' eliminada.")
        else:
            self.status.show(f"'{name}' no existe.", error=True)

    def toggle_favorite(self, channel: Channel) -> None:
        try:
            added = self.favorites.toggle(channel)
        except FavoritesError as exc:
            self.status.show(str(exc), error=True)
            return
        if isinstance(self.screen, ChannelsScreen):
            self.screen._apply_filter(keep_selection=True)
        verb = "añadido a" if added else "quitado de"
        self.status.show(f"'{channel.name}' {verb} favoritos.")

    def unfavorite(self, channel: Channel) -> None:
        try:
            removed = self.favorites.remove(channel)
        except FavoritesError as exc:
            self.status.show(str(exc), error=True)
            return
        self.screen.reload()
        if removed:
            self.status.show(f"'{channel.name}' quitado de favoritos.")
        else:
            self.status.show(f"'{channel.name}' no estaba en favoritos.", error=True)

    def ensure_epg(
        self,
        channel: Channel,
        *,
        force_refresh: bool = False,
        url_hint: str | None = None,
    ) -> list[Program]:
        """Devuelve los programas del canal, pidiendo el XMLTV una vez.

        El origen puede ser un path local (.xml/.gz) o una URL http(s)
        (descarga con cache TTL; 'r' en la pantalla EPG fuerza re-descarga).
        `url_hint` sugiere el x-tvg-url de la playlist de origen.
        """
        if self.epg is None:
            default = self.epg_source or (url_hint or "").strip()
            label = f"XMLTV (ruta o URL .xml/.gz){' [' + default + ']' if default else ''}:"
            path = prompt_text(self.stdscr, self.status, label)
            if not path and default:
                path = default
            if not path:
                return []
            try:
                self.epg = load_epg_source(path)
            except (OSError, ValueError) as exc:
                self.status.show(str(exc), error=True)
                return []
            self.epg_source = path
            self.status.show(f"EPG cargado ({len(self.epg.programs)} canales).")
        elif force_refresh and self.epg_source:
            try:
                self.epg = load_epg_source(self.epg_source, force_refresh=True)
                self.status.show(f"EPG recargado ({len(self.epg.programs)} canales).")
            except (OSError, ValueError) as exc:
                self.status.show(str(exc), error=True)
        cid = _epg_channel_id(self.epg, channel)
        if cid is None:
            self.status.show(
                f"No hay correspondencia EPG para '{channel.name}'.", error=True
            )
            return []
        return self.epg.programmes_for(cid)

    def handle_action(self, action: dict) -> None:
        match action.get("action"):
            case "add_playlist":
                self.add_playlist()
            case "remove_playlist":
                self.remove_playlist(action["name"])
            case "confirm_remove_playlist":
                self._confirm_remove_playlist(action["name"])
            case "open_playlist":
                open_playlist(self, action["entry"])
            case "show_favorites":
                self.push(FavoritesScreen(self))
            case "toggle_favorite":
                self.toggle_favorite(action["channel"])
            case "unfavorite":
                self.unfavorite(action["channel"])
            case "show_epg":
                self.push(EpgScreen(self, action["channel"], action.get("epg_url")))
            case "show_groups":
                self.push(GroupsScreen(self, action["playlist"]))
            case "open_group":
                self.push(
                    ChannelsScreen(self, action["playlist"], group=action["group"])
                )
            case "show_resolutions":
                self.push(ResolutionScreen(self, action["channel"], action["variants"]))
            case "open_channel":
                # Ruta común: si hay variantes swappable (≥2 resoluciones
                # distintas para el mismo base_name), mostrar ResolutionScreen.
                # Si no hay variantes, ir directo a PlayerScreen.
                channel = action["channel"]
                variants = self.variants_for(channel)
                if variants:
                    self.push(ResolutionScreen(self, channel, variants))
                    return
                screen = PlayerScreen(self, channel)
                if not screen.players:
                    self.status.show(
                        "No hay reproductor disponible. Instala uno de: "
                        + ", ".join(config.SUPPORTED_PLAYERS),
                        error=True,
                    )
                    return
                self.push(screen)
            case "select_player":
                screen = PlayerScreen(self, action["channel"])
                if not screen.players:
                    self.status.show(
                        "No hay reproductor disponible. Instala uno de: "
                        + ", ".join(config.SUPPORTED_PLAYERS),
                        error=True,
                    )
                    return
                self.push(screen)
            case "play_with":
                play_channel(self, action["channel"], player_name=action["player_name"])
            case "stop_playback":
                # Cierre solicitado desde NowPlayingScreen (q/Esc): mata proceso si sigue vivo y vuelve.
                cur = self.screen
                if isinstance(cur, NowPlayingScreen):
                    try:
                        if cur.is_alive():
                            cur.proc.terminate()
                            # Dar un momento al reproductor para salir sin bloquear la UI
                            try:
                                cur.proc.wait(timeout=0.5)
                            except Exception:
                                try:
                                    cur.proc.kill()
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    try:
                        cur.stop_health()
                    except Exception:
                        pass
                    self.pop()
                    self.status.show(f"'{cur.channel.name}' detenido.")
                else:
                    self.pop()

    # --- Bucle ---------------------------------------------------------------

    def run(self) -> None:
        stdscr = self.stdscr
        curses.curs_set(0)
        theme.init_colors(self.theme_name)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except Exception:
            pass
        while True:
            if isinstance(self.screen, NowPlayingScreen):
                stdscr.timeout(500)
            else:
                stdscr.timeout(-1)

            # Verificar tamaño mínimo
            max_y, max_x = stdscr.getmaxyx()
            if max_y < 10 or max_x < 40:
                stdscr.erase()
                msg = "Ventana demasiado pequeña"
                hint = f"({max_y}×{max_x}) — mínimo 10×40"
                try:
                    stdscr.addstr(max_y // 2, max(0, (max_x - len(msg)) // 2),
                                  msg, colors.pair(colors.PAIR_DANGER) | curses.A_BOLD)
                    stdscr.addstr(max_y // 2 + 1, max(0, (max_x - len(hint)) // 2),
                                  hint, colors.pair(colors.PAIR_DIM))
                except curses.error:
                    pass
                stdscr.refresh()
                stdscr.timeout(500)
                stdscr.getch()
                continue

            stdscr.erase()
            self.header.render(stdscr, self.screen.title, len(self.stack))
            self.screen.render(stdscr)
            self._render_footer(stdscr)
            stdscr.refresh()

            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue

            if key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                    # Click simple: mover selección al elemento bajo cursor
                    if bstate & curses.BUTTON1_CLICKED:
                        handled = False
                        try:
                            if hasattr(self.screen, "handle_mouse"):
                                handled = self.screen.handle_mouse(mx, my, self.screen)
                            elif hasattr(self.screen, "list") and hasattr(self.screen.list, "handle_mouse"):
                                # Fallback genérico para listas (no usado ahora)
                                pass
                        except Exception:
                            handled = False
                        if handled:
                            # Refrescar sin consumir como Enter
                            continue
                        continue
                    if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                        key = curses.KEY_ENTER
                    else:
                        continue
                except Exception:
                    continue

            if isinstance(self.screen, NowPlayingScreen) and key == -1:
                try:
                    if not self.screen.is_alive():
                        try:
                            self.screen.stop_health()
                        except Exception:
                            pass
                        ch_name = self.screen.channel.name
                        self.pop()
                        self.footer.show(f"'{ch_name}' finalizado.")
                except Exception:
                    pass
                continue

            # Respetar modo búsqueda: 't'/'?'/'q' no son globales mientras se escribe
            is_searching = bool(getattr(self.screen, "searching", False))
            if not is_searching:
                if key in (ord("q"), ord("Q")):
                    if isinstance(self.screen, NowPlayingScreen):
                        action = self.screen.handle_key(key)
                        if action:
                            self.handle_action(action)
                        continue
                    if not self.pop():
                        return
                    continue
                if key == ord("t"):
                    self.toggle_theme()
                    continue
                if key == ord("?"):
                    self._show_help()
                    continue
                if key in (27, curses.KEY_BACKSPACE):
                    if isinstance(self.screen, NowPlayingScreen):
                        action = self.screen.handle_key(key)
                        if action:
                            self.handle_action(action)
                        continue
                    if not self.pop():
                        self.footer.show("Ya estás en la raíz. 'q' para salir.")
                    else:
                        self.footer.show("")
                    continue
            else:
                # En búsqueda, 'q'/'t'/'?' deben ir al campo de texto (si es imprimible)
                # Esc sí debe salir de búsqueda (manejado por screen.handle_key)
                pass

            action = self.screen.handle_key(key)
            if action:
                self.handle_action(action)

    def _parse_shortcuts_to_chips(self, shortcuts: str) -> list[tuple[str, str]]:
        """Convierte string de atajos 'a Añadir · Enter ▶' a chips [(key, label)]."""
        chips: list[tuple[str, str]] = []
        for part in shortcuts.split("·"):
            part = part.strip()
            if not part:
                continue
            tokens = part.split(None, 1)
            if len(tokens) == 2:
                chips.append((tokens[0], tokens[1]))
            elif len(tokens) == 1:
                chips.append(("", tokens[0]))
        return chips

    def _render_footer(self, stdscr: curses.window) -> None:
        """Renderiza footer moderno con chips + toast stack."""
        raw = self.screen.shortcuts()
        self.footer.chips = self._parse_shortcuts_to_chips(raw)
        msg = self.status._current_message()
        if msg:
            self.footer.show(msg, error=self.status.is_error)
        self.footer.render(stdscr)

    def _show_help(self) -> None:
        """Muestra overlay de ayuda con atajos de teclado (contextual)."""
        from .widgets import Modal
        ctx = self.screen.shortcuts()
        # Ayuda base + contexto actual
        help_text = (
            "Atajos globales:\n"
            " ↑/↓/←/→  Navegar listas/cards\n"
            " Enter    Seleccionar / Reproducir\n"
            " Esc/←    Volver atrás\n"
            " q        Salir (o detener reproducción)\n"
            " t        Tema claro/oscuro\n"
            " ?        Esta ayuda\n"
            " Mouse    Click selecciona · doble-click abre\n"
            "\n"
            f"Contexto actual: {ctx}\n"
            "\n"
            "Playlists: a Añadir · d Borrar · f Favoritos\n"
            "Canales:   / Buscar · g Grupos · f Favorito · e EPG\n"
            "Grupos:    / Buscar · Enter abrir\n"
            "EPG:       r Recargar · Enter reproducir\n"
            "Resolución/Reproductor: ←/→ o ↑/↓ · Enter confirmar\n"
        )
        modal = Modal("Ayuda", help_text, ["Cerrar"])
        stdscr = self.stdscr
        stdscr.erase()
        self.header.render(stdscr, self.screen.title, len(self.stack))
        self.screen.render(stdscr)
        self._render_footer(stdscr)
        modal.render(stdscr)
        stdscr.refresh()
        while True:
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                stdscr.erase()
                self.header.render(stdscr, self.screen.title, len(self.stack))
                self.screen.render(stdscr)
                self._render_footer(stdscr)
                modal.render(stdscr)
                stdscr.refresh()
                continue
            if key in (27, ord("q"), ord("?"), curses.KEY_ENTER, 10, 13):
                break


def main() -> None:
    """Punto de entrada de la TUI; garantiza restaurar la terminal."""
    # Configurar locale para Unicode (box-drawing, fracciones)
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    config.ensure_dirs()
    curses.wrapper(lambda stdscr: App(stdscr).run())
