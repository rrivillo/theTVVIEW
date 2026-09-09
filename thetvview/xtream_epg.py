"""Adaptador short_epg → Program/Epg para integración con XMLTV existente.

Convierte las respuestas de get_short_epg del proveedor Xtream en
modelos Program del dominio, para que la TUI los muestre unificados.
"""

from __future__ import annotations

import base64
import html
import re
from datetime import datetime, timezone

from .models import Program


def _decode_epg_field(value: str) -> str:
    """Decodifica campos EPG que pueden estar base64 + HTML-encoded."""
    if not value:
        return ""
    decoded = value
    # Intentar base64
    try:
        raw = base64.b64decode(value).decode("utf-8-sig", errors="replace")
        if raw.strip():
            decoded = raw
    except Exception:
        pass
    # Decodificar HTML entities
    decoded = html.unescape(decoded)
    # Limpiar tags HTML básicos
    decoded = re.sub(r'<[^>]+>', '', decoded)
    return decoded.strip()


def _parse_epg_time(raw: str | None) -> datetime | None:
    """Parsea timestamp EPG (puede ser ISO o epoch)."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    # Intentar ISO format
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Intentar epoch (segundos o milisegundos)
    try:
        val = float(raw)
        if val > 1e12:
            val /= 1000
        return datetime.fromtimestamp(val, tz=timezone.utc)
    except (ValueError, OSError):
        return None


def short_epg_to_programs(epg_listings: list[dict]) -> list[Program]:
    """Convierte epg_listings de get_short_epg a lista de Program.

    Cada listing Xtream tiene campos como:
    - id, epg_id, title, lang, start, end, description, channel_id, etc.
    """
    programs: list[Program] = []
    for listing in epg_listings:
        if not isinstance(listing, dict):
            continue

        title = _decode_epg_field(
            listing.get("title", "") or listing.get("name", "") or "(sin título)"
        )
        desc = _decode_epg_field(listing.get("description", "") or listing.get("info", ""))
        sub_title = _decode_epg_field(listing.get("sub_title", "") or listing.get("episode", ""))

        start = _parse_epg_time(
            listing.get("start", "") or listing.get("start_time", "")
        )
        stop = _parse_epg_time(
            listing.get("end", "") or listing.get("end_time", "")
        )

        channel_id = str(
            listing.get("channel_id", "") or listing.get("epg_id", "") or ""
        )

        categories_raw = listing.get("categories", "") or listing.get("genre", "")
        if isinstance(categories_raw, str):
            categories = [c.strip() for c in categories_raw.split(",") if c.strip()]
        elif isinstance(categories_raw, list):
            categories = [str(c) for c in categories_raw if c]
        else:
            categories = []

        if start is None:
            continue

        programs.append(Program(
            channel_id=channel_id,
            title=title,
            start=start,
            stop=stop,
            desc=desc or None,
            sub_title=sub_title or None,
            categories=categories,
        ))

    programs.sort(key=lambda p: p.start)
    return programs


def merge_short_epg_into_programs(
    existing: list[Program],
    short_programs: list[Program],
) -> list[Program]:
    """Fusiona programas de short_epg con los existentes (XMLTV).

    Los de short_epg tienen prioridad si solapan.
    """
    if not short_programs:
        return existing
    if not existing:
        return short_programs

    # Combinar y deduplicar por start time
    seen_starts: set[datetime] = set()
    merged: list[Program] = []

    for p in short_programs:
        key = p.start
        if key not in seen_starts:
            seen_starts.add(key)
            merged.append(p)

    for p in existing:
        key = p.start
        if key not in seen_starts:
            seen_starts.add(key)
            merged.append(p)

    merged.sort(key=lambda p: p.start)
    return merged
