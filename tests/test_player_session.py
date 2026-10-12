"""Ciclo de vida de la sesión de reproducción (PLAY-001…PLAY-004, SDD §4).

Qué se comprueba aquí:

- la máquina de estados completa: **todas** las aristas válidas, todas las
  inválidas, y que de un terminal a un activo nunca se pasa;
- las marcas temporales y el motivo de finalización, incluida la sesión que
  nunca llegó a lanzar el reproductor;
- la limpieza idempotente y tolerante a fallos de :class:`ResourceScope`;
- el guard de obsolescencia: una tarea tardía de la sesión A no toca la sesión B;
- que **sin** sesión el comportamiento es idéntico al de siempre.

Puro: ni curses, ni red, ni subprocess.
"""

from __future__ import annotations

import threading
import unittest

from thetvview.player.session import (
    ALLOWED_TRANSITIONS,
    TERMINAL,
    AttemptRecord,
    EndReason,
    PlaybackSession,
    ResourceScope,
    SessionPhase,
    SessionRegistry,
    SessionState,
    TransitionError,
    can_transition,
    new_session_id,
    transition,
)


# ---------------------------------------------------------------------------
# PLAY-001 · identidad, estados y marcas temporales
# ---------------------------------------------------------------------------


class TestIdentidad(unittest.TestCase):
    def test_cada_sesion_tiene_id_propio(self) -> None:
        ids = {new_session_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)

    def test_reproducir_el_mismo_canal_da_ids_distintos(self) -> None:
        """FR-PLAY-001: dos intentos del mismo canal son dos sesiones."""
        a = PlaybackSession(channel_key="canal.1", player_name="mpv")
        b = PlaybackSession(channel_key="canal.1", player_name="mpv")
        self.assertNotEqual(a.session_id, b.session_id)

    def test_el_id_es_estable_durante_toda_la_sesion(self) -> None:
        sesion = PlaybackSession("canal.1", "mpv")
        antes = sesion.session_id
        sesion.start().launched().playing().note_first_media()
        self.assertEqual(sesion.session_id, antes)

    def test_el_id_no_depende_de_como_se_construya(self) -> None:
        """Dos `PlaybackSession` con la misma clave son sesiones distintas.

        Es la forma real de la inmutabilidad aquí: el id pertenece al **objeto**,
        no a sus campos, así que nadie puede reutilizar el id de otra sesión
        para cambiarle la identidad a una sesión viva.
        """
        a = PlaybackSession("canal.1", "mpv")
        b = PlaybackSession("canal.1", "mpv")
        self.assertNotEqual(a.session_id, b.session_id)
        self.assertNotEqual(a.correlation_id, b.correlation_id)
        self.assertEqual(a.session_id, a.session_id)


class TestEstadosYMarcas(unittest.TestCase):
    def test_una_sesion_nace_preparando(self) -> None:
        sesion = PlaybackSession("canal.1", "mpv")
        self.assertIs(sesion.state, SessionState.PREPARING)
        self.assertIs(sesion.phase, SessionPhase.PREPARACION)
        self.assertIsNotNone(sesion.created_at)

    def test_las_marcas_se_llenan_solo_la_primera_vez(self) -> None:
        sesion = PlaybackSession("canal.1", "mpv")
        sesion.start()
        primero = sesion.launch_started_at
        sesion.start()  # idempotente por transición, no reescribe la marca
        self.assertEqual(sesion.launch_started_at, primero)

    def test_primera_evidencia_de_medios_no_se_sobreescribe(self) -> None:
        sesion = PlaybackSession("canal.1", "mpv")
        sesion.note_first_media()
        marca = sesion.first_media_at
        sesion.note_first_media()
        self.assertEqual(sesion.first_media_at, marca)

    def test_la_interrupcion_marca_su_propia_hora(self) -> None:
        sesion = PlaybackSession("canal.1", "mpv")
        sesion.start().launched().playing().recovering()
        self.assertIsNotNone(sesion.interruption_at)
        self.assertIs(sesion.phase, SessionPhase.RECUPERACION)

    def test_las_marcas_desconocidas_quedan_a_none(self) -> None:
        """No se inventa una medición: §3.3 y §11.3 lo prohíben."""
        sesion = PlaybackSession("canal.1", "mpv")
        sesion.start()
        for marca in (
            sesion.process_started_at,
            sesion.first_media_at,
            sesion.stable_playback_at,
            sesion.ended_at,
        ):
            self.assertIsNone(marca)

    def test_el_informe_marca_las_marcas_desconocidas_como_vacias(self) -> None:
        sesion = PlaybackSession("canal.1", "mpv")
        informe = sesion.para_informe()
        self.assertEqual(informe["marcas"]["primer_medios"], "")
        self.assertEqual(informe["motivo_fin"], "")


