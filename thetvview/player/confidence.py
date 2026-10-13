"""Nivel de confianza de cada capacidad de la matriz (FR-CAP-001/002).

La matriz de capacidades responde «¿sabe hacer esto el reproductor?» y esa
pregunta tiene tres respuestas distintas, según de dónde venga el saber
(FR-CAP-002 del SDD de mejoras P1):

- ``DECLARED`` — indicado por documentación o configuración, sin comprobar
  en el binario instalado;
- ``DETECTED`` — comprobado en el binario instalado: es la medición empírica
  que documentan :mod:`thetvview.player.protocols` (2026-10-04) y
  :mod:`thetvview.player.capabilities` (2026-10-02) con los binarios de esta
  máquina;
- ``VERIFIED`` — comprobado mediante una **prueba reproducible** enlazada en
  :data:`EVIDENCIAS_VERIFICADAS` para una versión/entorno concreto.

El tercer nivel es el único que **no se puede declarar a mano**: la entrada
debe apuntar a un test del repositorio y :func:`referencias_invalidas` lo
comprueba. Hoy el registro está **vacío a propósito**: la medición se hizo a
mano, de forma empírica, así que toda capacidad medida es ``DETECTED`` y
ninguna es ``VERIFIED`` («nada VERIFIED sin test reproducible enlazado»,
plan Fase 5). Cuando exista un test que reproduzca una medición, su entrada
sube a ``VERIFIED`` sin tocar el resto de la regla.

Esta regla sustituye al «verificado» booleano implícito que había hasta
ahora —el conjunto ``medido`` de ``protocols`` y los booleanos de
``capabilities``—; **ambos se conservan tal cual** porque los siguen usando
el router y los tests (compatibilidad). La diferencia es que ahora cada
afirmación puede decir de qué tipo es su evidencia en vez de un sí/no que no
distinguía «declarado» de «medido».
"""

from __future__ import annotations

import importlib
from enum import Enum

__all__ = [
    "Confidence",
    "EVIDENCIAS_VERIFICADAS",
    "clave_de",
    "evidencia",
    "nivel_confianza",
    "referencias_invalidas",
]


class Confidence(str, Enum):
    """Nivel de confianza de una capacidad (FR-CAP-002).

    Es ``str`` a propósito: se puede serializar a JSON y comparar con
    strings sin importar el enum en cada sitio. Los valores son los
    literales del SDD, en el mismo orden de confianza creciente.
    """

    DECLARED = "DECLARED"
    DETECTED = "DETECTED"
    VERIFIED = "VERIFIED"

    @property
    def orden(self) -> int:
        """0 = declarado, 1 = detectado, 2 = verificado (para ordenar)."""
        return _ORDEN[self]


_ORDEN: dict[Confidence, int] = {
    Confidence.DECLARED: 0,
    Confidence.DETECTED: 1,
    Confidence.VERIFIED: 2,
}


def clave_de(reproductor: str, capacidad: str) -> str:
    """Clave normalizada de una capacidad en :data:`EVIDENCIAS_VERIFICADAS`.

    Formato ``"reproductor:capacidad"``, p. ej. ``"mpv:http"`` o
    ``"mplayer:mpegts"``. Los espacios de nombres de las capacidades
    (transportes, kinds de pista, contenedores, códecs) no se pisan hoy, y
    si algún día se pisan, la clave sigue siendo única porque lleva el
    reproductor delante.
    """
    return f"{str(reproductor or '').strip().lower()}:{str(capacidad or '').strip().lower()}"


#: Capacidad → referencia al test que la verifica (FR-CAP-008).
#:
#: El valor es una referencia al repositorio en forma
#: ``"tests.test_modulo"`` o ``"tests.test_modulo::test_algo"`` (con
#: ``::`` opcional hasta la clase). Una capacidad sólo puede ser
#: ``VERIFIED`` si está aquí **y** la referencia resuelve: no basta con
#: escribir el nivel a mano.
#:
#: Dato de sólo lectura en tiempo de ejecución: los tests lo extienden con
#: ``mock.patch.dict``; la app nunca lo modifica.
EVIDENCIAS_VERIFICADAS: dict[str, str] = {}


def evidencia(clave: str) -> str | None:
    """Referencia al test que verifica `clave`, o None si no tiene."""
    return EVIDENCIAS_VERIFICADAS.get(clave)


def nivel_confianza(medido: bool, clave: str | None = None) -> Confidence:
    """La regla completa, en un solo sitio (FR-CAP-002).

    Args:
        medido: True si la capacidad está comprobada en el binario
            instalado (``DETECTED``); False si sólo está declarada.
        clave: clave de :func:`clave_de` para mirar la evidencia
            ``VERIFIED``. None = no mirar (sólo declarado/detectado).

    Returns:
        ``VERIFIED`` sólo con evidencia enlazada; si no, ``DETECTED`` o
        ``DECLARED`` según `medido`. El orden de la regla importa: una
        evidencia reproducible manda sobre lo medido a mano.
    """
    if clave and clave in EVIDENCIAS_VERIFICADAS:
        return Confidence.VERIFIED
    return Confidence.DETECTED if medido else Confidence.DECLARED


def _referencia_resuelve(referencia: str) -> bool:
    """¿La referencia apunta a algo que existe en el repositorio?"""
    partes = [p for p in str(referencia).split("::") if p]
    if not partes:
        return False
    try:
        objeto = importlib.import_module(partes[0])
    except Exception:  # noqa: BLE001 - una referencia rota no debe romper nada
        return False
    for parte in partes[1:]:
        objeto = getattr(objeto, parte, None)
        if objeto is None:
            return False
    return True


def referencias_invalidas() -> list[str]:
    """Claves ``VERIFIED`` cuya evidencia no resuelve (para tests y auditoría).

    Si esto devuelve algo, alguien escribió un ``VERIFIED`` sin prueba
    reproducible enlazada, que es exactamente lo que el plan prohíbe.
    """
    return sorted(
        clave
        for clave, referencia in EVIDENCIAS_VERIFICADAS.items()
        if not _referencia_resuelve(referencia)
    )
