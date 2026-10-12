"""Evidencia de reproducción y política de reintentos (REC-001…REC-005, SDD §5).

El hueco que estos tests cierran es concreto: **«el proceso existe» no es
«se está viendo»**. Antes, `poll() is None` publicaba `PLAYING`, así que un
mpv abierto sobre un manifiesto vacío y uno decodificando eran indistinguibles.

Sin red, sin reproductor y **sin `sleep`**: el reloj y el `rng` son inyectables.

- `MpvSignalSource` con un doble de `MpvIpc`.
- `RetryPolicy` con `rng=lambda: 0.5` y esperas fijas.
- El supervisor con procesos falsos y esperas de milisegundos.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timezone

from thetvview.player.errors import NO_REINTENTABLE, StreamError, StreamErrorCode, explain
from thetvview.player.evidence import (
    STABLE_SECONDS,
    Evidence,
    EvidenceLevel,
    MpvSignalSource,
    ProcessAliveSignal,
    UnknownSignalSource,
    advance_seconds,
    signal_source_for,
)
from thetvview.player.session import PlaybackSession
from thetvview.player.supervisor import (
    MAX_WAIT_SECONDS,
    PlaybackState,
    ReconnectPolicy,
    ReconnectSupervisor,
    RetryPolicy,
    retryable,
)


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class IpcFalso:
    """Doble de ``MpvIpc``: devuelve lo que se le diga, o revienta.

    ``posiciones`` se **consume**: cada llamada toma el primer valor y, cuando se
    agota, devuelve ``None`` (propiedad ausente). Para una serie que avanza, se
    pasa `siguiente`, que se llama en cada consulta.
    """

    def __init__(
        self,
        posiciones=None,
        error: Exception | None = None,
        siguiente=None,
    ) -> None:
        #: Cola de valores a devolver. `None` = propiedad ausente.
        self.posiciones = list(posiciones or [])
        self.error = error
        self.siguiente = siguiente
        self.llamadas = 0

    def get_property(self, nombre: str):  # noqa: ANN201
        self.llamadas += 1
        if self.error is not None:
            raise self.error
        if self.siguiente is not None:
            return self.siguiente(self.llamadas)
        return self.posiciones.pop(0) if self.posiciones else None


class RelojFalso:
    """Reloj manual: el test avanza el tiempo cuando quiere, no cuando duerme."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def avanzar(self, segundos: float) -> None:
        self.t += segundos


class ProcFalso:
    def __init__(self, *, vivo: bool = True, code: int | None = 1) -> None:
        self.vivo = vivo
        self.code = code

    def poll(self):  # noqa: ANN201
        return None if self.vivo else self.code


# ---------------------------------------------------------------------------
# REC-001 · la escalera de evidencia
# ---------------------------------------------------------------------------


class TestEscalera(unittest.TestCase):
    def test_los_cinco_niveles_existen_y_se_ordenan(self) -> None:
        escalera = [
            EvidenceLevel.NONE,
            EvidenceLevel.PROCESS_ALIVE,
            EvidenceLevel.SOURCE_ACCEPTED,
            EvidenceLevel.MEDIA_SEEN,
            EvidenceLevel.STABLE,
        ]
        rangos = [n.rank for n in escalera]
        self.assertEqual(rangos, sorted(rangos))
        self.assertEqual(rangos[0], 0)

    def test_unknown_no_esta_en_la_escalera(self) -> None:
        """«No lo sé» no se puede comparar con «lo sé y es que no hay nada»."""
        self.assertLess(EvidenceLevel.UNKNOWN.rank, EvidenceLevel.NONE.rank)
        self.assertFalse(EvidenceLevel.UNKNOWN.comproblable)
        self.assertTrue(EvidenceLevel.NONE.comproblable)

    def test_advance_seconds_es_none_si_algo_es_desconocido(self) -> None:
        self.assertIsNone(
            advance_seconds(EvidenceLevel.UNKNOWN, EvidenceLevel.STABLE)
        )
        self.assertIsNone(
            advance_seconds(EvidenceLevel.NONE, EvidenceLevel.UNKNOWN)
        )

    def test_advance_seconds_cuenta_los_peldanos(self) -> None:
        self.assertEqual(
            advance_seconds(EvidenceLevel.PROCESS_ALIVE, EvidenceLevel.STABLE), 3.0
        )

    def test_cada_nivel_tiene_texto(self) -> None:
        for nivel in EvidenceLevel:
            with self.subTest(nivel=nivel):
                self.assertTrue(nivel.texto())


