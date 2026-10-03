"""Tests del modelo común de pistas (plan F0, SDD §7/§8/§26).

Lo que se fija aquí:

- los ids son **deterministas** (``a1``, ``s1``, ``v1080``), no índices
  efímeros: son la clave de las preferencias por canal;
- un ``MediaTrack`` sobrevive a un viaje de ida y vuelta por JSON (es lo que
  se cachea en disco);
- las consultas de "hay algo seleccionable" respetan el umbral de 2: una
  sola pista **no** es una opción (SDD §33/§34);
- `PlaybackSelection` distingue "desactivado" de "automático".
"""

from __future__ import annotations

import unittest

from thetvview.tracks.models import (
    AUDIO,
    DEGRADED_NOT_EXPOSED,
    DEGRADED_PROBE_FAILED,
    PROTO_HLS,
    TEXT,
    VIDEO,
    MediaCapabilities,
    MediaTrack,
    PlaybackSelection,
    audio_id,
    subtitle_id,
    video_id,
)


def audio(idx: int, lang: str | None = None, **kw) -> MediaTrack:
    return MediaTrack(
        id=audio_id(idx),
        type=AUDIO,
        language=lang,
        original_language=lang,
        **kw,
    )


def subtitle(idx: int, lang: str | None = None, **kw) -> MediaTrack:
    return MediaTrack(
        id=subtitle_id(idx),
        type=TEXT,
        language=lang,
        original_language=lang,
        **kw,
    )


def variant(height: int | None = None, **kw) -> MediaTrack:
    track = MediaTrack(id="", type=VIDEO, height=height, **kw)
    track.id = video_id(track)
    return track


class TestIdsDeterministas(unittest.TestCase):
    def test_audio_y_subtitulo_por_orden(self) -> None:
        self.assertEqual(audio_id(1), "a1")
        self.assertEqual(audio_id(2), "a2")
        self.assertEqual(subtitle_id(1), "s1")
        self.assertEqual(subtitle_id(3), "s3")

    def test_audio_id_nunca_cero(self) -> None:
        self.assertEqual(audio_id(0), "a1")
        self.assertEqual(subtitle_id(-4), "s1")

    def test_video_id_usa_altura(self) -> None:
        self.assertEqual(video_id(variant(1080)), "v1080")
        self.assertEqual(video_id(variant(480)), "v480")

    def test_video_id_cae_a_bitrate(self) -> None:
        track = MediaTrack(id="", type=VIDEO, bitrate=2_500_000)
        self.assertEqual(video_id(track), "v2500000")

    def test_video_id_cae_al_indice(self) -> None:
        track = MediaTrack(id="", type=VIDEO)
        self.assertEqual(video_id(track, fallback_index=3), "v3")

    def test_video_id_es_estable_entre_ordenes(self) -> None:
        # La misma calidad declarada en dos variantes distintas del mismo
        # master conserva el id: es lo que permite reconciliar un master en
        # vivo sin perder la preferencia del canal.
        a = MediaTrack(id="", type=VIDEO, height=720, bitrate=3_000_000)
        b = MediaTrack(id="", type=VIDEO, height=720, bitrate=2_500_000)
        self.assertEqual(video_id(a), video_id(b))


