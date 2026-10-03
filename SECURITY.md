# Seguridad — theTVVIEW

Documento vivo. El estado real se comprueba con:

```bash
python -m thetvview.security.check          # exit != 0 si algo falla
python -m thetvview.security.check --json   # para CI
python -m thetvview.security.check -v       # detalle de cada check
```

Dentro de la TUI, la tecla **`!`** ejecuta los mismos checks y abre **siempre**
un modal con el resultado. Ningún error ni aviso se comunica sólo en la barra
de estado: ésa es una restricción de diseño del proyecto, no una preferencia.

Restricciones duras del repo (ver `AGENTS.md`):

- Python 3.13.5, **cero dependencias pip** (`requirements.txt` vacío).
- Nunca `shell=True`; el reproductor recibe un argv en lista terminado en `--`.
- Toda descarga con `timeout` y error apto para modal.
- Sin colgues de `curses` (resize, excepciones, hilos y `stdscr=None`).

---

## 1. Superficie

| Superficie | Quién la controla | Bloqueo |
|---|---|---|
| URL de playlist / `x-tvg-url` / URL de canal | quien publique la lista | `url_policy` + `ssrf` |
| Cuerpo M3U / XMLTV / JSON del panel | servidor remoto | `local_files`, `xml_safe`, `limits` |
| `EXTVLCOPT` / `KODIPROP` de la lista | quien publique la lista | lista blanca de `player` |
| Plantilla `catchup-source` del `#EXTINF` | quien publique la lista | `url_policy` (stream) + placeholders blancos |
| Declaración `tv_archive*` del panel Xtream | servidor remoto | `catchup.can_use_catchup` (fail-closed) |
| Nombre de canal, grupo, título EPG | servidor remoto | redacción al pintar/mostrar |
| Ficheros locales `data/` | usuario del SO | permisos `0600`/`0700` |
| Argumentos al reproductor | todo lo anterior | validación de esquema + `--` |
| Manifiesto HLS/MPD del canal | proveedor | `xml_safe` + límites + `safe_http` |
| Sondeo de la encuesta (`-m thetvview.tracks.survey`) | proveedores terceros | `safe_http` (igual que la app), sin escribir en disco, URLs redactadas |
| Pistas declaradas (idioma, calidad) | proveedor | se muestran **como datos**, nunca se ejecutan |
| IPC local de mpv | otros procesos del usuario | socket en `data/ipc/` 0700 + nombre aleatorio |
| Master fijado (proxy de calidad) | procesos del usuario | 127.0.0.1 + puerto efímero + token 128 bits |

## 2. Reglas

1. **Un solo camino de red.** Todo lo que sale a la red pasa por
   `thetvview.security.safe_http.SafeHttpClient`: timeout total, tope de
   bytes, redirects manuales (máx. 3, revalidados uno a uno), TLS estricto
   (CA del sistema, `check_hostname`, TLS ≥ 1.2) y semáforo global de
   conexiones. No hay un segundo `urlopen` suelto.
2. **Política de URL antes de tocar la red.** Esquemas permitidos
   (`http`, `https` y `ffmpeg://` sólo para streams), longitud, host y puerto
   en `url_policy`; destino en `ssrf` (redes privadas, loopback, link-local,
   metadatos de nube, nombres `.local`, y **formas numéricas ambiguas** como
   `0177.0.0.1` o `2130706433`).
3. **La excepción es por fuente, nunca global.** `allow_private_network` se
   guarda por playlist y hay que confirmarlo en un modal al añadir la fuente.
4. **Cero secretos en `Channel.url`.** Los streams Xtream se referencian como
   `xtream://<fuente>/<tipo>/<id>.<ext>` y se resuelven en caliente contra
   `PlaylistManager.get_credentials()`. Las listas de recientes/favoritos se
   redactan al leer y se reescriben si traían una URL antigua con credenciales.
5. **Redacción central.** `redaction.redact_text/redact_exception/
   redact_mapping/SecretStr` se usan en mensajes de error, excepciones, UI y
   volcados. Sólo se redacta lo que es un secreto real: `?token=` de M3U se
   conserva a propósito (sin él la lista no se puede reproducir).
