# theTVVIEW

TUI (curses) en Python para explorar, organizar y reproducir listas IPTV con guía de programación (EPG).

- **Solo stdlib**: cero dependencias pip (`requirements.txt` vacío).
- Python 3.13+.
- Reproducción con **mpv**, **mplayer** o **vlc** (el que tengas instalado).

---

## Resumen

theTVVIEW es un visor de IPTV que vive en la terminal: le das una lista
(`.m3u`/`.m3u8`/`.ts` por ruta o URL, o unas **Listas Especiales X**) y te
navegas por canales, grupos, favoritos y parrilla EPG hasta elegir qué
ver y con qué reproductor.

| Área | Qué hace |
| --- | --- |
| **Catálogo** | Guarda varias listas en `data/playlists.json`; añadir/borrar con `a`/`d`, deshacer con `u`. |
| **Fuentes** | M3U/M3U8/TS local o `http(s)`, y Listas Especiales X (auth con test de conexión). |
| **Canales** | Lista con búsqueda incremental (`/`), favoritos (`f`), grupos (`g`), EPG (`e`). |
| **Calidad** | Selector de resolución **solo** si el canal tiene variantes reales. |
| **Reproductor** | Elige mpv/mplayer/vlc (solo se ofrecen los instalados) y reproduce. |
| **EPG** | XMLTV en ruta o URL, `.xml` o `.gz`, con cache y recarga (`r`); si la lista lo trae, se carga solo. |
| **Archivo** | Catch-up solo si el proveedor lo declara (ver la sección dedicada). |
| **Extras** | Recientes, salud del canal en vivo, tema claro/oscuro, ayuda contextual, ratón. |
| **Rendimiento** | Cachés en disco con TTL + carga en segundo plano: abrir listas grandes es instantáneo. |

---

## Requisitos

- Python 3.13 o superior.
- Al menos un reproductor: `mpv`, `mplayer` o `vlc`.
- Entorno virtual recomendado (el proyecto usa `.env/`).
- Terminal con al menos **40 × 10** caracteres y, idealmente, 256 colores.
- **Windows**: `pip install windows-curses` (único extra y solo ahí) porque el
  Python oficial no incluye `curses`. Si falta, el programa lo avisa al arrancar.

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
source .env/bin/activate   # Linux/Mac
# .env\Scripts\activate    # Windows

# No hay dependencias externas en Linux/macOS, solo stdlib
# Windows (una sola vez, con el venv activo):
#   pip install windows-curses
```

Al arrancar, `python -m thetvview` **detecta por su cuenta el sistema
operativo** (Linux, Windows, macOS), deja la consola en UTF-8 según
corresponda y comprueba que `curses` exista *antes* de cargar la interfaz:
si falta algo muestra un aviso con la solución, nunca un traceback.

## Inicio rápido

```bash
python -m thetvview
```

1. **Listas**: pulsa `a` y elige el tipo de fuente (`M3U / M3U8 / TS` o
   `Listas Especiales X`). Rellena el formulario y Enter.
2. **Canales**: Enter sobre la lista para abrirla; navega con flechas
   (o `j`/`k`), busca con `/`, agrupa con `g`, marca favoritos con `f`.
3. **Reproducir**: Enter sobre el canal → (si tiene variantes) eliges
   calidad → eliges reproductor → se abre en otra ventana.
4. **Volver**: `Esc` retrocede; `q` sale de la app (con confirmación).

La ayuda contextual (`?`) explica siempre dónde estás y qué puedes hacer
ahí mismo; es el mejor punto de partida si dudas.

### Reglas de interacción

- **Todo error y todo aviso sale en un modal**, con una explicación corta y
  accionable. La barra de estado es el refuerzo, nunca el único sitio donde
  se entera el usuario.
- **Ningún fallo se muestra como traceback**: los problemas del entorno
  (sin TTY, sin `curses`, terminal diminuta) se traducen a un mensaje con la
  solución.
- **La ayuda es contextual**: `?` describe la pantalla en la que estás, no un
  manual genérico.
- La app nunca bloquea la interfaz: descargas, parseos grandes y sondas de red
  van en hilos y la pantalla se repinta mientras tanto.

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
  └── f favoritos · C contraseña Listas Especiales X
```

### Navegación (común a casi todas las pantallas)

