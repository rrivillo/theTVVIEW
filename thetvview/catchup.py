"""Catch-up / TV Archive / Timeshift — dominio puro, sin curses y sin red.

Este módulo implementa el modelo interno del SDD
``SDD_IPTV_Catchup_Timeshift.md``: una **capacidad del proveedor**, no una
función que el usuario pueda activar arbitrariamente.

La regla de oro (§21) vive aquí y en un solo sitio, :func:`can_use_catchup`::

    if (provider_declared and enabled and event_within_archive_window):
        enable_catchup_playback()
    else:
        disable_catchup_playback()

Lo que este módulo **no** hace, a propósito (§17, §21):

- No adivina endpoints. Nunca prueba ``/archive/…``, ``/catchup/…`` ni
  ``/timeshift/…`` a ciegas: sólo construye peticiones cuando el proveedor
  ha declarado la capacidad, y **siempre** contra el mecanismo declarado.
- No infiere disponibilidad. Que exista EPG, una URL live, o que otro canal
  del mismo proveedor tenga archivo, no habilita nada.
- No habla con la red. Las credenciales se resuelven en el último momento
  (como ya hace ``stream_ref`` con ``xtream://``), así que este módulo es
  puro y se puede testear entero con ``unittest``.

Fail-closed (§16): si el proveedor no da ventana declarada, la respuesta es
``DISABLED``. No se inventa disponibilidad; se explica al usuario con un
modal.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

from .security.errors import IPTVError

if TYPE_CHECKING:  # pragma: no cover - sólo tipado
    from .models import Channel, Program

__all__ = [
    "CatchupCapability",
    "DISABLED",
    "SECONDS_PER_DAY",
    "PROTOCOL_XTREAM",
    "PROTOCOL_M3U",
    "PROTOCOL_GENERIC",
    "CatchupState",
    "CatchupError",
    "CatchupNotAvailableError",
    "CatchupOutsideArchiveWindowError",
    "CatchupInvalidIntervalError",
    "CatchupRequest",
    "PlaybackRequest",
    "CatchupAdapter",
    "XtreamCatchupAdapter",
    "M3UCatchupAdapter",
    "GenericCatchupAdapter",
    "CatchupRef",
    "TS_PREFIX",
    "MAX_REQUEST_SECONDS",
    "MAX_REQUEST_SECONDS_DOC",
    "from_xtream_stream",
    "from_m3u_attrs",
    "capability_for",
    "can_use_catchup",
    "archive_start",
    "is_within_archive_window",
    "classify",
    "classify_request",
    "describe_state",
    "adapter_for",
    "build_playback_request",
    "build_catchup_ref",
    "render_source_template",
    "template_values",
    "playback_channel",
    "validar_intervalo",
    "MAX_TEMPLATE_LENGTH",
    "MAX_TEMPLATE_VALUE_LENGTH",
]


#: Segundos por día: unidad de ``tv_archive_duration`` y ``catchup-days``.
SECONDS_PER_DAY: int = 86400

PROTOCOL_XTREAM: str = "xtream"
PROTOCOL_M3U: str = "m3u"
PROTOCOL_GENERIC: str = "generic"

#: Prefijo de la referencia opaca de catch-up. No es un esquema jugable:
#: ``validate_url`` lo rechaza, así que si se escapara sin resolver el
#: reproductor nunca lo vería.
TS_PREFIX: str = "xtream-ts://"

#: Valores de ``catchup`` que significan explícitamente "sin archivo".
#: Ante la duda se **deshabilita** (fail-closed, §16).
_CATCHUP_DISABLED_VALUES: frozenset[str] = frozenset(
    {"", "none", "no", "off", "false", "0", "disabled"}
)

#: Placeholders reconocidos en la plantilla ``catchup-source``. Cualquier
#: otro nombre es un error: no se adivina la sintaxis del proveedor (riesgo
#: declarado en el plan §6.4).
_TEMPLATE_PLACEHOLDERS: tuple[str, ...] = ("start", "end", "duration", "utc")

#: Tope de longitud de la plantilla ``catchup-source`` declarada, en
#: caracteres. Es texto de un tercero y entra en una URL: 2 000 es de sobra para
#: cualquier plantilla real (las publicadas rondan los 100) y corta de raíz el
#: abuso de pegar megabytes de query en un ``#EXTINF``.
MAX_TEMPLATE_LENGTH: int = 2000

#: Tope de longitud de **cada** valor renderizado, en caracteres. Los valores
#: que producimos son un epoch (10 dígitos) o un sello de 19; 64 deja margen de
#: sobra y sigue impidiendo que un dato enorme acabe en la query.
MAX_TEMPLATE_VALUE_LENGTH: int = 64

#: Caracteres que un marcador renderizado **jamás** debe poder emitir. No están
#: ahí hoy porque todos los valores son numéricos o un sello, pero son la
#: razón por la que la comprobación existe: si mañana alguien añade un marcador
#: con texto, esta lista lo frena.
_RESERVED_IN_VALUE: frozenset[str] = frozenset("&#\r\n\t \"'<>\\")

#: Caracteres de control que pueden aparecer en una plantilla y romper la
#: cabecera de una petición HTTP si la URL acaba en una cabecera.
_CONTROL_IN_TEMPLATE: frozenset[str] = frozenset("\r\n\t\x00")

#: Tope propio de duración para **una** petición histórica, en segundos.
#:
#: El estándar de los reproductores IPTV no define un máximo por petición y
#: los paneles no lo publican: cada uno impone el suyo y no hay forma de
#: consultarlo sin red. Inventar un límite interno sin dato sería la suposición
#: que el SDD prohíbe, pero dejar la duración sin tope permite que un dato
#: corrupto (un EPG con `stop` en el año 3000) genere una petición absurda.
#:
#: Por eso el tope es **nuestro y declarado**: 12 horas, que cubre de sobra un
#: programa largo o una mirarada de tarde, y corta cualquier cosa que no sea
#: una entrada de guía. Está justificado en ``docs/sdd/catchup-temporal.md`` §5.3.
MAX_REQUEST_SECONDS: int = 12 * 3600

#: Dónde se justifica el tope. Va en el propio módulo para que el número y su
#: razónviajen juntos: si algún día cambia el valor, el docstring obliga a
#: actualizar la referencia.
MAX_REQUEST_SECONDS_DOC: str = "docs/sdd/catchup-temporal.md"


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------


class CatchupError(IPTVError):
    """Fallo controlado del dominio catch-up (mensaje apto para modal)."""


class CatchupNotAvailableError(CatchupError):
    """§10: el proveedor no declaró archivo, o lo declaró sin ventana."""


class CatchupOutsideArchiveWindowError(CatchupError):
    """§13: el programa ya no está dentro de la ventana declarada."""


class CatchupInvalidIntervalError(CatchupError):
    """CU-02: el intervalo pedido no tiene sentido y se rechaza.

    Nace en la Fase 1 para separar un problema **nuestro** (el intervalo es
    imposible o excesivo) de un problema **del proveedor** (fuera de ventana,
    sin archivo declarado). Antes no existía porque el intervalo inválido se
    normalizaba en silencio a un segundo: un error que está en el dato de
    entrada no se convertía en ningún mensaje.
    """


# ---------------------------------------------------------------------------
# Capacidad
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatchupCapability:
    """Representación interna de la capacidad de archivo de **un** canal.

    Es la traducción de lo que dice el proveedor (``tv_archive*`` en Xtream,
    ``catchup*`` en M3U). La UI nunca lee esos atributos: siempre pasa por
    :func:`capability_for` (§4).

    Attributes:
        enabled: el proveedor declaró archivo **y** ventana utilizable.
        provider_declared: el proveedor dijo algo sobre el archivo de este
            canal. ``False`` cuando no dijo nada (no es lo mismo que "no").
        archive_duration_seconds: ventana máxima declarada, en segundos.
            ``0`` si el proveedor no la dio (fail-closed, §16).
        source_template: plantilla ``catchup-source`` declarada (sólo M3U).
        protocol: ``xtream`` | ``m3u`` | ``generic``.
    """

    enabled: bool = False
    provider_declared: bool = False
    archive_duration_seconds: int = 0
    source_template: str = ""
    protocol: str = PROTOCOL_GENERIC

    @property
    def archive_duration_days(self) -> int:
        """Ventana declarada redondeada a días (sólo para mostrar)."""
        if self.archive_duration_seconds <= 0:
            return 0
        return max(1, round(self.archive_duration_seconds / SECONDS_PER_DAY))

    @property
    def has_window(self) -> bool:
        """True si hay ventana declarada (sin ella no hay catch-up, §16)."""
        return self.archive_duration_seconds > 0


#: Sin declaración del proveedor: la respuesta por defecto de todo el
#: proyecto. Cualquier camino que dude acaba aquí.
DISABLED: CatchupCapability = CatchupCapability()


class CatchupState(Enum):
    """Estados de catch-up (§12).

    ``LOADING``, ``PLAYING`` y ``ERROR`` son estados de la sesión de
    reproducción y los posee la UI; :func:`classify` sólo devuelve los cuatro
    estables, que dependen únicamente de lo que declaró el proveedor y de si el
    intervalo pedido tiene sentido.
    """

    DISABLED_BY_PROVIDER = "disabled_by_provider"
    ENABLED = "enabled"
    OUTSIDE_ARCHIVE_WINDOW = "outside_archive_window"
    AVAILABLE = "available"
    #: CU-02: el intervalo pedido es imposible o excesivo. Es un problema del
    #: dato de la guía, no del proveedor: se distingue de
    #: ``OUTSIDE_ARCHIVE_WINDOW`` porque la reacción es otra (informar del dato,
    #: no esperar a que vuelva la copia).
    INVALID_INTERVAL = "invalid_interval"
    LOADING = "loading"
    PLAYING = "playing"
    ERROR = "error"


def _as_int(value: object) -> int:
    """Convierte a int tolerando lo que llega de un panel: ``7``, ``"7"``,
    ``"7d"``, ``7.0``, ``""``. Devuelve 0 si no hay número."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
        elif ch in "+-":
            continue
        else:
            break
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def _aware(moment: datetime) -> datetime:
    """Un EPG sin offset se interpreta en hora local (nunca naive vs aware)."""
    return moment if moment.tzinfo is not None else moment.astimezone()


