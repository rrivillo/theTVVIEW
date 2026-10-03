"""Tests del motor de selección (plan F3, SDD §39/§40/§33/§34).

Los 8 casos de §39 y los 2 de §40 están aquí uno a uno, más la política de
visibilidad (§33/§34) y la reconciliación (§28, AC-11).

Lo que se verifica con más cuidado, porque es donde una UI miente:

- **no se ofrece lo que no existe**: un solo audio, una sola calidad o un
  solo subtítulo no abren menú (AC-09);
- **no se cuelga un id**: pedir una pista inexistente da el error controlado
  de §39 caso 7, nunca una excepción sin manejar;
- **lo que desapareció se avisa**: si un master en vivo pierde una pista, la
  selección se corrige y hay un aviso para el modal (AC-11).
"""

from __future__ import annotations

import unittest

from thetvview.tracks.labels import audio_label, subtitle_label, video_label
from thetvview.tracks.manager import (
    QUICK_AUTO,
    TRACK_GONE_MESSAGE,
    SelectTrackError,
    TrackManager,
    apply_preferences,
    build_options,
    reconcile_selection,
    resolve_choice,
    selection_for,
)
from thetvview.tracks.models import (
    PROTO_DASH,
    PROTO_HLS,
    AUDIO,
    DEGRADED_NOT_EXPOSED,
    TEXT,
    VIDEO,
    MediaCapabilities,
    MediaTrack,
    PlaybackSelection,
)


class _Prefs:
    """Preferencias mínimas, como las resuelve ``tracks.prefs`` (F4)."""

    def __init__(
        self,
        audio: str | None = None,
        subtitle: str | None = None,
        subs_on: bool = False,
        quality: str | None = None,
    ) -> None:
        self.preferred_audio_language = audio
        self.preferred_subtitle_language = subtitle
        self.subtitles_enabled = subs_on
        self.preferred_quality = quality


def audio(idx: int, lang: str | None = None, **kw) -> MediaTrack:
    return MediaTrack(
        id=f"a{idx}", type=AUDIO, language=lang, original_language=lang,
        uri=kw.pop("uri", f"https://h/a{idx}.m3u8"), **kw,
    )


def sub(idx: int, lang: str | None = None, **kw) -> MediaTrack:
    return MediaTrack(
        id=f"s{idx}", type=TEXT, language=lang, original_language=lang,
        uri=kw.pop("uri", f"https://h/s{idx}.vtt"), **kw,
    )


def variant(height: int, **kw) -> MediaTrack:
    return MediaTrack(
        id=f"v{height}", type=VIDEO, height=height, width=int(height * 16 / 9),
        uri=kw.pop("uri", f"https://h/v{height}.m3u8"), **kw,
    )


def caps_multi() -> MediaCapabilities:
    """Caso rico: 3 audios, 2 subtítulos, 3 calidades."""
    return MediaCapabilities(
        audio_tracks=[
            audio(1, "es", is_default=True, label="Español"),
            audio(2, "en", label="English"),
            audio(3, "ca", label="Català"),
        ],
        subtitle_tracks=[sub(1, "es"), sub(2, "en")],
        video_variants=[variant(480), variant(720), variant(1080)],
        adaptive_bitrate=True,
        protocol="hls",
    )


def caps_single() -> MediaCapabilities:
    """Caso simple: una pista de cada. Debe reproducirse como siempre."""
    return MediaCapabilities(
        audio_tracks=[audio(1, "es")],
        video_variants=[variant(720)],
        adaptive_bitrate=False,
        protocol="hls",
    )


# ---------------------------------------------------------------------------
# §33/§34 — política de visibilidad (AC-09)
# ---------------------------------------------------------------------------


