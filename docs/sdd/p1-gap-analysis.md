# Auditoría de brechas P1 — theTVVIEW

**Documento:** `docs/sdd/p1-gap-analysis.md`
**Especificación:** `SDD/SDD_Mejoras_Prioridad_Alta_theTVVIEW.md` v1.0 (P1.1–P1.8)
**Plan de ejecución:** `.opencode/plans/plan-mejoras-prioridad-alta.md`
**Fecha de medición:** 2026-10-12
**Alcance:** AUD-001 (matriz de brechas + línea base) y AUD-002 (caracterización).

> Este documento **no** toca `thetvview/`. Describe lo que hay, con archivo,
> clase, función y test como evidencia. Toda fila es verificable con `grep`.

---

## 1. Línea base medida

### 1.1 Suite completa

```text
$ python -m unittest discover -q
Ran 2390 tests in 116.198s
OK (skipped=1)

$ python -m thetvview.security.check
SECURITY CHECK PASSED (15/15)     # exit 0
```

- 2 390 tests, 0 fallos, 1 skip (dependencia de binario opcional, con razón).
- 83 módulos en `thetvview/` (34 397 LOC), 97 ficheros en `tests/`.
- Python 3.13.5 · Linux 6.18 · glibc 2.41.
- Único `skip`: prueba que requiere un binario externo ausente.

### 1.2 Coste de parseo M3U (`python -m tests.bench.medir`)

| perfil    | entradas  | parseo | pico MB | escritura | ms/1k (parseo) |
|-----------|-----------|--------|---------|-----------|----------------|
| pequena   | 500       | 0.025s | 0.46    | 0.004s    | 49.20 |
| mediana   | 10 000    | 0.521s | 8.57    | 0.063s    | 52.12 |
| grande    | 100 000   | 5.217s | 86.17   | 0.692s    | 52.17 |

**Lectura:** el parseo es **lineal** (`ms/1k` constante ≈ 52). Es el FR-M3U-007
ya cumplido: no hace falta.indexarlo.

### 1.3 Coste de `Epg.now_playing` (`tests/bench/medir.py`, script ad-hoc reproducible)

Peor caso: el programa pedido está **al final** de la lista del canal; 40
consultas (un render de la pantalla de Favoritos con 40 favoritos).

| guía                                   | programas   | 40× `now_playing` | ms/consulta |
|----------------------------------------|-------------|------------------|-------------|
| 20 000 canales × 14 programas (7 días)| 280 000     | 0.21 ms          | 0.005 |
| 20 000 canales × 288 programas        | 5 760 000   | 1.89 ms          | 0.047 |
| 2 000 canales × 1 440 programas       | 2 880 000   | 10.26 ms         | 0.256 |

**Lectura honesta:** el escaneo lineal **no** es un desastre hoy (0.005 ms en la
guía normal). El daño aparece con guías densas o de *timeshift* largo, y —lo que
más pesa— en que el coste **crece linealmente con el tamaño del canal** en vez de
con su logaritmo. Por eso el índice + `bisect_right` es una mejora real, pero el
argumento correcto no es «la app se cuelga»: es **complejidad y previsibilidad**
(FR-EPG-008/012). No se promete una latencia que hoy no seIncumple.

### 1.4 Comportamiento actual de lanzamiento y terminación

| Motor    | Señal de «reproduciendo» disponible                      | Fuente                              |
|----------|----------------------------------------------------------|-------------------------------------|
| mpv      | IPC (`time-pos`, `track-list`, `idle-active`)             | `player/mpv_ipc.py`                 |
| VLC      | ninguna (proceso vivo o nada)                             | —                                   |
| mplayer  | ninguna (proceso vivo o nada)                             | —                                   |

**Consecuencia medida:** `ReconnectSupervisor._bucle` (`supervisor.py:255-259`)
publica `PLAYING` en cuanto `proc.poll() is None`. Un mpv que abre el proceso y
se queda bloqueado leyendo un manifiesto vacío produce la **misma** señal que uno
que está decodificando. Éste es el gap #1 de la auditoría (§3).

---

## 2. Matriz de brechas

Leyenda: **S** = `SATISFECHO` · **P** = `PARCIAL` · **A** = `AUSENTE` ·
**N/A** = `NO_APLICA`.

### 2.1 P1.1 Ciclo de vida de la reproducción (`thetvview/player/`)

