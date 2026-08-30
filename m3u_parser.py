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

import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

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
    return playlist


def parse_file(path: str | Path) -> Playlist:
    """Parsea un archivo M3U/M3U8 local a un Playlist.

    Nunca lanza por contenido malformado; solo por errores de E/S.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise OSError(f"No se pudo leer la playlist '{p}': {exc.strerror or exc}") from exc
    return parse_text(text, source=str(p), name=p.stem)


DEFAULT_TIMEOUT: float = 10.0


def parse_url(url: str, timeout: float = DEFAULT_TIMEOUT) -> Playlist:
    """Descarga y parsea un M3U/M3U8 remoto (solo http/https).

    Lanza ValueError para URLs no soportadas u OSError con mensaje
    amigable ante fallos de red/HTTP. Nunca cuelga: siempre hay timeout.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"URL no soportada (solo http/https): '{url}'")

    req = urllib.request.Request(url, headers={"User-Agent": "theTVVIEW/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
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

    name = Path(url.split("?", 1)[0]).stem or url
    return parse_text(raw.decode("utf-8-sig", errors="replace"), source=url, name=name)
