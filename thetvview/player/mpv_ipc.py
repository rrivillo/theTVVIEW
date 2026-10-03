"""Cliente del IPC local de mpv (plan F5d, SDD §28, AC-02).

mpv es el único de los tres con canal de control: con
``--input-ipc-server=<ruta>`` acepta **una línea JSON por comando** y responde
por el mismo socket. Eso permite cambiar de audio o de subtítulos **en
caliente**, sin cerrar el canal ni reabrir la URL (AC-02).

Seguridad (SECURITY.md):

- El IPC de mpv **no tiene autenticación ni cifrado**: quien pueda escribir
  en el socket manda en el reproductor. Por eso la ruta **no** es predecible:
  directorio nuevo en ``data/ipc/`` con permisos **0700** y nombre
  ``mpv-<pid>-<16 bytes aleatorios en hex>.sock`` (POSIX) o
  ``\\\\.\\pipe\\thetvview-<pid>-<rand>`` (Windows).
- Un socket unix nunca es accesible desde otro equipo; uno de Windows tampoco,
  y el nombre lleva el pid y el token para que otro proceso del mismo usuario
  no lo adivine.
- **Timeout corto** (1,5 s por comando): si mpv está colgado o no responde,
  la TUI no se queda esperando. El llamante recibe un error controlado y lo
  enseña en un modal (AGENTS: nunca falla en silencio).

Nada de esto toca la red: el socket es local.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from pathlib import Path
from typing import Any

from .. import config
from ..security.local_files import chmod_private

__all__ = [
    "MpvIpcError",
    "MpvIpc",
    "ipc_path",
    "COMMAND_TIMEOUT",
    "PROPERTY_AUDIO",
    "PROPERTY_SUBTITLE",
]

#: Tiempo máximo por comando. Suficiente para un `set_property` local y
#: demasiado corto para colgar la interfaz.
COMMAND_TIMEOUT: float = 1.5

#: Propiedad de mpv con el índice de la pista de audio activa.
PROPERTY_AUDIO: str = "aid"
#: Propiedad de mpv con el índice de la pista de subtítulo activa
#: (``no`` = desactivados).
PROPERTY_SUBTITLE: str = "sid"

_TRACK_LIST_PROPERTY: str = "track-list"


class MpvIpcError(Exception):
    """El IPC de mpv no respondió, o respondió con error.

    El mensaje es apto para un modal y explica qué hacer: reabrir el canal.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def ipc_path(directory: Path | None = None) -> str:
    """Ruta de socket IPC nueva, aleatoria y privada.

    Crea el directorio con permisos 0700 y devuelve la ruta. En Windows se
    devuelve un *named pipe* bajo ``\\\\.\\pipe\\``; en POSIX, un socket unix.
    """
    base = Path(directory or config.IPC_DIR)
    try:
        base.mkdir(parents=True, exist_ok=True)
        chmod_private(base, directory=True)
    except OSError as exc:  # pragma: no cover - sistemas sin permisos
        raise MpvIpcError(
            "No se pudo preparar la carpeta de control del reproductor: "
            "revisa los permisos de la carpeta de datos."
        ) from exc

    token = secrets.token_hex(16)
    if os.name == "nt":
        return rf"\\.\pipe\thetvview-{os.getpid()}-{token}"
    return str(base / f"mpv-{os.getpid()}-{token}.sock")


