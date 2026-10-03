"""Punto de entrada CLI de theTVVIEW (solo stdlib).

Uso:
    python -m thetvview

Overrides de una sesión (opcionales, SDD §43 del plan de pistas):
    python -m thetvview --audio es --subtitles es
    python -m thetvview --subtitles off --quality 720p
    python -m thetvview --audio en --quality auto

Sólo Duran lo que dura el proceso: no tocan `prefs.json` ni ningún flujo
existente. Un valor no reconocido se explica y se sale con código 2, en vez
de ignorarse en silencio.

Al arrancar se pregunta internamente el sistema operativo (Linux/Windows/…),
se prepara la consola según corresponda y se verifica que curses exista antes
de cargar la TUI. Así, en un Python de Windows sin windows-curses se ve un
aviso con la solución en vez de un traceback ModuleNotFoundError.
"""

from __future__ import annotations

import sys

from .platform_check import curses_problem, prepare_console, resolve_os
from .tracks.cli import OVERRIDE_OPTIONS, parse_overrides, set_session_overrides


def _launch() -> None:
    """Importa y ejecuta la TUI (diferido: solo tras pasar el preflight)."""
    from .ui.app import main

    main()


def run(argv: list[str] | None = None) -> int:
    """Arranque multiplataforma. Devuelve el código de salida."""
    args = list(sys.argv[1:] if argv is None else argv)
    overrides, error = parse_overrides(args)
    if error:
        print(error, file=sys.stderr)
        print(f"Opciones disponibles: {', '.join(sorted(OVERRIDE_OPTIONS))}",
              file=sys.stderr)
        return 2
    set_session_overrides(overrides)

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
