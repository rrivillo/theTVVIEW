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
| **Pistas** | Audio, subtítulos y calidad del canal **solo si el proveedor los publica** (HLS master / DASH). En caliente con mpv. |
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
- Terminal con al menos **40 columnas × 10 filas** y, idealmente, 256 colores.
- **Windows**: `pip install windows-curses` (único extra y solo ahí) porque el
  Python oficial no incluye `curses`. Si falta, el programa lo avisa al arrancar.

## Advertencia

- Éste proyecto recibió asistencia de OpenCode, en su plan free. No lo hizo
  completo, pero sí ayudó. Sé que harías un mejor trabajo sin IA, así que,
  sé educado.

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

Opciones de una sesión (sólo para este arranque, no se guardan en disco):

```bash
python -m thetvview --audio es --subtitles es   # español en ambos
python -m thetvview --subtitles off             # sin subtítulos
python -m thetvview --quality 720p              # fija la calidad
python -m thetvview --quality auto              # vuelve a automática
```

Son un atajo para probar un canal sin tocar tu configuración: `--audio` y
`--subtitles off` ganan a lo que haya en `prefs.json`, y al cerrar la sesión
no queda nada guardado. Si escribes una opción mal, la app lo dice y sale con
código 2, en vez de arrancarse como si la hubieras escrito bien.

1. **Listas**: pulsa `a` y elige el tipo de fuente (`M3U / M3U8 / TS` o
   `Listas Especiales X`). Rellena el formulario y Enter.
2. **Canales**: Enter sobre la lista para abrirla; navega con flechas
   (o `j`/`k`), busca con `/`, agrupa con `g`, marca favoritos con `f`.
3. **Reproducir**: Enter sobre el canal → (si tiene variantes en la lista)
   eliges calidad → (si el proveedor publica varias pistas) eliges audio,
   subtítulos y calidad → eliges reproductor → se abre en otra ventana.
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
- La app nunca bloquea la interfaz: descargas, parseos grandes, análisis de
  manifiesto y sondas de red van en hilos y la pantalla se repinta mientras
  tanto. La única espera síncrona es la acotada de 2,5 s al abrir un canal, y
  hay pantalla que explica qué está pasando mientras ocurre.
- Cuando un aviso viene de un hilo en segundo plano, se saca en el hilo de la
  interfaz: un modal desde un hilo secundario no es seguro con `curses`.

---

## Guía de funcionamiento

### Flujo general

```
Listas ──Enter──▶ Canales ──Enter──▶ [Pistas] ──▶ Reproductor ──▶ Reproduciendo
  │                  │                  │              │               │
  │                  ├── /  buscar      │ sólo si el    │ sólo los       │ q detiene
  │                  ├── g  grupos      │ proveedor     │ instalados     │ y vuelve
  │                  ├── f  favoritos   │ publica algo, │               │
  │                  ├── e  guía EPG    │ y sólo tras   │               │
  │                  └── p  reproductor │ ⏱ máx 2,5 s   │               │
  │                                     │ (4 s en Win)  │               │
  │                                     │ (+ el mismo   │               │
  │                                     │  margen si el │               │
  │                                     │  análisis     │               │
  │                                     │  sigue en     │               │
  │                                     │  marcha)      │               │
  ├── a añadir · d borrar · u deshacer   └── Esc ← vuelve en cualquier punto
  └── f favoritos · C contraseña Listas Especiales X
```

Lo de `[Pistas]` antes que `[Reproductor]` es deliberado: el reproductor no
cambia qué pistas publica el canal, así que preguntarlo primero sería preguntar
por algo que no depende de tu elección. Cuando el proveedor no publica
alternativas, ese paso no aparece y se va directo al reproductor. Y si el
análisis llega tarde y las pistas se ofrecen al confirmar el reproductor,
`Enter` en esa pantalla **reproduce directamente** con el reproductor ya
elegido: nunca te hacen elegirlo dos veces.

### Navegación (común a casi todas las pantallas)

| Tecla | Acción |
| --- | --- |
| `↑` / `↓` o `k` / `j` | Subir o bajar. En las rejillas de tarjetas se mueve de tarjeta en tarjeta. |
| `RePág` / `AvPág` | Saltar una pantalla. |
| `Inicio` / `Fin` o `g` / `G` | Primero o último elemento. |
| `Enter` | Abrir lo seleccionado. |
| `Esc` / `Retroceso` | Volver a la pantalla anterior. |

La barra inferior de cada pantalla muestra las acciones más relevantes de esa
pantalla, en una sola línea y con ajuste al ancho disponible; `?` para la ayuda
completa, que además es la referencia definitiva.

Un aviso sobre `g`: en **Canales** `g` no es "ir al principio" sino abrir los
**grupos**, así que para el principio está `Inicio`. En el resto de pantallas de
lista (`g` = primero, `G` = último) sí se cumple.

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
| `Enter` | Ver el canal (→ pistas, si las publica → reproductor). |
| `/` | Búsqueda incremental: la lista se filtra mientras escribes. |
| `g` | Ver los grupos (categorías) de esta lista. |
| `f` | Añadir/quitar el canal de favoritos (★). |
| `e` | Ver la guía EPG del canal. |
| `p` | Elegir reproductor para el canal. |
| `R` / `F5` | Actualizar la lista desde su origen. |

`g` sólo abre la pantalla de grupos si la lista tiene **dos o más**: con cero
o un grupo no hay nada que elegir y te lo dice en la barra de estado.

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

Los canales sin `group-title` se agrupan al final como `(sin grupo)`, y los
grupos salen siempre ordenados por nombre. El catálogo y la pantalla de grupos
muestran tarjetas en rejilla: 1 columna por defecto y 2 a partir de 100
caracteres de ancho; el catálogo añade una tercera columna a partir de 150.

### Calidad y reproductor

