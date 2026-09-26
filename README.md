# theTVVIEW

TUI (curses) en Python para explorar, organizar y reproducir listas IPTV con guía de programación (EPG).

- **Solo stdlib**: cero dependencias pip (`requirements.txt` vacío).
- Python 3.13+.
- Reproducción con **mpv**, **mplayer** o **vlc** (el que tengas instalado).

---

## Resumen

theTVVIEW es un visor de IPTV que vive en la terminal: le das una lista
(`.m3u`/`.m3u8`/`.ts` por ruta o URL, o una **Lista Especial X**) y te
navegas por canales, grupos, favoritos y parrilla EPG hasta elegir qué
ver y con qué reproductor.

| Área | Qué hace |
| --- | --- |
| **Catálogo** | Guarda varias listas en `data/playlists.json`; añadir/borrar con `a`/`d`, deshacer con `u`. |
| **Fuentes** | M3U/M3U8/TS local o `http(s)`, y Listas Especiales X (auth con test de conexión). |
| **Canales** | Lista con búsqueda incremental (`/`), favoritos (`f`), grupos (`g`), EPG (`e`). |
| **Calidad** | Selector de resolución **solo** si el canal tiene variantes reales. |
| **Reproductor** | Elige mpv/mplayer/vlc (solo se ofrecen los instalados) y reproduce. |
| **EPG** | XMLTV en ruta o URL, `.xml` o `.gz`, con cache y recarga (`r`). |
| **Extras** | Recientes, salud del canal en vivo, tema claro/oscuro, ayuda contextual, ratón. |
| **Rendimiento** | Cachés en disco con TTL + carga en segundo plano: abrir listas grandes es instantáneo. |

---

## Requisitos

- Python 3.13 o superior.
- Al menos un reproductor: `mpv`, `mplayer` o `vlc`.
- Entorno virtual recomendado (el proyecto usa `.env/`).
- Terminal con al menos **40 × 10** caracteres y, idealmente, 256 colores.

## Advertencia

- Éste proyecto recibió asistencia de OpenCode, en su plan free. No lo hizo completo, pero sí ayudó.Sé que harías un mejor trabajo sin IA, así que, sé educado.

---

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

## Inicio rápido

```bash
python -m thetvview
```

1. **Listas**: pulsa `a` y elige el tipo de fuente (`M3U / M3U8 / TS` o
   `Lista Especial X`). Rellena el formulario y Enter.
2. **Canales**: Enter sobre la lista para abrirla; navega con flechas
   (o `j`/`k`), busca con `/`, agrupa con `g`, marca favoritos con `f`.
3. **Reproducir**: Enter sobre el canal → (si tiene variantes) eliges
   calidad → eliges reproductor → se abre en otra ventana.
4. **Volver**: `Esc` retrocede; `q` sale de la app (con confirmación).

La ayuda contextual (`?`) explica siempre dónde estás y qué puedes hacer
ahí mismo; es el mejor punto de partida si dudas.

---

## Guía de funcionamiento

### Flujo general

```
Listas ──Enter──▶ Canales ──Enter──▶ [Calidad] ──▶ Reproductor ──▶ Reproduciendo
  │                  │                   │              │                │
  │                  ├── /  buscar       │  (solo si     │  (solo los     │  q detiene
  │                  ├── g  grupos       │   hay variantes) instalados)   │  y vuelve
  │                  ├── f  favoritos    │                              │
  │                  ├── e  guía EPG     │                              │
  │                  └── p  reproductor  │                              │
  │                                     │                              │
  ├── a añadir · d borrar · u deshacer   └── Esc ← vuelve en cualquier punto
  └── f favoritos · C contraseña Lista Especial X
```

### Listas (catálogo)

| Tecla | Acción |
| --- | --- |
| `Enter` | Abrir la lista seleccionada. |
| `a` | Añadir lista (elige M3U/M3U8/TS o Lista Especial X). |
| `d` | Borrar la lista (pide confirmación). |
| `u` | Deshacer el último borrado. |
| `C` | Cambiar la contraseña guardada (solo Listas Especiales X). |
| `R` / `F5` | Releer `playlists.json`. |
| `f` | Ver favoritos. |

Las Listas Especiales X se resaltan en púrpura; las M3U en amarillo.

### Canales

| Tecla | Acción |
| --- | --- |
| `Enter` | Ver el canal (→ selector de calidad si aplica). |
| `/` | Búsqueda incremental: la lista se filtra mientras escribes. |
| `g` | Ver los grupos (categorías) de esta lista. |
| `f` | Añadir/quitar el canal de favoritos (★). |
| `e` | Ver la guía EPG del canal. |
| `p` | Elegir reproductor para el canal. |
| `R` / `F5` | Actualizar la lista desde su origen. |

