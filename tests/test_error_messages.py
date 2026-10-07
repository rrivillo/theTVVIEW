"""Tests de los mensajes de error interactivos (SDD-error-messages, plan F6).

Cuatro cosas se comprueban aquí, y las cuatro son **invariantes**, no ejemplos:

1. **Clasificación** (unit): los quince casos de §15.1 se traducen al mensaje
   de §8, y el detalle técnico sólo aparece cuando existe un status real.
2. **Atajos truthful** (§13): para cada `Action` del pie del modal hay una tecla
   que `handle_key` atiende de verdad. Un atajo decorativo no pasa.
3. **Seguridad** (§15.3): ni contraseñas, ni tokens, ni credenciales embebidas
   en título, mensaje, detalle ni pie.
4. **Terminal estrecha** (§15.4): el modal se calcula dentro de la ventana y
   `R` y `Esc` siguen visibles en el pie.

Los tests de UI usan `App(None)`, el doble ya establecido en
`tests/test_cam_flow.py`: sin terminal, `_run_modal` degrada a la barra, así que
el flujo se puede recorrer entero sin levantar curses.
"""

from __future__ import annotations

import socket
import ssl
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from thetvview.m3u_parser import fetch_bytes
from thetvview.models import Channel
from thetvview.player.errors import PlayerError
from thetvview.security.errors import (
    AccountExpiredError,
    AuthenticationError,
    ConnectionLimitError,
    InvalidSourceError,
    NetworkError,
    ParseError,
    RateLimitError,
    ResponseTooLargeError,
    SSRFBlockedError,
    TLSValidationError,
    UnsupportedProviderError,
)
from thetvview.security.redaction import contains_secret
from thetvview.ui import actions as ui_actions
from thetvview.ui import app as ui_app
from thetvview.ui import colors, screens
from thetvview.ui.actions import P, V, Action
from thetvview.ui.app import App
from thetvview.ui.errormsg import (
    ErrorKind,
    UiError,
    describe_playback_error,
    describe_playlist_error,
    empty_playlist_error,
)
from thetvview.ui.widgets import Modal, StatusBar


def _network(status: int | None = None, reason: str | None = None) -> NetworkError:
    """`NetworkError` con el estado estructurado que fija `safe_http`."""
    exc = NetworkError("El servidor denegó el acceso (HTTP 403)." if status else "x")
    exc.status = status
    exc.reason = reason
    return exc


# ---------------------------------------------------------------------------
# 1. Unit — los quince casos de §15.1
# ---------------------------------------------------------------------------


class TestClasificacionHTTP(unittest.TestCase):
    """§8.1–§8.3 y §8.8: el status manda y va como detalle secundario."""

    def test_401_dice_credenciales_o_acceso(self) -> None:
        err = describe_playlist_error(_network(401))
        self.assertEqual(err.message, "El servidor rechazó las credenciales o el acceso.")
        self.assertEqual(err.detail, "Código HTTP: 401")

    def test_403_no_asume_credenciales_incorrectas(self) -> None:
        """§8.2 lo prohíbe explícitamente: puede ser IP, región o CDN."""
        err = describe_playlist_error(_network(403))
        self.assertEqual(err.message, "El servidor rechazó el acceso.")
        self.assertNotIn("credencial", err.message)
        self.assertEqual(err.detail, "Código HTTP: 403")

    def test_404_playlist_dice_que_no_se_encontro(self) -> None:
        err = describe_playlist_error(_network(404))
        self.assertEqual(err.message, "No se encontró la lista solicitada.")
        self.assertEqual(err.detail, "Código HTTP: 404")

    def test_404_reproduccion_habla_del_canal(self) -> None:
        """§8.3 da un texto distinto para reproducción."""
        err = describe_playback_error(_network(404))
        self.assertEqual(err.message, "La dirección del canal ya no existe.")

    def test_5xx_todo_igual(self) -> None:
        for code in (500, 502, 503):
            with self.subTest(code=code):
                err = describe_playlist_error(_network(code))
                self.assertEqual(
                    err.message,
                    "El servidor encontró un problema al procesar la solicitud.",
                )
                self.assertEqual(err.detail, f"Código HTTP: {code}")

    def test_titulo_segun_flujo(self) -> None:
        self.assertEqual(
            describe_playlist_error(_network(403)).title,
            "No se pudo cargar la lista",
        )
        self.assertEqual(
            describe_playback_error(PlayerError("x")).title,
            "No se pudo reproducir este canal",
        )

    def test_kind_corresponde_al_flujo(self) -> None:
        self.assertIs(describe_playlist_error(_network(403)).kind, ErrorKind.PLAYLIST)
        self.assertIs(describe_playback_error(PlayerError("x")).kind, ErrorKind.PLAYBACK)

    def test_sin_status_no_hay_detail(self) -> None:
        """§8.4: no se inventan códigos HTTP donde no los hay."""
        err = describe_playlist_error(_network())
        self.assertIsNone(err.detail)
        self.assertNotIn("HTTP", err.cuerpo())


