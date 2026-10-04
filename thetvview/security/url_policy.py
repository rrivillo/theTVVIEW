"""Política de URL (SDD §10, gaps B1/B2), solo stdlib.

Nada llega al reproductor ni a la red sin pasar por :func:`validate_url`.

Qué valida
----------

- esquema contra una **allowlist por propósito** (nunca una denylist sola);
- host no vacío;
- puerto en rango;
- **userinfo embebido** (``user:pass@host``) rechazado: las credenciales se
  guardan aparte, jamás dentro de la URL;
- fragmento ``#…`` rechazado;
- caracteres de control crudos y percent-encoded (``%00``…``%1F``, ``%7F``);
- espacios internos y longitud (``MAX_URL_LENGTH``);
- con ``purpose="stream"``, cualquier cadena que empiece por ``-``
  (inyección de argumentos en mpv/mplayer/vlc → gap B1).

Esquemas
--------

========================  ==================================================
propósito ``metadata``    ``http``, ``https``
propósito ``stream``      ``http``, ``https``, ``ffmpeg``, ``rtmp``,
                          ``rtmps``, ``rtsp``, ``udp``
========================  ==================================================

``ffmpeg://`` es el único *wrapper* interno que el reproductor ya usa
(``player.py`` antepone el prefijo para que libavformat resuelva playlists
HLS). Si va envuelta otra URL (``ffmpeg://http://…``), se valida la URL
interior; si es una ruta relativa, no hay destino de red que validar.

RTMP/RTMPS/RTSP/UDP en ``stream`` (SDD-M Fases 4-6)
---------------------------------------------------

Es la evaluación que el comentario de este archivo llevawa pendiente desde
hace tiempo: *«rtsp/udp/rtmp se evalúan antes de añadirlos»*. Ya está hecha,
y el resultado son estos cuatro esquemas, con estas condiciones:

- **RTMP y RTMPS** no llevan credenciales. Si el proveedor las exige, es un
  token en la query, que ya cubre ``needs_redaction_for_storage``; abrir la
  puerta a ``user:pass@`` aquí no compra nada y rompería el invariante de
  :func:`_finish`.
- **RTSP** lleva credenciales, y por eso no entran así: el parser las extrae
  y las deja en el almacén del SO, y al canal le queda una referencia opaca
  ``ipcam://`` (ver :mod:`thetvview.cam_ref`). Por eso ``rtsp://user:pass@…``
  sigue rechazado, igual que en toda la app.
- **UDP** incluye multicast, que es red privada **por definición** y hereda
  ``allow_private`` de la fuente sin excepción (SDD-M Fase 6).
- **Ninguno** se acepta para descargar datos (``metadata``): aquí no hay
  reproductor, sólo un cliente HTTP acotado que no sabe hablar estos
  protocolos.
- **No hay cambios en anti-SSRF**: una cámara en la LAN es tan privada como un
  servidor IPTV en la LAN, así que ``rtsp://192.168.1.9/…`` exige el flag
  ``allow_private_network`` de su fuente igual que un ``http://192.168.1.9/``.

Rechazados de forma explícita y con mensaje propio: ``file``, ``smb``,
``ftp``, ``sftp``, ``gopher``, ``tftp``, ``data``, ``javascript``,
``jar``, ``blob``, ``about``, ``rtsps``, ``rtp``, ``rtmpt``, ``multicast``,
``ws``, ``wss``.

``rtsps`` sigue fuera a propósito: no se ha podido verificar que ningún
reproductor de esta máquina lo abra (no hay servidor RTSP sobre TLS contra el
que medirlo) y la app no ofrece lo que no ha comprobado. Lo mismo con
``multicast`` como esquema propio: el multicast es ``udp://`` con una
dirección de grupo, no un protocolo aparte.

Lo que **no** hace este módulo: resolución DNS ni comprobación de red
privada. Para eso está :mod:`thetvview.security.ssrf`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import SplitResult, urlsplit

from .errors import InvalidUrlError

__all__ = [
    "PURPOSE_METADATA",
    "PURPOSE_STREAM",
    "PURPOSES",
    "MAX_URL_LENGTH",
    "ALLOWED_SCHEMES",
    "REJECTED_SCHEMES",
    "STREAM_ONLY_SCHEMES",
    "STREAM_DEFAULT_PORTS",
    "DEFAULT_PORTS",
    "UrlParts",
    "validate_url",
    "is_allowed_scheme",
    "authorize_camara_url",
]

PURPOSE_METADATA: str = "metadata"
PURPOSE_STREAM: str = "stream"
PURPOSES: tuple[str, ...] = (PURPOSE_METADATA, PURPOSE_STREAM)

#: Longitud máxima de una URL (SDD §10.2).
MAX_URL_LENGTH: int = 2048

ALLOWED_SCHEMES: dict[str, frozenset[str]] = {
    PURPOSE_METADATA: frozenset({"http", "https"}),
    PURPOSE_STREAM: frozenset(
        {"http", "https", "ffmpeg", "rtmp", "rtmps", "rtsp", "udp"}
    ),
}

#: Esquemas de streaming que sólo se aceptan para **reproducir**, nunca para
#: descargar. No es una احتيega de ``ALLOWED_SCHEMES``: es la lista de los que
#: requieren una decisión propia (credenciales fuera, multicast privado) y por
#: eso :func:`validate_url` los trata aparte. Un esquema que esté aquí y en
#: ``PURPOSE_METADATA`` sería un fallo de diseño.
STREAM_ONLY_SCHEMES: frozenset[str] = frozenset({"rtmp", "rtmps", "rtsp", "udp"})

#: Puerto por defecto de los esquemas de streaming. Vive aquí porque la
#: validación necesita un puerto para el chequeo de rango y para el informe de
#: diagnóstico («¿está en el 554?»), y duplicarlo en el dominio sería una
#: segunda fuente de verdad.
STREAM_DEFAULT_PORTS: dict[str, int] = {
    "rtmp": 1935,
    "rtmps": 443,
    "rtsp": 554,
}

#: Puerto por defecto de cada esquema admitido. Reexportado desde
#: :mod:`thetvview.streams.transport`, que es donde vive la tabla: son dos
#: módulos los que necesitan el puerto por defecto (esta validación y el
#: informe de diagnóstico), y duplicarlo haría que divergieran en silencio.
#: ``udp`` **no** tiene puerto por defecto —es siempre explícito—, así que no
#: aparece: su ausencia es la respuesta correcta, no un 0 que luego se
#: compararía contra un puerto que nunca es 0.
from ..streams.transport import DEFAULT_PORTS  # noqa: E402

#: Esquemas que nunca se aceptan, con mensaje explícito (denylist solo para
#: explicar el motivo: la decisión la toma la allowlist).
#:
#: ``rtsps`` y ``multicast`` están aquí a propósito, no por descuido:
#: - ``rtsps``: no se ha podido verificar que ningún reproductor de esta
#:   máquina lo abra (Fase 0), y la app no ofrece lo que no ha comprobado.
#: - ``multicast``: no es un protocolo, es un ``udp://`` con dirección de
#:   grupo. Aceptarlo sería aceptar dos formas de lo mismo.
REJECTED_SCHEMES: frozenset[str] = frozenset(
    {
        "file",
        "smb",
        "cifs",
        "ftp",
        "ftps",
        "sftp",
        "gopher",
        "tftp",
        "telnet",
        "data",
        "javascript",
        "vbscript",
        "jar",
        "blob",
        "about",
        "dict",
        "ldap",
        "rsync",
        "git",
        "ssh",
        "scp",
        "rtsps",
        "rtp",
        "rtmpt",
        "multicast",
        "ws",
        "wss",
    }
)

# Percent-encoded caracteres de control: %00-%1F y %7F.
_PCT_CONTROL_RE = re.compile(r"%(?:0[0-9a-fA-F]|1[0-9a-fA-F]|7[fF])")

_PURPOSE_LABEL = {
    PURPOSE_METADATA: "descargar datos",
    PURPOSE_STREAM: "reproducir",
}


@dataclass(frozen=True)
class UrlParts:
    """URL ya validada.

    .. warning::
       ``raw``/``path`` pueden contener credenciales en el *path* (estilo
       Xtream ``/live/usuario/contraseña/1.ts``). Pásalos por
       :func:`thetvview.security.redaction.redact_text` antes de mostrarlos.
    """

    scheme: str  # esquema efectivo con el que se conecta
    host: str
    port: int | None
    path: str
    query: str
    raw: str  # URL original, tal cual se validó
    wrapper: str = ""  # "ffmpeg" si venía envuelta

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"

    @property
    def is_http(self) -> bool:
        return self.scheme == "http"

    @property
    def default_port(self) -> int | None:
        """Puerto por defecto del esquema (ninguno para http/https/udp).

        Vive aquí y no en el enum de transporte para que la URL normalizada y
        el diagnóstico hablen del **mismo** puerto por defecto; duplicarlo en
        dos módulos sería una segunda fuente de verdad esperando a divergir.
        """
        return DEFAULT_PORTS.get(self.scheme)

    @property
    def netloc(self) -> str:
        """Host (entre corchetes si es IPv6) + puerto si no es el por defecto."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port and self.port != self.default_port:
            return f"{host}:{self.port}"
        return host

    @property
    def normalized(self) -> str:
        """URL reconstruida sin userinfo ni fragmento (para peticiones)."""
        if not self.host:
            return self.raw
        base = f"{self.scheme}://{self.netloc}{self.path}"
        if self.query:
            base += f"?{self.query}"
        if self.wrapper:
            return f"{self.wrapper}://{base}"
        return base


