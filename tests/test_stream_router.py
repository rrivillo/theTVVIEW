"""Tests del router de reproductores (SDD-M §8 y §27, plan Fase 2).

Lo que se verifica aquí es el hueco que rellena el router (H1 del plan):
:func:`thetvview.player.launch` recorría ``SUPPORTED_PLAYERS`` y lanzaba el
**primer binario que encontraba**, sin mirar qué sabe hacer. Estos tests son
puros —no lanzan un solo proceso— porque la decisión se puede comprobar entera
sin tocar disco ni red.
"""

from __future__ import annotations

import unittest
from unittest import mock

from thetvview import config
from thetvview.player.protocols import (
    PLAYER_PROTOCOL_SUPPORT,
    PlayerProtocolSupport,
    supports,
)
from thetvview.player.router import (
    MAX_BACKEND_ATTEMPTS,
    NoCompatibleBackend,
    explain_no_backend,
    order_candidates,
    select_backend,
)

#: Los tres instalados, para no depender de lo que haya en la máquina.
TODOS = {"mpv": "/usr/bin/mpv", "mplayer": "/usr/bin/mplayer", "vlc": "/usr/bin/vlc"}
NINGUNO = {"mpv": None, "mplayer": None, "vlc": None}
SIN_MPV = {"mpv": None, "mplayer": "/usr/bin/mplayer", "vlc": "/usr/bin/vlc"}


class TestTablaDeProtocolos(unittest.TestCase):
    def test_todo_reproductor_tiene_su_tabla(self) -> None:
        for nombre in config.SUPPORTED_PLAYERS:
            with self.subTest(reproductor=nombre):
                self.assertIn(nombre, PLAYER_PROTOCOL_SUPPORT)

    def test_los_que_abren_udp_estan_medidos(self) -> None:
        # Medido el 2026-10-04: mpv y mplayer abren UDP unicast; VLC abre el
        # socket pero se queda en su prefetch de 16 MiB sin decodificar. El
        # multicast es otra cosa y no se ha podido medir: eso lo dice la
        # tabla, no lo adivina el router.
        self.assertTrue(supports("mpv", "udp"))
        self.assertTrue(supports("mplayer", "udp"))
        self.assertFalse(supports("vlc", "udp"))

    def test_el_fallo_de_udp_de_vlc_tiene_motivo(self) -> None:
        from thetvview.player.protocols import support_for

        soporte = support_for("vlc")
        assert soporte is not None
        self.assertIn("prefetch", soporte.why_not("udp"))

    def test_mplayer_no_abre_rtmps(self) -> None:
        # Medido: «No stream found to handle url rtmps://…». No es que rechace
        # el TLS, es que su lista de protocolos no incluye rtmps.
        self.assertFalse(supports("mplayer", "rtmps"))
        self.assertTrue(supports("mpv", "rtmps"))
        self.assertTrue(supports("vlc", "rtmps"))

    def test_el_fallo_de_rtmps_tiene_motivo(self) -> None:
        from thetvview.player.protocols import support_for

        soporte = support_for("mplayer")
        assert soporte is not None
        self.assertIn("rtmps", soporte.why_not("rtmps").lower())

    def test_lo_no_verificado_no_se_ofrece(self) -> None:
        # rtsps no se ha podido medir en esta máquina: no hay servidor RTSP
        # sobre TLS contra el que probarlo.
        for nombre in config.SUPPORTED_PLAYERS:
            with self.subTest(reproductor=nombre):
                self.assertFalse(supports(nombre, "rtsps"))

    def test_un_reproductor_desconocido_no_promete_nada(self) -> None:
        # Si no sabemos lo que sabe, no prometemos (§51).
        self.assertFalse(supports("ffplay", "hls"))
        self.assertFalse(supports("", "hls"))
        self.assertFalse(supports(None, "hls"))

    def test_todo_fallo_tiene_motivo(self) -> None:
        # Un fallo sin explicación es peor que no informar.
        for nombre, tabla in PLAYER_PROTOCOL_SUPPORT.items():
            for transporte in ("http", "rtmp", "rtsp", "udp", "rtsps"):
                with self.subTest(reproductor=nombre, transporte=transporte):
                    motivo = tabla.why_not(transporte)
                    if not tabla.can_play(transporte):
                        self.assertTrue(motivo, f"{nombre}/{transporte} sin motivo")


