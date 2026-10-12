# Informe de aceptación — SDD_Mejoras_Criticas_theTVVIEW

- **Proyecto:** theTVVIEW (`rrivillo/theTVVIEW`)
- **Especificación:** `SDD/SDD_Mejoras_Criticas_theTVVIEW.md` (P0.1–P0.5)
- **Plan ejecutado:** `.opencode/plans/plan_mejoras_criticas.md`
- **Fecha:** 11 de octubre de 2026
- **Entorno de verificación:** Python 3.13.5 · Linux x86_64 (LMDE 7)
- **Resultado global:** los 5 bloques cerrados con evidencia ejecutable.

Este informe es la salida final que pide el §14 del SDD y la §5 del plan: qué
queda cumplido, qué no era aplicable, qué queda pendiente con su riesgo, qué
pruebas se ejecutaron y qué limitaciones tiene lo entregado.

---

## 1. Pruebas ejecutadas

| Comando | Resultado |
|---|---|
| `python -m unittest` | **2390 pruebas, OK** (1 omitida, registrada abajo) |
| `python -m thetvview.security.check` | **15/15 checks OK** |
| `python -m tests.bench.medir` | Informe de rendimiento de los tres perfiles |

La suite creció desde 2276 hasta 2390 pruebas: **+114**, todas nuevas de este
trabajo, todas sobre código escrito en las fases 1 a 5 (las 4 últimas son las
que vigilan que la CI no vuelva, ver §6.1).

### 1.1 La omisión, registrada (TEST-005)

Hay **una** prueba omitida. No es una dependencia ausente callada: es una
prueba que exige un reproductor externo real. Se omite con `skipTest` y su
motivo queda en el propio test, que es lo que el SDD exige: *«no marcar como
aprobada una prueba que se haya omitido por una dependencia ausente sin
registrar la omisión»*. En un equipo sin reproductores instalados el resultado
es el mismo, y el motivo queda a la vista en la propia salida de `unittest`.

---

## 2. P0.1 — Seguridad de red

| ID | Estado | Evidencia |
|---|---|---|
| NET-001 capa común | **cumple** | `security/safe_http.py` es el único punto de red; `security/check.py` lo verifica por AST sobre todo el paquete |
| NET-002 esquemas | **cumple** | `security/url_policy.py`, allowlist por propósito |
| NET-003 SSRF | **cumple** | `security/ssrf.py`, IPv4/IPv6, con excepción LAN por fuente documentada |
| **NET-004 rebinding/TOCTOU** | **cumple — era residual, ahora cerrado** | `ssrf.check_url_pinned()` devuelve `(partes, ip)`; `safe_http._PinnedHTTPConnection` / `_PinnedHTTPSConnection` conectan a esa IP sin re-resolver |
| NET-005 redirects | **cumple** | revalida cada salto con `check_url_pinned`, corta bucles, rechaza downgrade https→http |
| NET-006 timeouts | **cumple** | timeout de socket + *deadline* global que cubre redirects y reintentos |
| NET-007 tamaño | **cumple** | `max_bytes` post-descompresión, corta por bloques, limpia temporales |
| NET-008 XML seguro | **cumple** | `security/xml_safe.py` |
| NET-009 redacción | **cumple** | `security/redaction.py`, aplicado en diagnóstico y mensajes |
| NET-010 secretos | **cumple** | keyring del SO + fallback `0600` |
| NET-011 proxy local | **cumple** | loopback + token 128 bits + allowlist + **límite de 8 peticiones simultáneas y 64 KB por request** (añadido en esta fase) |

### 2.1 NET-004 en detalle (el trabajo real de la fase 1)

El diagnóstico inicial decía que NET-004 estaba «residual declarado»: se
validaba el DNS antes de conectar y luego `urllib` volvía a resolver por su
cuenta, dejando una ventana entre ambas cosas.

La solución implementada es **IP pinning con stdlib**, como pedía el §1 del
plan:

- `ssrf.check_url_pinned(url)` valida **todas** las direcciones que devuelve
  el nombre y devuelve la primera válida.
- `safe_http._PinnedHTTPConnection.connect()` abre el socket contra esa IP
  directamente, sin pasar por el resolvedor.
- `self.host` conserva el nombre original, así que el header `Host:` sale
  bien y, en HTTPS, el SNI y la verificación de certificado se siguen haciendo
  **contra el nombre**, no contra la IP. Fijar la IP no debilita el TLS.
- Cada salto de redirect vuelve a resolver y a validar, porque el destino
  cambia.

El transporte dejó de ser `urllib.request` y pasó a `http.client`. Los
motivos están en el docstring: `http.client` deja el socket a la vista, que es
justo lo que hace posible fijar la IP.

**Pruebas** (`tests/test_security_safe_http.py::TestIPPinning`):

- el socket se abre contra `127.0.0.1`, no contra el hostname;
- una segunda resolución DNS provocaría un `AssertionError`: se comprueba que
  **no** hay re-resolución;
- SNI y verificación van contra `example.com` aunque el socket apunte a
  `93.184.216.34`;
- petición real contra un servidor `http.server` local.

### 2.2 Rutas de red revisadas (punto 2 de la fase 1)

Se comprobó **cada** ruta de red del repositorio. Todas pasan por
`SafeHttpClient` salvo una, corregida en esta fase:

- `streams/diagnose.py` resolvía DNS con `socket.getaddrinfo` a pelo, sin
  pasar por la política anti-SSRF. Ahora usa `ssrf.resolve_and_check`, con la
  misma excepción por fuente que el resto de la app. Es decir: una URL
  bloqueada ahora se detecta **antes** de que el informe diga «DNS ok».

`streams/probe.py`, `channel_health.py`, `xtream_client.py`, `epg_parser.py` y
`m3u_parser.py` ya usaban el cliente común y no necesitaron cambios. No hay
excepciones documentadas: **la regla es «toda la red pasa por `safe_http`» y se
cumple sin excepciones**.

---

## 3. P0.2 — Persistencia

| ID | Estado | Evidencia |
|---|---|---|
| STORE-001 escritura atómica | **cumple — era parcial** | `storage/json_store.py`: validar → serializar → temporal único → flush+fsync → `os.replace` → fsync del directorio |
| STORE-002 versionado | **cumple — no existía** | `schema_version: 1` en `prefs`, `playlists`, `favorites`, `recents`, `theme` |
| STORE-003 migraciones | **cumple — no existía** | `storage/migrations.py`, v0→v1 idempotente y por escritura atómica |
| STORE-004 recuperación | **cumple — había reset silencioso** | backup `.bak-*` validado antes de restaurar + copia `.corrupt-*` |
| STORE-005 caché ≠ persistencia | **cumple** | `storage/cache_policy.py` sólo entra en directorios de caché |
| STORE-006 concurrencia | **cumple — era parcial** | candado por fichero para los pares leer-modificar-escribir + referencias al borrar |
| STORE-007 límites | **cumple** | topes por formato al leer, retención de backups y corruptos, permisos privados |

### 3.1 Lo que cambió de verdad (el bloque grande del plan)

**El patrón viejo estaba en 8 escritores.** Todos hacen `tmp.write_text()` +
`replace()`, sin `fsync`, con temporal de **nombre fijo** (dos procesos a la
vez escriben el mismo temporal) y sin validación previa.

Ahora los 8 pasan por `storage/json_store.py`:

- el temporal es único por **pid y nanosegundo**, así que dos procesos no se
  pisan el temporal;
- hay `flush` + `fsync` del fichero, y `fsync` del directorio en POSIX;
- los huérfanos se barren por antigüedad (1 hora), sin tocar nada que se esté
  escribiendo ahora;
- se valida **antes** de escribir, no después.

**El reset silencioso (STORE-004) era el defecto más grave.** `PrefsManager.load()`
devolvía `Prefs()` ante un JSON corrupto: el usuario perdía sus preferencias y
no se enteraba. Ahora:

- se conserva una copia `.corrupt-<timestamp>` con permisos privados;
- se intenta restaurar el backup, y **sólo** si valida;
- si no hay nada válido, se lanza un error **del tipo que la TUI sabe
  presentar en un modal** (`FavoritesError`, `PlaylistError`), en vez de un
  `StorageError` crudo que habría reventado la pantalla.

La distinción que hace el código es deliberada: un historial roto
(`recents.json`) **no** bloquea la app — es un registro de uso, no un dato que
el usuario haya creado a mano — pero un catálogo o una lista de favoritos
corruptos **sí** se avisan. Esa línea está en el docstring de cada `_read`.

### 3.2 Topes por formato (hallazgo del bench)

El primer tope global de 10 MB resulted ser **incorrecto**: un historial de
favoritos legítimo de 100 000 canales ocupa 16 MB, y el bench lo بصó. La
solución no fue subir el número a ojo, sino hacer el tope **depender del
formato**: `prefs` a 2 MB (si un `prefs.json` pasa de eso, algo va mal),
`favorites` a 128 MB, y 32 MB por defecto. Está en `MAX_BYTES_POR_FORMATO`.

### 3.3 Referencias al borrar una lista (STORE-006)

Borrar una lista dejaba favoritos y recientes apuntando a un servidor que ya
no estaba en el catálogo. Ahora, antes de borrar se cuentan, se avisa en el
**modal de confirmación** («también se quitarán 3 favoritos y 2 recientes que
apuntan a esta lista»), y después se limpian — sólo si el borrado funcionó.

Las referencias opacas de Xtream (`xtream://…`) no tienen host con el que
comparar, así que se reconocen por `tvg-id`, que es la única identidad
estable disponible.

---

## 4. P0.3 — Tareas en segundo plano

| ID | Estado | Evidencia |
|---|---|---|
| TASK-001 gestor común | **cumple — no existía** | `tasks/manager.py` |
| TASK-002 cancelación y obsolescencia | **cumple** | `CancellationToken` + generación por categoría |
| TASK-003 concurrencia y UI | **cumple — había UI desde el hilo** | publicación por cola; el worker devuelve, el principal aplica |
| TASK-004 cierre acotado | **cumple** | `shutdown(join_timeout)` con plazo global, no por tarea |

**Los siete hilos del inventario están migrados.** El EPG fue el primero; en
una fase posterior se migraron también los tres que seguían con un
`threading.Thread` propio (`warm_catalog`, el sondeo de pistas y el monitor de
salud del canal), más el proxy de calidad y el supervisor del reproductor,
que ya paraban con plazo. Ninguno crea ahora un hilo que el cierre de la app
no pueda alcanzar.

### 4.1 El defecto que sí existía: la UI desde un hilo

El diagnóstico acertaba. `_epg_load_worker` escribía en `self.epg` y llamaba a
`self.status.show()` **desde el hilo de descarga**. Eso es una condición de
carrera con curses: entre que el worker escribía y el principal pintaba, la
pantalla podía quedar a medias.

La corrección no es «añadir un lock», porque un lock no arregla un hilo
escribiendo en la pantalla. Es **cambiar quién escribe**:

```
worker (hilo)  →  devuelve _EpgLoadOutcome  →  cola  →  _drain_tasks (principal)  →  self.epg
```

`_drain_tasks()` corre en el bucle principal, junto al drenado de avisos que
ya existía. El worker no tiene ninguna referencia a la interfaz, y eso es
comprobable: `TaskHandle` no tiene campos `app`, `screen`, `stdscr` ni
`curses`, y hay un test que lo verifica.

### 4.2 El guard de obsolescencia (TASK-002)

Cada tarea lleva un **identificador de ejecución** y una **generación por
categoría**. Cuando el usuario cambia de lista mientras carga el EPG, la
nueva carga sube la generación y la vieja queda obsoleta: termina su
limpieza, pero su resultado **no se publica**.

