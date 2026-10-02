"""Cliente HTTP síncrono para la API Xtream (solo stdlib).

Nunca usa requests ni async. Siempre timeout. User-Agent identificable.
Mapea errores HTTP a ProviderError y sus subclases.

La red pasa SIEMPRE por :class:`thetvview.security.safe_http.SafeHttpClient`
(gaps B4/B5): política de URL, anti-SSRF, redirects revalidados, TLS
estricto y lectura acotada. Aquí solo se traduce el **status** HTTP al
dominio Xtream.
"""

from __future__ import annotations

import json

from .security.limits import get_limits
from .security.safe_http import SafeHttpClient
from .security.url_policy import PURPOSE_METADATA
from .xtream_errors import (
    AuthenticationError,
    InvalidSourceError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnsupportedProviderError,
)

DEFAULT_TIMEOUT: float = 10.0
USER_AGENT: str = "theTVVIEW/1.0"


def _status_error(code: int, reason: str, body: bytes, url: str) -> ProviderError | None:
    """Traduce un status HTTP al error de dominio Xtream (None si es 2xx)."""
    if 200 <= code < 300:
        return None
    if code in (401, 403):
        return AuthenticationError(
            "Usuario o contraseña incorrectos: el servidor rechazó el acceso."
        )
    if code == 429:
        return RateLimitError(
            "Demasiadas solicitudes. Espera un momento y vuelve a intentar."
        )
    if code == 404 and "player_api.php" in url:
        return UnsupportedProviderError(
            "El servidor no expone player_api.php. "
            "Puede no ser Xtream o tener la API deshabilitada. "
            "Prueba como lista M3U: <servidor>/get.php?username=<u>&password=<p>&type=m3u_plus&output=ts"
        )
    if code == 512:
        # Código no estándar que algunos paneles usan para login inválido
        # (p. ej. flexlive.lat devuelve {"login":false,...} con 512).
        # No se expone el "512" al usuario: es AuthenticationError y la UI
        # muestra "usuario o contraseña incorrectos". El código queda en
        # __cause__ para debug.
        text = body.decode("utf-8", errors="replace").lower()
        if "login" in text or "invalid" in text:
            return AuthenticationError(
                "Usuario o contraseña incorrectos: el servidor rechazó el acceso."
            )
        return AuthenticationError(
            "Usuario o contraseña incorrectos: el servidor denegó el acceso. "
            "Verifica usuario y contraseña."
        )
    if code >= 500:
        return NetworkError(
            "El servidor está fallando ahora mismo. Inténtalo más tarde."
        )
    detail = _short(reason)
    return ProviderError(
        f"El servidor devolvió una respuesta inesperada (HTTP {code}"
        + (f" {detail}" if detail else "")
        + "). Inténtalo más tarde."
    )


def _short(text: str, limit: int = 60) -> str:
    text = (text or "").strip()
    return text[: limit - 3] + "..." if len(text) > limit else text


def _make_request(url: str, timeout: float = DEFAULT_TIMEOUT, *,
                  allow_private: bool = False) -> dict | list:
    """Realiza GET y devuelve JSON parseado. Lanza ProviderError en fallo.

    ``allow_private`` habilita paneles en red privada/loopback solo cuando
    la fuente lo tiene declarado (SDD §11); por defecto está bloqueado.
    """
    client = SafeHttpClient(
        allow_private=allow_private,
        purpose=PURPOSE_METADATA,
        user_agent=USER_AGENT,
        # Listas Xtream de 200k entradas son JSON de decenas de MB: el
        # tope es el mismo que el de leer un fichero (max_file_bytes).
        max_bytes=get_limits().max_file_bytes,
    )
    resp = client.request(url, timeout=timeout)

    error = _status_error(resp.status, resp.reason, resp.body, url)
    if error is not None:
        raise error

    try:
        data = json.loads(resp.body.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidSourceError(
            "La respuesta del servidor no es JSON válido."
        ) from exc

    # Tope de entradas: un panel que devuelve un millón de canales no debe
    # reventar la memoria (SDD §19/§21).
    max_entries = get_limits().max_entries
    if isinstance(data, list) and len(data) > max_entries:
        raise InvalidSourceError(
            f"El servidor devolvió más de {max_entries} elementos y se corta "
            "la respuesta por seguridad."
        )

    return data


def api_call(url: str, timeout: float = DEFAULT_TIMEOUT, *,
             allow_private: bool = False) -> dict | list:
    """Llamada GET a la API Xtream con parseo JSON.

    Devuelve el dict/list resultante. Lanza ProviderError en cualquier fallo.
    """
    return _make_request(url, timeout, allow_private=allow_private)
