# Auditoría de brechas P2 — theTVVIEW

**Documento:** `docs/sdd/p2-gap-analysis.md`
**Especificación:** `SDD/SDD_Mejoras_Prioridad_Media_theTVVIEW.md` v1.0 (§5–§12, §21)
**Plan de ejecución:** `.opencode/plans/plan-mejoras-prioridad-media.md`
**Fecha de medición:** 2026-10-13
**Alcance:** Fase 0 — AUD-01 (matriz de brechas), AUD-02 (caracterización),
AUD-03 (línea base), AUD-04 (preguntas del §21).

> Este documento **no** toca `thetvview/`. Describe lo que hay, con archivo,
> clase, función y test como evidencia. Toda fila es verificable con `grep`.

---

## 1. Línea base medida

### 1.1 Suite completa y control de seguridad

```text
$ python -m unittest discover -q          # antes de la Fase 0
Ran 2855 tests in 152.308s
OK (skipped=1)

$ python -m unittest discover -q          # después de AUD-02 (+56)
Ran 2911 tests in 157.844s
OK (skipped=1)

$ python -m thetvview.security.check
SECURITY CHECK PASSED (15/15)     # exit 0
```

- 2 911 tests tras la Fase 0 (2 855 antes), 0 fallos, 1 skip (binario externo
  ausente, con razón declarada).
- 95 módulos en `thetvview/` (41 171 LOC), 111 ficheros `tests/test_*.py`.
- Python 3.13.5 · Linux 6.18 · glibc 2.41.
- Los 15 controles activos: `secret_redaction`, `tls_verification`, `ssrf_policy`,
  `redirect_policy`, `response_limits`, `xml_safe_parsing`,
  `ai_secret_sanitizer`, `shell_safe_player`, `opaque_refs_credentials`,
  `credential_storage`, `no_password_logs`, `stdlib_dependencies`,
  `catchup_capability_gate`, `track_discovery_is_sandboxed`, `persistence_atomic`
  (`thetvview/security/check.py:1301`).

### 1.2 Filtro por tecla — `ChannelsScreen._apply_filter`

Catálogo sintético (4 grupos, nombres `Canal <i> HD`), medido con
`time.perf_counter` sobre la clase real. «1ª tecla» paga las cachés de etiquetas
y de textos; «tecla caliente» es el coste de teclear sobre un índice ya
construido.

| entradas | 1ª tecla (ms) | tecla caliente (ms) | sin coincidencia (ms) |
|---------:|---------------:|--------------------:|----------------------:|
|  10 000  |  8,4 |  3,8 |  1,9 |
|  50 000  | 43,0 | 11,8 |  8,5 |
| 100 000  | 81,3 | 41,4 | 16,6 |

**Lectura honesta:** el filtro es **lineal** y **ya está cacheado**. La ruta
actual cuesta ~0,4 ms por cada 1 000 canales y por tecla; el techo medido
(100 k, tecla caliente) es 41 ms — por debajo de lo que se percibe como un
parpadeo en una TUI, pero ya no despreciable. No hay trabajo por pulsación que
no sea el recorrido: las cachés `_labels` y `_search_texts` se invalidan **por
identidad de la lista** (`screens.py:720-725`, `:746-747`), así que refiltrar no
las reconstruye. Esto fija el punto de partida de S-06/S-08: cualquier filtro
nuevo debe mantener este perfil y, si lo mejora, sólo con índices construidos
en carga.

### 1.3 `render_source_template` y ciclo de vida de sesión

| operación | coste medido |
|-----------|-------------:|
| `catchup.render_source_template` (plantilla real, 4 parámetros) | ~40 µs por render |
| Ciclo completo `PlaybackSession` (crear → start → intento → launched → playing → first_media → end) | ~113 µs |

El segundo número es el coste de **libro de la sesión** sin lanzar ningún
proceso: es el presupuesto que H-01 tendrá que respetar si decide emitir un
evento por transición (hoy `session_id` y `correlation_id` son dos `uuid4` y cada
marca temporal es un `datetime.now(timezone.utc)`).

### 1.4 Señal de «reproduciendo» por motor (medido en el plan P1, vigente)

| Motor | Señal concluyente | Fuente |
|-------|-------------------|--------|
| mpv   | IPC: `time-pos`, `track-list`, `idle-active` | `player/evidence.py` `MpvSignalSource` |
| VLC   | ninguna → `EvidenceLevel.UNKNOWN` (`UnknownSignalSource`) | `evidence.py:190` |
| mplayer | ninguna → `EvidenceLevel.UNKNOWN` | `evidence.py:190` |

**Consecuencia para P2.4:** un `playback_started` de VLC o mplayer sólo puede
registrarse como **incierto**, nunca como confirmado.

---

## 2. Matriz de brechas

Leyenda: **S** = `SATISFECHO` · **P** = `PARCIAL` · **A** = `AUSENTE` ·
**N/A** = `NO_APLICA`.

### 2.1 Requisitos transversales (§5)

