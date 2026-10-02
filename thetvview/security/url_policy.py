"""Política de URL (SDD §10, gaps B1/B2), solo stdlib.

Nada llega al reproductor ni a la red sin pasar por :func:`validate_url`.

Qué valida
----------

- esquema contra una **allowlist por propósito** (nunca una denylist sola);
- host no vacío;
- puerto en rango;
- **userinfo embebido** (``user:pass@host``) rechazado: las credenciales se
  guardan aparte, jamás dentro de la URL;
- fragmento ``#…`` rechazado;
- caracteres de control crudos y percent-encoded (``%00``…``%1F``, ``%7F``);
- espacios internos y longitud (``MAX_URL_LENGTH``);
- con ``purpose="stream"``, cualquier cadena que empiece por ``-``
  (inyección de argumentos en mpv/mplayer/vlc → gap B1).

Esquemas
--------

======================  ==========================================
propósito ``metadata``  ``http``, ``https``
propósito ``stream``    ``http``, ``https``, ``ffmpeg``
======================  ==========================================

``ffmpeg://`` es el único *wrapper* interno que el reproductor ya usa
(``player.py`` antepone el prefijo para que libavformat resuelva playlists
HLS). Si va envuelta otra URL (``ffmpeg://http://…``), se valida la URL
interior; si es una ruta relativa, no hay destino de red que validar.

Rechazados de forma explícita y con mensaje propio: ``file``, ``smb``,
``ftp``, ``sftp``, ``gopher``, ``tftp``, ``data``, ``javascript``,
``jar``, ``blob``, ``about``, ``rtsp``, ``rtsps``, ``udp``, ``rtp``,
``rtmp``, ``ws``, ``wss`` (SDD §36: ``rtsp``/``udp``/``rtmp`` se evalúan
antes de añadirlos; hoy no entran).

Lo que **no** hace este módulo: resolución DNS ni comprobación de red
privada. Para eso está :mod:`thetvview.security.ssrf`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import SplitResult, urlsplit

from .errors import InvalidUrlError

__all__ = [
    "PURPOSE_METADATA",
    "PURPOSE_STREAM",
    "PURPOSES",
    "MAX_URL_LENGTH",
    "ALLOWED_SCHEMES",
    "REJECTED_SCHEMES",
    "UrlParts",
    "validate_url",
    "is_allowed_scheme",
]

PURPOSE_METADATA: str = "metadata"
PURPOSE_STREAM: str = "stream"
PURPOSES: tuple[str, ...] = (PURPOSE_METADATA, PURPOSE_STREAM)

#: Longitud máxima de una URL (SDD §10.2).
MAX_URL_LENGTH: int = 2048

ALLOWED_SCHEMES: dict[str, frozenset[str]] = {
    PURPOSE_METADATA: frozenset({"http", "https"}),
    PURPOSE_STREAM: frozenset({"http", "https", "ffmpeg"}),
}

#: Esquemas que nunca se aceptan, con mensaje explícito (denylist solo para
#: explicar el motivo: la decisión la toma la allowlist).
REJECTED_SCHEMES: frozenset[str] = frozenset(
    {
        "file",
        "smb",
        "cifs",
        "ftp",
        "ftps",
        "sftp",
        "gopher",
        "tftp",
        "telnet",
        "data",
        "javascript",
        "vbscript",
        "jar",
        "blob",
        "about",
        "dict",
        "ldap",
        "rsync",
        "git",
        "ssh",
        "scp",
        "rtsp",
        "rtsps",
        "rtp",
        "rtmp",
        "rtmpt",
        "udp",
        "multicast",
        "ws",
        "wss",
    }
)

# Percent-encoded caracteres de control: %00-%1F y %7F.
_PCT_CONTROL_RE = re.compile(r"%(?:0[0-9a-fA-F]|1[0-9a-fA-F]|7[fF])")

_PURPOSE_LABEL = {
    PURPOSE_METADATA: "descargar datos",
    PURPOSE_STREAM: "reproducir",
}


@dataclass(frozen=True)
class UrlParts:
    """URL ya validada.

    .. warning::
       ``raw``/``path`` pueden contener credenciales en el *path* (estilo
       Xtream ``/live/usuario/contraseña/1.ts``). Pásalos por
       :func:`thetvview.security.redaction.redact_text` antes de mostrarlos.
    """

    scheme: str  # esquema efectivo con el que se conecta (http/https)
    host: str
    port: int | None
    path: str
    query: str
    raw: str  # URL original, tal cual se validó
    wrapper: str = ""  # "ffmpeg" si venía envuelta

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"

    @property
    def is_http(self) -> bool:
        return self.scheme == "http"

    @property
    def netloc(self) -> str:
        """Host (entre corchetes si es IPv6) + puerto si no es el por defecto."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port and not (
            (self.scheme == "http" and self.port == 80)
            or (self.scheme == "https" and self.port == 443)
        ):
            return f"{host}:{self.port}"
        return host

    @property
    def normalized(self) -> str:
        """URL reconstruida sin userinfo ni fragmento (para peticiones)."""
        if not self.host:
            return self.raw
        base = f"{self.scheme}://{self.netloc}{self.path}"
        if self.query:
            base += f"?{self.query}"
        if self.wrapper:
            return f"{self.wrapper}://{base}"
        return base


