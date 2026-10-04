"""Transporte de un stream: el **esquema**, no el contenido (SDD-M §4.1).

Por qué existe este módulo y por qué es un campo aparte
-----------------------------------------------------

``https://servidor/live.m3u8`` y ``https://servidor/live/canal.ts`` comparten
esquema y no se parecen en nada; ``rtmp://x/live/a`` no necesita ni pathname
ni extensión para decirnos cómo se transporta. El §36 del SDD-M loformula como
regla a recordar —*«el tipo de stream nunca es igual al tipo de URL»*— y aquí
se convierte en estructura: dos campos que no se confunden.

```text
transport  = el esquema    http | https | rtmp | rtmps | rtsp | udp | file
protocol   = el contenido  hls  | dash | mpegts | unknown
```

Añadir SRT o RIST (§32 del SDD-M) es añadir un valor al enum y una fila en
:data:`SCHEME_TRANSPORTS`: ni la UI ni el router se tocan.

Nada aquí lee red, ni disco, ni curses: sólo ``urllib.parse``. Es el módulo de
dominio del que dependen el detector, el router de reproductores y el
diagnóstico, y por eso no puede importar ninguno de los tres.
"""

from __future__ import annotations

from enum import Enum
from urllib.parse import urlsplit

__all__ = [
    "Transport",
    "SCHEME_TRANSPORTS",
    "STREAM_SCHEMES",
    "DEFAULT_PORTS",
    "HTTP_TRANSPORTS",
    "scheme_of",
    "transport_from_scheme",
    "transport_of",
    "is_multicast_host",
    "describe",
]


class Transport(str, Enum):
    """El transporte: cómo viajan los bytes.

    Extensible por diseño (§4.1): es un ``str`` Enum, así que un valor nuevo
    se serializa como su texto y se compara con ``==`` sin ceremonyas.
    """

    HTTP = "http"
    HTTPS = "https"
    RTMP = "rtmp"
    RTMPS = "rtmps"
    RTSP = "rtsp"
    RTSPS = "rtsps"
    UDP = "udp"
    FILE = "file"
    #: SRT y RIST están aquí para demostrar que el enum **es** extensible
    #: (§4.1, §32) y para que añadir uno sea una línea, no un rediseño. No se
    #: **ofrecen**: :data:`STREAM_SCHEMES` no los incluye y
    #: :data:`~thetvview.player.protocols.PLAYER_PROTOCOL_SUPPORT` no los ha
    #: medido. Conocer la palabra y poder ofrecerla son dos cosas distintas,
    #: y el repo las mantiene separadas a propósito (§51: no prometer lo que no
    #: está comprobado).
    SRT = "srt"
    RIST = "rist"
    #: Sin esquema reconocible, o un esquema que no sabemos clasificar.
    UNKNOWN = "unknown"


#: Puerto por defecto de cada transporte. Lo usan el diagnóstico y los
#: mensajes de error («¿está en el 554?»), nunca la conexión: de eso se
#: encarga el reproductor.
#:
#: ``udp`` **no** está: en UDP el puerto es siempre explícito y un 0 como
#: «puerto por defecto» sería un valor que no existe, que luego alguien
#: compararía y concluiría algo que no se sigue de nada.
DEFAULT_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
    "rtmp": 1935,
    "rtmps": 443,
    "rtsp": 554,
    "rtsps": 322,
}

#: Los esquemas que la app reconoce como «esto es una fuente de stream».
#: Es un conjunto cerrado **a propósito**: lo que no está aquí no se ofrece,
#: aunque el reproductor underlying lo sepa abrir (§51 del SDD de pistas:
#: no prometer lo que no está comprobado).
STREAM_SCHEMES: frozenset[str] = frozenset(
    {"http", "https", "rtmp", "rtmps", "rtsp", "rtsps", "udp", "file"}
)

