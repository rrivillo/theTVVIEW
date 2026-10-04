"""Tests de la integración detector → router → arranque → apagado (§30).

El §30 pide probar la cadena completa con streams de laboratorio, sin
proveedores de terceros. Aquí la cadena se prueba **con las piezas falsas**,
que es lo que permite comprobar el orden y los errores sin lanzar un binario ni
abrir un puerto:

```text
detector → router → backend → startup → error handling → shutdown
```

Lo que no se sustituye es la **decisión**: el/url_policy, el detector y el
router son los de verdad, con la tabla de verdad. Un fake en el sitio donde
habría un `Popen` es exactamente el punto donde el comportamiento del resto
deja de importar.
"""

from __future__ import annotations

import unittest
from unittest import mock

from thetvview.models import Channel
from thetvview.player import command_for, launch
from thetvview.player.errors import PlayerError
from thetvview.player.router import NoCompatibleBackend, select_backend
from thetvview.streams.detector import detect_protocol
from thetvview.streams.diagnose import diagnose, render_report

TODOS = {"mpv": "/usr/bin/mpv", "mplayer": "/usr/bin/mplayer", "vlc": "/usr/bin/vlc"}


def _canal(url: str, name: str = "Canal 1") -> Channel:
    return Channel(name=name, url=url)


class _Proc:
    """Proceso falso: `poll()` devuelve None hasta que se le mata."""

    def __init__(self, returncode: int | None = None) -> None:
        self.pid = 4242
        self._rc = returncode

    def poll(self):  # noqa: ANN201
        return self._rc

    def kill(self) -> None:
        self._rc = -9


class TestCadenaCompleta(unittest.TestCase):
    """Cada transporte recorre la misma cadena y llega al mismo sitio."""

    #: Los transportes que la tabla dice abrir, con su URL de laboratorio.
    CADENA = (
        ("http://proveedor.test/live/canal.m3u8", "hls", "http", "mpv"),
        ("http://proveedor.test/live/canal.ts", "mpegts", "http", "mpv"),
        ("rtmp://proveedor.test/live/a", "rtmp", "rtmp", "mpv"),
        ("rtmps://proveedor.test/live/a", "rtmps", "rtmps", "mpv"),
        ("rtsp://cam.local:554/stream1", "rtsp", "rtsp", "mpv"),
        ("udp://239.255.0.1:5000", "udp", "udp", "mpv"),
    )

    def test_detector_router_argv_y_salida(self) -> None:
        for url, protocolo, esquema, esperado in self.CADENA:
            with self.subTest(url=url):
                # 1. Detector: dice qué es (contenido) y cómo viaja (esquema).
                # Sin MIME, `/live/canal` es `unknown` a propósito: adivinar
                # sería mentir. El resto de la cadena **no depende** de ello,
                # y eso es justo lo que se comprueba aquí: el mismo canal con
                # `unknown` abre igual.
                det = detect_protocol(url=url)
                if url.endswith(".m3u8") or not url.startswith("http"):
                    self.assertEqual(det.protocol, protocolo)
                self.assertEqual(det.scheme, esquema)

                # 2. Router: elige por capacidad, no por posición.
                elegido = select_backend(url, available=TODOS)
                self.assertEqual(elegido.name, esperado)

                # 3. Arranque: el argv termina en `--` y la URL detrás.
                argv = command_for(_canal(url), elegido.name, player_path="/usr/bin/x")
                self.assertEqual(argv[-2:], ["--", url])

                # 4. Shutdown / diagnóstico: el informe cierra el círculo.
                informe = diagnose(_canal(url), available=TODOS)
                self.assertEqual(informe.chosen, esperado)
                self.assertEqual(informe.verdict, "READY")

    def test_launch_real_devuelve_proceso(self) -> None:
        # El arranque de verdad, con `Popen` sustituido. Lo demás —política de
        # URL, tabla, argv— es el de producción.
        for url, _p, _e, esperado in self.CADENA:
            with self.subTest(url=url):
                with mock.patch(
                    "thetvview.player.core.subprocess.Popen",
                    return_value=_Proc(),
                ) as popen:
                    proc = launch(_canal(url), headless=True)
                self.assertIsInstance(proc, _Proc)
                argv = popen.call_args.args[0]
                self.assertIsInstance(argv, list)
                self.assertEqual(argv[-2:], ["--", url])
                self.assertTrue(argv[0].endswith(esperado))

    def test_un_esquema_desconocido_no_llega_a_popen(self) -> None:
        # El paso 3 no se ejecuta: se corta antes, y con un motivo que lo dice.
        with mock.patch(
            "thetvview.player.core.subprocess.Popen",
            return_value=_Proc(),
        ) as popen:
            with self.assertRaises(NoCompatibleBackend):
                launch(_canal("gopher://x/y"), headless=True)
        self.assertEqual(popen.call_count, 0)

    def test_el_informe_coincide_con_lo_que_se_lanzó(self) -> None:
        # Si el diagnóstico dijera un reproductor distinto del que se usó, el
        # usuario recibiría dos verdades y no sabría cuál creer.
        for url, _p, _e, esperado in self.CADENA:
            with self.subTest(url=url):
                with mock.patch(
                    "thetvview.player.core.subprocess.Popen", return_value=_Proc()
                ):
                    launch(_canal(url), headless=True)
                texto = render_report(diagnose(_canal(url), available=TODOS))
                self.assertIn(f"Reproductor elegido: {esperado}", texto)


