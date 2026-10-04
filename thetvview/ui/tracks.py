"""Estado de pistas del canal abierto: sondeo, selección y reproducción.

Este módulo es el pegamento entre el dominio (:mod:`thetvview.tracks`,
:mod:`thetvview.streams`) y la TUI. Vive en ``ui/`` porque su trabajo es
justo el de mantener **estado de sesión** y porque **no** dibuja nada: la
pantalla y el :class:`~thetvview.ui.app.App` preguntan, nunca pintan desde
aquí.

Dos reglas que condicionan el diseño:

1. **El sondeo no bloquea.** Arranca un hilo daemon; mientras no llegue, el
   usuario ve el canal exactamente igual que antes y la pantalla de opciones
   simplemente no aparece. Nunca se espera a la red para reproducir.
2. **La UI no inventa.** Todo lo que se ofrece sale de
   :meth:`thetvview.tracks.manager.TrackManager.options`, que a su vez sale
   del manifiesto. Si no hay pistas, no hay menú.
"""

from __future__ import annotations

import threading
from typing import Any

from ..models import Channel
from ..player.capabilities import KIND_AUDIO, KIND_QUALITY, KIND_SUBTITLES, supports
from ..streams.headers import strip_ffmpeg
from ..streams.probe import ProbeResult, TrackProbe
from ..tracks.labels import audio_label, subtitle_label, video_label
from ..tracks.manager import TrackManager
from ..tracks.models import MediaCapabilities, PlaybackSelection
from ..tracks.cli import get_session_overrides
from ..tracks.prefs import (
    TrackPreferences,
    load_scopes,
    preferences_from_selection,
    remember_for_channel,
    remember_for_provider,
    resolve_preferences,
)

__all__ = ["TrackSession", "SECTION_LABELS", "degraded_outcome"]

#: Títulos de sección del selector, en el orden en que se pintan.
SECTION_LABELS: dict[str, str] = {
    KIND_AUDIO: "Audio",
    KIND_SUBTITLES: "Subtítulos",
    KIND_QUALITY: "Calidad",
}


