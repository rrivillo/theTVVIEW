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


#: Formato -> validador. Los nombres coinciden con el nombre de fichero sin
#: extensión, que es como se registran en :mod:`thetvview.storage.json_store`.
SCHEMAS: dict[str, Callable[[Any], None]] = {
    "prefs": validate_prefs,
    "theme": validate_theme,
    "playlists": validate_playlist_list,
    "favorites": validate_favorites,
    "recents": validate_recents,
    "collections": validate_collections,
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
