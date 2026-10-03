"""Tests del parser HLS (plan F1, SDD §38/§9).

Se cubren los casos del SDD §38 para HLS: una variante, varias variantes,
varios audios, varios subtítulos, DEFAULT, AUTOSELECT, FORCED, ausencia de
subtítulos y ausencia de audio alternativo.

Dos invariantes se comprueban en casi todos los tests:

- **no se inventa**: un master con una sola variante da 1 audio, 1 vídeo y
  0 subtítulos, y ninguna opción seleccionable;
- **no se rompe lo simple**: un media playlist y un master de una variante
  se reproducen igual que antes de esta funcionalidad (SDD §32).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from thetvview.streams.hls import (
    HlsParseError,
    is_master_playlist,
    parse_attribute_list,
    parse_hls,
    parse_master_playlist,
    parse_media_playlist,
    split_codecs,
)
from thetvview.tracks.labels import audio_label, subtitle_label, video_label
from thetvview.tracks.models import DEGRADED_NOT_EXPOSED, PROTO_HLS

FIXTURES = Path(__file__).parent / "fixtures" / "streams"
BASE = "https://h.test/live/canal/master.m3u8"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParseAttributeList(unittest.TestCase):
    def test_basico(self) -> None:
        self.assertEqual(
            parse_attribute_list("BANDWIDTH=1200,RESOLUTION=640x360"),
            {"BANDWIDTH": "1200", "RESOLUTION": "640x360"},
        )

    def test_comas_dentro_de_comillas(self) -> None:
        self.assertEqual(
            parse_attribute_list('CODECS="avc1.4d401f,mp4a.40.2"'),
            {"CODECS": "avc1.4d401f,mp4a.40.2"},
        )

    def test_comillas_escapadas_en_el_valor(self) -> None:
        parsed = parse_attribute_list('NAME="Audio ""5.1"" Mix"')
        self.assertEqual(parsed["NAME"], 'Audio "5.1" Mix')

    def test_espacios_alrededor_del_igual(self) -> None:
        self.assertEqual(parse_attribute_list('NAME = hola , LANG = es'),
                         {"NAME": "hola", "LANG": "es"})

    def test_vacio(self) -> None:
        self.assertEqual(parse_attribute_list(""), {})
        self.assertEqual(parse_attribute_list("   "), {})

    def test_ultima_clave_repetida_gana(self) -> None:
        self.assertEqual(parse_attribute_list("A=1,A=2"), {"A": "2"})


class TestSplitCodecs(unittest.TestCase):
    def test_comas_normales(self) -> None:
        self.assertEqual(split_codecs("avc1.4d401f,mp4a.40.2"),
                         ["avc1.4d401f", "mp4a.40.2"])

    def test_parametro_entrecomillado_no_se_inventa(self) -> None:
        # Un split(",") a pelo crearía un códec llamado "channels=2".
        self.assertEqual(
            split_codecs("mp4a.40.5,avc1.640028,channels=2"),
            ["mp4a.40.5", "avc1.640028", "channels=2"],
        )

    def test_none_y_vacio(self) -> None:
        self.assertEqual(split_codecs(None), [])
        self.assertEqual(split_codecs(""), [])
        self.assertEqual(split_codecs(" , , "), [])


class TestMasterFull(unittest.TestCase):
    def setUp(self) -> None:
        self.caps = parse_master_playlist(fixture("master_full.m3u8"), final_url=BASE)

    def test_protocolo_y_urls(self) -> None:
        self.assertEqual(self.caps.protocol, PROTO_HLS)
        self.assertEqual(self.caps.final_url, BASE)
        self.assertTrue(self.caps.live)
        self.assertGreater(self.caps.probed_at, 0)

    def test_dos_audios_con_idioma_normalizado(self) -> None:
        self.assertEqual(len(self.caps.audio_tracks), 2)
        primero, segundo = self.caps.audio_tracks
        self.assertEqual((primero.id, primero.language, primero.label), ("a1", "es", "Español"))
        # "eng" es ISO-639-2 y se normaliza igual que "en".
        self.assertEqual((segundo.id, segundo.language, segundo.label), ("a2", "en", "English"))

    def test_default_y_autoselect(self) -> None:
        primero, segundo = self.caps.audio_tracks
        self.assertTrue(primero.is_default)
        self.assertFalse(segundo.is_default)
        self.assertTrue(primero.is_auto_select)
        # El audio inglés no declara AUTOSELECT, así que se asume que sí
        # (HLS dice que el valor por defecto es YES).
        self.assertTrue(segundo.is_auto_select)

    def test_autoselect_no_se_ignora(self) -> None:
        # El subtítulo inglés sí declara AUTOSELECT=NO.
        self.assertFalse(self.caps.subtitle_tracks[1].is_auto_select)

    def test_canales_de_audio(self) -> None:
        self.assertEqual(self.caps.audio_tracks[0].channels, "2")

    def test_cuatro_pistas_de_subtitulo(self) -> None:
        self.assertEqual([t.id for t in self.caps.subtitle_tracks], ["s1", "s2"])
        self.assertEqual(self.caps.subtitle_tracks[0].language, "es")
        self.assertEqual(self.caps.subtitle_tracks[1].language, "en")

    def test_tres_variantes_ordenadas(self) -> None:
        self.assertEqual(
            [t.id for t in self.caps.video_variants], ["v360", "v720", "v1080"]
        )

    def test_variant_completa(self) -> None:
        alta = self.caps.video_variants[-1]
        self.assertEqual((alta.width, alta.height), (1920, 1080))
        self.assertEqual(alta.bitrate, 6_800_000)
        self.assertEqual(alta.fps, 50.0)
        self.assertEqual(alta.codec, "avc1.640028")

    def test_abr_con_tres_variantes(self) -> None:
        self.assertTrue(self.caps.adaptive_bitrate)
        self.assertTrue(self.caps.has_any_choice)

    def test_uri_relativas_absolutizadas_contra_la_url_final(self) -> None:
        self.assertEqual(
            self.caps.audio_tracks[0].uri,
            "https://h.test/live/canal/audio/es.m3u8",
        )
        self.assertEqual(
            self.caps.video_variants[0].uri,
            "https://h.test/live/canal/360p/index.m3u8",
        )

    def test_asociacion_de_grupos_por_variante(self) -> None:
        for variante in self.caps.video_variants:
            self.assertEqual(variante.metadata["audio_group"], "aud")
            self.assertEqual(variante.metadata["subtitles_group"], "sub")

    def test_mapas_de_uri_coherentes(self) -> None:
        self.assertEqual(self.caps.audio_uri_by_id["a1"],
                         self.caps.audio_tracks[0].uri)
        self.assertEqual(self.caps.subtitle_uri_by_id["s1"],
                         self.caps.subtitle_tracks[0].uri)
        self.assertEqual(self.caps.video_uri_by_id["v720"],
                         self.caps.video_variants[1].uri)

    def test_atributos_originales_conservados_para_el_proxy(self) -> None:
        # El proxy de fijado (F5c) reescribirá el master: necesita el
        # NAME/DEFAULT/AUTOSELECT/FORCED/CHARACTERISTICS originales.
        guardados = self.caps.audio_tracks[0].metadata["hls_attrs"]
        self.assertEqual(guardados["NAME"], "Español")
        self.assertEqual(guardados["GROUP-ID"], "aud")
        self.assertEqual(guardados["DEFAULT"], "YES")

    def test_etiquetas_amigables(self) -> None:
        self.assertEqual(audio_label(self.caps.audio_tracks[0], 1), "Español")
        self.assertEqual(audio_label(self.caps.audio_tracks[1], 2), "English")
        self.assertEqual(subtitle_label(self.caps.subtitle_tracks[0], 1), "Español")
        self.assertEqual(video_label(self.caps.video_variants[1]), "720p — 3800 kbps — H.264")


class TestMasterSinSubs(unittest.TestCase):
    def setUp(self) -> None:
        self.caps = parse_master_playlist(fixture("master_no_subs.m3u8"), final_url=BASE)

    def test_sin_subtitulos_no_inventa(self) -> None:
        self.assertEqual(self.caps.subtitle_tracks, [])
        self.assertEqual(self.caps.subtitle_uri_by_id, {})

    def test_dos_audios_con_iso_de_tres_letras(self) -> None:
        self.assertEqual([t.language for t in self.caps.audio_tracks], ["es", "ca"])

    def test_calidad_seleccionable_audio_seleccionable(self) -> None:
        self.assertTrue(self.caps.has_selectable_audio)
        self.assertTrue(self.caps.has_selectable_quality)
        self.assertFalse(self.caps.has_selectable_subtitles)


class TestMasterUnaSolaVariante(unittest.TestCase):
    """El caso mayoritario: un master con una pista. No debe abrir menús."""

    def setUp(self) -> None:
        texto = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,"
            'CODECS="avc1.4d401f,mp4a.40.2"\n'
            "720/index.m3u8\n"
        )
        self.caps = parse_master_playlist(texto, final_url=BASE)

    def test_exactamente_una_pista_de_cada_y_ningun_subtitulo(self) -> None:
        self.assertEqual(len(self.caps.audio_tracks), 1)
        self.assertEqual(len(self.caps.video_variants), 1)
        self.assertEqual(len(self.caps.subtitle_tracks), 0)

    def test_no_hay_opciones_seleccionables(self) -> None:
        self.assertFalse(self.caps.has_any_choice)
        self.assertFalse(self.caps.adaptive_bitrate)

    def test_el_audio_implicito_es_el_default(self) -> None:
        unico = self.caps.audio_tracks[0]
        self.assertTrue(unico.is_default)
        self.assertIsNone(unico.language)
        self.assertIsNone(unico.uri)

    def test_el_codec_de_audio_se_asocia_a_la_variante(self) -> None:
        variante = self.caps.video_variants[0]
        self.assertEqual(variante.codec, "avc1.4d401f")
        self.assertEqual(variante.metadata["codecs"], ["avc1.4d401f", "mp4a.40.2"])

    def test_si_es_mas_de_una_son_seleccionables(self) -> None:
        texto = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\n"
            "a/index.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080\n"
            "b/index.m3u8\n"
        )
        caps = parse_master_playlist(texto, final_url=BASE)
        self.assertTrue(caps.has_selectable_quality)
        self.assertTrue(caps.adaptive_bitrate)


class TestMasterForcedYClosedCaptions(unittest.TestCase):
    def setUp(self) -> None:
        self.caps = parse_master_playlist(fixture("master_forced.m3u8"), final_url=BASE)

    def test_subtitulo_forzado(self) -> None:
        forzado = self.caps.subtitle_tracks[0]
        self.assertTrue(forzado.is_forced)
        self.assertEqual(forzado.id, "s1")

    def test_characteristics_se_guardan_como_rol(self) -> None:
        self.assertEqual(
            self.caps.subtitle_tracks[0].role,
            "public.accessibility.transcribes-spoken-dialog",
        )

    def test_closed_captions_sin_uri_siguen_declarandose(self) -> None:
        cc = self.caps.subtitle_tracks[1]
        self.assertEqual(cc.metadata["hls_type"], "CLOSED-CAPTIONS")
        self.assertEqual(cc.metadata["instream_id"], "CC1")
        self.assertIsNone(cc.uri)

    def test_una_sola_variante_sin_abr(self) -> None:
        self.assertFalse(self.caps.adaptive_bitrate)
        self.assertFalse(self.caps.has_selectable_quality)

    def test_un_audio_un_sub_forzado_no_abre_selector(self) -> None:
        self.assertFalse(self.caps.has_selectable_audio)
        # 1 subtítulo real + 1 CC = dos "pistas de texto", pero sólo una es
        # seleccionable con URI: la CC no se puede pedir aparte.
        self.assertEqual(len(self.caps.subtitle_uri_by_id), 1)


class TestMasterCodecsConComas(unittest.TestCase):
    def test_parametro_entrecomillado_no_aparece_como_codec(self) -> None:
        caps = parse_master_playlist(fixture("master_codecs_comma.m3u8"), final_url=BASE)
        codecs = caps.video_variants[0].metadata["codecs"]
        self.assertEqual(codecs, ["avc1.640028", "mp4a.40.5", "channels=2"])
        self.assertEqual(caps.video_variants[0].codec, "avc1.640028")

    def test_dos_audios_mismo_idioma_distintos_canales(self) -> None:
        caps = parse_master_playlist(fixture("master_codecs_comma.m3u8"), final_url=BASE)
        self.assertEqual([t.id for t in caps.audio_tracks], ["a1", "a2"])
        self.assertEqual([t.channels for t in caps.audio_tracks], ["6", "2"])


class TestMediaPlaylist(unittest.TestCase):
    def setUp(self) -> None:
        self.caps = parse_media_playlist(fixture("media_simple.m3u8"), final_url=BASE)

    def test_una_sola_pista_de_cada(self) -> None:
        self.assertEqual(len(self.caps.audio_tracks), 1)
        self.assertEqual(len(self.caps.video_variants), 1)
        self.assertEqual(self.caps.subtitle_tracks, [])

    def test_no_hay_nada_que_elegir(self) -> None:
        self.assertFalse(self.caps.has_any_choice)
        self.assertFalse(self.caps.adaptive_bitrate)

    def test_degradacion_explicita(self) -> None:
        self.assertEqual(self.caps.degraded_reason, DEGRADED_NOT_EXPOSED)
        self.assertIn("6s", self.caps.note or "")

    def test_vod_con_endlist_no_es_directo(self) -> None:
        texto = fixture("media_simple.m3u8") + "#EXT-X-ENDLIST\n"
        caps = parse_media_playlist(texto, final_url=BASE)
        self.assertFalse(caps.live)


class TestParseHls(unittest.TestCase):
    def test_detecta_master(self) -> None:
        self.assertTrue(is_master_playlist(fixture("master_full.m3u8")))
        self.assertFalse(is_master_playlist(fixture("media_simple.m3u8")))
        self.assertFalse(is_master_playlist(""))

    def test_parse_hls_reparte_al_bueno(self) -> None:
        self.assertTrue(parse_hls(fixture("master_full.m3u8")).adaptive_bitrate)
        self.assertFalse(parse_hls(fixture("media_simple.m3u8")).adaptive_bitrate)

    def test_salto_de_lineas_crlf(self) -> None:
        texto = fixture("media_simple.m3u8").replace("\n", "\r\n")
        self.assertEqual(len(parse_media_playlist(texto, final_url=BASE).video_variants), 1)


class TestManifiestosInvalidos(unittest.TestCase):
    def test_vacio(self) -> None:
        with self.assertRaises(HlsParseError):
            parse_master_playlist("")
        with self.assertRaises(HlsParseError):
            parse_media_playlist("   \n")

    def test_sin_extm3u(self) -> None:
        with self.assertRaises(HlsParseError) as ctx:
            parse_master_playlist("#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n")
        self.assertIn("#EXTM3U", str(ctx.exception))

    def test_master_sin_variantes_ni_pistas(self) -> None:
        with self.assertRaises(HlsParseError):
            parse_master_playlist(fixture("master_broken.m3u8"))

    def test_manifest_broken_aun_asi_produce_una_variante(self) -> None:
        # BANDWIDTH=abc y RESOLUTION=notasize no pueden leerse: la variante
        # sigue existiendo y se muestra sin esos datos, no se descarta.
        caps = parse_master_playlist(
            fixture("master_broken.m3u8") + "\nroto.m3u8\n", final_url=BASE
        )
        self.assertEqual(len(caps.video_variants), 1)
        variante = caps.video_variants[0]
        self.assertIsNone(variante.bitrate)
        self.assertIsNone(variante.height)
        self.assertEqual(variante.uri, "https://h.test/live/canal/roto.m3u8")

    def test_media_playlist_sin_segmentos(self) -> None:
        with self.assertRaises(HlsParseError):
            parse_media_playlist("#EXTM3U\n#EXT-X-VERSION:3\n")

    def test_extinf_sin_bandwidth_no_revienta(self) -> None:
        texto = "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\nx.m3u8\n"
        caps = parse_master_playlist(texto, final_url=BASE)
        self.assertEqual(len(caps.video_variants), 1)

    def test_uri_absoluta_no_se_toca(self) -> None:
        texto = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            "https://otro.test/a/b/index.m3u8\n"
        )
        caps = parse_master_playlist(texto, final_url=BASE)
        self.assertEqual(caps.video_variants[0].uri, "https://otro.test/a/b/index.m3u8")

    def test_sin_final_url_la_uri_se_conserva(self) -> None:
        texto = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nb/index.m3u8\n"
        caps = parse_master_playlist(texto)
        self.assertEqual(caps.video_variants[0].uri, "b/index.m3u8")