| Req | Estado | Evidencia (archivo:símbolo / test) | Gap |
|-----|--------|------------------------------------|-----|
| REQ-X-001 compatibilidad de datos | **P** | `storage/json_store.atomic_write_json` (`os.replace` + `fsync` de directorio en POSIX), `make_backup`, `read_json_with_recovery`; `storage/migrations.py` con `needs_migration`/`migrate_data` idempotentes; `storage/schemas.py:41` `SCHEMA_VERSION=1`. `favorites.py:129` hace `.bak` antes de escribir. **Pero** `diagnostics/history.py:49` `_VERSION = 1` se escribe y **no se lee** (sin migración) y no hace copia previa | H-03 |
| REQ-X-002 secretos | **S** | `security/redaction.py` (`redact_text`, `redact_mapping`) aplicado **al escribir**: `diagnostics/history.py:141,143` y `diagnostics/events.py:188`. Centinelas cubiertos por `tests/test_diagnostics_history.py` y `tests/test_diagnostics_events.py`. `security/check.py` verifica redacción, almacenamiento y logs | — |
| REQ-X-003 estado desconocido | **P** | `streams/diagnose.StepState` distingue `UNKNOWN`; `player/evidence.EvidenceLevel.UNKNOWN`; `epg_match.Match.ambiguo`. **Pero** no hay un `epg_status`/`catchup_status` de tres valores para el usuario: `catchup.classify` sólo devuelve 3 estados y el estado «EPG no cargada» se trata como ausencia en la UI | S-03 |
| REQ-X-004 trabajo no bloqueante | **S** | `tasks/manager.py` `TaskManager` (`submit`, `drain`, `cancel`, `generacion`, `is_current`) usado por carga de EPG, salud y análisis; `platform_check.py` para portabilidad | — |
| REQ-X-005 regresión automatizada | **P** | Comando único (`python -m unittest discover`), 2 911 pruebas, `security/check` como control estático. **Desviación declarada:** el SDD pide CI en la nube; la regla del repo lo prohíbe (`AGENTS.md`, `tests/test_sin_ci.py`), así que la verificación es **local y obligatoria antes de fusionar**. No es un gap: es una restricción del proyecto | — |

### 2.2 P2.1 Búsqueda avanzada (§6)

| Req | Estado | Evidencia | Gap |
|-----|--------|-----------|-----|
| REQ-SRCH-001 búsqueda básica | **P** | `screens.py:748-750` indexa `name + "\n" + group` con `casefold()`; mayúsculas cubiertas (`tests/test_busqueda_contador_resaltado.py:345`). **Acentos y espacios repetidos no se normalizan** (no hay `unicodedata` en la ruta del filtro) → congelado en `tests/test_busqueda_caracterizacion.py::TestUnicodeYAcentos`. Orden estable: sí, es el del playlist | S-01/S-09 |
| REQ-SRCH-002 filtros combinados | **A** | Sólo hay `query` (texto) y `group_filtered` (`screens.py:651`), con **AND** implícito y sin OR. No hay modelo de consulta: `grep -rn "class Consulta\|class Consultador" thetvview/` → 0 | S-01/S-02 |
| REQ-SRCH-003 identificadores y fuentes | **A** | `tvg_id` existe en `models.py:19` pero **no** entra en el índice de búsqueda (`screens.py:748-750`); `source_id` existe como `@property` en `playlist_manager.PlaylistEntry:84` y como campo en `channel_identity.ChannelIdentity:241`, pero **`Channel` no lo tiene** y la UI no lo ve (`grep -rn "source_id" thetvview/ui/` → 0). Además `favorites.py:268` llama `channel_identity(c)` sin `source_id`, así que siempre es `""` | S-02/A-01 |
| REQ-SRCH-004 EPG | **P** | `epg_match.EpgIndex.resolve` (tvg-id → alias → nombre normalizado) y `Epg.now_playing`/`next_programme` con `bisect_right` (`epg_parser.py:282,329`). **No hay** estado de tres valores ni búsqueda por título ni por ventana: `grep -rn "next_24h\|por_titulo" thetvview/` → 0. El panel de detalle hace escaneo lineal en vez de usar `now_playing` (`screens.py:1117-1120`) | S-03/S-05 |
| REQ-SRCH-005 catch-up | **P** | `catchup.capability_for` + `can_use_catchup` (invariante única, `catchup.py:318`) y `classify` (`catchup.py:382`); control de seguridad `catchup_capability_gate`. **No hay** `catchup_status` con `unknown`, y la capacidad se **re-deriva por fila y por frame** en `screens.py:3455` | S-03 |
| REQ-SRCH-006 calidad | **P** | `resolutions.detect` prioriza `res=`/`quality=`, luego infiere del nombre y del sufijo `@HD` del `tvg-id`; `detect()` devuelve `None` (desconocido) y `quality_rank()` `0.0`. Cubierto por `tests/test_resolutions.py`. **Pero** la calidad no se puede filtrar y `resolutions` no se usa desde la búsqueda | S-04 |
| REQ-SRCH-007 índices | **P** | Cachés de etiquetas y de texto por identidad de lista (`screens.py:714-753`); `Epg.channels_by_id`, `aliases_by_id`, `starts_by_channel`, `stops_max_by_channel` (`epg_parser.py:214-232`); `EpgIndex._por_nombre`. **No hay** índices por `tvg_id`, por fuente, por grupo, ni por título de programa | S-06 |
| REQ-SRCH-008 actualización e invalidación | **P** | `refresh_from_playlist` (`screens.py:840`) re-aplica el filtro y conserva query y selección por identidad. La invalidación es **por identidad del objeto `list`**, no por versión de datos: una playlist mutada in-place reutilizaría cachés obsoletas. No hay reconstrucción parcial ni modal de fallo | S-07 |
| REQ-SRCH-009 rendimiento | **P** | `tests/bench/medir.py` mide parseo, escritura, **una** búsqueda lineal sintética y EPG. **No mide la ruta real del filtro** (§1.2), ni `resolutions`, ni `epg_match`, ni el resaltado | S-08 |

### 2.3 P2.2 Colecciones inteligentes (§7)

