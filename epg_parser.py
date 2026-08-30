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

TODO(epg_parser) — diferidos deliberadamente:
- credits, episode-num, ratings y otros campos menores de XMLTV.
"""

from __future__ import annotations

import gzip
import hashlib
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import Program

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
    """Parsea contenido XMLTV desde memoria."""
    try:
        root = ET.fromstring(text)
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
    """Lee un archivo XMLTV local (.xml/.xmltv, opcionalmente .gz)."""
    p = Path(path)
    try:
        if p.suffix.lower() == ".gz":
            with gzip.open(p, mode="rt", encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        else:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, EOFError) as exc:
        raise OSError(f"No se pudo leer el EPG '{p}': {exc.strerror or exc}") from exc
    return parse_text(text)


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
    """Descomprime gzip (por magia o sufijo .gz) y decodifica a texto."""
    looks_gz = name_hint.lower().split("?", 1)[0].endswith(".gz") or data[:2] == _GZIP_MAGIC
    if looks_gz:
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise OSError(f"El EPG descargado no es un gzip válido: {exc}") from exc
    return data.decode("utf-8-sig", errors="replace")


def fetch_bytes(url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Descarga los bytes crudos del EPG (solo http/https, con timeout).

    Lanza ValueError para URLs no soportadas u OSError con mensaje
    amigable ante fallos de red/HTTP.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"URL no soportada (solo http/https): '{url}'")

    req = urllib.request.Request(url, headers={"User-Agent": "theTVVIEW/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
    except urllib.error.HTTPError as exc:
        raise OSError(
            f"El servidor respondió {exc.code} {exc.reason} al descargar el EPG."
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise OSError(
                f"Tiempo de espera agotado ({timeout:g}s) al descargar el EPG."
            ) from exc
        raise OSError(f"No se pudo conectar al servidor del EPG: {reason}") from exc
    except TimeoutError as exc:  # timeouts que urlopen propaga directamente
        raise OSError(f"Tiempo de espera agotado ({timeout:g}s) al descargar el EPG.") from exc

    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise OSError(f"El EPG descargado no es un gzip válido: {exc}") from exc
    return raw


def load_url(
    url: str,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    force_refresh: bool = False,
    cache_dir: Path | None = None,
) -> Epg:
    """Descarga un EPG por URL usando cache local con TTL.

    - Si hay copia en cache con menos de `ttl_hours`, se usa sin red.
    - Si no, se descarga (con timeout), se guarda en cache y se parsea.
    - Con `ttl_hours <= 0` o `force_refresh=True` siempre re-descarga.
    - `cache_dir` permite tests; por defecto es config.EPG_CACHE_DIR.

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

    raw = fetch_bytes(url, timeout=timeout)

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(raw)
        tmp.replace(path)
    except OSError as exc:
        raise OSError(f"No se pudo guardar el cache del EPG '{path}': {exc}") from exc

    return parse_text(_decompress_and_decode(raw, url))
