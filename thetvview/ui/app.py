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
from thetvview.prefs import PrefsManager
from thetvview.recents import RecentsManager

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
    RecentsScreen,
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


# --- Ayuda -------------------------------------------------------------------
# Estilos de línea usados por build_help_lines(): "section" (encabezado),
# "key" (formato "TECLA  explicación", la tecla se resalta en negrita),
# "body" (texto normal) y "tip" (consejo en tenue).

# Dónde está el usuario y qué hacer allí: nombre legible + frase guía.
_HELP_WHERE: dict[str, tuple[str, str]] = {
    "PlaylistsScreen": (
        "Listas (playlists)",
        "Aquí están tus listas de canales. Entra en una con Enter.",
    ),
    "ChannelsScreen": (
        "Canales",
        "Aquí están los canales de la lista. Elige uno con Enter.",
    ),
    "GroupsScreen": (
        "Grupos",
        "Aquí los canales están agrupados por categoría. Entra con Enter.",
    ),
    "FavoritesScreen": (
        "Favoritos",
        "Aquí están los canales que marcaste con f. Enter para verlos.",
    ),
    "RecentsScreen": (
        "Recientes",
        "Aquí está lo último que reproduciste. Enter para volver a verlo.",
    ),
    "EpgScreen": (
        "Guía (EPG)",
        "Aquí ves la programación del canal elegido.",
    ),
    "ResolutionScreen": (
        "Calidad",
        "Este canal tiene varias calidades. Elige una con Enter.",
    ),
    "PlayerScreen": (
        "Reproductor",
        "Elige con qué programa externo ver el canal y pulsa Enter.",
    ),
    "NowPlayingScreen": (
        "Reproduciendo",
        "El canal se está viendo en el reproductor externo.",
    ),
}

# Qué se puede hacer en la pantalla actual, explicado con frases completas.
_HELP_HERE: dict[str, list[tuple[str, str]]] = {
    "PlaylistsScreen": [
        ("key", "Enter  abrir la lista seleccionada y ver sus canales."),
        ("key", "a  añadir una lista nueva (te pide nombre y ruta o URL)."),
        ("key", "d  borrar la lista seleccionada (pide confirmar)."),
        ("key", "R  actualizar el catálogo (releer playlists.json)."),
        ("key", "u  deshacer el último borrado."),
        ("key", "f  ver tus canales favoritos."),
        ("tip", "Consejo: una lista es un fichero o una dirección URL .m3u."),
    ],
    "ChannelsScreen": [
        ("key", "Enter  ver el canal seleccionado."),
        ("key", "/  buscar: escribe y la lista se filtra sola."),
        ("key", "g  ver los grupos (categorías) de esta lista."),
        ("key", "f  guardar o quitar el canal de favoritos (★)."),
        ("key", "e  ver la guía de programación (EPG) del canal."),
        ("key", "R  actualizar la lista (los canales pueden cambiar)."),
        ("key", "p  elegir con qué reproductor ver el canal."),
        ("tip", "En búsqueda: Enter confirma, Esc borra la búsqueda."),
    ],
    "GroupsScreen": [
        ("key", "Enter  abrir el grupo y ver sus canales."),
        ("key", "/  buscar un grupo escribiendo su nombre."),
        ("key", "R  actualizar la lista desde su origen."),
        ("tip", "En búsqueda: Enter confirma, Esc borra la búsqueda."),
    ],
    "FavoritesScreen": [
        ("key", "Enter  ver el canal favorito seleccionado."),
        ("key", "f  quitar el canal de favoritos."),
        ("key", "p  elegir con qué reproductor ver el canal."),
    ],
    "RecentsScreen": [
        ("key", "Enter  volver a ver el elemento seleccionado."),
        ("key", "f  guardar el elemento en favoritos."),
        ("key", "r  borrar todo el historial de recientes."),
    ],
    "EpgScreen": [
        ("key", "Enter  ver el canal de esta guía."),
        ("key", "r  volver a cargar la guía (por si está desactualizada)."),
        ("tip", "● marca el programa que están emitiendo ahora."),
    ],
    "ResolutionScreen": [
        ("key", "← / →  elegir calidad (p. ej. 1080p, 720p, Auto)."),
        ("key", "Enter  continuar con la calidad elegida."),
    ],
    "PlayerScreen": [
        ("key", "↑ / ↓  elegir reproductor (mpv, mplayer o vlc)."),
        ("key", "Enter  ver el canal con el reproductor elegido."),
    ],
    "NowPlayingScreen": [
        ("key", "q  detener la reproducción y volver a la lista."),
        ("tip", "El vídeo se ve en otra ventana; aquí ves el estado."),
    ],
}