class TestPoliticaDeVisibilidad(unittest.TestCase):
    def test_un_solo_audio_no_abre_selector(self) -> None:
        opciones = build_options(caps_single())
        self.assertEqual(opciones.audio, [])
        self.assertEqual(opciones.audio_info, "Español")
        self.assertFalse(opciones.audio_selectable)

    def test_una_sola_calidad_se_muestra_como_dato(self) -> None:
        opciones = build_options(caps_single())
        self.assertEqual(opciones.quality, [])
        self.assertEqual(opciones.quality_info, "720p")
        self.assertFalse(opciones.quality_selectable)

    def test_ningun_subtitulo_no_abre_selector(self) -> None:
        opciones = build_options(caps_single())
        self.assertEqual(opciones.subtitles, [])
        self.assertFalse(opciones.subtitles_selectable)

    def test_stream_simple_no_tiene_menu(self) -> None:
        opciones = build_options(caps_single())
        self.assertFalse(opciones.has_menu)
        self.assertEqual(opciones.selectable_kinds, [])

    def test_dos_audios_abren_selector(self) -> None:
        caps = MediaCapabilities(audio_tracks=[audio(1, "es"), audio(2, "en")],
                                 video_variants=[variant(720)])
        opciones = build_options(caps)
        self.assertTrue(opciones.audio_selectable)
        self.assertEqual([c.label for c in opciones.audio],
                         ["Automático", "Español", "English"])

    def test_dos_calidades_abren_selector_con_auto(self) -> None:
        caps = MediaCapabilities(video_variants=[variant(720), variant(1080)],
                                 adaptive_bitrate=True, protocol=PROTO_HLS)
        opciones = build_options(caps)
        self.assertTrue(opciones.quality_selectable)
        # "Automática (ABR)" sólo si el manifiesto declara ≥2 variantes.
        self.assertEqual(opciones.quality[0].label, "Automática (ABR)")
        self.assertEqual(opciones.quality[0].id, QUICK_AUTO)
        self.assertEqual([c.label for c in opciones.quality[1:]],
                         ["720p", "1080p"])

    def test_en_dash_no_se_ofrece_calidad(self) -> None:
        """Sin proxy de pinzado no hay forma de forzarla: no se promete.

        Un MPD no tiene master que reescribir, y sus representations no
        tienen URI de playlist, así que un master sintético no reproduciría
        nada. Ofrecer el menú sería hacer creer al usuario que eligió 720p.
        """
        caps = MediaCapabilities(video_variants=[variant(720), variant(1080)],
                                 adaptive_bitrate=True, protocol=PROTO_DASH)
        opciones = build_options(caps)
        self.assertFalse(opciones.quality_selectable)
        self.assertFalse(opciones.has_menu)

    def test_una_sola_calidad_no_ofrece_auto_enganoso(self) -> None:
        # §14/§15: con una única variante, "ABR" sería mentira.
        opciones = build_options(caps_single())
        self.assertEqual(opciones.quality, [])
        self.assertEqual(opciones.quality_info, "720p")

    def test_subtitulos_ofrecen_desactivados(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es")],
            subtitle_tracks=[sub(1, "es")],
            video_variants=[variant(720)],
        )
        opciones = build_options(caps)
        # Un subtítulo + "Desactivados" = dos opciones: sí es un menú.
        self.assertTrue(opciones.subtitles_selectable)
        self.assertEqual([c.label for c in opciones.subtitles],
                         ["Desactivados", "Español"])

    def test_sin_pistas_no_hay_nada(self) -> None:
        opciones = build_options(None)
        self.assertFalse(opciones.has_menu)
        self.assertEqual(opciones.audio_info, None)

    def test_lo_que_el_backend_no_aplica_no_se_muestra(self) -> None:
        # SDD §46: si el reproductor no puede fijar calidad, no se ofrece.
        opciones = build_options(
            caps_multi(), player_supports=lambda kind: kind != "quality"
        )
        self.assertFalse(opciones.quality_selectable)
        self.assertTrue(opciones.audio_selectable)

    def test_degradacion_explicita_visible(self) -> None:
        caps = caps_single()
        caps.degraded_reason = DEGRADED_NOT_EXPOSED
        opciones = build_options(caps)
        self.assertEqual(opciones.degraded_reason, DEGRADED_NOT_EXPOSED)

    def test_subtitulo_sin_uri_no_es_opcion(self) -> None:
        # Un subtítulo que no se puede pedir no aparece en el menú (AC-03).
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es"), audio(2, "en")],
            subtitle_tracks=[
                MediaTrack(id="s1", type=TEXT, language="es"),
                MediaTrack(id="s2", type=TEXT, language="en", uri="https://h/en.vtt"),
            ],
            video_variants=[variant(720)],
        )
        opciones = build_options(caps)
        self.assertEqual([c.id for c in opciones.subtitles], [QUICK_AUTO, "s2"])


