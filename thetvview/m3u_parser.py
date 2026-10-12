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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import config
from .cam_ref import registrar_camara
from .epg_parser import SOURCE_ATTRS, resolve_source, split_sources
from .channel_identity import ReconcileReport, reconcile
from .models import Channel, Playlist
from .storage.json_store import (
    atomic_write_bytes,
    atomic_write_json,
    read_json_with_recovery,
)
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

#: Veredicto de la **última** validación de lista hecha por `load_url`.
#:
#: Es una variable de módulo a propósito y no un valor de retorno: cambiar la
#: firma de `load_url` obligaría a tocar a todos los que ya la llaman, y quien
#: carga una lista desde la UI necesita poder **mostrar** el motivo («se conservó
#: la anterior») sin que `load_url` tenga que lanzar ni devolver dos cosas.
#: Es el mismo patrón que `epg_parser` usa para su política de avisos.
last_validation: "PlaylistValidation | None" = None


#: Proporción mínima de entradas que debe traer una lista nueva respecto de la
#: anterior para dar por hecho que es una lista **válida** (CHAN-003).
#:
#: Es un 30 %, no un 50 %: hay proveedores que reorganizan el catálogo y quitan
#: de golpe todos los canales de una zona, así que un umbral alto rechazaría
#: actualizaciones legítimas. Y no es 0: una lista que se queda en el 2 % de la
#: que había no es una lista nueva, es una descarga cortada.
MIN_RATIO: float = 0.3

#: Sidecar con los encabezados de validación de la última descarga (CHAN-004).
META_SUFFIX: str = ".meta.json"


@dataclass
class PlaylistValidation:
    """Veredicto de una lista recién descargada, antes de sustituir a la buena.

    Es un **valor**: dice si se puede aplicar y por qué, y quien llama decide.
    Nada se aplica en silencio (FR-CHAN-007).
    """

    ok: bool = True
    #: Motivo, en palabras, de por qué no se puede aplicar. Vacío si ``ok``.
    motivo: str = ""
    #: Cuántos canales trae la lista nueva.
    canales: int = 0
    #: Cuántos traía la lista anterior (0 si no había ninguna).
    anteriores: int = 0
    #: ``canales / anteriores``; 1.0 si no había anterior.
    proporcion: float = 1.0
    #: True si la lista llegó vacía.
    vacia: bool = False
    #: Resumen de cambios, si se calculó junto con la validación.
    resumen_cambios: str = ""

    def para_modal(self) -> str:
        """Texto apto para un modal, con la explicación de siempre."""
        if not self.ok:
            return self.motivo
        base = f"La lista nueva trae {self.canales} canales"
        if self.anteriores:
            base += f" (antes había {self.anteriores})"
        if self.resumen_cambios:
            base += f". {self.resumen_cambios}"
        return base + "."


def validate_playlist(
    nueva: Playlist,
    previa: Playlist | None = None,
    *,
    min_ratio: float = MIN_RATIO,
) -> PlaylistValidation:
    """¿Merece la pena sustituir la lista activa por esta? (FR-CHAN-006/007)

    Dos rechazos, y sólo dos, porque son los que de verdad destruyen datos:

    - **vacía**: una descarga que devuelve cero entradas es un panel caído, una
      sesión caducada o un corte de conexión, no una lista sin canales;
    - **truncada**: menos del ``min_ratio`` de lo que había. Una lista que pasa
      de 20 000 a 400 canales es una lista a medio servir.

    Lo que **no** se rechaza: una lista más corta por una caída real de canales,
    con nombres duplicados o sin `tvg-id`. Rechazar eso sería inventar un
    criterio que el usuario no pidió.

    Devuelve el veredicto y **no** cambia nada por su cuenta. Si quien llama
    decide aplicar una lista inválida, la aplica —pero porque lo decidió.
    """
    canales = len(nueva.channels)
    anteriores = len(previa.channels) if previa is not None else 0
    proporcion = 1.0 if anteriores <= 0 else canales / anteriores
    if canales == 0:
        return PlaylistValidation(
            ok=False,
            motivo=(
                f"La lista llegó vacía (la anterior tenía {anteriores} "
                "canales). Suele ser que el proveedor está saturado o que la "
                "sesión caducó, así que se conserva la lista que ya tenías."
            ),
            canales=0,
            anteriores=anteriores,
            proporcion=proporcion,
            vacia=True,
        )
    if anteriores > 0 and proporcion < min_ratio:
        return PlaylistValidation(
            ok=False,
            motivo=(
                f"La lista nueva trae {canales} canales y la anterior tenía "
                f"{anteriores}: parece incompleta. Se conserva la lista que ya "
                "tenías; si de verdad quieres cambiarla, dilo."
            ),
            canales=canales,
            anteriores=anteriores,
            proporcion=proporcion,
        )
    return PlaylistValidation(
        ok=True, canales=canales, anteriores=anteriores, proporcion=proporcion
    )


