"""`security-check` — los 12 checks del SDD §45 (+ auditoría de dependencias).

Se ejecuta de dos formas:

CLI / CI (exit code != 0 si algo falla)::

    python -m thetvview.security.check
    python -m thetvview.security.check --json
    python -m thetvview.security.check --only ssrf_policy,redirect_policy

TUI (tecla ``!``): la app abre **siempre** un modal con el resultado
(no negociable #1) — ver :meth:`thetvview.ui.app.App.run_security_check`.

Diseño: cada check es una función pura, sin red real ni ficheros fuera de
un temporal. Si algo no se puede comprobar en este entorno, **falla**
(no se degrada en silencio): un check verde debe significar "verificado".

El check 12 (``catchup_capability_gate``) no es de los del SDD §45: viene
del SDD de catch-up, y verifica su invariante funcional — sólo la
capacidad declarada por el proveedor habilita la reproducción de archivo,
y no se sondean endpoints para descubrirla.

Sustituye al ``pip-audit`` del SDD §52: con 0 dependencias pip no hay
vulnerabilidad de cadena de suministro que auditar, así que comprobamos
que ``requirements.txt`` siga vacío y que todos los ``import`` sean stdlib.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import ssl
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "CheckFailure",
    "CheckResult",
    "CHECKS",
    "run_checks",
    "format_report",
    "all_ok",
    "main",
]

PACKAGE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_DIR.parent

#: Marcadores de informe del SDD §45.
PASSED_MARK = "[\u2713]"
FAILED_MARK = "[\u2717]"
FAILED_BANNER = "SECURITY CHECK FAILED"


class CheckFailure(AssertionError):
    """Un check no se pudo verificar (o verificó algo prohibido)."""


@dataclass(frozen=True)
class CheckResult:
    """Resultado de una comprobación."""

    key: str
    label: str
    ok: bool
    detail: str = ""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(node: ast.Call) -> str:
    return ast.unparse(node.func)


#: Dueños de docstring en el AST. Un docstring no se ejecuta: documentar lo
#: prohibido no es infringirlo, así que los checks de AST los excluyen.
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstrings(tree: ast.AST) -> set[int]:
    """ids de los nodos ``Constant`` que son docstrings de módulo/clase/función."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_OWNERS) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                ids.add(id(first.value))
    return ids


# ---------------------------------------------------------------------------
# 1. Secret redaction
# ---------------------------------------------------------------------------


def check_secret_redaction() -> str:
    """SDD §16 / SEC-001: ningún secreto sobrevive en texto de error."""
    from .redaction import REDACTED, SecretStr, contains_secret, redact_exception, redact_text

    samples = (
        "http://admin:hunter2@panel.example.com:8080/live/admin/hunter2/1.ts",
        "GET /login?user=admin&pass=hunter2 HTTP/1.1",
        "Authorization: Bearer abcdef0123456789abcdef",
        "url=rtsp://u:p@10.0.0.1/stream",
        "url=movie://admin:hunter2@host/vod/x.mp4",
        "password=hunter2",
        "Cookie: session=hunter2",
        "Set-Cookie: sid=hunter2",
    )
    for sample in samples:
        out = redact_text(sample)
        _require(
            "hunter2" not in out and "abcdef0123456789abcdef" not in out,
            f"redact_text deja escapar un secreto: {out!r}",
        )
        _require(REDACTED in out, f"redact_text no marca {sample!r} como redactado")
    _require(contains_secret("password=hunter2"), "contains_secret no ve pass=hunter2")
    _require(not contains_secret("canal favorito: Noticias"),
             "contains_secret se dispara con texto normal")
    for exposed in (str(SecretStr("hunter2")), repr(SecretStr("hunter2")),
                    f"{SecretStr('hunter2')}", format(SecretStr("hunter2"), ">8")):
        _require("hunter2" not in exposed, f"SecretStr filtra: {exposed!r}")
    _require(bool(SecretStr("hunter2")), "SecretStr('hunter2') no es truthy")
    _require(str(SecretStr("hunter2").reveal()) == "hunter2", "reveal() roto")
    exc = redact_exception(RuntimeError("fallo con pass=hunter2"))
    _require("hunter2" not in exc, f"redact_exception filtra: {exc!r}")
    return f"{len(samples)} patrones + SecretStr + excepciones ({REDACTED})"


# ---------------------------------------------------------------------------
# 2. HTTPS certificate verification
# ---------------------------------------------------------------------------


def check_tls_verification() -> str:
    """SDD §12/§13: el único contexto TLS exige certificado y hostname."""
    from .safe_http import _ssl_context

    ctx = _ssl_context()
    _require(ctx.check_hostname is True, "SSLContext.check_hostname=False")
    _require(ctx.verify_mode == ssl.CERT_REQUIRED, "SSLContext no exige CERT_REQUIRED")
    min_v = getattr(ctx, "minimum_version", None)
    if min_v is not None:
        _require(
            min_v >= ssl.TLSVersion.TLSv1_2,
            f"TLS mínimo {min_v} < 1.2",
        )
    # El cliente usa http.client con el contexto estricto compartido.
    from .safe_http import SafeHttpClient, _PinnedHTTPSConnection

    client = SafeHttpClient()
    _require(
        _PinnedHTTPSConnection is not None,
        "SafeHttpClient sin _PinnedHTTPSConnection",
    )
    # Verificar que el contexto estricto se usa en la conexión HTTPS
    conn = _PinnedHTTPSConnection("example.com", 443, context=ctx)
    _require(
        conn._context is ctx,
        "_PinnedHTTPSConnection no usa el contexto estricto compartido",
    )
    return "CERT_REQUIRED + check_hostname + TLS>=1.2"


# ---------------------------------------------------------------------------
# 3. SSRF policy
# ---------------------------------------------------------------------------


def check_ssrf_policy() -> str:
    """SDD §11 / SEC-002 y SEC-005: destinos internos y esquemas raros."""
    from .errors import InvalidUrlError, SSRFBlockedError
    from .ssrf import check_url

    blocked = (
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/api",
        "http://localhost/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://192.168.1.10/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "https://instance-data/",
        "http://0177.0.0.1/",
        "http://2130706433/",
    )
    for url in blocked:
        try:
            check_url(url, resolve=False)
        except SSRFBlockedError:
            continue
        except InvalidUrlError as exc:
            raise CheckFailure(f"SSRF devolvió el error equivocado para {url}: {exc}") from exc
        raise CheckFailure(f"SSRF no bloquea {url}")

    for url in ("file:///etc/passwd", "gopher://127.0.0.1/_", "javascript:alert(1)",
                "ftp://host/x", "dict://127.0.0.1:11211/stat"):
        try:
            check_url(url, resolve=False)
        except (InvalidUrlError, SSRFBlockedError):
            continue
        raise CheckFailure(f"esquema no permitido aceptado: {url}")

    for url in ("http://example.com/a.m3u", "https://cdn.example.org/x.ts"):
        check_url(url, resolve=False)

    from .ssrf import check_host

    for host in ("127.0.0.1", "::1", "localhost", "169.254.169.254"):
        try:
            check_host(host, allow_private=False)
        except SSRFBlockedError:
            continue
        raise CheckFailure(f"check_host no bloquea {host}")
    return f"{len(blocked)} destinos internos + {5} esquemas rechazados"


# ---------------------------------------------------------------------------
# 4. Redirect policy
# ---------------------------------------------------------------------------


def _fake_opened(status: int, location: str, final_url: str) -> object:
    from .safe_http import _Opened

    opened = _Opened.__new__(_Opened)
    opened.fp = None
    opened.status = status
    opened.reason = "Redirect"
    opened.headers = {"location": location} if location else {}
    opened.final_url = final_url
    opened.redirects = 0
    return opened


def check_redirect_policy() -> str:
    """SDD §13 / SEC-003: sin auto-redirect, tope 3 y sin https→http."""
    from .errors import NetworkError, TLSValidationError
    from .limits import get_limits
    from .safe_http import SafeHttpClient

    client = SafeHttpClient(allow_private=True)
    # http.client no sigue redirects automáticamente: se validan a mano.
    _require(
        not hasattr(client, "_opener"),
        "SafeHttpClient sigue usando urllib (debe usar http.client)",
    )
    cap = int(get_limits().max_redirects)
    _require(0 < cap <= 3, f"max_redirects={cap} fuera de 1..3")

    # (a) tope de saltos: siempre 301 -> NetworkError con mensaje amable.
    #     Arranca en http para aislar este caso del guardia de downgrade.
    client._attempt = lambda *a, **k: _fake_opened(  # type: ignore[assignment]
        301, "http://127.0.0.1/loop", "http://127.0.0.1/loop"
    )
    try:
        client._open_validated("http://127.0.0.1/start", method="GET",
                               headers={}, timeout=5.0, retry=False)
    except NetworkError as exc:
        _require("demasiadas redirecciones" in str(exc), f"error raro: {exc}")
    else:
        raise CheckFailure("no corta una cadena infinita de redirects")

    # (b) downgrade https->http en el 2º salto.
    seq = iter((
        _fake_opened(301, "http://127.0.0.1/next", "https://127.0.0.1/start"),
        _fake_opened(200, "", "http://127.0.0.1/next"),
    ))
    client._attempt = lambda *a, **k: next(seq)  # type: ignore[assignment]
    try:
        client._open_validated("https://127.0.0.1/start", method="GET",
                               headers={}, timeout=5.0, retry=False)
    except TLSValidationError:
        pass
    except NetworkError as exc:
        raise CheckFailure(f"debería bloquear https->http, llegó: {exc}") from exc
    else:
        raise CheckFailure("permite el downgrade https->http (SEC-003)")

    # (c) el destino de cada salto vuelve a pasar por SSRF.
    from .errors import SSRFBlockedError
    from .ssrf import check_url

    try:
        check_url("http://127.0.0.1/after-redirect", resolve=False)
    except SSRFBlockedError:
        pass
    else:
        raise CheckFailure("el destino de un redirect no se revalida")
    return f"sin auto-redirect, tope={cap}, downgrade bloqueado, destino revalidado"


# ---------------------------------------------------------------------------
# 5. Response limits
# ---------------------------------------------------------------------------


def check_response_limits() -> str:
    """SDD §19/§21 / SEC-004: topes de bytes, tiempo y entradas."""
    from .limits import get_limits, human_size
    from .safe_http import SafeHttpClient

    lim = get_limits()
    for name in ("max_response_bytes", "max_file_bytes", "max_entries",
                 "max_xml_nodes", "max_xml_depth"):
        _require(int(getattr(lim, name)) > 0, f"{name} <= 0")
    _require(lim.total_timeout > 0, "total_timeout <= 0")
    _require(lim.max_response_bytes <= 512 * 1024 * 1024, "max_response_bytes > 512MB")
    _require(lim.max_entries <= 2_000_000, "max_entries > 2M")

    client = SafeHttpClient(max_bytes=16)
    _require(client._cap(None) == 16, "SafeHttpClient ignora max_bytes")
    _require(int(SafeHttpClient()._cap(None) or 0) == lim.max_response_bytes,
             "SafeHttpClient no cae en limits.max_response_bytes")

    from .local_files import read_limited_bytes as _rlb

    with tempfile.TemporaryDirectory() as tmp:
        big = Path(tmp) / "big.bin"
        big.write_bytes(b"x" * 4096)
        try:
            _rlb(big, max_bytes=16)
        except OSError as exc:
            _require("límite" in str(exc), f"error no apto para usuario: {exc}")
        else:
            raise CheckFailure("read_limited_bytes no aborta al superar el límite")
        ok = _rlb(big, max_bytes=8192)
        _require(len(ok) == 4096, "read_limited_bytes trunca ficheros dentro del tope")
    return f"respuesta {human_size(lim.max_response_bytes)}, fichero " \
           f"{human_size(lim.max_file_bytes)}, {lim.max_entries:,} entradas"


