"""Proxy loopback que sirve un master HLS con **una sola variante** (plan F5c).

Por qué existe: la verificación empírica de la fase F5 (ver
:mod:`thetvview.player.capabilities`) dejó claro que **ninguno** de los tres
reproductores sabe elegir una variante concreta de un master HLS por argv. La
única forma *verificable* e *igual para todos* es reescribir el master y
servirlo nosotros: una sola ``EXT-X-STREAM-INF`` (la elegida) más las pistas de
audio y subtítulo elegidas.

Esto **no** es un proxy de segmentos. Todas las URI del master reescrito
apuntan al proveedor, así que el reproductor sigue hablando directamente con
él (y con sus cabeceras ``EXTVLCOPT``); sólo el *manifiesto* pasa por aquí.
Consecuencia buscada: el pin funciona aunque el proveedor exija ``Referer``,
porque al reproductor sólo se le pide un manifiesto local.

Modelo de seguridad (SDD §37, SECURITY.md):

- Escucha **sólo en 127.0.0.1** y en un **puerto efímero** (puerto 0): nadie
  desde la red puede alcanzarlo.
- La ruta lleva un **token de 128 bits** aleatorio:
  ``http://127.0.0.1:<puerto>/<token>/master.m3u8``. Sin el token, 404.
- Allowlist de rutas: sólo el master con el token delante. Cualquier otra
  cosa —segmentos, otras rutas, sin token— responde **404**, nunca 403 ni
  500: no se filtra qué hay detrás.
- ``log_message`` silenciado: el servidor no escribe en el stderr del
  proceso, que en una TUI es la propia interfaz.
- **Se apaga siempre**: ``atexit`` + ``stop()`` en el ``finally`` de quien lo
  usa. Si el manifiesto fijado no trae variantes, o el servidor no arranca, el
  llamante cae a los argumentos del reproductor y lo avisa (SDD §48).
"""

from __future__ import annotations

import atexit
import http.server
import secrets
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..tracks.labels import audio_label, subtitle_label
from ..tracks.models import (
    MediaCapabilities,
    MediaTrack,
    PlaybackSelection,
    puede_fijar_calidad,
)

__all__ = [
    "PinProxyError",
    "PinProxy",
    "rewrite_master",
    "start_pin_proxy",
    "TOKEN_BYTES",
    "LOOPBACK",
    "AUDIO_GROUP",
    "AUTO_AUDIO_GROUP",
]

#: 128 bits de token: adivinarlo es inviable y, aunque se filtrara la URL,
#: el servidor sólo sirve un manifiesto que el atacante ya tiene.
TOKEN_BYTES: int = 16

#: La única dirección a la que se enlaza. Nada más se escucha.
LOOPBACK: str = "127.0.0.1"

_MASTER_NAME: str = "master.m3u8"
_ALT_NAMES: tuple[str, ...] = ("index.m3u8",)

#: Grupo de audio del master fijado. Un grupo propio evita colisionar con los
#: grupos que declara el proveedor en sus propios manifiestos.
AUDIO_GROUP: str = "thetvview-audio"
SUBTITLE_GROUP: str = "thetvview-audio-sub"
#: Grupo del master "sin fijar" (ABR): se mantiene el que el manifiesto real
#: usaba, para no cambiar el comportamiento del reproductor.
AUTO_AUDIO_GROUP: str = "thetvview-auto"


class PinProxyError(Exception):
    """El proxy de fijado no pudo arrancar, o no puede servir lo pedido."""


# ---------------------------------------------------------------------------
# Reescritura del master
# ---------------------------------------------------------------------------


def _quote(value: str) -> str:
    """Valor de atributo HLS entrecomillado, con las comillas internas
    duplicadas (así lo define el formato)."""
    return '"' + str(value).replace('"', '""') + '"'


