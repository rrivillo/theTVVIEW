"""Caracterización del ciclo de reproducción **actual** (AUD-002, Fase 0).

Estas pruebas congelan el comportamiento que existe **hoy**, antes de que la
Fase 1 (P1.1) añada `PlaybackSession`, `ResourceScope` y `SessionRegistry`.
No comprueban que el código sea bueno: comprueban que **cambiarlo** no rompa
nada que un usuario o un test ya dependa de.

Reglas de esta fase:

1. **Sin red, sin reproductor real.** `subprocess.Popen` está doblado y el
   `poll()` es el que devuelve el doble, así que el "proceso" no existe.
2. **Sin `sleep` largos.** El supervisor se prueba con esperas de milisegundos:
   lo que se congela es la *decisión* (reintenta, no reintenta, publica este
   estado), no la duración real de la tabla del §17, que ya tiene su propio test
   en `tests/test_stream_supervisor.py`.
3. Si alguna de estas pruebas **falla** al escribirse, es un bug preexistente y
   se documenta como tal en `docs/sdd/p1-gap-analysis.md`; no se "arregla"
   aquí, porque el arreglo pertenece a su propio ticket.

Casos cubiertos (los cinco que pide el plan):

- ``play_channel`` completo con doble de `Popen` y doble de `app`.
- Muerte inmediata del proceso (< ``EARLY_EXIT_SECONDS``) → mensaje actual.
- ``detach()`` con el bucle dentro de la espera de reintento → no relanza.
- Cierre de ``NowPlayingScreen`` con el proxy de pinning vivo → se para.
- IPC de mpv abierto y luego cerrado.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel
from thetvview.player import core as player_core
from thetvview.player.supervisor import (
    ReconnectPolicy,
    ReconnectSupervisor,
)
from thetvview.ui import app as ui_app
from thetvview.ui import screens as ui_screens
from thetvview.ui.screens import NowPlayingScreen


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class ProcFalso:
    """Sustituto de ``subprocess.Popen``: no lanza nada, sólo responde."""

    def __init__(self, *, vivo: bool = True, pid: int = 4242) -> None:
        self.vivo = vivo
        self.pid = pid
        self.polls = 0
        self.terminated = 0
        self.killed = 0

    def poll(self):  # noqa: ANN201 - imita a subprocess.Popen
        self.polls += 1
        return None if self.vivo else 1

    def terminate(self) -> None:
        self.terminated += 1
        self.vivo = False

    def kill(self) -> None:
        self.killed += 1
        self.vivo = False

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 1


class AppCase(unittest.TestCase):
    """App real sin pantalla, con las rutas de datos en un temporal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        for atributo, nombre in (
            ("PLAYLISTS_JSON", "playlists.json"),
            ("FAVORITES_JSON", "favorites.json"),
            ("PREFS_JSON", "prefs.json"),
            ("RECENTS_JSON", "recents.json"),
            ("THEME_JSON", "theme.json"),
        ):
            parche = mock.patch.object(ui_app.config, atributo, base / nombre)
            parche.start()
            self.addCleanup(parche.stop)
        self.app = ui_app.App(None)  # noqa: ANN001 - no se renderiza


def _canal(nombre: str = "Canal de prueba") -> Channel:
    return Channel(
        name=nombre,
        url="http://proveedor.invalid/live/demo/1.ts",
        tvg_id="demo.1",
        group="Noticias",
    )


# ---------------------------------------------------------------------------
# 1. `play_channel` de punta a punta con Popen doblado
# ---------------------------------------------------------------------------


