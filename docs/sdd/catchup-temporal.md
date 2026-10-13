# Semántica temporal del catch-up por proveedor (CU-01)

**Documento:** `docs/sdd/catchup-temporal.md`
**Módulo:** `thetvview/catchup.py`
**Ticket:** CU-01 del plan `.opencode/plans/plan-mejoras-prioridad-media.md` (Fase 1)
**Fecha:** 2026-10-13
**Referencias externas:** especificación de `catchup-source` y sus especificadores de
formato en `kodi-pvr/pvr.iptvsimple` (el estándar de facto que los reproductores IPTV
respetan), y el endpoint `streaming/timeshift.php` de Xtream Codes.

> Este documento **no cambia comportamiento**. Define qué significa cada instante y qué
> formato espera cada mecanismo, para que CU-02 a CU-06 puedan validar contra algo escrito
> en vez de contra una suposición.

---

## 1. La regla que ya no se puede negociar

**Dentro de theTVVIEW el catch-up se calcula siempre en instantes absolutos (UTC).**
Todo `datetime` que entra al dominio pasa por `_aware()` (`catchup.py:214`) y todo epoch
que sale se calcula con `_aware_seconds()` (`catchup.py:460`), que convierte a UTC antes de
llamar a `timestamp()`.

Consecuencia: la zona horaria del ordenador del usuario **no puede** cambiar el instante
que se pide. Un usuario en Madrid y otro en Lima piden el mismo archivo para el mismo
programa. Eso es correcto y es lo que debe seguir siendo.

**El problema no está dentro. Está en el último salto:** el instante se convierte al
formato que espera el proveedor, y ahí es donde hoy se pierde información de zona.

---

## 2. Los dos mecanismos soportados

### 2.1 Xtream Codes — `streaming/timeshift.php`

| Aspecto | Valor |
|---------|-------|
| Mecanismo declarado por | `tv_archive >= 1` **y** `tv_archive_duration > 0` |
| Parámetro de inicio | `start` = epoch **UTC en segundos** |
| Parámetro de duración | `duration` = segundos |
| Parámetro de fin | no se envía; se calcula el fin con `start + duration` |
| Zona de entrada | **ninguna**: el epoch no lleva zona. Es UTC por definición |
| Formato | entero decimal, sin decimales, sin signo `+`, sin unidades |
| Ventana | `tv_archive_duration` en **días**, multiplicada por 86400 |

Se construye en `xtream_security.build_timeshift_url()` y la referencia opaca que lo
transporta es `xtream-ts://<fuente>/<stream_id>.ts?start=<epoch>&dur=<segundos>`
(`catchup.CatchupRef`). **Sin ambigüedad posible**: epoch UTC en segundos.

### 2.2 M3U — atributo `catchup-source`

| Aspecto | Valor |
|---------|-------|
| Mecanismo declarado por | `catchup` presente, `catchup-source` presente, `catchup-days > 0` |
| Parámetro de inicio | el que diga la plantilla (`{utc}`, `${start}`, `{Y}-{m}-{d}T{H}:{M}:{S}`…) |
| Zona de entrada | **la del placeholder**, y hay que saber cuál es |
| Ventana | `catchup-days` en días |

Aquí está el problema: `catchup-source` es una **plantilla publicada por un tercero** y
sus marcadores significan cosas distintas según cuál se use.

---

## 3. Los marcadores de `catchup-source` y su zona

Extraídos de la especificación de `pvr.iptvsimple`. El significado de la columna
«tiempo» es el que decide si hace falta conversión de zona.

| Marcador | Tiempo | Formato | Zona |
|----------|--------|---------|------|
| `{utc}` | inicio del programa | `Y-m-d-H-M-S` | **UTC** |
| `${start}` | alias de `{utc}` | ídem | **UTC** |
| `{utcend}` | inicio + duración | ídem | **UTC** |
| `${end}` | alias de `{utcend}` | ídem | **UTC** |
| `{lutc}`, `${now}`, `${timestamp}` | **ahora** (no el programa) | ídem | **UTC** |
| `{Y}` `{m}` `{d}` `{H}` `{M}` `{S}` | inicio del programa | piezas sueltas | **UTC** |
| `{duration}`, `${duration}` | duración | segundos | — |
| `{duration:X}` | duración dividida por `X` | segundos | — |
| `{offset:X}` | (ahora − inicio) dividido por `X` | segundos | — |
| `{catchup-id}` | identificador del programa | texto del XMLTV | — |

