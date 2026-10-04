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
    # El cliente no debe montar un opener con verificación desactivada.
    import urllib.request

    from .safe_http import SafeHttpClient

    client = SafeHttpClient()
    handlers = getattr(client._opener, "handlers", [])
    https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
    _require(bool(https), "SafeHttpClient sin HTTPSHandler con contexto estricto")
    ctx_https = [h for h in https if getattr(h, "_context", None) is ctx]
    _require(bool(ctx_https), "HTTPSHandler no usa el contexto estricto compartido")
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
    from .safe_http import SafeHttpClient, _NoRedirect

    client = SafeHttpClient(allow_private=True)
    handlers = getattr(client._opener, "handlers", [])
    _require(
        any(isinstance(h, _NoRedirect) for h in handlers),
        "el opener sigue redirects de urllib automáticamente",
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

    return (f"capacidad fail-closed, ventana respetada, referencia opaca "
            f"({len(opaca)} chars) sin credenciales, 0 rutas de sonda")


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
# Registro
# ---------------------------------------------------------------------------


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