# ---------------------------------------------------------------------------
# 6. XML safe parsing
# ---------------------------------------------------------------------------


def check_xml_safe() -> str:
    """SDD §38/§39 / SEC-008: sin XXE, sin DTD, sin árbol infinito."""
    from .errors import ParseError, ResponseTooLargeError
    from .xml_safe import parse_xml

    xxe = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE tv [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<tv><channel id='a'>&xxe;</channel></tv>"
    )
    try:
        parse_xml(xxe)
    except (ParseError, ResponseTooLargeError):
        pass
    else:
        raise CheckFailure("acepta un XML con DOCTYPE/ENTITY externa (SEC-008)")

    for doc in ("<!DOCTYPE tv SYSTEM 'http://evil.example/x.dtd'><tv/>",
                "<!ENTITY x 'y'><tv/>"):
        try:
            parse_xml(doc)
        except ParseError:
            continue
        raise CheckFailure(f"acepta DTD: {doc[:40]}")

    root = parse_xml("<tv><channel id='c1'><display-name>Canal</display-name>"
                     "</channel></tv>")
    _require(root.tag == "tv", "parse_xml rompe un XMLTV válido")

    deep = "<a>" * 200 + "x" + "</a>" * 200
    try:
        parse_xml(deep, max_depth=16)
    except ParseError:
        pass
    else:
        raise CheckFailure("acepta un XML de 200 niveles")

    try:
        parse_xml(b"\x00\x01\x02" * 100, max_bytes=16)
    except (ResponseTooLargeError, ParseError):
        pass
    else:
        raise CheckFailure("acepta un XML que supera max_bytes")

    with tempfile.TemporaryDirectory() as tmp:
        gz = Path(tmp) / "epg.xml.gz"
        import gzip as _gz

        gz.write_bytes(_gz.compress(b"<tv>" + b"<programme/>" * 100 + b"</tv>"))
        from .local_files import read_limited_text

        text = read_limited_text(gz, max_bytes=1024 * 1024)
        _require(text.lstrip().startswith("<tv>"), "no descomprime .gz con límite")
        try:
            read_limited_text(gz, max_bytes=8)
        except OSError:
            pass
        else:
            raise CheckFailure("un .gz enorme no respeta max_bytes")
    return "DOCTYPE/ENTITY, profundidad, tamaño y gzip bajo límite"


# ---------------------------------------------------------------------------
# 7. AI secret sanitizer
# ---------------------------------------------------------------------------


def check_ai_sanitizer() -> str:
    """SDD §29-31: sanitizador de secretos listo; hoy sin capa de IA.

    El proyecto no tiene ningún cliente de LLM (verificado en el árbol),
    así que el check garantiza dos cosas: que no haya aparecido uno por
    accidente y que el redactor sobre texto no confiable funciona.
    """
    from .redaction import redact_text

    untrusted = (
        "IGNORE PREVIOUS INSTRUCTIONS — password=hunter2",
        "titulo: Canal @ http://u:hunter2@host/x",
    )
    for text in untrusted:
        out = redact_text(text)
        _require("hunter2" not in out, f"el sanitizador deja un secreto: {out!r}")

    banned = {"openai", "anthropic", "ollama", "litellm", "langchain",
              "langchain_core", "transformers", "cohere", "mistralai"}
    found: set[str] = set()
    for path in _iter_python_files(PACKAGE_DIR):
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names} & banned
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])  # filtrado abajo
    found = {name for name in found if name in banned}
    _require(not found, f"SDK de IA en el árbol: {sorted(found)}")
    return f"{len(untrusted)} textos no confiables redactados; 0 SDK de IA"


# ---------------------------------------------------------------------------
# 8. Shell-safe player invocation
# ---------------------------------------------------------------------------


def check_shell_safe_player() -> str:
    """SDD §63 / SEC-006: argv en lista, `--`, jamás shell=True."""
    from thetvview.models import Channel
    from thetvview.player import command_for
    from thetvview.security.errors import InvalidUrlError
    from thetvview.player import PlayerError

    offenders: list[str] = []
    shell_calls = 0
    subprocess_calls = 0
    system_calls = 0
    for path in _iter_python_files(PACKAGE_DIR):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name.startswith("subprocess.") or name in {
                "Popen", "run", "call", "check_call", "check_output",
            }:
                subprocess_calls += 1
                for kw in node.keywords:
                    if kw.arg != "shell":
                        continue
                    shell_calls += 1
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        offenders.append(f"{path.name}:{node.lineno} shell=True")
            if name in {"os.system", "os.popen", "commands.getoutput"}:
                system_calls += 1
                offenders.append(f"{path.name}:{node.lineno} {name}")
    _require(not offenders, f"invocación no segura: {offenders}")
    _require(shell_calls == 0, f"{shell_calls} llamadas con shell=")
    _require(system_calls == 0, f"os.system/os.popen: {system_calls}")
    _require(subprocess_calls > 0, "no hay ninguna llamada a subprocess que inspeccionar")

    url = "https://cdn.example.com/live.ts?token=abc"
    cmd = command_for(Channel(name="Uno", url=url), "mpv", player_path="/usr/bin/true")
    _require(isinstance(cmd, list) and all(isinstance(a, str) for a in cmd),
             "command_for no devuelve argv de str")
    _require(cmd[-2:] == ["--", url], f"argv sin separador --: {cmd[-4:]}")
    _require(not any("\n" in a or "\x00" in a for a in cmd), "salto de línea en argv")

    # URL hostil: no puede convertirse en opción ni en comando.
    hostile = Channel(name="x; rm -rf /", url="https://host/a.ts?q=1;$(id)")
    hcmd = command_for(hostile, "mpv", player_path="/usr/bin/true")
    _require(hcmd[-2:] == ["--", hostile.url], "la URL hostil altera el argv")
    _require(hcmd[0] == "/usr/bin/true", "el binario no es el resuelto")

    for bad in ("file:///etc/passwd", "javascript:alert(1)", "-rf", "data:text/html,x"):
        try:
            command_for(Channel(name="x", url=bad), "mpv",
                        player_path="/usr/bin/true")
        except (PlayerError, InvalidUrlError):
            continue
        raise CheckFailure(f"command_for acepta una URL de esquema prohibido: {bad}")
    return (f"{subprocess_calls} subprocess inspeccionados, shell=True={shell_calls}, "
            f"os.system={system_calls}, argv termina en --")


# ---------------------------------------------------------------------------
# 9-bis. Ampliación de SEC-001: credenciales en los transportes nuevos
# ---------------------------------------------------------------------------


def check_opaque_refs_no_leak_credentials() -> str:
    """SEC-001 ampliado (SDD-M Fases 5-6): con RTMP/RTSP/UDP abiertos, la
    superficie donde una credencial puede colarse ha crecido.

    Antes bastaba comprobar Xtream. Ahora hay un segundo mecanismo de
    referencia opaca (``ipcam://`` para cámaras RTSP) y cuatro transportes
    nuevos. Lo que se comprueba es lo mismo en todos:

    - una URL con **userinfo** nunca llega a ``data/``;
    - una URL con **password/token** nunca llega a un ``argv`` visible ni a un
      ``Modal``;
    - y la referencia opaca que queda en el canal **no** contiene ni la
      credencial ni la dirección del dispositivo.

    El último punto es el que no es obvio: si ``ipcam://`` llevara el host, un
    ``str(channel)`` bastaría para filtrar la red interna del usuario, y eso no
    es un secreto de la lista sino información que no debe salir de ella.
    """
    import tempfile

    from thetvview.cam_ref import PREFIX as CAM_PREFIX, build_cam_ref
    from thetvview.favorites import FavoritesManager
    from thetvview.m3u_parser import parse_text
    from thetvview.models import Channel
    from thetvview.player import command_for
    from thetvview.recents import RecentsManager
    from thetvview.security.errors import InvalidUrlError
    from thetvview.security.secrets import MemorySecretStore
    from thetvview.security.url_policy import PURPOSE_STREAM, validate_url
    from thetvview.stream_ref import is_opaque_ref, resolve_channel_url

    store = MemorySecretStore()
    camara = "rtsp://alice:P4ssw0rd@192.168.1.9:554/stream1"
    preparado = build_cam_ref("Camaras", camara)
    _require(preparado is not None, "una cámara con credenciales no se reconoce")
    if preparado is None:  # pragma: no cover - la línea de arriba ya lo dice
        raise CheckFailure("build_cam_ref devolvió None para una URL con credenciales")
    referencia, credenciales = preparado
    # El parser usa el almacén activo del proceso, así que es ahí donde hay que
    # registrar; el `store` de arriba sólo sirve para la resolución directa.
    from thetvview.cam_ref import registrar_camara

    _require(registrar_camara("Camaras", camara, store) == referencia,
             "registrar_camara no devuelve la referencia esperada")

    # 1) La referencia no lleva ni la clave ni el host.
    _require(is_opaque_ref(referencia), "la referencia de cámara no es opaca")
    _require(not is_opaque_ref(camara),
             "una URL cruda con credenciales no debe parecer opaca")
    for secreto in ("P4ssw0rd", "alice", "192.168.1.9", "stream1"):
        _require(secreto not in referencia,
                 f"la referencia opaca filtra {secreto!r}")
    _require(referencia.startswith(CAM_PREFIX), "prefijo de cámara inesperado")

    # 2) Resolverla sí devuelve la URL con credenciales (es su trabajo), y esa
    #    URL no pasa la política: es exactamente lo que impide que acabe en
    #    disco o en un argv por accidente.
    resuelta = resolve_channel_url(referencia, store)
    _require(resuelta == camara, "resolver la cámara no devuelve la URL original")
    try:
        validate_url(resuelta, PURPOSE_STREAM)
    except InvalidUrlError:
        pass
    else:
        raise CheckFailure("una URL de cámara con userinfo pasa la política de stream")

    # 3) El parser convierte la línea y la lista no queda con credenciales.
    texto = f'#EXTM3U\n#EXTINF:-1,Porton\n{camara}\n'
    lista = parse_text(texto, source="http://p/l.m3u", name="Camaras")
    _require(len(lista.channels) == 1, "la lista de cámaras no se parseó")
    canal = lista.channels[0]
    _require(canal.url == referencia, "el canal no lleva la referencia opaca")
    _require("P4ssw0rd" not in str(canal), "el Channel conserva la contraseña")

    # 4) Favoritos y recientes: el fichero escrito no puede contenerla.
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        fav = FavoritesManager(Path(tmp) / "favorites.json")
        fav.toggle(canal)
        texto_fav = (Path(tmp) / "favorites.json").read_text(encoding="utf-8")
        rec = RecentsManager(Path(tmp) / "recents.json")
        rec.push(canal, "mpv")
        texto_rec = (Path(tmp) / "recents.json").read_text(encoding="utf-8")
        for nombre, contenido in (("favorites.json", texto_fav),
                                  ("recents.json", texto_rec)):
            for secreto in ("P4ssw0rd", "192.168.1.9"):
                _require(secreto not in contenido,
                         f"{nombre} contiene {secreto!r}")

    # 5) Los transportes nuevos: un `argv` con credenciales embebidas tiene que
    #    salir con `PlayerError`, no construirse.
    from thetvview.player import PlayerError

    for url in (camara, "rtmp://u:P4ssw0rd@x/live/a",
                "udp://u:P4ssw0rd@239.0.0.1:5000"):
        try:
            command_for(Channel(name="x", url=url), "mpv", player_path="/usr/bin/true")
        except (PlayerError, InvalidUrlError):
            continue
        raise CheckFailure(f"una URL con credenciales llegó al argv: {url}")

    # 6) Y una URL **sin** credenciales sí se construye, detrás del `--`: la
    #    puerta está abierta para lo que se midió, no cerrada por principio.
    for url in ("rtmp://servidor.test/live/a", "rtsp://cam.local:554/stream",
                "udp://239.0.0.1:5000"):
        cmd = command_for(Channel(name="x", url=url), "mpv",
                          player_path="/usr/bin/true")
        _require(cmd[-2:] == ["--", url], f"argv sin -- para {url}")

    # 7) El texto que ve el usuario: la URL de cámara, redactada, no filtra.
    from thetvview.security.redaction import redact_text

    _require("P4ssw0rd" not in redact_text(resuelta),
             "redact_text no limpia la URL de cámara")
    _require(credenciales.password == "P4ssw0rd",
             "las credenciales de la cámara se han alterado")

    # 8) La excepción que sí deja pasar esa URL (`authorize_camara_url`) tiene
    #    que seguir siendo **estrecha**. Este es el único punto de todo el
    #    código por el que un userinfo llega a un argv, así que su perímetro
    #    se comprueba aquí y no sólo en los tests: si alguien la ensancha, el
    #    check se para.
    from thetvview.security.url_policy import authorize_camara_url

    partes = authorize_camara_url(camara)
    _require(partes.scheme == "rtsp" and partes.port == 554,
             "authorize_camara_url no reconstruye la URL de la cámara")
    # Puera única: `rtsp://` reconstruido por la app. Nada más.
    for url in ("http://alice:P4ssw0rd@proveedor.test/a.m3u8",
                "rtmp://alice:P4ssw0rd@x/live/a",
                "udp://alice:P4ssw0rd@239.0.0.1:5000"):
        try:
            authorize_camara_url(url)
        except InvalidUrlError:
            continue
        raise CheckFailure(f"authorize_camara_url acepta {url}")
    # Y no es una puerta trasera para saltarse el resto de la política:
    # espacios, controles, longitud, fragmento, puerto y «-» siguen rechazados
    # aunque la URL lleve credenciales.
    for url in ("rtsp://alice:P4ssw0rd@cam.local:99999/s",
                "rtsp://alice:pa ss@cam.local/s",
                "rtsp://alice:P4ss%00ss@cam.local/s",
                "rtsp://alice:P4ssw0rd@cam.local/s#frag",
                "--script=/tmp/evil.lua"):
        try:
            authorize_camara_url(url)
        except InvalidUrlError:
            continue
        raise CheckFailure(f"authorize_camara_url se salta la política con {url}")
    return (f"referencia {CAM_PREFIX} sin host ni clave; Xtream y cámara "
            f"verificadas en favorites/recents/argv")