| Req | Estado | Evidencia | Gap |
|-----|--------|-----------|-----|
| REQ-COLL-001 CRUD | **A** | `grep -rniE "coleccion\|collection" thetvview/ --include=*.py` → sólo `collections.abc/deque/Counter`. Lo más cercano: `favorites.py` y `recents.py`, que no son colecciones | C-01 |
| REQ-COLL-002 evaluación determinista | **A** | No hay gramática, ni evaluador, ni predicados | C-02/C-03 |
| REQ-COLL-003 recalcular | **A** | No existe bus de invalidación de dominio; sólo `TaskManager.generacion` | C-05 |
| REQ-COLL-004 sin duplicación | **A** | — | C-04 |
| REQ-COLL-005 historial y salud | **A** con dato parcial | Hay datos: `diagnostics/history.py` (500 entradas / 30 días) y `channel_health.py` (deque en memoria, **sin persistencia**). Falta la ventana declarada de 7 días (H-06) | C-06/H-06 |
| REQ-COLL-006 persistencia | **A** | El patrón existe y se debe reutilizar: `storage/schemas.py` (`SCHEMA_VERSION`, `get_validator`) y `storage/migrations.py` (`migrate_data`) | C-01/C-04 |

### 2.4 P2.3 Agrupación de fuentes alternativas (§8)

| Req | Estado | Evidencia | Gap |
|-----|--------|-----------|-----|
| REQ-SRC-001 candidatas | **P** | `channel_identity.normalize_name` (`channel_identity.py:134`), `normalize_url` (`:158`), `ChannelIdentity` con `IdentityKind` y `Confidence` (`:98,106`), `reconcile` (`:498`) y `ReconcileReport` con ambigüedades (`:400`). Pero `reconcile` compara **versiones de la misma lista**, no canales de **fuentes distintas**; y `channel_health.py` ya tiene noción de salud por URL, no por fuente | A-06 |
| REQ-SRC-002 confianza | **P** | `Confidence` (`LOW/MEDIUM/HIGH` → `valor()`) existe pero es **confianza de identidad**, no de emparejamiento entre fuentes, y no se acompaña de las señales que la produjeron | A-06 |
| REQ-SRC-003 confirmar/rechacer/deshacer | **A** | No hay modelo `Association`; `grep -rn "association\|candidata" thetvview/` → 0 | A-02/A-03 |
| REQ-SRC-004 fuente preferida | **A** | `player/success_cache.py` cachea **combinaciones de reproductor**, no fuentes; no hay «última fuente satisfactoria» con timestamp | H-07/A-02 |
| REQ-SRC-005 clasificación de errores | **S** (base) | `player/errors.StreamErrorCode` con 23 códigos, `classify_message`, `reintentable`, `NO_REINTENTABLE` (auth, url inválida, protocolo, reproductor ausente, cancelación), `RetryPolicy` con `Retry-After`. Es exactamente la tabla que necesita el failover | A-07 |
| REQ-SRC-006 límites | **A** | `RetryPolicy` acota **la misma URL** (`supervisor._relanzar`, `supervisor.py:712`): reconecta el mismo canal, nunca prueba otra fuente. No hay tope de fuentes, ni de tiempo total, ni «no repetir fuente» | A-08 |
| REQ-SRC-007 confirmación de reproducción | **S** (base P1) | `player/evidence.py` escalera completa + `UnknownSignalSource`; `player/session.py` `PlaybackSession` con `first_media_at`/`stable_playback_at`. Falta **persistirlo** (ver §2.5) | H-01 |
| REQ-SRC-008 preferencias | **P** | `tracks/prefs.py` recuerda calidad/idioma por canal y proveedor; `player/capabilities.py` declara lo que soporta cada motor. No hay reconciliación con las capacidades de la **fuente** elegida, ni aviso cuando una preferencia no está disponible | A-09 |
| REQ-SRC-009 privacidad | **S** | `validate_url(purpose="stream")` en la construcción de la URL de catch-up (`catchup.py:571`); referencias opacas `xtream://` / `xtream-ts://` sin credenciales; `security/check.py` lo verifica | — |

### 2.5 P2.4 Historial técnico de reproducción (§9)

**Hallazgo central de esta auditoría:** el subsistema existe y está probado, pero
**nadie lo alimenta durante la reproducción**.
`grep -rn "registrar(\|Event(\|\.emit(" thetvview/` → **1** escritura al historial
(`ui/app.py:2121`, desde el diagnóstico manual F5/H9), **0** `Event(...)` y
**0** `.emit(...)` en todo el paquete. `thetvview/player/` no importa
`thetvview.diagnostics` en ningún módulo.

| Req | Estado | Evidencia | Gap |
|-----|--------|-----------|-----|
| REQ-HIST-001 ciclo de vida | **A** | Los puntos de enganche existen y están identificados: `screens.py:4109-4110` (intento), `:2696-2698` (lanzado), `supervisor._nota()` (`:591-619`, ya centraliza `playing`/`nuevo_intento`/`note_stable`/`end`), `screens.py:4125` (fallo de arranque). Ninguno recibe bus ni historial | H-01 |
| REQ-HIST-002 errores normalizados | **S** (base) | `player/errors.py`: `StreamErrorCode` (23), `classify_message`, `StreamError` con `code` inferido y `observado` separado, `causa_conocida`, `confianza`. Cubierto por `tests/test_stream_errors.py` (21) y `tests/test_player_evidence.py` | H-02 |
| REQ-HIST-003 cambios de fuente y reproductor | **A** | `PlaybackSession` no registra cambios; `player/router.py` elige motor pero no avisa a nadie | H-01 |
| REQ-HIST-004 recurrencia | **P** | `historial.es_recurrente` (`history.py:156`) es un `any` **sin ventana propia** (sólo el recorte global de 30 días) y no distingue fallo de arranque de interrupción. `supervisor` ya sabe distinguirlo (`EndReason.INIT_FAILED` vs `PLAYBACK_ERROR`) | H-06 |
| REQ-HIST-005 persistencia local | **P** | `HistorialDiagnostico` con escritura atómica, `0600` y `threading.Lock`; una entrada ilegible **no** tumba el resto (`RegistroHistorial.de_dict` → `None`, filtrado en `_leer:214`). Es `_VERSION = 1` y **no se valida al leer** | H-03/H-08 |
| REQ-HIST-006 retención | **S** | `MAX_ENTRADAS=500`, `MAX_DIAS=30`, `MAX_BYTES=256 KB`; limpieza **al escribir** (`registrar` → `_purgar` → `_escribir`), documentada en el docstring. Falta poder **borrar desde la UI** | H-05/H-07 |
| REQ-HIST-007 privacidad | **S** | Redacción **al escribir** en `history.registrar` y en `EventBus.emit`; sin telemetría | H-04 |