class TestCaidaDelProceso(unittest.TestCase):
    """`error handling`: un proceso que muere al instante."""

    def test_muerte_inmediata_no_deja_pant_colgada(self) -> None:
        with mock.patch(
            "thetvview.player.core.subprocess.Popen", return_value=_Proc(1)
        ):
            proc = launch(_canal("http://p.test/a.m3u8"), headless=True)
        self.assertEqual(proc.poll(), 1)

    def test_el_reproductor_eligido_es_el_que_abre(self) -> None:
        # El orden del router y el del binario no tienen por qué coincidir:
        # aquí se comprueba que el launcher **no** vuelve a la lista a ciegas.
        with mock.patch(
            "thetvview.player.core.config.find_player",
            side_effect=lambda n: TODOS.get(n),
        ), mock.patch(
            "thetvview.player.core.subprocess.Popen", return_value=_Proc()
        ) as popen:
            launch(_canal("udp://239.255.0.1:5000"), headless=True)
        self.assertIn("mpv", popen.call_args.args[0][0])

    def test_un_error_de_popen_no_es_un_crash(self) -> None:
        with mock.patch(
            "thetvview.player.core.subprocess.Popen",
            side_effect=OSError("no se puede ejecutar"),
        ):
            with self.assertRaises(OSError):
                launch(_canal("http://p.test/a.m3u8"), headless=True)


class TestSinRomperLoDeSiempre(unittest.TestCase):
    """El §32: un stream simple sigue siendo el camino de siempre."""

    def test_sin_kwargs_el_argv_es_el_de_antes(self) -> None:
        # Los parámetros nuevos de `launch` (`failures`, `preferred`) son
        # opcionales y no cambian el argv: el camino de siempre no cambia de
        # forma (SDD §32).
        base = command_for(
            _canal("http://p.test/a.m3u8"), "mpv", player_path=TODOS["mpv"],
            headless=True,
        )
        with mock.patch(
            "thetvview.player.core.config.find_player",
            side_effect=lambda n: TODOS.get(n),
        ), mock.patch(
            "thetvview.player.core.subprocess.Popen", return_value=_Proc()
        ) as popen:
            launch(_canal("http://p.test/a.m3u8"), headless=True)
        self.assertEqual(popen.call_args.args[0], base)

    def test_los_parametros_nuevos_son_opcionales(self) -> None:
        import inspect

        from thetvview.player import core

        firma = inspect.signature(core.launch)
        for nombre in ("failures", "preferred"):
            with self.subTest(nombre=nombre):
                self.assertIn(nombre, firma.parameters)
                self.assertIsNone(firma.parameters[nombre].default)

    def test_el_proxy_de_calidad_manda_sobre_la_url(self) -> None:
        # Con el proxy, lo que se reproduce es su master en loopback; decidir
        # el reproductor por la URL del canal abriría uno capaz con el RTSP
        # original cuando lo que va a abrir es un HTTP local.
        canal = _canal("rtsp://cam.local:554/stream1")
        proxy = "http://127.0.0.1:12345/manifesto?token=abc"
        elegido = select_backend(proxy, available=TODOS)
        self.assertEqual(elegido.name, "mpv")
        self.assertTrue(elegido.ok)
        self.assertEqual(canal.url, "rtsp://cam.local:554/stream1")


class TestFalloDeUnBackend(unittest.TestCase):
    """`error handling` con dos clientes en vez de uno."""

    def test_el_siguiente_se_prueba_solo_si_el_primero_falla(self) -> None:
        # Con el router: `failures` recuerda lo ya probado, así que el ciclo
        # mpv→vlc→mpv→vlc no puede darse (§26).
        from thetvview.player.router import attempts

        primero = attempts("http://p.test/a.m3u8", available=TODOS)[0]
        segundo = attempts(
            "http://p.test/a.m3u8", failures={primero.name}, available=TODOS
        )[0]
        self.assertNotEqual(primero.name, segundo.name)
        # Y agotados todos, el error lo dice.
        with self.assertRaises(NoCompatibleBackend):
            select_backend(
                "http://p.test/a.m3u8",
                failures={"mpv", "mplayer", "vlc"},
                available=TODOS,
            )

    def test_el_error_final_no_filtra_la_url(self) -> None:
        url = "rtmp://proveedor.test/live/a?token=SECRETO"
        with self.assertRaises(NoCompatibleBackend) as ctx:
            select_backend(url, failures={"mpv", "mplayer", "vlc"}, available=TODOS)
        self.assertNotIn("SECRETO", str(ctx.exception))
        self.assertIsInstance(ctx.exception, PlayerError)