# ---------------------------------------------------------------------------
# §39 — los 8 casos de selección
# ---------------------------------------------------------------------------


class TestCasosDeSeleccion(unittest.TestCase):
    def test_caso_1_seleccionar_audio_espanol(self) -> None:
        sel = resolve_choice(caps_multi(), "audio", "a1")
        self.assertEqual(sel.audio_track_id, "a1")

    def test_caso_2_seleccionar_audio_ingles(self) -> None:
        sel = resolve_choice(caps_multi(), "audio", "a2")
        self.assertEqual(sel.audio_track_id, "a2")

    def test_caso_3_seleccionar_subtitulos(self) -> None:
        sel = resolve_choice(caps_multi(), "subtitles", "s2")
        self.assertTrue(sel.subtitles_enabled)
        self.assertEqual(sel.subtitle_track_id, "s2")

    def test_caso_4_desactivar_subtitulos(self) -> None:
        sel = resolve_choice(caps_multi(), "subtitles", None)
        self.assertFalse(sel.subtitles_enabled)
        self.assertIsNone(sel.subtitle_track_id)

    def test_caso_4b_desactivar_subtitulos_con_id_auto(self) -> None:
        sel = resolve_choice(caps_multi(), "subtitles", QUICK_AUTO)
        self.assertFalse(sel.subtitles_enabled)

    def test_caso_5_seleccionar_720p(self) -> None:
        sel = resolve_choice(caps_multi(), "quality", "v720")
        self.assertFalse(sel.auto_quality)
        self.assertEqual(sel.video_track_id, "v720")

    def test_caso_6_volver_a_auto_abr(self) -> None:
        sel = resolve_choice(caps_multi(), "quality", QUICK_AUTO)
        self.assertTrue(sel.auto_quality)
        self.assertIsNone(sel.video_track_id)

    def test_caso_7_pista_inexistente_error_controlado(self) -> None:
        with self.assertRaises(SelectTrackError) as ctx:
            resolve_choice(caps_multi(), "audio", "a99")
        self.assertEqual(str(ctx.exception), TRACK_GONE_MESSAGE)
        self.assertEqual(str(ctx.exception), "La pista seleccionada ya no está disponible.")

    def test_caso_7_tambien_para_subtitulos_y_calidad(self) -> None:
        for kind in ("subtitles", "quality"):
            with self.subTest(kind=kind), self.assertRaises(SelectTrackError):
                resolve_choice(caps_multi(), kind, "no-existe")

    def test_caso_7_sin_capacidades_no_revienta(self) -> None:
        with self.assertRaises(SelectTrackError) as ctx:
            resolve_choice(None, "audio", "a1")
        self.assertEqual(str(ctx.exception), TRACK_GONE_MESSAGE)

    def test_caso_7_tipo_desconocido(self) -> None:
        with self.assertRaises(SelectTrackError):
            resolve_choice(caps_multi(), "subtitulo", "s1")

    def test_caso_8_pista_desaparece_despues_de_elegirla(self) -> None:
        antes = resolve_choice(caps_multi(), "audio", "a3")
        # El master en vivo ya no publica el catalán.
        despues = MediaCapabilities(
            audio_tracks=[audio(1, "es", is_default=True), audio(2, "en")],
            subtitle_tracks=[sub(1, "es"), sub(2, "en")],
            video_variants=[variant(480), variant(720), variant(1080)],
        )
        sel, avisos = reconcile_selection(antes, despues)
        self.assertNotEqual(sel.audio_track_id, "a3")
        self.assertEqual(sel.audio_track_id, "a1")
        self.assertEqual(len(avisos), 1)
        self.assertIn("ya no está", avisos[0])

    def test_desaparecen_todas_las_alternativas(self) -> None:
        antes = PlaybackSelection(
            audio_track_id="a2", subtitle_track_id="s2",
            video_track_id="v1080", subtitles_enabled=True, auto_quality=False,
        )
        sel, avisos = reconcile_selection(antes, caps_single())
        # El audio cae al único que queda (el que marca el proveedor) y los
        # subtítulos se desactivan: en ningún caso queda un id colgando.
        self.assertEqual(sel.audio_track_id, "a1")
        self.assertIsNone(sel.subtitle_track_id)
        self.assertTrue(sel.auto_quality)
        self.assertFalse(sel.subtitles_enabled)
        self.assertEqual(len(avisos), 3)

    def test_reconciliar_no_avisa_si_no_cambio_nada(self) -> None:
        antes = PlaybackSelection(
            audio_track_id="a1", subtitle_track_id="s2",
            video_track_id="v720", subtitles_enabled=True, auto_quality=False,
        )
        sel, avisos = reconcile_selection(antes, caps_multi())
        self.assertEqual(sel, antes)
        self.assertEqual(avisos, [])

    def test_reconciliar_conserva_auto_quality(self) -> None:
        sel, avisos = reconcile_selection(
            PlaybackSelection(auto_quality=True), caps_multi()
        )
        self.assertTrue(sel.auto_quality)
        self.assertEqual(avisos, [])

    def test_reconciliar_con_capacidades_none(self) -> None:
        sel, avisos = reconcile_selection(
            PlaybackSelection(audio_track_id="a1"), None
        )
        self.assertTrue(sel.is_default)
        self.assertEqual(avisos, [])

    def test_reconciliar_nunca_deja_ids_colgando(self) -> None:
        sel, _avisos = reconcile_selection(
            PlaybackSelection(
                audio_track_id="a9", subtitle_track_id="s9", video_track_id="v9",
                subtitles_enabled=True, auto_quality=False,
            ),
            caps_multi(),
        )
        caps = caps_multi()
        for valor in (sel.audio_track_id, sel.subtitle_track_id, sel.video_track_id):
            if valor is not None:
                self.assertIsNotNone(caps.track_by_id(valor))


