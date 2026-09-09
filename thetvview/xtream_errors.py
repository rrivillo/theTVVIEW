"""Jerarquía de errores para proveedores Xtream (solo stdlib).

Cada subclase permite distinguir el tipo de fallo en la TUI y en tests
sin depender de strings ni códigos HTTP crudos.
"""


class ProviderError(Exception):
    """Error base de cualquier operación con un proveedor IPTV."""


class AuthenticationError(ProviderError):
    """Credenciales inválidas o expiradas (401/403 del servidor)."""


class InvalidSourceError(ProviderError):
    """Fuente no válida: URL malformada, respuesta inesperada, etc."""


class NetworkError(ProviderError):
    """Fallo de conexión, timeout, DNS, etc."""


class RateLimitError(ProviderError):
    """El servidor pide esperar (429 Too Many Requests)."""


class UnsupportedProviderError(ProviderError):
    """El servidor no es un proveedor Xtream compatible."""


def friendly_message(exc: BaseException) -> str:
    """Devuelve un mensaje entendible (sin códigos HTTP) para la TUI.

    Regla: ningún mensaje para el usuario incluye "HTTP 512", "401", etc.
    Esos códigos se quedan en el log/traceback (`__cause__`), nunca en
    la barra de estado. Cada mensaje dice qué pasó y qué hacer.
    """
    import re

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
    if isinstance(exc, UnsupportedProviderError):
        return (
            "Ese servidor no parece Xtream (no ofrece la API esperada). "
            "Prueba a añadirlo como lista M3U con su enlace get.php."
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