def from_xtream_stream(stream: object) -> CatchupCapability:
    """Traduce un ``XtreamStream`` a capacidad interna (§4, tests 1-3 §19).

    ``tv_archive >= 1`` **y** ``tv_archive_duration > 0`` → habilitada.

    Un panel que declara ``tv_archive = 1`` sin duración queda **sin**
    catch-up: es fail-closed (§16) y es lo correcto, porque sin ventana
    declarada no se puede saber qué es reproducible. Está documentado como
    riesgo asumido en el README, no relajado aquí.

    Args:
        stream: ``XtreamStream`` (o cualquier objeto con esos atributos).

    Returns:
        La capacidad, o :data:`DISABLED` si el proveedor no declaró ventana.
    """
    archive = _as_int(getattr(stream, "tv_archive", None))
    duration = _as_int(getattr(stream, "tv_archive_duration", None))
    return _xtream_capability(archive, duration)


def _xtream_capability(archive: int, duration_days: int) -> CatchupCapability:
    declared = archive >= 1
    seconds = duration_days * SECONDS_PER_DAY if duration_days > 0 else 0
    if not declared or seconds <= 0:
        return DISABLED
    return CatchupCapability(
        enabled=True,
        provider_declared=True,
        archive_duration_seconds=seconds,
        source_template="",
        protocol=PROTOCOL_XTREAM,
    )


