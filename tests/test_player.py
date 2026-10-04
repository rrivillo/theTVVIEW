"""Tests de player.py: construcción de comando y lanzamiento seguro.

Todos los testeos que involucran reproductores se hacen en modo headless
(command_for/launch con headless=True) o con subprocess.Popen mockeado:
nunca se abre ventana ni se requiere DISPLAY/Wayland en CI.
"""

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview import config
from thetvview.models import Channel
from thetvview.player import (
    PlayerError,
    _codec_h264_args,
    _headless_args,
    _option_args,
    command_for,
    explain_early_exit,
)


def _channel(**opts) -> Channel:
    return Channel(name="Test TV", url="http://stream.example.com/live", **opts)


class TestUserAgent(unittest.TestCase):
    """El User-Agent del reproductor es el de la app, salvo que el canal
    diga otro (medido: hay CDNs que sólo sirven los segmentos a esa
    cabecera y responden 403 a mpv, ffmpeg y Chrome por igual)."""

    def test_mpv_recibe_el_ua_de_la_app(self) -> None:
        cmd = command_for(_channel(), "mpv", player_path="/usr/bin/mpv")
        self.assertIn("--user-agent=theTVVIEW/1.0", cmd)

    def test_mplayer_recibe_el_ua_en_dos_argumentos(self) -> None:
        cmd = command_for(_channel(), "mplayer", player_path="/usr/bin/mplayer")
        self.assertIn("-user-agent", cmd)
        self.assertEqual(cmd[cmd.index("-user-agent") + 1], "theTVVIEW/1.0")

    def test_vlc_recibe_su_propia_opcion(self) -> None:
        cmd = command_for(_channel(), "vlc", player_path="/usr/bin/vlc")
        self.assertIn("--http-user-agent=theTVVIEW/1.0", cmd)

    def test_el_ua_del_canal_gana_y_no_se_duplica(self) -> None:
        canal = _channel(
            extra_options=[("EXTVLCOPT", "http-user-agent=Mozilla/5.0 Propio")]
        )
        for reproductor, path, opcion in (
            ("mpv", "/usr/bin/mpv", "--user-agent="),
            ("mplayer", "/usr/bin/mplayer", "-user-agent"),
            ("vlc", "/usr/bin/vlc", "--http-user-agent="),
        ):
            with self.subTest(reproductor=reproductor):
                cmd = command_for(canal, reproductor, player_path=path)
                propio = f"{opcion}Mozilla/5.0 Propio"
                esperados = [propio] if reproductor != "mplayer" else [opcion]
                self.assertEqual([a for a in cmd if a in esperados], esperados)
                self.assertNotIn("theTVVIEW/1.0", cmd)

    def test_el_ua_no_mueve_el_separador(self) -> None:
        for reproductor, path in (
            ("mpv", "/usr/bin/mpv"),
            ("mplayer", "/usr/bin/mplayer"),
            ("vlc", "/usr/bin/vlc"),
        ):
            with self.subTest(reproductor=reproductor):
                cmd = command_for(_channel(), reproductor, player_path=path)
                self.assertEqual(cmd[-2], "--")

    def test_headless_y_ua_conviven(self) -> None:
        cmd = command_for(
            _channel(), "mpv", player_path="/usr/bin/mpv", headless=True
        )
        self.assertIn("--vo=null", cmd)
        self.assertIn("--ao=null", cmd)
        self.assertIn("--user-agent=theTVVIEW/1.0", cmd)

    def test_reproductor_desconocido_no_rompe(self) -> None:
        canal = _channel()
        path = "/usr/bin/otro"
        with mock.patch.object(config, "find_player", return_value=path):
            cmd = command_for(canal, "otro")
        self.assertNotIn("--user-agent=theTVVIEW/1.0", cmd)


