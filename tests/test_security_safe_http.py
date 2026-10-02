"""Tests del cliente HTTP seguro (SDD §9, gaps B4/B5/B14).

Servidor local en 127.0.0.1: la política por defecto lo rechaza; los tests
que necesitan red lo habilitan de forma **explícita** con
``allow_private=True`` en el cliente.
"""

from __future__ import annotations

import gzip
import http.server
import io
import json
import socket
import ssl
import threading
import time
import unittest
import urllib.error
from unittest import mock

from thetvview.security import safe_http
from thetvview.security.errors import (
    ConnectionLimitError,
    InvalidSourceError,
    InvalidUrlError,
    NetworkError,
    RateLimitError,
    ResponseTooLargeError,
    SSRFBlockedError,
    TLSValidationError,
)
from thetvview.security.limits import reset_limits, set_limits
from thetvview.security.safe_http import (
    SafeHttpClient,
    SafeResponse,
    _map_network_error,
    get_bytes,
)
from thetvview.security.url_policy import UrlParts

_BODY_OK = b"hello world"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # noqa: A002
        pass

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head=True)

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    # -- helpers -------------------------------------------------------------

    def _send(
        self, code: int, body: bytes = b"", extra: list[tuple[str, str]] | None = None,
        head: bool = False, length: bool = True,
    ) -> None:
        self.send_response(code)
        for key, value in extra or []:
            self.send_header(key, value)
        if length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # El cliente ya cortó (tests de timeout): es esperado.
                self.close_connection = True

    # -- endpoints -----------------------------------------------------------

    def _serve(self, head: bool = False) -> None:
        path = self.path
        if path == "/ok":
            self._send(200, _BODY_OK, head=head)
        elif path == "/json":
            raw = json.dumps({"a": 1}).encode()
            self._send(
                200, raw, [("Content-Type", "application/json; charset=utf-8")], head
            )
        elif path == "/notjson":
            self._send(200, b"esto no es json", [("Content-Type", "text/plain")], head)
        elif path == "/big":
            self._send(200, b"x" * 5000, head=head)
        elif path == "/big-nolength":
            # Sin Content-Length: el tope se aplica leyendo.
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(b"y" * 5000)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
            self.close_connection = True
        elif path == "/gzip":
            self._send(
                200,
                gzip.compress(b"contenido comprimido"),
                [("Content-Encoding", "gzip")],
                head,
            )
        elif path == "/badgzip":
            self._send(
                200, b"esto no es gzip", [("Content-Encoding", "gzip")], head
            )
        elif path == "/br":
            self._send(200, b"xx", [("Content-Encoding", "br")], head)
        elif path == "/redir1":
            self._send(302, extra=[("Location", "/redir2")], head=True)
        elif path == "/redir2":
            self._send(302, extra=[("Location", "/ok")], head=True)
        elif path == "/loop":
            self._send(302, extra=[("Location", "/loop")], head=True)
        elif path == "/to-metadata":
            self._send(
                302, extra=[("Location", "http://169.254.169.254/latest/meta-data/")],
                head=True,
            )
        elif path == "/to-file":
            self._send(302, extra=[("Location", "file:///etc/passwd")], head=True)
        elif path == "/401":
            self._send(401, b"no", head=head)
        elif path == "/403":
            self._send(403, b"no", head=head)
        elif path == "/404":
            self._send(404, b"no", head=head)
        elif path == "/429":
            self._send(429, b"slow down", [("Retry-After", "7")], head)
        elif path == "/500":
            self._send(500, b"boom", head=head)
        elif path == "/flaky":
            count = getattr(self.server, "flaky", 0)
            self.server.flaky = count + 1  # type: ignore[attr-defined]
            if count < 2:
                self._send(503, b"retry", head=head)
            else:
                self._send(200, b"recuperado", head=head)
        elif path == "/retry-after":
            self._send(503, b"espera", [("Retry-After", "3600")], head)
        elif path == "/slow":
            time.sleep(1.5)
            self._send(200, b"lento", head=head)
        else:
            self._send(404, b"", head=head)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.flaky = 0  # type: ignore[attr-defined]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.client = SafeHttpClient(allow_private=True, timeout=5)
        self.addCleanup(reset_limits)

    def url(self, path: str) -> str:
        return self.base + path