def from_m3u_attrs(attrs: dict[str, str]) -> CatchupCapability:
    """Traduce los atributos ``catchup*`` de un ``#EXTINF`` a capacidad (§5).

    Habilita sólo si se cumplen **las tres** condiciones:

    1. ``catchup`` presente y distinto de ``none``/vacío;
    2. ``catchup-source`` presente (sin mecanismo declarado no hay cómo
       construir la petición);
    3. ``catchup-days`` > 0 (sin ventana declarada → fail-closed).

    Args:
        attrs: ``Channel.attrs`` tal cual lo deja ``m3u_parser`` (claves con
            ``-`` normalizadas a ``_``, lo no reconocido en este dict).

    Returns:
        La capacidad, o :data:`DISABLED`.
    """
    data = attrs if isinstance(attrs, dict) else {}
    mechanism = str(data.get("catchup") or "").strip().casefold()
    if mechanism in _CATCHUP_DISABLED_VALUES:
        return DISABLED
    template = str(data.get("catchup_source") or "").strip()
    if not template:
        return DISABLED
    days = _as_int(data.get("catchup_days"))
    if days <= 0:
        return DISABLED
    return CatchupCapability(
        enabled=True,
        provider_declared=True,
        archive_duration_seconds=days * SECONDS_PER_DAY,
        source_template=template,
        protocol=PROTOCOL_M3U,
    )


def capability_for(channel: "Channel | object") -> CatchupCapability:
    """Capacidad catch-up de un canal. **Único punto** por el que se pregunta.

    Despacha por lo que el proveedor declaró, sin mirar nada más:

    - canal normalizado desde Xtream (``attrs['xtream_id']``): se leen
      ``tv_archive`` / ``tv_archive_duration``;
    - cualquier otro (M3U, listas plugged): se leen ``catchup``,
      ``catchup_days`` y ``catchup_source``.

    La UI **no** lee ninguna de esas claves: siempre pasa por aquí (§4).

    Args:
        channel: el :class:`~thetvview.models.Channel` seleccionado.

    Returns:
        La capacidad derivada, o :data:`DISABLED`.
    """
    attrs = getattr(channel, "attrs", None)
    if not isinstance(attrs, dict) or not attrs:
        return DISABLED
    if attrs.get("xtream_id"):
        return _xtream_capability(
            _as_int(attrs.get("tv_archive")), _as_int(attrs.get("tv_archive_duration"))
        )
    return from_m3u_attrs(attrs)


def can_use_catchup(capability: CatchupCapability | None) -> bool:
    """**La invariante única** (§7 / §21). Un solo sitio, sin excepciones.

    Devuelve ``True`` sólo si el proveedor declaró archivo para ese canal
    **y** la capacidad está habilitada. Nada más habilita catch-up: ni el
    EPG, ni la URL live, ni el protocolo del proveedor.

    Args:
        capability: resultado de :func:`capability_for` (o :data:`DISABLED`).
    """
    return bool(
        capability is not None
        and capability.enabled is True
        and capability.provider_declared is True
    )


# ---------------------------------------------------------------------------
# Ventana temporal (§4, §5)
# ---------------------------------------------------------------------------


def archive_start(now: datetime, capability: CatchupCapability) -> datetime | None:
    """``archiveStart = now - archiveDuration`` (§5, FR-005).

    Returns:
        El inicio de la ventana, o ``None`` si el proveedor no declaró
        ventana (fail-closed: no hay ventana, no hay archivo).
    """
    if capability is None or capability.archive_duration_seconds <= 0:
        return None
    return _aware(now) - timedelta(seconds=capability.archive_duration_seconds)


def is_within_archive_window(
    program: "Program",
    capability: CatchupCapability,
    now: datetime,
) -> bool:
    """§5: True si el intervalo del programa intersecta ``[archivo, now]``.

    Tres condiciones, todas necesarias:

    - la capacidad es utilizable (:func:`can_use_catchup`);
    - el programa ya empezó (``program.start <= now``);
    - el final del programa es posterior al inicio de la ventana
      (``program.stop > archive_start``), o no tiene final conocido.
    """
    if not can_use_catchup(capability):
        return False
    window_start = archive_start(now, capability)
    if window_start is None:
        return False
    moment = _aware(now)
    start = _aware(program.start)
    if start > moment:
        return False  # aún no se ha emitido: no es catch-up
    raw_stop = getattr(program, "stop", None)
    stop = _aware(raw_stop) if raw_stop is not None else None
    if stop is not None and stop <= window_start:
        return False  # anterior a la ventana declarada (test 6 §19)
    return True