6. **El XML nunca ejecuta.** `xml_safe.parse_xml` rechaza `<!DOCTYPE>`,
   `<!ENTITY>`, profundidad y nodos excesivos; `.gz` se descomprime con tope.
7. **Nada por shell.** `subprocess` sólo con argv en lista; la URL va tras un
   `--` y se valida con `purpose="stream"` antes de construir el argv.
8. **El archivo sólo si el proveedor lo declara.** `thetvview/catchup.py` es la
   única fuente de la verdad: `can_use_catchup()` exige `provider_declared` **y**
   `enabled`, y `build_playback_request()` valida antes de construir. Ni el EPG,
   ni una URL live, ni que otro canal tenga archivo habilitan nada, y sin ventana
   declarada la respuesta es "no disponible". La referencia que circula es opaca
   (`xtream-ts://…`, sin usuario ni contraseña) y se resuelve en caliente, igual
   que `xtream://`. Ningún módulo construye rutas de sonda de endpoint: lo
   comprueba el check 12 sobre el AST de todo el paquete.
9. **`catchup-source` es entrada no confiable.** Viene de la lista, o sea de
   quien la publica. Sólo se sustituyen los placeholders de una lista blanca
   (`{start}`, `{end}`, `{duration}`, `{utc}`); cualquier otro nombre es un error
   con mensaje, y la URL renderizada se valida con `purpose="stream"` antes de
   llegar al reproductor.

## 3. Modelos de amenaza (SDD §46)

| ID | Amenaza | Mitigación |
|---|---|---|
| T-001 | Credencial en logs/excepciones/UI | redacción central + `SecretStr` + test AST de `print`/`logging` |
| T-002 | SSRF hacia la LAN o metadatos | `ssrf.check_url` en cada conexión y en **cada salto** de redirect |
| T-003 | Redirect bypass / downgrade https→http | redirects manuales, tope 3, revalidación y bloqueo explícito del downgrade |
| T-004 | Respuesta gigantesca | `limits` + `local_files.read_limited_*` + `max_entries` |
| T-005 | XML/EPG malicioso (XXE, billion laughs) | `xml_safe` sin DTD ni entidades |
| T-006 | Prompt injection desde el catálogo | **fuera de alcance hoy**: no hay capa IA en `thetvview/` (verificado por check) |
| T-007 | Shell/stream injection vía URL de canal | `player.command_for` valida el esquema y termina el argv en `--` |

## 4. Comprobaciones (`SDD §45`)

`security/check.py` ejecuta trece checks, cada uno de ellos **falla si no puede
comprobar** la propiedad (un check verde significa "verificado", no "desconozco"):

```text
[✓] Secret redaction
[✓] HTTPS certificate verification
[✓] SSRF policy
[✓] Redirect policy
[✓] Response limits
[✓] XML safe parsing
[✓] AI secret sanitizer
[✓] Shell-safe player invocation
[✓] Credential storage
[✓] No plaintext password logs
[✓] Zero pip dependencies
[✓] Catch-up capability gate
[✓] Track discovery sandbox
```

El undécimo sustituye al `pip-audit` del SDD §52: con `requirements.txt`
vacío no hay cadena de suministro que auditar, así que se comprueba que siga
vacío y que **todos** los `import` del paquete sean del stdlib.

El duodécimo (`catchup_capability_gate`, del SDD de catch-up) **verifica** la
invariante funcional en vez de suponerla, y se puede romper a propósito para
comprobar que lo detecta (`tests/test_security_check.py::TestCatchupGate`):

1. `can_use_catchup()` es Certain sólo con las dos condiciones (se prueba sobre
   la **función**, no sobre los datos: relajar la invariancia pone el check en
   rojo).
2. Construir con un canal no declarado **lanza**, y una petición fuera de la
   ventana declarada también.
3. La referencia de archivo no contiene `password=`, `validate_url` la rechaza
   con `purpose="stream"`, y sobrevive a favoritos y recientes sin dejar
   credenciales en disco (también cuando lo que se guarda es la URL ya
   resuelta, que se redacta).
