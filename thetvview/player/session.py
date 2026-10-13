"""Ciclo de vida de una sesión de reproducción (SDD P1.1 §4, plan Fase 1).

El problema
-----------

Hoy «reproducir un canal» son once llamadas repartidas entre
:func:`thetvview.ui.screens.play_channel`, :class:`NowPlayingScreen` y
:class:`~thetvview.player.supervisor.ReconnectSupervisor`, y **ninguna** se
pregunta qué sesión es. De ahí salen los tres fallos que más molestan:

- nada guarda un identificador, así que una tarea tardía de la sesión A puede
  tocar el estado de la sesión B sin que nadie lo note;
- nada dice **por qué** terminó una sesión: :meth:`NowPlayingScreen.exit_summary`
  compone texto a mano y un fallo de limpieza puede pisar el motivo real;
- la limpieza está escrita **a mano** en dos sitios (``stop_tracks`` y el
  ``except`` de ``play_channel``), así que un recurso nuevo se olvida.

Qué hay aquí
------------

Tres piezas puras —**sin curses, sin red, sin subprocess**— que se pueden probar
enteras sin lanzar un solo binario:

1. :class:`PlaybackSession`: identidad, estado, fase, marcas temporales,
   intentos y motivo de finalización.
2. :func:`transition` + :data:`_ALLOWED`: la máquina de estados que **rechaza**
   el salto inválido en vez de aceptarlo en silencio.
3. :class:`ResourceScope`: todo lo que una sesión adquiere tiene un dueño y una
   ruta de liberación, y :meth:`ResourceScope.close_all` es idempotente y
   tolerante a fallos.

Y :class:`SessionRegistry`, que es el **guard de obsolescencia** con la misma
forma que :meth:`thetvview.tasks.manager.TaskManager.is_current`: antes de
aplicar un resultado se pregunta si su sesión sigue vigente.

Decisiones que no son obvias
----------------------------

**Estado terminal único con motivo tipado.** El SDD §4.1 permite
``FAILED``/``CANCELLED``/``COMPLETED`` **o** un terminal común con resultado
tipado. Se elige el terminal común (``ENDED``) más
:class:`EndReason`, porque el motivo lo necesita la UI («se cortó y se reconectó»
no es lo mismo que «la app se cerró») y un estado por motivo obligaría a
duplicar toda la máquina por cada uno.

**``phase`` separada de ``state``.** El estado dice *dónde está la sesión*; la
fase dice *qué está haciendo ahora mismo*. ``PLAYING`` con fase ``ANALISIS`` es
un caso real —el stream se abrió y la app todavía está mirando pistas— y con un
solo campo no se podría expresar.

**El error de limpieza no pisa el motivo original.** Si el proceso ya terminó
con ``PLAYBACK_ERROR`` y luego falla ``proxy.stop()``, la sesión sigue
terminando como ``PLAYBACK_ERROR`` y el fallo del proxy se acumula aparte. Es el
requisito explícito del §4.2: «el motivo de error original no se pierde por un
fallo secundario de limpieza».

**Nunca de un terminal a un activo.** ``ENDED`` no tiene aristas de salida, así
que un ``PLAYING`` tardío de un hilo que no comprobó nada se convierte en un
:class:`TransitionError` controlado y no en una sesión resucitada.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..security.errors import IPTVError

__all__ = [
    "SessionState",
    "SessionPhase",
    "EndReason",
    "EventoDeCiclo",
    "SUCESOS_DE_PROBLEMA",
    "AttemptRecord",
    "PlaybackSession",
    "TransitionError",
    "ALLOWED_TRANSITIONS",
    "transition",
    "can_transition",
    "ResourceScope",
    "SessionRegistry",
    "new_session_id",
    "TERMINAL",
]


# ---------------------------------------------------------------------------
# Identidad
# ---------------------------------------------------------------------------

#: Contador de proceso. Un ``uuid4`` ya es único; sumarle el pid y un contador
#: hace que dos sesiones del mismo proceso **se distingan a ojo** en un informe
#: sin tener que abrir el ``uuid`` entero. No sustituye al uuid: lo acompaña.
_SECUENCIA = 0
_SECUENCIA_LOCK = threading.Lock()


def new_session_id() -> str:
    """Identificador nuevo e inmutable de sesión.

    Formato: ``<uuid4-hex-12>-<pid>-<contador>``. Lo que garantiza la unicidad
    es el uuid4 (48 bits de los 12 hex iniciales, más el resto del uuid en la
    cadena completa); el pid y el contador están para que **dos sesiones del
    mismo proceso se distingan a ojo** en un informe, sin abrir el uuid entero.
    """
    global _SECUENCIA
    with _SECUENCIA_LOCK:
        _SECUENCIA += 1
        secuencia = _SECUENCIA
    return f"{uuid.uuid4().hex[:12]}-{_pid()}-{secuencia:04d}"


def _pid() -> int:
    import os

    return os.getpid()


# ---------------------------------------------------------------------------
# Estados, fases y motivos
# ---------------------------------------------------------------------------


class SessionState(str, Enum):
    """Estados del ciclo de vida (FR-PLAY-002)."""

    PREPARING = "preparing"
    STARTING = "starting"
    PLAYING = "playing"
    RECOVERING = "recovering"
    STOPPING = "stopping"
    ENDED = "ended"

    @property
    def terminal(self) -> bool:
        """True si no se puede volver a un estado activo (FR-PLAY-003)."""
        return self is SessionState.ENDED

    def __str__(self) -> str:  # pragma: no cover - comodidad de depuración
        return self.value


class SessionPhase(str, Enum):
    """Qué está haciendo la sesión ahora mismo (FR-PLAY-004).

    Es **independiente** del estado a propósito: ``PLAYING`` con fase
    ``ANALISIS`` es real (el stream se abrió y la app sigue mirando pistas), y
    meterlo todo en un solo campo obligaría a mentir en uno de los dos casos.
    """

    PREPARACION = "preparacion"
    ANALISIS = "analisis"
    LANZAMIENTO = "lanzamiento"
    CONFIRMACION = "confirmacion"
    SUPERVISION = "supervision"
    RECUPERACION = "recuperacion"
    FINALIZACION = "finalizacion"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


class EndReason(str, Enum):
    """Por qué terminó una sesión (FR-PLAY-009).

    Los ocho motivos que el §4.1 exige como mínimo, más ninguno: un motivo que
    no está en la lista se representa con :attr:`PlaybackSession.end_detail`, no
    inventando un valor nuevo.
    """

    USER_STOPPED = "user_stopped"
    CANCELLED = "cancelled"
    PROCESS_EXITED = "process_exited"
    INIT_FAILED = "init_failed"
    PLAYBACK_ERROR = "playback_error"
    RETRIES_EXHAUSTED = "retries_exhausted"
    APP_SHUTDOWN = "app_shutdown"
    INTERNAL_ERROR = "internal_error"

    @property
    def explicacion(self) -> str:
        """Frase corta y apta para un modal (regla de `AGENTS.md`)."""
        return {
            EndReason.USER_STOPPED: "Lo detuviste tú.",
            EndReason.CANCELLED: "Se canceló antes de terminar.",
            EndReason.PROCESS_EXITED: "El reproductor terminó.",
            EndReason.INIT_FAILED: "No se pudo abrir el canal.",
            EndReason.PLAYBACK_ERROR: "El canal falló mientras se veía.",
            EndReason.RETRIES_EXHAUSTED: "Se agotaron los reintentos.",
            EndReason.APP_SHUTDOWN: "La aplicación se estaba cerrando.",
            EndReason.INTERNAL_ERROR: "Fallo interno de la aplicación.",
        }[self]

    def __str__(self) -> str:  # pragma: no cover
        return self.value


class EventoDeCiclo(str, Enum):
    """Los sucesos del ciclo de vida de una sesión, con nombre cerrado (H-01).

    Vive aquí, y no en el módulo que los consume, porque hay **tres** capas que
    necesitan hablar de lo mismo: la sesión que lo produce, el supervisor que
    decide cuándo, y el diagnóstico que lo escribe. Con una cadena suelta
    cada capa tendría su propia palabra para el mismo suceso —y el supervisor
    de hecho las tenía: ``_nota("reproducendo")`` repartía texto por un
    ``if/elif``— y nadie detectaría el desacuerdo porque todas serían
    «nombres válidos».

    Con un enum no se puede escribir un suceso que no exista, y añadir uno es
    un cambio visible en un solo sitio.

    El orden de los miembros no significa nada; sólo los valores.
    """

    #: Se registró un intento de reproducción (el 1, el 2, el relanzamiento).
    INTENTO = "intento"
    #: El proceso arrancó. Hecho comprobado: el proceso existe.
    LANZADO = "lanzado"
    #: Llegó medio de verdad (FR-REC-001). **Exige evidencia**, no un proceso vivo.
    INICIO_CONFIRMADO = "inicio_confirmado"
    #: Reproducción estable tras el umbral de segundos continuos.
    ESTABLE = "estable"
    #: Se cortó durante la reproducción.
    INTERRUMPIDO = "interrumpido"
    #: Volvió después de un corte.
    RECUPERADO = "recuperado"
    #: Se observó un fallo, con su código y su hecho observado.
    FALLO = "fallo"
    #: Cambió la lista de la que viene el canal.
    CAMBIO_DE_FUENTE = "cambio_de_fuente"
    #: Cambió el reproductor con el que se está viendo.
    CAMBIO_DE_REPRODUCTOR = "cambio_de_reproductor"
    #: La sesión terminó. El motivo va en ``reason_code`` (H-02).
    FIN = "fin"

    def __str__(self) -> str:  # pragma: no cover - comodidad de depuración
        return self.value


#: Los sucesos que son un problema. Lo demás es el ciclo normal, y el
#: informe de un ciclo normal en verde no es una sorpresa.
SUCESOS_DE_PROBLEMA: frozenset[EventoDeCiclo] = frozenset(
    {EventoDeCiclo.INTERRUMPIDO, EventoDeCiclo.FALLO}
)


#: Estados desde los que no se vuelve a uno activo (invariante §4.2).
TERMINAL: frozenset[SessionState] = frozenset(
    {estado for estado in SessionState if estado.terminal}
)


class TransitionError(IPTVError):
    """Se intentó una transición que la máquina de estados prohíbe.

    Hereda de :class:`~thetvview.security.errors.IPTVError` por la misma razón
    que el resto del dominio: la UI captura los errores del proyecto con un
    ``except IPTVError`` y no con ``except Exception``.

    **No es una excepción de flujo normal.** Se lanza cuando hay un bug —un
    ``PLAYING`` que llega tarde, un doble ``end()`` con dos motivos— y el
    mensaje dice exactamente qué salto se pidió para que el fallo sea localizable
    sin depurador.
    """


#: Aristas permitidas de la máquina de estados (FR-PLAY-003).
#:
#: Tres decisiones que conviene dejar escritas:
#:
#: - ``PREPARING`` puede ir **directamente** a ``ENDED``: una sesión que falla
#:   antes de lanzar nada no tiene proceso que parar, y obligarla a pasar por
#:   ``STOPPING`` sería mentir sobre lo que ocurrió (FR-PLAY-010).
#: - ``STARTING``/``PLAYING``/``RECOVERING`` van a ``STOPPING`` y de ahí a
#:   ``ENDED``: la ruta normal de cierre siempre pasa por las dos fases.
#: - ``ENDED`` **no tiene aristas de salida**. Es el invariante que impide que
#:   una sesión terminal vuelva a estar activa.
#: - ``PLAYING`` y ``RECOVERING`` son el único ciclo: un corte de red devuelve a
#:   ``PLAYING`` sin pasar por ``STARTING``, porque el proceso no se relanza desde
#:   cero.
ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.PREPARING: frozenset(
        {SessionState.STARTING, SessionState.STOPPING, SessionState.ENDED}
    ),
    SessionState.STARTING: frozenset(
        {SessionState.PLAYING, SessionState.RECOVERING, SessionState.STOPPING,
         SessionState.ENDED}
    ),
    SessionState.PLAYING: frozenset(
        {SessionState.RECOVERING, SessionState.STOPPING, SessionState.ENDED}
    ),
    SessionState.RECOVERING: frozenset(
        {SessionState.PLAYING, SessionState.STOPPING, SessionState.ENDED}
    ),
    SessionState.STOPPING: frozenset({SessionState.ENDED}),
    SessionState.ENDED: frozenset(),
}


def can_transition(desde: SessionState, hacia: SessionState) -> bool:
    """True si el salto `desde` → `hacia` está permitido."""
    return hacia in ALLOWED_TRANSITIONS.get(desde, frozenset())


def transition(session: "PlaybackSession", hacia: SessionState) -> SessionState:
    """Mueve `session` a `hacia`, o lanza :class:`TransitionError`.

    La transición **valida**; no la registra y sigue. Un salto inválido es un bug
    de programación, y ocultarlo convierte una carrera difícil de depurar en un
    fallo que aparece tres pantallas después.

    Los saltos **a un estado terminal** sí sellan la sesión: ``ended_at`` y
    ``end_reason`` quedan fijados en el momento exacto, no cuando alguien se
    acuerde de mirar.

    Returns:
        El estado al que se llegó (útil para encadenar).

    Raises:
        TransitionError: el salto no está en :data:`ALLOWED_TRANSITIONS`.
    """
    actual = session.state
    if hacia is actual:
        return actual
    if hacia not in ALLOWED_TRANSITIONS.get(actual, frozenset()):
        raise TransitionError(
            f"transición inválida en la sesión {session.session_id}: "
            f"{actual.value} → {hacia.value}"
        )
    session.state = hacia
    if hacia.terminal:
        session._sellar()
    return hacia


# ---------------------------------------------------------------------------
# Intentos
# ---------------------------------------------------------------------------


@dataclass
class AttemptRecord:
    """Un intento de reproducción y cómo terminó (FR-REC-010).

    Vive aquí porque :attr:`PlaybackSession.attempts` lo guarda, y separarlo en
    otro módulo crearía un ciclo: `player.session` no debería importar
    `player.evidence` para que un registro de intento tenga dónde apuntar.

    Los campos de la Fase 2 (``retry_after``, ``error``, ``evidence``) se
    añaden en REC-005 **con valor por defecto**, de modo que esta versión sigue
    siendo válida.
    """

    #: 1 = primer intento, 2 = segundo relanzamiento…
    index: int
    started_at: datetime
    ended_at: datetime | None = None
    #: Segundos que se esperó **antes** de este intento.
    espera_previa: float = 0.0
    #: Segundos que duró el medio antes de que este intento acabara.
    duracion_reproduccion: float | None = None
    #: Veredicto de este intento, tal cual lo devolvió el clasificador.
    resultado: str = ""
    #: `EndReason` si el intento terminó la sesión, o None si sólo fue un corte.
    end_reason: EndReason | None = None

    @property
    def duracion(self) -> float | None:
        """Duración del intento en segundos, o None si sigue vivo."""
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()

    def para_informe(self) -> dict[str, object]:
        """Resumen apto para el informe de diagnóstico (§11.3)."""
        return {
            "intento": self.index,
            "espera_previa_s": round(self.espera_previa, 3),
            "duracion_s": None if self.duracion is None else round(self.duracion, 3),
            "reproduccion_previa_s": (
                None
                if self.duracion_reproduccion is None
                else round(self.duracion_reproduccion, 3)
            ),
            "resultado": self.resultado,
            "motivo": self.end_reason.value if self.end_reason else "",
        }


# ---------------------------------------------------------------------------
# Sesión
# ---------------------------------------------------------------------------


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PlaybackSession:
    """Una sesión de reproducción: identidad, estado, tiempos y resultado.

    Es **el** dueño del ciclo de vida. No lanza nada, no dibuja nada y no sabe
    de curses: su trabajo es saber qué está pasando, cuándo y por qué terminó.
    """

    channel_key: str = ""
    player_name: str = ""
    session_id: str = field(default_factory=new_session_id)
    state: SessionState = SessionState.PREPARING
    phase: SessionPhase = SessionPhase.PREPARACION

    created_at: datetime = field(default_factory=_ahora)
    launch_started_at: datetime | None = None
    process_started_at: datetime | None = None
    first_media_at: datetime | None = None
    stable_playback_at: datetime | None = None
    interruption_at: datetime | None = None
    ended_at: datetime | None = None

    end_reason: EndReason | None = None
    #: Detalle libre del motivo (código de error, texto ya redactado).
    end_detail: str = ""
    #: `correlation_id` del contrato de eventos (§3.3). Viaja con la sesión para
    #: que un informe pueda unir línea de log, red, análisis y motor.
    correlation_id: str = field(default_factory=new_session_id)

    attempts: list[AttemptRecord] = field(default_factory=list)
    #: Recursos con dueño. Se rellena en cuanto se adquiere cada uno.
    resources: "ResourceScope" = field(default_factory=lambda: ResourceScope())

    #: Último fallo observado, con su código y su hecho (H-01). ``None`` si
    #: esta sesión no ha tenido ninguno: no se inicializa con un ``UNKNOWN``
    #: porque «no ha fallado» y «falló y no sabemos por qué» son cosas
    #: distintas, y confundirlas hace que un canal sano parezca dudoso.
    fallo: object = field(default=None, repr=False)
    #: Quien quiere enterarse de los sucesos del ciclo (H-01). Se llama con
    #: ``(sesion, EventoDeCiclo)`` y **sus excepciones se tragan**: un bus de
    #: diagnóstico roto no puede tumbar la reproducción del usuario.
    oyente: object = field(default=None, repr=False, compare=False)

    #: Reloj inyectable (monotónico). Los tests lo fijan; la app no.
    _reloj: object = field(default=None, repr=False, compare=False)
    _candado: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )

    # -- construcción ---------------------------------------------------------

    def __post_init__(self) -> None:
        if self._reloj is None:
            self._reloj = time.monotonic

    # -- consultas ------------------------------------------------------------

    @property
    def ended(self) -> bool:
        """True si la sesión ya terminó."""
        return self.state.terminal

    @property
    def activa(self) -> bool:
        """True si la sesión sigue viva (no terminal)."""
        return not self.state.terminal

    @property
    def intento_actual(self) -> AttemptRecord | None:
        """El último intento registrado, o None si aún no se lanzó nada."""
        return self.attempts[-1] if self.attempts else None

    def duracion_viva(self) -> float:
        """Segundos desde que se creó (o hasta que terminó)."""
        fin = self.ended_at or _ahora()
        return max(0.0, (fin - self.created_at).total_seconds())

    def duracion_reproduccion(self) -> float | None:
        """Segundos entre la primera evidencia de medios y el final.

        None si nunca hubo evidencia: es lo que distingue «se abrió pero no se
        vio nada» de «se vio durante medio minuto» (FR-REC-001).
        """
        if self.first_media_at is None:
            return None
        fin = self.ended_at or _ahora()
        return max(0.0, (fin - self.first_media_at).total_seconds())

    # -- transiciones de alto nivel -------------------------------------------

    def start(self) -> "PlaybackSession":
        """`PREPARING` → `STARTING`, con la marca `launch_started_at`."""
        with self._candado:
            transition(self, SessionState.STARTING)
            self.phase = SessionPhase.LANZAMIENTO
            self.launch_started_at = self.launch_started_at or _ahora()
        self.notificar(EventoDeCiclo.LANZADO)
        return self

    def launched(self) -> "PlaybackSession":
        """El proceso arrancó: marca `process_started_at` (FR-PLAY-011)."""
        with self._candado:
            self.process_started_at = self.process_started_at or _ahora()
            self.phase = SessionPhase.CONFIRMACION
        self.notificar(EventoDeCiclo.LANZADO)
        return self

    def playing(self) -> "PlaybackSession":
        """`STARTING`/`RECOVERING` → `PLAYING`.

        **No** emite «inicio confirmado»: entrar en `PLAYING` sólo quiere decir
        que el proceso está vivo (FR-REC-001), y eso no es lo mismo que estar
        viendo el canal. Ese suceso sale de :meth:`note_first_media`, que sólo
        se llama con evidencia comprobada. Sólo el regreso desde
        `RECOVERING` genera un suceso propio, porque es un hecho que ya no se
        puede deducir del estado.
        """
        with self._candado:
            estaba_recuperando = self.interruption_at is not None
            transition(self, SessionState.PLAYING)
            self.phase = SessionPhase.SUPERVISION
        if estaba_recuperando:
            self.notificar(EventoDeCiclo.RECUPERADO)
        return self

    def recovering(self) -> "PlaybackSession":
        """`PLAYING` → `RECOVERING`, con la marca `interruption_at`."""
        with self._candado:
            transition(self, SessionState.RECOVERING)
            self.phase = SessionPhase.RECUPERACION
            self.interruption_at = self.interruption_at or _ahora()
        self.notificar(EventoDeCiclo.INTERRUMPIDO)
        return self

    def note_first_media(self) -> "PlaybackSession":
        """Primera evidencia de medios (`first_media_at`), sólo la primera vez."""
        with self._candado:
            primera = self.first_media_at is None
            self.first_media_at = self.first_media_at or _ahora()
        if primera:
            self.notificar(EventoDeCiclo.INICIO_CONFIRMADO)
        return self

    def note_stable(self) -> "PlaybackSession":
        """Reproducción estable (`stable_playback_at`), sólo la primera vez."""
        with self._candado:
            primera = self.stable_playback_at is None
            self.stable_playback_at = self.stable_playback_at or _ahora()
        if primera:
            self.notificar(EventoDeCiclo.ESTABLE)
        return self

    def nota_fallo(self, fallo: object = None) -> "PlaybackSession":
        """Anota un fallo observado (H-01) y lo deja en la sesión.

        `fallo` es un :class:`~thetvview.player.errors.StreamError` o ``None``.
        ``None`` significa **que no se sabe la causa**, no que no haya pasado
        nada: por eso el evento sale sin ``reason_code`` en vez de inventar uno.
        """
        with self._candado:
            self.fallo = fallo
            intento = self.intento_actual
            if intento is not None:
                intento.resultado = str(
                    getattr(fallo, "code", "") or ""
                )
        self.notificar(EventoDeCiclo.FALLO)
        return self

    def nota_cambio_de_fuente(self, source_id: str) -> "PlaybackSession":
        """El canal pasa a venir de otra lista (H-01)."""
        self.notificar(EventoDeCiclo.CAMBIO_DE_FUENTE, {"source_id": str(source_id)})
        return self

    def nota_cambio_de_reproductor(self, player_name: str) -> "PlaybackSession":
        """Se cambia el reproductor con el que se está viendo (H-01).

        El nombre nuevo pasa a ser el de la sesión, de modo que los sucesos
        siguientes llevan el motor que realmente está sonando.
        """
        nuevo = str(player_name or "")
        if nuevo:
            self.player_name = nuevo
        self.notificar(EventoDeCiclo.CAMBIO_DE_REPRODUCTOR, {"player": nuevo})
        return self

    def notificar(
        self, evento: "EventoDeCiclo", detalles: dict | None = None
    ) -> None:
        """Avisa al :attr:`oyente` de un suceso del ciclo. Nunca lanza.

        Se traga las excepciones del oyente a propósito. La alternativa —dejar
        que el bus reviente— convertiría un problema de diagnóstico en una
        sesión de reproducción muerta: se pierde el canal abierto, no sólo la
        traza.
        """
        oyente = self.oyente
        if oyente is None:
            return
        try:
            oyente(self, evento, detalles or {})
        except Exception:  # noqa: BLE001 - un oyente roto no tumba la sesión
            pass

    def end(
        self,
        reason: EndReason,
        detail: str = "",
        *,
        release: bool = True,
    ) -> "PlaybackSession":
        """Termina la sesión con `reason` y libera lo que adquirió.

        **Idempotente**: llamarla dos veces no cambia nada ni destruye. La
        segunda llamada devuelve la sesión tal cual, con el motivo **original**
        intacto: el §4.2 es explícito en que un motivo de cierre posterior no
        puede pisar el motivo real.

        Args:
            reason: por qué terminó.
            detail: texto libre, **ya redactado** por quien lo compose.
            release: si True (lo normal) llama a
                :meth:`ResourceScope.close_all`. False para una sesión que aún
                no adquirió nada, donde el cierre no tiene sentido.
        """
        with self._candado:
            if self.ended:
                return self  # motivo original preservado
            self.phase = SessionPhase.FINALIZACION
            # Se intenta la ruta con STOPPING cuando la máquina la permite: es
            # la ruta real de cierre. Si la sesión ya está en un estado desde el
            # que STOPPING no es legal (no debería pasar), se va directo a ENDED
            # y la máquina lo deja pasar porque está terminal.
            if can_transition(self.state, SessionState.STOPPING):
                transition(self, SessionState.STOPPING)
            transition(self, SessionState.ENDED)
            self.end_reason = reason
            self.end_detail = detail
            intento = self.intento_actual
            if intento is not None and intento.ended_at is None:
                intento.ended_at = self.ended_at
                intento.end_reason = reason
            # `duracion_viva` siempre se sabe; `duracion_reproduccion` sólo
            # si llegó a verse algo, y por eso se omite en vez de ir en cero:
            # cero significaría «se cortó al instante de abrir».
            duracion = self.duracion_viva()
            reproduccion = self.duracion_reproduccion()
        # Fuera del candado: avisar puede hacer trabajo de verdad (escribir en
        # el bus) y `close_all` llama a liberadores externos. Con el candado
        # tomado, un oyente lento bloquearía a quien sólo consulta el estado.
        medidas: dict = {"duracion_viva_s": duracion}
        if reproduccion is not None:
            medidas["duracion_reproduccion_s"] = reproduccion
        self.notificar(EventoDeCiclo.FIN, medidas)
        if release:
            # Los `release` de recursos son llamadas externas (parar un proxy,
            # borrar un socket) y no deben hacerse con el candado de la sesión
            # tomado: un `release` lento bloquearía a quien sólo quiere
            # consultar el estado.
            self.resources.close_all()
        return self

    def _sellar(self) -> None:
        """Fija `ended_at` en el instante exacto de la transición terminal."""
        self.ended_at = self.ended_at or _ahora()

    # -- intentos -------------------------------------------------------------

    def nuevo_intento(self, espera_previa: float = 0.0) -> AttemptRecord:
        """Registra el intento `n` (1 = el primero) y lo devuelve.

        El intento **anterior** se cierra con la marca de ahora: así cada
        registro dice cuánto duró y cuándo, sin depender de que alguien lo
        rellene a mano (FR-REC-010).
        """
        with self._candado:
            ahora = _ahora()
            anterior = self.intento_actual
            if anterior is not None and anterior.ended_at is None:
                anterior.ended_at = ahora
            registro = AttemptRecord(
                index=len(self.attempts) + 1,
                started_at=ahora,
                espera_previa=max(0.0, float(espera_previa)),
            )
            self.attempts.append(registro)
            espera = registro.espera_previa
        self.notificar(
            EventoDeCiclo.INTENTO,
            {"intento": registro.index, "espera_previa_s": espera},
        )
        return registro

    # -- recursos -------------------------------------------------------------

    def add_resource(self, nombre: str, release) -> None:  # noqa: ANN001
        """Registra un recurso adquirido y cómo se libera (FR-PLAY-007)."""
        self.resources.add(nombre, release)

    def close_resources(self) -> None:
        """Libera lo adquirido. Idempotente (FR-PLAY-008)."""
        self.resources.close_all()

    # -- informe --------------------------------------------------------------

    def para_informe(self) -> dict[str, object]:
        """Volcado apto para el informe de diagnóstico (§11.3).

        Deliberadamente **no** incluye la URL del canal: el informe va a un modal
        y a un archivo que el usuario puede compartir, y una URL con token no
        va a ninguno de los dos sitios (regla 1 de `streams/diagnose`).
        """
        return {
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "canal": self.channel_key,
            "reproductor": self.player_name,
            "estado": self.state.value,
            "fase": self.phase.value,
            "marcas": {
                "creada": _iso(self.created_at),
                "lanzamiento": _iso(self.launch_started_at),
                "proceso": _iso(self.process_started_at),
                "primer_medios": _iso(self.first_media_at),
                "estable": _iso(self.stable_playback_at),
                "interrupcion": _iso(self.interruption_at),
                "fin": _iso(self.ended_at),
            },
            "motivo_fin": self.end_reason.value if self.end_reason else "",
            "detalle_fin": self.end_detail,
            "intentos": [a.para_informe() for a in self.attempts],
        }


def _iso(momento: datetime | None) -> str:
    """ISO-8601 o cadena vacía. Los valores desconocidos **se marcan** (§11.3)."""
    return momento.isoformat(timespec="milliseconds") if momento else ""


# ---------------------------------------------------------------------------
# Recursos
# ---------------------------------------------------------------------------


class ResourceScope:
    """Todo lo que una sesión adquiere, con su dueño y su ruta de liberación.

    Hoy la limpieza está escrita a mano en dos sitios distintos
    (``NowPlayingScreen.stop_tracks`` y el ``except`` de ``play_channel``), y
    un recurso nuevo se olvida. Aquí se **registra** en el momento de adquirirlo
    y se libera en bloque, con dos garantías que la limpieza manual no tenía:

    1. **Idempotente.** :meth:`close_all` dos veces no hace efecto destructivo
       adicional: la segunda vez no vuelve a parar nada (FR-PLAY-008).
    2. **Tolerante a fallos.** Si un ``release`` falla, se siguen liberando los
       demás y los errores se acumulan. Un proxy que no se puede apagar no puede
       impedir que se borre el socket (FR-PLAY-008).

    El orden es **LIFO** (último adquirido, primero liberado), que es el orden
    correcto para recursos anidados: el proxy se apagó después de abrir el
    socket, así que se apaga antes.
    """

    __slots__ = ("_cerrado", "_candado", "_errores", "_liberados", "_recursos")

    def __init__(self) -> None:
        self._recursos: list[tuple[str, object]] = []
        self._errores: list[tuple[str, BaseException]] = []
        self._cerrado = False
        self._candado = threading.RLock()

    # -- registro -------------------------------------------------------------

    def add(self, nombre: str, release) -> None:  # noqa: ANN001
        """Registra `nombre` con su función de liberación ``release``.

        ``release`` es un ``() -> None`` que **no** debería lanzar; si lanza, se
        acumula en :attr:`errores` y los demás se siguen liberando.

        Registrar en un scope **ya cerrado** libera de inmediato: un recurso
        adquirido tarde (el IPC que se abre después del proceso, por ejemplo) no
        puede quedarse colgado sin dueño.
        """
        if not callable(release):
            raise TypeError(f"el liberador de {nombre!r} no es invocable")
        with self._candado:
            if self._cerrado:
                # Fuera del `with` para no hacer E/S con el candado tomado, pero
                # se conserva la semántica: se libera y se registra el fallo.
                pass
            else:
                self._recursos.append((str(nombre), release))
                return
        try:
            release()
        except Exception as exc:  # noqa: BLE001 - liberar no puede propagar
            self._errores.append((str(nombre), exc))

    # -- consulta -------------------------------------------------------------

    @property
    def cerrado(self) -> bool:
        """True si ya se liberó todo (y la segunda llamada será un no-op)."""
        return self._cerrado

    @property
    def pendientes(self) -> tuple[str, ...]:
        """Nombres de lo que queda sin liberar."""
        with self._candado:
            return tuple(nombre for nombre, _ in self._recursos)

    @property
    def errores(self) -> tuple[tuple[str, BaseException], ...]:
        """Fallos acumulados al liberar: ``(nombre, excepción)``."""
        with self._candado:
            return tuple(self._errores)

    def liberado(self, nombre: str) -> bool:
        """True si `nombre` ya se liberó."""
        with self._candado:
            return nombre not in {n for n, _ in self._recursos}

    # -- liberación -----------------------------------------------------------

    def close_all(self) -> tuple[tuple[str, BaseException], ...]:
        """Libera todo en orden LIFO. Idempotente. Devuelve los fallos.

        Los fallos **no** interrumpen: se sigue liberando y se acumulan aquí,
        de modo que quien llama pueda registrarlos sin que el motivo original de
        la sesión se pierda (FR-PLAY-008).
        """
        with self._candado:
            if self._cerrado:
                return tuple(self._errores)
            self._cerrado = True
            pendientes = list(reversed(self._recursos))
            self._recursos.clear()
        for nombre, release in pendientes:
            try:
                release()
            except Exception as exc:  # noqa: BLE001 - un fallo no impide el resto
                self._errores.append((nombre, exc))
        return tuple(self._errores)


# ---------------------------------------------------------------------------
# Registro de sesiones
# ---------------------------------------------------------------------------


class SessionRegistry:
    """Guarda la sesión vigente y dice si una sesión sigue siendo actual.

    Es el **mismo patrón** que :meth:`thetvview.tasks.manager.TaskManager.is_current`,
    aplicado a la reproducción: antes de aplicar el resultado de una tarea
    tardía se pregunta si su sesión **sigue siendo la vigente**, y si no lo es,
    se descarta (FR-PLAY-005).

    Cada sesión nueva **sobrescribe** a la anterior y sueltas con
    :meth:`PlaybackSession.end`, porque la sesión vieja ya no tiene a nadie
    detrás: dejarla viva sería dejar un proceso sin dueño.

    Thread-safe: el registro se consulta desde el hilo del supervisor y desde el
    hilo principal.
    """

    def __init__(self) -> None:
        self._candado = threading.RLock()
        self._actual: PlaybackSession | None = None
        self._generacion = 0

    @property
    def generacion(self) -> int:
        """Número de sesiones registradas. Sube con cada `start()`."""
        with self._candado:
            return self._generacion

    @property
    def actual(self) -> PlaybackSession | None:
        """La sesión vigente, o None si no hay ninguna."""
        with self._candado:
            return self._actual

    def start(
        self,
        channel_key: str = "",
        player_name: str = "",
    ) -> PlaybackSession:
        """Registra una sesión nueva y la convierte en la vigente.

        La anterior, si la había, **se sella** con ``EndReason.CANCELLED``: el
        usuario cambió de canal, y una sesión sin dueño es una sesión que no se
        puede explicar después.
        """
        sesion = PlaybackSession(channel_key=channel_key, player_name=player_name)
        with self._candado:
            anterior = self._actual
            self._generacion += 1
            self._actual = sesion
        if anterior is not None and anterior.activa:
            anterior.end(EndReason.CANCELLED, "el usuario abrió otro canal")
        return sesion

    def is_current(self, session_id: str) -> bool:
        """True si `session_id` sigue siendo la sesión vigente.

        **Éste es el guard de obsolescencia.** Se consulta justo antes de tocar
        estado compartido (el proceso, la pantalla, la barra de estado): una tarea
        de la sesión anterior que termina tarde ve ``False`` y no toca nada.
        """
        with self._candado:
            actual = self._actual
        return actual is not None and actual.session_id == session_id

    def finish(self, session: PlaybackSession, reason: EndReason) -> PlaybackSession:
        """Termina `session` y la desregistra si era la vigente.

        Terminar una sesión **que no es la vigente** se permite y es correcto: la
        vieja se está apagando mientras la nueva ya está en marcha, y su motivo
        es parte del diagnóstico.
        """
        session.end(reason)
        with self._candado:
            if self._actual is session:
                self._actual = None
        return session

    def shutdown(self, detail: str = "la aplicación se está cerrando") -> PlaybackSession | None:
        """Sella la sesión vigente por cierre de la aplicación (FR-PLAY-006)."""
        with self._candado:
            actual = self._actual
            self._actual = None
        if actual is None:
            return None
        return actual.end(EndReason.APP_SHUTDOWN, detail)

    def __len__(self) -> int:  # pragma: no cover - conveniencia
        return 1 if self.actual is not None else 0