def is_allowed_scheme(scheme: str, purpose: str = PURPOSE_METADATA) -> bool:
    """True si `scheme` está en la allowlist de `purpose`."""
    allowed = ALLOWED_SCHEMES.get(purpose, ALLOWED_SCHEMES[PURPOSE_METADATA])
    return (scheme or "").lower() in allowed


def authorize_camara_url(url: str) -> UrlParts:
    """Valida una URL de cámara **que la propia app acaba de construir**.

    Existe, y es estrecha a propósito, porque hay un hueco real que el resto de
    este módulo no cubre por sí solo:

    - ``_finish()`` rechaza ``usuario:clave@`` en **todos** los esquemas, y
      sigue haciéndolo. Es la regla que protege las URLs que vienen de una
      lista, de un favorito o de un reciente: son datos no confiables.
    - pero ``play_channel`` **reconstruye** la URL de una cámara a partir del
      keyring, para poder dársela al reproductor. Esa URL no viene de ninguna
      parte: la ha hecho la app, y su credencial sale del almacén del SO.

    Sin esta función habría dos salidas, y las dos malas: o la reconstrucción
    no llegaría nunca al reproductor (una cámara imposible de abrir), o habría
    que relajar ``_finish()`` y abriría un agujero por el que una URL con
    credenciales de una lista llegaría al argv. Aquí no se relaja nada: sólo se
    declara que **esta** URL, y sólo ``rtsp://``, no vino de un M3U.

    Lo que no hace:

    - no acepta ningún otro esquema (una ``http://`` con userinfo sigue
      rechazada);
    - no acepta espacios, controles, fragmentos, longitudes abusivas ni
      URLs que empiecen por ``-``: todo eso lo comprueba ``validate_url`` igual
      que siempre;
    - no recuerda nada ni abre ningún hilo. Es una validación, no una
      capacidad.
    """
    try:
        partes = urlsplit((url or "").strip())
    except ValueError as exc:
        raise _reject("La dirección está mal formada.") from exc
    if (partes.scheme or "").lower() != "rtsp":
        raise _reject(
            "Sólo las direcciones de cámara (rtsp://) pueden llevar credenciales "
            "reconstruidas por la app."
        )
    # Los chequeos de texto se hacen sobre la URL **completa**, con la
    # credencial dentro: si hicieran sobre la versión recortada, un espacio o
    # un `%00` escondido en la contraseña pasarían y llegarían al argv. Se
    # repiten aquí (los mismos, con el mismo código: `_precheck_text`) porque
    # la versión sin userinfo es más corta y no los dispara.
    _precheck_text((url or "").strip(), PURPOSE_STREAM)
    # Se valida **sin** el userinfo: todo lo demás (longitud, espacios,
    # controles, esquema, host, puerto, fragmento) se comprueba igual.
    #
    # Ojo al quitar las credenciales: se quita **sólo** el userinfo. Un
    # ``netloc=partes.hostname`` tiraría también el puerto, y entonces
    # ``rtsp://cam:99999/…`` se validaría como ``rtsp://cam/…`` y llegaría al
    # reproductor con un puerto imposible. El puerto es parte del destino, no
    # parte de la credencial, así que se reconstruye a mano (con corchetes si
    # el host es IPv6).
    try:
        host = partes.hostname
        puerto_bruto = partes.port
    except ValueError as exc:
        raise _reject("La dirección tiene un host o un puerto no válidos.") from exc
    sin_userinfo = partes._replace(netloc=_netloc_sin_userinfo(host, puerto_bruto))
    try:
        plano = validate_url(sin_userinfo.geturl(), PURPOSE_STREAM)
    except InvalidUrlError:
        raise
    return replace(plano, raw=(url or "").strip())