def rewrite_master(
    caps: MediaCapabilities,
    selection: PlaybackSelection,
    *,
    variant_id: str | None = None,
    header: str = "#EXTM3U",
) -> str:
    """Devuelve el master HLS con una sola variante (la elegida).

    Con ``auto_quality`` activo, o si la variante pedida ya no existe, se
    reescribe **con todas** las variantes: el proxy nunca debe degradar la
    calidad automática (AC-08).

    Raises:
        PinProxyError: si no hay ninguna variante sobre la que trabajar, o si
            el manifiesto no es un master HLS (ver `puede_fijar_calidad`).
    """
    # Red de seguridad. Este proxy reescribe un master **HLS**; aplicado a un
    # MPD de DASH produciría un manifiesto sintético cuyas variantes no tienen
    # URI de playlist, es decir, un master con la línea de URI vacía: el
    # reproductor no arrancaría y el usuario vería un canal roto por haber
    # elegido calidad. Mejor negarse y que el llamante caiga a calidad
    # automática, que es lo que hace `start_pin_proxy` devolviendo None.
    if not puede_fijar_calidad(caps):
        raise PinProxyError(
            "Sólo se puede fijar la calidad en streams HLS: este canal usa "
            f"{caps.protocol or 'otro protocolo'} y no hay forma de forzar una "
            "variante. Se reproduce con calidad automática."
        )
    variantes = caps.sorted_variants()
    if not variantes:
        raise PinProxyError(
            "El manifiesto del canal no declara ninguna variante de vídeo: "
            "no hay calidad fija que ofrecer."
        )

    objetivo = variant_id or selection.video_track_id
    elegida = None
    if objetivo:
        elegida = caps.variant_by_id(objetivo)
        if elegida is None:
            raise PinProxyError(
                "La calidad elegida ya no existe en este stream; "
                "se reproduce con calidad automática."
            )

    lineas = [header, "#EXT-X-INDEPENDENT-SEGMENTS"]

    if elegida is None:
        # Sin fijar: todas las variantes con **sus** grupos de audio. Un
        # master al que se le quitan los `EXT-X-MEDIA` deja al reproductor
        # sin audio en las variantes que los usaban.
        # Cada grupo se escribe **una vez**, aunque lo usen varias
        # variantes: duplicarlo produce un manifest con pistas repetidas.
        emitidos: set[tuple[str, str]] = set()
        for variante in variantes:
            grupo = variante.metadata.get("audio_group") or ""
            grupo_subs = variante.metadata.get("subtitles_group") or ""
            for kind, identificador in (("AUDIO", grupo), ("SUBTITLES", grupo_subs)):
                clave = (kind, identificador)
                if identificador and clave not in emitidos:
                    emitidos.add(clave)
                    lineas += _group_entries(caps, identificador, kind)
            lineas.append(
                _stream_line(variante, grupo, subtitles_group=grupo_subs)
            )
            if variante.uri:
                lineas.append(variante.uri)
        return "\n".join(lineas) + "\n"

    # Con calidad fijada el audio se declara en un grupo propio: así el
    # índice de la pista elegida es 1 y no hay ambigüedad con el resto.
    elegida_audio = _track_of(caps.audio_tracks, selection.audio_track_id)
    if elegida_audio is not None:
        grupo_audio = AUDIO_GROUP
        lineas += _group_entries(caps, elegida_audio.group_id, "AUDIO",
                                grupo_destino=grupo_audio,
                por_defecto=elegida_audio.id)
    else:
        # Nadie ha elegido audio: se conserva el grupo del proveedor y sus
        # pistas tal cual, con su DEFAULT original.
        grupo_audio = elegida.metadata.get("audio_group") or ""
        lineas += _group_entries(caps, grupo_audio, "AUDIO")

    elegida_sub = _track_of(caps.subtitle_tracks, selection.subtitle_track_id)
    grupo_subs = ""
    if elegida_sub is not None and selection.subtitles_enabled:
        grupo_subs = SUBTITLE_GROUP
        lineas += _group_entries(caps, elegida_sub.group_id, "SUBTITLES",
                                grupo_destino=grupo_subs,
                                por_defecto=elegida_sub.id)

    lineas.append(_stream_line(elegida, grupo_audio, subtitles_group=grupo_subs))
    lineas.append(elegida.uri or "")
    return "\n".join(lineas) + "\n"


def _track_of(pistas: list[MediaTrack], track_id: str | None) -> MediaTrack | None:
    if not track_id:
        return None
    for pista in pistas:
        if pista.id == track_id:
            return pista
    return None