class TestPlayChannelConPopenDoblado(AppCase):
    """Lo que hace hoy `play_channel` cuando el reproductor «se abre»."""

    def test_empuja_la_pantalla_de_reproduciendo_y_anota_el_reciente(self) -> None:
        proc = ProcFalso(vivo=True)
        with (
            mock.patch.object(player_core.config, "find_player", return_value="/usr/bin/mpv"),
            mock.patch("thetvview.player.core.subprocess.Popen", return_value=proc) as popen,
            mock.patch.object(ui_app.config, "find_player", return_value="/usr/bin/mpv"),
        ):
            ui_screens.play_channel(self.app, _canal(), "mpv")
        # Se lanzó exactamente un proceso, con argv (nunca shell).
        popen.assert_called_once()
        argv = popen.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertNotIn("shell", popen.call_args[1])
        # Invariante B1: el argv termina en ["--", url].
        self.assertEqual(argv[-2], "--")
        self.assertEqual(argv[-1], _canal().url)
        # La pantalla es la de reproducción, con el proceso ya lanzado.
        self.assertIsInstance(self.app.screen, NowPlayingScreen)
        self.assertIs(self.app.screen.proc, proc)
        self.assertEqual(self.app.screen.player_name, "mpv")
        # Y el canal quedó en Recientes con el reproductor que se abrió de verdad.
        nombres = [c.name for c in self.app.recents.load()]
        self.assertIn("Canal de prueba", nombres)

    def test_sin_nombre_de_reproductor_el_router_elige_uno_instalado(self) -> None:
        """Sin `player_name` la elección la hace el router por capacidad."""
        proc = ProcFalso(vivo=True)
        with (
            mock.patch.object(player_core.config, "find_player", return_value="/usr/bin/mpv"),
            mock.patch("thetvview.player.core.subprocess.Popen", return_value=proc),
            mock.patch.object(
                ui_app.config, "detect_players", return_value={"mpv": "/usr/bin/mpv"}
            ),
        ):
            ui_screens.play_channel(self.app, _canal())
        self.assertIsInstance(self.app.screen, NowPlayingScreen)
        self.assertEqual(self.app.screen.player_name, "mpv")

    def test_si_el_reproductor_no_arranca_se_abre_un_modal_y_no_se_pusha(self) -> None:
        """Un `PlayerError` **no** deja media pantalla: sale por modal."""
        with (
            mock.patch.object(player_core.config, "find_player", return_value=None),
            mock.patch.object(ui_app.config, "detect_players", return_value={}),
        ):
            errores: list[str] = []
            with mock.patch.object(
                ui_app.App, "show_error", side_effect=lambda err, **_kw: errores.append(err.cuerpo())
            ):
                ui_screens.play_channel(self.app, _canal(), "mpv")
        self.assertTrue(errores, "un fallo al lanzar debe explicarse en un modal")
        self.assertNotIsInstance(self.app.screen, NowPlayingScreen)


# ---------------------------------------------------------------------------
# 2. Muerte inmediata del proceso
# ---------------------------------------------------------------------------


class TestMuerteInmediata(unittest.TestCase):
    """El texto de «se cerró al instante» es contrato de usuario."""

    def test_texto_actual_por_deajo_del_umbral(self) -> None:
        self.assertIsNotNone(player_core.explain_early_exit(1, 0.5))
        mensaje = player_core.explain_early_exit(1, 0.5) or ""
        self.assertIn("Se cerró al instante (código 1)", mensaje)
        self.assertIn("el stream no llegó a abrir", mensaje)

    def test_no_hay_mensaje_si_sigue_vivo(self) -> None:
        self.assertIsNone(player_core.explain_early_exit(None, 0.1))

    def test_no_hay_mensaje_si_termino_normalmente(self) -> None:
        self.assertIsNone(player_core.explain_early_exit(0, 0.1))

    def test_no_hay_mensaje_si_vivio_lo_suficiente(self) -> None:
        self.assertIsNone(player_core.explain_early_exit(1, player_core.EARLY_EXIT_SECONDS + 1))

    def test_el_resumen_de_salida_de_la_pantalla_lo_incluye(self) -> None:
        app = mock.Mock()
        app.epg = None
        proc = ProcFalso(vivo=False)
        pantalla = NowPlayingScreen(app, _canal(), "mpv", proc)
        # `elapsed_seconds()` mide desde la creación: aquí es ~0.
        resumen = pantalla.exit_summary()
        self.assertIn("no abrió", resumen)
        self.assertIn("Se cerró al instante", resumen)