| Tecla | Acción |
| --- | --- |
| `↑` / `↓` o `k` / `j` | Subir o bajar. En las rejillas de tarjetas se mueve de tarjeta en tarjeta. |
| `RePág` / `AvPág` | Saltar una pantalla. |
| `Inicio` / `Fin` o `g` / `G` | Primero o último elemento. |
| `Enter` | Abrir lo seleccionado. |
| `Esc` / `Retroceso` | Volver a la pantalla anterior. |

En las búsquedas (`/`): `Enter` confirma el filtro, `Esc` lo limpia y
`Ctrl-U` vacía lo escrito sin cerrar el campo.

### Listas (catálogo)

| Tecla | Acción |
| --- | --- |
| `Enter` | Abrir la lista seleccionada. |
| `a` | Añadir lista (elige M3U/M3U8/TS o Listas Especiales X). |
| `d` | Borrar la lista (pide confirmación en un modal). |
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

El título muestra el nombre de la lista, el grupo (sólo cuando estás dentro
de uno), los canales visibles/totales y un `★` si esa lista tiene favoritos.
Los canales de radio se marcan con `♪`. A partir de 70 columnas (y de 80
para el reparto ancho) aparece un panel lateral con los datos del canal
seleccionado: grupo, URL, días de archivo si los declara y el programa actual
si el EPG está cargado.

### Grupos / Favoritos / Recientes

| Pantalla | Teclas |
| --- | --- |
| **Grupos** | `Enter` abrir · `/` buscar · `R` actualizar |
| **Favoritos** | `Enter` reproducir · `f` quitar · `p` reproductor |
| **Recientes** | `Enter` volver a ver · `f` guardar en favoritos · `r` borrar historial |

Los canales sin `group-title` se agrupan al final como `(sin grupo)`. El
catálogo y la pantalla de grupos muestran tarjetas en rejilla: 1 columna por
defecto, 2 a partir de 100 caracteres de ancho y 3 a partir de 150.

### Calidad y reproductor

- **Calidad**: solo aparece si el canal tiene variantes. Se detectan por
  atributos EXTINF (`res`/`quality`), sufijo en el nombre
  (`"Canal (720p)"`, `"Canal HD"`, `"Canal 1080i"`), marcadores entre
  corchetes ignorados al agrupar y sufijo de `tvg-id` estilo iptv-org
  (`id.es@HD`). Se reconocen `SD`, `HD`, `FHD`, `QHD`, `UHD`, `4K` y `8K`.
  Navega con `←`/`→` y confirma con `Enter`.
- **Reproductor**: tarjetas con los reproductores instalados;
  `↑`/`↓` + `Enter`. Si no hay ninguno, la app te lo dice en vez de fallar.

### Cómo se lanza la reproducción

- El reproductor se lanza con `subprocess` y **argv en lista**, nunca con
  `shell=True`, y el argv termina siempre en `-- <url>` para que una URL
  hostile no se interprete como opción.
- Antes de construir nada, la URL se valida contra la política de esquemas
  (`http`/`https`): un `file://`, un `javascript:` o una URL que empiece por
  `-` no llegan al reproductor.
- Se prioriza **h264** (el estándar de facto en IPTV): `--vd=ffh264` en mpv,
  `-vc ffh264,ffmpeg2,…` en mplayer, `--avcodec-codec=h264` en vlc.
- El nombre del canal se pasa como título de la ventana del reproductor
  (saneado: sin saltos, sin no imprimibles, máx. 120 caracteres).
- De los `#EXTVLCOPT` / `#KODIPROP` de la lista sólo se traducen
  `http-user-agent` y `http-referrer` (con sus alias). El resto se ignora con
  un aviso: nunca se pasa texto arbitrario al reproductor.
- Si el reproductor muere en menos de 4 s, se explica que el stream no llegó
  a abrir (lo habitual es una línea caducada o bloqueada en el proveedor), no
  un error local.

### Pantalla de reproducción

Mientras ves el canal, la app se queda en una pantalla informativa (el vídeo
está en otra ventana) con:

- El nombre del canal, el origen y la barra de progreso del programa actual.
- La etiqueta `En vivo` (o `Archivo` si es catch-up) y el título del programa.
- La **salud del stream**: se sondea en segundo plano cada 3 s (HEAD y, si el
  servidor lo rechaza, GET con `Range`), con 3 s de timeout y sin reintentos,
  y se resume como `Señal: [●●●●○] Buena`. La escala va de `Sin señal` a
  `Excelente` según latencia media, jitter y porcentaje de éxito. Si el canal
  no es `http(s)` (rtmp, udp…) se indica que no es medible.
- `q` detiene la reproducción y vuelve a la pantalla anterior.