Ejemplos reales que aparecen en playlists publicadas:

```text
catchup-source="?utc={utc}&lutc={lutc}"                          # SIPTV / shift
catchup-source="&cutv={Y}-{m}-{d}T{H}:{M}:{S}"                  # el más común
catchup-source="http://host/video-{utc}-{duration}.m3u8"        # Flussonic
catchup-source="?start={utc:YmdHM}&end={end:YmdHM}"             # con formato explícito
```

**Los cuatro son UTC.** El nombre `{utc}` no es una convención de este proyecto: es el
nombre del estándar que los reproductores IPTV ya implementan.

### 3.1 El defecto que este documento deja escrito

`catchup.template_values()` (`catchup.py:743`) produce hoy:

```python
"utc": start.astimezone().strftime("%Y-%m-%d:%H-%M-%S")
```

`start` entra ya convertido a UTC y `.astimezone()` **vuelve a convertir a hora local**
del proceso. Medido en esta máquina (Europe/Madrid, UTC+1 en marzo):

```text
instante pedido  2026-03-01 20:00:00+00:00
{start} (epoch)  1772395200   -> 2026-03-01 20:00:00+00:00   correcto
{utc}            2026-03-01:17-00-00                          incorrecto (3 h de más)
```

Consecuencia para el usuario: en una lista con `catchup-source="?utc={utc}"`, el
proveedor recibe una hora local y devuelve **otro momento del día**. En un servidor de
otro hemisferio el error es de hasta 12 h.

**No se arregla en CU-01** (aquí no se toca `thetvview/`). Lo arregla **CU-03**, con test
de regresión, porque es un cambio de comportamiento visible.

---

## 4. El otro parámetro que ya existe y no se usa: la corrección de zona

El estándar define dos atributos que **desplazan** el instante antes de construir la URL:

| Atributo | Dónde | Rango | Significado |
|-----------|-------|-------|-------------|
| `catchup-correction` | `#EXTINF` | −12 h a +14 h | corrige el instante de este canal |
| `catchup-correction` | cabecera `#EXTM3U` | −12 h a +14 h | valor por defecto de la lista |
| `tvg-shift` | `#EXTINF` | −12 h a +14 h | desplazamiento de la guía de ese canal |

Hoy theTVVIEW **los ignora**: `grep -rn "catchup_correction\|tvg_shift" thetvview/` → 0
coincidencias. Es decir: una lista que declara `catchup-correction="-4.0"` porque su guía
va desfasada cuatro horas reproduce el archivo cuatro horas desplazado, y nada lo dice.

**Decisión de esta fase:** **no** se implementa la corrección. Se registra como hueco
conocido y **no** se infiere nada a partir de ella. Razón: es una decisión de producto y
de fuente de verdad (¿la guía manda sobre la URL, o al revés?), y aplicarla mal desplaza
la reproducción de forma silenciosa. El impacto real es bajo porque sólo afecta a listas
que ya declaran la corrección, y en ellas el resultado actual —ignorar el atributo— es
al menos **determinista y visible** (el usuario ve el programa incorrecto y puede
reportarlo), mientras que una corrección mal aplicada sería invisible.

Queda anotado para una fase posterior, junto con la identidad de fuentes (Fase 5A), que
es donde tiene sentido.

---

## 5. Ventanas y límites

### 5.1 Valores que declara un panel

`catchup-days` y `tv_archive_duration` están **en días**, no en horas. Valores habituales
en playlists publicadas: `1`, `2`, `3`, `5`, `7`, `14`, `30`, `90`.

`thetvview` los convierte con `days * SECONDS_PER_DAY` (`catchup.py:284`, `:247`) y los
guarda en `CatchupCapability.archive_duration_seconds`. `archive_duration_days` redondea
**sólo para mostrar** (`catchup.py:152`); el cálculo usa los segundos exactos. Correcto.

### 5.2 El valor cero no es una ventana

`catchup-days="0"` y `catchup-days="-3"` significan lo mismo: el proveedor **no**
declara ventana utilizable. Hoy ambos caen en `days <= 0 → DISABLED` (`catchup.py:279`),
que es fail-closed y correcto. Se mantiene.

### 5.3 No hay duración máxima declarada

El estándar **no define** un tope de duración por petición: quien lo impone es el
proveedor, y cada uno lo hace como quiere. Inventar un límite interno sin dato sería
exactamente la suposición que el SDD prohíbe.