Esquema actual de `RegistroHistorial`: `momento`, `canal`, `exito`, `motivo`,
`fase`. Faltan `source_id`, `player`, `error_code`, `recoverable`, `duration_ms`
(ticket H-03).

### 2.6 P2.5-A Catch-up robusto (§10)

| Req | Estado | Evidencia | Gap |
|-----|--------|-----------|-----|
| REQ-CU-001 intervalos | **P** — **con fallo real** | `CatchupRequest.duration_seconds` (`catchup.py:484-487`) es `max(1, round(delta))`: un intervalo **invertido** o de duración cero se normaliza a 1 s en silencio, se clasifica `AVAILABLE` y **construye la URL** (probado en `tests/test_catchup_caracterizacion.py::TestIntervaloInvertido`). No hay duración máxima: una petición de 400 días se acepta si la ventana alcanza | CU-02 |
| REQ-CU-002 zonas horarias | **P** | Todo el cálculo es **instante absoluto**: `_aware_seconds` pasa a UTC (`catchup.py:460`) y `template_values` (`catchup.py:743`) sólo produce epoch y un sello `strftime`. Bien para Xtream; **pero** la zona de entrada no está documentada por proveedor, no hay pruebas de DST (hora inexistente / repetida) y el sello `{utc}` usa hora local del proceso | CU-01/CU-05 |
| REQ-CU-003 plantillas | **P** | La lista blanca ya existe: `string.Formatter().parse` + `_TEMPLATE_PLACEHOLDERS`, rechazo de especificadores, de conversiones y de plantilla sin marcadores (`catchup.py:770-819`); `CatchupAdapter._validate_playable` pasa la URL por `validate_url(purpose="stream")` (`:571`), lo que ya rechaza `file://` y saltos de línea. Falta: límites por parámetro, rechazo explícito de `&`/`#` en marcadores y tope de longitud | CU-03 |
| REQ-CU-004 disponibilidad | **P** | `CatchupState` tiene 3 estados de proveedor (`DISABLED_BY_PROVIDER`, `AVAILABLE`, `OUTSIDE_ARCHIVE_WINDOW`) + 3 de sesión, y `describe_state` los explica. **Colapsa** lo que el SDD pide separar: contenido eliminado, autenticación fallida, fallo de red y desconocido; `CatchupState.ERROR` es único para todos | CU-04 |
| REQ-CU-005 compatibilidad | **S** | `can_use_catchup` es la invariante única, `capability_for` sólo lee metadatos declarados, y un canal sin catch-up conserva su directo. Verificado por `security/check.py::check_catchup_capability_gate` | — |

### 2.7 P2.5-B Grabación manual (§11)

| Req | Estado | Evidencia | Gap |
|-----|--------|-----------|-----|
| REQ-REC-001 validación previa | **A** | `grep -rniE "grabaci\|recording\|Recorder" thetvview/` → 0 hits reales | R-01/R-03 |
| REQ-REC-002 backend | **A** | `config.SUPPORTED_PLAYERS = ("mpv", "mplayer", "vlc")`; `player/core.command_for` da argv sin shell. Ninguno tiene captura uniforme; la decisión de producto es **sólo VLC**, y `config.find_player` ya sabe localizarlo en Linux, Windows y macOS (`config.py:122-176`) | R-01 |
| REQ-REC-003 supervisión | **A** | El patrón existe y se debe reutilizar: `player/supervisor.ReconnectSupervisor` + `player/session.PlaybackSession` (tiempos, motivos, `ResourceScope`) | R-04 |
| REQ-REC-004 parada y limpieza | **A** | — | R-05 |
| REQ-REC-005 archivos | **A** | Hay precedente: `storage/json_store.atomic_write_json` (tmp + `replace`) | R-06 |
| REQ-REC-006 almacenamiento | **P** (herramientas) | `security/limits.py` (`Limits`, `clamp_value`, `get_limits`) y `storage/cache_policy.py` (`espacio_usado`, `limpiar_cache`) ya miden y acotan disco; no hay política de retención de grabaciones | R-07 |
| REQ-REC-007 cierre de aplicación | **A** | El comportamiento está decidido (parada ordenada, 5 s, luego `incomplete`), no implementado | R-09 |
| REQ-REC-008 historial | **A** | Depende de H-01…H-04 | R-08 |

### 2.8 P2.5-C Planificador (§12)

| Req | Estado | Evidencia | Gap |
|-----|--------|-----------|-----|
| REQ-SCHED-001 persistencia | **A** | `grep -rniE "planificador\|scheduler" thetvview/` → 0 | P-01/P-02 |
| REQ-SCHED-002 tiempo | **A** | — | P-03 |
| REQ-SCHED-003 conflictos | **A** | `tasks/manager.py` tiene límites y cancelación reutilizables | P-04 |
| REQ-SCHED-004 recuperación | **A** | — | P-03 |
| REQ-SCHED-005 ciclo de vida | **A** | — | P-01 |
| REQ-SCHED-006 recursos | **A** | — | P-04/P-06 |

### 2.9 Rendimiento, concurrencia y recursos (§14)