| Req | Estado | Evidencia (archivo:símbolo / test) | Gap a cerrar |
|-----|--------|------------------------------------|--------------|
| FR-PLAY-001 id de sesión | **A** | — (no existe `session_id` en ningún sitio; `grep -rn "session_id" thetvview/` → 0) | PLAY-001 |
| FR-PLAY-002 estados | **P** | `supervisor.PlaybackState` (6 estados, pero con valores distintos: `connecting/playing/buffering/reconnecting/stopped/error`). No hay `PREPARING/STARTING/ENDED`. | PLAY-001 |
| FR-PLAY-003 transiciones | **A** | `supervisor.ReconnectSupervisor._cambiar` (línea 304) acepta **cualquier** salto; no hay tabla ni rechazo | PLAY-002 |
| FR-PLAY-004 fases | **A** | No existe `phase`; sólo hay estado de supervisor | PLAY-001 |
| FR-PLAY-005 aislamiento | **P** | `tasks/manager.py:244` `TaskManager.is_current()` — el patrón **ya existe** para tareas, pero la reproducción no lo usa (`screens.NowPlayingScreen` no comprueba nada) | PLAY-004 |
| FR-PLAY-006 cancelación | **P** | `supervisor.detach()` (línea 234) cubre «lo paró el usuario» en el bucle de reconexión. **No** cubre preparación/lanzamiento ni la ruta `app._cerrar_recursos` | PLAY-004 |
| FR-PLAY-007 limpieza centralizada | **P** | `screens.NowPlayingScreen.stop_tracks` (línea 2831) apaga supervisor + `tracks` + `pin_proxy` + socket IPC, **a mano**; `play_channel` para el proxy **en otro sitio** (línea 3924, dentro del `except`) | PLAY-003 |
| FR-PLAY-008 idempotente/tolerante | **P** | `stop_tracks` es idempotente por `try/except`, pero **no acumula errores** y cada bloque borra la referencia sin registrar fallos | PLAY-003 |
| FR-PLAY-009 motivo de fin | **A** | `exit_summary()` (línea 2773) compone texto libre a mano; no hay `end_reason`/`ended_at` | PLAY-001 |
| FR-PLAY-010 init parcial | **P** | `play_channel` para el proxy si `PlayerError` (línea 3923). Lo demás (IPC socket, sesión de pistas) **no** se limpian si el lanzamiento falla | PLAY-003 |
| FR-PLAY-011 registro temporal | **A** | Sólo `NowPlayingScreen._start_mono/_start_wall` (líneas 2663-2664), en memoria y no exportables | PLAY-001 |

### 2.2 P1.2 Detección de errores y recuperación

| Req | Estado | Evidencia | Gap a cerrar |
|-----|--------|-----------|--------------|
| FR-REC-001 niveles de evidencia | **A** | No existe `EvidenceLevel`. `supervisor.py:256` decide `PLAYING` con `poll() is None` | REC-001/002 |
| FR-REC-002 evidencia por motor | **A** | `mpv_ipc.MpvIpc` existe (`get_property`, `is_alive`) pero **nadie** lo consulta para saber si hay medios. VLC/mplayer no tienen adaptador de señal | REC-001 |
| FR-REC-003 modelo común | **P** | `player/errors.StreamErrorCode` tiene 14 códigos; faltan `DNS`, `HTTP_RATE_LIMIT`, `MANIFEST_INVALID`, `MEDIA_DEMUX`, `CODEC_UNSUPPORTED`, `TRACK_SELECTION`, `PLAYER_STARTUP`, `PLAYER_EXIT`, `TIMEOUT`, `CANCELLED` | REC-003 |
| FR-REC-004 error vs causa | **A** | `classify_returncode` (línea 193) devuelve `UNKNOWN` sin texto, pero **no** separa «error observado» de «causa inferida» en la estructura | REC-005 |
| FR-REC-005 reintentable | **A** | `ReconnectPolicy` no sabe qué error es; `_bucle` reintenta igual un 401 que un corte de red (5 veces) | REC-004 |
| FR-REC-006 backoff + jitter | **P** | `ReconnectPolicy.espera_para` (línea 150) tiene tabla y tope, **sin jitter** y sin `max_wait` explícito | REC-004 |
| FR-REC-007 `Retry-After` | **A** | `grep -rn "Retry-After" thetvview/` → 0 | REC-004 |
| FR-REC-008 reinicio de contadores | **A** | `self.intentos` sólo baja… nunca: se incrementa para siempre y no se reinicia tras estabilidade | REC-004 |
| FR-REC-009 supervisión única | **S** | Un solo `ReconnectSupervisor` creado en `screens.py:2693`. `grep -rn "ReconnectSupervisor(" thetvview/` → 1 sitio | — |
| FR-REC-010 registro de intentos | **A** | `supervisor.intentos` es un entero; no hay `AttemptRecord` | REC-005 |