class TestClasificacionRed(unittest.TestCase):
    """§8.4–§8.7: por atributo `reason`, no por la prosa del mensaje."""

    CASOS = {
        "timeout": "El servidor tardó demasiado en responder.",
        "dns": "No se pudo encontrar el servidor.",
        "refused": "No se pudo conectar con el servidor.",
        "unreachable": "No se pudo conectar con el servidor.",
        "connection": "No se pudo conectar con el servidor.",
    }

    def test_cada_reason_da_su_mensaje(self) -> None:
        for reason, esperado in self.CASOS.items():
            with self.subTest(reason=reason):
                err = describe_playlist_error(_network(reason=reason))
                self.assertEqual(err.message, esperado)

    def test_timeout_tiene_detalle_propio_y_no_un_codigo(self) -> None:
        err = describe_playlist_error(_network(reason="timeout"))
        self.assertEqual(err.detail, "Tiempo de espera agotado.")

    def test_tls(self) -> None:
        err = describe_playlist_error(TLSValidationError("certificado"))
        self.assertEqual(
            err.message, "No se pudo establecer una conexión segura con el servidor."
        )

    def test_safe_http_fija_los_seis_reason(self) -> None:
        """F1-bis: el atributo se fija en el dominio, no se deduce del texto."""
        import thetvview.security.safe_http as safe_http

        casos = [
            (ssl.SSLCertVerificationError("bad"), "tls"),
            (ssl.SSLError("handshake"), "tls"),
            (socket.gaierror(-2, "no resuelve"), "dns"),
            (TimeoutError(), "timeout"),
            (socket.timeout(), "timeout"),
            (ConnectionRefusedError(111, "rechazada"), "refused"),
            (OSError(101, "sin ruta"), "unreachable"),
            (OSError("otro motivo"), "connection"),
        ]
        for original, esperado in casos:
            with self.subTest(reason=esperado, tipo=type(original).__name__):
                mapped = safe_http._map_network_error(original, 5.0)
                self.assertEqual(mapped.reason, esperado)

    def test_texto_ilegible_es_parse(self) -> None:
        err = describe_playlist_error(ParseError("xml roto"))
        self.assertIn("no se puede leer", err.message)

    def test_respuesta_demasiado_grande(self) -> None:
        err = describe_playlist_error(ResponseTooLargeError("grande"))
        self.assertIn("demasiado grande", err.message)

    def test_bloqueo_antissrf(self) -> None:
        err = describe_playlist_error(SSRFBlockedError("privada"))
        self.assertIn("red interna o privada", err.message)

    def test_rate_limit_y_limite_local(self) -> None:
        for exc in (RateLimitError("espera"), ConnectionLimitError("muchas")):
            with self.subTest(tipo=type(exc).__name__):
                err = describe_playlist_error(exc)
                self.assertIn("demasiadas peticiones", err.message)

    def test_proveedor_no_compatible(self) -> None:
        err = describe_playlist_error(UnsupportedProviderError("nope"))
        self.assertIn("no parece un proveedor", err.message)

    def test_fuente_inesperada(self) -> None:
        err = describe_playlist_error(InvalidSourceError("raro"))
        self.assertEqual(
            err.message, "No se pudo interpretar la respuesta del servidor."
        )

    def test_cuenta_caducada_no_es_credencial(self) -> None:
        err = describe_playlist_error(AccountExpiredError("caducada"))
        self.assertIn("cuenta no está activa", err.message)

    def test_auth_sin_status_no_elige_entre_401_y_403(self) -> None:
        err = describe_playlist_error(AuthenticationError("mal"))
        self.assertEqual(err.message, "El servidor rechazó las credenciales o el acceso.")


class TestClasificacionReproduccion(unittest.TestCase):
    """§15.1 lado reproducción: decodificación, protocolo, reproductor, 404."""

    def test_decode(self) -> None:
        err = describe_playback_error(PlayerError("invalid data found"))
        self.assertEqual(
            err.message, "No se pudo interpretar la respuesta del servidor."
        )

    def test_protocolo_no_soportado(self) -> None:
        err = describe_playback_error(PlayerError("Protocol not found"))
        self.assertEqual(err.message, "El formato de esta fuente no es compatible.")

    def test_reproductor_ausente_por_texto(self) -> None:
        """§8.11: no hay reproductor, y eso no es «falló la reproducción»."""
        err = describe_playback_error(
            PlayerError("Reproductor 'mpv' no encontrado. Instálalo.")
        )
        self.assertEqual(err.message, "No hay ningún reproductor compatible instalado.")

    def test_reproductor_ausente_por_otro_mensaje(self) -> None:
        err = describe_playback_error(PlayerError("No hay reproductor disponible."))
        self.assertEqual(err.message, "No hay ningún reproductor compatible instalado.")

    def test_404_escrito_por_el_reproductor(self) -> None:
        """Los tres binarios ponen el código en su salida; hay que leerlo."""
        err = describe_playback_error(PlayerError("Server returned 404 (Not Found)"))
        self.assertEqual(err.message, "La dirección del canal ya no existe.")
        self.assertEqual(err.detail, "Código HTTP: 404")

    def test_403_escrito_por_el_reproductor(self) -> None:
        err = describe_playback_error(PlayerError("cannot be opened (403)"))
        self.assertEqual(err.detail, "Código HTTP: 403")

    def test_401_escrito_por_el_reproductor(self) -> None:
        err = describe_playback_error(PlayerError("401 Unauthorized"))
        self.assertEqual(err.detail, "Código HTTP: 401")

    def test_timeout(self) -> None:
        err = describe_playback_error(PlayerError("Connection timed out"))
        self.assertEqual(err.message, "El servidor tardó demasiado en responder.")

    def test_conexion(self) -> None:
        err = describe_playback_error(PlayerError("could not connect to host"))
        self.assertEqual(err.message, "No se pudo conectar con el servidor.")

    def test_tls(self) -> None:
        err = describe_playback_error(PlayerError("certificate verify failed"))
        self.assertEqual(
            err.message, "No se pudo establecer una conexión segura con el servidor."
        )

    def test_desconocido(self) -> None:
        err = describe_playback_error(PlayerError("algo raro sin pista"))
        self.assertEqual(err.message, "No se pudo completar la operación.")