| Req | Estado | Evidencia | Gap |
|-----|--------|-----------|-----|
| REQ-PERF-001 medición antes de fijar umbrales | **P** | `tests/bench/medir.py` con 500 / 10 k / 100 k y perfil EPG; §1.2 y §1.3 de este documento. **No mide la ruta real del filtro** | S-08 |
| REQ-PERF-002 sin red en consultas/render/historial | **S** | Filtro en memoria (probado con centinela en `tests/test_busqueda_caracterizacion.py::TestSinRed`), evaluación EPG local, historial en fichero local. La búsqueda **sí** consulta `streams/probe` desde otras pantallas, pero no desde la ruta de filtrado | — |
| REQ-PERF-003 todo proceso externo con timeout y limpieza | **P** | `player/core.py` y `player/supervisor.py` cubren arranque, parada, cancelación y cierre de descriptores del reproductor. **No hay** proceso externo de grabación | R-04 |
| REQ-PERF-004 tareas de fondo limitadas y sin duplicarse | **S** | `tasks/manager.TaskManager` con `generacion`, `is_current`, `active_for`, `cancel`, `drain` | — |
| REQ-PERF-005 invalidación explícita de índices | **P** | Las cachés del filtro se invalidan por identidad de lista; los índices EPG se construyen en `Epg.__post_init__`. **No hay** política centralizada ni reconstrucción con retención del último índice válido | S-07 |

### 2.10 Resumen cuantitativo

| Bloque | S | P | A | N/A |
|--------|---|---|---|-----|
| Transversales §5 (5) | 2 | 3 | 0 | 0 |
| P2.1 Búsqueda (9) | 0 | 7 | 2 | 0 |
| P2.2 Colecciones (6) | 0 | 0 | 6 | 0 |
| P2.3 Fuentes (9) | 3 | 3 | 3 | 0 |
| P2.4 Historial (7) | 3 | 2 | 2 | 0 |
| P2.5-A Catch-up (5) | 1 | 4 | 0 | 0 |
| P2.5-B Grabación (8) | 0 | 1 | 7 | 0 |
| P2.5-C Planificador (6) | 0 | 0 | 6 | 0 |
| Rendimiento §14 (5) | 2 | 3 | 0 | 0 |
| **Total (60)** | **11** | **23** | **26** | **0** |

Los totales se obtuvieron contando programáticamente las filas de las §2.1 a
§2.9; el reparto coincide con el recuento directo de los 60 `REQ-*` de los §5 a
§14 del SDD.

---

## 3. Hallazgos priorizados

Orden **por daño al usuario**, no por el orden del §19 del SDD:

1. **Un intervalo de catch-up invertido se reproduce «bien».** `max(1, …)`
   convierte en 1 segundo lo que el usuario pidió al revés, lo clasifica como
   `AVAILABLE` y entrega la URL al reproductor sin decir nada. *Impacto*: el
   usuario ve un canal que no es el que pidió y cree que el archivo está mal.
   → **Fase 1 (CU-02)**.
2. **El historial técnico nunca ve una reproducción.** El bus de eventos se crea
   en `App.run()` y **nadie emite**; el historial sólo se escribe desde el
   diagnóstico manual. Toda recurrencia, el failover y las colecciones
   basadas en salud se apoyarán en datos que hoy no existen. *Impacto*: tres
   fases dependiendo de una base que no se llena. → **Fase 2 (H-01)**.
3. **La búsqueda no puede distinguir fuentes.** `source_id` existe en el
   catálogo y en las identidades, pero `Channel` no lo tiene y la UI no lo ve;
   además `favorites.py` lo pasa siempre vacío. Dos listas con el mismo nombre
   son indistinguibles para el usuario. *Impacto*: ninguna fuente «de
   comparación» se puede ofrecer con criterio. → **Fase 5A (A-01/A-02)**.
4. **Sin `catchup_status` ni `epg_status` de tres valores**, la Fase 3 nacería
   con un campo cuya semántica cambia después. *Impacto*: retrabajo y
   semántica equivocada en el filtro. → **Fase 1 antes que Fase 3**
   (secuencia ya fijada por el plan).
5. **El filtro no normaliza acentos ni espacios repetidos** y sólo indexa dos
   campos. Un usuario que escribe «noticias» no encuentra «Noticias» si el
   proveedor acentúa. *Impacto*: molestia diaria. → **Fase 3 (S-01)**,
   con la caracterización ya escrita.
6. **La invalidación de cachés es por identidad de objeto**, no por versión de
   los datos: una lista mutada in-place serviría texto de búsqueda obsoleto.
   *Impacto*: resultados incorrectos sin aviso. → **Fase 3 (S-07)**.
7. **Catch-up colapsa cuatro causas en un «error».** *Impacto*: el modal miente
   sobre el motivo y el usuario no sabe si reintentar, recargar credenciales o
   rendirse. → **Fase 1 (CU-04)**.
8. **Nada de grabación ni de planificador existe**, y es correcto: son las fases
   de mayor superficie y el plan ya las pone al final y tras compuerta.
   *Impacto*: ninguno hoy; riesgo de ejecución. → **Fases 6 y 7**.

---

## 4. AUD-02 — Caracterización

56 pruebas nuevas, todas verdes **contra el código sin tocar**. Congelan
comportamiento, no calidad.

### 4.1 `tests/test_busqueda_caracterizacion.py` — 27 pruebas

| Caso | Clase (nº) | Comportamiento congelado |
|------|-------------|--------------------------|
| Normalización | `TestUnicodeYAcentos` (7) | `casefold` en consulta y catálogo (2); «Tele» **no** encuentra «Télé»; la consulta es literal y no colapsa espacios; `ß`→`ss` casa igual sin crashear ni resaltar; emoji y nombre vacío |
| Campos indexados / no indexados | `TestCamposIndexados` (6) | sólo `name` y `group`; ni `tvg_id`, ni `attrs`, ni `url`, ni `tvg_name` |
| Orden, cachés, selección y resaltado | `TestOrdenEstableYCaches` (7) | orden del playlist sin ordenar; refiltrar no rehace etiquetas ni textos; `keep_selection` conserva identidad, sin él se conserva la **posición**; los rangos caen en la etiqueta visible con tope de 10 |
| Cambio de playlist | `TestCambioDePlaylist` (3) | la lista fresca invalida cachés, conserva la consulta y vacía bien |
| Volumen | `TestCienMilCanales` (3) | 100 k: filtro correcto, cachés no se rehacen, grupo + texto combinados |
| Sin red | `TestSinRed` (1) | filtrar no abre ninguna conexión (centinela `urlopen`/`socket`) |

