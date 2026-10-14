"""Pruebas del planificador (Fase 7 · P2.5-C · P-01..P-06)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from thetvview.recording.backend import RecordingCapabilities, RecordingSupervisor
from thetvview.scheduler import (
    PolicyMissed,
    RetryPolicy,
    ScheduledTask,
    Scheduler,
    SchedulerConfig,
    SchedulerError,
    TaskState,
    TaskStore,
    RecordingRunner,
    validar_tarea,
)


class _ProcesoFalso:
    pid = 4242
    returncode = None

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode or 0

    def kill(self) -> None:  # pragma: no cover - defensivo
        self.returncode = -9


class TestTareaVersionada(unittest.TestCase):
    """P-01: la tarea guarda todo lo necesario para reproducirse sola."""

    def setUp(self) -> None:
        self.inicio = 1_800_000_000.0
        self.tarea = ScheduledTask(
            source="http://ejemplo.local/stream.m3u8",
            channel_id="noticias",
            start_at=self.inicio,
            duration_seconds=1800,
            destination="/tmp/grabaciones",
            tz_offset_minutes=-360,
            timezone="America/Bogota",
            retry_policy=RetryPolicy(max_retries=2, missed_grace_seconds=45),
        )

    def test_tiene_identificador_version_y_estado(self) -> None:
        self.assertTrue(self.tarea.task_id)
        self.assertEqual(self.tarea.version, 1)
        self.assertEqual(self.tarea.status, TaskState.PENDING)

    def test_calcula_el_final_a_partir_de_la_duracion(self) -> None:
        self.assertEqual(self.tarea.end_at, self.inicio + 1800)

    def test_el_instante_se_muestra_en_la_zona_de_presentacion(self) -> None:
        self.assertTrue(self.tarea.presentation_time.endswith("-06:00"))

    def test_ida_y_vuelta_de_serializacion_conserva_los_campos(self) -> None:
        copia = ScheduledTask.from_dict(self.tarea.to_dict())
        self.assertEqual(copia.definition(), self.tarea.definition())
        self.assertEqual(copia.retry_policy.max_retries, 2)
        self.assertEqual(copia.status, self.tarea.status)

    def test_acepta_instantes_en_iso(self) -> None:
        tarea = ScheduledTask(source="u", start_iso="2027-01-02T03:04:05+00:00")
        self.assertIsNotNone(tarea.start_at)
        self.assertEqual(validar_tarea(tarea), [])


class TestValidacion(unittest.TestCase):
    """P-02: nada inválido entra al planificador."""

    def test_falta_fuente(self) -> None:
        errores = validar_tarea(ScheduledTask(source="  ", start_at=time.time()))
        self.assertTrue(any("source" in e for e in errores))

    def test_falta_instante(self) -> None:
        errores = validar_tarea(ScheduledTask(source="u"))
        self.assertTrue(any("start_at" in e for e in errores))

    def test_duracion_negativa(self) -> None:
        errores = validar_tarea(ScheduledTask(source="u", start_at=1.0, duration_seconds=-1))
        self.assertTrue(any("duration_seconds" in e for e in errores))

    def test_fin_anterior_al_inicio(self) -> None:
        tarea = ScheduledTask(source="u", start_at=100.0, end_at=50.0)
        self.assertTrue(any("end_at" in e for e in validar_tarea(tarea)))

    def test_agregar_invalida_falla_con_mensaje_legible(self) -> None:
        planificador = Scheduler()
        with self.assertRaises(SchedulerError) as ctx:
            planificador.add(ScheduledTask(source="", start_at=time.time()))
        self.assertIn("source", str(ctx.exception))


class TestPersistenciaYRecuperacion(unittest.TestCase):
    """P-02 y P-03: los estados sobreviven y una tarea rota no bloquea a otras."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = TaskStore(self.dir / "tareas.json")

    def test_guarda_y_recarga(self) -> None:
        tarea = ScheduledTask(source="u", start_at=time.time() + 600, duration_seconds=60)
        planificador = Scheduler(store=self.store)
        planificador.add(tarea)
        otra = Scheduler(store=self.store)
        self.assertEqual(otra.get(tarea.task_id).status, TaskState.SCHEDULED)
        self.assertEqual(len(otra.tasks), 1)

    def test_una_tarea_corrupta_no_bloquea_a_las_demas(self) -> None:
        buena = ScheduledTask(source="u", start_at=time.time() + 600, duration_seconds=60)
        self.store.save([buena])
        datos = json.loads((self.dir / "tareas.json").read_text(encoding="utf-8"))
        datos["tasks"].extend(
            [
                {"source": "", "start_at": "no-es-un-numero"},
                {"basura": True},
                {"source": "u", "start_at": "tampoco"},
                dict(buena.to_dict()),  # duplicada por identificador
            ]
        )
        (self.dir / "tareas.json").write_text(json.dumps(datos), encoding="utf-8")

        planificador = Scheduler(store=self.store)
        self.assertEqual(len(planificador.tasks), 1)
        self.assertEqual(len(planificador.load_errors), 4)

    def test_archivo_ilegible_no_revienta(self) -> None:
        (self.dir / "tareas.json").write_text("{esto no es json", encoding="utf-8")
        planificador = Scheduler(store=self.store)
        self.assertEqual(planificador.tasks, {})
        self.assertTrue(planificador.load_errors)

    def test_una_grabacion_en_curso_no_queda_viva_al_reiniciar(self) -> None:
        tarea = ScheduledTask(
            source="u", start_at=time.time() + 60, duration_seconds=60, status=TaskState.RUNNING
        )
        self.store.save([tarea])
        planificador = Scheduler(store=self.store)
        recuperada = planificador.get(tarea.task_id)
        self.assertEqual(recuperada.status, TaskState.MISSED)
        self.assertIn("cerrada", recuperada.message)


