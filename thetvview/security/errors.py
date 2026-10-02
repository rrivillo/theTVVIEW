"""Taxonomía de errores de seguridad (SDD §27), solo stdlib.

Todos los errores del dominio comparten raíz en :class:`IPTVError` para que
la TUI pueda capturar "lo que falla por seguridad" sin `except Exception`.

Compatibilidad deliberada con el código previo:

- :class:`NetworkError` (y por tanto sus subclases ``TLSValidationError``,
  ``SSRFBlockedError`` y ``ResponseTooLargeError``) hereda también de
  ``OSError``: así los `except (OSError, ValueError)` que ya existen en
  ``m3u_parser``, ``epg_parser`` y en la TUI siguen capturándolos sin cambiar.
- :class:`ParseError` hereda de ``ValueError`` por la misma razón
  (el parser XMLTV ya informaba de XML inválido con ``ValueError``).
- ``AuthenticationError``, ``InvalidSourceError``, ``NetworkError``,
  ``RateLimitError`` y ``UnsupportedProviderError`` siguen siendo subclases
  de :class:`ProviderError`, como exigen los tests Xtream existentes.
"""

from __future__ import annotations


class IPTVError(Exception):
    """Raíz de todos los errores controlados de theTVVIEW."""


class ProviderError(IPTVError):
    """Error base de cualquier operación con un proveedor IPTV."""


class InvalidSourceError(ProviderError):
    """Fuente no válida: URL malformada, respuesta inesperada, etc."""


class InvalidUrlError(InvalidSourceError):
    """La URL no pasa la política de esquema/host/puerto (SDD §10)."""


class NetworkError(ProviderError, OSError):
    """Fallo de conexión, timeout, DNS, etc.

    Hereda de ``OSError`` a propósito: la mayor parte del código de red
    preexistente ya propagaba ``OSError`` con mensaje amigable.
    """


class TLSValidationError(NetworkError):
    """La conexión TLS no superó la validación (certificado/host)."""


class SSRFBlockedError(NetworkError):
    """Petición bloqueada por la política anti-SSRF (red privada/interna)."""


class ResponseTooLargeError(NetworkError):
    """El servidor superó el límite de bytes permitido para una respuesta."""


class RateLimitError(ProviderError):
    """El servidor pide esperar (429 Too Many Requests)."""


class ConnectionLimitError(ProviderError):
    """Demasiadas peticiones concurrentes hacia el mismo destino."""


class AuthenticationError(ProviderError):
    """Credenciales inválidas o rechazadas (401/403 o login=false)."""


class AccountExpiredError(AuthenticationError):
    """La cuenta existe pero su fecha de caducidad ya pasó."""


class AccountDisabledError(AuthenticationError):
    """La cuenta existe pero está desactivada/baneada por el proveedor."""


class UnsupportedProviderError(ProviderError):
    """El servidor no es un proveedor Xtream compatible."""


class ParseError(IPTVError, ValueError):
    """Documento incomprensible (XML/M3U corrupto o con XXE bloqueado)."""