class TestMediaTrack(unittest.TestCase):
    def test_resolution_solo_con_los_dos(self) -> None:
        self.assertEqual(variant(1080, width=1920).resolution, (1920, 1080))
        self.assertIsNone(MediaTrack(id="v", type=VIDEO, height=1080).resolution)
        self.assertIsNone(MediaTrack(id="v", type=VIDEO, width=1920).resolution)

    def test_language_label_usa_el_original_si_no_se_normaliza(self) -> None:
        self.assertEqual(audio(1, "spa").language_label, "Español")
        raro = MediaTrack(id="a1", type=AUDIO, original_language="Serranés")
        self.assertEqual(raro.language_label, "Serranés")

    def test_roundtrip_json(self) -> None:
        original = MediaTrack(
            id="a2",
            type=AUDIO,
            language="es",
            original_language="spa",
            label="Español (dub)",
            codec="mp4a.40.2",
            bitrate=128_000,
            role="dub",
            is_default=True,
            is_auto_select=True,
            is_forced=False,
            channels="2",
            uri="https://h/live/es.m3u8",
            group_id="aud",
        )
        data = original.to_dict()
        self.assertNotIn("metadata", data)
        self.assertEqual(MediaTrack.from_dict(data), original)

    def test_from_dict_tipo_desconocido_no_revienta(self) -> None:
        track = MediaTrack.from_dict({"id": "a1", "type": "inventado"})
        self.assertEqual(track.type, AUDIO)

    def test_from_dict_numeros_basura(self) -> None:
        track = MediaTrack.from_dict(
            {"id": "a1", "type": "audio", "bitrate": "x", "fps": "", "height": None}
        )
        self.assertIsNone(track.bitrate)
        self.assertIsNone(track.fps)
        self.assertIsNone(track.height)


class TestMediaCapabilities(unittest.TestCase):
    def test_una_sola_pista_no_es_seleccionable(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es")],
            video_variants=[variant(720)],
            adaptive_bitrate=False,
            protocol=PROTO_HLS,
        )
        self.assertFalse(caps.has_selectable_audio)
        self.assertFalse(caps.has_selectable_quality)
        self.assertFalse(caps.has_selectable_subtitles)
        self.assertFalse(caps.has_any_choice)

    def test_dos_audios_son_seleccionable(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es"), audio(2, "en")],
            video_variants=[variant(720)],
        )
        self.assertTrue(caps.has_selectable_audio)
        self.assertFalse(caps.has_selectable_quality)
        self.assertTrue(caps.has_any_choice)

    def test_dos_variantes_marcan_abr(self) -> None:
        caps = MediaCapabilities(
            video_variants=[variant(480), variant(1080)], adaptive_bitrate=True
        )
        self.assertTrue(caps.has_selectable_quality)

    def test_un_audio_mas_dos_variantes_no_es_audio_seleccionable(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es")],
            video_variants=[variant(480), variant(720)],
        )
        self.assertFalse(caps.has_selectable_audio)
        self.assertTrue(caps.has_selectable_quality)

    def test_busqueda_por_id_en_los_tres_casos(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es")],
            video_variants=[variant(720)],
            subtitle_tracks=[subtitle(1, "es")],
            audio_uri_by_id={"a1": "https://h/a1.m3u8"},
            video_uri_by_id={"v720": "https://h/v720.m3u8"},
            subtitle_uri_by_id={"s1": "https://h/s1.vtt"},
        )
        self.assertIsNotNone(caps.audio_by_id("a1"))
        self.assertIsNotNone(caps.variant_by_id("v720"))
        self.assertIsNotNone(caps.subtitle_by_id("s1"))
        self.assertIsNotNone(caps.track_by_id("a1"))
        self.assertIsNone(caps.track_by_id("nope"))
        self.assertIsNone(caps.track_by_id(None))
        self.assertEqual(caps.uri_for("s1"), "https://h/s1.vtt")
        self.assertIsNone(caps.uri_for("nope"))
        self.assertIsNone(caps.uri_for(None))

    def test_default_audio_prioriza_la_marcada(self) -> None:
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "en"), audio(2, "es", is_default=True)]
        )
        self.assertEqual(caps.default_audio().id, "a2")

    def test_default_audio_usa_la_primera_si_no_hay_default(self) -> None:
        caps = MediaCapabilities(audio_tracks=[audio(1, "en"), audio(2, "es")])
        self.assertEqual(caps.default_audio().id, "a1")

    def test_variantes_ordenadas_de_peor_a_mejor(self) -> None:
        caps = MediaCapabilities(
            video_variants=[variant(1080), variant(480), variant(720)]
        )
        self.assertEqual([t.height for t in caps.sorted_variants()], [480, 720, 1080])

    def test_degradacion_explicita(self) -> None:
        sin_pistas = MediaCapabilities(degraded_reason=DEGRADED_NOT_EXPOSED)
        sin_acceso = MediaCapabilities(degraded_reason=DEGRADED_PROBE_FAILED)
        completa = MediaCapabilities()
        self.assertTrue(sin_pistas.is_degraded)
        self.assertTrue(sin_acceso.is_degraded)
        self.assertFalse(completa.is_degraded)
        self.assertNotEqual(sin_pistas.degraded_reason, sin_acceso.degraded_reason)

    def test_roundtrip_json_no_guarda_urls(self) -> None:
        # La caché en disco guarda capacidades, no credenciales: las URI
        # absolutas (que en Xtream llevan usuario/contraseña) no se
        # persisten, por eso to_dict() las omite.
        caps = MediaCapabilities(
            audio_tracks=[audio(1, "es", uri="http://h/live/u/p/1.m3u8")],
            video_variants=[variant(720)],
            subtitle_tracks=[subtitle(1, "es")],
            adaptive_bitrate=False,
            protocol=PROTO_HLS,
            live=True,
            degraded_reason=DEGRADED_NOT_EXPOSED,
        )
        data = caps.to_dict()
        self.assertNotIn("uri", data["audio_tracks"][0])
        self.assertNotIn("http", str(data))
        back = MediaCapabilities.from_dict(data)
        self.assertEqual(len(back.audio_tracks), 1)
        self.assertEqual(back.audio_tracks[0].id, "a1")
        self.assertTrue(back.live)
        self.assertEqual(back.degraded_reason, DEGRADED_NOT_EXPOSED)
        self.assertFalse(back.adaptive_bitrate)

    def test_from_dict_tolera_basura(self) -> None:
        caps = MediaCapabilities.from_dict({"audio_tracks": "nope", "live": 1})
        self.assertEqual(caps.audio_tracks, [])
        self.assertTrue(caps.live)
        self.assertEqual(caps.protocol, "unknown")


