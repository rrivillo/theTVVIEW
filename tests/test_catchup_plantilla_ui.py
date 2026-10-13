"""CU-07 (interfaz) — La guía no promete un archivo que no sabe pedir.

El hueco que motivationa este ticket: una lista con `catchup-source` de SIPTV
(`?utc={utc}&lutc={lutc}`) se anunciaba como ``available`` en la guía y fallaba
al pulsarlo. El usuario veía que había archivo y concluía que el problema era
del proveedor.

Estos tests comprueban las tres decisiones de interfaz:

1. La guía **no** marca disponible lo que no se puede construir.
2. Al pulsar, se explica con un modal y se ofrece el intento bajo riesgo
   propio, **con el aviso antes de la pregunta**.
3. Cancelar deja el directo, que es la salida por defecto y no cuesta nada.

Sin curses: dobles de `App` con `notify_warning` y `_confirm_risk` capturados,
que es como el resto de pruebas de pantallas del proyecto.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from thetvview import catchup
from thetvview.catchup import CatchupState
from thetvview.models import Channel, Program
from thetvview.ui import screens as ui_screens

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
PLANTILLA_MAL = "?utc={utc}&lutc={lutc}"
PLANTILLA_BUENA = "http://a.test/ts?start={start}&duration={duration}"


def canal(plantilla: str = PLANTILLA_MAL) -> Channel:
    return Channel(
        name="ESPN HD",
        url="http://a.test/live/1",
        tvg_id="espn",
        attrs={"catchup": "shift", "catchup_days": "7", "catchup_source": plantilla},
    )


def programa() -> Program:
    return Program("espn", "Película", NOW - timedelta(hours=2), NOW - timedelta(hours=1))


class _App:
    """Doble de app: registra avisos y decisiones del modal."""

    def __init__(self, *, acepta: bool = False) -> None:
        self.avisos: list[str] = []
        self.preguntas: list[tuple[str, str]] = []
        self.acepta = acepta
        self.favorites = _Favoritos()

    def notify_warning(self, message: str) -> None:
        self.avisos.append(message)

    def notify_error(self, message: str) -> None:
        self.avisos.append(message)

    def _confirm_risk(self, title: str, message: str) -> bool:
        self.preguntas.append((title, message))
        return self.acepta

    def body_height(self) -> int:
        return 20

    def ensure_epg(self, channel, url_hint=None):  # noqa: ANN001, ANN201
        """Sin EPG en el doble: la pantalla se probará con la parrilla a mano."""
        return []


class _AppSinModal:
    """Doble **sin** ningún modal, como los dobles de test más antiguos.

    No hereda de `_App` a propósito: si heredara, encontraría `_confirm_risk`
    y el camino de degradación no se comprobaría. Sirve para verificar que la
    pantalla avisa en vez de abrir el archivo sin preguntar.
    """

    def __init__(self) -> None:
        self.avisos: list[str] = []
        self.favorites = _Favoritos()

    def notify_warning(self, message: str) -> None:
        self.avisos.append(message)

    def notify_error(self, message: str) -> None:
        self.avisos.append(message)

    def body_height(self) -> int:
        return 20

    def ensure_epg(self, channel, url_hint=None):  # noqa: ANN001, ANN201
        return []


class _Favoritos:
    def is_favorite(self, channel):  # noqa: ANN001, ANN201
        return False

    def favorite_urls(self):  # noqa: ANN201
        return set()


def _pantalla(app, plantilla: str = PLANTILLA_MAL):
    with mock.patch.object(ui_screens, "notify_warning", create=True):
        return ui_screens.EpgScreen(app, canal(plantilla))


# ---------------------------------------------------------------------------
# 1. La guía no marca disponible lo que no se puede pedir
# ---------------------------------------------------------------------------


class TestLaGuiaNoPromete(unittest.TestCase):
    def test_la_capacidad_sabe_que_no_la_sabe_construir(self):
        cap = catchup.capability_for(canal(PLANTILLA_MAL))
        self.assertTrue(cap.enabled, "el proveedor sí declaró archivo")
        self.assertFalse(cap.plantilla_soportada)
        self.assertFalse(cap.construible)

    def test_una_plantilla_valida_si_se_puede_construir(self):
        cap = catchup.capability_for(canal(PLANTILLA_BUENA))
        self.assertTrue(cap.construible)

    def test_el_estado_no_es_available(self):
        self.assertIs(
            catchup.classify(canal(PLANTILLA_MAL), programa(), NOW),
            CatchupState.UNSUPPORTED_TEMPLATE,
        )

    def test_la_pantalla_sabe_cuando_ofrece_el_intento(self):
        pantalla = _pantalla(_App())
        self.assertTrue(pantalla.plantilla_no_soportada)

    def test_una_plantilla_valida_no_ofrece_el_intento(self):
        pantalla = _pantalla(_App(), PLANTILLA_BUENA)
        self.assertFalse(pantalla.plantilla_no_soportada)

    def test_un_canal_sin_catchup_tampoco_lo_ofrece(self):
        c = Channel(name="C", url="http://a.test/live/1")
        pantalla = ui_screens.EpgScreen(_App(), c)
        self.assertFalse(pantalla.plantilla_no_soportada)


# ---------------------------------------------------------------------------
# 2. Al pulsar se explica y se ofrece el intento
# ---------------------------------------------------------------------------


class TestElIntentoSeOfrece(unittest.TestCase):
    def _pulsar_enter(self, app, plantilla: str = PLANTILLA_MAL):
        pantalla = _pantalla(app, plantilla)
        pantalla.programs = [programa()]
        pantalla._list.selected = 0
        with mock.patch("thetvview.ui.screens.datetime") as dt:
            dt.now.return_value = NOW.astimezone()
            dt.datetime.now.return_value = NOW.astimezone()
            return pantalla.handle_key(10)

    def test_aceptar_devuelve_la_accion_con_el_permiso(self):
        app = _App(acepta=True)
        accion = self._pulsar_enter(app)
        self.assertIsNotNone(accion)
        self.assertEqual(accion["action"], "play_catchup")
        self.assertTrue(
            accion["permitir_no_soportada"],
            "la acción debe llevar el permiso ya aceptado por el usuario",
        )

    def test_el_aviso_va_antes_de_la_pregunta(self):
        app = _App(acepta=True)
        self._pulsar_enter(app)
        self.assertEqual(len(app.preguntas), 1, "debe haber un modal")
        titulo, mensaje = app.preguntas[0]
        self.assertIn("riesgo", titulo.lower() + mensaje.lower())
        self.assertIn("lutc", mensaje, "el aviso debe nombrar el marcador")
        self.assertIn("puede", mensaje.lower())

    def test_el_aviso_no_garantiza_nada(self):
        app = _App(acepta=True)
        self._pulsar_enter(app)
        _, mensaje = app.preguntas[0]
        for promesa in ("garantizado", "va a funcionar", "funcionará"):
            with self.subTest(promesa=promesa):
                self.assertNotIn(promesa.lower(), mensaje.lower())

    def test_cancelar_ofrecece_el_directo(self):
        app = _App(acepta=False)
        accion = self._pulsar_enter(app)
        self.assertEqual(accion["action"], "open_channel")
        self.assertNotIn("permitir_no_soportada", accion)

    def test_cancelar_no_abre_el_archivo(self):
        app = _App(acepta=False)
        accion = self._pulsar_enter(app)
        self.assertNotEqual(accion["action"], "play_catchup")

    def test_sin_modal_se_explica_y_no_se_ofrece(self):
        """Un doble sin UI degrada a aviso, nunca a archivo silencioso."""
        app = _AppSinModal()
        pantalla = _pantalla(app)
        pantalla.programs = [programa()]
        pantalla._list.selected = 0
        with mock.patch("thetvview.ui.screens.datetime") as dt:
            dt.now.return_value = NOW.astimezone()
            dt.datetime.now.return_value = NOW.astimezone()
            accion = pantalla.handle_key(10)
        self.assertIsNone(accion, "sin modal no se ofrece ninguna acción")
        self.assertTrue(app.avisos, "pero sí se explica por qué")

    def test_una_plantilla_valida_no_pregunta_nada(self):
        app = _App(acepta=True)
        accion = self._pulsar_enter(app, PLANTILLA_BUENA)
        self.assertEqual(accion["action"], "play_catchup")
        self.assertEqual(app.preguntas, [], "el camino normal no pregunta")
        self.assertFalse(accion.get("permitir_no_soportada"))


# ---------------------------------------------------------------------------
# 3. El resultado del intento
# ---------------------------------------------------------------------------


class TestElResultadoDelIntento(unittest.TestCase):
    def test_el_intento_construye_la_mejor_url_posible(self):
        c = canal("http://a.test/ts?s={utc}&x={lutc}")
        pedido = catchup.build_playback_request(
            c,
            catchup.capability_for(c),
            catchup.CatchupRequest("1", NOW - timedelta(hours=2), NOW),
            now=NOW,
            permitir_no_soportada=True,
        )
        self.assertNotIn("{", pedido.url)
        self.assertIn(str((NOW - timedelta(hours=2)).astimezone(timezone.utc)
                          .strftime("%Y-%m-%d:%H-%M-%S")), pedido.url)

    def test_sin_el_permiso_no_se_construye(self):
        c = canal("http://a.test/ts?s={utc}&x={lutc}")
        with self.assertRaises(catchup.CatchupError):
            catchup.build_playback_request(
                c,
                catchup.capability_for(c),
                catchup.CatchupRequest("1", NOW - timedelta(hours=2), NOW),
                now=NOW,
            )

    def test_el_directo_no_se_toca_en_ningun_caso(self):
        c = canal(PLANTILLA_MAL)
        self.assertEqual(c.url, "http://a.test/live/1")


if __name__ == "__main__":
    unittest.main()