# ---------------------------------------------------------------------------
# 9. Credential storage
# ---------------------------------------------------------------------------


def check_credential_storage() -> str:
    """SDD §15/§21: almacén de credenciales + permisos 0600/0700."""
    from .local_files import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE, chmod_private

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        secret = base / "secret.json"
        secret.write_text('{"password": "hunter2"}', encoding="utf-8")
        chmod_private(secret)
        if os.name == "posix":
            mode = secret.stat().st_mode & 0o777
            _require(mode == PRIVATE_FILE_MODE, f"fichero con permisos {oct(mode)}")

        nested = base / "data"
        nested.mkdir()
        (nested / "a.json").write_text("{}", encoding="utf-8")
        chmod_private(nested, directory=True)
        if os.name == "posix":
            dmode = nested.stat().st_mode & 0o777
            _require(dmode == PRIVATE_DIR_MODE, f"directorio con permisos {oct(dmode)}")

        from thetvview.playlist_manager import PlaylistManager

        pm = PlaylistManager(base / "playlists.json")
        pm.add("Prueba", "https://example.com/lista.m3u")
        js = base / "playlists.json"
        _require(js.exists(), "PlaylistManager no persiste playlists.json")
        if os.name == "posix":
            jmode = js.stat().st_mode & 0o777
            _require(jmode == PRIVATE_FILE_MODE, f"playlists.json con permisos {oct(jmode)}")

    from .secrets import MemorySecretStore

    store = MemorySecretStore()
    store.set_password("panel.example.com", "hunter2")
    _require(store.get_password("panel.example.com") == "hunter2",
             "SecretStore no devuelve la contraseña")
    _require("hunter2" not in repr(store), "SecretStore filtra por repr")
    store.delete_password("panel.example.com")
    _require(store.get_password("panel.example.com") is None,
             "SecretStore no borra la contraseña")

    from thetvview.security.redaction import contains_embedded_login
    from thetvview.stream_ref import StreamRef

    opaque = StreamRef("Mi @ Panel", "live", "7").to_opaque()
    _require(opaque.startswith("xtream://"), "la referencia no es opaca")
    _require(not contains_embedded_login(opaque),
             "una fuente con '@' emula userinfo en la referencia opaca")
    return f"fichero {oct(PRIVATE_FILE_MODE)}, dir {oct(PRIVATE_DIR_MODE)}, store OK"


# ---------------------------------------------------------------------------
# 10. No plaintext password logs
# ---------------------------------------------------------------------------


def check_no_password_logs() -> str:
    """SDD §16 / SEC-001: ni print, ni logging, ni repr con credenciales."""
    import re

    secret_name = re.compile(r"(?i)^(password|passwd|pwd|passphrase|secret|token)$")
    log_like = re.compile(r"(?i)(^|\.)(print|log|logger|logging|debug|info|warning|"
                          r"error|exception|critical|warn|trace|msg)$")
    offenders: list[str] = []
    calls = 0
    for path in _iter_python_files(PACKAGE_DIR):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if not (name == "print" or log_like.search(name)):
                continue
            calls += 1
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Name) and secret_name.match(sub.id):
                        offenders.append(f"{path.name}:{node.lineno} {name}({sub.id})")
                    elif isinstance(sub, ast.Attribute) and secret_name.match(sub.attr):
                        offenders.append(f"{path.name}:{node.lineno} {name}(.{sub.attr})")
    _require(not offenders, f"credencial en salida: {offenders}")

    # Ningún módulo debe importar logging para volcar peticiones/respuestas.
    from .redaction import redact_exception, redact_mapping

    exc = redact_exception(ValueError("login falló: user=admin pass=hunter2"))
    _require("hunter2" not in exc, f"excepción sin redactar: {exc}")
    data = redact_mapping({"password": "hunter2", "server": "https://h/", "nested":
                           {"token": "hunter2"}})
    _require("hunter2" not in repr(data), f"redact_mapping filtra: {data!r}")
    return f"{calls} llamadas a print/log inspeccionadas, 0 con credenciales"


# ---------------------------------------------------------------------------
# 11. Dependencias (sustituye a pip-audit, SDD §52)
# ---------------------------------------------------------------------------


