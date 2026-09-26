"""Punto de entrada CLI de theTVVIEW (solo stdlib).

Uso:
    python -m thetvview

Al arrancar se pregunta internamente el sistema operativo (Linux/Windows/…),
se prepara la consola según corresponda y se verifica que curses exista antes
de cargar la TUI. Así, en un Python de Windows sin windows-curses se ve un
aviso con la solución en vez de un traceback ModuleNotFoundError.
"""

from __future__ import annotations

import sys

from .platform_check import curses_problem, prepare_console, resolve_os


def _launch() -> None:
    """Importa y ejecuta la TUI (diferido: solo tras pasar el preflight)."""
    from .ui.app import main

    main()


def run() -> int:
    """Arranque multiplataforma. Devuelve el código de salida."""
    system = resolve_os()
    prepare_console(system)
    problem = curses_problem(system)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    try:
        _launch()
    except KeyboardInterrupt:
        return 0  # Ctrl+C: la terminal ya la restauró curses.wrapper
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