def _group_entries(
    caps: MediaCapabilities,
    group_id: str,
    kind: str,
    *,
    grupo_destino: str = "",
    por_defecto: str = "",
) -> list[str]:
    """Reescribe las entradas `EXT-X-MEDIA` de un grupo, en orden.

    Se conservan `NAME`, `LANGUAGE`, `AUTOSELECT`, `FORCED`,
    `CHARACTERISTICS` y `CHANNELS` del proveedor: el reproductor sigue
    viendo lo mismo que antes, sólo que fijado. `DEFAULT=YES` va **sólo**
    en la pista elegida (`por_defecto`), o en la que el proveedor ya
    marcaba como tal si el usuario no eligió ninguna.
    """
    if not group_id:
        return []
    pistas = caps.audio_tracks if kind == "AUDIO" else caps.subtitle_tracks
    destino = grupo_destino or group_id
    lineas: list[str] = []
    for pista in pistas:
        if pista.group_id != group_id:
            continue
        elegida = (
            pista.id == por_defecto if por_defecto else bool(pista.is_default)
        )
        lineas.append(_media_line(pista, kind, destino, por_defecto=elegida))
    return lineas




def _media_line(
    pista: MediaTrack,
    kind: str,
    group_id: str,
    *,
    por_defecto: bool = False,
) -> str:
    """Una línea `EXT-X-MEDIA` con los atributos del original conservados."""
    campos: list[str] = [
        f"TYPE={kind}",
        f"GROUP-ID={_quote(group_id)}",
    ]
    nombre = pista.label or (
        audio_label(pista, 1) if kind == "AUDIO" else subtitle_label(pista, 1)
    )
    if nombre:
        campos.append(f"NAME={_quote(nombre)}")
    if pista.language:
        campos.append(f"LANGUAGE={_quote(pista.language)}")
    # `AUTOSELECT=NO` junto a `DEFAULT=YES` es una combinación inválida en
    # HLS, así que la pista elegida se declara siempre como default y
    # autoseleccionable: es justo lo que se está pidiendo.
    campos.append("DEFAULT=YES" if por_defecto else "DEFAULT=NO")
    campos.append("AUTOSELECT=YES" if pista.is_auto_select else "AUTOSELECT=NO")
    if pista.is_forced:
        campos.append("FORCED=YES")
    if pista.role and kind == "SUBTITLES":
        campos.append(f"CHARACTERISTICS={_quote(pista.role)}")
    if pista.channels:
        campos.append(f"CHANNELS={_quote(pista.channels)}")
    if pista.uri:
        campos.append(f"URI={_quote(pista.uri)}")
    return "#EXT-X-MEDIA:" + ",".join(campos)


def _stream_line(
    variante: MediaTrack,
    audio_group: str,
    subtitles_group: str = "",
) -> str:
    """`EXT-X-STREAM-INF` de una variante, con sus grupos reales.

    Sólo se escribe un atributo de grupo si ese grupo existe en el
    manifiesto que se está sirviendo: un `AUDIO="grupo-que-no-existe"` deja
    al reproductor **sin audio**, no con otro audio.
    """
    campos: list[str] = []
    if variante.bitrate:
        campos.append(f"BANDWIDTH={int(variante.bitrate)}")
    if variante.width and variante.height:
        campos.append(f"RESOLUTION={int(variante.width)}x{int(variante.height)}")
    if variante.fps:
        campos.append(f"FRAME-RATE={float(variante.fps):.3f}")
    codecs = variante.metadata.get("codecs")
    if isinstance(codecs, list) and codecs:
        campos.append(f"CODECS={_quote(','.join(str(c) for c in codecs))}")
    if audio_group:
        campos.append(f"AUDIO={_quote(audio_group)}")
    if subtitles_group:
        campos.append(f"SUBTITLES={_quote(subtitles_group)}")
    return "#EXT-X-STREAM-INF:" + ",".join(campos)


# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------


