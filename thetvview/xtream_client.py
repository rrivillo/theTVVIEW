"""Cliente HTTP síncrono para la API Xtream (solo stdlib).

Nunca usa requests ni async. Siempre timeout. User-Agent identificable.
Mapea errores HTTP a ProviderError y sus subclases.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

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


def _make_request(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict | list:
    """Realiza GET y devuelve JSON parseado. Lanza ProviderError en fallo."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code in (401, 403):
            raise AuthenticationError(
                "Usuario o contraseña incorrectos: el servidor rechazó el acceso."
            ) from exc
        if code == 429:
            raise RateLimitError(
                "Demasiadas solicitudes. Espera un momento y vuelve a intentar."
            ) from exc
        if code == 404 and "player_api.php" in url:
            raise UnsupportedProviderError(
                "El servidor no expone player_api.php. "
                "Puede no ser Xtream o tener la API deshabilitada. "
                "Prueba como lista M3U: <servidor>/get.php?username=<u>&password=<p>&type=m3u_plus&output=ts"
            ) from exc
        if code == 512:
            # Código no estándar que algunos paneles usan para login inválido
            # (p. ej. flexlive.lat devuelve {"login":false,...} con 512).
            # No se expone el "512" al usuario: es AuthenticationError y la UI
            # muestra "usuario o contraseña incorrectos". El código queda en
            # __cause__ para debug.
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if "login" in body.lower() or "invalid" in body.lower():
                raise AuthenticationError(
                    "Usuario o contraseña incorrectos: el servidor rechazó el acceso."
                ) from exc
            raise AuthenticationError(
                "Usuario o contraseña incorrectos: el servidor denegó el acceso. "
                "Verifica usuario y contraseña."
            ) from exc
        if code >= 500:
            raise NetworkError(
                "El servidor está fallando ahora mismo. Inténtalo más tarde."
            ) from exc
        raise ProviderError(
            "El servidor devolvió una respuesta inesperada. Inténtalo más tarde."
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise NetworkError(
                f"Tiempo de espera agotado ({timeout:g}s)."
            ) from exc
        raise NetworkError(
            f"No se pudo conectar al servidor: {reason}"
        ) from exc
    except TimeoutError as exc:
        raise NetworkError(
            f"Tiempo de espera agotado ({timeout:g}s)."
        ) from exc

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidSourceError(
            "La respuesta del servidor no es JSON válido."
        ) from exc

    return data


def api_call(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict | list:
    """Llamada GET a la API Xtream con parseo JSON.

    Devuelve el dict/list resultante. Lanza ProviderError en cualquier fallo.
    """
    return _make_request(url, timeout)
