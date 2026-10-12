"""Evidencia de reproducción: ¿hay imagen o sólo hay un proceso? (P1.2 §5.1).

El problema que motiva este módulo
----------------------------------

``proc.poll() is None`` dice que el **proceso** existe. No dice que el stream
haya abierto, ni que haya llegado un segmento, ni que el reloj avance. Y esa
diferencia es exactamente lo que el usuario vive: un canal que abre mpv y se
queda bloqueado leyendo un manifiesto vacío produce exactamente la misma señal
que uno que está decodificando, y la app le dice a los dos «Reproduciendo».

Con lo que había, un proceso abierto sin medios y una reproducción estable eran
**indistinguibles**, y no había forma de que dejaran de serlo.

La escalera
-----------

```text
NONE ──► PROCESS_ALIVE ──► SOURCE_ACCEPTED ──► MEDIA_SEEN ──► STABLE
 nada      el proceso       el motor         llegó medio    avanzó de forma
           existe           aceptó la        y lo decodifica   continuo
                             fuente
```

Cada peldaño es **más fuerte** que el anterior y ninguno se presume: sólo se
sube cuando hay una señal concreta. En particular ``STABLE`` exige que el reloj
de medios **avance** de forma continua durante un umbral, no que exista (FR-REC-001).

``UNKNOWN`` no está en la escalera
---------------------------------

VLC y mplayer, en esta app, no exponen por dónde mirar si hay medios. La
respuesta honesta es ``UNKNOWN``, **no** ``NONE`` ni ``STABLE``: ``NONE`` diría
«comprobado y no hay nada», y eso también es una afirmación. ``UNKNOWN`` dice
«no lo sé», que es lo único verdadero.

Por eso ``UNKNOWN`` **no** se ordena con la escalera: comparar «no lo sé» con
«lo sé y es que no hay nada» no tiene sentido, así que :attr:`EvidenceLevel.rank`
de ``UNKNOWN`` es ``-1`` y su comprobación es explícita.

Modos de fallo que este módulo **no** tiene
-------------------------------------------

- No lanza. Si el IPC se cae, el motor se cuelga o la propiedad no existe, el
  resultado es un nivel más bajo y un ``reason`` con el motivo. Una sonda que
  revienta no puede tumbar la reproducción.
- No inventa tiempos. Si no se puede comprobar, no se comprueba (NFR-005).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

__all__ = [
    "EvidenceLevel",
    "Evidence",
    "SignalSource",
    "UnknownSignalSource",
    "MpvSignalSource",
    "ProcessAliveSignal",
    "signal_source_for",
    "STABLE_SECONDS",
    "advance_seconds",
]

#: Umbral inicial de estabilidad (SDD §5.2): 30 s de evidencia continua.
#: Configurable por motor — aquí es el valor por defecto, no una verdad.
STABLE_SECONDS: float = 30.0


class EvidenceLevel(str, Enum):
    """Nivel de evidencia de que hay reproducción de verdad."""

    #: El motor no expone ninguna señal que sirva. **No** es «no hay nada».
    UNKNOWN = "unknown"
    #: Todavía no se ha mirado, o se miró y no hay señal.
    NONE = "none"
    #: El proceso existe. Es lo único que sabía la app antes de este módulo.
    PROCESS_ALIVE = "process_alive"
    #: El motor aceptó la fuente (abrió el archivo o respondió al IPC).
    SOURCE_ACCEPTED = "source_accepted"
    #: Llegó medio y el motor lo está procesando.
    MEDIA_SEEN = "media_seen"
    #: El reloj de medios avanzó de forma continua el tiempo suficiente.
    STABLE = "stable"

    @property
    def rank(self) -> int:
        """Posición en la escalera. ``UNKNOWN`` no está en ella: vale -1.

        La comparación entre niveles es por ``rank`` y **no** por el orden de
        declaración, porque ``UNKNOWN`` está declarado el primero (para que se lea
        primero en la UI) y declararlo el primero haría que ``UNKNOWN >= NONE``.
        """
        return _RANK.get(self, -1)

    @property
    def comproblable(self) -> bool:
        """False si este nivel significa «no lo sé» en vez de «lo sé»."""
        return self is not EvidenceLevel.UNKNOWN

    def texto(self) -> str:
        """Etiqueta corta, apta para la barra y para un modal."""
        return {
            EvidenceLevel.UNKNOWN: "Reproduciendo (sin comprobar)",
            EvidenceLevel.NONE: "Sin señal",
            EvidenceLevel.PROCESS_ALIVE: "Proceso abierto",
            EvidenceLevel.SOURCE_ACCEPTED: "Fuente abierta",
            EvidenceLevel.MEDIA_SEEN: "Llegando imagen",
            EvidenceLevel.STABLE: "Reproducción estable",
        }[self]

    def __str__(self) -> str:  # pragma: no cover - comodidad de depuración
        return self.value


_RANK: dict[EvidenceLevel, int] = {
    EvidenceLevel.NONE: 0,
    EvidenceLevel.PROCESS_ALIVE: 1,
    EvidenceLevel.SOURCE_ACCEPTED: 2,
    EvidenceLevel.MEDIA_SEEN: 3,
    EvidenceLevel.STABLE: 4,
}


def advance_seconds(desde: EvidenceLevel, hasta: EvidenceLevel) -> float | None:
    """Diferencia de rango entre dos niveles, o None si no son comparables.

    ``None`` cuando uno de los dos es ``UNKNOWN``: comparar «no lo sé» con
    «lo sé y es que no hay nada» no tiene sentido, y devolver un número ahí
    invitaría a usarlo.
    """
    if not desde.comproblable or not hasta.comproblable:
        return None
    return float(hasta.rank - desde.rank)


@dataclass(frozen=True)
class Evidence:
    """Una medición de evidencia, con su motivo y cuándo se tomó."""

    level: EvidenceLevel = EvidenceLevel.NONE
    #: Por qué se crees este nivel. Vacío si es obvio.
    reason: str = ""
    #: Reloj de medios observado (segundos dentro del medio), si se pudo leer.
    media_position: float | None = None
    #: Segundos de avance continuo acumulado para el umbral de estabilidad.
    continuo: float = 0.0
    observed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def estable(self) -> bool:
        """True sólo con evidencia de estabilidad. Nunca «el proceso sigue vivo»."""
        return self.level is EvidenceLevel.STABLE

    def resumen(self) -> str:
        """Una línea para el informe, sin inventar nada."""
        base = self.level.texto()
        if self.media_position is not None:
            base = f"{base} · reloj {self.media_position:.1f} s"
        return f"{base}. {self.reason}" if self.reason else base


class SignalSource:
    """Contrato de una fuente de señal de reproducción.

    Existe como interfaz documentada, no como clase base con implementación: los
    tres motores exponen cosas distintas y una clase abstracta común acabaría
    siendo un ``if motor == "mpv"`` gigante.
    """

    #: Nombre del motor. Aparece en el informe para saber quién habló.
    motor: str = ""

    def sample(self) -> Evidence:
        """Mide la evidencia **ahora**. Nunca lanza."""
        raise NotImplementedError

    def describe(self) -> str:
        """Qué señales mira esta fuente, en una frase (FR-REC-002)."""
        return ""


class UnknownSignalSource(SignalSource):
    """Fuente para motores sin señal disponible: siempre ``UNKNOWN``.

    Es la respuesta honesta para VLC y mplayer en esta app. Devolver
    ``PROCESS_ALIVE`` —que es lo que hacía el código anterior— convierte «no lo
    sé» en «lo sé», que es el éxito ficticio que FR-REC-002 prohíbe.
    """

    def __init__(self, motor: str = "", motivo: str = "") -> None:
        self.motor = str(motor or "")
        self.motivo = motivo or (
            f"'{self.motor}' no expone por dónde comprobar si hay medios, "
            "así que no se puede confirmar la reproducción"
            if self.motor
            else "este motor no expone ninguna señal de medios"
        )

    def sample(self) -> Evidence:
        return Evidence(level=EvidenceLevel.UNKNOWN, reason=self.motivo)

    def describe(self) -> str:
        return "Sin señal disponible: el nivel se queda en desconocido."


class ProcessAliveSignal(SignalSource):
    """La única evidencia que se puede tener sin ningún motor: el proceso.

    Sirve para los tres motores cuando no hay IPC, y es **el mínimo** que se
    puede afirmar: el proceso existe. Nunca sube de ahí por su cuenta, porque
    subir sería precisamente el hueco que este módulo cierra.
    """

    def sample(self) -> Evidence:
        return Evidence(
            level=EvidenceLevel.PROCESS_ALIVE,
            reason="el proceso existe, pero eso no demuestra que haya imagen",
        )

    def describe(self) -> str:
        return "Sólo se comprueba que el proceso esté vivo."


class MpvSignalSource(SignalSource):
    """Evidencia de reproducción por el IPC de mpv (FR-REC-002).

    Reutiliza :class:`thetvview.player.mpv_ipc.MpvIpc`, que ya sabe hablar con
    el socket: no hay un segundo cliente IPC, sólo un consumidor más.

    Qué mira y qué significa:

    - el IPC **responde** → ``SOURCE_ACCEPTED``: mpv aceptó el comando, o sea que
      está vivo y escuchando;
    - ``time-pos`` existe y **cambia** → ``MEDIA_SEEN``: hay medio y el reloj
      avanza. La clave es que **avance**: un ``time-pos`` congelado significa
      buffer vacío o fuente parada, y es precisamente el caso que el proceso
      vivo no distingue;
    - el reloj avanza de forma continua ≥ ``stable_seconds`` → ``STABLE``.

    Si el IPC no responde, el nivel baja (nunca sube) y el motivo explica por
    qué: un IPC desconectado es un dato, no una ausencia de datos.

    El reloj de medios y el reloj de pared son inyectables para que los tests no
    dependan de ``sleep`` (riesgo §10 del plan).
    """

    def __init__(
        self,
        ipc,
        *,  # noqa: ANN001 - MpvIpc
        motor: str = "mpv",
        stable_seconds: float = STABLE_SECONDS,
        reloj: Callable[[], float] | None = None,
        ahora: Callable[[], datetime] | None = None,
        min_delta: float = 0.05,
    ) -> None:
        self.ipc = ipc
        self.motor = motor
        self.stable_seconds = max(0.0, float(stable_seconds))
        self._reloj = reloj or time.monotonic
        self._ahora = ahora or (lambda: datetime.now(timezone.utc))
        #: Avance mínimo para considerarlo progreso. mpv publica ``time-pos``
        #: con milisegundos, así que dos sondeos seguidos casi nunca dan el
        #: mismo valor: sin este umbral, una diferencia de 1 ms contaría como
        #: «avance continuo» y un canal congelado se declararía estable.
        self._min_delta = max(0.0, float(min_delta))
        self._ultimo_pos: float | None = None
        self._ultimo_instante: float | None = None
        self._continuo = 0.0

    # -- API ------------------------------------------------------------------

    def sample(self) -> Evidence:
        """Mide y devuelve la evidencia de ahora. Nunca lanza."""
        try:
            pos = self._leer_posicion()
        except Exception as exc:  # noqa: BLE001 - una sonda no tumba la sesión
            # El IPC se cayó: se baja el nivel y se dice por qué. Bajar es
            # siempre seguro; subir sin señal es lo que este módulo no hace.
            self._olvidar()
            return Evidence(
                level=EvidenceLevel.UNKNOWN,
                reason=f"no se pudo leer el reproductor: {exc}",
                observed_at=self._ahora(),
            )
        if pos is None:
            self._olvidar()
            return Evidence(
                level=EvidenceLevel.SOURCE_ACCEPTED,
                reason="el reproductor responde pero aún no hay posición de medios",
                observed_at=self._ahora(),
            )
        instante = self._reloj()
        if self._ultimo_pos is None:
            self._ultimo_pos = pos
            self._ultimo_instante = instante
            # Hay posición pero no se ha visto avanzar: el motor acepta la fuente
            # y el medio existe, pero **todavía** no hay evidencia de avance.
            return Evidence(
                level=EvidenceLevel.SOURCE_ACCEPTED,
                reason="el medio está abierto pero el reloj aún no avanza",
                media_position=pos,
                observed_at=self._ahora(),
            )
        delta = pos - self._ultimo_pos
        transcurrido = max(0.0, instante - float(self._ultimo_instante or instante))
        if delta <= self._min_delta:
            # El reloj **no** avanzó. El medio está abierto pero no corre, que es
            # justo el caso que `poll() is None` no distingue: el proceso vive, el
            # buffer está lleno y no entra ni un segmento. La continuidad se
            # reinicia porque el umbral exige avance **continuo**.
            self._continuo = 0.0
            self._ultimo_pos = pos
            self._ultimo_instante = instante
            return Evidence(
                level=EvidenceLevel.SOURCE_ACCEPTED,
                reason="el medio está abierto pero su reloj no avanza",
                media_position=pos,
                continuo=0.0,
                observed_at=self._ahora(),
            )
        self._continuo += transcurrido
        self._ultimo_pos = pos
        self._ultimo_instante = instante
        nivel = (
            EvidenceLevel.STABLE
            if self.stable_seconds <= 0 or self._continuo >= self.stable_seconds
            else EvidenceLevel.MEDIA_SEEN
        )
        motivo = (
            "el reloj avanza de forma continua"
            if nivel is EvidenceLevel.STABLE
            else "el reloj avanza, todavía no lleva el tiempo suficiente"
        )
        return Evidence(
            level=nivel,
            reason=motivo,
            media_position=pos,
            continuo=round(self._continuo, 3),
            observed_at=self._ahora(),
        )

    def describe(self) -> str:
        return (
            "Canal de control de mpv: se lee la posición del medio y sólo se "
            "cuenta como estable si avanza de forma continua."
        )

    # -- interno --------------------------------------------------------------

    def _leer_posicion(self) -> float | None:
        """`time-pos` como float, o None si el motor no lo publica."""
        valor = self.ipc.get_property("time-pos")
        if valor is None or isinstance(valor, bool):
            return None
        try:
            return float(valor)
        except (TypeError, ValueError):
            return None

    def _olvidar(self) -> None:
        """Reinicia el seguimiento continuo: sin evidencia, no hay continuidad."""
        self._ultimo_pos = None
        self._ultimo_instante = None
        self._continuo = 0.0


def signal_source_for(
    motor: str | None,
    *,
    ipc=None,  # noqa: ANN001 - MpvIpc | None
    stable_seconds: float = STABLE_SECONDS,
    reloj: Callable[[], float] | None = None,
    ahora: Callable[[], datetime] | None = None,
) -> SignalSource:
    """Fuente de señal adecuada a `motor`.

    Sólo mpv tiene, hoy, una señal comprobable. Para el resto se devuelve
    :class:`UnknownSignalSource`: es preferible decir «no lo sé» a fabricar un
    ``PROCESS_ALIVE`` que es lo mismo que el hueco que se vino a cerrar.

    Con ``ipc=None`` y motor mpv también se devuelve ``UnknownSignalSource``:
    sin canal de control no hay nada que mirar, y adivinar sería inventar.
    """
    nombre = str(motor or "").strip().lower()
    if nombre == "mpv" and ipc is not None:
        return MpvSignalSource(
            ipc, stable_seconds=stable_seconds, reloj=reloj, ahora=ahora
        )
    return UnknownSignalSource(nombre)