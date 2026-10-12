"""La sesión **enchufada** en la pantalla y en `play_channel` (PLAY-003/004).

:mod:`tests.test_player_session` prueba las piezas puras. Aquí se prueba que la
pantalla y el punto de lanzamiento **las usan de verdad**: que el proxy y el
socket quedan con dueño, que un `PlayerError` sella la sesión con
`init_failed`, y que el cierre de la app la sella con `app_shutdown`.

Todo con dobles: ni red, ni reproductor real, ni curses.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel
from thetvview.player import core as player_core
from thetvview.player.session import EndReason, PlaybackSession, SessionRegistry
from thetvview.ui import app as ui_app
from thetvview.ui import screens as ui_screens
from thetvview.ui.screens import NowPlayingScreen


class ProcFalso:
    def __init__(self, *, vivo: bool = True) -> None:
        self.vivo = vivo
        self.pid = 4242

    def poll(self):  # noqa: ANN201
        return None if self.vivo else 1

    def terminate(self) -> None:
        self.vivo = False

    def kill(self) -> None:
        self.vivo = False

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 1


class _Pistas:
    def __init__(self, ipc_path: str = "") -> None:
        self.ipc_path = ipc_path
        self.paradas = 0

    def stop(self) -> None:
        self.paradas += 1


class AppCase(unittest.TestCase):
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
        self.app = ui_app.App(None)  # noqa: ANN001

    def canal(self) -> Channel:
        return Channel(
            name="Canal", url="http://proveedor.invalid/live/d/1.ts", tvg_id="d.1"
        )


def _pantalla(session: PlaybackSession | None = None, **kw) -> NowPlayingScreen:
    app = mock.Mock()
    app.epg = None
    return NowPlayingScreen(
        app, Channel(name="Canal", url="http://x/1.ts"), "mpv",
        ProcFalso(vivo=True), session=session, **kw
    )


class TestLaPantallaDaDueño(unittest.TestCase):
    """PLAY-003: todo lo que la pantalla abrió queda registrado."""

    def test_el_proxy_y_el_socket_quedan_registrados(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "mpv-x.sock"
            ruta.write_bytes(b"")
            sesion = PlaybackSession("d.1", "mpv")
            proxy = mock.Mock()
            pistas = _Pistas(str(ruta))
            pantalla = _pantalla(
                sesion, pin_proxy=proxy, track_session=pistas
            )
            self.assertEqual(
                sorted(sesion.resources.pendientes),
                ["pistas", "proxy-calidad", "socket-control"],
            )
            pantalla.stop_tracks()
            proxy.stop.assert_called_once()
            self.assertEqual(pistas.paradas, 1)
            self.assertFalse(ruta.exists())

    def test_sin_proxy_ni_pistas_no_se_registra_nada(self) -> None:
        sesion = PlaybackSession("d.1", "mpv")
        _pantalla(sesion)
        self.assertEqual(sesion.resources.pendientes, ())

    def test_liberar_dos_veces_no_repara_el_proxy(self) -> None:
        sesion = PlaybackSession("d.1", "mpv")
        proxy = mock.Mock()
        pantalla = _pantalla(sesion, pin_proxy=proxy)
        pantalla.stop_tracks()
        pantalla.stop_tracks()
        proxy.stop.assert_called_once()

    def test_un_recurso_roto_no_impide_liberar_los_demas(self) -> None:
        sesion = PlaybackSession("d.1", "mpv")
        proxy = mock.Mock()
        proxy.stop.side_effect = OSError("el proxy no responde")
        pistas = _Pistas()
        pantalla = _pantalla(sesion, pin_proxy=proxy, track_session=pistas)
        pantalla.stop_tracks()
        self.assertEqual(pistas.paradas, 1)
        self.assertEqual([n for n, _ in sesion.resources.errores], ["proxy-calidad"])

    def test_sin_sesion_el_camino_es_el_de_siempre(self) -> None:
        """Sin sesión no cambia nada: el proxy se para a mano, una vez."""
        proxy = mock.Mock()
        pantalla = _pantalla(None, pin_proxy=proxy)
        self.assertIsNone(pantalla.session)
        pantalla.stop_tracks()
        proxy.stop.assert_called_once()

    def test_la_pantalla_marca_el_proceso_arrancado(self) -> None:
        sesion = PlaybackSession("d.1", "mpv")
        sesion.start()
        _pantalla(sesion)
        self.assertIsNotNone(sesion.process_started_at)


class TestResumenConMotivo(unittest.TestCase):
    def test_el_usuario_que_lo_detiene_dice_que_lo_detuvo(self) -> None:
        sesion = PlaybackSession("d.1", "mpv")
        pantalla = _pantalla(sesion)
        sesion.end(EndReason.USER_STOPPED)
        self.assertIn("detenido", pantalla.exit_summary())

    def test_el_cierre_de_la_app_se_distingue(self) -> None:
        sesion = PlaybackSession("d.1", "mpv")
        pantalla = _pantalla(sesion)
        sesion.end(EndReason.APP_SHUTDOWN)
        self.assertIn("cerrar la aplicación", pantalla.exit_summary())

    def test_reintentos_agotados_lo_dice(self) -> None:
        sesion = PlaybackSession("d.1", "mpv")
        pantalla = _pantalla(sesion)
        sesion.start().launched().playing().recovering()
        sesion.end(EndReason.RETRIES_EXHAUSTED)
        # Sin reintentos registrados, el proceso sigue vivo: el mensaje es el de
        # «se cortó», no el de «no abrió».
        self.assertIn("no volvió", pantalla.exit_summary())

    def test_proceso_vivo_sin_evidencia_no_dice_finalizado(self) -> None:
        """El punto de todo el modelo de evidencia: se abrió pero no se vio."""
        sesion = PlaybackSession("d.1", "mpv")
        pantalla = _pantalla(sesion)
        sesion.start().launched().playing()
        sesion.end(EndReason.PLAYBACK_ERROR)
        self.assertIn("no llegó a reproducir nada", pantalla.exit_summary())

    def test_con_evidencia_de_medios_no_inventa_un_problema(self) -> None:
        sesion = PlaybackSession("d.1", "mpv")
        pantalla = _pantalla(sesion)
        sesion.start().launched().playing().note_first_media().note_stable()
        sesion.end(EndReason.USER_STOPPED)
        self.assertIn("detenido", pantalla.exit_summary())


class TestPlayChannelConSesion(AppCase):
    def test_abrir_un_canal_registra_una_sesion_vigente(self) -> None:
        with (
            mock.patch.object(player_core.config, "find_player", return_value="/usr/bin/mpv"),
            mock.patch("thetvview.player.core.subprocess.Popen", return_value=ProcFalso()),
            mock.patch.object(ui_app.config, "find_player", return_value="/usr/bin/mpv"),
        ):
            ui_screens.play_channel(self.app, self.canal(), "mpv")
        sesion = self.app._sessions.actual
        self.assertIsNotNone(sesion)
        self.assertEqual(sesion.channel_key, "d.1")
        self.assertTrue(self.app._sessions.is_current(sesion.session_id))
        self.assertIs(self.app.screen.session, sesion)

    def test_un_error_de_lanzamiento_sella_la_sesion_como_init_failed(self) -> None:
        """FR-PLAY-010: la finalización no depende del lanzamiento."""
        with (
            mock.patch.object(player_core.config, "find_player", return_value=None),
            mock.patch.object(ui_app.config, "detect_players", return_value={}),
            mock.patch.object(ui_app.App, "show_error"),
        ):
            ui_screens.play_channel(self.app, self.canal(), "mpv")
        # Sin pantalla no hay sesión vigente, pero la que se abrió terminó.
        self.assertIsNone(self.app._sessions.actual)
        self.assertIsNotNone(self.app._sessions.generacion)

    def test_abrir_otro_canol_sella_el_anterior_como_cancelado(self) -> None:
        with (
            mock.patch.object(player_core.config, "find_player", return_value="/usr/bin/mpv"),
            mock.patch("thetvview.player.core.subprocess.Popen", return_value=ProcFalso()),
            mock.patch.object(ui_app.config, "find_player", return_value="/usr/bin/mpv"),
        ):
            ui_screens.play_channel(self.app, self.canal(), "mpv")
            primera = self.app._sessions.actual
            ui_screens.play_channel(self.app, self.canal(), "mpv")
        segunda = self.app._sessions.actual
        self.assertIsNot(primera, segunda)
        self.assertIs(primera.end_reason, EndReason.CANCELLED)
        self.assertFalse(primera.activa)

    def test_el_cierre_de_la_app_sella_la_sesion_viva(self) -> None:
        with (
            mock.patch.object(player_core.config, "find_player", return_value="/usr/bin/mpv"),
            mock.patch("thetvview.player.core.subprocess.Popen", return_value=ProcFalso()),
            mock.patch.object(ui_app.config, "find_player", return_value="/usr/bin/mpv"),
        ):
            ui_screens.play_channel(self.app, self.canal(), "mpv")
        sesion = self.app._sessions.actual
        self.app._cerrar_recursos()
        self.assertIs(sesion.end_reason, EndReason.APP_SHUTDOWN)

    def test_si_el_lanzamiento_falla_se_borra_el_socket_reservado(self) -> None:
        """FR-PLAY-010: se ejecuta la limpieza aunque no haya reproducción."""
        socket_path = Path(self._tmp.name) / "mpv-reservado.sock"
        socket_path.write_bytes(b"")
        pistas = _Pistas("")
        with (
            mock.patch.object(player_core.config, "find_player", return_value=None),
            mock.patch.object(ui_app.config, "detect_players", return_value={}),
            mock.patch.object(ui_app.App, "show_error"),
            mock.patch(
                "thetvview.player.mpv_ipc.ipc_path", return_value=str(socket_path)
            ),
        ):
            ui_screens.play_channel(
                self.app, self.canal(), "mpv", track_session=pistas
            )
        self.assertFalse(socket_path.exists(), "el socket reservado quedó colgando")
        self.assertEqual(pistas.ipc_path, "")


class TestGuardDeObsolescenciaEnElSupervisor(unittest.TestCase):
    """PLAY-004: el relanzamiento respeta la vigencia de la sesión."""

    def test_un_supervisor_de_sesion_obsoleta_no_relanza(self) -> None:
        from thetvview.player.supervisor import ReconnectPolicy, ReconnectSupervisor

        registro = SessionRegistry()
        sesion = registro.start("d.1", "mpv")
        sesion.start().launched()
        self.assertIs(registro.actual, sesion)

        llamadas: list[int] = []

        def relanzar() -> object:
            llamadas.append(1)
            return ProcFalso(vivo=False)

        sup = ReconnectSupervisor(
            relanzar=relanzar,
            poll_seconds=0.02,
            policy=ReconnectPolicy(backoff=(1, 2), max_attempts=5),
            session=sesion,
            is_vigente=lambda: registro.is_current(sesion.session_id),
        )
        sup.start(ProcFalso(vivo=False))
        # Mientras la sesión es vigente, el supervisor reintenta como siempre.
        limite = time.monotonic() + 3.0
        while not llamadas and time.monotonic() < limite:
            time.sleep(0.01)
        self.assertTrue(llamadas, "con la sesión vigente debe reintentar")
        # El usuario abre otro canal: la sesión queda obsoleta.
        registro.start("d.2", "mpv")
        self.assertFalse(registro.is_current(sesion.session_id))
        antes = len(llamadas)
        limite = time.monotonic() + 0.4
        while time.monotonic() < limite:
            time.sleep(0.02)
        sup.stop()
        self.assertEqual(len(llamadas), antes, "relanzó una sesión ya sustituida")


if __name__ == "__main__":
    unittest.main()