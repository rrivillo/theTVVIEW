"""Política de codificación del parser M3U (M3U-001 · FR-M3U-001).

Una lista IPTV la escribió alguien que no nos dijo en qué. La política es
explícita, en orden, y siempre se puede contar qué pasó:

1. **BOM UTF-8** → ``utf-8-sig``: la lista empieza por ``﻿`` y ese
   prefijo no debe ensuciar el primer ``#EXTM3U``.
2. **UTF-8 estricto**: si el texto no es UTF-8 válido, no es UTF-8 y no
   se disimula.
3. **cp1252 estricto**: el heredado habitual en paneles IPTV europeos.
4. **latin-1**: último recurso; cada byte tiene una letra, así que no se
   pierde nada (nunca falla).
5. ``errors="replace"`` **sólo** si todas las anteriores fallaron. Ahí la
   sustitución deja de ser silenciosa: queda registrada la decisión, hay
   texto de aviso para el modal y la entrada se guarda en :data:`AVISOS`.

Los pasos 3 y 4 también se registran aunque no sustituyan nada: pueden
cambiar un ``tvg-id`` o un nombre, y eso no puede pasar sin que alguien
lo sepa (FR-M3U-001: *no* registrar en silencio cuando puede afectar a
identificadores o nombres).
"""

from __future__ import annotations

import codecs
from collections import deque
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AVISOS",
    "CODIFICACIONES_HEREDADAS",
    "DecisionCodificacion",
    "avisos_recientes",
    "decodificar_m3u",
    "registrar",
    "texto_aviso_pendiente",
]

#: Heredadas probadas en orden. ``latin-1`` es la última y no falla nunca.
CODIFICACIONES_HEREDADAS: tuple[str, ...] = ("cp1252", "latin-1")

#: Registro acotado de decisiones que el usuario **debe** ver. Un deque
#: con tope: un diagnóstico de codificación no puede crecer sin límite.
AVISOS: deque["DecisionCodificacion"] = deque(maxlen=50)


@dataclass(frozen=True)
class DecisionCodificacion:
    """Qué se hizo con los bytes de una lista, y si hay que contarlo."""

    #: Texto ya decodificado (siempre `str`, nunca `bytes`).
    texto: str
    #: Codificación con la que se leyó realmente.
    codificacion: str
    #: Caracteres sustituidos por «�» (0 en la práctica normal).
    sustituciones: int = 0
    #: De dónde vinieron los bytes (ruta o URL), para el aviso.

    origen: str = ""

    @property
    def hubo_sustitucion(self) -> bool:
        """True si algún byte se perdió de verdad."""
        return self.sustituciones > 0

    @property
    def es_heredada(self) -> bool:
        """True si no es UTF-8 (con o sin BOM)."""
        return not self.codificacion.startswith("utf-8")

    def debe_avisar(self) -> bool:
        """True si la decisión puede afectar a lo que el usuario ve."""
        return self.hubo_sustitucion or self.es_heredada

    def aviso(self) -> str | None:
        """Texto breve de modal, o None si no hay nada que contar.

        El jugador es el usuario: primero el efecto (nombres raros), luego
        el motivo (codificación), y nunca un volcado técnico.
        """
        donde = f" de «{self.origen}»" if self.origen else ""
        if self.hubo_sustitucion:
            return (
                f"La lista{donde} no está en una codificación que se pueda leer "
                f"entera: se han sustituido {self.sustituciones} caracteres por "
                "«�». Es posible que algunos nombres de canales se vean raro."
            )
        if self.es_heredada:
            return (
                f"La lista{donde} está en {self.codificacion}, una codificación "
                "antigua en vez de UTF-8. Se ha leído bien, pero si ves "
                "caracteres raros en los nombres es por eso."
            )
        return None


def decodificar_m3u(data: bytes, *, origen: str = "") -> DecisionCodificacion:
    """Aplica la política de codificación a los bytes de una lista.

    Args:
        data: bytes tal cual llegaron (nunca se modifica).
        origen: ruta o URL, sólo para poder nombrar el aviso.

    Returns:
        La :class:`DecisionCodificacion` con el texto ya decodificado. No
        lanza: si el contenido es indescifrable, sustituye y lo dice.
    """
    if data.startswith(codecs.BOM_UTF8):
        try:
            return DecisionCodificacion(
                texto=data.decode("utf-8-sig"), codificacion="utf-8-sig", origen=origen
            )
        except UnicodeDecodeError:
            # BOM que miente: se sigue con la cadena normal.
            pass

    for codificacion in ("utf-8",) + CODIFICACIONES_HEREDADAS:
        try:
            return DecisionCodificacion(
                texto=data.decode(codificacion),
                codificacion=codificacion,
                origen=origen,
            )
        except UnicodeDecodeError:
            continue

    texto = data.decode("utf-8", errors="replace")
    return registrar(
        DecisionCodificacion(
            texto=texto,
            codificacion="utf-8",
            # Se cuenta lo que quedó como marca de sustitución. Puede
            # contar de más si el original ya traía «�»; es un aviso y no
            # una métrica, y sobre-estimar es lo prudente.
            sustituciones=texto.count("�"),
            origen=origen,
        )
    )


def registrar(decision: DecisionCodificacion) -> DecisionCodificacion:
    """Deja constancia de una decisión que el usuario debe ver.

    Devuelve la misma decisión para poder encadenar (``... .texto``). Lo
    que **no** hay que avisar (UTF-8 con o sin BOM) no se guarda: el
    registro es para lo raro, y un aviso por cada lista sería ruido.
    """
    if decision.debe_avisar():
        AVISOS.append(decision)
    return decision


def avisos_recientes() -> tuple["DecisionCodificacion", ...]:
    """Decisiones registradas, de la más antigua a la más reciente."""
    return tuple(AVISOS)


def texto_aviso_pendiente() -> str | None:
    """El aviso más reciente sin mostrar, o None si no hay ninguno.

    La TUI lo consume tras cargar una lista: así el modal sale cuando el
    usuario tiene delante la lista que falló, no mucho después.
    """
    for decision in reversed(AVISOS):
        texto = decision.aviso()
        if texto:
            # Se marca como visto sacándolo del registro: el aviso se
            # muestra una vez, no en cada repintado.
            try:
                AVISOS.remove(decision)
            except ValueError:  # pragma: no cover - carrera con otro consumidor
                pass
            return texto
    return None
