"""Seguridad de la sección multi-stream (§29 del SDD-M, AC-007/AC-010).

Las siete comprobaciones que el §29 pide sobre URLs que llegan al reproductor,
con la diferencia de que ahora las URLs pueden ser de más transports que antes
(rtmp, rtsp, udp) y por eso la puerta de entrada tiene más superficie.
"""

from __future__ import annotations

import unittest

from thetvview.models import Channel
from thetvview.player import command_for, url_de_camara_resuelta
from thetvview.player.errors import PlayerError
from thetvview.security.errors import InvalidUrlError
from thetvview.security.redaction import (
    contains_secret,
    needs_redaction_for_storage,
    redact_text,
)
from thetvview.security.url_policy import PURPOSE_STREAM, validate_url

#: Las siete del §29 del SDD-M, verbatim en su intención. No todas tienen que
#: ser **rechazadas**: el AC-010 no pide «rechazar», pide «nunca ejecutar una
#: URL mediante shell». Una URL con `&` o con `$()` es perfectamente válida como
#: argumento —de hecho las listas IPTV reales las tienen— y lo que importa es
#: que llegue al reproductor como **un solo argv**, detrás del `--`, y que jamás
#: se interprete como comando.
URLS_DEL_SDD29 = (
    # URL con espacios
    "http://proveedor.test/live/canal con espacios.m3u8",
    # caracteres especiales
    "http://proveedor.test/live/canal'\"<>|·.m3u8",
    # ampersand
    "http://proveedor.test/live/x.m3u8?a=1&b=2",
    # credenciales embebidas
    "http://usuario:clave@proveedor.test/live/x.m3u8",
    # token
    "http://proveedor.test/live/x.m3u8?token=SECRETO123",
    # intento de inyección de shell
    "http://proveedor.test/$(reboot).m3u8",
    "http://proveedor.test/`id`.m3u8",
    "http://proveedor.test/;rm -rf /;.m3u8",
)

#: Las mismas formas de ataque sobre los transportes de las Fases 4-6. Un
#: `$(...)` en un `rtmp://` es tan inerte como en un `http://`: lo que lo hace
#: inerte es el argv, no el esquema.
URLS_ATAQUE_NUEVOS = (
    "rtmp://proveedor.test/live/$(reboot)",
    "rtmp://proveedor.test/live/`id`",
    "rtmp://proveedor.test/live;rm -rf /;",
    "rtsp://cam.local:554/stream1 && reboot",
    "udp://239.0.0.1:5000 & reboot",
)

#: Las que la política tiene que rechazar sí o sí, en los transportes nuevos.
URLS_QUE_DEBEN_RECHAZARSE = (
    "rtsp://usuario:clave@cam.local:554/stream1",  # credenciales embebidas (D2)
    "rtsp://cam.local:554/stream with spaces",  # espacio crudo
    "rtsps://cam.local:322/live",  # no verificado
    "multicast://239.0.0.1:5000",  # no es un protocolo
    "rtmp://x/live\x00",  # control embebido
)


class TestAtaquesNoSeEjecutan(unittest.TestCase):
    """AC-010: la URL es un argumento, nunca parte de un comando."""

    def _argv(self, url: str) -> list[str]:
        return command_for(
            Channel(name="X", url=url), "mpv", player_path="/usr/bin/mpv"
        )

    def test_una_url_de_ataque_llega_como_un_solo_argumento(self) -> None:
        # Aceptada o rechazada, nunca partida: `$(reboot)` no puede quedar
        # como una palabra suelta al final del argv.
        for url in URLS_DEL_SDD29 + URLS_ATAQUE_NUEVOS:
            with self.subTest(url=url):
                try:
                    argv = self._argv(url)
                except PlayerError:
                    continue  # rechazada: mejor todavía
                self.assertEqual(argv[-2], "--")
                self.assertEqual(argv[-1], url)
                # Y nada de lo que hay antes del `--` ha salido de la URL.
                for elemento in argv[:-1]:
                    self.assertNotIn("reboot", elemento)
                    self.assertNotIn("rm -rf", elemento)

    def test_no_hay_shell_en_ningun_camino(self) -> None:
        # La defensa es estructural: el argv es una lista. El test no busca la
        # cadena "shell=True" (que aparece en un docstring que explica que no
        # se usa), sino una llamada a `Popen`/`run` que realmente lo pase.
        import ast
        import inspect

        from thetvview.player import core

        arbol = ast.parse(inspect.getsource(core))
        offenders = [
            node.lineno
            for node in ast.walk(arbol)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "shell"
        ]
        self.assertEqual(offenders, [], "Popen/run con shell= en player/core.py")

    def test_lo_que_si_se_rechaza(self) -> None:
        for url in URLS_QUE_DEBEN_RECHAZARSE:
            with self.subTest(url=url):
                with self.assertRaises(PlayerError):
                    self._argv(url)

    def test_el_error_no_lleva_la_url(self) -> None:
        # El mensaje acaba en la barra de estado y en un modal: no puede
        # llevar un token ni una contraseña.
        for url in URLS_QUE_DEBEN_RECHAZARSE:
            with self.subTest(url=url):
                try:
                    self._argv(url)
                except PlayerError as exc:
                    self.assertFalse(contains_secret(str(exc)))

    def test_una_url_con_espacios_no_llega_ni_partida(self) -> None:
        # Si pasara el filtro, tendría que pasar como un elemento único.
        url = "rtsp://cam.local:554/stream with spaces"
        with self.assertRaises(PlayerError):
            self._argv(url)

    def test_la_separacion_double_dash_sigue_al_final(self) -> None:
        # AC-010: nada nuevo mueve el `--`.
        argv = self._argv("rtmp://servidor.test/live/canal")
        self.assertEqual(argv[-2], "--")
        self.assertEqual(argv[-1], "rtmp://servidor.test/live/canal")

    def test_una_url_valida_nunca_lleva_opciones_injectadas(self) -> None:
        # Una URL que empieza por `-` es una inyección de opciones (gap B1).
        for url in ("--script=/tmp/evil.lua", "-vf", "rtsp://-flag@cam/live"):
            with self.subTest(url=url):
                with self.assertRaises(PlayerError):
                    self._argv(url)