En modo búsqueda: `Enter` confirma el filtro, `Esc` lo limpia,
`Ctrl-U` vacía lo escrito.

### Grupos / Favoritos / Recientes

| Pantalla | Teclas |
| --- | --- |
| **Grupos** | `Enter` abrir · `/` buscar · `R` actualizar |
| **Favoritos** | `Enter` reproducir · `f` quitar · `p` reproductor |
| **Recientes** | `Enter` volver a ver · `f` guardar en favoritos · `r` borrar historial |

### Calidad y reproductor

- **Calidad**: solo aparece si el canal tiene variantes. Se detectan por
  atributos EXTINF (`res`/`quality`), sufijo en el nombre
  (`"Canal (720p)"`, `"Canal HD"`, `"Canal 1080i"`), marcadores entre
  corchetes ignorados al agrupar y sufijo de `tvg-id` estilo iptv-org
  (`id.es@HD`). Navega con `←`/`→` y confirma con `Enter`.
- **Reproductor**: tarjetas con los reproductores instalados;
  `↑`/`↓` + `Enter`. Si no hay ninguno, la app te lo dice en vez de fallar.

### Guía EPG

- La primera vez que la abres pide un XMLTV: ruta local (`.xml`/`.xmltv`
  o `.gz`) o URL `http(s)`.
- Si la playlist trae `x-tvg-url`, se ofrece como valor por defecto.
- `●` marca el programa que se está emitiendo ahora.
- `r` fuerza la recarga (en URLs re-descarga; en ficheros re-lee).
- Las URLs se cachean con TTL de 12 h.

### Teclas globales

| Tecla | Acción |
| --- | --- |
| `Esc` / `← Retroceso` | Volver a la pantalla anterior. |
| `q` | Salir de la app (pide confirmación; `s`/`y` sí, `n` no). |
| `?` | Abrir/cerrar la ayuda contextual (con scroll). |
| `t` | Alternar tema claro/oscuro (se guarda). |
| `r` | Ver Recientes (en la pantalla EPG, en su lugar recarga). |
| `p` | Elegir reproductor del canal actual. |
| `u` | Deshacer el último borrado de lista. |
| Ratón | Clic para seleccionar, doble clic para abrir. |

---

## Fuentes de canales

### M3U / M3U8 / TS

- Ruta local o URL `http://` / `https://` (típicamente `get.php` de paneles IPTV).
- Se aceptan `.m3u`, `.m3u8` y `.ts` sin tecnicismos adicionales.
- Se parsean atributos `tvg-id`, `tvg-name`, `tvg-logo`, `group-title`,
  marcas de radio, y tags `#EXTVLCOPT` / `#KODIPROP` (de los que solo se
  aplican a reproducción los de una lista blanca).

### Listas Especiales X

- Al añadir (`a` → `Lista Especial X`) se piden nombre, servidor, usuario y
  contraseña, y **se prueba la conexión** antes de guardar.
- La contraseña solo se persiste para Listas Especiales X (nunca para M3U) y el
  fichero se guarda con permisos `0600`.
- Si una lista vieja no tiene contraseña guardada, se pide al abrirla.
- `C` desde el catálogo cambia la contraseña: se valida contra el
  servidor antes de escribirla.
- En caché: autenticación y listados con TTL (ver abajo).

---

## Rendimiento y cachés

Las listas grandes (~90 000 canales) se abren rápido gracias a tres
mecanismos: caché en disco con TTL, caché en memoria por sesión y carga
del catálogo en un hilo aparte (`warm_catalog`) que no bloquea la UI.

| Qué | Dónde | Vigencia | Timeout |
| --- | --- | --- | --- |
| Playlist M3U por URL | `data/playlist_cache/` | TTL 6 h | 30 s |
| EPG XMLTV por URL | `data/epg_cache/` | TTL 12 h | 15 s |
| Lista Especial X: categorías | `data/xtream_cache/` | TTL 24 h | 10 s |
| Lista Especial X: canales | `data/xtream_cache/` | TTL 6 h | 10 s |
| Lista Especial X: EPG | `data/xtream_cache/` | TTL 2 h | 10 s |
| Playlist ya parseada | Memoria (sesión) | hasta 6 h / cambio de `mtime` | — |

- Si una URL remota falla pero hay caché previa, se usa la caché en vez
  de romper la experiencia.
- `R` / botón *Actualizar* fuerza la re-descarga (`force_refresh`).
- Una fuente del catálogo que falló se reintenta como muy pronto a los
  5 minutos, para no martillar servidores caídos.

---

## Datos

Todo lo persistido vive en `data/` (está en `.gitignore`):