# ---------------------------------------------------------------------------
# Política (SEC-005)
# ---------------------------------------------------------------------------


class TestPolitica(_Base):
    def test_por_defecto_bloquea_loopback(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            get_bytes(self.url("/ok"))

    def test_cliente_sin_excepcion_tambien_bloquea(self) -> None:
        client = SafeHttpClient(timeout=5)  # allow_private=False
        with self.assertRaises(SSRFBlockedError) as ctx:
            client.get_bytes(self.url("/ok"))
        self.assertIn("loopback", str(ctx.exception))

    def test_url_sin_esquema(self) -> None:
        with self.assertRaises(InvalidUrlError):
            self.client.get_bytes("127.0.0.1:8080/x.m3u")

    def test_url_con_userinfo(self) -> None:
        with self.assertRaises(InvalidUrlError) as ctx:
            self.client.get_bytes("http://u:p@127.0.0.1/x.m3u")
        self.assertIn("credenciales", str(ctx.exception))

    def test_esquema_prohibido(self) -> None:
        with self.assertRaises(InvalidUrlError):
            self.client.get_bytes("ftp://127.0.0.1/x")

    def test_url_con_espacios(self) -> None:
        with self.assertRaises(InvalidUrlError):
            self.client.get_bytes(self.url("/ok") + " con espacios")

    def test_url_demasiado_larga(self) -> None:
        with self.assertRaises(InvalidUrlError):
            self.client.get_bytes(self.url("/" + "a" * 3000))


# ---------------------------------------------------------------------------
# Respuesta básica (SEC-008)
# ---------------------------------------------------------------------------


class TestRespuesta(_Base):
    def test_get_bytes(self) -> None:
        self.assertEqual(self.client.get_bytes(self.url("/ok")), _BODY_OK)

    def test_get_text_con_charset(self) -> None:
        self.assertEqual(self.client.get_text(self.url("/json")), '{"a": 1}')

    def test_get_json(self) -> None:
        self.assertEqual(self.client.get_json(self.url("/json")), {"a": 1})

    def test_get_json_invalido(self) -> None:
        with self.assertRaises(InvalidSourceError):
            self.client.get_json(self.url("/notjson"))

    def test_head_no_lee_cuerpo(self) -> None:
        resp = self.client.head(self.url("/ok"))
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.ok)
        self.assertEqual(resp.body, b"")

    def test_request_no_lanza_por_status(self) -> None:
        resp = self.client.request(self.url("/404"))
        self.assertEqual(resp.status, 404)
        self.assertFalse(resp.ok)
        self.assertGreater(resp.elapsed, 0.0)

    def test_get_bytes_si_lanza_por_status(self) -> None:
        with self.assertRaises(NetworkError) as ctx:
            self.client.get_bytes(self.url("/404"))
        self.assertIn("404", str(ctx.exception))

    def test_401_y_403(self) -> None:
        for path in ("/401", "/403"):
            with self.subTest(path=path):
                with self.assertRaises(NetworkError) as ctx:
                    self.client.get_bytes(self.url(path))
                self.assertIn("denegó", str(ctx.exception))

    def test_429_es_rate_limit(self) -> None:
        with self.assertRaises(RateLimitError) as ctx:
            self.client.get_bytes(self.url("/429"))
        self.assertIn("7", str(ctx.exception))

    def test_500(self) -> None:
        with self.assertRaises(NetworkError) as ctx:
            self.client.get_bytes(self.url("/500"))
        self.assertIn("fallando", str(ctx.exception))


# ---------------------------------------------------------------------------
# Redirects (SEC-003)
# ---------------------------------------------------------------------------


