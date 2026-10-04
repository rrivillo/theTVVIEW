"""Parser M3U/M3U8 de IPTV (happy path, solo stdlib).

Contrato v1 (validado con iptv-epg-expert):
- parse_file acepta str|Path; abre con utf-8-sig + errors="replace"
  (resuelve BOM y encodings sucias sin crash).
- Falta #EXTM3U: se parsea igual.
- EXTINF huérfano (sin URL): descartado.
- Tags (#EXTVLCOPT/#KODIPROP) sin entrada pendiente: ignorados.
- Archivo vacío / solo comentarios: Playlist con channels=[].
- Cabecera #EXTM3U: EPG declarado en x-tvg-url / url-tvg / tvg-url (con o
  sin comillas, una o varias fuentes) queda en Playlist.epg_urls, ya
  resuelto contra el origen de la lista (ver epg_parser.resolve_source).
- Metadatos de catch-up (`catchup`, `catchup-days`, `catchup-source`): el
  parser **no los interpreta**, sólo los deja en `Channel.attrs` con el
  guion normalizado a guion bajo (`catchup_days`, `catchup_source`; ver
  `_split_extinf`). Quien los consume es `thetvview.catchup`
  (`from_m3u_attrs`), que es el único que decide si un canal tiene archivo
  declarado. Un `catchup*` mal formado no rompe el parseo: simplemente
  acaba sin capacidad.

TODO(m3u_parser) — diferidos deliberadamente:
- Resolver URLs relativas contra el directorio del playlist.
- Comillas simples / escapadas en atributos EXTINF.
- Detección robusta de la coma separadora cuando el nombre contiene
  comas dentro de atributos entrecomillados.
- Deduplicación de canales idénticos.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from . import config
from .cam_ref import registrar_camara
from .epg_parser import SOURCE_ATTRS, resolve_source, split_sources
from .models import Channel, Playlist
from .security.errors import IPTVError, ParseError
from .security.limits import get_limits
from .security.local_files import gunzip_limited, read_limited_text
from .security.redaction import redact_text
from .security.safe_http import SafeHttpClient
from .security.url_policy import PURPOSE_METADATA

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

# key="valor" | key='valor' | key=valor en la cabecera #EXTM3U.
# La forma sin comillas es la de muchas listas (url-tvg=http://...).
_HEADER_ATTR_RE = re.compile(r'([\w-]+)=(?:"([^"]*)"|\'([^\']*)\'|([^\s"\']+))')


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


def _read_header_epg(playlist: Playlist, header: str) -> None:
    """Extrae las fuentes EPG declaradas en la cabecera #EXTM3U.

    Acepta los alias reales del campo (x-tvg-url, url-tvg, tvg-url...),
    valores con o sin comillas y varias fuentes en el mismo atributo.
    Cada referencia se resuelve contra el origen de la lista (directorio
    del fichero o la propia URL) para que funcione en cualquier SO.
    """
    for match in _HEADER_ATTR_RE.finditer(header):
        if match.group(1).lower() not in SOURCE_ATTRS:
            continue
        raw = match.group(2) or match.group(3) or match.group(4) or ""
        for ref in split_sources(raw):
            resolved = resolve_source(ref, base=playlist.source)
            if resolved and resolved not in playlist.epg_urls:
                playlist.epg_urls.append(resolved)
    if playlist.epg_urls and not playlist.epg_url:
        playlist.epg_url = playlist.epg_urls[0]


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
    if '"' not in attr_part:
        # Sin comillas no puede haber pares clave="valor": evita el regex.
        return {}, name
    # Fix bug real: tvg-logo="tvg-logo="https://..." (comillas anidadas)
    # Aparece en IPTVSV.m3u para un canal de Paraguay.
    if 'tvg-logo="tvg-logo="' in attr_part:
        attr_part = attr_part.replace('tvg-logo="tvg-logo="', 'tvg-logo="')
    # Variante con espacios o mayúsculas raras: regex fallback.
    # Solo se ejecuta si quedan DOS 'tvg-logo="' (necesario para que la
    # regex pueda coincidir): en listas grandes el re.sub por línea costaba
    # ~0,2 s sobre 96k canales y casi nunca encontraba nada.
    if attr_part.lower().count('tvg-logo="') > 1:
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
    channels = playlist.channels
    max_entries = get_limits().max_entries
    # Prefijos cacheados: se comprueba el '#' antes de subir a mayúsculas
    # (una copia entera de la línea) porque las URLs son mitad de un M3U.
    extm3u, extinf = "#EXTM3U", "#EXTINF:"
    vlc_opt, kodi_opt = _OPTION_TAGS

    for raw_line in text.splitlines():  # normaliza \r\n, \r y \n
        line = raw_line.strip()
        if not line:
            continue

        if line[0] != "#":
            # Línea no-comentario => URL que cierra la entrada actual.
            if current_extinf is not None:
                ch_name, attrs = current_extinf
                known: dict[str, str] = {}
                extra: dict[str, str] = {}
                for key, value in attrs.items():
                    field_name = _KNOWN_ATTRS.get(key)
                    if field_name:
                        # Se strippea una sola vez (antes se llamaba hasta 4
                        # veces por atributo: era de lo más caro del parseo).
                        stripped = value.strip()
                        # Valores vacíos (tvg-logo="" ) -> None, no cadena vacía.
                        if not stripped:
                            continue
                        # Sanitiza tvg-logo que arranca con 'tvg-logo=' residual
                        if field_name == "tvg_logo" and stripped.startswith("tvg-logo="):
                            cleaned = stripped[len("tvg-logo="):].strip().lstrip('"').strip()
                            if not cleaned:
                                continue
                            known[field_name] = cleaned
                            continue
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
                if len(channels) >= max_entries:
                    raise ParseError(
                        f"La lista supera el límite de {max_entries} entradas; "
                        "se rechaza por seguridad."
                    )
                channels.append(
                    Channel(
                        name=ch_name or line,
                        url=registrar_camara(playlist.name, line),
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
            continue

        upper = line.upper()
        if upper.startswith(extm3u):
            # Cabecera: puede traer el EPG asociado a la lista
            # (x-tvg-url, url-tvg, tvg-url... con una o varias fuentes).
            _read_header_epg(playlist, line[len(extm3u) :])
        elif upper.startswith(extinf):
            # Nueva entrada: descarta cualquier EXTINF pendiente sin URL.
            attrs, ch_name = _split_extinf(line[len(extinf) :])
            current_extinf = (ch_name, attrs)
            current_options = []
        elif upper.startswith(vlc_opt) or upper.startswith(kodi_opt):
            tag = vlc_opt if upper.startswith(vlc_opt) else kodi_opt
            if current_extinf is not None:
                current_options.append((tag.rstrip(":").lstrip("#"), line[len(tag) :]))
            # Huérfano (sin EXTINF previo): ignorado silenciosamente.
        # Resto de comentarios (#EXTGRP, #PLAYLIST, ...): ignorados.

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
                # Una fuente .ts suelta también puede ser una cámara: el mismo
                # camino de credenciales que las entradas con EXTINF.
                url=(
                    registrar_camara(playlist.name, single_url)
                    if single_url
                    else (source or playlist.name)
                ),
            )
        )
    return playlist


def parse_file(path: str | Path) -> Playlist:
    """Parsea un archivo M3U/M3U8/TS local a un Playlist.

    Los .ts son listas válidas: si traen M3U se parsean normal; si son
    un stream suelto o binario MPEG-TS se devuelven como lista de un
    solo canal apuntando al propio origen.
    Nunca lanza por contenido malformado; solo por errores de E/S.

    La lectura pasa por
    :func:`thetvview.security.local_files.read_limited_text`: fichero
    regular, sin symlinks y con límite de tamaño (gap B11). Los fallos
    salen como ``OSError`` con mensaje ya amable (incluye la ruta).
    """
    p = Path(path)
    return parse_text(read_limited_text(p), source=str(p), name=p.stem)


DEFAULT_TIMEOUT: float = 30.0

# Cache en disco para listas remotas (como el EPG): evita re-descargar
# un get.php de varios MB en cada apertura. TTL 6h por defecto.
DEFAULT_TTL_HOURS: float = 6.0

# Tope de descarga: `limits.max_file_bytes` (64 MB), el mismo que para un
# fichero local. Cubre listas de 200k entradas (`limits.max_entries`), que
# es justo lo que `parse_text` acepta; por encima de eso se rechaza antes
# de gastar memoria. El lag de la TUI con listas grandes se evita por otro
# lado (render solo de lo visible + favoritas en memoria, sin IO por canal).

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
    """Descomprime gzip (por magia o sufijo .gz) y decodifica a texto.

    La descompresión va acotada por ``limits.max_file_bytes``: una bomba
    gzip servida por el panel no puede expandirse en memoria sin tope.
    """
    looks_gz = url_hint.lower().split("?", 1)[0].endswith(".gz") or data[:2] == _GZIP_MAGIC
    if looks_gz:
        data = gunzip_limited(data, what="La playlist descargada")
    return data.decode("utf-8-sig", errors="replace")


def _playlist_name_for(url: str) -> str:
    name = Path(url.split("?", 1)[0]).stem or url
    return name


def fetch_bytes(url: str, timeout: float = DEFAULT_TIMEOUT,
                max_bytes: int | None = None, *,
                allow_private: bool = False) -> bytes:
    """Descarga los bytes crudos de una playlist (solo http/https).

    Pasa por :class:`thetvview.security.safe_http.SafeHttpClient`, así que
    aplica política de URL, anti-SSRF, redirects revalidados en cada salto,
    TLS estricto y lectura acotada (gaps B4/B5).

    - Siempre con timeout; el tope por defecto es
      ``limits.max_file_bytes`` (64 MB), el mismo que se aplica a un
      fichero local de playlist.
    - `max_bytes` permite afinar ese tope para una llamada concreta.
    - Lanza ValueError para URLs no soportadas u OSError amigable.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(
            f"URL no soportada (solo http/https): '{redact_text(url)}'"
        )

    limit = get_limits().max_file_bytes if max_bytes is None else int(max_bytes)
    client = SafeHttpClient(
        allow_private=allow_private,
        max_bytes=limit,
        purpose=PURPOSE_METADATA,
    )
    try:
        return client.get_bytes(url, timeout=timeout)
    except (OSError, ValueError):
        raise
    except IPTVError as exc:
        # RateLimitError/ConnectionLimitError no heredan de OSError y el
        # contrato de esta función es "OSError amigable"; los mensajes ya
        # vienen redactados por safe_http.
        raise OSError(str(exc)) from exc