- **Calidad**: solo aparece si el canal tiene variantes. Se detectan por
  atributos EXTINF (`res`/`quality`), sufijo en el nombre
  (`"Canal (720p)"`, `"Canal HD"`, `"Canal 1080i"`), marcadores entre
  corchetes ignorados al agrupar y sufijo de `tvg-id` estilo iptv-org
  (`id.es@HD`). Se reconocen `SD`, `HD`, `FHD`, `QHD`, `UHD`, `4K` y `8K`.
  Navega con `←`/`→` y confirma con `Enter`.
  El selector **abre sobre la variante que acabas de elegir**, así que
  `Enter` seguido de `Enter` reproduce lo mismo que elegiste.
  Si una lista publica varias entradas del mismo canal con la misma calidad
  (habitual con `[Opc.2]`, `[Opc.3]`), los botones dicen cuál es cuál usando
  ese marcador en vez de repetir `HD` o `SD` dos veces: dos botones iguales no
  son un botón.
  Este selector y el de pistas son cosas distintas: éste elige entre
  **canales** de tu lista, el otro entre **variantes del manifiesto** del
  canal que ya has elegido. Si la lista trae variantes, gana éste y el otro
  se pregunta al abrir el canal ya concreto.
- **Reproductor**: tarjetas con los reproductores instalados;
  `↑`/`↓` + `Enter`. Si no hay ninguno, la app te lo dice en vez de fallar.

### Calidad, audio y subtítulos

Cuando el proveedor **publica** varias pistas para el mismo canal (HLS master
o DASH), la app te deja elegir. Si no publica ninguna, **no aparece ningún
menú nuevo**: un `.ts` o un manifest de una sola pista se reproduce igual que
siempre.

- **El manifiesto es la fuente de verdad**, no la M3U. La app lo descarga en
  segundo plano (6–8 s como máximo, en un hilo que nunca bloquea la
  interfaz), detecta el protocolo por el **cuerpo** y no por la extensión, y
  lee `EXT-X-MEDIA` / `EXT-X-STREAM-INF` (HLS) o `AdaptationSet` /
  `Representation` (DASH).
- **Sólo se ofrece lo que hay.** Con menos de dos opciones por tipo no hay
  selector; un solo audio o una sola calidad se muestran como información.
  Nunca se inventa una pista.
- **Antes de elegir reproductor**, no después. Al abrir el canal, si el
  proveedor publica alternativas, la pantalla `Audio y calidad` aparece con
  tres secciones (Audio / Subtítulos / Calidad), sólo las que tengan
  opciones de verdad. `↑`/`↓` elige, `←`/`→` salta de sección, `Espacio`
  marca con `*` lo que has elegido tú, `0` vuelve a
  `Automático`/`Desactivados`, `m` recuerda la elección para ese canal y
  `Enter` **confirma y pasa al selector de reproductor**. Es el orden
  natural: el reproductor no cambia qué pistas publica el canal, así que
  preguntarlo primero sería preguntar por lo que no depende de la elección.
  Si el canal no publica nada, la pantalla no aparece y se va directamente
  al reproductor, como siempre.
- **El cursor aplica, el asterisco distingue.** Moverse con `↑`/`↓` va
  aplicando lo que pasa por debajo, así que sin nada más no habría forma
  de separar *"esto ya venía así"* de *"esto lo he elegido yo"*. `Espacio`
  marca la opción del cursor con un `*`.
- **El asterisco es una sola marca por lista**, como en un selector de
  archivos: marcas `360p`, luego `720p`, y el asterisco **se mueve** — `360p`
  lo pierde — porque lo que has dicho es "720p", no las dos. Como cada sección
  es una lista con su propia pregunta (audio, subtítulos, calidad), sí se
  puede marcar **una de cada una** (un subtítulo *y* una calidad) sin que una
  pise a la otra. `0` quita la marca de la sección, porque volver a
  `Automático` no es elegir. El asterisco vive en la pantalla, no en las
  preferencias: al salir se borra.
- **La espera está acotada y se explica**: como el manifiesto se descarga en
  segundo plano, al abrir el canal se espera **como máximo 2,5 s** (4 s en
  Windows, donde la primera petición tarda más: resolver de DNS del sistema,
  handshake de TLS y configuración de proxy del registro) —y sólo si el canal
  puede tener manifiesto; un `.ts` no espera— con una pantalla de "Analizando
  las pistas del canal…". **Un sondeo todavía en marcha nunca se confunde con
  "este canal no tiene pistas"**: mientras la petición sigue viva, la pantalla
  dice que está esperando, aunque todavía no haya nada que pintar. Antes ese
  estado se pintaba como un canal sin pistas y volvía solo, sin explicación.
  Si el análisis **todavía** sigue en marcha en el momento de confirmar el
  reproductor, se concede un **margen corto más** (el mismo tope, con su propio
  aviso) antes de lanzar: es lo que mantiene el orden `pistas → reproductor` en
  equipos lentos o con proveedores que tardan, donde antes las pistas aparecían
  después del reproductor y había que elegirlo **dos veces**. Si ni así llega,
  se sigue el camino de siempre: las pistas llegan igualmente después y se
  ofrecen la próxima vez que se elige reproductor (`p`), no con el canal ya
  abierto. Cuando el reproductor ya está elegido y se confirman las pistas, la
  reproducción arranca directamente con él: no se vuelve a preguntar. La
  espera es siempre la misma y sólo ocurre
  si la URL puede ser un manifiesto; los `--audio`/`--subtitles`/`--quality` de
  arranque fijan la pista pero **no** la evitan, porque para saber qué pista es
  "la de español" o "la de 720p" hay que leer el manifiesto igualmente.
- **La app se identifica ante el proveedor** con su propio `User-Agent`
  (`theTVVIEW/1.0`), también cuando lanza el reproductor. No es un detalle:
  hay CDNs que responden **403 a cualquier** User-Agent de reproductor (mpv,
  ffmpeg e incluso el de Chrome) y sólo entregan los segmentos al de la app.
  Sin esto el canal se quedaba reintentando segmentos para siempre, con el
  mismo síntoma que una línea caída pero sin serlo. Si el canal declara su
  propio `#EXTVLCOPT:http-user-agent=…`, **gana el del proveedor**.