### 2.3 P1.3 Compatibilidad de reproductores

| Req | Estado | Evidencia | Gap a cerrar |
|-----|--------|-----------|--------------|
| FR-CAP-001 dimensiones | **P** | `player/protocols.PlayerProtocolSupport.transports` (transportes) + `player/capabilities.PlayerTrackSupport` (audio/subtítulos/calidad/en caliente). Faltan **códecs** y **contenedores** como dimensión | CAP-001 |
| FR-CAP-002 nivel de confianza | **P** | Campo `medido: frozenset[str]` en `protocols.py:122` y booleans en `capabilities.py`. Es un `DETECTED` implícito, no `DECLARED/DETECTED/VERIFIED` | CAP-001 |
| FR-CAP-003 selección por requisitos | **S** | `player/router.select_backend` elige por capacidad (`protocols.supports`), luego preferencia, luego historial de fallos | — |
| FR-CAP-004 política sin datos | **S** | `router.NoCompatibleBackend` + `protocols.why_not()` dan el motivo | — |
| FR-CAP-005 argv sin shell | **S** | `player/core.command_for` devuelve lista y termina en `["--", url]` (líneas 461-472); `security.check` lo verifica por AST | — |
| FR-CAP-006 no cambiar de motor por culpa del proveedor | **S** | `_lanzar_con_router` (`screens.py:4022`) sólo prueba otro motor tras `PlayerError` de **lanzamiento**, no por error del canal | — |
| FR-CAP-007 caché de combinaciones | **A** | No existe. `grep -rn "cache" thetvview/player/` → sólo `router` de fallos, no de combinaciones | CAP-002 |
| FR-CAP-008 fixtures sintéticos | **P** | `tests/fixtures/streams/` con 9 manifiestos HLS/DASH. `verificado` se rellena a mano, sin test enlazado por fila | CAP-001 |

### 2.4 P1.4 HLS/DASH y pistas

| Req | Estado | Evidencia | Gap a cerrar |
|-----|--------|-----------|--------------|
| FR-STREAM-001 URI tras redirección | **P** | `streams/hls.py`, `streams/dash.py` resuelven relativas contra la URL base; **no** contra la URL final tras redirecciones (`safe_http` sí revalida el destino, pero no devuelve la URL efectiva al analizador) | STREAM-001 |
| FR-STREAM-002 anti-SSRF en referencias | **S** | `security/ssrf` + `safe_http` + `url_policy`; `test_stream_security.py` | — |
| FR-STREAM-003 tipos de error | **P** | `streams/diagnose.StepState` (OK/FAIL/SKIP/UNKNOWN) distingue; falta separar «redirección fallida» | STREAM-001 |
| FR-STREAM-004 límites de manifiesto | **P** | `security/limits.py` acota **bytes**; no hay tope explícito de **profundidad**, **nº de referencias** ni **tiempo** en el analizador | STREAM-001 |
| FR-TRACK-001..004 | **S** | `tracks/language.py`, `tracks/manager.py`, `player/track_args.py` + `tests/test_tracks_*.py` (idioma, preferencias, sustitución por política, lista blanca) | — |
| FR-CAP-008 DASH sin `Representation` manual | **N/A** | `plan` §8: ya se cumple — DASH no ofrece calidad | — |

### 2.5 P1.5 Robustez del parser M3U