class TestPoliticaDeTiempo(unittest.TestCase):
    """P-03: reloj, suspensión y tareas perdidas."""

    def test_una_tarea_vieja_se_marca_perdida(self) -> None:
        ahora = time.time()
        planificador = Scheduler(recorder_runner=lambda t: None)
        tarea = ScheduledTask(source="u", start_at=ahora - 10_000, duration_seconds=60)
        planificador.add(tarea)
        eventos = planificador.tick(ahora)
        self.assertEqual(eventos["missed"], [tarea.task_id])
        self.assertEqual(planificador.get(tarea.task_id).status, TaskState.MISSED)

    def test_la_politica_omitir_marca_omitida(self) -> None:
        ahora = time.time()
        planificador = Scheduler(recorder_runner=lambda t: None)
        tarea = ScheduledTask(
            source="u",
            start_at=ahora - 10_000,
            duration_seconds=60,
            retry_policy=RetryPolicy(missed_policy=PolicyMissed.SKIP),
        )
        planificador.add(tarea)
        planificador.tick(ahora)
        self.assertEqual(planificador.get(tarea.task_id).status, TaskState.SKIPPED)

    def test_la_gracia_evita_marcar_perdida_demasiado_temprano(self) -> None:
        ahora = time.time()
        planificador = Scheduler(recorder_runner=lambda t: None)
        tarea = ScheduledTask(
            source="u",
            start_at=ahora - 5,
            duration_seconds=600,
            retry_policy=RetryPolicy(missed_grace_seconds=3600),
        )
        planificador.add(tarea)
        eventos = planificador.tick(ahora)
        self.assertEqual(eventos["missed"], [])
        self.assertNotEqual(planificador.get(tarea.task_id).status, TaskState.MISSED)

    def test_reiniciar_el_reloj_no_programa_nada_dos_veces(self) -> None:
        ahora = time.time()
        arrancadas: list[str] = []

        def corredor(tarea):
            arrancadas.append(tarea.task_id)
            return tarea

        planificador = Scheduler(recorder_runner=corredor)
        tarea = ScheduledTask(source="u", start_at=ahora - 1, duration_seconds=600)
        planificador.add(tarea)
        planificador.tick(ahora)
        planificador.tick(ahora + 1)  # segundo paso del reloj
        planificador.tick(ahora + 2)
        self.assertEqual(arrancadas, [tarea.task_id])