- **Los subtítulos dependen del reproductor, y la app lo avisa.** Medido:
  `mpv` y `mplayer` usan el demuxer HLS de ffmpeg, que **no expone** las
  pistas de subtítulo declaradas como rendition (`EXT-X-MEDIA` con
  `TYPE=SUBTITLES`); lo dice explícitamente: `hls: Can't support the
  subtitle(...)`. `vlc` sí las ve (trae su propio demuxer adaptativo), así que
  **con VLC la elección se aplica**. Con subtítulos *incrustados* en los
  segmentos (no en un rendition aparte) funciona en todos. Cuando eliges
  subtítulos que el reproductor elegido no va a poder ver, aparece un **modal
  informativo** —no una pregunta— que dice qué va a pasar y que con VLC sí
  funciona. Se muestra **una sola vez por canal y reproductor**: no es algo que
  haya que confirmar cada vez que reabres el canal.
- **La preferencia manda al entrar**: idioma exacto → idioma base → etiqueta →
  `DEFAULT` del stream → primera disponible. Los ámbitos del formato son canal →
  proveedor (el host de la lista) → global, y se leen en ese orden; la tecla
  `m` de la pantalla de pistas es la que **escribe el de canal** (lo que has
  elegido ahí se aplica a ese canal la próxima vez). En `prefs.json` no se
  guarda nunca la URL del canal: la clave es el `tvg-id` o el hash de la URL
  **ya redactada**.
- **Nada se cambia con el canal abierto**: audio, subtítulos y calidad se eligen
  **antes**, en la pantalla `Audio y calidad`, y se aplican al lanzar el
  reproductor. Por eso la pantalla `Reproduciendo` no tiene atajos de pista: una
  pista fija ya no tendría a qué aplicarse. La única excepción es que el sondeo
  del manifiesto llegara tarde; entonces se vuelve a elegir reproductor con `p`
  y ahí se preguntan las pistas. La tecla `i` sigue reanalizando el manifiesto
  del canal que se está viendo.
- **Los cambios que sí se aplican en caliente**: con `mpv` el reproductor acepta
  el cambio por su canal de control (IPC local) durante la reproducción; con
  `vlc` y `mplayer` no es posible, y por eso la elección se hace antes de
  lanzar.
- **Cómo comprobar si una lista sirve de algo para esto**: la app trae una
  encuesta que usa **el mismo camino real** (no una simulación) y dice, canal
  a canal, qué se encontró:

  ```bash
  python -m thetvview.tracks.survey mi-lista.m3u -n 60
  python -m thetvview.tracks.survey https://…/get.php -n 60 --private
  python -m thetvview.tracks.survey mi-lista.m3u --solo-menu   # sólo los que sí ofrecen
  python -m thetvview.tracks.survey mi-lista.m3u --conseguir 8  # para en cuanto haya 8
  ```

  Distingue las razones por las que no aparece el menú —"una sola pista",
  MPEG-TS, no-HTTP, 403, 404, timeout, cuerpo no reconocido— porque **no
  ofrecer opciones es lo correcto** cuando el proveedor no las publica.

  - **`--conseguir N` para en cuanto encuentre N canales con menú**, que es
    lo que hace falta para no demorarse en una lista larga. En una de **1810
    canales** encontró los 8 buscados en **96 sondeos y 53 s**, sin recorrer
    la lista entera. El informe avisa de que paró, para que su porcentaje no
    se lea como una medición completa.
  - **Diversifica por host** y **prioriza** las formas de URL que en listas
    reales han dado resultado (`master` en la ruta, `playlist.m3u8`,
    `index.m3u8`). Sin esto, preguntar los primeros N de una lista es
    preguntarle a un solo proveedor: en esa lista hay 59 canales de un mismo
    host, y los 96 primeros sondeos visitaron 96 hosts distintos. Se puede
    apagar con `--sin-diversificar` y `--sin-priorizar`.
  - El resto del mando: `-j/--jobs` sondeos simultáneos (4 por defecto),
    `--timeout` segundos por canal (8), `-n 0` para recorrerla entera,
    `--muestreo inicio|aleatorio|fin`, `--ejemplos N` ejemplos por veredicto
    y `--solo-menu` para ver sólo los que sí tendrían menú.
  - Medido sobre listas reales: 1755 canales → 60 sondeos, 5 (8 %) ofrecen
    pistas. 387 canales (todas `.m3u8`) → 53 de 387 (13,7 %), de ellos **51
    sólo calidad**, 1 sólo audio y 1 sólo subtítulos, ninguno las tres a la
    vez. 1810 canales → 22 de 96 (22,9 %). 11152 canales → 11 de 112 (9,8 %),
  y con más canales muertos (39 timeouts). El resto son manifiestos de una
    sola pista (muchos proveedores sirven un `index.m3u8` por canal, que es
    un media playlist, no un master), enlaces muertos o bloqueos.
    No escribe nada en disco y redacta las URLs.
- **Si el master en vivo cambia**, la app reconcilia: si la pista elegida
  sigue existiendo la conserva, y si ha desaparecido elige una alternativa
  válida y lo explica en un modal.
- **Sólo se ofrece calidad cuando se puede fijar.** La calidad se aplica
  reescribiendo el master HLS (el proxy), así que **en DASH no hay menú de
  calidad**: un MPD no tiene master que reescribir y sus representations no
  tienen URI de playlist, de modo que un master sintético saldría con la URI
  vacía y el canal no reproduciría nada. Antes se ofrecía igual y el
  reproductor arrancaba con un manifiesto roto; ahora el menú no aparece y,
  si alguien fuerza la selección por API, sale un aviso explicando que se
  reproducirá con calidad automática.