def check_stdlib_dependencies() -> str:
    """0 dependencias pip: `requirements.txt` vacío y sólo stdlib."""
    req = REPO_ROOT / "requirements.txt"
    if req.exists():
        lines = [
            line.strip() for line in req.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        _require(not lines, f"requirements.txt no vacío: {lines}")

    stdlib = set(sys.stdlib_module_names) | {PACKAGE_DIR.name}
    top: set[str] = set()
    for path in _iter_python_files(PACKAGE_DIR):
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    top.add(mod)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top.add(node.module.split(".")[0])
    unknown = {mod for mod in top if mod not in stdlib}
    _require(not unknown, f"imports que no son del stdlib: {sorted(unknown)}")
    return f"{len(top)} módulos importados, todos stdlib; requirements.txt vacío"


# ---------------------------------------------------------------------------
# 12. Catch-up: sólo la capacidad declarada por el proveedor
# ---------------------------------------------------------------------------


def check_catchup_capability_gate() -> str:
    """SDD Catch-up §7/§10/§17/§21: la puerta de catch-up no es negociable.

    Verifica, no supone (H7). Si algo no se puede comprobar aquí, el check
    falla: un check verde significa "verificado".

    Se comprueban cuatro cosas:

    1. **Puerta cerrada por defecto.** Un canal sin declaración produce una
       petición histórica que lanza, no una URL. Y la ventana se respeta.
    2. **La referencia es opaca.** No lleva `password=` ni credenciales
       embebidas, y `validate_url(purpose="stream")` la rechaza: si se
       escapara sin resolver, el reproductor no la vería.
    3. **Nada se persiste con secretos.** La referencia de archivo sobrevive
       a favoritos y recientes sin dejar credenciales en disco.
    4. **No hay sondeo (§17).** `catchup.py` no importa nada de red ni
       contiene rutas de sonda de endpoint, en todo el paquete.
    """
    from thetvview import catchup
    from thetvview.catchup import (
        DISABLED,
        CatchupCapability,
        CatchupNotAvailableError,
        CatchupOutsideArchiveWindowError,
        CatchupRef,
        CatchupRequest,
        CatchupState,
        build_playback_request,
        capability_for,
        classify,
    )
    from thetvview.models import Channel, Program
    from thetvview.security.errors import IPTVError
    from thetvview.security.redaction import contains_embedded_login
    from thetvview.security.url_policy import PURPOSE_STREAM, validate_url

    # --- (1) Puerta cerrada por defecto ---------------------------------
    # La invariante se comprueba sobre la FUNCIÓN, no sobre los datos: si
    # alguien la relaja (por ejemplo, borrando la condición
    # `provider_declared`), este check tiene que ponerse rojo.
    _require(catchup.can_use_catchup(None) is False,
             "can_use_catchup(None) no es False")
    _require(catchup.can_use_catchup(DISABLED) is False,
             "DISABLED no está bloqueado por can_use_catchup")
    _require(
        catchup.can_use_catchup(CatchupCapability(enabled=True, provider_declared=True))
        is True,
        "can_use_catchup rechaza una capacidad declarada y habilitada",
    )
    _require(
        catchup.can_use_catchup(CatchupCapability(enabled=True, provider_declared=False))
        is False,
        "can_use_catchup ignora provider_declared (§7/§21)",
    )
    _require(
        catchup.can_use_catchup(CatchupCapability(enabled=False, provider_declared=True))
        is False,
        "can_use_catchup ignora enabled (§7/§21)",
    )

    ahora = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
    pasado = ahora - timedelta(hours=2)
    viejo = ahora - timedelta(days=30)

    def _canal(archivo: int, dias: int) -> Channel:
        return Channel(
            name="ESPN",
            url="xtream://Panel/live/101.ts",
            tvg_id="espanol.espn",
            attrs={
                "xtream_id": "101", "source_name": "Panel",
                "tv_archive": str(archivo), "tv_archive_duration": str(dias),
            },
        )

    def _programo(desde: datetime, minutos: int = 90) -> Program:
        return Program("espanol.espn", "Película", desde,
                       desde + timedelta(minutes=minutos))

    _require(capability_for(_canal(0, 7)) is DISABLED,
             "tv_archive=0 no equivale a DISABLED")
    _require(capability_for(_canal(1, 0)) is DISABLED,
             "tv_archive=1 sin ventana no equivale a DISABLED")
    _require(capability_for(Channel(name="x", url="http://x/1.ts")) is DISABLED,
             "un canal sin metadatos no equivale a DISABLED")

    # EPG presente + tv_archive=0 → sin catch-up (§16).
    con_epg = _canal(0, 7)
    _require(
        classify(con_epg, _programo(pasado), ahora)
        is CatchupState.DISABLED_BY_PROVIDER,
        "la existencia de EPG habilita catch-up",
    )
    try:
        build_playback_request(con_epg, capability_for(con_epg),
                               CatchupRequest("101", pasado, ahora))
    except CatchupNotAvailableError:
        pass
    else:
        raise CheckFailure("un canal sin declaración construyó una petición histórica")

    # Ventana declarada respetada (7 días): dentro sí, hace 30 días no.
    con_archivo = _canal(1, 7)
    cap = capability_for(con_archivo)
    dentro = build_playback_request(
        con_archivo, cap, CatchupRequest("101", pasado, ahora), now=ahora,
    )
    _require(bool(dentro.url), "la petición dentro de ventana no produjo URL")
    try:
        build_playback_request(
            con_archivo, cap,
            CatchupRequest("101", viejo, viejo + timedelta(minutes=90)), now=ahora,
        )
    except CatchupOutsideArchiveWindowError:
        pass
    else:
        raise CheckFailure("una petición fuera de la ventana se construyó")

    # --- (2) La referencia es opaca --------------------------------------
    ref = CatchupRef("Panel", "101", int(pasado.timestamp()), 3600)
    opaca = ref.to_opaque()
    for prohibido in ("password", "username", "://u", "secret"):
        _require(prohibido not in opaca.lower(),
                 f"la referencia de archivo filtró «{prohibido}»")
    _require(not contains_embedded_login(opaca),
             "la referencia de archivo emula userinfo")
    try:
        validate_url(opaca, purpose=PURPOSE_STREAM)
    except IPTVError:
        pass
    else:
        raise CheckFailure("validate_url acepta la referencia de archivo: podría "
                           "llegar al reproductor sin resolver")

    # --- (3) Nada se persiste con secretos -------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        from thetvview.favorites import FavoritesManager
        from thetvview.recents import RecentsManager

        fav_path = base / "favorites.json"
        FavoritesManager(fav_path).toggle(Channel(name="ESPN", url=opaca))
        fav_raw = fav_path.read_text(encoding="utf-8")
        _require(opaca in fav_raw, "la referencia de archivo no sobrevive a favoritos")
        _require("password" not in fav_raw and "secreto" not in fav_raw,
                 "favorites.json contiene credenciales")

        rec_path = base / "recents.json"
        RecentsManager(rec_path).push(Channel(name="ESPN", url=opaca), "mpv")
        rec_raw = rec_path.read_text(encoding="utf-8")
        _require(opaca in rec_raw, "la referencia de archivo no sobrevive a recientes")
        _require("password" not in rec_raw and "secreto" not in rec_raw,
                 "recents.json contiene credenciales")

    # Y la red de seguridad: una URL **resuelta** (con la contraseña en la
    # query, como la de timeshift.php) se redacta antes de tocar disco, por
    # quien sea que la pase por favoritos o recientes.
    class _Catalogo:
        def get_credentials(self, name: str):
            return ("http://srv.example.com:8080", "usuario", "hunter2")

    resuelta = ref.resolve(_Catalogo())
    _require("hunter2" in resuelta,
             "resolve no metabolizó las credenciales del catálogo")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        from thetvview.favorites import FavoritesManager
        from thetvview.recents import RecentsManager

        fav_path = base / "favorites.json"
        FavoritesManager(fav_path).toggle(Channel(name="ESPN", url=resuelta))
        rec_path = base / "recents.json"
        RecentsManager(rec_path).push(Channel(name="ESPN", url=resuelta), "mpv")
        for etiqueta, path in (("favorites", fav_path), ("recents", rec_path)):
            crudo = path.read_text(encoding="utf-8")
            _require("hunter2" not in crudo,
                     f"{etiqueta}.json guardó la contraseña de la URL resuelta")

    # --- (4) No hay sondeo (§17) -----------------------------------------
    mod = PACKAGE_DIR / "catchup.py"
    _require(mod.exists(), "thetvview/catchup.py no existe")
    tree = _parse(mod)
    prohibidos = {"socket", "ssl", "subprocess", "http", "ftplib", "smtplib",
                  "telnetlib", "asyncio", "shutil", "pickle"}
    vistos: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            vistos.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            vistos.add(node.module.split(".")[0])
    intrusos = sorted(vistos & prohibidos)
    _require(not intrusos, f"catchup.py hace red o procesos: {intrusos}")

    # Tramos de ruta al estilo "sonda". El regex no se contiene a sí mismo,
    # así que este check puede auditarse a sí mismo.
    sonda_re = re.compile(r"/(?:archive|catchup|timeshift)/", re.IGNORECASE)
    docstrings = _docstrings(tree)
    sondas: list[str] = []
    for path in _iter_python_files(PACKAGE_DIR):
        t = _parse(path)
        docs = _docstrings(t)
        for node in ast.walk(t):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs):
                for match in sonda_re.finditer(node.value):
                    sondas.append(f"{path.name}:{node.lineno} {match.group(0)}")
    _require(not sondas, f"el paquete construye rutas de sonda de archivo: {sondas}")

    # --- (5) Fase 1: el intervalo se rechaza y la plantilla se acota ----
    # Lo que la Fase 0 encontró y la Fase 1 cerró: un intervalo invertido se
    # normalizaba a un segundo y producía una URL. Si alguien relaja el
    # `max(1, ...)` o quita la validación, este check tiene que ponerse rojo.
    validar = getattr(catchup, "validar_intervalo", None)
    _require(callable(validar),
             "catchup.validar_intervalo no existe: el intervalo inválido "
             "vuelve a normalizarse en silencio (CU-02)")
    invertido = CatchupRequest("101", ahora, pasado)  # fin antes del inicio
    try:
        validar(invertido)
    except IPTVError:
        pass
    else:
        raise CheckFailure("un intervalo invertido pasó la validación (CU-02)")
    try:
        build_playback_request(con_archivo, cap, invertido, now=ahora)
    except IPTVError:
        pass
    else:
        raise CheckFailure("un intervalo invertido construyó una URL (CU-02)")

    _require(int(getattr(catchup, "MAX_REQUEST_SECONDS", 0)) > 0,
             "catchup.MAX_REQUEST_SECONDS no está declarado (CU-02)")
    _require(int(getattr(catchup, "MAX_TEMPLATE_LENGTH", 0)) > 0,
             "catchup.MAX_TEMPLATE_LENGTH no está declarado (CU-03)")

    # La plantilla de un tercero es una entrada no confiable: ni esquema raro,
    # ni salto de línea (que en una URL es una cabecera inyectada).
    from thetvview.catchup import render_source_template
    for plantilla in ("file:///etc/passwd?s={start}",
                      "javascript:alert(1)?s={start}",
                      "http://a.test/x?s={start}\nX-Inyectada: 1",
                      "http://a.test/x?s={marcador_que_no_existe}"):
        try:
            salida = render_source_template(plantilla, CatchupRequest("101", pasado, ahora))
        except IPTVError:
            continue
        raise CheckFailure(f"la plantilla de un tercero pasó sin revisarse: {plantilla!r}")
    # El salto de línea se comprueba aparte: `validate_url` también lo
    # rechaza, así que el test de arriba pasaría igual si el módulo dejara de
    # mirarlo. Aquí se exige que el propio render no devuelva control alguno.
    for plantilla in ("http://a.test/x?s={start}\nX: 1",
                      "http://a.test/x?s={start}\r\nX: 1"):
        try:
            salida = render_source_template(plantilla, CatchupRequest("101", pasado, ahora))
        except IPTVError:
            continue
        raise CheckFailure(
            f"render_source_template devolvió caracteres de control: {salida!r}")
    # Y una plantilla legítima sigue funcionando (el control no puede ser
    # tan estricto que rompa el caso normal).
    legitima = render_source_template("http://a.test/x?s={start}",
                                      CatchupRequest("101", pasado, ahora))
    _require(f"s={int(pasado.timestamp())}" in legitima,
             "una plantilla legítima de catch-up dejó de renderizarse (CU-03)")

    # Las causas de fallo se distinguen: sin eso, el modal dice «falló» y el
    # usuario no sabe si recargar credenciales o esperar la red (CU-04).
    from thetvview.catchup import describe_unavailable
    from thetvview.player.errors import StreamErrorCode
    _require(describe_unavailable(StreamErrorCode.AUTH_FAILED)
             is CatchupState.AUTH_FAILED,
             "un fallo de autenticación no se distingue (CU-04)")
    _require(describe_unavailable(StreamErrorCode.CONNECTION_TIMEOUT)
             is CatchupState.NETWORK,
             "un fallo de red no se distingue (CU-04)")
    _require(describe_unavailable(None) is CatchupState.UNKNOWN,
             "un fallo sin clasificar no es «desconocido» (CU-04: "
             "desconocido ≠ falso)")

    return (f"capacidad fail-closed, ventana respetada, referencia opaca "
            f"({len(opaca)} chars) sin credenciales, 0 rutas de sonda, "
            f"intervalo rechazado, plantilla acotada, causas separadas")


# ---------------------------------------------------------------------------
# 13. Descubrimiento de pistas: red vigilada, proxy vigilado, prefs vigiladas
# ---------------------------------------------------------------------------


