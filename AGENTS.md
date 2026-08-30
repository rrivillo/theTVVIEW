# theTVVIEW — reglas del repo (OpenCode)

## Objetivo
Construir una TUI (curses) en Python (solo stdlib) para:
- Cargar playlists M3U/M3U8 desde archivo o URL
- Cargar EPG XMLTV (posiblemente .gz) con cache y TTL
- Gestionar playlists y favoritos (JSON)
- Lanzar reproductores externos (mpv/mplayer/vlc) con subprocess

## Restricciones NO negociables
- Python 3.13.5
- CERO dependencias pip (requirements.txt vacío).
- No usar `shell=True` en subprocess.
- Descargas con `urllib` deben tener timeout y errores amigables.
- Evitar cuelgues de curses (manejar resize y excepciones).

## Convenciones de código
- Dataclasses para modelos (Channel, Playlist, Program, etc.).
- Tipos (type hints) en APIs públicas.
- Separar "lógica" (parsers/managers) de "UI" (curses) de "player".
- Mantener módulos pequeños y testeables (unittest).

## Cómo verificar (mínimo)
- `python -m unittest -v` debe pasar.
- Tests mínimos para:
  - `m3u_parser.parse_file` y/o `parse_url` (con fixtures pequeñas).
  - `epg_parser.parse_file` (incluye .gz si aplica).
  - `playlist_manager` persistencia JSON.