class TestSolapamientos(unittest.TestCase):
    """P-04: una sola grabación simultánea y aviso, sin cambiar nada."""

    def test_detecta_el_solapamiento_y_avisa(self) -> None:
        a = ScheduledTask(source="A", start_at=1000, duration_seconds=100)
        b = ScheduledTask(source="B", start_at=1050, duration_seconds=100)
        planificador = Scheduler()
        planificador.add(a)
        planificador.add(b)
        planificador.detect_conflicts()
        self.assertEqual(planificador.conflicts_for(a), [b.task_id])
        self.assertIn(b.task_id, planificador.get(a.task_id).conflicts)

    def test_el_conflicto_no_cambia_hora_ni_fuente(self) -> None:
        a = ScheduledTask(source="A", start_at=1000, duration_seconds=100)
        b = ScheduledTask(source="B", start_at=1050, duration_seconds=100)
        planificador = Scheduler()
        planificador.add(a)
        planificador.add(b)
        planificador.detect_conflicts()
        self.assertEqual(a.start_at, 1000)
        self.assertEqual(a.source, "A")
        self.assertEqual(b.start_at, 1050)
        self.assertEqual(b.source, "B")

    def test_sin_solapamiento_no_hay_conflicto(self) -> None:
        a = ScheduledTask(source="A", start_at=1000, duration_seconds=100)
        b = ScheduledTask(source="B", start_at=1100, duration_seconds=100)
        planificador = Scheduler()
        planificador.add(a)
        planificador.add(b)
        self.assertEqual(planificador.detect_conflicts(), [])

    def test_solo_se_puede_grabar_una_a_la_vez(self) -> None:
        ahora = time.time()
        arrancadas: list[str] = []
        planificador = Scheduler(
            recorder_runner=lambda t: arrancadas.append(t.task_id) or t,
            config=SchedulerConfig(max_concurrent=1),
        )
        primera = ScheduledTask(source="A", start_at=ahora - 5, duration_seconds=3600)
        segunda = ScheduledTask(source="B", start_at=ahora - 1, duration_seconds=60)
        planificador.add(primera)
        planificador.add(segunda)
        eventos = planificador.tick(ahora)
        self.assertEqual(len(arrancadas), 1)
        self.assertEqual(len(planificador.active), 1)
        self.assertIn(segunda.task_id, eventos["skipped"])

    def test_la_espera_no_es_reprogramacion_silenciosa(self) -> None:
        ahora = time.time()
        planificador = Scheduler(recorder_runner=lambda t: t)
        primera = ScheduledTask(source="A", start_at=ahora - 5, duration_seconds=3600)
        segunda = ScheduledTask(source="B", start_at=ahora - 1, duration_seconds=60)
        planificador.add(primera)
        planificador.add(segunda)
        planificador.tick(ahora)
        self.assertEqual(planificador.get(segunda.task_id).start_at, segunda.start_at)
        self.assertIn("en espera", planificador.get(segunda.task_id).message)


