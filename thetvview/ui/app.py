"""Núcleo de la TUI: bucle principal, stack de pantallas y entrada de texto.

Nunca dejar la terminal rota: la app se ejecuta siempre vía curses.wrapper
(ver main()). KEY_RESIZE se captura y provoca re-layout sin crash.
"""

from __future__ import annotations

import curses
import locale
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from thetvview import catchup
from thetvview import config
from thetvview import resolutions
from thetvview.epg_parser import Epg, load_url, parse_file, resolve_source
from thetvview.favorites import FavoritesError, FavoritesManager
from thetvview.models import Channel, Playlist, Program
from thetvview.platform_check import OS_WINDOWS, detect_os
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

# Segundos mínimos entre reintentos de una fuente del catálogo que falló al
# cargarse en segundo plano (evita martillar servidores caídos en cada canal).
WARM_RETRY_S = 300.0

# Espera máxima (s) a que termine la carga en segundo plano del EPG que trae
# una playlist antes de caer en el prompt manual de "Cargar EPG".
EPG_WAIT_S = 60.0

# Reintento de una fuente EPG declarada en la lista que falló (misma idea
# que WARM_RETRY_S, para no martillar un servidor caído en cada apertura).
EPG_RETRY_S = 300.0


def playlist_epg_sources(playlist: Playlist | None) -> list[str]:
    """Fuentes EPG que declara la lista, resueltas y sin duplicados.

    Lee `epg_urls` (o `epg_url` como fallback) y las normaliza contra el
    origen de la lista: URLs http(s) tal cual, paths locales relativos
    contra el directorio del fichero (o urljoin si la lista es remota).
    """
    if playlist is None:
        return []
    refs = list(getattr(playlist, "epg_urls", None) or [])
    if not refs:
        single = (getattr(playlist, "epg_url", None) or "").strip()
        if single:
            refs = [single]
    base = (getattr(playlist, "source", None) or "").strip() or None
    sources: list[str] = []
    for ref in refs:
        resolved = resolve_source(ref, base=base)
        if resolved and resolved not in sources:
            sources.append(resolved)
    return sources


def load_epg_source(source: str, *, force_refresh: bool = False,
                    allow_private: bool = False) -> Epg:
    """Carga un XMLTV desde path local o URL http(s) (cache TTL).

    ``allow_private`` es la excepción anti-SSRF declarada para esa fuente
    (SDD §11); por defecto la red privada está bloqueada.
    """
    if source.lower().startswith(("http://", "https://")):
        return load_url(source, force_refresh=force_refresh, allow_private=allow_private)
    return parse_file(source)


def load_playlist_source(source: str, *, force_refresh: bool = False,
                         allow_private: bool = False) -> Playlist:
    """Parsea una fuente IPTV (path local o URL http(s)).

    Acepta .m3u/.m3u8 y también .ts como lista válida (un solo stream),
    sin pedir nada extra al usuario.

    Las URLs remotas usan cache en disco con TTL (6h): la primera vez
    descarga (hasta 30s de timeout para paneles lentos tipo get.php),
    las siguientes abren instantáneo desde cache. Si el servidor falla
    pero hay cache previa, se devuelve la cache en vez de romper.
    Con `force_refresh=True` se re-descarga siempre (botón Recargar).

    ``allow_private`` habilita red privada/loopback solo si la fuente lo
    tiene declarado en el catálogo (SDD §11).
    """
    from thetvview import m3u_parser

    if source.lower().startswith(("http://", "https://")):
        return m3u_parser.load_url(
            source, force_refresh=force_refresh, allow_private=allow_private
        )
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


def prompt_password(stdscr: curses.window, status: StatusBar, label: str) -> str | None:
    """Input modal para password sin eco. None si se cancela (Esc)."""
    buffer = ""
    curses.curs_set(1)
    try:
        while True:
            max_y, max_x = stdscr.getmaxyx()
            row = max(0, max_y - 1)
            prefix = f" {label} "
            avail = max(4, max_x - len(prefix) - 2)
            # Mostrar asteriscos en lugar de caracteres
            visible = "*" * len(buffer[-avail:]) if len(buffer) > avail else "*" * len(buffer)
            cursor = "▌"
            line = f"{prefix}{visible}{cursor}".ljust(max_x - 1)[: max_x - 1]
            try:
                stdscr.addstr(row, 0, line, colors.pair(colors.PAIR_SEARCH) | curses.A_BOLD)
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
                return buffer
            if key in (curses.KEY_BACKSPACE, 127, 8):
                buffer = buffer[:-1]
            elif 32 <= key < 127 or key > 160:
                if len(buffer) < 240:
                    buffer += chr(key)
    finally:
        curses.curs_set(0)
        status.show("")


# --- Espera del sondeo de pistas ---------------------------------------------
#
# Cuánto se espera, como mucho, a que vuelva el manifiesto **antes** de
# ofrecer audio/subtítulos/calidad. Es lo que hace que el orden pedido
# (pistas → reproductor) funcione de verdad: sin esta espera el usuario
# llega al selector de reproductor antes de que el proveedor haya
# contestado y las opciones nunca aparecen.
#
# Tres garantías para que esperar aquí no sea una espera mala:
#   1. sólo se espera si el canal puede tener manifiesto (`.ts` no entra);
#   2. hay pantalla de espera con lo que está haciendo, no un cuelgue mudo;
#   3. al agotarse el tiempo se sigue el camino de siempre y el sondeo
#      sigue en segundo plano, así que las pistas llegan después: se ofrecen
#      la próxima vez que se elige reproductor (`p`), nunca con el canal ya
#      abierto, porque una pista fija sólo se aplica al lanzar.
#
# El tope **depende del sistema** porque el tiempo de respuesta del primer
# sondeo no es el mismo en todos: en Windows la primera petición paga el
# resolver de DNS del sistema (que reintenta y prueba IPv6), el handshake de
# TLS y la lectura de la configuración de proxy del registro, y el manifiesto
# llegaba sistemáticamente después de los 2,5 s. Con un tope corto allí, el
# orden se invertía: se elegía reproductor, aparecían las pistas detrás y
# había que elegir reproductor **otra vez**. Más espera en Windows y la misma
# espera de siempre en Linux/macOS: el orden pedido es el mismo en todas las
# plataformas, sólo cambia cuánto se le concede al proveedor.
TRACK_WAIT_SECONDS: float = 4.0 if detect_os() == OS_WINDOWS else 2.5

# Margen **extra**, y sólo el justo, para el caso en que el usuario ya ha
# elegido reproductor y el sondeo *sigue en marcha*. Es la segunda red del
# orden pedido: si la primera espera no alcanzó, todavía se le concede este
# margen antes de lanzar. Sin él, con un sondeo lento (o un equipo lento)
# las pistas aparecían *después* del selector de reproductor.
#
# Sigue siendo una espera acotada y con su pantalla explicativa, y no
# bloquea un canal sin manifiesto (eso lo decide `wait_settles`).
TRACK_LATE_WAIT_SECONDS: float = 4.0 if detect_os() == OS_WINDOWS else 2.5

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
    "TrackOptionsScreen": (
        "Audio, subtítulos y calidad",
        "Aquí eliges el audio, los subtítulos y la calidad del canal, "
        "después eliges con qué reproductor verlo.",
    ),
}