def classify(
    channel: "Channel | object",
    program: "Program | None",
    now: datetime,
) -> CatchupState:
    """Estado estable de catch-up para un programa concreto (§12).

    Devuelve uno de los tres estados que dependen sólo del proveedor:

    - :attr:`CatchupState.DISABLED_BY_PROVIDER` — no hay declaración, o la
      declaración llega sin ventana utilizable;
    - :attr:`CatchupState.AVAILABLE` — declarado y dentro de la ventana;
    - :attr:`CatchupState.OUTSIDE_ARCHIVE_WINDOW` — declarado pero el
      programa queda fuera de la ventana.

    Los estados de sesión (``LOADING``, ``PLAYING``, ``ERROR``) no salen de
    aquí: los posee la UI, porque dependen del reproductor y no del
    proveedor (§13 exige distinguirlos, no mezclarlos aquí).
    """
    capability = capability_for(channel)
    if not can_use_catchup(capability):
        return CatchupState.DISABLED_BY_PROVIDER
    if program is not None and is_within_archive_window(program, capability, now):
        return CatchupState.AVAILABLE
    return CatchupState.OUTSIDE_ARCHIVE_WINDOW


def classify_request(
    capability: CatchupCapability, request: "CatchupRequest", now: datetime
) -> CatchupState:
    """Igual que :func:`classify`, pero sobre una petición ya construida.

    Existe para que la validación de §10 use **el mismo** criterio que la
    de la guía: si aquí sale ``AVAILABLE``, allí también.

    **Nunca lanza por datos**: es una consulta, y una consulta que lanza deja al
    llamante sin estado que mostrar. Un intervalo imposible devuelve
    :attr:`CatchupState.INVALID_INTERVAL` (CU-02), que es «este dato no sirve»
    y no «el proveedor no lo tiene».

    Para construir la URL hay que pasar por :func:`validar_intervalo`, que sí
    lanza con el motivo apto para modal.
    """
    if not can_use_catchup(capability):
        return CatchupState.DISABLED_BY_PROVIDER
    if not request.intervalo_valido():
        return CatchupState.INVALID_INTERVAL
    if is_within_archive_window(request.to_program(), capability, now):
        return CatchupState.AVAILABLE
    return CatchupState.OUTSIDE_ARCHIVE_WINDOW


def describe_state(
    state: CatchupState, capability: CatchupCapability | None = None
) -> str:
    """Mensaje en español, apto para modal, que explica **por qué** (§13).

    Distingue las tres situaciones que el usuario perceives distinto:
    el proveedor no ofrece archivo, el contenido se salió de la ventana, o
    la reproducción falló.
    """
    if state is CatchupState.DISABLED_BY_PROVIDER:
        return (
            "Este proveedor no ofrece archivo (catch-up) para este canal. "
            "Sólo puedes ver el directo."
        )
    if state is CatchupState.OUTSIDE_ARCHIVE_WINDOW:
        days = capability.archive_duration_days if capability else 0
        ventana = (
            f"Su archivo sólo guarda los últimos {days} "
            f"{'día' if days == 1 else 'días'}."
            if days
            else "No hay ventana de archivo declarada."
        )
        return f"Este contenido ya no está disponible en el archivo. {ventana}"
    if state is CatchupState.INVALID_INTERVAL:
        return (
            "El horario de este programa no sirve para pedir el archivo: la "
            "guía dice que termina antes de empezar, o dura demasiado. Es un "
            "dato de la guía, no un problema del proveedor."
        )
    if state is CatchupState.ERROR:
        return (
            "El proveedor ofrece archivo y la petición se construyó, pero la "
            "reproducción falló. Puede ser que la copia ya no exista."
        )
    return "Estado de archivo del proveedor sin cambios."


# ---------------------------------------------------------------------------
# Petición histórica (§9, §10)
# ---------------------------------------------------------------------------


def _aware_seconds(moment: datetime) -> int:
    """Epoch en segundos, sin depender del módulo ``time`` ni de la zona."""
    return int(_aware(moment).astimezone(timezone.utc).timestamp())


@dataclass(frozen=True)
class CatchupRequest:
    """Qué se quiere reproducir del archivo (§9).

    ``start`` admite un instante arbitrario, no sólo el inicio del programa:
    por eso cubre también el caso de *seek* (test 7 §19). ``end`` acota la
    duración.

    Attributes:
        channel_id: identificador del stream en el proveedor.
        start: instante de inicio (el del seek, si lo hay).
        end: instante final.
    """

    channel_id: str
    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> int:
        """Duración solicitada en segundos (mínimo 1).

        **No lanza, aunque el intervalo sea imposible** (``end`` anterior a
        ``start``): este valor es diagnóstico y alimenta el texto del modal y
        el informe. Lo que impide construir una URL es
        :func:`validar_intervalo`, que se llama en :func:`build_playback_request`.

        El mínimo de 1 s se conserva por compatibilidad de la referencia opaca
        (``CatchupRef.parse`` usa el mismo mínimo al leer). Lo que ya **no**
        ocurre es que ese mínimo decida el resultado por sí solo.
        """
        delta = (_aware(self.end) - _aware(self.start)).total_seconds()
        return max(1, int(round(delta)))

    def intervalo_valido(self) -> bool:
        """True si el intervalo tiene sentido: ``end`` posterior a ``start``.

        Comprobación pura, sin topes: responde sólo a la pregunta mínima
        "¿está esto al revés?". El resto de las reglas vive en
        :func:`validar_intervalo`.
        """
        if self.start is None or self.end is None:
            return False
        return _aware(self.end) > _aware(self.start)

    def to_program(self) -> "Program":
        """El intervalo de la petición, como si fuera un programa EPG.

        Permite reutilizar :func:`is_within_archive_window` sin duplicar la
        regla de la ventana.
        """
        from .models import Program

        return Program(
            channel_id=self.channel_id,
            title="",
            start=self.start,
            stop=self.end,
        )