| Req | Estado | Evidencia | Gap a cerrar |
|-----|--------|-----------|--------------|
| FR-M3U-001 codificación | **P** | `security/local_files.read_limited_text` y `m3u_parser._decompress_and_decode` (línea 345) usan `utf-8-sig` + `errors="replace"` **en silencio**: un `tvg-id` con byte inválido se convierte en U+FFFD y cambia la identidad | M3U-001 |
| FR-M3U-002 sintaxis | **P** | `_split_extinf` (línea 118) usa `rfind(",")`; hay tres parches *ad hoc* para `tvg-logo="tvg-logo="` (líneas 135-142) y un TODO declarado en el docstring | M3U-002 |
| FR-M3U-003 directivas | **P** | Comentarios desconocidos se ignoran en silencio (línea 239); duplicadas no tienen regla documentada. Los `attrs` desconocidos **sí** se conservan (`Channel.attrs`) | M3U-002 |
| FR-M3U-004 validación | **P** | `max_entries` (línea 201) y `max_file_bytes`; falta validar longitud de línea y tipo antes de construir el `Channel` | M3U-001/002 |
| FR-M3U-005 recuperación | **S** | `parse_text` no lanza por contenido; `parse_file` sólo por E/S. Cubierto por `tests/test_m3u_parser.py` (17 tests) | — |
| FR-M3U-006 límites | **P** | `security/limits.py`: `max_entries`, `max_file_bytes`, `gunzip_limited`. Falta `max_line_length` | M3U-002 |
| FR-M3U-007 rendimiento | **S** | Medido arriba: 52 ms/1k constante en los tres tamaños → lineal | — |
| §8.3 fuzzing con semilla | **A** | `tests/test_m3u_fuzz.py` no existe | M3U-003/004 |

### 2.6 P1.6 Identidad y actualización de canales

| Req | Estado | Evidencia | Gap a cerrar |
|-----|--------|-----------|--------------|
| FR-CHAN-001 nombre no es identidad | **A** | — en el dominio. **Pero** `favorites.py:6` dice literalmente «La identidad de un favorito es su `url`» | CHAN-001/002 |
| FR-CHAN-002 priorizar `tvg-id` | **A** | `Channel.tvg_id` existe (`models.py:19`) y `epg_match.EpgIndex` lo usa, pero **favoritos/recientes no** | CHAN-001 |
| FR-CHAN-003 señales secundarias | **A** | No hay reconciliación entre versiones de lista | CHAN-001 |
| FR-CHAN-004 normalización conservadora | **A** | No existe normalizador de nombres de canal (el de `epg_match.normalize_key` es para EPG, no para identidad) | CHAN-001 |
| FR-CHAN-005 ambigüedad → separar | **A** | No aplica hoy porque no se fusiona nada… por la razón equivocada: no se reconcilia | CHAN-001 |
| FR-CHAN-006 validar antes de sustituir | **A** | `m3u_parser.load_url` (línea 413) escribe la cache **antes** de parsear y no valida integridad: una respuesta vacía sí replaces el archivo de cache | CHAN-003 |
| FR-CHAN-007 lista inválida no reemplaza | **A** | `load_url` tiene *fallback stale* a la cache anterior, pero sólo si la descarga **falla**; si la descarga **devuelve basura**, se cachea la basura | CHAN-003 |
| FR-CHAN-008 resumen de cambios | **A** | No existe | CHAN-004 |
| FR-CHAN-009 `ETag`/`Last-Modified` | **A** | `grep -rn "ETag\|Last-Modified\|If-None-Match" thetvview/ tests/` → **0 resultados** | CHAN-004 |
| FR-CHAN-010 commit atómico | **P** | `storage/json_store.atomic_write_json` (`os.replace` + `fsync`) es sólido para JSON; la cache M3U usa `tmp.replace()` sin `validate` ni copia previa | CHAN-002/003 |

### 2.7 P1.7 Precisión y rendimiento XMLTV/EPG