4. `catchup.py` no importa nada de red ni de procesos, y **ningún** módulo del
   paquete escribe rutas de sonda de endpoint.

El decimotercero (`track_discovery_sandbox`, de la selección de pistas) se
puede romper a propósito igual que el anterior
(`tests/test_security_check.py::TestTrackDiscoverySandbox`):

1. **Red sólo por `safe_http`.** Ningún módulo de `streams/`, `tracks/` o
   `player/` llama a `urlopen`, `build_opener`, `create_connection`,
   `HTTP(S)Connection` ni importa `requests`/`httpx`/`aiohttp`.
2. **El proxy de calidad sólo escucha en loopback** (se comprueba sobre la
   función `PinProxy.start`, que rechaza cualquier host distinto de
   `127.0.0.1`) **y exige token**: sin él, o en cualquier otra ruta, la
   respuesta es `404`, y no se sirve ningún segmento.
3. **`prefs.json` de pistas nunca guarda la URL cruda**: ni el usuario, ni la
   contraseña, ni el host de la ruta Xtream, ni en las claves ni en los
   valores. Y la clave de preferencias por canal se calcula sobre la URL
   **redactada** (se comprueba sobre la función `channel_key`, no sobre los
   datos: hashear la URL cruda no escribiría nada legible, pero dejaría un
   hash atacable por diccionario).

## 5. Riesgos residuales (declarados)

1. **SSRF sin *IP pinning*.** La URL se valida antes de conectar y cada salto
   de redirect se revalida, pero la IP resolvida no se fija para la conexión
   posterior: queda una ventana teórica de *DNS rebinding* entre la
   comprobación y el `connect()`. Cerrarla exige un `socket` con IP ya
   resuelta, que `urllib` no permite sin reescribir el transporte.
2. **Falso positivo `DOCTYPE` en CDATA/comentario.** `xml_safe.assert_safe_text`
   inspecciona el texto plano, no el árbol: un XMLTV que mencione
   `<!DOCTYPE` dentro de un `CDATA` o un comentario se rechaza. Es
   *fail-closed* y el SDD lo acepta; el síntoma es "guía no cargada".
3. **`resolve=False`.** Las comprobaciones sin DNS sólo ven literales, nombres
   locales y formas numéricas ambiguas; el destino real de un nombre se
   comprueba con `resolve=True` en cada conexión.
4. **Red privada concedida a propósito.** Si el usuario confirma
   `allow_private_network` para una fuente, esa fuente sí puede hablar con la
   LAN. La aplicación no distingue entre "mi panel Xtream" y "una lista que me
   pasaron".
5. **Keyring del SO.** Sin keyring utilizable la app degrada a
   `data/playlists.json` con permisos `0600`; la contraseña no se guarda cifrada
   en disco, sólo con permisos de archivo.
6. **El reproductor es un binario de terceros.** Se controla el argv, no lo que
   `mpv`/`vlc`/`mplayer` hace después con la URL (p. ej. seguir playlists
   anidadas). No hay sandbox ni namespaces de red.
7. **Sin capa IA.** SDD §29-31 (prompt injection, contexto aislado, secretos
   fuera del contexto del modelo) queda fuera hasta que exista tal capa; el
   check garantiza que no haya aparecido un SDK de LLM por accidente y que
   `redact_text()` está listo para cuando llegue.
8. **`catchup-source` es una plantilla publicada por un tercero.** Aunque se
   validen los placeholders y el esquema, la URL final sale de texto que el
   proveedor controla: un `catchup-source` que apunte a un host distinto del de la
   lista se reproduce si su esquema es `http(s)`. Mitigaciones aplicadas:
   allowlist de placeholders, `validate_url(purpose="stream")`, credenciales
   fuera de la referencia opaca y redacción en todo mensaje. No queda cerrado: la
   app no puede saber si ese host es el del proveedor.
9. **Zona horaria del panel.** `timeshift.php` espera el sello del `start` en la
   zona del servidor y la app lo manda en la local del cliente. Normalmente
   coinciden; si no, el panel devuelve el programa equivocado. Se asume y se
   documenta: detectarlo exigiría sondear el servidor, que el SDD prohíbe.
