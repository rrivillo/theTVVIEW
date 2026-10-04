"""Detección interna del sistema operativo y arranque multiplataforma.

`python -m thetvview` se pregunta internamente en qué SO está ejecutando
(Linux, Windows, macOS…) y arranca en consecuencia:

1. ``resolve_os()``      → detecta el SO (solo pregunta al usuario si la
   detección automática queda en desconocido, p. ej. FreeBSD).
2. ``prepare_console()`` → deja la consola en UTF-8 (crítico en Windows,
   donde por defecto la consola no habla UTF-8).
3. ``curses_problem()``  → verifica *antes* de importar la UI que curses
   exista y que haya TTY; si no, devuelve un aviso legible en vez del
   traceback ``ModuleNotFoundError: No module named '_curses'`` típico
   del Python oficial de Windows (que no distribuye el módulo C).

Solo stdlib. Ninguna función lanza excepciones por problemas del entorno:
todo se degrada a un mensaje de texto.
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Callable, TextIO

OS_LINUX = "linux"
OS_WINDOWS = "windows"
OS_MACOS = "macos"
OS_UNKNOWN = "unknown"

# Nombres que platform.system() puede devolver y que significan Windows:
# los exactos y los prefijos de los entornos shell que corren sobre Windows.
_WINDOWS_ALIASES = frozenset({"windows", "win32", "win"})
_WINDOWS_PREFIXES = ("mingw", "msys", "cygwin")


def _is_windows_name(name: str) -> bool:
    return name in _WINDOWS_ALIASES or name.startswith(_WINDOWS_PREFIXES)


def detect_os(system: str | None = None) -> str:
    """Determina internamente el SO: 'linux' | 'windows' | 'macos' | 'unknown'.

    Args:
        system: valor a evaluar (para tests). None usa platform.system().
    """
    raw = platform.system() if system is None else system
    name = (raw or "").strip().lower()
    if _is_windows_name(name):
        return OS_WINDOWS
    if name == "linux":
        return OS_LINUX
    if name == "darwin":
        return OS_MACOS
    # Fallback cuando platform.system() no es fiable o viene vacío
    if os.name == "nt":
        return OS_WINDOWS
    if sys.platform == "darwin":
        return OS_MACOS
    if name:
        return OS_UNKNOWN  # p. ej. FreeBSD/SunOS: no afirmamos sin más
    if os.name == "posix":
        return OS_LINUX
    return OS_UNKNOWN


def resolve_os(
    system: str | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output: TextIO | None = None,
) -> str:
    """Resuelve el SO para el arranque: detección interna + duda como último recurso.

    Si la detección automática es inequívoca no se interactúa con nadie.
    Solo si queda 'unknown' se pregunta al usuario (con predeterminado
    razonable y sin romper en entornos sin stdin).
    """
    detected = detect_os(system)
    if detected != OS_UNKNOWN:
        return detected
    out = output if output is not None else sys.stderr
    default = OS_WINDOWS if os.name == "nt" else OS_LINUX
    hint = "W" if default == OS_WINDOWS else "L"
    try:
        out.write(
            "theTVVIEW no pudo identificar tu sistema operativo automáticamente.\n"
        )
        out.flush()
        answer = input_fn(f"¿Estás usando Linux o Windows? [{hint}]: ")
    except (EOFError, KeyboardInterrupt):
        return default
    option = (answer or "").strip().lower()
    if option.startswith("w"):
        return OS_WINDOWS
    if option.startswith("l"):
        return OS_LINUX
    return default


def prepare_console(system: str) -> None:
    """Deja la consola lista para la TUI (UTF-8; en Windows además code page)."""
    _force_utf8(sys.stdout)
    _force_utf8(sys.stderr)
    if system == OS_WINDOWS:
        _windows_console_utf8()


def _force_utf8(stream: TextIO) -> None:
    """Reconfigura `stream` a UTF-8 si no lo está ya (degrada en silencio)."""
    try:
        if not hasattr(stream, "reconfigure"):
            return
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding in ("utf8", "utf_8"):
            return
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        pass


def _windows_console_utf8() -> None:
    """Activa la code page UTF-8 (65001) de la consola de Windows."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass  # degrada: la TUI sigue pudiendo funcionar


def curses_problem(system: str) -> str | None:
    """Aviso legible si no se puede arrancar la TUI; None si todo va bien.

    Comprueba (a) que `curses` sea importable y (b) que exista una terminal
    interactiva. Nunca lanza: devuelve el texto a mostrar por el llamador.
    """
    try:
        import curses  # noqa: F401  (solo verificación de disponibilidad)
    except Exception as exc:
        return _missing_curses_message(system, exc)
    if not _is_interactive_terminal():
        return _no_tty_message()
    return None


