"""Parser M3U/M3U8 de IPTV (happy path, solo stdlib).

Contrato v1 (validado con iptv-epg-expert):
- parse_file acepta str|Path; abre con utf-8-sig + errors="replace"
  (resuelve BOM y encodings sucias sin crash).
- Falta #EXTM3U: se parsea igual.
- EXTINF huérfano (sin URL): descartado.
- Tags (#EXTVLCOPT/#KODIPROP) sin entrada pendiente: ignorados.
- Archivo vacío / solo comentarios: Playlist con channels=[].

TODO(m3u_parser) — diferidos deliberadamente:
- Resolver URLs relativas contra el directorio del playlist.
- Comillas simples / escapadas en atributos EXTINF.
- Detección robusta de la coma separadora cuando el nombre contiene
  comas dentro de atributos entrecomillados.
- Deduplicación de canales idénticos.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config
from .models import Channel, Playlist

# Atributos dobles "clave=valor" dentro del EXTINF (solo comillas dobles en v1).
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')

# Mapa de atributos conocidos -> campo de Channel. Normalizamos -/_ .
_KNOWN_ATTRS: dict[str, str] = {
    "tvg_id": "tvg_id",
    "tvg_name": "tvg_name",
    "tvg_logo": "tvg_logo",
    "group_title": "group",
}

# Tags que se acumulan en extra_options (pares ordenados, admite repetidos).
_OPTION_TAGS = ("#EXTVLCOPT:", "#KODIPROP:")

# Extensiones de lista aceptadas (solo stdlib, sin preguntar al usuario).
# .ts se incluye porque muchos proveedores Xtream sirven streams/listados
# MPEG-TS; a nivel de usuario sigue siendo "una lista", sin tecnicismos.
PLAYLIST_EXTENSIONS: tuple[str, ...] = (".m3u", ".m3u8", ".ts")


def _is_ts_source(source: str | None) -> bool:
    """True si el origen apunta a un .ts (path o URL, con o sin query)."""
    if not source:
        return False
    return source.strip().lower().split("?", 1)[0].endswith(".ts")


def is_valid_playlist_source(source: str) -> bool:
    """True si `source` es una lista válida: URL http(s) o fichero
    .m3u/.m3u8/.ts. Las URLs http(s) siempre se aceptan (el servidor puede
    servir M3U con cualquier nombre, p. ej. get.php?output=ts)."""
    s = (source or "").strip()
    if not s:
        return False
    if s.lower().startswith(("http://", "https://")):
        return True
    return s.lower().split("?", 1)[0].endswith(PLAYLIST_EXTENSIONS)


def _clean_group(raw: str | None) -> str | None:
    """Normaliza el group-title: vacío o '-' (Xtream) => sin grupo."""
    if raw is None:
        return None
    value = raw.strip()
    return value if value and value != "-" else None


def _split_extinf(rest: str) -> tuple[dict[str, str], str]:
    """Separa `#EXTINF:<rest>` en (attrs, nombre).

    Estrategia v1: la coma separadora es la última coma de la línea;
    los nombres IPTV rara vez contienen comas y esto evita romper cuando
    los valores de atributos incluyen comas entrecomilladas.
    """
    comma_idx = rest.rfind(",")
    if comma_idx == -1:
        # Sin nombre tras la coma: attrs vacíos, nombre vacío.
        return {}, ""
    attr_part, name = rest[:comma_idx].strip(), rest[comma_idx + 1 :].strip()
    # Fix bug real: tvg-logo="tvg-logo="https://..." (comillas anidadas)
    # Aparece en IPTVSV.m3u para un canal de Paraguay.
    if 'tvg-logo="tvg-logo="' in attr_part:
        attr_part = attr_part.replace('tvg-logo="tvg-logo="', 'tvg-logo="')
    # Variante con espacios o mayúsculas raras: regex fallback
    attr_part = re.sub(r'tvg-logo="\s*tvg-logo="', 'tvg-logo="', attr_part, flags=re.IGNORECASE)
    attrs = {k.lower().replace("-", "_"): v for k, v in _ATTR_RE.findall(attr_part)}
    return attrs, name


def parse_text(text: str, source: str | None = None, name: str | None = None) -> Playlist:
    """Parsea el contenido M3U desde memoria (útil para tests y parse_url)."""
    playlist = Playlist(name=name or source or "playlist", source=source)
    # Estado de la "entrada pendiente": EXTINF visto y opciones acumuladas
    # hasta que aparezca su URL.
    current_extinf: tuple[str, dict[str, str]] | None = None  # (nombre, attrs)
    current_options: list[tuple[str, str]] = []

    for raw_line in text.splitlines():  # normaliza \r\n, \r y \n
        line = raw_line.strip()
        if not line:
            continue

        upper = line.upper()
        if upper.startswith("#EXTM3U"):
            # Cabecera: puede traer x-tvg-url (EPG asociado a la lista).
            for key, value in _ATTR_RE.findall(line[len("#EXTM3U") :]):
                if key.lower() == "x-tvg-url" and value.strip():
                    playlist.epg_url = value.strip()
        elif upper.startswith("#EXTINF:"):
            # Nueva entrada: descarta cualquier EXTINF pendiente sin URL.
            attrs, ch_name = _split_extinf(line[len("#EXTINF:") :])
            current_extinf = (ch_name, attrs)
            current_options = []
        elif any(upper.startswith(tag) for tag in _OPTION_TAGS):
            tag = next(t for t in _OPTION_TAGS if upper.startswith(t))
            if current_extinf is not None:
                current_options.append((tag.rstrip(":").lstrip("#"), line[len(tag) :]))
            # Huérfano (sin EXTINF previo): ignorado silenciosamente.
        elif line.startswith("#"):
            # Otros tags (#EXTM3U, #EXTGRP, #PLAYLIST, comentarios): TODO(#EXTGRP).
            continue
        else:
            # Línea no-comentario => URL que cierra la entrada actual.
            if current_extinf is not None:
                ch_name, attrs = current_extinf
                known: dict[str, str] = {}
                extra: dict[str, str] = {}
                for key, value in attrs.items():
                    field_name = _KNOWN_ATTRS.get(key)
                    if field_name:
                        # Valores vacíos (tvg-logo="" ) -> None, no cadena vacía.
                        if not value.strip():
                            continue
                        # Sanitiza tvg-logo que arranca con 'tvg-logo=' residual
                        if field_name == "tvg_logo" and value.strip().startswith("tvg-logo="):
                            cleaned = value.strip()[len("tvg-logo="):].strip().lstrip('"').strip()
                            if not cleaned:
                                continue
                            value = cleaned
                        known[field_name] = value
                    else:
                        extra[key] = value
                # Normaliza tvg_logo vacío -> None (51 casos en IPTVSV.m3u)
                tvg_logo_val = known.get("tvg_logo")
                if tvg_logo_val is not None and not tvg_logo_val.strip():
                    tvg_logo_val = None
                tvg_id_val = known.get("tvg_id")
                if tvg_id_val is not None and not tvg_id_val.strip():
                    tvg_id_val = None
                tvg_name_val = known.get("tvg_name")
                if tvg_name_val is not None and not tvg_name_val.strip():
                    tvg_name_val = None
                playlist.channels.append(
                    Channel(
                        name=ch_name or line,
                        url=line,
                        tvg_id=tvg_id_val,
                        tvg_name=tvg_name_val,
                        tvg_logo=tvg_logo_val,
                        # Portales Xtream usan group-title="-" como placeholder.
                        group=_clean_group(known.get("group")),
                        radio=attrs.get("radio", "").lower() == "true",
                        attrs=extra,
                        extra_options=current_options,
                    )
                )
            current_extinf = None
            current_options = []

    # EXTINF final sin URL: descartado por contrato.
    if not playlist.channels and _is_ts_source(source):
        # Fuente .ts sin entradas M3U: sigue siendo una lista válida de
        # un solo stream (sin preguntar nada al usuario).
        # 1) Si el texto trae una URL suelta (sin EXTINF), úsala.
        # 2) Si es binario MPEG-TS u otro texto sin URL, el stream es el
        #    propio origen.
        single_url: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip().strip("\x00").strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower()
            if low.startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://", "ffmpeg://")):
                single_url = line
                break
            # Ruta local suelta: solo si es texto imprimible con pinta de
            # path/URL de stream (evita usar basura binaria como URL).
            if (
                line.isprintable()
                and len(line) < 512
                and "\x00" not in line
                and (
                    low.split("?", 1)[0].endswith(PLAYLIST_EXTENSIONS + (".mp4", ".mkv", ".avi"))
                    or line.startswith(("/", "./", "../", "~"))
                )
            ):
                single_url = line
                break
        playlist.channels.append(
            Channel(
                name=playlist.name,
                url=single_url or (source or playlist.name),
            )
        )
    return playlist


def parse_file(path: str | Path) -> Playlist:
    """Parsea un archivo M3U/M3U8/TS local a un Playlist.

    Los .ts son listas válidas: si traen M3U se parsean normal; si son
    un stream suelto o binario MPEG-TS se devuelven como lista de un
    solo canal apuntando al propio origen.
    Nunca lanza por contenido malformado; solo por errores de E/S.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise OSError(f"No se pudo leer la playlist '{p}': {exc.strerror or exc}") from exc
    return parse_text(text, source=str(p), name=p.stem)