@dataclass(frozen=True)
class PlaybackRequest:
    """Lo que el reproductor va a recibir (§6/§9).

    La capa de reproducción **no** sabe cómo construyó cada proveedor su
    URL: recibe esto y la pasa al mismo motor que el directo (§14).

    Attributes:
        channel: canal del dominio (se reemplaza por una copia con la URL
            resuelta justo antes de lanzar).
        url: URL opaca (``xtream-ts://``) o la URL declarada ya renderizada.
        start: instante de inicio solicitado.
        duration: duración solicitada en segundos.
        title: título del programa, sólo informativo.
    """

    channel: "Channel"
    url: str
    start: datetime
    duration: int
    title: str = ""


class CatchupAdapter:
    """Interfaz de construcción de la petición histórica (§9).

    Un adaptador por mecanismo declarado. Ninguno hace red: sólo construyen
    la referencia que más tarde resuelve ``stream_ref`` con las
    credenciales en caliente.
    """

    #: Mecanismo que implementa (``xtream`` | ``m3u`` | ``generic``).
    protocol: str = PROTOCOL_GENERIC

    def can_handle(self, capability: CatchupCapability) -> bool:
        """True si este adaptador sabe construir peticiones para `capability`."""
        raise NotImplementedError

    def build(
        self,
        channel: "Channel",
        capability: CatchupCapability,
        request: CatchupRequest,
    ) -> PlaybackRequest:
        """Construye la :class:`PlaybackRequest` de este canal e intervalo."""
        raise NotImplementedError

    # -- utilidades compartidas --------------------------------------------

    @staticmethod
    def _validate_playable(url: str) -> str:
        """Pasa la URL por la política de stream antes de devolverla.

        Cubre el riesgo declarado de ``catchup-source``: es una plantilla
        publicada por un tercero, así que se valida como cualquier otra
        dirección (``file://``, ``javascript:``, esquemas raros y flags de
        inyección de argumentos quedan fuera).
        """
        from .security.url_policy import PURPOSE_STREAM, validate_url

        if not isinstance(url, str) or not url.strip():
            raise CatchupError(
                "El proveedor declaró una plantilla de archivo vacía; "
                "no se puede construir la petición."
            )
        try:
            validate_url(url, purpose=PURPOSE_STREAM)
        except IPTVError as exc:
            raise CatchupError(
                "La dirección de archivo que declara el proveedor no es válida, "
                f"así que no se intenta reproducir: {exc}"
            ) from exc
        return url


class XtreamCatchupAdapter(CatchupAdapter):
    """Mecanismo declarado de Xtream Codes: ``streaming/timeshift.php``.

    No se **adivina**: se construye contra la referencia opaca
    ``xtream-ts://…``, que ``stream_ref`` resuelve con las credenciales del
    catálogo en el último instante (§2.1 del plan). Así ninguna credencial
    pasa por el dominio, por favoritos, por recientes ni por disco.
    """

    protocol = PROTOCOL_XTREAM

    def can_handle(self, capability: CatchupCapability) -> bool:
        return capability is not None and capability.protocol == PROTOCOL_XTREAM

    def build(
        self,
        channel: "Channel",
        capability: CatchupCapability,
        request: CatchupRequest,
    ) -> PlaybackRequest:
        ref = build_catchup_ref(channel, request)
        return PlaybackRequest(
            channel=channel,
            url=ref.to_opaque(),
            start=request.start,
            duration=request.duration_seconds,
            title="",
        )


class M3UCatchupAdapter(CatchupAdapter):
    """Mecanismo declarado en M3U: la plantilla ``catchup-source``.

    Se renderizan **sólo** los placeholders reconocidos (§1.1 del plan). Un
    placeholder desconocido es un error con mensaje: es preferible decir
    "no reconocemos esta plantilla" a adivinar una sintaxis de terceros.
    """

    protocol = PROTOCOL_M3U

    def can_handle(self, capability: CatchupCapability) -> bool:
        return (
            capability is not None
            and capability.protocol == PROTOCOL_M3U
            and bool(capability.source_template)
        )

    def build(
        self,
        channel: "Channel",
        capability: CatchupCapability,
        request: CatchupRequest,
    ) -> PlaybackRequest:
        rendered = render_source_template(
            capability.source_template, request
        )
        self._validate_playable(rendered)
        return PlaybackRequest(
            channel=channel,
            url=rendered,
            start=request.start,
            duration=request.duration_seconds,
            title="",
        )


class GenericCatchupAdapter(CatchupAdapter):
    """Adaptador vacío, a propósito (riesgo nº5 del plan).

    Es el sitio donde viviría un tercer mecanismo si algún día lo hubiera.
    Hoy ``can_handle`` devuelve ``False`` **siempre**, así que garantiza
    mecánicamente que no se genere ninguna URL especulativa (§17).
    """

    protocol = PROTOCOL_GENERIC

    def can_handle(self, capability: CatchupCapability) -> bool:
        return False

    def build(
        self,
        channel: "Channel",
        capability: CatchupCapability,
        request: CatchupRequest,
    ) -> PlaybackRequest:
        raise CatchupError(
            "Este proveedor declara archivo, pero theTVVIEW no reconoce el "
            "mecanismo. Se necesita más información para construir la petición."
        )


#: Registro de adaptadores, en orden de preferencia.
_ADAPTERS: tuple[CatchupAdapter, ...] = (
    XtreamCatchupAdapter(),
    M3UCatchupAdapter(),
    GenericCatchupAdapter(),
)