| Req | Estado | Evidencia | Gap a cerrar |
|-----|--------|-----------|--------------|
| FR-EPG-001 `tvg-id` primero | **S** | `epg_match.EpgIndex.resolve` (línea 78): `tvg_id` directo | — |
| FR-EPG-002 alias + nombre normalizado | **P** | `normalize_key` (línea 33) es conservador (`casefold` + espacios), **pero no hay alias** (`<display-name>` alternativos, `tvg-name`) | EPG-001/002 |
| FR-EPG-003 ambigüedad / huérfanos | **A** | `EpgIndex._por_nombre` guarda una **lista** de candidatos y `resolve` devuelve `[0]` sin registrar nada (línea 87) | EPG-001 |
| FR-EPG-004 método de correspondencia | **A** | `resolve` devuelve `str \| None`: no dice **por qué** casó | EPG-001 |
| FR-EPG-005 instantes coherentes | **P** | `epg_parser.parse_text` mezcla `tz`-aware del feed con UTC asumido; la comparación funciona pero el origen se pierde | EPG-001 |
| FR-EPG-006 sin zona ≠ UTC | **A** | `epg_parser._parse_ts` (línea 56) hace `.replace(tzinfo=timezone.utc)` **en silencio** | EPG-001 |
| FR-EPG-007 intervalos | **P** | Se rellena `stop` con el siguiente `start` (línea 159); **no** hay prueba de solapes, duración cero ni cruce de medianoche más allá de lo existente (`test_epg_parser.py:89` cubre un caso) | EPG-001 |
| FR-EPG-008 consultas indexadas | **A** | `Epg.now_playing` (línea 79) y `next_programme` (línea 85) recorren la lista del canal | EPG-002 |
| FR-EPG-009 validar antes de sustituir | **A** | `app._drain_tasks` (línea 1726) asigna `self.epg = salida.epg` sin validar nada: una guía vacía **sí** sustituye a la buena | EPG-003 |
| FR-EPG-010 conservar la última válida | **A** | `_epg_load_worker` (línea 1663) devuelve la primera que sirve; si todas fallan, `self.epg` se queda… **pero** una que "sirve" vacía ya la sustituyó | EPG-003 |
| FR-EPG-011 límites de recursos | **S** | `security/limits` + `gunzip_limited` + `parse_xml` con tope de nodos | — |
| FR-EPG-012 O(log n) | **A** | Ídem FR-EPG-008 | EPG-002 |

### 2.8 P1.8 Salud de canales y diagnóstico

| Req | Estado | Evidencia | Gap a cerrar |
|-----|--------|-----------|--------------|
| FR-DIAG-001 límites de las comprobaciones | **P** | `channel_health.probe_channel_url` (línea 229) tiene timeout y `read_body=False`; **no** hay límite de frecuencia (el monitor usa `interval=3.0` por defecto, sí lo hay, pero no por comprobación) | DIAG-003 |
| FR-DIAG-002 método más barato | **S** | HEAD → GET con `Range: bytes=0-1` (líneas 276-289), `read_body=False` | — |
| FR-DIAG-003 validar content-type | **P** | Se mira el código HTTP; el MIME sólo lo lee `streams/diagnose` (modal), no el monitor de salud | DIAG-003 |
| FR-DIAG-004 no interferir | **S** | `retries=0`, cuerpo no leído, `deque` acotado | — |
| FR-DIAG-005 fallo nuevo vs recurrente | **A** | `ChannelHealthMonitor` guarda `deque` de snapshots **sólo en memoria** y sin persistencia | DIAG-004 |
| FR-DIAG-006 `correlation_id` | **A** | `grep -rn "correlation_id" thetvview/` → 0 | DIAG-001 |
| FR-DIAG-007 informe exportable | **P** | `streams/diagnose.render_report` produce texto para modal; **no** se exporta a archivo | DIAG-002 |
| FR-DIAG-008 redacción | **S** | `security/redaction.py` (268 líneas) redacta en el punto donde nace el texto; `diagnose.Step.add` redacta (línea 118) | — |
| FR-DIAG-009 informe sin red | **S** | `diagnose()` es puro; `diagnose_connection()` es el botón explícito | — |
| FR-DIAG-010 historial acotado | **P** | `deque` en memoria; no hay `data/diagnostics.json` ni límites por edad/tamaño | DIAG-004 |
| §11.1 tres dimensiones | **A** | `classify_health` (línea 320) colapsa red + fuente + reproducción en **un** nivel 0..5 | DIAG-003 |

### 2.9 Requisitos no funcionales

| NFR | Estado | Evidencia |
|-----|--------|-----------|
| NFR-001 compatibilidad | **S** | `platform_check.py`, `os.name != "posix"` en `json_store._fsync_dir`, named pipes en `mpv_ipc._connect_pipe` |
| NFR-002 dependencias | **S** | `requirements.txt` vacío; `security.check` → «Zero pip dependencies» |
| NFR-003 determinismo | **P** | Todo es determinista salvo `EpgIndex.resolve` con nombres duplicados (devuelve `[0]`, estable por orden de inserción: determinista, sí) |
| NFR-004 resiliencia | **P** | Los hilos son daemon y capturan; falta que una EPG mala no destruya la buena (EPG-003) |
| NFR-005 rendimiento | **S** (con medición) | §1.2 y §1.3 de este documento |
| NFR-006 memoria | **S** | `limits.py`, `MAX_JSON_BYTES`, `deque(maxlen=…)` |
| NFR-007 observabilidad local | **P** | Diagnóstico local sí; sin eventos estructurados ni `correlation_id` |
| NFR-008 sin duplicar políticas | **S** (hoy) | Una sola `redaction.py`, una sola `ReconnectPolicy`. **Peligro**: añadir `RetryPolicy` sin envolver `ReconnectPolicy` crearía dos. Por eso REC-004 **envuelve**, no sustituye |
| NFR-009 interfaz | **S** | `ui/errormsg.UiError` + modales; `_avisos_sin_repetir` |
| NFR-010 retrocompatibilidad | **S** | `storage/migrations.py`, lectura dual de Xtream, `MAX_BACKUPS=1` |