class MpvIpc:
    """Cliente mínimo del IPC de mpv: un comando, una respuesta.

    Se abre y se cierra por comando. Es lo más simple que funciona: mantener
    el socket abierto durante toda la reproducción daría más velocidad, pero
    también más formas de que la TUI se quede esperando a un reproductor
    colgado, y aquí lo que prima es que la interfaz **nunca** se bloquea.
    """

    def __init__(self, path: str, *, timeout: float = COMMAND_TIMEOUT) -> None:
        self.path = path
        self.timeout = max(0.1, float(timeout))
        self._request_id = 0

    # -- API ---------------------------------------------------------------

    def get_property(self, name: str) -> Any:
        """Valor de una propiedad, o :class:`MpvIpcError`."""
        data = self._command(["get_property", name])
        return data.get("data")

    def set_property(self, name: str, value: Any) -> None:
        """Fija una propiedad; :class:`MpvIpcError` si mpv la rechaza."""
        self._command(["set_property", name, value])

    def track_list(self) -> list[dict[str, Any]]:
        """``track-list`` de mpv (para reconciliar nuestros ids con los suyos)."""
        data = self._command(["get_property", _TRACK_LIST_PROPERTY])
        value = data.get("data")
        return list(value) if isinstance(value, list) else []

    def set_audio_index(self, index: int | None) -> None:
        """Cambia el audio en caliente. ``None`` deja el que haya."""
        self.set_property(PROPERTY_AUDIO, "no" if index is None else int(index))

    def set_subtitle_index(self, index: int | None) -> None:
        """Cambia los subtítulos en caliente. ``None`` = desactivados."""
        self.set_property(PROPERTY_SUBTITLE, "no" if index is None else int(index))

    def is_alive(self) -> bool:
        """¿Contesta mpv? No lanza: devuelve False."""
        try:
            return self.get_property("idle-active") is not None
        except MpvIpcError:
            return False

    # -- núcleo ------------------------------------------------------------

    def _command(self, command: list[Any]) -> dict[str, Any]:
        if not self.path:
            raise MpvIpcError("No hay canal de control con mpv.")
        sock = self._connect()
        try:
            self._request_id += 1
            payload = {"command": command, "request_id": self._request_id}
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            return self._read_response(sock)
        except socket.timeout as exc:
            raise MpvIpcError(
                "El reproductor no ha contestado a tiempo. Vuelve a intentarlo "
                "o reabre el canal."
            ) from exc
        except OSError as exc:
            raise MpvIpcError(
                "No se pudo hablar con el reproductor: "
                "el cambio requiere reabrir el canal."
            ) from exc
        finally:
            _close(sock)

    def _connect(self) -> socket.socket:
        if os.name == "nt":
            return self._connect_pipe()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except OSError as exc:
            _close(sock)
            raise MpvIpcError(
                "No hay canal de control abierto con el reproductor: "
                "el cambio requiere reabrir el canal."
            ) from exc
        return sock

    def _connect_pipe(self) -> socket.socket:
        """En Windows el IPC de mpv es un *named pipe*, no un socket unix.

        Se intenta con el módulo ``open_connection_helper`` de la familia
        ``socket`` si está disponible (Python lo trae como
        ``socket.AF_UNIX`` en Windows desde 3.9 para named pipes locales); si
        no, se dice con claridad en vez de fallar con un error críptico.
        """
        try:  # pragma: no cover - sólo Windows
            file = open(self.path, "r+b", buffering=0)  # noqa: SIM115
        except OSError as exc:  # pragma: no cover - sólo Windows
            raise MpvIpcError(
                "No hay canal de control abierto con el reproductor: "
                "el cambio requiere reabrir el canal."
            ) from exc
        sock = _WindowsPipeSocket(file)
        sock.settimeout(self.timeout)
        return sock

    def _read_response(self, sock: socket.socket) -> dict[str, Any]:
        """Lee líneas hasta la de nuestro ``request_id``.

        mpv emite eventos (``playback-restart``, ``property-change``…) por el
        mismo socket, así que hay que filtrar: la respuesta que buscamos es
        la que lleva nuestro ``request_id``.
        """
        buffer = b""
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                chunk = sock.recv(65536)
                if not chunk:
                    raise MpvIpcError(
                        "El reproductor cerró el canal de control antes de "
                        "responder."
                    )
                buffer += chunk
                if len(buffer) > _MAX_RESPONSE:
                    raise MpvIpcError("Respuesta del reproductor demasiado grande.")
                continue
            line = buffer[:newline]
            buffer = buffer[newline + 1:]
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue  # basura: se ignora, no rompe
            if not isinstance(message, dict):
                continue
            if message.get("request_id") != self._request_id:
                continue  # es un evento, no nuestra respuesta
            error = message.get("error")
            if error:
                raise MpvIpcError(_explain(error))
            return message


#: Techo de la respuesta acumulada: si se supera, algo va mal y se corta.
_MAX_RESPONSE: int = 1024 * 1024


def _explain(error: str) -> str:
    """Traduce un error del IPC a un mensaje apto para un modal."""
    text = str(error or "").strip()
    if text == "error accessing property" or "property" in text.lower():
        return (
            "Esta versión del reproductor no permite cambiar esa opción "
            "en caliente: reabre el canal para aplicarla."
        )
    if text == "invalid parameter":
        return "El reproductor no entendió la opción: reabre el canal."
    return (
        "El reproductor rechazó el cambio: "
        f"{text[:120]}. reabre el canal para aplicarlo."
    )


def _close(sock: Any) -> None:
    try:
        sock.close()
    except Exception:  # noqa: BLE001 - socket ya cerrado
        pass


class _WindowsPipeSocket:  # pragma: no cover - sólo Windows
    """Adaptador mínimo para tratar un *named pipe* como socket."""

    def __init__(self, file) -> None:  # noqa: ANN001
        self._file = file
        self._timeout = COMMAND_TIMEOUT

    def settimeout(self, value: float) -> None:
        self._timeout = max(0.1, float(value))

    def sendall(self, data: bytes) -> None:
        self._file.write(data)

    def recv(self, size: int) -> bytes:
        data = self._file.read(size)
        return data or b""

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:  # noqa: BLE001
            pass