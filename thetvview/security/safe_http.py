"""Cliente HTTP único, seguro y síncrono (SDD §9, gaps B4/B5/B14), solo stdlib.

Es el **único** punto por el que la app debe tocar la red. Sustituye a
``urllib.request.urlopen`` en los cuatro call sites y garantiza:

- **Política de URL + SSRF en cada salto**
  (:func:`thetvview.security.ssrf.check_url`): esquema, host, puerto,
  longitud, caracteres y destino real resuelto.
- **Redirects manuales** (``limits.max_redirects``, por defecto 3)
  revalidando URL, esquema y destino en *cada* salto; se rechaza el
  *downgrade* ``https → http`` (SEC-003).
- **TLS estricto**: ``ssl.create_default_context()`` con
  ``check_hostname=True`` y ``verify_mode=CERT_REQUIRED``, TLS ≥ 1.2.
- **Lectura acotada** a ``max_bytes`` (por defecto
  ``limits.max_response_bytes``) → :class:`ResponseTooLargeError`,
  incluida la salida descomprimida (bomba gzip).
- **Timeout siempre**: uno de socket por operación y un *deadline* global
  que cubre redirects, reintentos y descompresión.
- **Concurrencia limitada** con ``BoundedSemaphore(max_concurrent_requests)``
  → :class:`ConnectionLimitError` si no hay hueco; nunca una cola infinita.
- **Reintentos** en 408/429/502/503/504 con backoff 1/2/4 s y respeto de
  ``Retry-After`` (acotado), sin pasarse del *deadline*.
- **No cuelga la TUI**: toda espera va con timeout y toda excepción sale
  convertida en un error del dominio con mensaje apto para un modal.

Contrato de errores
-------------------

============================  ============================================
``InvalidUrlError``           la URL viola la política (SDD §10)
``SSRFBlockedError``          destino privado/interno/de metadata (§11)
``TLSValidationError``        certificado/host/TLS inválido o downgrade
``ResponseTooLargeError``     respuesta o descompresión sobre el límite
``ConnectionLimitError``      sin hueco en el semáforo de peticiones
``RateLimitError``            el servidor pide esperar (429)
``NetworkError``              cualquier otro fallo de red o estado HTTP
============================  ============================================

:meth:`SafeHttpClient.request` y :meth:`SafeHttpClient.head` **no** lanzan
por el código HTTP (lo devuelven para que el caller decida);
``get_bytes``/``get_text``/``get_json``/``stream`` sí.

**Residual conocido:** la validación DNS ocurre *antes* de conectar y se
repite en cada redirect, pero el socket lo abre urllib resolviendo de nuevo.
Fijar la IP (pinning) exigiría reescribir la capa de conexión; queda
documentado en SECURITY.md.
"""

from __future__ import annotations

import datetime as _dt
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, Mapping
from urllib.parse import urljoin

from .errors import (
    ConnectionLimitError,
    InvalidSourceError,
    NetworkError,
    ProviderError,
    RateLimitError,
    ResponseTooLargeError,
    TLSValidationError,
)
from .limits import get_limits, human_size
from .redaction import redact_text
from .ssrf import check_url
from .url_policy import PURPOSE_METADATA, UrlParts

__all__ = [
    "DEFAULT_USER_AGENT",
    "RETRYABLE_STATUSES",
    "BACKOFF_SECONDS",
    "MAX_RETRY_WAIT",
    "SafeResponse",
    "SafeHttpClient",
    "get_bytes",
    "get_text",
    "get_json",
    "head",
]

DEFAULT_USER_AGENT: str = "theTVVIEW/1.0"

#: Códigos reintentables (SDD §41).
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429, 502, 503, 504})

#: Backoff progresivo entre reintentos (SDD §41).
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

#: Techo de una espera por ``Retry-After``: no bloqueamos la TUI minutos.
MAX_RETRY_WAIT: float = 10.0

_CHUNK: int = 64 * 1024
_GZIP_ENCODINGS = frozenset({"gzip", "x-gzip"})
_PLAIN_ENCODINGS = frozenset({"identity", ""})

_SSL_CTX: ssl.SSLContext | None = None
_SSL_LOCK = threading.Lock()

_SEM: threading.BoundedSemaphore | None = None
_SEM_SIZE: int = 0
_SEM_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Infraestructura compartida
# ---------------------------------------------------------------------------


