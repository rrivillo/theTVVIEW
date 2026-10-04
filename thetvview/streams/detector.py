"""Detección del protocolo de un stream (SDD §6), solo stdlib, cero red.

La detección **no** da por hecho HLS por la extensión: un proveedor puede
servir un master en ``/playlist.php`` y una página de error en
``canal.m3u8``. Se combinan cinco señales, de la más fuerte a la más débil:

0. el **esquema** es RTMP/RTSP/UDP → el transporte ya es la respuesta (no
   hay cuerpo que mirar: no son descargas);
1. el cuerpo empieza por ``#EXTM3U`` → **HLS**;
2. el cuerpo es XML cuyo elemento raíz es ``MPD`` → **DASH**;
3. el primer byte es ``0x47`` y hay sync cada 188 bytes → **MPEG-TS**
   (pista única: no seleccionable, y por tanto no genera menús);
4. el ``Content-Type`` dice ``application/vnd.apple.mpegurl``,
   ``application/dash+xml``, ``video/mp2t``, ``video/mp4`` o ``video/x-flv``;
5. la extensión de la URL — la **más débil**, sólo desempata.

Si nada concluye se devuelve ``unknown`` con su motivo. Eso **no** es un
error: el stream se reproduce igual, simplemente sin pistas seleccionables
(SDD §48, AC-10).

Sobre la etapa 0 y el orden (SDD-M §36): *«el tipo de stream nunca es igual
al tipo de URL»* se cumple porque son dos campos distintos,
``Detection.protocol`` y ``Detection.scheme``. La etapa 0 no contradice la
regla: sólo se aplica a los esquemas cuyo transporte **no admite inspección**
(§9.1 lista RTMP, RTSP, UDP y FILE justo por eso). Para HTTP/HTTPS el
esquema nunca se mira: manda el cuerpo.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from ..tracks.models import (
    PROTO_DASH,
    PROTO_FLV,
    PROTO_HLS,
    PROTO_MPEGTS,
    PROTO_MP4,
    PROTO_RTMP,
    PROTO_RTMPS,
    PROTO_RTSP,
    PROTO_UDP,
    PROTO_UNKNOWN,
)
from .transport import Transport, scheme_of, transport_from_scheme

__all__ = [
    "Detection",
    "detect_protocol",
    "detect_transport",
    "looks_like_hls",
    "looks_like_dash",
    "looks_like_mpegts",
    "TS_SYNC_BYTE",
    "TS_PACKET_SIZE",
]

#: Byte de sincronización de un paquete MPEG-TS y tamaño del paquete.
TS_SYNC_BYTE: int = 0x47
TS_PACKET_SIZE: int = 188

#: Cuántos paquetes consecutiveos exigimos para no confundir un byte suelto
#: 0x47 con el principio de un flujo TS real.
_TS_MIN_PACKETS: int = 3

#: Prefijo de todo manifiesto HLS.
_HLS_MARKER: bytes = b"#EXTM3U"

#: ``Content-Type`` que declaran HLS (el segundo es habitual en servidores
#: con mod_mime).
_HLS_CONTENT_TYPES: tuple[str, ...] = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "vnd.apple.mpegurl",
)

#: Por debajo de este tamaño el cuerpo no sirve para juzgar el protocolo:
#: es el caso de la sonda con ``read_body=False`` (0-2 bytes), donde sólo
#: sabemos que "hay algo".
_MIN_BODY_BYTES: int = 8

#: ``Content-Type`` de un MPD.
_DASH_CONTENT_TYPES: tuple[str, ...] = (
    "application/dash+xml",
    "video/vnd.mpeg.dash.mpd",
)

#: Extensiones que sugieren HLS (señal débil).
_HLS_EXTENSIONS: frozenset[str] = frozenset({".m3u8", ".m3u"})

#: Extensiones que sugieren MPEG-TS / flujo directo (señal débil).
_TS_EXTENSIONS: frozenset[str] = frozenset({".ts", ".m2ts", ".mpegts"})

#: ``Content-Type`` de un MPEG-TS. Es el hueco del §9.3 que el repo tenía
#: (SDD-M): faltaba ``video/mp2t``, que es justo lo que declara un servidor
#: que sirve el directo por una ruta sin extensión.
_MPEGTS_CONTENT_TYPES: frozenset[str] = frozenset(
    {"video/mp2t", "video/mpeg", "video/mp2t-stream", "application/mp2t"}
)

#: MIME de directo con pista única. No dan pistas seleccionables, pero **sí**
#: dicen qué es el canal, que es lo que la UI necesita para no mentirle.
_DIRECT_CONTENT_TYPES: dict[str, str] = {
    "video/mp4": PROTO_MP4,
    "video/x-flv": PROTO_FLV,
    "video/flv": PROTO_FLV,
    "application/x-flv": PROTO_FLV,
}

#: Extensiones de directo equivalente (señal débil).
_DIRECT_EXTENSIONS: dict[str, str] = {
    ".mp4": PROTO_MP4,
    ".m4v": PROTO_MP4,
    ".flv": PROTO_FLV,
}

#: Esquemas que **son** la respuesta (SDD-M §9.1, etapa 0). No se inspecciona
#: un RTMP ni un RTSP: la app no lee un byte de esos flujos, así que el
#: esquema es la única señal que existe y es suficiente.
_SCHEME_ONLY: dict[str, str] = {
    "rtmp": PROTO_RTMP,
    "rtmps": PROTO_RTMPS,
    "rtsp": PROTO_RTSP,
    "rtsps": PROTO_RTSP,
    "udp": PROTO_UDP,
}


@dataclass(frozen=True)
class Detection:
    """Resultado de la detección: protocolo + por qué se decidió."""

    protocol: str = PROTO_UNKNOWN
    #: Motivo corto, apto para un log o un modal. Nunca lleva la URL.
    reason: str = ""
    #: True si el veredicto salió de una señal fuerte (cuerpo) y no del
    #: nombre del fichero: la UI lo usa para no alarmarse con un 403.
    confident: bool = False
    #: El **esquema** de la URL (SDD-M §3.1). Es un campo aparte del
    #: `protocol` a propósito: el §36 del SDD-M («el tipo de stream nunca es
    #: igual al tipo de URL») sale gratis si las dos preguntas se guardan en
    #: dos sitios distintos. Vacío si la URL no tenía esquema reconocible.
    scheme: str = ""
    #: ``Content-Type`` que declaró el servidor, si se miró. Se guarda aparte
    #: porque es una **señal**, no un veredicto: el mismo MIME puede llegar a
    #: conclusiones distintas según lo que traiga el cuerpo, y el informe de
    #: diagnóstico (§21) tiene que poder enseñar la señal además del
    #: veredicto sin volver a hacer la petición.
    mime: str = ""

    @property
    def transport(self) -> Transport:
        """Transporte efectivo (`:mod:`streams.transport`)."""
        from .transport import transport_from_scheme

        return transport_from_scheme(self.scheme)

    @property
    def is_hls(self) -> bool:
        return self.protocol == PROTO_HLS

    @property
    def is_dash(self) -> bool:
        return self.protocol == PROTO_DASH

    @property
    def is_mpegts(self) -> bool:
        return self.protocol == PROTO_MPEGTS

    @property
    def is_selectable(self) -> bool:
        """True sólo para HLS y DASH: lo demás tiene pista única."""
        return self.protocol in (PROTO_HLS, PROTO_DASH)

    @property
    def is_streaming_transport(self) -> bool:
        """True si el transporte es de los que no se descargan para analizar.

        RTMP, RTSP y UDP son **conversaciones**, no descargas: no hay cuerpo
        que inspeccionar ni MIME que leer. La app se limita a identificarlos
        y a pasárselos al reproductor (SDD-M §2.2: nada de implementar
        protocolos aquí).
        """
        return self.protocol in (
            PROTO_RTMP,
            PROTO_RTMPS,
            PROTO_RTSP,
            PROTO_UDP,
        )

    def describe(self) -> str:
        """Una línea para modales: qué es esto, según cómo se decidió."""
        if self.is_streaming_transport:
            return self.reason or f"transporte {self.scheme.upper()}"
        return self.reason or "protocolo no determinado"


def looks_like_hls(body: bytes | str) -> bool:
    """True si el cuerpo empieza por ``#EXTM3U`` (señal fuerte)."""
    head = _head_bytes(body, 16)
    return head.lstrip(b"\xef\xbb\xbf \t\r\n").upper().startswith(_HLS_MARKER)


def looks_like_dash(body: bytes | str) -> bool:
    """True si el cuerpo es un XML cuyo elemento raíz es ``MPD``."""
    text = _head_text(body, 4096).lstrip()
    if not text.startswith("<"):
        return False
    # Basta con el primer elemento: `<MPD ...>` o `<mpd ...>` con o sin
    # espacio de nombres y declaración XML delante.
    stripped = text.lstrip("\ufeff")
    head = stripped[:1]
    if head != "<":
        return False
    body_start = 1
    if stripped[1:2] == "?":
        end = stripped.find("?>")
        if end == -1:
            return False
        return _root_is_mpd(stripped[end + 2 :])
    return _root_is_mpd(stripped[body_start:])


def _root_is_mpd(rest: str) -> bool:
    text = rest.lstrip()
    if not text.startswith("<") or text.startswith("<!"):
        return False
    match_end = _tag_name_end(text)
    if match_end <= 1:
        return False
    name = text[1:match_end].strip()
    # `<MPD` y `<mpd` valen; `<MPDX>` no.
    return name.lower() == "mpd"


def _tag_name_end(text: str) -> int:
    for index, ch in enumerate(text):
        if ch in " \t\r\n/>":
            return index
    return -1


def looks_like_mpegts(body: bytes) -> bool:
    """True si hay patrón de sincronización TS cada 188 bytes (señal fuerte)."""
    if not body or len(body) < TS_PACKET_SIZE * _TS_MIN_PACKETS:
        return False
    for index in range(_TS_MIN_PACKETS):
        if body[index * TS_PACKET_SIZE] != TS_SYNC_BYTE:
            return False
    return True


def detect_protocol(
    body: bytes | str = b"",
    *,
    url: str = "",
    content_type: str = "",
) -> Detection:
    """Detecta el protocolo de un recurso ya descargado (o sin descargar).

    El orden es el del SDD §6 y no se altera: la señal del cuerpo gana
    siempre, aunque la extensión diga otra cosa. Un ``.m3u8`` que devuelve
    HTML es **HTML**, no HLS: por eso, si hay cuerpo y no encaja con ninguna
    señal fuerte, la extensión —la más débil— no "resucita" el veredicto.

    Args:
        body: primeros bytes de la respuesta. Puede estar vacío o ser muy
            corto (sonda con ``read_body=False``): entonces no se usa como
            señal fuerte.
        url: URL **final** (tras redirecciones), sólo para desempates.
        content_type: cabecera ``Content-Type`` de la respuesta.

    Returns:
        :class:`Detection`. Nunca lanza.
    """
    scheme = scheme_of(url)
    mime = (content_type or "").split(";", 1)[0].strip().lower()

    # Etapa 0 (SDD-M §9.1): el esquema manda cuando el esquema ya **es** la
    # respuesta. RTMP, RTSP y UDP no se "inspeccionan": son conexiones en las
    # que la app no lee nada. Por eso se resuelven antes del cuerpo, y no
    # después — no hay cuerpo que mirar.
    if scheme in _SCHEME_ONLY:
        nombre = scheme.upper()
        return Detection(
            _SCHEME_ONLY[scheme],
            f"el esquema {nombre} identifica el transporte; no se inspecciona",
            confident=True,
            scheme=scheme,
            mime=mime,
        )

    # 1 + 2 + 3: cuerpo (señal fuerte).
    if body and len(body) >= _MIN_BODY_BYTES:
        if looks_like_hls(body):
            return Detection(
                PROTO_HLS,
                "el cuerpo empieza por #EXTM3U (HLS)",
                confident=True,
                scheme=scheme,
                mime=mime,
            )
        if looks_like_dash(body):
            return Detection(
                PROTO_DASH,
                "el cuerpo es un MPD (DASH)",
                confident=True,
                scheme=scheme,
                mime=mime,
            )
        if isinstance(body, (bytes, bytearray)) and looks_like_mpegts(bytes(body)):
            return Detection(
                PROTO_MPEGTS,
                "patrón de sincronización MPEG-TS cada 188 bytes",
                confident=True,
                scheme=scheme,
                mime=mime,
            )
        # Hay contenido y no es HLS, DASH ni MPEG-TS. La extensión es la
        # señal más débil y no puede contradecir al cuerpo.
        return Detection(
            PROTO_UNKNOWN,
            "el contenido no es HLS, DASH ni MPEG-TS reconocible",
            confident=True,
            scheme=scheme,
            mime=mime,
        )

    # 4: Content-Type (el servidor lo declara mejor que el nombre).
    if mime:
        if mime in _HLS_CONTENT_TYPES:
            return Detection(
                PROTO_HLS,
                f"el Content-Type declara HLS ({mime})",
                scheme=scheme,
                mime=mime,
            )
        if mime in _DASH_CONTENT_TYPES:
            return Detection(
                PROTO_DASH,
                f"el Content-Type declara DASH ({mime})",
                scheme=scheme,
                mime=mime,
            )
        if mime in _MPEGTS_CONTENT_TYPES:
            # El caso real que faltaba (SDD-M §9.3): un servidor que sirve
            # MPEG-TS en `/live/canal` **sin extensión** declarando el tipo.
            # Antes caía a `unknown` y la app decía «no pude determinar el
            # protocolo» sobre un canal que se reproduce bien.
            return Detection(
                PROTO_MPEGTS,
                f"el Content-Type declara MPEG-TS ({mime})",
                confident=True,
                scheme=scheme,
                mime=mime,
            )
        if mime in _DIRECT_CONTENT_TYPES:
            # MP4 y FLV son directos de pista única: no hay nada que elegir,
            # pero **sí** sabemos qué es. Por eso `confident=True`: la UI dice
            # «es un directo» y no «no pude determinarlo».
            protocolo = _DIRECT_CONTENT_TYPES[mime]
            return Detection(
                protocolo,
                f"el Content-Type declara un directo ({mime})",
                confident=True,
                scheme=scheme,
                mime=mime,
            )

    # 5: extensión (la más débil; sólo cuando no hay cuerpo que juzgar).
    extension = _extension_of(url)
    if extension in _HLS_EXTENSIONS:
        return Detection(
            PROTO_HLS, f"la URL termina en {extension}", scheme=scheme, mime=mime
        )
    if extension in _TS_EXTENSIONS:
        return Detection(
            PROTO_MPEGTS, f"la URL termina en {extension}", scheme=scheme, mime=mime
        )
    if extension in _DIRECT_EXTENSIONS:
        protocolo = _DIRECT_EXTENSIONS[extension]
        return Detection(
            protocolo, f"la URL termina en {extension}", scheme=scheme, mime=mime
        )

    return Detection(
        PROTO_UNKNOWN,
        "sin cuerpo y sin pistas en Content-Type o URL",
        scheme=scheme,
        mime=mime,
    )


def detect_transport(url: str) -> Transport:
    """Sólo el transporte de una URL, sin descargar nada (SDD-M §9.1).

    Atajo para quien no necesita el protocolo —el router, el diagnóstico—
    y no quiere pagar el resto de la detección.
    """
    return transport_from_scheme(scheme_of(url))


def _extension_of(url: str) -> str:
    if not url:
        return ""
    try:
        path = urlsplit(str(url)).path
    except ValueError:
        return ""
    # Se des-codifica sólo el último segmento: un %2F en el path no debe
    # inventar un nombre de fichero distinto al real.
    tail = unquote(path.rsplit("/", 1)[-1])
    if "." not in tail:
        return ""
    return ("." + tail.rsplit(".", 1)[-1]).lower()


def _head_bytes(body: bytes | str, size: int) -> bytes:
    if isinstance(body, str):
        return body[:size].encode("utf-8", errors="replace")
    return bytes(body[:size])


def _head_text(body: bytes | str, size: int) -> str:
    if isinstance(body, str):
        return body[:size]
    return bytes(body[:size]).decode("utf-8", errors="replace")