| Archivo / carpeta | Contenido |
| --- | --- |
| `playlists.json` | Catálogo de listas (puede incluir contraseña de Lista Especial X; `0600`). |
| `favorites.json` | Canales favoritos (identidad = `url`). |
| `recents.json` | Últimos 20 elementos reproducidos. |
| `prefs.json` | Último reproductor usado y tema. |
| `theme.json` | Tema claro/oscuro. |
| `epg_cache/` | XMLTV descargados. |
| `playlist_cache/` | Playlists M3U descargadas. |
| `xtream_cache/` | Respuestas de la API de Listas Especiales X. |
| `ux_last.json` | Informe del runner de tests UX. |

Para empezar de cero, borra la carpeta `data/` (se recrea sola).

---

## Tests

```bash
# Suite completa (490 tests, ~35 s)
python -m unittest -v

# Solo tests de una parte
python -m unittest tests.test_m3u_parser -v
python -m unittest tests.test_epg_parser -v

# Informe visual de los tests de experiencia de usuario
python -m tests.ux_report            # tarjeta con barras de progreso
python -m tests.ux_report --verbose  # con tracebacks
```

Cobertura mínima exigida por el proyecto: parser M3U (archivo y URL),
parser XMLTV (incluye `.gz`) y persistencia JSON de playlists. El resto
cubre UI, Listas Especiales X, reproductores, layout, tema y recorridos UX completos.

---

## Estructura

El código vive en el paquete `thetvview/`:

```
thetvview/
├── __main__.py         # punto de entrada: python -m thetvview
├── models.py           # dataclasses: Channel, Playlist, Program
├── m3u_parser.py       # parser M3U/M3U8/TS (archivo y URL, cache TTL)
├── epg_parser.py       # parser XMLTV (.xml/.gz) con cache TTL
├── playlist_manager.py # catálogo de playlists (JSON, M3U + Especiales X)
├── favorites.py        # favoritos (JSON)
├── recents.py          # historial de recientes
├── prefs.py            # preferencias (tema, último reproductor)
├── resolutions.py      # detección/agrupación de variantes de calidad
├── groups.py           # agrupación por group-title
├── channel_health.py   # salud del stream en vivo (hilo no bloqueante)
├── player.py           # lanzamiento de mpv/mplayer/vlc (sin shell)
├── config.py           # rutas, detección de reproductores, tema
├── xtream_*.py         # Listas Especiales X: cliente, provider, modelos, EPG,
│                       # errores, seguridad y config
└── ui/
    ├── app.py          # bucle principal, stack de pantallas, ayuda
    ├── screens.py      # todas las pantallas
    ├── widgets.py      # listas, modales, formularios, toast, loading
    ├── layout.py       # rectángulos (header/main/footer)
    ├── theme.py        # paletas dual light/dark (256/8 colores, NO_COLOR)
    ├── colors.py       # IDs de pares de color
    └── icons.py        # iconos y cajas Unicode
tests/                  # unittest + fixtures (m3u, xmltv, .gz)
data/                   # datos locales (no versionados)
```

### Arquitectura en una frase

`pantallas (ui) → acciones (dict) → App.handle_action → módulos de
lógica → persistencia`, con la terminal siempre protegida por
`curses.wrapper`, `KEY_RESIZE` manejado y excepciones de dibujo
degradando en silencio.

---

## Solucionar problemas

| Síntoma | Qué hacer |
| --- | --- |
| *No hay reproductor disponible* | Instala `mpv`, `vlc` o `mplayer`. La app busca también fuera del `PATH` (Program Files, Homebrew, snap/flatpak…). |
| *Sin correspondencia EPG* | El `tvg-id` del canal no está en el XMLTV. Prueba otra fuente (`e` → `r`). |
| *Lista vacía* | Revisa que el fichero tenga canales; los `.ts` también valen. |
| *Ventana demasiado pequeña* | Agranda la terminal (mínimo 40 × 10). |
| *Una URL no responde* | Espera al TTL o fuerza recarga con `R`; si hay caché previa se usa esa. |
| *Contraseña de Lista Especial X cambiada en el servidor* | `C` en el catálogo para actualizarla. |
| *Colores raros* | Se usan 256 colores si existen y 8 si no; `NO_COLOR=1` los desactiva por completo. |
| *¿De dónde salen los datos?* | Borra `data/` para reiniciar de cero. |

---

## Seguridad

- `subprocess` **nunca** con `shell=True`: siempre argv en lista.
- Las descargas (`urllib`) llevan timeout y devuelven errores amigables.
- Las contraseñas solo se guardan para Listas Especiales X, en
  `data/playlists.json` con permisos `0600`, y no se muestran en la UI.
- Las URLs con `password=` se redactan en los mensajes de error.
- Solo se pasan al reproductor opciones `EXTVLCOPT` de una lista blanca.

---

## Licencia

MIT — ver [LICENSE](LICENSE).
