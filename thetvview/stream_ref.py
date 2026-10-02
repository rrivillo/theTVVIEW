"""Referencias opacas a un stream con credenciales (SDD §37, gap B8).

Antes, `Channel.url` para una fuente Xtream era
``…/live/usuario/contraseña/101.ts``: esa contraseña se guardaba en
``data/recents.json``, en ``favorites.json` y aparecía en la barra de
estado. Aquí la URL que circula por el dominio es **opaca**::

    xtream://<fuente>/<tipo>/<id>.<ext>          → directo (live)
    xtream-ts://<fuente>/<id>.<ext>?start&dur     → archivo (catch-up)

y las credenciales sólo se recuperan en el último momento, para
construir el argv del reproductor. La resolución nunca persiste: el
objeto `Channel` original no se toca.

Hay dos prefijos porque hay dos caminos, y ambos son igual de opacos: el
directo (`StreamRef`) y el archivo (`thetvview.catchup.CatchupRef`). Se
resuelven en el **mismo** sitio, `resolve_channel_url`, contra
`PlaylistManager.get_credentials()`.

Sólo stdlib. Ninguno de los dos prefijos es un esquema real: si algo se
escapara, `validate_url(purpose="stream")` lo rechaza y el reproductor
nunca lo ve.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote

__all__ = [
    "PREFIX",
    "TS_PREFIX",
    "OPAQUE_PREFIXES",
    "MissingCredentialsError",
    "StreamRef",
    "is_opaque_ref",
    "resolve_channel_url",
]

#: Prefijo de la referencia opaca. No es un esquema jugable en la red.
PREFIX = "xtream://"

#: Prefijo de la referencia opaca de archivo (catch-up / timeshift). Vive
#: aquí (y no sólo en `catchup.py`) para que favoritos y recientes puedan
#: reconocer ambas referencias sin importar el dominio.
TS_PREFIX = "xtream-ts://"

#: Todos los prefijos opacos. `favorites`/`recents` los consultan para no
#: redactar (ni reescribir) una referencia que ya es segura.
OPAQUE_PREFIXES: tuple[str, ...] = (PREFIX, TS_PREFIX)


class MissingCredentialsError(Exception):
    """No hay credenciales para resolver la referencia (mensaje apto para modal)."""


@dataclass(frozen=True)
class StreamRef:
    """Identidad de un stream Xtream sin secretos.

    Attributes:
        source_name: nombre de la fuente en el catálogo (clave de credenciales).
        content_type: ``live`` | ``movie`` | ``series``.
        stream_id: identificador del stream en el panel.
        extension: extensión del fichero (``ts``, ``mp4``…).
        provider: hoy sólo ``xtream``; está para poder añadir otros.
    """

    source_name: str
    content_type: str
    stream_id: str
    extension: str = "ts"
    provider: str = "xtream"

    def to_opaque(self) -> str:
        """URL opaca, sin usuario ni contraseña.

        >>> StreamRef("Panel", "live", "101").to_opaque()
        'xtream://Panel/live/101.ts'
        """
        # str() defensivo: los paneles devuelven stream_id como int.
        return (
            f"{PREFIX}{quote(str(self.source_name), safe='')}/"
            f"{quote(str(self.content_type), safe='')}/"
            f"{quote(str(self.stream_id), safe='')}"
            f".{quote(str(self.extension), safe='')}"
        )

    @classmethod
    def parse(cls, url: str) -> "StreamRef | None":
        """Extrae la referencia de una URL opaca; None si no lo es.

        Tolerante a nombres de fuente con ``/`` o caracteres raros: van
        percent-encoded al construir y se restauran aquí.
        """
        if not isinstance(url, str) or not url.startswith(PREFIX):
            return None
        rest = url[len(PREFIX):]
        parts = rest.split("/")
        if len(parts) != 3:
            return None
        source, content_type, leaf = parts
        stream_id, dot, extension = leaf.rpartition(".")
        if not dot or not source or not content_type or not stream_id:
            return None
        return cls(
            source_name=unquote(source),
            content_type=unquote(content_type),
            stream_id=unquote(stream_id),
            extension=unquote(extension),
        )

    def resolve(self, manager: object) -> str:
        """URL real con credenciales, consultando el catálogo en caliente.

        Args:
            manager: objeto con ``get_credentials(nombre) ->
                (server_url, username, password) | None`` (PlaylistManager).

        Raises:
            MissingCredentialsError: la fuente no existe o no tiene
                contraseña guardada. El mensaje no lleva secretos.
        """
        creds = None
        getter = getattr(manager, "get_credentials", None)
        if callable(getter):
            try:
                creds = getter(self.source_name)
            except Exception:  # noqa: BLE001 - un keyring roto no rompe el flujo
                creds = None
        if not creds:
            raise MissingCredentialsError(
                f"No tengo credenciales guardadas para la fuente "
                f"'{self.source_name}'; abre esa lista y vuelve a intentarlo."
            )
        server_url, username, password = creds
        from .xtream_security import build_stream_url

        return build_stream_url(
            server_url, username, password,
            self.stream_id, self.content_type, self.extension,
        )


def is_opaque_ref(url: str) -> bool:
    """True si `url` es una referencia opaca (directo o archivo).

    Las referencias opacas no llevan credenciales, así que quien las
    persiste (favoritos, recientes) debe dejarlas intactas: no hay nada
    que redactar ni que migrar.
    """
    return isinstance(url, str) and url.startswith(OPAQUE_PREFIXES)


def resolve_channel_url(url: str, manager: object) -> str:
    """Devuelve la URL que hay que pasar al reproductor.

    - Si `url` **no** es una referencia opaca, se devuelve tal cual
      (M3U con URL cruda, plantilla M3U ya renderizada, rutas locales).
    - Si es ``xtream://``, se resuelve con `StreamRef` (directo).
    - Si es ``xtream-ts://``, se resuelve con `CatchupRef` (archivo):
      el mismo `MissingCredentialsError`, el mismo "en caliente".
    - Si lleva un prefijo pero está corrupta, también se niega: jamás se
      manda ``xtream://…`` ni ``xtream-ts://…`` al reproductor.
    """
    if not isinstance(url, str):
        return url
    if url.startswith(TS_PREFIX):
        from .catchup import CatchupRef  # import perezoso: evita el ciclo

        ts_ref = CatchupRef.parse(url)
        if ts_ref is None:
            raise MissingCredentialsError(
                "Referencia de archivo ilegible; vuelve a abrir la lista de origen."
            )
        return ts_ref.resolve(manager)
    if not url.startswith(PREFIX):
        return url
    ref = StreamRef.parse(url)
    if ref is None:
        raise MissingCredentialsError(
            "Referencia de stream ilegible; vuelve a abrir la lista de origen."
        )
    return ref.resolve(manager)


def has_credentials(url: str) -> bool:
    """True si `url` lleva login embebido (userinfo o path Xtream)."""
    from .security.redaction import contains_embedded_login

    return contains_embedded_login(url or "")
