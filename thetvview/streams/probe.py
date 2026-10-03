"""Descubrimiento de pistas con red: bajar el manifiesto y analizarlo.

Es el **único** módulo de :mod:`thetvview.streams` que abre un socket, y lo
hace por :mod:`thetvview.security.safe_http` (H4): nunca ``urllib`` a pelo,
siempre política de URL, anti-SSRF, redirecciones revalidadas, TLS estricto,
timeout y lectura acotada.

Filosofía (plan F2):

- **Nunca bloquea la TUI.** El sondeo corre en un hilo daemon y entrega el
  resultado por callback; si no ha terminado, el usuario ve el canal igual.
- **Nunca propaga excepciones.** Cualquier fallo —403, 404, timeout, HTML de
  error, XML hostil— sale como ``capabilities=None`` **con un motivo ya
  redactado**, que la UI explica en un modal (§29, AC-12).
- **No materializa un directo.** Antes de descargar nada se hace un ``HEAD``;
  si la respuesta no parece un manifiesto, se lee un rango pequeño acotado
  (patrón ya probado en :mod:`thetvview.channel_health`).
- **La excepción anti-SSRF la hereda la fuente** que aportó el canal
  (``allow_private``), nunca es global (H8, SDD §11).
- **Caché en dos niveles**: memoria 60 s (los masters en vivo cambian) y disco
  15 min usada **sólo como respaldo** si la red falla (§36).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import config
from ..models import Channel
from ..security.local_files import chmod_private
from ..security.redaction import redact_text
from ..security.errors import ResponseTooLargeError
from ..security.safe_http import SafeHttpClient
from ..security.url_policy import PURPOSE_METADATA
from ..tracks.models import (
    DEGRADED_NOT_EXPOSED,
    DEGRADED_PROBE_FAILED,
    DEGRADED_PROTOCOL,
    DEGRADED_UNKNOWN,
    MediaCapabilities,
    Protocol,
)
from .dash import parse_dash
from .detector import detect_protocol
from .headers import build_headers, strip_ffmpeg
from .hls import HlsParseError, parse_hls

__all__ = [
    "ProbeResult",
    "TrackProbe",
    "probe_capabilities",
    "MAX_MANIFEST_BYTES",
    "MAX_MPD_BYTES",
    "RANGE_PROBE_BYTES",
    "MEMORY_TTL",
    "DISK_TTL",
    "TRACK_CACHE_DIR",
]

#: Topes de descarga: un master HLS son unos pocos KiB; un MPD, algunos
#: cientos de KiB. Los dos topes son\fijo de seguridad, no de comfort.
MAX_MANIFEST_BYTES: int = 2 * 1024 * 1024
MAX_MPD_BYTES: int = 8 * 1024 * 1024
#: Cuánto se lee cuando la respuesta **no** parece un manifiesto (un directo,
#: un 403 con HTML de error, un servidor que ignora el tipo). Acotado para no
#: materializar un stream infinito.
RANGE_PROBE_BYTES: int = 128 * 1024

#: TTL de la caché en memoria. Corto a propósito: un master en vivo cambia.
MEMORY_TTL: float = 60.0
#: TTL de la caché en disco. Sólo se usa si la red falla.
DISK_TTL: float = 15 * 60.0

#: Tope de tiempo: es una mejora opcional y nunca justifica esperar.
CONNECT_TIMEOUT: float = 6.0
TOTAL_TIMEOUT: float = 8.0

#: Caché en disco. Vive bajo ``data/`` con permisos privados (SDD §21).
TRACK_CACHE_DIR: Path = config.TRACK_CACHE_DIR

#: Códigos con los que un ``HEAD`` no dice nada útil y hay que reintentar
#: con un ``GET`` (muchos servidores IPTV responden así; es el mismo motivo
#: por el que la sonda de salud hace dos pasos, patrón H5).
_HEAD_UNUSABLE: frozenset[int] = frozenset({400, 403, 405, 501})

_MIME_MANIFEST_HINTS: tuple[str, ...] = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/dash+xml",
    "video/vnd.mpeg.dash.mpd",
    "text/plain",
    "text/html",
    "application/octet-stream",
)

_MEMORY_LOCK = threading.Lock()
_MEMORY: dict[str, tuple[float, MediaCapabilities]] = {}


@dataclass(frozen=True)
class ProbeResult:
    """Resultado del sondeo. ``capabilities is None`` + ``reason`` = degradado."""

    capabilities: MediaCapabilities | None = None
    #: Motivo corto, redactado y apto para un modal. Vacío si todo fue bien.
    reason: str = ""
    #: True si las capacidades salieron de la caché (en memoria o en disco).
    from_cache: bool = False
    #: Código HTTP, si se llegó a tener uno (útil para "403" vs "timeout").
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.capabilities is not None

    @property
    def failed(self) -> bool:
        return self.capabilities is None


def probe_capabilities(
    channel: Channel,
    *,
    allow_private: bool = False,
    timeout: float = TOTAL_TIMEOUT,
    use_cache: bool = True,
    cache_dir: Path | None = TRACK_CACHE_DIR,
) -> ProbeResult:
    """Descarga el manifiesto de `channel` y devuelve sus capacidades.

    Bloqueante; para uso en hilo. **Nunca lanza**: todo fallo sale como
    ``ProbeResult(capabilities=None, reason=<texto redactado>)``.

    Args:
        channel: canal a analizar. Se usa su URL y sus ``EXTVLCOPT``.
        allow_private: excepción anti-SSRF **de la fuente** de este canal
            (SDD §11). Nunca se pone a True por mouthful.
        timeout: presupuesto total en segundos.
        use_cache: si False, salta las cachés (tests y "re-sondar").
        cache_dir: dónde está la caché en disco (tests apuntan a un temporal).
    """
    url = strip_ffmpeg(getattr(channel, "url", "") or "")
    if not url:
        return ProbeResult(reason="El canal no tiene dirección.")
    if not url.lower().startswith(("http://", "https://")):
        # No es HTTP: no hay manifiesto que pedir y no es un error.
        return ProbeResult(reason="Este canal no es HTTP: no hay manifiesto.")

    if use_cache:
        cached = _memory_get(url)
        if cached is not None:
            return ProbeResult(capabilities=cached, from_cache=True)

    headers = build_headers(channel)
    try:
        body, final_url, content_type, status = _download(
            url, headers, allow_private=allow_private, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 - el sondeo nunca rompe la TUI
        reason = redact_text(_short(str(exc)))
        fallback = _disk_get(url, cache_dir) if use_cache else None
        if fallback is not None:
            return ProbeResult(capabilities=fallback, from_cache=True)
        return ProbeResult(
            reason=reason or "No se pudo consultar el manifiesto.",
            http_status=None,
        )

    if status >= 400:
        fallback = _disk_get(url, cache_dir) if use_cache else None
        if fallback is not None:
            return ProbeResult(capabilities=fallback, from_cache=True, http_status=status)
        return ProbeResult(reason=_http_reason(status), http_status=status)

    detection = detect_protocol(body, url=final_url, content_type=content_type)

    if not detection.is_selectable:
        # No es un error: un .ts o un directo no tiene pistas que elegir y
        # la reproducción sigue igual (SDD §32/§48).
        reason = _degraded_reason_for(detection.protocol, detection.reason)
        caps = MediaCapabilities(
            protocol=detection.protocol,
            manifest_url=redact_text(final_url),
            final_url=final_url,
            live=True,
            degraded_reason=reason,
            note=detection.reason,
        )
        return ProbeResult(capabilities=caps, http_status=status)

    cap = MAX_MPD_BYTES if detection.is_dash else MAX_MANIFEST_BYTES
    try:
        capabilities = _parse(body, detection, final_url)
    except Exception as exc:  # noqa: BLE001
        fallback = _disk_get(url, cache_dir) if use_cache else None
        if fallback is not None:
            return ProbeResult(capabilities=fallback, from_cache=True, http_status=status)
        return ProbeResult(
            reason=redact_text(_short(str(exc))) or "El manifiesto no se pudo leer.",
            http_status=status,
        )

    capabilities.manifest_url = redact_text(final_url)
    capabilities.final_url = final_url
    if use_cache:
        _memory_put(url, capabilities)
        _disk_put(url, capabilities, cache_dir, cap)
    return ProbeResult(capabilities=capabilities, http_status=status)


# ---------------------------------------------------------------------------
# Descarga
# ---------------------------------------------------------------------------


def _download(
    url: str,
    headers: Mapping[str, str],
    *,
    allow_private: bool,
    timeout: float,
) -> tuple[bytes, str, str, int]:
    """Descarga el cuerpo del manifiesto acotando lo que sea un directo.

    Estrategia: ``HEAD`` primero (no lee nada) para decidir si lo que hay
    detrás es un manifiesto; sólo si lo parece se pide el cuerpo entero. Si
    el HEAD no sirve (403/405… muy habitual en IPTV) se cae a un ``GET`` con
    ``Range`` acotado, que además detecta el protocolo sin tragarse un
    directo entero.
    """
    client = SafeHttpClient(
        allow_private=allow_private,
        purpose=PURPOSE_METADATA,
        retries=0,
        max_bytes=MAX_MANIFEST_BYTES,
        accept_gzip=False,
    )

    head: Any = None
    try:
        head = client.head(url, headers=headers, timeout=min(CONNECT_TIMEOUT, timeout))
    except Exception:
        head = None

    head_status = int(head.status) if head is not None else 0
    if head is not None and head_status >= 400 and head_status not in _HEAD_UNUSABLE:
        # El HEAD dio una respuesta definitiva (404, 401, 403 de verdad):
        # no insisto, que el error hay que explicarlo.
        return b"", head.final_url, str(head.headers.get("content-type", "")), head_status

    if head is not None and head_status < 400:
        content_type = str(head.headers.get("content-type", ""))
        declared = _content_length(head.headers)
        if _looks_like_manifest(content_type) or (
            declared is not None and declared <= MAX_MANIFEST_BYTES
        ):
            resp = client.request(
                url, headers=headers, timeout=timeout, max_bytes=MAX_MANIFEST_BYTES
            )
            return (
                resp.body,
                resp.final_url,
                str(resp.headers.get("content-type", "")),
                resp.status,
            )

    ranged = dict(headers)
    ranged["Range"] = f"bytes=0-{RANGE_PROBE_BYTES - 1}"
    try:
        resp = client.request(
            url,
            headers=ranged,
            timeout=timeout,
            max_bytes=RANGE_PROBE_BYTES,
        )
    except ResponseTooLargeError:
        # El servidor hizo caso omiso del Range y está soltando un directo
        # sin fin. No se materializa: se leen los primeros bytes y se
        # decide con eso (patrón H5, ya probado en channel_health).
        prefix, final_url, ctype = _read_prefix(client, url, headers, timeout)
        return prefix, final_url, ctype, 200
    return resp.body, resp.final_url, str(resp.headers.get("content-type", "")), resp.status


def _read_prefix(
    client: SafeHttpClient,
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[bytes, str, str]:
    """Lee sólo los primeros :data:`RANGE_PROBE_BYTES` y para.

    Se usa cuando el servidor ignora el ``Range``: los bytes ya leidos bastan
    para detectar el protocolo y no hace falta seguir consumiendo un
    stream infinito.
    """
    ranged = dict(headers)
    ranged["Range"] = f"bytes=0-{RANGE_PROBE_BYTES - 1}"
    out = bytearray()
    iterator = client.stream(
        url, headers=ranged, timeout=timeout, chunk_size=16 * 1024
    )
    try:
        for piece in iterator:
            out += piece
            if len(out) >= RANGE_PROBE_BYTES:
                break
    except ResponseTooLargeError:
        pass  # ya hay suficiente para decidir
    finally:
        iterator.close()
    return bytes(out[:RANGE_PROBE_BYTES]), url, ""


def _parse(body: bytes, detection, final_url: str) -> MediaCapabilities:
    """Convierte el cuerpo en capacidades según lo detectado."""
    text = _decode(body)
    if detection.is_dash:
        return parse_dash(text, final_url=final_url)
    if detection.is_hls:
        return parse_hls(text, final_url=final_url)
    raise HlsParseError("El contenido no es un manifiesto de pistas.")


def _decode(body: bytes) -> str:
    for charset in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return body.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _looks_like_manifest(content_type: str) -> bool:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not mime:
        # Sin Content-Type no hay pista: se asume manifiesto y deja que el
        # detector juzgue por el cuerpo.
        return True
    return mime in _MIME_MANIFEST_HINTS


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _degraded_reason_for(protocol: str, reason: str) -> str:
    if protocol == Protocol.MPEGTS.value:
        return DEGRADED_PROTOCOL
    if protocol == Protocol.UNKNOWN.value:
        return DEGRADED_UNKNOWN
    return DEGRADED_NOT_EXPOSED


def _http_reason(status: int) -> str:
    if status == 403:
        return (
            "El proveedor respondió 403 al pedir el manifiesto. Suele faltar "
            "la cabecera Referer o la línea está bloqueada."
        )
    if status == 404:
        return "El manifiesto no existe (404): el proveedor no lo publica."
    if status == 401:
        return "El proveedor respondió 401: la línea necesita credenciales."
    return f"El proveedor respondió HTTP {status} al pedir el manifiesto."


def _short(text: str, limit: int = 140) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Caché en memoria (TTL corto)
# ---------------------------------------------------------------------------


def _memory_get(url: str) -> MediaCapabilities | None:
    now = time.monotonic()
    with _MEMORY_LOCK:
        entry = _MEMORY.get(url)
        if entry is None:
            return None
        stamp, caps = entry
        if now - stamp > MEMORY_TTL:
            _MEMORY.pop(url, None)
            return None
        return caps


def _memory_put(url: str, caps: MediaCapabilities) -> None:
    with _MEMORY_LOCK:
        if len(_MEMORY) > 64:
            _MEMORY.clear()
        _MEMORY[url] = (time.monotonic(), caps)


def clear_memory_cache() -> None:
    """Vacía la caché en memoria (tests)."""
    with _MEMORY_LOCK:
        _MEMORY.clear()


# ---------------------------------------------------------------------------
# Caché en disco (TTL largo, sólo respaldo)
# ---------------------------------------------------------------------------

_SALT_FILE = ".salt"
_salt_lock = threading.Lock()
_SALT: bytes | None = None


def _salt() -> bytes:
    """Sal por instalación para el nombre del fichero de caché.

    El nombre es ``sha256(sal + URL)``: sin sal, un hash de una URL con
    credenciales sería atacable por diccionario (H7). Con sal, además de
    no filtrar nada, dos canales con rutas que sólo difieren en usuario y
    contraseña no colisionan en el mismo fichero.
    """
    global _SALT
    with _salt_lock:
        if _SALT is not None:
            return _SALT
        try:
            TRACK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            chmod_private(TRACK_CACHE_DIR, directory=True)
            path = TRACK_CACHE_DIR / _SALT_FILE
            if path.exists():
                data = path.read_bytes().strip()
                if len(data) >= 16:
                    _SALT = data
                    return _SALT
            data = os.urandom(32)
            path.write_bytes(data)
            chmod_private(path)
            _SALT = data
            return _SALT
        except OSError:
            # Sin sal (sistema de ficheros rarísimo): se usa un valor fijo.
            # Sigue sin escribirse la URL, que es lo importante.
            _SALT = b"thetvview-no-salt"
            return _SALT


def cache_key(url: str) -> str:
    """Nombre del fichero de caché para `url` (no revela nada)."""
    digest = hashlib.sha256(_salt() + url.encode("utf-8", "replace")).hexdigest()
    return f"{digest}.json"


def _cache_path(url: str, cache_dir: Path | None) -> Path | None:
    if cache_dir is None:
        return None
    try:
        return Path(cache_dir) / cache_key(url)
    except OSError:  # pragma: no cover - defensivo
        return None


def _disk_put(url: str, caps: MediaCapabilities, cache_dir: Path | None, max_bytes: int) -> None:
    path = _cache_path(url, cache_dir)
    if path is None:
        return
    payload = {
        "version": 1,
        "stored_at": time.time(),
        "protocol": caps.protocol,
        "capabilities": caps.to_dict(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        chmod_private(path.parent, directory=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        chmod_private(tmp)
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        # La caché es una optimización: si falla, se sigue sin ella.
        return


def _disk_get(url: str, cache_dir: Path | None) -> MediaCapabilities | None:
    path = _cache_path(url, cache_dir)
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    stored_at = payload.get("stored_at")
    if not isinstance(stored_at, (int, float)):
        return None
    if time.time() - float(stored_at) > DISK_TTL:
        return None
    data = payload.get("capabilities")
    if not isinstance(data, dict):
        return None
    try:
        caps = MediaCapabilities.from_dict(data)
    except Exception:  # noqa: BLE001 - una caché corrupta no es un error
        return None
    # Sin URIs no se puede fijar una pista: la caché sólo sirve como
    # información ("este canal tiene 3 audios"), no como fuente de verdad.
    for bucket in (caps.audio_tracks, caps.video_variants, caps.subtitle_tracks):
        for track in bucket:
            track.uri = None
    return caps


# ---------------------------------------------------------------------------
# Sondeo en segundo plano
# ---------------------------------------------------------------------------


@dataclass
class TrackProbe:
    """Sondea un canal en un hilo daemon y avisa por callback.

    Uso en la TUI::

        probe = TrackProbe(channel, allow_private=app._allow_private_for(src))
        probe.start(on_ready)     # no bloquea
        ...
        probe.result              # None hasta que llega

    Ninguna excepción sale del hilo ni llega a la TUI: los fallos se
    reportan como ``ProbeResult(capabilities=None, reason=...)`` (SDD §29).
    """

    channel: Channel
    allow_private: bool = False
    timeout: float = TOTAL_TIMEOUT
    cache_dir: Path | None = TRACK_CACHE_DIR
    use_cache: bool = True

    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _result: ProbeResult | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _on_ready: Callable[[ProbeResult], None] | None = field(default=None, init=False, repr=False)

    # -- API ---------------------------------------------------------------

    @property
    def result(self) -> ProbeResult | None:
        """Resultado si ya está; ``None`` mientras sondea."""
        with self._lock:
            return self._result

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def probe_now(self) -> ProbeResult:
        """Sondea en este hilo (síncrono). Nunca lanza."""
        result = probe_capabilities(
            self.channel,
            allow_private=self.allow_private,
            timeout=self.timeout,
            use_cache=self.use_cache,
            cache_dir=self.cache_dir,
        )
        with self._lock:
            self._result = result
        return result

    def start(self, on_ready: Callable[[ProbeResult], None] | None = None) -> None:
        """Arranca el hilo daemon. Si ya está corriendo, no hace nada."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._on_ready = on_ready
            self._thread = threading.Thread(
                target=self._run, name="track-probe", daemon=True
            )
        self._thread.start()

    def stop(self, timeout: float = 0.6) -> None:
        """Espera (breve) al hilo. Es daemon: no bloquea la salida."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        self._thread = None

    def _run(self) -> None:
        try:
            result = self.probe_now()
        except Exception as exc:  # noqa: BLE001 - red de seguridad total
            result = ProbeResult(
                reason=redact_text(_short(str(exc))) or "El sondeo falló."
            )
        callback = self._on_ready
        if callback is None:
            return
        try:
            callback(result)
        except Exception:  # noqa: BLE001 - un callback roto no tumba el hilo
            pass