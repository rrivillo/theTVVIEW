"""Clasificación de errores para presentarlos como **mensajes, no como trazas**
(SDD `SDD-error-messages-theTVVIEW.md`, plan `plan-mensajes-error.md` F1).

Este módulo es **puro**: sin ``curses``, sin red, sin efectos y sin acceso a
disco. Es el sitio donde vive la decisión «qué le digo al usuario»; *quién* lo
muestra y *cómo* es cosa de :mod:`thetvview.ui.app`. Esa separación (§2.2) es la
que permite testear los quince casos de §15.1 sin terminal.

Dos ideas gobiernan el módulo:

**El detalle técnico es secundario y va redactado.** El mensaje principal es
siempre una frase que dice qué pasó y qué puede hacer el usuario; el código
HTTP —si existe— va en ``detail`` (§8, §9). Nunca ``str(exc)`` como mensaje
principal: ``HTTPError(403, …)`` no es una explicación, es una excepción de
Python (§2.5, §9).

**No se inventan códigos.** ``detail`` sólo aparece cuando hay un ``status``
real: un 404 que nadie ha visto no se escribe (§8.4).

Clasificación por **tipo y atributo**, no por *string matching* sobre el texto:
``NetworkError.status`` y ``NetworkError.reason`` (F1-bis) son los que deciden.
``fetch_bytes`` de :mod:`thetvview.m3u_parser` re-envuelve la excepción del
dominio en un ``OSError`` plano, pero conserva ``__cause__``
(``raise … from exc``), así que :func:`_cause_chain` la deshace para recuperar el
tipo real sin romper el contrato ``OSError`` que capturan las pantallas (H3).

Los errores de reproducción no usan una taxonomía nueva: consumen
:class:`~thetvview.player.errors.StreamErrorCode`, que ya distingue un 404 de
reproducción de uno de red (H8). Duplicarla sería drift.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ..security.errors import (
    AccountDisabledError,
    AccountExpiredError,
    AuthenticationError,
    ConnectionLimitError,
    InvalidSourceError,
    InvalidUrlError,
    NetworkError,
    ParseError,
    ProviderError,
    RateLimitError,
    ResponseTooLargeError,
    SSRFBlockedError,
    TLSValidationError,
    UnsupportedProviderError,
)
from ..security.redaction import redact_text

__all__ = [
    "ErrorKind",
    "UiError",
    "describe_playlist_error",
    "describe_playback_error",
    "empty_playlist_error",
]


class ErrorKind(str, Enum):
    """De qué flujo viene el error: decide **qué acciones existen** (§7).

    No es decorativo: ``show_error`` construye el pie a partir de lo que hay,
    así que un ``kind`` sin retry posible no puede acabar anunciando un
    atajo que nadie atiende (§13).
    """

    PLAYLIST = "playlist"
    PLAYBACK = "playback"


@dataclass(frozen=True)
class UiError:
    """Un error listo para mostrarse: título, mensaje y detalle opcional.

    Attributes:
        title: qué pasó, en una línea. «No se pudo cargar la lista».
        message: por qué, en lenguaje llano. «El servidor rechazó el acceso.»
        detail: información técnica **ya redactada** y sólo secundaria:
            «Código HTTP: 403». ``None`` si no hay nada que añadir (§8.4).
        kind: qué flujo es; de aquí salen las acciones disponibles (§7).
    """

    title: str
    message: str
    detail: str | None = None
    kind: ErrorKind = ErrorKind.PLAYLIST

    def cuerpo(self) -> str:
        """Título + mensaje + detalle, como texto para un modal.

        El detalle va en su propio párrafo para que se lea como lo que es:
        información secundaria. Y todo pasa por :func:`redact_text` otra vez
        aquí (§2.4): la redacción en :class:`~thetvview.ui.widgets.StatusBar`
        es la segunda barrera, no la primera.
        """
        partes = [f"{self.title}\n\n{self.message}"]
        if self.detail:
            partes.append(self.detail)
        return redact_text("\n\n".join(partes))


# ---------------------------------------------------------------------------
# Títulos por flujo (§8)
# ---------------------------------------------------------------------------

_TITULO_LISTA = "No se pudo cargar la lista"
_TITULO_LISTA_VACIA = "La lista está vacía"
_TITULO_REPRODUCCION = "No se pudo reproducir este canal"

#: Títulos que dependen de si es una lista o un canal. Lo que cambia entre 401
#: y 403 no es el título sino el mensaje (§8.1 vs §8.2).
_TITULOS: dict[ErrorKind, str] = {
    ErrorKind.PLAYLIST: _TITULO_LISTA,
    ErrorKind.PLAYBACK: _TITULO_REPRODUCCION,
}


def _titulo(kind: ErrorKind) -> str:
    return _TITULOS[kind]


# ---------------------------------------------------------------------------
# Mensajes por estado HTTP (§8.1–§8.3, §8.8)
# ---------------------------------------------------------------------------


def _mensaje_401(kind: ErrorKind) -> str:
    """401: el servidor rechaza credenciales o acceso (§8.1).

    A diferencia del 403, aquí **sí** se puede mencionar la credencial: un 401
    significa que el servidor recibió una identidad y no la aceptó.
    """
    del kind  # el texto es el mismo en los dos flujos (§8.1)
    return "El servidor rechazó las credenciales o el acceso."


def _mensaje_403(kind: ErrorKind) -> str:
    """403: acceso denegado (§8.2).

    **No** se dice «credenciales incorrectas». Un 403 puede ser permisos, IP,
    región, cabeceras, URL caducada, CDN o protección del proveedor, y afirmar
    una causa concreta sería inventar un diagnóstico (§8.2).
    """
    del kind
    return "El servidor rechazó el acceso."


def _mensaje_404(kind: ErrorKind) -> str:
    """404: la dirección no existe (§8.3)."""
    if kind is ErrorKind.PLAYBACK:
        return "La dirección del canal ya no existe."
    return "No se encontró la lista solicitada."


def _mensaje_5xx() -> str:
    """5xx: problema del servidor al procesar (§8.8)."""
    return "El servidor encontró un problema al procesar la solicitud."


def _mensaje_timeout() -> str:
    """§8.4. Sin código HTTP: aquí no lo hay y no se inventa."""
    return "El servidor tardó demasiado en responder."


def _mensaje_tls() -> str:
    """§8.5."""
    return "No se pudo establecer una conexión segura con el servidor."


def _mensaje_dns() -> str:
    """§8.6."""
    return "No se pudo encontrar el servidor."


def _mensaje_conexion() -> str:
    """§8.7."""
    return "No se pudo conectar con el servidor."


def _mensaje_decode() -> str:
    """§8.9."""
    return "No se pudo interpretar la respuesta del servidor."


def _mensaje_protocolo() -> str:
    """§8.10."""
    return "El formato de esta fuente no es compatible."


def _mensaje_sin_reproductor() -> str:
    """§8.11."""
    return "No hay ningún reproductor compatible instalado."


def _mensaje_desconocido() -> str:
    """§8.12. El detalle técnico sólo aparece si es seguro y útil."""
    return "No se pudo completar la operación."


def _mensaje_respuesta_grande() -> str:
    """Respuesta por encima del límite de seguridad: no es un error de red."""
    return (
        "El servidor devolvió una respuesta demasiado grande y la app la detuvo "
        "para no agotar la memoria."
    )


def _mensaje_bloqueada() -> str:
    """Bloqueo anti-SSRF: la app lo hizo, no el servidor."""
    return (
        "Esa dirección apunta a una red interna o privada y la app la bloquea "
        "por seguridad."
    )


def _mensaje_limite_peticiones() -> str:
    """429 y el semáforo local: en los dos casos hay que esperar."""
    return "Hay demasiadas peticiones en marcha. Espera un momento e inténtalo de nuevo."


def _mensaje_url_invalida() -> str:
    """La política de URL la rechazó: la culpa es de la dirección, no del servidor."""
    return "La dirección no es válida. Corrígela e inténtalo de nuevo."


def _mensaje_formato() -> str:
    """Fichero/respuesta ilegible o corrupto."""
    return "El archivo no se puede leer: su contenido no es válido o está dañado."


def _mensaje_proveedor() -> str:
    """El servidor no habla el protocolo del proveedor."""
    return (
        "Ese servidor no parece un proveedor compatible. "
        "Prueba a añadirlo como lista M3U."
    )


def _mensaje_cuenta() -> str:
    """Cuenta caducada o desactivada: la causa está en el proveedor."""
    return "Tu cuenta no está activa. Contacta con tu proveedor para recuperarla."


# ---------------------------------------------------------------------------
# Cadena de causas y estado
# ---------------------------------------------------------------------------

#: Tope de la cadena ``__cause__``/``__context__``. Es una cadena corta por
#: construcción (``fetch_bytes`` añade un eslabón), pero el tope evita que un
#: `raise ... from ...` en bucle convierta la clasificación en un cuelgue.
_MAX_CAUSES = 12


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """La excepción y sus antecesoras, en orden, sin ciclos ni repeticiones.

    ``m3u_parser.fetch_bytes`` convierte ``RateLimitError``/``ConnectionLimitError``
    (que no heredan de ``OSError``) en un ``OSError`` plano para cumplir su
    contrato, pero lo hace con ``raise … from exc``: el tipo real sigue
    accesible en ``__cause__`` (H3). Recorriendo la cadena recuperamos la
    clasificación sin tocar ese contrato.

    Se recorren también ``__context__``: ``raise X`` dentro de un ``except`` —sin
    ``from``— lo rellena, y es el caso de la mayoría del código legacy.
    """
    cadena: list[BaseException] = []
    vistos: set[int] = set()
    actual: BaseException | None = exc
    while actual is not None and len(cadena) < _MAX_CAUSES:
        if id(actual) in vistos:
            break
        vistos.add(id(actual))
        cadena.append(actual)
        siguiente = actual.__cause__ or actual.__context__
        if siguiente is actual or not isinstance(siguiente, BaseException):
            break
        actual = siguiente
    return cadena


def _buscar(
    cadena: list[BaseException], tipo: type | tuple[type, ...]
) -> BaseException | None:
    """El primero de `tipo` en `cadena`, buscando primero el tipo exacto.

    No vale un ``isinstance(exc, NetworkError)`` para encontrar un
    ``TLSValidationError``: la jerarquía es justo la información que se busca,
    así que el tipo concreto gana siempre al genérico.

    Acepta también una tupla de tipos (``AccountExpiredError`` y
    ``AccountDisabledError`` son dos clases y se buscan igual), que es la
    firma que ya usan ``isinstance`` y ``except``.
    """
    for exc in cadena:
        if type(exc) is tipo:
            return exc
    for exc in cadena:
        if isinstance(exc, tipo):
            return exc
    return None


def _status_of(cadena: list[BaseException]) -> int | None:
    """El código HTTP de la cadena, si lo hubo.

    Se busca en **todas** las eslabones y se prefiere el más específico: un
    ``NetworkError`` con ``status=403`` reenvuelto en un ``OSError`` plano sigue
    teniendo su status a dos saltos (§8, H2/H3).
    """
    for exc in cadena:
        status = getattr(exc, "status", None)
        if isinstance(status, int) and 100 <= status <= 599:
            return status
    return None


def _reason_of(cadena: list[BaseException]) -> str | None:
    """La razón de red de :class:`NetworkError`, si la fijó `safe_http` (F1-bis)."""
    for exc in cadena:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, str) and reason:
            return reason
    return None


#: ``reason`` de red → mensaje de §8. Lo que se clasifica por atributo y no por
#: la prosa es justo lo que hace que el mensaje no cambie al refactorizar
#: ``_status_message`` (F1-bis, punto 3).
_POR_RAZON: dict[str, Callable[[], str]] = {
    "tls": _mensaje_tls,
    "dns": _mensaje_dns,
    "timeout": _mensaje_timeout,
    "refused": _mensaje_conexion,
    "unreachable": _mensaje_conexion,
    "connection": _mensaje_conexion,
}


def _detail_status(status: int | None) -> str | None:
    """«Código HTTP: 403» o ``None`` si no hay status (§8.4).

    Es el único detalle técnico que se escribe a mano, y es secundario: nunca
    sustituye al mensaje (§9).
    """
    if status is None:
        return None
    return redact_text(f"Código HTTP: {status}")


def _detalle_red(cadena: list[BaseException], status: int | None) -> str | None:
    """El detalle de un fallo de red: el status si lo hay, si no el motivo.

    El texto original de la excepción **no** se copia aquí: suele traer la URL
    y el SDD §2.4 lo prohíbe. El ``reason`` de `safe_http` es un token cerrado
    («dns», «timeout»…), no prosa, así que es seguro como dato.
    """
    if status is not None:
        return _detail_status(status)
    reason = _reason_of(cadena)
    if not reason:
        return None
    etiqueta = {
        "tls": "La conexión segura no se pudo validar.",
        "dns": "El nombre del servidor no resuelve.",
        "timeout": "Tiempo de espera agotado.",
        "refused": "El servidor rechazó la conexión.",
        "unreachable": "No hay ruta hasta el servidor.",
        "connection": "No se pudo completar la conexión.",
    }.get(reason)
    return redact_text(etiqueta) if etiqueta else None


# ---------------------------------------------------------------------------
# Taxonomía de reproducción (H8: se reutiliza, no se duplica)
# ---------------------------------------------------------------------------

#: ``StreamErrorCode`` → **texto** de §8, ya resuelto. Se importa perezosamente
#: para que este módulo no dependa del paquete ``player`` al importarse desde la
#: UI.
#:
#: Son cadenas y no fábricas porque aquí el flujo ya es ``PLAYBACK``: por eso
#: ``_mensaje_403`` y ``_mensaje_404`` se llaman con el ``kind`` ya metido en el
#: momento de construir la tabla, y quien consulta sólo tiene que pintar el
#: resultado. En :data:`_POR_RAZON`, en cambio, **sí** son fábricas, porque allí
#: el mismo ``reason`` llega desde los dos flujos.
_MENSAJE_POR_CODIGO: dict[str, str] = {}


def _cargar_mensajes_reproduccion() -> None:
    """Traduce ``StreamErrorCode`` a los mensajes de §8. Idempotente."""
    if _MENSAJE_POR_CODIGO:
        return
    from ..player.errors import StreamErrorCode

    _MENSAJE_POR_CODIGO.update(
        {
            StreamErrorCode.INVALID_URL.value: _mensaje_url_invalida(),
            StreamErrorCode.UNSUPPORTED_PROTOCOL.value: _mensaje_protocolo(),
            StreamErrorCode.CONNECTION_FAILED.value: _mensaje_conexion(),
            StreamErrorCode.CONNECTION_TIMEOUT.value: _mensaje_timeout(),
            StreamErrorCode.NETWORK_ERROR.value: _mensaje_conexion(),
            StreamErrorCode.BUFFERING_TIMEOUT.value: _mensaje_timeout(),
            StreamErrorCode.TLS_FAILED.value: _mensaje_tls(),
            StreamErrorCode.DECODE_ERROR.value: _mensaje_decode(),
            StreamErrorCode.PLAYER_MISSING.value: _mensaje_sin_reproductor(),
            StreamErrorCode.AUTH_FAILED.value: _mensaje_403(ErrorKind.PLAYBACK),
            StreamErrorCode.NOT_FOUND.value: _mensaje_404(ErrorKind.PLAYBACK),
            StreamErrorCode.SERVER_ERROR.value: _mensaje_5xx(),
            StreamErrorCode.BACKEND_ERROR.value: _mensaje_desconocido(),
            StreamErrorCode.UNKNOWN.value: _mensaje_desconocido(),
        }
    )


def _status_de_reproductor(exc: BaseException) -> int | None:
    """El código HTTP que el reproductor escribió en su salida, o ``None``.

    Los tres binarios lo ponen en su mensaje de depuración y el ``returncode``
    no lo distingue (mpv, ffmpeg y VLC usan 1 para casi todo), así que se
    recupera con ``classify_message`` —que ya existe— en lugar de con otro
    regex parallelo.
    """
    from ..player.errors import StreamErrorCode, classify_message

    texto = str(exc)
    codigo = classify_message(texto)
    if codigo is StreamErrorCode.AUTH_FAILED:
        return 403 if "403" in texto or "forbidden" in texto.lower() else 401
    if codigo is StreamErrorCode.NOT_FOUND:
        return 404
    if codigo is StreamErrorCode.SERVER_ERROR:
        return 500
    return None


#: Un ``PlayerError`` cuyo texto trae un código suelto. Los tres binarios lo
#: escriben («Server returned 404 (Not Found)», «cannot be opened (403)»), y sin
#: esto el usuario vería «No se pudo completar la operação» delante de un 404
#: que sí sabemos leer (§8.3).
_RE_STATUS_PROTO = re.compile(r"\b(4\d\d|5\d\d)\b")

#: «No hay reproductor» **no** es un fallo de reproducción (§8.11): no se intentó
#: nada. Los dos mensajes de :mod:`thetvview.player.core` que lo dicen son
#: «Reproductor 'mpv' no encontrado» y «No hay reproductor disponible». El
#: ``returncode`` 126/127 ya lo clasifica como ``PLAYER_MISSING``, pero aquí la
#: excepción llega aplanada a texto, así que se reconoce por su forma.
_RE_SIN_REPRODUCTOR = re.compile(
    r"reproductor\s+'[^']*'\s+no\s+encontrad"
    r"|no\s+hay\s+reproductor\s+disponible"
    r"|no\s+se\s+encontr[oó]\s+(?:el\s+)?(?:binario|reproductor|ejecutable)",
    re.I,
)


# ---------------------------------------------------------------------------
# Descriptores públicos
# ---------------------------------------------------------------------------


def empty_playlist_error(name: str = "") -> UiError:
    """Una lista que se abrió bien pero **no trae canales** (E2).

    Sin ``retry`` a propósito en el call site: recargar una lista vacía no la va
    a llenar, y el §13 prohíbe anunciar un atajo decorativo. El título es
    distinto del de un fallo de red porque es otro hecho: la lista **sí** se
    cargó.
    """
    quien = f"'{str(name).strip()}' " if str(name or "").strip() else ""
    return UiError(
        title=_TITULO_LISTA_VACIA,
        message=(
            f"{quien}no contiene ningún canal, así que no hay nada que ver. "
            "Comprueba que la lista sea la correcta o que el proveedor siga "
            "enviando canales."
        ),
        detail=None,
        kind=ErrorKind.PLAYLIST,
    )


def describe_playlist_error(exc: BaseException) -> UiError:
    """Traduce un error de carga de lista al mensaje de §8.

    Cubre los quince casos de §15.1 más los bloqueos propios del dominio. La
    clasificación va **por tipo y atributo**; el texto sólo se mira para el
    status que el reproductor escribió a mano, que es el único dato que no
    existe como atributo en ningún sitio.
    """
    cadena = _cause_chain(exc)
    status = _status_of(cadena)
    kind = ErrorKind.PLAYLIST

    # --- 1. Status HTTP: manda sobre el tipo, porque es lo más concreto ----
    if status is not None:
        if status == 401:
            return UiError(_titulo(kind), _mensaje_401(kind), _detail_status(status), kind)
        if status == 403:
            return UiError(_titulo(kind), _mensaje_403(kind), _detail_status(status), kind)
        if status == 404:
            return UiError(_titulo(kind), _mensaje_404(kind), _detail_status(status), kind)
        if status == 429:
            return UiError(_titulo(kind), _mensaje_limite_peticiones(), _detail_status(status), kind)
        if 500 <= status <= 599:
            return UiError(_titulo(kind), _mensaje_5xx(), _detail_status(status), kind)

    # --- 2. Bloqueos propios del dominio (más específicos que la red) ------
    if _buscar(cadena, SSRFBlockedError) is not None:
        return UiError(_titulo(kind), _mensaje_bloqueada(), None, kind)
    if _buscar(cadena, ResponseTooLargeError) is not None:
        return UiError(_titulo(kind), _mensaje_respuesta_grande(), None, kind)
    if _buscar(cadena, TLSValidationError) is not None:
        return UiError(_titulo(kind), _mensaje_tls(), _detalle_red(cadena, status), kind)
    if _buscar(cadena, RateLimitError) is not None:
        return UiError(_titulo(kind), _mensaje_limite_peticiones(), _detail_status(status), kind)
    if _buscar(cadena, ConnectionLimitError) is not None:
        return UiError(_titulo(kind), _mensaje_limite_peticiones(), _detail_status(status), kind)
    if _buscar(cadena, (AccountExpiredError, AccountDisabledError)) is not None:
        # La cuenta existe pero no está activa: reintentar es inútil, así que la
        # causa tiene que estar en el mensaje (§2.3).
        return UiError(_titulo(kind), _mensaje_cuenta(), _detail_status(status), kind)
    if _buscar(cadena, AuthenticationError) is not None:
        # Sin status: 401 y 403 indistinguibles, así que se dice lo que §8.1
        # dice —«credenciales o el acceso»— y no se elige por el usuario.
        return UiError(
            _titulo(kind),
            "El servidor rechazó las credenciales o el acceso.",
            _detail_status(status),
            kind,
        )
    if _buscar(cadena, UnsupportedProviderError) is not None:
        return UiError(_titulo(kind), _mensaje_proveedor(), None, kind)
    if _buscar(cadena, ParseError) is not None:
        return UiError(_titulo(kind), _mensaje_formato(), None, kind)
    if _buscar(cadena, InvalidUrlError) is not None:
        return UiError(_titulo(kind), _mensaje_url_invalida(), None, kind)
    if _buscar(cadena, InvalidSourceError) is not None:
        return UiError(_titulo(kind), _mensaje_decode(), None, kind)

    # --- 3. Red: por `reason`, no por la prosa del mensaje ------------------
    if _buscar(cadena, NetworkError) is not None:
        reason = _reason_of(cadena)
        fabrica = _POR_RAZON.get(reason or "") if reason else None
        if fabrica is not None:
            return UiError(_titulo(kind), fabrica(), _detalle_red(cadena, status), kind)
        return UiError(_titulo(kind), _mensaje_conexion(), _detalle_red(cadena, status), kind)

    # --- 4. Dominio genérico ------------------------------------------------
    if _buscar(cadena, ProviderError) is not None:
        return UiError(_titulo(kind), _mensaje_desconocido(), _detail_status(status), kind)

    # --- 5. Sin tipo del dominio: se mira la prosa de `OSError`/`ValueError` --
    #    Un `OSError` plano es justo lo que deja `fetch_bytes` (H3) cuando no
    #    conserva la causa, y un `ValueError` es un parseo local.
    return _por_texto(exc, cadena, kind)


def describe_playback_error(exc: BaseException) -> UiError:
    """Traduce un error de reproducción al mensaje de §8 (flujo PLAYBACK).

    El orden es el del §17 del SDD-M: «el protocolo funciona y la red falló» y
    «el backend no abre el protocolo» son cosas distintas y no se mezclan.

    Dos decisiones que no son obvias:

    - **La taxonomía del reproductor sólo se aplica a lo que escribió el
      reproductor.** Un :class:`NetworkError` con ``status=403`` se clasifica por
      su atributo, no buscando «403» en su prosa: si se hiciera al revés, el
      texto de `safe_http» («(HTTP 403)») haría que *toda* lista rechazada
      pareciera un fallo de reproducción, y un 404 de playlist se leería como
      «la dirección del canal ya no existe».

    - **Sin reproductor no es «falló la reproducción»** (§8.11): no se intentó
      nada. Se comprueba antes que la red porque aquí no hay red.
    """
    _cargar_mensajes_reproduccion()
    cadena = _cause_chain(exc)
    kind = ErrorKind.PLAYBACK
    status = _status_of(cadena)

    from ..player.errors import PlayerError, StreamErrorCode, classify_message

    player_exc = _buscar(cadena, PlayerError)
    if player_exc is not None:
        codigo = classify_message(str(player_exc))
        status_reproductor = _status_de_reproductor(player_exc)
        if codigo is StreamErrorCode.PLAYER_MISSING or (
            codigo is None and _RE_SIN_REPRODUCTOR.search(str(player_exc))
        ):
            return UiError(_titulo(kind), _mensaje_sin_reproductor(), None, kind)
        # El código que escribió el binario es un status, y como tal se muestra.
        status = status if status is not None else status_reproductor
        # Un 404 escrito por el reproductor sigue siendo un 404 (§8.3).
        if codigo is StreamErrorCode.NOT_FOUND and status == 404:
            return UiError(_titulo(kind), _mensaje_404(kind), _detail_status(404), kind)
        if codigo is StreamErrorCode.AUTH_FAILED and status in (401, 403):
            por_status = _mensaje_401 if status == 401 else _mensaje_403
            return UiError(
                _titulo(kind), por_status(kind), _detail_status(status), kind
            )
        if codigo is not None:
            texto = _MENSAJE_POR_CODIGO.get(codigo.value)
            if texto is not None:
                return UiError(_titulo(kind), texto, _detail_status(status), kind)

    # Sin taxonomía de reproducción: si es un error del dominio, se clasifica
    # con las mismas reglas que una playlist y sólo cambian el título y el 404.
    if any(
        isinstance(e, (NetworkError, ProviderError, ParseError, InvalidUrlError))
        for e in cadena
    ):
        err = describe_playlist_error(exc)
        if status == 404:
            return UiError(_titulo(kind), _mensaje_404(kind), _detail_status(404), kind)
        return UiError(
            _titulo(kind), err.message, _detail_status(status) or err.detail, kind
        )

    return _por_texto(exc, cadena, kind)


# ---------------------------------------------------------------------------
# Último recurso: clasificación por prosa
# ---------------------------------------------------------------------------


def _por_texto(exc: BaseException, cadena: list[BaseException], kind: ErrorKind) -> UiError:
    """Clasifica un ``OSError``/``ValueError`` que no conserva su tipo real.

    Es el camino que queda cuando la cadena de causas no aporta nada: un
    ``OSError`` con el mensaje puesto a mano y un ``ValueError`` de parseo. Se
    busca sólo lo **irreversible** —el status HTTP escrito en el texto— y el
    resto cae en §8.12: es preferible un mensaje honesto y genérico a un
    diagnóstico inventado.
    """
    texto = " ".join(str(e) for e in cadena)
    status: int | None = None
    if isinstance(exc, ValueError):
        return UiError(_titulo(kind), _mensaje_formato(), None, kind)
    match = _RE_STATUS_PROTO.search(texto)
    if match is not None:
        status = int(match.group(1))
    if status is None:
        return UiError(_titulo(kind), _mensaje_desconocido(), None, kind)
    if status == 401:
        return UiError(_titulo(kind), _mensaje_401(kind), _detail_status(status), kind)
    if status == 403:
        return UiError(_titulo(kind), _mensaje_403(kind), _detail_status(status), kind)
    if status == 404:
        return UiError(_titulo(kind), _mensaje_404(kind), _detail_status(status), kind)
    if status == 429:
        return UiError(_titulo(kind), _mensaje_limite_peticiones(), _detail_status(status), kind)
    if 500 <= status <= 599:
        return UiError(_titulo(kind), _mensaje_5xx(), _detail_status(status), kind)
    return UiError(_titulo(kind), _mensaje_desconocido(), _detail_status(status), kind)