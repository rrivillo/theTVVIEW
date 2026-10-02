"""Parser XMLTV (EPG) con soporte .gz, solo stdlib.

Contrato v1.1:
- parse_file acepta .xml/.xmltv y variantes .gz (se descomprimen con gzip).
- start/stop con offset tz se convierten a datetime consciente; sin offset,
  se asume UTC.
- programme sin stop: se rellena con el start del siguiente programa del
  mismo canal; el último queda con stop=None.
- Canales indexados por id con su primer display-name y primer icon (<icon src>).
- Programas con sub-title y categorías (en orden del feed).
- now_playing trata el último programa (sin stop) como abierto; next_programme
  da el fallback "a continuación" en consulta.
- Malformed: elementos inválidos se saltan sin romper el parseo.
- split_sources/resolve_source normalizan las fuentes EPG que declara una
  playlist (http, file://, path absoluto o relativo) en cualquier SO.

TODO(epg_parser) — diferidos deliberadamente:
- credits, episode-num, ratings y otros campos menores de XMLTV.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import Program
from .security.errors import IPTVError
from .security.limits import get_limits
from .security.local_files import gunzip_limited, read_limited_text
from .security.redaction import redact_text
from .security.safe_http import SafeHttpClient
from .security.url_policy import PURPOSE_METADATA
from .security.xml_safe import parse_xml

_FMT = "%Y%m%d%H%M%S %z"
_FMT_NO_TZ = "%Y%m%d%H%M%S"


def _parse_ts(raw: str | None) -> datetime | None:
    """Convierte 'YYYYMMDDHHMMSS ±ZZZZ' a datetime aware (UTC si no hay tz)."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        if " " in raw:
            return datetime.strptime(raw, _FMT)
        return datetime.strptime(raw[:14], _FMT_NO_TZ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class Epg:
    """EPG indexado por channel_id."""

    channels_by_id: dict[str, str]  # id -> display-name
    programs: dict[str, list[Program]]
    icons_by_id: dict[str, str] = field(default_factory=dict)  # id -> icon src

    def channel_name(self, channel_id: str) -> str | None:
        return self.channels_by_id.get(channel_id)

    def channel_icon(self, channel_id: str) -> str | None:
        """URL del primer <icon src> declarado para el canal, si hay."""
        return self.icons_by_id.get(channel_id)

    def programmes_for(self, channel_id: str) -> list[Program]:
        return self.programs.get(channel_id, [])

    def now_playing(self, channel_id: str, when: datetime) -> Program | None:
        for prog in self.programmes_for(channel_id):
            if prog.start <= when and (prog.stop is None or when < prog.stop):
                return prog
        return None

    def next_programme(self, channel_id: str, when: datetime) -> Program | None:
        """Primer programa que empieza estrictamente después de `when`.

        Fallback "a continuación" en consulta: también sirve para acotar un
        programa cuyo stop es None (el último de la parrilla).
        """
        for prog in self.programmes_for(channel_id):
            if prog.start > when:
                return prog
        return None


def _text_of(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    text = (el.text or "").strip()
    return text or None


def parse_text(text: str) -> Epg:
    """Parsea contenido XMLTV desde memoria.

    Usa :func:`thetvview.security.xml_safe.parse_xml`, que rechaza
    ``<!DOCTYPE>``/``<!ENTITY>`` y acota profundidad y número de nodos
    (gap B6). Un XML mal formado sigue lanzando ``ValueError`` con el
    mensaje de siempre.
    """
    try:
        root = parse_xml(text)
    except ET.ParseError as exc:
        raise ValueError(f"XMLTV inválido: {exc}") from exc

    channels: dict[str, str] = {}
    icons: dict[str, str] = {}
    for ch in root.iter("channel"):
        cid = ch.get("id")
        if not cid:
            continue
        display = _text_of(ch.find("display-name"))
        if display and cid not in channels:
            channels[cid] = display
        if cid not in icons:
            icon_el = ch.find("icon")
            src = icon_el.get("src", "").strip() if icon_el is not None else ""
            if src:
                icons[cid] = src

    by_channel: dict[str, list[Program]] = {}
    for node in root.iter("programme"):
        cid = node.get("channel") or ""
        start = _parse_ts(node.get("start"))
        if not cid or start is None:
            continue  # sin canal o sin fecha válida: entrada inservible
        title = _text_of(node.find("title")) or "(sin título)"
        categories = [
            cat for cat in (_text_of(el) for el in node.findall("category")) if cat
        ]
        by_channel.setdefault(cid, []).append(
            Program(
                channel_id=cid,
                title=title,
                start=start,
                stop=_parse_ts(node.get("stop")),
                desc=_text_of(node.find("desc")),
                sub_title=_text_of(node.find("sub-title")),
                categories=categories,
            )
        )

    # Rellenar stops ausentes con el start del siguiente programa del canal.
    for progs in by_channel.values():
        progs.sort(key=lambda p: p.start)
        for prev, nxt in zip(progs, progs[1:]):
            if prev.stop is None:
                prev.stop = nxt.start

    return Epg(channels_by_id=channels, programs=by_channel, icons_by_id=icons)


def parse_file(path: str | Path) -> Epg:
    """Lee un archivo XMLTV local (.xml/.xmltv, opcionalmente .gz).

    La lectura pasa por :func:`thetvview.security.local_files.read_limited_text`:
    fichero regular, sin symlinks y con límite de tamaño tanto comprimido
    como descomprimido (gap B11). Los fallos salen como ``OSError`` con
    mensaje ya amable (incluye la ruta).
    """
    return parse_text(read_limited_text(Path(path)))


# --- Referencias EPG declaradas en la cabecera de una playlist -------------

# Atributos de #EXTM3U que traen el EPG asociado (alias reales en el campo).
SOURCE_ATTRS: frozenset[str] = frozenset(
    {"x-tvg-url", "url-tvg", "tvg-url", "x-url-tvg", "x-epg-url", "epg-url"}
)

# Separadores admitidos entre varias URLs EPG dentro del mismo atributo.
_SPLIT_RE = re.compile(r"[\s|;]+")

# Cadenas que pueden ser una fuente EPG (para no trocear URLs con comas).
_PATH_ENDINGS = (".xml", ".xmltv", ".gz", ".xz", ".bz2", ".zip", ".json")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
# /C:/ruta (file:///C:/ruta ya pasado por url2pathname en un SO POSIX).
_FILE_WIN_ABS_RE = re.compile(r"^/[A-Za-z]:[\\/]")


def _looks_like_source(part: str) -> bool:
    """True si `part` tiene pinta de path/URL de EPG (no un fragmento suelto)."""
    low = part.lower()
    if low.startswith(("http://", "https://", "file://", "//")):
        return True
    if _WINDOWS_ABS_RE.match(part):
        return True
    path = low.split("?", 1)[0].split("#", 1)[0]
    return path.endswith(_PATH_ENDINGS) or "/" in part or "\\" in part


def split_sources(value: str) -> list[str]:
    """Trocea el valor de un atributo EPG en varias fuentes.

    Admite separación por espacio, `|`, `;` y `,`. Una coma solo trocea si
    todas las piezas siguen pareciendo fuentes: así no se rompen URLs con
    comas dentro de la query (p. ej. `guia.xml?ids=a,b`).
    """
    refs: list[str] = []
    for chunk in (c for c in _SPLIT_RE.split((value or "").strip()) if c):
        chunk = chunk.strip(",")
        pieces = [p for p in chunk.split(",") if p]
        if len(pieces) > 1 and all(_looks_like_source(p) for p in pieces):
            refs.extend(pieces)
        elif pieces:
            refs.append(chunk)
    return refs


def resolve_source(source: str | None, *, base: str | None = None) -> str | None:
    """Normaliza una referencia EPG a algo que `parse_file`/`load_url` entiende.

    Multiplataforma (sin dependencias del SO en uso):
    - `http(s)://`  → se devuelve tal cual (descarga con cache TTL).
    - `file:///C:/guia.xml` → path local del SO (percent-decoding + UNC).
    - Absoluta POSIX (`/opt/guia.xml`), Windows (`C:\\guia.xml`) o UNC
      (`\\\\srv\\guia.xml`) → tal cual, aunque el SO actual sea otro.
    - Relativa (`guia.xml.gz`) → se resuelve contra `base`:
      directorio del fichero de la playlist, o `urljoin` si la playlist
      es remota. Las barras invertidas se normalizan a `/` para que una
      lista creada en Windows abra también en Linux/macOS.

    Devuelve None si no hay referencia que usar.
    """
    ref = (source or "").strip().strip("\"'").strip()
    if not ref:
        return None
    low = ref.lower()
    if low.startswith(("http://", "https://")):
        return ref
    if low.startswith("file://"):
        parsed = urllib.parse.urlparse(ref)
        path = urllib.request.url2pathname(parsed.path)
        # file:///C:/guia.xml en un SO POSIX deja la barra inicial de más.
        if _FILE_WIN_ABS_RE.match(path):
            path = path[1:]
        host = parsed.netloc
        if host and host.lower() != "localhost":
            # file://servidor/compartido/x.xml → UNC //servidor/compartido/x.xml
            path = f"//{host}{path}"
        return path or None
    if ref.startswith(("/", "\\\\", "//")) or _WINDOWS_ABS_RE.match(ref):
        return ref  # absoluta (otro SO o la propia): no se toca
    if not base:
        return ref
    base = base.strip()
    if base.lower().startswith(("http://", "https://")):
        return urllib.parse.urljoin(base, ref.replace("\\", "/"))
    # `\` → `/` (aceptado por Windows) para que una lista creada en un SO
    # con separadores distintos resuelva igual en el actual.
    joined = Path(base).expanduser().parent / ref.replace("\\", "/")
    try:
        return os.path.normpath(str(joined))
    except (OSError, ValueError):
        return str(joined)


# --- Descarga por URL con cache TTL ---------------------------------------

DEFAULT_TIMEOUT: float = 15.0
DEFAULT_TTL_HOURS: float = 12.0

_GZIP_MAGIC = b"\x1f\x8b"


def _cache_path_for(url: str, cache_dir: Path) -> Path:
    """Nombre seguro y determinista para la entrada de cache de una URL."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    suffix = ".gz" if url.split("?", 1)[0].lower().endswith(".gz") else ".xml"
    return cache_dir / f"epg_{digest}{suffix}"


def _is_fresh(path: Path, ttl_seconds: float, now: float | None = None) -> bool:
    """True si el archivo de cache existe y su edad es menor que el TTL."""
    if not path.is_file():
        return False
    ref = time.time() if now is None else now
    try:
        return (ref - path.stat().st_mtime) < ttl_seconds
    except OSError:
        return False


def _decompress_and_decode(data: bytes, name_hint: str) -> str:
    """Descomprime gzip (por magia o sufijo .gz) y decodifica a texto.

    La descompresión va acotada por ``limits.max_file_bytes``: un EPG.gz
    servido como contenido (sin cabecera Content-Encoding) no puede
    expandirse en memoria sin tope.
    """
    looks_gz = name_hint.lower().split("?", 1)[0].endswith(".gz") or data[:2] == _GZIP_MAGIC
    if looks_gz:
        data = gunzip_limited(data, what="El EPG descargado")
    return data.decode("utf-8-sig", errors="replace")


def fetch_bytes(url: str, timeout: float = DEFAULT_TIMEOUT, *,
                allow_private: bool = False) -> bytes:
    """Descarga los bytes crudos del EPG (solo http/https, con timeout).

    Pasa por :class:`thetvview.security.safe_http.SafeHttpClient`: política
    de URL, anti-SSRF (B3), redirects revalidados, TLS estricto y lectura
    acotada a ``limits.max_file_bytes``. Es la fuente que elige la propia
    playlist (`x-tvg-url`), así que la validación es obligatoria.

    Lanza ValueError para URLs no soportadas u OSError con mensaje
    amigable ante fallos de red/HTTP.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(
            f"URL no soportada (solo http/https): '{redact_text(url)}'"
        )

    client = SafeHttpClient(
        allow_private=allow_private,
        max_bytes=get_limits().max_file_bytes,
        purpose=PURPOSE_METADATA,
    )
    try:
        return client.get_bytes(url, timeout=timeout)
    except (OSError, ValueError):
        raise
    except IPTVError as exc:
        # RateLimitError/ConnectionLimitError no heredan de OSError y el
        # contrato de esta función es "OSError amigable".
        raise OSError(str(exc)) from exc


def load_url(
    url: str,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    force_refresh: bool = False,
    cache_dir: Path | None = None,
    allow_private: bool = False,
) -> Epg:
    """Descarga un EPG por URL usando cache local con TTL.

    - Si hay copia en cache con menos de `ttl_hours`, se usa sin red.
    - Si no, se descarga (con timeout), se guarda en cache y se parsea.
    - Con `ttl_hours <= 0` o `force_refresh=True` siempre re-descarga.
    - `cache_dir` permite tests; por defecto es config.EPG_CACHE_DIR.
    - `allow_private` habilita red privada/loopback solo si la fuente lo
      tiene declarado (SDD §11); por defecto está bloqueado.

    Errores de red/HTTP se propagan como OSError con mensaje amigable;
    URLs no http(s) lanzan ValueError.
    """
    url = url.strip()
    directory = config.EPG_CACHE_DIR if cache_dir is None else Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = _cache_path_for(url, directory)

    fresh = ttl_hours > 0 and not force_refresh and _is_fresh(path, ttl_hours * 3600)
    if fresh:
        try:
            text = _decompress_and_decode(path.read_bytes(), path.name)
            return parse_text(text)
        except (OSError, ValueError):
            pass  # cache corrupta: re-descargar

    raw = fetch_bytes(url, timeout=timeout, allow_private=allow_private)

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(raw)
        tmp.replace(path)
    except OSError as exc:
        raise OSError(f"No se pudo guardar el cache del EPG '{path}': {exc}") from exc

    return parse_text(_decompress_and_decode(raw, url))