def check_track_discovery_is_sandboxed() -> str:
    """Selección de pistas: los tres invariantes del plan F7, verificados.

    No son comentarios: se comprueban sobre el **código**, y si alguien los
    relaja el check se pone rojo.

    1. **Nada de red fuera de ``safe_http``.** Ningún módulo nuevo
       (``streams``, ``tracks``, ``player``) llama a ``urllib.request``,
       ``http.client``, ``socket.create_connection`` ni importa ``requests``:
       todo lo que sale a la red pasa por el cliente único (H4).
    2. **El proxy de fijado sólo escucha en loopback y exige token.** Se
       comprueba sobre ``PinProxy.start`` (que rechaza cualquier host que no
       sea 127.0.0.1) y sobre el servidor real: sin token la ruta responde
       **404** y no se sirve ningún otro camino.
    3. **``prefs.json`` de pistas nunca guarda la URL cruda.** Se comprueba
       con una URL de Xtream real (usuario y contraseña en el *path*): ni el
       fichero, ni las claves, ni los valores contienen nada de eso.
    """
    import tempfile

    from thetvview.models import Channel
    from thetvview.prefs import Prefs, PrefsManager
    from thetvview.streams.pin_proxy import LOOPBACK, TOKEN_BYTES, PinProxy, PinProxyError
    from thetvview.security.redaction import contains_embedded_login
    from thetvview.tracks.models import VIDEO as VIDEO_TYPE
    from thetvview.tracks.models import MediaTrack
    from thetvview.tracks.prefs import (
        TrackPreferences,
        channel_key,
        remember_for_channel,
        resolve_preferences,
    )

    # --- (1) todo lo que sale a la red pasa por safe_http -----------------
    nuevos = ("streams", "tracks", "player")
    prohibido = {
        "urlopen", "build_opener", "create_connection", "create_server",
        "HTTPConnection", "HTTPSConnection", "requests", "httpx", "urllib3",
    }
    fugas: list[str] = []
    for nombre in nuevos:
        carpeta = PACKAGE_DIR / nombre
        for ruta in _iter_python_files(carpeta):
            arbol = _parse(ruta)
            docs = _docstrings(arbol)
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Call):
                    llamada = _call_name(nodo)
                    if llamada.split(".")[-1] in prohibido and nodo.lineno not in docs:
                        fugas.append(f"{ruta.name}:{nodo.lineno} {llamada}")
                elif isinstance(nodo, (ast.Import, ast.ImportFrom)):
                    modulos = (
                        [a.name for a in nodo.names] if isinstance(nodo, ast.Import)
                        else [nodo.module or ""]
                    )
                    for modulo in modulos:
                        raiz = modulo.split(".")[0]
                        # `socket` sí se usa, pero sólo para el IPC local de
                        # mpv y para el proxy de loopback (comprobado abajo).
                        if raiz in {"requests", "httpx", "aiohttp"}:
                            fugas.append(f"{ruta.name}:{nodo.lineno} import {modulo}")
    _require(not fugas, f"red fuera de safe_http: {fugas}")

    # --- (2) el proxy sólo escucha en loopback y exige token --------------
    _require(LOOPBACK == "127.0.0.1", f"el proxy escucha en {LOOPBACK}, no en loopback")
    _require(TOKEN_BYTES >= 16, f"token de {TOKEN_BYTES * 8} bits: hacen falta 128")

    with tempfile.TemporaryDirectory() as td:
        ruta = Path(td) / "prefs.json"
        # Se comprueba primero el rechazo de cualquier host que no sea loopback.
        from thetvview.tracks.models import MediaCapabilities, PlaybackSelection

        vacio = MediaCapabilities(
            video_variants=[
                MediaTrack(id="v720", type=VIDEO_TYPE, height=720,
                           uri="http://h/v720.m3u8")
            ],
            protocol="hls",
        )
        sel = PlaybackSelection(video_track_id="v720", auto_quality=False)
        try:
            fugado = PinProxy.start(vacio, sel, host="0.0.0.0")
        except PinProxyError:
            pass
        else:
            fugado.stop()
            raise CheckFailure("el proxy acepta escuchar fuera de loopback")

        proxy = PinProxy.start(vacio, sel)
        try:
            import urllib.error
            import urllib.request

            with urllib.request.urlopen(proxy.url, timeout=5) as respuesta:  # noqa: S310
                _require(respuesta.status == 200, "el proxy no sirve su manifest")
                cuerpo = respuesta.read().decode()
            _require("#EXTM3U" in cuerpo, "el proxy no sirve un manifest HLS")
            rutas = {r for r in proxy.routes()}
            _require(
                all(r.startswith(f"/{proxy.token}/") for r in rutas),
                f"rutas sin token: {rutas}",
            )
            # Sin token, y en cualquier otro camino: 404.
            for url in (
                f"http://{LOOPBACK}:{proxy.port}/{proxy.token}../master.m3u8",
                f"http://{LOOPBACK}:{proxy.port}/master.m3u8",
                f"http://{LOOPBACK}:{proxy.port}/",
                f"http://{LOOPBACK}:{proxy.port}/{proxy.token}/seg0.ts",
            ):
                try:
                    urllib.request.urlopen(url, timeout=5)  # noqa: S310
                    raise CheckFailure(f"el proxy sirve una ruta no permitida: {url}")
                except urllib.error.HTTPError as exc:
                    _require(exc.code == 404, f"{url} devolvió {exc.code}, no 404")
        finally:
            proxy.stop()
        _require(not proxy.is_running, "el proxy sigue vivo tras stop()")

        # --- (3) prefs.json de pistas sin la URL cruda -------------------
        url_secreta = "http://proveedor.test/live/alice/P4ssw0rd/1234.m3u8"
        canal = Channel(name="Canal", url=url_secreta, tvg_id=None)
        prefs = remember_for_channel(
            PrefsManager(ruta), canal,
            TrackPreferences(preferred_audio_language="ca", preferred_quality="720p"),
        )
        _require(prefs is not None, "no se pudo guardar la preferencia del canal")
        texto = ruta.read_text(encoding="utf-8")
        for secreto in ("alice", "P4ssw0rd", "proveedor.test/live"):
            _require(secreto not in texto, f"prefs.json contiene {secreto!r}")
        _require(not contains_embedded_login(texto), "prefs.json tiene login embebido")
        clave = channel_key(canal)
        _require(clave is not None, "el canal no tiene clave")
        _require("alice" not in clave and "P4ssw0rd" not in clave,
                 f"la clave de canal filtra credenciales: {clave}")

        # Y el invariante de fondo: la clave se calcula sobre la URL
        # **redactada**. Un hash de la URL cruja no metería nada legible en
        # el fichero, pero sí dejaría algo atacable por diccionario, así que
        # se comprueba la FUNCIÓN, no los datos (igual que el check 12).
        arbol_prefs = _parse(PACKAGE_DIR / "tracks" / "prefs.py")
        fn = next(
            (n for n in ast.walk(arbol_prefs)
             if isinstance(n, ast.FunctionDef) and n.name == "channel_key"),
            None,
        )
        _require(fn is not None, "tracks/prefs.py ya no define channel_key")
        llamada_redaccion = any(
            (isinstance(n, ast.Name) and n.id.startswith("redact"))
            or (isinstance(n, ast.Attribute) and n.attr.startswith("redact"))
            for n in ast.walk(fn)
        )
        _require(
            llamada_redaccion,
            "channel_key no redacta la URL antes de hashearla: el hash "
            "sería atacable por diccionario",
        )
        resuelta = resolve_preferences(canal, "https://proveedor.test/x",
                                       prefs=Prefs(), scopes={prefs: {"preferred_audio_language": "ca"}})
        _require(resuelta.preferred_audio_language == "ca",
                 "la preferencia del canal no se resuelve")

    return ("red sólo por safe_http, proxy en loopback con token de 128 bits "
            "y 404 fuera de la allowlist, prefs.json sin la URL del canal")


# ---------------------------------------------------------------------------
# 15. Persistencia: escritura atómica y versionado
# ---------------------------------------------------------------------------