### 4.2 `tests/test_catchup_caracterizacion.py` — 29 pruebas

| Caso | Clase (nº) | Comportamiento congelado |
|------|-------------|--------------------------|
| Intervalo invertido | `TestIntervaloInvertido` (6) | duración 1 s, `AVAILABLE`, URL construida, sin objeción; `describe_state` dice «sin cambios»; 400 días se aceptan si la ventana alcanza |
| Duraciones válidas | `TestDuraciones` (5) | redondeo al segundo, mínimo 1 s, naive ≡ aware, zona irrelevante |
| Ventana de archivo | `TestVentanaDeArchivo` (5) | dentro/fuera, sin declaración, fuente sin catch-up conserva el directo, no se adivina por el nombre |
| Plantillas | `TestPlantillaDeTerceros` (8) | los 4 marcadores; marcador desconocido, especificador, sin marcadores, vacía, `file://` y salto de línea se rechazan |
| Referencia opaca | `TestReferenciaOpaca` (5) | sin credenciales, ida y vuelta exacta, esquema desconocido no se parsea |

### 4.3 Hallazgos al escribir la caracterización

**Ningún test detectó un defecto del producto al escribirse**: los cuatro fallos
que aparecieron en la primera ejecución eran expectativas equivocadas (cuántos
canales casa con un prefijo, y el hecho de que `set_items(...,
keep_selection=True)` conserva la **posición** aunque no se pida
`keep_selection`). Quedan **documentados como comportamiento**, no como bug,
porque son decisiones defendibles del código actual.

Sí se confirmaron **cuatro defectos reales**, ya escritos como prueba:

1. `CatchupRequest.duration_seconds` normaliza en silencio el intervalo
   invertido y el de duración cero (`catchup.py:484`).
2. `build_playback_request` construye la URL de ese intervalo sin objeción
   (`catchup.py:694`).
3. `CatchupRef.to_opaque()` escribe `dur=-5` mientras `CatchupRef.parse()`
   acota a 5: la normalización es asimétrica (`catchup.py:856` vs `:896`).
4. `CatchupCapability` se re-deriva por fila y por frame en la pantalla de EPG
   (`screens.py:3455`), aunque ya esté cacheada en `__init__`.

Ninguno se corrige en la Fase 0: son de la Fase 1.

---

## 5. AUD-04 — Preguntas del §21 resueltas

| # | Pregunta | Respuesta con evidencia |
|---|----------|-------------------------|
| 1 | ¿Clave de identidad actual? | Tres criterios, en tres sitios distintos: **favoritos** = URL y luego `tvg-id` (sólo si el canal no es referencia opaca) en `favorites.is_favorite` (`favorites.py:222-245`); **recientes** = `tracks.prefs.channel_key` (tvg-id, o hash de la URL redactada) en `recents.push` (`recents.py:192-214`); **identidad de dominio** = `ChannelIdentity` con `IdentityKind` y `Confidence` más una clave estable (`channel_identity.py:263`). **Deuda:** `source_id` se pasa siempre vacío desde favoritos (`favorites.py:268`) | A-01 |
| 2 | ¿Formatos de persistencia y garantías? | JSON con `atomic_write_json` (tmp + `os.replace` + `fsync` de directorio sólo en POSIX), `make_backup`, `read_json_with_recovery` con copia `.bak`, validadores por fichero (`storage/schemas.py`) y migraciones idempotentes (`storage/migrations.py`). Excepción: `diagnostics/history.py` escribe su propia versión **sin leerla ni respaldar** | H-03 |
| 3 | ¿Señal de «reproduciendo» por motor? | Sólo **mpv con IPC** (`time-pos` avanzando, `track-list`, `idle-active`). VLC y mplayer devuelven `UnknownSignalSource` → `EvidenceLevel.UNKNOWN` → el resultado se registra como **incierto**, nunca como éxito | H-01 |
| 4 | ¿Taxonomía de errores y reintentos? | `player/errors.StreamErrorCode`, 23 códigos, con `classify_message`, `classify_returncode`, `reintentable` y `NO_REINTENTABLE`; `RetryPolicy` con backoff, tope y `Retry-After`. Separación observado/inferido ya en `StreamError`. **No se crea una taxonomía paralela** | H-02/A-07 |
| 5 | ¿Zona horaria y formato por proveedor catch-up? | **Internamente** todo es instante absoluto en UTC (`_aware_seconds`). **Falta lo importante**: no hay documento que diga qué zona y qué formato espera cada proveedor soportado → **ticket CU-01**, y es la razón de que CU-05 (DST) no se pueda escribir todavía | CU-01 |
| 6 | ¿Horizonte de búsqueda por título? | Decisión de producto ya tomada: `current`, `today`, `next_24h`; la ventana personalizada queda fuera | S-05 |
| 7 | ¿Campos de calidad fiables? | `resolutions.detect` con tres fuentes (`res=`/`quality=` declarados → nombre → sufijo del `tvg-id`) y `None` como desconocido. Se acepta la inferencia del nombre **etiquetada «estimada»** en la UI | S-04 |
| 8 | ¿Volumen a soportar? | Medido: 100 000 entradas ≈ 5,2 s de parseo y 86 MB de pico (`tests/bench/medir.py`); filtro caliente 41 ms por tecla (§1.2) | S-08 |
| 9 | ¿Backend de captura portable? | **Sólo VLC**, con gate `can_record()` al estilo de `can_use_catchup()`. `config.find_player` ya localiza VLC en las tres plataformas (`config.py:122-176`) | R-01 |
| 10 | ¿Qué pasa al cerrar con grabación activa? | Parada ordenada, espera máxima 5 s; si no responde, terminar el proceso y marcar `incomplete`. El modal dice qué pasó; nunca se afirma que sigue en segundo plano | R-09 |
| 11 | ¿Grabaciones simultáneas? | **Una**. Al pedir otra, modal con «detener la actual» o «cancelar» | R-10 |
| 12 | ¿Plataformas y reproductores validados en CI? | **No hay CI en la nube** (regla del repo, verificada por `tests/test_sin_ci.py`). Todo es local: `python -m unittest discover` y `python -m thetvview.security.check`. Las pruebas que exigen binario real se etiquetan y se documentan | REQ-X-005 |