@dataclass
class _Config:
    body: str
    token: str
    server: http.server.ThreadingHTTPServer
    thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "theTVVIEW"
    sys_version = ""

    def log_message(self, *args) -> None:
        # El stderr del proceso es la TUI: el proxy no escribe nada.
        return

    def do_GET(self) -> None:  # noqa: N802 - firma de BaseHTTPRequestHandler
        self._serve()

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve()

    def _serve(self) -> None:
        cfg = getattr(self.server, "pin_config", None)
        if cfg is None:  # pragma: no cover - defensivo
            self._send(404, b"", "text/plain")
            return
        ruta = urlsplit(self.path).path
        permitidas = [f"/{cfg.token}/{_MASTER_NAME}"]
        permitidas += [f"/{cfg.token}/{nombre}" for nombre in _ALT_NAMES]
        # Allowlist estricta: sólo el master con el token delante. Los
        # segmentos no se sirven: el reproductor los pide al proveedor.
        if ruta not in permitidas:
            self._send(404, b"", "text/plain")
            return
        self._send(200, cfg.body.encode("utf-8"), "application/vnd.apple.mpegurl")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # El reproductor cerró antes de terminar: no es un error.
            return


class PinProxy:
    """Servidor loopback de un master fijado, apagable.

    Uso típico, dentro de un ``try/finally`` (como pide el plan)::

        proxy = PinProxy.start(caps, selection)
        try:
            if proxy.url:
                channel = replace(channel, url=proxy.url)
                ...
        finally:
            proxy.stop()
    """

    def __init__(self, config: _Config) -> None:
        self._config = config
        atexit.register(self.stop)

    # -- construcción -------------------------------------------------------

    @classmethod
    def start(
        cls,
        caps: MediaCapabilities,
        selection: PlaybackSelection,
        *,
        host: str = LOOPBACK,
    ) -> "PinProxy":
        """Arranca el servidor. Lanza :class:`PinProxyError` si no puede."""
        if host != LOOPBACK:
            raise PinProxyError(
                "El proxy de calidad sólo puede escuchar en loopback."
            )
        body = rewrite_master(caps, selection)
        token = secrets.token_hex(TOKEN_BYTES)

        try:
            server = http.server.ThreadingHTTPServer((host, 0), _Handler)
        except OSError as exc:
            raise PinProxyError(
                "No se pudo abrir el puerto local para fijar la calidad."
            ) from exc

        server.daemon_threads = True
        config = _Config(body=body, token=token, server=server)
        server.pin_config = config  # type: ignore[attr-defined]

        thread = threading.Thread(
            target=server.serve_forever, name="pin-proxy", daemon=True
        )
        config.thread = thread
        thread.start()
        return cls(config)

    # -- estado -------------------------------------------------------------

    @property
    def url(self) -> str:
        """URL del master fijado (la que irá detrás de ``--``)."""
        return f"http://{LOOPBACK}:{self.port}/{self.token}/{_MASTER_NAME}"

    @property
    def token(self) -> str:
        return self._config.token

    @property
    def body(self) -> str:
        return self._config.body

    @property
    def port(self) -> int:
        return self._config.port

    @property
    def is_running(self) -> bool:
        thread = self._config.thread
        return bool(thread and thread.is_alive())

    def routes(self) -> tuple[str, ...]:
        """Rutas que el proxy responde. Para tests y para documentar."""
        return tuple(f"/{self._config.token}/{n}" for n in (_MASTER_NAME,) + _ALT_NAMES)

    # -- apagado ------------------------------------------------------------

    def stop(self) -> None:
        """Para el servidor. Idempotente y sin lanzar (SDD §29/§51)."""
        cfg = self._config
        try:
            cfg.server.shutdown()
        except Exception:  # noqa: BLE001 - ya estaba parado
            pass
        try:
            cfg.server.server_close()
        except Exception:  # noqa: BLE001
            pass
        thread = cfg.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        cfg.thread = None
        try:
            atexit.unregister(self.stop)
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "PinProxy":
        return self

    def __exit__(self, *_exc) -> bool:
        self.stop()
        return False


def start_pin_proxy(
    caps: MediaCapabilities | None,
    selection: PlaybackSelection | None,
) -> PinProxy | None:
    """Atajo: arranca el proxy y devuelve ``None`` si no se puede.

    Que devuelva ``None`` es una señal, no un fallo: el llamante cae a los
    argumentos del reproductor y lo explica en un modal.
    """
    if caps is None or selection is None or selection.auto_quality:
        return None
    if not selection.video_track_id:
        return None
    try:
        return PinProxy.start(caps, selection)
    except PinProxyError:
        return None