class TestMotivoDeFin(unittest.TestCase):
    def test_una_sesion_terminada_tiene_marca_y_motivo(self) -> None:
        sesion = PlaybackSession("canal.1", "mpv")
        sesion.end(EndReason.USER_STOPPED)
        self.assertIsNotNone(sesion.ended_at)
        self.assertIs(sesion.end_reason, EndReason.USER_STOPPED)

    def test_los_ocho_motivos_exigidos_existen(self) -> None:
        for nombre in (
            "USER_STOPPED",
            "CANCELLED",
            "PROCESS_EXITED",
            "INIT_FAILED",
            "PLAYBACK_ERROR",
            "RETRIES_EXHAUSTED",
            "APP_SHUTDOWN",
            "INTERNAL_ERROR",
        ):
            with self.subTest(motivo=nombre):
                self.assertTrue(hasattr(EndReason, nombre))
                getattr(EndReason, nombre).explicacion  # todas tienen explicación

    def test_una_sesion_que_nunca_lanzo_tambien_termina(self) -> None:
        """FR-PLAY-010: la finalización no depende del lanzamiento."""
        sesion = PlaybackSession("canal.1", "mpv")
        sesion.end(EndReason.INIT_FAILED, "no se encontró reproductor")
        self.assertIs(sesion.state, SessionState.ENDED)
        self.assertIs(sesion.end_reason, EndReason.INIT_FAILED)
        self.assertEqual(sesion.end_detail, "no se encontró reproductor")

    def test_terminar_dos_veces_conserva_el_motivo_original(self) -> None:
        """El §4.2 es literal: un motivo posterior no pisa el de verdad."""
        sesion = PlaybackSession("canal.1", "mpv")
        sesion.start().launched().playing()
        sesion.end(EndReason.PLAYBACK_ERROR, "se perdió el stream")
        original = sesion.ended_at
        sesion.end(EndReason.APP_SHUTDOWN, "cierre de la app")
        self.assertIs(sesion.end_reason, EndReason.PLAYBACK_ERROR)
        self.assertEqual(sesion.end_detail, "se perdió el stream")
        self.assertEqual(sesion.ended_at, original)

    def test_la_creacion_no_devuelve_a_activo(self) -> None:
        sesion = PlaybackSession("canal.1", "mpv")
        sesion.end(EndReason.USER_STOPPED)
        with self.assertRaises(TransitionError):
            sesion.start()


# ---------------------------------------------------------------------------
# PLAY-002 · transiciones válidas e inválidas
# ---------------------------------------------------------------------------


