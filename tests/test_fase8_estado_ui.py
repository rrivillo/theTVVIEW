"""Fase 8 · U-09 — Grabación: estado, parada y cierre.

Cubre el arranque del backend en _grabar(), el modal de estado,
la detención (stop/cancel) y el cierre con limpieza.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.ui.app import App
from thetvview.recording import backend as rec_backend


def _fake_stdscr(h=24, w=80):
    class S:
        pass
    s = S()
    s.h, s.w = h, w
    s.addstr_calls = []

    def gm():
        return (s.h, s.w)

    def addstr(y, x, t, a=0):
        room = s.w - 1 if x == 0 else s.w - x
        if room > 0 and len(t) > room:
            t = t[:room]
        s.addstr_calls.append((y, x, t, a))

    s.getmaxyx = gm
    s.addstr = addstr
    s.erase = lambda: s.addstr_calls.clear()
    s.getch = lambda: -1
    s.texto = lambda: " ".join(t for _y, _x, t, _a in s.addstr_calls)
    return s


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = _fake_stdscr(24, 80)
        tmp = Path(tempfile.mkdtemp())
        import thetvview.config as cfg
        self._cfg_backup = {}
        for name in ("COLLECTIONS_JSON", "FAVORITES_JSON", "RECENTS_JSON",
                     "PREFS_JSON", "PLAYLISTS_JSON", "THEME_JSON", "RECORDINGS_DIR"):
            self._cfg_backup[name] = getattr(cfg, name, None)
        for name in ("COLLECTIONS_JSON", "FAVORITES_JSON", "RECENTS_JSON",
                     "PREFS_JSON", "PLAYLISTS_JSON", "THEME_JSON"):
            setattr(cfg, name, tmp / f"{name}.json")
        cfg.RECORDINGS_DIR = tmp / "grabaciones"
        cfg.ensure_dirs = lambda: None

        def _restore_cfg():
            for name, value in self._cfg_backup.items():
                setattr(cfg, name, value)
        self.addCleanup(_restore_cfg)

        self.app = App(self.fake)
        self.app.notify = lambda *a, **kw: None
        self.app.notify_warning = lambda *a, **kw: None
        self.app.notify_error = lambda *a, **kw: None
        self.app._run_modal = lambda m: "continuar"

    def tearDown(self) -> None:
        self.app._recording_session = None
        self.app._recording_supervisor = None
        self.app._recording_session_path = None


class TestArranqueBackend(Base):
    def test_grabar_arranca_backend(self):
        """U-09: _grabar crea RecordingSession y llama start()."""
        session = rec_backend.RecordingSession(
            source_url="http://test",
            output_dir=str(self.app._recording_session_path or Path("/tmp")),
            requested_name="test.mp4",
        )
        supervisor = mock.MagicMock()
        supervisor.start.return_value = session

        with mock.patch.object(rec_backend, "RecordingSession", return_value=session), \
             mock.patch.object(rec_backend, "RecordingSupervisor", return_value=supervisor):
            self.app._autorizar_grabacion = lambda *a, **kw: True
            self.app._formulario_grabacion = lambda *a, **kw: Path("/tmp/test.mp4")
            result = self.app._grabar(source_url="http://test", destino="/tmp", nombre="test.mp4", duracion=60)

        self.assertIsNotNone(result)
        self.assertIs(self.app._recording_session, session)
        self.assertIs(self.app._recording_supervisor, supervisor)
        supervisor.start.assert_called_once()

    def test_grabar_sin_backend_notifica_error(self):
        """U-09: si start() lanza, se notifica y devuelve None."""
        supervisor = mock.MagicMock()
        supervisor.start.side_effect = RuntimeError("VLC no disponible")

        with mock.patch.object(rec_backend, "RecordingSupervisor", return_value=supervisor):
            self.app._autorizar_grabacion = lambda *a, **kw: True
            self.app._formulario_grabacion = lambda *a, **kw: Path("/tmp/test.mp4")
            result = self.app._grabar(destino="/tmp", nombre="test.mp4")

        self.assertIsNone(result)
        self.assertIsNone(self.app._recording_session)


class TestEstadoGrabacion(Base):
    def test_estado_sin_sesion(self):
        """U-09: estado sin sesión devuelve modal con mensaje."""
        modal = self.app._estado_grabacion()
        self.assertIsNotNone(modal)
        self.assertIn("No hay", modal.message)

    def test_estado_con_sesion_activa(self):
        """U-09: estado muestra datos de la sesión."""
        session = rec_backend.RecordingSession(
            source_url="http://test",
            state=rec_backend.RecordingState.RECORDING,
            started_at=__import__("time").monotonic() - 30,
        )
        self.app._recording_session = session
        modal = self.app._estado_grabacion()
        self.assertIsNotNone(modal)
        self.assertIn("recording", modal.message)
        self.assertIn("30s", modal.message)


class TestDetenerGrabacion(Base):
    def test_detener_sin_sesion(self):
        """U-09: detener sin sesión devuelve False."""
        result = self.app._detener_grabacion()
        self.assertFalse(result)

    def test_detener_con_stop(self):
        """U-09: detener llama supervisor.stop()."""
        session = mock.MagicMock()
        session.state = rec_backend.RecordingState.RECORDING
        supervisor = mock.MagicMock()
        self.app._recording_session = session
        self.app._recording_supervisor = supervisor

        result = self.app._detener_grabacion()
        self.assertTrue(result)
        supervisor.stop.assert_called_once()

    def test_detener_con_cancel(self):
        """U-09: cancelar llama supervisor.cancel()."""
        session = mock.MagicMock()
        session.state = rec_backend.RecordingState.RECORDING
        supervisor = mock.MagicMock()
        self.app._recording_session = session
        self.app._recording_supervisor = supervisor

        result = self.app._detener_grabacion(cancelar=True)
        self.assertTrue(result)
        supervisor.cancel.assert_called_once()


class TestCerrarGrabacion(Base):
    def test_cerrar_sin_sesion(self):
        """U-09: cerrar sin sesión devuelve False."""
        result = self.app._cerrar_grabacion()
        self.assertFalse(result)

    def test_cerrar_limpia_sesion(self):
        """U-09: cerrar llama cleanup y limpia atributos."""
        session = mock.MagicMock()
        supervisor = mock.MagicMock()
        self.app._recording_session = session
        self.app._recording_supervisor = supervisor

        result = self.app._cerrar_grabacion()
        self.assertTrue(result)
        supervisor.cleanup.assert_called_once_with(session)
        self.assertIsNone(self.app._recording_session)
        self.assertIsNone(self.app._recording_supervisor)


class TestAliasesU09(Base):
    def test_estado_aliases(self):
        """U-09: aliases de estado existen."""
        for name in ("estado_grabacion", "estado", "recording_state"):
            self.assertTrue(hasattr(App, name), f"App.{name} no existe")

    def test_detener_aliases(self):
        """U-09: aliases de detener existen."""
        for name in ("detener_grabacion", "detener", "stop_recording"):
            self.assertTrue(hasattr(App, name), f"App.{name} no existe")

    def test_cerrar_aliases(self):
        """U-09: aliases de cerrar existen."""
        for name in ("cerrar_grabacion", "cerrar", "close_recording"):
            self.assertTrue(hasattr(App, name), f"App.{name} no existe")


if __name__ == "__main__":
    unittest.main()