class TestFuenteDesconocida(unittest.TestCase):
    def test_siempre_unknown_nunca_exito_ficticio(self) -> None:
        """FR-REC-002: la falta de evidencia es «desconocida», no «funciona»."""
        fuente = UnknownSignalSource("vlc")
        for _ in range(5):
            self.assertIs(fuente.sample().level, EvidenceLevel.UNKNOWN)

    def test_explica_por_que_no_se_puede_comprobar(self) -> None:
        fuente = UnknownSignalSource("mplayer")
        self.assertIn("mplayer", fuente.sample().reason)
        self.assertIn("no expone", fuente.sample().reason)

    def test_se_usa_para_vlc_y_mplayer(self) -> None:
        for motor in ("vlc", "mplayer"):
            with self.subTest(motor=motor):
                self.assertIsInstance(signal_source_for(motor), UnknownSignalSource)

    def test_mpv_sin_ipc_tambien_es_desconocida(self) -> None:
        """Sin canal de control no hay nada que mirar: adivinar sería inventar."""
        self.assertIsInstance(signal_source_for("mpv"), UnknownSignalSource)

    def test_mpv_con_ipc_si_tiene_fuente_measurable(self) -> None:
        fuente = signal_source_for("mpv", ipc=IpcFalso([1.0]))
        self.assertIsInstance(fuente, MpvSignalSource)

    def test_la_evidencia_desconocida_no_se_puede_degradar_a_nada(self) -> None:
        evidencia = UnknownSignalSource().sample()
        self.assertFalse(evidencia.estable)
        self.assertIn("sin comprobar", evidencia.resumen())


class TestSoloElProceso(unittest.TestCase):
    def test_la_evidencia_minima_es_el_proceso(self) -> None:
        self.assertIs(ProcessAliveSignal().sample().level, EvidenceLevel.PROCESS_ALIVE)

    def test_no_se_promociona_a_estable_sola(self) -> None:
        """Sin más que mirar, no hay forma honesta de subir."""
        self.assertFalse(ProcessAliveSignal().sample().estable)

    def test_dice_que_el_proceso_no_demuestra_nada(self) -> None:
        self.assertIn("no demuestra", ProcessAliveSignal().sample().reason)


# ---------------------------------------------------------------------------
# REC-001 · MpvSignalSource
# ---------------------------------------------------------------------------


