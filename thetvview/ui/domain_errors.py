"""Errores del dominio traducidos a modales (Fase 8 · P2.6 · U-02).

Las Fases 3 a 7 dejaron cuatro backends que **validan y rechazan** con su
propio vocabulario (``ReglaInvalida``, ``ColeccionesManagerError``,
``SchedulerError``, ``RecordingError``). Ninguno puede mostrar nada —eso sería
mezclar dominio con interfaz— y sin traducción esos mensajes nunca llegarían
al usuario en la forma que el resto de la app usa.

Este módulo es el **único** puente, y es puro: sin ``curses``, sin disco, sin
efectos. Recibe la excepción y devuelve un
:class:`~thetvview.ui.errormsg.UiError` listo para ``App.show_error``.

Dos reglas gobiernan la traducción:

**El backend dice qué pasó; la UI no lo reescribe.** El mensaje del dominio es
la verdad sobre el fallo —«campo no permitido: quality» es más útil que un
«error inesperado»— y por eso se conserva literal. Lo que la UI añade es el
título, que dice en qué flujo pasó.

**Nada se muestra sin redactar.** Las URLs de streaming llevan token y
contraseña con frecuencia, y algunos errores del backend las incluyen en el
texto. Todo pasa por :func:`~thetvview.security.redaction.redact_text` antes de
convertirse en mensaje, y hay un test que lo comprueba con un token real.
"""

from __future__ import annotations

from ..security.redaction import redact_text
from .errormsg import ErrorKind, UiError

__all__ = [
    "is_domain_error",
    "describe_domain_error",
]


#: Excepción de backend -> título del modal. Se resuelve por el **nombre de la
#: clase**, no importándola: la interfaz no debe depender de que cada backend
#: cargue bien para poder mostrar un error. Los nombres son estables porque cada
#: paquete publica su excepción con ese nombre exacto.
_TITULOS: tuple[tuple[str, str, ErrorKind], ...] = (
    ("ReglaInvalida", "La regla no es válida", ErrorKind.COLECCIONES),
    ("ColeccionesManagerError", "No se pudo trabajar con la colección", ErrorKind.COLECCIONES),
    ("SchedulerError", "No se pudo programar la grabación", ErrorKind.GRABACION),
    ("RecordingError", "No se pudo grabar", ErrorKind.GRABACION),
    ("StorageError", "No se pudo guardar el archivo", ErrorKind.PLAYLIST),
)

#: Índice por nombre de clase, construido una vez: `_TITULOS` se recorre en
#: cada error y el bucle lineal no hace falta.
_POR_CLASE: dict[str, tuple[str, ErrorKind]] = {
    clase: (titulo, kind) for clase, titulo, kind in _TITULOS
}

#: Título cuando la excepción es de un backend que todavía no se conoce. Honesto
#: y sin inventar: dice la verdad, que es que no sabemos de dónde viene.
_TITULO_DESCONOCIDO = "Algo no salió como se esperaba"

#: Mensaje cuando la excepción no dice nada: un modal con el cuerpo vacío no
#: explica nada, y un texto genérico es mejor que un hueco.
_SIN_DETALLE = (
    "No hay más detalle sobre este fallo. "
    "Puedes volver atrás e intentarlo de nuevo."
)


def is_domain_error(exc: BaseException) -> bool:
    """¿Este error viene de un backend de las Fases 3 a 7?"""
    return type(exc).__name__ in _POR_CLASE


def describe_domain_error(exc: BaseException) -> UiError:
    """Traduce una excepción de dominio a un :class:`UiError` mostrable.

    No inventa el motivo, no lo suaviza y no lo esconde: envuelve el mensaje
    que el backend escribió, ya redactado, bajo un título que dice en qué flujo
    pasó. Una excepción de un backend desconocido también se traduce —con un
    título honesto— porque dejar un ``str(exc)`` suelto por la interfaz es
    justamente lo que esta fase evita.
    """
    titulo, kind = _POR_CLASE.get(type(exc).__name__, (_TITULO_DESCONOCIDO, ErrorKind.PLAYLIST))
    crudo = redact_text(str(exc)).strip()
    return UiError(
        title=titulo,
        message=crudo or _SIN_DETALLE,
        detail=None,  # el backend no separa el detalle técnico del mensaje
        kind=kind,
    )