def resumir_cambios(previa: Playlist | None, nueva: Playlist) -> ReconcileReport:
    """Qué cambió entre la lista activa y la recién descargada (FR-CHAN-008).

    No hace nada por su cuenta: devuelve el informe. Quien llama decide si lo
    muestra y con qué texto.
    """
    return reconcile(
    list(previa.channels) if previa is not None else [],
    list(nueva.channels),
)


def _meta_path_for(cache: Path) -> Path:
    """Ruta del sidecar de metadatos de `cache`."""
    return cache.with_name(cache.name + META_SUFFIX)


def _leer_meta(cache: Path) -> dict[str, str]:
    """Encabezados guardados de la última descarga. Nunca lanza."""
    try:
        datos = read_json_with_recovery(_meta_path_for(cache))
    except Exception:
        return {}
    if not isinstance(datos, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in datos.items()
        if isinstance(v, (str, int, float))
    }


def _escribir_meta(cache: Path, meta: dict[str, str]) -> None:
    """Guarda los encabezados. Un fallo aquí no invalida la descarga."""
    try:
        atomic_write_json(
            _meta_path_for(cache),
            {str(k): str(v) for k, v in meta.items() if v},
            schema_version=None,
        )
    except Exception:
        pass  # el sidecar es una optimización, no un requisito


def _borrar_meta(cache: Path) -> None:
    """Olvida los encabezados: si la URL cambia, un `ETag` viejo miente."""
    try:
        _meta_path_for(cache).unlink(missing_ok=True)
    except OSError:
        pass


def _headers_condicionales(meta: dict[str, str]) -> dict[str, str]:
    """`If-None-Match` / `If-Modified-Since` a partir del sidecar.

    Sólo se envían si el valor está **presente**: mandarlos vacíos es peor que
    no mandarlos, porque hay servidores que responden 304 a un
    `If-Modified-Since` vacío en vez de 200, y entonces la app se quedaría con
    la cache vieja para siempre.
    """
    headers: dict[str, str] = {}
    etag = str(meta.get("etag") or "").strip()
    modificado = str(meta.get("last_modified") or "").strip()
    if etag:
        headers["If-None-Match"] = etag
    if modificado:
        headers["If-Modified-Since"] = modificado
    return headers


def _meta_de_respuesta(url: str, headers: Mapping[str, str]) -> dict[str, str]:
    """Encabezados de validación de una respuesta 200 (FR-CHAN-009).

    **Nunca** se asume que esos encabezados garantizan que el contenido sea
    válido: sólo permiten no volver a descargar algo que no ha cambiado. La
    comprobación de integridad la sigue haciendo :func:`validate_playlist`, y
    por eso se aplican las dos cosas (CHAN-004).
    """
    etag = str(headers.get("etag") or "").strip()
    modificado = str(headers.get("last-modified") or "").strip()
    if not etag and not modificado:
        return {}
    return {"url": url, "etag": etag, "last_modified": modificado}


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