class TestMpvSignalSource(unittest.TestCase):
    def _fuente(self, posiciones, *, estable: float = STABLE_SECONDS):
        self.reloj = RelojFalso()
        return MpvSignalSource(
            IpcFalso(posiciones), stable_seconds=estable, reloj=self.reloj
        )

    def test_sin_posicion_no_hay_evidencia_de_medios(self) -> None:
        fuente = self._fuente([None, None])
        for _ in range(3):
            self.assertLessEqual(fuente.sample().level.rank, EvidenceLevel.SOURCE_ACCEPTED.rank)

    def test_una_sola_lectura_no_dice_que_avanza(self) -> None:
        """`time-pos` existe pero aún no se ha visto cambiar: no hay avance."""
        fuente = self._fuente([12.5])
        medida = fuente.sample()
        self.assertIs(medida.level, EvidenceLevel.SOURCE_ACCEPTED)
        self.assertEqual(media_position := medida.media_position, 12.5)
        self.assertIn("aún no avanza", medida.reason)

    def _fuente_que_avanza(self, paso: float = 5.0, *, estable: float = 30.0):
        """Fuente cuya posición del medio avanza `paso` por sondeo."""
        self.reloj = RelojFalso()
        ipc = IpcFalso(siguiente=lambda n: 10.0 + n * paso)
        return MpvSignalSource(ipc, stable_seconds=estable, reloj=self.reloj)

    def test_un_reloj_congelado_no_llega_a_estable(self) -> None:
        """El caso que `poll() is None` no distingue: proceso vivo, sin imagen."""
        fuente = self._fuente([30.0] * 8)
        for _ in range(6):
            self.reloj.avanzar(30.0)
            medida = fuente.sample()
            self.assertIs(medida.level, EvidenceLevel.SOURCE_ACCEPTED)
            self.assertFalse(medida.estable)
        self.assertIn("no avanza", medida.reason)

    def test_un_salto_de_un_milisegundo_no_cuenta_como_avance(self) -> None:
        """mpv publica con milisegundos: 0.001 s de diferencia es ruido."""
        fuente = self._fuente([10.0, 10.001, 10.002, 10.003], estable=5.0)
        for _ in range(4):
            self.reloj.avanzar(10.0)
            medida = fuente.sample()
        self.assertIs(medida.level, EvidenceLevel.SOURCE_ACCEPTED)
        self.assertFalse(medida.estable)

    def test_el_reloj_avanzando_llega_a_estable(self) -> None:
        fuente = self._fuente_que_avanza(5.0, estable=30.0)
        for _ in range(9):
            self.reloj.avanzar(5.0)
            medida = fuente.sample()
        self.assertIs(medida.level, EvidenceLevel.STABLE)
        self.assertTrue(medida.estable)
        self.assertGreaterEqual(medida.continuo, 30.0)

    def test_avanzar_una_vez_no_basta_para_estable(self) -> None:
        """Un salto aislado es «llegando imagen», no «estable»."""
        fuente = self._fuente_que_avanza(5.0, estable=60.0)
        self.reloj.avanzar(5.0)
        fuente.sample()
        self.reloj.avanzar(5.0)
        medida = fuente.sample()
        self.assertIs(medida.level, EvidenceLevel.MEDIA_SEEN)
        self.assertFalse(medida.estable)

    def test_una_parada_reinicia_la_continuidad(self) -> None:
        """El umbral es de avance **continuo**: una parada lo rompe."""
        fuente = self._fuente_que_avanza(5.0, estable=20.0)
        for _ in range(3):
            self.reloj.avanzar(5.0)
            fuente.sample()
        self.assertGreater(fuente.sample().continuo, 0.0)
        # Ahora el reloj se queda congelado: la continuidad vuelve a cero.
        congelada = self._fuente([50.0] * 6, estable=20.0)
        for _ in range(3):
            self.reloj.avanzar(5.0)
            congelada.sample()
        self.assertEqual(congelada.sample().continuo, 0.0)

    def test_un_ipc_caido_baja_el_nivel_y_dice_por_que(self) -> None:
        fuente = MpvSignalSource(
            IpcFalso(error=OSError("el socket no responde")),
            reloj=RelojFalso(),
        )
        medida = fuente.sample()
        self.assertIs(medida.level, EvidenceLevel.UNKNOWN)
        self.assertIn("no se pudo leer", medida.reason)
        self.assertFalse(medida.estable)

    def test_una_posicion_no_numerica_no_inventa(self) -> None:
        fuente = self._fuente(["no soy un número", True, object()])
        for _ in range(3):
            medida = fuente.sample()
            self.assertIsNone(medida.media_position)

    def test_la_consulta_al_ipc_no_es_un_interrogatorio(self) -> None:
        """Un `get_property` por sondeo, no tres."""
        ipc = IpcFalso([10.0, 11.0])
        reloj = RelojFalso()
        fuente = MpvSignalSource(ipc, reloj=reloj)
        reloj.avanzar(5.0)
        fuente.sample()
        self.assertEqual(ipc.llamadas, 1)

    def test_el_umbral_por_defecto_es_treinta_segundos(self) -> None:
        self.assertEqual(STABLE_SECONDS, 30.0)


# ---------------------------------------------------------------------------
# REC-003 · modelo común de error
# ---------------------------------------------------------------------------