def is_allowed_scheme(scheme: str, purpose: str = PURPOSE_METADATA) -> bool:
    """True si `scheme` está en la allowlist de `purpose`."""
    allowed = ALLOWED_SCHEMES.get(purpose, ALLOWED_SCHEMES[PURPOSE_METADATA])
    return (scheme or "").lower() in allowed


def _reject(message: str) -> "InvalidUrlError":
    # Nunca se interpola la URL en el mensaje: puede llevar la contraseña
    # en el path (Xtream) y el mensaje acaba en la barra de estado.
    return InvalidUrlError(message)


def validate_url(raw: str, purpose: str = PURPOSE_METADATA) -> UrlParts:
    """Valida `raw` contra la política de URL y devuelve sus partes.

    Args:
        raw: URL candidata.
        purpose: ``"metadata"`` (descargas) o ``"stream"`` (reproductor).

    Raises:
        InvalidUrlError: si no cumple la política. El mensaje es apto para
            mostrar al usuario y **nunca** contiene la URL original.
    """
    if purpose not in PURPOSES:
        purpose = PURPOSE_METADATA
    if not isinstance(raw, str):
        raise _reject("La dirección debe ser texto.")
    text = raw.strip()
    if not text:
        raise _reject("La dirección está vacía.")
    if len(text) > MAX_URL_LENGTH:
        raise _reject(
            f"La dirección es demasiado larga (máximo {MAX_URL_LENGTH} caracteres)."
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise _reject("La dirección contiene caracteres de control no permitidos.")
    if _PCT_CONTROL_RE.search(text):
        raise _reject("La dirección contiene caracteres escapados no permitidos.")
    # Antes que el chequeo de espacios: si empieza por «-» el motivo real
    # es la inyección de argumentos en el reproductor.
    if purpose == PURPOSE_STREAM and text.startswith("-"):
        raise _reject(
            "Una dirección de reproducción no puede empezar por «-» "
            "(se interpretaría como una opción del reproductor)."
        )
    if any(ch.isspace() for ch in text):
        raise _reject("La dirección no puede contener espacios.")

    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise _reject("La dirección está mal formada.") from exc

    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise _reject("La dirección no tiene esquema (falta «http://» o «https://»).")

    if scheme == "ffmpeg":
        return _validate_ffmpeg(text, purpose)

    if scheme in REJECTED_SCHEMES:
        raise _reject(
            f"El esquema «{scheme}» nunca se acepta. "
            f"Usa http o https para {_PURPOSE_LABEL[purpose]}."
        )
    if scheme not in ALLOWED_SCHEMES[purpose]:
        allowed = ", ".join(sorted(ALLOWED_SCHEMES[purpose]))
        raise _reject(
            f"El esquema «{scheme}» no está permitido para "
            f"{_PURPOSE_LABEL[purpose]} (permitidos: {allowed})."
        )

    return _finish(text, parts, scheme)


def _finish(text: str, parts: SplitResult, scheme: str) -> UrlParts:
    if parts.username is not None or parts.password is not None:
        raise _reject(
            "La dirección no puede llevar usuario ni contraseña embebidos: "
            "las credenciales se guardan por separado, nunca en la URL."
        )
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise _reject("La dirección tiene un host o un puerto no válidos.") from exc
    if not host:
        raise _reject("La dirección no indica el servidor (host vacío).")
    if port is not None and not (1 <= port <= 65535):
        raise _reject("El puerto de la dirección no es válido.")
    if parts.fragment:
        raise _reject(
            "La dirección no puede llevar un fragmento (#…): "
            "no viaja al servidor y solo añade ambigüedad."
        )
    return UrlParts(
        scheme=scheme,
        host=host.lower(),
        port=port,
        path=parts.path or "/",
        query=parts.query,
        raw=text,
    )


def _validate_ffmpeg(text: str, purpose: str) -> UrlParts:
    """Maneja ``ffmpeg://`` (wrapper interno del reproductor)."""
    if purpose != PURPOSE_STREAM:
        raise _reject(
            "El esquema «ffmpeg» solo sirve para reproducir, no para descargar datos."
        )
    inner = text[len("ffmpeg://"):]
    if not inner:
        raise _reject("El esquema «ffmpeg» necesita una URL o ruta detrás.")
    if inner.lower().startswith("ffmpeg://"):
        raise _reject("No se permite anidar «ffmpeg://» varias veces.")
    if "://" in inner:
        # Caso real: player.py antepone ffmpeg:// a http://host/lista.m3u8
        wrapped = validate_url(inner, purpose=purpose)
        return replace(wrapped, raw=text, wrapper="ffmpeg")
    if inner.startswith("-"):
        raise _reject(
            "Una dirección de reproducción no puede empezar por «-» "
            "(se interpretaría como una opción del reproductor)."
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in inner):
        raise _reject("La dirección contiene caracteres de control no permitidos.")
    if _PCT_CONTROL_RE.search(inner):
        raise _reject("La dirección contiene caracteres escapados no permitidos.")
    # Ruta relativa/absoluta para libavformat: no hay destino de red.
    return UrlParts(
        scheme="ffmpeg",
        host="",
        port=None,
        path=inner,
        query="",
        raw=text,
        wrapper="ffmpeg",
    )