class TestListaVacia(unittest.TestCase):
    def test_titulo_propio(self) -> None:
        err = empty_playlist_error("Mi lista")
        self.assertEqual(err.title, "La lista está vacía")
        self.assertIn("'Mi lista'", err.message)

    def test_es_error_de_playlist(self) -> None:
        self.assertIs(empty_playlist_error("x").kind, ErrorKind.PLAYLIST)

    def test_sin_nombre_no_inventa_uno(self) -> None:
        err = empty_playlist_error("")
        self.assertNotIn("''", err.message)


class TestCadenaDeCausas(unittest.TestCase):
    """H3: `fetch_bytes` aplana a `OSError` pero conserva `__cause__`."""

    def test_el_status_sobrevive_a_fetch_bytes(self) -> None:
        """El caso del §1: 403 de una lista remota, recién envuelto."""
        original = _network(403)

        def falla(*_args: object, **_kwargs: object) -> bytes:
            raise original

        with mock.patch(
            "thetvview.security.safe_http.SafeHttpClient.get_bytes",
            side_effect=falla,
        ):
            with self.assertRaises(OSError) as ctx:
                fetch_bytes("https://ejemplo.test/l.m3u")

        err = describe_playlist_error(ctx.exception)
        self.assertEqual(err.message, "El servidor rechazó el acceso.")
        self.assertEqual(err.detail, "Código HTTP: 403")

    def test_el_reason_sobrevive_a_fetch_bytes(self) -> None:
        original = _network(reason="dns")

        def falla(*_args: object, **_kwargs: object) -> bytes:
            raise original

        with mock.patch(
            "thetvview.security.safe_http.SafeHttpClient.get_bytes",
            side_effect=falla,
        ):
            with self.assertRaises(OSError) as ctx:
                fetch_bytes("https://ejemplo.test/l.m3u")

        err = describe_playlist_error(ctx.exception)
        self.assertEqual(err.message, "No se pudo encontrar el servidor.")

    def test_una_excepcion_por_debajo_de_otra_se_encuentra(self) -> None:
        try:
            try:
                raise TLSValidationError("certificado")
            except OSError as exc:
                raise OSError("envuelto") from exc
        except OSError as exc:
            err = describe_playlist_error(exc)
        self.assertIn("conexión segura", err.message)

    def test_context_tambien_se_recorre(self) -> None:
        """`raise X` sin `from` rellena `__context__`, que también vale."""
        try:
            try:
                raise ParseError("xml roto")
            except ValueError:
                raise OSError("sin from")
        except OSError as exc:
            err = describe_playlist_error(exc)
        self.assertIn("no se puede leer", err.message)

    def test_ciclo_no_cuelga(self) -> None:
        a = OSError("a")
        b = OSError("b")
        a.__cause__ = b
        b.__cause__ = a
        self.assertIsInstance(describe_playlist_error(a), UiError)


# ---------------------------------------------------------------------------
# 2. Seguridad (§15.3)
# ---------------------------------------------------------------------------

#: Los cuatro secretos que el SDD nombra explícitamente.
_SECRETOS = ("password=hunter2", "token=abcdef123456", "user:pass@host", "/live/u/p/123.ts")


class TestSeguridad(unittest.TestCase):
    """§15.3 + §2.4: ningún secreto llega a título, mensaje, detalle ni pie."""

    def _errores_con_secreto(self) -> list[UiError]:
        casos: list[BaseException] = [
            NetworkError(f"failed to fetch http://user:pass@host/live/u/p/123.ts?password=hunter2"),
            OSError("http://user:pass@host/live/u/p/123.ts"),
            PlayerError("Failed to open 'http://user:pass@host/live/u/p/123.ts?token=abcdef123456'"),
            TLSValidationError("falló con http://user:pass@host/live/u/p/123.ts"),
            ParseError("http://user:pass@host/live/u/p/123.ts"),
        ]
        errs = [describe_playlist_error(e) for e in casos]
        errs += [describe_playback_error(e) for e in casos]
        return errs

    def test_no_aparece_el_secreto(self) -> None:
        for err in self._errores_con_secreto():
            with self.subTest(titulo=err.title):
                texto = "\n".join(filter(None, (err.title, err.message, err.detail)))
                for secreto in _SECRETOS:
                    self.assertNotIn(secreto, texto)

    def test_cuerpo_no_contains_secret(self) -> None:
        """El predicado del propio módulo de redacción, no una copia del patrón."""
        for err in self._errores_con_secreto():
            with self.subTest(titulo=err.title):
                self.assertFalse(contains_secret(err.cuerpo()))

    def test_la_url_entera_no_se_muestra(self) -> None:
        err = describe_playlist_error(
            OSError("no se pudo http://proveedor.test/secreto/live.m3u")
        )
        self.assertNotIn("proveedor.test", err.cuerpo())

    def test_el_status_se_conserva_aunque_el_texto_se_redacte(self) -> None:
        """Redactar el mensaje no puede borrar el status: es un atributo."""
        err = describe_playlist_error(_status_y_secreto())
        self.assertEqual(err.detail, "Código HTTP: 403")
        self.assertNotIn("pass@host", err.cuerpo())