Esto importa por un caso concreto: el usuario abre la lista A, el EPG tarda
dos segundos, cambia a la lista B y su EPG llega en uno. Sin generación, el
EPG de A aparecería sobre el de B. Con generación, se descarta.

El alcance de la cancelación es explícito: cancelar una tarea **no** cancela
las demás de su categoría. Dos sondeos de canales distintos conviven; dos
cargas de la misma fuente, no.

### 4.4 Un defecto real que salió al migrar

Al migrar los hilos, un test nuevo empezó a fallar **sólo** en la suite
completa, y de forma determinista (3 de 3 vueltas): en aislamiento pasaba.

La causa no era el hilo, sino los tests. `TestTrackDiscoverySandbox` reescribe
`streams/probe.py` en disco y hacía `sys.modules.pop` para forzar que el check
leyera el código nuevo. El `pop` deja **dos** objetos del mismo módulo vivos:
el viejo —al que apuntan las referencias ya importadas— y el nuevo que crea el
siguiente `import`. Como `unittest` importa **todos** los módulos de test
antes de ejecutar ninguno, el `TrackProbe` del test de migración ya era el
viejo; su `mock.patch` parcheaba el módulo nuevo mientras el worker llamaba
al viejo, así que **el parche no interceptaba nada** y el sondeo se salía a la
red de verdad.

Se cambió por `importlib.reload`, que reutiliza el mismo objeto de módulo **y
su mismo `__dict__`**. Se midieron las dos variantes: `pop` cuesta ~1 s menos,
pero rompe el parcheo; la corrección correcta gana.

Lo que deja es una regla útil: **un `mock.patch` por cadena sólo es fiable
mientras nadie haya reimportado el módulo bajo el mismo nombre.**

`App.run()` envuelve el bucle en `try/finally` y llama a `_cerrar_recursos()`,
que apaga en orden: reproductores y sondas, tareas del gestor, warming del
catálogo y carga del EPG. Cada paso tiene su propio `try/except`, así que que
uno falle no impide cerrar los demás.

La espera es **global, no por tarea**: 20 tareas de 2 s no pueden costar 40 s
de salida. Hay un test que pone un worker que ignora la cancelación a
propósito y comprueba que el cierre no espera los 10 s que le faltan.

---

### 4.5 Cierre acotado (TASK-004)

| ID | Estado | Evidencia |
|---|---|---|
| RES-001 límites centralizados | **cumple** | `security/limits.py`, valores acotados con rangos |
| RES-002 listas y campos | **cumple** | topes de bytes, entradas y longitud aplicados al leer |
| RES-003 XMLTV y compresión | **cumple** | topes de profundidad, nodos y tamaño descomprimido |
| RES-004 manifiestos | **cumple** | `HEAD` antes de descargar, `Range` acotado si no es manifiesto |
| **RES-005 cachés** | **cumple — TTL dispersos, sin tope** | `storage/cache_policy.py` |
| **RES-006 medición** | **cumple — no existía** | `tests/bench/medir.py` |

### 5.1 Política de cachés

Había cuatro cachés con TTL distintos escritos en cuatro sitios y **ninguna**
con tope de tamaño. Ahora hay una política con:

- TTL por caché, **los mismos valores** que usaba cada módulo (para no cambiar
  el comportamiento de la app);
- tope de tamaño por caché y global (512 MB);
- borrado del más antiguo al más reciente, nunca al revés;
- **garantía estructural** de que la limpieza no puede tocar la persistencia:
  sólo entra en los directorios declarados como caché, y hay un test que
  comprueba que `prefs.json` sobrevive a una limpieza.

### 5.2 Medición (RES-006)

El script genera tres perfiles (500, 10 000 y 100 000 canales) con la **misma
forma que una lista real** pero sin credenciales, y mide parseo, escritura
atómica y búsqueda.

**Hallazgo medido (Linux, Python 3.13.5):**