class TestCodigosDeError(unittest.TestCase):
    def test_se_agregan_los_que_faltaban_del_sdd(self) -> None:
        for nombre in (
            "DNS",
            "HTTP_RATE_LIMIT",
            "MANIFEST_INVALID",
            "MEDIA_DEMUX",
            "CODEC_UNSUPPORTED",
            "TRACK_SELECTION",
            "PLAYER_STARTUP",
            "PLAYER_EXIT",
            "TIMEOUT",
            "CANCELLED",
        ):
            with self.subTest(codigo=nombre):
                self.assertTrue(hasattr(StreamErrorCode, nombre))

    def test_los_codigos_preexistentes_no_se_renombran(self) -> None:
        for nombre in (
            "INVALID_URL",
            "UNSUPPORTED_PROTOCOL",
            "AUTH_FAILED",
            "TLS_FAILED",
            "BUFFERING_TIMEOUT",
            "BACKEND_ERROR",
            "PLAYER_MISSING",
            "UNKNOWN",
        ):
            with self.subTest(codigo=nombre):
                self.assertTrue(hasattr(StreamErrorCode, nombre))

    def test_cada_codigo_tiene_descripcion(self) -> None:
        from thetvview.player.errors import DESCRIPCION

        for codigo in StreamErrorCode:
            with self.subTest(codigo=codigo):
                self.assertTrue(DESCRIPCION.get(codigo))

    def test_el_texto_de_no_hay_red_no_sale_en_ninguno(self) -> None:
        """El §12.2 del SDD-M: ningún texto de este módulo lleva la URL."""
        from thetvview.player.errors import DESCRIPCION

        for codigo, texto in DESCRIPCION.items():
            with self.subTest(codigo=codigo):
                self.assertNotIn("http://", texto)
                self.assertNotIn("https://", texto)


# ---------------------------------------------------------------------------
# REC-004 · clasificación de reintento
# ---------------------------------------------------------------------------


class TestRetryable(unittest.TestCase):
    def test_los_permanentes_no_se_reintentan(self) -> None:
        for codigo in (
            StreamErrorCode.AUTH_FAILED,
            StreamErrorCode.INVALID_URL,
            StreamErrorCode.UNSUPPORTED_PROTOCOL,
            StreamErrorCode.PLAYER_MISSING,
            StreamErrorCode.CANCELLED,
            StreamErrorCode.CODEC_UNSUPPORTED,
        ):
            with self.subTest(codigo=codigo):
                self.assertFalse(retryable(codigo))
                self.assertFalse(codigo.reintentable)

    def test_un_corte_de_red_si_se_reintenta(self) -> None:
        for codigo in (
            StreamErrorCode.NETWORK_ERROR,
            StreamErrorCode.CONNECTION_FAILED,
            StreamErrorCode.CONNECTION_TIMEOUT,
            StreamErrorCode.SERVER_ERROR,
            StreamErrorCode.TLS_FAILED,
            StreamErrorCode.HTTP_RATE_LIMIT,
            StreamErrorCode.PLAYER_EXIT,
            StreamErrorCode.UNKNOWN,
        ):
            with self.subTest(codigo=codigo):
                self.assertTrue(retryable(codigo))

    def test_lo_desconocido_si_se_reintenta(self) -> None:
        """Sin clasificar puede ser un corte de dos segundos: eso es lo que
        la reconexión existe para arreglar."""
        self.assertTrue(retryable(None))

    def test_el_conjunto_de_permanentes_es_explicito(self) -> None:
        self.assertEqual(
            NO_REINTENTABLE,
            frozenset(
                {
                    StreamErrorCode.AUTH_FAILED,
                    StreamErrorCode.INVALID_URL,
                    StreamErrorCode.UNSUPPORTED_PROTOCOL,
                    StreamErrorCode.PLAYER_MISSING,
                    StreamErrorCode.CANCELLED,
                    StreamErrorCode.CODEC_UNSUPPORTED,
                }
            ),
        )

    def test_acepta_un_stream_error_completo(self) -> None:
        self.assertFalse(retryable(None, error=explain(1, "HTTP 401 Unauthorized")))
        self.assertTrue(retryable(None, error=explain(1, "")))


