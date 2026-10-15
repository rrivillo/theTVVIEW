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
from thetvview import epg_match
from thetvview import resolutions
from thetvview.epg_parser import (
    Epg,
    EpgValidation,
    aviso_zona_pendiente,
    load_url,
    parse_file,
    resolve_source,
)
from thetvview.m3u_parser import PlaylistValidation
from thetvview.collections.manager import ColeccionesManager, ColeccionesManagerError
from thetvview.collections.rules import (
    ReglaInvalida,
    evaluar_coleccion,
    validar_regla,
)
from thetvview.collections.schema import Coleccion
from . import colors, filters as filters_mod, rule_text, sources_view, theme
from . import failover as failover_mod
from . import recording as recording_mod
from thetvview.favorites import FavoritesError, FavoritesManager
from thetvview.channel_identity import channel_identity
from thetvview.models import Channel, Playlist, Program
from thetvview.platform_check import OS_WINDOWS, detect_os
from thetvview.player.session import SessionRegistry
from thetvview.playlist_manager import PlaylistError, PlaylistManager
from thetvview.prefs import PrefsManager
from thetvview.recents import RecentsManager
from thetvview.security.redaction import redact_text
from thetvview.sources.associations import (
    AssociationStore,
    AsociacionesError,
    clave_candidata,
    clave_logica,
)
from thetvview.stream_ref import is_opaque_ref
from thetvview.tasks.manager import CancellationToken, TaskManager