Lo que sí se puede fijar sin suposiciones es un **tope propio y declarado** para evitar
que una petición absurda llegue a la red, y ese es el que define CU-02.

---

## 6. Formato del instante: tres formatos y una ambigüedad real

Los formatos de marca temporal que aparecen en plantillas reales:

| Formato | Ejemplo | Dónde |
|---------|---------|-------|
| Epoch en **segundos** | `1772395200` | `{start}` de Xtream, `${timestamp}` de algunos paneles |
| Epoch en **milisegundos** | `1772395200000` | paneles que multiplican por 1000 |
| `Y-m-d-H-M-S` | `2026-03-01:20-00-00` | `{utc}` del estándar |
| `YmdH` con prefijo | `2026030120` | `{utc:YmdHM}` con formato explícito |

**La ambigüedad segundos/milisegundos es real**: los dos son enteros y los dos son
plausibles. Se resuelve por **magnitud**, no por adivinanza:

```text
epoch < 1_000_000_000_000  (~año 33658)  ->  segundos
epoch >= 1_000_000_000_000                ->  milisegundos, dividir entre 1000
```

El umbral es el mismo que usan los reproductores IPTV maduros y es el que hace
`CatchupRef.parse()` correcto al leer una referencia que escribió otro cliente.

**Decisión:** elTVVIEW **nunca** genera epoch en milisegundos. Envía segundos. Si un
proveedor los exige, es un mecanismo que el estándar no describe y por tanto no se admite
como declarado (`catchup.py` rechaza el mecanismo desconocido en `adapter_for`).

---

## 7. Reglas para el cambio de horario (DST)

El instante que se pide es UTC, así que **el DST no puede alterar el resultado**: 20:00
UTC son las 20:00 UTCo elija la zona que elija el usuario. Eso ya está garantizado.

Lo que sí hay que probar (CU-05) es que la conversión **de vuelta** a zona local, que es lo
que hacen `{utc}` y las piezas `{Y}{m}{d}{H}{M}{S}` después de corregir CU-03, no produzca:

| Caso | Ejemplo (Europe/Madrid) | Qué hay que comprobar |
|------|------------------------|-----------------------|
| Hora **inexistente** (spring forward) | 2026-03-29 02:30 no existe | qué sello se genera |
| Hora **repetida** (fall back) | 2026-10-25 02:30 ocurre dos veces | que se use `fold=0` y quede **documentado** |
| Programa que **cruza medianoche** | 23:30 → 00:15 | que el intervalo sea válido y su duración correcta |

Medido con `zoneinfo` (stdlib) en esta máquina:

```text
2026-03-29 02:30 +01:00 (no existe)  ->  2026-03-29 01:30 UTC   # Python no valida
2026-10-25 02:30 +02:00 (fold=0)     ->  2026-10-25 00:30 UTC
2026-10-25 02:30 +01:00 (fold=1)     ->  2026-10-25 01:30 UTC
```

Python **no lanza** error al construir una hora inexistente: la normaliza. Esa
normalización silenciosa es exactamente el tipo de cosa que CU-05 debe hacer explícita, y
por eso el módulo no puede depender de que `datetime` avise.

---

## 8. Resumen para la implementación

| Regla | Dónde se aplica | Ticket |
|-------|-----------------|--------|
| Todo instante es UTC dentro del dominio | `_aware`, `_aware_seconds` | ya hecho |
| Xtream: epoch UTC en segundos, sin ambigüedad | `CatchupRef`, `build_timeshift_url` | ya hecho |
| `{utc}` y `{Y}{m}{d}{H}{M}{S}` son **UTC**, no hora local | `template_values` | **CU-03** |
| Epoch < 10¹² son segundos; ≥ 10¹² son milisegundos | `CatchupRef.parse` | ya hecho |
| `catchup-days <= 0` es fail-closed, no ventana de 0 | `from_m3u_attrs` | ya hecho |
| `fin > inicio` obligatorio, **rechazar** en vez de normalizar | `CatchupRequest` | **CU-02** |
| Tope propio de duración por petición | `validar_intervalo` | **CU-02** |
| Zona de entrada: UTC para todos los marcadores soportados | este documento | **CU-01** |
| `catchup-correction` / `tvg-shift` se ignoran a propósito | este documento §4 | hueco declarado |