class TestRetryPolicy(unittest.TestCase):
    def _politica(self, **kw) -> RetryPolicy:
        kw.setdefault("rng", lambda: 0.0)
        return RetryPolicy(**kw)

    def test_el_primer_intento_es_inmediato(self) -> None:
        self.assertEqual(self._politica().espera(1), 0.0)

    def test_el_backoff_crece(self) -> None:
        politica = self._politica()
        esperas = [politica.espera(n) for n in (2, 3, 4, 5)]
        self.assertEqual(esperas, sorted(esperas))
        self.assertGreater(esperas[0], 0)

    def test_la_espera_tiene_tope(self) -> None:
        politica = self._politica()
        for intento in (10, 50, 500):
            with self.subTest(intento=intento):
                self.assertLessEqual(politica.espera(intento), MAX_WAIT_SECONDS)

    def test_el_jitter_varia_la_espera(self) -> None:
        sin = RetryPolicy(rng=lambda: 0.0).espera(3)
        con = RetryPolicy(rng=lambda: 1.0).espera(3)
        self.assertNotEqual(sin, con)
        self.assertGreater(con, sin)

    def test_el_jitter_no_ahorra_espera(self) -> None:
        """El jitter suma: jamás puede hacer esperar menos de la tabla."""
        base = RetryPolicy(rng=lambda: 0.0).espera(4)
        for azar in (0.0, 0.25, 0.5, 1.0):
            with self.subTest(azar=azar):
                self.assertGreaterEqual(RetryPolicy(rng=lambda: azar).espera(4), base)

    def test_un_rng_roto_no_rompe_la_espera(self) -> None:
        def rng_malo() -> float:
            raise RuntimeError("sin aleatoriedad")

        self.assertGreaterEqual(RetryPolicy(rng=rng_malo).espera(3), 1.0)

    def test_retry_after_manda_sobre_el_backoff(self) -> None:
        decision = self._politica().espera_con_retry_after(2, retry_after=7.0)
        self.assertEqual(decision.segundos, 7.0)
        self.assertEqual(decision.origen, "retry-after")
        self.assertFalse(decision.acotado)

    def test_retry_after_se_acota_a_la_politica(self) -> None:
        decision = self._politica(max_wait=5.0).espera_con_retry_after(
            2, retry_after=120.0
        )
        self.assertEqual(decision.segundos, 5.0)
        self.assertTrue(decision.acotado)
        self.assertTrue(decision.nota)

    def test_sin_retry_after_es_backoff(self) -> None:
        decision = self._politica().espera_con_retry_after(3)
        self.assertEqual(decision.origen, "backoff")

    def test_un_retry_after_ilegible_no_rompe(self) -> None:
        decision = self._politica().espera_con_retry_after(3, retry_after="mañana")
        self.assertEqual(decision.origen, "backoff")

    def test_los_tres_cortes(self) -> None:
        politica = self._politica()
        self.assertFalse(
            politica.puede_reintentar(0, error=explain(1, "HTTP 401 Unauthorized"))
        )
        self.assertFalse(politica.puede_reintentar(0, cancelado=True))
        self.assertFalse(politica.puede_reintentar(5))
        self.assertTrue(politica.puede_reintentar(4, error=explain(1, "")))


# ---------------------------------------------------------------------------
# REC-005 · evidencia y causa inferida
# ---------------------------------------------------------------------------


class TestEvidenciaVsCausa(unittest.TestCase):
    def test_sin_texto_la_causa_queda_desconocida(self) -> None:
        error = explain(1, "")
        self.assertIs(error.code, StreamErrorCode.UNKNOWN)
        self.assertFalse(error.causa_conocida)
        self.assertEqual(error.confianza, "desconocida")

    def test_la_evidencia_del_primer_error_se_conserva(self) -> None:
        error = explain(1, "Failed to open 'http://x'")
        self.assertIn("terminó con el código 1", error.observado)
        self.assertIn("Failed to open", error.observado)

    def test_el_observado_no_crece_sin_limite(self) -> None:
        error = explain(1, "x" * 5_000)
        self.assertLess(len(error.observado), 400)

    def test_el_informe_no_lleva_el_detalle_crudo(self) -> None:
        """El informe se exporta a un archivo: el texto del motor no va."""
        informe = explain(1, "Failed to open").para_informe()
        self.assertNotIn("detalle", informe)
        self.assertIn("causa", informe)
        self.assertIn("confianza", informe)

    def test_con_causa_conocida_la_confianza_lo_dice(self) -> None:
        error = explain(1, "HTTP 403 Forbidden")
        self.assertIs(error.code, StreamErrorCode.AUTH_FAILED)
        self.assertEqual(error.confianza, "observada")

    def test_un_codigo_igual_a_cero_tambien_es_desconocido(self) -> None:
        self.assertIs(explain(0, "").code, StreamErrorCode.UNKNOWN)

    def test_reintentado_deja_el_intento_anterior_cerrado(self) -> None:
        from thetvview.player.session import EndReason

        sesion = PlaybackSession("c", "mpv")
        sesion.nuevo_intento()
        sesion.nuevo_intento(2.0)
        sesion.end(EndReason.RETRIES_EXHAUSTED)
        for intento in sesion.attempts:
            with self.subTest(intento=intento.index):
                self.assertIsNotNone(intento.ended_at)
        self.assertEqual(sesion.attempts[0].index, 1)
        self.assertEqual(sesion.attempts[1].index, 2)
        # Sólo el intento en curso lleva el motivo: los anteriores ya se
        # cerraron cuando empezó el siguiente.
        self.assertEqual(sesion.attempts[0].end_reason, None)
        self.assertEqual(sesion.attempts[1].end_reason, EndReason.RETRIES_EXHAUSTED)