DEFAULT_TIMEOUT: float = 30.0

# Cache en disco para listas remotas (como el EPG): evita re-descargar
# un get.php de varios MB en cada apertura. TTL 6h por defecto.
DEFAULT_TTL_HOURS: float = 6.0

# Sin límite de tamaño: las listas grandes (p. ej. 50k canales) se
# descargan completas por chunks y se cachean en disco con TTL; el lag
# de la TUI con listas grandes se evita por otro lado (render solo de
# lo visible + favoritas en memoria, sin IO por canal).
_CHUNK_SIZE: int = 512 * 1024

_GZIP_MAGIC = b"\x1f\x8b"


def _cache_path_for(url: str, cache_dir: Path) -> Path:
    """Nombre seguro y determinista para la entrada de cache de una URL."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    base = url.split("?", 1)[0].lower()
    suffix = ".ts" if base.endswith(".ts") else ".m3u"
    return cache_dir / f"playlist_{digest}{suffix}"


def _is_fresh(path: Path, ttl_seconds: float, now: float | None = None) -> bool:
    """True si el archivo de cache existe y su edad es menor que el TTL."""
    if not path.is_file():
        return False
    ref = time.time() if now is None else now
    try:
        return (ref - path.stat().st_mtime) < ttl_seconds
    except OSError:
        return False


def _decompress_and_decode(data: bytes, url_hint: str) -> str:
    """Descomprime gzip (por magia o sufijo .gz) y decodifica a texto."""
    looks_gz = url_hint.lower().split("?", 1)[0].endswith(".gz") or data[:2] == _GZIP_MAGIC
    if looks_gz:
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise OSError(f"La playlist descargada no es un gzip válido: {exc}") from exc
    return data.decode("utf-8-sig", errors="replace")


def _playlist_name_for(url: str) -> str:
    name = Path(url.split("?", 1)[0]).stem or url
    return name


def fetch_bytes(url: str, timeout: float = DEFAULT_TIMEOUT,
                max_bytes: int | None = None) -> bytes:
    """Descarga los bytes crudos de una playlist (solo http/https).

    - Envía `Accept-Encoding: gzip` y descomprime el transporte gzip.
    - Siempre con timeout; sin límite de tamaño por defecto: lee por
      chunks hasta EOF para soportar listas grandes sin picos de memoria
      por el truco de `read(n+1)`.
    - `max_bytes` queda como parámetro obsoleto por compatibilidad:
      si se pasa un entero, se respeta como tope (lanza OSError
      amigable al superarlo); por defecto (None) no hay tope.
    - Lanza ValueError para URLs no soportadas u OSError amigable.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"URL no soportada (solo http/https): '{url}'")

    req = urllib.request.Request(url, headers={
        "User-Agent": "theTVVIEW/1.0",
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            chunks: list[bytes] = []
            total = 0
            while True:
                piece = resp.read(_CHUNK_SIZE)
                if not piece:
                    break
                chunks.append(piece)
                total += len(piece)
                if max_bytes is not None and total > max_bytes:
                    raise OSError(
                        f"La playlist supera el límite de {max_bytes // (1024 * 1024)} MB; "
                        "el servidor devolvió una lista demasiado grande."
                    )
            raw = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise OSError(
            f"El servidor respondió {exc.code} {exc.reason} al descargar la playlist."
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise OSError(
                f"Tiempo de espera agotado ({timeout:g}s) al descargar la playlist."
            ) from exc
        raise OSError(f"No se pudo conectar al servidor de la playlist: {reason}") from exc
    except TimeoutError as exc:  # timeouts que urlopen propaga directamente
        raise OSError(f"Tiempo de espera agotado ({timeout:g}s) al descargar la playlist.") from exc

    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise OSError(f"La playlist descargada no es un gzip válido: {exc}") from exc
    return raw


def parse_url(url: str, timeout: float = DEFAULT_TIMEOUT) -> Playlist:
    """Descarga y parsea un M3U/M3U8/TS remoto (solo http/https).

    Las URLs .ts son listas válidas (p. ej. get.php?output=ts o
    /live/u/p/123.ts): si no traen M3U se devuelven como lista de un
    solo canal. Lanza ValueError para URLs no soportadas u OSError con mensaje
    amigable ante fallos de red/HTTP. Nunca cuelga: siempre hay timeout.

    Nota: para uso en la TUI se prefiere `load_url` (con cache TTL),
    que evita re-descargar listas grandes en cada apertura.
    """
    url = url.strip()
    raw = fetch_bytes(url, timeout=timeout)
    name = _playlist_name_for(url)
    return parse_text(_decompress_and_decode(raw, url), source=url, name=name)


def load_url(
    url: str,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    force_refresh: bool = False,
    cache_dir: Path | None = None,
) -> Playlist:
    """Descarga una playlist por URL usando cache local con TTL.

    - Si hay copia en cache con menos de `ttl_hours`, se usa sin red
      (apertura instantánea, ideal para get.php lentos).
    - Si no, se descarga (con timeout), se guarda en cache y se parsea.
    - Con `ttl_hours <= 0` o `force_refresh=True` siempre re-descarga.
    - Si la descarga falla pero hay cache previa (aunque caducada),
      se devuelve la cache como fallback en vez de romper ("a veces
      no funciona" por paneles saturados).
    - `cache_dir` permite tests; por defecto es config.PLAYLIST_CACHE_DIR.

    Errores de red/HTTP sin cache disponible se propagan como OSError
    con mensaje amigable; URLs no http(s) lanzan ValueError.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"URL no soportada (solo http/https): '{url}'")
    directory = config.PLAYLIST_CACHE_DIR if cache_dir is None else Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = _cache_path_for(url, directory)
    name = _playlist_name_for(url)

    fresh = ttl_hours > 0 and not force_refresh and _is_fresh(path, ttl_hours * 3600)
    if fresh:
        try:
            text = _decompress_and_decode(path.read_bytes(), url)
            return parse_text(text, source=url, name=name)
        except (OSError, ValueError):
            pass  # cache corrupta: re-descargar

    try:
        raw = fetch_bytes(url, timeout=timeout)
    except (OSError, ValueError):
        # Fallback stale: el panel a veces cae o tarda; mejor abrir
        # la última copia conocida que no abrir nada.
        if path.is_file():
            try:
                text = _decompress_and_decode(path.read_bytes(), url)
                return parse_text(text, source=url, name=name)
            except (OSError, ValueError):
                pass
        raise

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(raw)
        tmp.replace(path)
    except OSError:
        pass  # si no se puede cachear, igual se parsea lo descargado

    return parse_text(_decompress_and_decode(raw, url), source=url, name=name)
