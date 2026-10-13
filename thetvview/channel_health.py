"""Salud del canal en tiempo real (solo stdlib).

Mide la conectividad del stream URL de forma no bloqueante y produce un
resumen fácil de entender para el usuario (barra 5 niveles + colores +
mensajes humanos).

Diseño:
- probe_channel_url(channel, timeout) -> HealthSnapshot : prueba HTTP HEAD
  (fallback GET range) midiendo latencia y código; el Content-Type que
  ya viene en la cabecera es la pista gratuita de la dimensión «fuente».
- classify_health(snapshots, player_alive) -> HealthStatus : agrega últimos
  probes + estado del reproductor y devuelve nivel 0..5 con etiqueta, barra
  y color curses, **y las tres dimensiones del §11.1** (red / fuente /
    reproducción) de las que el nivel se deriva (DIAG-003).
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
from enum import Enum
from typing import Deque

from urllib.error import HTTPError

from thetvview.models import Channel
from thetvview.player.evidence import EvidenceLevel
from thetvview.security.safe_http import SafeHttpClient
from thetvview.security.url_policy import PURPOSE_STREAM
from thetvview.streams.headers import build_headers as _build_headers
from thetvview.tasks.manager import CancellationToken, TaskManager

#: Gestor compartido para monitores que se crean sueltos (tests, uso sin TUI).
_DEFAULT_MANAGER: TaskManager | None = None
_MANAGER_LOCK = threading.Lock()


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
    #: Content-Type que el servidor declaró, si lo declaró. Es la única
    #: pista de la **validez de la fuente** (§11.1, dimensión 2) que sale
    #: gratis de una sonda que ya se está haciendo: no cuesta una petición
    #: extra (FR-DIAG-002/004).
    content_type: str | None = None


class DimensionEstado(str, Enum):
    """Estado de **una** dimensión de salud (§11.1).

    §11.1 exige tres dimensiones independientes —red/servidor, validez de
    la fuente y reproducción— sin colapsarlas en un «funciona». Ésta es la
    escala común de las tres y sólo dice lo que se ha medido:
    ``DESCONOCIDO`` no es «malo»: es «no se ha comprobado» (FR-STA-02).
    """

    OK = "ok"
    DEGRADADO = "degradado"
    FALLA = "falla"
    DESCONOCIDO = "desconocido"

    def __str__(self) -> str:  # pragma: no cover - depuración
        return self.value


@dataclass
class HealthStatus:
    """Estado agregado listo para UI.

    Las tres dimensiones del §11.1 viven aquí **por separado** y el nivel
    0..5 se deriva de ellas: la UI no cambia (sigue leyendo `level`), pero
    «el servidor contesta» deja de significar «esto funciona». Lo que no se
    ha medido queda ``DESCONOCIDO`` en su dimensión, nunca en «ok».
    """

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
    #: False cuando el canal no se puede medir por red (rtmp://, udp://...):
    #: el nivel que devuelva entonces es una conjetura, no una comprobación.
    #: La capa de estados (`estado_de`) lo lee para no pintar como «offline»
    #: o «online» lo que en realidad no se sabe (FR-STA-01/02).
    medible: bool = True
    #: Evidencia de reproducción que se usó para calcular esto (FR-REC-002).
    #: La barra lo muestra para no decir «funciona» cuando sólo se sabe que el
    #: proceso existe.
    evidencia: EvidenceLevel = EvidenceLevel.NONE
    #: Dimensión 1 (§11.1): DNS, conexión, TLS, HTTP y tiempos.
    red: DimensionEstado = DimensionEstado.DESCONOCIDO
    #: Dimensión 2 (§11.1): tipo de contenido y validez de la fuente.
    fuente: DimensionEstado = DimensionEstado.DESCONOCIDO
    #: Dimensión 3 (§11.1): señales del motor y avance de medios.
    reproduccion: DimensionEstado = DimensionEstado.DESCONOCIDO


class Estado(str, Enum):
    """Los cuatro estados lógicos que la UI sabe pintar (FR-STA-01).

    Securities sobre el nivel 0..5 que usa el diagnóstico: el nivel es una
    medida de calidad **cuando hay medición**, y `DESCONOCIDO` es el estado de
    primera clase para cuando no la hay. Un canal recién añadido no está
    «offline»: no se sabe nada de él, y decirlo sería inventar (FR-STA-02).
    """

    DESCONOCIDO = "desconocido"
    ONLINE = "online"
    AVISO = "aviso"
    OFFLINE = "offline"

    def __str__(self) -> str:  # pragma: no cover - conveniencia de depuración
        return self.value


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


def estado_de(status: HealthStatus | None) -> Estado:
    """Traduce el nivel 0..5 a los cuatro estados lógicos de la UI.

    Sólo **etiqueta**: no mide, no sondea y no decide disponibilidad. Lee el
    `HealthStatus` que el monitor ya tiene en memoria, de modo que pintar el
    estado no cuesta una red (FR-STA-04/05, T-STA-04).

    - sin sondas, o un canal que no se puede medir → `DESCONOCIDO`;
    - calidad buena → `ONLINE`; dudosa → `AVISO`; mala o reproductor parado
      → `OFFLINE`.
    """
    if status is None:
        return Estado.DESCONOCIDO
    try:
        sondas = int(getattr(status, "probe_count", 0) or 0)
        nivel = int(getattr(status, "level", 0) or 0)
    except (TypeError, ValueError):
        # Un estado que no se puede leer no se pinta como ONLINE ni como
        # OFFLINE: se dice que no se sabe (FR-STA-02).
        return Estado.DESCONOCIDO
    if sondas <= 0:
        return Estado.DESCONOCIDO
    if not bool(getattr(status, "medible", True)):
        return Estado.DESCONOCIDO
    if nivel >= 4:
        return Estado.ONLINE
    if nivel >= 2:
        return Estado.AVISO
    return Estado.OFFLINE


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

    def _tipo(resp) -> str | None:  # noqa: ANN001 - respuesta de safe_http
        """Content-Type declarado, si lo hay: gratis, ya viene en la cabecera."""
        try:
            return str(resp.headers.get("content-type", "") or "").strip() or None
        except Exception:  # noqa: BLE001 - una cabecera rara no rompe la sonda
            return None

    def _network_fail(exc: Exception) -> HealthSnapshot:
        # safe_http ya redacta URLs/credenciales en el mensaje.
        return HealthSnapshot(
            now, _latency(), False, None, _friendly_error(exc), str(exc)
        )

    try:
        resp = client.head(raw_url, headers=headers, timeout=timeout)
        content_type = _tipo(resp)
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
            content_type = _tipo(resp) or content_type
            code = int(resp.status)
    except Exception as exc:  # noqa: BLE001 - la sonda nunca debe romper el hilo
        return _network_fail(exc)

    # 2xx y 3xx se consideran éxito (redirecciones HLS válidas)
    success = 200 <= code < 400
    err = None if success else f"HTTP {code}"
    return HealthSnapshot(
        now, _latency(), success, code, err, err, content_type=content_type
    )


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


def _normalizar_evidencia(
    evidencia: EvidenceLevel | None,
    player_alive: bool | None,
) -> EvidenceLevel:
    """Traduce los dos parámetros públicos a un único nivel de evidencia.

    Acepta las dos formas a propósito: la nueva (``evidencia``) y la vieja
    (``player_alive``), para que cambiar el criterio no rompa a quien llama.
    Con los dos, manda ``evidencia``: es más específico.
    """
    if evidencia is not None:
        return evidencia
    return EvidenceLevel.PROCESS_ALIVE if player_alive else EvidenceLevel.NONE


def _proceso_vivo(
    evidencia: EvidenceLevel,
    player_alive: bool | None,
) -> bool:
    """¿Está vivo el reproductor, según lo que sabemos?

    ``UNKNOWN`` **no** significa «parado»: significa «no lo sé de los medios».
    Quien llama a :func:`classify_health` lo hace porque hay una reproducción en
    marcha, así que con evidencia desconocida se asume el proceso vivo y lo que
    se dice es «no comprobado», no «detenido» —que sería afirmar algo que nadie
    sabe (FR-STA-02).
    """
    if evidencia is EvidenceLevel.UNKNOWN:
        return True if player_alive is None else bool(player_alive)
    if evidencia is EvidenceLevel.NONE:
        return False
    return evidencia.rank >= EvidenceLevel.PROCESS_ALIVE.rank


# ---------------------------------------------------------------------------
# Dimensiones de salud (§11.1 · DIAG-003)
# ---------------------------------------------------------------------------
#
# §11.1 exige tres dimensiones **independientes**: red/servidor, validez de
# la fuente y reproducción. El nivel 0..5 se deriva de ellas (con un techo
# cuando una dimensión está a la baja); ellas no se derivan del nivel.
# Nada se colapsa en un «funciona».

#: Lo que un Content-Type **sí** prueba sobre la fuente (FR-DIAG-003). Un
#: HTML o un JSON en un canal de vídeo no es un stream: es un error
#: disfrazado de 200. Todo lo demás queda DESCONOCIDO — un tipo raro no
#: prueba nada en ninguna dirección, y inventar un veredicto sería peor.
_FUENTE_FALLA_MIME: tuple[str, ...] = ("text/html", "application/json", "xhtml")
_FUENTE_OK_MIME: tuple[str, ...] = (
    "mpegurl",
    "dash+xml",
    "mp2t",
    "video/",
    "audio/",
    "mpg",
    "mp4",
    "matroska",
    "ogg",
)


def fuente_de_mimetipo(content_type: str | None) -> DimensionEstado:
    """Traduce un Content-Type declarado a la dimensión «fuente» (§11.1).

    Sólo afirma lo que el tipo prueba: ``text/html`` no puede ser un stream
    (``FALLA``); los tipos de manifiesto y medios dicen ``OK``; lo demás,
    incluido el vacío, es ``DESCONOCIDO`` — no «malo».
    """
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not mime:
        return DimensionEstado.DESCONOCIDO
    if any(pista in mime for pista in _FUENTE_FALLA_MIME):
        return DimensionEstado.FALLA
    if any(pista in mime for pista in _FUENTE_OK_MIME):
        return DimensionEstado.OK
    return DimensionEstado.DESCONOCIDO


def _dimension_red(snaps: list[HealthSnapshot]) -> DimensionEstado:
    """Dimensión 1 (§11.1): lo que las sondas prueban del servidor."""
    if not snaps:
        return DimensionEstado.DESCONOCIDO
    if snaps[-1].success is None:
        # No medible por red (rtmp://…): no se sabe, no es «ok».
        return DimensionEstado.DESCONOCIDO
    exitos = sum(1 for s in snaps if s.success is True)
    tasa = exitos / len(snaps)
    if tasa == 0.0:
        return DimensionEstado.FALLA
    if tasa < 0.8 or snaps[-1].success is False:
        return DimensionEstado.DEGRADADO
    return DimensionEstado.OK


def _dimension_reproduccion(
    vivo: bool, nivel: EvidenceLevel
) -> DimensionEstado:
    """Dimensión 3 (§11.1): señales del motor y avance de medios.

    El proceso vivo **sin** confirmación de medios no es ``OK``: es
    ``DESCONOCIDO``. Que mpv exista no dice que haya imagen (FR-REC-002).
    """
    if not vivo:
        return DimensionEstado.FALLA
    if nivel.rank >= EvidenceLevel.MEDIA_SEEN.rank:
        return DimensionEstado.OK
    return DimensionEstado.DESCONOCIDO


def _techo_por_dimensiones(
    nivel: int,
    red: DimensionEstado,
    fuente: DimensionEstado,
) -> int:
    """Techo que las dimensiones imponen al nivel medido (§11.1).

    Un servidor accesible con fuente inválida **no** se marca como
    reproducción funcional: cae a «Mala» aunque la red sea impecable. Y si
    la red está en ``FALLA``, no hay nivel que salvar.
    """
    if red is DimensionEstado.FALLA:
        return 0
    if fuente is DimensionEstado.FALLA:
        return min(nivel, 1)
    if fuente is DimensionEstado.DEGRADADO:
        return min(nivel, 3)
    return nivel


def _aviso_de_fuente(fuente: DimensionEstado, detalle: str) -> str:
    """Añade al detalle lo que la dimensión «fuente» sabe, si algo sabe."""
    if fuente is DimensionEstado.FALLA:
        return (
            f"{detalle}. La fuente no sirve: el servidor contesta, pero el "
            "contenido no parece reproducible."
        )
    if fuente is DimensionEstado.DEGRADADO:
        return f"{detalle}. El tipo de contenido de la fuente no es el esperado."
    return detalle


def classify_health(
    snapshots: list[HealthSnapshot] | Deque[HealthSnapshot],
    *,
    player_alive: bool | None = None,
    evidencia: EvidenceLevel | None = None,
    fuente: DimensionEstado | None = None,
) -> HealthStatus:
    """Agrega snapshots y produce HealthStatus 0..5 con sus dimensiones.

    Reglas fáciles de explicar al usuario:
    - Si el reproductor no está vivo -> 0 Sin señal.
    - Si no hay probes aún -> 0 Sin señal con mensaje "Midiendo...".
    - Si success es None (protocolo no HTTP) -> nivel 3 Regular con aviso
      "No medible: ver reproductor".
    - Sin no: success_rate y latencia mediana determinan nivel; jitter alto
      baja un escalón.

    Dimensiones (§11.1 · DIAG-003)
    -------------------------------

    Además del número, el resultado lleva **tres dimensiones por separado**
    —`red` (servidor/tiempos), `fuente` (contenido válido) y `reproduccion`
    (evidencia del motor)— y el nivel se deriva de ellas: una red
    impecable con fuente inválida techoa a «Mala», porque un servidor que
    contesta no está sirviendo algo reproducible. La UI sigue leyendo
    sólo `level`/`label`/`summary`, así que nada cambia visualmente; lo
    que cambia es que ya se puede responder **por qué**.

    `fuente`: si quien llama ya sabe el veredicto de la fuente (p. ej. de
    un análisis de manifiesto), lo pasa aquí y manda; si no, se deriva del
    Content-Type que la sonda ya trajo (gratis: FR-DIAG-002/004).

    ``evidencia`` (FR-REC-002)
    ---------------------------

    El criterio de «está vivo» pasó de un ``bool`` a un
    :class:`~thetvview.player.evidence.EvidenceLevel`, porque ``poll() is None``
    sólo dice que el proceso existe. El parámetro ``player_alive`` **se sigue
    aceptando** y se traduce a ``PROCESS_ALIVE``/``NONE``: es lo que permite
   phrasing este cambio sin tocar a quien ya llama.

    Con evidencia declarada, la etiqueta dice lo que sabe y lo que no:
    ``Reproduciendo (sin comprobar)`` cuando el motor no expone señal, que es
    una información real y no un adorno.
    """
    snaps = list(snapshots)
    n = len(snaps)
    nivel_evidencia = _normalizar_evidencia(evidencia, player_alive)
    vivo = _proceso_vivo(nivel_evidencia, player_alive)
    # Las tres dimensiones (§11.1) se calculan con lo que ya se tiene:
    # nunca abren red ni inventan un veredicto.
    ultima = snaps[-1] if snaps else None
    dim_red = _dimension_red(snaps)
    if fuente is not None and not isinstance(fuente, DimensionEstado):
        fuente = DimensionEstado(str(fuente))
    dim_fuente = (
        fuente
        if fuente is not None
        else fuente_de_mimetipo(ultima.content_type if ultima else None)
    )
    dim_repro = _dimension_reproduccion(vivo, nivel_evidencia)

    if not vivo:
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
            evidencia=nivel_evidencia,
            red=dim_red,
            fuente=dim_fuente,
            reproduccion=DimensionEstado.FALLA,
        )

    if n == 0:
        # Sin sondas **todavía**. Decir "Sin señal" aquí afirmaría algo que no
        # se sabe: el canal puede estar perfectamente (FR-STA-02).
        return HealthStatus(
            level=0,
            label="Sin comprobar",
            bar="○○○○○",
            color_pair=_color_for(0),
            summary="Sin comprobar todavía",
            detail="Aún no se ha comprobado la conexión con este canal.",
            success_rate=0.0,
            avg_latency_ms=None,
            last_latency_ms=None,
            jitter_ms=None,
            probe_count=0,
            success_count=0,
            evidencia=nivel_evidencia,
            red=DimensionEstado.DESCONOCIDO,
            fuente=DimensionEstado.DESCONOCIDO,
            reproduccion=dim_repro,
        )

    # Caso no HTTP medible: si el último es None
    last = ultima
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
            medible=False,
            evidencia=nivel_evidencia,
            red=dim_red,
            fuente=dim_fuente,
            reproduccion=dim_repro,
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
            evidencia=nivel_evidencia,
            red=DimensionEstado.FALLA,
            fuente=dim_fuente,
            reproduccion=dim_repro,
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

    # El nivel mide la calidad de la **red**, pero no manda él solo: las
    # dimensiones del §11.1 techoan el resultado — un servidor accesible
    # con fuente inválida no se marca como reproducción funcional.
    level = _techo_por_dimensiones(max(0, min(5, base)), dim_red, dim_fuente)
    label = health_label(level)
    bar = health_bar(level)
    color = _color_for(level)
    detail = _aviso_de_fuente(dim_fuente, _LEVEL_META[level][3])

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

    # La barra de red y la de reproducción son cosas distintas: que el servidor
    # responda rápido no dice que haya imagen. Cuando la evidencia no llega a
    # ``MEDIA_SEEN`` se lo dice al usuario en vez de dejar «Excelente» solo
    # (FR-REC-002, §11.1 del SDD de P1).
    #
    # Sólo se añade el matiz si el llamante pasó `evidencia`: quien sigue
    # llamando con `player_alive` recibe exactamente el texto de siempre, que es
    # lo que permite cambiar el criterio sin cambiar lo que el usuario ve.
    if evidencia is not None and not _evidencia_de_medios(nivel_evidencia):
        summary = f"{summary} · {nivel_evidencia.texto()}"
        detail = (
            f"{detail}. {nivel_evidencia.texto()}: la conexión está bien, pero "
            "no hay confirmación de que esté llegando imagen."
        )

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
        evidencia=nivel_evidencia,
        red=dim_red,
        fuente=dim_fuente,
        reproduccion=dim_repro,
    )


def _evidencia_de_medios(nivel: EvidenceLevel) -> bool:
    """True si hay evidencia de que están llegando medios."""
    return nivel.rank >= EvidenceLevel.MEDIA_SEEN.rank


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

    Desde la fase 3 el sondeo periódico corre en el
    :class:`~thetvview.tasks.manager.TaskManager` en vez de en un hilo propio:
    gana cancelación cooperativa, límite de concurrencia y parada con plazo
    al cerrar la app. ``task_manager`` deja inyectar el gestor compartido; si
    no se pasa, el monitor usa uno propio.
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
        task_manager: TaskManager | None = None,
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
        #: Handle de la tarea periódica cuando corre por el gestor.
        self._handle: object | None = None
        #: Gestor compartido; ``None`` = se crea uno propio para este monitor.
        self._task_manager = task_manager
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
        if self.is_running:
            return
        self._stop.clear()
        self._handle = self._manager().submit("health-probe", self._loop)

    def _manager(self) -> TaskManager:
        """Gestor de tareas: el de la app si se pasó uno, o uno propio.

        Lo propio es el caso de un monitor suelto (tests). Lo compartido es el
        de la TUI, para que el cierre de la app lo pare con plazo en vez de
        dejar un hilo midiendo la salud de un canal que ya no se ve.
        """
        global _DEFAULT_MANAGER
        if self._task_manager is not None:
            return self._task_manager
        with _MANAGER_LOCK:
            if _DEFAULT_MANAGER is None:
                _DEFAULT_MANAGER = TaskManager(max_global=8, max_per_category=2)
            return _DEFAULT_MANAGER

    def stop(self) -> None:
        self._stop.set()
        handle = self._handle
        if handle is not None:
            try:
                handle.cancel("ya no se está viendo este canal")  # type: ignore[attr-defined]
                handle.join(timeout=0.6)  # type: ignore[attr-defined]
            except Exception:
                pass
            self._handle = None
        thr = self._thread
        if thr and thr.is_alive():
            thr.join(timeout=0.6)
        self._thread = None

    def _loop(self, token: CancellationToken | None = None) -> None:
        # Probe inmediato para que la UI tenga dato rápido sin bloquear el
        # hilo principal; luego cada `interval` segundos.
        #
        # La cancelación es cooperativa y se comprueba en dos sitios: la
        # espera entre sondas (`_stop.wait`, que devuelve True si alguien
        # puso la señal) y antes de cada sonda. Es lo que hace que `stop()`
        # sea rápido aunque el intervalo sea largo.
        def _cancelado() -> bool:
            if self._stop.is_set():
                return True
            return bool(token is not None and token.check())

        try:
            self.probe_once()
        except Exception:
            pass
        while not _cancelado():
            # `wait` sobre el evento: devuelve False al agotarse el plazo y
            # True en cuanto alguien cancela, así que no hay ni sondeo
            # fantasma ni espera a ciegas.
            if self._stop.wait(self.interval):
                break
            if _cancelado():
                break
            try:
                self.probe_once()
            except Exception:
                # nunca matar el hilo por excepción de probe
                continue

    def snapshots(self) -> list[HealthSnapshot]:
        with self._lock:
            return list(self.history)

    def status(
        self,
        *,
        player_alive: bool | None = None,
        evidencia: EvidenceLevel | None = None,
    ) -> HealthStatus:
        with self._lock:
            snaps = list(self.history)
        return classify_health(
            snaps, player_alive=player_alive, evidencia=evidencia
        )

    def latest(self) -> HealthSnapshot | None:
        with self._lock:
            return self.history[-1] if self.history else None

    @property
    def is_running(self) -> bool:
        handle = self._handle
        if handle is not None:
            return bool(not handle.done)  # type: ignore[attr-defined]
        t = self._thread
        return bool(t and t.is_alive())