# ---------------------------------------------------------------------------
# Integración en el supervisor
# ---------------------------------------------------------------------------


class PoliticaCorta(ReconnectPolicy):
    def __init__(self, max_attempts: int = 5) -> None:
        super().__init__(backoff=(1, 2, 5, 10), max_attempts=max_attempts)

    def espera_para(self, intento: int) -> float:
        return super().espera_para(intento) / 1000.0


class _FuenteFija:
    """Fuente de evidencia que devuelve siempre lo mismo."""

    def __init__(self, nivel: EvidenceLevel) -> None:
        self.nivel = nivel
        self.llamadas = 0

    def sample(self) -> Evidence:
        self.llamadas += 1
        return Evidence(level=self.nivel, reason="doble de test")


class TestSupervisorConEvidencia(unittest.TestCase):
    @staticmethod
    def _esperar(condicion, timeout: float = 3.0) -> bool:  # noqa: ANN001
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if condicion():
                return True
            time.sleep(0.01)
        return False

    def test_un_proceso_vivo_sin_evidencia_no_es_estable(self) -> None:
        """El requisito literal del §5.3."""
        sup = ReconnectSupervisor(
            relanzar=lambda: ProcFalso(),
            poll_seconds=0.01,
            evidencia=UnknownSignalSource("vlc"),
        )
        sup.start(ProcFalso(vivo=True))
        try:
            self.assertTrue(self._esperar(lambda: sup.estado is PlaybackState.PLAYING))
            sup._muestrear_evidencia()
            self.assertIs(sup.evidencia_nivel, EvidenceLevel.UNKNOWN)
            self.assertFalse(sup.evidencia_actual.estable)
        finally:
            sup.stop()

    def test_un_salto_de_evidencia_no_degrada_lo_ya_sabido(self) -> None:
        sup = ReconnectSupervisor(poll_seconds=0.01, evidencia=None)
        sup.evidencia_actual = Evidence(level=EvidenceLevel.STABLE)
        fuente = _FuenteFija(EvidenceLevel.NONE)
        sup.evidencia = fuente  # type: ignore[assignment]
        sup._proxima_evidencia = 0.0
        sup._muestrear_evidencia()
        self.assertIs(sup.evidencia_nivel, EvidenceLevel.STABLE)

    def test_la_estabilidad_resetea_los_contadores_del_ciclo(self) -> None:
        """FR-REC-008: sólo tras evidencia, no porque el proceso siga vivo."""
        sup = ReconnectSupervisor(poll_seconds=0.01, evidencia=None)
        sup.intentos = 4
        sup.reconexiones = 4
        sup.evidencia = _FuenteFija(EvidenceLevel.STABLE)  # type: ignore[assignment]
        sup._proxima_evidencia = 0.0
        sup._muestrear_evidencia()
        self.assertEqual(sup.intentos, 0)
        self.assertEqual(sup.reconexiones, 4)

    def test_el_texto_de_reproduccion_distingue_sin_comprobar(self) -> None:
        # Sin fuente declarada, el texto es el de siempre: no se añade matiz que
        # nadie ha comprobado.
        sup = ReconnectSupervisor(poll_seconds=0.01)
        self.assertEqual(sup.texto_reproduccion, "Reproduciendo")
        # Con fuente declarada que no puede comprobar, el texto lo dice.
        con_fuente = ReconnectSupervisor(
            poll_seconds=0.01, evidencia=UnknownSignalSource("vlc")
        )
        con_fuente._proxima_evidencia = 0.0
        con_fuente._muestrear_evidencia()
        self.assertIn("sin comprobar", con_fuente.texto_reproduccion)


