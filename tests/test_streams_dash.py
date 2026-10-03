"""Tests del parser DASH (plan F1, SDD §10/§38).

Casos del SDD §38 para DASH: audio, vídeo, texto, varias ``Representation``,
idiomas y resoluciones. Además se comprueba que el MPD se lee con el parser
endurecido (sin DTD ni entidades) y que un MPD de una sola representación
no abre ningún selector.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from thetvview.security.errors import ParseError, ResponseTooLargeError
from thetvview.streams.dash import DASH_MAX_BYTES, parse_dash
from thetvview.tracks.labels import video_label
from thetvview.tracks.models import PROTO_DASH

FIXTURES = Path(__file__).parent / "fixtures" / "streams"
BASE = "https://h.test/dash/manifest.mpd"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestMpdCompleto(unittest.TestCase):
    def setUp(self) -> None:
        self.caps = parse_dash(fixture("full.mpd"), final_url=BASE)

    def test_protocolo_y_directo(self) -> None:
        self.assertEqual(self.caps.protocol, PROTO_DASH)
        self.assertTrue(self.caps.live)  # type="dynamic"
        self.assertEqual(self.caps.final_url, BASE)

    def test_tres_representaciones_de_video(self) -> None:
        self.assertEqual(
            [t.id for t in self.caps.video_variants], ["v360", "v720", "v1080"]
        )
        self.assertTrue(self.caps.adaptive_bitrate)

    def test_resoluciones_y_bitrates(self) -> None:
        alta = self.caps.video_variants[-1]
        self.assertEqual((alta.width, alta.height), (1920, 1080))
        self.assertEqual(alta.bitrate, 6_800_000)
        self.assertEqual(alta.codec, "avc1.640028")

    def test_framerate_fraccion(self) -> None:
        media = self.caps.video_variants[1]
        self.assertEqual(media.height, 720)
        self.assertAlmostEqual(media.fps or 0, 29.97, places=2)

    def test_framerate_entero(self) -> None:
        self.assertEqual(self.caps.video_variants[0].fps, 25.0)

    def test_tres_pistas_de_audio_con_idiomas(self) -> None:
        self.assertEqual(len(self.caps.audio_tracks), 3)
        self.assertEqual([t.id for t in self.caps.audio_tracks],
                         ["a1", "a2", "a3"])
        self.assertEqual([t.language for t in self.caps.audio_tracks],
                         ["es", "en", "en"])

    def test_roles_de_audio(self) -> None:
        self.assertEqual(self.caps.audio_tracks[0].role, "main")
        self.assertEqual(self.caps.audio_tracks[1].role, "dub")
        self.assertEqual(self.caps.audio_tracks[2].role, "commentary")

    def test_texto_por_content_type(self) -> None:
        # Uno es WebVTT (mimeType) y otro TTML: ambos AdaptationSet de
        # texto, aunque usen mimeType distinto.
        self.assertEqual([t.id for t in self.caps.subtitle_tracks], ["s1", "s2"])
        self.assertEqual(self.caps.subtitle_tracks[0].codec, "wvtt")
        self.assertEqual(self.caps.subtitle_tracks[1].codec, "stpp")
        self.assertEqual(self.caps.subtitle_tracks[0].role, "subtitle")

    def test_uri_absolutizada(self) -> None:
        self.assertEqual(self.caps.audio_tracks[0].uri,
                         "https://h.test/dash/audio/es.m4a")
        self.assertEqual(self.caps.video_uri_by_id["v720"],
                         "https://h.test/dash/video/720.mp4")

    def test_etiqueta_de_video_amigable(self) -> None:
        self.assertEqual(video_label(self.caps.video_variants[1]),
                         "720p — 3800 kbps — H.264")

    def test_no_hay_subtitulo_inventado(self) -> None:
        self.assertEqual(len(self.caps.subtitle_tracks), 2)
        self.assertEqual(len(self.caps.subtitle_uri_by_id), 2)


class TestMpdDeUnaRepresentacion(unittest.TestCase):
    def setUp(self) -> None:
        self.caps = parse_dash(fixture("minimal.mpd"), final_url=BASE)

    def test_una_pista_de_cada_y_nada_que_elegir(self) -> None:
        self.assertEqual(len(self.caps.video_variants), 1)
        self.assertEqual(len(self.caps.audio_tracks), 1)
        self.assertEqual(self.caps.subtitle_tracks, [])
        self.assertFalse(self.caps.has_any_choice)
        self.assertFalse(self.caps.adaptive_bitrate)

    def test_es_vod(self) -> None:
        self.assertFalse(self.caps.live)

    def test_audio_implicito_es_el_default(self) -> None:
        self.assertTrue(self.caps.audio_tracks[0].is_default)
        self.assertIsNone(self.caps.audio_tracks[0].language)

    def test_uri_absoluta_no_se_toca(self) -> None:
        self.assertEqual(self.caps.video_variants[0].uri,
                         "https://h.test/dash/720.mp4")


class TestMpdSinNamespace(unittest.TestCase):
    """DASH en la práctica no lleva namespace fijo: se compara sin prefijo."""

    def test_sin_namespace(self) -> None:
        mpd = (
            '<MPD type="static"><Period><AdaptationSet mimeType="video/mp4">'
            '<Representation id="a" bandwidth="1" width="640" height="360"/>'
            '<Representation id="b" bandwidth="2" width="1280" height="720"/>'
            "</AdaptationSet></Period></MPD>"
        )
        caps = parse_dash(mpd)
        self.assertEqual([t.height for t in caps.video_variants], [360, 720])
        self.assertTrue(caps.adaptive_bitrate)

    def test_baseurl_en_el_adaptationset(self) -> None:
        mpd = (
            '<MPD><Period><AdaptationSet mimeType="video/mp4" baseUrl="v/">'
            '<Representation id="a" bandwidth="1" width="1280" height="720"/>'
            "</AdaptationSet></Period></MPD>"
        )
        caps = parse_dash(mpd, final_url="https://h.test/d/manifest.mpd")
        self.assertEqual(caps.video_variants[0].uri, "https://h.test/d/v/")


class TestMpdHostil(unittest.TestCase):
    def test_dtd_rechazado(self) -> None:
        mpd = (
            '<!DOCTYPE MPD [<!ENTITY x "y">]>'
            "<MPD><Period/></MPD>"
        )
        with self.assertRaises(ParseError):
            parse_dash(mpd)

    def test_entidades_rechazadas(self) -> None:
        mpd = '<MPD><!ENTITY lol "haha"><Period/></MPD>'
        with self.assertRaises(ParseError):
            parse_dash(mpd)

    def test_xml_malformado(self) -> None:
        with self.assertRaises(Exception):
            parse_dash("<MPD><Period>")

    def test_no_es_un_mpd(self) -> None:
        with self.assertRaises(ValueError):
            parse_dash("<playlist><x/></playlist>")

    def test_tope_de_bytes(self) -> None:
        grande = (
            '<MPD><Period><AdaptationSet mimeType="video/mp4">'
            '<Representation id="a" bandwidth="1" width="1" height="1"/>'
            "</AdaptationSet></Period></MPD>"
        )
        with self.assertRaises(ResponseTooLargeError):
            parse_dash(grande, max_bytes=10)

    def test_tope_de_nodos(self) -> None:
        with self.assertRaises(ParseError):
            parse_dash(fixture("full.mpd"), max_bytes=DASH_MAX_BYTES, max_nodes=5)

    def test_adaptationset_sin_representations_se_ignora(self) -> None:
        mpd = '<MPD><Period><AdaptationSet mimeType="audio/mp4" lang="es"/></Period></MPD>'
        caps = parse_dash(mpd)
        self.assertEqual(len(caps.audio_tracks), 1)  # sólo el implícito
        self.assertTrue(caps.audio_tracks[0].is_default)


class TestParserSeguro(unittest.TestCase):
    def test_dash_acepta_bytes(self) -> None:
        caps = parse_dash(fixture("minimal.mpd").encode("utf-8"))
        self.assertEqual(len(caps.video_variants), 1)

    def test_bom_utf8(self) -> None:
        caps = parse_dash("﻿" + fixture("minimal.mpd"))
        self.assertEqual(len(caps.video_variants), 1)