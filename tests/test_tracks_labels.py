"""Tests de las etiquetas de audio/subtítulos/vídeo (plan F0, SDD §24/§25).

La regla que más importa aquí es negativa: **lo que el manifiesto no
declara no aparece**. Un canal sin resolución no puede mostrar "1080p" sólo
porque su nombre en la M3U lo diga (eso es otro módulo).
"""

from __future__ import annotations

import unittest

from thetvview.tracks.labels import (
    audio_label,
    audio_role_suffix,
    bitrate_kbps,
    codec_label,
    resolution_label,
    subtitle_label,
    video_label,
)
from thetvview.tracks.models import AUDIO, TEXT, VIDEO, MediaTrack


def aud(**kw) -> MediaTrack:
    return MediaTrack(id="a1", type=AUDIO, **kw)


def sub(**kw) -> MediaTrack:
    return MediaTrack(id="s1", type=TEXT, **kw)


def vid(**kw) -> MediaTrack:
    return MediaTrack(id="v1080", type=VIDEO, **kw)


class TestAudioLabel(unittest.TestCase):
    def test_prioridad_name_primero(self) -> None:
        track = aud(label="Español (dub)", language="es")
        self.assertEqual(audio_label(track, 1), "Español (dub)")

    def test_si_no_hay_name_manda_el_idioma(self) -> None:
        self.assertEqual(audio_label(aud(language="spa"), 1), "Español")
        self.assertEqual(audio_label(aud(language="en"), 2), "English")
        self.assertEqual(audio_label(aud(language="fr"), 3), "Français")

    def test_si_no_hay_idioma_manda_el_codigo_crudo(self) -> None:
        self.assertEqual(audio_label(aud(original_language="Serranés")), "Serranés")
        self.assertEqual(audio_label(aud(language="und")), "und")

    def test_ultimo_recurso_es_audio_n(self) -> None:
        self.assertEqual(audio_label(aud(), 4), "Audio #4")
        self.assertEqual(audio_label(aud(), 0), "Audio #1")

    def test_label_en_blanco_no_cuenta(self) -> None:
        self.assertEqual(audio_label(aud(label="   ", language="spa")), "Español")
        self.assertEqual(audio_label(aud(label="\n")), "Audio #1")

    def test_label_se_limpia(self) -> None:
        track = aud(label="  English   commentary  ")
        self.assertEqual(audio_label(track, 1), "English commentary")

    def test_rol_como_sufijo(self) -> None:
        self.assertEqual(audio_role_suffix(aud(role="dub")), " (dub)")
        self.assertEqual(audio_role_suffix(aud(role="main")), "")
        self.assertEqual(audio_role_suffix(aud()), "")


class TestSubtitleLabel(unittest.TestCase):
    def test_idioma_primero_para_subtitulos(self) -> None:
        track = sub(language="es", label="1")
        self.assertEqual(subtitle_label(track, 1), "Español · 1")

    def test_solo_idioma(self) -> None:
        self.assertEqual(subtitle_label(sub(language="es"), 1), "Español")

    def test_solo_name(self) -> None:
        self.assertEqual(subtitle_label(sub(label="Forced"), 1), "Forced")

    def test_ultimo_recurso(self) -> None:
        self.assertEqual(subtitle_label(sub(), 3), "Subtítulo #3")

    def test_no_duplica_el_nombre_igual_al_idioma(self) -> None:
        track = sub(language="es", label="Español")
        self.assertEqual(subtitle_label(track, 1), "Español")


class TestVideoLabel(unittest.TestCase):
    def test_resolucion_bitrate_codec(self) -> None:
        track = vid(height=1080, width=1920, bitrate=6_000_000, codec="avc1.640028")
        self.assertEqual(video_label(track), "1080p — 6000 kbps — H.264")

    def test_solo_resolucion(self) -> None:
        self.assertEqual(video_label(vid(height=720)), "720p")

    def test_solo_bitrate(self) -> None:
        self.assertEqual(video_label(vid(bitrate=3_000_000)), "3000 kbps")

    def test_sin_resolucion_menor_se_etiqueta_por_bitrate(self) -> None:
        track = vid(bitrate=1_200_000, codec="h265")
        self.assertEqual(video_label(track), "1200 kbps — H.265")

    def test_sin_datos_no_inventa(self) -> None:
        self.assertEqual(video_label(vid()), "Automática")
        self.assertEqual(video_label(None), "Automática")

    def test_no_adivina_el_entrelazado(self) -> None:
        # 1080p30 y 1080i50 declaran FRAME-RATE parecidos: poner "i" sería
        # inventar. La altura es lo único fiable.
        self.assertEqual(resolution_label(vid(height=1080, fps=50)), "1080p")
        self.assertEqual(resolution_label(vid(height=1080, fps=29.97)), "1080p")

    def test_resolucion_none_si_no_hay_altura(self) -> None:
        self.assertIsNone(resolution_label(vid(width=1920)))

    def test_bitrate_basura_o_cero(self) -> None:
        self.assertIsNone(bitrate_kbps(None))
        self.assertIsNone(bitrate_kbps(0))
        self.assertIsNone(bitrate_kbps("x"))
        self.assertEqual(bitrate_kbps(1_500_000), 1500)


class TestCodecLabel(unittest.TestCase):
    def test_codigos_conocidos(self) -> None:
        self.assertEqual(codec_label("h264"), "H.264")
        self.assertEqual(codec_label("hevc"), "H.265")
        self.assertEqual(codec_label("AVC1.640028"), "H.264")
        self.assertEqual(codec_label("mp4a.40.2"), "AAC")

    def test_codigo_desconocido_se_devuelve_tal_cual(self) -> None:
        self.assertEqual(codec_label("MiCoso"), "MiCoso")

    def test_none(self) -> None:
        self.assertIsNone(codec_label(None))
        self.assertIsNone(codec_label("   "))