# ---------------------------------------------------------------------------
# 3. `detach()` con el bucle dentro de la espera de reintento
# ---------------------------------------------------------------------------


class PoliticaRapida(ReconnectPolicy):
    """Mismos topes que la política real, esperas en milisegundos."""

    def __init__(self, max_attempts: int = 5) -> None:
        super().__init__(backoff=(1, 2, 5, 10), max_attempts=max_attempts)

    def espera_para(self, intento: int) -> float:
        return super().espera_para(intento) / 1000.0


class TestDetachDuranteLaEspera(unittest.TestCase):
    def setUp(self) -> None:
        self.llamadas = 0

    def _relanzar(self) -> object:
        self.llamadas += 1
        return ProcFalso(vivo=False)

    @staticmethod
    def _esperar(condicion, timeout: float = 3.0) -> bool:  # noqa: ANN001
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if condicion():
                return True
            time.sleep(0.01)
        return False

    def test_detach_durante_la_espera_no_relanza(self) -> None:
        """El caso que `test_detach_no_reconecta` de la suite del supervisor
        cubre desde fuera; aquí se congela **desde el bucle**: tras `detach()`
        el hilo tiene que morir aunque estuviera a mitad de `_stop.wait()`."""
        sup = ReconnectSupervisor(
            relanzar=self._relanzar,
            poll_seconds=0.02,
            policy=PoliticaRapida(max_attempts=5),
        )
        sup.start(ProcFalso(vivo=False))
        try:
            self.assertTrue(self._esperar(lambda: self.llamadas >= 1))
            # Ahora está esperando antes del segundo reintento.
            sup.detach()
            # El segundo intento no llega nunca.
            self._esperar(lambda: False, timeout=0.3)
        finally:
            sup.stop()
        self.assertEqual(self.llamadas, 1)
        self.assertFalse(sup.running)

    def test_detach_no_mata_el_proceso_observado(self) -> None:
        """`detach()` desvincula; el proceso sigue siendo del reproductor."""
        proc = ProcFalso(vivo=True)
        sup = ReconnectSupervisor(relanzar=self._relanzar, poll_seconds=0.02)
        sup.start(proc)
        sup.detach()
        try:
            self.assertTrue(proc.vivo)
        finally:
            sup.stop()


# ---------------------------------------------------------------------------
# 4 y 5. Cierre de la pantalla con proxy e IPC vivos
# ---------------------------------------------------------------------------


class _SesionPistasFalsa:
    """Doble de ``TrackSession`` con un ``ipc_path`` real."""

    def __init__(self, ipc_path: str) -> None:
        self.ipc_path = ipc_path
        self.paradas = 0

    def stop(self) -> None:
        self.paradas += 1