def _is_interactive_terminal() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def multicast_supported(system: str | None = None) -> bool:
    """True si este SO y esta máquina pueden llevar tráfico multicast (Fase 6).

    Tres capas, de la más barata a la más cara, y se para en la primera que
    falla:

    1. **el SO lo soporta** —Windows, macOS y Linux sí; un SO desconocido no se
       afirma porque no se comprueba (:func:`detect_os` devuelve ``unknown`` a
       propósito, y adivinar aquí sería lo contrario de lo que hace el resto del
       módulo);
    2. **esta máquina tiene interfaces con la bandera MULTICAST**;
    3. **se puede abrir un socket multicast** y unirse a un grupo de prueba.

    La tercera capa es la que de verdad informa: hay máquinas Linux con la
    bandera puesta y sin ruta multicast, que es exactamente el caso que se
    cuenta en el módulo :mod:`thetvview.player.protocols` (donde el multicast
    quedó «no verificado»). Nunca lanza: cualquier duda se resuelve con
    ``False``, y un ``False`` equivocado se traduce en «compruébalo tú», no en
    un error.

    .. warning::
       Esto responde «¿puede esta máquina?», no «¿llega este grupo?». Lo
       segundo depende de la red (router, Wi-Fi, firewall) y no lo puede
       decidir ningún ``getsockopt``; por eso el §14 del SDD-M exige separar
       los dos mensajes.
    """
    so = detect_os(system)
    if so not in (OS_LINUX, OS_MACOS, OS_WINDOWS):
        return False
    try:
        # (2) Alguna interfaz con la bandera de multicast.
        interfaces = _interfaces_con_multicast()
        if not interfaces:
            return False
        # (3) Socket de prueba: unirse a un grupo y salir sin enviar nada.
        return _puede_unirse_a(interfaces)
    except Exception:  # noqa: BLE001 - una duda se resuelve con «no»
        return False


def _puede_unirse_a(interfaces: list[str]) -> bool:
    """¿Se puede unir a un grupo multicast por alguna de esas interfaces?

    Va en su propia función por dos razones: es lo único del módulo que abre
    un socket, y así se puede sustituir en los tests sin tener que parchear
    ``socket.socket`` a nivel de módulo.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        grupo = socket.inet_aton(_GRUPO_PRUEBA)
        for interfaz in interfaces:
            try:
                s.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_ADD_MEMBERSHIP,
                    grupo + socket.inet_aton(interfaz),
                )
                return True
            except OSError:
                continue
        return False
    finally:
        s.close()


#: Grupo que se usa sólo para probar la pertenencia. Está en el rango
#: administrativo (239.255.0.0/16, RFC 6034) justamente para que nadie pueda
#: tener tráfico real en él: unirse es inocuo.
_GRUPO_PRUEBA = "239.255.255.254"


def _interfaces_con_multicast() -> list[str]:
    """Direcciones IPv4 de las interfaces locales con la bandera MULTICAST.

    Sólo stdlib, como todo el repo: se pregunta al SO por sus interfaces
    (``getaddrinfo`` con el nombre de host y ``socket.if_nameindex``, que
    existen en las tres plataformas) y se usa :func:`_interfaz_por_defecto`
    como respaldo. Si no se puede averiguar nada, se lista vacía y
    :func:`multicast_supported` responde False, que es la respuesta honesta.

    Importar ``psutil`` aquí estaría bien en cualquier otro proyecto y está
    **mal** en este: el security-check falla si aparece un import que no sea del
    stdlib, y ``requirements.txt`` tiene que seguir vacío.
    """
    direcciones: list[str] = []
    try:
        import socket

        for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        ):
            # sockaddr es una tupla heterogénea según la familia; en AF_INET
            # el primer elemento es la dirección, pero el tipo declarado es
            # `str | int`, así que se normaliza en vez de suponer.
            direccion = str(info[4][0])
            if direccion and not direccion.startswith("127."):
                if direccion not in direcciones:
                    direcciones.append(direccion)
    except Exception:
        pass
    if not direcciones:
        return _interfaz_por_defecto()
    return direcciones


def _interfaz_por_defecto() -> list[str]:
    """IPv4 de la ruta por defecto, si la hay."""
    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # No envía nada: connect en UDP sólo elige la ruta local.
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, RFC 5737
            return [s.getsockname()[0]]
    except Exception:
        return []


def _missing_curses_message(system: str, exc: Exception) -> str:
    header = [
        "theTVVIEW no pudo cargar el módulo 'curses' (interfaz de terminal).",
        f"Detalle: {type(exc).__name__}: {exc}",
        "",
    ]
    if system == OS_WINDOWS:
        body = [
            "En Windows, el Python oficial no incluye curses. Instala el",
            "complemento que lo aporta (una sola vez, con el venv activo):",
            "",
            "    pip install windows-curses",
            "",
            "y vuelve a lanzar:  python -m thetvview",
            "",
            "Alternativa sin instalar nada: ejecuta theTVVIEW bajo WSL (Linux),",
            "donde curses ya viene incluido.",
        ]
    else:
        body = [
            "Falta el soporte de ncurses en tu Python.",
            "",
            "  Debian/Ubuntu:  sudo apt install python3-curses",
            "  macOS:          xcode-select --install",
            "",
            "En el resto de sistemas instala el paquete de ncurses de tu gestor",
            "o reinstala Python con soporte de curses.",
        ]
    return "\n".join(header + body)


def _no_tty_message() -> str:
    return "\n".join(
        [
            "theTVVIEW necesita una terminal interactiva (TTY) y no la detectó.",
            "Ejecútalo desde una consola real (PowerShell, CMD, bash, Windows",
            "Terminal…), no desde un IDE, un pipe ni una tarea programada.",
        ]
    )
