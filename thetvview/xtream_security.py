"""Utilidades de seguridad para Xtream: redacción, normalización, validación.

Solo stdlib. Nunca loggear password ni URL con password=.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

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
    """Normaliza la URL base del servidor Xtream.

    - Añade esquema si falta (http por defecto).
    - Elimina trailing '/' y path sobrante.
    - Conserva esquema y puerto.
    - http ≠ https (no se fuerza ninguno).
    """
    url = url.strip()
    if not url:
        raise InvalidSourceError("La URL del servidor no puede estar vacía.")

    # Añadir esquema si falta
    if not re.match(r'^https?://', url, re.IGNORECASE):
        url = 'http://' + url

    parsed = urlparse(url)
    if not parsed.hostname:
        raise InvalidSourceError(f"URL del servidor inválida: '{url}'")

    # Reconstruir sin path (solo esquema + host + puerto)
    netloc = parsed.hostname
    if parsed.port and parsed.port not in (80, 443):
        netloc = f"{netloc}:{parsed.port}"

    return urlunparse((parsed.scheme, netloc, '', '', '', ''))


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
    """Construye la URL completa de la API Xtream."""
    base = normalize_server_url(server_url).rstrip('/')
    return f"{base}/player_api.php?username={username}&password={password}&action={action}"


def build_stream_url(
    server_url: str, username: str, password: str,
    stream_id: int | str, content_type: str, extension: str = "ts"
) -> str:
    """Construye URL de stream: /live/u/p/id.ext, /movie/u/p/id.ext, /series/u/p/id.ext."""
    base = normalize_server_url(server_url).rstrip('/')
    return f"{base}/{content_type}/{username}/{password}/{stream_id}.{extension}"


def build_m3u_url(
    server_url: str, username: str, password: str,
    output: str = "ts", m3u_type: str = "m3u_plus",
) -> str:
    """Construye URL M3U (get.php) equivalente para una cuenta Xtream.

    Útil como fallback cuando player_api.php está deshabilitado (404)
    pero get.php sí responde. No expone nada en logs: úsalo con
    redact_secret() al mostrar.
    """
    base = normalize_server_url(server_url).rstrip('/')
    return (
        f"{base}/get.php?username={username}&password={password}"
        f"&type={m3u_type}&output={output}"
    )
