# theTVVIEW

TUI (curses) en Python para explorar y reproducir listas IPTV con guía EPG.

- **Solo stdlib**: cero dependencias pip (`requirements.txt` vacío).
- Python 3.13+.

## Requisitos

- Python 3.13 o superior.
- Reproductor multimedia instalado (mpv, mplayer o vlc).
- Entorno virtual recomendado (el proyecto usa `.env/`).

## Advertencia

- Éste proyecto recibió asistencia de OpenCode, en su plan free. No lo hizo completo, pero sí ayudó.Sé que harías un mejor trabajo sin IA, así que, sé educado.


## Instalación

```bash
# Clonar el repositorio
git clone https://github.com/rrivillo/theTVVIEW.git
cd theTVVIEW

# Crear entorno virtual (opcional pero recomendado)
python -m venv .env
source .env/bin/activate  # Linux/Mac
# .env\Scripts\activate   # Windows (no he probado en esta plataforma. Usen bajo su propio riesgo). 

# No hay dependencias externas, solo stdlib
```

## Uso

```bash
python -m thetvview
```

1. **Playlists**: pulsa `a` para añadir una lista M3U/M3U8 (ruta local o URL
   http/https) y `d` para borrarla. Enter abre la lista seleccionada.
2. **Canales**: navega con flechas (o `j`/`k`), busca en vivo con `/`,
   marca favoritos con `f`, mira la parrilla EPG con `e` y abre los grupos
   con `g`.
3. **Reproducir**: Enter sobre un canal. Si el canal tiene variantes de
   resolución, se elige primero la resolución; después se elige el
   reproductor (mpv/mplayer/vlc, solo se ofrecen los instalados).
   Las resoluciones se detectan por atributos EXTINF (`res`/`quality`),
   sufijo en el nombre ("Canal (720p)", "Canal HD") o tvg-id (`id@HD`).
4. **Favoritos**: accesibles con `f` desde el catálogo de playlists.
5. **EPG**: al verlo por primera vez pide un XMLTV (`.xml`/`.gz`, ruta o
   URL). Las URLs se cachean en `data/epg_cache/` con TTL de 12 h;
   `r` fuerza la recarga.

Tecla `q` o Esc para volver atrás / salir.

## Datos

Todo se guarda en `data/`: `playlists.json` (catálogo),
`favorites.json` (favoritos) y `epg_cache/` (cache del EPG).

## Tests

```bash
python -m unittest -v
```

## Estructura

```
thetvview/
├── models.py           # dataclasses: Channel, Playlist, Program
├── m3u_parser.py       # parser M3U/M3U8 (archivo y URL)
├── epg_parser.py       # parser XMLTV (.xml/.gz) con cache TTL
├── playlist_manager.py # catálogo de playlists (JSON)
├── favorites.py        # favoritos (JSON)
├── resolutions.py      # detección/agrupación de resoluciones
├── groups.py           # agrupación por group-title
├── player.py           # lanzamiento de mpv/mplayer/vlc (subprocess seguro)
├── config.py           # rutas y detección de reproductores
└── ui/                 # capa curses: app, screens, widgets, colors
tests/                  # unittest + fixtures
```

## Licencia

Este proyecto no tiene licencia definida. Todos los derechos reservados.