class TestSupervisorConErrores(unittest.TestCase):
    @staticmethod
    def _esperar(condicion, timeout: float = 3.0) -> bool:  # noqa: ANN001
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if condicion():
                return True
            time.sleep(0.01)
        return False

    def _sup(self, relanzar, clasificar, policy: RetryPolicy) -> ReconnectSupervisor:  # noqa: ANN001
        return ReconnectSupervisor(
            relanzar=relanzar,
            poll_seconds=0.01,
            policy=PoliticaCorta(),
            retry_policy=policy,
            clasificar_error=clasificar,
        )

    def test_un_401_no_genera_reintentos(self) -> None:
        """El efecto visible que promete el plan: un 401 deja de repetirse."""
        llamadas: list[int] = []

        def relanzar() -> object:
            llamadas.append(1)
            return ProcFalso(vivo=False)

        sup = self._sup(
            relanzar,
            lambda proc: explain(proc.poll(), "HTTP/1.1 401 Unauthorized"),
            RetryPolicy(rng=lambda: 0.0),
        )
        sup.start(ProcFalso(vivo=False))
        try:
            self.assertTrue(self._esperar(lambda: sup.estado is PlaybackState.ERROR))
        finally:
            sup.stop()
        self.assertEqual(llamadas, [], "un 401 no se reintenta")

    def test_un_corte_de_red_si_se_reintenta(self) -> None:
        llamadas: list[int] = []

        def relanzar() -> object:
            llamadas.append(1)
            return ProcFalso(vivo=False)

        sup = self._sup(
            relanzar,
            lambda proc: explain(proc.poll(), "Connection timed out"),
            RetryPolicy(rng=lambda: 0.0, max_attempts=2),
        )
        sup.start(ProcFalso(vivo=False))
        try:
            self.assertTrue(self._esperar(lambda: len(llamadas) >= 2))
        finally:
            sup.stop()
        self.assertGreaterEqual(len(llamadas), 2)

    def test_retry_after_se_aplica_y_se_registra(self) -> None:
        """FR-REC-007: se respeta y se dice si hubo que recortarlo."""
        politica = RetryPolicy(rng=lambda: 0.0, max_wait=2.0)
        sup = self._sup(
            lambda: ProcFalso(vivo=False),
            lambda proc: StreamError(
                code=StreamErrorCode.HTTP_RATE_LIMIT,
                resumen="límite",
                retry_after=1.5,
            ),
            politica,
        )
        sup.start(ProcFalso(vivo=False))
        try:
            self.assertTrue(self._esperar(lambda: sup.ultima_espera is not None))
        finally:
            sup.stop()
        self.assertIsNotNone(sup.ultima_espera)
        assert sup.ultima_espera is not None
        self.assertEqual(sup.ultima_espera.segundos, 1.5)
        self.assertEqual(sup.ultima_espera.origen, "retry-after")

    def test_un_retry_after_imposible_queda_marcado_como_acotado(self) -> None:
        sup = self._sup(
            lambda: ProcFalso(vivo=False),
            lambda proc: StreamError(
                code=StreamErrorCode.HTTP_RATE_LIMIT,
                resumen="límite",
                retry_after=3600.0,
            ),
            RetryPolicy(rng=lambda: 0.0, max_wait=0.5),
        )
        sup.start(ProcFalso(vivo=False))
        try:
            self.assertTrue(self._esperar(lambda: sup.ultima_espera is not None))
        finally:
            sup.stop()
        assert sup.ultima_espera is not None
        self.assertTrue(sup.ultima_espera.acotado)

    def test_detach_no_dispara_reintentos_ni_con_politica(self) -> None:
        """FR-REC-006: la cancelación detiene el ciclo de recuperación."""
        llamadas: list[int] = []

        def relanzar() -> object:
            llamadas.append(1)
            return ProcFalso(vivo=False)

        proceso = ProcFalso(vivo=True)
        sup = self._sup(relanzar, lambda proc: None, RetryPolicy(rng=lambda: 0.0))
        sup.start(proceso)
        # El usuario pulsa `q` mientras el canal todavía se ve; después el
        # proceso muere. Sin `detach()`, el bucle lo interpretaría como corte.
        sup.detach()
        proceso.vivo = False
        try:
            time.sleep(0.2)
        finally:
            sup.stop()
        self.assertEqual(llamadas, [])
        self.assertTrue(sup.session is None)

    def test_el_informe_no_inventa_un_error(self) -> None:
        sup = ReconnectSupervisor(poll_seconds=0.01)
        informe = sup.para_informe()
        self.assertIsNone(informe["ultimo_error"])
        self.assertIsNone(informe["ultima_espera"])
        self.assertEqual(informe["evidencia"], "process_alive")


if __name__ == "__main__":
    unittest.main()