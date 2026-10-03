"""Detección del protocolo de un stream (SDD §6), solo stdlib, cero red.

La detección **no** da por hecho HLS por la extensión: un proveedor puede
servir un master en ``/playlist.php`` y una página de error en
``canal.m3u8``. Se combinan cinco señales, de la más fuerte a la más débil:

1. el cuerpo empieza por ``#EXTM3U`` → **HLS**;
2. el cuerpo es XML cuyo elemento raíz es ``MPD`` → **DASH**;
3. el primer byte es ``0x47`` y hay sync cada 188 bytes → **MPEG-TS**
   (pista única: no seleccionable, y por tanto no genera menús);
4. el ``Content-Type`` dice ``application/vnd.apple.mpegurl`` o
   ``application/dash+xml``;
5. la extensión de la URL — la **más débil**, sólo desempata.

Si nada concluye se devuelve ``unknown`` con su motivo. Eso **no** es un
error: el stream se reproduce igual, simplemente sin pistas seleccionables
(SDD §48, AC-10).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from ..tracks.models import (
    PROTO_DASH,
    PROTO_HLS,
    PROTO_MPEGTS,
    PROTO_UNKNOWN,
)

__all__ = [
    "Detection",
    "detect_protocol",
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


@dataclass(frozen=True)
class Detection:
    """Resultado de la detección: protocolo + por qué se decidió."""

    protocol: str = PROTO_UNKNOWN
    #: Motivo corto, apto para un log o un modal. Nunca lleva la URL.
    reason: str = ""
    #: True si el veredicto salió de una señal fuerte (cuerpo) y no del
    #: nombre del fichero: la UI lo usa para no Alarmarse con un 403.
    confident: bool = False

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
    # 1 + 2 + 3: cuerpo (señal fuerte).
    if body and len(body) >= _MIN_BODY_BYTES:
        if looks_like_hls(body):
            return Detection(
                PROTO_HLS, "el cuerpo empieza por #EXTM3U (HLS)", confident=True
            )
        if looks_like_dash(body):
            return Detection(
                PROTO_DASH, "el cuerpo es un MPD (DASH)", confident=True
            )
        if isinstance(body, (bytes, bytearray)) and looks_like_mpegts(bytes(body)):
            return Detection(
                PROTO_MPEGTS,
                "patrón de sincronización MPEG-TS cada 188 bytes",
                confident=True,
            )
        # Hay contenido y no es HLS, DASH ni MPEG-TS. La extensión es la
        # señal más débil y no puede contradecir al cuerpo.
        return Detection(
            PROTO_UNKNOWN,
            "el contenido no es HLS, DASH ni MPEG-TS reconocible",
            confident=True,
        )

    # 4: Content-Type (el servidor lo declara mejor que el nombre).
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime:
        if mime in _HLS_CONTENT_TYPES:
            return Detection(
                PROTO_HLS, f"el Content-Type declara HLS ({mime})"
            )
        if mime in _DASH_CONTENT_TYPES:
            return Detection(PROTO_DASH, f"el Content-Type declara DASH ({mime})")

    # 5: extensión (la más débil; sólo cuando no hay cuerpo que juzgar).
    extension = _extension_of(url)
    if extension in _HLS_EXTENSIONS:
        return Detection(PROTO_HLS, f"la URL termina en {extension}")
    if extension in _TS_EXTENSIONS:
        return Detection(PROTO_MPEGTS, f"la URL termina en {extension}")

    return Detection(
        PROTO_UNKNOWN,
        "sin cuerpo y sin pistas en Content-Type o URL",
    )


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