### Guía EPG

- Si la playlist trae su EPG en la cabecera (`x-tvg-url`, `url-tvg`,
  `tvg-url`… con URL o path relativo, local o remota), se carga solo al
  abrirla, en segundo plano: no hace falta pedirlo a mano.
- Si no lo trae (o falla su descarga), la primera vez que abres la guía
  pide un XMLTV: ruta local (`.xml`/`.xmltv` o `.gz`) o URL `http(s)`,
  con el de la cabecera como valor por defecto.
- La correspondencia se busca por `tvg-id` y, si no aparece, por `tvg-name`
  (o por el nombre del canal) contra los `<channel>` del XMLTV.
- `●` marca el programa que se está emitiendo ahora.
- `r` fuerza la recarga (en URLs re-descarga; en ficheros re-lee).
- Las URLs se cachean con TTL de 12 h; una fuente que acaba de fallar no se
  reintenta antes de 5 minutos.
- En **Listas Especiales X** la guía se pide igual que en el resto: un XMLTV
  local o una URL. El EPG del panel (`get_short_epg`) todavía no se consulta
  desde la TUI, así que sin un XMLTV no hay parrilla para esos canales.

### Catch-up / Archivo (reproducir lo ya emitido)

Sólo funciona si **el proveedor lo declara** para ese canal. No hay forma de
activarlo a mano, y no se buscan endpoints a ciegas: si tu lista o tu panel no
lo anuncia, no hay archivo.

| Marcador | Significado |
| --- | --- |
| `●` | En directo. `Enter` abre el canal como siempre. |
| `▶` | Programa pasado que el proveedor guarda: `Enter` lo pide al archivo. |
| `○` | Programa pasado **sin** copia guardada: se ve, no se reproduce. |

Dónde sale la declaración:

- **Listas Especiales X**: el canal trae `tv_archive ≥ 1` **y**
  `tv_archive_duration > 0`. El panel de detalles muestra `◷ Archivo: N días`
  cuando la declaración es utilizable.
- **M3U**: el `#EXTINF` trae `catchup`, `catchup-days` y `catchup-source`. Sólo
  se sustituyen los marcadores `{start}`, `{end}`, `{duration}` y `{utc}`; si el
  proveedor usa otro, se explica en un modal y no se intenta reproducir.

Concreciones que conviene saber:

- Si `Enter` cae en un programa que no se puede recuperar, sale un **modal** con
  el motivo exacto: o el proveedor no ofrece archivo, o ese contenido ya se salió
  de su ventana de días.
- El directo no cambia: sigue siendo el camino por defecto y funciona igual con
  o sin catch-up.
- La reproducción de archivo usa el **mismo** reproductor externo y el mismo argv
  que el directo.
- La pantalla de reproducción cambia `En vivo` por `Archivo` y muestra el programa
  y la hora que elegiste, no el que se esté emitiendo.
- **Si tu panel declara `tv_archive` pero no `tv_archive_duration`, ese canal se
  queda sin archivo.** Es deliberado: sin ventana declarada no se puede saber
  qué es reproducible, y preferimos decir "no disponible" a inventarlo. Si
  esperabas catch-up y no aparece, ése es el primer sitio donde mirar.

### Teclas globales

Funcionan en cualquier pantalla salvo mientras escribes en una búsqueda.

| Tecla | Acción |
| --- | --- |
| `Esc` / `Retroceso` | Volver a la pantalla anterior. |
| `q` | Salir de la app (pide confirmación; `s`/`y` sí, `n` no, `Esc` cancela). |
| `?` | Abrir/cerrar la ayuda contextual (con scroll). |
| `t` | Alternar tema claro/oscuro (se guarda en `data/theme.json`). |
| `r` | Ver Recientes (en la pantalla EPG, en su lugar recarga). |
| `p` | Elegir reproductor del canal actual. |
| `u` | Deshacer el último borrado de lista. |
| `!` | Comprobar la seguridad (`security-check`); el resultado sale en un modal. |
| Ratón | Clic para seleccionar, doble clic para abrir. |

---

## Fuentes de canales

### M3U / M3U8 / TS

- Ruta local o URL `http://` / `https://` (típicamente `get.php` de paneles IPTV).
- Se aceptan `.m3u`, `.m3u8` y `.ts` sin tecnicismos adicionales.
- Se parsean atributos `tvg-id`, `tvg-name`, `tvg-logo`, `group-title`,
  marcas de radio (`radio="true"`), metadatos de catch-up (`catchup`,
  `catchup-days`, `catchup-source`) y tags `#EXTVLCOPT` / `#KODIPROP` (de los
  que solo se aplican a reproducción los de una lista blanca).