class TestPoliticaDeUrl(unittest.TestCase):
    def test_los_transportes_nuevos_solo_para_reproducir(self) -> None:
        for url in ("rtmp://x/live/a", "rtmps://x/live/a", "rtsp://x/live",
                    "udp://239.0.0.1:5000"):
            with self.subTest(url=url):
                # Reproducir: vale.
                validate_url(url, PURPOSE_STREAM)
                # Descargar datos: no vale, y hay un motivo.
                with self.assertRaises(InvalidUrlError):
                    validate_url(url, "metadata")

    def test_credenciales_embebidas_se_rechazan_en_todos(self) -> None:
        # Decisión D2: `_finish()` no se toca, para ninguna URL.
        for url in (
            "rtsp://u:p@cam/live",
            "rtmp://u:p@x/live/a",
            "udp://u:p@239.0.0.1:5000",
            "http://u:p@x/a.m3u8",
        ):
            with self.subTest(url=url):
                with self.assertRaises(InvalidUrlError) as ctx:
                    validate_url(url, PURPOSE_STREAM)
                self.assertNotIn("p@", str(ctx.exception))

    def test_no_se_puede_anidar_una_url_dentro_de_otra(self) -> None:
        # `ffmpeg://` es un wrapper para HLS, no un vehículo para colar un
        # esquema cualquiera por la puerta de http.
        for url in ("ffmpeg://rtmp://x/live/a", "ffmpeg://rtsp://cam/live",
                    "ffmpeg://udp://239.0.0.1:5000"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidUrlError):
                    validate_url(url, PURPOSE_STREAM)

    def test_fragmento_rechazado_en_todos_los_transportes(self) -> None:
        for url in ("rtmp://x/live#a", "rtsp://cam/live#a", "udp://239.0.0.1:5000#a"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidUrlError):
                    validate_url(url, PURPOSE_STREAM)

    def test_caracteres_de_control_rechazados(self) -> None:
        for url in ("rtmp://x/live\x00", "rtsp://cam/%00live", "udp://239.0.0.1:5000\x01"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidUrlError):
                    validate_url(url, PURPOSE_STREAM)

    def test_puerto_fuera_de_rango(self) -> None:
        for url in ("rtmp://x:99999/live", "rtsp://cam:0/live"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidUrlError):
                    validate_url(url, PURPOSE_STREAM)


class TestUrlMuyLarga(unittest.TestCase):
    """El §29 pide una URL de 5000 caracteres."""

    def test_se_rechaza_por_largitud(self) -> None:
        from thetvview.security.url_policy import MAX_URL_LENGTH

        larga = "rtmp://servidor.test/live/" + "a" * 5000
        self.assertGreater(len(larga), MAX_URL_LENGTH)
        with self.assertRaises(InvalidUrlError) as ctx:
            validate_url(larga, PURPOSE_STREAM)
        self.assertIn("larga", str(ctx.exception))

    def test_tambien_en_los_transportes_nuevos(self) -> None:
        from thetvview.security.url_policy import MAX_URL_LENGTH

        for prefijo in ("rtsp://cam.local:554/", "udp://239.0.0.1:5000/"):
            with self.subTest(url=prefijo):
                with self.assertRaises(InvalidUrlError):
                    validate_url(prefijo + "b" * 5000, PURPOSE_STREAM)