def adapter_for(capability: CatchupCapability) -> CatchupAdapter | None:
    """Adaptador que sabe construir peticiones para `capability`.

    Devuelve ``None`` si ninguno la maneja: es el camino fail-closed cuando
    el proveedor declara un mecanismo que no reconocemos.
    """
    for adapter in _ADAPTERS:
        try:
            if adapter.can_handle(capability):
                return adapter
        except Exception:  # noqa: BLE001 - un adaptador roto no rompe el flujo
            continue
    return None


def build_playback_request(
    channel: "Channel",
    capability: CatchupCapability,
    request: "CatchupRequest",
    *,
    now: datetime | None = None,
) -> PlaybackRequest:
    """Valida (§10) y **después** construye la petición. Nunca al revés.

    El orden es el contrato del SDD y no es decorativo: ninguna URL se
    construye hasta que la capacidad y la ventana están verificadas, de
    modo que no se puede lanzar una petición histórica por error.

    Args:
        channel: canal elegido (de él sale el ``channel_id`` si falta).
        capability: resultado de :func:`capability_for`.
        request: intervalo solicitado (admite seek).
        now: instante de referencia; por defecto, ``datetime.now()``.

    Raises:
        CatchupNotAvailableError: el proveedor no declaró archivo (§10).
        CatchupOutsideArchiveWindowError: el intervalo quedó fuera (§13).
        CatchupError: no hay adaptador, o la plantilla declarada es
            inservible (placeholder desconocido, esquema no permitido).
    """
    if not can_use_catchup(capability):
        raise CatchupNotAvailableError(
            "Este canal no tiene archivo declarado por el proveedor, así que "
            "no se genera ninguna petición histórica."
        )
    moment = _aware(now) if now is not None else _aware(datetime.now())
    # CU-02: el intervalo se comprueba **antes** de la ventana. Un intervalo
    # imposible es un problema nuestro y no del proveedor: si se comprobara
    # después, un intervalo invertido dentro de la ventana se clasificaría
    # AVAILABLE y llegaría al reproductor.
    validar_intervalo(request)
    if not is_within_archive_window(request.to_program(), capability, moment):
        raise CatchupOutsideArchiveWindowError(
            describe_state(CatchupState.OUTSIDE_ARCHIVE_WINDOW, capability)
        )
    adapter = adapter_for(capability)
    if adapter is None:
        raise CatchupError(
            "El proveedor declara archivo, pero theTVVIEW no reconoce su "
            "mecanismo; no se intenta construir la dirección."
        )
    return adapter.build(channel, capability, request)


# ---------------------------------------------------------------------------
# Plantilla catchup-source (§5)
# ---------------------------------------------------------------------------


def template_values(request: CatchupRequest) -> dict[str, object]:
    """Valores derivados de **nuestros** ``datetime``, nada más.

    ============== ==================================================
    ``{start}``    epoch UTC, segundos (admite seek)
    ``{end}``      epoch UTC, segundos
    ``{duration}`` duración pedida, en segundos
    ``{utc}``      sello ``YYYY-MM-DD:HH-MM-SS`` del instante en **UTC**
    ============== ==================================================

    Se calculan aquí, no se interpolan desde fuera: un valor crudo del
    proveedor no llega nunca al texto de la URL. Y todos son seguros en una
    query: nada de espacios ni de caracteres que la política de URL
    (``validate_url(purpose="stream")``) pueda rechazar.

    ``{utc}`` es **UTC** y no hora local (CU-03). Antes se hacía
    ``start.astimezone()``, que deshacía la conversión a UTC y devolvía la hora
    del proceso: en un equipo en Madrid el proveedor recibía una hora de más y
    devolvía otro momento del día. El nombre del marcador no es una convención
    de este proyecto: es el del estándar de los reproductores IPTV, donde
    ``{utc}``, ``{Y}``, ``{m}``, ``{d}``, ``{H}``, ``{M}`` y ``{S}`` son todos
    UTC. Ver ``docs/sdd/catchup-temporal.md`` §3.
    """
    inicio_utc = _aware(request.start).astimezone(timezone.utc)
    return {
        "start": _aware_seconds(request.start),
        "end": _aware_seconds(request.end),
        "duration": request.duration_seconds,
        # Mismo formato de facto que usan los paneles Xtream Codes para
        # timeshift.php; sin espacios, para que la query siga siendo válida.
        "utc": inicio_utc.strftime("%Y-%m-%d:%H-%M-%S"),
    }


def _validar_valor_renderizado(nombre: str, valor: object) -> str:
    """Comprueba que un valor renderizado sea apto para una query.

    Es una defensa en profundidad: hoy los valores son un epoch o un sello, y
    ninguno de los dos tiene nada sospechoso. La comprobación existe para que
    **añadir un marcador con texto** en el futuro no introduzca una inyección
    de parámetros ni un salto de línea en la URL.

    Raises:
        CatchupError: el valor es demasiado largo o trae un carácter prohibido.
    """
    texto = str(valor)
    if len(texto) > MAX_TEMPLATE_VALUE_LENGTH:
        raise CatchupError(
            f"El valor del marcador «{{{nombre}}}» es demasiado largo para "
            f"una dirección ({len(texto)} caracteres); no se intenta construir "
            "la petición."
        )
    malo = _RESERVED_IN_VALUE.intersection(texto)
    if malo:
        raise CatchupError(
            f"El valor del marcador «{{{nombre}}}» trae caracteres que no son "
            "válidos en una dirección; no se intenta construir la petición."
        )
    return texto


