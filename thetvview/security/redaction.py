"""Redacción de secretos (SDD §16), solo stdlib.

Regla de oro: **ningún** password, token o usuario:password puede llegar a
un log, una excepción mostrada en la TUI, la caché en disco ni a `data/`.

Cobertura:

- query strings con claves sensibles (``password``, ``token``, ``api_key``…)
- userinfo embebido en el netloc (``user:pass@host``)
- paths de stream Xtream (``/live/u/p/123.ts``)
- cabeceras ``Authorization`` / ``Proxy-Authorization`` y esquemas Bearer
- cabeceras ``Cookie`` / ``Set-Cookie``
- pares clave/valor de dicts vía :func:`redact_mapping`
- cualquier texto libre vía :func:`redact_text`
- excepciones vía :func:`redact_exception` (solo el mensaje, nunca args)
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

REDACTED: str = "***"

# Claves de query que nunca deben verse. `user`/`username` NO están aquí:
# el usuario necesita ver su propio usuario, y los tests de compatibilidad
# exigen que `username=u` sobreviva intacto.
_SENSITIVE_QUERY_KEYS: tuple[str, ...] = (
    "password",
    "pass",
    "pwd",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "secret",
    "session",
    "sessionid",
    "key",
)

# `?`/`&`/`;` son el caso típico de query string, pero también limpiamos
# `password=xxx` suelto en texto libre (mensajes de error, trazas, args de
# excepciones). El `\b` evita tocar `notpassword=x`.
_QUERY_RE = re.compile(
    r"(?i)([?&;]?\b(?:" + "|".join(_SENSITIVE_QUERY_KEYS) + r")=)[^&#\s\"']*"
)

# scheme://user:pass@host  →  scheme://***@host  (se conserva el esquema)
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s]+)@")

# /live/u/p/id.ext · /movie/u/p/id.ext · /series/u/p/id.ext (Xtream)
_XTREAM_PATH_RE = re.compile(r"(?i)(/(?:live|movie|series)/)[^/?#]+/[^/?#]+/")

# Authorization: Bearer <jwt>  |  Authorization: Basic <b64>
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(\S+(?:[ \t]+\S+)?)")
_BEARER_RE = re.compile(r"(?i)\b(bearer[ \t]+)[A-Za-z0-9._~+/=\-]{8,}")
_COOKIE_RE = re.compile(r"(?i)\b((?:set-)?cookie\s*[:=]\s*)([^\r\n;]+)")

# Claves de dict cuyo valor se sustituye entero por ***.
_SENSITIVE_MAPPING_KEYS: frozenset[str] = (
    frozenset(_SENSITIVE_QUERY_KEYS)
    | {
        "auth_header",
        "cookie",
        "cookies",
        "credentials",
        "private_key",
    }
) - {"key"}


def redact_text(text: str) -> str:
    """Devuelve `text` sin secretos visibles (best effort, idempotente).

    >>> redact_text("http://x/player_api.php?user=a&password=secret")
    'http://x/player_api.php?user=a&password=***'
    >>> redact_text("rtsp://alice:s3cr3t@cam.local/live")
    'rtsp://***@cam.local/live'
    """
    if not text:
        return text
    out = _QUERY_RE.sub(r"\1" + REDACTED, text)
    out = _USERINFO_RE.sub(r"\1" + REDACTED + "@", out)
    out = _XTREAM_PATH_RE.sub(r"\1" + REDACTED + "/" + REDACTED + "/", out)
    out = _AUTH_HEADER_RE.sub(r"\1" + REDACTED, out)
    out = _BEARER_RE.sub(r"\1" + REDACTED, out)
    out = _COOKIE_RE.sub(r"\1" + REDACTED, out)
    return out


def redact_url(url: str) -> str:
    """Redacta una URL (query, userinfo y path Xtream)."""
    return redact_text(url or "")


def redact_exception(exc: BaseException) -> str:
    """Mensaje de una excepción ya redactado, listo para log o UI."""
    try:
        return redact_text(str(exc))
    except Exception:  # pragma: no cover - defensive
        return type(exc).__name__


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Copia de `data` con valores sensibles sustituidos por `***`.

    Los valores no sensibles de tipo str también pasan por
    :func:`redact_text` (por si contienen una URL con password).
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        if str(key).strip().lower() in _SENSITIVE_MAPPING_KEYS:
            out[key] = REDACTED
        elif isinstance(value, str):
            out[key] = redact_text(value)
        elif isinstance(value, Mapping):
            out[key] = redact_mapping(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [redact_text(v) if isinstance(v, str) else v for v in value]
        else:
            out[key] = value
    return out


def contains_secret(text: str) -> bool:
    """True si `text` todavía conserva algo con pinta de secreto.

    Útil para el security-check (SDD §45) y para tests SEC-001.
    """
    if not text:
        return False
    if _QUERY_RE.search(text):
        return True
    if _USERINFO_RE.search(text):
        return True
    if _XTREAM_PATH_RE.search(text):
        return True
    if _BEARER_RE.search(text):
        return True
    return False


def contains_embedded_login(text: str) -> bool:
    """True si `text` lleva el **login dentro de la propia URL**.

    Distinto de :func:`contains_secret`: aquí sólo entran las credenciales
    que van en el userinfo (``scheme://user:pass@host``) o en el path
    Xtream (``/live/u/p/101.ts``), que es justo lo que las URLs opacas de
    :mod:`thetvview.stream_ref` eliminan.

    Un ``?token=…`` **no** cuenta: es lo único que permite reproducir una
    lista M3U, así que enmascararlo rompería favoritos y recientes sin
    aportar nada.
    """
    if not text:
        return False
    return bool(_USERINFO_RE.search(text) or _XTREAM_PATH_RE.search(text))


#: ``password=``, ``passwd=``, ``pwd=``… en la query. A diferencia del
#: ``?token=`` de un M3U, una URL con la contraseña en la query **nunca**
#: hace falta para reproducir: es exactamente lo que la capa de stream
#: opaca evita, y lo que la URL de archivo de Xtream lleva mientras está
#: en caliente.
_PASSWORD_PARAM_RE = re.compile(
    r"(?i)[?&;\b](?:password|passwd|pwd|pass)=[^&#\s\"']+")


def contains_password_param(text: str) -> bool:
    """True si `text` lleva la contraseña como parámetro de query.

    Se usa, junto con :func:`contains_embedded_login`, en lo que se escribe
    en disco (favoritos y recientes). Es un predicado **más estricto** a
    propósito: si algo se cuela con credenciales, preferimos que el
    favorito se vuelva irreproducible a que la contraseña acabe en
    ``data/``.
    """
    if not text:
        return False
    return bool(_PASSWORD_PARAM_RE.search(text))


def needs_redaction_for_storage(url: str) -> bool:
    """True si `url` lleva credenciales y hay que limpiarla antes de disco.

    La regla de :mod:`thetvview.stream_ref`: lo que circula por el dominio
    son referencias opacas (``xtream://``, ``xtream-ts://``), y eso ya está
    limpio. Este predicado es la red de seguridad para lo que **no** es una
    referencia opaca:

    - login embebido (userinfo o path Xtream), y
    - contraseña como parámetro de query, que es lo que lleva la URL de
      archivo de Xtream mientras está en caliente.

    El ``?token=`` de una lista M3U **no** cuenta: sin él la lista no se
    puede reproducir, así que enmascararlo rompería favoritos y recientes
    sin ganar nada. Preferimos un favorito que se vuelva a limpiar al
    releerlo (queda ``password=***``) antes que una contraseña en disco.
    """
    if not url:
        return False
    return contains_embedded_login(url) or contains_password_param(url)


class SecretStr:
    """Cadena cuyo ``str``/``repr``/``format`` muestran siempre ``***``.

    El valor real solo se obtiene con :meth:`reveal`, que está pensada para
    construir una petición, nunca para imprimir.

    >>> s = SecretStr("hunter2")
    >>> str(s), f"{s}", repr(s)
    ('***', '***', "SecretStr('***')")
    >>> s.reveal()
    'hunter2'
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any = "") -> None:
        self._value = "" if value is None else str(value)

    def reveal(self) -> str:
        """Valor real. Solo para el sitio exacto donde hace falta."""
        return self._value

    def __str__(self) -> str:
        return REDACTED

    def __repr__(self) -> str:
        return f"SecretStr('{REDACTED}')"

    def __format__(self, spec: str) -> str:
        return format(REDACTED, spec) if spec else REDACTED

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretStr):
            return self._value == other._value
        if isinstance(other, str):
            return self._value == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        # La longitud de un password no es un secreto usable por sí sola,
        # pero la ocultamos igual para no dar pistas en reprs de UI.
        return len(REDACTED)

    @classmethod
    def from_any(cls, value: Any) -> "SecretStr":
        if isinstance(value, SecretStr):
            return cls(value.reveal())
        return cls(value)


def redact_iterable(items: Iterable[str]) -> list[str]:
    """Redacta una lista de cadenas (p. ej. argv o cabeceras)."""
    return [redact_text(str(item)) for item in items]