- **Avisos sólo cuando algo no va a funcionar.** Elegir calidad en HLS
  funciona y no se avisa de nada: un aviso que explica el mecanismo ("se
  sirve un master fijado por un proxy local") suena a avería y llega por
  modal para informarte de que tu elección se aplicó. Y cuando el canal no
  ofrece nada que elegir (una sola pista, un directo), el motivo va a la
  barra de estado en lugar de interrumpir con un modal **antes incluso de
  elegir reproductor**: el canal se reproduce bien, así que no hay nada que
  interrumpir. El modal se reserva para lo que de verdad falló (403, 404,
  timeout, o un 200 que no es un manifiesto).
- **Un manifiesto puede mentir**. Medido en la lista de iptv-org: hay canales
  cuyo master se lee perfectamente, declara 2 variantes de calidad… y **las dos
  dan 404**. Ahí el menú aparece porque el manifiesto dice que hay alternativas,
  y cualquier elección falla. No es un fallo del parser (las URIs relativas se
  resuelven bien contra la URL final) sino del proveedor. La encuesta lo
  distingue del resto de 404 porque el master **sí** se pudo leer.
- **Errores del proveedor no rompen nada**: un 403 (suele faltar el
  `Referer` del `#EXTVLCOPT`), un 404, un timeout o una página de error en
  lugar del manifiesto se traducen a un mensaje que explica qué ha pasado, y
  el canal se reproduce igualmente.

#### Qué sabe hacer cada reproductor

Verificado ejecutando los binarios, no de documentation:

| | audio | subtítulos | calidad fija | cambiar en caliente |
|---|---|---|---|---|
| **mpv** 0.40 | `--aid` | `--sid` | ✗ | ✓ (IPC) |
| **VLC** 3.0 | `--audio-language` / `--audio-track-id` | `--sub-language` / `--sub-track-id` | ✗ | ✗ |
| **mplayer** 1.5 | `-alang` / `-aid` | `-slang` / `-sid` | ✗ | ✗ |

**Ningún reproductor sabe fijar una variante concreta de un master HLS.** Los
tres `✗` de la última columna no son una limitación nuestra: se comprobó
ejecutando los binarios contra un master de prueba. En mpv 0.40,
`--video-bitrate` **no existe como opción** (`option not found`); la propiedad
del mismo nombre existe, pero es de estadísticas, va en bits/s y es de sólo
lectura. En VLC, `--program` es el selector de **programa de TV digital**
(DVB), no de variante HLS: usarlo aquí no fijaría calidad y además rompería
la reproducción. mplayer no tiene ninguna opción de bitrate por pista. Por eso
la calidad manual se resuelve por el otro camino, y el que funciona igual en
los tres:

- La app reescribe el manifiesto con **una sola** `EXT-X-STREAM-INF` (la
  elegida) y conserva el grupo de pistas de audio y subtítulos **entero**,
  marcando la elegida como `DEFAULT=YES`. Si se quitaran esas entradas, el
  master dejaría de ser válido y el reproductor se quedaría **sin audio**.
- Se sirve en `127.0.0.1` con **token de 128 bits** en la ruta y **puerto
  efímero**, y sólo acepta esa ruta: cualquier otra —segmentos incluidos—
  responde `404`. Los segmentos no pasan por aquí: sus URI apuntan al
  proveedor, así que el reproductor sigue hablando directamente con él y con
  sus cabeceras, y el pin funciona aunque el proveedor exija `Referer`.
- Con el proxy levantado no se pasan índices de pista (`--aid` y compañía),
  porque el manifiesto servido se ha renumerado y esos índices ya no
  coinciden.
- Si eliges **Automático** con el proxy levantado, se reescriben **todas** las
  variantes, cada grupo de audio una sola vez. Un master al que se le quitan
  los `EXT-X-MEDIA` deja al reproductor sin audio, y duplicarlos produce
  pistas repetidas: el proxy nunca degrada la calidad automática.
- En DASH la calidad manual **no se ofrece**: no hay mecanismo fiable de
  fijar una `Representation`.

### Cómo se lanza la reproducción

- El reproductor se lanza con `subprocess` y **argv en lista**, nunca con
  `shell=True`, y el argv termina siempre en `-- <url>` para que una URL
  hostile no se interprete como opción.
- Antes de construir nada, la URL se valida contra la política de esquemas:
  `http`/`https` siempre, y además `rtmp`, `rtmps`, `rtsp` y `udp` **sólo
  para reproducir** (para descargar datos siguen fuera). Un `file://`, un
  `javascript:` o una URL que empiece por `-` no llegan al reproductor.
- **El reproductor se elige por lo que sabe abrir, no por ser el primero que
  se encuentre.** La tabla `player/protocols.py` dice qué transporte abre cada
  binario, y está **medida** contra los binarios de esta máquina con el mismo
  comando que recibiría el usuario. Si el elegido muere al instante se prueban
  los siguientes, como mucho dos, y no se repite uno que ya falló con ese
  canal.
- Se prioriza **h264** (el estándar de facto en IPTV): `--vd=ffh264` en mpv,
  `-vc ffh264,ffmpeg2,…` en mplayer, `--avcodec-codec=h264` en vlc.
- El nombre del canal se pasa como título de la ventana del reproductor
  (saneado: sin saltos, sin no imprimibles, máx. 120 caracteres).
- El reproductor se identifica ante el proveedor con el mismo `User-Agent`
  que usa la app (`theTVVIEW/1.0`), y **sólo si el canal no declara el suyo**
  en un `#EXTVLCOPT:http-user-agent=…`: en ese caso gana el del proveedor y no
  se añade nada.
- De los `#EXTVLCOPT` / `#KODIPROP` de la lista sólo se traducen
  `http-user-agent` y `http-referrer` (con sus alias). El resto se ignora con
  un aviso: nunca se pasa texto arbitrario al reproductor. Lo mismo con las
  opciones de pistas: salen de una **lista blanca** por reproductor
  (`player/track_args.py`), no del texto que traiga el manifiesto.
- Si el reproductor muere en menos de 4 s, se explica que el stream no llegó
  a abrir (lo habitual es una línea caducada o bloqueada en el proveedor), no
  un error local.
- Si el canal se corta con el reproductor ya en marcha, un supervisor lo
  detecta y **reconecta el mismo motor** hasta cinco veces, con esperas de
  1 s, 2 s, 5 s y 10 s. Al rendirse, el mensaje dice cuántas veces se cortó.

### Qué tipos de canal se abren

El tipo de un canal y el modo en que viaja son **dos cosas distintas**, y la
app las guarda por separado: `https://servidor/live/canal.m3u8` y
`https://servidor/live/canal` comparten transporte y no se parecen en nada.

| Transporte | Qué es | Quién lo abre (medido aquí) |
|---|---|---|
| `http` / `https` | HLS, MPEG-TS, MP4, FLV | los tres |
| `rtmp` / `rtmps` | directo RTMP, con y sin TLS | mpv y VLC (mplayer **no** abre `rtmps`) |
| `rtsp` | cámaras IP | los tres, con y sin credenciales |
| `udp` | directo por UDP | mpv y mplayer (VLC **no**) |

Lo que **no** se ofrece, y por qué, está en `player/protocols.py` con el motivo
al lado:

- **`rtsps://`** (RTSP sobre TLS): no se ha podido comprobar que los
  reproductores de esta máquina lo abran, y la app no ofrece lo que no ha
  medido. Escribe `rtsp://` y, si tu cámara usa TLS, no funciona todavía.
- **UDP multicast**: se ha podido medir UDP unicast, no multicast, porque esta
  máquina no tiene ruta multicast utilizable. La diferencia importa: un grupo
  multicast es red privada por definición y, si el reproductor lo abriera pero
  el grupo no llegara, el problema sería de tu red (interfaz, firewall, router)
  y el mensaje lo dice así.

Un canal con transporte que ningún reproductor instalado abre **no se ofrece y
no es un error**: no se intentó reproducir nada. El modal explica si lo que
falta es un reproductor o, en el caso de multicast, la red.

### Cámaras IP (RTSP con contraseña)

Una lista con líneas `rtsp://usuario:clave@192.168.1.9:554/stream1` funciona, y
la contraseña **no** se escribe en ninguna parte:

- Al cargar la lista, un modal te explica qué va a pasar con esas credenciales,
  porque a partir de ahí la lista ya no se parece a la que escribiste.
- La clave se guarda en el almacén de secretos del sistema (el mismo donde ya
  vivían las contraseñas de las Listas Especiales X) y el canal se queda con una
  referencia opaca `ipcam://…` que no contiene ni la contraseña **ni la dirección
  de la cámara**.
- Ese modal sale **una vez** por sesión y por fuente: si la vuelves a abrir y
  quieres volver a leerlo, vuelve a cargar la lista desde cero (o borra la
  entrada y añádela otra vez).
- Esa referencia es lo que se guarda en favoritos y recientes, así que también
  funcionan con cámaras: no hay nada que redactar porque no hay nada sensible.
- La contraseña sólo se recupera en el instante de construir el comando del
  reproductor.
- Una cámara está en tu red local, así que su lista necesita el permiso de red
  privada que ya se pide al añadir una fuente.
- Si la clave cambia en la lista, vuelve a cargarla: es el momento en que se
  registra la nueva.

### Diagnóstico de un canal

Con el canal reproduciendo, la tecla `d` abre un informe en un modal con lo que
la app sabe y lo que no:

- Tipo detectado, esquema y MIME, y **por qué** se decidió así.
- Qué reproductores pueden abrirlo, cada uno con `[OK]` o su motivo, y cuál se
  eligió.
- El veredicto: `READY` o el motivo por el que no.

El informe **no incluye la URL** —ni cruda ni redactada—, sólo el tipo y el
motivo. Es lo que puedes copiar a un canal de soporte sin filtrar un token.

### Pantalla de reproducción

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
- `d` abre el diagnóstico del canal (ver arriba) sin tocar la red.
- El estado del reproductor se refleja en el medidor: `Reproduciendo`,
  `Reconectando` si se cortó, `Detenido` al parar.
- Al salir se apagan todo lo que esa pantalla había abierto: el sondeo en
  segundo plano, el proxy de calidad, el supervisor de reconexión y el socket
  de control de mpv. Dejarlo puesto acumularía procesos y basura en
  `data/ipc/`.

### Guía EPG

- Si la playlist trae su EPG en la cabecera (`x-tvg-url`, `url-tvg`,
  `tvg-url`… con URL o path relativo, local o remota), se carga solo al
  abrirla, en segundo plano: no hace falta pedirlo a mano.
- Si no lo trae (o falla su descarga), la primera vez que abres la guía
  pide un XMLTV: ruta local (`.xml`/`.xmltv` o `.gz`) o URL `http(s)`,
  con el de la cabecera como valor por defecto.
- La correspondencia se busca por `tvg-id` y, si no aparece, por `tvg-name`
  (o por el nombre del canal) contra los `<channel>` del XMLTV, comparando el
  `display-name` **exacto** y sin distinguir mayúsculas. No hay búsqueda
  parcial: si el XMLTV llama al canal de otra forma, no hay correspondencia.
- `●` marca el programa que se está emitiendo ahora.
- `r` fuerza la recarga (en URLs re-descarga; en ficheros re-lee).
- Si pulsas `e` mientras el EPG de la lista **sigue descargándose**, la app
  espera a que termine (hasta 60 s) en vez de preguntarte una ruta que ya le
  van a volver; si se agota, te avisa y te deja elegir.
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

En la pantalla `Reproduciendo` quedan dos teclas:

| Tecla | Acción |
| --- | --- |
| `d` | Diagnóstico del canal: qué es, qué reproductor puede abrirlo y cuál se eligió. |
| `i` | Volver a analizar el manifiesto (pistas nuevas o que desaparecieron), sólo si el canal publica alternativas. |

Audio, subtítulos y calidad **no** se cambian desde `Reproduciendo`: eso se
pregunta antes, en la pantalla `Audio y calidad`, que es la que sale al abrir el
canal. Con el canal ya abierto no hay atajo para ellos; si el sondeo del
manifiesto llegó tarde, se vuelve a elegir reproductor con `p` y ahí se
preguntan.

En la pantalla `Audio y calidad` (la que sale antes del reproductor):

| Tecla | Acción |
| --- | --- |
| `↑` / `↓` | Moverse por las opciones de la sección (el cursor **es** la selección). |
| `←` / `→` / `Tab` | Saltar de sección (audio, subtítulos, calidad). |
| `Espacio` | Marcar con `*` la opción del cursor. Una marca por sección: al marcar otra, el `*` se mueve. |
| `0` | Volver a `Automático` / `Desactivados` y quitar la marca de la sección. |
| `Enter` | Confirmar y pasar a elegir reproductor (o reproducir directamente, si el reproductor ya estaba elegido). |
| `m` | Recordar esta elección para este canal. |

Junto a la lista de subtítulos aparece, cuando aplica, una nota que dice
que **VLC** los aplica y que con MPV o MPLAYER sólo funcionan si van dentro
del vídeo. Se dice aquí, antes de elegir, y no en un modal después: cambiar
de reproductor sólo tiene sentido si aún no se ha lanzado nada.

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
  `x-url-tvg`, `x-epg-url`, `epg-url`; incluso sin comillas, y una o varias
  fuentes), se resuelve —URLs http(s) tal cual, rutas relativas contra el
  directorio del fichero o contra la URL de la lista— y se carga en segundo
  plano.
