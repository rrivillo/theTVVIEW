"""Pruebas del puente de errores de dominio a modales (Fase 8 · P2.6 · U-02).

Los backends de las Fases 3 a 7 rechazan con su propio vocabulario y no pueden
mostrar nada. Este módulo traduce esas excepciones a `UiError`, y lo que se
comprueba aquí son las dos reglas que lo gobiernan:

1. **el backend dice qué pasó y la UI no lo reescribe** — su mensaje se conserva;
2. **nada se muestra sin redactar** — una URL con token en el texto del error
   no puede llegar al modal.
"""

from __future__ import annotations

import unittest

from thetvview.collections.manager import ColeccionesManagerError
from thetvview.collections.rules import ReglaInvalida
from thetvview.recording.backend import RecordingError
from thetvview.scheduler.backend import SchedulerError
from thetvview.storage.errors import StorageError
from thetvview.ui.domain_errors import describe_domain_error, is_domain_error
from thetvview.ui.errormsg import ErrorKind, UiError
from thetvview.ui.screens import _error_dominio


class TestSeReconoceElErrorDeDominio(unittest.TestCase):
    def test_reconoce_los_cuatro_backends(self) -> None:
        for exc in (
            ReglaInvalida("campo no permitido: quality"),
            ColeccionesManagerError("nombre vacío"),
            SchedulerError("ya existe otra tarea con el id t-1"),
            RecordingError("espacio libre insuficiente"),
        ):
            with self.subTest(clase=type(exc).__name__):
                self.assertTrue(is_domain_error(exc))

    def test_no_confunde_un_error_de_red(self) -> None:
        self.assertFalse(is_domain_error(TimeoutError("se agotó el tiempo")))
        self.assertFalse(is_domain_error(ValueError("lo que sea")))


class TestElMensajeDelBackendSeConserva(unittest.TestCase):
    """La UI no reescribe lo que el backend ya escribió en español."""

    def test_conserva_el_mensaje_literal(self) -> None:
        err = describe_domain_error(ReglaInvalida("campo no permitido: quality"))
        self.assertEqual(err.message, "campo no permitido: quality")

    def test_el_titulo_dice_en_que_flujo_fallo(self) -> None:
        casos = (
            (ReglaInvalida("x"), "La regla no es válida"),
            (ColeccionesManagerError("x"), "No se pudo trabajar con la colección"),
            (SchedulerError("x"), "No se pudo programar la grabación"),
            (RecordingError("x"), "No se pudo grabar"),
        )
        for exc, titulo in casos:
            with self.subTest(clase=type(exc).__name__):
                self.assertEqual(describe_domain_error(exc).title, titulo)

    def test_el_flujo_no_se_confunde_con_reproducir(self) -> None:
        """Una colección no es una lista y una grabación no es un canal.

        `kind` decide qué acciones existen (§7): ofrecer «reintentar la
        descarga» cuando lo que falló fue una regla sería un atajo que no
        arregla nada.
        """
        self.assertEqual(
            describe_domain_error(ReglaInvalida("x")).kind, ErrorKind.COLECCIONES
        )
        self.assertEqual(
            describe_domain_error(ColeccionesManagerError("x")).kind,
            ErrorKind.COLECCIONES,
        )
        self.assertEqual(
            describe_domain_error(SchedulerError("x")).kind, ErrorKind.GRABACION
        )
        self.assertEqual(
            describe_domain_error(RecordingError("x")).kind, ErrorKind.GRABACION
        )

    def test_error_desconocido_no_se_finge_conocido(self) -> None:
        err = describe_domain_error(TimeoutError("la red tardó demasiado"))
        self.assertEqual(err.title, "Algo no salió como se esperaba")
        self.assertEqual(err.message, "la red tardó demasiado")

    def test_excepcion_sin_mensaje_no_deja_el_modal_vacio(self) -> None:
        err = describe_domain_error(RecordingError())
        self.assertTrue(err.message.strip())
        self.assertNotEqual(err.message.strip(), "")


class TestNadaSeMuestraSinRedactar(unittest.TestCase):
    """Una URL con token en el texto del error no puede llegar al modal."""

    def test_redacta_el_token_de_la_url(self) -> None:
        """U-02 garantiza el **paso** por el redactor, no el alcance del regex.

        El regex de `security/redaction.py` es compartido y ya cubre las
        rutas Xtream, el userinfo y la query. Ensancharlo es otro cambio de
        alcance; lo que esta fase sí puede prometer es que nada de lo que dice
        un backend llega al modal sin pasar por él.
        """
        exc = RecordingError(
            "no se pudo grabar http://proveedor.local/live/alicia/secreto/45678.m3u8"
        )
        err = describe_domain_error(exc)
        self.assertNotIn("secreto", err.cuerpo())
        self.assertIn("***", err.cuerpo())

    def test_redacta_credenciales_en_la_consulta(self) -> None:
        exc = SchedulerError(
            "falló al iniciar: http://x.local/player_api.php?user=alicia&password=secreta"
        )
        cuerpo = describe_domain_error(exc).cuerpo()
        self.assertNotIn("secreta", cuerpo)

    def test_redacta_tambien_en_error_desconocido(self) -> None:
        exc = TimeoutError("rtsp://alicia:clave@vlc.local/live")
        self.assertNotIn("clave", describe_domain_error(exc).cuerpo())

    def test_el_detalle_tecnico_no_se_inventa(self) -> None:
        """Sin `detail` no hay nada que rellenar: el backend no lo da."""
        self.assertIsNone(describe_domain_error(SchedulerError("x")).detail)


class TestDevuelveAlgoMostrable(unittest.TestCase):
    """El contrato con `App.show_error` es un `UiError` real."""

    def test_devuelve_un_uierror(self) -> None:
        self.assertIsInstance(describe_domain_error(SchedulerError("x")), UiError)

    def test_cuerpo_une_titulo_y_mensaje(self) -> None:
        err = describe_domain_error(SchedulerError("no hay grabador configurado"))
        cuerpo = err.cuerpo()
        self.assertIn("No se pudo programar la grabación", cuerpo)
        self.assertIn("no hay grabador configurado", cuerpo)


class TestElHelperDePantallasDegrada(unittest.TestCase):
    """`_error_dominio` es el camino único; en un doble sin UI no se rompe."""

    def test_app_real_recibe_un_uierror(self) -> None:
        """Con `show_error` el backend nunca se muestra crudo."""
        vistos = []

        class _ConShowError:
            def show_error(self, err, *, retry=None, diagnose=None):
                vistos.append(err)

        _error_dominio(_ConShowError(), SchedulerError("no hay grabador configurado"))
        self.assertEqual(len(vistos), 1)
        self.assertIsInstance(vistos[0], UiError)
        self.assertEqual(vistos[0].title, "No se pudo programar la grabación")

    def test_doble_minimo_cae_en_la_barra(self) -> None:
        """Patrón de `tests/test_epg_ui.py`: una app sin `show_error` no revienta."""

        class _Barra:
            def __init__(self) -> None:
                self.mensajes: list[str] = []

            def show(self, message: str, error: bool = False) -> None:
                self.mensajes.append(message)

        class _SinUI:
            def __init__(self) -> None:
                self.status = _Barra()

        app = _SinUI()
        _error_dominio(app, RecordingError("espacio libre insuficiente"))
        self.assertEqual(len(app.status.mensajes), 1)
        self.assertIn("espacio libre insuficiente", app.status.mensajes[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()