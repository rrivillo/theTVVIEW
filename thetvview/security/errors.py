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
    """Error base de cualquier operación con un proveedor IPTV.

    **Atributo ``status`` (F1-bis).** Vive aquí y no en :class:`NetworkError`
    porque no es sólo la red la que sabe un código HTTP: ``RateLimitError`` es un
    429 del servidor y no hereda de ``OSError``, y sin el atributo declarado la
    presentación del error no podría leerlo sin ``getattr`` a ciegas. Se declara
    como atributo de clase con valor por defecto (no campo del constructor) para
    que siga siendo retrocompatible: ``ProviderError(msg)`` se sigue
    construyendo igual, y quien no lo fije lee ``None``.
    """

    #: Código HTTP con el que respondió el proveedor, o ``None`` si el fallo no
    #: fue una respuesta (DNS, timeout, validación local…). El valor real se fija
    #: **en la instancia** (``exc.status = 429``), nunca en la clase.
    status: int | None = None


class InvalidSourceError(ProviderError):
    """Fuente no válida: URL malformada, respuesta inesperada, etc."""


class InvalidUrlError(InvalidSourceError):
    """La URL no pasa la política de esquema/host/puerto (SDD §10)."""


class NetworkError(ProviderError, OSError):
    """Fallo de conexión, timeout, DNS, etc.

    Hereda de ``OSError`` a propósito: la mayor parte del código de red
    preexistente ya propagaba ``OSError`` con mensaje amigable.

    **Estado estructurado (F1-bis).** El mensaje es prosa y la prosa cambia; la
    UI necesita el dato, no la frase. ``status`` —heredado de
    :class:`ProviderError`— es el código HTTP cuando el fallo fue una respuesta,
    y ``reason`` es el motivo de red cuando fue el transporte, para que la
    presentación del error pueda **clasificar por atributo** en vez de por
    *string matching*.

    ``reason`` es un **atributo de clase** con valor por defecto, no un campo
    del constructor: añadirlo así es retrocompatible (se lee igual de un
    ``NetworkError(msg)`` que de uno construido en otro sitio) y ninguna
    subclase —``TLSValidationError``, ``SSRFBlockedError``,
    ``ResponseTooLargeError``— lo define, así que none colisiona. El valor se
    fija **en la instancia** al crearla (``exc.reason = "dns"``), nunca en la
    clase, que es sólo el valor por defecto.
    """

    #: Motivo del fallo de transporte: ``"tls"``, ``"dns"``, ``"timeout"``,
    #: ``"refused"``, ``"unreachable"``, ``"connection"`` o ``None``.
    #: Vocabulario cerrado a propósito: si mañana hace falta uno más se añade
    #: aquí, no se adivina leyendo el mensaje.
    reason: str | None = None


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