class TrackSession:
    """Pistas del canal abierto, con sondeo en segundo plano.

    Uso en la TUI::

        sesion = TrackSession(app, channel, source)
        sesion.start()                  # no bloquea
        ...
        if sesion.has_menu: push(TrackOptionsScreen(app, channel, sesion, player))
    """

    def __init__(self, app, channel: Channel, source: str | None = None) -> None:  # noqa: ANN001
        self.app = app
        self.channel = channel
        self.source = source
        self.manager = TrackManager(None, self._preferences())
        self.probe: TrackProbe | None = None
        self.result: ProbeResult | None = None
        self.ipc_path: str | None = None
        #: Razón redactada por la que el sondeo no pudo averiguar nada.
        self.reason: str = ""
        #: Avisos de reconciliación pendientes de que la UI los muestre
        #: (§28). Los guarda el hilo del sondeo y los drena el hilo de la
        #: interfaz: un aviso nunca se enseña desde un hilo secundario.
        self._avisos: list[str] = []
        self._lock = threading.Lock()
        self._starting_url: str | None = None
        #: Puesto por `App._explain_degraded` para no repetir el mismo
        #: explicación una y otra vez.
        self.degraded_shown = False
        #: True cuando el usuario ya ha pasado por el selector de pistas y ha
        #: confirmado. Evita preguntarle dos veces lo mismo.
        self.chosen = False

    # -- preferencias -------------------------------------------------------

    def _preferences(self) -> TrackPreferences:
        """Preferencias efectivas: guardadas + overrides de esta sesión.

        Los overrides de la línea de órdenes (``--audio es``) van **por
        encima** de todo, incluidos los guardados: son la intención más
        reciente del usuario y no tocan disco (SDD §43).
        """
        prefs_manager = getattr(self.app, "prefs", None)
        prefs = prefs_manager.load() if prefs_manager is not None else None
        scopes = load_scopes(prefs_manager) if prefs_manager is not None else {}
        resueltas = resolve_preferences(
            self.channel, self.source, prefs=prefs, scopes=scopes
        )
        overrides = get_session_overrides()
        if overrides is not None:
            resueltas = overrides.merged_with(resueltas)
        return resueltas

    def remember(self, *, for_provider: bool = False) -> bool:
        """Guarda la selección para este canal (o para toda la fuente).

        Devuelve False si no hay dónde guardar; la UI lo explica en un modal.
        """
        prefs_manager = getattr(self.app, "prefs", None)
        if prefs_manager is None:
            return False
        caps = self.capabilities
        prefs = preferences_from_selection(caps, self.selection) if caps else None
        if prefs is None:
            return False
        try:
            if for_provider:
                if not self.source:
                    return False
                return remember_for_provider(prefs_manager, self.source, prefs) is not None
            return remember_for_channel(prefs_manager, self.channel, prefs) is not None
        except OSError:
            return False

    # -- estado -------------------------------------------------------------

    @property
    def capabilities(self) -> MediaCapabilities | None:
        return self.manager.capabilities

    @property
    def selection(self) -> PlaybackSelection:
        return self.manager.selection

    @property
    def pending(self) -> bool:
        """True mientras el sondeo sigue en marcha."""
        probe = self.probe
        return bool(probe is not None and probe.is_running)

    @property
    def wait_settles(self) -> bool:
        """True si este canal puede tener manifiesto y merece la pena esperar.

        Un ``.ts`` es MPEG-TS: pista única por definición, no hay nada que
        descubrir, así que **no** se espera ni un milisegundo. Sólo cuando la
        dirección apunta a un manifiesto (o el proveedor ya nos devolvió uno
        para ese mismo stream) tiene sentido Spendir unos segundos.

        También entra el caso "el sondeo ya terminó": si respondió, la espera
        pendiente es cero, pero la propiedad tiene que ser ``True`` para que
        el flujo pueda distinguir "no hay nada que descubrir" de "todavía no
        se sabe". Sin esto, el orden pistas → reproductor se rompía en
        cuanto el sondeo acababa justo después de la primera espera: el
        selector de pistas se saltaba y el usuario tenía que elegir
        reproductor, ver las pistas y elegir reproductor otra vez.
        """
        if self.answered:
            return True
        return _url_may_be_manifest(self._probe_url() or "")

    @property
    def answered(self) -> bool:
        """True si el sondeo ya terminó (haya resultado o no)."""
        return self.result is not None

    def options(self, player_name: str | None = None) -> Any:
        """Opciones visibles para `player_name`.

        El filtro no es "qué opciones acepta el argv del reproductor", sino
        "qué opciones se pueden **aplicar** de verdad" (SDD §46):

        - audio y subtítulos: si el reproductor los acepta por argv;
        - calidad: siempre, porque se aplica por el proxy de pinning. En
          DASH **no** se ofrece, porque no hay mecanismo fiable de fijar una
          ``Representation`` (fuera de alcance explícito del plan, §7).
        """
        caps = self.capabilities

        def permitido(kind: str) -> bool:
            if kind == KIND_QUALITY:
                return caps is not None and caps.protocol == "hls"
            return supports(player_name, kind) if player_name else True

        return self.manager.options(player_supports=permitido)

    def has_menu(self, player_name: str | None = None) -> bool:
        """¿Merece la pena abrir el selector? (§33/§34: menos de 2, no)."""
        return self.options(player_name).has_menu

    def kinds(self, player_name: str | None = None) -> list[str]:
        return self.options(player_name).selectable_kinds

    # -- sondeo -------------------------------------------------------------

    def start(self) -> None:
        """Arranca el sondeo en segundo plano. Idempotente y no bloqueante."""
        with self._lock:
            if self.probe is not None and self.probe.is_running:
                return
            url = self._probe_url()
            if url is None:
                return
            self._starting_url = url
            probe = TrackProbe(
                self.channel,
                allow_private=self._allow_private(),
                cache_dir=None if self._cache_dir_forbidden() else _default_cache_dir(),
            )
            probe.channel = _channel_with_url(self.channel, url)
            self.probe = probe
            self.result = None
        probe.start(self._on_ready)

    def recheck(self) -> None:
        """Vuelve a sondear ignorando la caché (tecla ``i`` del plan F6).

        Se usa para notar que un master en vivo ha cambiado: pistas nuevas o
        desaparecidas (AC-11). El TTL corto de la caché (60 s) se respeta
        igualmente, salvo que el usuario pida freshness en su propio canal.
        """
        with self._lock:
            probe = self.probe
            url = self._probe_url()
            if probe is not None and url is not None:
                self._starting_url = url
                probe.channel = _channel_with_url(self.channel, url)
                probe.use_cache = False
                self.result = None
        if probe is None:
            self.start()
            return
        # El sondeo va en un hilo daemon, como el primero: `i` no puede
        # parar la interfaz mientras el proveedor contesta.
        probe.start(self._on_ready)

    def take_avisos(self) -> list[str]:
        """Saca y devuelve los avisos pendientes (sólo hilo de la UI).

        Los avisos los produce el hilo del sondeo porque ése es quien sabe
        qué ha cambiado; enseñarlos es cosa de la interfaz. Vaciar la lista
        aquí es lo que evita que el mismo aviso aparezca dos veces.
        """
        with self._lock:
            avisos = list(self._avisos)
            self._avisos.clear()
        return avisos

    def stop(self) -> None:
        probe = self.probe
        if probe is not None:
            probe.stop()

    def _on_ready(self, result: ProbeResult) -> None:
        """Callback del hilo: sólo guarda el resultado. No toca curses."""
        self._apply(result)

    def _apply(self, result: ProbeResult) -> None:
        with self._lock:
            self.result = result
            self.reason = result.reason or ""
        avisos = self.manager.on_tracks_changed(result.capabilities)
        if avisos:
            with self._lock:
                self._avisos.extend(avisos)

    # -- reproducción -------------------------------------------------------

    def select(self, kind: str, choice_id: str | None, *, enabled: bool | None = None):
        """Aplica una elección validada. Lanza SelectTrackError si no existe."""
        return self.manager.select(kind, choice_id, enabled=enabled)

    def apply_hot_change(self, kind: str, player_name: str | None) -> tuple[bool, str]:
        """Intenta aplicar un cambio **en caliente**.

        Devuelve ``(aplicado, motivo)``. Con mpv va por el IPC; con los otros
        dos no se puede y se devuelve el motivo ya redactado para el modal
        (SDD §29, AC-02).
        """
        if not player_name:
            return False, "No se sabe con qué reproductor se está viendo."
        from ..player.capabilities import hot_control
        from ..player.mpv_ipc import MpvIpc, MpvIpcError
        from ..player.track_args import audio_index, subtitle_index

        caps = self.capabilities
        selection = self.selection
        if caps is None:
            return False, "No se-analyses aún las pistas de este canal."

        if not hot_control(player_name):
            return False, (
                f"{player_name.upper()} no puede cambiar de pista en caliente. "
                "La elección se aplicará al reabrir el canal."
            )
        if not self.ipc_path:
            return False, (
                "Este canal se abrió sin canal de control, así que el cambio "
                "no se puede aplicar en caliente: reabre el canal."
            )

        ipc = MpvIpc(self.ipc_path)
        try:
            if kind == KIND_AUDIO:
                indice = audio_index(caps, selection.audio_track_id)
                if indice is None:
                    return False, "No hay ninguna pista de audio que elegir aquí."
                ipc.set_audio_index(indice)
            elif kind == KIND_SUBTITLES:
                if not selection.subtitles_enabled or not selection.subtitle_track_id:
                    ipc.set_subtitle_index(None)
                else:
                    indice = subtitle_index(caps, selection.subtitle_track_id)
                    if indice is None:
                        return False, "Ese subtítulo ya no está en el stream."
                    ipc.set_subtitle_index(indice)
            else:
                return False, (
                    "La calidad no se puede cambiar con el canal en marcha: "
                    "reabre el canal para aplicarla."
                )
        except MpvIpcError as exc:
            return False, exc.message
        return True, ""

    # -- etiquetas para la tarjeta ------------------------------------------

    def summary(self) -> dict[str, str]:
        """Valores legibles de audio/subtítulo/calidad para la UI."""
        caps = self.capabilities
        out = {"audio": "", "subtitles": "", "quality": "", "resolution": "",
               "codec": ""}
        if caps is None:
            return out
        audio = caps.audio_by_id(self.selection.audio_track_id)
        if audio is None and len(caps.audio_tracks) == 1:
            audio = caps.audio_tracks[0]
        if audio is not None:
            out["audio"] = audio_label(audio, 1)
        pista_sub = caps.subtitle_by_id(self.selection.subtitle_track_id)
        if pista_sub is not None:
            out["subtitles"] = subtitle_label(pista_sub, 1)
        variante = caps.variant_by_id(self.selection.video_track_id)
        if variante is None and len(caps.video_variants) == 1:
            variante = caps.video_variants[0]
        if variante is not None:
            out["quality"] = video_label(variante)
            if variante.height:
                out["resolution"] = f"{variante.width or '?'}x{variante.height}"
            if variante.codec:
                out["codec"] = variante.codec.split(".")[0].upper()
        return out

    def degraded_outcome(self) -> tuple[str | None, bool]:
        """Motivo a explicar y si es un **fallo**: ``(texto, es_fallo)``.

        Distingue "el proveedor no expone pistas" de "no se pudo averiguar"
        (SDD §48), y además dice si cada uno merece un modal o sólo la barra
        de estado. No es un detalle de forma: un modal es una interrupción, y
        gastarla en "no hay nada que elegir" hace creer al usuario que el
        canal está roto cuando se reproduce sin problema.

        ``es_fallo=True`` sólo cuando no se pudo averiguar nada.
        """
        if self.pending:
            return None, False
        return degraded_outcome(self.result)

    def degraded_message(self) -> str | None:
        """Sólo el texto, para quien no necesite saber si es un fallo."""
        return self.degraded_outcome()[0]

    # -- internos -----------------------------------------------------------

    def _probe_url(self) -> str | None:  # noqa: D401
        """URL a sondear: la resuelta, nunca la referencia opaca.

        Las referencias Xtream (``xtream-ts://…``) no sirven para pedir un
        manifiesto: hay que resolverse antes, y sólo aquí.
        """
        raw = strip_ffmpeg(getattr(self.channel, "url", "") or "")
        if raw.lower().startswith(("http://", "https://")):
            return raw
        try:
            from ..stream_ref import resolve_channel_url

            return strip_ffmpeg(
                resolve_channel_url(self.channel.url, getattr(self.app, "playlists", None))
            )
        except Exception:
            return None

    def _allow_private(self) -> bool:
        getter = getattr(self.app, "_allow_private_for", None)
        if not callable(getter) or not self.source:
            return False
        try:
            return bool(getter(self.source))
        except Exception:
            return False

    def _cache_dir_forbidden(self) -> bool:
        """En tests o sin ``data/`` escribible, la caché en disco se salta."""
        return bool(getattr(self.app, "_tracks_no_disk_cache", False))


