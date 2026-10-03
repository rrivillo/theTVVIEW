"""Salud del canal en tiempo real (solo stdlib).

Mide la conectividad del stream URL de forma no bloqueante y produce un
resumen fácil de entender para el usuario (barra 5 niveles + colores +
mensajes humanos).

Diseño:
- probe_channel_url(channel, timeout) -> HealthSnapshot : prueba HTTP HEAD
  (fallback GET range) midiendo latencia y código.
- classify_health(snapshots, player_alive) -> HealthStatus : agrega últimos
  probes + estado del reproductor y devuelve nivel 0..5 con etiqueta, barra
  y color curses.
- ChannelHealthMonitor : hilo daemon que sondea cada `interval` segundos sin
  bloquear la TUI; expone status() thread-safe.

Todo con timeout y errores amigables; sin dependencias ni shell.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque

from urllib.error import HTTPError

from thetvview.models import Channel
from thetvview.security.safe_http import SafeHttpClient
from thetvview.security.url_policy import PURPOSE_STREAM
from thetvview.streams.headers import build_headers as _build_headers


# ---------------------------------------------------------------------------
# Snapshots y Status
# ---------------------------------------------------------------------------

@dataclass
class HealthSnapshot:
    """Resultado de una sonda individual al stream URL."""

    timestamp: datetime
    latency_ms: float | None  # None si falló antes de medir
    success: bool | None  # None = no medible (ej. rtmp://)
    http_code: int | None
    error: str | None  # mensaje corto amigable en español
    raw_error: str | None = None  # opcional para debug


@dataclass
class HealthStatus:
    """Estado agregado listo para UI."""

    level: int  # 0..5
    label: str  # Ej. "Excelente"
    bar: str  # Ej. "●●●●●"
    color_pair: int  # id curses PAIR_* (no color_pair() aún)
    summary: str  # línea corta: "124 ms · 100% ok"
    detail: str  # línea explicativa: "Fluida, ideal para ver sin cortes"
    success_rate: float  # 0..1
    avg_latency_ms: float | None
    last_latency_ms: float | None
    jitter_ms: float | None
    probe_count: int
    success_count: int


# Mapeo nivel -> (label, bar, color_pair_name, detail_base)
_LEVEL_META: dict[int, tuple[str, str, str, str]] = {
    5: ("Excelente", "●●●●●", "success", "Fluida, sin cortes ni pixelado"),
    4: ("Buena", "●●●●○", "success", "Muy fluida, cortes muy raros"),
    3: ("Regular", "●●●○○", "warning", "Puede entrecortarse en picos"),
    2: ("Inestable", "●●○○○", "warning", "Cortes y congelamientos probables"),
    1: ("Mala", "●○○○○", "danger", "Pixelado y cortes frecuentes"),
    0: ("Sin señal", "○○○○○", "danger", "Sin señal: revisa URL o internet"),
}

# Resolver color_pair id lazily (evitar import curses en tests)
def _color_for(level: int) -> int:
    from thetvview.ui import colors as _colors

    name = _LEVEL_META[level][2]
    return {
        "success": _colors.PAIR_SUCCESS,
        "warning": _colors.PAIR_WARNING,
        "danger": _colors.PAIR_DANGER,
    }.get(name, _colors.PAIR_DANGER)


def health_bar(level: int) -> str:
    level = max(0, min(5, int(level)))
    return _LEVEL_META[level][1]


def health_label(level: int) -> str:
    level = max(0, min(5, int(level)))
    return _LEVEL_META[level][0]


# ---------------------------------------------------------------------------
# Helpers: headers + probe
# ---------------------------------------------------------------------------

def _friendly_error(exc: Exception) -> str:
    """Traduce una excepción a un mensaje corto para la barra de estado.

    Los errores de `safe_http` ya vienen en español y redactados, así que
    aquí solo se abrevian; se conservan las reglas clásicas por si llega
    algo de otra capa.
    """
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    msg = str(exc).lower()
    for needle, short in _FRIENDLY_RULES:
        if needle in msg:
            return short
    s = str(exc)
    # truncar mensaje crudo a algo legible
    if len(s) > 60:
        s = s[:57] + "..."
    # limpiar prefijo feo de URLError
    s = s.replace("<urlopen error ", "").strip(" []")
    return s or "Error de red"


#: (fragmento en minúsculas, etiqueta corta) para la sonda.
_FRIENDLY_RULES: tuple[tuple[str, str], ...] = (
    ("tiempo de espera agotado", "Tiempo agotado"),
    ("timed out", "Tiempo agotado"),
    ("timeout", "Tiempo agotado"),
    ("no resuelve en dns", "DNS no resuelve"),
    ("name or service not known", "DNS no resuelve"),
    ("nodename nor servname", "DNS no resuelve"),
    ("rechazó la conexión", "Conexión rechazada"),
    ("connection refused", "Conexión rechazada"),
    ("no hay ruta", "Sin ruta al servidor"),
    ("no route to host", "Sin ruta al servidor"),
    ("red no disponible", "Red no disponible"),
    ("network is unreachable", "Red no disponible"),
    ("destino bloqueado", "URL bloqueada"),
    ("demasiadas solicitudes", "Límite de peticiones"),
    ("demasiadas peticiones", "Límite de peticiones"),
    ("supera el límite", "Respuesta muy grande"),
    ("no es segura", "Error TLS/certificado"),
    ("certificado", "Error TLS/certificado"),
    ("tls", "Error TLS/certificado"),
    ("ssl", "Error TLS/certificado"),
)


def _is_http_url(url: str) -> bool:
    low = url.strip().lower()
    return low.startswith("http://") or low.startswith("https://")


def _strip_ffmpeg_prefix(url: str) -> str:
    if url.startswith("ffmpeg://"):
        return url[len("ffmpeg://") :]
    return url


#: Tope de cuerpo para la sonda. Con `read_body=False` no se lee nada,
#: pero si algún día se lee, es el máximo que se materializa.
_PROBE_BODY_BYTES: int = 64


def probe_channel_url(
    channel: Channel,
    timeout: float = 3.0,
    *,
    allow_private: bool = False,
) -> HealthSnapshot:
    """Hace una sonda HTTP al URL del canal y mide latencia.

    - Soporta http/https únicamente; otros esquemas (rtmp, udp, rtsp) devuelven
      success=None (no medible) sin hacer red.
    - Intenta HEAD; si el servidor responde 405/501/403/400, reintenta GET con
      Range bytes=0-1 (más compatible con HLS) **sin leer el cuerpo**: en un
      directo el servidor nunca terminaría de enviarlo.
    - Timeout corto (default 3s) y sin reintentos, para no congelar el hilo
      ni martillar al servidor.
    - La URL pasa por `safe_http` (B3): política de URL, anti-SSRF,
      redirects revalidados y TLS estricto. ``allow_private=True`` solo si
      la fuente lo declaró (SDD §11).
    """
    now = datetime.now(timezone.utc).astimezone()
    raw_url = _strip_ffmpeg_prefix(channel.url.strip())
    if not raw_url:
        return HealthSnapshot(now, None, False, None, "URL vacía", "empty url")
    if not _is_http_url(raw_url):
        # No podemos medir sin http; devolvemos "no medible" pero no error
        return HealthSnapshot(now, None, None, None, "Protocolo no HTTP", raw_url[:60])

    headers = _build_headers(channel)
    client = SafeHttpClient(
        allow_private=allow_private,
        purpose=PURPOSE_STREAM,
        max_bytes=_PROBE_BODY_BYTES,
        retries=0,
        accept_gzip=False,
    )
    start = time.monotonic()

    def _latency() -> float:
        return (time.monotonic() - start) * 1000.0

    def _network_fail(exc: Exception) -> HealthSnapshot:
        # safe_http ya redacta URLs/credenciales en el mensaje.
        return HealthSnapshot(
            now, _latency(), False, None, _friendly_error(exc), str(exc)
        )

    try:
        resp = client.head(raw_url, headers=headers, timeout=timeout)
        code = int(resp.status)
        if code in (405, 501, 403, 400):
            # Reintentar con GET: muchos servidores HLS rechazan HEAD.
            # `read_body=False`: sólo nos interesa el status.
            get_headers = dict(headers)
            get_headers["Range"] = "bytes=0-1"
            resp = client.request(
                raw_url,
                method="GET",
                headers=get_headers,
                timeout=timeout,
                read_body=False,
            )
            code = int(resp.status)
    except Exception as exc:  # noqa: BLE001 - la sonda nunca debe romper el hilo
        return _network_fail(exc)

    # 2xx y 3xx se consideran éxito (redirecciones HLS válidas)
    success = 200 <= code < 400
    err = None if success else f"HTTP {code}"
    return HealthSnapshot(now, _latency(), success, code, err, err)


# ---------------------------------------------------------------------------
# Clasificación agregada
# ---------------------------------------------------------------------------

def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _jitter(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    # jitter simple: max - min (fácil de entender)
    try:
        return max(values) - min(values)
    except Exception:
        return None


def classify_health(
    snapshots: list[HealthSnapshot] | Deque[HealthSnapshot],
    *,
    player_alive: bool,
) -> HealthStatus:
    """Agrega snapshots y produce HealthStatus 0..5.

    Reglas fáciles de explicar al usuario:
    - Si el reproductor no está vivo -> 0 Sin señal.
    - Si no hay probes aún -> 0 Sin señal con mensaje "Midiendo...".
    - Si success es None (protocolo no HTTP) -> nivel 3 Regular con aviso
      "No medible: ver reproductor".
    - Sin no: success_rate y latencia mediana determinan nivel; jitter alto
      baja un escalón.
    """
    snaps = list(snapshots)
    n = len(snaps)

    if not player_alive:
        return HealthStatus(
            level=0,
            label=health_label(0),
            bar=health_bar(0),
            color_pair=_color_for(0),
            summary="Reproductor detenido",
            detail="El reproductor externo no está en ejecución.",
            success_rate=0.0,
            avg_latency_ms=None,
            last_latency_ms=None,
            jitter_ms=None,
            probe_count=n,
            success_count=0,
        )

    if n == 0:
        return HealthStatus(
            level=0,
            label="Midiendo…",
            bar="○○○○○",
            color_pair=_color_for(0),
            summary="Midiendo salud…",
            detail="Probando conexión del canal, un momento…",
            success_rate=0.0,
            avg_latency_ms=None,
            last_latency_ms=None,
            jitter_ms=None,
            probe_count=0,
            success_count=0,
        )

    # Caso no HTTP medible: si el último es None
    last = snaps[-1]
    if last.success is None:
        return HealthStatus(
            level=3,
            label="No medible",
            bar=health_bar(3),
            color_pair=_color_for(3),
            summary="URL no HTTP · ver reproductor",
            detail="Este canal usa protocolo directo; la salud se ve en el reproductor.",
            success_rate=1.0,
            avg_latency_ms=None,
            last_latency_ms=None,
            jitter_ms=None,
            probe_count=n,
            success_count=n,
        )

    success_count = sum(1 for s in snaps if s.success is True)
    # None ya filtrado, por lo que False es fallo
    success_rate = success_count / n if n else 0.0

    # latencias solo de éxitos
    lat_vals = [s.latency_ms for s in snaps if s.success and s.latency_ms is not None]
    avg_lat = _avg(lat_vals) if lat_vals else None
    last_lat = last.latency_ms if last.success and last.latency_ms is not None else None
    # si último falló, avg sigue valiendo pero last es None -> usar avg para summary
    jitter_val = _jitter(lat_vals)

    # Si todo falló recientemente
    if success_rate == 0.0:
        # todos fallaron
        err = last.error or "Sin respuesta"
        return HealthStatus(
            level=0,
            label=health_label(0),
            bar=health_bar(0),
            color_pair=_color_for(0),
            summary=f"0% ok · {err}",
            detail="Sin señal: revisa tu internet o la URL del canal.",
            success_rate=0.0,
            avg_latency_ms=avg_lat,
            last_latency_ms=last_lat,
            jitter_ms=jitter_val,
            probe_count=n,
            success_count=0,
        )

    # Nivel base por latencia (sobre avg o last)
    ref_lat = avg_lat if avg_lat is not None else last_lat
    if ref_lat is None:
        base = 3  # sin latencia pero éxito: regular
    elif ref_lat < 250:
        base = 5
    elif ref_lat < 600:
        base = 4
    elif ref_lat < 1200:
        base = 3
    elif ref_lat < 2000:
        base = 2
    else:
        base = 1

    # Penalización por tasa de éxito y jitter
    if success_rate < 0.5:
        base = min(base, 1)
    elif success_rate < 0.8:
        base = max(0, base - 2)
    elif success_rate < 1.0:
        base = max(0, base - 1)

    # Jitter alto (>800ms) indica inestabilidad aunque latencia media baja
    if jitter_val is not None and jitter_val > 800:
        base = max(0, base - 1)
        # Si además success_rate no perfecto, baja otro por inestable
        if success_rate < 1.0:
            base = max(0, base - 1)

    # Si el último probe falló pero history mayormente ok, no caer a 0 de golpe: penaliza 1
    if last.success is False and success_rate >= 0.5:
        base = max(1, base - 1)

    level = max(0, min(5, base))
    label = health_label(level)
    bar = health_bar(level)
    color = _color_for(level)
    detail = _LEVEL_META[level][3]

    # Summary humano
    parts: list[str] = []
    # latencia
    if last_lat is not None:
        parts.append(f"{int(last_lat)} ms")
    elif avg_lat is not None:
        parts.append(f"~{int(avg_lat)} ms")
    else:
        parts.append("— ms")
    # tasa
    pct = int(success_rate * 100)
    parts.append(f"{pct}% ok ({success_count}/{n})")
    # jitter si relevante
    if jitter_val is not None and jitter_val > 300:
        parts.append(f"jitter {int(jitter_val)} ms")
    # si último error y no 100%
    if last.success is False and last.error:
        parts.append(last.error)

    summary = " · ".join(parts)

    return HealthStatus(
        level=level,
        label=label,
        bar=bar,
        color_pair=color,
        summary=summary,
        detail=detail,
        success_rate=success_rate,
        avg_latency_ms=avg_lat,
        last_latency_ms=last_lat,
        jitter_ms=jitter_val,
        probe_count=n,
        success_count=success_count,
    )


# ---------------------------------------------------------------------------
# Monitor en hilo daemon (no bloquea curses)
# ---------------------------------------------------------------------------

class ChannelHealthMonitor:
    """Sondea el canal periódicamente en segundo plano.

    Uso en TUI:
        mon = ChannelHealthMonitor(channel)
        mon.start()
        # en cada render:
        status = mon.status(player_alive=screen.is_alive())
        # al salir:
        mon.stop()

    Tests pueden usar auto_start=False y llamar a probe_once() manual.
    """

    def __init__(
        self,
        channel: Channel,
        *,
        interval: float = 3.0,
        history_size: int = 8,
        timeout: float = 3.0,
        auto_start: bool = True,
        allow_private: bool = False,
    ) -> None:
        self.channel = channel
        self.interval = max(0.5, float(interval))
        self.timeout = max(0.5, float(timeout))
        #: Excepción anti-SSRF de la fuente a la que pertenece el canal
        #: (SDD §11). Por defecto la red privada está bloqueada.
        self.allow_private = bool(allow_private)
        self.history: Deque[HealthSnapshot] = deque(maxlen=max(0, int(history_size)) or 8)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if auto_start:
            self.start()

    def probe_once(self) -> HealthSnapshot:
        snap = probe_channel_url(
            self.channel, timeout=self.timeout, allow_private=self.allow_private
        )
        with self._lock:
            self.history.append(snap)
        return snap

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="health-probe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thr = self._thread
        if thr and thr.is_alive():
            thr.join(timeout=0.6)
        self._thread = None

    def _loop(self) -> None:
        # Probe inmediato para que la UI tenga dato rápido sin bloquear el
        # hilo principal; luego cada `interval` segundos.
        try:
            self.probe_once()
        except Exception:
            pass
        while not self._stop.wait(self.interval):
            try:
                self.probe_once()
            except Exception:
                # nunca matar el hilo por excepción de probe
                continue

    def snapshots(self) -> list[HealthSnapshot]:
        with self._lock:
            return list(self.history)

    def status(self, *, player_alive: bool) -> HealthStatus:
        with self._lock:
            snaps = list(self.history)
        return classify_health(snaps, player_alive=player_alive)

    def latest(self) -> HealthSnapshot | None:
        with self._lock:
            return self.history[-1] if self.history else None

    @property
    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())