- Las claves con guion se normalizan a guion bajo (`catchup_days`), y los
  atributos no reconocidos se guardan tal cual, por si acaso.
- Si la cabecera `#EXTM3U` declara EPG (`x-tvg-url`, `url-tvg`, `tvg-url`,
  incluso sin comillas), se resuelve y se carga en segundo plano.

### Listas Especiales X

- Al añadir (`a` → `Listas Especiales X`) se piden nombre, servidor, usuario y
  contraseña, y **se prueba la conexión** antes de guardar.
- La contraseña vive en el **keyring del sistema** (keychain de macOS,
  Credential Manager/DPAPI de Windows, libsecret de Linux). Si no hay keyring
  utilizable, se queda **solo en memoria** para la sesión: nunca se escribe en
  claro en `playlists.json` (las contraseñas que hubiera ahí de versiones
  antiguas se migran al keyring y se borran del JSON). Nunca para M3U.
- Si una lista vieja no tiene contraseña guardada, se pide al abrirla.
- `C` desde el catálogo cambia la contraseña: se valida contra el
  servidor antes de escribirla, y las URLs de stream en memoria se olvidan
  para que la siguiente apertura se re-autentique.
- Los streams no se guardan con credenciales dentro: se referencian como
  `xtream://fuente/tipo/id.ts` y se resuelven al reproducir.
- Categorías y canales se cachean con TTL (ver abajo).

---

## Rendimiento y cachés

Las listas grandes (decenas de miles de canales) se abren rápido gracias a
tres mecanismos: caché en disco con TTL, caché en memoria por sesión y carga
del catálogo en un hilo aparte (`warm_catalog`) que no bloquea la UI.

| Qué | Dónde | Vigencia | Timeout |
| --- | --- | --- | --- |
| Playlist M3U por URL | `data/playlist_cache/` | TTL 6 h | 30 s |
| EPG XMLTV por URL | `data/epg_cache/` | TTL 12 h | 15 s |
| Listas Especiales X: categorías | `data/xtream_cache/` | TTL 24 h | 10 s |
| Listas Especiales X: canales | `data/xtream_cache/` | TTL 6 h | 10 s |
| Playlist ya parseada | Memoria (sesión) | hasta 6 h / cambio de `mtime` | — |

- Si una URL remota falla pero hay caché previa, se usa la caché en vez
  de romper la experiencia.
- `R` / `F5` fuerza la re-descarga (`force_refresh`).
- Una fuente del catálogo o un EPG declarado que falló se reintenta como muy
  pronto a los 5 minutos, para no martillar servidores caídos.
- Los topes de red y de tamaño son globales y ajustables
  (`security/limits.py`): por defecto 60 s de timeout total, 25 MB por
  respuesta, 64 MB para ficheros locales, 3 redirects, 200 000 entradas por
  lista, 500 000 nodos XML y 64 de profundidad.

---

## Datos

Todo lo persistido vive en `data/` (está en `.gitignore`):

| Archivo / carpeta | Contenido |
| --- | --- |
| `playlists.json` | Catálogo de listas: nombre, origen, tipo y (en Listas Especiales X) servidor y usuario. **Nunca** incluye contraseñas; permisos `0600`. |
| `favorites.json` | Canales favoritos (identidad = `url`). |
| `recents.json` | Últimos 20 elementos reproducidos (con la URL redactada si traía credenciales). |
| `prefs.json` | Último reproductor usado, criterio de orden de grupos y tema. |
| `theme.json` | Tema claro/oscuro. |
| `epg_cache/` | XMLTV descargados. |
| `playlist_cache/` | Playlists M3U descargadas. |
| `xtream_cache/` | Respuestas de la API de Listas Especiales X (categorías y canales). |
| `ux_last.json` | Informe del runner de tests UX. |

Las carpetas se crean con permisos `0700` y los ficheros con `0600`. Para
empezar de cero, borra la carpeta `data/` (se recrea sola).

---

## Tests

```bash
# Suite completa (~1031 tests, ~55 s)
python -m unittest -v

# Solo tests de una parte
python -m unittest tests.test_m3u_parser -v
python -m unittest tests.test_epg_parser -v

# Informe visual de los tests de experiencia de usuario
python -m tests.ux_report            # tarjeta con barras de progreso
python -m tests.ux_report --verbose  # con tracebacks
```

