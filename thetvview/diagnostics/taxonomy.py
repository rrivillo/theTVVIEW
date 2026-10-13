"""H-02 — Taxonomía del historial técnico: una sola, la del reproductor.

El historial técnico va a escribir un ``reason_code`` por evento y por
entrada. Ese código tiene que salir de **algún sitio único**, y ese sitio ya
existe: :mod:`thetvview.player.errors`. Si el diagnóstico se inventara sus
propias cadenas (``"fallo_de_red"``, ``"no_se_abre"``), aparecería un segundo
vocabulario que nadie mantiene y que nadie sabe traducir: el informe diría una
cosa y el historial otra, sin que nadie pudiera detectar el desacuerdo.

Reglas que fija este módulo:

**Los códigos son del reproductor.** Los de :class:`StreamErrorCode` más los
motivos de cierre :class:`EndReason`, que no son errores pero son el otro
vocabulario que el historial tiene que poder escribir. La unión de ambos es
:data:`MOTIVOS_CONOCIDOS`, y :func:`codigo_de` sólo sabe resolver dentro de
ahí.

**``recoverable`` no es una política nueva.** Sale de
:attr:`StreamError.reintentable`. La política de reintentos vive en un solo
sitio —:class:`thetvview.player.supervisor.RetryPolicy` y el conjunto
``NO_REINTENTABLE``— y el diagnóstico la *lee*. Si el criterio cambiara
mañana, cambiaría solo.

**Lo observado y lo inferido no se mezclan** (FR-REC-004). ``UNKNOWN`` es una
interpretación, no un hecho: el hecho es el ``returncode`` y el mensaje del
reproductor. :class:`Causa` lleva las dos columnas separadas, y cuando no hay
código conocido no se inventa ninguno: ``reason_code`` queda vacío, que es
distinto de ``"unknown"`` y más honesto que ambas.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..player.errors import DESCRIPCION, StreamError, StreamErrorCode
from ..player.session import EndReason

__all__ = [
    "CODES_CONOCIDOS",
    "MOTIVOS_CONOCIDOS",
    "Causa",
    "causa_de",
    "causa_de_codigo",
    "causa_de_motivo",
    "codigo_de",
    "es_razon_conocida",
    "explicacion_de",
]


#: Los códigos de error del reproductor, tal cual, sin reescribir (H-02).
CODES_CONOCIDOS: frozenset[str] = frozenset(c.value for c in StreamErrorCode)

#: Todo lo que el historial puede escribir como ``reason_code``: errores de
#: reproducción **y** motivos de cierre de sesión.
#:
#: Son dos vocabularios distintos y legítimamente distintos: un ``auth_failed``
#: dice qué pasó, un ``user_stopped`` dice por qué dejamos de mirar. Lo que
#: no se admite es un tercer vocabulario que no sea ninguno de los dos.
MOTIVOS_CONOCIDOS: frozenset[str] = CODES_CONOCIDOS | frozenset(
    m.value for m in EndReason
)

#: Índice inverso para no recorrer el enum en cada resolución.
_POR_VALOR: dict[str, StreamErrorCode] = {c.value: c for c in StreamErrorCode}

#: Los motivos de cierre, por su valor. No son códigos de error y no entran
#: en :func:`codigo_de`: son otro vocabulario con otro significado, y mezclarlos
#: haría que «lo paró el usuario» se pudiera leer como «error de red».
_FINAL: frozenset[str] = frozenset(m.value for m in EndReason)


def codigo_de(valor: object) -> StreamErrorCode | None:
    """Resuelve `valor` a un :class:`StreamErrorCode`, o ``None``.

    Devuelve ``None`` —no lanza, no normaliza a ``UNKNOWN``— para todo lo que
    no sea un código real. La diferencia importa: ``UNKNOWN`` afirma «se
    observó algo y no se sabe la causa», que es un dato; ``None`` afirma «no
    hay causa que decir». Confundirlas es como nace un diagnóstico que
    inventa precisión que no tiene.
    """
    if isinstance(valor, StreamErrorCode):
        return valor
    if isinstance(valor, str):
        return _POR_VALOR.get(valor.strip())
    return None


def es_razon_conocida(texto: object) -> bool:
    """True si `texto` es un código de error o un motivo de cierre real."""
    if not isinstance(texto, str):
        return False
    limpio = texto.strip()
    return limpio in MOTIVOS_CONOCIDOS or limpio in _POR_VALOR


@dataclass(frozen=True)
class Causa:
    """Por qué pasó algo, con lo observado **separado** de lo inferido.

    Los dos campos que antes iban juntos en un solo ``motivo``:

    - ``reason_code`` es la **interpretación**: un código de la taxonomía del
      reproductor, o vacío si no hay ninguno defendible;
    - ``observado`` son los **hechos**: el mensaje tal cual salió del
      reproductor, el estado HTTP, el ``returncode``.

    Y el campo que hace útil la separación: :attr:`causa_conocida`. Un
    ``reason_code`` de ``"unknown"`` parece un dato; :attr:`causa_conocida`
    en ``False`` dice que es una conjetura, y es lo que impide que el informe
    presente una suposición con la seguridad de una medición.
    """

    #: Código de la taxonomía del reproductor. Vacío si no se sabe.
    reason_code: str = ""
    #: Si repetir (o cambiar de motor) puede servir de algo. ``None`` si no
    #: hay motivo sobre el que pronunciarse.
    recoverable: bool | None = None
    #: Lo que pasó, sin interpretar. Ya redactado por quien lo compuso.
    observado: str = ""
    #: ``returncode`` del reproductor, si terminó. ``None`` si no terminó.
    returncode: int | None = None
    #: HTTP status de la última respuesta conocida, si la hay.
    http_status: int | None = None

    @property
    def causa_conocida(self) -> bool:
        """True si el ``reason_code`` no es una conjetura.

        Vacío **y** ``"unknown"`` cuentan como desconocidos, pero por motivos
        distintos: el primero porque no hay diagnóstico, el segundo porque
        el diagnóstico no pudo averagediguar.
        """
        return bool(self.reason_code) and self.reason_code != StreamErrorCode.UNKNOWN.value

    @property
    def confianza(self) -> str:
        """``"observada"`` u ``"desconocida"``. Va al informe, no a la UI."""
        return "observada" if self.causa_conocida else "desconocida"

    @classmethod
    def desde(cls, fallo: "StreamError | None") -> "Causa":
        """Construye la causa desde un :class:`StreamError` del reproductor."""
        if fallo is None:
            return cls()
        codigo = codigo_de(fallo.code)
        return cls(
            reason_code=codigo.value if codigo is not None else "",
            # `reintentable` es la política del supervisor, no una de aquí.
            recoverable=codigo.reintentable if codigo is not None else None,
            observado=fallo.observado,
            returncode=fallo.returncode,
            http_status=fallo.http_status,
        )

    def para_informe(self) -> dict[str, object]:
        """Diccionario apto para el informe de diagnóstico.

        Omite lo que no se sabe en vez de rellenarlo con ceros o con
        ``"unknown"`` (la regla de §3.3, que también aplica aquí).
        """
        datos: dict[str, object] = {
            "reason_code": self.reason_code,
            "causa_conocida": self.causa_conocida,
            "confianza": self.confianza,
        }
        if self.recoverable is not None:
            datos["recoverable"] = self.recoverable
        if self.observado:
            datos["observado"] = self.observado
        if self.returncode is not None:
            datos["returncode"] = self.returncode
        if self.http_status is not None:
            datos["http_status"] = self.http_status
        return datos


def causa_de(fallo: "StreamError | None") -> Causa:
    """Causa de un fallo del reproductor. Sin fallo, causa vacía."""
    return Causa.desde(fallo)


def causa_de_motivo(motivo: object) -> Causa:
    """Causa a partir de un :class:`EndReason`, no de un error de red.

    Un motivo de cierre no viene con hecho observado ni con política de
    reintento: «lo detuviste tú» no se reintenta, pero tampoco es un fallo que
    haya que arreglar. Por eso :attr:`Causa.recoverable` queda en ``None``
    —no se pronuncia— en vez de en ``False``, que affirmaría que se comprobó
    que no sirve.
    """
    if isinstance(motivo, EndReason):
        return Causa(reason_code=motivo.value, recoverable=None)
    if isinstance(motivo, str) and motivo.strip() in _FINAL:
        return Causa(reason_code=motivo.strip(), recoverable=None)
    return Causa()


def causa_de_codigo(codigo: object) -> Causa:
    """Causa a partir de un código suelto (``StreamErrorCode``, str o None).

    Es la puerta de entrada para quien ya tiene el código y no el
    ``StreamError`` entero: el supervisor, que clasifica; o el historial al
    releer una entrada. Un valor que no es un código real produce una causa
    **vacía**, no un código inventado.
    """
    encontrado = codigo_de(codigo)
    if encontrado is None:
        return Causa()
    return Causa(
        reason_code=encontrado.value,
        recoverable=encontrado.reintentable,
    )


def explicacion_de(razon_code: object) -> str:
    """Frase corta y apta para un modal (regla de `AGENTS.md`).

    Cubre los dos vocabularios: si es un código de error usa la descripción
    del reproductor; si es un motivo de cierre, la del propio
    :class:`EndReason`. Lo que no se reconoce se declara desconocido —no se
    inventa una explicación para un código que no existe.
    """
    if not isinstance(razon_code, str) or not razon_code.strip():
        return ""
    limpio = razon_code.strip()
    codigo = _POR_VALOR.get(limpio)
    if codigo is not None:
        return DESCRIPCION.get(codigo, codigo.value)
    try:
        return EndReason(limpio).explicacion
    except ValueError:
        return (
            f"No se reconoce «{limpio}» como motivo de reproducción. "
            "Puede que venga de una versión anterior de la aplicación."
        )