### 2.10 Resumen cuantitativo

| Bloque | S | P | A | N/A |
|--------|---|---|---|-----|
| P1.1 (11) | 0 | 5 | 6 | 0 |
| P1.2 (10) | 1 | 2 | 7 | 0 |
| P1.3 (8) | 4 | 2 | 1 | 1 |
| P1.4 (10) | 5 | 4 | 0 | 1 |
| P1.5 (7) | 2 | 4 | 1 | 0 |
| P1.6 (10) | 0 | 1 | 9 | 0 |
| P1.7 (12) | 3 | 4 | 5 | 0 |
| P1.8 (10) | 4 | 5 | 1 | 0 |
| NFR (10) | 7 | 3 | 0 | 0 |
| **Total (78)** | **26** | **30** | **29** | **2** |

---

## 3. Hallazgos priorizados

Orden **por daño al usuario**, no por el orden del §15 del SDD:

1. **«Reproduciendo» sin evidencia.** `poll() is None` prueba que el proceso
   existe, no que haya imagen. Un canal que abre y se cuelga se presenta como
   sano, y el usuario no tiene forma de saber que no ve nada.
   *Impacto*: Engaño de la UI en el flujo principal. → Fase 1+2.
2. **Un 401 se reintenta cinco veces.** `ReconnectPolicy` no sabe qué error
   lleva: un canal con credenciales caducadas hace al usuario esperar ~18 s y ver
   «reconectando» cinco veces antes de un mensaje que dice «caducó».
   *Impacto*: tiempo y confianza. → Fase 2.
3. **Perder favoritos al cambiar la URL.** La identidad **es** la URL
   (`favorites.py:6`). Un proveedor que rota el token, o simplemente actualiza su
   `get.php`, deja al usuario con favoritos inservibles y sin aviso.
   *Impacto*: pérdida de datos del usuario. → Fase 3.
4. **Una EPG vacía sustituye a la buena.** `app._drain_tasks` asigna sin
   validar. Una descarga truncada deja la pantalla de Favoritos sin programas y
   el usuario no sabe si es un fallo o es que no hay guía.
   *Impacto*: pérdida de información + diagnóstico imposible. → Fase 4.
5. **Sin `correlation_id`.** Cuando algo falla en un hilo, nadie puede unir la
   línea de log con la sesión. Con `TaskManager` el resultado obsoleto se
   descarta sin dejar rastro de **por qué**.
   *Impacto*: diagnóstico opaco. → Fase 1 (mínimo) y Fase 5 (completo).

---

## 4. AUD-002 — Caracterización

`tests/test_playback_characterization.py` — **18 pruebas, todas verdes** sobre el
código sin tocar. Congelan:

| Caso | Test | Comportamiento congelado |
|------|------|--------------------------|
| `play_channel` completo | `TestPlayChannelConPopenDoblado` (3) | Empuja `NowPlayingScreen`, `argv` es lista sin `shell`, termina en `["--", url]`, anota Recientes; sin reproductor → modal y **no** pantalla a medias |
| Muerte inmediata | `TestMuerteInmediata` (5) | Texto literal de `explain_early_exit`, `exit_summary()` lo incluye; `None`/código 0/duración larga → sin mensaje |
| `detach()` en la espera | `TestDetachDuranteLaEspera` (2) | Tras el primer reintento, `detach()` impide el segundo y el hilo muere; **no** mata el proceso |
| Proxy vivo al cerrar | `TestCierreDeLaPantalla.test_el_proxy_de_pinning_vivo_se_para_al_cerrar` | `stop_tracks` llama `proxy.stop()` **una** vez y suelta la referencia |
| IPC abierto y cerrado | `TestCierreDeLaPantalla.test_el_socket_de_control_se_borra_al_cerrar` | El fichero del socket se borra; uno que ya no existe no revienta |
| Idempotencia | `TestCierreDeLaPantalla` (3) | `stop_tracks()` dos veces = una sola liberación; sin proxy ni pistas tampoco falla |
| Aislamiento | `TestAislamientoPorGeneracion` (2) | El resultado obsoleto **aparece** con `obsolete=True` y `ok=False`; el vigente es publicable |

