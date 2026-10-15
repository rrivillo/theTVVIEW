"""Fase 8 · U-10 — Planificador: pantalla y ciclo.

Cubre la pantalla SchedulerScreen, la integración del Scheduler en App,
el tick en el bucle principal y las acciones de crear/cancelar/borrar.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.ui.app import App
from thetvview.scheduler import backend as sched_backend


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
                     "PREFS_JSON", "PLAYLISTS_JSON", "THEME_JSON", "RECORDINGS_DIR",
                     "SCHEDULER_JSON"):
            self._cfg_backup[name] = getattr(cfg, name, None)
        for name in ("COLLECTIONS_JSON", "FAVORITES_JSON", "RECENTS_JSON",
                     "PREFS_JSON", "PLAYLISTS_JSON", "THEME_JSON"):
            setattr(cfg, name, tmp / f"{name}.json")
        cfg.RECORDINGS_DIR = tmp / "grabaciones"
        cfg.SCHEDULER_JSON = tmp / "scheduler.json"
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

        # Inicializar scheduler manualmente (run() no se llama en tests)
        from thetvview.scheduler.backend import Scheduler, TaskStore, RecordingRunner
        store = TaskStore(tmp / "scheduler.json")
        runner = RecordingRunner(supervisor=None)
        self.app._scheduler = Scheduler(store=store, recorder_runner=runner)

    def tearDown(self) -> None:
        self.app._scheduler = None


class TestSchedulerIntegration(Base):
    def test_scheduler_inicializado_en_app(self):
        """U-10: App tiene _scheduler inicializado."""
        self.assertIsNotNone(self.app._scheduler)

    def test_scheduler_es_instancia_de_scheduler(self):
        """U-10: _scheduler es una instancia de Scheduler."""
        self.assertIsInstance(self.app._scheduler, sched_backend.Scheduler)

    def test_scheduler_puede_añadir_tarea(self):
        """U-10: Se puede añadir una tarea programada."""
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        added = self.app._scheduler.add(task)
        self.assertEqual(added.task_id, task.task_id)

    def test_scheduler_puede_cancelar_tarea(self):
        """U-10: Se puede cancelar una tarea programada."""
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        cancelled = self.app._scheduler.cancel(task.task_id)
        self.assertEqual(cancelled.status, sched_backend.TaskState.CANCELLED)

    def test_scheduler_puede_borrar_tarea(self):
        """U-10: Se puede borrar una tarea programada."""
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        self.app._scheduler.remove(task.task_id)
        with self.assertRaises(sched_backend.SchedulerError):
            self.app._scheduler.get(task.task_id)

    def test_scheduler_detecta_conflictos(self):
        """U-10: El scheduler detecta solapamientos entre tareas."""
        task1 = sched_backend.ScheduledTask(
            source="http://test1",
            start_at=1000.0,
            duration_seconds=3600,
        )
        task2 = sched_backend.ScheduledTask(
            source="http://test2",
            start_at=2000.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task1)
        self.app._scheduler.add(task2)
        self.app._scheduler.detect_conflicts()
        self.assertTrue(len(task1.conflicts) > 0 or len(task2.conflicts) > 0)

    def test_tick_es_idempotente(self):
        """U-10: tick() no duplica tareas ni procesos."""
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        self.app._scheduler.tick()
        self.app._scheduler.tick()
        tasks = self.app._scheduler.tasks_list()
        self.assertEqual(len(tasks), 1)


class TestSchedulerScreen(Base):
    def test_pantalla_scheduler_existe(self):
        """U-10: SchedulerScreen se puede instanciar."""
        from thetvview.ui.screens import SchedulerScreen
        screen = SchedulerScreen(self.app)
        self.assertIsNotNone(screen)

    def test_pantalla_scheduler_muestra_estado_vacío(self):
        """U-10: Sin tareas muestra estado vacío."""
        from thetvview.ui.screens import SchedulerScreen
        screen = SchedulerScreen(self.app)
        with mock.patch("thetvview.ui.colors.pair", return_value=0):
            screen.render(self.fake)
        self.assertIn("programadas", self.fake.texto().lower())

    def test_pantalla_scheduler_muestra_tareas(self):
        """U-10: Con tareas las muestra en la lista."""
        from thetvview.ui.screens import SchedulerScreen
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        screen = SchedulerScreen(self.app)
        with mock.patch("thetvview.ui.colors.pair", return_value=0):
            screen.render(self.fake)
        self.assertIn("test", self.fake.texto())

    def test_pantalla_scheduler_añadir_acción(self):
        """U-10: 'a' devuelve acción add_scheduled_task."""
        from thetvview.ui.screens import SchedulerScreen
        screen = SchedulerScreen(self.app)
        result = screen.handle_key(ord("a"))
        self.assertEqual(result, {"action": "add_scheduled_task"})

    def test_pantalla_scheduler_cancelar_acción(self):
        """U-10: 'c' devuelve acción cancel_scheduled_task."""
        from thetvview.ui.screens import SchedulerScreen
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        screen = SchedulerScreen(self.app)
        result = screen.handle_key(ord("c"))
        self.assertEqual(result["action"], "cancel_scheduled_task")
        self.assertEqual(result["task_id"], task.task_id)

    def test_pantalla_scheduler_borrar_acción(self):
        """U-10: 'd' devuelve acción remove_scheduled_task."""
        from thetvview.ui.screens import SchedulerScreen
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        screen = SchedulerScreen(self.app)
        result = screen.handle_key(ord("d"))
        self.assertEqual(result["action"], "remove_scheduled_task")
        self.assertEqual(result["task_id"], task.task_id)

    def test_pantalla_scheduler_recargar(self):
        """U-10: reload() actualiza la lista de tareas."""
        from thetvview.ui.screens import SchedulerScreen
        screen = SchedulerScreen(self.app)
        self.assertEqual(len(screen.tasks), 0)
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        screen.reload()
        self.assertEqual(len(screen.tasks), 1)


class TestSchedulerActions(Base):
    def test_show_scheduler_pantalla(self):
        """U-10: show_scheduler() push SchedulerScreen."""
        self.app.show_scheduler()
        from thetvview.ui.screens import SchedulerScreen
        self.assertIsInstance(self.app.screen, SchedulerScreen)

    def test_add_scheduled_task_con_datos_validos(self):
        """U-10: add_scheduled_task crea tarea con datos válidos."""
        self.app._prompt_form = lambda *a, **kw: {
            "source": "http://test",
            "start_at": "2099-01-01 12:00",
            "duration": "3600",
            "destino": "/tmp",
        }
        self.app.add_scheduled_task()
        self.assertEqual(len(self.app._scheduler.tasks_list()), 1)

    def test_add_scheduled_task_sin_fuente(self):
        """U-10: add_scheduled_task rechaza sin fuente."""
        self.app._prompt_form = lambda *a, **kw: {
            "source": "",
            "start_at": "2099-01-01 12:00",
            "duration": "3600",
            "destino": "/tmp",
        }
        self.app.add_scheduled_task()
        self.assertEqual(len(self.app._scheduler.tasks_list()), 0)

    def test_add_scheduled_task_hora_invalida(self):
        """U-10: add_scheduled_task rechaza hora inválida."""
        self.app._prompt_form = lambda *a, **kw: {
            "source": "http://test",
            "start_at": "invalida",
            "duration": "3600",
            "destino": "/tmp",
        }
        self.app.add_scheduled_task()
        self.assertEqual(len(self.app._scheduler.tasks_list()), 0)

    def test_cancel_scheduled_task(self):
        """U-10: cancel_scheduled_task cancela una tarea."""
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        self.app.cancel_scheduled_task(task.task_id)
        self.assertEqual(
            self.app._scheduler.get(task.task_id).status,
            sched_backend.TaskState.CANCELLED,
        )

    def test_remove_scheduled_task(self):
        """U-10: remove_scheduled_task borra una tarea."""
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        self.app.remove_scheduled_task(task.task_id)
        self.assertEqual(len(self.app._scheduler.tasks_list()), 0)

    def test_show_scheduled_task(self):
        """U-10: show_scheduled_task muestra modal con detalle."""
        task = sched_backend.ScheduledTask(
            source="http://test",
            start_at=9999999999.0,
            duration_seconds=3600,
        )
        self.app._scheduler.add(task)
        self.app.show_scheduled_task(task.task_id)
        # No debe lanzar excepción


class TestChannelsScreenSchedulerShortcut(Base):
    def test_tecla_P_abre_scheduler(self):
        """U-10: 'P' en ChannelsScreen abre el planificador."""
        from thetvview.ui.screens import ChannelsScreen
        from thetvview.models import Playlist, Channel

        playlist = Playlist(name="Test", source="http://test", kind="m3u")
        channel = Channel(name="Canal Test", url="http://test/channel")
        playlist.channels = [channel]

        screen = ChannelsScreen(self.app, playlist)
        result = screen.handle_key(ord("P"))
        self.assertEqual(result, {"action": "show_scheduler"})


if __name__ == "__main__":
    unittest.main()
