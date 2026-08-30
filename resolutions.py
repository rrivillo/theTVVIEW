"""Detección y agrupación de variantes de resolución de canales.

El M3U no tiene un campo estándar de resolución; soportamos las
convenciones reales observadas en listas IPTV (validado contra
iptv-org/index.m3u):

1. Atributos EXTINF explícitos: ``res="1080"`` o ``quality="FHD"``.
2. Token de calidad al final del nombre, con o sin paréntesis:
   "Canal X (720p)", "Canal X HD", "Canal X 1080i".
3. Marcadores entre corchetes al final ("[Geo-blocked]", "[Not 24/7]")
   que se ignoran al buscar el token de calidad y al agrupar.
4. Sufijo del tvg-id estilo iptv-org: "id.es@HD", "id.us@SD".

Un canal tiene "opciones de resolución" cuando existen varias entradas
con el mismo nombre base y distinta resolución; solo entonces la TUI
muestra el selector.
"""

from __future__ import annotations

import re

from .models import Channel

# Atributos EXTINF (normalizados a guion bajo) que pueden traer calidad.
_RES_ATTRS = ("res", "quality")

# Etiquetas simbólicas reconocidas -> altura canónica en líneas.
_LABEL_HEIGHT: dict[str, int] = {
    "SD": 480,
    "HD": 720,
    "FHD": 1080,
    "QHD": 1440,
    "UHD": 2160,
    "4K": 2160,
    "8K": 4320,
}

# Token de calidad: numérico (480p, 720i, 1080p...) o simbólico.
# Admite paréntesis opcionales: "(720p)" o "720p".
_RES_TOKEN_RE = re.compile(
    r"\(?(\d{3,4}[PI]|SD|FHD|UHD|QHD|HD|4K|8K)\)?", re.IGNORECASE
)

# Palabra simbólica suelta al final del nombre (sin paréntesis).
_SYMBOL_WORD_RE = re.compile(
    r"(SD|FHD|UHD|QHD|HD|4K|8K)", re.IGNORECASE
)

# Marcadores entre corchetes pegados al final: "[Geo-blocked]", "[Not 24/7]".
_TAG_END_RE = re.compile(r"\s*\[[^\]]*\]\s*$")

# Paréntesis al final: "(MPV)", "(Nacional)", "(Opc.2)" etc.
_PAREN_END_RE = re.compile(r"\s*\([^)]*\)\s*$")

# Sufijo @CALIDAD del tvg-id (convención iptv-org).
_TVGID_RE = re.compile(r"@(\d{3,4}[PI]|SD|FHD|UHD|QHD|HD|4K|8K)$", re.IGNORECASE)


def _norm_label(raw: str) -> str:
    """Normaliza una etiqueta: '4k'->'4K', 'fhd'->'FHD', '720P'->'720p'."""
    raw = raw.strip()
    upper = raw.upper()
    if upper in _LABEL_HEIGHT:
        return upper
    m = re.fullmatch(r"(\d{3,4})([PI]?)", upper)
    if m:
        return f"{m.group(1)}{m.group(2).lower() or 'p'}"
    return raw


def quality_rank(label: str) -> float:
    """Ordena variantes de peor a mejor; desconocidas van al final."""
    upper = label.upper()
    if upper in _LABEL_HEIGHT:
        return float(_LABEL_HEIGHT[upper])
    m = re.fullmatch(r"(\d{3,4})([PI])", upper)
    if m:
        # El entrelazado ('i') se penaliza ligeramente frente al progresivo.
        return int(m.group(1)) - (0.5 if m.group(2) == "I" else 0.0)
    return 0.0