class TestRedirects(_Base):
    def test_sigue_dos_saltos_revalidando(self) -> None:
        resp = self.client.request(self.url("/redir1"))
        self.assertEqual(resp.body, _BODY_OK)
        self.assertEqual(resp.redirects, 2)
        self.assertTrue(resp.final_url.endswith("/ok"))

    def test_bucle_cortado(self) -> None:
        with self.assertRaises(NetworkError) as ctx:
            self.client.get_bytes(self.url("/loop"))
        self.assertIn("redirecciones", str(ctx.exception))

    def test_redirect_a_metadata_bloqueado(self) -> None:
        # allow_private=True salva LAN/loopback, pero nunca metadata de nube.
        with self.assertRaises(SSRFBlockedError):
            self.client.get_bytes(self.url("/to-metadata"))

    def test_redirect_a_esquema_prohibido(self) -> None:
        with self.assertRaises(InvalidUrlError):
            self.client.get_bytes(self.url("/to-file"))

    def test_max_redirects_configurable(self) -> None:
        set_limits({"max_redirects": 1})
        with self.assertRaises(NetworkError) as ctx:
            self.client.get_bytes(self.url("/redir1"))
        self.assertIn("redirecciones", str(ctx.exception))

    def test_downgrade_https_a_http_bloqueado(self) -> None:
        client = SafeHttpClient(allow_private=True, timeout=5)
        calls = {"n": 0}

        def fake_check(url: str, purpose: str = "metadata", **kwargs: object) -> UrlParts:
            calls["n"] += 1
            scheme = "https" if calls["n"] == 1 else "http"
            return UrlParts(
                scheme=scheme, host="example.com", port=None, path="/", query="", raw=url
            )

        def fake_attempt(*args: object, **kwargs: object) -> object:
            return safe_http._Opened(
                fp=io.BytesIO(b""),
                status=302,
                reason="Found",
                headers={"location": "http://example.com/x"},
                final_url="https://example.com/",
            )

        with (
            mock.patch.object(safe_http, "check_url", side_effect=fake_check),
            mock.patch.object(SafeHttpClient, "_attempt", side_effect=fake_attempt),
        ):
            with self.assertRaises(TLSValidationError) as ctx:
                client.request("https://example.com/")
        self.assertIn("insegura", str(ctx.exception))


# ---------------------------------------------------------------------------
# Límites (SEC-004 / B4)
# ---------------------------------------------------------------------------


class TestLimites(_Base):
    def test_content_length_sobre_el_tope(self) -> None:
        with self.assertRaises(ResponseTooLargeError):
            self.client.get_bytes(self.url("/big"), max_bytes=1000)

    def test_lectura_sin_content_length(self) -> None:
        with self.assertRaises(ResponseTooLargeError):
            self.client.get_bytes(self.url("/big-nolength"), max_bytes=1000)

    def test_tope_global_de_limits(self) -> None:
        set_limits({"max_response_bytes": 1024})
        with self.assertRaises(ResponseTooLargeError):
            self.client.get_bytes(self.url("/big"))

    def test_gzip_ok(self) -> None:
        self.assertEqual(self.client.get_bytes(self.url("/gzip")), b"contenido comprimido")

    def test_gzip_danado(self) -> None:
        with self.assertRaises(NetworkError) as ctx:
            self.client.get_bytes(self.url("/badgzip"))
        self.assertIn("comprimida", str(ctx.exception))

    def test_encoding_no_admitida(self) -> None:
        with self.assertRaises(NetworkError) as ctx:
            self.client.get_bytes(self.url("/br"))
        self.assertIn("codificación", str(ctx.exception))

    def test_stream_respeta_el_tope(self) -> None:
        with self.assertRaises(ResponseTooLargeError):
            b"".join(self.client.stream(self.url("/big"), max_bytes=1000))

    def test_stream_itera_el_cuerpo(self) -> None:
        self.assertEqual(b"".join(self.client.stream(self.url("/ok"))), _BODY_OK)


# ---------------------------------------------------------------------------
# Timeout y concurrencia
# ---------------------------------------------------------------------------