def build_help_lines(screen) -> list[tuple[str, str]]:
    """Contenido de la ayuda como lista de (estilo, texto).

    Función pura (no toca curses): así se puede probar con unittest.
    `screen` puede ser la pantalla actual o None.
    """
    cls_name = type(screen).__name__ if screen is not None else ""
    title = getattr(screen, "title", "") if screen is not None else ""
    where, guide = _HELP_WHERE.get(cls_name, ("la app", ""))
    lines: list[tuple[str, str]] = [
        ("section", f"Estás en: {where}"),
    ]
    if title:
        lines.append(("body", f'"{title}"'))
    if guide:
        lines.append(("body", guide))
    lines += [
        ("blank", ""),
        ("section", "Lo esencial (empieza aquí)"),
        ("key", "Flechas  moverte por la lista."),
        ("key", "Enter  abrir lo seleccionado."),
        ("body", "El camino habitual es: Lista → Canales → Enter para ver."),
        ("key", "Esc  volver a la pantalla anterior."),
        ("key", "?  abrir o cerrar esta ayuda."),
        ("key", "q  salir de la app."),
        ("blank", ""),
        ("section", "Teclas que valen en todas partes"),
        ("key", "t  cambiar entre tema claro y oscuro."),
        ("key", "r  ver lo último reproducido (Recientes)."),
        ("key", "p  elegir reproductor para el canal actual."),
        ("key", "u  deshacer el último borrado de lista."),
        ("key", "Ratón  clic para elegir, doble clic para abrir."),
        ("blank", ""),
        ("section", f"En esta pantalla: {where}"),
    ]
    lines.extend(_HELP_HERE.get(cls_name, [
        ("body", "Usa las flechas y Enter; Esc para volver."),
    ]))
    lines += [
        ("blank", ""),
        ("section", "Las demás pantallas (referencia rápida)"),
        ("body", "Listas: a añadir · d borrar · Enter abrir · f favoritos."),
        ("body", "Canales: / buscar · R actualizar · g grupos · Enter ver."),
        ("body", "Grupos: / buscar · R actualizar · Enter abrir."),
        ("body", "Favoritos: f quitar · Enter ver."),
        ("body", "Recientes: r borrar historial · Enter volver a ver."),
        ("body", "Guía: r recargar · Enter ver el canal."),
        ("body", "Calidad / Reproductor: flechas y Enter para confirmar."),
        ("body", "Reproduciendo: q detiene y vuelve a la lista."),
        ("blank", ""),
        ("section", "Cómo buscar (Canales y Grupos)"),
        ("body", "1. Pulsa / y escribe: la lista se filtra sola."),
        ("body", "2. Pulsa Enter para quedarte con el filtro."),
        ("body", "3. Pulsa Esc para borrar la búsqueda."),
        ("blank", ""),
        ("section", "Si algo no funciona"),
        ("tip", "Sin reproductor: instala mpv, vlc o mplayer."),
        ("tip", "Sin guía: pulsa e y escribe la ruta o URL del XMLTV."),
        ("tip", "Lista vacía: revisa que el fichero .m3u tenga canales."),
        ("tip", "Ventana pequeña: agrándala (mínimo 40 × 10)."),
        ("blank", ""),
        ("tip", "Para cerrar esta ayuda: pulsa Esc, q o ?."),
    ]
    return lines