class TestCommandFor(unittest.TestCase):
    def test_comando_basico_mpv(self) -> None:
        cmd = command_for(_channel(), "mpv", player_path="/usr/bin/mpv")
        self.assertEqual(
            cmd,
            [
                "/usr/bin/mpv",
                "--vd=ffh264",
                "--gpu-api=opengl",
                "--user-agent=theTVVIEW/1.0",
                "--title=Test TV",
                "--force-media-title=Test TV",
                "--",
                "http://stream.example.com/live",
            ],
        )

    def test_comando_basico_mplayer(self) -> None:
        cmd = command_for(_channel(), "mplayer", player_path="/usr/bin/mplayer")
        # Verificar que codec h264 está presente después del path
        self.assertEqual(cmd[0], "/usr/bin/mplayer")
        self.assertIn("-vc", cmd)
        self.assertEqual(cmd[cmd.index("-vc") + 1], "ffh264,ffmpeg2,ffmpeg1,h264,divx5")

    def test_comando_basico_vlc(self) -> None:
        cmd = command_for(_channel(), "vlc", player_path="/usr/bin/vlc")
        # Verificar que codec h264 está presente después del path
        self.assertEqual(cmd[0], "/usr/bin/vlc")
        self.assertIn("--avcodec-hw=any", cmd)
        self.assertIn("--avcodec-codec=h264", cmd)

    def test_extvlcopt_user_agent_por_player(self) -> None:
        ch = _channel(extra_options=[("EXTVLCOPT", "http-user-agent=MiApp/1.0")])
        self.assertEqual(
            command_for(ch, "mpv", player_path="/usr/bin/mpv"),
            [
                "/usr/bin/mpv",
                "--vd=ffh264",
                "--gpu-api=opengl",
                "--user-agent=MiApp/1.0",
                "--title=Test TV",
                "--force-media-title=Test TV",
                "--",
                "http://stream.example.com/live",
            ],
        )
        self.assertEqual(
            command_for(ch, "mplayer", player_path="/usr/bin/mplayer"),
            [
                "/usr/bin/mplayer",
                "-vc", "ffh264,ffmpeg2,ffmpeg1,h264,divx5",
                "-demuxer", "lavf",
                "-cache", "8192",
                "-prefer-ipv4",
                "-framedrop",
                "-osdlevel", "0",
                "-lavfdopts", "probesize=500000:analyzeduration=10000000",
                "-ac", "ffaac,ffmp2float,ffmp3float,ffac3,mpg123,mad",
                "-user-agent",
                "MiApp/1.0",
                "-title",
                "Test TV",
                "--",
                "http://stream.example.com/live",
            ],
        )
        self.assertEqual(
            command_for(ch, "vlc", player_path="/usr/bin/vlc"),
            [
                "/usr/bin/vlc",
                "--avcodec-hw=any",
                "--avcodec-codec=h264",
                "--http-user-agent=MiApp/1.0",
                "--input-title-format=Test TV",
                "--",
                "http://stream.example.com/live",
            ],
        )

    def test_http_referrer_estilo_real_por_player(self) -> None:
        # Convención habitual en listas IPTV reales (p. ej. IPTV-SV).
        ch = _channel(extra_options=[("EXTVLCOPT", "http-referrer=https://teleon.tv/")])
        self.assertEqual(
            _option_args(ch, "mpv")[0], ["--referrer=https://teleon.tv/"]
        )
        self.assertEqual(
            _option_args(ch, "vlc")[0], ["--http-referrer=https://teleon.tv/"]
        )
        self.assertEqual(
            _option_args(ch, "mplayer")[0],
            ["-http-referrer", "https://teleon.tv/"],
        )

    def test_alias_de_claves_se_normalizan(self) -> None:
        for raw in (
            "referrer=https://x.example/",
            "referer=https://x.example/",
            "http-referrer=https://x.example/",
        ):
            with self.subTest(raw=raw):
                ch = _channel(extra_options=[("EXTVLCOPT", raw)])
                self.assertEqual(
                    _option_args(ch, "vlc")[0], ["--http-referrer=https://x.example/"]
                )
        for raw in ("user-agent=App/2.0", "user_agent=App/2.0"):
            with self.subTest(raw=raw):
                ch = _channel(extra_options=[("EXTVLCOPT", raw)])
                self.assertEqual(_option_args(ch, "mpv")[0], ["--user-agent=App/2.0"])

    def test_kodiprop_ignorado_con_aviso(self) -> None:
        ch = _channel(
            extra_options=[
                ("KODIPROP", "inputstream=inputstream.adaptive"),
                ("EXTVLCOPT", "http-user-agent=X"),
            ]
        )
        args, warnings = _option_args(ch, "mpv")
        self.assertEqual(args, ["--user-agent=X"])
        self.assertTrue(any("KODIPROP" in w for w in warnings))

    def test_opcion_no_soportada_ignorada(self) -> None:
        ch = _channel(extra_options=[("EXTVLCOPT", "network-caching=3000")])
        args, warnings = _option_args(ch, "mpv")
        self.assertEqual(args, [])
        self.assertTrue(any("no soportada" in w for w in warnings))

    def test_valor_peligroso_ignorado(self) -> None:
        for evil in ("--evil=1", "", "x\x00y"):
            with self.subTest(evil=evil):
                ch = _channel(extra_options=[("EXTVLCOPT", f"http-user-agent={evil}")])
                args, _ = _option_args(ch, "mpv")
                self.assertNotIn(f"--user-agent={evil}", args)

    def test_player_inexistente_lanza_error_amigable(self) -> None:
        from thetvview import player

        original = player.config.find_player
        player.config.find_player = lambda name: None  # type: ignore[assignment]
        try:
            with self.assertRaises(PlayerError) as ctx:
                command_for(_channel(), "mpv")
            self.assertIn("no encontrado", str(ctx.exception))
        finally:
            player.config.find_player = original  # type: ignore[assignment]

    def test_mplayer_hls_usa_ffmpeg_prefijo_y_demuxer_lavf(self) -> None:
        # Workaround HLS: URLs .m3u8 y .m3u requieren ffmpeg:// para evitar
        # "Protocol not found [mp:/...]" por rutas relativas en el playlist.
        ch = Channel(name="HLS", url="https://example.com/live/playlist.m3u8")
        cmd = command_for(ch, "mplayer", player_path="/usr/bin/mplayer")
        self.assertIn("-demuxer", cmd)
        self.assertIn("lavf", cmd)
        # La URL final debe venir prefijada
        self.assertEqual(cmd[-1], "ffmpeg://https://example.com/live/playlist.m3u8")
        # No duplicar si ya viene con prefijo
        ch2 = Channel(name="HLS", url="ffmpeg://https://example.com/live/playlist.m3u8")
        cmd2 = command_for(ch2, "mplayer", player_path="/usr/bin/mplayer")
        self.assertEqual(cmd2[-1], "ffmpeg://https://example.com/live/playlist.m3u8")
        self.assertEqual(cmd2.count("ffmpeg://https://example.com/live/playlist.m3u8"), 1)
        # .m3u también debe recibir el prefijo
        ch3 = Channel(name="M3U", url="https://example.com/playlist.m3u")
        cmd3 = command_for(ch3, "mplayer", player_path="/usr/bin/mplayer")
        self.assertEqual(cmd3[-1], "ffmpeg://https://example.com/playlist.m3u")
        # .m3u con query string
        ch4 = Channel(name="M3U", url="https://example.com/playlist.m3u?id=123")
        cmd4 = command_for(ch4, "mplayer", player_path="/usr/bin/mplayer")
        self.assertEqual(cmd4[-1], "ffmpeg://https://example.com/playlist.m3u?id=123")
        # mpv/vlc no deben prefijar
        self.assertEqual(
            command_for(ch, "mpv", player_path="/usr/bin/mpv")[-1],
            "https://example.com/live/playlist.m3u8",
        )

    def test_codec_h264_args_mpv(self) -> None:
        args = _codec_h264_args("mpv")
        self.assertEqual(args, ["--vd=ffh264"])

    def test_codec_h264_args_mplayer(self) -> None:
        args = _codec_h264_args("mplayer")
        self.assertEqual(args, ["-vc", "ffh264,ffmpeg2,ffmpeg1,h264,divx5"])

    def test_codec_h264_args_vlc(self) -> None:
        args = _codec_h264_args("vlc")
        self.assertEqual(args, ["--avcodec-hw=any", "--avcodec-codec=h264"])

    def test_codec_h264_args_player_desconocido(self) -> None:
        args = _codec_h264_args("desconocido")
        self.assertEqual(args, [])