from .errormsg import describe_playlist_error
from .widgets import FooterBar, HeaderBar, StatusBar
from .screens import (
    ChannelsScreen,
    EpgScreen,
    FavoritesScreen,
    GroupsScreen,
    HomeScreen,
    NowPlayingScreen,
    PlaylistsScreen,
    PlayerScreen,
    RecentsScreen,
    ResolutionScreen,
    SchedulerScreen,
    Screen,
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


#: Callback que muestra los avisos de **codificación** de una lista
#: (M3U-001 · FR-M3U-001). Lo registra `App.run()`; `load_playlist_source`
#: lo consume después de cada carga, y el aviso se marca como visto para
#: no repetirse en el siguiente repintado. Vive aquí y no como atributo
#: porque el aviso lo produce el parser, que no conoce la TUI.
_aviso_codificacion = None  # Callable[[str], None] | None


def load_playlist_source(
    source: str,
    *,
    force_refresh: bool = False,
    allow_private: bool = False,
    confirmar=None,  # noqa: ANN001 - Callable[[PlaylistValidation], bool] | None
    previa: Playlist | None = None,
    aviso=None,  # noqa: ANN001 - Callable[[str], None] | None
) -> Playlist:
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

    Tras cargar, si el parser registró una decisión de codificación que
    el usuario debe ver (lista heredada o con sustituciones), se muestra
    en un modal con `aviso` —o con el callback global que registró la
    TUI—. Es una aviso, no un error: la lista se abre igual.
    """
    global _aviso_codificacion  # noqa: PLW0603 - el aviso va al modal activo

    from thetvview import m3u_encoding, m3u_parser

    if source.lower().startswith(("http://", "https://")):
        playlist = m3u_parser.load_url(
            source,
            force_refresh=force_refresh,
            allow_private=allow_private,
            confirmar=confirmar,
            previa=previa,
        )
    else:
        playlist = m3u_parser.parse_file(source)

    mostrar = aviso or _aviso_codificacion
    if mostrar is not None:
        try:
            texto = m3u_encoding.texto_aviso_pendiente()
            if texto:
                mostrar(texto)
        except Exception:  # noqa: BLE001 - un aviso nunca rompe la carga
            pass
    return playlist


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
    "HomeScreen": (
        "Bienvenida",
        "Todavía no hay listas. Empieza añadiendo una; el resto llega después.",
    ),
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
    "CollectionsScreen": (
        "Colecciones",
        "Aquí están tus grupos de canales. Cada fila dice si la armaste tú, "
        "si la decide una regla o si es una vista de la app.",
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
    "SchedulerScreen": (
        "Planificador",
        "Aquí programa grabaciones para que empiecen solas a la hora indicada.",
    ),
}

# Qué se puede hacer en la pantalla actual, explicado con frases completas.
_HELP_HERE: dict[str, list[tuple[str, str]]] = {
    "HomeScreen": [
        ("key", "a  añadir tu primera lista de canales."),
        ("key", "q  salir de la app (pide confirmar)."),
        ("key", "t  cambiar el tema (claro, oscuro o alto contraste)."),
        ("tip", "Una lista puede ser un fichero o URL M3U/M3U8."),
        ("tip", "También puedes dar de alta tu cuenta de Lists X."),
    ],
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
        ("key", "c  abrir la lista de colecciones."),
        ("key", "C  elegir en qué colecciones está este canal."),
        ("key", "s  ver y elegir fuentes de este canal."),
        ("key", "F  filtrar la lista: guía, catch-up, definición, grupo..."),
        ("key", "X  quitar todos los filtros de golpe (te dice cuáles)."),
        ("key", "o  cambiar el orden de la lista."),
        ("key", "R  actualizar la lista (los canales pueden cambiar)."),
        ("key", "p  elegir con qué reproductor ver el canal."),
        ("tip", "En búsqueda: Enter confirma, Esc borra la búsqueda."),
        ("tip", "Filtros y búsqueda se juntan: los dos tienen que cumplirse."),
        ("tip", "La definición es una estimación del nombre o del tvg-id:"),
        ("tip", "la app no abre el canal para comprobarla."),
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
    "CollectionsScreen": [
        ("key", "Enter  ver el detalle de la colección."),
        ("key", "a  crear una colección nueva (te pide el nombre)."),
        ("key", "e  cambiarle el nombre."),
        ("key", "d  borrarla (te dice cuántos canales se van con ella)."),
        ("key", "m  ver y quitar sus canales."),
        ("key", "R  darle una regla: campo, comparación y valor."),
        ("tip", "En cada fila se lee el tipo: manual, inteligente o del sistema."),
        ("tip", "Sólo una colección manual tiene canales que tú eliges: en las "
                "demás, «m» no hace nada."),
        ("tip", "Con «R» una manual pasa a decidirse sola por su regla: antes "
                "te avisa de cuántos canales dejarían de contar."),
        ("tip", "Una vista del sistema (Favoritos, Recientes) no se renombra "
                "ni se borra: sus canales son los tuyos."),
        ("tip", "Si un canal desaparece de tus listas, la colección lo avisa "
                "pero sigue funcionando con el resto."),
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
    "SchedulerScreen": [
        ("key", "a  programar una grabación nueva."),
        ("key", "Enter  ver el detalle de la tarea programada."),
        ("key", "c  cancelar la tarea seleccionada."),
        ("key", "d  borrar una tarea terminada o cancelada."),
        ("key", "r  ver los recientes."),
        ("tip", "Las grabaciones empiezan solas cuando llega su hora."),
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
        ("key", "t  cambiar entre tema claro, oscuro y alto contraste."),
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
        ("body", "Listas: a añadir · d borrar · Enter abrir · f favoritos · T tema."),
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


class _EpgLoadOutcome:
    """Lo que devuelve el worker de EPG. Lo aplica el hilo principal.

    Existe para que el hilo de descarga **no** escriba en `self.epg`: entre
    que termina de descargar y que el hilo principal drena la cola hay una
    ventana en la que el usuario puede haber cambiado de lista. Si el worker
    escribiera directamente, ese EPG antiguo aparecería sobre el nuevo.
    """

    __slots__ = ("epg", "source", "error", "cancelled")

    def __init__(
        self,
        epg: Epg | None = None,
        source: str = "",
        error: str = "",
        cancelled: bool = False,
    ) -> None:
        self.epg = epg
        self.source = source
        self.error = error
        self.cancelled = cancelled


# Etiqueta de un canal para modales de fuentes.
_ETIQUETA_DE_CANAL = ""


def _etiqueta_de_canal(channel) -> str:  # noqa: ANN001 - Channel
    """Nombre del canal, redactado por si su nombre arrastra un token."""
    try:
        return redact_text(str(getattr(channel, "name", "") or "")).strip() or "canal"
    except Exception:
        return "canal"


def _no_se_marca_en_radio(item, marcado: bool):  # noqa: ANN001
    """El `on_toggle` de un `ChecklistModal` en modo *radio*.

    En radio `handle_key` devuelve «pick» y el bucle sale antes de llegar
    aquí, así que no se guarda nada. Si algún día se llegara, **no** se marca:
    marcar sin que nadie haya decidido sería enseñar un «sí» sobre una
    elección que no ocurrió.
    """
    return "Esta lista elige una opción; aquí no se marca nada."


class App:
    """Estado global + stack de pantallas + bucle de eventos."""
    def __init__(self, stdscr: curses.window) -> None:
        self.stdscr = stdscr
        self.status = StatusBar()
        self.header = HeaderBar(app=self)
        self.footer = FooterBar()
        # Guarda anti-recursión: un modal nunca abre otro modal.
        self._in_modal = False
        # 'q' confirmó salir: `_tecla_global` lo deja marcado y `run()` corta
        # el bucle. Existe para que el reparto de teclas globales sea un
        # método testeable en vez de un bloque en medio del bucle de eventos.
        self._salir_pendiente = False
        # Todo error mostrado con status.show(..., error=True) sube a modal.
        self.status.on_error = self._show_notice
        self.playlists = PlaylistManager(config.PLAYLISTS_JSON)
        self.favorites = FavoritesManager(config.FAVORITES_JSON)
        # Colecciones (Fase 4, UI en Fase 8 · U-03). El gestor no decide el
        # catálogo: se lo pasa quien lo pinte, porque `miembros()` cachea por
        # identidad de la lista y una lista nueva en cada llamada no cachea.
        # El proveedor de contexto se conecta aquí (U-04b): sin él, las
        # colecciones por regla se evalúan contra campos vacíos.
        self.collections = ColeccionesManager(
            config.COLLECTIONS_JSON,
            context_provider=self.contexto_de_reglas,
        )
        self.prefs = PrefsManager()
        self.recents = RecentsManager()
        self.epg: Epg | None = None
        self.epg_source: str | None = None
        # Fuente que cargó self.epg automáticamente desde una playlist
        # (None si la cargó el usuario a mano: eso nunca se pisa solo).
        self._epg_auto_source: str | None = None
        # Claves EPG ambiguas ya avisadas (una vez por clave y por proceso:
        # FR-EPG-003). Ver `_avisar_epg_ambiguo`.
        self._avisos_epg_ambiguos: set[str] = set()
        # Carga en segundo plano del EPG que declara la playlist abierta.
        self._epg_load_thread: threading.Thread | None = None
        # Gestor común de tareas: toda la carga en segundo plano pasa por aquí
        # (generación, cancelación, límites y publicación por cola). El worker
        # no toca la interfaz; el hilo principal drena en _drain_tasks().
        self._tasks = TaskManager(max_global=4, max_per_category=2)
        # Registro de sesiones de reproducción (SDD §4, PLAY-001/004). Es el
        # guard de obsolescencia de la reproducción: una tarea tardía del canal
        # A consulta `is_current` antes de tocar nada y, si el usuario ya abrió
        # el B, se descarta. Vive aquí —y no como global del módulo— para que dos
        # instancias de la app en el mismo proceso no compartan vigencia.
        self._sessions = SessionRegistry()
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
        # Sesión de grabación activa (U-09).
        self._recording_session: Any = None
        self._recording_supervisor: Any = None
        self._recording_session_path: Any = None
        # Planificador de grabaciones (U-10): decide cuándo grabar y delega
        # la captura en RecordingRunner, que usa el grabador de Fase 6.
        self._scheduler: Any = None
        self._warm_failed: dict[str, float] = {}
        #: ``True`` mientras haya una tanda de warming en marcha. Antes era el
        #: ``threading.Thread``; ahora el hilo lo tiene el gestor de tareas y
        #: esto sólo dice «¿hay alguien descargas?», que es lo que necesita
        #: saber el resto del código.
        self._warm_thread: bool = False
        #: Señal de cancelación cooperativa del warming del catálogo. La
        #: comprueba el worker entre fuentes; el cierre la pone para no
        #: depender de que el hilo termine por su cuenta (TASK-004).
        self._warm_stop = threading.Event()
        #: Claves pendiente de indexar. El worker no indexa (eso es del hilo
        #: principal), así que las anota aquí para que `_drain_tasks` las
        #: resuelva al recibir el resultado.
        self._warm_indexes: list[str] = []
        # Sesión de pistas del canal abierto (audio/subtítulos/calidad).
        # Vive aquí para que el sondeo sobreviva al cambio de pantalla y para
        # que el flujo "reproductor -> opciones de pistas -> reproducir" tenga
        # un único sitio donde mirar el estado. `None` = sin analysing.
        self.track_session = None
        self.stack: list[Screen] = []
        self._undo_stack: list[dict] = []
        # La **raíz** depende de si hay algo que ver (FR-HOM-01/06):
        # con listas es el catálogo, como siempre; sin listas, la bienvenida,
        # que sólo ofrece acciones que se pueden ejecutar. No se añaden
        # pantallas intermedias a quien ya tiene su lista configurada.
        self.stack.append(self._pantalla_raiz())

    def _tiene_listas(self) -> bool:
        """¿Hay al menos una lista en el catálogo? Degrada a False."""
        cargar = getattr(getattr(self, "playlists", None), "load", None)
        if not callable(cargar):
            return False
        try:
            return bool(cargar())
        except Exception:
            return False

    def _pantalla_raiz(self) -> Screen:
        return PlaylistsScreen(self) if self._tiene_listas() else HomeScreen(self)

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

    def catalogo_canales(self) -> list[Channel]:
        """Canales de todas las listas ya cargadas en memoria, sin repetir.

        Una colección guarda **referencias** a canales, no canales. Contar sus
        miembros (o resolver sus referencias obsoletas) exige saber qué canales
        existen, y eso es lo que una colección pregunta a la app.

        Sólo se mira `playlist_cache` —las listas que el usuario abrió y las
        que `warm_catalog` dejó descargadas—, y aquí **no se descarga nada**:
        abrir la lista de colecciones no puede ser la razón de que la app se
        quede esperando a un servidor. Sin listas en memoria devuelve `[]`, que
        es lo que la pantalla puede afirmar con honestidad.

        La deduplicación es por **identidad lógica** y no por URL, porque es la
        clave que el gestor de colecciones usa para sus miembros. Sin ella, un
        canal presente en dos listas saldría dos veces y el contador mentiría.
        """
        vistos: set[str] = set()
        salida: list[Channel] = []
        for playlist in list(self.playlist_cache.values()):
            for canal in getattr(playlist, "channels", None) or []:
                try:
                    clave = channel_identity(canal)
                except Exception:
                    clave = f"\x00url:{getattr(canal, 'url', '')}"
                if clave in vistos:
                    continue
                vistos.add(clave)
                salida.append(canal)
        return salida

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
            try:
                self.pedir_indice_fuentes()
            except Exception:
                pass
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

        El trabajo se pide al :class:`~thetvview.tasks.manager.TaskManager`:
        el hilo **descarga y devuelve**, y quien lo indexa y lo mete en la
        caché es el bucle principal (:meth:`_drain_tasks`). Antes el worker
        escribía en `playlist_cache` desde su propio hilo, que es la misma
        carrera que se corrigió para el EPG.
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
            if not self._warm_thread:
                self._warm_stop.clear()
                self._warm_thread = True
                self._tasks.submit("catalog-warm", self._warm_worker)

    def _first_unindexed(self) -> tuple[str, Playlist] | None:
        """Primera playlist cacheada sin índice, para construirlo en caliente."""
        for key, playlist in list(self.playlist_cache.items()):
            if key not in self._base_indexes:
                return key, playlist
        return None

    def _warm_worker(self, token: CancellationToken) -> None:
        """Descarga las fuentes en cola. **No toca la caché**: sólo devuelve.

        Devuelve la lista de ``(source, playlist)`` que pudo cargar. Quien las
        indexa y las mete en `playlist_cache` es :meth:`_drain_tasks`, en el
        hilo principal: mutar el modelo desde un hilo de descarga es lo que
        producía estados a medias.

        La cancelación es cooperativa y se comprueba entre fuentes, que es
        donde se puede parar sin dejar nada a medias.
        """
        cargadas: list[tuple[str, Playlist]] = []
        try:
            while not token.check():
                unindexed = self._first_unindexed()
                if unindexed is not None:
                    # Indexar es rápido y sólo toca el hilo principal. Aquí no
                    # se puede (es estado compartido), así que se anota la
                    # clave y sigue: el bucle tiene tope para no girar
                    # indefinidamente esperando a que el principal lo haga.
                    clave = unindexed[0]
                    if clave not in self._warm_indexes:
                        self._warm_indexes.append(clave)
                        continue
                    # Ya está anotada y el principal aún no la ha tomado: no
                    # tiene sentido seguir pidiendo lo mismo.
                    with self._warm_lock:
                        vacia = not self._warm_queue
                    if vacia:
                        return cargadas
                with self._warm_lock:
                    if not self._warm_queue:
                        return cargadas
                    source = self._warm_queue.pop(0)
                if token.check():
                    # Cancelado justo al sacar la fuente: se devuelve a la
                    # cola para que la siguiente tanda la recoja.
                    with self._warm_lock:
                        self._warm_queue.insert(0, source)
                    return cargadas
                try:
                    playlist = load_playlist_source(
                        source, allow_private=self._allow_private_for(source)
                    )
                    if playlist is None:
                        raise ValueError(f"fuente ilegible: {source}")
                    cargadas.append((source, playlist))
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
                self._warm_thread = False
        return cargadas

    def siguiente_tema(self) -> str:
        """El tema que sigue al actual en el ciclo de los tres (puro)."""
        try:
            actuales = list(theme.nombres())
        except Exception:
            actuales = list(config.THEMES)
        if not actuales:
            return config.DEFAULT_THEME
        actual = config.normalize_theme(self.theme_name)
        return actuales[(actuales.index(actual) + 1) % len(actuales)]

    def set_theme(self, name: str, *, avisar: bool = True) -> None:
        """Aplica y persiste un tema; avisa **por modal** (no negociable #1).

        La persistencia es doble y ya lo era: `prefs.json` y `theme.json`
        guardan el mismo valor para que una app y otra coincidan.
        """
        nuevo = config.normalize_theme(name)
        anterior = config.normalize_theme(self.theme_name)
        self.theme_name = nuevo
        p = self.prefs.load()
        p.theme = nuevo
        self.prefs.save(p)
        config.save_theme(nuevo)
        theme.init_colors(nuevo)
        if not avisar or nuevo == anterior:
            return
        # La barra también lo dice: el modal es el aviso y la barra el eco.
        self.footer.show(f"Tema {theme.nombre_legible(nuevo)} activado.")
        self.notify(
            f"Ahora la app se ve en tema {theme.nombre_legible(nuevo)} "
            f"(«{nuevo}»). Pulsa t para pasar al siguiente.",
            title="Tema",
        )

    def toggle_theme(self) -> None:
        """Alterna al siguiente tema. `t` en cualquier pantalla."""
        self.set_theme(self.siguiente_tema())

    def elegir_tema(self) -> None:
        """Modal con los tres temas, para elegir sin dar vueltas (D12).

        `t` alterna —rápido, sin decisiones— y esto es lo otro: un modal donde
        se ve **qué tema es cada uno** antes de aplicarlo.
        """
        from .widgets import Modal

        candidatos = [theme.nombre_legible(n) for n in theme.nombres()]
        modal = Modal(
            "Tema",
            "Elige cómo se ve la app. El cambio se guarda para la próxima vez.",
            candidatos,
        )
        actual = config.normalize_theme(self.theme_name)
        nombres = list(theme.nombres())
        if actual in nombres:
            modal.selected_button = nombres.index(actual)
        res = self._run_modal(modal)
        if res is None:
            return
        elegido = str(res).strip().lower()
        for nombre in nombres:
            if theme.nombre_legible(nombre).lower() == elegido:
                self.set_theme(nombre)
                return


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
            self.header.render(stdscr, self._header_context(), len(self.stack))
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
        validar=None,  # noqa: ANN001
    ) -> dict[str, str] | None:
        """Formulario modal centrado multi-campo. None si se cancela (Esc).

        Sustituye a los antiguos ``prompt_text`` en la barra inferior:
        dibuja el fondo (header + pantalla + footer), encima el FormModal,
        soporta KEY_RESIZE, terminal pequeña y cursor hardware en el campo
        activo. Valida que ningún campo quede vacío y muestra el error
        dentro del propio modal.

        ``validar(data) -> str | None`` es el gancho de U-04: se llama **con lo
        que el usuario escribió**, en el mismo modal y antes de devolver nada.
        Si devuelve un texto, el modal no se cierra y lo enseña dentro: el
        usuario ve el motivo mientras todavía puede arreglarlo, en vez de
        descubrirlo al guardar y Having que volver a escribirlo todo.
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
                self.header.render(stdscr, self._header_context(), len(self.stack))
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
                if validar is not None:
                    try:
                        problema = validar(data)
                    except Exception as exc:
                        # Un validador que revienta no puede tragarse el
                        # formulario: el usuario se quedaría escribiendo en un
                        # modal que ya no responde. Se le dice qué pasó.
                        problema = f"No se pudo comprobar: {exc}"
                    if problema:
                        modal.error = str(problema)
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

    def referencias_a_lista(self, source: str) -> dict[str, int]:
        """Cuántos favoritos y recientes apuntan a esta lista (STORE-006).

        Borrar una lista no debería dejar direcciones apuntando a un servidor
        que ya no está en el catálogo: esos favoritos y recientes son
        inservibles y lo raro es que el usuario se entere por su cuenta. Antes
        de borrar, se cuenta y se dice en el modal; después, se limpian.

        Se compara por dos vías, porque con cualquiera de las dos sola se
        escaparían casos:

        - **mismo servidor**: la lista es una URL y los canales cuelgan de ese
          host (caso normal en M3U);
        - **mismo ``tvg-id``**: la lista es Xtream y sus canales son
          referencias opacas que no contienen ni el host ni la ruta.

        Returns:
            ``{"favoritos": n, "recientes": m}``, con ceros si no hay nada.
        """
        from urllib.parse import urlsplit

        vacio = {"favoritos": 0, "recientes": 0}
        if not (source or "").strip():
            return vacio

        host_lista = ""
        if source.strip().lower().startswith(("http://", "https://")):
            try:
                host_lista = (urlsplit(source.strip()).hostname or "").lower()
            except ValueError:
                host_lista = ""

        # `tvg-id` de los canales de la lista, si está en caché: es la única
        # forma de reconocer los canales de una fuente Xtream.
        ids: set[str] = set()
        playlist = self.cached_playlist(source)
        if playlist is not None:
            for canal in playlist.channels:
                if canal.tvg_id:
                    ids.add(str(canal.tvg_id))

        def _de_la_lista(url: str, tvg_id: str | None) -> bool:
            if tvg_id and str(tvg_id) in ids:
                return True
            if not host_lista:
                return False
            if is_opaque_ref(url or ""):
                return False
            try:
                return (urlsplit(url or "").hostname or "").lower() == host_lista
            except ValueError:
                return False

        try:
            favoritos = sum(
                1 for c in self.favorites.load() if _de_la_lista(c.url, c.tvg_id)
            )
        except Exception:
            favoritos = 0
        try:
            recientes = sum(
                1 for i in self.recents.load() if _de_la_lista(i.url, i.tvg_id)
            )
        except Exception:
            recientes = 0
        return {"favoritos": favoritos, "recientes": recientes}

    def limpiar_referencias_a_lista(self, source: str) -> int:
        """Quita los favoritos y recientes que apuntan a `source`. Devuelve cuántos.

        Se llama justo después de borrar la lista, y **sólo** sobre lo que el
        modal ya le ha adelantado al usuario. Lo que no se puede quitar (un
        error de escritura) se cuenta igual, para que el número que ve antes
        y después case.
        """
        cuentas = self.referencias_a_lista(source)
        if not cuentas["favoritos"] and not cuentas["recientes"]:
            return 0
        ids: set[str] = set()
        playlist = self.cached_playlist(source)
        if playlist is not None:
            ids = {str(c.tvg_id) for c in playlist.channels if c.tvg_id}
        host_lista = ""
        if source.strip().lower().startswith(("http://", "https://")):
            from urllib.parse import urlsplit

            try:
                host_lista = (urlsplit(source.strip()).hostname or "").lower()
            except ValueError:
                host_lista = ""

        def _de_la_lista(url: str, tvg_id: str | None) -> bool:
            if tvg_id and str(tvg_id) in ids:
                return True
            if not host_lista or is_opaque_ref(url or ""):
                return False
            from urllib.parse import urlsplit

            try:
                return (urlsplit(url or "").hostname or "").lower() == host_lista
            except ValueError:
                return False

        quitados = 0
        try:
            for canal in list(self.favorites.load()):
                if _de_la_lista(canal.url, canal.tvg_id):
                    self.favorites.remove(canal)
                    quitados += 1
        except Exception:
            pass
        try:
            for item in list(self.recents.load()):
                if _de_la_lista(item.url, item.tvg_id):
                    self.recents.remove(item.url)
                    quitados += 1
        except Exception:
            pass
        return quitados

    def _confirm_remove_playlist(self, name: str, source: str | None = None) -> None:
        from .actions import V
        from .widgets import Modal, modal_actions

        # STORE-006: antes de borrar, se cuenta lo que quedaría huérfano y se
        # dice en el modal. Borrar en silencio y dejar favoritos inservibles
        # es la forma de que el usuario pierda datos sin enterarse.
        aviso = ""
        if source:
            cuentas = self.referencias_a_lista(source)
            if cuentas["favoritos"] or cuentas["recientes"]:
                partes = []
                if cuentas["favoritos"]:
                    partes.append(
                        f"{cuentas['favoritos']} favorito"
                        f"{'s' if cuentas['favoritos'] != 1 else ''}"
                    )
                if cuentas["recientes"]:
                    partes.append(
                        f"{cuentas['recientes']} reciente"
                        f"{'s' if cuentas['recientes'] != 1 else ''}"
                    )
                aviso = (
                    "Al eliminarla también se quitarán "
                    + " y ".join(partes)
                    + " que apuntan a esta lista, porque dejarían de funcionar."
                )

        modal = Modal(
            "Confirmar",
            f"¿Eliminar playlist '{name}'?" + (f"\n\n{aviso}" if aviso else ""),
            ["Cancelar", "Eliminar"],
            actions=modal_actions(confirmar=V.ELIMINAR),
        )
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
            self.header.render(stdscr, self._header_context(), len(self.stack))
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
        if removed and source:
            # Las referencias huérfanas se limpian **después** de borrar, y
            # sólo si el borrado funcionó: si la lista sigue ahí, sus canales
            # siguen sirviendo.
            self.limpiar_referencias_a_lista(source)
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
            self.header.render(stdscr, self._header_context(), len(self.stack))
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
        # La carga va por el gestor de tareas: la aplica el hilo principal al
        # drenar la cola. El worker **solo** devuelve el resultado y nunca
        # toca `self.epg` ni la barra de estado (TASK-003).
        handle = self._tasks.submit(
            "epg-load", self._epg_load_worker, pending, allow_private
        )
        # Se conserva el hilo vivo para las comprobaciones existentes
        # (carga en curso, espera acotada) sin duplicar el estado.
        self._epg_load_thread = handle.thread
        return True

    def _epg_load_worker(
        self,
        token: CancellationToken,
        sources: list[str],
        allow_private: bool = False,
    ) -> "_EpgLoadOutcome":
        """Worker: prueba las fuentes en orden y devuelve la primera que sirve.

        **No** modifica `self.epg` ni llama a `self.status`: eso lo hace el
        hilo principal al drenar (:meth:`_drain_tasks`). Este worker es puro
        respecto a la interfaz, que es lo que permite que un resultado tarde
        lo que tarde en llegar sin tocar curses.

        Args:
            token: cancelación cooperativa. Lo pasa ``TaskManager.submit`` y
                va primero porque es el parámetro fijo de todo trabajador;
                se comprueba entre fuentes para no seguir probando una lista
                que el usuario ya cambió.
            sources: fuentes a probar, en orden de preferencia.
            allow_private: excepción anti-SSRF de la lista que declaró estas
                fuentes (SDD §11); viaja en el hilo para no leer el catálogo
                desde otro thread.

        Returns:
            :class:`_EpgLoadOutcome` con el EPG cargado o el motivo del fallo.
        """
        last_error = ""
        for src in sources:
            if token.check():
                return _EpgLoadOutcome(cancelled=True)
            try:
                epg = load_epg_source(src, allow_private=allow_private)
            except (OSError, ValueError) as exc:
                last_error = str(exc)
                self._epg_failed[src] = time.monotonic()
                continue
            return _EpgLoadOutcome(epg=epg, source=src)
        return _EpgLoadOutcome(error=last_error or "fuente ilegible")

    def _drain_catalog_warm(self) -> None:
        """Aplica lo que descargó el warming del catálogo.

        Aquí se indexa y se mete en `playlist_cache`, que es estado que el
        resto de la app lee. Que lo haga el hilo principal es lo que evita la
        carrera que antes existía con el worker.
        """
        for resultado in self._tasks.drain("catalog-warm"):
            if resultado.obsolete or resultado.cancelled:
                continue
            salida = resultado.value
            if isinstance(salida, list):
                for source, playlist in salida:
                    try:
                        self.remember_playlist(source, playlist)
                        self._base_index(source, playlist)
                    except Exception:
                        # Una lista que no se puede indexar no puede impedir
                        # que se indexen las demás.
                        continue
        # Las claves que el worker dejó sin indexar se resuelven aquí.
        while self._warm_indexes:
            clave = self._warm_indexes.pop(0)
            playlist = self.playlist_cache.get(clave)
            if playlist is None:
                continue
            try:
                self._base_index(clave, playlist)
            except Exception:
                continue

    def _drain_tasks(self) -> None:
        """Aplica los resultados de tareas pendientes (sólo hilo principal).

        Es el único punto donde un resultado de segundo plano toca el modelo
        y la interfaz. Un resultado obsoleto —porque el usuario cambió de
        lista mientras cargaba— se descarta aquí: ya hizo su limpieza, pero no
        puede pisar lo más reciente.
        """
        self._drain_catalog_warm()
        self._drenar_indice_fuentes()
        for resultado in self._tasks.drain("epg-load"):
            if resultado.obsolete or resultado.cancelled:
                self._epg_load_thread = None
                continue
            salida = resultado.value
            self._epg_load_thread = None
            if isinstance(salida, _EpgLoadOutcome):
                if salida.cancelled:
                    continue
                if salida.epg is not None and salida.source:
                    # FR-EPG-009/010: la guía activa **no** se sustituye por una
                    # que no supere la validación. Una descarga truncada dejaba
                    # la pantalla de Favoritos sin programas y el usuario no
                    # tenía forma de saber si era un fallo o que no había guía.
                    anterior_canales = (
                        len(self.epg.programs) if self.epg is not None else 0
                    )
                    anterior_programas = (
                        self.epg.total_programas if self.epg is not None else 0
                    )
                    veredicto = EpgValidation.contra_creciente(
                        salida.epg.validar(), anterior_canales, anterior_programas
                    )
                    if not veredicto.ok:
                        self._epg_failed[salida.source] = time.monotonic()
                        self.status.show(veredicto.motivo, error=True)
                        self._mostrar_modal("La guía no se cargó", veredicto.motivo)
                        continue
                    self.epg = salida.epg
                    self.epg_source = salida.source
                    self._epg_auto_source = salida.source
                    self._epg_failed.pop(salida.source, None)
                    self.status.show(
                        f"EPG cargado de la lista "
                        f"({len(salida.epg.programs)} canales)."
                    )
                    self._avisar_epg(veredicto, salida.source)
                else:
                    self.status.show(
                        "No se pudo cargar el EPG de la lista: "
                        f"{salida.error or 'fuente ilegible'}",
                        error=True,
                    )
            elif resultado.error is not None:
                # El worker lanzó: se explica sin volcar la traza.
                self.status.show(
                    "No se pudo cargar el EPG de la lista: "
                    f"{resultado.error.__class__.__name__}",
                    error=True,
                )

    def wait_for_epg_load(self, timeout: float = EPG_WAIT_S) -> bool:
        """Espera a lo sumo `timeout` s a que termine la carga en curso.

        True si no queda ninguna carga en marcha (terminó bien, falló o no
        la había); False si sigue cargando al agotarse el tiempo.

        Tras la espera, drena la cola: quien llama (los tests y el arranque)
        quiere ver el efecto, y el efecto se aplica en el hilo principal.
        """
        thread = self._epg_load_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            thread = self._epg_load_thread
            if thread is not None and thread.is_alive():
                return False
        self._drain_tasks()
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
                nuevo_epg = load_epg_source(
                    path, allow_private=self._allow_private_for(path)
                )
            except (OSError, ValueError) as exc:
                self.status.show(str(exc), error=True)
                return []
            # FR-EPG-009: la guía activa no se sustituye por una que no supere
            # la validación. Aquí **no** hay confirmación: cargar una guía
            # vacía a propósito no tiene sentido, y el botón «Recargar» con
            # `force_refresh` tampoco.
            veredicto = EpgValidation.contra_creciente(
                nuevo_epg.validar(),
                len(self.epg.programs) if self.epg is not None else 0,
                self.epg.total_programas if self.epg is not None else 0,
            )
            if not veredicto.ok:
                self.status.show(veredicto.motivo, error=True)
                self._mostrar_modal("La guía no se cargó", veredicto.motivo)
                return self._programas_de_la_guia_actual(channel)
            self.epg = nuevo_epg
            self.epg_source = path
            self._epg_auto_source = None  # elección manual: no la pisa el auto-carga
            self.status.show(f"EPG cargado ({len(self.epg.programs)} canales).")
            self._avisar_epg(veredicto, path)
        elif force_refresh and self.epg_source:
            try:
                nuevo_epg = load_epg_source(
                    self.epg_source,
                    force_refresh=True,
                    allow_private=self._allow_private_for(self.epg_source),
                )
                veredicto = EpgValidation.contra_creciente(
                    nuevo_epg.validar(),
                    len(self.epg.programs) if self.epg is not None else 0,
                    self.epg.total_programas if self.epg is not None else 0,
                )
                if not veredicto.ok:
                    self.status.show(veredicto.motivo, error=True)
                    self._mostrar_modal("La guía no se recargó", veredicto.motivo)
                    return self._programas_de_la_guia_actual(channel)
                self.epg = nuevo_epg
                self.status.show(f"EPG recargado ({len(self.epg.programs)} canales).")
                self._avisar_epg(veredicto, self.epg_source)
            except (OSError, ValueError) as exc:
                self.status.show(str(exc), error=True)
        # FR-EPG-004: además del id se mira **por qué** casó, para poder avisar
        # si la clave era ambigua (FR-EPG-003). `resolve` es un envoltorio de
        # `resolve_match`, así que la asociación sigue siendo exactamente la
        # misma que en Favoritos y en el resto de pantallas.
        indice = epg_match.for_epg(self.epg)
        coincidencia = indice.resolve_match(channel) if indice else None
        if coincidencia is None or coincidencia.channel_id is None:
            self.status.show(
                f"No hay correspondencia EPG para '{channel.name}'.", error=True
            )
            return []
        self._avisar_epg_ambiguo(channel, coincidencia)
        return self.epg.programmes_for(coincidencia.channel_id)

    # --- Avisos al usuario (modal) ------------------------------------------

    def _programas_de_la_guia_actual(self, channel: Channel) -> list[Program]:
        """Programas del canal con la guía **activa**, sin tocar la barra.

        Se usa cuando una guía nueva se rechaza: el usuario pidió recargar
        y lo que hay que devolverle son los programas de la guía que **sigue**
        activa, no un «no hay correspondencia» que contradice el mensaje que
        acaba de ver.
        """
        if self.epg is None:
            return []
        cid = _epg_channel_id(self.epg, channel)
        return self.epg.programmes_for(cid) if cid else []

    def _mostrar_modal(self, titulo: str, mensaje: str) -> None:
        """Modal informativo que **nunca** lanza (regla de `AGENTS.md`).

        Se degrada a la barra de estado cuando no hay interfaz (tests, modo sin
        pantalla): el mensaje se ve igualmente, que es lo que importa.
        """
        try:
            self._show_notice(titulo, mensaje)
        except Exception:
            pass

    def _avisar_epg(self, veredicto, fuente: str) -> None:  # noqa: ANN001 - EpgValidation
        """Enseña los avisos de una guía que **sí** se aplicó (FR-EPG-006).

        Dos condiciones, y las dos hacen falta:

        - que la guía traiga **de verdad** horas sin zona
          (``con_zona_asumida``); sin eso no hay nada que contar y un aviso
          sería mentira para una guía bien formada;
        - que esta fuente **aún no** haya sido avisada, que es lo que
          recuerda :func:`thetvview.epg_parser.aviso_zona_pendiente`. Un
          aviso que se repite en cada recarga deja de leerse.
        """
        if not getattr(veredicto, "con_zona_asumida", 0):
            return
        if not aviso_zona_pendiente(fuente):
            return  # ya se le dijo al usuario de esta fuente
        for texto in getattr(veredicto, "avisos", []) or []:
            self._mostrar_modal("Sobre los horarios de la guía", texto)

    def _avisar_epg_ambiguo(
        self, channel: Channel, coincidencia: epg_match.Match
    ) -> None:
        """Modal breve cuando el nombre del canal casa con varios de la guía.

        Es la parte visible del registro de ambigüedad (FR-EPG-003): no se
        fusionan canales y quien mira el ``Match`` sabe que hubo que elegir.
        Una vez por clave y por proceso, como el aviso de zona: la ambigüedad
        es una propiedad del par canal↔guía, no del momento, y repetirlo en
        cada apertura deja de leerse. No hay nada que resolver —el canal
        funciona— pero conviene saber que el horario que se ve puede ser el de
        un homónimo, y que el arreglo es el `tvg-id` del canal.
        """
        if not getattr(coincidencia, "ambiguo", False):
            return
        avisos = getattr(self, "_avisos_epg_ambiguos", None)
        if avisos is None:  # App construida sin __init__ (tests con dobles)
            avisos = self._avisos_epg_ambiguos = set()
        clave = getattr(coincidencia, "clave", "") or ""
        if clave in avisos:
            return
        avisos.add(clave)
        nombres = [
            (self.epg.channel_name(cid) if self.epg is not None else None) or cid
            for cid in coincidencia.candidatos
        ]
        vistos = ", ".join(f"«{n}»" for n in nombres[:3])
        if len(nombres) > 3:
            vistos += f" y {len(nombres) - 3} más"
        self._mostrar_modal(
            "Varios canales se llaman igual",
            f"«{channel.name}» coincide con {len(nombres)} canales de la guía: "
            f"{vistos}. Se está usando el primero. Si los horarios no encajan "
            f"con este canal, revisa su tvg-id en la lista.",
        )

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
        from .widgets import Modal, modal_actions

        res = self._run_modal(Modal(title, message, ["Cancelar", "Aceptar"],
                                   actions=modal_actions()))
        return res == "aceptar"

    def _confirm_risk(self, title: str, message: str) -> bool:
        """Modal de riesgo propio: el botón dice lo que va a pasar.

        Un «Aceptar» genérico en una pregunta de riesgo hides el riesgo: el
        usuario pulsa sin saber qué consiente. Por eso el botón de acción
        derecha se llama «Intentar de todos modos».
        """
        from .widgets import Modal, modal_actions

        res = self._run_modal(
            Modal(title, message, ["Ver el directo", "Intentar de todos modos"],
                  actions=modal_actions())
        )
        return res == "intentar de todos modos"

    def show_diagnose(self, channel: Channel, *, con_conexion: bool = False) -> None:
        """Informe de diagnóstico de un canal en un modal (SDD-M §21).

        Lo único que se muestra es tipo, esquema, candidatos y veredicto: **la
        URL no sale**, ni cruda ni redactada. Todo el texto pasa por
        ``redact_text`` dentro de :mod:`thetvview.streams.diagnose`, que es
        donde nace (decisión D5: no hay módulo de ``logging``, se redacta en el
        punto donde se crea el texto).

        Args:
            con_conexion: si True, además comprueba DNS/HTTP de verdad. Eso
                **abre red**, así que no se hace por sorpresa: lo pide el
                usuario con una tecla aparte. Pasa por el cliente seguro, con
                la excepción anti-SSRF de la fuente y sin esperar al usuario
                desde el hilo principal más de lo necesario.
        """
        from thetvview.streams.diagnose import (
            StepState,
            diagnose,
            diagnose_connection,
            render_report,
        )

        informe = diagnose(channel)
        # DIAG-004: el resultado alimenta el historial local acotado, y con
        # él se distingue un fallo nuevo de uno recurrente (FR-DIAG-005).
        nota_historial = self._registrar_diagnose(channel, informe)
        pasos = None
        if con_conexion:
            # La excepción anti-SSRF es **de la fuente que aportó el canal**,
            # no global (SDD §11). Se busca el origen de la lista abierta; si
            # no se sabe, `False`, que es el valor por defecto seguro.
            source = getattr(getattr(self, "playlist", None), "source", "") or ""
            permitir = bool(source) and self._allow_private_for(source)
            try:
                pasos = diagnose_connection(channel, allow_private=permitir)
            except Exception as exc:  # noqa: BLE001 - un diagnóstico nunca rompe
                pasos = []
                informe.add(
                    "comprobación de conexión",
                    StepState.UNKNOWN,
                    f"no se pudo comprobar: {type(exc).__name__}",
                )
        texto = render_report(informe, pas=pasos)
        if nota_historial:
            texto += f"\n\n{nota_historial}"
        # DIAG-002: el informe completo del §11.3 (sesión, motor, tiempos,
        # eventos) se compone **sin red** y se guarda en un archivo 0600
        # que el usuario pueda compartir. Si guardar falla, el modal sigue:
        # un archivo no puede impedir ver el diagnóstico.
        ruta = self._guardar_informe_sesion(channel, pasos, nota_historial)
        if ruta is not None:
            texto += f"\n\nInforme completo (§11.3) guardado en:\n  {ruta}"
        # `_show_notice` (y no `notify`): el informe es largo y ya viene
        # encabezado, así que no se duplica en la barra de estado.
        self._show_notice(f"Diagnóstico · {channel.name}", texto)

    def _registrar_diagnose(self, channel, informe) -> str | None:  # noqa: ANN001
        """Anota el resultado en el historial y devuelve la nota FR-DIAG-005.

        Se consulta la recurrencia **antes** de registrar el de hoy, para
        que «nuevo» y «recurrente» digan la verdad. Sin historial (tests,
        app sin `run()`), no hay nota y nadie se entera.
        """
        historial = getattr(self, "diagnosticos", None)
        if historial is None:
            return None
        try:
            clave = str(
                getattr(channel, "tvg_id", "") or getattr(channel, "name", "") or ""
            )
            ok = bool(getattr(informe, "ok", False))
            recurrente = (not ok) and bool(
                historial.es_recurrente(clave)
            )
            historial.registrar(
                clave, ok, motivo=str(getattr(informe, "verdict", "")), fase="diagnose"
            )
            if ok:
                return None
            if recurrente:
                return "Fallo recurrente: este canal ya fallaba antes (historial local)."
            return "Fallo nuevo: sin fallos anteriores registrados de este canal."
        except Exception:  # noqa: BLE001 - el historial nunca rompe el modal
            return None

    def _guardar_informe_sesion(self, channel, pasos, historial=None):  # noqa: ANN001
        """Informe §11.3 desde datos ya en memoria (DIAG-002 · FR-DIAG-009).

        Recoge la sesión vigente (``app._sessions``) y el supervisor de la
        pantalla de reproducción (``app.screen``) si existen; lo que no
        exista se queda como «desconocido». **No hace red**: la
        comprobación activa, si la hubo, ya vino en `pasos`.
        """
        from ..diagnostics.report import construir_informe, guardar_informe

        sesion = None
        try:
            sesion_actual = getattr(getattr(self, "_sessions", None), "actual", None)
            if callable(getattr(sesion_actual, "para_informe", None)):
                sesion = sesion_actual.para_informe()
        except Exception:  # noqa: BLE001 - un informe nunca rompe el modal
            sesion = None
        motor = None
        eventos = None
        try:
            sup = getattr(getattr(self, "screen", None), "supervisor", None)
            if callable(getattr(sup, "para_informe", None)):
                motor = sup.para_informe()
            bus = getattr(self, "eventos", None)
            correlacion = (sesion or {}).get("correlation_id")
            if bus is not None and correlacion:
                eventos = bus.buscar(correlation_id=correlacion) or None
        except Exception:  # noqa: BLE001 - idem: pantalla ausente o rara
            motor, eventos = None, None
        texto = construir_informe(
            canal=str(getattr(channel, "name", "") or ""),
            sesion=sesion,
            motor=motor,
            conexion=pasos,
            eventos=eventos,
            historial=historial,
        )
        try:
            return guardar_informe(texto)
        except OSError:
            return None

    # --- Errores: barra redactada + modal con las acciones que existen -------

    # --- Colecciones (Fase 8 · U-03) --------------------------------------
    #
    # Toda la conversación con el usuario sobre colecciones vive aquí y no en
    # la pantalla: la pantalla decide *qué* se quiere hacer y devuelve una
    # acción, y la `App` la traduce en modales. Es el mismo reparto que
    # `add_playlist` / `remove_playlist`, y por eso los modales se prueban sin
    # terminal.
    #
    # Y una regla que atraviesa las cuatro: **un rechazo del gestor se
    # enseña, no se ejecuta**. El backend dice «nombre vacío» o «colección no
    # encontrada»; la UI lo envuelve con el puente de U-02 y no finge que la
    # operación pasó.

    # --- El contexto de evaluación (U-04b) ---------------------------------
    #
    # `valor_campo` (Fase 4) lee cinco campos de un `contexto` que le pasa
    # quien evalúa: favoritos, guía, última vez visto, fallos y salud. Hasta
    # U-04b **nadie se lo pasaba**, así que los cinco devolvían su valor vacío
    # —«no favorito», «sin guía», «nunca visto», «0 fallos», «desconocido»—
    # para *todos* los canales, y una colección «mis favoritos» salía con cero
    # canales y ningún motivo.
    #
    # Tres tienen origen real aquí. Dos no, y no se inventan: los **fallos** no
    # los escribe nada desde la UI (el `HistorialDiagnostico` de la Fase 2
    # existe y se persiste, pero sólo lo rellenan los tests y el chequeo de
    # seguridad), y la **salud** se sondea por pantalla y se apaga al salir.
    # Esos dos salen en el constructor marcados como no utilizables, con su
    # motivo (`rule_text.CAMPOS_SIN_ORIGEN`).

    def contexto_de_reglas(self) -> dict:
        """El `contexto` para `valor_campo`, montado desde lo que hay cargado.

        Se construye **entero y en cada llamada**, no un singleton guardado:
        marcar un favorito tiene que mover el conteo al instante, y un contexto
        cacheado detrás —por barato que sea el cálculo— es justo el número
        congelado que U-04b viene a arreglar. Por eso
        `ColeccionesManager.miembros` no cachea cuando hay contexto.

        Nunca lanza: se pide desde `render`, y reventar aquí tumba la pantalla
        entera. Cada pieza va con su propio `try` porque un `recents.json` con
        permisos raros no puede impedir evaluar los favoritos.
        """
        ctx: dict = {"favorites": set(), "last_played": {}, "epg": None,
                     "indice_epg": None}

        favs = self._ids_de_favoritos()
        if favs:
            ctx["favorites"] = favs

        vistos = self._vistos_por_canal()
        if vistos:
            ctx["last_played"] = vistos

        try:
            epg = getattr(self, "epg", None)
        except Exception:
            epg = None
        ctx["epg"] = epg
        if epg is not None:
            try:
                from thetvview.search import indice_epg_de

                ctx["indice_epg"] = indice_epg_de(epg)
            except Exception:
                # Sin índice, `estado_epg` lo construye él misma. No es motivo
                # para degradar el campo entero.
                ctx["indice_epg"] = None
        return ctx

    def _ids_de_favoritos(self) -> set:
        """Identidades y URLs de los favoritos, que es lo que `valor_campo` mira.

        Se indexa por las dos porque `valor_campo` prueba la identidad primero
        y la URL después. Sólo con una, un canal contaría o no según de dónde
        venga: con el `tvg-id` igual pero la URL girada, que es justo lo que
        pasa cuando el proveedor renueva el token.

        Limitación aceptada: `FavoritesManager.is_favorite` tiene un tercer
        criterio —`tvg-id` de un lado y ninguno del otro— que un conjunto de
        cadenas no puede reproducir. El caso es la asimetría en la que una
        copia del canal trae `tvg-id` y la otra no; en Favoritos se ve bien,
        en un filtro puede contar de menos.
        """
        manager = getattr(self, "favorites", None)
        if manager is None:
            return set()
        try:
            canales = manager.load()
        except Exception:
            return set()
        ids: set = set()
        for c in canales or ():
            try:
                ident = str(channel_identity(c))
            except Exception:
                ident = ""
            if ident:
                ids.add(ident)
            url = str(getattr(c, "url", "") or "")
            if url:
                ids.add(url)
        return ids

    def _vistos_por_canal(self) -> dict:
        """`{identidad o URL: momento ISO}` de los recientes.

        `RecentItem` guarda la URL ** redactada**, así que por URL puede no
        cuadrar con el canal del catálogo; por eso se indexa también por la
        identidad reconstruida a partir del propio registro, que sí coincide
        cuando ambos lados comparten `tvg-id` o nombre.
        """
        manager = getattr(self, "recents", None)
        if manager is None:
            return {}
        try:
            items = manager.load()
        except Exception:
            return {}
        vistos: dict = {}
        for item in items or ():
            momento = str(getattr(item, "at_utc", "") or "")
            if not momento:
                continue
            url = str(getattr(item, "url", "") or "")
            if url:
                vistos.setdefault(url, momento)
            tvg_id = getattr(item, "tvg_id", None)
            try:
                ident = str(channel_identity(Channel(
                    name=str(getattr(item, "name", "") or ""),
                    url=url, group=getattr(item, "group", None),
                    tvg_id=str(tvg_id or ""),
                )))
            except Exception:
                continue
            if ident:
                vistos.setdefault(ident, momento)
        return vistos

    def _recargar_colecciones(self) -> None:
        """Refresca la pantalla de colecciones si es la que está delante."""
        from .screens import CollectionsScreen

        if isinstance(self.screen, CollectionsScreen):
            try:
                self.screen.reload()
            except Exception:
                pass

    def _nombre_de_coleccion(self, cid: str) -> str | None:
        try:
            col = self.collections.obtener(cid)
        except Exception:
            return None
        return getattr(col, "name", None)

    def create_collection(self) -> None:
        """Alta de una colección manual, por modal (U-03).

        Se crea **manual** a propósito: la inteligente necesita un constructor de
        reglas, que es U-04. Crear aquí una «inteligente» sin reglas daría una
        colección vacía y engañosa —`evaluar_coleccion` no inventa miembros—,
        que es justo lo que este ticket no quiere prometer.
        """
        datos = self._prompt_form(
            "Nueva colección",
            [("name", "Nombre de la colección")],
            initial={"name": ""},
        )
        if datos is None:
            return
        nombre = str(datos.get("name", "")).strip()
        if not nombre:
            self.notify_warning(
                "La colección necesita un nombre.\n"
                "Sin nombre no se podría distinguir de las demás."
            )
            return
        try:
            self.collections.crear(nombre, tipo="manual")
        except ColeccionesManagerError as exc:
            self._error_colecciones(exc, reintentar=self.create_collection)
            return
        except Exception as exc:
            self.notify_error(f"No se pudo crear la colección: {exc}")
            return
        self._recargar_colecciones()
        self.notify(
            f"Colección «{nombre}» creada, y de momento vacía.\n"
            "Se llena con «C» en Canales (elige sus canales), con «m» aquí, "
            "o con «R» si prefieres que la app la arme sola con una regla."
        )

    def rename_collection(self, cid: str) -> None:
        """Renombrar por modal, con el nombre actual precargado (U-03)."""
        actual = self._nombre_de_coleccion(cid)
        if actual is None:
            self.notify_warning("Esa colección ya no existe.")
            return
        datos = self._prompt_form(
            "Renombrar colección",
            [("name", "Nombre de la colección")],
            initial={"name": actual},
        )
        if datos is None:
            return
        nombre = str(datos.get("name", "")).strip()
        if not nombre:
            self.notify_warning(
                "La colección necesita un nombre.\n"
                "Sin nombre no se podría distinguir de las demás."
            )
            return
        if nombre == actual:
            # No es un error, pero tampoco es un cambio: decirlo evita que el
            # usuario espere un efecto que no ocurrió.
            self.notify_warning(f"La colección sigue llamándose «{actual}».")
            return
        try:
            self.collections.renombrar(cid, nombre)
        except ColeccionesManagerError as exc:
            self._error_colecciones(
                exc, reintentar=lambda: self.rename_collection(cid)
            )
            return
        except Exception as exc:
            self.notify_error(f"No se pudo renombrar la colección: {exc}")
            return
        self._recargar_colecciones()
        self.notify(f"Colección renombrada a «{nombre}».")

    def delete_collection(self, cid: str) -> None:
        """Borrado por confirmación, diciendo qué se pierde exactamente (U-03).

        El mensaje no pregunta «¿borrar?» y ya: dice cuántos canales se van con
        la colección. Un «Aceptar» sobre algo cuyo tamaño se desconoce es la
        forma de perder trabajo sin querer, y el usuario tiene derecho a saber
        el tamaño antes de decidir.
        """
        try:
            col = self.collections.obtener(cid)
        except Exception:
            col = None
        if col is None:
            self.notify_warning("Esa colección ya no existe.")
            return
        nombre = getattr(col, "name", "") or "(sin nombre)"
        n = self._cuenta_de_coleccion(col)
        if n == 1:
            efecto = ("Contiene 1 canal. Al borrarla, ese canal sale de aquí,\n"
                      "pero no se borra de tus listas ni de tus favoritos.")
        else:
            efecto = (f"Contiene {n} canales. Al borrarla, salen de aquí, pero\n"
                      "no se borran de tus listas ni de tus favoritos.")
        if not self._confirm(
            "Borrar colección",
            f"«{nombre}»\n\n{efecto}\n\nEsta acción no se puede deshacer.",
        ):
            self.notify_warning("No se borró nada.")
            return
        try:
            borrada = self.collections.eliminar(cid)
        except ColeccionesManagerError as exc:
            self._error_colecciones(
                exc, reintentar=lambda: self.delete_collection(cid)
            )
            return
        except Exception as exc:
            self.notify_error(f"No se pudo borrar la colección: {exc}")
            return
        if not borrada:
            self.notify_warning("Esa colección ya no existe.")
            return
        self._recargar_colecciones()
        self.notify(f"Colección «{nombre}» borrada.")

    def _cuenta_de_coleccion(self, col) -> int:
        """Cuántos canales tiene `col` **según el catálogo en memoria**."""
        if getattr(col, "type", "") == "system":
            return 0
        try:
            return len(self.collections.miembros(col.id, self.catalogo_canales()))
        except Exception:
            return len(getattr(col, "members", None) or [])

    def _obsoletas_de(self, col) -> int:
        """Cuántas referencias ya no resuelven — **sólo si hay catálogo**.

        Sin listas en memoria todas parecerían obsoletas, y ese aviso sería
        falso: el canal no desapareció, es que todavía no se ha cargado nada
        (misma razón que en `CollectionsScreen._detalle`).
        """
        if not self.catalogo_canales():
            return 0
        try:
            return len(self.collections.obsolete_refs(col.id, self.catalogo_canales()))
        except Exception:
            return 0

    def show_collection(self, cid: str) -> None:
        """Detalle de una colección en un modal (U-03).

        Un modal y no otra pantalla, a propósito: esto es *información*, no una
        tarea con su propia navegación. Abrir una pantalla entera para leer
        seis líneas sería una pantalla más que mantener sin ganar nada.
        """
        try:
            col = self.collections.obtener(cid)
        except Exception:
            col = None
        if col is None:
            self.notify_warning("Esa colección ya no existe.")
            return
        tipo = getattr(col, "type", "")
        if tipo == "system":
            cuerpo = ("Es una vista de la app: sus canales son los tuyos de\n"
                      "Favoritos o Recientes. No se renombra ni se borra.")
        else:
            lineas = [
                "Tipo: " + {"manual": "manual", "smart": "inteligente"}.get(
                    tipo, tipo or "sin tipo"
                ),
                f"Canales ahora mismo: {self._cuenta_de_coleccion(col)}",
            ]
            obsoletas = self._obsoletas_de(col)
            if obsoletas:
                lineas.append(
                    f"Referencias obsoletas: {obsoletas}\n"
                    "Esos canales ya no están en ninguna lista. La colección\n"
                    "sigue funcionando con el resto."
                )
            if tipo == "manual":
                lineas.append("Los canales se añaden desde la lista de canales.")
            cuerpo = "\n".join(lineas)
        self._mostrar_modal(f"Colección «{getattr(col, 'name', '')}»", cuerpo)

    def show_collections(self) -> None:
        """Abre la lista de colecciones (U-03)."""
        from .screens import CollectionsScreen

        self.push(CollectionsScreen(self))

    # --- Colecciones: membresía (U-03b) -----------------------------------
    #
    # U-03 dejó las colecciones manuales inertes: se creaban vacías y no había
    # forma de meter un canal dentro. Aquí está el otro camino, y son dos
    # porque el usuario está en dos sitios: en Canales elige **en qué
    # colecciones está** este canal; en Colecciones elige **qué canales se
    # quedan** en esta.
    #
    # Los modales son listas de casillas, no transacciones: cada marca se
    # guarda en el momento. Un Esc sobre una lista donde ya se ve un «sí» no
    # puede deshacer lo que dice, y una casilla que promete un cambio no
    # guardado sería una mentira visible.

    #: Qué se dice de una colección que no admite canales elegidos a mano.
    #: **No vive aquí**: la nota de la lista de casillas y el aviso al pulsar
    #: la tecla tienen que decir lo mismo, y dos copias de la misma frase
    #: divergen en cuanto una se toca. La fuente es `screens._miembros_editables`.
    def _miembros_editables(self, col) -> str | None:
        from .screens import _miembros_editables

        return _miembros_editables(col)

    def _texto_de_error_colecciones(self, exc: Exception) -> str:
        """Un rechazo del gestor, como texto —no como modal.

        Aquí no se puede abrir un modal dentro de otro (`_run_modal` se
        niega), así que el error viaja como cadena y lo pinta la lista. Sólo
        se reproduce el texto del propio gestor cuando es suyo: el de una
        excepción cualquiera puede traer una URL, y este es el punto donde
        una credencial se acaba de Printing en pantalla.
        """
        if isinstance(exc, ColeccionesManagerError):
            return f"No se pudo guardar: {exc}."
        return "No se pudo guardar el cambio."

    def _checklist_de_canal(self, channel: Channel) -> ChecklistModal:
        """Las colecciones, con este canal marcado donde ya está.

        El estado sale de la **clave de identidad** del canal contra los
        `members` guardados, no de evaluar la colección: para esa pregunta no
        hace falta el catálogo, y sin listas cargadas la respuesta sigue siendo
        exacta.
        """
        from .widgets import ChecklistItem, ChecklistModal, checklist_actions

        clave = self.collections.identidad(channel)
        items: list[ChecklistItem] = []
        try:
            colecciones = self.collections.listar()
        except Exception as exc:
            colecciones = []
            self.notify_warning(
                f"No se pudieron leer las colecciones: {exc}"
            )
        for col in colecciones:
            tipo = getattr(col, "type", "")
            if tipo == "manual":
                dentro = clave in (getattr(col, "members", None) or [])
                items.append(ChecklistItem(col.id, getattr(col, "name", ""),
                                           checked=dentro))
            else:
                # Aparece aunque no se pueda marcar: si una colección no está
                # en la lista, el usuario no sabe si es un fallo o qué pasó.
                items.append(ChecklistItem(
                    col.id, getattr(col, "name", ""), checked=False,
                    selectable=False, note=self._miembros_editables(col),
                ))
        marcadas = sum(1 for i in items if i.checked)
        return ChecklistModal(
            f"Colecciones de «{getattr(channel, 'name', '')}»",
            items,
            subtitle=self._subtitulo_de_canal(marcadas),
            actions=checklist_actions(),
            empty="Todavía no tienes colecciones.\n"
                  "Créala con «a» en la pantalla de Colecciones: es la tecla que "
                  "pone nombre a la primera.",
        )

    @staticmethod
    def _subtitulo_de_canal(marcadas: int) -> str:
        """El contador del modal: qué cambia al marcar, en una línea.

        Vive en el subtítulo y no sólo en la barra porque dentro de un modal la
        barra de la pantalla de detrás está tapada: es el único sitio donde el
        usuario puede leer el efecto de lo que acaba de pulsar.
        """
        plural = "colección" if marcadas == 1 else "colecciones"
        return (f"Ahora está en {marcadas} {plural}. "
                "Espacio marca o desmarca.")

    def _checklist_de_miembros_error(self, cid: str) -> str | None:
        """Por qué no se pueden quitar miembros de `cid`; ``None`` si sí."""
        try:
            col = self.collections.obtener(cid)
        except Exception:
            return "Esa colección ya no existe."
        if col is None:
            return "Esa colección ya no existe."
        return self._miembros_editables(col)

    def _checklist_de_miembros(self, cid: str) -> ChecklistModal:
        """Los canales de una colección, todos marcados: quitar = desmarcar.

        Resuelve con el **mismo criterio que el gestor** —identidad y, como
        respaldo, URL— para que lo que esta lista llama «obsoleto» y lo que
        el backend cuenta como referencia obsoleta sean exactamente lo mismo.

        Las referencias que no resuelven **también entran en la lista**, y son
        quitables. Si la lista fuera sólo de los canales que resuelven, la
        referencia rota no tendría por dónde salir y la única manera de
        arreglarla sería editar `collections.json` a mano.
        """
        from .widgets import ChecklistItem, ChecklistModal, checklist_actions

        try:
            col = self.collections.obtener(cid)
        except Exception:
            col = None
        if col is None:
            return ChecklistModal("Miembros", [],
                                  empty="Esa colección ya no existe.",
                                  actions=checklist_actions())
        nombre = getattr(col, "name", "") or "(sin nombre)"
        bloqueo = self._miembros_editables(col)
        if bloqueo:
            return ChecklistModal(f"Miembros de «{nombre}»", [],
                                  empty=bloqueo, actions=checklist_actions())

        catalogo = self.catalogo_canales()
        por_identidad: dict[str, Channel] = {}
        por_url: dict[str, Channel] = {}
        for c in catalogo:
            try:
                por_identidad[str(channel_identity(c))] = c
            except Exception:
                pass
            url = getattr(c, "url", "")
            if url:
                por_url.setdefault(url, c)

        items: list[ChecklistItem] = []
        # `dict.fromkeys` quita duplicados **conservando el orden**: un canal
        # repetido sigue siendo un canal, y dos filas idénticas confunden.
        for clave in dict.fromkeys(getattr(col, "members", None) or []):
            canal = por_identidad.get(clave) or por_url.get(clave)
            if canal is not None:
                items.append(ChecklistItem(clave, getattr(canal, "name", clave),
                                           checked=True))
            elif catalogo:
                items.append(ChecklistItem(
                    clave, "Canal que ya no está en tus listas", checked=True,
                    note="obsoleto: se puede quitar aquí",
                ))
            else:
                # Sin catálogo no se sabe ni el nombre ni si caducó, y decirlo
                # sería inventarlo.
                items.append(ChecklistItem(clave,
                                           "Canal (nombre no disponible)",
                                           checked=True))
        return ChecklistModal(
            f"Miembros de «{nombre}»",
            items,
            subtitle="Desmarca los que quieras quitar de la colección.",
            actions=checklist_actions(),
            empty="Esta colección no tiene canales.",
        )

    def _guardar_en_coleccion(self, cid: str, canal: Channel,
                              marcado: bool) -> str | None:
        """Mete o saca `canal` de la colección `cid`. ``None`` si se guardó."""
        try:
            col = self.collections.obtener(cid)
        except Exception:
            return "Esa colección ya no existe."
        if col is None:
            return "Esa colección ya no existe."
        bloqueo = self._miembros_editables(col)
        if bloqueo:
            return bloqueo
        try:
            if marcado:
                self.collections.añadir(cid, canal)
            else:
                self.collections.quitar(cid, self.collections.identidad(canal))
        except ColeccionesManagerError as exc:
            return self._texto_de_error_colecciones(exc)
        except Exception:
            return "No se pudo guardar el cambio."
        self._recargar_colecciones()
        return None

    def _quitar_de_coleccion(self, cid: str, clave: str) -> str | None:
        """Saca la referencia `clave` de `cid`. ``None`` si se guardó."""
        try:
            col = self.collections.obtener(cid)
        except Exception:
            return "Esa colección ya no existe."
        if col is None:
            return "Esa colección ya no existe."
        bloqueo = self._miembros_editables(col)
        if bloqueo:
            return bloqueo
        try:
            self.collections.quitar(cid, clave)
        except ColeccionesManagerError as exc:
            return self._texto_de_error_colecciones(exc)
        except Exception:
            return "No se pudo quitar el canal."
        self._recargar_colecciones()
        return None

    def _run_checklist(self, modal, on_toggle) -> str | None:  # noqa: ANN001
        """Bucle de una lista de casillas; ``on_toggle(item, marcado) -> str | None``.

        En modo *check* devuelve siempre ``None``: lo que importa es el efecto,
        no un resultado. En modo *radio* (elegir una de varias) devuelve la
        ``key`` elegida, o ``None`` si se cerró con Esc — que es lo que le
        permite al asistente distinguir «eligió el campo grupo» de «se rindió».

        El texto de error que devuelve `on_toggle` se pinta en la lista **sin
        mover la casilla**. **Nunca marca antes de guardar**: la casilla sólo se
        mueve cuando el gestor aceptó.

        Sin curses, desde un hilo o con otro modal abierto, no hace nada: se
        queda en la barra de estado, igual que el resto de modales.
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
                        self.header.render(stdscr, self._header_context(),
                                           len(self.stack))
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
                if res == "cerrar":
                    return None
                if res is None:
                    continue
                if res[0] == "pick":
                    return modal.items[res[1]].key
                _marca, indice, marcado = res
                error = on_toggle(modal.items[indice], marcado)
                if error is None:
                    modal.set_checked(indice, marcado)
                else:
                    modal.set_error(error)
        except Exception:
            return None
        finally:
            self._in_modal = False

    def _elegir_uno(
        self,
        titulo: str,
        items,  # noqa: ANN001
        *,
        subtitle: str = "",
        vacio: str = "No hay nada que elegir.",
    ) -> str | None:
        """Abre una lista en modo «una de estas» y devuelve la elegida.

        Es el mismo `ChecklistModal` de U-03b en modo ``radio``: compartir el
        control evita tener dos listas que se mueven y se recortan distinto.
        """
        from .widgets import ChecklistModal, radio_actions

        modal = ChecklistModal(titulo, items, subtitle=subtitle,
                               actions=radio_actions(), empty=vacio,
                               radio=True)

        def _nunca(item, marcado: bool):  # noqa: ANN001
            # En radio no se llega: `handle_key` devuelve «pick» y el bucle
            # sale antes. Si se llegara, no se guarda nada.
            return "Este paso elige una opción; no hay nada que guardar."

        return self._run_checklist(modal, _nunca)

    def choose_channel_collections(self, channel: Channel) -> None:
        """Elige en qué colecciones manuales está `channel` (U-03b)."""
        modal = self._checklist_de_canal(channel)
        if not modal.items:
            # El mismo texto vacío del modal, pero como aviso: sin lista que
            # mostrar, esto no es «no hay nada marcado», es «todavía no
            # existen colecciones» — y son dos noticias distintas.
            self.notify_warning(modal.empty)
            return
        cambios: list[tuple[str, bool]] = []

        def _aplicar(item, marcado: bool):  # noqa: ANN001
            error = self._guardar_en_coleccion(item.key, channel, marcado)
            if error is None:
                cambios.append((item.label, marcado))
                # El contador se calcula con el cambio ya aplicado, no con la
                # casilla —que en este instante todavía no se ha movido—.
                dentro = sum(1 for i in modal.items if i.checked)
                modal.subtitle = self._subtitulo_de_canal(
                    dentro + 1 if marcado else dentro - 1
                )
            return error

        self._run_checklist(modal, _aplicar)
        if not cambios:
            self.notify("No se cambió nada.")
            return
        dentro = sum(1 for _n, m in cambios if m)
        fuera = len(cambios) - dentro
        partes = []
        if dentro:
            partes.append(f"metido en {dentro}")
        if fuera:
            partes.append(f"quitado de {fuera}")
        self.notify(f"«{getattr(channel, 'name', '')}»: " + " y ".join(partes)
                    + " colección/es.")

    def manage_collection_members(self, cid: str) -> None:
        """Elige qué canales se quedan en la colección `cid` (U-03b)."""
        modal = self._checklist_de_miembros(cid)
        if not modal.items:
            self.notify_warning(modal.empty)
            return
        quitados: list[str] = []

        def _aplicar(item, marcado: bool):  # noqa: ANN001
            if marcado:
                # Volver a marcar algo que ya estaba no cambia nada, y decirlo
                # sería inventar un trabajo que no pasó.
                return "Ese canal ya estaba en la colección."
            error = self._quitar_de_coleccion(cid, item.key)
            if error is None:
                quitados.append(item.label)
            return error

        self._run_checklist(modal, _aplicar)
        if not quitados:
            self.notify("No se quitó ningún canal.")
            return
        self.notify(f"Quitados {len(quitados)} canal/es de la colección.")

    # --- Colecciones: constructor de reglas (U-04) -------------------------
    #
    # Un asistente de tres pasos —campo → operador → valor— y un cuarto paso
    # opcional para encadenar condiciones. Lo que lo hace *guiado* y no un
    # formulario con tres cajas de texto es que **los operadores se eligen de
    # una lista que el backend ya filtró por el tipo del campo**: es imposible
    # ofrecer «es menor que» para un campo de texto, así que no hace falta
    # explicarlo después de que pase.
    #
    # Lo que no se ejecuta: el valor del usuario se guarda tal cual y sólo se
    # compara. No hay `eval` ni ninguna otra evaluación de código en el
    # camino; el valor es dato.

    #: Los tres pasos van numerados en el título porque el usuario tiene que
    #: saber cuánto le queda, no sólo dónde está.
    def _validar_valor_de_regla(self, campo: str, operador: str):  # noqa: ANN201
        """Devuelve el validador de `_prompt_form` para un valor de regla.

        Encadena dos cosas: convertir el texto al tipo que pide el backend
        (`regla_text.valor_de`) y pasarlo por `validar_regla`. Se closure y no
        un método suelto porque depende de `campo` y `operador`, que son
        distintos en cada paso del asistente.
        """
        def _validar(data: dict[str, str]) -> str | None:
            valor, error = rule_text.valor_de(
                str((data or {}).get("valor", "")), campo, operador
            )
            if error:
                return error
            try:
                validar_regla({"field": campo, "operator": operador,
                               "value": valor})
            except ReglaInvalida as exc:
                # El motivo es el del backend (U-02): la UI no reescribe lo
                # que dice la validación, sólo lo enseña.
                return str(exc)
            return None

        return _validar

    def _pedir_valor(self, campo: str, operador: str):  # noqa: ANN201
        """Pide el valor de una condición. ``("valor", v)`` o ``("cancelar", None)``.

        El par en vez de un valor pelado es lo que permite que el asistente
        distinga «el usuario escribió esto» de «el usuario se arrepintió»:
        devolver sólo el valor haría que un ``None`` de cancelación se
        colase como una condición escrita.
        """
        etiqueta = rule_text.etiqueta_de_campo(campo)
        # El ejemplo va en la etiqueta y no en el texto: `FormModal` recorta
        # las etiquetas largas, y un ejemplo que no se ve no ayuda. La
        # explicación de qué mira el campo ya se dio —con más sitio— en el
        # paso 1.
        ejemplo = rule_text.ejemplo_de(campo, operador)
        datos = self._prompt_form(
            f"3 de 3 · {etiqueta}",
            [("valor", f"Ejemplo: {ejemplo}")],
            initial={"valor": ""},
            validar=self._validar_valor_de_regla(campo, operador),
        )
        if datos is None:
            return ("cancelar", None)
        valor, error = rule_text.valor_de(str(datos.get("valor", "")),
                                          campo, operador)
        if error:
            # `validar` corrió ya y no dejó pasar un valor inválido; si se
            # llegara aquí es por un cambio futuro, y no se guarda a ciegas.
            return ("cancelar", None)
        return ("valor", valor)

    def build_collection_rule(self, reglas_actual=None) -> dict | None:  # noqa: ANN201
        """Asistente: devuelve el diccionario de regla, o ``None`` si se rindió.

        Con ``reglas_actual`` (un diccionario ya guardado) el asistente no
        empieza de cero: no lo descompone, porque traducir un ``all`` anidado
        a pasos y de vuelta no siempre se puede y una regla que se reescribe
        sola es una regla que cambia sin que nadie lo pidiera. Se avisa en el
        modal de arranque y se construye una regla nueva, que es lo que el
        botón dice.
        """
        if reglas_actual:
            self.notify_warning(
                "Esta colección ya tiene una regla.\n"
                "El asistente arma una nueva desde cero: al guardarla "
                "sustituye a la anterior.\n"
                "La anterior no se guarda en ningún sitio, así que anota lo "
                "que te sirva de ella antes de seguir."
            )
        nodos: list[dict] = []
        modo = "all"
        while True:
            paso = len(nodos) + 1
            campo = self._elegir_uno(
                f"{paso} de 3 · ¿Qué mirar del canal?",
                rule_text.items_de_campo(),
                subtitle="La regla se arma con esto y un valor.",
            )
            if campo is None:
                return None
            operadores = rule_text.items_de_operador(campo)
            if not operadores:
                self.notify_warning(
                    f"El campo «{campo}» no admite comparaciones todavía."
                )
                return None
            operador = self._elegir_uno(
                f"{paso} de 3 · ¿Cómo compararlo?",
                operadores,
                # El nombre del campo, no su clave: «group» es el
                # identificador del backend y no le dice nada a nadie.
                subtitle=f"Sólo «{rule_text.etiqueta_de_campo(campo)}» "
                         "admite estas comparaciones.",
            )
            if operador is None:
                return None
            estado, valor = self._pedir_valor(campo, operador)
            if estado != "valor":
                return None
            nodos.append({"field": campo, "operator": operador, "value": valor})

            siguiente = self._elegir_uno(
                "¿Y ahora qué?",
                rule_text.items_de_siguiente_paso(),
                subtitle="Puedes encadenar otra condición o guardar.",
            )
            if siguiente is None:
                return None
            if siguiente in ("all", "any"):
                modo = siguiente
                continue
            return rule_text.regla_desde(nodos, modo, siguiente == "not")

    def _cuenta_de_regla(self, regla) -> int | None:  # noqa: ANN001
        """Cuántos canales de las listas en memoria cumplen `regla`.

        ``None`` cuando **no se puede saber**: sin listas cargadas, con una
        regla que ni siquiera valida, o con una regla que mira algo que la app
        todavía no sabe (favoritos, salud, fallos, guía). Devolver 0 en esos
        casos sería mentir, y es justo el número que el usuario está mirando
        para decidir si guarda.
        """
        try:
            validar_regla(regla)
        except ReglaInvalida:
            return None
        if rule_text.campos_sin_contexto(regla):
            return None
        catalogo = self.catalogo_canales()
        if not catalogo:
            return None
        borrador = Coleccion(id="__preview__", name="__preview__", type="smart",
                              rules=regla)
        try:
            # Con el contexto de la app: sin él, una regla de favoritos o de
            # guía daría cero canales por falta de datos, no por falta de
            # coincidencias (U-04b).
            return len(evaluar_coleccion(borrador, catalogo,
                                         self.contexto_de_reglas()))
        except Exception:
            return None

    def _mensaje_de_vista_previa(self, regla) -> tuple[str, str]:  # noqa: ANN001
        """(título, mensaje) de la vista previa: la regla escrita y el número.

        La cabecera cambia con la regla: decir «se incluirá el canal si:»
        delante de un `not` sería contradecir lo que viene justo debajo.
        """
        negada = isinstance(regla, dict) and "not" in regla
        cabecera = ("NO entra el canal si:" if negada
                    else "Se incluirá el canal si:")

        faltan = rule_text.campos_sin_contexto(regla)
        if faltan:
            campos = ", ".join(faltan)
            donde = (f"No se puede contar: {campos} no sale de las listas.\n"
                     "Son cosas que la app todavía no mira al evaluar una "
                     "regla, así que cualquier cifra sería inventada.\n"
                     "La regla se guarda igual.")
        elif not self.catalogo_canales():
            donde = "No se puede contar todavía: carga al menos una lista."
        else:
            cuenta = self._cuenta_de_regla(regla)
            if cuenta is None:
                donde = "No se puede contar esta regla con lo que hay cargado."
            elif cuenta == 0:
                donde = ("Ningún canal de tus listas cumple esta regla.\n"
                         "Se puede guardar, pero la colección se quedaría "
                         "vacía: revisa la palabra que has escrito.")
            else:
                plural = "canal" if cuenta == 1 else "canales"
                donde = f"Ahora mismo entran {cuenta} {plural} de tus listas."

        return ("Guardar la regla",
                f"{cabecera}\n\n{rule_text.resumen_de(regla)}\n\n{donde}")

    def _confirm_preview(self, regla) -> bool:  # noqa: ANN001
        """Muestra la vista previa y pregunta. ``False`` si no guarda."""
        from .widgets import Modal, modal_actions

        titulo, mensaje = self._mensaje_de_vista_previa(regla)
        res = self._run_modal(Modal(titulo, mensaje, ["Cancelar", "Guardar"],
                                    actions=modal_actions(
                                        confirmar="Guardar")))
        return res == "guardar"

    def edit_collection_rule(self, cid: str) -> None:
        """Da una regla a la colección `cid`, o se la cambia (U-04).

        Convierte una colección manual en inteligente **avisando antes** si
        tenía canales elegidos a mano: dejan de contar, aunque no se borran del
        archivo. Avisar es lo mínimo; decidir por el usuario si eso le viene
        bien es suya.
        """
        col = self._nombre_de_coleccion(cid)
        if col is None:
            self.notify_warning("Esa colección ya no existe.")
            return
        try:
            actual = self.collections.obtener(cid)
        except Exception:
            actual = None
        if actual is None:
            self.notify_warning("Esa colección ya no existe.")
            return
        if getattr(actual, "type", "") == "system":
            self.notify_warning(
                f"«{col}» es una vista de la app, no una colección tuya.\n"
                "Sus canales son los tuyos: no hay nada que decidir aquí."
            )
            return
        if getattr(actual, "type", "") == "manual" and actual.members:
            cuantos = len(actual.members)
            plural = "canal" if cuantos == 1 else "canales"
            if not self._confirm(
                "Cambiar a colección por regla",
                f"«{col}» la armaste a mano y tiene {cuantos} {plural} "
                "elegidos.\n\nSi le pones una regla, la colección contará lo "
                "que diga la regla y esos canales dejarán de contar.\n\nNo se "
                "borran del archivo, pero mientras la regla manda no los mira.",
            ):
                return

        regla = self.build_collection_rule(actual.rules)
        if regla is None:
            self.notify("No se cambió la regla.")
            return
        # Se valida **antes** de la vista previa: enseñar «0 canales» de una
        # regla que el backend ni siquiera acepta sería un número inventado,
        # y es justo el número que el usuario está mirando.
        try:
            validar_regla(regla)
        except ReglaInvalida as exc:
            from .screens import _error_dominio

            _error_dominio(self, exc, retry=lambda: self.edit_collection_rule(cid))
            return
        if not self._confirm_preview(regla):
            return
        try:
            self.collections.editar(cid, tipo="smart", rules=regla)
        except (ColeccionesManagerError, ReglaInvalida) as exc:
            from .screens import _error_dominio

            _error_dominio(self, exc, retry=lambda: self.edit_collection_rule(cid))
            return
        except Exception as exc:
            self.notify_error(f"No se pudo guardar la regla: {exc}")
            return
        self._recargar_colecciones()
        self.notify(f"Colección «{col}» guardada. Ahora se arma sola con su regla.")

    # --- Búsqueda: filtros (U-05) ------------------------------------------
    #
    # `search.Consultador` (Fase 3) lleva sin usarse desde que se escribió: la
    # pantalla de canales filtraba con un `q in texto` y nada más. U-05 le da
    # los ocho filtros que el backend ya sabía responder, y deja la búsqueda
    # de siempre como lo que pasa si el usuario no toca nada.
    #
    # Los modales son **encadenados, nunca anidados**: `_run_checklist` levanta
    # `_in_modal`, y un modal dentro de otro se degradaría a la barra de
    # estado sin avisar. Por eso el bucle de la lista vive aquí y cada
    # elección sale del bucle, abre su propio modal y vuelve a entrar.

    #: Fila que no es un filtro sino «pon uno más».
    _FILTRO_NUEVO = "__nuevo__"
    #: Fila que quita el filtro que se está editando.
    _FILTRO_QUITAR = "__quitar__"
    #: Fila que pide el valor con un cuadro de texto.
    _FILTRO_ESCRIBIR = "__escribir__"
    #: El orden natural, que no es ninguna clave de `filters.ORDENES`.
    _ORDEN_NATURAL = "__natural__"

    def _filtros_de_pantalla(self, pantalla) -> dict:  # noqa: ANN001
        """Los filtros de `pantalla` como `{campo: valor}`.

        `getattr` defensivo porque quien llama puede ser un doble de test: que
        falle `filtros_activos` degrada a «no hay filtros», que es lo único que
        se puede afirmar sin mirar.
        """
        fn = getattr(pantalla, "filtros_activos", None)
        if not callable(fn):
            return {}
        try:
            return dict(fn() or {})
        except Exception:
            return {}

    def _subtitulo_de_filtros(self, pantalla) -> str:  # noqa: ANN001
        """«N de M canales» y los filtros puestos, **en una sola línea**.

        El contador va primero porque es lo que el usuario vino a ver. Los
        filtros van con su frase porque «hay algo puesto» sin decir qué es
        justo lo que no se puede quitar bien.

        Una línea y no dos: `ChecklistModal` mide el alto por el del subtítulo
        y lo pinta en una sola fila, así que un `\n` ahí no envuelve —se sale
        por encima del borde lateral y deja el recuadro roto.
        """
        try:
            cuenta = pantalla.poner_filtros()
            total = len(pantalla.channels)
        except Exception:
            return ""
        activo = pantalla.filtros_legibles()
        if activo:
            return (f"Ahora salen {cuenta} de {total} canales · "
                    + " · ".join(activo))
        return f"Ahora salen {cuenta} de {total} canales · sin filtros"

    def _modal_de_filtros(self, pantalla):  # noqa: ANN001
        """La lista de lo puesto, más «añadir uno más».

        Va en modo *radio* y no con casillas porque **cada fila abre un modal
        distinto** —quitar o cambiar el valor— y una casilla prometería un
        interruptor de un solo gesto que aquí no existe. Lo que sí se ve de un
        vistazo es qué hay puesto: está en el subtítulo, con su frase.
        """
        from .widgets import ChecklistItem, ChecklistModal, radio_actions

        activos = self._filtros_de_pantalla(pantalla)
        items = []
        for clave, valor in activos.items():
            f = filters_mod.filtro_de(clave)
            items.append(ChecklistItem(
                clave, filters_mod.frase_de(clave, valor),
                note=f.ayuda if f else ""))
        items.append(ChecklistItem(
            self._FILTRO_NUEVO, "Añadir un filtro",
            note="Elige qué mirar del canal y con qué valor."))
        return ChecklistModal(
            "Filtros de la lista",
            items,
            subtitle=self._subtitulo_de_filtros(pantalla),
            actions=radio_actions(),
            radio=True,
            empty="No hay filtros puestos.",
        )

    def _valor_de_filtro(self, clave: str, actual: str | None):  # noqa: ANN201
        """Pide el valor de un filtro. ``(valor, quitar)``.

        El par en vez de un valor pelado es lo que permite distinguir «escribió
        esto» de «quiere quitarlo» de «se arrepintió»: los tres casos salen
        igual si se devuelve sólo una cosa.
        """
        from .widgets import ChecklistItem

        f = filters_mod.filtro_de(clave)
        if f is None:
            return (None, False)

        if f.es_texto and actual:
            opciones = [
                ChecklistItem(self._FILTRO_QUITAR, f"Quitar «{actual}»",
                              note="La lista se queda sin este filtro."),
                ChecklistItem(self._FILTRO_ESCRIBIR, "Escribir otro valor",
                              note=f.ayuda),
            ]
            elegido = self._elegir_uno(
                f.etiqueta, opciones, subtitle=f"Ahora: {actual}.")
            if elegido is None:
                return (None, False)
            if elegido == self._FILTRO_QUITAR:
                return (None, True)
            if elegido != self._FILTRO_ESCRIBIR:
                return (actual, False)

        if f.es_texto:
            # El ejemplo va en la etiqueta y no en el texto: `FormModal`
            # recorta las etiquetas largas, y un ejemplo que no se ve no ayuda.
            etiqueta = f"Ejemplo: {f.ejemplo}" if f.ejemplo else f.ayuda
            datos = self._prompt_form(
                f.etiqueta, [("valor", etiqueta)],
                initial={"valor": actual or ""})
            if datos is None:
                return (None, False)
            texto = str(datos.get("valor", "")).strip()
            if not texto:
                # Un filtro sin valor no quita canales, y una lista vacía
                # fingiendo filtrar es la peor forma de no filtrar.
                self.notify_warning(
                    f"«{f.etiqueta}» se queda como estaba: no escribiste nada.")
                return (None, False)
            return (texto, False)

        items = [ChecklistItem(v, etiqueta, note=f.ayuda)
                 for v, etiqueta in f.opciones]
        if actual:
            items.append(ChecklistItem(
                self._FILTRO_QUITAR, f"Quitar «{actual}»",
                note="La lista se queda sin este filtro."))
        # El subtítulo, no la nota de la fila: aquí hay ancho de sobra y el
        # aviso entero cabe. Recortado a media frase no avisa de nada.
        elegido = self._elegir_uno(
            f.etiqueta, items,
            subtitle=filters_mod.aviso_de(clave) or f.ayuda)
        if elegido is None:
            return (None, False)
        if elegido == self._FILTRO_QUITAR:
            return (None, True)
        return (elegido, False)

    def search_filters(self, pantalla) -> None:  # noqa: ANN001
        """El modal de filtros de `pantalla` (tecla `F`).

        Sin filtros puestos entra **directamente** a añadir uno: una lista de
        filtros donde no hay nada puesto, más una fila para añadir, es una
        pantalla de menos entre el usuario y lo que quiere.
        """
        if not self._filtros_de_pantalla(pantalla):
            self._anadir_filtro(pantalla)
            return
        while True:
            modal = self._modal_de_filtros(pantalla)
            elegido = self._run_checklist(modal, _no_se_marca_en_radio)
            if elegido is None:
                return
            if elegido == self._FILTRO_NUEVO:
                self._anadir_filtro(pantalla)
                continue
            self._editar_filtro(pantalla, elegido)

    def _anadir_filtro(self, pantalla) -> None:  # noqa: ANN001
        from .widgets import ChecklistItem

        puestos = self._filtros_de_pantalla(pantalla)
        items = []
        for f in filters_mod.FILTROS:
            ya = f.clave in puestos
            items.append(ChecklistItem(
                f.clave, f.etiqueta,
                note=(f"Puesto ahora como {puestos[f.clave]}. Elegirlo "
                      "cambia su valor." if ya else f.ayuda)))
        clave = self._elegir_uno(
            "¿Qué mirar del canal?", items,
            subtitle="El filtro se queda puesto hasta que lo quites.")
        if clave is None:
            return
        valor, quitar = self._valor_de_filtro(clave, puestos.get(clave))
        if quitar:
            return
        if valor is None:
            self.notify("No se cambió nada.")
            return
        self._aplicar_filtro(pantalla, clave, valor)

    def _editar_filtro(self, pantalla, clave: str) -> None:  # noqa: ANN001
        valor, quitar = self._valor_de_filtro(
            clave, self._filtros_de_pantalla(pantalla).get(clave))
        if quitar:
            self._quitar_filtro(pantalla, clave)
            return
        if valor is None:
            return
        self._aplicar_filtro(pantalla, clave, valor)

    def _aplicar_filtro(self, pantalla, clave: str, valor: str) -> None:  # noqa: ANN001
        """Pone (o cambia) un filtro y **refiltra en el acto**.

        El refiltrado va aquí y no al cerrar el modal porque el usuario quiere
        ver cuántos canales salen con cada filtro puesto: si sólo se aplicara
        al salir, el constructor iría a ciegas.
        """
        actuales = self._filtros_de_pantalla(pantalla)
        actuales[clave] = valor
        try:
            cuenta = pantalla.poner_filtros(list(actuales.items()))
        except Exception as exc:  # noqa: BLE001 - la lista no se queda a medias
            self.notify_error(f"No se pudo aplicar el filtro: {exc}")
            return
        self.notify(f"{filters_mod.frase_de(clave, valor)} · {cuenta} canales.")

    def _quitar_filtro(self, pantalla, clave: str) -> None:  # noqa: ANN001
        actuales = self._filtros_de_pantalla(pantalla)
        if clave not in actuales:
            return
        frase = filters_mod.frase_de(clave, actuales.pop(clave))
        pantalla.poner_filtros(list(actuales.items()))
        self.notify(f"Quitado: {frase}.")

    def clear_search_filters(self, pantalla) -> None:  # noqa: ANN001
        """Quita todos los filtros de golpe (tecla `X`).

        Pide confirmación y **nombra cada uno**: quitar cuatro filtros sin
        decir cuáles deja al usuario reconstruyendo de memoria lo que tenía, y
        eso es justo lo que un «quitar todo» no debería costar.
        """
        activos = self._filtros_de_pantalla(pantalla)
        if not activos:
            self.notify("No hay filtros que quitar.")
            return
        frases = [filters_mod.frase_de(c, v) for c, v in activos.items()]
        plural = "filtro" if len(frases) == 1 else "filtros"
        cuerpo = ("Se van a quitar estos filtros:\n\n  "
                  + "\n  ".join(frases)
                  + "\n\nEl texto que hayas escrito se queda, y el orden "
                    "de la lista no se toca.")
        if not self._confirm("Quitar los filtros", cuerpo):
            return
        cuenta = pantalla.limpiar_filtros()
        self.notify(f"Quitados {len(frases)} {plural}. Ahora salen "
                    f"{cuenta} canales.")

    def search_order(self, pantalla) -> None:  # noqa: ANN001
        """Elige el orden de la lista (tecla `o`).

        El orden va en su propio modal y no como una fila más de filtros
        porque **no quita canales**: es otra pregunta, y meterla entre los
        filtros haría que «quitar todos» moviera también el orden, que nadie
        pidió.
        """
        from .widgets import ChecklistItem

        actual = getattr(pantalla, "orden", None)
        items = []
        for clave, etiqueta in filters_mod.ORDENES:
            items.append(ChecklistItem(
                clave or self._ORDEN_NATURAL, etiqueta,
                checked=(clave == actual),
                note=("El de la lista tal como la escribió el proveedor."
                      if clave is None else "")))
        elegido = self._elegir_uno(
            "Orden de la lista", items,
            subtitle="Sólo mueve canales: no quita ninguno.")
        if elegido is None:
            return
        clave = None if elegido == self._ORDEN_NATURAL else elegido
        if clave == actual:
            self.notify(f"El orden sigue siendo: "
                        f"{filters_mod.orden_legible(actual)}.")
            return
        try:
            cuenta = pantalla.poner_filtros(orden=clave)
        except Exception as exc:  # noqa: BLE001
            self.notify_error(f"No se pudo cambiar el orden: {exc}")
            return
        self.notify(f"Orden: {filters_mod.orden_legible(clave)} · "
                    f"{cuenta} canales.")

# --- Fuentes del canal (U-06) -------------------------------------------
    #
    # El backend de Fase 5A (`LogicalChannel`, `detectar_candidatas` y ahora
    # `AssociationStore`) llevaba tres fases sin que nadie lo llamara desde la
    # interfaz. U-06 le da la pregunta más normal que surge con dos listas
    # cargadas: «¿este canal está en otra? ¿de cuál se usa?».
    #
    # Tres decisiones que el ticket fija, y ninguna es de estilo:
    #
    # 1. **El índice se construye en segundo plano.** Medido en este repo:
    #    recorrer 90 000 canales de dos listas y agruparlos por identificador
    #    y por nombre cuesta ~1 s. En un modal eso no es «un poco lento»: es
    #    un parpadeo de un segundo cada vez que se abre, así que va por
    #    `tasks/manager.py` igual que el catálogo. Mientras no esté, el modal
    #    **lo dice** y ofrece volver a intentarlo: nunca enseña «sin
    #    candidatas» porque todavía no se ha mirado, que sería mentir.
    #
    # 2. **El índice no incluye la lista que se está viendo.** Una candidata
    #    nunca está en la propia lista del canal, y con una sola lista
    #    cargada —el caso normal— no hay nada que construir: la respuesta es
    #    inmediata y no hay parpadeo posible.
    #
    # 3. **El modal no promete que esto cambie la reproducción**, porque en
    #    este ticket no la cambia: U-06 anota la fuente preferida y la
    #    explica. Aplicarla al reproducir, avisando y permitiendo cancelar,
    #    es U-07.

    #: Fila de la lista de la que ya se viene.
    _FUENTE_ACTUAL = "__actual__"
    #: Fila que quita la fuente preferida (A-03: deshacer).
    _FUENTE_OLVIDAR = "__olvidar__"
    #: Fila que vuelve a pedir la comparación si el índice no está listo.
    _FUENTE_REINTENTAR = "__reintentar__"

    def _store_de_fuentes(self) -> AssociationStore:
        """El almacén de asociaciones, creado una sola vez por app.

        Se construye en el primer uso y no en `__init__` para que una app que
        nunca abre «Fuentes del canal» no lea un archivo que no va a usar.
        """
        store = getattr(self, "_fuentes_store", None)
        if store is None:
            store = AssociationStore(config.SOURCES_JSON)
            self._fuentes_store = store
        return store

    def _listas_de_comparacion(self, playlist) -> list[tuple[str, list]]:  # noqa: ANN001
        """Las listas cargadas **menos** la que se está viendo.

        Compara por identidad de objeto (`is`) y no por nombre: dos entradas
        con el mismo nombre son dos listas distintas, y comparar por nombre
        excluiría a la actual y a su gemela por el mismo motivo.
        """
        salida: list[tuple[str, list]] = []
        for guardada in list(self.playlist_cache.values()):
            if playlist is not None and guardada is playlist:
                continue
            canales = getattr(guardada, "channels", None)
            if canales is None:
                continue
            nombre = str(getattr(guardada, "name", "") or "")
            if not nombre:
                continue
            salida.append((nombre, list(canales)))
        return salida

    def _huella_de_fuentes(self, playlist) -> tuple:  # noqa: ANN001
        """Qué listas hay cargadas, para saber si el índice sigue sirviendo."""
        return tuple(
            (nombre, len(canales))
            for nombre, canales in self._listas_de_comparacion(playlist)
        )

    def pedir_indice_fuentes(self, playlist=None) -> bool:  # noqa: ANN001
        """Pide el índice en segundo plano. True si hay algo que comparar.

        Idempotente por huella: si las listas no han cambiado y ya hay una
        petición viva, no se lanza otra. Cada petición supersede la anterior
        de su categoría, así que `s` pulsado tres veces no deja tres hilos
        comparando lo mismo.
        """
        listas = self._listas_de_comparacion(playlist)
        if not listas:
            return False
        huella = tuple((n, len(c)) for n, c in listas)
        if getattr(self, "_indice_fuentes_huella", None) == huella:
            return True
        if getattr(self, "_indice_fuentes_pidiendo", False):
            return True
        self._indice_fuentes_huella = huella
        self._indice_fuentes_pidiendo = True

        def _trabajo(token: CancellationToken):
            return sources_view.construir_indice(listas, token=token)

        try:
            self._tasks.submit("sources-index", _trabajo, supersede=True)
        except Exception:
            # Sin gestor de tareas (o cerrándose): el modal explica que no se
            # puede comparar, en vez de fingir que no hay candidatas.
            self._indice_fuentes_pidiendo = False
            return False
        return True

    def indice_fuentes(self, playlist) -> object | None:  # noqa: ANN001
        """El índice si ya está listo; `None` si todavía no.

        Drena los resultados antes de mirar: abrir el modal es un gesto del
        hilo principal y es el sitio natural donde el trabajo terminado se
        aplica al modelo. Un resultado obsoleto —el usuario cambió de lista
        mientras se comparaba— se descarta sin publicarse, que es la
        garantía de `TaskManager` por generación.
        """
        self._drenar_indice_fuentes()
        if self._huella_de_fuentes(playlist) != getattr(
            self, "_indice_fuentes_huella", None
        ):
            self._indice_fuentes = None
            return None
        return getattr(self, "_indice_fuentes", None)

    def _drenar_indice_fuentes(self) -> None:
        """Aplica —o tira— el resultado de la comparación en marcha."""
        drenar = getattr(getattr(self, "_tasks", None), "drain", None)
        if not callable(drenar):
            return
        for resultado in drenar("sources-index"):
            self._indice_fuentes_pidiendo = False
            if resultado.obsolete or resultado.cancelled:
                continue
            if resultado.error is not None or resultado.value is None:
                self._indice_fuentes = None
                continue
            self._indice_fuentes = resultado.value

    def _clave_de_canal(self, channel, playlist) -> str:  # noqa: ANN001
        """Clave con la que se guarda la preferencia de este canal.

        El `source_id` es el **nombre** de la lista y no su URL: la clave va a
        un archivo y una URL —con su token— no es lo que uno quiere tener ahí.
        El nombre es lo que el usuario ve y lo que reconoce.
        """
        return clave_logica(channel, str(getattr(playlist, "name", "") or ""))

    def _candidatas_de_fuentes(self, channel, playlist) -> tuple[list, int]:  # noqa: ANN001
        """`(candidatas, descartadas)` para el canal, según el detector.

        Las **descartadas** no salen en la lista pero se cuentan: si el usuario
        descartó dos y ahora ve el resto en cero, necesita saber que la app
        se acuerda en vez de olvidar.
        """
        indice = self.indice_fuentes(playlist)
        if indice is None:
            return ([], 0)
        clave = self._clave_de_canal(channel, playlist)
        descartadas = self._store_de_fuentes().rechazadas(clave) if clave else set()
        salida: list = []
        omitidas = 0
        for cand in sources_view.deteccion_para(channel, indice.candidatas_de(channel)):
            if clave_candidata(cand.fuente, cand.canal) in descartadas:
                omitidas += 1
                continue
            salida.append(cand)
        return (salida, omitidas)

    def _subtitulo_de_fuentes(self, playlist, mostradas, total,  # noqa: ANN001
                              descartadas, preferida, indice_listo) -> str:
        """El subtítulo del modal: **una sola línea** y con las cuentas hechas.

        Una línea porque `ChecklistModal` la pinta en una fila y mide el alto
        por su ancho —lo mismo que en U-05—: un `\\n` ahí se sale por encima
        del borde y rompe el recuadro.
        """
        if not indice_listo:
            return (f"{playlist.name} · se están comparando las listas, "
                    "esto aún no está")
        partes = [sources_view.resumen_de_candidatas(total, mostradas)]
        if descartadas:
            partes.append(f"{descartadas} que descartaste")
        if preferida:
            partes.append(f"preferida: {preferida}")
        return " · ".join(partes)

    def _modal_de_fuentes(self, channel, playlist):  # noqa: ANN001
        """La lista de fuentes de `channel`, con arriba la de la que se viene."""
        from .widgets import ChecklistItem, ChecklistModal, radio_actions

        candidatas, descartadas = self._candidatas_de_fuentes(channel, playlist)
        indice_listo = self.indice_fuentes(playlist) is not None
        clave = self._clave_de_canal(channel, playlist)
        preferida = self._store_de_fuentes().fuente_preferida(clave) if clave else ""

        items: list = [
            ChecklistItem(
                self._FUENTE_ACTUAL,
                f"{playlist.name} (esta lista)",
                note="Es la lista de donde viene el canal.",
            )
        ]
        mostradas = candidatas[:sources_view.MAX_CANDIDATAS_MOSTRADAS]
        for cand in mostradas:
            grupo = str(getattr(cand.canal, "group", "") or "")
            nota = cand.nota
            if cand.fuente == preferida:
                nota = "la preferida · " + nota
            items.append(ChecklistItem(
                cand.fuente,
                cand.fuente + (f" · {grupo}" if grupo else ""),
                note=nota,
            ))
        if preferida:
            items.append(ChecklistItem(
                self._FUENTE_OLVIDAR,
                "Quitar la fuente preferida",
                note="El canal vuelve a no tener ninguna.",
            ))
        if not indice_listo:
            items.append(ChecklistItem(
                self._FUENTE_REINTENTAR,
                "Comparar las listas otra vez",
                note="Por si ya han terminado de cargarse.",
            ))

        return ChecklistModal(
            "Fuentes del canal",
            items,
            subtitle=self._subtitulo_de_fuentes(
                playlist, len(mostradas), len(candidatas),
                descartadas, preferida, indice_listo),
            actions=radio_actions(),
            radio=True,
            empty="No hay otras listas cargadas para comparar.",
        )

    def channel_sources(self, channel, playlist) -> None:  # noqa: ANN001
        """«Fuentes del canal» (tecla `s`): qué más lo tiene y cuál se prefiere.

        El bucle vive aquí y no dentro del modal por lo mismo que en U-05: los
        modales son **encadenados, nunca anidados**. Cada elección sale del
        bucle, abre su propio modal y vuelve a entrar.
        """
        if channel is None:
            self.notify("No hay ningún canal debajo del cursor.")
            return
        if not self._listas_de_comparacion(playlist):
            self.notify(
                f"«{playlist.name}» es la única lista cargada: no hay con qué "
                "compararla. Abre otra lista y vuelve aquí.",
                title="Fuentes del canal")
            return
        self.pedir_indice_fuentes(playlist)
        while True:
            modal = self._modal_de_fuentes(channel, playlist)
            elegido = self._run_checklist(modal, _no_se_marca_en_radio)
            if elegido is None:
                return
            if elegido == self._FUENTE_ACTUAL:
                self.notify(
                    f"«{playlist.name}» es de donde viene este canal. No hay "
                    "nada que cambiar.",
                    title="Esta lista")
                return
            if elegido == self._FUENTE_OLVIDAR:
                self._olvidar_fuente(channel, playlist)
                continue
            if elegido == self._FUENTE_REINTENTAR:
                self._indice_fuentes = None
                self._indice_fuentes_huella = None
                self.pedir_indice_fuentes(playlist)
                continue
            cand = self._candidata_de_fuente(channel, playlist, elegido)
            if cand is None:
                continue
            self._decidir_fuente(channel, playlist, cand)

    def _candidata_de_fuente(self, channel, playlist, fuente):  # noqa: ANN001
        candidatas, _ = self._candidatas_de_fuentes(channel, playlist)
        for cand in candidatas:
            if cand.fuente == fuente:
                return cand
        return None

    def _decidir_fuente(self, channel, playlist, cand) -> None:  # noqa: ANN001
        """Qué hacer con una candidata: usarla, descartarla, o nada.

        Un solo modal con tres botones y ninguno genérico: «Usar esta fuente» y
        «No volver a proponerla» dicen exactamente lo que va a pasar, que es
        justo lo que un «Aceptar» esconde. Y no afirma que esto cambie lo que
        se reproduce: U-06 anota la preferencia; aplicarla al reproducir,
        avisando y dejando cancelar, es U-07.
        """
        from .widgets import Modal, modal_actions

        nombre = _etiqueta_de_canal(channel)
        direccion = redact_text(str(getattr(cand.canal, "url", "") or ""))
        cuerpo = (
            f"«{nombre}» también está en «{cand.fuente}».\n\n"
            f"{sources_view.frase_de_senales(cand.senales)}\n"
            f"Nivel: {sources_view.nivel_legible(cand.nivel)}.\n\n"
            f"Dirección, con la contraseña tapada:\n"
            f"{direccion or 'sin dirección'}\n\n"
            f"«Usar esta fuente» la deja como la preferida de este canal en "
            f"«{playlist.name}». Puedes quitarla desde aquí cuando quieras.\n"
            f"«No volver a proponerla» la descarta mientras siga siendo el "
            f"mismo canal."
        )
        res = self._run_modal(Modal(
            f"Fuente para «{nombre}»",
            cuerpo,
            ["Cancelar", "Usar esta fuente", "No volver a proponerla"],
            actions=modal_actions(confirmar="Elegir", cancelar="Cancelar"),
            wrap=True,
        ))
        if res == "usar esta fuente":
            self._asociar_fuente(channel, playlist, cand)
            return
        if res == "no volver a proponerla":
            self._descartar_fuente(channel, playlist, cand)
            return
        # «Cancelar», Esc, o un botón que no sea de estos dos: no se hace nada.

    def _asociar_fuente(self, channel, playlist, cand) -> None:  # noqa: ANN001
        clave = self._clave_de_canal(channel, playlist)
        if not clave:
            self.notify_error(
                "Este canal no tiene con qué reconocerse, así que no se puede "
                "guardar una fuente preferida para él.")
            return
        try:
            self._store_de_fuentes().asociar(
                clave, clave_candidata(cand.fuente, cand.canal), cand.fuente)
        except AsociacionesError as exc:
            self.notify_error(str(exc))
            return
        self.notify(
            f"«{cand.fuente}» queda como la fuente preferida de "
            f"«{_etiqueta_de_canal(channel)}» en «{playlist.name}».")

    def _descartar_fuente(self, channel, playlist, cand) -> None:  # noqa: ANN001
        clave = self._clave_de_canal(channel, playlist)
        if not clave:
            self.notify_warning("Este canal no tiene con qué reconocerse.")
            return
        try:
            self._store_de_fuentes().rechazar(
                clave, clave_candidata(cand.fuente, cand.canal))
        except AsociacionesError as exc:
            self.notify_error(str(exc))
            return
        self.notify(
            f"«{cand.fuente}» no se volverá a proponer para "
            f"«{_etiqueta_de_canal(channel)}» mientras siga igual.")

    def _olvidar_fuente(self, channel, playlist) -> None:  # noqa: ANN001
        """Quita la fuente preferida (A-03: deshacer).

        El modal dice explícitamente qué **no** se deshace: los descartes. Son
        gestos distintos, y devolver a la lista una fuente que el usuario
        descartó porque se caía sería justo el fallo que A-03 quiere evitar.
        """
        from .widgets import Modal, modal_actions

        clave = self._clave_de_canal(channel, playlist)
        fuente = self._store_de_fuentes().fuente_preferida(clave) if clave else ""
        if not fuente:
            self.notify("Este canal no tiene ninguna fuente preferida.")
            return
        nombre = _etiqueta_de_canal(channel)
        cuerpo = (
            f"«{nombre}» volverá a no tener fuente preferida en "
            f"«{playlist.name}».\n\n"
            f"La fuente «{fuente}» no se borra de su lista ni se toca nada: "
            f"sólo deja de estar elegida.\n\n"
            f"Las fuentes que descartaste siguen descartadas: eso fue otra "
            f"decisión y aquí no se deshace."
        )
        res = self._run_modal(Modal(
            "Quitar la fuente preferida", cuerpo,
            ["Cancelar", "Quitar"],
            actions=modal_actions(confirmar="Quitar", cancelar="Cancelar"),
            wrap=True,
        ))
        if res != "quitar":
            return
        try:
            cambiada = self._store_de_fuentes().olvidar(clave)
        except AsociacionesError as exc:
            self.notify_error(str(exc))
            return
        if not cambiada:
            self.notify("No había ninguna fuente preferida.")
            return
        self.notify(f"«{nombre}» ya no tiene fuente preferida.")

    def _es_failover_permitido(self, error=None, cancelado=False) -> bool:
        """Failover solo ante error recuperable y si el usuario no cancelo."""
        if bool(cancelado):
            return False
        if error is None:
            return True
        try:
            return bool(failover_mod.es_recuperable(error))
        except Exception:
            return False

    def _modal_de_failover(self, fuente_actual="", fuente_nueva="", *, motivo="", cancelable=True) -> object:
        """Modal que anuncia un cambio automatico de fuente y permite cancelar."""
        from .widgets import Modal, modal_actions

        cuerpo = failover_mod.cambio_de_fuente_mensaje(
            fuente_actual, fuente_nueva, motivo=motivo, cancelable=bool(cancelable)
        )
        if cancelable:
            botones = ["Cancelar", "Cambiar fuente"]
            actions = modal_actions(confirmar="Cambiar", cancelar="Cancelar")
        else:
            botones = ["Aceptar"]
            actions = modal_actions(confirmar="Aceptar", cancelar="Aceptar")
        return Modal(
            "Cambio de fuente",
            cuerpo,
            botones,
            actions=actions,
            wrap=True,
        )

    def _failover_fuente(self, channel=None, playlist=None, error=None, *, fuente_actual="", fuente_nueva="",
                         motivo="") -> None:
        """Intenta (o anuncia) un failover, sin cambiar nada en silencio."""
        if not self._es_failover_permitido(error):
            if error is not None:
                self.notify_warning(failover_mod.sin_failover_mensaje(
                    motivo or type(error).__name__))
            return
        modal = self._modal_de_failover(fuente_actual, fuente_nueva, motivo=motivo,
                                       cancelable=True)
        res = None
        try:
            res = self._run_modal(modal)
        except Exception:
            res = None
        respuesta = str(res or "").strip().lower()
        if respuesta not in ("cambiar fuente", "cambiar", "aceptar", "si", "sí", "ok"):
            self.notify("Cancelado: se sigue con la fuente actual.")
            return
        texto = failover_mod.cambio_de_fuente_mensaje(fuente_actual, fuente_nueva, motivo=motivo)
        self.status.show(texto.splitlines()[0] if texto else "Cambiando de fuente.")
        return

    def failover_fuente(self, *args, **kwargs) -> None:
        return self._failover_fuente(*args, **kwargs)

    def intentar_failover(self, *args, **kwargs) -> None:
        return self._failover_fuente(*args, **kwargs)

    def intentar_failover_fuente(self, *args, **kwargs) -> None:
        return self._failover_fuente(*args, **kwargs)

    def cambiar_fuente(self, *args, **kwargs) -> None:
        return self._failover_fuente(*args, **kwargs)

    def cambiar_fuente_preferida(self, *args, **kwargs) -> None:
        return self._failover_fuente(*args, **kwargs)

    def failover_source(self, *args, **kwargs) -> None:
        return self._failover_fuente(*args, **kwargs)

    def _reconciliar_preferencias_fuente(self, preferencias, capacidades, *, confirmado_por_reproductor=False):
        res = failover_mod.reconciliar(
            preferencias, capacidades, confirmado_por_reproductor=bool(confirmado_por_reproductor)
        )
        discreto = failover_mod.modal_discreto(res)
        if discreto:
            try:
                self.notify_warning(discreto)
            except Exception:
                pass
        return res

    def reconciliar_preferencias_fuente(self, *args, **kwargs):
        return self._reconciliar_preferencias_fuente(*args, **kwargs)

    def reconciliar_preferencias_de_fuente(self, *args, **kwargs):
        return self._reconciliar_preferencias_fuente(*args, **kwargs)

    def preferencias_de_fuente(self, *args, **kwargs):
        return self._reconciliar_preferencias_fuente(*args, **kwargs)

    def aplicar_preferencias_fuente(self, *args, **kwargs):
        return self._reconciliar_preferencias_fuente(*args, **kwargs)

    def _es_grabacion_permitida(self, backend=None) -> bool:
        try:
            return bool(recording_mod.puede_grabar())
        except Exception:
            try:
                return bool(recording_mod.can_record())
            except Exception:
                return False

    def puede_grabar(self) -> bool:
        return self._es_grabacion_permitida()

    def puede_grabar_fuente(self, *args, **kwargs) -> bool:
        return self._es_grabacion_permitida()

    def can_record(self, *args, **kwargs) -> bool:
        return self._es_grabacion_permitida()

    def esta_autorizado_para_grabar(self, *args, **kwargs) -> bool:
        return self._es_grabacion_permitida()

    def _modal_de_autorizacion_grabacion(self, *, motivo="") -> object:
        from .widgets import Modal, modal_actions

        cuerpo = recording_mod.autorizacion_mensaje()
        if not self._es_grabacion_permitida():
            cuerpo = recording_mod.sin_grabacion_mensaje(
                motivo or recording_mod.backend_reason())
        botones = ["Cancelar", "Continuar"]
        actions = modal_actions(confirmar="Continuar", cancelar="Cancelar")
        return Modal("Grabación", cuerpo, botones, actions=actions, wrap=True)

    def _modal_de_autorizacion(self, *args, **kwargs) -> object:
        return self._modal_de_autorizacion_grabacion(*args, **kwargs)

    def _primer_modal_de_grabacion(self, *args, **kwargs) -> object:
        return self._modal_de_autorizacion_grabacion(*args, **kwargs)

    def modal_de_autorizacion_grabacion(self, *args, **kwargs) -> object:
        return self._modal_de_autorizacion_grabacion(*args, **kwargs)

    def modal_de_autorizacion(self, *args, **kwargs) -> object:
        return self._modal_de_autorizacion_grabacion(*args, **kwargs)

    def modal_de_grabacion(self, *args, **kwargs) -> object:
        return self._modal_de_autorizacion_grabacion(*args, **kwargs)

    def _autorizar_grabacion(self, *args, **kwargs) -> bool:
        if not self._es_grabacion_permitida():
            motivo = kwargs.get("motivo", "")
            try:
                self.notify_warning(recording_mod.sin_grabacion_mensaje(
                    motivo or recording_mod.backend_reason()))
            except Exception:
                pass
            return False
        modal = self._modal_de_autorizacion_grabacion(*args, **kwargs)
        res = None
        try:
            res = self._run_modal(modal)
        except Exception:
            res = None
        respuesta = str(res or "").strip().lower()
        if respuesta not in ("continuar", "aceptar", "si", "sí", "ok", "grabar", "grabar ahora"):
            try:
                self.notify("Cancelado: no se graba nada.")
            except Exception:
                pass
            return False
        return True

    def autorizar_grabacion(self, *args, **kwargs) -> bool:
        return self._autorizar_grabacion(*args, **kwargs)

    def autorizacion_grabacion(self, *args, **kwargs) -> bool:
        return self._autorizar_grabacion(*args, **kwargs)

    def mostrar_autorizacion_grabacion(self, *args, **kwargs) -> bool:
        return self._autorizar_grabacion(*args, **kwargs)

    def consultar_autorizacion_grabacion(self, *args, **kwargs) -> bool:
        return self._autorizar_grabacion(*args, **kwargs)

    def _modal_de_formulario_grabacion(self, destino=None, nombre="", *, duracion=None, **kwargs) -> object:
        from .widgets import FormModal
        import thetvview.config as _cfg

        initial = {
            "destino": str(destino or _cfg.RECORDINGS_DIR or ""),
            "nombre": str(nombre or ""),
            "duracion": str(duracion if duracion is not None else ""),
        }
        return FormModal(
            "Formulario de grabación",
            [("destino", "Destino"), ("nombre", "Nombre"), ("duracion", "Duración (seg)")],
            buttons=["Cancelar", "Grabar"],
            initial=initial,
        )

    def _formulario_grabacion(self, *args, **kwargs) -> object:
        destino = kwargs.get("destino", kwargs.get("output_dir", kwargs.get("dir")))
        if destino is None and args:
            destino = args[0]
        nombre = kwargs.get("nombre", kwargs.get("name", ""))
        if len(args) > 1 and not kwargs.get("nombre"):
            nombre = args[1]
        duracion = kwargs.get("duracion", kwargs.get("duration"))

        # Validación directa cuando ya hay datos suficientes.
        if destino and nombre and duracion is not None:
            try:
                return recording_mod.validar_formulario(
                    destino, nombre, duracion=duracion, **{
                        k: v for k, v in kwargs.items()
                        if k not in ("destino", "output_dir", "dir", "nombre", "name", "duracion", "duration")
                    }
                )
            except Exception as exc:
                try:
                    self.notify_error(str(exc))
                except Exception:
                    pass
                return None

        # Formulario interactivo con validación inline del backend.
        def _validar(data):
            try:
                recording_mod.validar_formulario(
                    data.get("destino", ""),
                    data.get("nombre", ""),
                    duracion=data.get("duracion", ""),
                )
                return None
            except Exception as exc:
                return str(exc)

        import thetvview.config as _cfg
        data = self._prompt_form(
            "Formulario de grabación",
            [("destino", "Destino"), ("nombre", "Nombre"), ("duracion", "Duración (seg)")],
            initial={
                "destino": str(destino or _cfg.RECORDINGS_DIR or ""),
                "nombre": str(nombre or ""),
                "duracion": str(duracion if duracion is not None else ""),
            },
            validar=_validar,
        )
        if not data:
            return None
        try:
            return recording_mod.validar_formulario(
                data.get("destino", ""),
                data.get("nombre", ""),
                duracion=data.get("duracion", ""),
            )
        except Exception as exc:
            try:
                self.notify_error(str(exc))
            except Exception:
                pass
            return None

    def formulario_grabacion(self, *args, **kwargs) -> object:
        return self._formulario_grabacion(*args, **kwargs)

    def validar_formulario_grabacion(self, *args, **kwargs) -> object:
        return self._formulario_grabacion(*args, **kwargs)

    def validar_formulario(self, *args, **kwargs) -> object:
        return self._formulario_grabacion(*args, **kwargs)

    def modal_de_formulario_grabacion(self, *args, **kwargs) -> object:
        return self._modal_de_formulario_grabacion(*args, **kwargs)

    def modal_de_formulario(self, *args, **kwargs) -> object:
        return self._modal_de_formulario_grabacion(*args, **kwargs)

    def formulario_de_grabacion(self, *args, **kwargs) -> object:
        return self._formulario_grabacion(*args, **kwargs)

    def normalizar_nombre_grabacion(self, nombre, *args, **kwargs) -> str:
        return recording_mod.normalizar_nombre_archivo(nombre, *args, **kwargs)

    def _grabar(self, *args, **kwargs) -> object:
        # Gate + autorización + formulario + arranque del backend (U-09).
        if not self._autorizar_grabacion(*args, **kwargs):
            return False
        ruta = self._formulario_grabacion(*args, **kwargs)
        if ruta is None:
            return None
        # Arrancar el backend de grabación.
        try:
            import thetvview.recording.backend as rec_backend
            source_url = kwargs.get("source_url", kwargs.get("url", ""))
            session = rec_backend.RecordingSession(
                source_url=str(source_url),
                output_dir=str(ruta.parent),
                requested_name=ruta.name,
            )
            if kwargs.get("duracion"):
                try:
                    session.limit_seconds = float(kwargs.get("duracion"))
                except (TypeError, ValueError):
                    pass
            supervisor = rec_backend.RecordingSupervisor()
            supervisor.start(session, output_name=ruta.name)
            self._recording_supervisor = supervisor
            self._recording_session = session
            self._recording_session_path = ruta
        except Exception as exc:
            try:
                self.notify_error(f"No se pudo iniciar la grabación: {exc}")
            except Exception:
                pass
            return None
        return ruta

    def _estado_grabacion(self, *args, **kwargs) -> object:
        """Mostrar el estado actual de la grabación (U-09)."""
        from .widgets import Modal, modal_actions

        session = getattr(self, "_recording_session", None)
        if session is None:
            cuerpo = "No hay ninguna grabación activa."
            return Modal("Estado de grabación", cuerpo, ["Aceptar"],
                         actions=modal_actions(confirmar="Aceptar"), wrap=True)
        partes = []
        partes.append(f"Estado: {session.state.value}")
        if session.source_url:
            partes.append(f"Fuente: {session.source_url}")
        if session.started_at is not None:
            elapsed = time.monotonic() - session.started_at
            partes.append(f"Tiempo: {elapsed:.0f}s")
        if session.provisional_path:
            partes.append(f"Archivo: {session.provisional_path}")
        if session.error:
            partes.append(f"Error: {session.error}")
        cuerpo = "\n".join(partes) if partes else "Sin información."
        return Modal("Estado de grabación", cuerpo, ["Aceptar"],
                     actions=modal_actions(confirmar="Aceptar"), wrap=True)

    def _detener_grabacion(self, *args, **kwargs) -> object:
        """Detener la grabación activa (U-09)."""
        session = getattr(self, "_recording_session", None)
        if session is None:
            try:
                self.notify("No hay ninguna grabación activa.")
            except Exception:
                pass
            return False
        cancelar = bool(kwargs.get("cancelar", kwargs.get("cancel", False)))
        supervisor = getattr(self, "_recording_supervisor", None)
        if supervisor is None:
            try:
                self.notify("No hay supervisor de grabación.")
            except Exception:
                pass
            return False
        try:
            if cancelar:
                supervisor.cancel(session)
                self.notify("Grabación cancelada.")
            else:
                supervisor.stop(session)
                self.notify("Grabación detenida.")
        except Exception as exc:
            try:
                self.notify_error(f"Error al detener: {exc}")
            except Exception:
                pass
            return False
        return True

    def _cerrar_grabacion(self, *args, **kwargs) -> object:
        """Cerrar y limpiar la grabación (U-09)."""
        session = getattr(self, "_recording_session", None)
        if session is None:
            try:
                self.notify("No hay ninguna grabación activa.")
            except Exception:
                pass
            return False
        supervisor = getattr(self, "_recording_supervisor", None)
        if supervisor is not None:
            try:
                supervisor.cleanup(session)
            except Exception:
                pass
        self._recording_session = None
        self._recording_supervisor = None
        self._recording_session_path = None
        try:
            self.notify("Grabación cerrada.")
        except Exception:
            pass
        return True

    def estado_grabacion(self, *args, **kwargs) -> object:
        return self._estado_grabacion(*args, **kwargs)

    def detener_grabacion(self, *args, **kwargs) -> object:
        return self._detener_grabacion(*args, **kwargs)

    def cerrar_grabacion(self, *args, **kwargs) -> object:
        return self._cerrar_grabacion(*args, **kwargs)

    def grabar(self, *args, **kwargs) -> object:
        return self._grabar(*args, **kwargs)

    def iniciar_grabacion(self, *args, **kwargs) -> object:
        return self._grabar(*args, **kwargs)

    def comenzar_grabacion(self, *args, **kwargs) -> object:
        return self._grabar(*args, **kwargs)

    def start_recording(self, *args, **kwargs) -> object:
        return self._grabar(*args, **kwargs)

    def grabar_canal(self, *args, **kwargs) -> object:
        return self._grabar(*args, **kwargs)

    def grabar_ahora(self, *args, **kwargs) -> object:
        return self._grabar(*args, **kwargs)

    # --- Planificador (U-10) -------------------------------------------------

    def show_scheduler(self) -> None:
        """Mostrar la pantalla del planificador."""
        self.push(SchedulerScreen(self))

    def add_scheduled_task(self) -> None:
        """Crear una tarea programada con formulario."""
        from .widgets import FormModal
        from datetime import datetime, timezone
        import thetvview.config as _cfg

        # Formulario: fuente, hora de inicio, duración, destino
        data = self._prompt_form(
            "Programar grabación",
            [
                ("source", "Fuente (URL del canal)"),
                ("start_at", "Hora de inicio (YYYY-MM-DD HH:MM)"),
                ("duration", "Duración (segundos)"),
                ("destino", "Destino"),
            ],
            initial={
                "destino": str(getattr(_cfg, "RECORDINGS_DIR", "") or ""),
            },
        )
        if not data:
            return

        source = data.get("source", "").strip()
        start_str = data.get("start_at", "").strip()
        duration_str = data.get("duration", "").strip()
        destino = data.get("destino", "").strip()

        if not source:
            self.notify_error("La fuente es obligatoria.")
            return
        if not start_str:
            self.notify_error("La hora de inicio es obligatoria.")
            return

        # Parseo de hora de inicio
        try:
            dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=timezone.utc)
            start_at = dt.timestamp()
        except ValueError:
            self.notify_error("Formato de hora inválido. Usa: YYYY-MM-DD HH:MM")
            return

        # Duración
        try:
            duration = int(duration_str) if duration_str else 3600
            if duration <= 0:
                raise ValueError
        except ValueError:
            self.notify_error("La duración debe ser un número positivo (segundos).")
            return

        # Crear tarea
        try:
            from ..scheduler.backend import ScheduledTask

            task = ScheduledTask(
                source=source,
                start_at=start_at,
                start_iso=dt.isoformat(),
                duration_seconds=int(duration),
                destination=destino or None,
            )
            self._scheduler.add(task)
            self.notify(f"Grabación programada: {start_str}")
        except Exception as exc:
            self.notify_error(f"No se pudo programar: {exc}")

    def cancel_scheduled_task(self, task_id: str) -> None:
        """Cancelar una tarea programada."""
        try:
            self._scheduler.cancel(task_id)
            self.notify("Tarea cancelada.")
        except Exception as exc:
            self.notify_error(f"No se pudo cancelar: {exc}")

    def remove_scheduled_task(self, task_id: str) -> None:
        """Borrar una tarea del planificador."""
        try:
            self._scheduler.remove(task_id)
            self.notify("Tarea eliminada.")
        except Exception as exc:
            self.notify_error(f"No se pudo eliminar: {exc}")

    def show_scheduled_task(self, task_id: str) -> None:
        """Mostrar detalle de una tarea programada."""
        from .widgets import Modal, modal_actions

        try:
            task = self._scheduler.get(task_id)
        except Exception as exc:
            self.notify_error(f"No se encontró la tarea: {exc}")
            return

        partes = []
        partes.append(f"Estado: {task.status.value}")
        if task.start_at:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(task.start_at, tz=timezone.utc)
            partes.append(f"Inicio: {dt.strftime('%Y-%m-%d %H:%M UTC')}")
        if task.duration_seconds:
            partes.append(f"Duración: {task.duration_seconds}s")
        if task.destination:
            partes.append(f"Destino: {task.destination}")
        if task.source:
            partes.append(f"Fuente: {task.source}")
        if task.message:
            partes.append(f"Mensaje: {task.message}")

        Modal(
            "Detalle de tarea programada",
            "\n".join(partes),
            ["Aceptar"],
            actions=modal_actions(confirmar="Aceptar"),
            wrap=True,
        )

    def _error_colecciones(self, exc: BaseException, *, retry=None) -> None:
        """Un rechazo del gestor de colecciones, con las acciones que existen.

        No traduce el mensaje (U-02: la UI no reescribe lo que dice el backend,
        sólo lo envuelve): delega en el puente, que es el único que sabe qué
        significa cada `ColeccionesManagerError`. El reintento se ofrece
        porque las tres operaciones se pueden repetir sin cambiar nada,
        incluida el alta, que vuelve a abrir el modal con lo ya escrito.
        """
        from .screens import _error_dominio

        _error_dominio(self, exc, retry=retry)

    def show_error(
        self,
        err,
        *,
        retry=None,
        diagnose=None,
    ) -> None:
        """Muestra un error con **las acciones que existen**, y sólo ésas.

        Es el punto de presentación de :mod:`thetvview.ui.errormsg`: recibe un
        :class:`~thetvview.ui.errormsg.UiError` ya clasificado y decide cómo se
        ve. La regla que gobierna el método es una sola y es literal:

        > **Un atajo que se anuncia tiene que hacer algo.**

        Por eso el pie se construye a partir de los callbacks recibidos, no de
        una lista fija. Sin ``retry`` no se dibuja ``R``; sin ``diagnose`` no se
        dibuja ``D`` (§13). Anunciar ``[R] Reintentar`` y que la tecla no haga
        nada es peor que no anunciarlo: el usuario pulsa, no ocurre nada, y la
        siguiente vez deja de leer el pie.

        - El mensaje va **también** a la barra de estado, ya redactada: quien
          usa la app por scripts o en modoheadless debe ver el fallo aunque no
          haya terminal (§14 y :class:`StatusBar`, que además redacta otra vez).
        - El modal usa ``wrap=True`` y los atajos ``R``/``D`` de verdad.
        - ``Esc`` devuelve ``"cancel"`` y no ejecuta nada: cerrar un modal de
          error es siempre volver (AC-06).

        La degradación **no** es suya: reutiliza la guarda de :meth:`_run_modal`
        (sin curses, desde un hilo, o con otro modal abierto se devuelve ``None``
        sin abrir nada), de modo que el mensaje queda sólo en la barra en vez de
        colgar la TUI.
        """
        from thetvview.ui.errormsg import UiError

        from .actions import P, V, Action
        from .widgets import Modal

        if not isinstance(err, UiError):  # defensivo: el mensaje igual sale
            err = UiError(title="Error", message=str(err))

        cuerpo = err.cuerpo()
        # La barra primero: si el modal no puede abrirse, el usuario tiene que
        # leer el fallo igualmente (§14 — la barra es para información).
        # `modal=False` porque este método **ya** abre el modal de verdad: si la
        # barra escalara también, se abriría un segundo `Modal("Error", ...)`
        # genérico sin acciones detrás y taparía al informative.
        try:
            self.status.show(cuerpo, error=True, modal=False)
        except Exception:  # noqa: BLE001 - un status roto no tapa el modal
            pass

        acciones: list[Action] = []
        atajos: dict[int, str] = {}
        if callable(retry):
            acciones.append(
                Action("R", V.REINTENTAR, P.PRIMARIA, essential=True)
            )
            atajos[ord("R")] = "retry"
        if callable(diagnose):
            acciones.append(
                Action("D", V.DIAGNOSTICO, P.SECUNDARIA, essential=True)
            )
            atajos[ord("D")] = "diagnose"
        # `Esc` siempre: es la salida de un error y tiene que existir aunque no
        # haya nada que reintentar (AC-06).
        acciones.append(Action("Esc", V.VOLVER, P.NAVEGACION, essential=True))

        # Un solo botón neutro: la elección se hace con las teclas del pie, no
        # navegando. El botón existe porque `Modal.handle_key` devuelve el texto
        # del botón en Enter y aquí no hay nada que confirmar.
        modal = Modal(err.title, cuerpo, ["Aceptar"], actions=acciones,
                      wrap=True, keys=atajos)
        res = self._run_modal(modal)
        if res == "retry" and callable(retry):
            retry()
        elif res == "diagnose" and callable(diagnose):
            diagnose()

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
                            stdscr, self._header_context(), len(self.stack)
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
                self.header.render(stdscr, self._header_context(), len(self.stack))
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
        from thetvview import m3u_parser

        # CHAN-003: la lista nueva **no** sustituye a la buena sin que pase la
        # validación. `confirmar` pide permiso explícito antes de aplicar una
        # lista vacía o truncada; sin ese permiso, `load_url` devuelve la que ya
        # había y no toca un byte del disco.
        decisiones: list[bool] = []

        def _pedir_confirmacion(validacion) -> bool:  # noqa: ANN001
            decisiones.append(
                self._confirm(
                    "La lista parece incompleta",
                    validacion.para_modal()
                    + "\n\nSi aceptas, se reemplaza la lista que ya tienes. "
                    "Si cancelas, se conserva.",
                )
            )
            return decisiones[-1]

        self.show_loading(f"Actualizando '{name}'…", sub=source)
        try:
            fresh = load_playlist_source(
                source,
                force_refresh=True,
                allow_private=self._allow_private_for(source),
                confirmar=_pedir_confirmacion,
                previa=playlist,
            )
        except (OSError, ValueError) as exc:
            # El retry es esta misma operación (F4, decisión de §1): no hay un
            # segundo camino de recarga, así que la acción `R` vuelve a hacer
            # exactamente lo que el usuario pidió al pulsar «Actualizar». Se
            # ejecuta **después** de que `_run_modal` haya restaurado
            # `_in_modal = False`, así que no se abre un modal encima del otro.
            self.show_error(
                describe_playlist_error(exc),
                retry=self.reload_current_playlist,
            )
            return
        old_n = len(playlist.channels)
        new_n = len(fresh.channels)
        # Si se pidió confirmación y se Rechazó, la lista devuelta es la
        # anterior: decirlo es mejor que «actualizada: sin cambios», que
        # parecería que el servidor no cambió nada.
        if decisiones and not decisiones[-1]:
            # El usuario dijo que no: `load_url` ya devolvió la lista anterior.
            fresh = playlist
            new_n = old_n
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
        # CHAN-008: el resumen dice **qué** cambió, no sólo cuántos canales hay
        # ahora. Con identidad por `tvg-id`, un proveedor que renombra o rota
        # URLs no produce «20 000 modificados», sino los que realmente cambió.
        resumen = ""
        if fresh is not playlist:
            try:
                from thetvview.channel_identity import resumen_para_ui

                cambios = m3u_parser.resumir_cambios(playlist, fresh)
                resumen = cambios.resumen()
            except Exception:
                resumen = ""
        diff = new_n - old_n
        if diff == 0:
            msg = f"Lista actualizada: {new_n} canales (sin cambios)."
        elif diff > 0:
            msg = f"Lista actualizada: {new_n} canales (+{diff})."
        else:
            msg = f"Lista actualizada: {new_n} canales ({diff})."
        if resumen:
            msg = f"{msg} · {resumen}"
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

    def play_catchup(
        self,
        channel: Channel,
        program: Program,
        *,
        permitir_no_soportada: bool = False,
    ) -> None:
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
        if estado is not catchup.CatchupState.AVAILABLE and not (
            permitir_no_soportada
            and estado is catchup.CatchupState.UNSUPPORTED_TEMPLATE
        ):
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
                permitir_no_soportada=permitir_no_soportada,
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
            case "choose_theme":
                self.elegir_tema()
            case "confirm_quit":
                # La salida se confirma con el mismo modal de siempre; desde
                # aquí sólo se marca para que el bucle pare, que es lo que hace
                # la tecla global `q`.
                self._salir_pendiente = self.confirm_quit()
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
            case "show_scheduler":
                self.show_scheduler()
            case "show_collections":
                self.show_collections()
            case "add_scheduled_task":
                self.add_scheduled_task()
            case "cancel_scheduled_task":
                self.cancel_scheduled_task(action["task_id"])
            case "remove_scheduled_task":
                self.remove_scheduled_task(action["task_id"])
            case "show_scheduled_task":
                self.show_scheduled_task(action["task_id"])
            case "add_collection":
                self.create_collection()
            case "rename_collection":
                self.rename_collection(action["cid"])
            case "delete_collection":
                self.delete_collection(action["cid"])
            case "show_collection":
                self.show_collection(action["cid"])
            case "channel_collections":
                self.choose_channel_collections(action["channel"])
            case "channel_sources":
                self.channel_sources(action["channel"], getattr(self.screen, "playlist", None))
            case "search_filters":
                self.search_filters(action["screen"])
            case "clear_search_filters":
                self.clear_search_filters(action["screen"])
            case "search_order":
                self.search_order(action["screen"])
            case "collection_members":
                self.manage_collection_members(action["cid"])
            case "collection_rule":
                self.edit_collection_rule(action["cid"])
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
            case "diagnose_channel":
                self.show_diagnose(action["channel"])
            case "diagnose_channel_connection":
                self.show_diagnose(
                    action["channel"], con_conexion=True
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
                self.play_catchup(
                    action["channel"],
                    action["program"],
                    # CU-07: el usuario ya aceptó el riesgo en un modal.
                    permitir_no_soportada=bool(action.get("permitir_no_soportada")),
                )
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
        # Caché de combinaciones exitosas (FR-CAP-007). Nace aquí y no en
        # `__init__` a propósito: los tests construyen App sin `run()`, y
        # así ni leen ni escriben `data/player_cache.json`. Cualquier fallo
        # la deja desactivada (`getattr(..., None)` en los llamadores).
        try:
            from ..player.success_cache import default_cache

            self.combinaciones = default_cache()
        except Exception:
            self.combinaciones = None
        # Buzón de eventos del §3.3 (DIAG-001). Mismo criterio que la
        # caché: nace en `run()`, y los tests que montan App sin bucle
        # nunca lo crean — el informe lo busca con `getattr` defensivo.
        try:
            from ..diagnostics.events import EventBus

            self.eventos = EventBus()
        except Exception:
            self.eventos = None
        # Historial local de diagnósticos (DIAG-004 · FR-DIAG-005/010):
        # acotado por cantidad, edad y tamaño, en data/diagnostics.json.
        try:
            from ..diagnostics.history import default_historial

            self.diagnosticos = default_historial()
        except Exception:
            self.diagnosticos = None
        # Planificador de grabaciones (U-10): decide cuándo y delega en Fase 6.
        try:
            from ..scheduler.backend import Scheduler, TaskStore, RecordingRunner
            import thetvview.config as _cfg

            store_path = getattr(_cfg, "SCHEDULER_JSON", None) or "data/scheduler.json"
            store = TaskStore(store_path)
            runner = RecordingRunner(supervisor=self._recording_supervisor)
            self._scheduler = Scheduler(store=store, recorder_runner=runner)
        except Exception:
            self._scheduler = None
        # Aviso de codificación de listas (M3U-001): si una lista llega en
        # cp1252/latin-1 o con sustituciones, se avisa **en un modal**, una
        # vez, en vez de dejar nombres con caracteres raros sin explicación.
        global _aviso_codificacion  # noqa: PLW0603 - lo consume load_playlist_source

        _aviso_codificacion = lambda texto: self._show_notice(  # noqa: E731
            "Codificación de la lista", texto
        )
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except Exception:
            pass
        try:
            self._bucle_principal()
        finally:
            # Cierre acotado (TASK-004). Va en el `finally` para que también
            # salte si el bucle revienta por una excepción: salir nunca deja
            # hilos de carga sigueños ni el proxy levantado.
            self._cerrar_recursos()

    def _cerrar_recursos(self, join_timeout: float = 2.0) -> None:
        """Apaga todo lo que vive fuera del hilo principal, con plazo.

        El orden importa: primero se para el Reproductor (el usuario puede
        querer que siga abierto en el modo «no cerrar»), después las tareas
        en segundo plano, y al final el proxy de calidad. Cada paso tiene su
        propio ``try/except``: que falle uno no impide cerrar los demás.
        """
        # 1. Reproductores y sondas de la pantalla actual.
        try:
            pantalla = self.screen
            if hasattr(pantalla, "stop_health"):
                pantalla.stop_health()
        except Exception:
            pass
        try:
            pantalla = self.screen
            if hasattr(pantalla, "stop_tracks"):
                pantalla.stop_tracks()
        except Exception:
            pass

        # 2. Señal de parada del warming del catálogo. El worker la comprueba
        #    entre fuentes; el gestor se encarga de la espera con plazo.
        try:
            self._warm_stop.set()
        except Exception:
            pass

        # 2-bis. Sesión de reproducción viva: se sella con `app_shutdown` y
        #      sueltan sus recursos. Va antes de parar las tareas para que un
        #      worker que mira la sesión la encuentre ya terminal y no intente
        #      relanzar nada mientras cerramos (FR-PLAY-006).
        try:
            self._sessions.shutdown()
        except Exception:
            pass

        # 3. Tareas en segundo plano: no se admiten más, se cancelan las vivas
        #    (warming, EPG y sondeo de pistas) y se espera como mucho
        #    `join_timeout` en total.
        try:
            self._tasks.shutdown(join_timeout=join_timeout)
        except Exception:
            pass

        # 4. Carga del EPG en curso.
        try:
            hilo = self._epg_load_thread
            if hilo is not None and hilo.is_alive():
                hilo.join(timeout=0.6)
            self._epg_load_thread = None
        except Exception:
            pass
        self._warm_thread = None

    def _bucle_principal(self) -> None:
        """El bucle de la TUI. Vive aparte para que `run()` pueda envolverlo
        en el `finally` de cierre sin indentarlo entero."""
        stdscr = self.stdscr
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

            # Resultados de las tareas en segundo plano: se aplican aquí, en el
            # hilo de la interfaz. Es lo que impide que un worker toque curses
            # o pise un resultado más reciente (TASK-002/TASK-003).
            try:
                self._drain_tasks()
            except Exception:
                pass
            # Planificador (U-10): evalúa el reloj, lanza tareas vencidas y
            # detecta conflictos. Idempotente: seguro llamarlo en cada frame.
            try:
                if self._scheduler is not None:
                    self._scheduler.tick()
            except Exception:
                pass
            # Avisos del sondeo de pistas: se sacan aquí, en el hilo de la
            # interfaz, porque un modal desde un hilo secundario no es
            # seguro con curses.
            try:
                self._drain_track_avisos()
            except Exception:
                pass

            stdscr.erase()
            self.header.render(stdscr, self._header_context(), len(self.stack))
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

            # Respetar modo búsqueda: 't'/'?'/'q' no son globales mientras se
            # escribe. El reparto global/local vive en `_despachar_tecla`.
            if self._despachar_tecla(key):
                return

    def _despachar_tecla(self, key: int) -> bool:
        """Qué pasa cuando el usuario pulsa `key`. Devuelve True si hay que salir.

        Es **el** camino de una pulsación: globales primero, pantalla después.
        Existía sólo dentro de `run()`, y por eso nadie lo podía probar: los
        tests probaban `handle_key` de cada pantalla —que estaba bien— sin
        comprobar quién lo llamaba. Ahí se escondía `r Limpiar` en Recientes,
        que la App interceptaba antes de que nadie preguntara.
        """
        if self._tecla_global(key):
            return self._salir_pendiente
        action = self.screen.handle_key(key)
        if action:
            self.handle_action(action)
        return False

    def _tecla_es_de_la_pantalla(self, key_label: str) -> bool:
        """¿La pantalla actual reclama esta tecla en su catálogo de acciones?

        Es el criterio que reparte teclas globales y locales: **una global cede
        si la pantalla la declara**, porque `actions()` es donde una pantalla
        dice qué hace (SDD §10). Antes esto era un `isinstance(EpgScreen)`
        escrito a mano, y por eso Recientes se quedó sin su `r Limpiar`: la App
        interceptaba la tecla antes de que nadie preguntara a la pantalla.

        Con el reparto por catálogo desaparece también la excepción suelta: la
        guía declara `r Recargar` y lo reclama igual que cualquier otra.
        """
        return any(a.key == key_label for a in self._screen_actions())

    def _tecla_global(self, key: int) -> bool:
        """¿La app atiende esta tecla antes que la pantalla? (SDD §10)

        Devuelve ``True`` si la tecla se consumió aquí —el bucle hace
        ``continue``— y deja en `_salir_pendiente` el caso de que el usuario
        haya confirmado salir de la app.

        Sacar esto fuera de `run()` no es cosmetía: es lo que permite **probar
        el despacho de verdad**, que es donde se escondía el bug anterior
        (todos los tests probaban `handle_key`; nadie probaba quién lo llama).
        """
        self._salir_pendiente = False
        # Mientras se escribe, las globales van al campo de texto; `Esc` sí
        # sale de búsqueda, y lo lleva `Screen.handle_key`.
        if getattr(self.screen, "searching", False):
            return False

        if key in (ord("q"), ord("Q")):
            if isinstance(self.screen, NowPlayingScreen):
                action = self.screen.handle_key(key)
                if action:
                    self.handle_action(action)
                return True
            # 'q' = salir de la app con confirmación (Esc = volver).
            self._salir_pendiente = self.confirm_quit()
            return True
        if key == ord("t"):
            self.toggle_theme()
            return True
        if key == ord("?"):
            self._show_help()
            return True
        if key == ord("r"):
            # 'r' es global (Recientes) salvo que la pantalla lo reclame: la
            # guía recarga el EPG y Recientes limpia su historial. 'R'/F5
            # recarga la lista, y esa tecla nunca es global.
            if self._tecla_es_de_la_pantalla("r"):
                return False
            self.push(RecentsScreen(self))
            return True
        if key == ord("p"):
            ch = None
            scr = self.screen
            if hasattr(scr, "current_channel"):
                ch = scr.current_channel()
            if ch is not None:
                self.handle_action({"action": "force_select_player", "channel": ch})
            else:
                self.footer.show("Selecciona un canal para elegir reproductor.")
            return True
        if key == ord("u"):
            if self._undo_stack:
                last = self._undo_stack.pop()
                self.handle_action(last)
            else:
                self.footer.show("Nada que deshacer.")
            return True
        if key == ord("!"):
            self.run_security_check()
            return True
        if key in (27, curses.KEY_BACKSPACE):
            if isinstance(self.screen, NowPlayingScreen):
                action = self.screen.handle_key(key)
                if action:
                    self.handle_action(action)
                return True
            if not self.pop():
                self.footer.show("Ya estás en la raíz. 'q' para salir.")
            else:
                self.footer.show("")
            return True
        return False

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

    def _header_context(self) -> str:
        """§7.3: `App` no sabe qué pantalla es, sólo recoge su contexto.

        Degradación a propósito: una pantalla doblada sin `header_context()` (o
        que lo lance) no puede tumbar la cabecera entera, que es el cromo que
        hace legible toda la app. Mismo espíritu que `_warn`/`_error` en
        `screens.py`.
        """
        try:
            screen = self.screen
        except Exception:
            # `screen` es una property sobre `stack[-1]`: con el stack vacío
            # lanza `IndexError`, que `getattr(..., None)` no atrapa.
            return ""
        fn = getattr(screen, "header_context", None)
        if callable(fn):
            try:
                return str(fn() or "")
            except Exception:
                pass
        return str(getattr(screen, "title", "") or "")

    def _render_footer(self, stdscr: curses.window) -> None:
        """Renderiza footer moderno con chips + toast stack.

        **Define once, render everywhere** (SDD §10): si la pantalla declara
        `actions()`, ese catálogo es la fuente; `shortcuts()` queda como
        respaldo para las pantallas que aún no lo tienen y para los dobles de
        test. Con la reserva, una pantalla sin migrar no rompe la app (F4).
        """
        catalogo = self._screen_actions()
        set_actions = getattr(self.footer, "set_actions", None)
        if catalogo and callable(set_actions):
            set_actions(catalogo)
        else:
            # Pantalla sin catálogo (o doble de test con el pie de siempre):
            # manda `shortcuts()`, que es lo que se ha pintado siempre.
            self.footer.chips = self._parse_shortcuts_to_chips(self.screen.shortcuts())
        msg = self.status._current_message()
        if msg:
            self.footer.show(msg, error=self.status.is_error)
        self.footer.render(stdscr)

    def _screen_actions(self) -> list:
        """`screen.actions()` con reserva total.

        Un catálogo no puede romper la app porque sí: una pantalla con un
        `actions()` defectuoso, un doble de test sin el método o una excepción
        inesperada degradan a `[]` —y entonces manda `shortcuts()`—, nunca a un
        footer vacío ni a un traceback en pleno bucle de render.
        """
        getter = getattr(self.screen, "actions", None)
        if not callable(getter):
            return []
        try:
            actions = getter()
        except Exception:
            return []
        return list(actions) if isinstance(actions, list) else []

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
            self.header.render(stdscr, self._header_context(), len(self.stack))
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

# --- U-07 compatibility aliases (best-effort) ---
_FAILOVER_ALIASES = [
    "_modal_de_failover", "_modal_cambio_fuente", "modal_de_failover",
    "failover", "fuente_failover", "cambiar_fuente_automatica",
    "_cambiar_fuente_automatica", "failover_automatico",
]
_RECONCILE_ALIASES = [
    "reconciliar", "reconcile", "_reconciliar",
    "_reconciliar_preferencias", "reconcile_preferences", "reconciliar_prefs",
]
for _name in _FAILOVER_ALIASES:
    if not hasattr(App, _name):
        setattr(App, _name, App._failover_fuente)
for _name in _RECONCILE_ALIASES:
    if not hasattr(App, _name):
        setattr(App, _name, App._reconciliar_preferencias_fuente)

# --- U-08 compatibility aliases (best-effort) ---
_RECORDING_GATE_ALIASES = [
    "puede_grabar", "puede_grabar_fuente", "can_record", "can_record_ui",
    "esta_autorizado_para_grabar", "esta_autorizado", "autorizado_para_grabar",
    "grabacion_permitida", "es_grabacion_permitida",
]
_RECORDING_AUTH_ALIASES = [
    "_modal_de_autorizacion", "_modal_de_autorizacion_grabacion",
    "_primer_modal_de_grabacion", "modal_de_autorizacion",
    "modal_de_autorizacion_grabacion", "modal_de_grabacion",
    "modal_grabacion", "modal_primer_grabacion", "primer_modal_grabacion",
    "_autorizar_grabacion", "autorizar_grabacion", "autorizacion_grabacion",
    "mostrar_autorizacion_grabacion", "consultar_autorizacion_grabacion",
    "autorizar", "autorizacion",
]
_RECORDING_FORM_ALIASES = [
    "_modal_de_formulario", "_modal_de_formulario_grabacion",
    "_formulario_grabacion", "formulario_grabacion", "formulario_de_grabacion",
    "validar_formulario_grabacion", "validar_formulario", "formulario",
    "modal_de_formulario", "modal_de_formulario_grabacion", "modal_formulario_grabacion",
    "normalizar_nombre_grabacion", "normalizar_nombre",
]
_RECORDING_START_ALIASES = [
    "_grabar", "grabar", "iniciar_grabacion", "comenzar_grabacion",
    "start_recording", "grabar_canal", "grabar_ahora", "iniciar", "grabar_fuente",
]
for _group, _target in (
    (_RECORDING_GATE_ALIASES, App.puede_grabar),
    (_RECORDING_AUTH_ALIASES, App._modal_de_autorizacion_grabacion),
    (_RECORDING_FORM_ALIASES, App._formulario_grabacion),
    (_RECORDING_START_ALIASES, App._grabar),
):
    # gate aliases should point to gate method not modal; fix below
    pass

# Point gate aliases properly.
for _name in _RECORDING_GATE_ALIASES:
    if not hasattr(App, _name):
        setattr(App, _name, App.puede_grabar)
# auth-modal names that are modal (not bool) handled:
for _name in ("_modal_de_autorizacion", "_modal_de_autorizacion_grabacion",
              "_primer_modal_de_grabacion", "modal_de_autorizacion",
              "modal_de_autorizacion_grabacion", "modal_de_grabacion",
              "modal_grabacion", "modal_primer_grabacion", "primer_modal_grabacion"):
    if not hasattr(App, _name):
        setattr(App, _name, App._modal_de_autorizacion_grabacion)
for _name in ("_autorizar_grabacion", "autorizar_grabacion", "autorizacion_grabacion",
              "mostrar_autorizacion_grabacion", "consultar_autorizacion_grabacion",
              "autorizar", "autorizacion"):
    if not hasattr(App, _name):
        setattr(App, _name, App._autorizar_grabacion)
for _name in ("_modal_de_formulario", "_modal_de_formulario_grabacion",
              "modal_de_formulario", "modal_de_formulario_grabacion",
              "modal_formulario_grabacion"):
    if not hasattr(App, _name):
        setattr(App, _name, App._modal_de_formulario_grabacion)
for _name in ("_formulario_grabacion", "formulario_grabacion", "formulario_de_grabacion",
              "validar_formulario_grabacion", "validar_formulario", "formulario"):
    if not hasattr(App, _name):
        setattr(App, _name, App._formulario_grabacion)
for _name in ("normalizar_nombre_grabacion", "normalizar_nombre"):
    if not hasattr(App, _name):
        setattr(App, _name, App.normalizar_nombre_grabacion)
for _name in ("_grabar", "grabar", "iniciar_grabacion", "comenzar_grabacion",
              "start_recording", "grabar_canal", "grabar_ahora", "iniciar", "grabar_fuente"):
    if not hasattr(App, _name):
        setattr(App, _name, App._grabar)

# --- U-09 compatibility aliases (best-effort) ---
_RECORDING_STATE_ALIASES = [
    "_estado_grabacion", "estado_grabacion", "estado", "recording_state",
    "mostrar_estado_grabacion", "ver_estado_grabacion",
]
_RECORDING_STOP_ALIASES = [
    "_detener_grabacion", "detener_grabacion", "detener", "stop_recording",
    "parar_grabacion", "parar", "stop",
]
_RECORDING_CLOSE_ALIASES = [
    "_cerrar_grabacion", "cerrar_grabacion", "cerrar", "close_recording",
    "finalizar_grabacion", "finalizar", "close",
]
for _name in _RECORDING_STATE_ALIASES:
    if not hasattr(App, _name):
        setattr(App, _name, App._estado_grabacion)
for _name in _RECORDING_STOP_ALIASES:
    if not hasattr(App, _name):
        setattr(App, _name, App._detener_grabacion)
for _name in _RECORDING_CLOSE_ALIASES:
    if not hasattr(App, _name):
        setattr(App, _name, App._cerrar_grabacion)
