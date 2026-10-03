"""Tests de las preferencias de pistas (plan F4, SDD §31/§37).

Lo que se fija:

- la precedencia **canal → proveedor → global → DEFAULT del stream**, campo a
  campo (un valor puede venir de un ámbito y otro de otro);
- el ``prefs.json`` de pistas **no contiene secretos ni URLs**: la clave de
  un canal es el ``tvg-id`` o el hash de su URL **redactada** (H7), y los
  valores son sólo idiomas y calidades;
- retrocompatibilidad: un ``prefs.json`` viejo (sin las claves nuevas) carga
  igual y no se rompe nada (H11).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from thetvview.models import Channel
from thetvview.prefs import Prefs, PrefsManager
from thetvview.security.redaction import contains_embedded_login
from thetvview.tracks.manager import QUICK_AUTO, selection_for
from thetvview.tracks.models import AUDIO, VIDEO, MediaCapabilities, MediaTrack, PlaybackSelection
from thetvview.tracks.prefs import (
    GLOBAL_KEY,
    TrackPreferences,
    channel_key,
    load_scopes,
    preferences_from_selection,
    provider_key,
    remember_for_channel,
    remember_for_provider,
    resolve_preferences,
)

# URL con credenciales en el path, estilo Xtream.
SECRETA = "http://proveedor.test/live/alice/P4ssw0rd/1234.m3u8"


def canal(url: str = "http://proveedor.test/live/x.m3u8", tvg: str | None = None) -> Channel:
    return Channel(name="Canal", url=url, tvg_id=tvg)


def caps() -> MediaCapabilities:
    return MediaCapabilities(
        audio_tracks=[
            MediaTrack(id="a1", type=AUDIO, language="es", is_default=True,
                       uri="https://h/a1.m3u8"),
            MediaTrack(id="a2", type=AUDIO, language="ca",
                       uri="https://h/a2.m3u8"),
        ],
        video_variants=[
            MediaTrack(id="v720", type=VIDEO, height=720, uri="https://h/v720.m3u8"),
            MediaTrack(id="v1080", type=VIDEO, height=1080, uri="https://h/v1080.m3u8"),
        ],
        adaptive_bitrate=True,
    )


class TestClaves(unittest.TestCase):
    def test_tvg_id_gana(self) -> None:
        self.assertEqual(channel_key(canal(tvg="canal.test.es")), "tvg:canal.test.es")

    def test_sin_tvg_id_es_un_hash(self) -> None:
        clave = channel_key(canal())
        assert clave is not None
        self.assertTrue(clave.startswith("channel:"))
        self.assertEqual(len(clave), len("channel:") + 32)

    def test_la_clave_no_contiene_la_url(self) -> None:
        clave = channel_key(canal(url=SECRETA))
        assert clave is not None
        self.assertNotIn("alice", clave)
        self.assertNotIn("P4ssw0rd", clave)
        self.assertFalse(contains_embedded_login(clave))

    def test_la_clave_es_estable(self) -> None:
        self.assertEqual(channel_key(canal()), channel_key(canal()))
        self.assertNotEqual(channel_key(canal()), channel_key(canal(url="http://otro/x.m3u8")))

    def test_canales_equivalentes_comparten_clave(self) -> None:
        # La clave se calcula sobre la URL redactada: es lo que garantiza que
        # no se pueda deshacer un secreto desde prefs.json. El precio
        # asumido es que dos URLs que sólo difieren en usuario/contraseña
        # comparten preferencias (documentado en channel_key).
        a = channel_key(canal(url="http://h/live/alice/secreto/1.ts"))
        b = channel_key(canal(url="http://h/live/bob/secreto/1.ts"))
        self.assertEqual(a, b)
        # En cambio, si no hay credenciales embebidas, cada canal va por su
        # sitio: es el caso real de una M3U normal.
        c = channel_key(canal(url="http://h/live/canal1.m3u8"))
        d = channel_key(canal(url="http://h/live/canal2.m3u8"))
        self.assertNotEqual(c, d)

    def test_canal_sin_url_ni_tvg_no_tiene_clave(self) -> None:
        self.assertIsNone(channel_key(Channel(name="x", url="")))
        self.assertIsNone(channel_key(None))

    def test_clave_de_proveedor_es_el_host(self) -> None:
        self.assertEqual(provider_key("https://Proveedor.test/live/user/pass"),
                         "provider:proveedor.test")
        self.assertIsNone(provider_key(""))
        self.assertIsNone(provider_key("/ruta/local.m3u"))
        self.assertIsNone(provider_key(None))


class TestPrecedencia(unittest.TestCase):
    def setUp(self) -> None:
        self.scopes = {
            GLOBAL_KEY: {
                "preferred_audio_language": "en",
                "preferred_subtitle_language": "en",
                "subtitles_enabled": True,
                "preferred_quality": QUICK_AUTO,
            },
            "provider:proveedor.test": {
                "preferred_audio_language": "ca",
            },
            channel_key(canal()): {
                "preferred_audio_language": "es",
            },
        }

    def test_canal_gana(self) -> None:
        resuelta = resolve_preferences(
            canal(), "https://proveedor.test/live/u/p/x.m3u8", scopes=self.scopes
        )
        self.assertEqual(resuelta.preferred_audio_language, "es")
        self.assertEqual(resuelta.scope, "canal")

    def test_proveedor_cuando_el_canal_no_dice(self) -> None:
        resuelta = resolve_preferences(
            canal(), "https://proveedor.test/live/u/p/x.m3u8", scopes=self.scopes
        )
        # El canal no fija subtítulos ni calidad: entra el global.
        self.assertEqual(resuelta.preferred_subtitle_language, "en")
        self.assertTrue(resuelta.subtitles_enabled)
        self.assertEqual(resuelta.preferred_quality, QUICK_AUTO)

    def test_proveedor_si_el_canal_no_dice(self) -> None:
        scopes_sin_canal = {
            k: v for k, v in self.scopes.items() if not k.startswith("channel:")
        }
        resuelta = resolve_preferences(
            canal(url="http://otro.test/x.m3u8"),
            "https://proveedor.test/live/u/p/x.m3u8",
            scopes=scopes_sin_canal,
        )
        self.assertEqual(resuelta.preferred_audio_language, "ca")
        self.assertEqual(resuelta.scope, "proveedor")

    def test_canal_tiene_prioridad_sobre_proveedor(self) -> None:
        resuelta = resolve_preferences(
            canal(), "https://proveedor.test/live/u/p/x.m3u8", scopes=self.scopes
        )
        self.assertEqual(resuelta.preferred_audio_language, "es")
        self.assertEqual(resuelta.scope, "canal")

    def test_global_si_no_hay_mas(self) -> None:
        resuelta = resolve_preferences(
            canal(url="http://otro.test/x.m3u8"),
            "https://otro.test/live/x.m3u8",
            scopes={GLOBAL_KEY: {"preferred_audio_language": "fr"}},
            prefs=Prefs(),
        )
        self.assertEqual(resuelta.preferred_audio_language, "fr")
        self.assertEqual(resuelta.scope, "global")

    def test_prefs_globales_como_ultimo_recurso(self) -> None:
        resuelta = resolve_preferences(
            canal(url="http://otro.test/x.m3u8"),
            "http://otro.test/x.m3u8",
            scopes={},
            prefs=Prefs(preferred_audio_language="de"),
        )
        self.assertEqual(resuelta.preferred_audio_language, "de")

    def test_sin_nada_devuelve_vacio(self) -> None:
        resuelta = resolve_preferences(canal(), None, scopes={}, prefs=Prefs())
        self.assertIsNone(resuelta.preferred_audio_language)
        self.assertIsNone(resuelta.preferred_quality)
        self.assertFalse(resuelta.subtitles_enabled)
        self.assertTrue(resuelta.is_empty())

    def test_el_gestor_consume_el_formato_resuelto(self) -> None:
        resuelta = resolve_preferences(
            canal(), "https://proveedor.test/x", scopes=self.scopes, prefs=Prefs()
        )
        sel = selection_for(caps(), resuelta)
        self.assertEqual(sel.audio_track_id, "a1")  # "es"


class TestPersistencia(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "prefs.json"
        self.pm = PrefsManager(self.path)

    def test_ida_y_vuelta(self) -> None:
        self.pm.save(Prefs(last_player="mpv", preferred_audio_language="es"))
        prefs = self.pm.load()
        self.assertEqual(prefs.last_player, "mpv")
        self.assertEqual(prefs.preferred_audio_language, "es")
        self.assertTrue(prefs.ask_track_options)
        self.assertFalse(prefs.subtitles_enabled)

    def test_prefs_viejo_sigue_cargando(self) -> None:
        self.path.write_text(
            json.dumps({"last_player": "vlc", "theme": "dark", "clave_rara": 1}),
            encoding="utf-8",
        )
        prefs = self.pm.load()
        self.assertEqual(prefs.last_player, "vlc")
        self.assertEqual(prefs.theme, "dark")
        self.assertIsNone(prefs.preferred_audio_language)
        self.assertEqual(prefs.track_prefs, {})

    def test_recordar_para_el_canal(self) -> None:
        clave = remember_for_channel(
            self.pm,
            canal(url=SECRETA),
            TrackPreferences(preferred_audio_language="ca"),
        )
        self.assertIsNotNone(clave)
        prefs = self.pm.load()
        self.assertEqual(prefs.track_prefs[clave]["preferred_audio_language"], "ca")
        # Y al releer, la preferencia se aplica.
        resuelta = resolve_preferences(
            canal(url=SECRETA), None, scopes=load_scopes(self.pm), prefs=prefs
        )
        self.assertEqual(resuelta.preferred_audio_language, "ca")

    def test_recordar_para_el_proveedor(self) -> None:
        clave = remember_for_provider(
            self.pm, "https://proveedor.test/live/u/p/x.m3u8",
            TrackPreferences(preferred_quality="720p"),
        )
        self.assertEqual(clave, "provider:proveedor.test")
        prefs = self.pm.load()
        self.assertEqual(prefs.track_prefs[clave]["preferred_quality"], "720p")

    def test_recordar_sobreescribe_el_ambito(self) -> None:
        remember_for_channel(self.pm, canal(), TrackPreferences(preferred_audio_language="ca"))
        remember_for_channel(self.pm, canal(), TrackPreferences(preferred_audio_language="fr"))
        scopes = load_scopes(self.pm)
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[channel_key(canal())]["preferred_audio_language"], "fr")

    def test_recordar_vacio_borra_el_bloque(self) -> None:
        remember_for_channel(self.pm, canal(), TrackPreferences(preferred_audio_language="ca"))
        remember_for_channel(self.pm, canal(), TrackPreferences())
        self.assertEqual(load_scopes(self.pm), {})

    def test_canales_distintos_no_se_pisan(self) -> None:
        remember_for_channel(self.pm, canal(), TrackPreferences(preferred_audio_language="es"))
        remember_for_channel(self.pm, canal(url="http://otro.test/y.m3u8"),
                             TrackPreferences(preferred_audio_language="ca"))
        scopes = load_scopes(self.pm)
        self.assertEqual(len(scopes), 2)

    def test_track_prefs_basura_se_ignora(self) -> None:
        self.path.write_text(
            json.dumps({"track_prefs": {"canal:x": "no soy dict", "": {}, "k": {"inventada": 1}}}),
            encoding="utf-8",
        )
        self.assertEqual(self.pm.load().track_prefs, {})

    def test_no_se_persiste_la_url_cruda(self) -> None:
        """H7: ni la URL, ni el usuario, ni la contraseña, en prefs.json."""
        remember_for_channel(
            self.pm, canal(url=SECRETA),
            TrackPreferences(preferred_audio_language="ca", preferred_quality="720p"),
        )
        texto = self.path.read_text(encoding="utf-8")
        self.assertNotIn("alice", texto)
        self.assertNotIn("P4ssw0rd", texto)
        self.assertNotIn("proveedor.test/live", texto)
        self.assertFalse(contains_embedded_login(texto))
        # Y tampoco se guarda ninguna URL en los valores.
        for bloque in json.loads(texto).get("track_prefs", {}).values():
            for valor in bloque.values():
                if isinstance(valor, str):
                    self.assertNotIn("http", valor)

    def test_no_se_persiste_nada_si_el_canal_no_tiene_clave(self) -> None:
        self.assertIsNone(
            remember_for_channel(self.pm, Channel(name="x", url=""),
                                 TrackPreferences(preferred_audio_language="ca"))
        )
        self.assertEqual(self.pm.load().track_prefs, {})

    def test_recordar_si_el_disco_falla_no_rompe(self) -> None:
        class _Roto(PrefsManager):
            def save(self, prefs):  # type: ignore[override]
                raise OSError("disco lleno")

        roto = _Roto(self.path)
        self.assertIsNotNone(
            remember_for_channel(roto, canal(), TrackPreferences(preferred_audio_language="ca"))
        )


class TestDeSeleccionAPreferencias(unittest.TestCase):
    def test_idioma_se_guarda_normalizado(self) -> None:
        c = caps()
        sel = PlaybackSelection(audio_track_id="a2")
        prefs = preferences_from_selection(c, sel)
        self.assertEqual(prefs.preferred_audio_language, "ca")

    def test_calidad_se_guarda_como_altura(self) -> None:
        c = caps()
        sel = PlaybackSelection(video_track_id="v1080", auto_quality=False)
        prefs = preferences_from_selection(c, sel)
        self.assertEqual(prefs.preferred_quality, "1080p")

    def test_auto_no_guarda_calidad(self) -> None:
        prefs = preferences_from_selection(caps(), PlaybackSelection())
        self.assertIsNone(prefs.preferred_quality)

    def test_subtitulos(self) -> None:
        sel = PlaybackSelection(subtitles_enabled=True)
        prefs = preferences_from_selection(caps(), sel)
        self.assertTrue(prefs.subtitles_enabled)

    def test_seleccion_vacia(self) -> None:
        prefs = preferences_from_selection(caps(), None)
        self.assertTrue(prefs.is_empty())

    def test_ida_y_vuelta_con_el_gestor(self) -> None:
        c = caps()
        prefs = preferences_from_selection(
            c, PlaybackSelection(audio_track_id="a2", video_track_id="v720",
                                auto_quality=False, subtitles_enabled=False)
        )
        self.assertEqual(prefs.preferred_audio_language, "ca")
        self.assertEqual(prefs.preferred_quality, "720p")
        sel = selection_for(c, prefs)
        self.assertEqual(sel.audio_track_id, "a2")
        self.assertEqual(sel.video_track_id, "v720")
        self.assertFalse(sel.auto_quality)