# ---------------------------------------------------------------------------
# §40 — preferencias
# ---------------------------------------------------------------------------


class TestPreferenciasDeIdioma(unittest.TestCase):
    def setUp(self) -> None:
        self.caps = MediaCapabilities(
            audio_tracks=[audio(1, "en"), audio(2, "es"), audio(3, "fr")],
            video_variants=[variant(720)],
        )

    def test_caso_1_elige_el_idioma_pedido(self) -> None:
        self.assertEqual(apply_preferences(self.caps, _Prefs(audio="es")).id, "a2")

    def test_caso_2_si_no_existe_cae_al_default(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "en"), audio(2, "en", is_default=True)],
            video_variants=[variant(720)],
        )
        self.assertEqual(apply_preferences(caps, _Prefs(audio="de")).id, "a2")

    def test_idioma_base_gana_a_la_primera(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "fr"), audio(2, "es-ES")],
            video_variants=[variant(720)],
        )
        self.assertEqual(apply_preferences(caps, _Prefs(audio="es")).id, "a2")

    def test_exacto_gana_a_base(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es-MX"), audio(2, "es-ES")],
            video_variants=[variant(720)],
        )
        self.assertEqual(apply_preferences(caps, _Prefs(audio="es-ES")).id, "a2")

    def test_iso_de_tres_letras(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "en"), audio(2, "spa")],
            video_variants=[variant(720)],
        )
        self.assertEqual(apply_preferences(caps, _Prefs(audio="es")).id, "a2")

    def test_nombre_completo(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "en"), audio(2, "Spanish")],
            video_variants=[variant(720)],
        )
        self.assertEqual(apply_preferences(caps, _Prefs(audio="Español")).id, "a2")

    def test_sin_preferencia_no_toca_nada(self) -> None:
        sel = selection_for(self.caps, _Prefs())
        self.assertIsNone(sel.audio_track_id)
        self.assertTrue(sel.auto_quality)
        self.assertFalse(sel.subtitles_enabled)

    def test_sin_preferencias_no_toca_nada(self) -> None:
        sel = selection_for(self.caps, None)
        self.assertTrue(sel.is_default)

    def test_preferencia_de_subtitulos(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es")],
            subtitle_tracks=[sub(1, "en"), sub(2, "es")],
            video_variants=[variant(720)],
        )
        sel = selection_for(caps, _Prefs(subtitle="es", subs_on=True))
        self.assertTrue(sel.subtitles_enabled)
        self.assertEqual(sel.subtitle_track_id, "s2")

    def test_subtitulos_activados_sin_preferencia_usan_default(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es")],
            subtitle_tracks=[sub(1, "en"), sub(2, "es", is_default=True)],
            video_variants=[variant(720)],
        )
        sel = selection_for(caps, _Prefs(subs_on=True))
        self.assertEqual(sel.subtitle_track_id, "s2")

    def test_subtitulos_desactivados_no_elegen_nada(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es")],
            subtitle_tracks=[sub(1, "en")],
            video_variants=[variant(720)],
        )
        sel = selection_for(caps, _Prefs(subtitle="en", subs_on=False))
        self.assertFalse(sel.subtitles_enabled)
        self.assertIsNone(sel.subtitle_track_id)

    def test_calidad_concreta_por_id(self) -> None:
        caps = caps_multi()
        sel = selection_for(caps, _Prefs(quality="v720"))
        self.assertEqual(sel.video_track_id, "v720")
        self.assertFalse(sel.auto_quality)

    def test_calidad_concreta_por_altura(self) -> None:
        sel = selection_for(caps_multi(), _Prefs(quality="1080p"))
        self.assertEqual(sel.video_track_id, "v1080")
        self.assertFalse(sel.auto_quality)

    def test_calidad_auto_no_fuerza_nada(self) -> None:
        sel = selection_for(caps_multi(), _Prefs(quality=QUICK_AUTO))
        self.assertIsNone(sel.video_track_id)
        self.assertTrue(sel.auto_quality)

    def test_calidad_inexistente_no_fuerza_nada(self) -> None:
        sel = selection_for(caps_multi(), _Prefs(quality="4320p"))
        self.assertTrue(sel.auto_quality)
        self.assertIsNone(sel.video_track_id)

    def test_preferencia_ignora_pistas_inexistentes(self) -> None:
        # apply_preferences nunca devuelve algo que no esté en caps.
        for preference in ("es", "de", "Klingon", ""):
            with self.subTest(preference=preference):
                elegida = apply_preferences(self.caps, _Prefs(audio=preference))
                if elegida is not None:
                    self.assertIn(elegida, self.caps.audio_tracks)

    def test_sin_capacidades(self) -> None:
        self.assertIsNone(apply_preferences(None, _Prefs(audio="es")))
        self.assertTrue(selection_for(None, _Prefs(audio="es")).is_default)


