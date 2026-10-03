"""Parser DASH: MPD → :class:`MediaCapabilities` (SDD §10), sin red.

El XML se lee con :func:`thetvview.security.xml_safe.parse_xml`: sin DTD,
sin entidades, con tope de profundidad y de nodos y con tope de bytes. Un
MPD hostil no puede expandirse ni colgarnos la TUI (gap B6).

DASH no lleva espacios de nombres estables en la práctica, así que los
nombres de elemento se comparan **sin** el ``{uri}`` que añade expat. Se
interpretan:

- ``AdaptationSet`` y sus ``Representation`` (vídeo, audio, texto);
- ``ContentComponent`` + ``ContentRole`` (``main``, ``alternate``,
  ``commentary``, ``subtitle``, ``sign``, ``dub``);
- ``lang``/``Role`` heredados del ``AdaptationSet`` al ``Representation``;
- ``mimeType``, ``codecs``, ``bandwidth``, ``width``, ``height``,
  ``frameRate`` (número o fracción ``30000/1001``).

Como en HLS: **no se inventa nada**. Un ``AdaptationSet`` sin ``lang`` no
recibe idioma, y un stream con una sola representación no genera selector.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from ..security.xml_safe import parse_xml
from ..tracks.language import normalize_language
from ..tracks.models import (
    AUDIO,
    DEGRADED_NOT_EXPOSED,
    PROTO_DASH,
    TEXT,
    VIDEO,
    MediaCapabilities,
    MediaTrack,
    audio_id,
    subtitle_id,
    video_id,
)

__all__ = ["parse_dash", "DASH_MAX_BYTES", "DASH_MAX_DEPTH", "DASH_MAX_NODES"]

#: Topes propios del MPD: es más grande que un manifiesto HLS, pero sigue
#: siendo un fichero de texto. Si lo excede, es un MPD hostil o gigante.
DASH_MAX_BYTES: int = 8 * 1024 * 1024
DASH_MAX_DEPTH: int = 64
DASH_MAX_NODES: int = 200_000

#: ``mimeType``/``contentType`` que identifican el tipo de AdaptationSet.
_TEXT_TYPES: frozenset[str] = frozenset({"text", "application/ttml+xml", "application/mp4t+ttml"})
_AUDIO_TYPES: frozenset[str] = frozenset({"audio", "application/mp4a-latm"})
_VIDEO_TYPES: frozenset[str] = frozenset({"video"})

#: Roles DASH → los nuestros (SDD §10). Lo que no está aquí se conserva tal
#: cual en ``MediaTrack.role``.
_ROLE_MAP: dict[str, str] = {
    "main": "main",
    "alternate": "alternate",
    "commentary": "commentary",
    "subtitle": "subtitle",
    "sign": "sign",
    "dub": "dub",
    "description": "description",
}


def parse_dash(
    data: str | bytes,
    *,
    final_url: str = "",
    live: bool | None = None,
    max_bytes: int = DASH_MAX_BYTES,
    max_depth: int = DASH_MAX_DEPTH,
    max_nodes: int = DASH_MAX_NODES,
) -> MediaCapabilities:
    """Interpreta un MPD y devuelve las capacidades del stream.

    Args:
        data: MPD como texto o bytes.
        final_url: URL final tras redirecciones (sólo informativa: las URI
            de DASH son sueles absolutas y, si no, el reproductor las
            resuelve él; no se absolutizan aquí a propósito).
        live: si se sabe. Por defecto se deduce de ``type="dynamic"``.

    Raises:
        ParseError (de ``security.xml_safe``): DTD/entidades/profundidad/
            nodos/bytes.
        ValueError: el XML no es un MPD utilizable.
    """
    root = parse_xml(
        data,
        max_bytes=max_bytes,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    if _local(root.tag) != "MPD":
        raise ValueError("El documento no es un MPD (elemento raíz distinto de MPD).")

    if live is None:
        live = (root.get("type") or "static").strip().lower() == "dynamic"

    audios: list[MediaTrack] = []
    texts: list[MediaTrack] = []
    videos: list[MediaTrack] = []

    # La base URL se hereda MPD → Period → AdaptationSet → Representation,
    # que es como el DASH spec compone los BaseURL sucesivos.
    root_base, root_declared = _inherited_base(root, final_url)
    containers: list[tuple[ET.Element, str, bool]] = [(root, root_base, root_declared)]
    for period in root:
        if _local(period.tag) == "Period":
            base, declared = _inherited_base(period, root_base, root_declared)
            containers.append((period, base, declared))

    for container, base, declared in containers:
        for adaptation in container:
            if _local(adaptation.tag) != "AdaptationSet":
                continue
            _collect(adaptation, base, declared, audios, texts, videos)

    audio_uris = {t.id: t.uri for t in audios if t.uri}
    subtitle_uris = {t.id: t.uri for t in texts if t.uri}
    video_uris = {t.id: t.uri for t in videos if t.uri}

    if not audios:
        # El audio vive dentro del Representation de vídeo: existe, pero
        # no es una alternativa. Se declara una sola pista, sin idioma.
        audios.append(MediaTrack(id=audio_id(1), type=AUDIO, is_default=True))

    return MediaCapabilities(
        audio_tracks=audios,
        video_variants=videos,
        subtitle_tracks=texts,
        # Sin prueba de que el MPD sea multi-bitrate no prometemos ABR; con
        # dos o más representaciones de vídeo, el reproductor puede elegir.
        adaptive_bitrate=len(videos) >= 2,
        protocol=PROTO_DASH,
        manifest_url=final_url or None,
        final_url=final_url or None,
        live=bool(live),
        video_uri_by_id=video_uris,
        audio_uri_by_id=audio_uris,
        subtitle_uri_by_id=subtitle_uris,
        probed_at=time.time(),
        degraded_reason=DEGRADED_NOT_EXPOSED if not videos else "",
        note=_note(len(videos), len(audios), len(texts)),
    )


def _collect(
    adaptation: ET.Element,
    base: str,
    declared: bool,
    audios: list[MediaTrack],
    texts: list[MediaTrack],
    videos: list[MediaTrack],
) -> None:
    """Traduce un ``AdaptationSet`` y añade sus pistas a las listas."""
    lang, role = _adaptation_identity(adaptation)
    content_type = _content_type(adaptation)
    reps = [node for node in adaptation if _local(node.tag) == "Representation"]
    if not reps:
        return
    adapt_base, adapt_declared = _inherited_base(adaptation, base, declared)
    if content_type == "text":
        texts.extend(_text_tracks(reps, lang, role, adapt_base, adapt_declared, len(texts)))
    elif content_type == "audio":
        audios.extend(
            _audio_tracks(reps, lang, role, adapt_base, adapt_declared, len(audios))
        )
    elif content_type == "video":
        videos.extend(
            _video_tracks(reps, lang, role, adapt_base, adapt_declared, len(videos))
        )


def _inherited_base(
    node: ET.Element, parent_base: str, parent_declared: bool = False
) -> tuple[str, bool]:
    """Base URL efectiva de un nodo: ``(url, la_declará_algún_nivel)``.

    ``baseUrl`` (atributo) y ``BaseURL`` (elemento) componen por niveles
    MPD → Period → AdaptationSet → Representation, que es como los
    resuelve el reproductor. El booleano distingue "heredé una base
    declarada" de "aquí no dijo nadie nada": en el segundo caso **no** se
    inventa una URI a partir de la URL del MPD.
    """
    base = parent_base
    declared = parent_declared
    own = _clean(node.get("baseUrl"))
    if own:
        base = _absolutize(own, base)
        declared = True
    for child in node:
        if _local(child.tag) == "BaseURL":
            text = (child.text or "").strip()
            if text:
                base = _absolutize(text, base)
                declared = True
    return base, declared


def _video_tracks(
    reps: list[ET.Element],
    lang: str | None,
    role: str | None,
    base: str,
    declared: bool,
    offset: int,
) -> list[MediaTrack]:
    out: list[MediaTrack] = []
    for index, rep in enumerate(reps, start=offset + 1):
        bandwidth = _int(rep.get("bandwidth")) or _int(rep.get("averageBandwidth"))
        track = MediaTrack(
            id="",
            type=VIDEO,
            language=lang,
            original_language=_orig(lang),
            codec=_clean(rep.get("codecs")),
            bitrate=bandwidth,
            width=_int(rep.get("width")),
            height=_int(rep.get("height")),
            fps=_ratio(rep.get("frameRate")),
            role=role,
            uri=_uri(rep, base, declared),
            metadata={"mime_type": _clean(rep.get("mimeType"))},
        )
        track.id = video_id(track, fallback_index=index)
        out.append(track)
    return out


def _audio_tracks(
    reps: list[ET.Element],
    lang: str | None,
    role: str | None,
    base: str,
    declared: bool,
    offset: int,
) -> list[MediaTrack]:
    out: list[MediaTrack] = []
    for index, rep in enumerate(reps, start=offset + 1):
        track = MediaTrack(
            id=audio_id(index),
            type=AUDIO,
            language=lang,
            original_language=_orig(lang),
            codec=_clean(rep.get("codecs")),
            bitrate=_int(rep.get("bandwidth")) or _int(rep.get("averageBandwidth")),
            role=role,
            uri=_uri(rep, base, declared),
            metadata={"mime_type": _clean(rep.get("mimeType"))},
        )
        out.append(track)
    return out


def _text_tracks(
    reps: list[ET.Element],
    lang: str | None,
    role: str | None,
    base: str,
    declared: bool,
    offset: int,
) -> list[MediaTrack]:
    out: list[MediaTrack] = []
    for index, rep in enumerate(reps, start=offset + 1):
        track = MediaTrack(
            id=subtitle_id(index),
            type=TEXT,
            language=lang,
            original_language=_orig(lang),
            codec=_clean(rep.get("codecs")),
            role=role or "subtitle",
            uri=_uri(rep, base, declared),
            metadata={"mime_type": _clean(rep.get("mimeType"))},
        )
        out.append(track)
    return out


def _adaptation_identity(node: ET.Element) -> tuple[str | None, str | None]:
    """`(lang, role)` heredados del ``AdaptationSet``."""
    lang = _clean(node.get("lang"))
    role: str | None = None
    for child in node:
        if _local(child.tag) != "ContentComponent":
            continue
        ctype = (_clean(child.get("contentType")) or "").lower()
        for role_node in child:
            if _local(role_node.tag) != "ContentRole":
                continue
            value = (_clean(role_node.get("value")) or "").lower()
            if value and ctype in ("", "audio", "video", "text"):
                # El primer rol propio de la pista gana; "main" no aporta.
                if role is None or role == "main":
                    role = _ROLE_MAP.get(value, value)
    if not lang:
        for child in node:
            if _local(child.tag) == "Role" and child.get("value"):
                # El idioma puede venir en <Role schemeIdUri="…ISO-639-2">.
                if "iso-639" in (child.get("schemeIdUri") or "").lower():
                    lang = _clean(child.get("value"))
                    break
    normalized = normalize_language(lang) if lang else None
    return (normalized.code if normalized and normalized.code else lang), role


def _content_type(node: ET.Element) -> str:
    """``video``/``audio``/``text``/``"`` a partir de mimeType y contentType."""
    for child in node:
        if _local(child.tag) != "ContentComponent":
            continue
        declared = (_clean(child.get("contentType")) or "").lower()
        if declared in ("video", "audio", "text"):
            return declared
    mime = (_clean(node.get("mimeType")) or "").lower()
    if mime in _TEXT_TYPES:
        return "text"
    if mime.startswith("audio/") or mime in _AUDIO_TYPES:
        return "audio"
    if mime.startswith("video/") or mime in _VIDEO_TYPES:
        return "video"
    return ""


