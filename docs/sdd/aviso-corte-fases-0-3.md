# Aviso de corte — Fases 0 a 3 del plan P1

**Plan:** `.opencode/plans/plan-mejoras-prioridad-alta.md`
**Corte:** entre la Fase 3 (P1.6 identidad) y la Fase 4 (P1.7 EPG)
**Fecha:** 2026-10-12

El plan pide este corte explícito: «Al terminar la Fase 3 se emite un informe
breve… A continuación se continúa sin esperar confirmación.» Se emite y se sigue.

---

## 1. Tickets cerrados

| Fase | Tickets | Estado |
|------|---------|--------|
| 0 | AUD-001, AUD-002 | ✅ |
| 1 | PLAY-001, PLAY-002, PLAY-003, PLAY-004 | ✅ |
| 2 | REC-001, REC-002, REC-003, REC-004, REC-005 | ✅ |
| 3 | CHAN-001, CHAN-002, CHAN-003, CHAN-004 | ✅ |

**13 de 25 tickets.** Quedan 12 para las Fases 4, 5 y 6.

---

## 2. Módulos tocados

### Nuevos

| Módulo | Qué es |
|--------|--------|
| `thetvview/player/session.py` | Ciclo de vida de sesión: estados, fases, marcas temporales, motivo de fin, tabla de transiciones, `ResourceScope`, `SessionRegistry` |
| `thetvview/player/evidence.py` | Escalera de evidencia de reproducción, `MpvSignalSource` sobre el IPC existente, `UnknownSignalSource` |
| `thetvview/channel_identity.py` | Identidad lógica del canal, normalización conservadora, `reconcile()` |

### Modificados

| Módulo | Cambio |
|--------|--------|
| `thetvview/player/supervisor.py` | `session`, `is_vigente`, `evidencia`, `clasificar_error` y `retry_policy` (todos opcionales); `RetryPolicy` con jitter, tope y `Retry-After`; `AttemptRecord` y estado observable de evidencia |
| `thetvview/player/errors.py` | 10 códigos nuevos (sin renombrar ni borrar); `StreamError` separa `observado` / `returncode` / `confianza`; descripciones y consejos nuevos |
| `thetvview/channel_health.py` | `classify_health` acepta `EvidenceLevel` (y sigue aceptando `player_alive`); `HealthStatus.evidencia` |
| `thetvview/favorites.py` | Identidad deducida, `is_favorite` por `tvg-id`, `pending_confirmation()`, copia `.bak` previa a cada escritura |
| `thetvview/m3u_parser.py` | `validate_playlist`, `resumir_cambios`, sidecar de `ETag`/`Last-Modified`, commit atómico con rollback, `FetchResult`, `PlaylistValidation` |
| `thetvview/storage/json_store.py` | `atomic_write_bytes` (mismo protocolo que el JSON, para cachés binarias) |
| `thetvview/security/safe_http.py` | `SafeHttpClient.get_conditional()` — GET que devuelve status y cabeceras, incluido un 304 |
| `thetvview/ui/screens.py` | Sesión en `NowPlayingScreen` y `play_channel`; limpieza por `ResourceScope`; `exit_summary` por motivo; fuente de evidencia |
| `thetvview/ui/app.py` | `SessionRegistry`; cierre que sella la sesión viva; modal de confirmación al rechazar una lista; resumen de cambios |

---

## 3. Pruebas añadidas

| Fichero | Tests | Qué congelan |
|---------|-------|--------------|
| `tests/test_playback_characterization.py` | 18 | El ciclo de reproducción **antes** de tocarlo (AUD-002) |
| `tests/test_player_session.py` | 54 | Máquina de estados completa, motivo de fin, `ResourceScope`, guard de obsolescencia |
| `tests/test_playback_session_integrada.py` | 17 | La sesión enchufada en la pantalla y en `play_channel` |
| `tests/test_player_evidence.py` | 62 | Escalera de evidencia, `MpvSignalSource`, `RetryPolicy`, error observado vs. causa inferida |
| `tests/test_channel_identity.py` | 82 | Identidad, reconciliación, favoritos por identidad, validación de listas, encabezados condicionales, modal de recarga |

**233 pruebas nuevas.**

```
Línea base (Fase 0):  2390 tests en 116 s
Ahora:                 2625 tests en 121 s   (+235 netas: las 233 de la tabla más un par
                                              de casos añadidos dentro de
                                              `test_m3u_cache`)
python -m thetvview.security.check → SECURITY CHECK PASSED (15/15)
```

Ningún test se saltó ni se eliminó. Los dos que cambiaron son de
`tests/test_m3u_cache.py`, y sólo en el **patch point** o en el reloj:

- el parche de `get_bytes` → `get_conditional` en dos pruebas de mensajes de
  error: las aserciones (el contrato) no cambian;
- `test_ttl_expirado_redescarga` fija el `mtime` del fichero servido, porque
  `Last-Modified` tiene granularidad de segundo y el test dependía del reloj.

---

## 4. Efecto visible para el usuario