- Las líneas `rtmp://`, `rtmps://`, `rtsp://` y `udp://` también son canales
  válidos, siempre que algún reproductor instalado sepa abrirlas (ver «Qué
  tipos de canal se abren»). Las de cámara con contraseña se registran solas al
  cargar la lista.

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
del catálogo en un hilo aparte (`warm_catalog`) que no bloquea la UI. Esa carga
previa se hace **por listas de una en una y en segundo plano** (para no
machacar al proveedor con descargas en paralelo), y **no incluye las Listas
Especiales X**: necesitan credenciales, así que sólo se abren cuando tú las
abres.

| Qué | Dónde | Vigencia | Timeout |
| --- | --- | --- | --- |
| Playlist M3U por URL | `data/playlist_cache/` | TTL 6 h | 30 s |
| EPG XMLTV por URL | `data/epg_cache/` | TTL 12 h | 15 s |
| Listas Especiales X: categorías | `data/xtream_cache/` | TTL 24 h | 10 s |
| Listas Especiales X: canales | `data/xtream_cache/` | TTL 6 h | 10 s |
| Manifiesto de pistas (HLS/MPD) | `data/track_cache/` | 60 s en memoria · 15 min en disco | 6–8 s |
| Playlist ya parseada | Memoria (sesión) | hasta 6 h / cambio de `mtime` | — |