Las 12 quedan resueltas. Ninguna se convierte en supuesto silencioso: las que
dependen de una decisión ya escrita (5) tienen ticket asignado.

---

## 6. Mapa de arquitectura y puntos de integración

Dónde toca cada fase, para no descubrirlo a mitad de la implementación:

| Fase | Ficheros que ya existen y **se reutilizan** | Ficheros nuevos |
|------|--------------------------------------------|-----------------|
| 1 Catch-up | `catchup.py`, `security/url_policy.py`, `security/limits.py`, `ui/errormsg.py` | `docs/sdd/catchup-temporal.md` |
| 2 Historial | `diagnostics/events.py`, `diagnostics/history.py`, `player/session.py`, `player/errors.py`, `player/evidence.py`, `security/redaction.py` | — (todo es cableado + esquema v2) |
| 3 Búsqueda | `ui/screens.ChannelsScreen`, `epg_parser.Epg`, `epg_match.EpgIndex`, `resolutions`, `tasks/manager.py`, `tests/bench/medir.py` | `thetvview/search.py` |
| 4 Colecciones | `storage/schemas.py`, `storage/migrations.py`, `favorites.py`, `recents.py`, `search.py` (de la fase 3) | `thetvview/collections/` |
| 5A/5B Fuentes | `channel_identity.py`, `playlist_manager.PlaylistEntry.source_id`, `favorites.py`, `recents.py`, `tracks/prefs.py`, `player/router.py`, `player/supervisor.py` | `LogicalChannel`/`SourceCandidate`/`Association` |
| 6 Grabación | `config.find_player`, `player/core.py` (argv sin shell), `player/session.py`, `storage/cache_policy.py` (espacio), `security/limits.py` | `thetvview/recording/` |
| 7 Planificador | `tasks/manager.py`, `storage/`, `player/session.py` | `thetvview/scheduler/` |

**Regla de no duplicación** (DoD global): cada fase debe poder responder «¿esto
ya existía?» con un `grep`. Los candidatos reales a duplicar, ya identificados:
una segunda taxonomía de errores (usar `player/errors.py`), un segundo gestor de
tareas (usar `tasks/manager.py`), un segundo motor de predicados (usar
`search.py` de la fase 3) y un segundo mecanismo de escritura atómica (usar
`storage/json_store.py`).

---

## 7. Riesgos reales y dependencias

| Riesgo | Dependencia | Mitigación ya decidida en el plan |
|--------|-------------|------------------------------------|
| **El historial se llena de humo**: si H-01 emite sin criterio, el bus crece con ruido y `es_recurrente` de 7 días produce falsos positivos | H-01/H-06 | Emitir **sólo** transiciones con evidencia (`player/evidence.py`); un solo error ambiguo no declara fuente caída |
| **Pérdida de favoritos al migrar a identidad lógica** | A-01…A-03 | Documento de identidad **antes** de código; lectura dual ya existente; `.bak` antes de escribir; test de migración con datos reales de la versión anterior |
| **Falsas equivalencias entre fuentes** (mismo nombre, distinto idioma) | A-06 | Nivel `low/medium/high` **con las señales**; sin fusión automática por nombre solo; probatorio de falsos positivos/negativos |
| **El filtro «estimado» de calidad contradice el REQ-SRCH-006** | S-04 | Discrepancia documentada (§3 del plan y aquí); la compensación es la **etiqueta «estimada»**, no la ausencia del dato |
| **Rechazar el intervalo inválido cambia comportamiento observable** | CU-02 | Caracterización escrita (§4.2) y modal que explica el motivo; ningún otro dato del usuario cambia |
| **VLC no detectable en alguna plataforma** | R-01 | Si el detector no es portable en las tres, la ejecución se detiene y se replantea; no se fuerza |
| **Rendimiento de la búsqueda al añadir índices** | S-06/S-08 | Umbrales **medidos** (§1.2), no inventados; benchmark antes/después en `tests/bench/` |
| **`tests/test_stream_supervisor` intermitente** (`test_rebinding_no_afecta_a_la_conexion` cuenta **todas** las resoluciones, incluidas las de otros hilos) | cualquiera | Ruido preexistente documentado en memoria del proyecto; si aparece, se identifica el módulo que deja el hilo vivo |

---

## 8. Secuencia confirmada

```text
Fase 0  AUD-01, AUD-02, AUD-03, AUD-04          ← este documento + 56 tests
Fase 1  P2.5-A  CU-01…CU-06                     (define catchup_status fiable)
Fase 2  P2.4    H-01…H-08                       (cablea lo que P1 construyó)
Fase 3  P2.1    S-01…S-09
Fase 4  P2.2    C-01…C-07
Fase 5A P2.3    A-01…A-05
Fase 5B P2.3    A-06…A-09
Fase 6  P2.5-B  R-01…R-10
Fase 7  P2.5-C  P-01…P-06
```