def split_name(name: str) -> tuple[str, str | None]:
    """Separa la calidad final del nombre: -> (nombre_base, etiqueta|None).

    Ignora marcadores '[...]' y '(MPV)'/parentéticos no-calidad pegados
    al final antes de mirar el último token. El nombre base queda limpio
    de tokens de calidad repetidos ("Canal HD (720p)" -> "Canal") y de
    marcadores finales.
    """
    text = name.strip()
    probe = text
    while True:
        m = _TAG_END_RE.search(probe)
        if m:
            probe = probe[: m.start()].rstrip()
            continue
        m = _PAREN_END_RE.search(probe)
        if m:
            inner = m.group(0).strip()[1:-1].strip()
            # Si el contenido del paréntesis es un token de calidad
            # ("720p", "HD"...), no lo consideramos marcador: deja de pelar.
            if inner and _RES_TOKEN_RE.fullmatch(inner):
                break
            if not inner:
                probe = probe[: m.start()].rstrip()
                continue
            probe = probe[: m.start()].rstrip()
            continue
        break

    parts = probe.rsplit(None, 1)
    token = parts[1] if len(parts) == 2 else ""
    m = _RES_TOKEN_RE.fullmatch(token) if token else None
    if m is None:
        return text, None

    label = _norm_label(m.group(1))
    return _clean_base(probe[: probe.rfind(token)].rstrip()), label


def _clean_base(text: str) -> str:
    """Quita del nombre base colas de calidad/marcadores ya redundantes."""
    prev = None
    while prev != text and text:
        prev = text
        m = _TAG_END_RE.search(text)
        if m:
            text = text[: m.start()].rstrip()
            continue
        m = _PAREN_END_RE.search(text)
        if m:
            # En el base limpiamos cualquier paréntesis final (sea MPV,
            # Nacional u otro) y también paréntesis de calidad redundantes.
            text = text[: m.start()].rstrip()
            continue
        parts = text.rsplit(None, 1)
        if len(parts) == 2 and _SYMBOL_WORD_RE.fullmatch(parts[1]):
            text = parts[0].rstrip()
    return text


def detect(channel: Channel) -> str | None:
    """Resolución declarada: atributo EXTINF > sufijo del nombre > tvg-id."""
    for attr in _RES_ATTRS:
        value = channel.attrs.get(attr, "").strip()
        if value:
            return _norm_label(value)
    _, label = split_name(channel.name)
    if label is not None:
        return label
    if channel.tvg_id:
        m = _TVGID_RE.search(channel.tvg_id.strip())
        if m:
            return _norm_label(m.group(1))
    return None


def base_name(channel: Channel) -> str:
    """Nombre sin calidad final ni marcadores (para agrupar variantes)."""
    base, _ = split_name(channel.name)
    return base


def name_has_resolution(channel: Channel) -> bool:
    """True si el nombre del canal declara una resolución (sufijo visible)."""
    return split_name(channel.name)[1] is not None


def swappable_variants(channels: list[Channel], target: Channel) -> list[Channel]:
    """Variantes de resolución disponibles, sin exigir que target tenga etiqueta.

    Devuelve todas las entradas con mismo base_name y resolución detectada
    (incluido target si tiene etiqueta), ordenadas de peor a mejor.
    Vacío si hay menos de 2 variantes con resolución o si todas tienen
    la misma resolución (no hay nada que cambiar).
    """
    base = base_name(target)
    group = [c for c in channels if base_name(c) == base and detect(c) is not None]
    if len(group) < 2:
        return []
    distinct = {detect(c) for c in group}
    if len(distinct) < 2:
        return []
    return sorted(group, key=lambda c: quality_rank(detect(c) or ""))


def variants_of(channels: list[Channel], target: Channel) -> list[Channel]:
    """Variantes de resolución del canal objetivo (incluido él), de peor a mejor.

    Vacío si el canal no tiene resolución detectada o es único con ella.
    """
    label = detect(target)
    if label is None:
        return []
    base = base_name(target)
    group = [c for c in channels if base_name(c) == base and detect(c) is not None]
    if len(group) < 2:
        return []
    return sorted(group, key=lambda c: quality_rank(detect(c) or ""))