class TestIntegracionConElGrabador(unittest.TestCase):
    """P-05: la captura la sigue haciendo el grabador manual de Fase 6."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.supervisor = RecordingSupervisor(
            backend=RecordingCapabilities(vlc_available=True, vlc_path="/usr/bin/vlc"),
            popen_factory=lambda *a, **k: _ProcesoFalso(),
        )
        self.ahora = time.time()

    def test_arranca_una_sesion_del_grabador_manual(self) -> None:
        planificador = Scheduler(recorder_runner=RecordingRunner(self.supervisor, destination=str(self.dir)))
        tarea = ScheduledTask(
            source="http://ejemplo.local/stream.m3u8",
            start_at=self.ahora - 1,
            duration_seconds=600,
            destination=str(self.dir),
        )
        planificador.add(tarea)
        eventos = planificador.tick(self.ahora)
        self.assertEqual(eventos["started"], [tarea.task_id])
        self.assertEqual(planificador.get(tarea.task_id).status, TaskState.RUNNING)
        self.assertEqual(planificador.get(tarea.task_id).recording_session_id, tarea.task_id)

    def test_sin_grabador_la_tarea_falla_con_mensaje(self) -> None:
        planificador = Scheduler()
        tarea = ScheduledTask(source="u", start_at=self.ahora - 1, duration_seconds=60)
        planificador.add(tarea)
        eventos = planificador.tick(self.ahora)
        self.assertEqual(eventos["failed"], [tarea.task_id])
        self.assertIn("grabador", planificador.get(tarea.task_id).message)

    def test_el_fallo_usa_la_politica_de_reintentos(self) -> None:
        def corredor_fallido(tarea):
            raise RuntimeError("vlc no responde")

        planificador = Scheduler(recorder_runner=corredor_fallido)
        tarea = ScheduledTask(
            source="u",
            start_at=self.ahora - 1,
            duration_seconds=60,
            retry_policy=RetryPolicy(max_retries=1),
        )
        planificador.add(tarea)
        planificador.tick(self.ahora)
        self.assertEqual(planificador.get(tarea.task_id).retries_used, 1)
        self.assertEqual(planificador.get(tarea.task_id).status, TaskState.SCHEDULED)
        planificador.tick(self.ahora)
        self.assertEqual(planificador.get(tarea.task_id).status, TaskState.FAILED)


class TestIdempotenciaYCicloDeVida(unittest.TestCase):
    """P-06: sin duplicados y sin efectos colaterales."""

    def setUp(self) -> None:
        self.ahora = time.time()

    def _tarea(self) -> ScheduledTask:
        return ScheduledTask(source="u", start_at=self.ahora + 600, duration_seconds=600)

    def test_reagregar_la_misma_tarea_no_duplica(self) -> None:
        planificador = Scheduler()
        tarea = self._tarea()
        planificador.add(tarea)
        repetida = self._tarea()
        repetida.task_id = tarea.task_id
        resultado = planificador.add(repetida)
        self.assertIs(resultado, planificador.get(tarea.task_id))
        self.assertEqual(len(planificador.tasks), 1)

    def test_mismo_id_con_otra_definicion_se_rechaza(self) -> None:
        planificador = Scheduler()
        tarea = self._tarea()
        planificador.add(tarea)
        otra = ScheduledTask(source="otra", start_at=tarea.start_at, duration_seconds=60)
        otra.task_id = tarea.task_id
        with self.assertRaises(SchedulerError):
            planificador.add(otra)

    def test_reiniciar_el_planificador_no_duplica_tareas(self) -> None:
        directorio = Path(tempfile.mkdtemp())
        store = TaskStore(directorio / "t.json")
        tarea = self._tarea()
        Scheduler(store=store).add(tarea)
        segundo = Scheduler(store=store)
        self.assertEqual(len(segundo.tasks), 1)
        segundo.add(ScheduledTask(task_id=tarea.task_id, source=tarea.source,
                                  start_at=tarea.start_at, duration_seconds=tarea.duration_seconds))
        self.assertEqual(len(segundo.tasks), 1)

    def test_cancelar_una_tarea_futura_no_toca_la_activa(self) -> None:
        planificador = Scheduler(recorder_runner=lambda t: t)
        activa = ScheduledTask(source="A", start_at=self.ahora - 5, duration_seconds=3600)
        futura = ScheduledTask(source="B", start_at=self.ahora + 600, duration_seconds=600)
        planificador.add(activa)
        planificador.add(futura)
        planificador.tick(self.ahora)
        planificador.cancel(futura.task_id)
        self.assertEqual(planificador.get(futura.task_id).status, TaskState.CANCELLED)
        self.assertEqual(planificador.get(activa.task_id).status, TaskState.RUNNING)

    def test_cancelar_una_activa_exige_force(self) -> None:
        planificador = Scheduler(recorder_runner=lambda t: t)
        activa = ScheduledTask(source="A", start_at=self.ahora - 5, duration_seconds=3600)
        planificador.add(activa)
        planificador.tick(self.ahora)
        with self.assertRaises(SchedulerError):
            planificador.cancel(activa.task_id)
        cancelada = planificador.cancel(activa.task_id, force=True)
        self.assertEqual(cancelada.status, TaskState.CANCELLED)

    def test_borrar_una_tarea_no_borra_el_archivo(self) -> None:
        archivo = Path(tempfile.mkdtemp()) / "clip.mp4"
        archivo.write_text("datos", encoding="utf-8")
        planificador = Scheduler()
        tarea = ScheduledTask(
            source="u", start_at=self.ahora + 600, duration_seconds=60, file_path=str(archivo)
        )
        planificador.add(tarea)
        planificador.remove(tarea.task_id)
        self.assertTrue(archivo.exists())

    def test_borrar_el_archivo_exige_una_accion_explicita(self) -> None:
        archivo = Path(tempfile.mkdtemp()) / "clip.mp4"
        archivo.write_text("datos", encoding="utf-8")
        planificador = Scheduler()
        tarea = ScheduledTask(
            source="u", start_at=self.ahora + 600, duration_seconds=60, file_path=str(archivo)
        )
        planificador.add(tarea)
        planificador.remove(tarea.task_id, delete_file=True)
        self.assertFalse(archivo.exists())

    def test_no_se_borra_una_grabacion_en_curso(self) -> None:
        planificador = Scheduler(recorder_runner=lambda t: t)
        activa = ScheduledTask(source="A", start_at=self.ahora - 5, duration_seconds=3600)
        planificador.add(activa)
        planificador.tick(self.ahora)
        with self.assertRaises(SchedulerError):
            planificador.remove(activa.task_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()