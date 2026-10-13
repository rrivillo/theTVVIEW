"""H-01 — Traducción de sucesos de sesión a eventos del §3.3.

:class:`~thetvview.player.session.EventoDeCiclo` y
:class:`~thetvview.diagnostics.events.Event` hablan de lo mismo en dos
lenguas: la primera nombra lo que **ocurre en la reproducción**, la segunda lo
que **queda registrado**. Este módulo es el único sitio donde se cruzan, y
por eso las tres reglas que gobiernan los eventos se pueden exigir aquí:

**El motivo sale de la taxonomía del reproductor** (H-02). Un ``fallo`` de
reproducción lleva el código de :class:`~thetvview.player.errors.StreamError`,
su ``recoverable`` derivado de la política del supervisor y, aparte, el hecho
observado. Un ``fin`` lleva su :class:`~thetvview.player.session.EndReason`.

**Gravedad según lo que pasó, no según el código.** «Lo detuviste tú» es
``INFO``: marcarla como error asusta de más y hace que el informe enseñe un
rojo donde no hubo problema. Un corte es ``WARNING`` porque el ciclo lo
recupera solo; un fallo que acaba la sesión es ``ERROR``.

**Nada de lo que no se sabe.** La duración se mide o no se pone. El
``reason_code`` se pone si existe y no se rellena. El bus, además, redacta
**al añadir** (FR-DIAG-008), así que lo que queda dentro ya está limpio y
leerlo no puede colar un secreto «por si acaso».

Nivel de evidencia que confirma «está sonando»
----------------------------------------------

``inicio_confirmado`` es un nombre que hay que ganarse. El proceso vivo sólo
dice que hay un proceso; la fuente aceptada sólo dice que el motor cogió el
archivo. Las dos cosas las hace también un canal que abre y se queda en negro.
A partir de :attr:`~thetvview.player.evidence.EvidenceLevel.MEDIA_SEEN` sí ha
llegado medio de verdad, y sólo desde ahí se emite.

Y bajar de nivel después **no** lo desdice: se emitió porque se comprobó, y un
corte posterior no borra el hecho.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..player.evidence import Evidence, EvidenceLevel
from ..player.session import EndReason, EventoDeCiclo, PlaybackSession
from ..security.redaction import redact_text
from .events import Event, EventBus, Severity
from .taxonomy import Causa, causa_de, causa_de_motivo

__all__ = [
    "NIVEL_DE_INICIO",
    "COMPONENTE",
    "ObservadorDeSesion",
    "observar",
    "tipo_de_evento",
]

#: Nivel de evidencia a partir del cual se dice «está sonando». Por debajo de
#: esto hay un proceso y nada más, y decirlo sería mentir (FR-REC-001).
NIVEL_DE_INICIO: EvidenceLevel = EvidenceLevel.MEDIA_SEEN

#: Componente del §3.3. Es el subsistema que emite («player», «probe»…), no
#: el motor concreto: el nombre de mpv o vlc va en ``details.player``, que es
#: donde el informe puede agrupar por él sin mezclar las dos cosas.
COMPONENTE = "player"

#: De qué suceso del ciclo sale qué nombre de evento del §3.3. Explícito y
#: cerrado a propósito: si el supervisor inventa un suceso nuevo, esta tabla
#: lo delata en vez de dejar que caiga en un ``else`` que no avisa de nada.
_TIPOS: dict[EventoDeCiclo, str] = {
    EventoDeCiclo.INTENTO: "playback.intento",
    EventoDeCiclo.LANZADO: "playback.lanzado",
    EventoDeCiclo.INICIO_CONFIRMADO: "playback.inicio_confirmado",
    EventoDeCiclo.ESTABLE: "playback.estable",
    EventoDeCiclo.INTERRUMPIDO: "playback.interrumpido",
    EventoDeCiclo.RECUPERADO: "playback.recuperado",
    EventoDeCiclo.FALLO: "playback.fallo",
    EventoDeCiclo.CAMBIO_DE_FUENTE: "playback.cambio_de_fuente",
    EventoDeCiclo.CAMBIO_DE_REPRODUCTOR: "playback.cambio_de_reproductor",
    EventoDeCiclo.FIN: "playback.fin",
}

#: Motivos de cierre que **no** son un problema. Todo lo demás que no esté
#: aquí se considera un fallo, que es la lectura conservadora: preferimos
#: marcar de más un fin raro que no marcar de menos un fallo silencioso.
_FINES_TRANQUILOS: frozenset[EndReason] = frozenset(
    {EndReason.USER_STOPPED, EndReason.CANCELLED, EndReason.APP_SHUTDOWN}
)


def tipo_de_evento(evento: EventoDeCiclo) -> str:
    """Nombre del evento del §3.3 para un suceso del ciclo.

    Un suceso sin nombre no se descarta en silencio: se registra con el
    nombre del propio suceso. Perder un evento porque nadie lo mapeó es peor
    que escribir uno con un nombre que se reconoce como no catalogado.
    """
    return _TIPOS.get(evento, f"playback.{getattr(evento, 'value', evento)}")


def _gravedad_de(evento: EventoDeCiclo, sesion: PlaybackSession) -> Severity:
    """La gravedad va según lo que pasó, no según el código.

    Un ciclo normal es ``INFO``: pintar de rojo una sesión que el usuario
    paró a propósito enseña un problema donde no lo hay, y habitúa a mirar el
    informe sin ver nada.
    """
    if evento is EventoDeCiclo.FALLO:
        return Severity.ERROR
    if evento is EventoDeCiclo.INTERRUMPIDO:
        return Severity.WARNING
    if evento is EventoDeCiclo.FIN:
        if sesion.end_reason in _FINES_TRANQUILOS:
            return Severity.INFO
        return Severity.ERROR
    return Severity.INFO


class ObservadorDeSesion:
    """Convierte los sucesos de :class:`PlaybackSession` en eventos del bus.

    Se registra como :attr:`PlaybackSession.oyente`. No guarda estado propio
    salvo la sesión a la que está atado, y **nunca** lanza: si el bus falla, la
    sesión del usuario sigue su curso. Perder un registro de diagnóstico es un
    misterio; perder la reproducción es una queja.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        ahora: Callable[[], float] | None = None,
        sesion: PlaybackSession | None = None,
    ) -> None:
        self.bus = bus
        self.sesion = sesion
        self._ahora = ahora
        # Un evento de «inicio confirmado» sólo puede salir una vez por sesión.
        self._confirmado = False
        #: Errores del propio traductor. No se muestran al usuario (no es su
        #: problema), pero se guardan para que un informe pueda decir «faltan
        #: eventos» en vez de fingir que no pasó nada.
        self.errores: list[str] = []

    # -- interfaz de oyente ---------------------------------------------------

    def __call__(
        self,
        sesion: PlaybackSession,
        evento: EventoDeCiclo,
        detalles: Mapping[str, Any] | None = None,
    ) -> None:
        self.sesion = sesion
        self.registrar(evento, dict(detalles or {}))

    # -- construcción de eventos ---------------------------------------------

    def registrar(
        self, evento: EventoDeCiclo, detalles: dict[str, Any] | None = None
    ) -> Event | None:
        """Escribe el evento del suceso. Devuelve el que quedó en el bus.

        ``None`` si no había sesión a la que atribuirlo: sin ``session_id`` un
        evento no se puede atribuir a nada, y escribirlo sería dejar basura
        que luego no se puede consultar.

        Nunca lanza. Un error aquí se anota en :attr:`errores` y se sigue:
        perder la reproducción del usuario porque el registro de diagnóstico
        tenía un fallo es el peor de los dos resultados posibles, y un error
        que no se anota es un error que nadie va a encontrar nunca.
        """
        try:
            return self._registrar(evento, detalles)
        except Exception as exc:  # noqa: BLE001 - de aquí no sale nada
            self._anotar(exc)
            return None

    def _registrar(
        self, evento: EventoDeCiclo, detalles: dict[str, Any] | None
    ) -> Event | None:
        sesion = self.sesion
        if sesion is None:
            return None
        extra = dict(detalles or {})
        causa = self._causa_de(evento, sesion)
        extra = self._extra_de(evento, sesion, causa, extra)
        if sesion.player_name:
            extra.setdefault("player", sesion.player_name)
        bruto = Event(
            event_type=tipo_de_evento(evento),
            timestamp=self._timestamp(),
            correlation_id=sesion.correlation_id,
            session_id=sesion.session_id,
            channel_id=sesion.channel_key or None,
            component=COMPONENTE,
            phase=sesion.phase.value,
            severity=_gravedad_de(evento, sesion),
            reason_code=causa.reason_code or None,
            retryable=causa.recoverable,
            details=extra,
        )
        return self.bus.emit(bruto)

    def evidencia(self, medida: Evidence) -> Event | None:
        """Traduce una medida de evidencia en su evento, si corresponde.

        Este es el camino del «inicio confirmado»: no sale de que la sesión
        entre en ``PLAYING`` —eso el proceso vivo ya lo hizo— sino de que
        alguien **compruebe** que llegó medio (FR-REC-001).
        """
        if self.sesion is None or not isinstance(medida, Evidence):
            return None
        nivel = medida.level
        if not nivel.comproblable:
            # `UNKNOWN` es «no lo sé». Anunciar algo con eso de base sería
            # justo el rumor que este módulo existe para no contar.
            return None
        # No se corta el flujo con un `return` en la primera rama: una medida
        # `STABLE` cumple las dos condiciones a la vez —llegó medio **y** lleva
        # el tiempo suficiente— y emitsela sólo como «inicio confirmado»
        # perdería justo el dato más útil del muestreo.
        if not self._confirmado and nivel.rank >= NIVEL_DE_INICIO.rank:
            self._confirmado = True
            self.sesion.note_first_media()
        if medida.estable:
            self.sesion.note_stable()
        if self._confirmado:
            return self._ultimo()
        return None

    def _ultimo(self) -> Event | None:
        eventos = self.bus.events()
        return eventos[-1] if eventos else None

    # -- internos ------------------------------------------------------------

    def _causa_de(self, evento: EventoDeCiclo, sesion: PlaybackSession) -> Causa:
        if evento is EventoDeCiclo.FALLO:
            return causa_de(getattr(sesion, "fallo", None))
        if evento is EventoDeCiclo.FIN:
            # El motivo de cierre es un `EndReason`, no un `StreamError`: son
            # vocabularios distintos y cada uno va por su puerta. Confundirlos
            # era un bug real: `causa_de` de un `EndReason` reventaba al
            # tocar `.observado`, y el `except` de la sesión se lo tragaba en
            # silencio, así que el evento de fin desaparecía sin que nadie se
            # enterara.
            return causa_de_motivo(sesion.end_reason)
        return Causa()

    def _anotar(self, exc: BaseException) -> None:
        """Deja rastro del error del traductor, sin texto de terceros."""
        self.errores.append(f"{type(exc).__name__}: {redact_text(str(exc))}")

    def _extra_de(
        self, evento: EventoDeCiclo, sesion: PlaybackSession,
        causa: Causa, detalles: dict[str, Any],
    ) -> dict[str, Any]:
        extra = dict(detalles)
        if evento is EventoDeCiclo.FALLO:
            # El hecho va **aparte** del código: son las dos columnas de H-02.
            if causa.observado:
                extra["observado"] = causa.observado
            extra["causa_conocida"] = causa.causa_conocida
        if evento is EventoDeCiclo.FIN and sesion.end_detail:
            extra["detalle"] = sesion.end_detail
        return extra

    def _timestamp(self) -> float:
        if self._ahora is None:
            import time

            return time.time()
        return float(self._ahora())


def observar(
    sesion: PlaybackSession, bus: EventBus, *,
    ahora: Callable[[], float] | None = None,
) -> ObservadorDeSesion:
    """Ata un :class:`ObservadorDeSesion` a `sesion` y lo devuelve.

    Es el atajo que se usa en producción. Devolver el observador sirve para
    poder seguir dando señales (la evidencia, sobre todo) sin volver a
    montarlo.
    """
    observador = ObservadorDeSesion(bus, ahora=ahora, sesion=sesion)
    sesion.oyente = observador
    return observador