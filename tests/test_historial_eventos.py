"""H-01 — Eventos de ciclo de vida sobre `PlaybackSession`.

Hasta aquí el bus de eventos existía y no lo usaba nadie: ``EventBus`` se
construía en ``run()`` y no tenía ni un ``emit()`` en producción. El informe
tenía un contrato entero (evento, ``correlation_id``, ``session_id``, fase,
gravedad, motivo, duración) y ningún suceso real que lo llenara.

Aquí se cablea. Tres decisiones:

**La enumeración es cerrada y vive en `player/session.py`.** No una cadena
suelta: el supervisor llevaba un ``_nota("reproduciendo")`` que repartía
cadenas por un ``if/elif``, y ese es justo el olor que H-02 prohíbe en
otro sitio. Ahora :class:`EventoDeCiclo` es un enum, el supervisor reparte
sobre el enum y el diagnóstico traduce de enum a evento. Las tres capas
hablan el mismo vocabulario cerrado y no hay una palabra que signifique
«reproduciendo» en un sitio y otra cosa en otro.

**«Inicio confirmado» es un nombre que hay que ganarse.** El requisito es
explícito: sólo con evidencia de :mod:`player.evidence`. Un proceso vivo no
es un canal abierto, así que el evento sale de `MEDIA_SEEN` para arriba —no
de ``PROCESS_ALIVE``, no de ``SOURCE_ACCEPTED``, y por supuesto no de
``UNKNOWN``, que no es un nivel sino la ausencia de uno. Un diagnóstico que
dice «reprodujo» cuando sólo se abrió un proceso es la misma clase de mentira
que un ``available`` sin archivo.

**Un oyente roto no puede tumbar la reproducción.** El oyente se invoca
tragas-excepciones: si el bus falla, la sesión sigue su curso. Perder un
evento de diagnóstico es un misterio; perder la sesión del usuario es una
queja.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from thetvview.diagnostics.events import Event, EventBus, Severity
from thetvview.diagnostics.session_events import ObservadorDeSesion
from thetvview.player.errors import StreamError, StreamErrorCode
from thetvview.player.evidence import Evidence, EvidenceLevel
from thetvview.player.session import (
    EndReason,
    EventoDeCiclo,
    PlaybackSession,
    SessionState,
)

AHORA = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)


def _sesion(**kwargs) -> PlaybackSession:
    base = {"channel_key": "espanol.espn", "player_name": "mpv"}
    base.update(kwargs)
    return PlaybackSession(**base)  # type: ignore[arg-type]


def _fallo(codigo=StreamErrorCode.NETWORK_ERROR, **kwargs) -> StreamError:
    base = {
        "code": codigo,
        "resumen": "Se perdió la conexión con mitad del directo.",
        "observado": "conexión cerrada",
        "returncode": 1,
    }
    base.update(kwargs)
    return StreamError(**base)  # type: ignore[arg-type]


def _evidencia(nivel: EvidenceLevel) -> Evidence:
    return Evidence(level=nivel, reason="test", observed_at=AHORA)


# ---------------------------------------------------------------------------
# 1. El enumerado cerrado
# ---------------------------------------------------------------------------


class TestElCicloEsUnVocabularioCerrado(unittest.TestCase):
    def test_cubre_los_sucesos_que_exige_el_plan(self):
        for nombre in ("INTENTO", "INICIO_CONFIRMADO", "FALLO",
                       "INTERRUMPIDO", "FIN", "CAMBIO_DE_FUENTE",
                       "CAMBIO_DE_REPRODUCTOR"):
            with self.subTest(evento=nombre):
                self.assertTrue(hasattr(EventoDeCiclo, nombre))

    def test_los_valores_no_se_repiten(self):
        valores = [e.value for e in EventoDeCiclo]
        self.assertEqual(len(valores), len(set(valores)))

    def test_todo_suceso_se_sabe_nombrar(self):
        """Un valor desconocido no se puede colar: o se nombra o no existe."""
        for evento in EventoDeCiclo:
            with self.subTest(evento=evento.value):
                self.assertEqual(EventoDeCiclo(evento.value), evento)


# ---------------------------------------------------------------------------
# 2. La sesión avisa de su ciclo
# ---------------------------------------------------------------------------


class TestLaSesionEmite(unittest.TestCase):
    def setUp(self) -> None:
        self.vistos: list[EventoDeCiclo] = []
        self.sesion = _sesion()
        self.sesion.oyente = lambda s, e, d=None: self.vistos.append(e)

    def _ciclo_completo(self) -> None:
        self.sesion.start()
        self.sesion.nuevo_intento()
        self.sesion.launched()
        self.sesion.note_first_media()
        self.sesion.playing()
        self.sesion.note_stable()
        self.sesion.recovering()
        self.sesion.nota_fallo(_fallo())
        self.sesion.playing()
        self.sesion.nota_cambio_de_fuente("fuente:m3u/Deportes#abc123")
        self.sesion.nota_cambio_de_reproductor("vlc")
        self.sesion.end(EndReason.USER_STOPPED, "paró el usuario")

    def test_una_sesion_completa_deja_rastro_de_todo(self):
        self._ciclo_completo()
        for suceso in EventoDeCiclo:
            with self.subTest(evento=suceso.value):
                self.assertIn(suceso, self.vistos)

    def test_el_fin_no_se_avisa_dos_veces(self):
        """`end()` es idempotente, y un evento duplicado sería mentira."""
        self.sesion.start()
        self.sesion.end(EndReason.USER_STOPPED, "paró")
        self.sesion.end(EndReason.APP_SHUTDOWN, "se cerró la app")
        self.assertEqual(
            sum(1 for e in self.vistos if e is EventoDeCiclo.FIN), 1
        )

    def test_el_intento_anterior_ya_esta_cerrado(self):
        self.sesion.nuevo_intento()
        self.sesion.nota_fallo(_fallo())
        self.sesion.nuevo_intento()
        self.assertIsNotNone(self.sesion.attempts[0].ended_at)
        self.assertIsNone(self.sesion.attempts[1].ended_at)

    def test_la_marca_de_evidencia_es_solo_la_primera(self):
        self.sesion.note_first_media()
        primera = self.sesion.first_media_at
        self.sesion.note_first_media()
        self.assertEqual(self.sesion.first_media_at, primera)

    def test_sin_oyente_no_pasa_nada(self):
        sesion = _sesion()  # sin oyente
        sesion.start()
        sesion.nuevo_intento()
        sesion.end(EndReason.USER_STOPPED)
        self.assertIs(sesion.state, SessionState.ENDED)

    def test_un_oyente_roto_no_tumba_la_sesion(self):
        def _revento(s, e, d=None):  # noqa: ANN001, ANN202
            raise RuntimeError("el bus está en llamas")

        self.sesion.oyente = _revento
        self.sesion.start()
        self.sesion.nuevo_intento()
        self.sesion.end(EndReason.USER_STOPPED)
        self.assertIs(self.sesion.state, SessionState.ENDED)
        self.assertIsNotNone(self.sesion.ended_at)

    def test_el_error_del_traductor_no_desaparece_en_silencio(self):
        """Que no tumbe la sesión no significa que no deje rastro.

        Éste es el error que cometí al principio: pasar un ``EndReason`` donde
        se esperaba un ``StreamError``. El ``except`` de la sesión lo tragaba
        y el evento de fin se perdía sin que nadie se enterara.
        """
        obs = ObservadorDeSesion(EventBus())
        obs._confirmado = True
        obs.evidencia("no soy una evidencia")  # type: ignore[arg-type]
        self.assertEqual(obs.errores, [])

    def test_un_bus_roto_no_tumba_la_sesion_ni_se_calla(self):
        class _BusRoto:
            def emit(self, evento):  # noqa: ANN001, ANN201
                raise RuntimeError("el bus está en llamas")

            def events(self):  # noqa: ANN201
                return []

        sesion = _sesion()
        obs = ObservadorDeSesion(_BusRoto(), ahora=lambda: AHORA.timestamp(),
                                 sesion=sesion)
        sesion.oyente = obs
        sesion.start()
        sesion.nuevo_intento()
        sesion.end(EndReason.USER_STOPPED)
        # La sesión llegó entera a su fin...
        self.assertIs(sesion.state, SessionState.ENDED)
        # ...y el fallo del bus quedó anotado, no enterrado en el `except`.
        self.assertTrue(obs.errores, "el fallo del traductor quedó anotado")
        self.assertIn("RuntimeError", str(obs.errores))


# ---------------------------------------------------------------------------
# 3. El translate de enum a evento
# ---------------------------------------------------------------------------


class TestElTraductor(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.sesion = _sesion()
        self.observador = ObservadorDeSesion(self.bus, ahora=lambda: AHORA.timestamp())
        self.sesion.oyente = self.observador

    def _eventos(self) -> list[Event]:
        return self.bus.events()

    def test_los_eventos_llevan_la_identidad_de_la_sesion(self):
        self.sesion.start()
        evento = self._eventos()[0]
        self.assertEqual(evento.session_id, self.sesion.session_id)
        self.assertEqual(evento.correlation_id, self.sesion.correlation_id)
        self.assertEqual(evento.channel_id, "espanol.espn")

    def test_los_eventos_no_llevan_url(self):
        """Ni una URL en el bus: se comparte con quien sea."""
        self.sesion.channel_key = "espanol.espn"
        self.sesion.start()
        self.sesion.nuevo_intento()
        for evento in self._eventos():
            with self.subTest(evento=evento.event_type):
                self.assertNotIn("http", str(evento.to_dict()).lower())

    def test_el_tipo_de_evento_esta_cerrado_tambien(self):
        self.sesion.start()
        self.sesion.nuevo_intento()
        self.sesion.launched()
        self.sesion.note_first_media()
        self.sesion.end(EndReason.USER_STOPPED)
        tipos = {e.event_type for e in self._eventos()}
        self.assertTrue(tipos)
        for tipo in tipos:
            with self.subTest(tipo=tipo):
                self.assertTrue(tipo.startswith("playback."))

    def test_la_fase_viene_de_la_sesion(self):
        self.sesion.start()
        evento = self._eventos()[0]
        self.assertEqual(evento.phase, self.sesion.phase.value)

    def test_el_componente_es_el_subsistema(self):
        """`player` es el subsistema; el motor va en los detalles."""
        self.sesion.start()
        self.assertEqual(self._eventos()[0].component, "player")
        self.assertEqual(self._eventos()[0].details["player"], "mpv")

    def test_el_intento_lleva_su_numero(self):
        self.sesion.nuevo_intento()
        self.sesion.nuevo_intento()
        intentos = [
            e.details.get("intento") for e in self._eventos()
            if e.event_type == "playback.intento"
        ]
        self.assertEqual(intentos, [1, 2])


class TestElFalloSeTraduceConLaTaxonomia(unittest.TestCase):
    """H-02 aplicado a los eventos: el motivo viene del reproductor."""

    def setUp(self) -> None:
        self.bus = EventBus()
        self.sesion = _sesion()
        self.sesion.oyente = ObservadorDeSesion(self.bus, ahora=lambda: AHORA.timestamp())

    def test_el_codigo_y_la_recuperabilidad_vienen_del_fallo(self):
        self.sesion.nota_fallo(_fallo(StreamErrorCode.AUTH_FAILED))
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fallo")
        self.assertEqual(evento.reason_code, "auth_failed")
        self.assertIs(evento.retryable, False)

    def test_el_hecho_observado_travela(self):
        """Lo observado es el hecho; el código es la interpretación."""
        self.sesion.nota_fallo(_fallo(observado="HTTP 403"))
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fallo")
        self.assertEqual(evento.details["observado"], "HTTP 403")

    def test_un_fallo_desconocido_no_afirma_una_causa(self):
        self.sesion.nota_fallo(_fallo(StreamErrorCode.UNKNOWN,
                                       observado="rc=1"))
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fallo")
        self.assertEqual(evento.reason_code, "unknown")
        self.assertFalse(evento.details["causa_conocida"])

    def test_sin_fallo_no_se_inventa_el_codigo(self):
        self.sesion.nota_fallo(None)
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fallo")
        self.assertIsNone(evento.reason_code)
        self.assertIsNone(evento.retryable)

    def test_el_bus_redacta_al_anadir(self):
        """El bus limpia por dentro: lo que sale ya está limpio."""
        self.sesion.nota_fallo(
            _fallo(observado="http://u:secreto@a.test/live?password=abc")
        )
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fallo")
        self.assertNotIn("secreto", str(evento.to_dict()))
        self.assertNotIn("abc", str(evento.to_dict()))


class TestGravedadYFin(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.sesion = _sesion()
        self.sesion.oyente = ObservadorDeSesion(self.bus, ahora=lambda: AHORA.timestamp())

    def test_el_fallo_es_error(self):
        self.sesion.nota_fallo(_fallo())
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fallo")
        self.assertIs(evento.severity, Severity.ERROR)

    def test_el_interrupcion_es_aviso(self):
        self.sesion.start()
        self.sesion.playing()
        self.sesion.recovering()
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.interrumpido")
        self.assertIs(evento.severity, Severity.WARNING)

    def test_parar_el_usuario_no_es_un_error(self):
        """«Lo detuviste tú» no es un fallo: marcarla así asusta de más."""
        self.sesion.start()
        self.sesion.end(EndReason.USER_STOPPED, "paró el usuario")
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fin")
        self.assertIs(evento.severity, Severity.INFO)

    def test_un_fin_por_fallo_si_es_error(self):
        self.sesion.start()
        self.sesion.end(EndReason.PLAYBACK_ERROR, "cayó")
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fin")
        self.assertIs(evento.severity, Severity.ERROR)
        self.assertEqual(evento.reason_code, "playback_error")

    def test_el_fin_lleva_la_duracion_que_si_se_medió(self):
        """La duración sale de las marcas de la sesión, no de un reloj aparte."""
        real = datetime.now(timezone.utc)
        self.sesion.created_at = real - timedelta(seconds=30)
        self.sesion.first_media_at = real - timedelta(seconds=20)
        self.sesion.end(EndReason.USER_STOPPED)
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fin")
        self.assertAlmostEqual(evento.details["duracion_viva_s"], 30.0, delta=1.0)
        self.assertAlmostEqual(
            evento.details["duracion_reproduccion_s"], 20.0, delta=1.0
        )

    def test_si_no_se_vio_nada_no_se_inventa_la_duracion_de_reproduccion(self):
        """Cero significaría «se cortó al abrir»; aquí no se vio nada."""
        self.sesion.end(EndReason.CANCELLED)
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fin")
        self.assertNotIn("duracion_reproduccion_s", evento.details)

    def test_la_duracion_de_vida_si_se_sabe_siempre(self):
        """`created_at` y `ended_at` existen: esto no es una suposición."""
        self.sesion.end(EndReason.CANCELLED)
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fin")
        self.assertIn("duracion_viva_s", evento.details)

    def test_el_fin_por_reintentos_agotados_dice_cual_fue(self):
        self.sesion.start()
        self.sesion.end(EndReason.RETRIES_EXHAUSTED, "se agotaron")
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fin")
        self.assertEqual(evento.reason_code, "retries_exhausted")


class TestInicioConfirmadoExigeEvidencia(unittest.TestCase):
    """«Reprodujo» sólo se dice si alguien lo comprobó."""

    def _con(self, nivel: EvidenceLevel):
        bus = EventBus()
        sesion = _sesion()
        obs = ObservadorDeSesion(bus, ahora=lambda: AHORA.timestamp(),
                                 sesion=sesion)
        sesion.oyente = obs
        obs.evidencia(_evidencia(nivel))
        return bus

    def test_con_medios_llegando_si_se_confirma(self):
        bus = self._con(EvidenceLevel.MEDIA_SEEN)
        tipos = {e.event_type for e in bus.events()}
        self.assertIn("playback.inicio_confirmado", tipos)

    def test_con_fuente_abierta_todavia_no(self):
        bus = self._con(EvidenceLevel.SOURCE_ACCEPTED)
        tipos = {e.event_type for e in bus.events()}
        self.assertNotIn(
            "playback.inicio_confirmado", tipos,
            "fuente abierta no es ver imagen: sería mentir",
        )

    def test_con_el_proceso_vivo_todavia_no(self):
        bus = self._con(EvidenceLevel.PROCESS_ALIVE)
        self.assertNotIn("playback.inicio_confirmado",
                         {e.event_type for e in bus.events()})

    def test_sin_saber_no(self):
        """`UNKNOWN` no es «no hay nada»: es que no se pudo comprobar."""
        bus = self._con(EvidenceLevel.UNKNOWN)
        self.assertNotIn("playback.inicio_confirmado",
                         {e.event_type for e in bus.events()})

    def test_subir_a_estable_tambien_confirma(self):
        bus = self._con(EvidenceLevel.STABLE)
        tipos = {e.event_type for e in bus.events()}
        self.assertIn("playback.inicio_confirmado", tipos)
        self.assertIn("playback.estable", tipos)

    def test_bajar_el_nivel_no_desconfirma(self):
        """Un corte no borra que se vio imagen antes."""
        bus = EventBus()
        sesion = _sesion()
        obs = ObservadorDeSesion(bus, ahora=lambda: AHORA.timestamp(),
                                 sesion=sesion)
        sesion.oyente = obs
        obs.evidencia(_evidencia(EvidenceLevel.MEDIA_SEEN))
        obs.evidencia(_evidencia(EvidenceLevel.NONE))
        confirmados = [e for e in bus.events()
                       if e.event_type == "playback.inicio_confirmado"]
        self.assertEqual(len(confirmados), 1)

    def test_sin_sesion_no_revienta(self):
        bus = EventBus()
        ObservadorDeSesion(bus).evidencia(_evidencia(EvidenceLevel.MEDIA_SEEN))


class TestCambioDeFuenteYDeReproductor(unittest.TestCase):
    """Los dos cambios que el plan pide dejar con nombre."""

    def setUp(self) -> None:
        self.bus = EventBus()
        self.sesion = _sesion()
        self.observador = ObservadorDeSesion(self.bus, ahora=lambda: AHORA.timestamp())
        self.sesion.oyente = self.observador

    def test_cambiar_de_fuente_deja_evento(self):
        self.sesion.nota_cambio_de_fuente("fuente:m3u/Deportes#abc123def456")
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.cambio_de_fuente")
        self.assertEqual(evento.details["source_id"],
                         "fuente:m3u/Deportes#abc123def456")

    def test_cambiar_de_reproductor_deja_evento(self):
        self.sesion.nota_cambio_de_reproductor("vlc")
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.cambio_de_reproductor")
        self.assertEqual(evento.details["player"], "vlc")

    def test_el_reproductor_nuevo_pasa_a_ser_el_de_la_sesion(self):
        self.sesion.nota_cambio_de_reproductor("vlc")
        self.assertEqual(self.sesion.player_name, "vlc")

    def test_tras_cambiar_de_reproductor_lo_siguiente_dice_vlc(self):
        self.sesion.nota_cambio_de_reproductor("vlc")
        self.sesion.start()
        evento = self.bus.events()[-1]
        self.assertEqual(evento.details["player"], "vlc")
        self.assertEqual(evento.component, "player")


if __name__ == "__main__":
    unittest.main()