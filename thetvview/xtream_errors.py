"""Errores Xtream: re-export de la taxonomía central + mensajes amigables.

El árbol de excepciones vive en :mod:`thetvview.security.errors` (SDD §27).
Este módulo se queda como fachada para no romper los cientos de imports y
tests existentes (`from thetvview.xtream_errors import ...`).

`friendly_message()` es copia de la TUI (nunca muestra códigos HTTP ni
rastros de secretos): traduce cualquier excepción a una frase que el
usuario puede entender y en la que puede actuar.
"""

from __future__ import annotations

import re

from .security.errors import (
    AccountDisabledError,
    AccountExpiredError,
    AuthenticationError,
    ConnectionLimitError,
    InvalidSourceError,
    InvalidUrlError,
    IPTVError,
    NetworkError,
    ParseError,
    ProviderError,
    RateLimitError,
    ResponseTooLargeError,
    SSRFBlockedError,
    TLSValidationError,
    UnsupportedProviderError,
)

__all__ = [
    "IPTVError",
    "ProviderError",
    "AuthenticationError",
    "AccountExpiredError",
    "AccountDisabledError",
    "InvalidSourceError",
    "InvalidUrlError",
    "NetworkError",
    "TLSValidationError",
    "SSRFBlockedError",
    "ResponseTooLargeError",
    "RateLimitError",
    "ConnectionLimitError",
    "ParseError",
    "UnsupportedProviderError",
    "friendly_message",
]


def friendly_message(exc: BaseException) -> str:
    """Devuelve un mensaje entendible (sin códigos HTTP) para la TUI.

    Regla: ningún mensaje para el usuario incluye "HTTP 512", "401", etc.
    Esos códigos se quedan en el log/traceback (`__cause__`), nunca en
    la barra de estado. Cada mensaje dice qué pasó y qué hacer.
    """
    # --- Bloqueos de seguridad (subclases de NetworkError: van primero) -----
    if isinstance(exc, SSRFBlockedError):
        return (
            "Esa dirección apunta a una red interna o privada y la app la "
            "bloquea por seguridad. Si es tu propio servidor, añade la fuente "
            "aceptando el aviso de red privada."
        )
    if isinstance(exc, ResponseTooLargeError):
        return (
            "El servidor devolvió una respuesta demasiado grande y la app la "
            "detuvo para no agotar la memoria. Prueba más tarde o revisa la fuente."
        )
    if isinstance(exc, TLSValidationError):
        return (
            "La conexión no es segura: el certificado del servidor no es válido. "
            "No se envió ninguna credencial. Revisa la dirección o usa https."
        )
    if isinstance(exc, AccountDisabledError):
        return (
            "Tu cuenta está desactivada o bloqueada por el proveedor. "
            "Contacta con tu proveedor para recuperar el acceso."
        )
    if isinstance(exc, AccountExpiredError):
        return (
            "Tu cuenta ha caducado. Contacta con tu proveedor para renovarla."
        )
    if isinstance(exc, AuthenticationError):
        return (
            "Usuario o contraseña incorrectos: el servidor rechazó el acceso. "
            "Revisa usuario y contraseña o pide ayuda a tu proveedor."
        )
    if isinstance(exc, RateLimitError):
        return (
            "Demasiados intentos seguidos: el servidor pide esperar un momento "
            "antes de reintentar."
        )
    if isinstance(exc, ConnectionLimitError):
        return (
            "Hay demasiadas peticiones en marcha a la vez. "
            "Espera unos segundos y vuelve a intentarlo."
        )
    if isinstance(exc, UnsupportedProviderError):
        return (
            "Ese servidor no parece Xtream (no ofrece la API esperada). "
            "Prueba a añadirlo como lista M3U con su enlace get.php."
        )
    if isinstance(exc, ParseError):
        return (
            "El archivo no se puede leer: su contenido no es válido o está "
            "dañado. Revisa la ruta o pide una copia nueva a tu proveedor."
        )
    if isinstance(exc, NetworkError):
        raw = str(exc)
        low = raw.lower()
        if "tiempo de espera" in low or "timeout" in low:
            return (
                "El servidor tarda demasiado en responder. "
                "Comprueba tu internet e inténtalo de nuevo."
            )
        if "conectar" in low or "connection" in low or "dns" in low:
            return (
                "No se pudo conectar con el servidor. "
                "Revisa tu internet y que la dirección sea correcta."
            )
        if "servidor" in low:
            return (
                "El servidor está fallando ahora mismo. "
                "Espera unos minutos e inténtalo de nuevo."
            )
        return (
            "Problema de conexión con el servidor. "
            "Revisa tu internet e inténtalo de nuevo."
        )
    if isinstance(exc, InvalidUrlError):
        detail = str(exc).strip()
        if detail:
            msg = detail[0].upper() + detail[1:] if len(detail) > 1 else detail
            if not msg.endswith("."):
                msg += "."
            return f"{msg} Corrige la dirección e inténtalo de nuevo."
        return (
            "La dirección no es válida. "
            "Corrige la dirección e inténtalo de nuevo."
        )
    if isinstance(exc, InvalidSourceError):
        return (
            "La respuesta del servidor no se entiende (datos inesperados). "
            "Revisa la dirección o avisa a tu proveedor."
        )
    if isinstance(exc, ProviderError):
        # Fallback genérico: sanea cualquier "HTTP 512" residual.
        cleaned = re.sub(r"\s*\(?\bHTTP\s*\d{3}\)?\.?", "", str(exc), flags=re.IGNORECASE).strip()
        if cleaned:
            # Capitalizar y asegurar punto final sin código.
            msg = cleaned[0].upper() + cleaned[1:] if len(cleaned) > 1 else cleaned
            if not msg.endswith("."):
                msg += "."
            return f"{msg} Si se repite, revisa los datos o tu conexión."
        return (
            "No se pudo completar la operación con el servidor. "
            "Inténtalo de nuevo en un momento."
        )
    # Error inesperado (no ProviderError): nunca mostrar traceback ni códigos.
    return (
        "Ocurrió un problema al abrir la lista. "
        "Inténtalo de nuevo y revisa los datos de acceso."
    )