- Si una URL remota falla pero hay caché previa, se usa la caché en vez
  de romper la experiencia.
- `R` / `F5` fuerza la re-descarga (`force_refresh`).
- Una fuente del catálogo o un EPG declarado que falló se reintenta como muy
  pronto a los 5 minutos, para no martillar servidores caídos.
- La caché de pistas tiene los dos TTL cortos **a propósito**: un master en
  vivo cambia, y guardar 15 minutos una lista de variantes ya retiradas
  daría al usuario una calidad que el proveedor ya no publica. La de disco
  (con sal por instalación) sólo se consulta si la red falla, nunca para
  ahorrar una petición.
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
| `prefs.json` | Último reproductor usado, tema y preferencias de pistas (audio, subtítulos, calidad), con la precedencia canal → proveedor → global. También guarda `last_group_sort` y `ask_track_options`, que la TUI todavía no cambia desde los menús (los grupos salen siempre por nombre y `(sin grupo)` al final), y las de multi-stream: `preferred_backend`, `connect_timeout`, `startup_timeout`, `reconnect_max_attempts`, `playback_profile` y `diagnostics_enabled`. |
| `theme.json` | Tema claro/oscuro. |
| `epg_cache/` | XMLTV descargados. |
| `playlist_cache/` | Playlists M3U descargadas. |
| `xtream_cache/` | Respuestas de la API de Listas Especiales X (categorías y canales). |
| `track_cache/` | Manifiestos HLS/MPD ya analizados, con sal por instalación. |
| `ipc/` | Sockets de control de mpv (uno por reproducción, se borra al salir). |
| `ux_last.json` | Informe del runner de tests UX. |

Las carpetas se crean con permisos `0700` y los ficheros con `0600`. Para
empezar de cero, borra la carpeta `data/` (se recrea sola).

`prefs.json` es también el único sitio donde viven las preferencias de
pistas, y sus claves nunca son la URL del canal: son `tvg:…`, `channel:<hash
de la URL redactada>` o `provider:<host>`. Es la misma regla que en el resto
de la app: al disco sólo llegan datos sin credenciales. Del hash se conservan
32 caracteres: es un identificador estable, no un secreto.

Las preferencias de multi-stream (`preferred_backend`, `connect_timeout`,
`startup_timeout`, `reconnect_max_attempts`, `playback_profile`,
`diagnostics_enabled`) están **acotadas al leerlas**: lo que esté fuera de
rango se sustituye por el valor por defecto. El fichero lo edita una persona
con un editor de texto, y un `reconnect_max_attempts: 100000` no debe poder
convertir la app en algo que no responde. Editables a mano:

| Clave | Valores | Para qué |
| --- | --- | --- |
| `preferred_backend` | `mpv`, `mplayer`, `vlc` | Ir primero con este, si puede abrir el transporte del canal. No es lo mismo que el último usado. |
| `connect_timeout` | segundos | Cuánto esperar a que se abra la conexión. |
| `startup_timeout` | segundos | Cuánto esperar a que empiece a llegar el medio. |
| `reconnect_max_attempts` | 0-20 (5) | Cuántas veces reconectar **el mismo** reproductor si el canal se corta. El tope de cambiar de reproductor es otro y no se edita: son 2, y viven en `player/router.py`. |
| `playback_profile` | `low_latency`, `balanced` (por defecto), `stable` | Banderas de caché del reproductor. Desconocido -> `balanced`. |
| `diagnostics_enabled` | `true` / `false` | Si se puede pedir comprobar la conexión del canal (eso **sale** a la red; el informe sin conexión siempre está). |

---

## Tests

```bash
# Suite completa (1956 tests, ~2,5 min)
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
Especiales X, reproductores, layout, tema, recorridos UX completos,
selección de pistas (modelo, parsers HLS/DASH, sondeo, política de
selección, preferencias, argv, IPC y proxy de calidad) y el multi-stream
(transporte, detector, router, errores, cámara IP, supervisor, diagnóstico,
preferencias e integración).

Las acciones contextuales tienen su propia suite, y una invariante que la
sostiene: **todo lo que la barra inferior anuncia, la pantalla ya lo hace**
(`keys(actions()) ⊆ shortcuts() ∧ ⊆ handle_key`). La convención está en
[`docs/actions.md`](docs/actions.md).

```bash
python -m unittest tests.test_catchup -v      # dominio catch-up
python -m unittest tests.test_catchup_ui -v   # guía, marcadores y modales
python -m unittest tests.test_tracks_manager -v   # política de selección
python -m unittest tests.test_streams_hls -v       # parser HLS
python -m unittest tests.test_player_track_args -v # argv por reproductor
python -m thetvview.security.check            # 14 controles de seguridad
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
├── streams/            # descubrimiento del contenido de un canal
│   ├── transport.py    # el ESQUEMA (http/rtmp/rtsp/udp/…) — no el contenido
│   ├── detector.py     # protocolo: esquema + cuerpo + MIME + extensión
│   ├── diagnose.py     # informe de un canal (sin URL, texto redactado)
│   ├── hls.py          # master/media playlist → capacidades (lista blanca)
│   ├── dash.py         # MPD → capacidades (xml_safe, sin DTD/entidades)
│   ├── probe.py        # descarga del manifest por safe_http + caché TTL
│   ├── headers.py      # cabeceras EXTVLCOPT (sonda de salud y de pistas)
│   └── pin_proxy.py    # master HLS fijado en loopback con token
├── tracks/             # representación de pistas (sin red, sin curses)
│   ├── models.py       # MediaTrack, MediaCapabilities, PlaybackSelection
│   ├── language.py     # es/es-ES/spa/Spanish/Español → es + etiqueta
│   ├── labels.py       # etiquetas de audio, subtítulos y vídeo
│   ├── manager.py      # qué se ofrece y por qué; reconciliación
│   ├── prefs.py        # preferencias por canal / proveedor / global
│   ├── cli.py          # --audio / --subtitles / --quality de una sesión
│   └── survey.py       # python -m thetvview.tracks.survey (encuesta)
├── player/             # backend: qué sabe hacer cada reproductor
│   ├── __init__.py     # reexporta la API pública (from thetvview.player import launch)
│   ├── core.py         # lanzamiento de mpv/mplayer/vlc (sin shell)
│   ├── capabilities.py # tabla verificada de PISTAS (audio/subtítulos/calidad)
│   ├── protocols.py    # tabla verificada de TRANSPORTES (hls/rtmp/rtsp/udp)
│   ├── router.py       # qué reproductor abrir por capacidad (el §8 del SDD-M)
│   ├── errors.py       # PlayerError y errores de reproducción normalizados
│   ├── supervisor.py   # reconexión limitada (1/2/5/10 s, máx. 5) y perfiles
│   ├── track_args.py   # PlaybackSelection → argv (lista blanca)
│   └── mpv_ipc.py      # cliente del IPC local de mpv (1,5 s por comando)
├── stream_ref.py       # referencias opacas xtream:// y xtream-ts://
│                       #   (directo y archivo, sin credenciales)
├── cam_ref.py          # referencia opaca ipcam:// (cámaras RTSP, sin
│                       #   credenciales ni dirección del dispositivo)
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
    ├── tracks.py       # sesión de pistas del canal abierto (estado)
    ├── widgets.py      # listas, modales, formularios, toast, loading
    ├── actions.py      # Action, prioridades, vocabulario y ajuste del pie
    ├── textwidth.py    # ancho en celdas (cell_width/clip_cells)
    ├── layout.py       # rectángulos (header/main/footer)
    ├── theme.py        # paletas dual light/dark (256/8 colores, NO_COLOR)
    ├── colors.py       # IDs de pares de color
    └── icons.py        # iconos y cajas Unicode