def fetch_bytes(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int | None = None,
    *,
    allow_private: bool = False,
    condicional: dict[str, str] | None = None,
) -> "FetchResult":
    """Descarga los bytes crudos de una playlist (solo http/https).

    Pasa por :class:`thetvview.security.safe_http.SafeHttpClient`, así que
    aplica política de URL, anti-SSRF, redirects revalidados en cada salto,
    TLS estricto y lectura acotada (gaps B4/B5).

    - Siempre con timeout; el tope por defecto es
      ``limits.max_file_bytes`` (64 MB), el mismo que se aplica a un
      fichero local de playlist.
    - `max_bytes` permite afinar ese tope para una llamada concreta.
    - `condicional` son las cabeceras de petición condicional ya calculadas
      (``If-None-Match`` / ``If-Modified-Since``). Las calcula quien sabe dónde
      está la cache —:func:`load_url`— y llegan vacías o ausentes cuando no hay
      sidecar, que es el caso por defecto: no se manda nada que no se sepa
      (FR-CHAN-009). Si el servidor responde 304, el resultado viene con
      ``not_modified=True`` y el cuerpo vacío.
    - Devuelve un :class:`FetchResult` con los encabezados de la respuesta, para
      poder guardarlos después.
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
    headers: dict[str, str] = dict(condicional or {})
    try:
        respuesta = client.get_conditional(
            url, headers=headers, max_bytes=limit, timeout=timeout
        )
    except (OSError, ValueError):
        raise
    except IPTVError as exc:
        # RateLimitError/ConnectionLimitError no heredan de OSError y el
        # contrato de esta función es "OSError amigable"; los mensajes ya
        # vienen redactados por safe_http.
        raise OSError(str(exc)) from exc
    if respuesta.status == 304:
        return FetchResult(b"", headers=dict(respuesta.headers), not_modified=True)
    return FetchResult(respuesta.body, headers=dict(respuesta.headers))


@dataclass
class FetchResult:
    """Cuerpo descargado y encabezados de la respuesta.

    Existe para que la descarga condicional (CHAN-004) pueda **guardar** el
    `ETag`/`Last-Modified` que recibió, sin que el llamante tenga que repetir
    la petición.
    """

    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    #: True cuando el servidor respondió 304 y el cuerpo está vacío.
    not_modified: bool = False


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
    raw = fetch_bytes(url, timeout=timeout, allow_private=allow_private).body
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
    confirmar=None,  # noqa: ANN001 - Callable[[PlaylistValidation], bool] | None
    condicional: bool = True,
    previa: Playlist | None = None,
) -> Playlist:
    """Descarga una playlist por URL usando cache local con TTL.

    - Si hay copia en cache con menos de `ttl_hours`, se usa sin red
      (apertura instantánea, ideal para get.php lentos).
    - Con `ttl_hours <= 0` o `force_refresh=True` siempre se re-descarga.
    - `condicional` (por defecto) manda ``If-None-Match``/``If-Modified-Since``
      si hay sidecar, y un 304 usa la cache sin volver a descargar (CHAN-004).
    - Si la descarga falla pero hay cache previa (aunque caducada), se devuelve
      la cache en vez de romper: un panel saturado no puede dejar al usuario sin
      lista.
    - `cache_dir` permite tests; por defecto es `config.PLAYLIST_CACHE_DIR`.
    - `allow_private` es la excepción anti-SSRF declarada para esa fuente
      (SDD §11); por defecto la red privada está bloqueada.

    **La lista nueva no sustituye a la buena sin pasar la validación**
    (CHAN-003): una descarga vacía o truncada deja la cache anterior intacta y
    devuelve la anterior, con el motivo disponible en :attr:`last_validation`.
    Si la validación falla pero `confirmar` está y devuelve True, se aplica
    igual —porque el usuario lo pidió explícitamente— y se dice en el informe.

    Errores de red/HTTP sin cache disponible se propagan como OSError con
    mensaje amigable; URLs no http(s) lanzan ValueError.
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
    global last_validation  # noqa: PLW0603 - estado de diagnóstico del store
    last_validation = None

    def desde_cache() -> Playlist | None:
        try:
            text = _decompress_and_decode(path.read_bytes(), url)
            return parse_text(text, source=url, name=name)
        except (OSError, ValueError):
            return None

    fresh = ttl_hours > 0 and not force_refresh and _is_fresh(path, ttl_hours * 3600)
    if fresh:
        lista = desde_cache()
        if lista is not None:
            return lista

    try:
        resultado = fetch_bytes(
            url,
            timeout=timeout,
            allow_private=allow_private,
            # `force_refresh` significa «quiero la lista de ahora», así que
            # **no** se manda nada condicional: un 304 dejaría al usuario con la
            # copia anterior, que es justo lo que pidió para no ver.
            condicional=(
                _headers_condicionales(_leer_meta(path))
                if condicional and not force_refresh
                else None
            ),
        )
    except (OSError, ValueError):
        # Fallback stale: el panel a veces cae o tarda; mejor abrir la última
        # copia conocida que no abrir nada.
        lista = desde_cache()
        if lista is not None:
            return lista
        raise

    if resultado.not_modified:
        # 304: el servidor dice que no cambió. La validación de integridad **no**
        # se salta —ya se hizo al guardarla—, pero si no hay cache usable no hay
        # nada que hacer y hay que decirlo en vez de devolver una lista vacía.
        lista = desde_cache()
        if lista is None:
            raise OSError(
                "El servidor dice que la lista no cambió, pero no hay copia "
                "guardada para usar. Vuelve a intentarlo en un momento."
            )
        return lista

    raw = resultado.body
    try:
        nueva = parse_text(_decompress_and_decode(raw, url), source=url, name=name)
    except (OSError, ValueError):
        # No se puede ni leer lo descargado: la cache anterior sigue siendo la
        # mejor respuesta disponible.
        lista = desde_cache()
        if lista is not None:
            return lista
        raise

    # La lista que el llamante tiene delante manda sobre la copia de disco: es
    # la que el usuario está viendo y la que hay que proteger.
    referencia = previa if previa is not None else desde_cache()
    validacion = validate_playlist(nueva, referencia)
    cambios = resumir_cambios(referencia if not fresh else None, nueva)
    validacion.resumen_cambios = cambios.resumen()
    last_validation = validacion

    if not validacion.ok:
        aplicar = False
        if confirmar is not None:
            try:
                aplicar = bool(confirmar(validacion))
            except Exception:
                aplicar = False
        if aplicar:
            _guardar_cache(path, raw)
            _escribir_meta(path, _meta_de_respuesta(url, resultado.headers))
            return nueva
        # Se conserva la anterior. Si no había, la nueva es lo único que hay:
        # se usa, pero **sin** escribirla en cache, para no dejar fija una lista rota.
        if referencia is not None:
            return referencia
        _borrar_meta(path)
        return nueva

    _guardar_cache(path, raw)
    _escribir_meta(path, _meta_de_respuesta(url, resultado.headers))
    return nueva


def _guardar_cache(path: Path, raw: bytes) -> bool:
    """Escribe la cache de forma atómica. True si se guardó.

    El ``.bak`` va antes del ``os.replace``: el archivo principal queda siempre
    completo (ver :func:`atomic_write_bytes`), y además queda una copia de la
    versión anterior por si la nueva resultara ser inválida al parsearla más
    tarde (FR-CHAN-010).
    """
    from .storage.json_store import make_backup

    try:
        make_backup(path)
    except Exception:
        pass
    try:
        atomic_write_bytes(path, raw)
        return True
    except Exception:
        return False