def build_help_text(screen) -> str:
    """Versión en texto plano de la ayuda (para tests y fallbacks)."""
    out: list[str] = []
    for style, text in build_help_lines(screen):
        if style == "blank":
            out.append("")
        elif style == "section":
            out.append(f"[{text}]")
        else:
            out.append(text)
    return "\n".join(out)


class App:
    """Estado global + stack de pantallas + bucle de eventos."""

    def __init__(self, stdscr: curses.window) -> None:
        self.stdscr = stdscr
        self.status = StatusBar()
        self.header = HeaderBar(app=self)
        self.footer = FooterBar()
        self.playlists = PlaylistManager(config.PLAYLISTS_JSON)
        self.favorites = FavoritesManager(config.FAVORITES_JSON)
        self.prefs = PrefsManager()
        self.recents = RecentsManager()
        self.epg: Epg | None = None
        self.epg_source: str | None = None
        self.theme_name: str = self.prefs.load().theme or config.load_theme()
        self.playlist_cache: dict[str, Playlist] = {}
        self.stack: list[PlaylistsScreen] = []
        self._undo_stack: list[dict] = []
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
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        p = self.prefs.load()
        p.theme = self.theme_name
        self.prefs.save(p)
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
        entry = self.playlists.get(name)
        if entry is None:
            self.status.show(f"'{name}' no existe.", error=True)
            return
        self._undo_stack.clear()
        self._undo_stack.append({"action": "restore_playlist", "entry": {"name": entry.name, "source": entry.source}})
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

    def show_loading(self, message: str = "Cargando…", sub: str = "") -> None:
        """Dibuja la pantalla de 'Cargando' y la muestra ya (refresh).

        Se llama justo antes de una operación bloqueante (descargar
        una playlist remota) para que la espera no sea una pantalla
        congelada. Nunca lanza: todo está protegido contra
        curses.error y resize.
        """
        from .widgets import LoadingOverlay

        stdscr = self.stdscr
        try:
            stdscr.erase()
            try:
                self.header.render(stdscr, getattr(self.screen, "title", ""), len(self.stack))
            except Exception:
                pass
            LoadingOverlay.render(stdscr, message, sub=sub)
            try:
                self._render_footer(stdscr)
            except Exception:
                pass
            stdscr.refresh()
        except curses.error:
            pass

    def reload_current_playlist(self) -> None:
        """Re-parsea la fuente de la lista actual (Canales/Grupos).

        Muestra la pantalla de 'Cargando', reintenta con timeout
        amigable (vía load_playlist_source) y actualiza todas las
        pantallas del stack que compartan la misma fuente para que
        Grupos y Canales filtrados queden en sync.
        """
        scr = self.screen
        playlist = getattr(scr, "playlist", None)
        if playlist is None:
            msg = "Nada que actualizar aquí."
            try:
                self.status.show(msg)
            except Exception:
                pass
            try:
                self.footer.show(msg)
            except Exception:
                pass
            return
        source = (getattr(playlist, "source", None) or "").strip()
        if not source:
            msg = "Esta lista no tiene origen recargable."
            self.status.show(msg, error=True)
            return
        name = getattr(playlist, "name", "lista") or "lista"
        self.show_loading(f"Actualizando '{name}'…", sub=source)
        try:
            fresh = load_playlist_source(source)
        except (OSError, ValueError) as exc:
            self.status.show(str(exc), error=True)
            return
        old_n = len(playlist.channels)
        new_n = len(fresh.channels)
        self.playlist_cache[source] = fresh
        for s in self.stack:
            pl = getattr(s, "playlist", None)
            if pl is None:
                continue
            if (getattr(pl, "source", None) or "").strip() != source:
                continue
            refresher = getattr(s, "refresh_from_playlist", None)
            if callable(refresher):
                try:
                    refresher(fresh)
                except Exception:
                    pass
        diff = new_n - old_n
        if diff == 0:
            msg = f"Lista actualizada: {new_n} canales (sin cambios)."
        elif diff > 0:
            msg = f"Lista actualizada: {new_n} canales (+{diff})."
        else:
            msg = f"Lista actualizada: {new_n} canales ({diff})."
        self.status.show(msg)
        try:
            self.footer.show(msg)
        except Exception:
            pass

    def handle_action(self, action: dict) -> None:
        match action.get("action"):
            case "add_playlist":
                self.add_playlist()
            case "remove_playlist":
                self.remove_playlist(action["name"])
            case "confirm_remove_playlist":
                self._confirm_remove_playlist(action["name"])
            case "restore_playlist":
                entry = action.get("entry")
                if entry is None:
                    return
                try:
                    self.playlists.add(entry["name"], entry["source"])
                except PlaylistError as exc:
                    self.status.show(str(exc), error=True)
                    return
                self.screen.reload()
                self.status.show(f"Playlist '{entry['name']}' restaurada.")
            case "open_playlist":
                open_playlist(self, action["entry"])
            case "show_favorites":
                self.push(FavoritesScreen(self))
            case "show_recents":
                self.push(RecentsScreen(self))
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
            case "reload_playlist":
                self.reload_current_playlist()
            case "show_resolutions":
                self.push(ResolutionScreen(self, action["channel"], action["variants"]))
            case "open_channel":
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
            case "force_select_player":
                self.push(PlayerScreen(self, action["channel"]))
            case "play_with":
                play_channel(self, action["channel"], player_name=action["player_name"])
            case "stop_playback":
                cur = self.screen
                if isinstance(cur, NowPlayingScreen):
                    try:
                        if cur.is_alive():
                            cur.proc.terminate()
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
                if key == ord("r"):
                    # 'r' en la guía recarga el EPG (ver EpgScreen);
                    # en el resto abre Recientes. 'R'/F5 recarga la lista.
                    if isinstance(self.screen, EpgScreen):
                        pass  # deja que EpgScreen.handle_key lo gestione
                    else:
                        self.push(RecentsScreen(self))
                        continue
                if key == ord("p"):
                    ch = None
                    scr = self.screen
                    if hasattr(scr, "current_channel"):
                        ch = scr.current_channel()
                    if ch is not None:
                        self.handle_action({"action": "force_select_player", "channel": ch})
                    else:
                        self.footer.show("Selecciona un canal para elegir reproductor.")
                    continue
                if key == ord("u"):
                    if self._undo_stack:
                        last = self._undo_stack.pop()
                        self.handle_action(last)
                    else:
                        self.footer.show("Nada que deshacer.")
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
        """Ayuda contextual con scroll: qué hacer aquí, paso a paso."""
        from . import icons

        lines = build_help_lines(self.screen)
        offset = 0
        stdscr = self.stdscr

        def render() -> tuple[int, int, int]:
            """Dibuja fondo + ventana de ayuda. Devuelve (visibles, total, alto)."""
            max_y, max_x = stdscr.getmaxyx()
            stdscr.erase()
            self.header.render(stdscr, self.screen.title, len(self.stack))
            self.screen.render(stdscr)
            self._render_footer(stdscr)

            box_w = max(20, min(max_x - 4, 74))
            box_h = max(8, min(max_y - 2, len(lines) + 6))
            by = max(0, (max_y - box_h) // 2)
            bx = max(0, (max_x - box_w) // 2)
            inner_w = max(1, box_w - 4)
            inner_h = max(1, box_h - 4)  # título + ayuda + pie
            total = len(lines)

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
            title = " Ayuda "
            try:
                stdscr.addstr(by, bx + max(0, (box_w - len(title)) // 2), title,
                              colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
            except curses.error:
                pass

            # Cuerpo con scroll: solo se ven inner_h líneas desde offset.
            for i in range(inner_h):
                idx = offset + i
                row = by + 2 + i
                if row >= by + box_h - 2:
                    break
                # Limpiar la fila antes de escribir.
                try:
                    stdscr.addstr(row, bx + 2, " " * inner_w,
                                  colors.pair(colors.PAIR_NORMAL))
                except curses.error:
                    pass
                if idx >= total:
                    continue
                style, text = lines[idx]
                try:
                    if style == "section":
                        stdscr.addstr(row, bx + 2, text[:inner_w],
                                      colors.pair(colors.PAIR_PRIMARY) | curses.A_BOLD)
                    elif style == "key":
                        # "TECLA  explicación": la tecla en negrita.
                        parts = text.split("  ", 1)
                        if len(parts) == 2:
                            key_part, rest = parts
                            stdscr.addstr(row, bx + 2, key_part[:inner_w],
                                          colors.pair(colors.PAIR_ACCENT) | curses.A_BOLD)
                            rest_x = bx + 2 + len(key_part) + 2
                            if rest_x < bx + 2 + inner_w:
                                stdscr.addstr(row, rest_x, rest[: bx + 2 + inner_w - rest_x],
                                              colors.pair(colors.PAIR_NORMAL))
                        else:
                            stdscr.addstr(row, bx + 2, text[:inner_w],
                                          colors.pair(colors.PAIR_NORMAL))
                    elif style == "tip":
                        stdscr.addstr(row, bx + 2, text[:inner_w],
                                      colors.pair(colors.PAIR_DIM))
                    elif style != "blank":
                        stdscr.addstr(row, bx + 2, text[:inner_w],
                                      colors.pair(colors.PAIR_NORMAL))
                except curses.error:
                    pass

            # Pie: pista de scroll + cómo cerrar.
            if total > inner_h:
                pos = f" {min(offset + inner_h, total)}/{total} "
                try:
                    stdscr.addstr(by + box_h - 2, bx + box_w - len(pos) - 2, pos,
                                  colors.pair(colors.PAIR_DIM) | curses.A_DIM)
                except curses.error:
                    pass
                hint = "↑/↓ leer"
            else:
                hint = "Ayuda completa"
            close = "Esc/q/? cerrar"
            foot = f"{hint} · {close}"
            try:
                stdscr.addstr(by + box_h - 2, bx + 2, foot[:inner_w],
                              colors.pair(colors.PAIR_DIM))
            except curses.error:
                pass
            stdscr.refresh()
            return inner_h, total, box_h

        inner_h, total, _ = render()
        while True:
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                offset = max(0, min(offset, max(0, len(lines) - inner_h)))
                inner_h, total, _ = render()
                continue
            if key in (27, ord("q"), ord("Q"), ord("?"),
                       curses.KEY_ENTER, 10, 13):
                break
            if key in (curses.KEY_UP, ord("k"), ord("K")):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
                offset = max(0, min(max(0, total - inner_h), offset + 1))
            elif key in (curses.KEY_PPAGE,):
                offset = max(0, offset - max(1, inner_h - 1))
            elif key in (curses.KEY_NPAGE,):
                offset = max(0, min(max(0, total - inner_h), offset + max(1, inner_h - 1)))
            elif key in (curses.KEY_HOME,):
                offset = 0
            elif key in (curses.KEY_END,):
                offset = max(0, total - inner_h)
            else:
                continue
            inner_h, total, _ = render()


def main() -> None:
    """Punto de entrada de la TUI; garantiza restaurar la terminal."""
    # Configurar locale para Unicode (box-drawing, fracciones)
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    config.ensure_dirs()
    curses.wrapper(lambda stdscr: App(stdscr).run())