def render_source_template(template: str, request: CatchupRequest) -> str:
    """Rellena ``{start}``/``{end}``/``{duration}``/``{utc}`` de la plantilla.

    Se usa ``string.Formatter`` en vez de ``str.replace`` para poder
    **rechazar** lo que no se reconoce en vez de dejarlo pasar literal.

    Raises:
        CatchupError: placeholder desconocido, especificadores de formato,
            sintaxis rota, o plantilla sin ningún marcador de tiempo (eso
            significaría una URL fija que no lleva a ninguna parte).
    """
    if not isinstance(template, str) or not template.strip():
        raise CatchupError(
            "El proveedor declaró una plantilla de archivo vacía; "
            "no se puede construir la petición."
        )
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise CatchupError(
            "La plantilla de archivo del proveedor es demasiado larga "
            f"({len(template)} caracteres) para ser una dirección de archivo; "
            "no se intenta construir la petición."
        )
    if _CONTROL_IN_TEMPLATE.intersection(template):
        raise CatchupError(
            "La plantilla de archivo del proveedor trae saltos de línea o "
            "caracteres de control; no se intenta construir la dirección."
        )
    values = template_values(request)
    chunks: list[str] = []
    used: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise CatchupError(
            "La plantilla de archivo del proveedor está mal formada; "
            "no se intenta construir la dirección."
        ) from exc
    for literal, field_name, spec, conversion in parsed:
        chunks.append(literal)
        if field_name is None:
            continue
        if spec or conversion:
            raise CatchupError(
                "La plantilla de archivo del proveedor usa formatos que "
                "theTVVIEW no admite; no se intenta construir la dirección."
            )
        if field_name not in _TEMPLATE_PLACEHOLDERS:
            known = ", ".join(_TEMPLATE_PLACEHOLDERS)
            raise CatchupError(
                f"La plantilla de archivo usa «{{{field_name}}}», que no es de "
                f"las que theTVVIEW reconoce ({known}). Es mejor no adivinar "
                "la sintaxis del proveedor."
            )
        used.add(field_name)
        chunks.append(_validar_valor_renderizado(field_name, values[field_name]))
    if not used:
        raise CatchupError(
            "La plantilla de archivo del proveedor no indica dónde colocar el "
            "instante; no se intenta construir la dirección."
        )
    return CatchupAdapter._validate_playable("".join(chunks))


# ---------------------------------------------------------------------------
# Validación del intervalo (CU-02)
# ---------------------------------------------------------------------------


def validar_intervalo(
    request: "CatchupRequest",
    *,
    maximo: int = MAX_REQUEST_SECONDS,
) -> None:
    """Valida el intervalo pedido **antes** de construir nada (CU-02).

    Es el orden que exige el SDD §10: primero se comprueba que la petición tiene
    sentido, después se decide si el proveedor la sirve. Antes de la Fase 1 esa
    comprobación no existía y el intervalo imposible se normalizaba a un
    segundo en silencio (``max(1, …)``): una petición invertida llegaba al
    reproductor como si fuera válida.

    Los cuatro motivos se distinguen porque el usuario reacciona distinto a
    cada uno:

    1. **Falta un extremo del intervalo** — no hay nada que pedir.
    2. **El fin es igual al inicio** — intervalo de duración cero.
    3. **El fin es anterior al inicio** — intervalo invertido.
    4. **La duración supera el tope** — el dato es disparatado o la petición,
       demasiado larga.

    Un programa que cruza medianoche **no** es un caso especial: es un
    intervalo válido con fin posterior al inicio.

    Args:
        request: la petición a comprobar.
        maximo: duración máxima admitida en segundos.

    Raises:
        CatchupInvalidIntervalError: el intervalo no sirve, o ``maximo`` no es
            un entero positivo. Los dos son fallos controlados con motivo.
    """
    try:
        tope = int(maximo)
    except (TypeError, ValueError) as exc:
        raise CatchupInvalidIntervalError(
            f"El tope de duración tiene que ser un entero en segundos, no {maximo!r}."
        ) from exc
    if tope <= 0:
        raise CatchupInvalidIntervalError(
            f"El tope de duración tiene que ser mayor que cero, no {tope}."
        )

    inicio = getattr(request, "start", None)
    fin = getattr(request, "end", None)
    if not isinstance(inicio, datetime) or not isinstance(fin, datetime):
        raise CatchupInvalidIntervalError(
            "No se sabe qué intervalo pedir: a este programa le falta la hora "
            "de inicio o la de fin en la guía, así que no se intenta reproducir "
            "el archivo."
        )

    if _aware(fin) > _aware(inicio):
        delta = (_aware(fin) - _aware(inicio)).total_seconds()
    elif _aware(fin) == _aware(inicio):
        raise CatchupInvalidIntervalError(
            "Este programa empieza y termina a la misma hora, así que no hay "
            "nada que reproducir del archivo."
        )
    else:
        raise CatchupInvalidIntervalError(
            "Este programa termina antes de empezar. No es un problema del "
            "proveedor: es un dato de la guía, así que no se intenta reproducir "
            "el archivo."
        )

    if delta > tope:
        pedidas = delta / 3600
        if tope % 3600 == 0 and tope / 3600 == 1:
            maximo_texto = "1 hora"
        elif tope % 3600 == 0:
            maximo_texto = f"{tope // 3600} horas"
        else:
            maximo_texto = f"{tope / 3600:.1f} horas"
        raise CatchupInvalidIntervalError(
            f"Esta grabación dura {pedidas:.1f} horas y el máximo por intento es "
            f"{maximo_texto}. Reproduce sólo la parte del programa que te interese."
        )


