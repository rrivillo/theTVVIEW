"""Agrupación de canales por group-title (lógica pura, testeable).

La TUI usa groups_of() para construir la vista de grupos: un canal sin
group-title se agrupa bajo None y se muestra al final ("(sin grupo)").
"""

from __future__ import annotations

from .models import Channel


def groups_of(channels: list[Channel]) -> dict[str | None, list[Channel]]:
    """Mapa {grupo: canales del grupo}.

    - Claves ordenadas alfabéticamente; None ("sin grupo") al final.
    - Los canales conservan el orden original dentro de cada grupo.
    """
    result: dict[str | None, list[Channel]] = {}
    for ch in channels:
        result.setdefault(ch.group if ch.group else None, []).append(ch)
    return dict(
        sorted(result.items(), key=lambda kv: (kv[0] is None, kv[0] or ""))
    )