def parse_url(url: str, timeout: float = DEFAULT_TIMEOUT, *,
              allow_private: bool = False) -> Playlist:
    """Descarga y parsea un M3U/M3U8/TS remoto (solo http/https).

    Las URLs .ts son listas válidas (p. ej. get.php?output=ts o
    /live/u/p/123.ts): si no traen M3U se devuelven como lista de un
    solo canal. Lanza ValueError para URLs no soportadas u OSError con mensaje
    amigable ante fallos de red/HTTP. Nunca cuelga: siempre hay timeout.

    ``allow_private=True`` habilita el acceso a red privada/loopback para
    fuentes que el usuario declaró como tales (SDD §11); por defecto está
    desactivado.

    Nota: para uso en la TUI se prefiere `load_url` (con cache TTL),
    que evita re-descargar listas grandes en cada apertura.
    """
    url = url.strip()
    raw = fetch_bytes(url, timeout=timeout, allow_private=allow_private)
    name = _playlist_name_for(url)
    return parse_text(_decompress_and_decode(raw, url), source=url, name=name)


def load_url(
    url: str,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    force_refresh: bool = False,
    cache_dir: Path | None = None,
    allow_private: bool = False,
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
    - `allow_private` es la excepción anti-SSRF declarada para esa fuente
      (SDD §11): por defecto la red privada está bloqueada.

    Errores de red/HTTP sin cache disponible se propagan como OSError
    con mensaje amigable; URLs no http(s) lanzan ValueError.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(
            f"URL no soportada (solo http/https): '{redact_text(url)}'"
        )
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
        raw = fetch_bytes(url, timeout=timeout, allow_private=allow_private)
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