La secuencia del plan §5 se confirma con esta auditoría y **no se modifica**.
Se confirma además el orden interno de dos pares:

- **Fase 5A antes que 5B**: la migración de favoritos, recientes y preferencias
  es riesgo de datos; el failover automático es riesgo de red. No se mezclan.
- **Fase 6 después de la 2**: la grabación escribe en el historial técnico, y
  sin H-01…H-04 no habría a quién atribuir un fallo de captura.

---

## 9. Cómo reproducir estas mediciones

```bash
python -m unittest discover -q                                   # 2911 tests, ~158 s
python -m thetvview.security.check                               # 15/15, exit 0
python -m tests.bench.medir                                      # parseo / escritura / EPG
python -m unittest tests.test_busqueda_caracterizacion -v         # 27
python -m unittest tests.test_catchup_caracterizacion -v          # 29
```

Las mediciones de §1.2 y §1.3 salen del script del apéndice A: usa las clases
reales (`ChannelsScreen._apply_filter`, `catchup.render_source_template`,
`PlaybackSession`), `Popen` doblado, catálogo sintético y ninguna red.

---

## Apéndice A — Script de la línea base (AUD-03)

Guárdalo como `tests/bench/linea_base_p2.py` o pégalo en un archivo temporal;
no forma parte de la suite porque no es una aserción, es una medición. El
ticket S-08 lo incorporará a `tests/bench/medir.py` con umbrales medidos.

```python
"""Línea base AUD-03 del plan P2 (Fase 0). Medición, no aserción."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from thetvview.catchup import CatchupRequest, render_source_template
from thetvview.models import Channel, Playlist
from thetvview.player.session import EndReason, PlaybackSession
from thetvview.ui import screens as ui_screens


class _Favoritos:
    def is_favorite(self, channel):
        return False

    def favorite_urls(self):
        return set()


class _App:
    def __init__(self) -> None:
        self.favorites = _Favoritos()

    def body_height(self) -> int:
        return 20


def _canales(n: int) -> list[Channel]:
    grupos = ("Noticias", "Deportes", "Cine", "Infantil", "Documentales")
    return [
        Channel(
            name=f"Canal {i} HD",
            url=f"http://a.test/{i}",
            tvg_id=f"canal.{i}.es",
            group=grupos[i % len(grupos)],
        )
        for i in range(n)
    ]


def medir_filtro() -> list[tuple]:
    filas = []
    for n in (10_000, 50_000, 100_000):
        canales = _canales(n)
        pantalla = ui_screens.ChannelsScreen(
            _App(), Playlist(name="B", channels=canales)
        )
        pantalla.query = "canal 9999"          # 1ª tecla: paga las cachés
        t0 = time.perf_counter()
        pantalla._apply_filter()
        frio = time.perf_counter() - t0
        t0 = time.perf_counter()               # tecla caliente: sólo recorrido
        for q in ("canal 999", "canal 99", "canal 9", "canal 9", "canal 999"):
            pantalla.query = q
            pantalla._apply_filter()
        caliente = (time.perf_counter() - t0) / 5
        pantalla.query = "zzz"                 # peor caso: recorre todo
        t0 = time.perf_counter()
        pantalla._apply_filter()
        sin = time.perf_counter() - t0
        filas.append((n, frio, caliente, sin))
        del pantalla, canales
    return filas


def medir_render_source_template() -> float:
    inicio = datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)
    req = CatchupRequest(
        channel_id="101", start=inicio, end=inicio + timedelta(hours=2)
    )
    plantilla = (
        "http://a.test/streaming/timeshift.php?username=u&password=p"
        "&stream={start}&duration={duration}&start={utc}"
    )
    for _ in range(100):
        render_source_template(plantilla, req)
    t0 = time.perf_counter()
    n = 20_000
    for _ in range(n):
        render_source_template(plantilla, req)
    return (time.perf_counter() - t0) / n * 1e6


class _Proc:
    def __init__(self) -> None:
        self.pid = 4242

    def poll(self):
        return None

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout=None):
        return 0


def _ciclo(sesion: PlaybackSession) -> None:
    sesion.start()
    sesion.nuevo_intento()
    sesion.launched()
    sesion.playing()
    sesion.note_first_media()
    sesion.end(EndReason.USER_STOPPED)


def medir_sesion() -> float:
    n = 5_000
    parche = mock.patch(
        "thetvview.player.core.subprocess.Popen", return_value=_Proc()
    )
    parche.start()
    try:
        for _ in range(200):
            _ciclo(PlaybackSession(channel_key="c1", player_name="mpv"))
        t0 = time.perf_counter()
        for _ in range(n):
            _ciclo(PlaybackSession(channel_key="c1", player_name="mpv"))
        return (time.perf_counter() - t0) / n * 1e6
    finally:
        parche.stop()


def main() -> None:
    print("== Filtro por tecla (ChannelsScreen._apply_filter) ==")
    print(
        f"{'entradas':>10} {'1a tecla (ms)':>14} "
        f"{'tecla caliente (ms)':>20} {'sin coincidencia (ms)':>22}"
    )
    for n, frio, caliente, sin in medir_filtro():
        print(f"{n:>10} {frio*1e3:>14.2f} {caliente*1e3:>20.2f} {sin*1e3:>22.2f}")
    print(f"\n== render_source_template ==\n  {medir_render_source_template():.1f} us")
    print(f"== Ciclo de vida de sesion ==\n  {medir_sesion():.1f} us")


if __name__ == "__main__":
    main()
```

Salida esperada en esta máquina (Linux 6.18, Python 3.13.5): ver §1.2 y §1.3.
Los números varían con la máquina; lo que **no** debe variar es la forma: el
filtro es lineal en el número de entradas y las cachés no se reconstruyen al
refiltrar.