def _channel_with_url(channel: Channel, url: str) -> Channel:
    from dataclasses import replace

    if url == channel.url:
        return channel
    return replace(channel, url=url)


def _default_cache_dir():
    from ..streams.probe import TRACK_CACHE_DIR

    return TRACK_CACHE_DIR


def _url_may_be_manifest(url: str) -> bool:
    """True si la dirección **podría** servir un manifiesto de pistas.

    Heurística conservadora y explicable: se miran la extensión y el
    parámetro `type`/`path` que usan los paneles para pedir una lista en vez
    de un stream. Un ``.ts`` no entra: MPEG-TS es pista única y no hay nada
    que preguntar (SDD §32).
    """
    if not url:
        return False
    baja = url.lower()
    if baja.endswith((".ts", ".m2ts", ".mpegts", ".mpg", ".mpeg")):
        # Salvo que pidan explícitamente un manifiesto.
        return any(marca in baja for marca in ("type=m3u", ".m3u8", "manifest", ".mpd"))
    if any(marca in baja for marca in (".m3u8", ".m3u", ".mpd", "manifest", "master")):
        return True
    # Panel Xtream: `...&action=get_live_streams&type=m3u8` es una lista.
    return "type=m3u" in baja or "type=m3u8" in baja or "type=mpd" in baja


