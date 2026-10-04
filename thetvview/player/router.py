"""Elección de reproductor por **capacidad**, no por el primero que se encuentre
(SDD-M §8, plan Fase 2).

El hueco que esto cubre (H1 del plan): :func:`thetvview.player.launch` recorre
``config.SUPPORTED_PLAYERS`` y lanza el primer binario que localiza, sin mirar
qué sabe hacer. Para HLS da igual; para ``rtsp://`` significa abrir un canal en
un reproductor que se queda esperando en silencio.

El orden de decisión es el §27 del SDD-M, que es también el §8 avanzado:

1. el backend preferido por el usuario (``prefs.preferred_backend``);
2. soporte **declarado y medido** para el transporte
   (:data:`player.protocols.PLAYER_PROTOCOL_SUPPORT`);
3. la prioridad de :data:`thetvview.config.SUPPORTED_PLAYERS`;
4. la disponibilidad del ejecutable (:func:`config.find_player`);
5. el historial de fallo de la sesión, para no repetir un backend que ya murió
   con este mismo canal (§26: ``max_backend_attempts``).

Y una regla que el SDD-M no escribe pero que sale de su propio §51: **si
ningún backend es compatible, el canal no se ofrece y no es un error.** No es
un fallo de reproducción —no se intentó reproducir nada— sino que el entorno
no puede abrir ese tipo de fuente. La diferencia importa en el mensaje que ve
el usuario y por eso la devuelve :class:`NoCompatibleBackend` en vez de un
``PlayerError`` genérico.

Ni una clase ABC por reproductor (decisión D8): ``player/core.py`` y
``player/capabilities.py`` ya son tablas, y una clase por reproductor sería el
**segundo** sitio que dice qué sabe hacer mpv. Añadir un backend es añadir una
fila a :data:`PLAYER_PROTOCOL_SUPPORT`, sin tocar este archivo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .. import config
from ..streams.detector import detect_protocol
from ..streams.transport import Transport, describe as describe_transport
from .errors import PlayerError
from .protocols import (
    PLAYER_PROTOCOL_SUPPORT,
    PlayerProtocolSupport,
    support_for,
    verified,
)

__all__ = [
    "NoCompatibleBackend",
    "Candidate",
    "select_backend",
    "attempts",
    "explain_no_backend",
    "order_candidates",
    "MAX_BACKEND_ATTEMPTS",
]

#: Cuántos reproductores distintos se prueban para **un** canal antes de
#: rendirse (SDD-M §26). Dos es lo que dice el SDD-M; tres convertirían un
#: canal malo en una espera de un minuto.
MAX_BACKEND_ATTEMPTS: int = 2


class NoCompatibleBackend(PlayerError):
    """Ningún reproductor instalado abre este transporte.

    No es un fallo de reproducción: es una limitación del entorno. El mensaje
    está escrito para un modal y **nunca** lleva la URL (puede traer token).

    Hereda de :class:`~thetvview.player.errors.PlayerError` a propósito: todo el
    código que ya captura «falló el reproductor» sigue funcionando sin cambios,
    y quien quiera distinguir los dos casos (SDD-M §14) comprueba
    ``isinstance(exc, NoCompatibleBackend)``.
    """


@dataclass(frozen=True)
class Candidate:
    """Un reproductor considerado para un canal, con por qué se queda o no."""

    name: str
    #: None = descartado antes de llegar a ser candidato.
    path: str | None = None
    #: Por qué no sirve, si no sirve (va al modal, no a un log).
    motivo: str = ""
    #: Era el preferido por el usuario.
    preferido: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.path)


def _transport_of(url: str) -> Transport:
    """Transporte de una URL (sólo el esquema: no descarga nada)."""
    return detect_protocol(url=url).transport


def order_candidates(
    url: str,
    *,
    preferred: str | None = None,
    failures: Iterable[str] = (),
    available: dict[str, str | None] | None = None,
) -> list[Candidate]:
    """Reproductores para `url`, en el orden en que deben probarse.

    Es puro: no toca disco ni la red, sólo decide. Por eso se puede probar
    entero sin lanzar un solo binario, que es como lo verifica la matriz del
    §29 del SDD-M.

    Args:
        url: la URL **ya resuelta** (sin referencias opacas: la resolución
            ocurre antes, en :func:`thetvview.stream_ref.resolve_channel_url`).
        preferred: nombre del reproductor que eligió el usuario la última vez.
        failures: reproductores que ya fallaron con **este** canal.
        available: mapa nombre → ruta, para no llamar a ``shutil.which`` en los
            tests. Si es None se usa :func:`config.find_player`.

    Returns:
        Lista de :class:`Candidate`: los primeros son viables y el resto
        explica por qué quedaron fuera. Los descartados vienen **al final**,
        para que un modal pueda decir «probé mpv y vlc, ninguno abre udp://»
        sin que el llamador tenga que reconstruirlos.
    """
    transporte = _transport_of(url)
    ya_fallaron = {str(n).strip().lower() for n in failures if n}
    if available is None:
        available = {name: config.find_player(name) for name in config.SUPPORTED_PLAYERS}

    viables: list[Candidate] = []
    descartados: list[Candidate] = []
    sobra = False

    for nombre in config.SUPPORTED_PLAYERS:
        soporte: PlayerProtocolSupport | None = support_for(nombre)
        es_preferido = (str(preferred or "").strip().lower() == nombre)
        ruta = available.get(nombre)
        # La disponibilidad del ejecutable va **después** del soporte (orden
        # §27): saber que un reproductor no abre RTSP es más útil que saber
        # que no está instalado, porque instalar no lo arregla.
        if soporte is None:
            descartados.append(
                Candidate(nombre, motivo="sin tabla de transportes declarada")
            )
            continue
        if not soporte.can_play(transporte):
            descartados.append(
                Candidate(
                    nombre,
                    motivo=soporte.why_not(transporte)
                    or f"no abre {describe_transport(transporte)}",
                    preferido=es_preferido,
                )
            )
            continue
        if not ruta:
            descartados.append(
                Candidate(nombre, motivo="no está instalado en esta máquina",
                          preferido=es_preferido)
            )
            continue
        if nombre in ya_fallaron:
            # §26: el historial manda al final, no impide intentarlo. Con un
            # solo candidato reintentarlo es mejor que no hacer nada.
            sobra = True
            descartados.append(
                Candidate(nombre, motivo="ya falló con este canal en esta sesión",
                          preferido=es_preferido)
            )
            continue
        viables.append(Candidate(nombre, path=ruta, preferido=es_preferido))

    # 1: el preferido por el usuario, si es viable, va primero.
    viables.sort(key=lambda c: 0 if c.preferido else 1)
    return viables + descartados


def select_backend(
    url: str,
    *,
    preferred: str | None = None,
    failures: Iterable[str] = (),
    available: dict[str, str | None] | None = None,
) -> Candidate:
    """El reproductor con el que hay que abrir `url`.

    Aplica el tope de :data:`MAX_BACKEND_ATTEMPTS`: si el transporte lo abren
    cuatro binarios, se prueban los dos primeros y el resto no se llega a
    intentar. Sin tope, un canal con una URL rota sería cuatro esperas
    seguidas en vez de un mensaje.

    Raises:
        NoCompatibleBackend: no hay ningún reproductor que abra el transporte.
            El mensaje dice **qué revisar**, distinguiendo los dos fallos que
            el §14 del SDD-M exige separar: que el binario no lo abra (instalar
            otro reproductor lo arregla) de que la red falle (no lo arregla).
    """
    candidatos = order_candidates(
        url, preferred=preferred, failures=failures, available=available
    )
    viables = [c for c in candidatos if c.ok]
    if viables:
        return viables[0]
    raise NoCompatibleBackend(explain_no_backend(url, candidatos))


def attempts(
    url: str,
    *,
    preferred: str | None = None,
    failures: Iterable[str] = (),
    available: dict[str, str | None] | None = None,
    max_attempts: int = MAX_BACKEND_ATTEMPTS,
) -> list[Candidate]:
    """Los reproductores que hay que **intentar**, en orden, con tope.

    :func:`select_backend` responde «con cuál empiezo»; esta responde «cuáles
    puedo probar si el primero falla», y es donde vive el tope del §26
    (``max_backend_attempts = 2``).

    La diferencia importa: un canal puede estar mal **y** los tres reproductores
    abrir su transporte. Sin tope serían tres esperas para un canal que no va a
    funcionar nunca; con tope son dos y un mensaje honesto.
    """
    candidatos = order_candidates(
        url, preferred=preferred, failures=failures, available=available
    )
    viables = [c for c in candidatos if c.ok]
    return viables[: max(1, int(max_attempts))]


def explain_no_backend(url: str, candidatos: Iterable[Candidate]) -> str:
    """Mensaje de modal: por qué no se puede abrir esta fuente.

    Separa los dos casos del §14 del SDD-M porque el consejo es distinto:

    - *«ningún reproductor abre este transporte»* → instalar otro sí arregla;
    - *«lo abren, pero la red no»* → lo que hay que revisar es la red (para
      multicast: grupo, interfaz, Wi-Fi, router; para UDP: ruta y firewall).
    """
    transporte = _transport_of(url)
    nombre = describe_transport(transporte)
    lista = list(candidatos)
    if not lista:
        return (
            f"No hay ningún reproductor instalado para abrir un canal {nombre}. "
            "Instala mpv, VLC o mplayer y vuelve a intentarlo."
        )

    # Dos limitaciones distintas, con consejos distintos (§14 del SDD-M).
    # Se decide por la tabla, no parseando el texto del motivo: el motivo es
    # para el usuario, no para la lógica.
    lo_abren = [s for s in PLAYER_PROTOCOL_SUPPORT.values() if s.can_play(transporte)]
    if lo_abren:
        return (
            f"Los reproductores que abren {nombre} no están instalados "
            "aquí, así que el canal no se puede reproducir. "
            f"Instala uno de: {', '.join(s.name for s in lo_abren)}."
        )

    if not verified(transporte):
        if transporte is Transport.UNKNOWN:
            return (
                "No se ha podido reconocer el tipo de este canal, así que no "
                "se sabe qué reproductor lo abriría. Comprueba que la línea de "
                "la lista apunte a una URL completa (con http://, rtsp://…)."
            )
        if transporte is Transport.UDP:
            return (
                "Este canal es UDP/multicast y no se ha podido verificar en "
                "esta máquina, así que no se ofrece. Si el grupo es correcto, "
                "comprueba tu red: que la interfaz tenga multicast, que el "
                "firewall no lo bloquee y que el router lo reenvíe."
            )
        return (
            f"Este canal usa {nombre}, un transporte que no se ha podido "
            "verificar en esta máquina, así que no se ofrece."
        )

    motivos = "; ".join(f"{c.name}: {c.motivo}" for c in lista if c.motivo)
    return f"No hay reproductor disponible para {nombre}. {motivos}".strip()