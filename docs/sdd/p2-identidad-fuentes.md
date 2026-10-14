# Identidad de fuentes — plan de migración (P2.3 / Fase 5A)

## Objetivo

Permitir fuentes alternativas para un canal sin romper `Channel`, favoritos, recientes ni preferencias. La identidad del canal se conserva y se registra procedencia (`source_id`) como candidata técnica.

## Identidades hoy

1. `ChannelIdentity`: lógica del canal (prioridad: `tvg-id`, luego URL normalizada, luego nombre+grupo).
2. `source_id`: lista o cuenta que define el recurso técnico.
3. URL: cómo abrir el stream, incluidos tokens.
4. `tracks.prefs.channel_key`: ámbito de preferencias de pistas (hash), no se sustituye.

## Migración

- No modificar archivos originales sin copia previa (`.bak-<timestamp>`).
- `migrar_identidad_logica(dir)` lee favoritos/recientes y construye candidatos lógicos.
- Preferencias de pistas se conservan intactas; se hace backup por seguridad.
- Falla de una lista de comparación no impide arrancar: el catálogo entra en estado `failed-safe` o `cancelled`.

## Fuente elegida

- Asociación manual y deshacer persisten en `logical_candidates.json`.
- Candidata rechazada no se vuelve a proponer sin cambios relevantes.
- Asociar no borra la entrada original.
PY