| Antes | Ahora |
|-------|-------|
| Un canal que abre mpv y se queda bloqueado se lee «Reproduciendo» | La barra dice **«Reproduciendo (sin comprobar)»** si el motor no expone señal, y sólo «estable» si el reloj de medios avanza de forma continua |
| Un 401 reintenta 5 veces (~18 s de espera) antes del mensaje | Un 401 **no** reintenta: el error es permanente y se dice de inmediato |
| Cambiar la URL de un canal pierde el favorito | El favorito sobrevive por `tvg-id`; los que sólo tienen URL se marcan «pendientes de confirmar» |
| Una lista vacía sustituye la buena y la cache se llena de basura | Se conserva la lista anterior, con un modal que explica por qué y pregunta si se aplica igual |
| «Lista actualizada: 20000 canales» | «Lista actualizada: 20 001 canales · 1 nuevos · 20 sin cambios» |
| Nada dice por qué terminó una reproducción | Cada sesión terminada conserva `ended_at` y un `end_reason` tipado |
| Un fallo al apagar el proxy puede tapar el motivo real | El motivo original nunca se sobrescribe; los fallos de limpieza se acumulan aparte |
| Sin red al abrir un canal, la app gana un 401 | Pide `If-None-Match`/`If-Modified-Since` cuando el servidor los da, y un 304 no baja la lista |

---

## 5. Hallazgos nuevos de la auditoría

Cinco cosas que **no** estaban en el plan y aparecieron al ejecutar:

1. **`StreamErrorCode.BUFFERING_TIMEOUT` y `BACKEND_ERROR` no tenían descripción**
   (`player/errors.py`). Un fallo con esos códigos llegaba al usuario como
   `buffering_timeout` — el identificador interno, en inglés. Corregido aquí
   (REC-003); era un bug visible anterior a este trabajo.

2. **`Channel` no puede crecer.** `tests/test_catchup.py:432` fija el conjunto de
   campos de `Channel` (regla H1). El plan de CHAN-002 proponía un campo
   `identity` en el modelo; se resolvió **sin tocar el dataclass**: la identidad
   se deduce al leer. Además `Channel(**entrada_json)` sobre `favorites.json` es
   un contrato usado por `tests/test_cam_flow.py`, que se habría roto.

3. **`is_favorite` por `tvg-id` rompe el catch-up de Xtream si se aplica sin
   matiz.** En Xtream la URL **es** el modo: `xtream://` es el directo y
   `xtream-ts://` el archivo. Dos favoritos con el mismo `tvg-id` son dos
   favoritos legítimamente distintos (`tests/test_catchup.py:837`). La regla que
   quedó: `is_favorite` usa `tvg-id` **sólo para canales que no son referencia
   opaca**, y `toggle` sigue comparando por URL exacta.

4. **`fetch_bytes` usaba `get_bytes`, que descarta las cabeceras.** Para poder
   guardar `ETag`/`Last-Modified` hacía falta la respuesta entera, así que
   `SafeHttpClient` gana `get_conditional()`, que reutiliza `_raise_for_status`
   y por tanto conserva la cadena de errores intacta.

5. **`Path.replace()` no hace `fsync`.** El cache de playlist se escribía con
   `tmp.replace(path)`, que es atómico pero deja la ventana de pérdida de datos
   que es justo lo que importa aquí. Ahora pasa por `atomic_write_bytes`.

---

## 6. Riesgos del §10 del plan: estado

| Riesgo | Estado |
|--------|--------|
| Ruptura de `NowPlayingScreen` | **Cerrado.** 18 pruebas de caracterización escritas antes de tocar nada, todas verdes al día de hoy. |
| Regresión silenciosa en favoritos | **Cerrado.** Lectura dual, `.bak`, `Channel(**entrada)` intacto, prueba explícita de la rotación de URL. |
| Falsos positivos de evidencia en mpv | **Cerrado.** `time-pos` debe avanzar, con umbral mínimo de 1 tick; sin avance → «fuente abierta», nunca «estable». |
| Equivalencia del índice EPG mal resuelta | **Pendiente** (Fase 4). |
| Dos políticas de reintento (NFR-008) | **Cerrado.** `RetryPolicy` **envuelve** a `ReconnectPolicy`; `tests/test_stream_supervisor.py` no se modificó. |
| `classify_health(player_alive=…)` en 2 sitios | **Cerrado.** El parámetro viejo sigue funcionando con el texto de antes; sólo se añade matiz si el llamante pasa `evidencia`. |
| Tests dependientes del orden o del reloj | **Cerrado.** `rng`, reloj de evidencia y reloj de sesión inyectables; `reloj` en `MpvSignalSource`. |

---

## 7. Lo que queda

| Fase | Tickets | Requisitos |
|------|---------|------------|
| **4** | EPG-001 … EPG-004 | FR-EPG-001 … FR-EPG-013 |
| **5** | CAP-001, CAP-002, STREAM-001, DIAG-001 … DIAG-004 | FR-CAP, FR-STREAM-001/004, FR-DIAG |
| **6** | M3U-001 … M3U-004 | FR-M3U-001 … FR-M3U-007 |

### Pendientes que ya se ven venir

- **FR-EPG-006** (`_parse_ts` asume UTC en silencio) y **FR-EPG-009** (una EPG
  vacía sustituye a la buena) son los dos gaps que más daño hacen de lo que
  queda: son pérdida de datos y pérdida de información.
- **FR-STREAM-001** (resolver URI contra la URL **final** tras redirecciones) toca
  `streams/hls.py` y `streams/dash.py`, que ya están al ~80 % del SDD: es pulido,
  no rediseño.
- **M3U-001** (política de codificación) tiene un efecto secundario de seguridad:
  `errors="replace"` en silencio convierte bytes inválidos en U+FFFD, y eso
  cambia un `tvg-id` **sin avisar**. Con la identidad de la Fase 3 eso ya es
  mucho más visible, y por eso conviene hacerlo pronto.