class TestTimeoutYConcurrencia(_Base):
    def test_timeout_no_cuelga(self) -> None:
        client = SafeHttpClient(allow_private=True, timeout=0.4)
        started = time.monotonic()
        with self.assertRaises(NetworkError):
            client.get_bytes(self.url("/slow"))
        self.assertLess(time.monotonic() - started, 3.0)

    def test_sin_hueco_en_el_semaforo(self) -> None:
        set_limits({"connect_timeout": 0.3, "max_concurrent_requests": 1})
        slow = SafeHttpClient(allow_private=True, timeout=5)
        other = SafeHttpClient(allow_private=True, timeout=5)
        holder = threading.Thread(
            target=lambda: slow.get_bytes(self.url("/slow")), daemon=True
        )
        holder.start()
        time.sleep(0.5)  # el primer cliente ya tiene el semáforo
        try:
            with self.assertRaises(ConnectionLimitError) as ctx:
                other.get_bytes(self.url("/ok"))
            self.assertIn("descargas", str(ctx.exception))
        finally:
            holder.join(timeout=5)

    def test_deadline_global(self) -> None:
        # 2 peticiones lentas en serie no pueden superar el presupuesto.
        client = SafeHttpClient(allow_private=True, timeout=0.8)
        with self.assertRaises(NetworkError):
            client.get_bytes(self.url("/slow"), timeout=0.8)


# ---------------------------------------------------------------------------
# Reintentos (SDD §41)
# ---------------------------------------------------------------------------


class TestReintentos(_Base):
    def test_reintenta_503_y_recupera(self) -> None:
        self.server.flaky = 0  # type: ignore[attr-defined]
        with mock.patch.object(safe_http, "BACKOFF_SECONDS", (0.0, 0.0, 0.0)):
            body = self.client.get_bytes(self.url("/flaky"))
        self.assertEqual(body, b"recuperado")
        self.assertEqual(self.server.flaky, 3)  # type: ignore[attr-defined]

    def test_retry_after_respetado_y_acotado(self) -> None:
        with (
            mock.patch.object(safe_http, "BACKOFF_SECONDS", (0.0, 0.0, 0.0)),
            mock.patch.object(safe_http, "MAX_RETRY_WAIT", 0.05),
        ):
            started = time.monotonic()
            with self.assertRaises(NetworkError):
                self.client.get_bytes(self.url("/retry-after"))
            elapsed = time.monotonic() - started
        # Ha esperado por el Retry-After, pero acotado a MAX_RETRY_WAIT.
        self.assertGreaterEqual(elapsed, 0.09)
        self.assertLess(elapsed, 2.0)

    def test_reintentos_se_pueden_desactivar(self) -> None:
        self.server.flaky = 0  # type: ignore[attr-defined]
        with mock.patch.object(safe_http, "BACKOFF_SECONDS", (0.0, 0.0, 0.0)):
            with self.assertRaises(NetworkError):
                self.client.get_bytes(self.url("/flaky"), retry=False)
        self.assertEqual(self.server.flaky, 1)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Mapeo de errores de red
# ---------------------------------------------------------------------------


class TestMapeoErrores(unittest.TestCase):
    def test_tls_certificado(self) -> None:
        exc = ssl.SSLCertVerificationError(1, "certificate verify failed")
        mapped = _map_network_error(urllib.error.URLError(exc), 10)
        self.assertIsInstance(mapped, TLSValidationError)
        self.assertIn("certificado", str(mapped))

    def test_tls_generico(self) -> None:
        exc = ssl.SSLError(1, "wrong version number")
        mapped = _map_network_error(urllib.error.URLError(exc), 10)
        self.assertIsInstance(mapped, TLSValidationError)

    def test_dns(self) -> None:
        mapped = _map_network_error(
            urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), 10
        )
        self.assertIn("DNS", str(mapped))

    def test_timeout(self) -> None:
        mapped = _map_network_error(urllib.error.URLError(TimeoutError()), 3)
        self.assertIn("3s", str(mapped))

    def test_connection_refused(self) -> None:
        mapped = _map_network_error(urllib.error.URLError(ConnectionRefusedError()), 3)
        self.assertIn("rechazó", str(mapped))

    def test_error_generico_es_redactado(self) -> None:
        mapped = _map_network_error(
            urllib.error.URLError("fallo password=supersecreta en el proxy"), 3
        )
        self.assertNotIn("supersecreta", str(mapped))
        self.assertIsInstance(mapped, NetworkError)

    def test_safe_response_text_usa_charset(self) -> None:
        resp = SafeResponse(
            url="u",
            final_url="u",
            status=200,
            reason="OK",
            headers={"content-type": "text/plain; charset=latin-1"},
            body="ñ".encode("latin-1"),
        )
        self.assertEqual(resp.text, "ñ")

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
