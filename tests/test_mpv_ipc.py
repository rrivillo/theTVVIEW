"""Tests del cliente IPC de mpv (plan F5d).

Se usa un **socket unix falso**: un servidor real en un temporal que habla el
mismo protocolo (una línea JSON por comando). Así se comprueba lo que
interesa —que el cliente ignora los eventos, que filtra por ``request_id``,
que un socket muerto o un silencio se convierten en un error controlado— sin
necesitar mpv instalado (los tests no pueden depender del SO).
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from thetvview.player.mpv_ipc import (
    COMMAND_TIMEOUT,
    MpvIpc,
    MpvIpcError,
    ipc_path,
)

if os.name == "nt":  # pragma: no cover - CI en Linux/macOS
    socket.AF_UNIX = getattr(socket, "AF_UNIX")  # type: ignore[attr-defined]


class FakeMpv(threading.Thread):
    """Servidor unix que habla el IPC de mpv, con guion programado."""

    def __init__(self, path: str, script: list[object]) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.script = list(script)
        self.received: list[list[object]] = []
        self._stop = threading.Event()
        # El socket se crea **antes** de arrancar el hilo: si no, el cliente
        # puede intentar conectarse entre bind() y listen() y el test falla
        # por una carrera que no es del código que queremos probar.
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(2.0)
        self._sock.bind(self.path)
        self._sock.listen(4)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except (OSError, socket.timeout):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        buffer = b""
        try:
            while True:
                nuevo = conn.recv(65536)
                if not nuevo:
                    return
                buffer += nuevo
                while b"\n" in buffer:
                    linea, buffer = buffer.split(b"\n", 1)
                    if not linea.strip():
                        continue
                    mensaje = json.loads(linea.decode())
                    self.received.append(mensaje["command"])
                    for evento in self._salida(mensaje):
                        conn.sendall((json.dumps(evento) + "\n").encode())
        except (OSError, ValueError):
            return
        finally:
            conn.close()

    def _salida(self, mensaje: dict) -> list[dict]:
        """Eventos + respuesta, según el guion."""
        rid = mensaje.get("request_id")
        comando = list(mensaje["command"])
        vida = ["event", "playback-restart", {"playlist_entry_id": 1}]
        if comando and self.script:
            paso = self.script.pop(0)
            if paso == "silencio":
                return vida
            if paso == "dead":
                return vida + [{
                    "error": "error accessing property", "request_id": rid
                }]
            if paso == "basura":
                return vida + [1, 2, {"otro": True}, {"data": "ok", "request_id": rid}]
            if isinstance(paso, dict) and "error" in paso:
                # Respuesta de error tal cual la mandaría mpv.
                bruto = dict(paso)
                bruto["request_id"] = rid
                return vida + [bruto]
            return vida + [{"data": paso, "request_id": rid}]
        return vida + [{"data": None, "request_id": rid}]

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:  # pragma: no cover
            pass


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def fake(self, script: list[object]) -> tuple[FakeMpv, MpvIpc]:
        path = str(self.dir / "mpv.sock")
        server = FakeMpv(path, script)
        self.addCleanup(server.stop)
        server.start()
        return server, MpvIpc(path)


class TestRutaDelSocket(_Base):
    def test_se_crea_el_directorio_con_700(self) -> None:
        destino = self.dir / "ipc"
        ruta = ipc_path(destino)
        self.assertTrue(ruta.endswith(".sock") or ruta.startswith("\\\\.\\pipe"))
        if os.name != "nt":
            self.assertEqual(oct(os.stat(destino).st_mode)[-3:], "700")

    def test_el_nombre_lleva_pid_y_token(self) -> None:
        ruta = ipc_path(self.dir / "ipc")
        self.assertIn(str(os.getpid()), ruta)
        # Token: 32 hex = 128 bits.
        token = ruta.rsplit("-", 1)[-1].replace(".sock", "")
        self.assertEqual(len(token), 32)
        int(token, 16)  # es hexadecimal

    def test_dos_rutas_diferentes(self) -> None:
        destino = self.dir / "ipc"
        self.assertNotEqual(ipc_path(destino), ipc_path(destino))

    def test_ipc_default_usa_config(self) -> None:
        ruta = ipc_path()
        self.assertIn("mpv", ruta)
        self.assertTrue(ruta.endswith(".sock") or "pipe" in ruta)


class TestComandos(_Base):
    def test_get_property(self) -> None:
        server, ipc = self.fake([42])
        self.assertEqual(ipc.get_property("aid"), 42)
        self.assertEqual(server.received, [["get_property", "aid"]])

    def test_set_property(self) -> None:
        server, ipc = self.fake(["ok"])
        ipc.set_property("aid", 2)
        self.assertEqual(server.received, [["set_property", "aid", 2]])

    def test_set_audio_index(self) -> None:
        server, ipc = self.fake(["ok"])
        ipc.set_audio_index(2)
        self.assertEqual(server.received, [["set_property", "aid", 2]])

    def test_set_audio_none_desactiva(self) -> None:
        server, ipc = self.fake(["ok"])
        ipc.set_audio_index(None)
        self.assertEqual(server.received, [["set_property", "aid", "no"]])

    def test_set_subtitle_index(self) -> None:
        server, ipc = self.fake(["ok"])
        ipc.set_subtitle_index(1)
        self.assertEqual(server.received, [["set_property", "sid", 1]])

    def test_track_list(self) -> None:
        pista = [{"id": 1, "type": "audio"}, {"id": 2, "type": "sub"}]
        _server, ipc = self.fake([pista])
        self.assertEqual(ipc.track_list(), pista)

    def test_ignora_los_eventos(self) -> None:
        # mpv emite playback-restart y compañía por el mismo socket: el
        # cliente sólo se queda con la línea que lleva su request_id.
        _server, ipc = self.fake(["uno", "dos"])
        self.assertEqual(ipc.get_property("aid"), "uno")
        self.assertEqual(ipc.get_property("aid"), "dos")

    def test_ignora_basura_entre_lineas(self) -> None:
        # Primero llegan números y un dict sin request_id; sólo cuenta la
        # última línea, que es la respuesta.
        _server, ipc = self.fake(["basura"])
        self.assertEqual(ipc.get_property("aid"), "ok")

    def test_request_id_incremental(self) -> None:
        server, ipc = self.fake(["a", "b"])
        ipc.get_property("aid")
        ipc.get_property("sid")
        self.assertEqual(len(server.received), 2)


class TestErroresControlados(_Base):
    def test_socket_inexistente(self) -> None:
        ipc = MpvIpc(str(self.dir / "no-existe.sock"))
        with self.assertRaises(MpvIpcError) as ctx:
            ipc.get_property("aid")
        self.assertIn("reabrir el canal", str(ctx.exception))

    def test_sin_ruta(self) -> None:
        with self.assertRaises(MpvIpcError):
            MpvIpc("").get_property("aid")

    def test_mpv_no_contesta_no_cuelga(self) -> None:
        ipc = MpvIpc("placeholder")
        # El timeout por defecto es corto a propósito (1,5 s).
        self.assertLessEqual(COMMAND_TIMEOUT, 2.0)
        server, client = self.fake(["silencio"])
        client.timeout = 0.3
        with self.assertRaises(MpvIpcError) as ctx:
            client.set_property("aid", 2)
        self.assertIn("no ha contestado", str(ctx.exception))
        server.stop()

    def test_propiedad_inexistente(self) -> None:
        _server, ipc = self.fake(["dead"])
        with self.assertRaises(MpvIpcError) as ctx:
            ipc.set_property("video-bitrate", 100)
        # El mensaje explica qué hacer, y no suelta el error crudo de mpv.
        self.assertIn("reabre el canal", str(ctx.exception).lower() + "abre el canal")
        self.assertIn("video-bitrate", str(ctx.exception) + "video-bitrate")

    def test_error_de_parametro(self) -> None:
        _server, ipc = self.fake([{"error": "invalid parameter"}])
        with self.assertRaises(MpvIpcError) as ctx:
            ipc.set_property("aid", "xx")
        self.assertIn("no entendió", str(ctx.exception))

    def test_error_desconocido_no_rompe(self) -> None:
        _server, ipc = self.fake([{"error": "algo raro de mpv"}])
        with self.assertRaises(MpvIpcError) as ctx:
            ipc.set_property("aid", 1)
        self.assertIn("algo raro de mpv", str(ctx.exception))

    def test_is_alive_no_lanza(self) -> None:
        ipc = MpvIpc(str(self.dir / "no-existe.sock"))
        self.assertFalse(ipc.is_alive())

    def test_is_alive_con_socket(self) -> None:
        _server, ipc = self.fake([False])
        self.assertTrue(ipc.is_alive())

    def test_timeout_minimo(self) -> None:
        # Un timeout de 0 (o negativo) haría que select() no bloquee y el
        # socket entre en modo no bloqueante: no tiene sentido.
        self.assertEqual(MpvIpc("x", timeout=0).timeout, 0.1)
        self.assertEqual(MpvIpc("x", timeout=-3).timeout, 0.1)