def _netloc_sin_userinfo(host: str | None, port: int | None) -> str:
    """``host[:port]`` sin usuario ni contraseña (corchetes para IPv6)."""
    nombre = host or ""
    if ":" in nombre:
        nombre = f"[{nombre}]"
    if port is not None:
        return f"{nombre}:{port}"
    return nombre


def _reject(message: str) -> "InvalidUrlError":
    # Nunca se interpola la URL en el mensaje: puede llevar la contraseña
    # en el path (Xtream) y el mensaje acaba en la barra de estado.
    return InvalidUrlError(message)


def _precheck_text(text: str, purpose: str) -> None:
    """Chequeos que no dependen de parsear: longitud, controles, espacios, «-».

    Vive aparte porque :func:`authorize_camara_url` necesita pasarlos **antes**
    de recortar el userinfo (si no, un espacio en la contraseña se escaparía) y
    porque :func:`validate_url` los hace sobre la URL entera. Una sola copia:
    dos listas de comprobaciones son dos listas que divergen sin que nadie lo
    note.
    """
    if not isinstance(text, str):
        raise _reject("La dirección debe ser texto.")
    if not text:
        raise _reject("La dirección está vacía.")
    if len(text) > MAX_URL_LENGTH:
        raise _reject(
            f"La dirección es demasiado larga (máximo {MAX_URL_LENGTH} caracteres)."
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise _reject("La dirección contiene caracteres de control no permitidos.")
    if _PCT_CONTROL_RE.search(text):
        raise _reject("La dirección contiene caracteres escapados no permitidos.")
    # Antes que el chequeo de espacios: si empieza por «-» el motivo real
    # es la inyección de argumentos en el reproductor.
    if purpose == PURPOSE_STREAM and text.startswith("-"):
        raise _reject(
            "Una dirección de reproducción no puede empezar por «-» "
            "(se interpretaría como una opción del reproductor)."
        )
    if any(ch.isspace() for ch in text):
        raise _reject("La dirección no puede contener espacios.")


def validate_url(raw: str, purpose: str = PURPOSE_METADATA) -> UrlParts:
    """Valida `raw` contra la política de URL y devuelve sus partes.

    Args:
        raw: URL candidata.
        purpose: ``"metadata"`` (descargas) o ``"stream"`` (reproductor).

    Raises:
        InvalidUrlError: si no cumple la política. El mensaje es apto para
            mostrar al usuario y **nunca** contiene la URL original.
    """
    if purpose not in PURPOSES:
        purpose = PURPOSE_METADATA
    _precheck_text(raw.strip() if isinstance(raw, str) else raw, purpose)
    text = raw.strip()

    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise _reject("La dirección está mal formada.") from exc

    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise _reject("La dirección no tiene esquema (falta «http://» o «https://»).")

    if scheme == "ffmpeg":
        return _validate_ffmpeg(text, purpose)

    if scheme in REJECTED_SCHEMES:
        if scheme == "rtsps":
            raise _reject(
                "El esquema «rtsps» no se acepta: no se ha podido comprobar "
                "que los reproductores de esta máquina lo abran, y no se "
                "ofrece lo que no está verificado. Usa «rtsp://»."
            )
        if scheme == "multicast":
            raise _reject(
                "El esquema «multicast» no existe: el multicast se indica con "
                "«udp://» y una dirección de grupo (udp://239.x.x.x:puerto)."
            )
        raise _reject(
            f"El esquema «{scheme}» nunca se acepta. "
            f"Usa http o https para {_PURPOSE_LABEL[purpose]}."
        )
    if scheme in STREAM_ONLY_SCHEMES and purpose != PURPOSE_STREAM:
        raise _reject(
            f"El esquema «{scheme}» solo sirve para reproducir un canal, no "
            "para descargar datos."
        )
    if scheme not in ALLOWED_SCHEMES[purpose]:
        allowed = ", ".join(sorted(ALLOWED_SCHEMES[purpose]))
        raise _reject(
            f"El esquema «{scheme}» no está permitido para "
            f"{_PURPOSE_LABEL[purpose]} (permitidos: {allowed})."
        )

    return _finish(text, parts, scheme)


def _finish(text: str, parts: SplitResult, scheme: str) -> UrlParts:
    if parts.username is not None or parts.password is not None:
        raise _reject(
            "La dirección no puede llevar usuario ni contraseña embebidos: "
            "las credenciales se guardan por separado, nunca en la URL."
        )
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise _reject("La dirección tiene un host o un puerto no válidos.") from exc
    if not host:
        raise _reject("La dirección no indica el servidor (host vacío).")
    if port is not None and not (1 <= port <= 65535):
        raise _reject("El puerto de la dirección no es válido.")
    if parts.fragment:
        raise _reject(
            "La dirección no puede llevar un fragmento (#…): "
            "no viaja al servidor y solo añade ambigüedad."
        )
    return UrlParts(
        scheme=scheme,
        host=host.lower(),
        port=port,
        path=parts.path or "/",
        query=parts.query,
        raw=text,
    )


def _validate_ffmpeg(text: str, purpose: str) -> UrlParts:
    """Maneja ``ffmpeg://`` (wrapper interno del reproductor)."""
    if purpose != PURPOSE_STREAM:
        raise _reject(
            "El esquema «ffmpeg» solo sirve para reproducir, no para descargar datos."
        )
    inner = text[len("ffmpeg://"):]
    if not inner:
        raise _reject("El esquema «ffmpeg» necesita una URL o ruta detrás.")
    if inner.lower().startswith("ffmpeg://"):
        raise _reject("No se permite anidar «ffmpeg://» varias veces.")
    if "://" in inner:
        # `ffmpeg://` existe para un motivo concreto: que libavformat resuelva
        # las rutas relativas de un manifiesto HLS. Envolver con él cualquier
        # otro transporte (rtmp, rtsp, udp) no aporta nada y sólo crea un
        # camino donde una URL se declara dos veces, así que se rechaza.
        esquema_interno = inner.split("://", 1)[0].lower()
        if esquema_interno not in ("http", "https"):
            raise _reject(
                "El prefijo «ffmpeg://» solo envuelve direcciones http o https "
                "(es para que el reproductor resuelva las rutas relativas de un "
                "manifiesto HLS)."
            )
        # Caso real: player.py antepone ffmpeg:// a http://host/lista.m3u8
        wrapped = validate_url(inner, purpose=purpose)
        return replace(wrapped, raw=text, wrapper="ffmpeg")
    if inner.startswith("-"):
        raise _reject(
            "Una dirección de reproducción no puede empezar por «-» "
            "(se interpretaría como una opción del reproductor)."
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in inner):
        raise _reject("La dirección contiene caracteres de control no permitidos.")
    if _PCT_CONTROL_RE.search(inner):
        raise _reject("La dirección contiene caracteres escapados no permitidos.")
    # Ruta relativa/absoluta para libavformat: no hay destino de red.
    return UrlParts(
        scheme="ffmpeg",
        host="",
        port=None,
        path=inner,
        query="",
        raw=text,
        wrapper="ffmpeg",
    )