# ---------------------------------------------------------------------------
# TrackManager (estado vivo, §22/§28)
# ---------------------------------------------------------------------------


class TestTrackManager(unittest.TestCase):
    def test_estado_inicial_sin_tocar_nada(self) -> None:
        gestor = TrackManager(caps_multi(), _Prefs())
        self.assertTrue(gestor.has_tracks)
        self.assertTrue(gestor.has_menu)
        self.assertTrue(gestor.selection.is_default)

    def test_sin_capacidades_no_hay_tracks(self) -> None:
        gestor = TrackManager(None, None)
        self.assertFalse(gestor.has_tracks)
        self.assertFalse(gestor.has_menu)
        self.assertTrue(gestor.selection.is_default)

    def test_stream_simple_no_tiene_menu(self) -> None:
        gestor = TrackManager(caps_single(), _Prefs())
        self.assertFalse(gestor.has_menu)

    def test_select_valida(self) -> None:
        gestor = TrackManager(caps_multi(), _Prefs())
        gestor.select("audio", "a2")
        self.assertEqual(gestor.selection.audio_track_id, "a2")
        with self.assertRaises(SelectTrackError):
            gestor.select("audio", "a99")

    def test_options_refleja_la_seleccion(self) -> None:
        gestor = TrackManager(caps_multi(), _Prefs())
        gestor.select("quality", "v720")
        opciones = gestor.options()
        self.assertEqual(opciones.selected_id("quality"), "v720")

    def test_on_tracks_changed_avisa_a_los_oyentes(self) -> None:
        gestor = TrackManager(caps_multi(), _Prefs())
        gestor.select("audio", "a3")
        gestor.select("subtitles", "s2")
        gestor.select("quality", "v1080")
        recibidos: list[tuple] = []
        gestor.add_listener(lambda caps, sel, avisos: recibidos.append((caps, sel, avisos)))
        avisos = gestor.on_tracks_changed(caps_single())
        self.assertEqual(len(recibidos), 1)
        self.assertEqual(avisos, recibidos[0][2])
        # Las tres pistas elegidas desaparecieron: hay tres avisos y no
        # queda ningún id colgando.
        self.assertEqual(len(avisos), 3)
        caps = caps_single()
        self.assertIsNotNone(caps.audio_by_id(gestor.selection.audio_track_id))
        self.assertIsNone(gestor.selection.subtitle_track_id)
        self.assertTrue(gestor.selection.auto_quality)
        self.assertFalse(gestor.options().has_menu)

    def test_oyente_roto_no_rompe_el_modelo(self) -> None:
        gestor = TrackManager(caps_multi(), _Prefs())

        def roto(*_args) -> None:
            raise RuntimeError("boom")

        gestor.add_listener(roto)
        self.assertEqual(gestor.on_tracks_changed(caps_single()), gestor.last_change)
        self.assertIsNotNone(gestor.capabilities)

    def test_remove_listener(self) -> None:
        gestor = TrackManager(caps_multi(), _Prefs())
        gestor.remove_listener(None)  # no explota

    def test_preferencias_al_construir(self) -> None:
        gestor = TrackManager(caps_multi(), _Prefs(audio="ca"))
        self.assertEqual(gestor.selection.audio_track_id, "a3")
        # Y el menú lo muestra marcado.
        opciones = gestor.options()
        self.assertEqual(opciones.selected_id("audio"), "a3")


