"""Cámara IP: referencia opaca para RTSP con credenciales (SDD-M Fase 5).

El problema
-----------

Una lista de cámaras trae líneas así::

    rtsp://admin:clave@192.168.1.9:554/stream1

Con el userinfo dentro de la URL, esa contraseña acaba en
``data/recents.json``, en ``data/favorites.json`` y en la barra de estado. Es
exactamente el problema que :mod:`thetvview.stream_ref` resolvió para Xtream con
``xtream://``, y aquí se resuelve **igual**: con el mecanismo ya construido y
probado, no con uno nuevo.

El mecanismo
------------

1. el parser de M3U ve una línea ``rtsp://`` con userinfo;
2. guarda el **secreto** en el almacén del SO (0700, como Xtream), con una
   clave derivada de fuente + identificador de cámara;
3. al canal le deja ``ipcam://<fuente>/<id>``, que no lleva secretos ni host.

A partir de ahí ``Channel`` no tiene secretos, **favoritos y recientes
funcionan** (guardan la referencia opaca, que ya es segura) y la credencial
sólo se recupera en :func:`resolve_cam_ref`, para construir el argv.

La referencia **no lleva la URL**: sólo un identificador. Es lo que obliga a
guardar la URL sin credenciales en el almacén, y es la opción correcta: si la
URL viviera en la referencia, bastaría con que un favorito, un reciente o un
``str(channel)`` la filtraran para tenerla entera. Aquí la referencia no
contiene ni el host de la cámara.

Por qué ``ipcam://`` y no otra cosa
-----------------------------------

- ``rtsps://`` queda **descartado**: es el nombre real del esquema RTSP sobre
  TLS, y un prefijo opaco que se parece a un esquema real es un accidente
  esperando a ocurrir. Choca de frente con el criterio de `stream_ref`
  («ninguno de los prefijos es un esquema real»).
- ``ipcam://`` no lo usa ningún protocolo y dice lo que es: una referencia a
  una cámara IP. Igual que ``xtream://`` lo es para un panel.

Sólo stdlib, y sin red ni disco: la resolución ocurre en caliente, como la de
Xtream, y no persiste nada.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import SplitResult, quote, unquote, urlsplit

__all__ = [
    "PREFIX",
    "OPAQUE_PREFIXES",
    "MissingCamCredentialsError",
    "CamRef",
    "CamCredentials",
    "is_cam_ref",
    "camera_id_of",
    "split_credentials",
    "build_cam_ref",
    "registrar_camara",
    "store_cam_credentials",
    "cam_key",
    "resolve_cam_ref",
    "camera_names",
    "explain_camera_flow",
]

#: Prefijo de la referencia opaca de cámara. No es un esquema jugable.
PREFIX = "ipcam://"

#: Lo consultan favoritos y recientes para dejar intacta una referencia que ya
#: es segura (junto a los prefijos de :mod:`thetvview.stream_ref`).
OPAQUE_PREFIXES: tuple[str, ...] = (PREFIX,)

#: Longitud del hash que identifica la cámara dentro de la fuente. 128 bits,
#: como el token del proxy de fijado: bastante para que dos cámaras distintas
#: no colisionen, y no reversible a mano.
_HASH_LEN = 32


class MissingCamCredentialsError(Exception):
    """No hay credenciales guardadas para esa cámara.

    Mismo criterio que ``MissingCredentialsError`` de Xtream: el mensaje
    explica qué hacer y **no** lleva ni la URL ni el usuario.
    """


@dataclass(frozen=True)
class CamRef:
    """Identidad de una cámara sin secretos ni dirección.

    Attributes:
        source_name: nombre de la lista en el catálogo (parte de la clave).
        camera_id: hash de la URL **sin** credenciales. Identifica la cámara
            dentro de la fuente sin revelar host ni ruta.
    """

    source_name: str
    camera_id: str

    def to_opaque(self) -> str:
        """Referencia opaca, sin usuario, contraseña ni dirección.

        >>> CamRef("Cámaras", "abc123").to_opaque()
        'ipcam://C%C3%A1maras/abc123'
        """
        return (
            f"{PREFIX}{quote(str(self.source_name), safe='')}/"
            f"{quote(str(self.camera_id), safe='')}"
        )

    @classmethod
    def parse(cls, url: str) -> "CamRef | None":
        """Extrae la referencia de una URL opaca; None si no lo es.

        Tolera nombres de fuente con ``/`` o caracteres raros: van
        percent-encoded al construir y se restauran aquí, igual que
        :class:`thetvview.stream_ref.StreamRef`.
        """
        if not isinstance(url, str) or not url.startswith(PREFIX):
            return None
        parts = url[len(PREFIX):].split("/")
        if len(parts) != 2:
            return None
        source, camera_id = parts
        if not source or not camera_id:
            return None
        return cls(source_name=unquote(source), camera_id=unquote(camera_id))


@dataclass(frozen=True)
class CamCredentials:
    """Lo que hay que guardar de una cámara para poder reproducirla.

    Se guarda **entero** (ubicación, usuario y contraseña) bajo una sola clave
    del almacén del SO. Podría repartirse en dos o tres claves —la ubicación no
    es un secreto—, pero entonces habría que garantizar que las tres se
    escriben y se leen juntas; con una sola clave la operación es atómica por
    construcción y no existe el estado intermedio «tiene sitio pero no clave».

    Que la ubicación viva en el almacén cifrado y no en ``data/`` no es
    paranoia: ``ipcam://`` no la lleva, así que es el **único** sitio donde
    existe, y ese es el que el SO protege.
    """

    location: str
    username: str
    password: str

    def to_blob(self) -> str:
        """Serializa para el almacén. JSON: el almacén sólo guarda cadenas."""
        return json.dumps(
            {
                "location": self.location,
                "username": self.username,
                "password": self.password,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_blob(cls, blob: str) -> "CamCredentials | None":
        """Reconstruye desde el almacén; None si el valor está dañado.

        None en vez de excepción: un almacén con basura dentro no debe romper la
        reproducción del resto de canales.
        """
        if not blob:
            return None
        try:
            datos = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(datos, dict):
            return None
        ubicacion = str(datos.get("location") or "")
        if not ubicacion:
            return None
        return cls(
            location=ubicacion,
            username=str(datos.get("username") or ""),
            password=str(datos.get("password") or ""),
        )


def is_cam_ref(url: str) -> bool:
    """True si `url` es una referencia opaca de cámara.

    No lleva credenciales, así que quien la persiste (favoritos, recientes) la
    deja intacta.
    """
    return isinstance(url, str) and url.startswith(OPAQUE_PREFIXES)


def camera_id_of(url: str) -> str:
    """Identificador estable de una URL de cámara, calculada **sin** credenciales.

    Determinista, para que la clave del almacén sea la misma en cada sesión sin
    guardar la URL. Se hashea la URL **reconstruida sin userinfo**: si se
    hashtara la original, cambiar la contraseña en el M3U crearía una cámara
    distinta y la anterior quedaría huérfana en el almacén.
    """
    try:
        partes = urlsplit(url)
    except ValueError:
        partes = None
    if partes is None:
        limpio = url
    else:
        limpio = partes._replace(netloc=_host_puerto(partes)).geturl()
    return hashlib.sha256(limpio.encode("utf-8")).hexdigest()[:_HASH_LEN]


def split_credentials(url: str) -> tuple[str, str, str] | None:
    """Separa ``(usuario, contraseña, url_sin_credenciales)`` de una URL de cámara.

    Devuelve None si la URL no lleva userinfo o no es ``rtsp://``: no se toca lo
    que no es de una cámara, y una URL HTTP con token sigue funcionando
    exactamente igual que hasta ahora.

    >>> split_credentials("rtsp://alice:s3cr3t@cam.local/live")
    ('alice', 's3cr3t', 'rtsp://cam.local/live')
    >>> split_credentials("http://x/a.m3u8") is None
    True
    """
    if not isinstance(url, str):
        return None
    try:
        partes = urlsplit(url.strip())
        usuario = partes.username
        clave = partes.password
    except ValueError:
        return None
    if (partes.scheme or "").lower() != "rtsp" or usuario is None:
        return None
    # ``SplitResult`` no tiene campos username/password: el netloc se rehace a
    # mano desde host + puerto, que es lo único que hay que conservar.
    sin_userinfo = partes._replace(netloc=_host_puerto(partes)).geturl()
    return unquote(usuario), (unquote(clave) if clave else ""), sin_userinfo


def _host_puerto(partes: SplitResult) -> str:
    """``host[:port]`` de un ``SplitResult``, con el IPv6 entre corchetes."""
    host = (partes.hostname or "").lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    try:
        puerto = partes.port
    except ValueError:  # puerto malformado: se conserva el host y ya
        puerto = None
    return f"{host}:{puerto}" if puerto else host


def build_cam_ref(source_name: str, url: str) -> tuple[str, CamCredentials] | None:
    """Prepara el registro de una cámara con credenciales.

    Returns:
        ``(referencia_opaca, credenciales)`` si la URL es una cámara con
        credenciales; None si no aplica. No guarda nada: quien llama decide
        si registra (o actualiza) el secreto.
    """
    partes = split_credentials(url)
    if partes is None:
        return None
    usuario, clave, sin_credenciales = partes
    ref = CamRef(source_name, camera_id_of(sin_credenciales)).to_opaque()
    return ref, CamCredentials(
        location=sin_credenciales, username=usuario, password=clave
    )


def registrar_camara(source_name: str, url: str, store: object | None = None) -> str:
    """Convierte una línea ``rtsp://user:pass@…`` en una referencia opaca.

    Es la función que el parser M3U llama por cada URL de canal, y la que hace
    que **favoritos y recientes funcionen** con cámaras: en el ``Channel``
    queda ``ipcam://…``, que no lleva secretos ni host, mientras la credencial
    va al almacén cifrado del SO.

    Todo lo que no sea una cámara con credenciales se devuelve **tal cual**, sin
    tocarlo. Es importante: una URL HTTP con token tiene que seguir siendo esa
    URL, o las listas IPTV dejarían de reproducirse.

    Nunca lanza. Si el almacén de secretos falla, la referencia se devuelve
    igualmente: la reproducción dará un modal que explica que faltan las
    credenciales, que es un error visible y localizable, en vez de perder la
    cámara sin explicación.

    Args:
        source_name: nombre de la lista (parte de la clave del almacén).
        url: la línea tal cual venía del M3U.
        store: dónde guardar. ``None`` (lo normal) usa el almacén activo del
            proceso; los tests pasan uno propio para no depender del singleton.
    """
    preparado = build_cam_ref(source_name, url)
    if preparado is None:
        return url
    referencia, credenciales = preparado
    ref = CamRef.parse(referencia)
    if ref is None:  # pragma: no cover - la referencia la construye este módulo
        return url
    destino = store
    if destino is None:
        try:
            from .security.secrets import get_store

            destino = get_store()
        except Exception:  # noqa: BLE001 - sin almacén se pierde la cámara, no la lista
            return referencia
    store_cam_credentials(destino, source_name, credenciales, ref.camera_id)
    return referencia


def cam_key(source_name: str, camera_id: str) -> str:
    """Clave del almacén de secretos para una cámara.

    El nombre de la lista entra para que dos catálogos distintos (tests,
    usuarios) no colisionen en el keyring compartido del SO, igual que en
    :meth:`thetvview.playlist_manager.PlaylistManager._secret_key`.
    """
    return f"cam::{source_name}::{camera_id}"


def store_cam_credentials(
    store: object, source_name: str, credenciales: CamCredentials, camera_id: str
) -> None:
    """Guarda las credenciales de una cámara en el almacén de secretos.

    Nunca lanza: si el almacén falla (sin keyring, keyring bloqueado), la app
    sigue funcionando con el resto de canales y esa cámara da un modal que
    explica el motivo. Perder una cámara es mejor que perder la lista entera.
    """
    escribir = getattr(store, "set_password", None)
    if not callable(escribir):
        return
    try:
        escribir(cam_key(source_name, camera_id), credenciales.to_blob())
    except Exception:  # noqa: BLE001 - degrada, no rompe
        return


def resolve_cam_ref(ref: CamRef, store: object) -> str:
    """Reconstruye la URL real con credenciales desde el almacén de secretos.

    Es el único punto donde la credencial vuelve a existir como texto, y ocurre
    justo antes de construir el argv. La URL que sale **no** se persiste ni se
    muestra: :func:`thetvview.security.redaction.redact_text` la deja en
    ``rtsp://***@…`` si alguien la imprime.

    Raises:
        MissingCamCredentialsError: la cámara no está registrada en esta
            máquina (o se borró su clave). El mensaje no lleva ni el usuario
            ni la URL.
    """
    leer = getattr(store, "get_password", None)
    if not callable(leer):
        raise MissingCamCredentialsError(
            "No hay credenciales guardadas para esa cámara; vuelve a cargar "
            "la lista de cámaras para registrarlas."
        )
    try:
        blob = leer(cam_key(ref.source_name, ref.camera_id)) or ""
    except Exception:  # noqa: BLE001 - un almacén roto no rompe la reproducción
        raise MissingCamCredentialsError(
            "No se pudieron leer las credenciales de esa cámara. Vuelve a "
            "cargar la lista de cámaras."
        ) from None
    credenciales = CamCredentials.from_blob(blob)
    if credenciales is None:
        raise MissingCamCredentialsError(
            "No tengo las credenciales de esa cámara. Vuelve a cargar la lista "
            "de cámaras para registrarlas de nuevo."
        )
    try:
        partes = urlsplit(credenciales.location)
    except ValueError:
        raise MissingCamCredentialsError(
            "La referencia de esa cámara está dañada; vuelve a cargar la lista."
        ) from None
    # Usuario y contraseña van percent-encoded: una clave con «@» o «:»
    # rompería el netloc si se concatenara en crudo. Es el mismo motivo por el
    # que stream_ref usa quote() al construir sus referencias.
    netloc = (
        f"{quote(credenciales.username, safe='')}:"
        f"{quote(credenciales.password, safe='')}@{partes.netloc}"
    )
    sufijo = f"?{partes.query}" if partes.query else ""
    return f"rtsp://{netloc}{partes.path}{sufijo}"


def camera_names(channels: Iterable[object] | None) -> list[str]:
    """Nombres de los canales de una lista que son referencias de cámara.

    Acepta cualquier iterable de objetos con ``url`` y ``name``, así que no
    importa :mod:`thetvview.models` (que a su vez pasa por aquí): la UI sólo
    necesita **cuántas** hay y **cómo** se llaman, para explicárselo al
    usuario.

    .. note::
      Lo que sale son los nombres que el usuario ve, no las direcciones: un
      modal no es el sitio donde aparece el host de la cámara.
    """
    nombres: list[str] = []
    for canal in channels or ():
        url = getattr(canal, "url", "")
        if not is_cam_ref(url):
            continue
        nombre = str(getattr(canal, "name", "") or url).strip()
        if nombre:
            nombres.append(nombre)
    return nombres


def explain_camera_flow(cantidad: int) -> str:
    """Texto del modal que explica qué pasa con las cámaras de la lista (H11).

    Vive aquí y no en la UI porque el texto **es** parte del contrato del
    mecanismo: si mañana el almacén cambia de sitio, esta es la función que hay
    que revisar. Y porque en los tests se puede comprobar sin montar curses.

    Dice cuatro cosas, y ni una más: dónde ha ido la contraseña, que la lista
    puede usarse igual que cualquier otra, qué hace falta para reproducirlas y
    qué revisar si no abren. No lleva ni el host, ni el usuario, ni la clave.
    """
    plural = "cámara" if cantidad == 1 else "cámaras"
    return (
        f"Esta lista trae {cantidad} {plural} IP (líneas rtsp://).\n"
        "\n"
        "Su usuario y su contraseña no se quedan en la lista: se guardan en el "
        "almacén de secretos del sistema (0700) y el canal se queda con una "
        "referencia opaca que no contiene ni la clave ni la dirección.\n"
        "\n"
        "Puedes usar la lista igual que cualquier otra: favoritos y recientes "
        "guardan la referencia, no la contraseña.\n"
        "\n"
        "Para abrirlas hace falta un reproductor que abra RTSP con credenciales "
        "(MPV o MPlayer). Si no abren, comprueba que la cámara esté encendida, "
        "que el reproductor elegido soporte RTSP y que tu red llegue hasta ella."
    )