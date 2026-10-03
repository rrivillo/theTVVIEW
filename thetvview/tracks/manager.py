"""Motor de selección de pistas (plan F3, SDD §12/§13/§22/§28/§33/§34/§49).

Aquí vive el corazón del plan: **qué se le enseña al usuario y por qué**.

Cadena de preferencia (§12/§13/§40), siempre en este orden:

1. idioma **exacto** (``es`` ↔ ``es-ES``);
2. idioma **base** (``es`` encaja con ``es-MX``);
3. coincidencia por **etiqueta** (``NAME`` del proveedor);
4. la pista marcada ``DEFAULT`` por el proveedor;
5. la primera disponible.

Y dos reglas que nunca se rompen:

- **Nunca se selecciona una pista que no existe.** Pedir una pista
  inexistente devuelve un error controlado —"La pista seleccionada ya no
  está disponible."— y no una excepción (SDD §39 caso 7).
- **Un track único no es una opción.** Con menos de dos alternativas el
  selector no se abre: un stream simple se reproduce exactamente igual que
  antes (SDD §32/§33/§34, AC-09).

La política de visibilidad se expone como datos (:class:`TrackOptions`), no
como una función que la UI interprete: la UI pinta lo que dice aquí, y aquí
no dice nada que el proveedor no haya declarado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .labels import (
    AUDIO_AUTO,
    SUBTITLES_OFF,
    audio_label,
    subtitle_label,
    video_label,
)
from .language import matches_language, normalize_language, same_language
from .models import (
    QUICK_AUTO,
    MediaCapabilities,
    MediaTrack,
    PlaybackSelection,
    puede_fijar_calidad,
)

__all__ = [
    "TrackChoice",
    "TrackOptions",
    "TrackManager",
    "SelectTrackError",
    "TRACK_GONE_MESSAGE",
    "AUDIO_AUTO",
    "SUBTITLES_OFF",
    "QUICK_AUTO",
    "build_options",
    "apply_preferences",
    "reconcile_selection",
    "apply_subtitle_preferences",
    "apply_quality_preferences",
    "selection_for",
    "resolve_choice",
]

#: Mensaje único para "pedí una pista que ya no está" (SDD §39 caso 7). La
#: UI lo enseña en un modal; los tests lo comprueban tal cual para que no
#: haya tres redacciones distintas del mismo problema.
TRACK_GONE_MESSAGE: str = "La pista seleccionada ya no está disponible."


class SelectTrackError(Exception):
    """Petición imposible de atender (SDD §29/§39).

    Es el error **controlado** del caso 7 de §39: la UI lo enseña y sigue
    funcionando; nunca se propaga como excepción sin manejar.
    """

    def __init__(self, message: str = TRACK_GONE_MESSAGE) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class TrackChoice:
    """Una opción real del menú, con su etiqueta ya lista para pintar.

    ``id`` es el id **nuestro** de pista (o :data:`QUICK_AUTO` para
    "Automática"); ``kind`` es ``"audio"``, ``"subtitles"`` o ``"video"``,
    para que la UI no tenga que deducirlo.
    """

    kind: str
    id: str
    label: str
    is_selected: bool = False
    #: Detalle secundario opcional (codec, canales, idioma original…).
    detail: str | None = None


@dataclass
class TrackOptions:
    """Qué se puede cambiar, agrupado por tipo.

    Una lista vacía significa **"no hay menú"**: no se abre ninguna pantalla
    para ese tipo (SDD §33/§34). ``info_only`` guarda el dato que se
    muestra como información cuando no hay nada que elegir, porque informar
    y ofrecer elegir son cosas distintas.
    """

    audio: list[TrackChoice] = field(default_factory=list)
    subtitles: list[TrackChoice] = field(default_factory=list)
    quality: list[TrackChoice] = field(default_factory=list)
    #: Valor único a mostrar como información (audio/vídeo con una sola vía).
    audio_info: str | None = None
    quality_info: str | None = None
    #: Motivo de degradación heredado de las capacidades, si lo hubo.
    degraded_reason: str = ""
    note: str | None = None

    @property
    def audio_selectable(self) -> bool:
        return len(self.audio) >= 2

    @property
    def subtitles_selectable(self) -> bool:
        return len(self.subtitles) >= 2

    @property
    def quality_selectable(self) -> bool:
        return len(self.quality) >= 2

    @property
    def has_menu(self) -> bool:
        """True si hay alguna sección con dos o más opciones."""
        return (
            self.audio_selectable
            or self.subtitles_selectable
            or self.quality_selectable
        )

    @property
    def selectable_kinds(self) -> list[str]:
        """Tipos con menú, en orden estable: audio, subtitles, quality."""
        kinds: list[str] = []
        if self.audio_selectable:
            kinds.append("audio")
        if self.subtitles_selectable:
            kinds.append("subtitles")
        if self.quality_selectable:
            kinds.append("quality")
        return kinds

    def choices_for(self, kind: str) -> list[TrackChoice]:
        if kind == "audio":
            return self.audio
        if kind == "subtitles":
            return self.subtitles
        return self.quality

    def selected_id(self, kind: str) -> str | None:
        for choice in self.choices_for(kind):
            if choice.is_selected:
                return choice.id
        return None

    def replace(self, kind: str, choices: Sequence[TrackChoice]) -> "TrackOptions":
        """Copia con una sección sustituida (para la reconciliación)."""
        nuevo = TrackOptions(
            audio=list(self.audio),
            subtitles=list(self.subtitles),
            quality=list(self.quality),
            audio_info=self.audio_info,
            quality_info=self.quality_info,
            degraded_reason=self.degraded_reason,
            note=self.note,
        )
        if kind == "audio":
            nuevo.audio = list(choices)
        elif kind == "subtitles":
            nuevo.subtitles = list(choices)
        else:
            nuevo.quality = list(choices)
        return nuevo


# ---------------------------------------------------------------------------
# Construcción de las opciones (lo que la UI pinta)
# ---------------------------------------------------------------------------


def build_options(
    caps: MediaCapabilities | None,
    selection=None,
    *,
    player_supports: Callable[[str], bool] | None = None,
) -> TrackOptions:
    """Construye el menú visible a partir de las capacidades y la selección.

    ``player_supports`` es un filtro opcional del backend (``player.py`` lo
    pasa en F5): si dice que el reproductor no puede fijar calidad, esa
    sección **no** se ofrece. Es el sitio donde se cumple "no mostrar
    opciones que el backend no puede aplicar" (SDD §46, §51).

    Sólo aparecen las secciones con ≥2 opciones; el valor único se deja en
    ``audio_info``/``quality_info`` para mostrarlo como dato (SDD §33/§34).
    """
    supports = player_supports or (lambda _kind: True)
    if caps is None:
        return TrackOptions()

    audio = _audio_choices(caps, selection, supports)
    subtitles = _subtitle_choices(caps, selection, supports)
    quality = _quality_choices(caps, selection, supports)

    return TrackOptions(
        audio=audio if len(audio) >= 2 else [],
        subtitles=subtitles if len(subtitles) >= 2 else [],
        quality=quality if len(quality) >= 2 else [],
        audio_info=_single_audio_label(caps) if len(audio) < 2 else None,
        quality_info=_single_quality_label(caps) if len(quality) < 2 else None,
        degraded_reason=caps.degraded_reason,
        note=caps.note,
    )


def _audio_choices(caps: MediaCapabilities, selection, supports) -> list[TrackChoice]:
    if not supports("audio"):
        return []
    choices = [
        TrackChoice("audio", track.id, audio_label(track, index))
        for index, track in enumerate(caps.audio_tracks, start=1)
    ]
    if len(choices) < 2:
        # Una sola pista no es un menú: no se inserta ni el "Automático"
        # (SDD §33). Con dos o más, "Automático" significa "el DEFAULT del
        # stream", que es justo lo que se está viendo sin tocar nada.
        return _mark_selected(choices, selection, "audio")
    choices.insert(0, TrackChoice("audio", QUICK_AUTO, AUDIO_AUTO))
    return _mark_selected(choices, selection, "audio")


def _subtitle_choices(caps: MediaCapabilities, selection, supports) -> list[TrackChoice]:
    if not supports("subtitles"):
        return []
    if not caps.subtitle_tracks:
        return []
    choices = [TrackChoice("subtitles", QUICK_AUTO, SUBTITLES_OFF)]
    for index, track in enumerate(caps.subtitle_tracks, start=1):
        if not track.uri and not track.metadata.get("instream_id"):
            # Un subtítulo sin URI ni INSTREAM-ID no se puede pedir: no es
            # una opción, es un dato (AC-03: sólo los subtítulos anunciados
            # y realmente disponibles).
            continue
        choices.append(
            TrackChoice("subtitles", track.id, subtitle_label(track, index))
        )
    return _mark_selected(choices, selection, "subtitles")


def _quality_choices(caps: MediaCapabilities, selection, supports) -> list[TrackChoice]:
    if not supports("quality"):
        return []
    # No basta con que el reproductor "sepa" de calidad: tiene que existir una
    # forma de *forzarla*. En HLS la hay (el proxy reescribe el master); en
    # DASH no, y ofrecer el menú ahí sería prometer algo que no ocurre: el
    # canal se quedaría en automatico y el usuario creería que eligió 720p.
    if not puede_fijar_calidad(caps):
        return []
    variants = caps.sorted_variants()
    if not variants:
        return []
    choices = [TrackChoice("quality", variant.id, video_label(variant)) for variant in variants]
    if len(choices) >= 2:
        # "Automática (ABR)" sólo tiene sentido si hay algo entre lo que
        # elegir. Con una sola variante el reproductor no puede "elegir" nada
        # y ofrecer un ABR sería mentir (SDD §14/§15/§34).
        choices.insert(0, TrackChoice("quality", QUICK_AUTO, "Automática (ABR)"))
    return _mark_selected(choices, selection, "quality")


def _mark_selected(
    choices: list[TrackChoice], selection, kind: str
) -> list[TrackChoice]:
    actual = _current_id(selection, kind)
    marcado = False
    salida: list[TrackChoice] = []
    for choice in choices:
        elegido = actual is not None and choice.id == actual
        if elegido:
            marcado = True
        salida.append(
            TrackChoice(
                kind=choice.kind,
                id=choice.id,
                label=choice.label,
                is_selected=elegido,
                detail=choice.detail,
            )
        )
    if not marcado and salida:
        # Sin selección explícita se marca la primera opción: la UI nunca
        # muestra un menú sin una marca que explique qué se va a aplicar.
        primero = salida[0]
        salida[0] = TrackChoice(
            kind=primero.kind,
            id=primero.id,
            label=primero.label,
            is_selected=True,
            detail=primero.detail,
        )
    return salida


def _current_id(selection, kind: str) -> str | None:
    if selection is None:
        return None
    if kind == "audio":
        return getattr(selection, "audio_track_id", None)
    if kind == "subtitles":
        return getattr(selection, "subtitle_track_id", None)
    return None if getattr(selection, "auto_quality", True) else getattr(
        selection, "video_track_id", None
    )


def _single_audio_label(caps: MediaCapabilities) -> str | None:
    if len(caps.audio_tracks) != 1:
        return None
    return audio_label(caps.audio_tracks[0], 1)


def _single_quality_label(caps: MediaCapabilities) -> str | None:
    if len(caps.video_variants) != 1:
        return None
    return video_label(caps.video_variants[0])


# ---------------------------------------------------------------------------
# Aplicación de preferencias (§12/§13/§40)
# ---------------------------------------------------------------------------


def _audio_match(
    preferred: str | None, tracks: Sequence[MediaTrack]
) -> MediaTrack | None:
    """Aplica la cadena exacto → base → etiqueta → DEFAULT → primera."""
    if not tracks:
        return None
    if not preferred:
        return None
    for track in tracks:  # 1. idioma exacto
        if same_language(preferred, track.language or track.original_language):
            return track
    for track in tracks:  # 2. idioma base
        if matches_language(
            preferred, track.language or track.original_language
        ):
            return track
    wanted = normalize_language(preferred)
    if wanted is not None and wanted.label:
        for track in tracks:  # 3. etiqueta del proveedor
            if track.label and track.label.strip().lower() == wanted.label.lower():
                return track
    return None  # el DEFAULT y la primera los aplica el llamante


def apply_preferences(
    caps: MediaCapabilities | None, prefs
) -> MediaTrack | None:
    """Devuelve la pista de audio que encaja con la preferencia.

    Cadena del SDD §12/§40: idioma exacto → idioma base → etiqueta →
    ``DEFAULT`` del manifiesto → primera disponible. **Nunca** devuelve una
    pista que no esté en `caps`.

    Args:
        caps: capacidades del stream (o None si el sondeo falló).
        prefs: preferencias ya resueltas por :mod:`thetvview.tracks.prefs`
            (cualquier objeto con ``preferred_audio_language``, o None).

    Returns:
        La pista elegida, o None si no hay ninguna que encaje.
    """
    if caps is None or not caps.audio_tracks:
        return None
    preferred = getattr(prefs, "preferred_audio_language", None)
    elegida = _audio_match(preferred, caps.audio_tracks)
    if elegida is not None:
        return elegida
    return caps.default_audio()  # DEFAULT → primera


def apply_subtitle_preferences(
    caps: MediaCapabilities | None, prefs
) -> MediaTrack | None:
    """Pista de subtítulo preferida, o None si están desactivados.

    No elige si ``subtitles_enabled`` es False: desactivar subtítulos es una
    decisión explícita (AC-04) y la preferencia no puede contradecirla.
    """
    if caps is None or not caps.subtitle_tracks:
        return None
    if prefs is not None and not getattr(prefs, "subtitles_enabled", False):
        return None
    preferred = getattr(prefs, "preferred_subtitle_language", None)
    elegida = _audio_match(preferred, caps.subtitle_tracks)
    if elegida is not None:
        return elegida
    if prefs is not None and not getattr(prefs, "subtitles_enabled", False):
        return None
    return caps.default_subtitle()


def apply_quality_preferences(
    caps: MediaCapabilities | None, prefs
) -> MediaTrack | None:
    """Variante preferida, o None si toca "Automática (ABR)".

    Sólo se fuerza si la preferencia es **concreta** y existe: con
    ``"auto"`` (o sin preferencia) no se fuerza nada, porque el ABR lo hace
    mejor el reproductor (SDD §14/§15).
    """
    if caps is None or not caps.video_variants:
        return None
    preferred = getattr(prefs, "preferred_quality", None)
    if not preferred or preferred == QUICK_AUTO:
        return None
    for variant in caps.video_variants:
        if variant.id == preferred:
            return variant
    texto = str(preferred).strip().lower().removesuffix("p")
    if not texto:
        return None
    for variant in caps.sorted_variants():
        if variant.height is not None and str(variant.height) == texto:
            return variant
    return None


def selection_for(
    caps: MediaCapabilities | None, prefs=None
) -> PlaybackSelection:
    """Construye la :class:`PlaybackSelection` inicial según preferencias.

    El principio que manda es **no tocar lo que el usuario no ha tocado**:

    - si no hay ninguna preferencia, la selección sale vacía y el stream se
      reproduce exactamente igual que antes de esta funcionalidad (SDD §32);
    - si la preferencia no encaja con ninguna pista, se cae al ``DEFAULT``
      del manifiesto (SDD §40) y se deja constancia con un id real;
    - la calidad sólo se fuerza si la preferencia es concreta y existe;
      ``"auto"`` significa dejar el ABR en manos del reproductor (§14/§15).
    """
    if caps is None:
        return PlaybackSelection()

    preferred_audio = _pref(prefs, "preferred_audio_language")
    audio = _audio_match(preferred_audio, caps.audio_tracks) if preferred_audio else None
    if preferred_audio and audio is None:
        # §40: la preferencia existe pero no está; cae al DEFAULT/first.
        audio = caps.default_audio()

    preferred_subs = _pref(prefs, "preferred_subtitle_language")
    subs_on = bool(prefs is not None and getattr(prefs, "subtitles_enabled", False))
    subtitle = None
    if subs_on:
        subtitle = _audio_match(preferred_subs, caps.subtitle_tracks) if preferred_subs else None
        if subtitle is None:
            subtitle = caps.default_subtitle()

    variant = apply_quality_preferences(caps, prefs)

    return PlaybackSelection(
        audio_track_id=audio.id if audio is not None else None,
        subtitle_track_id=subtitle.id if subtitle is not None else None,
        video_track_id=variant.id if variant is not None else None,
        subtitles_enabled=subtitle is not None,
        # Si la preferencia global pide subtítulos, es una decisión del
        # usuario aunque no venga de un menú de este canal.
        subtitles_decided=subs_on,
        auto_quality=variant is None,
    )


def _pref(prefs, name: str) -> str | None:
    value = getattr(prefs, name, None) if prefs is not None else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Validación y reconciliación
# ---------------------------------------------------------------------------


def resolve_choice(
    caps: MediaCapabilities | None,
    kind: str,
    choice_id: str | None,
    *,
    enabled: bool | None = None,
) -> PlaybackSelection:
    """Valida una elección hecha en la UI y devuelve la selección nueva.

    Es la puerta que impide las selecciones imposibles (SDD §39 caso 7):

    - ``kind="subtitles"`` con ``choice_id=None`` o :data:`QUICK_AUTO`
      significa "desactivados" (AC-04);
    - cualquier otro id tiene que existir en `caps`; si no,
      :class:`SelectTrackError` con :data:`TRACK_GONE_MESSAGE`.

    `enabled` permite activar/desactivar subtítulos sin cambiar de pista: es
    la vía del reproductor que no soporta "desactivar" por separado.
    """
    actual = PlaybackSelection()
    if kind == "audio":
        if choice_id in (None, QUICK_AUTO):
            actual.audio_track_id = None
            return actual
        pista = caps.audio_by_id(choice_id) if caps is not None else None
        if pista is None:
            raise SelectTrackError(TRACK_GONE_MESSAGE)
        actual.audio_track_id = pista.id
        return actual

    if kind == "subtitles":
        if enabled is False:
            return PlaybackSelection(subtitles_enabled=False, subtitles_decided=True)
        if choice_id in (None, QUICK_AUTO):
            return PlaybackSelection(subtitles_enabled=False, subtitles_decided=True)
        pista = caps.subtitle_by_id(choice_id) if caps is not None else None
        if pista is None:
            raise SelectTrackError(TRACK_GONE_MESSAGE)
        return PlaybackSelection(
            subtitle_track_id=pista.id,
            subtitles_enabled=True,
            subtitles_decided=True,
        )

    if kind == "quality":
        if choice_id in (None, QUICK_AUTO):
            return PlaybackSelection(auto_quality=True)
        variante = caps.variant_by_id(choice_id) if caps is not None else None
        if variante is None:
            raise SelectTrackError(TRACK_GONE_MESSAGE)
        return PlaybackSelection(video_track_id=variante.id, auto_quality=False)

    raise SelectTrackError("Ese tipo de pista no existe.")


def reconcile_selection(
    selection,
    caps: MediaCapabilities | None,
) -> tuple[PlaybackSelection, list[str]]:
    """Reconcilia una selección con unas capacidades nuevas (SDD §28, AC-11).

    Devuelve ``(seleción, avisos)``:

    - si la pista elegida sigue existiendo, se conserva intacta;
    - si ha desaparecido, se elige una alternativa **válida** (nunca un id
      colgando) y se devuelve un aviso para que la UI lo explique en un
      modal;
    - si lo que desaparece son las alternativas y sólo queda una pista, la
      selección se deja "sin fijar": el reproductor usa el DEFAULT y sigue
      igual que antes.

    Nunca lanza.
    """
    avisos: list[str] = []
    if caps is None:
        return PlaybackSelection(), avisos

    audio_id = getattr(selection, "audio_track_id", None)
    sub_id = getattr(selection, "subtitle_track_id", None)
    video_id = getattr(selection, "video_track_id", None)
    auto_quality = bool(getattr(selection, "auto_quality", True))
    sub_enabled = bool(getattr(selection, "subtitles_enabled", False))

    if audio_id and caps.audio_by_id(audio_id) is None:
        # §28 paso 4: "si desapareció, elegir una alternativa válida". La
        # alternativa es el audio por defecto del propio manifiesto, no una
        # pista inventada; si no hay ninguno, se deja sin fijar y el
        # reproductor usa su criterio.
        alternativa = caps.default_audio()
        etiqueta = _label_of(caps, audio_id)
        if alternativa is not None:
            avisos.append(
                f"El audio '{etiqueta}' ya no está en este stream; "
                f"se usa '{audio_label(alternativa, 1)}', el que marca el proveedor."
            )
            audio_id = alternativa.id
        else:
            avisos.append(
                f"El audio '{etiqueta}' ya no está en este stream; "
                "se usa el que elija el reproductor."
            )
            audio_id = None

    if sub_id and caps.subtitle_by_id(sub_id) is None:
        avisos.append(
            "Los subtítulos elegidos ya no están en el stream; "
            "se desactivan."
        )
        sub_id = None
        sub_enabled = False

    if video_id and caps.variant_by_id(video_id) is None:
        avisos.append(
            f"La calidad '{_label_of(caps, video_id)}' ya no está en el stream; "
            "se vuelve a calidad automática."
        )
        video_id = None
        auto_quality = True

    if video_id is not None:
        auto_quality = False

    return PlaybackSelection(
        audio_track_id=audio_id,
        subtitle_track_id=sub_id,
        video_track_id=video_id,
        subtitles_enabled=sub_enabled,
        subtitles_decided=bool(getattr(selection, "subtitles_decided", False)),
        auto_quality=auto_quality,
    ), avisos


def _label_of(caps: MediaCapabilities, track_id: str | None) -> str:
    pista = caps.track_by_id(track_id) if track_id else None
    if pista is None:
        return str(track_id)
    for etiqueta in (audio_label(pista, 1), subtitle_label(pista, 1), video_label(pista)):
        if etiqueta:
            return etiqueta
    return str(track_id)


# ---------------------------------------------------------------------------
# Gestor con estado
# ---------------------------------------------------------------------------


class TrackManager:
    """Estado vivo de las pistas de un canal (SDD §22/§28).

    Responsabilidades (y sólo estas):

    - guardar las capacidades actuales y las anteriores;
    - resolver la selección inicial a partir de las preferencias;
    - **avisar** cuando las pistas cambian y la selección deja de ser
      válida (:meth:`on_tracks_changed`);
    - exponer las opciones para la UI.

    No sabe nada de mpv, de curses ni de disco: recibe los callbacks.
    """

    def __init__(
        self,
        caps: MediaCapabilities | None = None,
        prefs=None,
    ) -> None:
        self.capabilities: MediaCapabilities | None = caps
        self.prefs = prefs
        self.selection = selection_for(caps, prefs)
        self.last_change: list[str] = []
        self._listeners: list[Callable[[MediaCapabilities | None, object, list[str]], None]] = []

    # -- suscripción --------------------------------------------------------

    def add_listener(
        self, listener: Callable[[MediaCapabilities | None, object, list[str]], None]
    ) -> None:
        """Registra un callback ``(caps, selección, avisos)``."""
        if callable(listener):
            self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    # -- estado -------------------------------------------------------------

    @property
    def has_tracks(self) -> bool:
        return self.capabilities is not None

    @property
    def has_menu(self) -> bool:
        """True si **merece la pena** abrir el selector (SDD §33/§34)."""
        return self.options().has_menu

    def options(self, *, player_supports=None) -> TrackOptions:
        return build_options(
            self.capabilities, self.selection, player_supports=player_supports
        )

    def select(
        self, kind: str, choice_id: str | None, *, enabled: bool | None = None
    ) -> PlaybackSelection:
        """Aplica una elección validada y **conserva** el resto de la selección.

        Elegir audio no puede borrar los subtítulos que el usuario ya había
        elegido (y al revés). Lanza :class:`SelectTrackError` si la pista no
        existe: es el error controlado del §39, nunca una excepción suelta.
        """
        eleccion = resolve_choice(
            self.capabilities, kind, choice_id, enabled=enabled
        )
        self.selection = _merge(self.selection, eleccion, kind)
        return self.selection

    # -- actualización dinámica --------------------------------------------

    def on_tracks_changed(self, caps: MediaCapabilities | None) -> list[str]:
        """Actualiza el modelo y devuelve los avisos a mostrar (SDD §28).

        Conserva la selección si sigue existiendo; si una pista
        desapareció, elige alternativa válida y **avisa** (AC-11).
        """
        self.capabilities = caps
        self.selection, self.last_change = reconcile_selection(self.selection, caps)
        for listener in list(self._listeners):
            try:
                listener(caps, self.selection, self.last_change)
            except Exception:  # noqa: BLE001 - un listener roto no rompe el modelo
                continue
        return self.last_change

def _merge(base: PlaybackSelection, eleccion: PlaybackSelection, kind: str) -> PlaybackSelection:
    """Fusiona una elección de un solo tipo sobre la selección existente."""
    actual = base.copy() if base is not None else PlaybackSelection()
    if kind == "audio":
        actual.audio_track_id = eleccion.audio_track_id
    elif kind == "subtitles":
        actual.subtitle_track_id = eleccion.subtitle_track_id
        actual.subtitles_enabled = eleccion.subtitles_enabled
        actual.subtitles_decided = eleccion.subtitles_decided
    else:
        actual.video_track_id = eleccion.video_track_id
        actual.auto_quality = eleccion.auto_quality
    return actual