class TestUrlDeCamaraReconstruida(unittest.TestCase):
    """La única excepción a «`user:pass@` nunca pasa», y sus límites.

    Existe porque la app **reconstruye** la URL de una cámara desde el keyring
    para poder dársela al reproductor. Sin una excepción explícita habría que
    elegir entre una cámara imposible de abrir y relajar `_finish()`, que
    protegería las URLs de las listas. Aquí no se relaja nada: sólo se declara
    que **esta** URL, y sólo `rtsp://`, no vino de un M3U.
    """

    CAMARA = "rtsp://admin:P4ssw0rd@192.168.1.9:554/stream1"

    def test_sin_marca_no_pasa(self) -> None:
        # Por defecto es una URL más: la marca es lo único que la distingue.
        with self.assertRaises(PlayerError):
            command_for(
                Channel(name="c", url=self.CAMARA), "mpv", player_path="/usr/bin/true"
            )

    def test_con_marca_pasa(self) -> None:
        with url_de_camara_resuelta(self.CAMARA):
            argv = command_for(
                Channel(name="c", url=self.CAMARA), "mpv", player_path="/usr/bin/true"
            )
        self.assertEqual(argv[-2:], ["--", self.CAMARA])

    def test_la_marca_no_sobrevive_al_bloque(self) -> None:
        with url_de_camara_resuelta(self.CAMARA):
            pass
        with self.assertRaises(PlayerError):
            command_for(
                Channel(name="c", url=self.CAMARA), "mpv", player_path="/usr/bin/true"
            )

    def test_la_marca_no_sirve_para_otro_esquema(self) -> None:
        # Marcar una URL `http://` con userinfo no abre nada: la excepción es
        # sólo de `rtsp://`, que es donde el userinfo es una credencial de
        # cámara y no un token de lista.
        url = "http://usuario:clave@proveedor.test/a.m3u8"
        with url_de_camara_resuelta(url):
            with self.assertRaises(PlayerError):
                command_for(
                    Channel(name="c", url=url), "mpv", player_path="/usr/bin/true"
                )

    def test_sigue_comprobando_todo_lo_demas(self) -> None:
        # Marcar no desactiva la política: sólo el userinfo.
        for url in (
            "rtsp://admin:P4ssw0rd@cam.local:554/stream with spaces",
            "rtsp://admin:P4ssw0rd@cam.local:554/stream#frag",
            "rtsp://admin:P4ssw0rd@cam.local:99999/stream",
            # Escondidos dentro de la credencial: los chequeos de texto se
            # hacen sobre la URL completa, no sobre la versión recortada.
            "rtsp://admin:pa ss@cam.local/stream",
            "rtsp://admin:P4ss%00ss@cam.local/stream",
            "rtsp://admin:P4ssw0rd@cam.local/" + "a" * 5000,
        ):
            with self.subTest(url=url):
                with url_de_camara_resuelta(url):
                    with self.assertRaises(PlayerError):
                        command_for(
                            Channel(name="c", url=url), "mpv",
                            player_path="/usr/bin/true",
                        )

    def test_un_host_que_empieza_por_guion_no_es_inyeccion(self) -> None:
        # Un host que empieza por «-» es un nombre DNS inválido, pero no es una
        # inyección: la URL va **entera** detrás del `--`, así que el
        # reproductor no puede interpretarla como una opción. Marcar la URL no
        # cambia nada de eso, que es lo que se comprueba.
        url = "rtsp://admin:P4ssw0rd@-flags/stream"
        with url_de_camara_resuelta(url):
            argv = command_for(
                Channel(name="c", url=url), "mpv", player_path="/usr/bin/true"
            )
        self.assertEqual(argv[-2:], ["--", url])

    def test_la_url_que_empieza_por_guion_sigue_rechazandose(self) -> None:
        # El caso peligroso sigue recházandose: `--script=…` completo.
        url = "--script=/tmp/evil.lua"
        with url_de_camara_resuelta(url):
            with self.assertRaises(PlayerError):
                command_for(
                    Channel(name="c", url=url), "mpv", player_path="/usr/bin/true"
                )


class TestRedaccionYDisco(unittest.TestCase):
    """AC-007: lo que se persiste no lleva credenciales."""

    def test_una_camara_no_necesita_redaccion_para_disco(self) -> None:
        # Ya viene como referencia opaca: no hay nada que limpiar, y si se
        # «redactara» se rompería sin ganar nada.
        from thetvview.cam_ref import build_cam_ref

        preparado = build_cam_ref("Cámaras", "rtsp://admin:P4ssw0rd@192.168.1.9/s")
        assert preparado is not None
        self.assertFalse(needs_redaction_for_storage(preparado[0]))

    def test_token_de_lista_se_conserva(self) -> None:
        # El `?token=` de una lista M3U NO se redacta: sin él la lista no se
        # reproduce. Es una decisión deliberada, con su predicado.
        url = "http://proveedor.test/live/x.m3u8?token=abc"
        self.assertFalse(needs_redaction_for_storage(url))
        self.assertTrue(contains_secret(url))  # hay token...
        self.assertNotEqual(redact_text(url), url)  # ...pero no para disco

    def test_password_en_query_si_necesita_redaccion(self) -> None:
        url = "http://proveedor.test/live/x.m3u8?password=secreto"
        self.assertTrue(needs_redaction_for_storage(url))
        self.assertNotIn("secreto", redact_text(url))