class TestOrdenDeDecision(unittest.TestCase):
    """El orden del §27, que es también el §8 avanzado."""

    def test_prioridad_por_defecto(self) -> None:
        self.assertEqual(select_backend("http://x/a.m3u8", available=TODOS).name, "mpv")

    def test_el_preferido_va_primero(self) -> None:
        elegido = select_backend("http://x/a.m3u8", preferred="vlc", available=TODOS)
        self.assertEqual(elegido.name, "vlc")
        self.assertTrue(elegido.preferido)

    def test_el_preferido_que_no_abre_no_gana(self) -> None:
        # Dos casos medidos: VLC no abre UDP y mplayer no abre RTMPS. Pedir
        # cualquiera de los dos explícitamente no lo hace posible, y el router
        # cae en el siguiente que sí (AC-009).
        self.assertNotEqual(
            select_backend("udp://239.1.1.1:5000", preferred="vlc", available=TODOS).name,
            "vlc",
        )
        elegido = select_backend(
            "rtmps://x/live/a", preferred="mplayer", available=TODOS
        )
        self.assertNotEqual(elegido.name, "mplayer")
        self.assertIn(elegido.name, {"mpv", "vlc"})

    def test_el_preferido_que_no_esta_instalado_tampoco_gana(self) -> None:
        # mpv no está instalado y el usuario prefiere VLC: gana VLC, que sí.
        elegido = select_backend("http://x/a.m3u8", preferred="vlc", available=SIN_MPV)
        self.assertEqual(elegido.name, "vlc")

    def test_soporte_manda_sobre_prioridad(self) -> None:
        # Con UDP sólo pueden mpv y mplayer (VLC medido ❌), así que el
        # prioritise en mpv y el segundo puesto se salta a mplayer. Es el
        # AC-009 hecho caso: un backend incompatible no bloquea a los demás.
        elegidos = order_candidates("udp://239.1.1.1:5000", available=TODOS)
        self.assertEqual([c.name for c in elegidos if c.ok], ["mpv", "mplayer"])

    def test_la_prioridad_sigue_manda_donde_todos_abren(self) -> None:
        # Donde los tres abren (HLS, RTMP, RTSP), decide la prioridad.
        for url in ("http://x/a.m3u8", "rtmp://x/live/a", "rtsp://x/live"):
            with self.subTest(url=url):
                elegidos = order_candidates(url, available=TODOS)
                self.assertEqual(
                    [c.name for c in elegidos if c.ok],
                    list(config.SUPPORTED_PLAYERS),
                )
        # mpv sin UDP, como el mplayer que no abre rtmps.
        sin_udp = PlayerProtocolSupport(
            name="mpv",
            transports=frozenset({"http", "https", "rtmp", "rtmps", "rtsp"}),
            medido=frozenset({"http", "https", "rtmp", "rtmps", "rtsp"}),
            notas={"udp": "no abre UDP: así, para la prueba"},
        )
        with mock.patch.dict(PLAYER_PROTOCOL_SUPPORT, {"mpv": sin_udp}):
            elegidos = order_candidates("udp://239.1.1.1:5000", available=TODOS)
        self.assertNotIn("mpv", [c.name for c in elegidos if c.ok])
        self.assertIn(
            "no abre UDP",
            dict((c.name, c.motivo) for c in elegidos if not c.ok)["mpv"],
        )

    def test_rtmps_salta_mplayer(self) -> None:
        # AC-009: un backend incompatible no bloquea a los compatibles. Con
        # `rtmps://`, mplayer queda fuera aunque sea el segundo de la lista.
        elegidos = order_candidates("rtmps://x/live/a", available=TODOS)
        self.assertEqual([c.name for c in elegidos if c.ok], ["mpv", "vlc"])

    def test_historial_de_fallo_al_final(self) -> None:
        elegidos = order_candidates(
            "http://x/a.m3u8", failures={"mpv"}, available=TODOS
        )
        viables = [c.name for c in elegidos if c.ok]
        self.assertEqual(viables[0], "mplayer")
        # Pero sigue estando en la lista, con su motivo: si fuera el único
        # candidato, reintentarlo es mejor que no hacer nada.
        caidos = [c for c in elegidos if not c.ok]
        self.assertIn("mpv", [c.name for c in caidos])
        self.assertIn("ya falló", dict((c.name, c.motivo) for c in caidos)["mpv"])