class TestPlaybackSelection(unittest.TestCase):
    def test_valores_por_defecto_son_no_tocar_nada(self) -> None:
        sel = PlaybackSelection()
        self.assertTrue(sel.is_default)
        self.assertTrue(sel.auto_quality)
        self.assertFalse(sel.subtitles_enabled)
        self.assertIsNone(sel.audio_track_id)
        self.assertIsNone(sel.subtitle_track_id)

    def test_tocar_cualquier_cosa_sale_del_default(self) -> None:
        self.assertFalse(PlaybackSelection(audio_track_id="a1").is_default)
        self.assertFalse(PlaybackSelection(subtitles_enabled=True).is_default)
        self.assertFalse(PlaybackSelection(auto_quality=False).is_default)
        self.assertFalse(PlaybackSelection(subtitle_track_id="s1").is_default)

    def test_subtitulos_desactivados_no_equivale_a_no_tocados(self) -> None:
        # Desactivar subtítulos es una decisión explícita (AC-04) aunque no
        # haya ninguna pista de subtítulo que elegir.
        sel = PlaybackSelection(subtitles_enabled=False, subtitle_track_id=None)
        self.assertTrue(sel.is_default)

    def test_auto_quality_y_video_track_no_conviven(self) -> None:
        # Invariante que comprueba el gestor: si se fuerza una variante, la
        # calidad automática tiene que estar apagada.
        sel = PlaybackSelection(video_track_id="v720", auto_quality=False)
        self.assertFalse(sel.auto_quality)
        self.assertEqual(sel.video_track_id, "v720")

    def test_copy_no_aliasea(self) -> None:
        sel = PlaybackSelection(audio_track_id="a1")
        clone = sel.copy()
        clone.audio_track_id = "a2"
        self.assertEqual(sel.audio_track_id, "a1")

    def test_roundtrip_json(self) -> None:
        sel = PlaybackSelection(
            audio_track_id="a2",
            subtitle_track_id="s1",
            video_track_id="v1080",
            subtitles_enabled=True,
            auto_quality=False,
        )
        self.assertEqual(PlaybackSelection.from_dict(sel.to_dict()), sel)