Cobertura mínima exigida por el proyecto: parser M3U (archivo y URL),
parser XMLTV (incluye `.gz`), persistencia JSON de playlists, arranque
multiplataforma (detección del SO + preflight de curses) y dominio
catch-up (los 8 tests obligatorios del §19, la invariante de capacidad y
el test AST de "no sondear endpoints"). El resto cubre UI, Listas
Especiales X, reproductores, layout, tema y recorridos UX completos.

```bash
python -m unittest tests.test_catchup -v      # dominio catch-up
python -m unittest tests.test_catchup_ui -v   # guía, marcadores y modales
python -m thetvview.security.check            # 12 controles de seguridad
```

---

## Estructura

El código vive en el paquete `thetvview/`:

```
thetvview/
├── __main__.py         # punto de entrada: python -m thetvview
├── platform_check.py   # detecta el SO, UTF-8 de consola y curses (preflight)
├── models.py           # dataclasses: Channel, Playlist, Program
├── m3u_parser.py       # parser M3U/M3U8/TS (archivo y URL, cache TTL)
├── epg_parser.py       # parser XMLTV (.xml/.gz) con cache TTL
├── playlist_manager.py # catálogo de playlists (JSON, M3U + Especiales X)
├── favorites.py        # favoritos (JSON)
├── recents.py          # historial de recientes (20, con URL redactada)
├── prefs.py            # preferencias (tema, último reproductor)
├── resolutions.py      # detección/agrupación de variantes de calidad
├── groups.py           # agrupación por group-title
├── channel_health.py   # salud del stream en vivo (hilo no bloqueante)
├── player.py           # lanzamiento de mpv/mplayer/vlc (sin shell)
├── stream_ref.py       # referencias opacas xtream:// y xtream-ts://
│                       #   (directo y archivo, sin credenciales)
├── catchup.py          # dominio catch-up: capacidad declarada, ventana,
│                       #   estados, adaptadores y plantilla catchup-source
├── security/           # política de URL, SSRF, TLS, redacción, límites,
│   │                   # XML seguro, ficheros con 0600 y security-check
│   ├── safe_http.py    # único camino de red: timeouts, topes, redirects
│   ├── ssrf.py         # redes privadas, metadatos y formas ambiguas
│   ├── url_policy.py   # esquemas/hand permitidos por propósito
│   ├── redaction.py    # redacción de secretos (SDD §16)
│   ├── secrets.py      # keyring del SO (macOS/Windows/Linux) + memoria
│   ├── xml_safe.py     # XML sin DTD/entidades ni árboles infinitos
│   ├── local_files.py  # lecturas acotadas + permisos 0600/0700
│   ├── limits.py       # timeouts, tamaños, nº de entradas
│   ├── errors.py       # excepciones de seguridad (tipadas, sin secretos)
│   └── check.py        # python -m thetvview.security.check
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

Los nombres de módulo y de ruta conservan el término *xtream* porque es el
que usan el código y las carpetas (`xtream_cache/`, `xtream://`); en la
interfaz y en esta documentación, la fuente se llama **Listas Especiales X**.

### Arquitectura en una frase

`pantallas (ui) → acciones (dict) → App.handle_action → módulos de
lógica → persistencia`, con la terminal siempre protegida por
`curses.wrapper`, `KEY_RESIZE` manejado y excepciones de dibujo
degradando en silencio.

---

## Solucionar problemas

| Síntoma | Qué hacer |
| --- | --- |
| *Windows: `ModuleNotFoundError: No module named '_curses'`* | Con el venv activo: `pip install windows-curses` y relanza. El aviso del propio programa ya te lo recuerda antes de fallar. |
| *Aviso: «necesita una terminal interactiva (TTY)»* | Ejecútalo desde una consola real (PowerShell, CMD, bash), no desde un IDE, un pipe ni una tarea programada. |
| *No hay reproductor disponible* | Instala `mpv`, `vlc` o `mplayer`. La app busca también fuera del `PATH` (Program Files, Homebrew, snap/flatpak…). |
| *Sin correspondencia EPG* | El `tvg-id` del canal no está en el XMLTV. Prueba otra fuente (`e` → `r`). |
| *Sin guía en Listas Especiales X* | La TUI todavía pide un XMLTV para la parrilla (el EPG del panel no se consulta): indica una ruta local o una URL al abrir la guía. |
| *Sin archivo (catch-up)* | El proveedor no lo declara para ese canal: revisa que traiga `tv_archive` **y** `tv_archive_duration`, o `catchup` + `catchup-days` + `catchup-source` en el `#EXTINF`. |
| *Lista vacía* | Revisa que el fichero tenga canales; los `.ts` también valen. |
| *Ventana demasiado pequeña* | Agranda la terminal (mínimo 40 × 10). |
| *Una URL no responde* | Espera al TTL o fuerza recarga con `R`; si hay caché previa se usa esa. |
| *Bloqueada por red privada* | Es una IP de LAN, loopback o metadatos: confirma la excepción **por lista** al añadirla, o usa una fuente pública. |
| *Contraseña de Listas Especiales X cambiada en el servidor* | `C` en el catálogo para actualizarla. |
| *La contraseña no se recuerda entre sesiones* | No hay keyring del sistema disponible; en ese caso la app sólo la guarda en memoria. |
| *Colores raros* | Se usan 256 colores si existen y 8 si no; `NO_COLOR=1` los desactiva por completo. |
| *¿De dónde salen los datos?* | Borra `data/` para reiniciar de cero. |

