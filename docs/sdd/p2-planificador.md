# P2.5-C Planificador — política de tiempo, reinicio y tareas perdidas

Documento de decisión de la **Fase 7** (P2.5-C) del plan de mejoras de prioridad
media. Cierra el ticket **P-03** y define el comportamiento observable del
planificador, para que lo que le pasa al reloj sea predecible y no dependa de
en qué momento arrancó la aplicación.

El planificador **no captura nada**: decide el instante y delega la captura en
`thetvview/recording/backend.py` (Fase 6), que es el único módulo autorizado a
lanzar procesos de grabación.

## 1. Principios

1. **El reloj no manda sobre el usuario.** Si una tarea se pierde, se dice; no
   se graba a destiempo ni se cambia la hora o la fuente programada.
2. **Nunca dos veces lo mismo.** Una tarea que ya se está ejecutando no vuelve a
   arrancar, por más que el reloj avance o la aplicación se reinicie.
3. **Nada se borra por sorpresa.** Cancelar o borrar una tarea no toca el
   archivo ya grabado, y una grabación en curso no se borra ni se cancela sin
   una acción explícita (`force=True`).
4. **Nada sobrevive por accidente.** Al arrancar, ninguna tarea puede seguir
   «grabando sola»: no hay proceso huérfano detrás de la aplicación cerrada.

## 2. Estados

| Estado | Significado |
| --- | --- |
| `pending` | creada y validada, todavía no entra en la cola efectiva |
| `scheduled` | en cola; es el estado normal de una tarea futura |
| `running` | el grabador manual de Fase 6 tiene una sesión viva para esa tarea |
| `completed` | terminó bien; se guarda la ruta del archivo |
| `failed` | falló al iniciar o durante la ejecución, sin reintentos disponibles |
| `cancelled` | cancelada por el usuario |
| `missed` | su momento pasó y no pudo ejecutarse (aplicación cerrada, suspensión larga) |
| `skipped` | omitida por política declarada (`missed_policy = skip`) |
| `conflict` | disponible como aviso; el estado efectivo sigue siendo `scheduled` con la lista `conflicts` |

Las transiciones válidas son: `pending → scheduled → running → completed |
failed`, más `pending | scheduled → cancelled`, `pending | scheduled →
missed | skipped`. No hay regreso desde un estado terminal.

## 3. Reloj

- El instante de inicio se guarda como **epoch en UTC** (`start_at`) y se
  muestra en la **zona horaria de presentación** de la tarea (`timezone` +
  `tz_offset_minutes`). Cambiar la zona de presentación no cambia el instante.
- `tick(now)` es **idempotente**: llamarlo muchas veces, o con el reloj antes y
  después, no duplica tareas ni procesos. Una tarea en `running` nunca vuelve a
  la cola.
- **Cambio de reloj hacia atrás**: una tarea futura pasa a estar «más lejos», no
  antes. Se respeta el instante guardado.
- **Cambio de reloj hacia adelante**: la tarea se dispara en el siguiente
  `tick`, dentro de la ventana de gracia (§4).

## 4. Ventana de gracia y tareas perdidas

- `grace_seconds` (por omisión 30 s) es la anticipación con la que una tarea se
  considera «llegó su momento».
- `retry_policy.missed_grace_seconds` extiende esa ventana por tarea.
- Una tarea se marca `missed` cuando `start_at + gracia < ahora`, es decir
  cuando su instante pasó más allá de la ventana. Antes de eso sigue en cola y
  puede arrancar.
- `missed_policy` declara qué hacer: `mark_missed` (por omisión), `run_if_safe`
  o `skip` → estado `skipped`.
- **Reintentos**: si el grabador falla al arrancar, la tarea consume un reintento
  de `retry_policy.max_retries` y sigue en cola. Al agotarse, pasa a `failed`.
  El reintento nunca duplica una tarea ya en ejecución.

## 5. Reinicio, suspensión y aplicación cerrada

- Al cargar, `TaskStore.recover` marca `missed` toda tarea que estuviera en
  `running`: la aplicación anterior se cerró, nadie seguía grabando.
- Una tarea que empezó mientras la aplicación estaba cerrada **no se graba
  retroactivamente**: se marca `missed` o `skipped` según su política.
- Una tarea que empezó con la aplicación abierta y terminó durante una
  suspensión larga se evalúa con la misma regla de gracia al volver.

## 6. Solapamientos y concurrencia

- `max_concurrent = 1`: una sola grabación simultánea.
- Al agregar o evaluar tareas se detectan los solapamientos de intervalo y se
  registra el aviso en `task.conflicts` y en `task.message`.
- **Nunca se cambia en silencio** ni la hora ni la fuente de una tarea para
  «arreglar» un conflicto.
- Si al llegar su turno no hay hueco, la tarea **queda en cola** con el motivo
  visible («en espera: ya hay una grabación en curso») y arranca en cuanto se
  libere el hueco, siempre que no supere su ventana de gracia.

## 7. Idempotencia

- La identidad de una tarea es su **definición** (identificador, fuente, canal,
  instante, duración, fin, zona horaria y destino), no su estado.
- Reagregar una tarea con la misma definición devuelve la existente: no hay
  duplicados tras un reintento o un reinicio.
- El mismo identificador con otra definición se **rechaza** con un error claro:
  es un conflicto de identidad, no una sobrescritura.
- Al cargar del disco, un identificador repetido se ignora y se reporta.

## 8. Persistencia

- `thetvview/scheduler/backend.py` guarda un documento versionado
  (`{"version": 1, "tasks": [...]}`) con escritura atómica (archivo temporal +
  `os.replace`).
- Al cargar se **valida cada tarea**: una ilegible, inválida o duplicada se
  registra en `load_errors` y no impide cargar las demás.
- Se acepta un archivo legado que sea una lista simple de tareas.

## 9. Límites

| Límite | Valor | Dónde |
| --- | --- | --- |
| Grabaciones simultáneas | 1 | `SchedulerConfig.max_concurrent` |
| Anticipación máxima | 30 s | `SchedulerConfig.grace_seconds` |
| Reintentos | declarados por tarea | `RetryPolicy.max_retries` |
| Archivo borrado al quitar tarea | nunca, salvo `delete_file=True` | `Scheduler.remove` |
| Procesos de captura | los del grabador manual | `thetvview/recording/backend.py` |

## 10. Cancelación y borrado

- `cancel(id)` cancela una tarea **futura**. Sobre una grabación en curso lanza
  error: hace falta `force=True`, que es la señal explícita del usuario.
- `remove(id)` borra la tarea del planificador. Si la tarea está en curso, se
  rechaza.
- El archivo grabado **nunca** se borra como efecto secundario: sólo con
  `remove(id, delete_file=True)`.