def check_persistence_atomic() -> str:
    """STORE-001/002/003/004: escritura atómica, schema_version y migración.

    Este check existe para que la verificación siga siendo ejecutable con una
    tecla (``!``) sin tener que acordarse de que el store tiene una garantía.
    Comprueba tres cosas concretas, y las tres fallan si alguien reintroduce
    el patrón viejo de ``tmp.write_text()`` + ``replace()``:

    1. **atomicidad real**: una escritura deja el fichero legible y completo,
       incluso si se inyecta un fallo en mitad del ``flush``/``fsync``; el
       fichero anterior sobrevive intacto.
    2. **versionado**: los formatos persistentes llevan ``schema_version``.
    3. **migración idempotente**: un fichero v0 pasa a v1 una vez y no se
       vuelve a migrar.
    """
    from ..storage.json_store import atomic_write_json, read_json_with_recovery
    from ..storage.migrations import migrate_file, needs_migration
    from ..storage.schemas import SCHEMA_VERSION, get_validator

    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "check.json"

        def leer_valor(destino: Path) -> str:
            """Valor persistido, o un texto que delata que no es JSON.

            Devolver una cadena en vez de dejar propagar el
            ``JSONDecodeError`` es a propósito: el mensaje del check tiene que
            explicar **qué garantía se rompió**, no escribir el error interno
            de ``json`` y dejar que el que corre ``!`` vea una traza.
            """
            try:
                datos = json.loads(destino.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                return "<no es JSON válido>"
            if not isinstance(datos, dict):
                return "<no es un objeto JSON>"
            return str(datos.get("valor", "<sin valor>"))

        # (1) atomicidad: el estado bueno está siempre en disco.
        atomic_write_json(ruta, {"valor": "uno"})
        _require(
            leer_valor(ruta) == "uno",
            "la escritura atómica no dejó el fichero legible",
        )
        # Fallo inyectado en el punto más caro (el fsync del temporal).
        try:
            with mock.patch("os.fsync", side_effect=OSError("disco lleno")):
                atomic_write_json(ruta, {"valor": "dos"})
        except Exception:
            pass  # el error es lo esperado; lo que importa es el estado
        _require(
            leer_valor(ruta) == "uno",
            f"un fallo a mitad de escritura dejó {leer_valor(ruta)!r} en disco: "
            "el estado bueno debería haber sobrevivido intacto",
        )
        # Y no debe quedar ningún temporal por medio.
        _require(
            not list(Path(tmp).glob(".*.tmp")),
            "la escritura dejó un temporal abandonado",
        )

        # (2) versionado: los formatos con validador están registrados.
        for nombre in ("prefs", "playlists", "favorites", "recents", "theme"):
            _require(
                get_validator(nombre) is not None,
                f"el formato «{nombre}» no tiene validador semántico",
            )
        atomic_write_json(ruta, {"valor": "tres"}, schema_version=SCHEMA_VERSION)
        datos = read_json_with_recovery(ruta, schema_version=SCHEMA_VERSION)
        _require(datos.get("valor") == "tres", "schema_version no se escribió")

        # (3) migración idempotente v0 -> v1.
        v0 = Path(tmp) / "v0.json"
        v0.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        _require(needs_migration({"theme": "dark"}), "un v0 no se reconoce como viejo")
        primera = migrate_file(v0, nombre="theme")
        _require(primera.changed, "la migración v0->v1 no migra")
        segunda = migrate_file(v0, nombre="theme")
        _require(not segunda.changed, "la migración se aplica dos veces (no es idempotente)")
        _require(
            json.loads(v0.read_text(encoding="utf-8")).get("schema_version")
            == SCHEMA_VERSION,
            "la migración no dejó el fichero en la versión actual",
        )

    return (f"escritura atómica verificada, schema_version={SCHEMA_VERSION}, "
            "migración v0→v1 idempotente")


def check_historial_migracion_v2() -> str:
    """H-03: el historial se migra a la v2 sin perder nada y sin filtrar.

    Tres cosas que un ``json.dump`` no garantiza por sí solo:

    1. **El identificador de fuente no llega a disco en crudo.** El real es
       ``xtream|<panel>|<url>|<usuario>``: la URL y el usuario son justo lo
       que un ``diagnostics.json`` no puede contener, porque es un fichero
       que el usuario copia para pasar el informe a otro. Se comprueba
       leyendo el fichero **de verdad** después de registrar.
    2. **La migración no pierde datos ni inventa datos.** Las nueve entradas
       de una v1 real siguen ahí con sus cinco campos, y los cinco nuevos
       quedan desconocidos en vez de rellenados con conjeturas.
    3. **El retroceso existe.** ``escribir_como(1)`` produce un fichero que la
       v1 leería sin Que la v1 lo lea, y el v2 que escribió la v2 lo seguiría
       leyendo después (los campos nuevos se omiten al no saberse, en vez de
       escribir ``null``, que parece una medición).
    """
    import json
    import tempfile

    from thetvview.diagnostics.history import (
        VERSION_ACTUAL,
        HistorialDiagnostico,
        RegistroHistorial,
        source_id_opaco,
    )

    #: Reloj falso. Sin él, la limpieza por edad (30 días) tiraría las
    #: entradas de la v1 de ejemplo y la comprobación mediría otra cosa.
    ahora = 1_767_225_600.0 + 86_400.0

    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "diagnostics.json"

        # --- (1) El source_id crudo no llega a disco --------------------
        crudo = "xtream|Mi Panel|http://usuario:clave@panel.test:8080/live"
        historial = HistorialDiagnostico(ruta, ahora=lambda: ahora)
        historial.registrar("espn", False, source_id=crudo, player="mpv",
                            error_code="network_error", duration_ms=1500.0)
        en_disco = ruta.read_text(encoding="utf-8")
        _require("clave" not in en_disco,
                 "la contraseña de la fuente llegó a diagnostics.json")
        _require("panel.test" not in en_disco,
                 "el servidor de la fuente llegó a diagnostics.json")
        _require("Mi Panel" in en_disco,
                 "el nombre de la lista no se guarda: el modal no puede nombrarla")
        _require(source_id_opaco(source_id_opaco(crudo)) == source_id_opaco(crudo),
                 "opacar el source_id dos veces no da lo mismo")

        # --- (2) Una v1 real se migra sin perder ni inventar -------------
        v1 = {"version": 1, "entradas": [
            {"momento": 1767225600.0 + i * 3600.0, "canal": f"canal.{i}",
             "exito": i % 2 == 0, "motivo": "READY", "fase": "diagnose"}
            for i in range(9)
        ]}
        ruta.write_text(json.dumps(v1), encoding="utf-8")
        migrado = HistorialDiagnostico(ruta, ahora=lambda: ahora)
        entradas = migrado.entradas()
        _require(len(entradas) == 9,
                 f"la migración perdió entradas: {len(entradas)} de 9")
        for entrada in entradas:
            _require(entrada.source_id == "" and entrada.player == ""
                     and entrada.error_code == "" and entrada.recoverable is None
                     and entrada.duration_ms is None,
                     f"la migración inventó datos en {entrada.canal}")
        antes = {(e["momento"], e["canal"], e["exito"]) for e in v1["entradas"]}
        despues = {(e.momento, e.canal, e.exito) for e in entradas}
        _require(antes == despues, "la migración alteró los datos de la v1")

        # --- (3) El retroceso existe y no rompe nada ---------------------
        _require(VERSION_ACTUAL == 2, "la versión actual del historial no es la 2")
        _require(migrado.escribir_como(1), "escribir_como(1) no pudo escribir")
        retroceso = json.loads(ruta.read_text(encoding="utf-8"))
        _require(retroceso["version"] == 1, "el retroceso no escribió version 1")
        _require(set(retroceso["entradas"][0]) == {
            "momento", "canal", "exito", "motivo", "fase"
        }, "el retroceso metió campos de la v2 en el fichero")
        releido = HistorialDiagnostico(ruta, ahora=lambda: ahora).entradas()
        _require(len(releido) == 9, "el fichero retrocedido no se vuelve a leer")
        _require(releido[0].source_id == "",
                 "el fichero retrocedido trae un source_id de la v2")

        # --- (extra) Lo que no se sabe, no se escribe ---------------------
        _require("recoverable" not in RegistroHistorial(
            momento=1.0, canal="a", exito=False).a_dict(),
            "un recoverable desconocido se escribe en el fichero")
        _require("duration_ms" not in RegistroHistorial(
            momento=1.0, canal="a", exito=False).a_dict(),
            "una duración no medida se escribe en el fichero")

    return ("migración v1→v2 sin pérdida (9 entradas de fixture), retroceso "
            "escribir_como(1) verificado, source_id opaco en disco")


def check_historial_eventos_ciclo_vida() -> str:
    """H-01: los sucesos de reproducción dejan rastro, y no se inventan.

    El bus de eventos existía con el contrato entero del §3.3 y **cero**
    ``emit()`` en producción: el informe tenía un formato y ningún suceso que
    lo llenara. Este control comprueba que ahora hay cableado, y sobre todo
    que el cableado no miente:

    1. **Existe.** `play_channel` ata la sesión al bus y un ciclo completo
       deja intento, lanzamiento, fin con su motivo y su duración.
    2. **«Inicio confirmado» se gana.** Sólo sale de una medición
       :class:`Evidence` de nivel ``MEDIA_SEEN`` o superior. Un proceso vivo,
       una fuente aceptada o un ``UNKNOWN`` **no** lo producen: son hechos
       distintos y decir «está sonando» con ellos sería mentir.
    3. **Un fallo sale de la taxonomía.** Su ``reason_code`` y su
       ``retryable`` vienen de ``StreamError``, nunca inventados.
    4. **Un oyente roto no tumba la reproducción** y **anota** el fallo, para
       que no desaparezca en silencio.
    5. **Ningún evento lleva la URL del canal.**
    """
    from thetvview.diagnostics.events import EventBus
    from thetvview.diagnostics.session_events import (
        NIVEL_DE_INICIO,
        ObservadorDeSesion,
        tipo_de_evento,
    )
    from thetvview.player.errors import StreamError, StreamErrorCode
    from thetvview.player.evidence import Evidence, EvidenceLevel
    from thetvview.player.session import EndReason, EventoDeCiclo, PlaybackSession

    # --- (2) El nivel que confirma ----------------------------------------
    _require(NIVEL_DE_INICIO is EvidenceLevel.MEDIA_SEEN,
             "«inicio confirmado» no exige evidencia de medios")
    for nivel in EvidenceLevel:
        bus = EventBus()
        sesion = PlaybackSession(channel_key="d.1", player_name="mpv")
        observador = ObservadorDeSesion(bus, sesion=sesion)
        sesion.oyente = observador
        observador.evidencia(Evidence(level=nivel, reason="check"))
        confirma = sesion.first_media_at is not None
        debe = nivel.comproblable and nivel.rank >= NIVEL_DE_INICIO.rank
        _require(confirma == debe,
                 f"el nivel {nivel.value} da «inicio confirmado»={confirma} "
                 f"cuando debería ser {debe}")

    # --- (2 bis) Y entrar en PLAYING no lo produce -------------------------
    # El sabotaje que este control tenía que cazar: hacer que `playing()` —
    # que sólo significa «el proceso está vivo»— emita «inicio confirmado».
    bus = EventBus()
    sesion = PlaybackSession(channel_key="d.1", player_name="mpv")
    sesion.oyente = ObservadorDeSesion(bus, sesion=sesion)
    sesion.start()
    sesion.launched()
    sesion.playing()
    sesion.end(EndReason.USER_STOPPED)
    tipos_sin_medir = [e.event_type for e in bus.events()]
    _require("playback.lanzado" in tipos_sin_medir,
             "sin evidencia tampoco debe faltar el lanzamiento")
    _require(tipo_de_evento(EventoDeCiclo.INICIO_CONFIRMADO) not in tipos_sin_medir,
             "entrar en PLAYING declara que se está viendo el canal sin "
             "ninguna evidencia: eso es mentir")

    # --- (3) El fallo sale de la taxonomía -------------------------------
    bus = EventBus()
    sesion = PlaybackSession(channel_key="d.1", player_name="mpv")
    sesion.oyente = ObservadorDeSesion(bus, sesion=sesion)
    for codigo in StreamErrorCode:
        sesion.nota_fallo(StreamError(code=codigo, resumen="x",
                                      observado=f"rc={codigo.value}"))
    fallos = [e for e in bus.events() if e.event_type == "playback.fallo"]
    _require(len(fallos) == len(StreamErrorCode),
             "no salió un evento de fallo por cada código")
    for evento, codigo in zip(fallos, StreamErrorCode):
        _require(evento.reason_code == codigo.value,
                 f"el fallo de {codigo.value} dice {evento.reason_code}")
        _require(evento.retryable == codigo.reintentable,
                 f"el fallo de {codigo.value} no lleva la política de reintento")

    # --- (1) y (4) Un ciclo completo --------------------------------------
    class _BusRoto:
        def emit(self, evento):  # noqa: ANN001, ANN201
            raise RuntimeError("el bus está en llamas")

        def events(self):  # noqa: ANN201
            return []

    bus = EventBus()
    # Se construye con una URL con credenciales y luego se le deja sólo la
    # clave: si algo se colara, el evento llevaría el userinfo y salta aquí.
    sesion = PlaybackSession(
        channel_key="http://u:secreto@proveedor.test/live/d/1.ts", player_name="mpv"
    )
    sesion.channel_key = "d.1"
    sesion.oyente = ObservadorDeSesion(bus, sesion=sesion)
    sesion.start()
    sesion.nuevo_intento()
    sesion.launched()
    sesion.note_first_media()
    sesion.nota_fallo(StreamError(code=StreamErrorCode.NETWORK_ERROR,
                                  resumen="x", observado="cerrada"))
    sesion.playing()
    sesion.end(EndReason.USER_STOPPED, "paró el usuario")
    tipos = [e.event_type for e in bus.events()]
    for suceso in (EventoDeCiclo.LANZADO, EventoDeCiclo.INTENTO,
                   EventoDeCiclo.INICIO_CONFIRMADO, EventoDeCiclo.FALLO,
                   EventoDeCiclo.FIN):
        _require(tipo_de_evento(suceso) in tipos,
                 f"un ciclo completo no dejó rastro de {suceso.value}")
    fin = next(e for e in bus.events() if e.event_type == "playback.fin")
    _require(fin.reason_code == EndReason.USER_STOPPED.value,
             "el evento de fin no lleva su motivo")
    _require("duracion_viva_s" in fin.details, "el evento de fin no lleva duración")

    # --- (5) Ningún evento lleva la URL ------------------------------------
    crudo = str([e.to_dict() for e in bus.events()])
    _require("secreto" not in crudo and "proveedor.test" not in crudo,
             "un evento de reproducción lleva la URL del canal")

    # --- (4) Un bus roto no tumba la sesión --------------------------------
    sesion2 = PlaybackSession(channel_key="d.1", player_name="mpv")
    observador2 = ObservadorDeSesion(_BusRoto(), sesion=sesion2)
    sesion2.oyente = observador2
    sesion2.start()
    sesion2.end(EndReason.CANCELLED)
    _require(sesion2.ended, "un bus roto tumbó la sesión de reproducción")
    _require(observador2.errores, "el fallo del bus no quedó anotado en ningún sitio")

    return ("ciclo completo con 5 sucesos; «inicio confirmado» exige "
            f"{NIVEL_DE_INICIO.value}; un bus roto deja {len(observador2.errores)} "
            "error anotado sin tumbar la sesión")


def check_historial_redaccion_al_escribir() -> str:
    """H-04: lo que se escribe ya está limpio, y no se limpia al leer.

    El destino de todo esto es un ``diagnostics.json`` que el usuario copia
    para pasar el informe a otra persona. Si la redacción ocurriera al leer,
    el secreto seguiría en el fichero que se copió, en la copia de seguridad y
    en el adjunto de correo: se habría protegido **la pantalla**, que es justo
    lo que no importa.

    Por eso este control comprueba dos cosas que parecen contrarias:

    1. **Nada que escribamos lleva el centinela**, ni en disco, ni en memoria,
       ni en el registro que devuelve ``registrar`` — que es donde se nota la
       diferencia: si sólo se limpiara en disco, el objeto en memoria llevaría
       el secreto y podría acabar en un informe por otro camino.
    2. **Leer NO limpia.** Un fichero escrito a mano con un secreto se
       devuelve tal cual. Es contraintuitivo y es la prueba: si la lectura
       limpiara, las dos garantías coexistirían y nadie sabría cuál estaba
       protegiendo de verdad. Al no limpiar la lectura, la única garantía que
       existe es la de no escribirlo, que es la que importa.

    Y de paso, que nadie escribe un historial por su cuenta fuera de
    ``history.py``, que es un camino que ningún centinela vería.
    """
    import json
    import stat
    import tempfile

    from thetvview import config
    from thetvview.diagnostics import default_historial
    from thetvview.diagnostics.events import EventBus
    from thetvview.diagnostics.history import HistorialDiagnostico
    from thetvview.diagnostics.session_events import ObservadorDeSesion
    from thetvview.player.errors import StreamError, StreamErrorCode
    from thetvview.player.session import EndReason, PlaybackSession

    canario = "CANARIO-SECRETO-4f2a9c"
    sentinelas: dict[str, str] = {
        "userinfo": f"http://usuario:{canario}@proveedor.test/live/1.ts",
        "parametro": f"http://proveedor.test/live/1.ts?password={canario}&x=1",
        "xtream": f"xtream://Panel/live/{canario}/{canario}/101.ts",
        "cabecera": f"Authorization: Bearer {canario}",
        "token": f"fallo con token={canario} al abrir",
        "cookie": f"Cookie: sesion={canario}",
    }

    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "diagnostics.json"

        # --- (1) Nada que escribamos lleva el centinela ------------------
        reloj = [1_800_000_000.0]
        historial = HistorialDiagnostico(ruta, ahora=lambda: reloj[0])
        for nombre, valor in sentinelas.items():
            registro = historial.registrar(
                valor, False, motivo=valor, fase=valor,
                source_id=f"m3u|{valor}|{valor}", player=valor,
                error_code="network_error", duration_ms=7.0,
            )
            destinos = {
                "el registro devuelto": str(registro.a_dict()),
                "las entradas en memoria": str([e.a_dict()
                                                for e in historial.entradas()]),
                "el fichero": ruta.read_text(encoding="utf-8"),
            }
            for destino, texto in destinos.items():
                _require(canario not in texto,
                         f"el centinela «{nombre}» llegó a {destino}")

        modo = stat.S_IMODE(ruta.stat().st_mode)
        _require(modo == 0o600, f"diagnostics.json no es 0600 (es {modo:o})")

        # --- (2) Leer NO limpia ------------------------------------------
        # Se escribe a mano, se borra todo lo que el historial escribiría, y
        # se comprueba que la lectura lo devuelve tal cual.
        ruta.write_text(json.dumps({
            "version": 2,
            "entradas": [{
                "momento": 1_800_000_000.0,
                "canal": f"http://u:{canario}@a.test/1.ts",
                "exito": False, "motivo": "", "fase": "diagnose",
            }],
        }), encoding="utf-8")
        leido = HistorialDiagnostico(ruta, ahora=lambda: reloj[0]).entradas()
        _require(len(leido) == 1, "no se pudo leer el fichero escrito a mano")
        _require(canario in leido[0].canal,
                 "la lectura está redactando: la garantía de escritura dejó de "
                 "ser la única, y entonces no sabemos cuál protege")

        # --- (3) El bus limpia por dentro --------------------------------
        bus = EventBus()
        sesion = PlaybackSession(channel_key="espn", player_name="mpv")
        sesion.oyente = ObservadorDeSesion(bus, sesion=sesion)
        sesion.nota_fallo(StreamError(code=StreamErrorCode.AUTH_FAILED,
                                      resumen="x", detalle=sentinelas["userinfo"],
                                      observado=sentinelas["parametro"]))
        sesion.end(EndReason.PLAYBACK_ERROR, sentinelas["token"])
        _require(canario not in str([e.to_dict() for e in bus.events()]),
                 "un centinela llegó al bus de eventos")

        # --- (4) El fichero por defecto tiene los mismos riesgos ----------
        with tempfile.TemporaryDirectory() as data_dir:
            real = Path(data_dir) / "diagnostics.json"
            original = config.DIAGNOSTICS_JSON
            try:
                config.DIAGNOSTICS_JSON = real  # type: ignore[misc]
                default_historial().registrar(sentinelas["cookie"], False)
                _require(canario not in real.read_text(encoding="utf-8"),
                         "default_historial() escribe sin redactar")
            finally:
                config.DIAGNOSTICS_JSON = original  # type: ignore[misc]

    # --- (5) Nadie escribe un historial por su cuenta --------------------
    permitidos = {"history.py"}
    ofensas: list[str] = []
    for paquete in ("diagnostics", "player"):
        directorio = Path(__file__).resolve().parents[1] / paquete
        for ruta in sorted(directorio.rglob("*.py")):
            if ruta.name in permitidos:
                continue
            texto = ruta.read_text(encoding="utf-8")
            if "json.dump" in texto and ("diagnostics.json" in texto
                                         or "DIAGNOSTICS_JSON" in texto):
                ofensas.append(ruta.name)
    _require(not ofensas, f"escritura de historial fuera de history.py: {ofensas}")

    return (f"{len(sentinelas)} centinelas limpios en 3 destinos; la lectura no "
            "redacta (comprobado); permisos 0600; 0 escrituras fuera de history.py")


def check_historial_retencion() -> str:
    """H-05: el historial no crece, y los tres límites cuentan cosas distintas.

    Es la comprobación de que el FR-DIAG-010 no es una intención. Con un
    reloj falso que llega muy lejos, porque con el reloj real «30 días» es
    una afirmación que no se puede examinar.

    Se comprueba, no se supone:

    1. **Los tres límites cuentan algo distinto.** Sólo cantidad dejaría un
       fichero pequeño capaz de guardar un único diagnóstico de hace dos años
       —justo el que el usuario consulta para saber si un fallo es viejo—;
       sólo edad daría varios megas con una lista grande; sólo tamaño
       llenaría el tope con un día intenso y tiraría la semana anterior.
    2. **El corte es exacto**, no «aproximadamente»: una entrada que cae justo
       en el límite se queda, y una que pasa un segundo se va.
    3. **La limpieza corre al escribir**, y se mira **el fichero**, no el
       objeto: si fuera al leer, el disco seguiría gordo y quien lo mirara
       creería que está limpio.
    4. **El corte por tamaño está acotado**: descarta la mitad de golpe, no una
       entrada por vuelta, que con 200 entradas y un tope pequeño sería
       escribir el fichero cientos de veces para un solo registro.
    5. **El fichero real cabe en el tope**, con los límites por defecto.
    """
    import json
    import tempfile

    from thetvview.diagnostics.history import (
        MAX_BYTES,
        MAX_DIAS,
        MAX_ENTRADAS,
        HistorialDiagnostico,
    )

    _require(MAX_ENTRADAS > 0 and MAX_DIAS > 0 and MAX_BYTES > 0,
             "los límites del historial tienen que ser positivos")
    _require(len({id(MAX_ENTRADAS), id(MAX_DIAS), id(MAX_BYTES)}) == 3,
             "los tres límites se declaran con el mismo valor: son tres cosas")

    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "diagnostics.json"
        reloj = [1_800_000_000.0]

        def plantar(entradas: int, cada: float, antiguedad: float) -> None:
            ultima = reloj[0] - antiguedad
            base = ultima - (entradas - 1) * cada
            ruta.write_text(json.dumps({
                "version": 2,
                "entradas": [
                    {"momento": base + i * cada, "canal": f"c{i}", "exito": True}
                    for i in range(entradas)
                ],
            }), encoding="utf-8")

        dia = 86_400.0

        # --- (1) y (2) El límite de edad es exacto ------------------------
        h = HistorialDiagnostico(ruta, ahora=lambda: reloj[0], max_dias=7.0)
        justo = reloj[0] - 7.0 * dia
        ruta.write_text(json.dumps({
            "version": 2,
            "entradas": [
                {"momento": justo - 1.0, "canal": "viejo", "exito": True},
                {"momento": justo, "canal": "en_el_borde", "exito": True},
                {"momento": justo + 1.0, "canal": "nuevo", "exito": True},
            ],
        }), encoding="utf-8")
        h.registrar("nueva", True)
        canales = {e.canal for e in h.entradas()}
        _require("viejo" not in canales, "una entrada un segundo de edad sobrevive")
        _require("en_el_borde" in canales,
                 "una entrada exactamente en el límite no debe caer")

        # --- (1) La edad manda cuando hay muchas -------------------------
        h = HistorialDiagnostico(ruta, ahora=lambda: reloj[0],
                                 max_entradas=5000, max_dias=7.0)
        plantar(40, dia, antiguedad=8 * dia)
        h.registrar("reciente", True)
        _require(len(h.entradas()) == 1,
                 f"el límite de edad no se aplicó: quedan {len(h.entradas())}")

        # --- (1) La cantidad manda cuando son muchas ----------------------
        h = HistorialDiagnostico(ruta, ahora=lambda: reloj[0],
                                 max_entradas=10, max_dias=3650.0)
        for i in range(60):
            reloj[0] += dia
            h.registrar(f"c{i}", True)
        _require(len(h.entradas()) == 10,
                 f"el límite de cantidad no se aplicó: quedan {len(h.entradas())}")
        _require(h.entradas()[-1].canal == "c59",
                 "el límite de cantidad se quedó con las viejas")

        # --- (1) El tamaño manda y está acotado ---------------------------
        h = HistorialDiagnostico(ruta, ahora=lambda: reloj[0],
                                 max_entradas=10_000, max_dias=3650.0,
                                 max_bytes=1024)
        for i in range(200):
            reloj[0] += 1.0
            h.registrar(f"canal-con-nombre-largo-{i:04d}", False,
                        motivo="un motivo bastante largo para ocupar sitio")
        _require(ruta.stat().st_size <= 1024,
                 f"el fichero pesa {ruta.stat().st_size} con un tope de 1024")
        vueltas = [0]

        original = h._texto_serializado

        def contando(version: int = 2) -> str:
            vueltas[0] += 1
            return original(version)

        h._texto_serializado = contando  # type: ignore[method-assign]
        # Varios registros seguidos, no uno: tras la purga anterior el
        # fichero justo cabe, así que un único registro más no llega a forzar
        # el corte y la comprobación no estaría midiendo nada.
        for i in range(20):
            reloj[0] += 1.0
            h.registrar(f"relleno-largo-{i:04d}", False)
        _require(vueltas[0] > 0,
                 "el fichero no llegó a forzarse: el corte por tamaño no se midió")
        _require(vueltas[0] <= 20 * 20,
                 f"el corte por tamaño itera {vueltas[0]} veces para 20 "
                 "registros: no está acotado")

        # --- (3) La limpieza corre al escribir ----------------------------
        # Se mira el FICHERO, no el objeto: si la limpieza fuera al leer, el
        # disco seguiría gordo y quien lo mirara creería que está limpio.
        h = HistorialDiagnostico(ruta, ahora=lambda: reloj[0],
                                 max_entradas=10, max_dias=7.0)
        plantar(30, dia, antiguedad=8 * dia)
        h.registrar("nueva", True)
        crudo = json.loads(ruta.read_text(encoding="utf-8"))
        _require(len(crudo["entradas"]) == 1,
                 f"en disco quedan {len(crudo['entradas'])} entradas sin limpiar")

        # --- (5) Con los límites por defecto ------------------------------
        h = HistorialDiagnostico(ruta, ahora=lambda: reloj[0])
        for i in range(200):
            reloj[0] += 60.0
            h.registrar(f"canal{i}", False, motivo="algo que ocupar sitio")
        _require(ruta.stat().st_size <= MAX_BYTES,
                 f"el fichero por defecto pesa {ruta.stat().st_size} "
                 f"con un tope de {MAX_BYTES}")
        _require(len(h.entradas()) <= MAX_ENTRADAS,
                 "el historial por defecto supera el tope de entradas")

    return (f"cantidad={MAX_ENTRADAS}, edad={MAX_DIAS}d, tamaño={MAX_BYTES}B; "
            "corte exacto en el límite; limpieza al escribir verificada en disco; "
            f"corte por tamaño acotado a {vueltas[0]} vueltas")


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------


def check_historial_taxonomia_unica() -> str:
    """H-02: el historial técnico escribe motivos de la taxonomía del reproductor.

    El riesgo que se cierra aquí no es «un dato feo»: es una **segunda
    taxonomía**. Si el módulo de diagnóstico acepta cualquier cadena como
    ``reason_code``, nada impide que mañana aparezca ``"fallo_de_red"`` al
    lado de ``"network_error"``, los dos diciendo lo mismo con nombres
    distintos, y ningún informe detecte el desacuerdo porque los dos son
    «motivos válidos».

    Se comprueba, no se supone:

    1. **Todo código conocido es del reproductor**, y el conjunto no tiene ni
       un código de más: es exactamente la unión de ``StreamErrorCode`` y
       ``EndReason``.
    2. **Un motivo inventado no se resuelve.** ``codigo_de`` devuelve ``None``
       en vez de inventar o normalizar a ``UNKNOWN``.
    3. **Sin código no se afirma uno.** ``reason_code`` vacío y
       ``"unknown"`` son cosas distintas, y la segunda es la única que se
       marca como causa no conocida.
    4. **En el paquete no hay ``reason_code`` literal.** Se recorre el
       código real de ``thetvview/diagnostics/`` buscando una cadena escrita
       a mano en una llamada a ``Event(...)`` o ``registrar(...)``.
    """
    import ast

    from thetvview.diagnostics import taxonomy
    from thetvview.diagnostics.taxonomy import (
        CODES_CONOCIDOS,
        MOTIVOS_CONOCIDOS,
        Causa,
        causa_de_codigo,
        codigo_de,
        es_razon_conocida,
    )
    from thetvview.player.errors import StreamError, StreamErrorCode
    from thetvview.player.session import EndReason

    # --- (1) La unión es exacta ------------------------------------------
    esperados = {c.value for c in StreamErrorCode} | {m.value for m in EndReason}
    _require(set(CODES_CONOCIDOS) <= esperados,
             "CODES_CONOCIDOS trae códigos que no son de player.errors")
    _require(set(MOTIVOS_CONOCIDOS) == esperados,
             "MOTIVOS_CONOCIDOS no es exactamente la taxonomía del reproductor")

    for codigo in StreamErrorCode:
        _require(codigo_de(codigo.value) is codigo,
                 f"codigo_de no resuelve {codigo.value}")

    # --- (2) Un motivo inventado no se resuelve ---------------------------
    # Cadenas que parecen motivo pero no son: dos son castellano (una posible
    # taxonomía paralelo mal pensada) y dos son HTTP (códigos que existen en
    # otros sitios pero no aquí, y es justo el cruce peligroso).
    for inventado in ("fallo_de_red", "no_se_abre", "http_500", "fallo de red"):
        _require(codigo_de(inventado) is None,
                 f"codigo_de se inventó un código para {inventado!r}")
        _require(not es_razon_conocida(inventado),
                 f"es_razon_conocida acepta {inventado!r}")
    _require(codigo_de("") is None, "codigo_de('') no es None")
    _require(codigo_de(None) is None, "codigo_de(None) no es None")
    _require(codigo_de(42) is None, "codigo_de(42) no es None")

    # --- (3) Sin código no se afirma uno ----------------------------------
    vacia = causa_de_codigo(None)
    _require(vacia.reason_code == "",
             "sin código, reason_code debe quedar vacío, no relleno")
    _require(vacia.recoverable is None,
             "sin código, no se pronuncia la recuperabilidad")
    _require(not vacia.causa_conocida,
             "una causa vacía no puede declarar su causa conocida")
    _require(Causa().confianza == "desconocida",
             "una causa vacía no puede declarar confianza observada")
    unknown = causa_de_codigo(StreamErrorCode.UNKNOWN)
    _require(unknown.reason_code == StreamErrorCode.UNKNOWN.value,
             "UNKNOWN debe conservarse como código, no desaparecer")
    _require(not unknown.causa_conocida,
             "UNKNOWN es una conjetura y no puede declararse causa conocida")
    _require(unknown.observado == "",
             "causa_de_codigo no puede inventar un hecho observado")

    # --- (4) En el paquete no hay motivos escritos a mano ------------------
    permitidos = {"taxonomy.py"}
    ofensas: list[str] = []
    for ruta in sorted(Path(taxonomy.__file__).parent.rglob("*.py")):
        if ruta.name in permitidos:
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"), str(ruta))
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            nombre = getattr(nodo.func, "id", None) or getattr(nodo.func, "attr", None)
            if nombre not in ("Event", "registrar"):
                continue
            for palabra in nodo.keywords:
                if palabra.arg != "reason_code":
                    continue
                if not isinstance(palabra.value, ast.Constant):
                    continue
                valor = palabra.value.value
                if valor in (None, ""):
                    continue
                ofensas.append(f"{ruta.name}:{nodo.lineno}={valor!r}")
    _require(not ofensas, f"reason_code escrito a mano en: {'; '.join(ofensas)}")

    # --- (extra) El hecho y la inferencia no se mezclan -------------------
    # Se recorre TODO el enum, no un ejemplo: la regla que se comprueba es
    # «recoverable sale de reintentable», y un solo ejemplo no la garantiza.
    for codigo in StreamErrorCode:
        fallo = StreamError(code=codigo, resumen="x", observado=f"rc={codigo.value}")
        causa = taxonomy.causa_de(fallo)
        _require(causa.recoverable == codigo.reintentable,
                 f"recoverable deja de ser la política de player.errors en "
                 f"{codigo.value}")
        _require(causa.reason_code == codigo.value,
                 f"causa_de cambia el código de {codigo.value}")
        _require(causa.observado == f"rc={codigo.value}",
                 f"causa_de pierde el hecho observado de {codigo.value}")
    _require(taxonomy.causa_de(None) == Causa(),
             "sin fallo no debe haber causa")
    _require(taxonomy.causa_de(StreamError(code=StreamErrorCode.AUTH_FAILED,
                                           resumen="x")).causa_conocida,
             "auth_failed es una causa observada")

    return (
        f"Motivos conocidos: {len(MOTIVOS_CONOCIDOS)} "
        f"({len(CODES_CONOCIDOS)} de error + {len(esperados) - len(CODES_CONOCIDOS)} "
        f"de cierre). Códigos literales en diagnostics/: {len(ofensas)}."
    )


CHECKS: tuple[tuple[str, str, Callable[[], str]], ...] = (
    ("secret_redaction", "Secret redaction", check_secret_redaction),
    ("tls_verification", "HTTPS certificate verification", check_tls_verification),
    ("ssrf_policy", "SSRF policy", check_ssrf_policy),
    ("redirect_policy", "Redirect policy", check_redirect_policy),
    ("response_limits", "Response limits", check_response_limits),
    ("xml_safe_parsing", "XML safe parsing", check_xml_safe),
    ("ai_secret_sanitizer", "AI secret sanitizer", check_ai_sanitizer),
    ("shell_safe_player", "Shell-safe player invocation", check_shell_safe_player),
    ("opaque_refs_credentials", "Opaque refs carry no credentials",
     check_opaque_refs_no_leak_credentials),
    ("credential_storage", "Credential storage", check_credential_storage),
    ("no_password_logs", "No plaintext password logs", check_no_password_logs),
    ("stdlib_dependencies", "Zero pip dependencies", check_stdlib_dependencies),
    ("catchup_capability_gate", "Catch-up capability gate",
     check_catchup_capability_gate),
    ("track_discovery_sandbox", "Track discovery sandbox",
     check_track_discovery_is_sandboxed),
    ("persistence_atomic", "Atomic persistence & versioning",
     check_persistence_atomic),
    ("historial_taxonomia_unica", "Single taxonomy for the technical history",
     check_historial_taxonomia_unica),
    ("historial_migracion_v2", "History schema v2 migration",
     check_historial_migracion_v2),
    ("historial_eventos_ciclo", "Playback lifecycle events",
     check_historial_eventos_ciclo_vida),
    ("historial_redaccion", "History redaction at write time",
     check_historial_redaccion_al_escribir),
    ("historial_retencion", "History retention limits",
     check_historial_retencion),
)


def run_checks(only: Sequence[str] | None = None) -> list[CheckResult]:
    """Ejecuta los checks en orden y nunca lanza.

    Args:
        only: claves concretas a ejecutar (por defecto, todas).

    Returns:
        Un :class:`CheckResult` por check, en orden de registro.
    """
    wanted = set(only) if only else None
    results: list[CheckResult] = []
    for key, label, func in CHECKS:
        if wanted is not None and key not in wanted:
            continue
        try:
            detail = func()
        except Exception as exc:  # noqa: BLE001 - un check jamás rompe el run
            results.append(CheckResult(key, label, False, f"{type(exc).__name__}: {exc}"))
        else:
            results.append(CheckResult(key, label, True, str(detail)))
    return results


def all_ok(results: Iterable[CheckResult]) -> bool:
    return all(r.ok for r in results)


def format_report(
    results: Sequence[CheckResult],
    *,
    use_color: bool = False,
    verbose: bool = False,
) -> str:
    """Informe tipo SDD §45. Devuelve ``SECURITY CHECK FAILED`` si algo falla."""
    ok_count = sum(1 for r in results if r.ok)
    total = len(results)
    green, red, dim, reset = ("", "", "", "")
    if use_color:
        green, red, dim, reset = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
    lines: list[str] = []
    for r in results:
        mark = PASSED_MARK if r.ok else FAILED_MARK
        color = green if r.ok else red
        lines.append(f"{color}{mark:<4}{reset}{r.label}")
        if verbose or not r.ok:
            if r.detail:
                lines.append(f"     {dim}{r.detail}{reset}")
    failed = total - ok_count
    if failed:
        lines.append("")
        lines.append(f"{red}{FAILED_BANNER} ({failed}/{total}){reset}")
    else:
        lines.append("")
        lines.append(f"{green}SECURITY CHECK PASSED ({ok_count}/{total}){reset}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: exit code 0 si todo pasa, 1 si algo falla, 2 si el uso es erróneo."""
    parser = argparse.ArgumentParser(
        prog="python -m thetvview.security.check",
        description="Comprobaciones de seguridad del SDD §45.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="informe en JSON (para CI)")
    parser.add_argument("--only", metavar="A,B", default="",
                        help="sólo estas claves (separadas por coma)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="muestra el detalle de los checks que pasan")
    parser.add_argument("--no-color", action="store_true", dest="no_color",
                        help="sin color ANSI")
    args = parser.parse_args(list(argv) if argv is not None else None)

    only = [part.strip() for part in args.only.split(",") if part.strip()]
    unknown = sorted(set(only) - {c[0] for c in CHECKS})
    if unknown:
        parser.error(f"checks desconocidos: {', '.join(unknown)}")

    results = run_checks(only or None)
    ok = all_ok(results)

    if args.as_json:
        print(json.dumps(
            {
                "passed": ok,
                "total": len(results),
                "results": [
                    {"key": r.key, "label": r.label, "ok": r.ok, "detail": r.detail}
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        use_color = not args.no_color and sys.stdout.isatty() and \
            not os.environ.get("NO_COLOR")
        print(format_report(results, use_color=use_color, verbose=args.verbose))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