#: Los esquemas **reconhecidos pero no ofrecidos**. Tienen valor en el enum
#: (el vocabulario es completo) y no en :data:`STREAM_SCHEMES` (lo que la app
#: se compromete a abrir). El router usa la segunda lista, así que aquí no hay
#: ningún camino por el que un canal SRT llegue al reproductor.
KNOWN_NOT_OFFERED: frozenset[str] = frozenset({"srt", "rist"})

#: Mapeo esquema → transporte. Tabla y no ``if`` en cascada: es el mismo sitio
#: donde se añadiría ``srt`` o ``rist`` (§32 del SDD-M).
SCHEME_TRANSPORTS: dict[str, Transport] = {
    "http": Transport.HTTP,
    "https": Transport.HTTPS,
    "rtmp": Transport.RTMP,
    "rtmps": Transport.RTMPS,
    "rtsp": Transport.RTSP,
    "rtsps": Transport.RTSPS,
    "udp": Transport.UDP,
    "file": Transport.FILE,
    "srt": Transport.SRT,
    "rist": Transport.RIST,
}

#: Transportes que llevan cabeceras HTTP (y por tanto admiten User-Agent y
#: Referer). El §15 del SDD-M avisa de que **no todos** los protocolos admiten
#: cabeceras: pasárselas a un ``rtmp://`` no hace nada, no rompe nada.
HTTP_TRANSPORTS: frozenset[Transport] = frozenset({Transport.HTTP, Transport.HTTPS})


def scheme_of(url: str) -> str:
    """Esquema en minúsculas de `url`, o ``""`` si no lo tiene.

    >>> scheme_of("HTTPS://x/a.m3u8")
    'https'
    >>> scheme_of("sin esquema")
    ''
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        return (urlsplit(url.strip()).scheme or "").lower()
    except ValueError:
        return ""


def transport_from_scheme(scheme: str) -> Transport:
    """Transporte de un esquema. Desconocido → :attr:`Transport.UNKNOWN`.

    >>> transport_from_scheme("rtmps") is Transport.RTMPS
    True
    >>> transport_from_scheme("gopher") is Transport.UNKNOWN
    True
    """
    return SCHEME_TRANSPORTS.get((scheme or "").strip().lower(), Transport.UNKNOWN)


def transport_of(url: str) -> Transport:
    """Transporte completo de una URL, en un paso.

    >>> transport_of("rtmp://x/live/a") is Transport.RTMP
    True
    """
    return transport_from_scheme(scheme_of(url))


def is_multicast_host(host: str) -> bool:
    """True si `host` está en el rango multicast IPv4 (224.0.0.0/4).

    Multicast **es red privada por definición** (decisión de la Fase 6): el
    paquete no sale a Internet, así que hereda ``allow_private`` de la fuente
    sin excepción. Reconocerlo aquí es lo que permite al diagnóstico decir
    «esto es una dirección multicast, comprueba tu red» en vez de «error».

    Acepta también la forma entre corchetes de un IPv6, que viene así en las
    URL y no es un host IPv4 válido.
    """
    texto = (host or "").strip().lower()
    if not texto:
        return False
    if texto.startswith("[") and texto.endswith("]"):
        texto = texto[1:-1]
    partes = texto.split(".")
    if len(partes) != 4:
        return False
    try:
        octetos = [int(p) for p in partes]
    except ValueError:
        return False
    if any(o < 0 or o > 255 for o in octetos):
        return False
    return 224 <= octetos[0] <= 239


def describe(transport: Transport) -> str:
    """Nombre legible para modales e informes (nunca un enum en crudo)."""
    return {
        Transport.HTTP: "HTTP",
        Transport.HTTPS: "HTTPS",
        Transport.RTMP: "RTMP",
        Transport.RTMPS: "RTMPS",
        Transport.RTSP: "RTSP",
        Transport.RTSPS: "RTSPS",
        Transport.UDP: "UDP",
        Transport.FILE: "fichero local",
        Transport.SRT: "SRT",
        Transport.RIST: "RIST",
        Transport.UNKNOWN: "desconocido",
    }.get(transport, "desconocido")