class TestCierreDeLaPantalla(AppCase):
    def _pantalla(self, **kw) -> NowPlayingScreen:  # noqa: ANN003
        app = mock.Mock()
        app.epg = None
        return NowPlayingScreen(app, _canal(), "mpv", ProcFalso(vivo=True), **kw)

    def test_el_proxy_de_pinning_vivo_se_para_al_cerrar(self) -> None:
        proxy = mock.Mock()
        pantalla = self._pantalla(pin_proxy=proxy)
        pantalla.stop_tracks()
        proxy.stop.assert_called_once()
        # Y la pantalla suelta la referencia: no la vuelve a parar.
        self.assertIsNone(pantalla.pin_proxy)
        pantalla.stop_tracks()
        proxy.stop.assert_called_once()

    def test_el_socket_de_control_se_borra_al_cerrar(self) -> None:
        ruta = Path(self._tmp.name) / "mpv-test.sock"
        ruta.write_bytes(b"")  # mpv crea el fichero y no lo borra
        sesion = _SesionPistasFalsa(str(ruta))
        pantalla = self._pantalla(track_session=sesion)
        pantalla.stop_tracks()
        self.assertEqual(sesion.paradas, 1)
        self.assertFalse(ruta.exists(), "el socket de control debe quedar borrado")

    def test_un_socket_que_ya_no_existe_no_revienta(self) -> None:
        sesion = _SesionPistasFalsa(str(Path(self._tmp.name) / "nunca-existe.sock"))
        pantalla = self._pantalla(track_session=sesion)
        pantalla.stop_tracks()  # no debe lanzar

    def test_cerrar_dos_veces_es_idempotente(self) -> None:
        proxy = mock.Mock()
        pantalla = self._pantalla(pin_proxy=proxy)
        pantalla.stop_tracks()
        pantalla.stop_tracks()
        proxy.stop.assert_called_once()

    def test_el_supervisor_se_detiene_al_cerrar(self) -> None:
        sup = ReconnectSupervisor(relanzar=lambda: ProcFalso(), poll_seconds=0.02)
        pantalla = self._pantalla(relanzar=lambda: ProcFalso())
        self.assertIsNotNone(pantalla.supervisor)
        pantalla.stop_tracks()
        self.assertIsNone(pantalla.supervisor)

    def test_sin_proxy_ni_pistas_cerrar_no_falla(self) -> None:
        pantalla = self._pantalla()
        pantalla.stop_tracks()


# ---------------------------------------------------------------------------
# 6. Aislamiento de sesiones: el `is_current` de `TaskManager` ya lo hace
# ---------------------------------------------------------------------------


class TestAislamientoPorGeneracion(unittest.TestCase):
    """Hoy el aislamiento de tareas lo da la **generación** del TaskManager.

    Se congela porque la Fase 1 mete `SessionRegistry.is_current()` con la
    misma forma: si este comportamiento cambiara, la fase nueva heredaría un
    contrato distinto del que sus tests asumen.
    """

    def test_el_resultado_de_una_generacion_vieja_no_se_publica(self) -> None:
        from thetvview.tasks.manager import TaskManager

        manager = TaskManager()
        soltado = threading.Event()

        def esperar(token, valor: object) -> str:  # noqa: ANN001
            soltado.wait(2.0)
            return str(valor)

        with manager:
            viejo = manager.submit("prueba", esperar, "viejo")
            nuevo = manager.submit("prueba", esperar, "nuevo")
            # La generación del segundo invalida al primero: es el guard que
            # impide que una tarea tardía pise el resultado más reciente.
            self.assertFalse(manager.is_current(viejo))
            self.assertTrue(manager.is_current(nuevo))
            soltado.set()
            self.assertTrue(viejo.join(2.0))
            self.assertTrue(nuevo.join(2.0))
            resultados = {r.value: r for r in manager.drain("prueba") if not r.cancelled}
        # El obsoleto **aparece** (la UI necesita poder limpiar) pero no es
        # publicable: `ok` es False. El vigente sí lo es.
        self.assertTrue(resultados["viejo"].obsolete)
        self.assertFalse(resultados["viejo"].ok)
        self.assertTrue(resultados["nuevo"].ok)

    def test_una_generacion_que_no_supersede_convive(self) -> None:
        from thetvview.tasks.manager import TaskManager

        manager = TaskManager()
        soltado = threading.Event()

        def esperar(token, valor: object) -> str:  # noqa: ANN001
            soltado.wait(2.0)
            return str(valor)

        with manager:
            a = manager.submit("prueba", esperar, "a")
            b = manager.submit("prueba", esperar, "b", supersede=False)
            self.assertTrue(manager.is_current(a))
            self.assertTrue(manager.is_current(b))
            soltado.set()
            a.join(2.0)
            b.join(2.0)
            manager.drain("prueba")


if __name__ == "__main__":
    unittest.main()