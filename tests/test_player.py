"""Tests de player.py: construcción de comando y lanzamiento seguro."""

import stat
import tempfile
import unittest
from pathlib import Path

from thetvview.models import Channel
from thetvview.player import PlayerError, _codec_h264_args, _option_args, command_for


def _channel(**opts) -> Channel:
    return Channel(name="Test TV", url="http://stream.example.com/live", **opts)


class TestCommandFor(unittest.TestCase):
    def test_comando_basico_mpv(self) -> None:
        cmd = command_for(_channel(), "mpv", player_path="/usr/bin/mpv")
        self.assertEqual(
            cmd,
            [
                "/usr/bin/mpv",
                "--vd=ffh264",
                "--gpu-api=opengl",
                "--title=Test TV",
                "--force-media-title=Test TV",
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


class TestLaunch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Reproductor falso: script ejecutable que termina al instante.
        self.fake = Path(self._tmp.name) / "fakeplayer"
        self.fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake.chmod(stat.S_IRWXU)

    def test_launch_devuelve_proceso_y_usa_argv(self) -> None:
        from thetvview import player

        original = player.config.find_player

        def fake_find(name: str) -> str | None:
            return str(self.fake) if name == "mpv" else None

        player.config.find_player = fake_find  # type: ignore[assignment]
        try:
            proc = player.launch(_channel())
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0)
            self.assertIsInstance(proc.args, list)  # argv en lista, sin shell
        finally:
            player.config.find_player = original  # type: ignore[assignment]

    def test_launch_sin_players_lanza_error(self) -> None:
        from thetvview import player

        player.config.find_player = lambda name: None  # type: ignore[assignment]
        try:
            with self.assertRaises(PlayerError):
                player.launch(_channel())
        finally:
            player.config.find_player = (  # type: ignore[assignment]
                lambda name: "/bin/true"
            )


if __name__ == "__main__":
    unittest.main()
