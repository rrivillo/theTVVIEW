from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .models import Channel
from .security.local_files import chmod_private
from .security.redaction import needs_redaction_for_storage, redact_text
from .stream_ref import is_opaque_ref
from .tracks.prefs import channel_key

#: Marca mínima de una fecha: "sin fecha" no es una fecha (FR-REC-03).
SIN_FECHA = "sin fecha"


def _parse_marca(at_utc: str | None) -> datetime | None:
    """Lee una marca ISO y la normaliza a UTC; ``None`` si no es válida.

    Acepta lo que `datetime.fromisoformat` acepta (incluida la `Z` de Python
    3.11+) y lo que no lleva zona, que se toma como UTC — las marcas las escribe
    este mismo módulo con `timezone.utc`. Un `recents.json` editado a mano, o de
    una versión anterior, no puede romper la pantalla: se trata como "sin fecha".
    """
    texto = str(at_utc or "").strip()
    if not texto:
        return None
    try:
        momento = datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc)


def relative_time(at_utc: str | None, now: datetime | None = None) -> str:
    """Fecha relativa legible: "ahora mismo", "hace 4 min", "ayer"...

    Función **pura**: recibe el instante de referencia, así que se prueba con
    un `now` fijo y sin terminal ni reloj del sistema. Nunca lanza y nunca
    miente: una marca futura, inválida o ausente sale como `sin fecha`, porque
    "hace -3 min" afirma algo que no se sabe (FR-REC-03).

    Los cortes son los del lenguaje corriente: segundos, minutos, horas, el
    "ayer" de ayer, días y —a partir de una semana— semanas.
    """
    momento = _parse_marca(at_utc)
    if momento is None:
        return SIN_FECHA
    try:
        referencia = datetime.now(timezone.utc) if now is None else _parse_marca(now.isoformat())
    except Exception:
        return SIN_FECHA
    if referencia is None:
        return SIN_FECHA
    segundos = (referencia - momento).total_seconds()
    if segundos < 0:
        # Reloj desincronizado o marca escrita a mano en el futuro.
        return SIN_FECHA
    if segundos < 60:
        return "ahora mismo"
    if segundos < 3600:
        return f"hace {int(segundos // 60)} min"
    if segundos < 86400:
        return f"hace {int(segundos // 3600)} h"
    dias = int(segundos // 86400)
    if dias == 1:
        return "ayer"
    if dias < 7:
        return f"hace {dias} días"
    semanas = dias // 7
    return "hace 1 semana" if semanas == 1 else f"hace {semanas} semanas"


@dataclass
class RecentItem:
    name: str
    url: str
    group: str | None
    player: str
    at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    #: Identificador estable del canal (`tvg-id`). Va **al final** y es
    #: opcional a propósito: los `recents.json` escritos antes de existir este
    #: campo siguen leyéndose y deduplicando por URL (NFR-07).
    tvg_id: str | None = None

    def identidad(self) -> str | None:
        """Clave estable del canal de este registro, o `None` si no hay.

        Delega en `tracks.prefs.channel_key`, la **misma** identidad que usan
        las preferencias de pistas: así "el mismo canal" significa lo mismo en
        los dos sitios, sin una segunda política de claves que mantener (FR-REC-04).
        """
        return channel_key(
            Channel(name=self.name, url=self.url, group=self.group, tvg_id=self.tvg_id)
        )


class RecentsManager:
    def __init__(self, path: Path | str | None = None, *, max_items: int = 20) -> None:
        self.path = Path(path) if path is not None else config.RECENTS_JSON
        self.max_items = max(1, max_items)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return []
        # Un `recents.json` que no sea una lista (un `{}`, un número) es un
        # archivo dañado, no un error: se ignora entero (FR-REC-08).
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict)]

    def _write(self, items: list[RecentItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        chmod_private(self.path)

    @staticmethod
    def _safe_url(url: str) -> tuple[str, bool]:
        """URL sin login embebido y flag de si hubo que tocarla (SDD §37)."""
        # Dos casos se dejan intactos: las referencias opacas
        # (xtream:// y xtream-ts://, que no llevan credenciales aunque un
        # nombre de fuente contenga '@'), y el `?token=` de un M3U, sin el
        # cual la lista no se puede reproducir. Todo lo demás que lleve
        # `password=` en la query se redacta antes de tocar disco.
        if is_opaque_ref(url) or not needs_redaction_for_storage(url):
            return url, False
        return redact_text(url), True

    def load(self) -> list[RecentItem]:
        out: list[RecentItem] = []
        migrated = False
        for raw in self._read():
            try:
                url = str(raw["url"])
            except KeyError:
                continue
            url, touched = self._safe_url(url)
            migrated = migrated or touched
            try:
                tvg_id = raw.get("tvg_id")
                out.append(
                    RecentItem(
                        name=str(raw["name"]),
                        url=url,
                        group=raw.get("group"),
                        player=str(raw.get("player") or ""),
                        at_utc=str(raw.get("at_utc") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
                        tvg_id=str(tvg_id) if tvg_id else None,
                    )
                )
            except KeyError:
                continue
        # Orden por última reproducción, del más reciente al más antiguo
        # (FR-REC-02). Se ordena **al leer** y no sólo al escribir: un
        # `recents.json` editado a mano llega en el orden que el usuario dejó.
        out.sort(key=lambda i: _parse_marca(i.at_utc) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        # Entradas antiguas con `/live/u/p/…` se limpian en disco al leerlas.
        if migrated:
            try:
                self._write(out)
            except OSError:
                pass
        return out

    def push(self, channel: Channel, player: str) -> None:
        """Deja `channel` como lo último reproducido.

        La identidad es la del canal, no la de su URL visible: `tvg-id` si lo
        tiene y, si no, el hash de la URL redactada (`tracks.prefs.channel_key`).
        Dos canales con el mismo nombre y distinta URL siguen siendo dos
        entradas; el mismo canal reproducido otra vez actualiza su fecha en vez
        de acumular duplicados (FR-REC-02/04).
        """
        url, _ = self._safe_url(channel.url)
        clave = channel_key(channel)
        if clave is None:
            # Sin identidad (canal sin URL): la URL es lo único que hay.
            items = [i for i in self.load() if i.url != url]
        else:
            items = [
                i for i in self.load()
                if i.identidad() != clave and i.url != url
            ]
        items.insert(0, RecentItem(
            name=channel.name,
            url=url,
            group=channel.group,
            player=player,
            tvg_id=str(getattr(channel, "tvg_id", "") or "") or None,
        ))
        self._write(items[: self.max_items])

    def clear(self) -> None:
        self._write([])
