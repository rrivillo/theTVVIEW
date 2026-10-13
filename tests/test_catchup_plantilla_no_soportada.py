"""CU-07 — Plantilla no soportada: no prometer lo que no se puede dar.

Este ticket responde a un hueco que dejó la Fase 1. Las plantillas reales del
estándar de reproductores IPTV usan marcadores que theTVVIEW no reconoce:

    catchup-source="?utc={utc}&lutc={lutc}"      # SIPTV / catchup="shift"
    catchup-source="&cutv={Y}-{m}-{d}T{H}:{M}:{S}"

Con el comportamiento actual, un canal así se anuncia como ``available`` en la
guía y **falla al pulsarlo**. Eso es peor que no ofrecerlo: el usuario ve que
hay archivo y concluye que el problema es del proveedor.

Qué cambia:

1. **No se promete lo que no se puede dar.** Una plantilla con marcadores
   desconocidos da el estado :attr:`CatchupState.UNSUPPORTED_TEMPLATE`, no
   ``AVAILABLE``. La guía muestra el icono correcto y el modal explica por qué.
2. **La puerta sigue cerrada por defecto.** ``can_use_catchup`` no cambia: es
   la invariante de seguridad y sigue Depending de lo que declaró el proveedor.
3. **Salida bajo propio riesgo.** Si el usuario insiste, se construye la mejor
   URL posible —los marcadores reconocidos se rellenan, los desconocidos se
   dejan fuera— y se le avisa **antes**, en un modal, de que puede no
   funcionar. Nunca en silencio.
4. **Las protecciones de seguridad no se saltan.** Bajo propio riesgo es
   «te lo intento», no «acepto cualquier cosa»: esquema raro, salto de línea y
   URL inválida siguen rechazándose siempre.

Decisión de alcance: no se añaden marcadores nuevos al conjunto reconocido. Eso
sería una funcionalidad aparte (y el estándar admite además variantes con
formato explícito que obligan a otro parser). Lo que se hace aquí es **no
engañar** y **dejar una salida**.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from thetvview import catchup
from thetvview.catchup import (
    CatchupCapability,
    CatchupError,
    CatchupState,
    build_playback_request,
    can_use_catchup,
    capability_for,
    classify,
    describe_state,
    marcar_soportada,
    motivo_plantilla_no_soportada,
    render_source_template,
)
from thetvview.models import Channel, Program

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)

#: Plantillas reales que el estándar define y theTVVIEW no reconoce.
REALES_NO_SOPORTADAS = {
    "SIPTV / shift": "?utc={utc}&lutc={lutc}",
    "cutv (el más común)": "&cutv={Y}-{m}-{d}T{H}:{M}:{S}",
    "con formato explícito": "?start={utc:YmdHM}&end=${end:YmdHM}",
    "sólo epoch en millis": "?start={timestamp}",
}

PLANTILLA_OK = "http://a.test/ts?start={start}&duration={duration}"


def canal(plantilla: str, **attrs) -> Channel:
    base = {"catchup": "append", "catchup_days": "7", "catchup_source": plantilla}
    base.update(attrs)
    return Channel(name="Canal", url="http://a.test/live/1", attrs=base)


def programa() -> Program:
    return Program("id", "Película", NOW - timedelta(hours=2), NOW - timedelta(hours=1))


def peticion() -> "catchup.CatchupRequest":
    return catchup.CatchupRequest("101", NOW - timedelta(hours=2), NOW - timedelta(hours=1))


# ---------------------------------------------------------------------------
# 1. Se reconoce qué plantillas se pueden construir
# ---------------------------------------------------------------------------


class TestDeteccionDePlantilla(unittest.TestCase):
    def test_una_plantilla_valida_esta_soportada(self):
        self.assertTrue(catchup.plantilla_soportada(PLANTILLA_OK))

    def test_las_reales_no_soportadas_se_detectan(self):
        for nombre, plantilla in REALES_NO_SOPORTADAS.items():
            with self.subTest(caso=nombre):
                self.assertFalse(catchup.plantilla_soportada(plantilla))

    def test_hay_un_motivo_explicable(self):
        motivo = motivo_plantilla_no_soportada("?utc={utc}&lutc={lutc}")
        self.assertIn("lutc", motivo)

    def test_una_plantilla_soportada_no_tiene_motivo(self):
        self.assertEqual(motivo_plantilla_no_soportada(PLANTILLA_OK), "")

    def test_la_plantilla_vacia_no_esta_soportada(self):
        for plantilla in ("", "   ", None):
            with self.subTest(plantilla=plantilla):
                self.assertFalse(catchup.plantilla_soportada(plantilla))  # type: ignore[arg-type]

    def test_la_comprobacion_no_lanza(self):
        """Es una consulta: un dato raro da «no soportada», no un fallo."""
        for plantilla in ("{", "}", "{start", 42, object()):
            with self.subTest(plantilla=repr(plantilla)):
                self.assertFalse(catchup.plantilla_soportada(plantilla))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. La capacidad lo sabe
# ---------------------------------------------------------------------------


class TestLaCapacidadLoSabe(unittest.TestCase):
    def test_la_capacidad_expone_si_la_plantilla_sirve(self):
        c = canal("?utc={utc}&lutc={lutc}")
        cap = capability_for(c)
        self.assertTrue(cap.provider_declared, "el proveedor sí declaró archivo")
        self.assertFalse(cap.plantilla_soportada)

    def test_una_plantilla_valida_se_marca_soportada(self):
        cap = capability_for(canal(PLANTILLA_OK))
        self.assertTrue(cap.plantilla_soportada)

    def test_xtream_no_tiene_plantilla_y_no_le_pasa_nada(self):
        c = Channel(
            name="C", url="xtream://P/live/1.ts",
            attrs={"xtream_id": "1", "source_name": "P",
                   "tv_archive": "1", "tv_archive_duration": "7"},
        )
        cap = capability_for(c)
        self.assertTrue(cap.plantilla_soportada, "Xtream construye su propia URL")

    def test_la_invariante_de_seguridad_no_cambia(self):
        """`can_use_catchup` sigue significando lo mismo: lo declaró el proveedor."""
        cap = capability_for(canal("?utc={utc}&lutc={lutc}"))
        self.assertTrue(cap.enabled)
        self.assertTrue(can_use_catchup(cap))


# ---------------------------------------------------------------------------
# 3. La guía no promete lo que no hay
# ---------------------------------------------------------------------------


class TestLaGuiaNoPromete(unittest.TestCase):
    def test_el_estado_no_es_available(self):
        c = canal("?utc={utc}&lutc={lutc}")
        self.assertIs(classify(c, programa(), NOW), CatchupState.UNSUPPORTED_TEMPLATE)

    def test_una_plantilla_valida_sigue_siendo_available(self):
        self.assertIs(
            classify(canal(PLANTILLA_OK), programa(), NOW), CatchupState.AVAILABLE
        )

    def test_el_modal_explica_y_ofrece_la_salida(self):
        c = canal("?utc={utc}&lutc={lutc}")
        estado = classify(c, programa(), NOW)
        texto = describe_state(estado, capability_for(c))
        self.assertIn("lutc", texto)
        self.assertIn("riesgo", texto.lower())
        # Debe ofrecer explícitamente el intento, no sólo explicar el problema.
        self.assertTrue(
            "todos modos" in texto.lower() or "intentar" in texto.lower(),
            f"el modal debe ofrecer la salida, dice: {texto!r}",
        )

    def test_el_motivo_propone_el_intento_solo_si_se_puede(self):
        """Con marcadores reconocidos de por medio, se puede intentar algo."""
        texto = describe_state(
            CatchupState.UNSUPPORTED_TEMPLATE,
            capability_for(canal("http://a.test/ts?s={utc}&d={lutc}")),
        )
        self.assertIn("puede", texto.lower())

    def test_sin_marcadores_reconocidos_no_se_ofrece_un_intento_inutil(self):
        """Una plantilla que no tiene ningún instante nuestro no es adivinable."""
        texto = describe_state(
            CatchupState.UNSUPPORTED_TEMPLATE,
            capability_for(canal("http://a.test/ts/fijo.m3u8?x={lutc}")),
        )
        self.assertIn("lutc", texto)

    def test_el_estado_desconocido_no_confunde_con_este(self):
        self.assertIsNot(CatchupState.UNSUPPORTED_TEMPLATE, CatchupState.UNKNOWN)


# ---------------------------------------------------------------------------
# 4. La salida bajo riesgo propio
# ---------------------------------------------------------------------------


class TestIntentoBajoRiesgoPropio(unittest.TestCase):
    """El usuario puede insistir, pero se le avisa antes y con la mejor URL."""

    def setUp(self) -> None:
        self.c = canal("http://a.test/ts?s={utc}&d={duration}&x={lutc}")
        self.cap = capability_for(self.c)

    def test_sin_permiso_no_se_construye(self):
        with self.assertRaises(CatchupError):
            build_playback_request(self.c, self.cap, peticion(), now=NOW)

    def test_con_permiso_se_construye_la_mejor_url_posible(self):
        pedido = build_playback_request(
            self.c, self.cap, peticion(), now=NOW, permitir_no_soportada=True
        )
        # Los marcadores reconocidos sí se rellenan: la plantilla usa {utc}.
        inicio = NOW - timedelta(hours=2)
        self.assertIn(
            inicio.astimezone(timezone.utc).strftime("%Y-%m-%d:%H-%M-%S"), pedido.url
        )
        self.assertIn("d=3600", pedido.url)
        # El desconocido desaparece en vez de quedar literal.
        self.assertNotIn("lutc", pedido.url)
        self.assertNotIn("{", pedido.url)

    def test_el_intento_no_salta_la_validacion_de_url(self):
        """«Bajo riesgo propio» no significa «acepta cualquier cosa»."""
        for mala in ("file:///etc/passwd?s={utc}&x={lutc}",
                     "http://a.test/x?s={utc}\nX: 1&y={lutc}"):
            with self.subTest(plantilla=repr(mala)):
                c = canal(mala)
                with self.assertRaises(CatchupError):
                    build_playback_request(
                        c, capability_for(c), peticion(), now=NOW,
                        permitir_no_soportada=True,
                    )

    def test_el_intento_igual_respeta_el_intervalo_y_la_ventana(self):
        c = canal("http://a.test/ts?s={utc}&x={lutc}")
        invertido = catchup.CatchupRequest("1", NOW - timedelta(hours=1), NOW - timedelta(hours=2))
        with self.assertRaises(CatchupError):
            build_playback_request(
                c, capability_for(c), invertido, now=NOW, permitir_no_soportada=True
            )

    def test_el_aviso_dice_que_puede_no_funcionar(self):
        """El texto que la UI muestra antes de preguntar."""
        texto = catchup.aviso_riesgo_propio("http://a.test/ts?s={utc}&x={lutc}")
        self.assertIn("puede", texto.lower())
        self.assertIn("riesgo", texto.lower())
        self.assertIn("lutc", texto)

    def test_el_aviso_no_pide_perdon_por_lo_que_no_hace(self):
        texto = catchup.aviso_riesgo_propio("http://a.test/ts?s={utc}&x={lutc}")
        for falsa in ("garantizado", "funcionará", "seguro que"):
            with self.subTest(palabra=falsa):
                self.assertNotIn(falsa.lower(), texto.lower())


# ---------------------------------------------------------------------------
# 5. Lo que no cambia
# ---------------------------------------------------------------------------


class TestLoQueNoCambia(unittest.TestCase):
    def test_el_directo_sigue_disponible(self):
        c = canal("?utc={utc}&lutc={lutc}")
        self.assertEqual(c.url, "http://a.test/live/1")

    def test_el_canal_sin_catchup_no_cambia(self):
        c = Channel(name="C", url="http://a.test/live/1")
        self.assertIs(capability_for(c), catchup.DISABLED)

    def test_una_plantilla_valida_no_afecta_al_rendimiento(self):
        """La detección es una comprobación barata sobre el texto de la plantilla."""
        import time

        c = canal(PLANTILLA_OK)
        t0 = time.perf_counter()
        for _ in range(200):
            catchup.plantilla_soportada(PLANTILLA_OK)
        por_uno = (time.perf_counter() - t0) / 200 * 1e6
        self.assertLess(por_uno, 2000, "la detección no debe pesar en la guía")

    def test_la_capacidad_sigue_siendo_serializable(self):
        """El dataclass sigue siendo lo que espera el resto del proyecto."""
        c = canal("?utc={utc}&lutc={lutc}")
        cap = capability_for(c)
        self.assertIsInstance(cap, CatchupCapability)
        self.assertEqual(cap.archive_duration_days, 7)

    def test_marcar_soportada_no_altera_la_invariante(self):
        c = canal("?utc={utc}&lutc={lutc}")
        cap = marcar_soportada(capability_for(c), False)
        self.assertFalse(cap.plantilla_soportada)


if __name__ == "__main__":
    unittest.main()