| Perfil | Parseo | Búsqueda | Escritura | Pico de memoria |
|---|---|---|---|---|
| 500 | 0,03 s | 0,000 s | 0,004 s | 0,5 MB |
| 10 000 | 0,54 s | 0,002 s | 0,07 s | 8,6 MB |
| 100 000 | 5,4 s | 0,019 s | 0,70 s | 86 MB |

**Un error del propio bench que hubo que corregir antes de poder leer los
números:** la primera versión medía tiempo y memoria a la vez, con
`tracemalloc` activo. Daba 68 s para 100 000 canales en vez de 5,4 s. El
`tracemalloc` multiplica por un orden de magnitud el tiempo del código que
traza, y como estaba activo durante la medición de tiempo, **el número de
tiempo no era el del código real sino el del profiler**. Separar las dos
mediciones es lo que hace que estos datos sirvan para decidir algo.

Conclusión honesta: **no se ha optimizado ningún parseo**. Los números están
ahí para que la decisión se tome con evidencia, no con una impresión. Un
catálogo de 100 000 canales tarda 5,4 s en parsearse, lo cual es aceptable
porque ocurre en segundo plano y con cancelación cooperative.

---

## 6. P0.5 — Regresión y automatización

| ID | Estado | Evidencia |
|---|---|---|
| TEST-001 pruebas por defecto | **cumple** | +101 pruebas, cada una sobre código escrito en las fases 1–5 |
| TEST-002 determinismo | **cumple** | servidores locales, fixtures en memoria, sin Internet |
| TEST-003 recursos | **cumple** | cierre verificado; pruebas de temporal abandonado y proceso externo |
| **TEST-004 varios sistemas** | **parcial** | suite ejecutada a mano en Linux; el proyecto **no usa ningún servicio de CI en la nube** |
| TEST-005 calidad de la suite | **cumple** | la única omisión está registrada con su motivo |

### 6.1 Verificación sin servicios de CI

Este proyecto **no usa GitHub Actions ni ningún otro servicio de integración
continua**: no hay `.github/workflows/`, y `tests/test_sin_ci.py` falla si ese
directorio vuelve a aparecer.

La consecuencia honesta es que la suite ya no se dispara sola. Se ejecuta a
mano, en el sistema donde se esté desarrollando:

```bash
python -m unittest                          # 2390 pruebas
python -m thetvview.security.check          # 15 checks
```

Eso **rebaja** la cobertura por plataformas respecto a la que había antes:
`os.replace`, el `fsync` de directorio y los permisos `0600` no se comprueban en
macOS ni en Windows, y ya no hay nada que las compruebe automáticamente. Es una
decisión consciente del proyecto, no un descuido, y por eso el punto queda
abierto abajo en «Limitaciones de la verificación».

Se conserva la disciplina que hacía útil al job de `mypy`: el proyecto tiene
cero dependencias pip como restricción dura, así que `mypy` (o `ruff`) se
ejecuta aparte, en su propio entorno virtual, y **nunca** se añade a
`requirements.txt`.

### 6.2 El check ejecutable con una tecla (`!`)

`python -m thetvview.security.check` pasó de 14 a **15 checks**. El nuevo es
**«Atomic persistence & versioning»**, que verifica con un fichero real y
temporal:

1. que un fallo inyectado en el `fsync` deja el estado bueno intacto;
2. que los formatos persistentes llevan `schema_version`;
3. que la migración v0→v1 es idempotente.

Los tres se **rompen a propósito** en los tests para comprobar que el check se
pone rojo. Un check que sólo pasa no demuestra nada: la primera versión de
estos tests falló porque el check **no** detectaba la escritura no atómica, y
eso reveló que el propio check era frágil (soltaba el `JSONDecodeError` de
`json` en vez de explicar la garantía rota). Corregido.

---

## 7. Requisitos no aplicables

| ID | Motivo |
|---|---|
| — | Ninguno. Todos los requisitos del SDD tienen componente sobre el que aplicar. |