class TestHeadless(unittest.TestCase):
    """El modo headless añade drivers nulos/dummy y nunca abre GUI."""

    def test_headless_args_por_reproductor(self) -> None:
        self.assertEqual(_headless_args("mpv"), ["--vo=null", "--ao=null"])
        self.assertEqual(
            _headless_args("vlc"),
            ["--intf", "dummy", "--vout", "dummy", "--aout", "dummy"],
        )
        self.assertEqual(_headless_args("mplayer"), ["-vo", "null", "-ao", "null"])
        self.assertEqual(_headless_args("desconocido"), [])

    def test_por_defecto_no_es_headless(self) -> None:
        cmd = command_for(_channel(), "mpv", player_path="/usr/bin/mpv")
        self.assertNotIn("--vo=null", cmd)
        self.assertNotIn("--ao=null", cmd)
        self.assertIn("--gpu-api=opengl", cmd)

    def test_mpv_headless_sin_ventana_ni_gpu(self) -> None:
        cmd = command_for(_channel(), "mpv", player_path="/usr/bin/mpv", headless=True)
        self.assertIn("--vo=null", cmd)
        self.assertIn("--ao=null", cmd)
        # --vo=null no necesita GPU: no forzar opengl sin display.
        self.assertNotIn("--gpu-api=opengl", cmd)
        self.assertEqual(cmd[-1], "http://stream.example.com/live")

    def test_vlc_headless_es_dummy(self) -> None:
        cmd = command_for(_channel(), "vlc", player_path="/usr/bin/vlc", headless=True)
        for flag in ("--intf", "dummy", "--vout", "dummy", "--aout", "dummy"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[-1], "http://stream.example.com/live")

    def test_mplayer_headless_es_null(self) -> None:
        cmd = command_for(
            _channel(), "mplayer", player_path="/usr/bin/mplayer", headless=True
        )
        self.assertIn("-vo", cmd)
        self.assertIn("null", cmd)
        self.assertIn("-ao", cmd)
        self.assertEqual(cmd[-1], "http://stream.example.com/live")

    def test_headless_va_tras_el_binario(self) -> None:
        cmd = command_for(_channel(), "mpv", player_path="/usr/bin/mpv", headless=True)
        self.assertEqual(cmd[0], "/usr/bin/mpv")
        self.assertEqual(cmd[1:3], ["--vo=null", "--ao=null"])

    def test_headless_con_extvlcopt(self) -> None:
        ch = _channel(extra_options=[("EXTVLCOPT", "http-user-agent=MiApp/1.0")])
        cmd = command_for(ch, "mpv", player_path="/usr/bin/mpv", headless=True)
        self.assertIn("--vo=null", cmd)
        self.assertIn("--user-agent=MiApp/1.0", cmd)
        self.assertEqual(cmd[-1], ch.url)


