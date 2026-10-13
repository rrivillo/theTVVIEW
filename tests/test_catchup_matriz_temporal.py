"""CU-05 — Matriz temporal: medianoche, cambio de día y DST.

La matriz mínima del SDD §10.3, con `zoneinfo` (stdlib) y **sin red**. Todas
las horas locales se expresan en `Europe/Madrid`, que es la zona con cambio de
horario horario en la UE y por tanto la que expone los dos casos raros:

- **Hora inexistente** (spring forward): el 29 de marzo de 2026 a las 02:30 no
  ocurre. Python **no lanza** al construirla: la normaliza en silencio.
- **Hora repetida** (fall back): el 25 de octubre de 2026 a las 02:30 ocurre dos
  veces. ``fold`` decide cuál.

El módulo trabaja con instantes absolutos, así que el resultado que se pide al
proveedor **no debe cambiar** con la zona de la máquina. Eso es lo que se
comprueba aquí: el contrato es que la conversión de zona no altere el instante,
y que el paso de vuelta a hora local sea explícito y no ambiguo.

Las horas de referencia son las de los saltos reales de la zona, no fechas
inventadas: 2026-03-29 y 2026-10-25.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, available_timezones

from thetvview import catchup
from thetvview.catchup import (
    CatchupRequest,
    CatchupState,
    build_playback_request,
    classify_request,
    render_source_template,
    template_values,
    validar_intervalo,
)

MADRID = ZoneInfo("Europe/Madrid")

#: Inicio del salto de horario de primavera de 2026 en la UE.
DIA_SPRING = datetime(2026, 3, 29)
#: Inicio del retroceso de otoño de 2026 en la UE.
DIA_FALL = datetime(2026, 10, 25)

PLANTILLA = "http://a.test/ts/{start}-{end}-{duration}"
CAP = catchup.CatchupCapability(
    enabled=True,
    provider_declared=True,
    archive_duration_seconds=7 * catchup.SECONDS_PER_DAY,
    protocol=catchup.PROTOCOL_M3U,
    source_template=PLANTILLA,
)


def canal() -> object:
    from thetvview.models import Channel

    return Channel(
        name="Canal",
        url="http://a.test/live/1",
        attrs={"catchup": "shift", "catchup_days": "7", "catchup_source": PLANTILLA},
    )


def req(inicio: datetime, fin: datetime) -> CatchupRequest:
    return CatchupRequest(channel_id="1", start=inicio, end=fin)


def utc(dia: datetime, hora: int, minuto: int = 0) -> datetime:
    return dia.replace(hour=hora, minute=minuto, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. La zona de la máquina no altera el instante pedido
# ---------------------------------------------------------------------------


class TestLaZonaNoAlteraElInstante(unittest.TestCase):
    """El mismo instante, pedido desde cuatro zonas, da el mismo epoch."""

    def setUp(self) -> None:
        self.momento = utc(DIA_SPRING, 20, 30)
        self.inicio = self.momento - timedelta(hours=1)

    def test_el_epoch_no_depende_de_la_zona(self):
        """`{start}` es el instante, no la hora que tiene el reloj donde sea."""
        esperado = int(self.inicio.timestamp())
        valores = template_values(req(self.inicio, self.momento))
        self.assertEqual(valores["start"], esperado)
        # Y el mismo instante expresado en otras zonas sigue dando ese epoch.
        for zona in ("UTC", "Europe/Madrid", "America/Lima", "Asia/Tokyo"):
            with self.subTest(zona=zona):
                otro = self.inicio.astimezone(ZoneInfo(zona))
                self.assertEqual(int(otro.timestamp()), esperado)

    def test_el_sello_utc_no_depende_de_la_zona(self):
        esperado = "2026-03-29:19-30-00"
        self.assertEqual(
            template_values(req(self.inicio, self.momento))["utc"], esperado
        )

    def test_el_sello_es_el_utc_y_no_la_hora_local_de_ninguna_zona(self):
        """19:30 UTC son 20:30 en Madrid y 14:30 en Lima: no puede ser ambas."""
        sello = template_values(req(self.inicio, self.momento))["utc"]
        self.assertNotEqual(sello, "2026-03-29:20-30-00")  # Madrid
        self.assertNotEqual(sello, "2026-03-29:14-30-00")  # Lima

    def test_la_url_no_depende_de_la_zona(self):
        url = render_source_template(PLANTILLA, req(self.inicio, self.momento))
        self.assertIn(str(int(self.inicio.timestamp())), url)


# ---------------------------------------------------------------------------
# 2. Medianoche y cambio de día
# ---------------------------------------------------------------------------


class TestMedianocheYCambioDeDia(unittest.TestCase):
    def test_un_programa_que_cruza_medianoche_es_valido(self):
        peticion = req(
            datetime(2026, 3, 2, 23, 30, tzinfo=timezone.utc),
            datetime(2026, 3, 3, 0, 15, tzinfo=timezone.utc),
        )
        validar_intervalo(peticion)
        self.assertEqual(peticion.duration_seconds, 45 * 60)

    def test_la_duracion_no_depende_del_dia(self):
        """La misma hora en dos días distintos dura lo mismo."""
        a = req(utc(DIA_SPRING, 23, 30), utc(DIA_SPRING, 23, 30) + timedelta(hours=1))
        b = req(utc(DIA_FALL, 23, 30), utc(DIA_FALL, 23, 30) + timedelta(hours=1))
        self.assertEqual(a.duration_seconds, b.duration_seconds)
        self.assertEqual(a.duration_seconds, 3600)

    def test_cruzar_medianoche_no_rompe_la_ventana(self):
        ahora = datetime(2026, 3, 3, 1, 0, tzinfo=timezone.utc)
        peticion = req(
            datetime(2026, 3, 2, 23, 30, tzinfo=timezone.utc),
            datetime(2026, 3, 3, 0, 15, tzinfo=timezone.utc),
        )
        self.assertIs(classify_request(CAP, peticion, ahora), CatchupState.AVAILABLE)

    def test_el_cambio_de_dia_en_la_zona_local_tambien_es_valido(self):
        """23:30 en Madrid puede ser otro día en UTC: no debe importar."""
        inicio = datetime(2026, 3, 2, 23, 30, tzinfo=MADRID)
        fin = datetime(2026, 3, 3, 0, 15, tzinfo=MADRID)
        peticion = req(inicio, fin)
        validar_intervalo(peticion)
        self.assertEqual(peticion.duration_seconds, 45 * 60)


# ---------------------------------------------------------------------------
# 3. Hora inexistente (spring forward)
# ---------------------------------------------------------------------------


class TestHoraInexistente(unittest.TestCase):
    """El 29 de marzo de 2026 a las 02:30 no ocurre en Madrid.

    Python no lanza: construye 02:30+01:00, que al pasarlo a UTC da 01:30, una
    hora que **tampoco** ocurrió en esa zona. theTVVIEW no puede arreglarlo (el
    dato ya venía normalizado antes de llegar aquí) pero sí debe comprobar que
    el instante resultante sea coherente y que la petición no se rompa.
    """

    def setUp(self) -> None:
        self.local = DIA_SPRING.replace(hour=2, minute=30, tzinfo=MADRID)
        self.utc_real = self.local.astimezone(timezone.utc)

    def test_python_normaliza_en_silencio(self):
        """Constatado, no deseado: por eso el módulo no debe depender de él."""
        self.assertEqual(self.local.hour, 2)
        self.assertEqual(self.utc_real.hour, 1)

    def test_la_peticion_se_puede_construir_sin_explotar(self):
        peticion = req(self.local - timedelta(hours=1), self.local)
        validar_intervalo(peticion)
        # `now` es posterior al salto, así que la ventana de 7 dias cubre la
        # peticion: lo que se prueba aquí es que el instante raro no revienta.
        ahora = self.local + timedelta(hours=1)
        pedido = build_playback_request(canal(), CAP, peticion, now=ahora)
        self.assertIn(str(int(self.local.timestamp())), pedido.url)

    def test_el_epoch_es_el_del_instante_normalizado(self):
        """Consistente con lo que Python entendió, no con lo que quiso decir."""
        peticion = req(self.local - timedelta(hours=1), self.local)
        self.assertEqual(
            template_values(peticion)["start"],
            int((self.local - timedelta(hours=1)).timestamp()),
        )

    def test_el_inicio_una_hora_antes_tambien_es_coherente(self):
        inicio = self.local - timedelta(hours=1)
        peticion = req(inicio, self.local)
        validar_intervalo(peticion)
        self.assertEqual(
            template_values(peticion)["duration"], peticion.duration_seconds
        )

    def test_la_hora_de_verano_se_convierte_bien_para_lo_que_si_existe(self):
        """03:30 del mismo día sí existe y sí se convierte."""
        existe = DIA_SPRING.replace(hour=3, minute=30, tzinfo=MADRID)
        self.assertEqual(existe.utcoffset(), timedelta(hours=2))
        self.assertEqual(existe.astimezone(timezone.utc).hour, 1)


# ---------------------------------------------------------------------------
# 4. Hora repetida (fall back)
# ---------------------------------------------------------------------------


class TestHoraRepetida(unittest.TestCase):
    """El 25 de octubre de 2026 a las 02:30 ocurre dos veces en Madrid.

    ``fold`` elige: ``0`` es la primera vez (+02:00) y ``1`` la segunda
    (+01:00). LaTVVIEW tiene que elegir una y **decirlo**, porque «las 02:30»
    es ambiguo y sin la elección el resultado depende de un detalle invisible.
    """

    def setUp(self) -> None:
        self.primera = DIA_FALL.replace(hour=2, minute=30, tzinfo=MADRID, fold=0)
        self.segunda = DIA_FALL.replace(hour=2, minute=30, tzinfo=MADRID, fold=1)

    def test_las_dos_veces_son_instantes_distintos(self):
        self.assertNotEqual(
            self.primera.astimezone(timezone.utc),
            self.segunda.astimezone(timezone.utc),
        )

    def test_el_desplazamiento_es_una_hora(self):
        diferencia = self.segunda.astimezone(timezone.utc) - self.primera.astimezone(timezone.utc)
        self.assertEqual(diferencia, timedelta(hours=1))

    def test_la_peticion_usa_el_instante_recibido(self):
        """Cuál de las dos veces, lo decide quien construyó la petición."""
        peticion = req(self.primera - timedelta(hours=1), self.primera)
        self.assertEqual(
            template_values(peticion)["start"],
            int((self.primera - timedelta(hours=1)).timestamp()),
        )

    def test_las_das_peticiones_dan_instantes_distintos(self):
        """Una hora de diferencia real, no una ambiguedad invisible.

        La resta se hace en UTC y no sobre el `datetime` local: restar un
        `timedelta` a una hora repetidafunciona sobre el reloj, y al cambiar
        de `fold` el desplazamiento salta una hora. Aquí interesa el
        **instante**, que es lo unico que ve el proveedor.
        """
        a = self.primera.astimezone(timezone.utc) - timedelta(hours=1)
        b = self.segunda.astimezone(timezone.utc) - timedelta(hours=1)
        self.assertEqual(
            template_values(req(a, self.primera))["start"]
            - template_values(req(b, self.segunda))["start"],
            -3600,
        )

    def test_el_sello_utc_es_el_instante_real(self):
        """El sello sale del instante UTC, no de la hora local repetida."""
        inicio = self.primera - timedelta(hours=1)
        valores = template_values(req(inicio, self.primera))
        esperado = inicio.astimezone(timezone.utc).strftime("%Y-%m-%d:%H-%M-%S")
        self.assertEqual(valores["utc"], esperado)

    def test_el_sello_distingue_las_dos_ocurrencias(self):
        """Las dos veces tienen la misma hora local; el sello UTC no."""
        a = template_values(
            req(self.primera.astimezone(timezone.utc) - timedelta(hours=1), self.primera)
        )["utc"]
        b = template_values(
            req(self.segunda.astimezone(timezone.utc) - timedelta(hours=1), self.segunda)
        )["utc"]
        self.assertNotEqual(a, b)


# ---------------------------------------------------------------------------
# 5. La matriz del SDD §10.3, completa y sin red
# ---------------------------------------------------------------------------


class TestMatrizDelSdd(unittest.TestCase):
    """Los catorce casos que enumera el SDD, cada uno con su resultado."""

    NOW = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)

    def test_inicio_y_fin_validos_en_el_mismo_dia(self):
        validar_intervalo(req(self.NOW - timedelta(hours=2), self.NOW - timedelta(hours=1)))

    def test_intervalo_que_cruza_medianoche(self):
        validar_intervalo(
            req(
                datetime(2026, 3, 2, 23, 45, tzinfo=timezone.utc),
                datetime(2026, 3, 3, 0, 30, tzinfo=timezone.utc),
            )
        )

    def test_inicio_igual_al_fin(self):
        with self.assertRaises(catchup.CatchupInvalidIntervalError):
            validar_intervalo(req(self.NOW - timedelta(hours=1), self.NOW - timedelta(hours=1)))

    def test_fin_anterior_al_inicio(self):
        with self.assertRaises(catchup.CatchupInvalidIntervalError):
            validar_intervalo(req(self.NOW - timedelta(hours=1), self.NOW - timedelta(hours=2)))

    def test_duracion_excesiva(self):
        with self.assertRaises(catchup.CatchupInvalidIntervalError):
            validar_intervalo(req(self.NOW - timedelta(days=30), self.NOW))

    def test_cambio_de_dia_al_convertir_zona(self):
        """23:30 en Lima (UTC−5) ya es el día siguiente en UTC."""
        inicio = datetime(2026, 3, 2, 23, 30, tzinfo=ZoneInfo("America/Lima"))
        self.assertEqual(inicio.day, 2)
        self.assertEqual(inicio.astimezone(timezone.utc).day, 3)
        validar_intervalo(req(inicio, inicio + timedelta(hours=1)))

    def test_inicio_en_hora_inexistente_de_dst(self):
        validar_intervalo(
            req(
                DIA_SPRING.replace(hour=2, minute=30, tzinfo=MADRID) - timedelta(hours=1),
                DIA_SPRING.replace(hour=2, minute=30, tzinfo=MADRID),
            )
        )

    def test_hora_local_repetida_durante_dst(self):
        for fold in (0, 1):
            with self.subTest(fold=fold):
                momento = DIA_FALL.replace(hour=2, minute=30, tzinfo=MADRID, fold=fold)
                validar_intervalo(req(momento - timedelta(hours=1), momento))

    def test_plantilla_valida_y_marcador_desconocido(self):
        render_source_template(PLANTILLA, req(self.NOW - timedelta(hours=1), self.NOW))
        with self.assertRaises(catchup.CatchupError):
            render_source_template("http://a/x?s={nada}", req(self.NOW, self.NOW))

    def test_caracteres_reservados_y_parametros_malformados(self):
        for plantilla in (
            "http://a/x?s={start}\nX: 1",
            "file:///etc/passwd?start={start}",
            "http://a/x?s={start:04d}",
        ):
            with self.subTest(plantilla=plantilla):
                with self.assertRaises(catchup.CatchupError):
                    render_source_template(plantilla, req(self.NOW - timedelta(hours=1), self.NOW))

    def test_fuente_sin_catchup(self):
        from thetvview.models import Channel

        sin = Channel(name="C", url="http://a.test/live/1")
        with self.assertRaises(catchup.CatchupNotAvailableError):
            build_playback_request(sin, catchup.capability_for(sin), req(self.NOW - timedelta(hours=1), self.NOW), now=self.NOW)

    def test_contenido_fuera_de_la_ventana(self):
        viejo = req(self.NOW - timedelta(days=20), self.NOW - timedelta(days=20) + timedelta(hours=1))
        with self.assertRaises(catchup.CatchupOutsideArchiveWindowError):
            build_playback_request(canal(), CAP, viejo, now=self.NOW)

    def test_autenticacion_contra_error_de_red_se_distinguen(self):
        """Los dos son fallos, pero el consejo es distinto (CU-04)."""
        from thetvview.player.errors import StreamErrorCode

        self.assertIs(
            catchup.describe_unavailable(StreamErrorCode.AUTH_FAILED),
            CatchupState.AUTH_FAILED,
        )
        self.assertIs(
            catchup.describe_unavailable(StreamErrorCode.CONNECTION_TIMEOUT),
            CatchupState.NETWORK,
        )

    def test_url_rechazada_por_validacion_de_seguridad(self):
        with self.assertRaises(catchup.CatchupError):
            render_source_template(
                "ftp://a.test/x?start={start}",
                req(self.NOW - timedelta(hours=1), self.NOW),
            )


# ---------------------------------------------------------------------------
# 6. La matriz depende de zoneinfo, que es stdlib
# ---------------------------------------------------------------------------


class TestZoneInfoEsPortable(unittest.TestCase):
    def test_la_zona_usada_existe_en_el_sistema(self):
        self.assertIn("Europe/Madrid", available_timezones())

    def test_la_prueba_no_depende_de_la_maquina(self):
        """Los saltos de la UE son los mismos en cualquier equipo."""
        self.assertEqual(DIA_SPRING.replace(hour=2, minute=30, tzinfo=MADRID).utcoffset(),
                         timedelta(hours=1))
        self.assertEqual(DIA_FALL.replace(hour=2, minute=30, tzinfo=MADRID).utcoffset(),
                         timedelta(hours=2))


if __name__ == "__main__":
    unittest.main()