# Qué se puede hacer en la pantalla actual, explicado con frases completas.
_HELP_HERE: dict[str, list[tuple[str, str]]] = {
    "PlaylistsScreen": [
        ("key", "Enter  abrir la lista seleccionada y ver sus canales."),
        ("key", "a  añadir una lista nueva (te pide nombre y ruta o URL)."),
        ("key", "d  borrar la lista seleccionada (pide confirmar)."),
        ("key", "C  cambiar la contraseña guardada (solo listas Xtream)."),
        ("key", "R  actualizar el catálogo (releer playlists.json)."),
        ("key", "u  deshacer el último borrado."),
        ("key", "f  ver tus canales favoritos."),
        ("tip", "Consejo: una lista es un fichero o URL (.m3u, .m3u8, .ts)."),
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
        ("key", "Enter  ver el directo del canal."),
        ("key", "Enter  sobre ▶ ver ese programa del archivo."),
        ("key", "r  volver a cargar la guía."),
        ("tip", "● se emite ahora · ▶ está en el archivo · ○ sólo info."),
        ("tip", "El archivo existe sólo si el proveedor lo declara."),
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
        ("key", "i  volver a analizar las pistas del canal."),
        ("tip", "Audio, subtítulos y calidad se eligen antes, al abrir el canal."),
        ("tip", "Para verlos otra vez: p, elige reproductor, y vuelve a preguntarlos."),
        ("tip", "El vídeo se ve en otra ventana; aquí ves el estado."),
    ],
    "TrackOptionsScreen": [
        ("key", "↑ / ↓  moverte por las opciones."),
        ("key", "← / →  saltar de sección (audio, subtítulos, calidad)."),
        ("key", "Espacio  marcar con * la opción del cursor."),
        ("key", "0  volver a Automático o a Desactivados."),
        ("key", "Enter  confirmar y pasar a elegir reproductor."),
        ("key", "m  recordar esta elección para este canal."),
        ("tip", "Si llegaste aquí con el reproductor ya elegido (el análisis "
                "tardó), Enter reproduce con él: no te lo vuelven a pedir."),
        ("tip", "Puedes marcar una de cada sección: por ejemplo un subtítulo "
                "y una calidad, y luego Enter."),
        ("tip", "El asterisco es una sola marca por lista: si marcas otra "
                "opción, la anterior se desmarca."),
        ("tip", "Sólo aparecen las secciones con más de una opción."),
    ],
}


def _help_here_for(screen, cls_name: str) -> list[tuple[str, str]]:
    """Teclas de la pantalla actual; oculta 'C' si no hay lista Xtream elegida."""
    entries = list(_HELP_HERE.get(cls_name, [
        ("body", "Usa las flechas y Enter; Esc para volver."),
    ]))
    checker = getattr(screen, "can_change_password", None)
    if callable(checker):
        try:
            allowed = bool(checker())
        except Exception:
            allowed = True
        if not allowed:
            entries = [e for e in entries if not e[1].startswith("C  ")]
    return entries


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
        ("key", "q  salir de la app (pide confirmar)."),
        ("blank", ""),
        ("section", "Teclas que valen en todas partes"),
        ("key", "t  cambiar entre tema claro y oscuro."),
        ("key", "r  ver lo último reproducido (Recientes)."),
        ("key", "p  elegir reproductor para el canal actual."),
        ("key", "u  deshacer el último borrado de lista."),
        ("key", "!  comprobar la seguridad (security-check) con modal."),
        ("key", "Ratón  clic para elegir, doble clic para abrir."),
        ("blank", ""),
        ("section", f"En esta pantalla: {where}"),
    ]
    lines.extend(_help_here_for(screen, cls_name))
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
        ("body", "Pistas: ↑↓ elige · 0 Automático · Enter ver · m recordar."),
        ("body", "Reproduciendo: q detiene · i revisa las pistas."),
        ("blank", ""),
        ("section", "Cómo buscar (Canales y Grupos)"),
        ("body", "1. Pulsa / y escribe en el modal: la lista se filtra sola."),
        ("body", "2. Pulsa Enter para quedarte con el filtro y cerrar."),
        ("body", "3. Pulsa Esc para borrar la búsqueda, Ctrl-U para vaciar."),
        ("blank", ""),
        ("section", "Si algo no funciona"),
        ("tip", "Sin reproductor: instala mpv, vlc o mplayer."),
        ("tip", "Sin guía: pulsa e y escribe la ruta o URL del XMLTV."),
        ("tip", "Lista vacía: revisa que el fichero tenga canales (.ts vale)."),
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


# --- Confirmación de salida -------------------------------------------------

QUIT_TITLE = "Salir"
QUIT_MESSAGE = "¿Seguro que deseas salir de la aplicación?"
QUIT_BUTTONS = ["Sí", "No"]


def interpret_quit_choice(res: str | None, key: int) -> bool | None:
    """Decide el modal de salida de forma pura (testeable sin curses).

    Devuelve True si hay que salir, False si hay que quedarse,
    None si el modal debe seguir abierto (navegación entre botones).

    - ``res`` es lo que devuelve ``Modal.handle_key`` (botón en
      minúsculas, "cancel" o None).
    - ``key`` es la tecla cruda (para atajos Esc/q/s/n).
    """
    # Atajos directos de teclado (funcionan sin navegar botones).
    if key in (ord("s"), ord("S"), ord("y"), ord("Y")):
        return True
    if key in (ord("n"), ord("N")):
        return False
    # Esc / q dentro del modal siempre cancelan (quedarse).
    if key in (27, ord("q"), ord("Q")):
        return False
    if res == "cancel":
        return False
    if res in ("sí", "si", "s", "yes", "y"):
        return True
    if res in ("no", "n"):
        return False
    return None


