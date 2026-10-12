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
    "DESCRIPCION",
    "NO_REINTENTABLE",
    "classify_returncode",
    "classify_message",
    "explain",
    "describe_observed",
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
    """Taxonomía de fallos de reproducción (SDD-M §20,ampliada por §5.1 del SDD-P1).

    Los catorce primeros son los que el proyecto ya usaba y **no se renombran
    ni se borran**: `tests/test_player.py` y `tests/test_stream_errors.py`
    dependen de ellos por nombre. Los que siguen se **agregan** para cubrir la
    lista del §5.1 del SDD de P1 (FR-REC-003).

    Criterio para añadir, no para multiplicar: un código nuevo describe una
    situación que el usuario puede reconocer y sobre la que puede actuar. Un
    matiz que no cambia el consejo no merece un código: ese detalle va en el
    `resumen`, que es texto libre.
    """

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

    # --- Códigos añadidos por el SDD de P1 (FR-REC-003) ---------------------

    #: El nombre del servidor no resuelve. Distinto de «conexión rechazada»:
    #: aquí ni siquiera hay servidor al que conectarse.
    DNS = "dns"
    #: 429. Es el único código al que tiene sentido respetar un `Retry-After`.
    HTTP_RATE_LIMIT = "http_rate_limit"
    #: Llegó la respuesta pero no es un manifiesto válido (HLS/DASH mal formado).
    MANIFEST_INVALID = "manifest_invalid"
    #: El demuxer no pudo leer el flujo: llegó algo, pero no se pudo descomprimir.
    MEDIA_DEMUX = "media_demux"
    #: El motor no tiene el códec. Reintentarlo cinco veces no cambia nada.
    CODEC_UNSUPPORTED = "codec_unsupported"
    #: La pista elegida (audio, subtítulo o calidad) no existe o no se pudo aplicar.
    TRACK_SELECTION = "track_selection"
    #: El motor arrancó pero no llegó a reproducir nada.
    PLAYER_STARTUP = "player_startup"
    #: El motor se fue por su cuenta, sin código que sirva.
    PLAYER_EXIT = "player_exit"
    #: Se agotó el tiempo de espera en alguna fase.
    TIMEOUT = "timeout"
    #: El usuario (o el cierre de la app) canceló. No es un fallo del canal.
    CANCELLED = "cancelled"

    @property
    def reintentable(self) -> bool:
        """True si repetir este error con el mismo motor puede servir de algo.

        **Nunca** son reintentables los que repetir no arregla: credenciales
        rechazadas, dirección inválida, protocolo que ningún motor abierto abre,
        reproductor ausente, códec que falta y cancelaciones. Reintentar un 401
        cinco veces sólo consigue que el usuario espere unos segundos para leer
        dos veces el mismo mensaje (FR-REC-005).

        Esto es el **dato**, no la decisión. La política vive en un solo sitio,
        :class:`thetvview.player.supervisor.RetryPolicy`, para que adaptadores y
        supervisor no se procuren reglas propias (NFR-008).
        """
        return self not in NO_REINTENTABLE


#: Errores que reintentar no arregla (FR-REC-005). Vive junto a la taxonomía
#: para que «añadir un código» y «clasificarlo» sean el mismo gesto.
NO_REINTENTABLE: frozenset[StreamErrorCode] = frozenset(
    {
        StreamErrorCode.AUTH_FAILED,
        StreamErrorCode.INVALID_URL,
        StreamErrorCode.UNSUPPORTED_PROTOCOL,
        StreamErrorCode.PLAYER_MISSING,
        StreamErrorCode.CANCELLED,
        StreamErrorCode.CODEC_UNSUPPORTED,
    }
)