def _ssl_context() -> ssl.SSLContext:
    """Contexto TLS estricto y compartido (CA del sistema, host, ≥1.2)."""
    global _SSL_CTX
    if _SSL_CTX is None:
        with _SSL_LOCK:
            if _SSL_CTX is None:
                ctx = ssl.create_default_context()
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
                try:
                    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                except (AttributeError, ValueError):  # pragma: no cover
                    pass
                _SSL_CTX = ctx
    return _SSL_CTX


def _semaphore() -> threading.BoundedSemaphore:
    """Semáforo global, re-creado si cambia ``max_concurrent_requests``."""
    global _SEM, _SEM_SIZE
    size = max(1, int(get_limits().max_concurrent_requests))
    with _SEM_LOCK:
        if _SEM is None or _SEM_SIZE != size:
            _SEM = threading.BoundedSemaphore(size)
            _SEM_SIZE = size
        return _SEM


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Convierte cualquier 3xx en ``HTTPError`` para seguirlo a mano."""

    def redirect_request(  # type: ignore[override]
        self, req, fp, code, msg, headers, newurl
    ):
        return None


def _headers_of(message: Any) -> dict[str, str]:
    """Cabeceras en minúsculas (``Location`` → ``location``)."""
    if message is None:
        return {}
    try:
        items = message.items()
    except AttributeError:  # pragma: no cover - ya es un mapping
        items = list(message)
    return {str(k).lower(): str(v) for k, v in items}


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _close(fp: Any) -> None:
    try:
        fp.close()
    except Exception:  # noqa: BLE001 - socket ya cerrado
        pass


def _con_reason(exc: NetworkError, reason: str | None) -> NetworkError:
    """Fija ``.reason`` (F1-bis) y devuelve el error, para encadenar la construcción.

    El texto **no** cambia: el motivo de red va en un atributo aparte para que la
    UI clasifique sin leer prosa (§8.5–§8.7), pero todos los mensajes de red
    siguen byte a byte los de siempre y los tests de seguridad no se enteran.
    """
    exc.reason = reason
    return exc


def _map_network_error(exc: BaseException, timeout: float) -> NetworkError:
    """Convierte un error de urllib/ssl/socket en un error de dominio amable.

    Clasifica por **tipo** del motivo y fija ``.reason`` con el vocabulario
    cerrado de :class:`~thetvview.security.errors.NetworkError`. Es lo que
    permite que la presentación del error distinga «no resuelve el nombre» de
    «no hay ruta» sin hacer *string matching* sobre la frase.
    """
    reason: Any = getattr(exc, "reason", None)
    if reason is None:
        reason = exc

    if isinstance(reason, ssl.SSLCertVerificationError):
        return _con_reason(
            TLSValidationError(
                "La conexión no es segura: el certificado del servidor no es de "
                "confianza o no corresponde al dominio. Revisa la URL o el sistema."
            ),
            "tls",
        )
    if isinstance(reason, ssl.SSLError):
        return _con_reason(
            TLSValidationError(
                "La conexión segura (TLS) falló con el servidor: "
                + redact_text(_short(str(reason)))
            ),
            "tls",
        )
    if isinstance(reason, socket.gaierror):
        return _con_reason(
            NetworkError(
                "No se pudo encontrar el servidor: el nombre no resuelve en DNS."
            ),
            "dns",
        )
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return _con_reason(
            NetworkError(f"Tiempo de espera agotado ({timeout:g}s)."), "timeout"
        )
    if isinstance(reason, ConnectionRefusedError):
        return _con_reason(NetworkError("El servidor rechazó la conexión."), "refused")
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in (101, 51, 65):
        return _con_reason(
            NetworkError("No hay ruta hasta el servidor (red no disponible)."),
            "unreachable",
        )

    text = str(reason) if str(reason) else str(exc)
    text = text.replace("<urlopen error ", "").strip(" []")
    if not text:
        text = "error de red"
    if len(text) > 90:
        text = text[:87] + "..."
    return _con_reason(
        NetworkError("No se pudo conectar con el servidor: " + redact_text(text)),
        "connection",
    )


def _check_deadline(deadline: float, budget: float) -> None:
    if time.monotonic() > deadline:
        # Es un timeout del presupuesto global, no del socket: el mismo `reason`
        # porque para el usuario es la misma cosa («tardó demasiado»), y así el
        # presentador no necesita distinguir dos relojes.
        raise _con_reason(
            NetworkError(
                f"La descarga ha superado el tiempo máximo permitido ({budget:g}s)."
            ),
            "timeout",
        )


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _retry_delay(
    headers: Mapping[str, str], attempt: int, deadline: float
) -> float | None:
    """Espera antes del siguiente reintento, o None si no cabe en el deadline."""
    if attempt < len(BACKOFF_SECONDS):
        wait = BACKOFF_SECONDS[attempt]
    else:
        wait = MAX_RETRY_WAIT
    raw = (headers.get("retry-after") or "").strip()
    if raw:
        try:
            wait = max(wait, float(raw))
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_dt.timezone.utc)
                wait = max(wait, (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
    wait = min(max(0.0, wait), MAX_RETRY_WAIT)
    return None if time.monotonic() + wait >= deadline else wait


def _too_large(max_bytes: int) -> ResponseTooLargeError:
    return ResponseTooLargeError(
        f"El servidor respondió más de {human_size(int(max_bytes))} y se "
        "corta la descarga por seguridad."
    )


def _status_message(status: int, reason: str) -> str:
    if status in (401, 403):
        return f"El servidor denegó el acceso (HTTP {status})."
    if status == 404:
        return "El servidor no encontró el recurso solicitado (HTTP 404)."
    if status == 429:
        return "Demasiadas solicitudes: el servidor pide esperar (HTTP 429)."
    if status >= 500:
        return f"El servidor está fallando ahora mismo (HTTP {status})."
    detail = _short(reason) if reason else ""
    return f"El servidor respondió «{status}»" + (f" {detail}." if detail else ".")


def _raise_for_status(resp: "SafeResponse") -> None:
    if 200 <= resp.status < 300:
        return
    if resp.status == 429:
        retry = (resp.headers.get("retry-after") or "").strip()
        hint = f" Espera {retry} s." if retry.isdigit() else ""
        raise _with_status(
            RateLimitError(
                "Demasiadas solicitudes. El servidor pide esperar un momento." + hint
            ),
            resp.status,
        )
    raise _with_status(NetworkError(_status_message(resp.status, resp.reason)), resp.status)


def _with_status(exc: ProviderError, status: int) -> ProviderError:
    """Fija ``.status`` (F1-bis) y devuelve el error.

    La prosa de :func:`_status_message` no cambia —«(HTTP 403)» sigue dentro—:
    lo que se añade es el **dato**, para que quien presente el error pueda
    poner «Código HTTP: 403» como detalle secundario sin tener que parsear la
    frase (§8, §9). Los tests que asertan el texto siguen viendo lo mismo.

    El parámetro es un :class:`ProviderError` y no un :class:`NetworkError` a
    propósito: ``RateLimitError`` es un 429 del servidor y **no** hereda de
    ``NetworkError``, así que escribir el tipo concreto obligaría a un ``type:
    ignore`` que escondería justo el error que se quiere atrapar. Como
    ``status`` está declarado en :class:`ProviderError`, ambos lo tienen y el
    presentador busca el atributo, no la clase base.
    """
    exc.status = int(status)
    return exc


def _loads_json(raw: bytes) -> "dict[str, Any] | list[Any]":
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidSourceError(
            "La respuesta del servidor no es JSON válido."
        ) from exc
    if not isinstance(data, (dict, list)):
        raise InvalidSourceError("La respuesta del servidor no es JSON válido.")
    return data


# ---------------------------------------------------------------------------
# Respuesta / conexión abierta
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafeResponse:
    """Respuesta ya validada, acotada y con las cabeceras normalizadas."""

    url: str
    final_url: str
    status: int
    reason: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    redirects: int = 0
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        charset = "utf-8-sig"
        content_type = str(self.headers.get("content-type", ""))
        if "charset=" in content_type.lower():
            charset = (
                content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or charset
            )
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:  # pragma: no cover - charset raro
            return self.body.decode("utf-8", errors="replace")


@dataclass
class _Opened:
    """Respuesta HTTP abierta (o un ``HTTPError`` con cuerpo) ya validada."""

    fp: Any
    status: int
    reason: str
    headers: Mapping[str, str]
    final_url: str
    redirects: int = 0


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


class SafeHttpClient:
    """Cliente HTTP seguro. Una instancia por destino es muy barata."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        timeout: float | None = None,
        max_bytes: int | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        purpose: str = PURPOSE_METADATA,
        max_redirects: int | None = None,
        retries: int | None = None,
        headers: Mapping[str, str] | None = None,
        accept_gzip: bool = True,
    ) -> None:
        """
        Args:
            allow_private: excepción por fuente para red privada/loopback.
                Nunca es global: cada fuente lo pide y se persiste en
                ``playlists.json``.
            timeout: presupuesto total en segundos (``limits.total_timeout``).
            max_bytes: tope de respuesta (``limits.max_response_bytes``).
            purpose: ``"metadata"`` o ``"stream"`` (política de esquema).
            max_redirects: tope de saltos (``limits.max_redirects``).
            retries: reintentos sobre 408/429/5xx (``len(BACKOFF_SECONDS)``).
            accept_gzip: pedir y aceptar ``Content-Encoding: gzip``.
        """
        self.allow_private = bool(allow_private)
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.purpose = purpose
        self.max_redirects = max_redirects
        self.retries = retries
        self.headers: dict[str, str] = dict(headers or {})
        self.accept_gzip = bool(accept_gzip)
        self._opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        )

    # -- resolución de límites ------------------------------------------------

    def _budget(self, timeout: float | None) -> float:
        if timeout is not None:
            return float(timeout)
        if self.timeout is not None:
            return float(self.timeout)
        return float(get_limits().total_timeout)

    def _cap(self, max_bytes: int | None) -> int | None:
        if max_bytes is not None:
            return int(max_bytes)
        if self.max_bytes is not None:
            return int(self.max_bytes)
        return int(get_limits().max_response_bytes)

    def _redirect_cap(self) -> int:
        if self.max_redirects is not None:
            return max(0, int(self.max_redirects))
        return int(get_limits().max_redirects)

    def _retry_cap(self) -> int:
        if self.retries is not None:
            return max(0, int(self.retries))
        return len(BACKOFF_SECONDS)

    def _base_headers(self) -> dict[str, str]:
        out = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Connection": "close",
            "Accept-Encoding": "gzip" if self.accept_gzip else "identity",
        }
        out.update(self.headers)
        return out

    # -- API pública ----------------------------------------------------------

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        timeout: float | None = None,
        retry: bool = True,
        body: bytes | None = None,
        read_body: bool = True,
    ) -> SafeResponse:
        """Petición validada con redirects revalidados y lectura acotada.

        **No** lanza por el código HTTP: el status queda en
        :attr:`SafeResponse.status` para que el caller lo interprete.

        ``read_body=False`` abre, valida y cierra **sin leer** el cuerpo:
        útil para sondear el status de un stream en vivo que nunca termina
        de enviarse (la sonda de salud de canal no quiere descargar nada).
        """
        cap = self._cap(max_bytes)
        started = time.monotonic()
        opened = self._open_validated(
            url,
            method=method,
            headers=headers,
            timeout=timeout,
            retry=retry,
            body=body,
        )
        fp = opened.fp
        try:
            if method.upper() == "HEAD" or not read_body:
                payload = b""
            else:
                payload = self._read_body(fp, opened.headers, cap, started, timeout)
            reason = opened.reason
        finally:
            _close(fp)
        return SafeResponse(
            url=url,
            final_url=opened.final_url,
            status=opened.status,
            reason=reason,
            headers=opened.headers,
            body=payload,
            redirects=opened.redirects,
            elapsed=time.monotonic() - started,
        )

    def head(self, url: str, **kwargs: Any) -> SafeResponse:
        """``HEAD``: no lee cuerpo y no lanza por el código HTTP."""
        kwargs.pop("method", None)
        return self.request(url, method="HEAD", **kwargs)

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> bytes:
        """``GET`` con código 2xx obligatorio; devuelve el cuerpo acotado."""
        resp = self.request(
            url, headers=headers, max_bytes=max_bytes, timeout=timeout, retry=retry
        )
        _raise_for_status(resp)
        return resp.body

    def get_text(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> str:
        """Igual que :meth:`get_bytes`, decodificado según el ``charset``."""
        resp = self.request(
            url, headers=headers, max_bytes=max_bytes, timeout=timeout, retry=retry
        )
        _raise_for_status(resp)
        return resp.text

    def get_json(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> "dict[str, Any] | list[Any]":
        """``GET`` + ``json.loads`` con mensaje amable si no es JSON."""
        resp = self.request(
            url, headers=headers, max_bytes=max_bytes, timeout=timeout, retry=retry
        )
        _raise_for_status(resp)
        return _loads_json(resp.body)

    def stream(
        self,
        url: str,
        *,
        offset: int = 0,
        length: int | None = None,
        chunk_size: int = _CHUNK,
        max_bytes: int | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> Iterator[bytes]:
        """Itera el cuerpo por trozos con el límite de tamaño activo.

        ``offset``/``length`` piden un rango (``Range``); si el servidor lo
        ignora, el límite de tamaño sigue aplicándose.
        """
        cap = self._cap(max_bytes)
        extra = dict(headers or {})
        if offset or length is not None:
            end = "" if length is None else str(offset + max(1, length) - 1)
            extra["Range"] = f"bytes={offset}-{end}"
        started = time.monotonic()
        opened = self._open_validated(
            url, method="GET", headers=extra, timeout=timeout, retry=retry
        )
        if not (200 <= opened.status < 300):
            status, reason = opened.status, opened.reason
            _close(opened.fp)
            raise _with_status(NetworkError(_status_message(status, reason)), status)
        try:
            yield from _iter_body(opened.fp, opened.headers, cap, started, timeout, chunk_size)
        finally:
            _close(opened.fp)

    # -- núcleo ---------------------------------------------------------------

    def _open_validated(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str] | None,
        timeout: float | None,
        retry: bool,
        body: bytes | None = None,
    ) -> _Opened:
        budget = self._budget(timeout)
        deadline = time.monotonic() + budget
        sock_timeout = max(0.1, min(budget, float(get_limits().read_timeout)))
        redirect_cap = self._redirect_cap()
        retry_cap = self._retry_cap() if retry else 0

        base = self._base_headers()
        if headers:
            base.update(headers)

        current = url
        redirects = 0
        attempts = 0
        was_https = False

        sem = _semaphore()
        if not sem.acquire(timeout=max(0.1, float(get_limits().connect_timeout))):
            raise ConnectionLimitError(
                "Hay demasiadas descargas en marcha; espera un momento y "
                "vuelve a intentarlo."
            )
        try:
            while True:
                parts = check_url(
                    current,
                    purpose=self.purpose,
                    allow_private=self.allow_private,
                    resolve=True,
                )
                if redirects > 0 and was_https and parts.scheme == "http":
                    raise TLSValidationError(
                        "El servidor intentó cambiar una conexión segura (https) "
                        "a una insegura (http); se bloquea la petición."
                    )
                was_https = parts.scheme == "https"

                opened = self._attempt(
                    parts, base, method, sock_timeout, deadline, budget, body
                )
                code = opened.status

                if 300 <= code < 400:
                    location = opened.headers.get("location")
                    _close(opened.fp)
                    if not location:
                        raise NetworkError(
                            f"El servidor respondió una redirección sin destino "
                            f"(HTTP {code})."
                        )
                    redirects += 1
                    if redirects > redirect_cap:
                        raise NetworkError(
                            f"El servidor hizo demasiadas redirecciones "
                            f"(más de {redirect_cap}); se corta la petición."
                        )
                    current = urljoin(opened.final_url, location)
                    continue

                if code in RETRYABLE_STATUSES and attempts < retry_cap:
                    delay = _retry_delay(opened.headers, attempts, deadline)
                    if delay is not None:
                        attempts += 1
                        _close(opened.fp)
                        time.sleep(delay)
                        continue

                opened.redirects = redirects
                return opened
        finally:
            sem.release()

    def _attempt(
        self,
        parts: UrlParts,
        base: Mapping[str, str],
        method: str,
        sock_timeout: float,
        deadline: float,
        budget: float,
        body: bytes | None,
    ) -> _Opened:
        """Una sola conexión a un destino ya validado."""
        _check_deadline(deadline, budget)
        req = urllib.request.Request(
            parts.normalized, data=body, headers=dict(base), method=method
        )
        try:
            fp = self._opener.open(req, timeout=sock_timeout)
        except urllib.error.HTTPError as exc:
            return _Opened(
                fp=exc,
                status=int(exc.code),
                reason=str(exc.reason or ""),
                headers=_headers_of(exc.headers),
                final_url=parts.normalized,
            )
        except urllib.error.URLError as exc:
            raise _map_network_error(exc, sock_timeout) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise _con_reason(
                NetworkError(f"Tiempo de espera agotado ({sock_timeout:g}s)."),
                "timeout",
            ) from exc
        except ssl.SSLError as exc:
            raise _map_network_error(exc, sock_timeout) from exc
        except OSError as exc:
            raise _map_network_error(exc, sock_timeout) from exc

        status = getattr(fp, "status", None)
        if status is None:  # pragma: no cover - http.client siempre lo trae
            try:
                status = fp.getcode()
            except Exception:  # noqa: BLE001
                status = 200
        return _Opened(
            fp=fp,
            status=int(status or 200),
            reason=str(getattr(fp, "reason", "") or ""),
            headers=_headers_of(getattr(fp, "headers", None)),
            final_url=parts.normalized,
        )

    def _read_body(
        self,
        fp: Any,
        headers: Mapping[str, str],
        max_bytes: int | None,
        started: float,
        timeout: float | None,
    ) -> bytes:
        budget = self._budget(timeout)
        declared = _content_length(headers)
        if max_bytes is not None and declared is not None and declared > max_bytes:
            raise _too_large(max_bytes)
        return _consume(fp, headers, max_bytes, started + budget, budget)


# ---------------------------------------------------------------------------
# Lectura del cuerpo
# ---------------------------------------------------------------------------


def _consume(
    fp: Any,
    headers: Mapping[str, str],
    max_bytes: int | None,
    deadline: float,
    budget: float,
) -> bytes:
    encoding = (headers.get("content-encoding") or "").lower().strip()
    gz = None
    if encoding in _GZIP_ENCODINGS:
        gz = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding not in _PLAIN_ENCODINGS:
        raise NetworkError(
            f"El servidor usó una codificación no admitida ({encoding})."
        )

    out = bytearray()
    try:
        while True:
            _check_deadline(deadline, budget)
            piece = fp.read(_CHUNK)
            if not piece:
                break
            if gz is not None:
                room = None
                if max_bytes is not None:
                    room = max_bytes + 1 - len(out)
                    if room <= 0:
                        raise _too_large(max_bytes)
                    out += gz.decompress(piece, room)
                else:
                    out += gz.decompress(piece)
            else:
                out += piece
            if max_bytes is not None and len(out) > max_bytes:
                raise _too_large(max_bytes)
        if gz is not None:
            if not gz.eof:
                raise NetworkError(
                    "El servidor envió una respuesta comprimida incompleta."
                )
            out += gz.flush()
    except NetworkError:
        raise
    except zlib.error as exc:
        raise NetworkError(
            "El servidor envió una respuesta comprimida dañada."
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise _con_reason(
            NetworkError(f"Tiempo de espera agotado ({budget:g}s)."), "timeout"
        ) from exc
    except OSError as exc:
        raise _map_network_error(exc, budget) from exc

    if max_bytes is not None and len(out) > max_bytes:
        raise _too_large(max_bytes)
    return bytes(out)


def _iter_body(
    fp: Any,
    headers: Mapping[str, str],
    max_bytes: int | None,
    started: float,
    timeout: float | None,
    chunk_size: int = _CHUNK,
) -> Iterator[bytes]:
    budget = get_limits().total_timeout if timeout is None else float(timeout)
    deadline = started + budget
    encoding = (headers.get("content-encoding") or "").lower().strip()
    gz = (
        zlib.decompressobj(16 + zlib.MAX_WBITS)
        if encoding in _GZIP_ENCODINGS
        else None
    )
    total = 0
    while True:
        _check_deadline(deadline, budget)
        piece = fp.read(chunk_size)
        if not piece:
            break
        if gz is not None:
            piece = gz.decompress(piece)
        total += len(piece)
        if max_bytes is not None and total > max_bytes:
            raise _too_large(max_bytes)
        if piece:
            yield piece
    if gz is not None and not gz.eof:
        raise NetworkError("El servidor envió una respuesta comprimida incompleta.")


# ---------------------------------------------------------------------------
# Atajos de módulo (cliente por defecto: SIN excepción de red privada)
# ---------------------------------------------------------------------------

_default_client = SafeHttpClient()


def get_bytes(url: str, **kwargs: Any) -> bytes:
    """GET acotado con el cliente por defecto (bloquea red privada)."""
    return _default_client.get_bytes(url, **kwargs)


def get_text(url: str, **kwargs: Any) -> str:
    """GET a texto con el cliente por defecto."""
    return _default_client.get_text(url, **kwargs)


def get_json(url: str, **kwargs: Any) -> "dict[str, Any] | list[Any]":
    """GET + JSON con el cliente por defecto."""
    return _default_client.get_json(url, **kwargs)


def head(url: str, **kwargs: Any) -> SafeResponse:
    """HEAD con el cliente por defecto (no lanza por el código HTTP)."""
    return _default_client.head(url, **kwargs)
