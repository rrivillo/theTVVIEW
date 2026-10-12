"""Supervisor de reproducción: reconexión limitada (SDD-M §17, AC-008).

El problema
-----------

Un canal que cae a mitad —el proveedor reinicia, la red se va un segundo, el
enrutador cambia— deja al usuario mirando un reproductor que ya está muerto. Hoy
la app se limita a mostrar el proceso y, cuando termina, a explicar por qué. Lo
que no hay es **reintento**: y reintentar sin límite es peor, porque convierte
un corte de dos segundos en un bucle infinito de esperas.

La política es la del §17, medida en segundos y no inventada:

```text
intento 1 → inmediato
intento 2 → 1 s
intento 3 → 2 s
intento 4 → 5 s
intento 5 → 10 s   (y ahí se rinde: max_reconnect_attempts)
```

Tres reglas que gobiernan el módulo
-----------------------------------

1. **El supervisor no reproduce.** Observa ``Popen.poll()`` y publica un
   :class:`PlaybackState`; el relanzado lo hace quien lo configura, con la
   función que le pasa. Así el módulo no depende de :mod:`thetvview.player` ni
   de la UI, y se puede probar sin lanzar un solo binario.
2. **Un hilo daemon, y siempre el mismo patrón** que
   :class:`thetvview.channel_health.ChannelHealthMonitor`: observa, no bloquea,
   y un fallo suyo nunca tumba la TUI.
3. **Nada de buffering en Python** (§19). Los perfiles ``LOW_LATENCY`` /
   ``BALANCED`` / ``STABLE`` se mapean a banderas **reales** del reproductor,
   medidas en :mod:`player.protocols`, no a una cola en este proceso.
4. **La sesión es opcional.** Sin ``session`` el comportamiento es byte a byte
   el de siempre —:mod:`tests.test_stream_supervisor` no se modifica—; con
   sesión, el supervisor además registra los intentos y respeta el guard de
   obsolescencia, para que una tarea de un canal ya sustituido no pueda
   relanzar nada (PLAY-004, FR-PLAY-005).
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .errors import NO_REINTENTABLE, StreamError, StreamErrorCode
from .evidence import Evidence, EvidenceLevel, ProcessAliveSignal
from .session import AttemptRecord, EndReason, PlaybackSession

__all__ = [
    "PlaybackState",
    "PlaybackProfile",
    "PROFILE_FLAGS",
    "ReconnectPolicy",
    "RetryPolicy",
    "ReconnectSupervisor",
    "DEFAULT_BACKOFF",
    "MAX_WAIT_SECONDS",
    "profile_flags",
    "retryable",
]

#: Los cuatro segundos del §17, medidos. El último reintento espera 10 s; más
#: allá no es «paciencia», es una app que parece colgada.
DEFAULT_BACKOFF: tuple[int, ...] = (1, 2, 5, 10)


class PlaybackState(str, Enum):
    """Estado de la reproducción (SDD-M §17)."""

    CONNECTING = "connecting"
    PLAYING = "playing"
    BUFFERING = "buffering"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    ERROR = "error"

    @property
    def texto(self) -> str:
        """Etiqueta corta para la pantalla de reproducción."""
        return {
            PlaybackState.CONNECTING: "Conectando",
            PlaybackState.PLAYING: "Reproduciendo",
            PlaybackState.BUFFERING: "Cargando",
            PlaybackState.RECONNECTING: "Reconectando",
            PlaybackState.STOPPED: "Detenido",
            PlaybackState.ERROR: "Error",
        }[self]


class PlaybackProfile(str, Enum):
    """Perfil de red (SDD-M §19). No decide nada aquí."""

    LOW_LATENCY = "low_latency"
    BALANCED = "balanced"
    STABLE = "stable"


#: Banderas reales por reproductor, **medidas**, no supuestas (SDD-M §19).
#: El objetivo era el jitter de los streams IPTV, no el gaming, y el criterio es
#: que el perfil no rompa el arranque: con 8 s de caché un canal con segments de
#: 6 s se queda esperando al siguiente y ve congelaciones, que es peor que la
#: latencia que se quería evitar.
PROFILE_FLAGS: dict[str, dict[str, list[str]]] = {
    "mpv": {
        PlaybackProfile.LOW_LATENCY.value: ["--cache-secs=2", "--demuxer-max-bytes=16MiB"],
        PlaybackProfile.BALANCED.value: ["--cache-secs=5", "--demuxer-max-bytes=64MiB"],
        PlaybackProfile.STABLE.value: ["--cache-secs=15", "--demuxer-max-bytes=256MiB"],
    },
    "mplayer": {
        # mplayer no expone `--demuxer-max-bytes`; sólo el tamaño de caché.
        PlaybackProfile.LOW_LATENCY.value: ["-cache", "1024", "-cache-min", "1"],
        PlaybackProfile.BALANCED.value: ["-cache", "8192", "-cache-min", "8"],
        PlaybackProfile.STABLE.value: ["-cache", "32768", "-cache-min", "32"],
    },
    "vlc": {
        PlaybackProfile.LOW_LATENCY.value: ["--network-caching=1500"],
        PlaybackProfile.BALANCED.value: ["--network-caching=8000"],
        PlaybackProfile.STABLE.value: ["--network-caching=30000"],
    },
}


def profile_flags(player_name: str | None, perfil: str) -> list[str]:
    """Banderas de red de un reproductor para un perfil (SDD-M §19).

    Devuelve ``[]`` para un reproductor o un perfil que no conoce. Preferimos
    no pasar nada a pasar una bandera inventada: un perfil desconocido es «el
    de siempre», no una opción que el reproductor pueda no entender.
    """
    if not player_name:
        return []
    tabla = PROFILE_FLAGS.get(str(player_name).strip().lower())
    if not tabla:
        return []
    return list(tabla.get(str(perfil or "").strip().lower(), []))


@dataclass
class ReconnectPolicy:
    """Cuántos reintentos y con qué espera (§17, §26).

    Los dos topes son independientes y los dos importan:

    - ``max_attempts`` es el tope de **reconexión** de un mismo reproductor
      (5 por defecto, §17);
    - el tope de **cambio de reproductor** es 2 (§26) y vive en
      :mod:`thetvview.player.router`, porque es una decisión de qué binario
      elegir y no de cuándo esperar.

    Confundirlos es el error fácil: con 5×2 intentos un canal malo se convierte
    en veinte esperas, y el usuario ve la app colgada y no un mensaje.
    """

    backoff: tuple[int, ...] = DEFAULT_BACKOFF
    max_attempts: int = 5

    def espera_para(self, intento: int) -> float:
        """Segundos a esperar antes del reintento ``intento`` (1 = primero).

        Devuelve 0 para el primer intento —que es inmediato por definición— y
        el último valor de la tabla cuando se agotó, en vez de crecer: pasada
        la lista, esperar más sólo empeora la experiencia.
        """
        if intento <= 1:
            return 0.0
        indice = intento - 2
        if indice < len(self.backoff):
            return float(self.backoff[indice])
        return float(self.backoff[-1]) if self.backoff else 0.0

    @property
    def intentos_maximos(self) -> int:
        return max(0, int(self.max_attempts))


#: Tope de espera del backoff (SDD §5.2). Pasada esta espera, esperar más deja de
#: ser paciencia y pasa a ser una aplicación que parece colgada.
MAX_WAIT_SECONDS: float = 30.0


def retryable(
    code: StreamErrorCode | None,
    *,
    error: StreamError | None = None,
) -> bool:
    """¿Tiene sentido reintentar este fallo con el mismo motor? (FR-REC-005)

    **La política vive aquí y en ningún otro sitio** (NFR-008): adaptadores,
    router y supervisor consultan esta función. Si cada uno decidiera por su
    cuenta, «un 401 se reintenta cinco veces» volvería a ser posible en tres
    sitios distintos.

    ``None`` (no sabemos qué falló) **sí** es reintentable: un fallo sin clasificar
    puede ser un corte de red de dos segundos, que es justo lo que la reconexión
    existe para arreglar. Lo que no se reintenta es lo que se sabe que no
    arregla repitiendo.
    """
    if error is not None and error.code is not None:
        code = error.code
    if code is None:
        return True
    return code not in NO_REINTENTABLE


@dataclass
class RetryPolicy:
    """Backoff con tope, jitter, `Retry-After` y reinicio tras estabilidad.

    Envuelve a :class:`ReconnectPolicy` en lugar de sustituirla
    ---------------------------------------------------------

    ``ReconnectPolicy.espera_para`` es el contrato que el SDD-M §17 fijó
    (1 s, 2 s, 5 s, 10 s) y que ``tests/test_stream_supervisor.py`` comprueba
    valor por valor. Cambiarlo sería romper un comportamiento ya verificado a
    cambio de nada: aquí se **envuelve**, y la tabla sigue siendo la misma
    whenever se llama sin política nueva.

    Qué añade sobre la tabla fija:

    - **jitter inyectable** (``rng``): sin él, diez clientes que se cortan a la
      vez vuelven a pedir lo mismo a la vez. Con un ``rng`` fijo el test es
      determinista, que es el requisito §10 del plan.
    - **tope de espera**: pasado ``MAX_WAIT_SECONDS`` no se espera más.
    - **``Retry-After`` acotado**: se respeta lo que dice el servidor, pero
      nunca más que ``max_wait`` ni más que el tope de la sesión (FR-REC-007).
      Si no se puede respetar exacto, :attr:`RetryDecision.acotado` lo dice.
    - **reinicio de contadores sólo tras estabilidad** (FR-REC-008): con el
      proceso vivo no basta; hace falta :meth:`nota_estable`.

    El reloj y el ``rng`` son inyectables: ningún test de esto duerme.
    """

    #: Espera base del backoff exponencial, en segundos.
    base_seconds: float = 1.0
    #: Tope de una espera individual.
    max_wait: float = MAX_WAIT_SECONDS
    #: Tope de intentos por ciclo de fallo.
    max_attempts: int = 5
    #: Factor de crecimiento entre intentos.
    growth: float = 2.0
    #: Semilla aleatoria inyectable (FR-REC-006).
    rng: Callable[[], float] = random.random
    #: Tope del `Retry-After` del servidor, aunque no haya `max_wait` menor.
    max_retry_after: float = 60.0

    def espera(self, intento: int) -> float:
        """Espera para el reintento ``intento`` (1 = primero, inmediato).

        Exponencial con jitter **aditivo** y tope. El jitter va de 0 a la mitad
        de la espera: suficiente para desincronizar a los clientes, sin Convertir
        un reintento en un castigo aleatorio.
        """
        if intento <= 1:
            return 0.0
        crudo = self.base_seconds * (self.growth ** max(0, intento - 2))
        if crudo <= 0:
            return 0.0
        try:
            azar = float(self.rng())
        except Exception:  # noqa: BLE001 - un rng roto no rompe la recuperación
            azar = 0.5
        azar = min(1.0, max(0.0, azar))
        return min(self.max_wait, crudo + (crudo * 0.5 * azar))

    def espera_con_retry_after(
        self,
        intento: int,
        retry_after: float | None = None,
    ) -> "RetryDecision":
        """Espera del reintento respetando un ``Retry-After`` del servidor.

        El `Retry-After` **manda** sobre el backoff cuando viene: el servidor
        sabe cuándo volverá a aceptar peticiones y nosotros no. Si pide esperar
        más que el tope, se espera el tope y :attr:`RetryDecision.acotado` queda
        True para que el diagnóstico diga que la decisión fue nuestra (FR-REC-007).
        """
        base = self.espera(intento)
        if retry_after is None:
            return RetryDecision(segundos=base, origen="backoff", acotado=False)
        try:
            pedido = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            return RetryDecision(segundos=base, origen="backoff", acotado=False)
        if pedido <= 0:
            return RetryDecision(segundos=base, origen="backoff", acotado=False)
        if pedido > self.max_retry_after:
            return RetryDecision(
                segundos=self.max_wait,
                origen="retry-after",
                acotado=True,
                nota="el servidor pidió más espera del permitido",
            )
        if pedido > self.max_wait:
            return RetryDecision(
                segundos=self.max_wait,
                origen="retry-after",
                acotado=True,
                nota="el `Retry-After` supera la espera máxima de la política",
            )
        return RetryDecision(segundos=pedido, origen="retry-after", acotado=False)

    def puede_reintentar(
        self,
        intento_actual: int,
        *,
        code: StreamErrorCode | None = None,
        error: StreamError | None = None,
        cancelado: bool = False,
    ) -> bool:
        """¿Toca otro intento? Centraliza los tres cortes (FR-REC-005/006).

        Corta por: cancelación, error permanente, y tope de intentos. Los tres
        son reasons distintos y el llamante puede querer distinguirlos en el
        informe.
        """
        if cancelado:
            return False
        if not retryable(code, error=error):
            return False
        return intento_actual < max(0, int(self.max_attempts))


@dataclass(frozen=True)
class RetryDecision:
    """Qué espera se decidió y por qué (para el informe de diagnóstico)."""

    segundos: float
    #: ``"backoff"`` o ``"retry-after"``.
    origen: str = "backoff"
    #: True cuando se pidió más espera de la permitida y se recortó.
    acotado: bool = False
    nota: str = ""


@dataclass
class ReconnectSupervisor:
    """Observa un proceso y decide cuándo hay que reintentar (§17).

    Es un hilo **daemon** que sólo mira ``poll()``. No lanza nada por su cuenta:
    recibe un ``relanzar`` y lo llama cuando toca. Esa separación es la que
    permite probarlo entero sin lanzar mpv, y la que evita que el supervisor
    tenga que conocer el argv (que es de :mod:`thetvview.player.core`).

    Args:
        proc: el proceso ya lanzado, con ``poll()``.
        relanzar: ``() -> proceso_nuevo``. Se llama en cada reintento.
        policy: topes y esperas.
        session: sesión de reproducción **opcional**. Sin ella, este supervisor
            se comporta exactamente como antes. Con ella:
            - cada reintento queda registrado como :class:`AttemptRecord`;
            - ``det``/``stop`` sellan la sesión con su motivo;
            - y se consulta su ``is_current`` antes de relanzar, de modo que una
              tarea de una sesión ya sustituida no abre nada (FR-PLAY-005).
        al_cambiar: callback opcional ``(estado, intento)`` para que la UI se
            entere. Se invoca en el hilo del supervisor, así que **no** puede
            dibujar: sólo puede encolar o poner una bandera.
    """

    policy: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    #: Periodo con el que se mira `poll()`. Corto para que el corte se note,
    #: sin llegar a ser un busy-loop.
    poll_seconds: float = 1.0
    relanzar: Callable[[], object] | None = None
    #: `:func:`is_vigente`` del registro de sesiones, si lo hay. Se consulta
    #: **antes de cada relanzamiento**: sin esta comprobación, la tarea tardía
    #: del canal A reabre el canal que el usuario ya sustituyó por el B.
    is_vigente: Callable[[], bool] | None = None
    session: PlaybackSession | None = None
    #: Fuente de señal de reproducción. Sin ella, el supervisor se queda en la
    #: evidencia mínima («el proceso existe»), que es lo de siempre; con ella,
    #: distingue proceso abierto de reproducción efectiva (FR-REC-001).
    evidencia: object | None = None
    #: Clasificador opcional ``(proceso) -> StreamError``. Permite que el corte
    #: se traduzca antes de decidir si se reintenta, que es lo que impide el
    #: «401 cinco veces» (FR-REC-005).
    clasificar_error: Callable[[object], object] | None = None
    #: Política de reintentos con jitter, tope y ``Retry-After``. ``None``
    #: conserva la tabla fija de :attr:`policy` (comportamiento de siempre).
    retry_policy: RetryPolicy | None = None
    al_cambiar: Callable[[PlaybackState, int], None] | None = None

    def __post_init__(self) -> None:
        self.estado: PlaybackState = PlaybackState.CONNECTING
        self.intentos: int = 0
        self.proceso: object | None = None
        self._stop = threading.Event()
        #: El usuario lo paró: no es una caída, así que no se reconecta.
        self._desvinculado = False
        self._hilo: threading.Thread | None = None
        self._candado = threading.Lock()
        #: Evidencia de reproducción más reciente. Con fuente de señal es la
        #: medida; sin ella es el mínimo de siempre («el proceso existe»).
        self.evidencia_actual = Evidence(level=EvidenceLevel.PROCESS_ALIVE)
        #: Reconexiones **totales** del proceso. Se distingue de `intentos`,
        #: que se reinicia al alcanzar estabilidad: el informe quiere las dos.
        self.reconexiones: int = 0
        #: Último fallo clasificado, para el informe. Nunca se inventa uno.
        self.ultimo_error: StreamError | None = None
        #: Última decisión de espera, con su motivo (FR-REC-007).
        self.ultima_espera: RetryDecision | None = None
        #: Momento del próximo muestreo de evidencia. Evita preguntar al IPC en
        #: cada vuelta del bucle, que a `poll_seconds=1.0` sería un interrogatorio
        #: por segundo.
        self._evidencia_interval: float = 5.0
        self._proxima_evidencia: float = 0.0

    # --- API ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._hilo is not None and self._hilo.is_alive()

    @property
    def texto_reproduccion(self) -> str:
        """Lo que la barra dice, con el matiz de evidencia (FR-REC-002).

        Sin fuente de señal, el nivel es ``PROCESS_ALIVE`` y el texto es el de
        siempre («Reproduciendo»), porque sin evidencia no se puede añadir matiz sin
        inventarlo. Con fuente declarada y sin señal comprobable, el texto
        **sí** distingue «reproduciendo» de «reproduciendo sin comprobar», que es
        justo lo que el usuario necesita para no pensar que ve imagen.
        """
        if self.evidencia is None:
            return PlaybackState.PLAYING.texto
        return self.evidencia_actual.level.texto()

    @property
    def evidencia_nivel(self) -> EvidenceLevel:
        """Nivel de evidencia vigente."""
        return self.evidencia_actual.level

    def para_informe(self) -> dict[str, object]:
        """Resumen del supervisor para el informe (§11.3). Sin secretos."""
        return {
            "estado": self.estado.value,
            "intentos_del_ciclo": self.intentos,
            "reconexiones": self.reconexiones,
            "evidencia": self.evidencia_actual.level.value,
            "evidencia_motivo": self.evidencia_actual.reason,
            "posicion_medios": self.evidencia_actual.media_position,
            "ultimo_error": (
                self.ultimo_error.para_informe() if self.ultimo_error else None
            ),
            "ultima_espera": (
                {
                    "segundos": self.ultima_espera.segundos,
                    "origen": self.ultima_espera.origen,
                    "acotada": self.ultima_espera.acotado,
                    "nota": self.ultima_espera.nota,
                }
                if self.ultima_espera
                else None
            ),
        }

    def start(self, proc: object) -> None:
        """Empieza a observar `proc`. Idempotente."""
        with self._candado:
            if self.running:
                return
            self.proceso = proc
            self.estado = PlaybackState.CONNECTING
            self._desvinculado = False
            self._stop.clear()
            self._hilo = threading.Thread(
                target=self._bucle, name="thetvview-reconexion", daemon=True
            )
            self._hilo.start()
        # El primer intento es el que ya está corriendo: se registra para que la
        # sesión sepa cuánto duró y con qué espera arrancó.
        if self.session is not None:
            self.session.nuevo_intento()
        self._avisar(self.estado, 0)

    def stop(self) -> None:
        """Deja de observar. Idempotente; no mata el proceso."""
        self._stop.set()
        hilo = self._hilo
        if hilo is not None and hilo.is_alive():
            hilo.join(timeout=2.0)
        self._hilo = None
        self.estado = PlaybackState.STOPPED
        # `stop()` es «dejo de mirar», no «el canal terminó»: la sesión **no** se
        # sella aquí. Quien sabe por qué terminó es quien la cierra
        # (`NowPlayingScreen.stop_tracks` / `App._cerrar_recursos`), y sellarla
        # aquí pondría `PROCESS_EXITED` aunque el usuario la hubiera parado.
        return None

    def detach(self) -> None:
        """Olvida el proceso y **no** reconecta (lo paró el usuario).

        No basta con marcar parada: el bucle puede estar ya dentro de la rama
        de «se murió», y entonces relanzaría un canal que el usuario acaba de
        cerrar. Por eso hay una bandera propia, y no sólo el evento: distingue
        *«lo paró el usuario»* de *«se acabó la espera»*, que comparten evento
        pero no intención.
        """
        self._desvinculado = True
        self._stop.set()
        if self.session is not None:
            self.session.end(EndReason.USER_STOPPED, "el usuario detuvo el canal")

    # --- Interno ----------------------------------------------------------

    def _bucle(self) -> None:
        """El corazón del módulo: mirar, decidir, dormir.

        Nunca lanza. Un fallo aquí se traduce a ``ERROR`` y se publica, no a una
        excepción en un hilo que nadie ve.

        Todo lo que toca la sesión pasa por :meth:`_nota` y por la comprobación
        ``self.session is not None``, y **no hace nada** sin sesión: es lo que
        mantiene el comportamiento sin ``session`` byte a byte igual al de antes.
        """
        try:
            while not self._stop.is_set():
                if self._proceso_vivo():
                    if self.estado is not PlaybackState.PLAYING:
                        self._cambiar(PlaybackState.PLAYING)
                        self._nota("reproduciendo")
                    self._muestrear_evidencia()
                    self._stop.wait(self.poll_seconds)
                    continue
                # El proceso se fue. ¿Fue el usuario o se murió?
                if self._stop.is_set() or self._desvinculado:
                    return
                # Guard de obsolescencia: si el usuario ya abrió otro canal, esta
                # sesión dejó de ser la vigente y **no** se relanza nada.
                if self.session is not None and not self._es_vigente():
                    self._cambiar(PlaybackState.STOPPED)
                    return
                # Antes de decidir, se clasifica el corte: un 401 y un corte de
                # red llegan aquí igual y **no** pueden recibir el mismo trato.
                fallo = self._clasificar()
                self.ultimo_error = fallo
                if fallo is not None and not retryable(getattr(fallo, "code", None)):
                    self._cambiar(PlaybackState.ERROR)
                    self._nota("permanente")
                    return
                if not self._puede_reintentar(fallo):
                    self._cambiar(PlaybackState.ERROR)
                    self._nota("agotado")
                    return
                self.intentos += 1
                self.reconexiones += 1
                self._cambiar(PlaybackState.RECONNECTING)
                espera = self._espera_decidida(fallo)
                if self._stop.wait(espera):
                    return
                # La obsolescencia se comprueba **también después** de la
                # espera: el usuario pudo cambiar de canal mientras esperábamos,
                # y relanzar entonces volvería a abrir el canal viejo.
                if self.session is not None and not self._es_vigente():
                    self._cambiar(PlaybackState.STOPPED)
                    return
                nuevo = self._relanzar()
                if nuevo is None:
                    self._cambiar(PlaybackState.ERROR)
                    self._nota("reintento_fallido")
                    return
                with self._candado:
                    self.proceso = nuevo
                self._cambiar(PlaybackState.CONNECTING)
                self._nota("reintentado")
        except Exception:  # noqa: BLE001 - un hilo supervisor nunca tumba la app
            self._cambiar(PlaybackState.ERROR)
            self._nota("error_interno")

    def _es_vigente(self) -> bool:
        """¿Sigue vigente la sesión que observa este supervisor?

        Con ``is_vigente`` se delega en el guard del registro, que sabe de la
        **generación** (canal A → canal B aunque la A siga viva). Sin él, basta
        con que la sesión no haya terminado.
        """
        if self.is_vigente is not None:
            try:
                return bool(self.is_vigente())
            except Exception:  # noqa: BLE001 - un guard roto no relanza
                return False
        if self.session is None:  # pragma: no cover - nunca se llama sin sesión
            return True
        return not self.session.ended

    def _nota(self, evento: str) -> None:
        """Anota un hito del bucle en la sesión. Sin sesión, no hace nada.

        Los fallos de la anotación **se tragan**: un registro que revienta no
        puede tumbar el hilo del supervisor ni cambiar la recuperación.
        """
        sesion = self.session
        if sesion is None:
            return
        try:
            if evento == "reproduciendo":
                sesion.playing()
            elif evento == "reintentado":
                sesion.nuevo_intento()
            elif evento == "estable":
                sesion.note_stable()
            elif evento == "agotado":
                sesion.end(EndReason.RETRIES_EXHAUSTED, "se agotaron los reintentos")
            elif evento == "permanente":
                sesion.end(
                    EndReason.PLAYBACK_ERROR,
                    getattr(self.ultimo_error, "resumen", "") or "error permanente",
                )
            elif evento == "reintento_fallido":
                sesion.end(EndReason.PLAYBACK_ERROR, "el relanzamiento falló")
            elif evento == "error_interno":
                sesion.end(EndReason.INTERNAL_ERROR, "fallo en el supervisor")
        except Exception:  # noqa: BLE001 - anotar nunca rompe la recuperación
            pass

    def _proceso_vivo(self) -> bool:
        proc = self.proceso
        if proc is None:
            return False
        try:
            return proc.poll() is None  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False

    def _puede_reintentar(self, fallo: StreamError | None = None) -> bool:
        """¿Toca otro intento? Sin política nueva, la tabla de siempre.

        Con :attr:`retry_policy` entran los tres cortes que antes no existían:
        cancelación, error permanente y ``Retry-After`` (FR-REC-005/006).
        """
        if self.relanzar is None:
            return False
        if self.retry_policy is None:
            return self.intentos < self.policy.intentos_maximos
        return self.retry_policy.puede_reintentar(
            self.intentos, error=fallo, cancelado=self._desvinculado or self._stop.is_set()
        )

    def _espera_decidida(self, fallo: StreamError | None) -> float:
        """Segundos a esperar, y por qué (FR-REC-007).

        Sin :attr:`retry_policy` es exactamente ``policy.espera_para``, que es
        lo que los tests del §17 comprueban. Con ella, manda el `Retry-After`
        del servidor y la decisión queda registrada en
        :attr:`ultima_espera` para que el informe diga si se respetó o se recortó.
        """
        if self.retry_policy is None:
            return self.policy.espera_para(self.intentos)
        retry_after = getattr(fallo, "retry_after", None) if fallo else None
        decision = self.retry_policy.espera_con_retry_after(
            self.intentos, retry_after
        )
        self.ultima_espera = decision
        return decision.segundos

    def _clasificar(self) -> StreamError | None:
        """Traduce el corte a un :class:`StreamError`, si se puede.

        Sin :attr:`clasificar_error` devuelve ``None``, que es «no se sabe qué
        pasó» — y eso **sí** es reintentable, porque un corte de red de dos
        segundos es justo lo que la reconexión existe para arreglar.
        """
        if self.clasificar_error is None:
            return None
        proc = self.proceso
        try:
            resultado = self.clasificar_error(proc)
        except Exception:  # noqa: BLE001 - un clasificador roto no corta la recuperación
            return None
        return resultado if isinstance(resultado, StreamError) else None

    def _muestrear_evidencia(self) -> None:
        """Mide la evidencia de reproducción si toca (FR-REC-001).

        Sólo puede **subir o bajar** el nivel, nunca fabricarlo. Y el umbral de
        estabilidad resetea los contadores del ciclo: es lo que distingue «el
        proceso sigue vivo» de «se ha reproducido bien» (FR-REC-008).
        """
        fuente = self.evidencia
        if fuente is None:
            # Sin fuente declarada, el único hecho comprobable es que existe.
            self.evidencia_actual = ProcessAliveSignal().sample()
            return
        ahora = time.monotonic()
        if ahora < self._proxima_evidencia:
            return
        self._proxima_evidencia = ahora + self._evidencia_interval
        try:
            medida = fuente.sample()
        except Exception:  # noqa: BLE001 - una sonda rota no cambia el estado
            return
        if not isinstance(medida, Evidence):  # pragma: no cover - contrato interno
            return
        anterior = self.evidencia_actual
        # ``NONE`` es el único nivel que **no** pisa a otro: una lectura que no
        # vio nada no aporta información sobre los medios, así que no debe
        # borrar lo que ya se sabía. Todo lo demás —incluido ``UNKNOWN``, que
        # significa «no lo sé» y no «no hay nada»— sí manda, porque reportar que
        # no se puede comprobar **es** información.
        if medida.level is not EvidenceLevel.NONE:
            self.evidencia_actual = medida
        if medida.estable:
            if self.intentos:
                self.intentos = 0
            self._nota("estable")

    def _relanzar(self) -> object | None:
        if self.relanzar is None:
            return None
        try:
            return self.relanzar()
        except Exception:  # noqa: BLE001 - un relanzamiento fallido no es un crash
            return None

    def _cambiar(self, estado: PlaybackState) -> None:
        if estado is self.estado:
            return
        self.estado = estado
        self._avisar(estado, self.intentos)

    def _avisar(self, estado: PlaybackState, intento: int) -> None:
        if self.al_cambiar is None:
            return
        try:
            self.al_cambiar(estado, intento)
        except Exception:  # noqa: BLE001 - el observador no rompe al supervisor
            pass