class TestLaunch(unittest.TestCase):
    """Lanzamiento siempre headless en tests: Popen mockeado o fake binario.

    Nunca se ejecuta un reproductor real con GUI: o bien se intercepta
    subprocess.Popen y se inspecciona el argv, o bien se usa un script
    falso que sale al instante (sin display ni red).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Reproductor falso: script ejecutable que termina al instante.
        self.fake = Path(self._tmp.name) / "fakeplayer"
        self.fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake.chmod(stat.S_IRWXU)

    def test_launch_headless_no_abre_gui_usa_argv(self) -> None:
        from thetvview import player

        with (
            mock.patch.object(
                player.config,
                "find_player",
                side_effect=lambda name: str(self.fake) if name == "mpv" else None,
            ),
            mock.patch.object(player.subprocess, "Popen") as popen,
        ):
            player.launch(_channel(), player_name="mpv", headless=True)
        popen.assert_called_once()
        cmd = popen.call_args[0][0]
        kwargs = popen.call_args[1]
        self.assertIsInstance(cmd, list)  # argv en lista, sin shell
        self.assertIn("--vo=null", cmd)
        self.assertIn("--ao=null", cmd)
        self.assertNotIn("--gpu-api=opengl", cmd)
        self.assertEqual(cmd[-1], "http://stream.example.com/live")
        self.assertNotIn("shell", kwargs)  # nunca shell=True
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertIs(kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertIs(kwargs.get("stderr"), subprocess.DEVNULL)

    def test_launch_por_defecto_no_es_headless(self) -> None:
        from thetvview import player

        with (
            mock.patch.object(
                player.config,
                "find_player",
                side_effect=lambda name: str(self.fake) if name == "mpv" else None,
            ),
            mock.patch.object(player.subprocess, "Popen") as popen,
        ):
            player.launch(_channel(), player_name="mpv")
        cmd = popen.call_args[0][0]
        self.assertNotIn("--vo=null", cmd)
        self.assertIn("--gpu-api=opengl", cmd)

    def test_launch_headless_vlc_y_mplayer(self) -> None:
        from thetvview import player

        for name, expected in (
            ("vlc", ["--intf", "dummy"]),
            ("mplayer", ["-vo", "null"]),
        ):
            with self.subTest(player=name):
                with (
                    mock.patch.object(
                        player.config,
                        "find_player",
                        side_effect=lambda n, _name=name: (
                            str(self.fake) if n == _name else None
                        ),
                    ),
                    mock.patch.object(player.subprocess, "Popen") as popen,
                ):
                    player.launch(_channel(), player_name=name, headless=True)
                cmd = popen.call_args[0][0]
                for flag in expected:
                    self.assertIn(flag, cmd)

    def test_launch_headless_integracion_fake_binary(self) -> None:
        """End-to-end headless contra binario falso (sale al instante)."""
        from thetvview import player

        original = player.config.find_player

        def fake_find(name: str) -> str | None:
            return str(self.fake) if name == "mpv" else None

        player.config.find_player = fake_find  # type: ignore[assignment]
        try:
            proc = player.launch(_channel(), headless=True)
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0)
            self.assertIsInstance(proc.args, list)  # argv en lista, sin shell
            self.assertIn("--vo=null", proc.args)
        finally:
            player.config.find_player = original  # type: ignore[assignment]

    def test_launch_sin_players_lanza_error(self) -> None:
        from thetvview import player

        original = player.config.find_player
        player.config.find_player = lambda name: None  # type: ignore[assignment]
        try:
            with self.assertRaises(PlayerError):
                player.launch(_channel(), headless=True)
        finally:
            player.config.find_player = original  # type: ignore[assignment]


class TestExplainEarlyExit(unittest.TestCase):
    def test_sigue_vivo_no_explica(self) -> None:
        self.assertIsNone(explain_early_exit(None, 0.5))

    def test_salida_limpia_no_explica(self) -> None:
        self.assertIsNone(explain_early_exit(0, 0.5))

    def test_muerte_tardia_no_explica(self) -> None:
        self.assertIsNone(explain_early_exit(1, 30.0))

    def test_muerte_al_instante_explica_proveedor(self) -> None:
        msg = explain_early_exit(1, 0.8)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertIn("1", msg)
        self.assertIn("401", msg)


class TestB1PoliticaDeEsquema(unittest.TestCase):
    """Gap B1: ninguna línea de un M3U hostil llega al reproductor."""

    def _reject(self, url: str) -> str:
        with self.assertRaises(PlayerError) as ctx:
            command_for(Channel(name="X", url=url), "mpv", player_path="/usr/bin/mpv")
        return str(ctx.exception)

    def test_esquema_file_rechazado(self) -> None:
        msg = self._reject("file:///etc/passwd")
        self.assertNotIn("/etc/passwd", msg)  # el mensaje nunca trae la URL

    def test_javascript_rechazado(self) -> None:
        self._reject("javascript:alert(1)")

    def test_data_rechazado(self) -> None:
        self._reject("data:text/html,<script>x</script>")

    def test_linea_de_opcion_rechazada(self) -> None:
        # Una línea no-# de un M3U que empieza por `-` (p. ej. --script=…)
        msg = self._reject("--script=/tmp/evil.lua")
        self.assertNotIn("/tmp/evil.lua", msg)

    def test_rtsp_udp_ya_no_se_rechazan_por_esquema(self) -> None:
        # SDD-M Fases 5 y 6: la evaluación que este test esperaba ya está
        # hecha y el resultado es que RTMP/RTSP/UDP entran para reproducir.
        # Lo que **no** entra son las credenciales embebidas (decisión D2):
        # una cámara se registra con una referencia opaca `ipcam://`, y la
        # contraseña vive en el almacén del SO (ver thetvview/cam_ref.py).
        for url in ("rtsp://192.168.1.5/stream", "udp://239.0.0.1:1234",
                    "rtmp://servidor.test/live/canal"):
            with self.subTest(url=url):
                cmd = command_for(
                    Channel(name="X", url=url), "mpv", player_path="/usr/bin/mpv"
                )
                self.assertEqual(cmd[-1], url)

    def test_rtsp_con_credenciales_embebidas_sigue_rechazado(self) -> None:
        # D2: `_finish()` no se tocó. El rechazo es el de toda la app.
        msg = self._reject("rtsp://admin:P4ssw0rd@192.168.1.9:554/stream1")
        self.assertNotIn("P4ssw0rd", msg)
        self.assertNotIn("192.168.1.9", msg)

    def test_rtsps_y_multicast_siguen_rechazados(self) -> None:
        # rtsps: no verificable en esta máquina (Fase 0) → no se ofrece.
        # multicast: no es un protocolo, es `udp://` con dirección de grupo.
        for url in ("rtsps://cam.local:322/live", "multicast://239.0.0.1:1234"):
            with self.subTest(url=url):
                self._reject(url)

    def test_url_valida_lleva_separador_doble_guion(self) -> None:
        cmd = command_for(_channel(), "mpv", player_path="/usr/bin/mpv")
        self.assertEqual(cmd[-2], "--")
        self.assertEqual(cmd[-1], "http://stream.example.com/live")

    def test_el_doble_guion_va_antes_de_la_url_en_todos_los_players(self) -> None:
        for name in ("mpv", "mplayer", "vlc"):
            cmd = command_for(_channel(), name, player_path=f"/usr/bin/{name}")
            self.assertEqual(cmd[-2], "--", name)
            self.assertEqual(cmd[-1], "http://stream.example.com/live", name)

    def test_ffmpeg_envuelta_siguesiendo_valida(self) -> None:
        ch = Channel(name="HLS", url="ffmpeg://https://example.com/a.m3u8")
        cmd = command_for(ch, "mplayer", player_path="/usr/bin/mplayer")
        self.assertEqual(cmd[-2], "--")
        self.assertEqual(cmd[-1], "ffmpeg://https://example.com/a.m3u8")


if __name__ == "__main__":
    unittest.main()
