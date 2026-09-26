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