def _uri(node: ET.Element, base: str, declared: bool) -> str | None:
    """URI del ``Representation``: la suya, o la heredada si la hubo.

    Si ningún nivel declaró una base, se devuelve ``None``: la URL del MPD
    no es la URI del segmento y hacer esa equivalencia sería inventar.
    """
    own = _clean(node.get("baseUrl"))
    if own:
        return _absolutize(own, base)
    for child in node:
        if _local(child.tag) == "BaseURL":
            text = (child.text or "").strip()
            if text:
                return _absolutize(text, base)
    return base if declared and base else None


def _absolutize(uri: str, base: str) -> str:
    if uri.startswith(("http://", "https://")):
        return uri
    if not base:
        return uri
    from urllib.parse import urljoin

    return urljoin(base, uri)


def _note(videos: int, audios: int, subtitles: int) -> str | None:
    if videos <= 1 and audios <= 1 and subtitles == 0:
        return "Una sola pista: no hay nada que elegir."
    return None


def _local(tag: str) -> str:
    """Nombre del elemento sin el espacio de nombres ``{uri}``."""
    text = str(tag)
    if text.startswith("{"):
        return text.partition("}")[2]
    return text


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ratio(value: str | None) -> float | None:
    """``30`` o ``30000/1001`` → float."""
    text = _clean(value)
    if not text:
        return None
    try:
        if "/" in text:
            num, _, den = text.partition("/")
            denominator = float(den)
            if denominator == 0:
                return None
            return round(float(num) / denominator, 3)
        return round(float(text), 3)
    except (TypeError, ValueError):
        return None


def _orig(lang: str | None) -> str | None:
    """Valor original del idioma (se conserva aunque se normalice)."""
    return lang or None