def _status_y_secreto() -> NetworkError:
    """`NetworkError` con status y con un secreto en el mensaje."""
    exc = NetworkError("El servidor denegó el acceso (HTTP 403). user:pass@host")
    exc.status = 403
    return exc


class TestSinTraceback(unittest.TestCase):
    """§2.5: al usuario nunca se le enseña una excepción de Python."""

    def test_ningun_camino_lo_escribe(self) -> None:
        for caso in (
            _network(403),
            _network(reason="dns"),
            OSError("algo"),
            ValueError("algo"),
            PlayerError("algo"),
            ParseError("algo"),
        ):
            with self.subTest(tipo=type(caso).__name__):
                for err in (describe_playlist_error(caso), describe_playback_error(caso)):
                    texto = err.cuerpo()
                    self.assertNotIn("Traceback", texto)
                    self.assertNotIn("__cause__", texto)
                    self.assertNotIn(type(caso).__name__, texto)


# ---------------------------------------------------------------------------
# 3. UI — el doble de App
# ---------------------------------------------------------------------------


class _AppCase(unittest.TestCase):
    """`App(None)` con `data/` de pruebas: sin terminal, sin tocar disco real."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        for attr, name in (
            ("PLAYLISTS_JSON", "playlists.json"),
            ("FAVORITES_JSON", "favorites.json"),
            ("PREFS_JSON", "prefs.json"),
            ("RECENTS_JSON", "recents.json"),
            ("THEME_JSON", "theme.json"),
        ):
            patcher = mock.patch.object(ui_app.config, attr, base / name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.app = App(None)  # noqa: ANN001 - sin terminal, a propósito
        self.app._in_modal = False


class _ModalSpy:
    """Un `Modal` real que además se recuerda, para poder inspeccionarlo.

    No es un doble: **es el modal de verdad**, sólo que además de dibujarse se
    apunta en una lista. Importa, porque el §13 no es «el pie dice R y existe una
    R en algún sitio», sino «la tecla que el pie anuncia devuelve lo que el
    modal dice que devuelve»; comprobarlo con el `Modal` real es la única forma
    de que el test no dependa de una copia de su lógica.
    """

    instancias: list[Modal] = []

    def __new__(cls, *args: object, **kwargs: object) -> Modal:
        modal = Modal(*args, **kwargs)  # type: ignore[arg-type]
        _ModalSpy.instancias.append(modal)
        return modal


class _UI(_AppCase):
    """Base de los tests de flujo: memoriza los modales y simula el teclado.

    Sin curses, `_run_modal` degrada a ``None`` y ningún callback se ejecutaría:
    hay que sustituirlo por uno que **simule el bucle** — abre el modal, le pasa
    las teclas de la cola y devuelve lo que su `handle_key` responda. Es el
    mismo contrato que el bucle real, así que lo que se prueba es el despacho
    de `show_error`, no una ruta paralelo de test.
    """

    def setUp(self) -> None:
        super().setUp()
        _ModalSpy.instancias.clear()
        patcher = mock.patch("thetvview.ui.widgets.Modal", _ModalSpy)
        patcher.start()
        self.addCleanup(patcher.stop)
        #: Teclas que el usuario «pulsa», en orden. Es una **cola que se
        #: consume**: cada modal que se abre se lleva lo suyo y se lo come. Sin
        #: esto un `R` se reintentaría para siempre, porque el doble siempre
        #: devolvería «retry»; el bucle real no puede hacer eso, porque cada
        #: vuelta espera una tecla **nueva** del usuario.
        self.teclas: deque[int] = deque()
        runner = mock.patch.object(
            self.app,
            "_run_modal",
            side_effect=self._simular_bucle_modal,
        )
        runner.start()
        self.addCleanup(runner.stop)

    def pulsar(self, *teclas: int) -> None:
        """Encola lo que el usuario pulsa dentro del modal que se abra."""
        self.teclas.extend(teclas)

    def _simular_bucle_modal(self, modal: Modal) -> str | None:
        while self.teclas:
            respuesta = modal.handle_key(self.teclas.popleft())
            if respuesta is not None:
                return respuesta
        return None

    @property
    def modal(self) -> Modal:
        self.assertEqual(len(_ModalSpy.instancias), 1, "se esperaba un solo modal")
        return _ModalSpy.instancias[0]

    def pie(self) -> str:
        """El pie tal y como se dibuja, sin límite de ancho artificial."""
        return self.modal._actions_line(10_000)  # noqa: SLF001 - es lo que se ve


def _entrada(nombre: str = "Lista") -> "object":
    from thetvview.playlist_manager import PlaylistEntry

    return PlaylistEntry(name=nombre, source="https://ejemplo.test/l.m3u")


class TestFlujoPlaylist(_UI):
    """§15.2 lado playlist: modal, título, código visible, `R` reintenta."""

    def test_un_403_abre_modal_con_titulo_y_codigo(self) -> None:
        with mock.patch(
            "thetvview.ui.app.load_playlist_source", side_effect=_network(403)
        ):
            screens.open_playlist(self.app, _entrada())
        modal = self.modal
        self.assertIn("No se pudo cargar la lista", modal.title)
        self.assertIn("El servidor rechazó el acceso.", modal.message)
        self.assertIn("Código HTTP: 403", modal.message)
        self.assertTrue(self.app.status.is_error)

    def test_r_reintenta_la_misma_operacion(self) -> None:
        intentos = {"n": 0}

        def falla(*_a: object, **_k: object) -> None:
            intentos["n"] += 1
            raise _network(403)

        self.pulsar(ord("R"))
        with mock.patch("thetvview.ui.app.load_playlist_source", side_effect=falla):
            screens.open_playlist(self.app, _entrada())
        self.assertEqual(
            intentos["n"], 2, "R debe repetir la descarga: la 1ª + la del retry"
        )

    def test_esc_cierra_sin_reintentar(self) -> None:
        intentos = {"n": 0}

        def falla(*_a: object, **_k: object) -> None:
            intentos["n"] += 1
            raise _network(403)

        self.pulsar(27)
        with mock.patch("thetvview.ui.app.load_playlist_source", side_effect=falla):
            screens.open_playlist(self.app, _entrada())
        self.assertEqual(intentos["n"], 1, "Esc no debe reintentar")

    def test_el_pie_anuncia_r_y_esc(self) -> None:
        with mock.patch(
            "thetvview.ui.app.load_playlist_source", side_effect=_network(403)
        ):
            screens.open_playlist(self.app, _entrada())
        pie = self.pie()
        self.assertIn("R Reintentar", pie)
        self.assertIn("Esc Volver", pie)
        self.assertNotIn("D Diagnóstico", pie)

    def test_lista_vacia_no_anuncia_r(self) -> None:
        """Recargar una lista vacía no la va a llenar: no hay `R` (§13)."""
        from thetvview.models import Playlist

        vacia = Playlist(name="Lista", channels=[], source="https://ejemplo.test/l.m3u")
        with mock.patch("thetvview.ui.app.load_playlist_source", return_value=vacia):
            screens.open_playlist(self.app, _entrada())
        pie = self.pie()
        self.assertNotIn("R Reintentar", pie)
        self.assertIn("Esc Volver", pie)
        self.assertIn("La lista está vacía", self.modal.title)

    def test_recargar_reintenta_la_propia_operacion(self) -> None:
        """E5: `R` es `reload_current_playlist`, no un segundo camino."""
        from thetvview.models import Playlist

        pl = Playlist(
            name="Lista",
            channels=[Channel(name="N", url="http://x/a.m3u", group="G")],
            source="https://ejemplo.test/l.m3u",
        )
        _ModalSpy.instancias.clear()
        pantalla = mock.Mock()
        pantalla.playlist = pl
        pantalla.title = "Canales"
        pantalla.actions.return_value = []
        pantalla.shortcuts.return_value = ""
        self.app.stack = [pantalla]

        intentos = {"n": 0}

        def falla(*_a: object, **_k: object) -> None:
            intentos["n"] += 1
            raise _network(403)

        self.pulsar(ord("R"))
        with mock.patch("thetvview.ui.app.load_playlist_source", side_effect=falla):
            self.app.reload_current_playlist()
        self.assertEqual(
            intentos["n"], 2, "R debe recargar otra vez: la 1ª + la del retry"
        )

    def test_reload_sin_lista_sigue_diciendo_nada_que_actualizar(self) -> None:
        """Sin lista abierta no hay nada que recargar, y eso no es un error."""
        pantalla = mock.Mock()
        pantalla.playlist = None
        pantalla.title = "Playlists"
        pantalla.actions.return_value = []
        pantalla.shortcuts.return_value = ""
        self.app.stack = [pantalla]
        self.app.reload_current_playlist()
        self.assertIn("Nada que actualizar", self.app.status.message)
        self.assertFalse(self.app.status.is_error)
        self.assertEqual(_ModalSpy.instancias, [], "no debe abrir modal")


class TestFlujoPlayback(_UI):
    """§15.2 lado reproducción: `R` relanza, `D` diagnostica, `Esc` cierra."""

    CANAL = Channel(name="Canal 1", url="http://ejemplo.test/live/a.m3u8", group="G")

    def _play_channel(self, exc: BaseException, **kwargs: object) -> None:
        with mock.patch(
            "thetvview.ui.screens._lanzar_con_router", side_effect=exc
        ):
            screens.play_channel(self.app, self.CANAL, "mpv", **kwargs)

    def test_el_error_abre_modal_contextual(self) -> None:
        self._play_channel(PlayerError("Server returned 404 (Not Found)"))
        self.assertIn("No se pudo reproducir", self.modal.title)
        self.assertIn("Código HTTP: 404", self.modal.message)
        self.assertTrue(self.modal.wrap, "el modal de error debe envolver el texto")

    def test_r_relanza_con_los_mismos_argumentos(self) -> None:
        """H10: el retry es un cierre sobre los argumentos de esta llamada."""
        launches: list[tuple] = []

        def lanza(*_a: object, **kwargs: object) -> tuple:
            launches.append(kwargs)
            raise PlayerError("Server returned 404 (Not Found)")

        self.pulsar(ord("R"))
        with mock.patch("thetvview.ui.screens._lanzar_con_router", side_effect=lanza):
            screens.play_channel(self.app, self.CANAL, "mpv")
        self.assertEqual(len(launches), 2, "R debe volver a lanzar")

    def test_el_retry_recibe_los_mismos_argumentos_que_la_primera_vez(self) -> None:
        """No basta con «relanzó»: tiene que relanzar **lo mismo** (H10)."""
        vistas: list[tuple] = []

        def lanza(canal, player_name, *args, **kwargs):  # noqa: ANN001, ANN202
            vistas.append((canal, player_name, args, tuple(sorted(kwargs.items(), key=str))))
            raise PlayerError("Server returned 404 (Not Found)")

        self.pulsar(ord("R"))
        with mock.patch("thetvview.ui.screens._lanzar_con_router", side_effect=lanza):
            screens.play_channel(self.app, self.CANAL, "mpv", selection="sel")
        self.assertEqual(len(vistas), 2)
        self.assertEqual(vistas[0], vistas[1], "el retry cambió algún argumento")

    def test_d_invoca_show_diagnose(self) -> None:
        """H9: el diagnóstico es el que ya existe, con conexión."""
        self.pulsar(ord("D"))
        with mock.patch("thetvview.ui.screens._lanzar_con_router",
                        side_effect=PlayerError("Server returned 404")):
            with mock.patch.object(self.app, "show_diagnose") as diag:
                screens.play_channel(self.app, self.CANAL, "mpv")
        diag.assert_called_once_with(self.CANAL, con_conexion=True)

    def test_el_pie_anuncia_las_tres(self) -> None:
        self._play_channel(PlayerError("boom"))
        pie = self.pie()
        self.assertIn("R Reintentar", pie)
        self.assertIn("D Diagnóstico", pie)
        self.assertIn("Esc Volver", pie)

    def test_esc_cierra_sin_relanzar(self) -> None:
        intentos = {"n": 0}

        def lanza(*_a: object, **_k: object) -> tuple:
            intentos["n"] += 1
            raise PlayerError("boom")

        self.pulsar(27)
        with mock.patch("thetvview.ui.screens._lanzar_con_router", side_effect=lanza):
            screens.play_channel(self.app, self.CANAL, "mpv")
        self.assertEqual(intentos["n"], 1, "Esc no debe relanzar")

    def test_sin_reproductor_conserva_su_mensaje_propio(self) -> None:
        """H13/§14 del SDD-M: «no se puede abrir aquí» no es «falló»."""
        from thetvview.player.router import NoCompatibleBackend

        self._play_channel(
            NoCompatibleBackend("Ningún reproductor instalado abre este transporte.")
        )
        self.assertIn("transporte", self.modal.message)
        # Sin retry ni diagnóstico: no hay nada que reintentar ni que explicar,
        # porque el problema es del entorno y no del canal.
        self.assertNotIn("Diagnóstico", self.pie())
        self.assertNotIn("Reintentar", self.pie())

    def test_credenciales_ausentes_no_ofrece_reintentar(self) -> None:
        """Sin contraseña, repetir la resolución daría el mismo error (F5)."""
        from thetvview.stream_ref import MissingCredentialsError

        with mock.patch(
            "thetvview.ui.screens.resolve_channel_url",
            side_effect=MissingCredentialsError("Falta la contraseña."),
        ):
            screens.play_channel(self.app, self.CANAL, "mpv")
        pie = self.pie()
        self.assertNotIn("Reintentar", pie)
        self.assertIn("Esc Volver", pie)
        self.assertIn("Diagnóstico", pie)
        self.assertIn("Falta la contraseña", self.modal.message)


# ---------------------------------------------------------------------------
# 4. Atajos truthful (§13)
# ---------------------------------------------------------------------------


class TestAtajosTruthful(unittest.TestCase):
    """Para cada `Action` del pie existe una tecla que `handle_key` atiende.

    El mismo espíritu que AC-11 en `tests/test_empty_states.py`: no basta con que
    el pie diga «R Reintentar», tiene que haber una `R` detrás.
    """

    def _modal(self, *, retry: bool, diagnose: bool):
        acciones: list[Action] = []
        atajos: dict[int, str] = {}
        if retry:
            acciones.append(Action("R", V.REINTENTAR, P.PRIMARIA, essential=True))
            atajos[ord("R")] = "retry"
        if diagnose:
            acciones.append(Action("D", V.DIAGNOSTICO, P.SECUNDARIA, essential=True))
            atajos[ord("D")] = "diagnose"
        acciones.append(Action("Esc", V.VOLVER, P.NAVEGACION, essential=True))
        return Modal("T", "M", ["Aceptar"], actions=acciones, wrap=True, keys=atajos)

    def test_cada_anuncio_tiene_tecla(self) -> None:
        for retry in (True, False):
            for diagnose in (True, False):
                with self.subTest(retry=retry, diagnose=diagnose):
                    modal = self._modal(retry=retry, diagnose=diagnose)
                    for accion in modal.actions:
                        if accion.key in ("Enter", "↑↓"):
                            continue
                        respuesta = modal.handle_key(_tecla_de(accion.key))
                        self.assertIsNotNone(
                            respuesta,
                            f"el pie anuncia «{accion.key}» pero la tecla no hace nada",
                        )

    def test_sin_callback_no_se_anuncia_la_tecla(self) -> None:
        modal = self._modal(retry=False, diagnose=False)
        self.assertIsNone(modal.handle_key(ord("R")))
        self.assertIsNone(modal.handle_key(ord("D")))
        pie = modal._actions_line(200)  # noqa: SLF001
        self.assertNotIn("R Reintentar", pie)
        self.assertNotIn("D Diagnóstico", pie)

    def test_reintentar_esta_en_el_vocabulario_cerrado(self) -> None:
        """H6: sin estar en `V`, `validate()` rechazaría el pie."""
        self.assertIn(V.REINTENTAR, ui_actions.LABELS)
        ui_actions.validate(self._modal(retry=True, diagnose=True).actions)


def _tecla_de(anuncio: str) -> int:
    """La tecla que el pie anuncia con `anuncio`."""
    return ord({"Esc": "\x1b", "Enter": "\n", "R": "R", "D": "D"}.get(anuncio, anuncio[0]))


# ---------------------------------------------------------------------------
# 5. Terminal estrecha (§15.4)
# ---------------------------------------------------------------------------


class _VentanaFalsa:
    """`addstr` que valida los límites como hace curses de verdad."""

    def __init__(self, max_y: int, max_x: int) -> None:
        self._y, self._x = max_y, max_x
        self.calls: list[tuple[int, int, str]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return self._y, self._x

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        import curses

        if not (0 <= y < self._y) or not (0 <= x < self._x):
            raise curses.error(f"fuera de la ventana: ({y},{x}) en {self._y}x{self._x}")
        self.calls.append((y, x, text))

    def refresh(self) -> None:
        pass


class TestTerminalEstrecha(unittest.TestCase):
    """§12 + §15.4: el modal se adapta y no pierde sus acciones."""

    MENSAJES = (
        "El servidor rechazó el acceso.",
        "El servidor encontró un problema al procesar la solicitud. "
        "Vuelve a intentarlo en unos minutos y, si sigue igual, revisa la fuente.",
        "No se pudo completar la operación.",
    )

    def _modal(self, mensaje: str) -> Modal:
        return Modal(
            "No se pudo cargar la lista",
            mensaje,
            ["Aceptar"],
            actions=[
                Action("R", V.REINTENTAR, P.PRIMARIA, essential=True),
                Action("D", V.DIAGNOSTICO, P.SECUNDARIA),
                Action("Esc", V.VOLVER, P.NAVEGACION, essential=True),
            ],
            wrap=True,
            keys={ord("R"): "retry", ord("D"): "diagnose"},
        )

    #: Por debajo de este alto `Modal.render` no dibuja nada (§39): una caja con
    #: borde y sombra no cabe, y una modal a medias se ve peor que ninguna. Por
    #: eso el mínimo se comprueba **desde** 8, no desde 1.
    _ALTO_MINIMO = 8

    def test_calc_rect_no_sale_de_la_ventana(self) -> None:
        for max_x in (20, 40, 80, 120, 200):
            for max_y in (7, 10, 24, 50):
                with self.subTest(max_y=max_y, max_x=max_x):
                    modal = self._modal(self.MENSAJES[1])
                    y, x, h, w = modal._calc_rect(max_y, max_x)
                    self.assertGreaterEqual(y, 1)
                    self.assertGreaterEqual(x, 1)
                    self.assertLessEqual(x + w - 1, max_x)
                    if max_y >= self._ALTO_MINIMO:
                        self.assertLessEqual(
                            y + h - 1,
                            max_y - 1,
                            "el modal dibuja por debajo de la última fila",
                        )

    def test_a_partir_del_alto_minimo_el_alto_no_decrece(self) -> None:
        """`_calc_rect` es monótona en el alto: crecer no encoge el modal."""
        for max_x in (20, 40, 80):
            with self.subTest(max_x=max_x):
                anterior = 0
                for max_y in range(self._ALTO_MINIMO, 60):
                    alto = self._modal(self.MENSAJES[1])._calc_rect(max_y, max_x)[2]
                    self.assertGreaterEqual(alto, anterior)
                    anterior = alto

    def test_render_no_lanza_curses_error(self) -> None:
        errores = 0
        with mock.patch.object(colors, "pair", return_value=0):
            for mensaje in self.MENSAJES:
                for max_y in range(7, 45, 3):
                    for max_x in range(20, 181, 7):
                        try:
                            self._modal(mensaje).render(_VentanaFalsa(max_y, max_x))
                        except Exception:  # noqa: BLE001 - §39: nunca debe-tañar
                            errores += 1
        self.assertEqual(errores, 0)

    def test_r_y_esc_siguen_en_el_pie(self) -> None:
        """§12: en terminal estrecha se cae `D`, nunca `R` ni `Esc`."""
        for max_x in (20, 40, 80, 120):
            with self.subTest(max_x=max_x):
                modal = self._modal(self.MENSAJES[0])
                _y, _x, _h, w = modal._calc_rect(24, max_x)
                pie = modal._actions_line(max(0, w - 4))
                self.assertIn("Esc Volver", pie)
                if max_x >= 40:
                    self.assertIn("R Reintentar", pie)

    def test_el_mensaje_no_se_trunca(self) -> None:
        """§12: envolver es partir, no cortar."""
        modal = self._modal(self.MENSAJES[1])
        _lines, inner_w = modal._layout(24, 80)
        todas = " ".join(modal._lines(inner_w))
        for frase in ("El servidor encontró un problema", "revisar la fuente"):
            # La última palabra de la frase puede partirse por el borde, pero el
            # texto no puede perder contenido.
            palabras = frase.split()
            self.assertIn(palabras[-1].rstrip("."), todas)
        self.assertGreater(len(modal._lines(inner_w)), 1)

    def test_sin_wrap_el_comportamiento_no_cambia(self) -> None:
        """R1: los veinte modales existentes no se tocan."""
        modal = Modal("T", "uno\ndos muy largo")
        self.assertEqual(modal._lines(), ["uno", "dos muy largo"])


# ---------------------------------------------------------------------------
# 6. Degradación
# ---------------------------------------------------------------------------


class TestDegradacion(_AppCase):
    """Sin curses / en un hilo / con otro modal: nunca se cuelga (§39)."""

    def test_sin_curses_queda_en_la_barra(self) -> None:
        self.app.show_error(describe_playlist_error(_network(403)))
        self.assertIn("Código HTTP: 403", self.app.status.message)
        self.assertTrue(self.app.status.is_error)
        self.assertFalse(self.app._in_modal)

    def test_no_anida_modales(self) -> None:
        """Con otro modal abierto, el mensaje va a la barra y nada más (§39)."""
        self.app._in_modal = True
        self.app.show_error(describe_playlist_error(_network(403)))
        self.assertIn("No se pudo cargar la lista", self.app.status.message)
        # `_in_modal` sigue como estaba: `show_error` no abre ni cierra nada.
        self.assertTrue(self.app._in_modal)

    def test_desde_un_hilo_no_abre_modal(self) -> None:
        errores: list[BaseException] = []
        hilo = threading.Thread(target=lambda: _worker(errores))
        hilo.start()
        hilo.join(10)
        self.assertFalse(hilo.is_alive(), "show_error se colgó en un hilo")
        self.assertEqual(errores, [])
        self.assertFalse(self.app._in_modal)

    def test_el_retry_se_ejecuta_con_el_modal_ya_cerrado(self) -> None:
        """R6: el callback corre **después**, con `_in_modal` ya restaurado.

        Es lo que evita que el retry abra un modal encima del que se está
        cerrando: si se ejecutara dentro, `_run_modal` vería `_in_modal=True` y
        devolvería ``None`` sin hacer nada.
        """
        estados: list[tuple[str, bool]] = []
        llamado = {"n": 0}

        def _falso_run_modal(_modal: object) -> str | None:
            estados.append(("dentro_del_modal", self.app._in_modal))
            # Se reproduce lo que hace `_run_modal` de verdad: marcar y restaurar.
            self.app._in_modal = True
            try:
                return "retry"
            finally:
                self.app._in_modal = False

        def retry() -> None:
            llamado["n"] += 1
            estados.append(("en_el_retry", self.app._in_modal))

        with mock.patch.object(self.app, "_run_modal", side_effect=_falso_run_modal):
            self.app.show_error(describe_playlist_error(_network(403)), retry=retry)

        self.assertEqual(llamado["n"], 1)
        self.assertEqual(
            estados, [("dentro_del_modal", False), ("en_el_retry", False)]
        )

    def test_call_sites_degradan_sin_show_error(self) -> None:
        """Un doble de app sin `show_error` recibe el mensaje en la barra.

        Es la razón de que `_mostrar_error` consulte el atributo en vez de
        llamar a `app.show_error(...)` sin más: los dobles que ya usaba la
        suite siguen funcionando sin que este plan los invalide.
        """
        app = mock.Mock()
        del app.show_error  # el doble "viejo" no lo tiene
        app.status = StatusBar()
        app.cached_playlist.return_value = None  # fuerza la descarga
        with mock.patch(
            "thetvview.ui.app.load_playlist_source", side_effect=_network(403)
        ):
            screens.open_playlist(app, _entrada())
        self.assertIn("No se pudo cargar la lista", app.status.message)
        self.assertTrue(app.status.is_error)


def _worker(errores: list[BaseException]) -> None:
    """Ejecuta `show_error` desde un hilo secundario (R6 / §39)."""
    try:
        app = ui_app.App(None)  # noqa: ANN001
        app.show_error(describe_playlist_error(_network(403)))
    except BaseException as exc:  # noqa: BLE001 - el test lo reporta
        errores.append(exc)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()