def _is_private_source(source: str) -> bool:
    """True si `source` es una URL hacia red privada/loopback/metadata.

    Sin DNS (`resolve=False`): sólo literales IP y nombres locales. Un
    hostname que resuelva a un rango privado lo pilla el anti-SSRF al
    descargar y se muestra en su propio modal.
    """
    text = (source or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        return False  # ruta local: no hay destino de red
    from thetvview.security.ssrf import SSRFBlockedError, check_url
    from thetvview.security.url_policy import PURPOSE_METADATA, InvalidUrlError

    try:
        check_url(text, PURPOSE_METADATA, allow_private=False, resolve=False)
    except SSRFBlockedError:
        return True
    except (InvalidUrlError, ValueError, OSError):
        return False
    return False


def _is_plain_http(source: str) -> bool:
    """True si `source` es remota y va sin TLS (B13)."""
    return (source or "").strip().lower().startswith("http://")


class App:
    """Estado global + stack de pantallas + bucle de eventos."""
    def __init__(self, stdscr: curses.window) -> None:
        self.stdscr = stdscr
        self.status = StatusBar()
        self.header = HeaderBar(app=self)
        self.footer = FooterBar()
        # Guarda anti-recursión: un modal nunca abre otro modal.
        self._in_modal = False
        # Todo error mostrado con status.show(..., error=True) sube a modal.
        self.status.on_error = self._show_notice
        self.playlists = PlaylistManager(config.PLAYLISTS_JSON)
        self.favorites = FavoritesManager(config.FAVORITES_JSON)
        self.prefs = PrefsManager()
        self.recents = RecentsManager()
        self.epg: Epg | None = None
        self.epg_source: str | None = None
        # Fuente que cargó self.epg automáticamente desde una playlist
        # (None si la cargó el usuario a mano: eso nunca se pisa solo).
        self._epg_auto_source: str | None = None
        # Carga en segundo plano del EPG que declara la playlist abierta.
        self._epg_load_thread: threading.Thread | None = None
        self._epg_failed: dict[str, float] = {}
        self.theme_name: str = self.prefs.load().theme or config.load_theme()
        self.playlist_cache: dict[str, Playlist] = {}
        # Vigencia de cada entrada de playlist_cache (mtime local / instante
        # de descarga): ver cached_playlist()/remember_playlist().
        self.playlist_cache_meta: dict[str, dict] = {}
        # Índice de variantes por fuente: clave -> (playlist, canales, índice).
        # Lo construye warm_catalog en segundo plano para que abrir un canal
        # no tenga que recorrer decenas de miles de nombres con regex.
        self._base_indexes: dict[str, tuple[Playlist, list, dict[str, list[Channel]]]] = {}
        # Carga asíncrona del catálogo: ver warm_catalog()/_warm_worker().
        self._warm_lock = threading.Lock()
        self._warm_queue: list[str] = []
        self._warm_queued: set[str] = set()
        self._warm_failed: dict[str, float] = {}
        self._warm_thread: threading.Thread | None = None
        # Sesión de pistas del canal abierto (audio/subtítulos/calidad).
        # Vive aquí para que el sondeo sobreviva al cambio de pantalla y para
        # que el flujo "reproductor -> opciones de pistas -> reproducir" tenga
        # un único sitio donde mirar el estado. `None` = sin analysing.
        self.track_session = None
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
                top.stop_tracks()
        except Exception:
            pass
        self.stack.pop()
        return True

    # --- Layout ---------------------------------------------------------------

    def body_height(self) -> int:
        from .layout import main_rect
        max_y, max_x = self.stdscr.getmaxyx()
        # La búsqueda vive en un modal centrado (overlay) y ya no reserva
        # una fila superior: el contenido usa siempre el alto completo.
        return main_rect(max_y, max_x).h

    # --- Cache de sesión de listas ya parseadas ------------------------------

    @staticmethod
    def _is_remote(source: str) -> bool:
        # 'xtream://' es la fuente sintética de las listas Xtream: también
        # se refresca por TTL (no existe fichero local cuyo mtime mirar).
        return source.startswith(("http://", "https://", "xtream://"))

    def cached_playlist(self, source: str) -> Playlist | None:
        """Playlist ya parseada en esta sesión si sigue vigente; si no, None.

        Sin esto, cada Enter re-leía y re-parseaba la lista entera: con ~96k
        canales son más de un segundo de bloqueo por apertura.
        """
        key = source.strip()
        playlist = self.playlist_cache.get(key)
        if playlist is None:
            return None
        if not self._session_cache_valid(key, self.playlist_cache_meta.get(key)):
            self.playlist_cache.pop(key, None)
            self.playlist_cache_meta.pop(key, None)
            self._base_indexes.pop(key, None)
            return None
        return playlist

    def _session_cache_valid(self, source: str, meta: dict | None) -> bool:
        """Vigencia de la copia en memoria: mtime (fichero) o TTL (URL)."""
        if not meta:
            # Asignación directa sin meta (tests): se da por válida.
            return True
        if self._is_remote(source):
            from thetvview.m3u_parser import DEFAULT_TTL_HOURS

            return (time.monotonic() - float(meta.get("at", 0.0))) < DEFAULT_TTL_HOURS * 3600
        try:
            return Path(source).stat().st_mtime == meta.get("mtime")
        except OSError:
            # Fichero borrado/ilegible: hay que volver a intentarlo.
            return False

    def remember_playlist(self, source: str, playlist: Playlist) -> None:
        """Guarda en la caché de sesión la playlist recién parseada."""
        key = source.strip()
        self.playlist_cache[key] = playlist
        self._base_indexes.pop(key, None)  # la lista cambió: índice obsoleto
        meta: dict = {"at": time.monotonic()}
        if not self._is_remote(key):
            try:
                meta["mtime"] = Path(key).stat().st_mtime
            except OSError:
                meta["mtime"] = None
        self.playlist_cache_meta[key] = meta

    def forget_playlist(self, source: str) -> None:
        """Descarta la copia en memoria de una lista (no toca disco)."""
        key = source.strip()
        self.playlist_cache.pop(key, None)
        self.playlist_cache_meta.pop(key, None)
        self._base_indexes.pop(key, None)

    # --- Resoluciones ---------------------------------------------------------

    def _base_index(self, key: str, playlist: Playlist) -> dict[str, list[Channel]]:
        """Índice nombre-base -> canales de `playlist` (construido una vez).

        Buscar variantes sin índice recorre cada canal aplicándole regex
        (~0,2s por lista grande) en cada apertura; el índice lo paga solo
        una vez y después la búsqueda es instantánea.
        """
        cached = self._base_indexes.get(key)
        if (
            cached is not None
            and cached[0] is playlist
            and cached[1] is playlist.channels
        ):
            return cached[2]
        index: dict[str, list[Channel]] = {}
        try:
            for channel in playlist.channels:
                index.setdefault(resolutions.base_name(channel), []).append(channel)
        except Exception:
            # Datos raros (nombre no str): índice vacío en vez de romper la
            # UI o de dejar al hilo de catálogo girando sin salir.
            index = {}
        self._base_indexes[key] = (playlist, playlist.channels, index)
        return index

    def variants_for(self, channel: Channel, *, catalog: bool = True) -> list[Channel]:
        """Variantes de resolución del canal buscando en todas las playlists.

        Devuelve variantes swappable (el canal objetivo puede o no tener
        etiqueta de resolución; se buscan entradas con distinta resolución).

        Orden de búsqueda:
        1. Playlists ya abiertas/cacheadas en esta sesión (vía índice: sin
           recorrer canales).
        2. Resto de fuentes del catálogo (se parsean una vez y se cachean;
           las que fallen se saltan en silencio).

        `catalog=False` es la ruta de la UI al abrir un canal: no descarga
        nada en el hilo de la interfaz, usa solo lo ya parseado y deja las
        fuentes pendientes cargando en segundo plano (warm_catalog). Así
        elegir un canal responde al instante en lugar de esperar a todos
        los servidores del catálogo (antes: hasta 30s por fuente caída).
        """
        base = resolutions.base_name(channel)

        def search(key: str, playlist: Playlist) -> list[Channel]:
            group = self._base_index(key, playlist).get(base)
            if not group:
                return []
            return resolutions.swappable_variants(group, channel)

        for key, playlist in list(self.playlist_cache.items()):
            found = search(key, playlist)
            if found:
                return found

        if not catalog:
            self.warm_catalog()
            return []

        for entry in self.playlists.load():
            source = entry.source.strip()
            if source in self.playlist_cache:
                continue
            try:
                playlist = load_playlist_source(
                    source, allow_private=entry.allow_private_network
                )
            except (OSError, ValueError):
                continue  # fuente caída/inaccesible: no bloquea la búsqueda
            self.remember_playlist(source, playlist)
            found = search(source, playlist)
            if found:
                return found
        return []

    # --- Catálogo en segundo plano --------------------------------------------

    def _allow_private_for(self, source: str) -> bool:
        """Excepción anti-SSRF que el catálogo declara para `source` (SDD §11).

        Por defecto la red privada está bloqueada; sólo una fuente que el
        usuario habilitó explícitamente puede salirse de esa regla. Nunca
        lanza: si el catálogo no está disponible, se queda en False.
        """
        try:
            return bool(self.playlists.allow_private_for(source))
        except Exception:
            return False

    def pending_catalog_sources(self) -> list[str]:
        """Fuentes del catálogo todavía no parseadas en esta sesión.

        Las listas Xtream se omiten: necesitan credenciales y solo se
        cargan cuando el usuario las abre.
        """
        pending: list[str] = []
        for entry in self.playlists.load():
            source = (entry.source or "").strip()
            if not source or source in self.playlist_cache:
                continue
            if entry.kind == "xtream":
                continue
            if source not in pending:
                pending.append(source)
        return pending

    def warm_catalog(self) -> None:
        """Carga (o indexa) el catálogo en un hilo aparte, sin tocar la UI.

        Llena la caché de sesión fuera del hilo de la interfaz para que
        variants_for(catalog=False) encuentre variantes entre listas sin
        bloquear la pantalla. Idempotente: lo ya cargado o en cola se salta.
        """
        pending = self.pending_catalog_sources()
        if not pending and self._first_unindexed() is None:
            return
        now = time.monotonic()
        with self._warm_lock:
            fresh = [
                source
                for source in pending
                if source not in self._warm_queued
                and now - self._warm_failed.get(source, -1e9) >= WARM_RETRY_S
            ]
            self._warm_queue.extend(fresh)
            self._warm_queued.update(fresh)
            if self._warm_thread is None:
                self._warm_thread = threading.Thread(
                    target=self._warm_worker, name="catalog-warm", daemon=True
                )
                self._warm_thread.start()

    def _first_unindexed(self) -> tuple[str, Playlist] | None:
        """Primera playlist cacheada sin índice, para construirlo en caliente."""
        for key, playlist in list(self.playlist_cache.items()):
            if key not in self._base_indexes:
                return key, playlist
        return None

    def _warm_worker(self) -> None:
        """Hilo daemon: primero indexa lo ya parseado, luego descarga el resto."""
        try:
            while True:
                unindexed = self._first_unindexed()
                if unindexed is not None:
                    self._base_index(*unindexed)
                    continue
                with self._warm_lock:
                    if not self._warm_queue:
                        # Sale con el candado puesto: warm_catalog no puede
                        # quedar con cola sin hilo que la procese.
                        self._warm_thread = None
                        return
                    source = self._warm_queue.pop(0)
                try:
                    playlist = load_playlist_source(
                        source, allow_private=self._allow_private_for(source)
                    )
                    if playlist is None:
                        raise ValueError(f"fuente ilegible: {source}")
                    self.remember_playlist(source, playlist)
                    self._base_index(source, playlist)
                except Exception:  # red caída/fuente inválida: se reintenta luego
                    with self._warm_lock:
                        self._warm_failed[source] = time.monotonic()
                finally:
                    with self._warm_lock:
                        self._warm_queued.discard(source)
        finally:
            # Red de seguridad: si el hilo muere por algo imprevisto, la
            # próxima llamada a warm_catalog() pueda relanzarlo.
            with self._warm_lock:
                self._warm_thread = None

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
        """Selector de tipo de fuente: lista IPTV o Xtream."""
        from .widgets import Modal

        modal = Modal("Tipo de fuente", "¿Qué tipo de fuente quieres añadir?", ["M3U / M3U8 / TS", "Xtream API"])
        modal.selected_button = 0
        stdscr = self.stdscr
        while True:
            # Avisos del sondeo de pistas: se sacan aquí, en el hilo de la
            # interfaz, porque un modal desde un hilo secundario no es
            # seguro con curses.
            try:
                self._drain_track_avisos()
            except Exception:
                pass

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
            if res in ("cancelar", "cancel", None):
                if key in (27, ord("q"), curses.KEY_LEFT, curses.KEY_BACKSPACE):
                    self.footer.show("Cancelado.")
                    return
                if res is None:
                    continue
                self.footer.show("Cancelado.")
                return
            if res in ("m3u / m3u8", "m3u / m3u8 / ts"):
                self._add_m3u_playlist()
                return
            if res == "xtream api":
                self._add_xtream_source()
                return

    def _prompt_form(
        self,
        title: str,
        fields: list[tuple],
        initial: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        """Formulario modal centrado multi-campo. None si se cancela (Esc).

        Sustituye a los antiguos ``prompt_text`` en la barra inferior:
        dibuja el fondo (header + pantalla + footer), encima el FormModal,
        soporta KEY_RESIZE, terminal pequeña y cursor hardware en el campo
        activo. Valida que ningún campo quede vacío y muestra el error
        dentro del propio modal.
        """
        from .widgets import FormModal

        modal = FormModal(title, fields, initial=initial)
        stdscr = self.stdscr
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                max_y, max_x = stdscr.getmaxyx()
                if max_y < 10 or max_x < 40:
                    # Terminal diminuta: no dibujar modal, solo aviso.
                    stdscr.erase()
                    msg = "Ventana demasiado pequeña"
                    try:
                        stdscr.addstr(max_y // 2, max(0, (max_x - len(msg)) // 2), msg)
                    except curses.error:
                        pass
                    stdscr.refresh()
                    key = stdscr.getch()
                    if key in (27, ord("q")):
                        self.footer.show("Cancelado.")
                        return None
                    continue
                stdscr.erase()
                self.header.render(stdscr, self.screen.title, len(self.stack))
                self.screen.render(stdscr)
                self._render_footer(stdscr)
                modal.render(stdscr)
                # Cursor hardware sobre el campo activo.
                try:
                    pos = modal.cursor_pos(max_y, max_x)
                    if pos is not None:
                        stdscr.move(*pos)
                        curses.curs_set(1)
                    else:
                        curses.curs_set(0)
                except curses.error:
                    pass
                stdscr.refresh()
                key = stdscr.getch()
                if key == curses.KEY_RESIZE:
                    continue
                res = modal.handle_key(key)
                if res is None:
                    continue
                if res == "cancel":
                    self.footer.show("Cancelado.")
                    return None
                # res == "ok": validar campos obligatorios
                data = modal.data()
                # La contraseña Xtream puede quedar vacía solo si el campo
                # no es secret; en general exigimos todo no vacío salvo
                # que el caller marque opcionales vía initial=None especial.
                missing = [lbl for key_, lbl, _ in modal.fields
                           if not modal.values.get(key_, "").strip()]
                if missing:
                    modal.error = f"Falta: {missing[0]}"
                    continue
                return data
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def _add_m3u_playlist(self) -> None:
        """Añadir playlist IPTV con modal centrado (Nombre + Ruta o URL).

        Acepta .m3u/.m3u8/.ts sin distinciones visibles para el usuario."""
        data = self._prompt_form(
            "Añadir playlist",
            [("name", "Nombre"), ("source", "Ruta o URL")],
        )
        if not data:
            return
        name, source = data["name"], data["source"]
        if not self._guard_source_policy(source):
            return
        try:
            self.playlists.add(
                name, source, allow_private_network=_is_private_source(source)
            )
        except PlaylistError as exc:
            self.status.show(str(exc), error=True)
            return
        self.screen.reload()
        self.status.show(f"Playlist '{name}' añadida.")

    def _add_xtream_source(self) -> None:
        """Xtream con un solo modal (nombre/servidor/usuario/contraseña)."""
        data = self._prompt_form(
            "Añadir Xtream",
            [
                ("name", "Nombre"),
                ("server", "Servidor URL"),
                ("user", "Usuario"),
                ("password", "Contraseña", True),
            ],
        )
        if not data:
            return
        # _prompt_form ya strippea nombre/servidor/usuario; la contraseña
        # se devuelve sin strip (puede llevar espacios).
        name = data["name"]
        server = data["server"]
        user = data["user"]
        password = data.get("password", "")

        # Test de conexión
        from thetvview.xtream_config import XtreamConfig
        from thetvview.xtream_provider import authenticate
        from thetvview.xtream_security import normalize_server_url

        try:
            normalized = normalize_server_url(server)
        except Exception as exc:
            self.status.show(f"URL inválida: {exc}", error=True)
            return

        if not self._guard_source_policy(normalized):
            return
        allow_private = _is_private_source(normalized)

        cfg = XtreamConfig(
            server_url=normalized,
            username=user,
            password=password,
            allow_private_network=allow_private,
        )
        self.show_loading("Probando conexión…", sub=normalized)
        try:
            info = authenticate(cfg, force_refresh=True)
        except Exception as exc:
            from thetvview.xtream_errors import friendly_message
            self.status.show(
                f"No se pudo añadir '{name}': {friendly_message(exc)}",
                error=True,
            )
            return

        # Auth OK: mostrar info y guardar
        user_info = info.get("user_info", {})
        status = user_info.get("status", "Active")
        exp_date = user_info.get("exp_date", "")
        msg = f"Conexión OK (estado: {status}"
        if exp_date:
            msg += f", expira: {exp_date}"
        msg += "). Guardando…"

        try:
            self.playlists.add_xtream(
                name, normalized, user, password,
                allow_private_network=allow_private,
            )
        except PlaylistError as exc:
            self.status.show(str(exc), error=True)
            return
        self.screen.reload()
        self.status.show(msg)

    def change_xtream_password(self, name: str) -> None:
        """Cambia la contraseña guardada de una lista Xtream (solo Xtream).

        Pide la nueva contraseña con campo secreto, la valida probando
        autenticación contra el servidor y, si es válida, la persiste.
        Para listas M3U muestra error amigable y no hace nada.
        """
        entry = self.playlists.get(name)
        if entry is None:
            self.status.show(f"'{name}' no existe.", error=True)
            return
        if not entry.is_xtream or not entry.server_url or not entry.username:
            self.status.show(
                "Solo las listas Xtream API tienen contraseña.", error=True
            )
            return
        data = self._prompt_form(
            "Cambiar contraseña Xtream",
            [("password", f"Nueva contraseña ({entry.name})", True)],
        )
        if not data:
            return
        new_password = data.get("password", "")
        if not new_password:
            self.status.show("Cancelado: contraseña vacía.", error=True)
            return
        # Validar contra el servidor antes de guardar.
        from thetvview.xtream_config import XtreamConfig
        from thetvview.xtream_provider import authenticate

        cfg = XtreamConfig(
            server_url=entry.server_url,
            username=entry.username,
            password=new_password,
            allow_private_network=entry.allow_private_network,
        )
        self.show_loading("Verificando contraseña…", sub=entry.server_url)
        try:
            authenticate(cfg, force_refresh=True)
        except Exception as exc:
            from thetvview.xtream_errors import friendly_message

            self.status.show(
                f"No se pudo cambiar: {friendly_message(exc)}",
                error=True,
            )
            return
        if not self.playlists.update_password(name, new_password):
            self.status.show(f"'{name}' no existe.", error=True)
            return
        # Las URLs de stream llevan la contraseña incrustada: hay que
        # olvidar la copia en memoria para que la próxima apertura use las
        # credenciales nuevas (y re-autentique contra el servidor).
        self.forget_playlist(entry.source)
        try:
            self.screen.reload()
        except Exception:
            pass
        self.status.show(f"Contraseña de '{name}' actualizada.")

    def remove_playlist(self, name: str) -> None:
        entry = self.playlists.get(name)
        if entry is None:
            self.status.show(f"'{name}' no existe.", error=True)
            return
        self._undo_stack.clear()
        saved: dict[str, str] = {
            "name": entry.name,
            "source": entry.source,
            "kind": entry.kind,
            "server_url": entry.server_url,
            "username": entry.username,
        }
        # Preservar password Xtream para poder restaurar (solo Xtream).
        if entry.is_xtream:
            creds = self.playlists.get_credentials(entry.name)
            saved["password"] = creds[2] if creds else entry.password
        self._undo_stack.append({"action": "restore_playlist", "entry": saved})
        self._confirm_remove_playlist(name, source=entry.source)

    def _confirm_remove_playlist(self, name: str, source: str | None = None) -> None:
        from .widgets import Modal

        modal = Modal("Confirmar", f"¿Eliminar playlist '{name}'?", ["Cancelar", "Eliminar"])
        modal.selected_button = 0
        stdscr = self.stdscr
        # Render loop del modal
        while True:
            # Avisos del sondeo de pistas: se sacan aquí, en el hilo de la
            # interfaz, porque un modal desde un hilo secundario no es
            # seguro con curses.
            try:
                self._drain_track_avisos()
            except Exception:
                pass

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
        if source:
            self.forget_playlist(source)
        self.screen.reload()
        if removed:
            self.status.show(f"Playlist '{name}' eliminada.")
        else:
            self.status.show(f"'{name}' no existe.", error=True)

    def confirm_quit(self) -> bool:
        """Modal de confirmación de salida. True = salir, False = quedarse.

        Botones ["Sí", "No"] con "No" preseleccionado por seguridad.
        Atajos: s/y = Sí, n = No, Esc/q = No. ←/→/Tab navegan,
        Enter confirma el botón enfocado. Soporta KEY_RESIZE.
        """
        from .widgets import Modal

        modal = Modal(QUIT_TITLE, QUIT_MESSAGE, list(QUIT_BUTTONS))
        modal.selected_button = 1  # "No" por defecto
        stdscr = self.stdscr
        while True:
            # Avisos del sondeo de pistas: se sacan aquí, en el hilo de la
            # interfaz, porque un modal desde un hilo secundario no es
            # seguro con curses.
            try:
                self._drain_track_avisos()
            except Exception:
                pass

            stdscr.erase()
            self.header.render(stdscr, self.screen.title, len(self.stack))
            self.screen.render(stdscr)
            self._render_footer(stdscr)
            modal.render(stdscr)
            stdscr.refresh()
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            # Atajos directos sin necesidad de navegar botones.
            if key in (ord("s"), ord("S"), ord("y"), ord("Y")):
                return True
            if key in (ord("n"), ord("N")):
                self.footer.show("Salida cancelada.")
                return False
            res = modal.handle_key(key)
            decision = interpret_quit_choice(res, key)
            if decision is True:
                return True
            if decision is False:
                self.footer.show("Salida cancelada.")
                return False
            # None: era navegación entre botones, seguir en el modal.

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

    def auto_load_playlist_epg(self, playlist: Playlist | None) -> bool:
        """Carga en segundo plano el EPG que declare la playlist, si lo trae.

        Muchas listas traen su XMLTV en la cabecera (#EXTM3U x-tvg-url /
        url-tvg / tvg-url, con URL o path relativo). En ese caso no hace
        falta pedirle nada al usuario: se descarga (o se lee) al abrir la
        lista, sin bloquear la TUI, y los canales muestran su parrilla
        en cuanto termina.

        Devuelve True si se lanzó la carga. No relanza si:
        - la fuente declarada ya está cargada,
        - el EPG actual lo eligió el usuario a mano (no se pisa),
        - hay una carga en curso,
        - o la fuente falló hace poco (WARM de reintentos).
        """
        sources = playlist_epg_sources(playlist)
        if not sources:
            return False
        if self.epg is not None:
            if self.epg_source in sources:
                return False  # ya está en uso esta fuente
            if self.epg_source != self._epg_auto_source:
                return False  # elección manual del usuario: se respeta
        thread = self._epg_load_thread
        if thread is not None and thread.is_alive():
            return False
        now = time.monotonic()
        pending = [s for s in sources if now - self._epg_failed.get(s, -1e9) >= EPG_RETRY_S]
        if not pending:
            return False
        name = (getattr(playlist, "name", None) or "lista").strip() or "lista"
        # El EPG lo declara la propia lista: hereda su excepción anti-SSRF.
        allow_private = self._allow_private_for(getattr(playlist, "source", "") or "")
        self.status.show(f"Cargando el EPG de '{name}'…")
        worker = threading.Thread(
            target=self._epg_load_worker,
            args=(pending, allow_private),
            name="epg-load",
            daemon=True,
        )
        self._epg_load_thread = worker
        worker.start()
        return True

    def _epg_load_worker(self, sources: list[str], allow_private: bool = False) -> None:
        """Hilo daemon: prueba las fuentes en orden y se queda con la primera.

        `allow_private` es la excepción anti-SSRF de la lista que declaró
        estas fuentes (SDD §11); viaja en el hilo para no leer el catálogo
        desde otro thread.
        """
        last_error = ""
        try:
            for src in sources:
                try:
                    epg = load_epg_source(src, allow_private=allow_private)
                except (OSError, ValueError) as exc:
                    last_error = str(exc)
                    self._epg_failed[src] = time.monotonic()
                    continue
                self.epg = epg
                self.epg_source = src
                self._epg_auto_source = src
                self._epg_failed.pop(src, None)
                self.status.show(f"EPG cargado de la lista ({len(epg.programs)} canales).")
                return
            self.status.show(
                f"No se pudo cargar el EPG de la lista: {last_error or 'fuente ilegible'}",
                error=True,
            )
        finally:
            self._epg_load_thread = None

    def wait_for_epg_load(self, timeout: float = EPG_WAIT_S) -> bool:
        """Espera a lo sumo `timeout` s a que termine la carga en curso.

        True si no queda ninguna carga en marcha (terminó bien, falló o no
        la había); False si sigue cargando al agotarse el tiempo.
        """
        thread = self._epg_load_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            thread = self._epg_load_thread
            if thread is not None and thread.is_alive():
                return False
        return True

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
        Si la playlist trae su EPG declarado en la cabecera, ya se lanza
        con `auto_load_playlist_epg` al abrirla: aquí solo se espera.
        `url_hint` sugiere el x-tvg-url de la playlist de origen.
        """
        if self.epg is None and getattr(self, "_epg_load_thread", None) is not None:
            # La playlist trae su EPG y se está cargando en segundo plano:
            # se espera (acotado) en vez de interrumpir con el prompt.
            if not self.wait_for_epg_load():
                self.notify_warning("El EPG de la lista sigue cargando; vuelve a pulsar 'e'.")
                return []
        if self.epg is None:
            default = self.epg_source or (url_hint or "").strip()
            data = self._prompt_form(
                "Cargar EPG",
                [("path", "XMLTV ruta o URL")],
                initial={"path": default},
            )
            path = (data or {}).get("path", "").strip() or default
            if not path:
                return []
            try:
                self.epg = load_epg_source(
                    path, allow_private=self._allow_private_for(path)
                )
            except (OSError, ValueError) as exc:
                self.status.show(str(exc), error=True)
                return []
            self.epg_source = path
            self._epg_auto_source = None  # elección manual: no la pisa el auto-carga
            self.status.show(f"EPG cargado ({len(self.epg.programs)} canales).")
        elif force_refresh and self.epg_source:
            try:
                self.epg = load_epg_source(
                    self.epg_source,
                    force_refresh=True,
                    allow_private=self._allow_private_for(self.epg_source),
                )
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

    # --- Avisos al usuario (modal) ------------------------------------------

    def notify_error(self, message: str) -> None:
        """Error bloqueante: barra de estado + modal de confirmación."""
        self.notify(message, error=True)

    def notify_warning(self, message: str) -> None:
        """Aviso no bloqueante: barra de estado + modal de confirmación."""
        self.notify(message, error=False)

    def notify(self, message: str, *, error: bool = False, title: str | None = None) -> None:
        """Muestra `message` en la barra de estado y en un modal (Fase 6).

        No negociable #1: errores, advertencias e interacciones del usuario
        se ven en un modal. Fuera de curses, desde un hilo o con otro modal
        abierto, se degrada a la barra de estado para no colgar la TUI.
        """
        self.status.show(message, error=error)
        if not error:
            self._show_notice(title or "Aviso", message)

    def _show_notice(self, title: str, message: str) -> None:
        """Modal informativo de un solo botón. Nunca lanza (Fase 6)."""
        from .widgets import Modal

        self._run_modal(Modal(title, message, ["Aceptar"]))

    def _confirm(self, title: str, message: str) -> bool:
        """Modal de confirmación. False sin UI o si el usuario cancela."""
        from .widgets import Modal

        res = self._run_modal(Modal(title, message, ["Cancelar", "Aceptar"]))
        return res == "aceptar"

    def _run_modal(self, modal) -> str | None:  # noqa: ANN001
        """Bucle de modal centrado; devuelve el botón o None si no hay UI.

        Nunca lanza y nunca se anida: fuera de curses, en un hilo o con otro
        modal abierto devuelve None y deja la decisión en la barra de estado.
        """
        if self._in_modal:
            return None
        if threading.current_thread() is not threading.main_thread():
            return None
        stdscr = getattr(self, "stdscr", None)
        if not isinstance(stdscr, curses.window):
            return None
        self._in_modal = True
        try:
            while True:
                try:
                    stdscr.erase()
                    try:
                        self.header.render(
                            stdscr, getattr(self.screen, "title", ""), len(self.stack)
                        )
                    except Exception:
                        pass
                    try:
                        self.screen.render(stdscr)
                    except Exception:
                        pass
                    try:
                        self._render_footer(stdscr)
                    except Exception:
                        pass
                    modal.render(stdscr)
                    stdscr.refresh()
                except curses.error:
                    pass
                key = stdscr.getch()
                if key == curses.KEY_RESIZE or key == -1:
                    continue
                res = modal.handle_key(key)
                if res is not None:
                    return res
        except Exception:
            # Cualquier sorpresa de curses: el mensaje ya está en la barra.
            return None
        finally:
            self._in_modal = False

    def _guard_source_policy(self, source: str) -> bool:
        """Confirmación previa al guardado de una fuente (SDD §11, B13).

        - Red privada/loopback/metadata → hay que confirmarlo a mano; sin
          confirmación no se guarda (la excepción anti-SSRF es por fuente y
          explícita, nunca global).
        - ``http://`` sin TLS → aviso, no bloquea.

        Devuelve False si hay que cancelar el guardado.
        """
        from thetvview.security.redaction import redact_text

        if _is_private_source(source):
            if not self._confirm(
                "Acceso a red privada",
                "Esta fuente apunta a una dirección local o privada:\n"
                f"  {redact_text(source)}\n"
                "El bloqueo anti-SSRF la impide por defecto.\n"
                "¿Permitir el acceso sólo para esta lista?",
            ):
                self.notify_error("Cancelado: red privada no permitida.")
                return False
        if _is_plain_http(source):
            self.notify_warning(
                "Guardando una URL sin cifrar (http://):\n"
                f"  {redact_text(source)}"
            )
        return True

    def show_loading(self, message: str = "Cargando…", sub: str = "") -> None:
        """Dibuja la pantalla de 'Cargando' y la muestra ya (refresh).

        Se llama justo antes de una operación bloqueante (descargar
        una playlist remota) para que la espera no sea una pantalla
        congelada. Nunca lanza: todo está protegido contra
        curses.error y resize.
        """
        from .widgets import LoadingOverlay

        stdscr = getattr(self, "stdscr", None)
        if not isinstance(stdscr, curses.window):
            return  # sin terminal (tests/headless): nada que dibujar
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
            fresh = load_playlist_source(
                source,
                force_refresh=True,
                allow_private=self._allow_private_for(source),
            )
        except (OSError, ValueError) as exc:
            self.status.show(str(exc), error=True)
            return
        old_n = len(playlist.channels)
        new_n = len(fresh.channels)
        try:
            fresh.kind = getattr(playlist, "kind", "m3u") or "m3u"
        except Exception:
            pass
        self.remember_playlist(source, fresh)
        try:
            self.auto_load_playlist_epg(fresh)
        except Exception:
            pass  # el EPG es opcional: no rompe la recarga de la lista
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


    # --- Pistas (audio / subtítulos / calidad) -------------------------------
    #
    # El sondeo arranca **al abrir el selector de reproductor**, en un hilo
    # daemon: el usuario elige reproductor mientras tanto y, al pulsar Enter,
    # normalmente ya hay resultado. Si no lo hay, se reproduce igual y la
    # tarjeta muestra "Analizando pistas…" (plan F6).

    def _track_source_for(self, channel: Channel) -> str | None:
        """Fuente a la que pertenece el canal (para el ámbito de preferencias)."""
        for screen in reversed(self.stack):
            playlist = getattr(screen, "playlist", None)
            if playlist is not None:
                return playlist.source
        return None

    def start_track_probe(self, channel: Channel, *, wait: float = 0.0) -> None:
        """Crea (o reutiliza) la sesión de pistas y arranca el sondeo.

        Nunca lanza: si algo falla, el canal se reproduce exactamente igual
        que antes de esta funcionalidad.

        Args:
            channel: canal a analizar.
            wait: segundos como mucho que se espera a que el sondeo termine,
                **sólo** si el canal puede tener manifiesto (`wait_settles`
                de la sesión). Con 0 no se espera nada y el sondeo sigue
                volando en segundo plano.
        """
        from .tracks import TrackSession

        actual = self.track_session
        if actual is not None and getattr(actual, "channel", None) == channel:
            actual.start()
            self._await_tracks(actual, wait)
            return
        if actual is not None:
            try:
                actual.stop()
            except Exception:
                pass
        try:
            session = TrackSession(self, channel, self._track_source_for(channel))
        except Exception:
            self.track_session = None
            return
        self.track_session = session
        session.start()
        self._await_tracks(session, wait)

    def _await_tracks(self, session, wait: float) -> None:  # noqa: ANN001
        """Espera **acotada** al sondeo, con un mensaje mientras tanto.

        Sin esta espera el usuario llega al selector de reproductor antes de
        que el manifiesto haya vuelto y las opciones de audio/calidad nunca
        llegan a aparecer: el orden era el equivocado. El tope es corto y hay
        una pantalla que lo explica, así que el coste nunca es un cuelgue
        silencioso.

        Idempotente y sin efectos aparte de esperar: si el sondeo ya
        respondió, o el canal no puede tener manifiesto, no hace nada. Eso la
        deja ser segura de llamar en cualquier momento del flujo.
        """
        if not session.wait_settles or wait <= 0:
            return
        if not session.pending:
            return  # ya estaba en caché: instantáneo
        inicio = time.monotonic()
        self.show_loading(
            "Analizando pistas del canal…",
            sub="Buscando audio, subtítulos y calidad que publica el proveedor",
        )
        while session.pending and (time.monotonic() - inicio) < wait:
            time.sleep(0.05)
        # Un último pintado para quitar la pantalla de espera.
        try:
            stdscr = getattr(self, "stdscr", None)
            stdscr.erase()
            stdscr.refresh()
        except Exception:
            pass

    def _await_tracks_late(self, session, channel: Channel) -> None:  # noqa: ANN001
        """Espera corta de cortesía si el sondeo seguía en marcha.

        Se usa en un único sitio: cuando el usuario **pulsa Enter en el
        selector de reproductor** y las pistas todavía no se han podido
        ofrecer. Si el sondeo sigue vivo se le concede un margen más
        (acotado, y sólo si el canal puede tener manifiesto) antes de
        renunciar a preguntar por el audio, los subtítulos y la calidad.

        Por qué hace falta: con sólo la primera espera, en un equipo lento el
        orden se rompía —el reproductor se elegía primero y las pistas
        aparecían después—, y al confirmar esas pistas había que elegir
        reproductor otra vez. Es un problema de cronología, no de SO: se ve
        en Windows porque allí el primer sondeo tarda más (DNS, TLS y la
        lectura del proxy del sistema), pero puede pasar en cualquier
        plataforma con un proveedor lento.
        """
        if session is None or getattr(session, "channel", None) != channel:
            return
        if not getattr(session, "pending", False):
            return  # ya respondió: no hay nada que esperar
        self._await_tracks(session, TRACK_LATE_WAIT_SECONDS)

    def _reproduccion_en_curso(self) -> bool:
        """True si hay un canal abierto en un reproductor externo ahora mismo."""
        return any(
            isinstance(screen, NowPlayingScreen)
            for screen in getattr(self, "stack", [])
        )

    def track_capabilities(self):  # noqa: ANN201
        session = self.track_session
        return session.capabilities if session is not None else None

    def track_selection(self):  # noqa: ANN201
        """Selección activa, o None si el usuario no ha tocado nada."""
        session = self.track_session
        if session is None:
            return None
        selection = session.selection
        return None if selection.is_default else selection

    def push_player_screen(self, channel: Channel) -> bool:
        """Empuja el selector de reproductor (o avisa si no hay ninguno).

        Devuelve False si no hay ningún reproductor instalado: en ese caso
        no se empuja nada, igual que antes de esta funcionalidad.
        """
        screen = PlayerScreen(self, channel)
        if not screen.players:
            self.status.show(
                "No hay reproductor disponible. Instala uno de: "
                + ", ".join(config.SUPPORTED_PLAYERS),
                error=True,
            )
            return False
        self.push(screen)
        return True

    def maybe_open_track_options(
        self, channel: Channel, player_name: str | None = None
    ) -> bool:
        """Abre el selector de pistas si hay algo real que elegir.

        Devuelve True si se abrió (y entonces no se sigue al reproductor
        todavía). Con menos de dos opciones por tipo **no** se abre
        (SDD §33/§34): ahí la reproducción sigue el camino de siempre.

        ``player_name=None`` significa "todavía no se ha elegido
        reproductor", que es el orden que se pidió: pistas primero,
        reproductor después. En ese caso se ofrecen todas las secciones,
        porque los tres reproductores soportan audio y subtítulos, y la
        calidad se aplica por el proxy de pinning.
        """
        from .screens import TrackOptionsScreen

        session = self.track_session
        if session is None or getattr(session, "channel", None) != channel:
            return False
        if not session.has_menu(player_name):
            self._explain_degraded(session)
            return False
        self.push(TrackOptionsScreen(self, channel, session, player_name))
        return True

    def _track_options_then_player(self, channel: Channel, *, wait: float) -> bool:
        """Orden pedido: pistas primero, reproductor después.

        Devuelve True si esta llamada ya ha resuelto la navegación (siempre,
        para que la caller no tenga que decidir nada): o ha empujado el
        selector de pistas, o el de reproductor, o ha avisado de que no hay
        ninguno instalado.

        Es el orden natural: el reproductor no cambia **qué** pistas publica
        el canal, así que preguntárselo al usuario antes de elegir con qué
        programa verlo es preguntar por lo que sí depende del canal.
        """
        # Primero lo barato y lo que más duele: si no hay reproductor
        # instalado no tiene sentido enseñar opciones de pistas.
        screen = PlayerScreen(self, channel)
        if not screen.players:
            self.status.show(
                "No hay reproductor disponible. Instala uno de: "
                + ", ".join(config.SUPPORTED_PLAYERS),
                error=True,
            )
            return True
        self.start_track_probe(channel, wait=wait)
        if not self.maybe_open_track_options(channel, None):
            self.push(screen)
        return True

    def open_track_options(self, channel: Channel, player_name: str | None, *, kind: str | None = None) -> None:
        """Abre el selector para una sección concreta (`kind`)."""
        from .screens import TrackOptionsScreen
        from thetvview.tracks.manager import SelectTrackError

        session = self.track_session
        if session is None:
            self.notify_warning(
                "Este canal no tiene pistas analizadas, así que no hay nada "
                "que cambiar aquí."
            )
            return
        if kind not in session.kinds(player_name):
            self.notify_warning(self._no_selection_reason(session, kind, player_name))
            return
        self.push(TrackOptionsScreen(self, channel, session, player_name, kind=kind))

    def _no_selection_reason(self, session, kind: str | None, player_name: str | None) -> str:
        """Por qué no hay menú para esa sección. Un texto, no un silencio."""
        from thetvview.player.capabilities import hot_control
        from thetvview.tracks.models import puede_fijar_calidad

        if session.capabilities is None:
            return (
                "No se pudo averiguar qué pistas publica este canal, así que "
                "no hay nada que cambiar. Se reproduce con normalidad."
            )
        if not hot_control(player_name) and session.capabilities.has_any_choice:
            return (
                f"{str(player_name).upper()} no puede cambiar de pista en "
                "caliente. Elige la pista antes de abrir el reproductor."
            )
        if kind == "quality" and not puede_fijar_calidad(session.capabilities):
            return (
                "La calidad fija sólo es posible en streams HLS, y este canal "
                "usa otro formato. Se reproduce con calidad automática."
            )
        if kind == "quality":
            return (
                "La calidad ya no se puede cambiar con el canal abierto: "
                "reabre el canal y elige la calidad antes de ver."
            )
        return (
            "Este canal publica una sola opción para eso, así que no hay nada "
            "que elegir."
        )

    def _explain_degraded(self, session) -> None:
        """Explica por qué no hay menú — pero sólo si hay algo que explicar.

        Distingue dos cosas que antes salían igual y no deberían:

        - **"No hay nada que elegir"** (una sola pista, un directo, el
          proveedor no publica alternativas). Eso no es un fallo: el canal se
          reproduce perfectamente. Interrumpir con un modal antes incluso de
          elegir reproductor hace creer que algo va mal. Va a la barra de
          estado, que es donde se lee sin interrumpir.
        - **"No se pudo averiguar"** (403, 404, timeout). Eso sí es un fallo
          del proveedor y sí merece un modal, aunque el canal suene igual:
          sin él el usuario no sabe si el canal iba a venir con pistas.
        """
        if session.degraded_shown:
            return
        mensaje, fallo = session.degraded_outcome()
        if not mensaje:
            return
        session.degraded_shown = True
        if fallo:
            self.notify_warning(mensaje)
        else:
            self.status.show(mensaje)

    def remember_track_prefs(self, channel, session, *, for_provider: bool = False) -> None:  # noqa: ANN001
        """'Recordar para este canal' con confirmación en modal (AGENTS)."""
        if session is None:
            self.notify_warning("No hay nada que recordar todavía.")
            return
        if not self._confirm(
            "Recordar calidad e idioma",
            "Se guardará esta elección de audio, subtítulos y calidad para:\n"
            f"  {channel.name if channel is not None else '?'}\n"
            + ("toda la fuente de esta lista.\n" if for_provider
               else "sólo este canal.\n")
            + "\nPuedes cambiarla luego desde las preferencias.",
        ):
            self.status.show("Cancelado: no se guardó ninguna preferencia.")
            return
        guardado = session.remember(for_provider=for_provider)
        if guardado:
            self.notify(
                "Preferencia guardada"
                + (" para toda la lista." if for_provider else " para este canal.")
            )
        else:
            self.notify_warning(
                "No se pudo guardar la preferencia: no hay donde escribirla."
            )

    def recheck_tracks(self, channel) -> None:  # noqa: ANN001
        """Vuelve a analizar las pistas del canal abierto (tecla `i`).

        El sondeo va en segundo plano: aquí sólo se avisa de que empieza. Los
        avisos que traiga (pista desaparecida, §28) se enseñan cuando
        lleguen, desde el hilo de la interfaz.
        """
        session = self.track_session
        if session is None:
            self.notify_warning(
                "Este canal no tiene sesión de pistas abierta: abre el canal "
                "de nuevo para analizarlas."
            )
            return
        session.recheck()
        self.status.show("Analizando las pistas del canal…")

    def _drain_track_avisos(self) -> None:
        """Enseña (en modal) los avisos que dejó el sondeo, si hay alguno."""
        session = self.track_session
        if session is None:
            return
        for aviso in session.take_avisos():
            self.notify_warning(aviso)

    def play_catchup(self, channel: Channel, program: Program) -> None:
        """Pide al proveedor un programa ya emitido y lo reproduce (§8, §10).

        El orden es el del SDD y es lo importante: **primero** se valida la
        capacidad y la ventana; **después** se construye la referencia
        opaca; **por último** se delega en `play_channel`, que es el mismo
        camino que el directo. Si algo falla, el motivo sale en un modal y
        no se lanza nada.

        La pantalla no decide si el canal tiene archivo: eso lo dice
        `catchup.capability_for`, que es la única fuente de la verdad.
        """
        cap = catchup.capability_for(channel)
        now = datetime.now().astimezone()
        estado = catchup.classify(channel, program, now)
        if estado is not catchup.CatchupState.AVAILABLE:
            # Ninguna petición histórica sale por aquí: sólo el porqué (§17).
            self.notify_warning(catchup.describe_state(estado, cap))
            return
        inicio = program.start
        fin = program.stop or program.start + timedelta(hours=1)
        request = catchup.CatchupRequest(
            channel_id=str((channel.attrs or {}).get("xtream_id") or channel.tvg_id or ""),
            start=inicio,
            end=fin,
        )
        try:
            playback = catchup.build_playback_request(
                channel, cap, request, now=now,
            )
        except catchup.CatchupError as exc:
            # Plantilla que no reconocemos, mecanismo desconocido, esquema
            # no permitido: se explica y no se intenta reproducir (§13).
            self.notify_error(str(exc))
            return
        # Hecho: la referencia es opaca y sin credenciales. De aquí en
        # adelante esto es una reproducción normal para el motor multimedia
        # (SDD Catch-up §14): mismo reproductor, mismo argv, mismo argv `--`.
        play_channel(
            self,
            catchup.playback_channel(playback),
            player_name=self.prefs.last_player(),
            catchup_playback=playback,
            catchup_program=program,
        )

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
                    if entry.get("kind") == "xtream":
                        self.playlists.add_xtream(
                            entry["name"],
                            entry.get("server_url", ""),
                            entry.get("username", ""),
                            entry.get("password", ""),
                        )
                    else:
                        self.playlists.add(entry["name"], entry["source"])
                except PlaylistError as exc:
                    self.status.show(str(exc), error=True)
                    return
                self.screen.reload()
                self.status.show(f"Playlist '{entry['name']}' restaurada.")
            case "change_password":
                self.change_xtream_password(action["name"])
            case "open_playlist":
                open_playlist(self, action["entry"])
                if isinstance(self.screen, ChannelsScreen):
                    # Precarga el resto del catálogo en segundo plano: así,
                    # al elegir un canal, las variantes ya están en memoria.
                    self.warm_catalog()
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
                # catalog=False: la UI nunca espera a descargar otras listas
                # (antiguamente hasta 30s por servidor caído antes de pintar).
                variants = self.variants_for(channel, catalog=False)
                if variants:
                    self.push(ResolutionScreen(self, channel, variants))
                    return
                self._track_options_then_player(channel, wait=TRACK_WAIT_SECONDS)
            case "select_player":
                self._track_options_then_player(
                    action["channel"], wait=TRACK_WAIT_SECONDS
                )
            case "force_select_player":
                # `p` desde cualquier pantalla: mismo orden, pero sin esperar
                # (el usuario ya está eligiendo reproductor a propósito).
                self._track_options_then_player(action["channel"], wait=0.0)
            case "play_with":
                channel = action["channel"]
                player_name = action["player_name"]
                session = self.track_session
                ya_elegido = bool(
                    session is not None
                    and getattr(session, "channel", None) == channel
                    and getattr(session, "chosen", False)
                )
                # Si el usuario ya pasó por el selector de pistas, se respeta
                # su elección y se abre el canal. Si no pasó (p. ej. porque
                # el sondeo terminó tarde y aquí es la primera oportunidad),
                # se le ofrece ahora antes de lanzar: es el único sitio
                # donde todos los reproductores hacen lo mismo.
                if not ya_elegido:
                    # Antes de renunciar a las pistas se les concede un
                    # margen corto si el sondeo sigue en marcha: es lo que
                    # evita que el orden se invierta en equipos lentos.
                    self._await_tracks_late(session, channel)
                    if self.maybe_open_track_options(channel, player_name):
                        return
                play_channel(
                    self,
                    channel,
                    player_name=player_name,
                    selection=self.track_selection(),
                    capabilities=self.track_capabilities(),
                    track_session=self.track_session,
                )
            case "tracks_choose_player":
                channel = action["channel"]
                session = self.track_session
                if session is not None and getattr(session, "channel", None) == channel:
                    # Marca de que la elección ya está hecha: al elegir
                    # reproductor no se vuelve a preguntar.
                    session.chosen = True
                player_name = action.get("player_name")
                if player_name and not self._reproduccion_en_curso():
                    # Esta pantalla se abrió con un reproductor ya elegido:
                    # el sondeo llegó tarde y las pistas se ofreció justo al
                    # confirmar el reproductor. Se reproduce con el que se
                    # eligió; volver a preguntar era hacer elegir al usuario
                    # dos veces lo mismo.
                    self.handle_action(
                        {
                            "action": "play_with",
                            "channel": channel,
                            "player_name": player_name,
                        }
                    )
                    return
                self.push_player_screen(channel)
            case "play_with_selection":
                play_channel(
                    self,
                    action["channel"],
                    player_name=action.get("player_name"),
                    selection=action.get("selection"),
                    capabilities=self.track_capabilities(),
                    track_session=self.track_session,
                )
            case "open_track_kind":
                self.open_track_options(
                    action["channel"],
                    action.get("player_name"),
                    kind=action.get("kind"),
                )
            case "remember_track_prefs":
                self.remember_track_prefs(
                    action.get("channel"),
                    action.get("session"),
                    for_provider=bool(action.get("for_provider")),
                )
            case "recheck_tracks":
                self.recheck_tracks(action.get("channel"))
            case "play_catchup":
                self.play_catchup(action["channel"], action["program"])
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
                    try:
                        cur.stop_tracks()
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
            sesion = self.track_session
            if (
                isinstance(self.screen, NowPlayingScreen)
                or self._epg_load_thread is not None
                or (sesion is not None and sesion.pending)
            ):
                # Reproducción (vivo) o EPG de la playlist cargándose en
                # segundo plano: refresco cada 500 ms para que los datos
                # aparezcan en cuanto estén, sin esperar a una tecla.
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

            # Avisos del sondeo de pistas: se sacan aquí, en el hilo de la
            # interfaz, porque un modal desde un hilo secundario no es
            # seguro con curses.
            try:
                self._drain_track_avisos()
            except Exception:
                pass

            stdscr.erase()
            self.header.render(stdscr, self.screen.title, len(self.stack))
            self.screen.render(stdscr)
            self._render_footer(stdscr)
            stdscr.refresh()

            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                continue
            if key == -1 and not isinstance(self.screen, NowPlayingScreen):
                # Refresco periódico (EPG de la playlist cargándose en
                # segundo plano): repinta sin consumirlo como pulsación.
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
                        try:
                            self.screen.stop_tracks()
                        except Exception:
                            pass
                        try:
                            msg = self.screen.exit_summary()
                        except Exception:
                            msg = f"'{self.screen.channel.name}' finalizado."
                        self.pop()
                        self.footer.show(msg)
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
                    # 'q' = salir de la app con confirmación (Esc = volver).
                    if self.confirm_quit():
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
                if key == ord("!"):
                    self.run_security_check()
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

    def run_security_check(self) -> None:
        """Tecla ``!``: ejecuta el security-check y abre el modal con el informe.

        No negociable #1: el resultado —correcto o no— siempre termina en
        un modal, nunca sólo en la barra de estado.
        """
        from ..security.check import FAILED_MARK, PASSED_MARK, run_checks

        results = run_checks()
        failed = [r for r in results if not r.ok]
        total = len(results)
        title = "Comprobación de seguridad"
        if not failed:
            msg = f"SECURITY CHECK PASSED ({total}/{total})"
            msg += "\n\nTodos los controles del SDD §45 responden correcto."
            msg += "\nDetalle: python -m thetvview.security.check"
            self.status.show(f"Security check: {total}/{total} correcto.")
        else:
            head = f"SECURITY CHECK FAILED ({len(failed)}/{total})"
            rows = [
                f"{FAILED_MARK} {r.label}\n     {r.detail}" for r in failed
            ]
            msg = head + "\n\n" + "\n".join(rows)
            msg += f"\n\nPulsa ! para repetir. ({PASSED_MARK} = correcto)"
            self.status.show(
                f"Security check: {len(failed)} comprobaciones fallan.",
                error=True,
            )
        self._show_notice(title, msg)

    def _show_help(self) -> None:
        """Ayuda contextual con scroll: qué hacer aquí, paso a paso."""
        from . import icons

        lines = build_help_lines(self.screen)
        offset = 0
        stdscr = self.stdscr

        def render() -> tuple[int, int, int]:
            """Dibuja fondo + ventana de ayuda. Devuelve (visibles, total, alto)."""
            max_y, max_x = stdscr.getmaxyx()
            # Avisos del sondeo de pistas: se sacan aquí, en el hilo de la
            # interfaz, porque un modal desde un hilo secundario no es
            # seguro con curses.
            try:
                self._drain_track_avisos()
            except Exception:
                pass

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
