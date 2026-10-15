"""Esquemas de persistencia: validación semántica y versionado (STORE-002).

Cada formato persistente adecuado lleva un ``schema_version``. Este módulo
declara:

- la **versión actual** de cada formato;
- un **validador** semántico por formato (no basta con que el JSON sea válido
  sintácticamente: los tipos, campos obligatorios y rangos se comprueban);
- la **política de campos desconocidos**: se conservan en el sense para no
  perder datos de una versión más nueva, pero no se usan.

Los cachés reconstruibles (``playlist_cache``, ``epg_cache``,
``xtream_cache``, ``track_cache``) **no** llevan ``schema_version``: se
borran y se regeneran. Versionarlos daría una falsa garantía sobre datos que
la app puede volver a descargar.

La validación ocurre en la lectura, antes de que un valor con el tipo
equivocado llegue a la interfaz.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .errors import StorageError

__all__ = [
    "SCHEMA_VERSION",
    "SCHEMAS",
    "validate_prefs",
    "validate_theme",
    "validate_playlist_list",
    "validate_favorites",
    "validate_recents",
    "validate_collections",
    "validate_scheduled_tasks",
    "validate_sources",
    "get_validator",
    "stamped",
    "ensure_version",
]

#: Versión actual de todos los formatos persistentes.
SCHEMA_VERSION: int = 1


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise StorageError(message)


def _opt_str(value: Any) -> bool:
    """True si `value` es ``None`` o un string."""
    return value is None or isinstance(value, str)


def validate_prefs(data: Any) -> None:
    """Valida un objeto de preferencias.

    Comprueba que los tipos son los esperados. No rechaza campos desconocidos:
    una versión más nueva puede escribir campos que esta todavía no conoce, y
    descartarlos perdería preferencias del usuario.
    """
    _require(isinstance(data, dict), "prefs.json debe contener un objeto.")
    d = data
    if "last_player" in d:
        _require(_opt_str(d["last_player"]), "last_player debe ser texto o nulo.")
    if "last_group_sort" in d:
        _require(
            d["last_group_sort"] in ("name", "count"),
            "last_group_sort debe ser «name» o «count».",
        )
    if "theme" in d:
        _require(isinstance(d["theme"], str), "theme debe ser texto.")
    for campo in ("subtitles_enabled", "ask_track_options", "diagnostics_enabled"):
        if campo in d:
            _require(isinstance(d[campo], bool), f"{campo} debe ser verdadero o falso.")
    for campo in (
        "preferred_audio_language",
        "preferred_subtitle_language",
        "preferred_quality",
        "preferred_backend",
    ):
        if campo in d:
            _require(_opt_str(d[campo]), f"{campo} debe ser texto o nulo.")
    for campo in ("connect_timeout", "startup_timeout", "reconnect_max_attempts"):
        if campo in d:
            valor = d[campo]
            _require(
                valor is None or (isinstance(valor, int) and not isinstance(valor, bool)),
                f"{campo} debe ser un número entero o nulo.",
            )
    if "reconnect_max_attempts" in d and isinstance(d["reconnect_max_attempts"], int):
        _require(
            0 <= d["reconnect_max_attempts"] <= 20,
            "reconnect_max_attempts debe estar entre 0 y 20.",
        )
    if "track_prefs" in d:
        _require(isinstance(d["track_prefs"], dict), "track_prefs debe ser un objeto.")
        for clave, bloque in d["track_prefs"].items():
            _require(isinstance(clave, str), "las claves de track_prefs deben ser texto.")
            _require(isinstance(bloque, dict), "cada bloque de track_prefs debe ser un objeto.")


def validate_theme(data: Any) -> None:
    """Valida ``theme.json``: un objeto con un único campo ``theme``."""
    _require(isinstance(data, dict), "theme.json debe contener un objeto.")
    if "theme" in data:
        _require(isinstance(data["theme"], str), "theme debe ser texto.")


def _validate_list_entries(
    data: Any, *, nombre: str, obligatorias: tuple[str, ...]
) -> None:
    _require(isinstance(data, list), f"{nombre} debe contener una lista.")
    for i, entrada in enumerate(data):
        _require(
            isinstance(entrada, dict),
            f"la entrada {i} de {nombre} no es un objeto.",
        )
        for campo in obligatorias:
            if campo in entrada:
                _require(
                    isinstance(entrada[campo], str),
                    f"«{campo}» de la entrada {i} de {nombre} debe ser texto.",
                )


def validate_playlist_list(data: Any) -> None:
    """Valida ``playlists.json``: lista de entradas del catálogo."""
    _validate_list_entries(
        data, nombre="playlists.json", obligatorias=("name", "source")
    )
    for entrada in data:
        if "allow_private_network" in entrada:
            _require(
                isinstance(entrada["allow_private_network"], bool)
                or isinstance(entrada["allow_private_network"], (int, float))
                or isinstance(entrada["allow_private_network"], str),
                "allow_private_network debe ser verdadero, falso o texto.",
            )


def validate_favorites(data: Any) -> None:
    """Valida ``favorites.json``: lista de canales serializados."""
    _validate_list_entries(data, nombre="favorites.json", obligatorias=("name", "url"))


def validate_recents(data: Any) -> None:
    """Valida ``recents.json``: lista de registros del historial."""
    _validate_list_entries(
        data, nombre="recents.json", obligatorias=("name", "url", "player")
    )


def validate_collections(data: Any) -> None:
    """Valida ``collections.json``: lista de colecciones versionadas."""
    _require(isinstance(data, list), "collections.json debe contener una lista.")
    for i, entrada in enumerate(data):
        _require(isinstance(entrada, dict), f"la entrada {i} de collections no es un objeto.")
        if "id" in entrada:
            _require(isinstance(entrada["id"], str) and entrada["id"].strip(), "id inválido.")
        if "name" in entrada:
            _require(isinstance(entrada["name"], str) and entrada["name"].strip(), "name inválido.")
        if "type" in entrada:
            _require(entrada["type"] in ("manual", "smart", "system"), "type inválido.")
        if "members" in entrada:
            _require(isinstance(entrada["members"], list), "members debe ser lista.")
        if "rules" in entrada and entrada["rules"] is not None:
            _require(isinstance(entrada["rules"], dict), "rules debe ser objeto.")
        if "schema_version" in entrada:
            try:
                int(entrada["schema_version"])
            except (TypeError, ValueError):
                raise StorageError("schema_version inválido en collections.") from None


def validate_scheduled_tasks(data: Any) -> None:
    """Valida ``scheduled_tasks.json``: documento versionado del planificador.

    Acepta las dos formas que :class:`thetvview.scheduler.TaskStore` sabe
    leer: el documento versionado ``{"version": N, "tasks": [...]}`` y la
    lista simple de una versión anterior. Comprobar sólo que cada tarea es un
    objeto con ``task_id`` y ``source`` en texto deja que un ``start_at``
    corrupto llegue hasta la interfaz; por eso el instante se comprueba aquí
    también.
    """
    if isinstance(data, dict):
        _require(
            isinstance(data.get("tasks"), list),
            "scheduled_tasks.json debe contener una lista de tareas.",
        )
        tareas = data["tasks"]
    elif isinstance(data, list):
        tareas = data
    else:
        raise StorageError("scheduled_tasks.json debe contener una lista o un objeto.")

    estados = {
        "pending", "scheduled", "running", "completed",
        "failed", "cancelled", "missed", "conflict", "skipped",
    }
    for i, tarea in enumerate(tareas):
        _require(isinstance(tarea, dict), f"la tarea {i} no es un objeto.")
        _require(
            isinstance(tarea.get("task_id"), str) and tarea["task_id"].strip(),
            f"task_id inválido en la tarea {i}.",
        )
        _require(
            isinstance(tarea.get("source"), str) and tarea["source"].strip(),
            f"source inválido en la tarea {i}.",
        )
        if "start_at" in tarea and tarea["start_at"] is not None:
            _require(
                isinstance(tarea["start_at"], (int, float))
                and not isinstance(tarea["start_at"], bool),
                f"start_at inválido en la tarea {i}.",
            )
        if "status" in tarea and tarea["status"] is not None:
            _require(
                str(tarea["status"]) in estados,
                f"status inválido en la tarea {i}.",
            )
        if "conflicts" in tarea and tarea["conflicts"] is not None:
            _require(
                isinstance(tarea["conflicts"], list)
                and all(isinstance(c, str) for c in tarea["conflicts"]),
                f"conflicts inválido en la tarea {i}.",
            )


#: Tope de canales con asociación guardada. Está por el mismo motivo que en
#: `validate_prefs`: un archivo que crece sin control es un archivo que
#: alguien está llenando a mano o que viene de una versión con otro criterio.
_MAX_CLAVES_FUENTES = 5_000
_MAX_RECHAZOS_FUENTES = 50


def validate_sources(data: Any) -> None:
    """Valida ``sources.json``: fuente preferida y descartes, por canal.

    Es un formato de **dos mapas de texto**, y se validan como tales: no una
    lista de objetos donde el nombre pueda ser cualquier cosa. La
    comprobación cara aquí no es el tipo sino el **tope**: un archivo con un
    millón de descartes es un archivo que se ha llenado solo, y aceptarlo en
    memoria es la forma de que la app se quede sin memoria justo al abrir el
    modal que los muestra.
    """
    _require(isinstance(data, dict), "sources.json debe contener un objeto.")
    max_claves = _MAX_CLAVES_FUENTES
    for campo in ("asociaciones", "rechazos"):
        if campo not in data:
            continue
        bloque = data[campo]
        _require(
            isinstance(bloque, dict),
            f"{campo} en sources.json debe ser un objeto.",
        )
        _require(
            len(bloque) <= max_claves,
            f"sources.json trae {len(bloque)} claves en {campo}: "
            f"el tope son {max_claves}.",
        )
        for clave, valor in bloque.items():
            _require(
                isinstance(clave, str) and clave.strip(),
                f"las claves de {campo} deben ser texto no vacío.",
            )
            if campo == "asociaciones":
                _require(
                    isinstance(valor, dict),
                    f"la asociación de «{clave}» no es un objeto.",
                )
                if "fuente" in valor:
                    _require(
                        isinstance(valor["fuente"], str),
                        f"la fuente de «{clave}» debe ser texto.",
                    )
                if "candidata" in valor:
                    _require(
                        valor["candidata"] is None or isinstance(valor["candidata"], str),
                        f"la candidata de «{clave}» debe ser texto o nulo.",
                    )
                if "fecha" in valor:
                    _require(
                        valor["fecha"] is None or isinstance(valor["fecha"], str),
                        f"la fecha de «{clave}» debe ser texto o nulo.",
                    )
            else:
                _require(
                    isinstance(valor, list)
                    and all(isinstance(v, str) for v in valor),
                    f"los descartes de «{clave}» deben ser una lista de texto.",
                )
                _require(
                    len(valor) <= _MAX_RECHAZOS_FUENTES,
                    f"«{clave}» acumula {len(valor)} descartes: "
                    f"el tope son {_MAX_RECHAZOS_FUENTES}.",
                )


#: Formato -> validador. Los nombres coinciden con el nombre de fichero sin
#: extensión, que es como se registran en :mod:`thetvview.storage.json_store`.
SCHEMAS: dict[str, Callable[[Any], None]] = {
    "prefs": validate_prefs,
    "theme": validate_theme,
    "playlists": validate_playlist_list,
    "favorites": validate_favorites,
    "recents": validate_recents,
    "collections": validate_collections,
    "scheduled_tasks": validate_scheduled_tasks,
    "sources": validate_sources,
}


def get_validator(name: str) -> Callable[[Any], None] | None:
    """Validador semántico del formato `name`, o ``None`` si no lo hay."""
    return SCHEMAS.get(str(name or "").strip().lower())


def stamped(data: Mapping[str, Any], *, version: int = SCHEMA_VERSION) -> dict[str, Any]:
    """Devuelve `data` con ``schema_version`` puesto."""
    out = dict(data)
    out["schema_version"] = int(version)
    return out


def ensure_version(data: Mapping[str, Any], *, nombre: str) -> int:
    """Lee el ``schema_version`` de `data`; 0 si no lo tiene.

    La ausencia de ``schema_version`` significa «formato anterior al
    versionado» (v0), no «fichero inválido»: quien llama decide si migra.
    """
    _require(isinstance(data, dict), f"{nombre} debe contener un objeto.")
    bruto = data.get("schema_version")
    if bruto is None:
        return 0
    try:
        return int(bruto)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            f"{nombre} tiene un schema_version que no es un número."
        ) from exc