---

## Seguridad

Todo lo que llega de una lista (URL, XMLTV, nombres, `EXTVLCOPT`) se trata
como **dato no confiable**. Compruébalo tú mismo:

```bash
python -m thetvview.security.check        # 12 controles; exit != 0 si falla
python -m thetvview.security.check -v     # detalle de cada control
python -m thetvview.security.check --json # para CI
```

Los 12 controles: redacción de secretos, verificación TLS, política SSRF,
redirects, límites de respuesta, XML seguro, saneado de secretos para IA,
invocación sin shell, almacenamiento de credenciales, ausencia de contraseñas
en logs, cero dependencias pip y puerta de capacidad catch-up.

Dentro de la app, la tecla **`!`** ejecuta lo mismo y el resultado sale
**siempre en un modal** (lo mismo ocurre con todos los errores y avisos).

- **Un solo camino de red** (`security/safe_http.py`): timeout total, tope de
  bytes, hasta 3 redirects **revalidados uno a uno**, bloqueo del downgrade
  `https → http` y TLS estricto (CA del sistema, hostname, ≥ 1.2).
- **Política de URL + SSRF**: sólo `http(s)`; loopback, redes privadas,
  enlaces locales, metadatos de la nube y direcciones ambiguas
  (`0177.0.0.1`, `2130706433`) se rechazan. La excepción de red privada es
  **por lista** y pide confirmación al añadirla; una URL `http://` sin cifrar
  genera un aviso, pero no se bloquea.
- **Sin credenciales en las URLs**: los streams de Listas Especiales X se
  guardan como `xtream://fuente/tipo/id.ts` y se resuelven en el momento de
  reproducir. La referencia de archivo es el mismo patrón con otro prefijo
  (`xtream-ts://fuente/id.ts?start=…&dur=…`). Favoritos y recientes se redactan
  y reescriben si traían una URL vieja.
- **Contraseñas en el keyring del SO**: `security/secrets.py` habla con
  libsecret, keychain o DPAPI según el SO; sin keyring la contraseña queda
  sólo en memoria. Nunca se escribe en claro en `data/playlists.json`.
- **El archivo sólo si el proveedor lo declara**: `catchup.py` es la única
  fuente de la verdad y es *fail-closed* (sin ventana declarada, no hay
  catch-up). La petición se valida **antes** de construir la URL, y ningún
  módulo del paquete escribe rutas de sonda de endpoint — el control número 12
  lo verifica sobre el AST, no sobre un comentario.
- **Redacción** (`security/redaction.py`) de contraseñas, `Authorization`,
  cookies y `password=` en errores, excepciones, mensajes y volcados.
- **XML seguro**: sin `DOCTYPE` ni entidades, con límites de tamaño,
  profundidad y nodos; los `.gz` se descomprimen con tope.
- **Nunca `shell=True`**: el reproductor recibe un argv en lista y la URL va
  tras un `--`, después de validar el esquema; sólo opciones `EXTVLCOPT` de
  una lista blanca.
- **Cero dependencias pip**: `requirements.txt` vacío y todos los `import`
  del paquete son del stdlib (también comprobado por `security-check`).

Riesgos residuales declarados (entre ellos: sin *IP pinning* contra DNS
rebinding, y `<!DOCTYPE` dentro de un `CDATA` se rechaza por ser
*fail-closed*) en **[SECURITY.md](SECURITY.md)**.

---

## Licencia

MIT — ver [LICENSE](LICENSE).