def degraded_outcome(result) -> tuple[str | None, bool]:  # noqa: ANN001
    """Qué explicar tras un sondeo, y si es un **fallo**: ``(texto, es_fallo)``.

    Función pura y suelta a propósito: la decisión de si algo merece un modal
    (o sólo la barra de estado) no debe depender de que haya una sesión viva,
    y así los tests pueden ejercitarla sin montar una.

    La distinción que importa:

    - **fallo** (modal): no se pudo averiguar nada — 403, 404, timeout, o un
      200 que no es un manifiesto. El canal suena igual, así que sin explicar
      algo el usuario no sabría si iba a venir con pistas.
    - **nada que ofrecer** (barra de estado): el manifiesto se leyó bien y
      declara una sola pista, o es un directo. El canal se reproduce
      perfectamente; interrumpir con un modal haría creer que algo va mal.
    """
    if result is None:
        return None, False
    if result.capabilities is None:
        return (
            result.reason or (
                "No se pudo consultar el manifiesto del canal. "
                "El canal se reproduce con normalidad."
            ),
            True,
        )
    caps = result.capabilities
    if caps.has_any_choice:
        return None, False
    from ..tracks.models import (
        DEGRADED_NOT_EXPOSED,
        DEGRADED_PROTOCOL,
        DEGRADED_UNKNOWN,
    )

    if caps.degraded_reason == DEGRADED_PROTOCOL:
        return (
            "Este canal es un directo con una sola pista de audio y vídeo: "
            "no hay calidad, audio ni subtítulos que elegir.",
            False,
        )
    if caps.degraded_reason == DEGRADED_UNKNOWN:
        return (
            "El proveedor no devolvió un manifiesto de pistas para este "
            "canal. Se reproduce con normalidad.",
            True,
        )
    if caps.degraded_reason == DEGRADED_NOT_EXPOSED:
        return (
            "El manifiesto del canal declara una sola pista: no hay nada que "
            "elegir. Se reproduce con normalidad.",
            False,
        )
    return None, False