class TestEtiquetasEnElMenu(unittest.TestCase):
    def test_etiquetas_de_audio(self) -> None:
        caps = caps_multi()
        opciones = build_options(caps)
        etiquetas = [c.label for c in opciones.audio]
        self.assertEqual(etiquetas, ["Automático", "Español", "English", "Català"])

    def test_etiquetas_de_subtitulos(self) -> None:
        opciones = build_options(caps_multi())
        self.assertEqual(
            [c.label for c in opciones.subtitles], ["Desactivados", "Español", "English"]
        )

    def test_etiquetas_de_calidad(self) -> None:
        opciones = build_options(caps_multi())
        self.assertEqual(
            [c.label for c in opciones.quality],
            ["Automática (ABR)", "480p", "720p", "1080p"],
        )

    def test_audio_sin_nombre_usa_el_indice(self) -> None:
        pista = MediaTrack(id="a4", type=AUDIO)
        self.assertEqual(audio_label(pista, 4), "Audio #4")

    def test_video_label_con_bitrate(self) -> None:
        pista = variant(720, bitrate=3_000_000, codec="avc1.4d401f")
        self.assertEqual(video_label(pista), "720p — 3000 kbps — H.264")
        self.assertEqual(subtitle_label(sub(1, "es"), 1), "Español")