class TestSinBackend(unittest.TestCase):
    def test_ninguno_instalado_da_error_normalizado(self) -> None:
        with self.assertRaises(NoCompatibleBackend):
            select_backend("http://x/a.m3u8", available=NINGUNO)

    def test_el_error_no_es_un_player_error_generico(self) -> None:
        # Hereda de PlayerError para que el código que ya captura «falló el
        # reproductor» siga funcionando, pero se distingue con isinstance.
        from thetvview.player.errors import PlayerError

        with self.assertRaises(NoCompatibleBackend) as ctx:
            select_backend("http://x/a.m3u8", available=NINGUNO)
        self.assertIsInstance(ctx.exception, PlayerError)

    def test_el_mensaje_dice_que_instalar(self) -> None:
        with self.assertRaises(NoCompatibleBackend) as ctx:
            select_backend("rtmp://x/live/a", available=NINGUNO)
        self.assertIn("RTMP", str(ctx.exception))
        self.assertIn("mpv", str(ctx.exception))

    def test_el_mensaje_no_lleva_la_url(self) -> None:
        # Puede traer token: la URL nunca va en un mensaje.
        with self.assertRaises(NoCompatibleBackend) as ctx:
            select_backend("rtmp://x/live/a?token=secreto123", available=NINGUNO)
        self.assertNotIn("secreto123", str(ctx.exception))

    def test_transporte_no_verificado_no_se_ofrece(self) -> None:
        with self.assertRaises(NoCompatibleBackend) as ctx:
            select_backend("rtsps://x/live", available=TODOS)
        self.assertIn("RTSPS", str(ctx.exception))
        self.assertIn("verificar", str(ctx.exception))

    def test_esquema_desconocido_dice_que_revisar_la_linea(self) -> None:
        with self.assertRaises(NoCompatibleBackend) as ctx:
            select_backend("gopher://x/y", available=TODOS)
        self.assertIn("reconocer", str(ctx.exception))


class TestSinRomper(unittest.TestCase):
    def test_candidates_desempatan_en_orden(self) -> None:
        candidatos = order_candidates("http://x/a.m3u8", available=TODOS)
        viables = [c.name for c in candidatos if c.ok]
        self.assertEqual(viables, list(config.SUPPORTED_PLAYERS))

    def test_sin_argumentos_devuelve_lista_vacia_de_viables(self) -> None:
        candidatos = order_candidates("http://x/a.m3u8", available={})
        self.assertEqual([c for c in candidatos if c.ok], [])

    def test_el_mismo_canal_no_repite_backend_ilimitado(self) -> None:
        # §26: el ciclo mpv→vlc→mpv→vlc no puede darse porque `failures`
        # recuerda lo ya probado. Con los tres quemados, no queda nadie y se
        # lanza el error normalizado en vez de reintentar el primero.
        fallos: set[str] = set()
        intentos: list[str] = []
        for _ in range(3):
            elegido = select_backend(
                "rtmp://x/live/a", failures=fallos, available=TODOS
            )
            intentos.append(elegido.name)
            fallos.add(elegido.name)
        self.assertEqual(intentos, ["mpv", "mplayer", "vlc"])
        with self.assertRaises(NoCompatibleBackend):
            select_backend("rtmp://x/live/a", failures=fallos, available=TODOS)

    def test_attempts_limita_las_puestas(self) -> None:
        # El tope del §26 vive aquí: `attempts` es lo que se itera cuando el
        # primero falla, y con tope son dos esperas, no tres.
        from thetvview.player.router import attempts

        self.assertEqual(
            [c.name for c in attempts("http://x/a.m3u8", available=TODOS)],
            ["mpv", "mplayer"],
        )

    def test_attempts_respeta_el_preferido(self) -> None:
        from thetvview.player.router import attempts

        self.assertEqual(
            [c.name for c in attempts(
                "http://x/a.m3u8", preferred="vlc", available=TODOS
            )],
            ["vlc", "mpv"],
        )

    def test_attempts_excluye_lo_ya_fallido(self) -> None:
        from thetvview.player.router import attempts

        self.assertEqual(
            [c.name for c in attempts(
                "rtmp://x/live/a", failures={"mpv"}, available=TODOS
            )],
            ["mplayer", "vlc"],
        )

    def test_tope_de_intentos_documentado(self) -> None:
        # El tope del §26 es 2; se comprueba para que nadie lo suba sin
        # darse cuenta de que un canal malo pasa a ser una espera larga.
        self.assertEqual(MAX_BACKEND_ATTEMPTS, 2)

    def test_explain_no_backend_acepta_lista_vacia(self) -> None:
        texto = explain_no_backend("http://x/a", [])
        self.assertIn("mpv", texto)


if __name__ == "__main__":
    unittest.main()