tests/                  # unittest + fixtures (m3u, xmltv, .gz)
docs/actions.md         # la convención de acciones contextuales
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
| *Ventana demasiado pequeña* | Agranda la terminal: el mínimo son 40 columnas × 10 filas, y la app lo dice con las medidas actuales. |
| *Una URL no responde* | Espera al TTL o fuerza recarga con `R`; si hay caché previa se usa esa. |
| *Bloqueada por red privada* | Es una IP de LAN, loopback o metadatos: confirma la excepción **por lista** al añadirla, o usa una fuente pública. |
| *Contraseña de Listas Especiales X cambiada en el servidor* | `C` en el catálogo para actualizarla. |
| *La contraseña no se recuerda entre sesiones* | No hay keyring del sistema disponible; en ese caso la app sólo la guarda en memoria. |
| *Colores raros* | Se usan 256 colores si existen y 8 si no; `NO_COLOR=1` los desactiva por completo. |
| *No me aparece el menú de audio o calidad* | Lo más probable es que el proveedor no lo publique. La encuesta (`python -m thetvview.tracks.survey`) distingue por qué: una sola pista, MPEG-TS, 403, 404 o timeout. No ofrecer nada ahí es lo correcto. |
| *El menú de pistas no aparece aunque antes sí* | El master en vivo cambió: se guardan 15 minutos las variantes ya retiradas, y la caché en memoria vive 60 s. `i` desde `Reproduciendo` vuelve a analizarlo ahora. |
| *`g` no abre los grupos* | La lista necesita **dos o más** grupos: con cero o uno no hay nada que elegir y la app lo dice en la barra de estado. |
| *El canal se ve pero no oigo* | Pasó al elegir calidad: revisa si el proxy quedó levantado con un master sin su grupo de audio. `i` desde `Reproduciendo` vuelve a analizar el manifiesto. |
| *Los subtítulos no aparecen con MPV* | Es una limitación del demuxer HLS de ffmpeg, no de la app: los declarados como pista aparte (`EXT-X-MEDIA TYPE=SUBTITLES`) no los expone. Con VLC funcionan. |
| *La app tarda un poco al abrir un canal* | Son los 2,5 s de espera acotada del sondeo de manifiesto (4 s en Windows), y sólo en canales cuya URL puede ser un manifiesto: un `.ts` no espera. Es una espera con su pantalla explicando qué pasa, y durante ella la interfaz sigue viva. |
| *Un canal RTSP/RTMP/UDP no abre* | `d` desde `Reproduciendo` (o el diagnóstico de la lista) dice por qué. Lo más común: que el transporte no lo abra ningún reproductor instalado, que la red no deje pasar el destino, o que sea una cámara sin contraseña registrada. |
| *La cámara no reproduce* | Vuelve a cargar la lista: es al cargarla cuando la contraseña se registra. Y comprueba que la lista tiene permitido el acceso a red privada. |
| *El canal se corta y vuelve solo* | Reconexión automática, hasta 5 veces con esperas de 1, 2, 5 y 10 s. Si se corta más, el aviso final dice cuántas veces pasó. |
| *Un HLS no se abre con MPlayer* | Si la URL no acaba en `.m3u8`, su libavformat no detecta el manifiesto. Es una limitación medida de ese binario, no de la app. |
| *¿De dónde salen los datos?* | Borra `data/` para reiniciar de cero. |

---

## Seguridad

Todo lo que llega de una lista (URL, XMLTV, nombres, `EXTVLCOPT`) se trata
como **dato no confiable**. Compruébalo tú mismo:

```bash
python -m thetvview.security.check        # 14 controles; exit != 0 si falla
python -m thetvview.security.check -v     # detalle de cada control
python -m thetvview.security.check --json # para CI
```

Los 14 controles: redacción de secretos, verificación TLS, política SSRF,
redirects, límites de respuesta, XML seguro, saneado de secretos para IA,
invocación sin shell, referencias opacas sin credenciales, almacenamiento de
credenciales, ausencia de contraseñas en logs, cero dependencias pip, puerta de
capacidad catch-up y aislamiento del descubrimiento de pistas.

Dentro de la app, la tecla **`!`** ejecuta lo mismo y el resultado sale
**siempre en un modal** (lo mismo ocurre con todos los errores y avisos).

- **Un solo camino de red** (`security/safe_http.py`): timeout total, tope de
  bytes, hasta 3 redirects **revalidados uno a uno**, bloqueo del downgrade
  `https → http` y TLS estricto (CA del sistema, hostname, ≥ 1.2).
- **Política de URL + SSRF**: `http(s)` para descargar, y además
  `rtmp`/`rtmps`/`rtsp`/`udp` **sólo para reproducir**; loopback, redes
  privadas, enlaces locales, metadatos de la nube y direcciones ambiguas
  (`0177.0.0.1`, `2130706433`) se rechazan. La excepción de red privada es
  **por lista** y pide confirmación al añadirla; una URL `http://` sin cifrar
  genera un aviso, pero no se bloquea. Una URL con `usuario:clave@` se rechaza
  en todos los esquemas: las credenciales van aparte, también las de cámara.
- **Sin credenciales en las URLs**: los streams de Listas Especiales X se
  guardan como `xtream://fuente/tipo/id.ts` y se resuelven en el momento de
  reproducir. La referencia de archivo es el mismo patrón con otro prefijo
  (`xtream-ts://fuente/id.ts?start=…&dur=…`). Las cámaras RTSP usan un tercer
  prefijo, `ipcam://fuente/id`, que **no contiene ni la contraseña ni la
  dirección del dispositivo**: sólo un identificador, y el secreto va al
  keyring. Favoritos y recientes se redactan y reescriben si traían una URL
  vieja.
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
- **Descubrimiento de pistas aislado** (control 13, verificado sobre el
  código y comprobable rompiéndolo a propósito): la red de `streams/`,
  `tracks/` y `player/` sale **sólo** por `safe_http`; el proxy que fija la
  calidad escucha **sólo en `127.0.0.1`** en un puerto efímero y **exige un
  token de 128 bits** (fuera de la ruta: `404`, y los segmentos nunca se
  sirven); y `prefs.json` no guarda nunca la URL del canal con credenciales
  dentro.
- **El canal de control de mpv no tiene autenticación ni cifrado**: quien
  pueda escribir en ese socket manda en el reproductor. Por eso vive en
  `data/ipc/` con permisos `0700` y un nombre aleatorio de 128 bits
  (`\\.\pipe\thetvview-<pid>-<rand>` en Windows, socket unix en POSIX), con
  1,5 s de timeout por comando y un modal explícito si falla. El proxy de
  calidad y el socket se apagan al cerrar el canal. Detalles y riesgos
  residuales en [SECURITY.md](SECURITY.md).

Riesgos residuales declarados (entre ellos: sin *IP pinning* contra DNS
rebinding, y `<!DOCTYPE` dentro de un `CDATA` se rechaza por ser
*fail-closed*) en **[SECURITY.md](SECURITY.md)**.

---

## Licencia

MIT — ver [LICENSE](LICENSE).
