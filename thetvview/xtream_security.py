"""Utilidades de seguridad para Xtream: redacción, normalización, validación.

Solo stdlib. Nunca loggear password ni URL con password=.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, urlunparse

from .xtream_errors import InvalidSourceError


def redact_secret(text: str) -> str:
    """Reemplaza passwords en URLs tipo ?password=xxx por '***'.

    >>> redact_secret("http://x.com/player_api.php?user=a&password=secret123")
    'http://x.com/player_api.php?user=a&password=***'
    """
    return re.sub(
        r'(password=)[^&]*',
        r'\1***',
        text,
        flags=re.IGNORECASE,
    )


def normalize_server_url(url: str) -> str:
    """Normaliza y **valida** la URL base del servidor Xtream (B2, SDD §10).

    - Añade ``http://`` si el usuario escribió sólo ``host[:puerto][/path]``.
    - Rechaza cualquier otro esquema (``file:``, ``ftp:``, ``javascript:``…)
      en vez de meterlo dentro de ``http://`` y silenciarlo.
    - Rechaza usuario/contraseña embebidos: van en el formulario.
    - Elimina path, query y fragmento; conserva esquema y puerto.
    - Devuelve sólo tras pasar por ``url_policy.validate_url``.
    - http ≠ https (no se fuerza ninguno).

    Raises:
        InvalidSourceError: la dirección no es un servidor Xtream válido.
            El mensaje nunca lleva credenciales.
    """
    from .security.redaction import redact_text
    from .security.url_policy import PURPOSE_METADATA, validate_url

    url = url.strip()
    if not url:
        raise InvalidSourceError("La URL del servidor no puede estar vacía.")

    if re.match(r"^https?://", url, re.IGNORECASE):
        pass
    else:
        scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", url)
        if scheme_match:
            # "host:8080" es un puerto; "javascript:alert(1)" es un esquema.
            after = url[scheme_match.end():]
            port_part = after.split("/", 1)[0]
            if not port_part.isdigit():
                raise InvalidSourceError(
                    f"Sólo se aceptan servidores http:// o https:// "
                    f"(no «{scheme_match.group(1)}:»)."
                )
        url = "http://" + url

    try:
        parsed = urlparse(url)
        if parsed.username is not None or parsed.password is not None:
            raise InvalidSourceError(
                "La dirección del servidor no puede llevar usuario ni "
                "contraseña: se piden por separado en el formulario."
            )
        if not parsed.hostname:
            raise InvalidSourceError(
                f"URL del servidor inválida: '{redact_text(url)}'"
            )
        port = parsed.port
        netloc = parsed.hostname
        if port and port not in (80, 443):
            netloc = f"{netloc}:{port}"
        normalized = urlunparse((parsed.scheme, netloc, "", "", "", ""))
        validate_url(normalized, purpose=PURPOSE_METADATA)
    except InvalidSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - puerto/host mal formados
        raise InvalidSourceError(
            f"URL del servidor inválida: {redact_text(str(exc))}"
        ) from exc
    return normalized

def validate_credentials(server_url: str, username: str, password: str = "") -> list[str]:
    """Valida campos obligatorios. Devuelve lista de errores (vacía = OK)."""
    errors: list[str] = []
    if not server_url or not server_url.strip():
        errors.append("La URL del servidor es obligatoria.")
    if not username or not username.strip():
        errors.append("El nombre de usuario es obligatorio.")
    # password puede ser vacío en algunos proveedores
    return errors


def build_api_url(server_url: str, username: str, password: str, action: str) -> str:
    """Construye la URL completa de la API Xtream.

    Usuario y password van con :func:`urllib.parse.quote` (gap B9): un
    ``&`` o un ``=`` en la contraseña no pueden inyectar parámetros ni
    romper la estructura de la query.
    """
    base = normalize_server_url(server_url).rstrip('/')
    user = quote(username, safe="")
    pwd = quote(password, safe="")
    return f"{base}/player_api.php?username={user}&password={pwd}&action={action}"


def build_stream_url(
    server_url: str, username: str, password: str,
    stream_id: int | str, content_type: str, extension: str = "ts"
) -> str:
    """Construye URL de stream: /live/u/p/id.ext, /movie/u/p/id.ext, /series/u/p/id.ext.

    Los segmentos van percent-encoded (gap B9): una contraseña con ``/``
    no puede convertirse en tramos extra de path ni salirse de ``/live``.
    """
    base = normalize_server_url(server_url).rstrip('/')
    user = quote(username, safe="")
    pwd = quote(password, safe="")
    return f"{base}/{content_type}/{user}/{pwd}/{stream_id}.{extension}"


#: Formato del parámetro ``start`` de ``timeshift.php``: es la convención
#: de facto de los paneles Xtream Codes (``YYYY-MM-DD:HH-MM-SS``). No es un
#: estándar, así que vive en una constante y aquí: si algún panel usa otra
#: sintaxis, se cambia en un único sitio (la app nunca lo adivina ni lo
#: sondea).
#:
#: El panel interpreta el sello en **su** zona horaria y la app se lo manda
#: en la **local** del cliente. En la práctica coinciden (el usuario ve la
#: televisión desde el mismo huso que el panel), pero si no fuera así el
#: panel devolvería el programa equivocado: es un límite del mecanismo
#: declarado, asumido y documentado en el README, no algo que se pueda
#: detectar sin sondear el servidor.
TIMESHIFT_TIME_FORMAT = "%Y-%m-%d:%H-%M-%S"


def build_timeshift_url(
    server_url: str, username: str, password: str,
    stream_id: int | str,
    start_epoch: int | str,
    duration_seconds: int | str,
    extension: str = "ts",
) -> str:
    """Construye la URL de **archivo** del panel: ``/streaming/timeshift.php``.

    Es el mecanismo **declarado** por Xtream Codes para catch-up, y sólo
    se construye cuando el proveedor lo ha declarado para ese canal
    (`tv_archive >= 1` **y** `tv_archive_duration > 0`); de eso se encarga
    `thetvview.catchup`, que valida antes de construir. Aquí no se decide
    nada: sólo se traduce el instante y se compone la query.

    Usuario, contraseña y `stream_id` van percent-encoded con
    ``quote(..., safe="")`` (gap B9), igual que en `build_stream_url`: un
    ``&`` o un ``=`` en la contraseña no puede inyectar parámetros.

    Args:
        start_epoch: instante de inicio en epoch **UTC**, en segundos
            (admite *seek*: no tiene por qué ser el comienzo del programa).
        duration_seconds: duración pedida en segundos.
        extension: extensión que espera el panel (`ts`, `mp4`...).

    Returns:
        URL con credenciales. **No** la registres ni la muestres: es el
        equivalente en caliente de `StreamRef.resolve`.
    """
    base = normalize_server_url(server_url).rstrip('/')
    user = quote(username, safe="")
    pwd = quote(password, safe="")
    stream = quote(str(stream_id), safe="")
    ext = quote(str(extension), safe="")
    try:
        seconds = int(start_epoch)
    except (TypeError, ValueError) as exc:
        raise InvalidSourceError("El instante de archivo no es válido.") from exc
    start = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
    try:
        duration = max(1, int(duration_seconds))
    except (TypeError, ValueError) as exc:
        raise InvalidSourceError("La duración de archivo no es válida.") from exc
    return (
        f"{base}/streaming/timeshift.php?username={user}&password={pwd}"
        f"&stream={stream}&start={start.strftime(TIMESHIFT_TIME_FORMAT)}"
        f"&duration={duration}&extension={ext}"
    )


def build_m3u_url(
    server_url: str, username: str, password: str,
    output: str = "ts", m3u_type: str = "m3u_plus",
) -> str:
    """Construye URL M3U (get.php) equivalente para una cuenta Xtream.

    Útil como fallback cuando player_api.php está deshabilitado (404)
    pero get.php sí responde. No expone nada en logs: úsalo con
    redact_secret() al mostrar. Credenciales percent-encoded (gap B9).
    """
    base = normalize_server_url(server_url).rstrip('/')
    user = quote(username, safe="")
    pwd = quote(password, safe="")
    return (
        f"{base}/get.php?username={user}&password={pwd}"
        f"&type={m3u_type}&output={output}"
    )