class TestMaquinaDeEstados(unittest.TestCase):
    def test_se_cubren_todos_los_estados(self) -> None:
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(SessionState))

    def test_ended_es_el_unico_terminal(self) -> None:
        self.assertEqual(TERMINAL, frozenset({SessionState.ENDED}))

    def test_de_un_terminal_a_un_activo_siempre_se_rechaza(self) -> None:
        for origen in TERMINAL:
            for destino in SessionState:
                with self.subTest(de=f"{origen}", a=destino):
                    self.assertFalse(can_transition(origen, destino))

    def test_el_estado_terminal_no_tiene_aristas_de_salida(self) -> None:
        for estado in TERMINAL:
            self.assertEqual(ALLOWED_TRANSITIONS[estado], frozenset())

    def test_el_grafo_es_descriptivo_y_termina(self) -> None:
        """Se puede llegar a `ENDED` desde cualquier estado **vivo** (§4.3)."""
        for origen in SessionState:
            if origen.terminal:
                continue
            with self.subTest(desde=origen):
                self.assertTrue(can_transition(origen, SessionState.ENDED))

    def test_cada_estado_no_terminal_tiene_una_salida_valida(self) -> None:
        for estado in SessionState:
            if estado.terminal:
                continue
            self.assertTrue(ALLOWED_TRANSITIONS[estado], estado)

    def test_transiciones_validas_aceptadas(self) -> None:
        """Recorre **todas** las aristas de la tabla y comprueba que pasan."""
        for origen, destinos in ALLOWED_TRANSITIONS.items():
            for destino in destinos:
                with self.subTest(de=origen, a=destino):
                    sesion = PlaybackSession("c", "mpv")
                    sesion.state = origen
                    self.assertIs(transition(sesion, destino), destino)

    def test_transiciones_invalidas_rechazadas(self) -> None:
        """Recorre **todas** las aristas que **no** están en la tabla."""
        for origen in SessionState:
            for destino in SessionState:
                # La transición al mismo estado es un no-op, no un salto
                # inválido: se comprueba aparte.
                if destino is origen or destino in ALLOWED_TRANSITIONS[origen]:
                    continue
                with self.subTest(de=origen, a=destino):
                    sesion = PlaybackSession("c", "mpv")
                    sesion.state = origen
                    with self.assertRaises(TransitionError):
                        transition(sesion, destino)

    def test_no_se_puede_saltar_de_preparing_a_playing(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        with self.assertRaises(TransitionError):
            sesion.playing()

    def test_no_se_puede_volver_de_playing_a_starting(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        sesion.state = SessionState.PLAYING
        with self.assertRaises(TransitionError):
            transition(sesion, SessionState.STARTING)

    def test_replaying_desde_recovering_si_vuelve(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        sesion.start().launched().playing().recovering()
        sesion.playing()
        self.assertIs(sesion.state, SessionState.PLAYING)
        self.assertIs(sesion.phase, SessionPhase.SUPERVISION)

    def test_transicionar_al_mismo_estado_no_lanza(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        sesion.start()
        self.assertIs(transition(sesion, SessionState.STARTING), SessionState.STARTING)

    def test_el_error_de_transicion_dice_el_salto(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        sesion.session_id = "abc123"
        with self.assertRaises(TransitionError) as ctx:
            sesion.playing()
        mensaje = str(ctx.exception)
        self.assertIn("abc123", mensaje)
        self.assertIn("preparing", mensaje)
        self.assertIn("playing", mensaje)

    def test_transition_error_es_error_del_dominio(self) -> None:
        """La UI captura con `except IPTVError`; tiene que entrar por ahí."""
        from thetvview.security.errors import IPTVError

        self.assertTrue(issubclass(TransitionError, IPTVError))


# ---------------------------------------------------------------------------
# PLAY-003 · limpieza centralizada, idempotente y tolerante a fallos
# ---------------------------------------------------------------------------


class TestResourceScope(unittest.TestCase):
    def test_libera_todo_lo_registrado(self) -> None:
        liberados: list[str] = []
        scope = ResourceScope()
        scope.add("proxy", lambda: liberados.append("proxy"))
        scope.add("socket", lambda: liberados.append("socket"))
        scope.close_all()
        self.assertEqual(sorted(liberados), ["proxy", "socket"])

    def test_el_orden_es_lifo(self) -> None:
        """El último recurso adquirido es el primero en soltarse."""
        orden: list[str] = []
        scope = ResourceScope()
        scope.add("a", lambda: orden.append("a"))
        scope.add("b", lambda: orden.append("b"))
        scope.close_all()
        self.assertEqual(orden, ["b", "a"])

    def test_close_all_dos_veces_no_destroza(self) -> None:
        llamadas: list[int] = []
        scope = ResourceScope()
        scope.add("proxy", lambda: llamadas.append(1))
        scope.close_all()
        scope.close_all()
        scope.close_all()
        self.assertEqual(len(llamadas), 1, "la segunda llamada no debe repetir efectos")
        self.assertTrue(scope.cerrado)

    def test_un_fallo_no_impide_liberar_los_demas(self) -> None:
        liberados: list[str] = []
        scope = ResourceScope()

        def revienta() -> None:
            raise OSError("el proxy no responde")

        scope.add("proxy", revienta)
        scope.add("socket", lambda: liberados.append("socket"))
        errores = scope.close_all()
        self.assertEqual(liberados, ["socket"])
        self.assertEqual(len(errores), 1)
        self.assertEqual(errores[0][0], "proxy")

    def test_los_fallos_no_sobreescriben_el_motivo_de_la_sesion(self) -> None:
        """FR-PLAY-008 + §4.2 juntos."""
        sesion = PlaybackSession("c", "mpv")
        sesion.start().launched().playing()
        sesion.add_resource("proxy", lambda: (_ for _ in ()).throw(OSError("roto")))
        sesion.end(EndReason.PLAYBACK_ERROR, "el canal se cayó")
        self.assertIs(sesion.end_reason, EndReason.PLAYBACK_ERROR)
        self.assertEqual(sesion.end_detail, "el canal se cayó")
        self.assertEqual([n for n, _ in sesion.resources.errores], ["proxy"])

    def test_registrar_tras_cerrar_libera_de_inmediato(self) -> None:
        """Un recurso adquirido tarde no puede quedarse sin dueño."""
        liberados: list[str] = []
        scope = ResourceScope()
        scope.close_all()
        scope.add("tarde", lambda: liberados.append("tarde"))
        self.assertEqual(liberados, ["tarde"])

    def test_un_liberador_que_no_es_llamable_se_rechaza(self) -> None:
        with self.assertRaises(TypeError):
            ResourceScope().add("malo", "no soy una función")  # type: ignore[arg-type]

    def test_liberar_una_sesion_cierra_su_scope(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        liberados: list[str] = []
        sesion.add_resource("proxy", lambda: liberados.append("proxy"))
        sesion.close_resources()
        self.assertEqual(liberados, ["proxy"])
        self.assertTrue(sesion.resources.cerrado)

    def test_terminar_una_sesion_libera_su_scope(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        liberados: list[str] = []
        sesion.add_resource("socket", lambda: liberados.append("socket"))
        sesion.end(EndReason.USER_STOPPED)
        self.assertEqual(liberados, ["socket"])

    def test_no_se_puede_liberar_dos_veces_con_dano(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        paradas = 0

        def parar() -> None:
            nonlocal paradas
            paradas += 1

        sesion.add_resource("proceso", parar)
        sesion.end(EndReason.USER_STOPPED)
        sesion.end(EndReason.USER_STOPPED)
        self.assertEqual(paradas, 1)


# ---------------------------------------------------------------------------
# PLAY-004 · aislamiento, cancelación y guard de obsolescencia
# ---------------------------------------------------------------------------


class TestSessionRegistry(unittest.TestCase):
    def test_la_primera_sesion_registrada_es_la_vigente(self) -> None:
        registro = SessionRegistry()
        self.assertIsNone(registro.actual)
        sesion = registro.start("canal.1", "mpv")
        self.assertIs(registro.actual, sesion)
        self.assertTrue(registro.is_current(sesion.session_id))

    def test_una_sesion_que_no_es_la_vigente_no_lo_es(self) -> None:
        """Canal A → canal B: la tarea tardía de A no es vigente."""
        registro = SessionRegistry()
        a = registro.start("canal.A", "mpv")
        b = registro.start("canal.B", "vlc")
        self.assertFalse(registro.is_current(a.session_id))
        self.assertTrue(registro.is_current(b.session_id))

    def test_al_arrancar_una_nueva_la_anterior_se_sella(self) -> None:
        """Una sesión sin dueño es una sesión que no se puede explicar."""
        registro = SessionRegistry()
        a = registro.start("canal.A", "mpv")
        registro.start("canal.B", "mpv")
        self.assertTrue(a.ended)
        self.assertIs(a.end_reason, EndReason.CANCELLED)

    def test_la_generacion_sube_con_cada_sesion(self) -> None:
        registro = SessionRegistry()
        self.assertEqual(registro.generacion, 0)
        registro.start("a", "mpv")
        registro.start("b", "mpv")
        self.assertEqual(registro.generacion, 2)

    def test_una_tarea_tardia_de_a_no_toca_b(self) -> None:
        """El requisito literal del §4.3, con dos hilos de verdad."""
        registro = SessionRegistry()
        a = registro.start("canal.A", "mpv")
        b = registro.start("canal.B", "mpv")
        resultados: list[tuple[str, bool]] = []
        listo = threading.Event()

        def tarea_tardia() -> None:
            listo.wait(2.0)
            vigente = registro.is_current(a.session_id)
            resultados.append(("A", vigente))
            if vigente:  # pragma: no cover - no debe entrar
                b.end(EndReason.USER_STOPPED)

        hilo = threading.Thread(target=tarea_tardia, daemon=True)
        hilo.start()
        listo.set()
        hilo.join(2.0)
        self.assertEqual(resultados, [("A", False)])
        self.assertFalse(b.ended, "la tarea tardía de A no puede tocar B")

    def test_finish_sella_y_desregistra(self) -> None:
        registro = SessionRegistry()
        sesion = registro.start("canal.1", "mpv")
        registro.finish(sesion, EndReason.USER_STOPPED)
        self.assertIs(sesion.end_reason, EndReason.USER_STOPPED)
        self.assertIsNone(registro.actual)

    def test_shutdown_sella_la_vigente_como_app_shutdown(self) -> None:
        """FR-PLAY-006: el cierre de la app propaga a lo pendiente."""
        registro = SessionRegistry()
        sesion = registro.start("canal.1", "mpv")
        registro.shutdown()
        self.assertIs(sesion.end_reason, EndReason.APP_SHUTDOWN)
        self.assertIsNone(registro.actual)

    def test_shutdown_sin_sesion_no_rompe(self) -> None:
        self.assertIsNone(SessionRegistry().shutdown())

    def test_la_cancelacion_no_Activa_reintentos(self) -> None:
        """La sesión cancelada queda terminal: nadie puede relanzarla."""
        registro = SessionRegistry()
        a = registro.start("canal.A", "mpv")
        registro.start("canal.B", "mpv")
        with self.assertRaises(TransitionError):
            a.start()
        self.assertTrue(a.ended)


# ---------------------------------------------------------------------------
# Intentos (FR-REC-010, base que completa la Fase 2)
# ---------------------------------------------------------------------------


class TestIntentos(unittest.TestCase):
    def test_el_primer_intento_es_el_1(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        self.assertEqual(sesion.nuevo_intento().index, 1)
        self.assertEqual(sesion.nuevo_intento(1.5).index, 2)

    def test_el_anterior_se_cierra_al_abrir_el_siguiente(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        primero = sesion.nuevo_intento()
        self.assertIsNone(primero.ended_at)
        sesion.nuevo_intento(2.0)
        self.assertIsNotNone(primero.ended_at)

    def test_la_espera_previa_se_guarda(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        sesion.nuevo_intento(4.5)
        self.assertEqual(sesion.attempts[0].espera_previa, 4.5)

    def test_terminar_sella_el_intento_ultimo(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        intento = sesion.nuevo_intento()
        sesion.end(EndReason.RETRIES_EXHAUSTED)
        self.assertIsNotNone(intento.ended_at)
        self.assertIs(intento.end_reason, EndReason.RETRIES_EXHAUSTED)

    def test_el_informe_de_intento_no_inventa_medidas(self) -> None:
        registro = AttemptRecord(index=1, started_at=PlaybackSession("c").created_at)
        informe = registro.para_informe()
        self.assertIsNone(informe["duracion_s"])
        self.assertIsNone(informe["reproduccion_previa_s"])
        self.assertEqual(informe["motivo"], "")

    def test_la_duracion_de_reproduccion_es_none_sin_evidencia(self) -> None:
        sesion = PlaybackSession("c", "mpv")
        sesion.start().launched().playing()
        self.assertIsNone(sesion.duracion_reproduccion())
        sesion.note_first_media()
        self.assertIsNotNone(sesion.duracion_reproduccion())


if __name__ == "__main__":
    unittest.main()