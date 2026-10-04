"""Errores de reproducción normalizados (SDD-M §20, plan Fase 3).

El reproductor muere con un ``returncode`` de FFmpeg y una línea de depuración
en inglés. Eso no es un mensaje para el usuario: «Se cerró al instante (código
1): el stream no llegó a abrir» no dice **qué** pasó ni **qué revisar**.

Aquí se traduce el código a un :class:`StreamErrorCode` con un texto que sí
distingue lo que el §20 del SDD-M enumera —timeout de conexión, 401, 404,
certificado— de lo que no se puede saber. Y distingue también lo que el §14
exige: que el backend no abre el protocolo (no se ofrece) de que el protocolo
funciona y **la red falló** (sí se ofrece, y el consejo es de red).

Todo hereda de :class:`~thetvview.security.errors.IPTVError`, la raíz que la
UI ya captura: así el menú de diagnóstico y la reproducción comparten el mismo
``except`` sin ``except Exception``.

Ningún texto de este módulo incluye la URL: quien lo llame compone el
mensaje final y le pasa la URL **ya redactada** por
:func:`thetvview.security.redaction.redact_text`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ..security.errors import IPTVError

__all__ = [
    "PlayerError",
    "StreamErrorCode",
    "StreamError",
    "classify_returncode",
    "classify_message",
    "explain",
]


class PlayerError(IPTVError):
    """Error amigable al lanzar un reproductor.

    Vive aquí, y no en :mod:`thetvview.player.core`, para que
    :class:`~thetvview.player.router.NoCompatibleBackend` pueda heredarlo sin
    ciclo de imports: ``core`` importa ``router``, ``router`` importa
    ``errors``, y ``errors`` no importa a ninguno de los dos.

    Heredar de :class:`~thetvview.security.errors.IPTVError` es lo que permite
    que la UI capture «lo que falla por reproducción» con el mismo ``except``
    que ya usa para el resto del dominio, sin ``except Exception``.

    ``core.PlayerError`` sigue siendo el mismo objeto: la API pública no cambia.
    """


class StreamErrorCode(str, Enum):
    """Taxonomía de fallos de reproducción (SDD-M §20)."""

    INVALID_URL = "invalid_url"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    CONNECTION_FAILED = "connection_failed"
    CONNECTION_TIMEOUT = "connection_timeout"
    AUTH_FAILED = "auth_failed"
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"
    TLS_FAILED = "tls_failed"
    DECODE_ERROR = "decode_error"
    NETWORK_ERROR = "network_error"
    #: El reproductor se quedó esperando datos. Con UDP/multicast es el síntoma
    #: típico de que el grupo no llega, así que el consejo es de red y no de
    #: canal.
    BUFFERING_TIMEOUT = "buffering_timeout"
    #: El reproductor falló por algo que no es del medio: opciones que no
    #: entendió, una ruta inexistente, un binario que no arranca.
    BACKEND_ERROR = "backend_error"
    PLAYER_MISSING = "player_missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StreamError(IPTVError):
    """Fallo de reproducción, con su código y qué hacer al respecto.

    Es un **valor**, no una excepción de control de flujo: lo devuelve el
    clasificador y lo muestra el informe de diagnóstico. :class:`PlayerError`
    sigue siendo la excepción que ve el menú de reproducción, porque cambiar eso
    rompería los ``except player.PlayerError`` que ya hay en
    :mod:`thetvview.ui.screens`.
    """

    code: StreamErrorCode
    #: Qué pasó, en una frase, sin jerga.
    resumen: str
    #: Qué puede hacer el usuario. Vacío si no hay consejo.
    consejo: str = ""
    #: Texto original del reproductor, **ya redactado** si el llamador lo
    #: redactó. Se conserva para el informe de diagnóstico.
    detalle: str = ""

    def __str__(self) -> str:
        base = self.resumen or DESCRIPCION.get(self.code, self.code.value)
        if self.consejo:
            return f"{base} {self.consejo}"
        return base

    def para_modal(self) -> str:
        """Texto listo para un modal (regla de `AGENTS.md`)."""
        return str(self)


#: Descripción por defecto cuando no hay un resumen más concreto.
DESCRIPCION: dict[StreamErrorCode, str] = {
    StreamErrorCode.INVALID_URL: "La dirección del canal no es válida.",
    StreamErrorCode.UNSUPPORTED_PROTOCOL: (
        "Ningún reproductor instalado abre el tipo de transporte de este canal."
    ),
    StreamErrorCode.CONNECTION_FAILED: "No se pudo conectar con el canal.",
    StreamErrorCode.CONNECTION_TIMEOUT: "La conexión con el canal tardó demasiado.",
    StreamErrorCode.AUTH_FAILED: "El canal rechazó las credenciales.",
    StreamErrorCode.NOT_FOUND: "El canal no existe en esa dirección.",
    StreamErrorCode.SERVER_ERROR: "El servidor del canal falló.",
    StreamErrorCode.TLS_FAILED: "La conexión segura con el canal no es válida.",
    StreamErrorCode.DECODE_ERROR: "El canal se abrió pero no se pudo reproducir.",
    StreamErrorCode.NETWORK_ERROR: "Se perdió la conexión con mitad del directo.",
    StreamErrorCode.PLAYER_MISSING: "No hay ningún reproductor instalado.",
    StreamErrorCode.UNKNOWN: "No se pudo reproducir el canal.",
}

# --- Señales del reproductor -------------------------------------------
# Todo en minúsculas porque los tres binarios escriben distinto:
#   mpv:   "Failed to open 'rtsp://…'"
#   ffmpeg:"Server returned 404 (Not Found)"
#   VLC:   "cannot be opened (404)"

_RE_404 = re.compile(r"\b404\b|not found|no such (?:file|host|url)", re.I)
_RE_403 = re.compile(r"\b403\b|forbidden|access denied", re.I)
_RE_401 = re.compile(r"\b401\b|unauthorized|unauthorised", re.I)
_RE_TLS = re.compile(
    r"certificate|ssl|tls|handshake|cert_verify|x509", re.I
)
_RE_TIMEOUT = re.compile(r"timed? ?out|timeout", re.I)
_RE_REFUSED = re.compile(r"connection refused|network is unreachable|no route|"
                        r"network is down|unreachable", re.I)
_RE_CONN = re.compile(r"connection (?:failed|refused|reset|closed)|could not connect|"
                      r"cannot connect|failed to open|unable to", re.I)
_RE_PROTOCOL = re.compile(r"protocol not (?:found|supported|supported)|"
                          r"unsupported protocol|no such protocol", re.I)
_RE_DECODE = re.compile(r"invalid data found|could not find codec|"
                        r"error while decoding|decoder .* not (?:found|opened)", re.I)
_RE_URL = re.compile(r"invalid url|malformed|url .* (?:invalid|not allowed)|"
                     r"no such file or directory", re.I)


def classify_message(text: str) -> StreamErrorCode | None:
    """Código de error según lo que escribió el reproductor, o None.

    Se mira el **texto**, no sólo el ``returncode``: los tres binarios usan el
    mismo código 1 para un 404 y para un certificado inválido, y la diferencia
    es justo lo que el usuario necesita leer en el modal.

    El orden importa: lo específico antes que lo genérico. Un «Connection
    refused» sobre un RTSP es un **problema de red o de que la cámara está
    apagada**, no «no se puede conectar»: el usuario revisa la cámara.
    """
    if not text:
        return None
    if _RE_TLS.search(text):
        return StreamErrorCode.TLS_FAILED
    if _RE_401.search(text) or _RE_403.search(text):
        return StreamErrorCode.AUTH_FAILED
    # «Protocol not found» va ANTES que «not found»: el patrón de 404 también
    # casa con «not found», y leer un «no conozco este protocolo» como «el
    # canal no existe» es exactamente la confusión que el §14 prohíbe.
    if _RE_PROTOCOL.search(text):
        return StreamErrorCode.UNSUPPORTED_PROTOCOL
    if _RE_404.search(text):
        return StreamErrorCode.NOT_FOUND
    if _RE_TIMEOUT.search(text):
        return StreamErrorCode.CONNECTION_TIMEOUT
    if _RE_REFUSED.search(text):
        return StreamErrorCode.NETWORK_ERROR
    if _RE_DECODE.search(text):
        return StreamErrorCode.DECODE_ERROR
    if _RE_403.search(text):
        return StreamErrorCode.AUTH_FAILED
    if _RE_CONN.search(text):
        return StreamErrorCode.CONNECTION_FAILED
    if _RE_URL.search(text):
        return StreamErrorCode.INVALID_URL
    return None


def classify_returncode(returncode: int | None, detalle: str = "") -> StreamErrorCode:
    """Código de error a partir del ``returncode`` y, si se puede, del texto.

    Sin texto, sólo el código: FFmpeg usa 1 para casi todo, así que
    ``1`` se traduce a ``UNKNOWN`` y no a un connato inventado. Es preferible
    decir «no se pudo reproducir» a mentir con un diagnóstico preciso.
    """
    if returncode is None:
        return StreamErrorCode.UNKNOWN
    por_texto = classify_message(detalle)
    if por_texto is not None:
        return por_texto
    try:
        code = int(returncode)
    except (TypeError, ValueError):
        return StreamErrorCode.UNKNOWN
    if code == 0:
        return StreamErrorCode.UNKNOWN
    # 127/126 y 2: binario no encontrado o no ejecutable (shell de Unix).
    if code in (126, 127):
        return StreamErrorCode.PLAYER_MISSING
    return StreamErrorCode.UNKNOWN


# --- Consejos por código ------------------------------------------------

_CONSEJOS: dict[StreamErrorCode, str] = {
    StreamErrorCode.AUTH_FAILED: (
        "Si es una cámara IP, revisa el usuario y la contraseña; si es un "
        "proveedor, es probable que la línea haya caducado."
    ),
    StreamErrorCode.NOT_FOUND: (
        "La línea de la lista ya no apunta a nada. Prueba otro canal."
    ),
    StreamErrorCode.CONNECTION_TIMEOUT: (
        "Suele ser la red: prueba con otro canal para ver si es del servidor "
        "o de tu conexión."
    ),
    StreamErrorCode.NETWORK_ERROR: (
        "Esto es de red, no del canal: comprueba que la cámara o el servidor "
        "esté encendido, que la ruta llegue y que el firewall no lo bloquee."
    ),
    StreamErrorCode.TLS_FAILED: (
        "La conexión segura falló. Si es una cámara con certificado propio, "
        "habrá que revisarlo: no se acepta un certificado no válido."
    ),
    StreamErrorCode.CONNECTION_FAILED: (
        "Prueba con otro canal para saber si es del proveedor o de tu conexión."
    ),
    StreamErrorCode.DECODE_ERROR: (
        "Se abrió pero no se pudo reproducir: el proveedor puede estar "
        "sirviendo una línea caducada o corrupta."
    ),
    StreamErrorCode.BUFFERING_TIMEOUT: (
        "Se abrió el canal pero no llegaron datos. Si es UDP o multicast, "
        "comprueba que el grupo llegue a tu red."
    ),
    StreamErrorCode.BACKEND_ERROR: (
        "El reproductor falló por su cuenta, no por el canal. Prueba con otro."
    ),
    StreamErrorCode.BUFFERING_TIMEOUT: (
        "Se abrió el canal pero no llegaron datos. Si es UDP o multicast, "
        "comprueba que el grupo llegue a tu red."
    ),
    StreamErrorCode.BACKEND_ERROR: (
        "El reproductor falló por su cuenta, no por el canal. Prueba con otro."
    ),
    StreamErrorCode.PLAYER_MISSING: (
        "Instala mpv, VLC o mplayer y vuelve a intentarlo."
    ),
    StreamErrorCode.UNKNOWN: (
        "No se pudo reproducir el canal. Prueba con otro para saber si el "
        "problema es del canal o de la conexión."
    ),
}


def explain(
    returncode: int | None,
    detalle: str = "",
    *,
    transporte_nombre: str = "",
) -> StreamError:
    """Construye el :class:`StreamError` final, con resumen y consejo.

    ``transporte_nombre`` («RTSP», «UDP») se usa en el caso de protocolo no
    soportado: el §14 del SDD-M pide exactamente que ese mensaje sea distinto
    del de un fallo de red, porque lo que hay que revisar es distinto.
    """
    codigo = classify_returncode(returncode, detalle)
    resumen = DESCRIPCION.get(codigo, codigo.value)
    consejo = _CONSEJOS.get(codigo, "")
    if codigo is StreamErrorCode.UNSUPPORTED_PROTOCOL and transporte_nombre:
        resumen = (
            f"Ningún reproductor instalado abre {transporte_nombre}, así que "
            "este canal no se puede reproducir aquí."
        )
        consejo = (
            "No es un fallo del canal: es de los reproductores instalados. "
            "Prueba otro canal o instala otro reproductor."
        )
    return StreamError(
        code=codigo, resumen=resumen, consejo=consejo, detalle=detalle
    )