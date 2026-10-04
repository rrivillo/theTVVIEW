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
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

__all__ = [
    "PlaybackState",
    "PlaybackProfile",
    "PROFILE_FLAGS",
    "ReconnectPolicy",
    "ReconnectSupervisor",
    "DEFAULT_BACKOFF",
    "profile_flags",
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
        al_cambiar: callback opcional ``(estado, intento)`` para que la UI se
            entere. Se invoca en el hilo del supervisor, así que **no** puede
            dibujar: sólo puede encolar o poner una bandera.
    """

    policy: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    #: Periodo con el que se mira `poll()`. Corto para que el corte se note,
    #: sin llegar a ser un busy-loop.
    poll_seconds: float = 1.0
    relanzar: Callable[[], object] | None = None
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

    # --- API ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._hilo is not None and self._hilo.is_alive()

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
        self._avisar(self.estado, 0)

    def stop(self) -> None:
        """Deja de observar. Idempotente; no mata el proceso."""
        self._stop.set()
        hilo = self._hilo
        if hilo is not None and hilo.is_alive():
            hilo.join(timeout=2.0)
        self._hilo = None
        self.estado = PlaybackState.STOPPED

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

    # --- Interno ----------------------------------------------------------

    def _bucle(self) -> None:
        """El corazón del módulo: mirar, decidir, dormir.

        Nunca lanza. Un fallo aquí se traduce a ``ERROR`` y se publica, no a una
        excepción en un hilo que nadie ve.
        """
        try:
            while not self._stop.is_set():
                if self._proceso_vivo():
                    if self.estado is not PlaybackState.PLAYING:
                        self._cambiar(PlaybackState.PLAYING)
                    self._stop.wait(self.poll_seconds)
                    continue
                # El proceso se fue. ¿Fue el usuario o se murió?
                if self._stop.is_set() or self._desvinculado:
                    return
                if not self._puede_reintentar():
                    self._cambiar(PlaybackState.ERROR)
                    return
                self.intentos += 1
                self._cambiar(PlaybackState.RECONNECTING)
                espera = self.policy.espera_para(self.intentos)
                if self._stop.wait(espera):
                    return
                nuevo = self._relanzar()
                if nuevo is None:
                    self._cambiar(PlaybackState.ERROR)
                    return
                with self._candado:
                    self.proceso = nuevo
                self._cambiar(PlaybackState.CONNECTING)
        except Exception:  # noqa: BLE001 - un hilo supervisor nunca tumba la app
            self._cambiar(PlaybackState.ERROR)

    def _proceso_vivo(self) -> bool:
        proc = self.proceso
        if proc is None:
            return False
        try:
            return proc.poll() is None  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False

    def _puede_reintentar(self) -> bool:
        if self.relanzar is None:
            return False
        return self.intentos < self.policy.intentos_maximos

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