@dataclass(frozen=True)
class StreamError(IPTVError):
    """Fallo de reproducción, con su código y qué hacer al respecto.

    Es un **valor**, no una excepción de control de flujo: lo devuelve el
    clasificador y lo muestra el informe de diagnóstico. :class:`PlayerError`
    sigue siendo la excepción que ve el menú de reproducción, porque cambiar eso
    rompería los ``except player.PlayerError`` que ya hay en
    :mod:`thetvview.ui.screens`.

    Separación error observado / causa inferida (FR-REC-004)
    ----------------------------------------------------------

    Dos campos que antes no existían y que se confundían en uno:

    - :attr:`observado` es **lo que pasó**: el ``returncode``, el mensaje del
      reproductor, el estado HTTP. Hechos, sin interpretar.
    - :attr:`causa` es **lo que creemos que pasó**: el
      :class:`StreamErrorCode`, que es siempre una *interpretación* de lo
      anterior.

    Con sólo el ``returncode``, :attr:`causa` queda en ``UNKNOWN`` y
    :attr:`observado` conserva el código: es preferible decir «el reproductor
    terminó con 1 y no sabemos por qué» a inventar una causa precisa. Los tres
    campos nuevos tienen valor por defecto, así que el constructor de siempre
    sigue funcionando igual.
    """

    code: StreamErrorCode
    #: Qué pasó, en una frase, sin jerga.
    resumen: str
    #: Qué puede hacer el usuario. Vacío si no hay consejo.
    consejo: str = ""
    #: Texto original del reproductor, **ya redactado** si el llamador lo
    #: redactó. Se conserva para el informe de diagnóstico.
    detalle: str = ""
    #: Hechos observados, sin interpretar. Cadena vacía si no se observó nada.
    observado: str = ""
    #: ``returncode`` del reproductor, o None si no terminó.
    returncode: int | None = None
    #: HTTP status de la última respuesta conocida, o None.
    http_status: int | None = None
    #: ``Retry-After`` que hopped del servidor, en segundos. O None.
    retry_after: float | None = None

    @property
    def causa_conocida(self) -> bool:
        """True si la causa **no** es una conjetura.

        ``UNKNOWN`` significa exactamente eso: que se observó algo pero no se
        sabe la causa. Confundir las dos cosas es lo que produce diagnósticos
        que mienten, así que el dato está explícito.
        """
        return self.code is not StreamErrorCode.UNKNOWN

    @property
    def confianza(self) -> str:
        """``"observada"`` o ``"desconocida"``. Va al informe, no a la UI."""
        return "observada" if self.causa_conocida else "desconocida"

    def para_informe(self) -> dict[str, object]:
        """Datos para el informe de diagnóstico, sin secretos (FR-DIAG-008).

        El `detalle` **no** se incluye: es texto del reproductor y quien lo
        pasó tiene que haberlo redactado ya, pero el informe es un destino de
        confianza cero (se exporta a un archivo que el usuario comparte). Va el
        `observado`, que es un hecho acotado y redactable.
        """
        return {
            "causa": self.code.value,
            "causa_conocida": self.causa_conocida,
            "confianza": self.confianza,
            "observado": self.observado,
            "returncode": self.returncode,
            "http_status": self.http_status,
            "retry_after": self.retry_after,
        }

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
    StreamErrorCode.BUFFERING_TIMEOUT: (
        "El reproductor abrió el canal pero no llegaron datos."
    ),
    StreamErrorCode.BACKEND_ERROR: "El reproductor falló por su cuenta.",
    StreamErrorCode.PLAYER_MISSING: "No hay ningún reproductor instalado.",
    StreamErrorCode.UNKNOWN: "No se pudo reproducir el canal.",
    StreamErrorCode.DNS: "El servidor del canal no existe o no resuelve.",
    StreamErrorCode.HTTP_RATE_LIMIT: "El servidor está limitando las peticiones.",
    StreamErrorCode.MANIFEST_INVALID: "La lista de reproducción del canal está dañada.",
    StreamErrorCode.MEDIA_DEMUX: "Llegó el canal pero su formato no se pudo leer.",
    StreamErrorCode.CODEC_UNSUPPORTED: "Ningún reproductor instalado tiene el códec de este canal.",
    StreamErrorCode.TRACK_SELECTION: "No se pudo aplicar el audio o los subtítulos elegidos.",
    StreamErrorCode.PLAYER_STARTUP: "El reproductor arrancó pero el canal no llegó a abrir.",
    StreamErrorCode.PLAYER_EXIT: "El reproductor se cerró sin llegar a reproducir.",
    StreamErrorCode.TIMEOUT: "Se agotó el tiempo de espera con este canal.",
    StreamErrorCode.CANCELLED: "Se canceló antes de que el canal abriera.",
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
    StreamErrorCode.DNS: (
        "El nombre del servidor no resuelve. Comprueba la dirección de la línea "
        "o que tu DNS esté funcionando; probar otro canal del mismo proveedor "
        "sirve para saber de quién es el problema."
    ),
    StreamErrorCode.HTTP_RATE_LIMIT: (
        "Estás pidiendo demasiado rápido. Espera unos minutos antes de "
        "reintentar; repetir ahora sólo alarga la espera."
    ),
    StreamErrorCode.MANIFEST_INVALID: (
        "El proveedor sirve un manifiesto que no se puede leer. Suele ser una "
        "línea caducada: prueba otro canal."
    ),
    StreamErrorCode.MEDIA_DEMUX: (
        "Llegó algo pero no se pudo descomprimir. Suele ser que la línea esté "
        "corrupta o que el formato no sea el que dice."
    ),
    StreamErrorCode.CODEC_UNSUPPORTED: (
        "Es un problema de los reproductores instalados, no del canal: instala "
        "otro (por ejemplo mpv, que trae más códecs) o busca otro canal."
    ),
    StreamErrorCode.TRACK_SELECTION: (
        "El canal se abre, pero no con el audio o los subtítulos que elegiste. "
        "Reabre el canal y acepta los valores que ofrezca."
    ),
    StreamErrorCode.PLAYER_STARTUP: (
        "El reproductor abrió pero el canal no llegó a empezar. Suele ser que la "
        "línea tardó en responder o que el proveedor está saturado."
    ),
    StreamErrorCode.PLAYER_EXIT: (
        "El reproductor se cerró solo. Prueba otra vez; si pasa siempre, es la "
        "línea, no la aplicación."
    ),
    StreamErrorCode.TIMEOUT: (
        "Se agotó el tiempo esperando. Prueba con otro canal para saber si es "
        "del servidor o de tu conexión."
    ),
    StreamErrorCode.CANCELLED: (
        "No pasó nada: la reproducción se canceló antes de empezar."
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
    observado = describe_observed(returncode, detalle)
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
        code=codigo,
        resumen=resumen,
        consejo=consejo,
        detalle=detalle,
        observado=observado,
        returncode=returncode,
    )


def describe_observed(returncode: int | None, detalle: str = "") -> str:
    """Los **hechos**, sin interpretarlos (FR-REC-004).

    Nunca inventa una causa: si sólo hay un ``returncode``, dice sólo el código.
    Y jamás incluye el texto crudo sin redactar — quien lo pase debe venir de
    :func:`thetvview.security.redaction.redact_text`, y aquí sólo se recorta y se
    limitan los caracteres, que es lo único que se puede hacer sin saber de
    dónde vino.
    """
    partes: list[str] = []
    if returncode is not None:
        partes.append(f"el reproductor terminó con el código {returncode}")
    texto = (detalle or "").strip()
    if texto:
        if len(texto) > 200:
            texto = texto[:197] + "..."
        partes.append(f"el reproductor escribió: {texto}")
    if not partes:
        return ""
    return "; ".join(partes)