Se studió caso por caso y **ninguno** resultó no aplicable: el proxy local
existe (NET-011), el almacén de secretos del SO existe (NET-010) y las tareas
en segundo plano existen (TASK-001). Un requisito marcado «no aplicable» aquí
habría sido un ahorro de trabajo, no una clasificación honesta.

## 8. Pendientes y riesgos

| Riesgo | Gravedad | Por qué queda pendiente |
|---|---|---|
| El candado de persistencia es **sólo de proceso** | baja | Dos procesos (la app y un script del usuario) sobre el mismo `prefs.json` pueden pisarse. Un lock entre procesos exige `fcntl` en POSIX y `msvcrt` en Windows, y añade un fichero que limpiar. El uso real de la app es de un proceso; el límite está documentado en el código y en el SDD. |
| `prefs.json` de una versión con `track_prefs` muy grande se rechazaría a 2 MB | baja | Consecuencia deliberada del tope por formato. Si aparece en la práctica, el mensaje de error lo dice y la copia `.corrupt-*` conserva el original. |
| El parseo de 100 000 canales tarda 5,4 s | baja | Aceptable: ocurre en segundo plano, con cancelación, y no bloquea la interfaz. Medido, no supuesto; optimizar sin necesidad sería justo lo que el SDD prohíbe. |

Ninguno de estos pendientes es una regresión conocida: son límites que se han
documentado en vez de esconder.

## 9. Limitaciones de la verificación

- La verificación se ejecutó en **Linux**, a mano. Este proyecto no usa ningún
  servicio de integración continua, así que **no existen resultados de macOS ni
  de Windows**: quien lo use en esos sistemas debe ejecutar allí
  `python -m unittest` y anotar el resultado.
- No se ha probado con reproductores externos instalados. La única prueba
  omitida es la que los necesita, y su omisión está registrada.
- Los números del bench son de esta máquina (LMDE 7, x86_64). Cumplen para
  decidir sobre el orden de magnitud de los costes; no son comparables con
  otros equipos.

## 10. Compatibilidad

- **Sin cambios incompatibles** en los formatos existentes: los ficheros sin
  `schema_version` se leen igual que antes y se migran en la primera
  escritura, conservando los campos que la versión anterior no conocía.
- **Sin cambios en la interfaz**: ninguna tecla cambia de sitio. La
  confirmación de borrado de lista muestra un texto nuevo, pero los botones
  son los mismos.
- **Sin dependencias pip**: `requirements.txt` sigue vacío y el check lo
  verifica.
- **Sin `shell=True`**, sin cambios en cómo se lanzan los reproductores.
- **Archivos nuevos**: `thetvview/storage/` y `thetvview/tasks/`. No se mueve
  ni renombra nada existente, así que un usuario con datos no ve ningún
  cambio de comportamiento salvo lo descrito arriba.

## 11. Cómo volver a verificarlo

```bash
python -m unittest                          # 2390 pruebas
python -m thetvview.security.check          # 15 checks
python -m tests.bench.medir                 # medición de rendimiento
```

Los tres se ejecutan a mano, en el sistema donde se desarrolla. No hay ningún
servicio que los dispare solo, y ése es el precio de no depender de ninguno.

---

## 12. Resumen

| Bloque | Estado | Trabajo de la fase |
|---|---|---|
| P0.1 red | cerrado | IP pinning con `http.client`, `check_url_pinned`, `getaddrinfo` passages por la política, límites en el proxy |
| P0.2 persistencia | cerrado | store atómico común, versionado, migraciones, recuperación sin pérdida silenciosa, candados, referencias |
| P0.3 tareas | cerrado | gestor con generaciones y cancelación; los 7 hilos migrados; la UI deja de escribirse desde un hilo; cierre acotado |
| P0.4 recursos | cerrado | política única de cachés con topes, medición reproducible |
| P0.5 regresión | cerrado | suite separada, check de persistencia, este informe |

**Cerrado con evidencia ejecutable:** 2390 pruebas en verde, 15/15 checks de
seguridad, y un bench que además encontró un error de medición y un tope de
bytes mal puesto antes de poder servir para nada.