10. **Panel que declara archivo sin ventana.** `tv_archive=1` sin
   `tv_archive_duration` deja el canal **sin** catch-up. Es *fail-closed* y es lo
   correcto, pero puede sorprender a quien use un panel mal declarado.

## 5-bis. Canal de control de mpv (IPC local)

mpv abre un canal de control con `--input-ipc-server=<ruta>` y acepta ahí
**una línea JSON por comando** (`set_property aid|sid …`). Es el único modo
de cambiar de pista sin reabrir el canal, y el único de los tres
reproductores que lo tiene.

**No tiene autenticación ni cifrado**: quien pueda escribir en ese socket
manda en el reproductor. Lo que lo hace aceptable:

| Medida | Detalle |
|---|---|
| Sólo local | Socket unix en POSIX, *named pipe* en Windows. Nunca red. |
| Directorio `0700` | `data/ipc/`, creado con permisos privados. |
| Nombre impredecible | `mpv-<pid>-<16 bytes aleatorios en hex>.sock`; 128 bits. |
| Timeout corto | 1,5 s por comando: si mpv no contesta, la TUI no espera. |
| Fallo explícito | Cualquier error sale como **modal** que dice que hay que reabrir el canal. |
| Se borra al salir | `NowPlayingScreen.stop_tracks()` borra el socket. |

Residual declarado: **otro proceso del mismo usuario** podría encontrar el
socket recorriendo `data/ipc/` (tiene permiso para leerlo). Sin embargo,
puede hacerlo quien ya puede leer los `data/` del usuario, que es el mismo
nivel de confianza que ya asumimos para el resto de la caché. Un atacante de
otro usuario del mismo equipo no puede.

## 5-ter. Proxy local de calidad (pinning)

Ninguno de los tres reproductores sabe elegir una variante concreta de un
master HLS (comprobado ejecutándolos, ver `player/capabilities.py`). Para
ofrecer calidad manual, la app reescribe el master con **una sola variante** y
lo sirve ella misma:

| Medida | Detalle |
|---|---|
| Sólo loopback | `127.0.0.1` en **puerto efímero** (puerto 0). `PinProxy.start` rechaza cualquier otro host. |
| Token en la ruta | 128 bits aleatorios: `http://127.0.0.1:<puerto>/<token>/master.m3u8`. |
| Allowlist de rutas | Sólo ese master (y su alias `index.m3u8`). Todo lo demás: `404`. |
| **No** proxea segmentos | Las URI del master reescrito apuntan al proveedor: el reproductor sigue hablando con él y con sus cabeceras `EXTVLCOPT`. |
| Sólo reescribe el master | El body se construye con los valores del manifiesto ya validados por el parser; nunca se interpola texto crudo del proveedor en una cabecera HTTP ni en una línea de respuesta. |
| Sin logs | `log_message` silenciado (el stderr del proceso es la TUI). |
| Se apaga siempre | `try/finally` + `stop()` idempotente + `atexit`. |

Residual declarado: el proxy **sí** expone durante la reproducción qué canal
se está viendo y con qué calidad, a cualquier proceso del mismo usuario que
consiga el token. El token se genera al azar por reproducción y no se
persiste, así que no se puede reutilizar para otro canal; y el riesgo real
es bajo porque esa información ya está en la línea de órdenes del proceso
del reproductor, que es legible por el mismo usuario.

## 6. Lo que **no** se afirma

`security-check` no puede (ni pretende) garantizar:

- Que el servidor IPTV sea benigno.
- Que el contenido del stream sea seguro.
- Que el reproductor no tenga vulnerabilidades.
- Que el panel no registre la actividad del usuario.

Nada en la app ejecuta *payloads* de la playlist: los datos se pintan como
texto. Fuera de eso:

```text
Network source is not trusted.
Stream content is external.
Player security depends on the playback backend.
```

## 7. Verificación continua

```bash
python -m unittest            # pruebas (incluye tests/test_security_*.py)
/usr/bin/mypy thetvview/ --ignore-missing-imports
python -m thetvview.security.check
```

Los casos SEC-001…SEC-008 del SDD §49 viven en `tests/test_security_*.py` y
`tests/test_security_integration.py`.
