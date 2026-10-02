"""Paquete de seguridad de theTVVIEW (solo stdlib, sin dependencias pip).

Submódulos:

- ``errors``      taxonomía de errores (SDD §27)
- ``limits``      límites de red/tamaño/peticiones (SDD §19/21)
- ``url_policy``  validación de esquema/host/puerto/longitud (SDD §10)
- ``ssrf``        bloqueo de redes privadas y DNS rebinding (SDD §11)
- ``redaction``   redacción de secretos en texto/URLs/excepciones (SDD §16)
- ``secrets``     almacén de contraseñas vía keyring del SO (SDD §15)
- ``safe_http``   único path de red permitido (SDD §9/12/13/18/19/41)
- ``xml_safe``    parser XML sin XXE ni billion laughs (SDD §38/39)
- ``local_files`` permisos privados (0600/0700) de ficheros locales (SDD §21)
- ``check``       security-check ejecutable en TUI y en CI (SDD §45)
"""

from __future__ import annotations

from .errors import (
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
    "InvalidSourceError",
    "InvalidUrlError",
    "NetworkError",
    "TLSValidationError",
    "SSRFBlockedError",
    "ResponseTooLargeError",
    "RateLimitError",
    "ConnectionLimitError",
    "AuthenticationError",
    "AccountExpiredError",
    "AccountDisabledError",
    "UnsupportedProviderError",
    "ParseError",
]