# ---------------------------------------------------------------------------
# Referencia opaca de catch-up (§2.1 del plan)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatchupRef:
    """Referencia opaca a un instante del archivo. Sin credenciales.

    Formato::

        xtream-ts://<fuente>/<stream_id>.ts?start=<epoch>&dur=<segundos>

    Es el mismo patrón que ``StreamRef`` (``xtream://``), con un segundo
    prefijo resuelto en el mismo sitio. Ventaja: ``favorites.json`` y
    ``recents.json`` no pueden filtrar credenciales, porque aquí no hay
    ninguna. Y si se escapara sin resolver, ``validate_url`` rechaza el
    esquema y el reproductor nunca lo ve.

    Attributes:
        source_name: nombre de la fuente en el catálogo (clave de
            credenciales).
        stream_id: identificador del stream en el panel.
        start_epoch: instante de inicio, epoch UTC en segundos.
        duration_seconds: duración solicitada.
        extension: extensión del fichero que devuelve el panel.
    """

    source_name: str
    stream_id: str
    start_epoch: int
    duration_seconds: int
    extension: str = "ts"

    def to_opaque(self) -> str:
        """URL opaca, sin usuario ni contraseña.

        >>> CatchupRef("Panel", "101", 1700000000, 3600).to_opaque()
        'xtream-ts://Panel/101.ts?start=1700000000&dur=3600'
        """
        return (
            f"{TS_PREFIX}{quote(str(self.source_name), safe='')}/"
            f"{quote(str(self.stream_id), safe='')}."
            f"{quote(str(self.extension), safe='')}"
            f"?start={int(self.start_epoch)}&dur={int(self.duration_seconds)}"
        )

    @classmethod
    def parse(cls, url: str) -> "CatchupRef | None":
        """Extrae la referencia de una URL opaca; ``None`` si no lo es."""
        if not isinstance(url, str) or not url.startswith(TS_PREFIX):
            return None
        rest = url[len(TS_PREFIX):]
        path, _, query = rest.partition("?")
        parts = path.split("/")
        if len(parts) != 2:
            return None
        source, leaf = parts
        stream_id, dot, extension = leaf.rpartition(".")
        if not dot or not source or not stream_id:
            return None
        start_raw = dur_raw = ""
        for chunk in query.split("&"):
            key, _, value = chunk.partition("=")
            if key == "start":
                start_raw = value
            elif key == "dur":
                dur_raw = value
        if not start_raw:
            return None
        return cls(
            source_name=unquote(source),
            stream_id=unquote(stream_id),
            start_epoch=_as_int(unquote(start_raw)),
            duration_seconds=max(1, _as_int(unquote(dur_raw))),
            extension=unquote(extension),
        )

    def start_datetime(self) -> datetime:
        """El instante de inicio, como ``datetime`` consciente de zona."""
        return datetime.fromtimestamp(int(self.start_epoch), tz=timezone.utc).astimezone()

    def resolve(self, manager: object) -> str:
        """URL real con credenciales, consultando el catálogo en caliente.

        Igual que ``StreamRef.resolve``: la contraseña se pide al catálogo en
        el último momento y no viaja con la referencia.

        Raises:
            MissingCredentialsError: la fuente no existe o no tiene
                contraseña guardada. El mensaje no lleva secretos.
        """
        from .stream_ref import MissingCredentialsError

        creds = None
        getter = getattr(manager, "get_credentials", None)
        if callable(getter):
            try:
                creds = getter(self.source_name)
            except Exception:  # noqa: BLE001 - un almacén roto no rompe el flujo
                creds = None
        if not creds:
            raise MissingCredentialsError(
                f"No tengo credenciales guardadas para la fuente "
                f"'{self.source_name}'; abre esa lista y vuelve a intentarlo."
            )
        server_url, username, password = creds
        from .xtream_security import build_timeshift_url

        return build_timeshift_url(
            server_url,
            username,
            password,
            self.stream_id,
            int(self.start_epoch),
            int(self.duration_seconds),
            self.extension,
        )


def build_catchup_ref(channel: "Channel", request: CatchupRequest) -> CatchupRef:
    """Construye la referencia opaca de catch-up de un canal Xtream.

    La fuente y el ``stream_id`` salen de lo que el propio canal ya
    transporta: la referencia live opaca (``xtream://``) o los atributos que
    dejó ``normalize_live_stream``. No se adivinan.

    Raises:
        CatchupError: el canal no viene de una fuente Xtream reconocible, así
            que no hay forma segura de construir la referencia.
    """
    from .stream_ref import StreamRef

    attrs = getattr(channel, "attrs", None) or {}
    source_name = str(attrs.get("source_name") or "").strip()
    stream_id = str(attrs.get("xtream_id") or "").strip()
    url = getattr(channel, "url", "") or ""
    live_ref = StreamRef.parse(url)
    if live_ref is not None:
        source_name = source_name or live_ref.source_name
        stream_id = stream_id or live_ref.stream_id
    if not source_name or not stream_id:
        raise CatchupError(
            "No se puede identificar este canal en su proveedor, así que no "
            "se construye ninguna dirección de archivo."
        )
    return CatchupRef(
        source_name=source_name,
        stream_id=stream_id,
        start_epoch=_aware_seconds(request.start),
        duration_seconds=request.duration_seconds,
    )


def playback_channel(playback: PlaybackRequest) -> "Channel":
    """Copia del canal con la URL de la petición (listo para el motor).

    El canal original **no** se toca: la caché, favoritos y recientes
    siguen guardando la referencia opaca del directo, sin credenciales y
    sin el instante de archivo.
    """
    return replace(playback.channel, url=playback.url)