### 4.1 Bugs preexistentes encontrados al escribir la caracterización

**Ninguno**: las 18 pruebas pasaron a la primera contra el código sin modificar.
Queda un matiz registrado como **hallazgo de auditoría**, no como bug:

- `TestCierreDeLaPantalla.test_el_socket_de_control_se_borra_al_cerrar` depende
  de que `NowPlayingScreen`Infiera el `ipc_path` de `self.tracks`
  (`screens.py:2866`). Si mañana se abre el IPC **sin** sesión de pistas —algo que
  `play_channel` ya permite por código (`screens.py:3881` sólo abre el socket si
  `track_session is not None`)— el socket **no** se borraría. Se arregla en
  PLAY-003, que centraliza la propiedad del recurso en la sesión.

---

## 5. Riesgos reales y dependencias

| Riesgo | Dependencia | Mitigación en el plan |
|--------|-------------|----------------------|
| **Ruptura de `NowPlayingScreen`** (4 155 líneas en `screens.py`) | PLAY-003/PLAY-004 | Caracterización primero (§4) + parámetro opcional con comportamiento idéntico por defecto |
| **Regresión silenciosa en favoritos** al migrar a `tvg-id` | CHAN-002 | Lectura dual + `.bak` + validación del store antes del commit + los favoritos sin `tvg-id` no se tocan |
| **Falsos positivos de evidencia en mpv** | REC-001 | `time-pos` debe **avanzar**, no sólo existir. Sin avance comprobable → `UNKNOWN`, nunca `STABLE` |
| **Equivalencia del índice EPG mal resuelta** | EPG-002 | Test que compara **exactamente** contra el escaneo lineal sobre las mismas fixtures antes de eliminar el anterior |
| **Dos políticas de reintento** | REC-004 vs `ReconnectPolicy` | `RetryPolicy` **envuelve** a `ReconnectPolicy`; `ReconnectPolicy.espera_para()` se conserva y sus tests no se tocan |
| **`tests/test_stream_supervisor.py` roto** | PLAY-001/002/004 | `ReconnectSupervisor` acepta `session: PlaybackSession | None`; **sin sesión su comportamiento es byte a byte el actual** |
| **`classify_health(player_alive=...)` en 2 sitios** | REC-002 | El parámetro nuevo **acepta también `bool`** (compatibilidad) y las pruebas se actualizan al criterio nuevo, no se saltan |

---

## 6. Secuencing revisada (coincide con el plan §5)

```text
Fase 0  AUD-001, AUD-002                            ← este documento + 18 tests
Fase 1  P1.1  PLAY-001…004   (+ DIAG-001 mínimo)
Fase 2  P1.2  REC-001…005
Fase 3  P1.6  CHAN-001…004
        ══ AVISO DE CORTE ══
Fase 4  P1.7  EPG-001…004
Fase 5  P1.3  CAP-001, CAP-002 · STREAM-001 · DIAG-001…004
Fase 6  P1.5  M3U-001…004
```

Tres desviaciones respecto del §15 del SDD, ya argumentadas en el plan §5:

1. **P1.4 sale de la lista de trabajo** (sólo queda pulido → Fase 5).
2. **EPG sube de la Fase 5 a la 4**: una EPG vacía que sustituye a la buena es
   pérdida de datos; un informe de diagnóstico ausente es una molestia.
3. **Diagnóstico se parte en dos**: el mínimo (eventos + redacción) se
   instrumenta en la Fase 1 porque los tickets de sesión necesitan poder explicar
   sus decisiones.

---

## 7. Cómo reproducir estas mediciones

```bash
python -m unittest discover -q          # 2390 tests, ~116 s
python -m thetvview.security.check      # 15/15, exit 0
python -m tests.bench.medir             # parseo/escritura/búsqueda
python -m unittest tests.test_playback_characterization -v
```

Las mediciones de §1.3 son un script de una sola línea sobre `Epg` construido en
memoria; el método está descrito en la tabla y es reproducible con el mismo
`generar_m3u`-style de `tests/bench/medir.py`, sin datos reales ni red.