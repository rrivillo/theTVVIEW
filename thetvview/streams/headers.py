"""Cabeceras HTTP derivadas de los ``EXTVLCOPT`` de un canal.

Se extrae aquí porque **dos** módulos necesitan exactamente las mismas
cabeceras y no pueden construirlas cada uno a su manera (plan F2, H6):

- la sonda de salud (:mod:`thetvview.channel_health`), que mide latencia;
- la sonda de pistas (:mod:`thetvview.streams.probe`), que baja el
  manifiesto.

No es cosmético: muchos proveedores devuelven **403 si falta el
``Referer``**, también al pedir el manifiesto, no sólo al reproducir. Sin
esto, el sondeo de pistas fallaría justo en los proveedores que más lo
necesitan y la UI mostraría "no hay pistas" cuando sí las hay (SDD §29).

Sólo se traducen dos claves de la lista blanca, igual que en
:mod:`thetvview.player`: ``http-user-agent`` y ``http-referrer``. Cualquier
otra opción del proveedor se ignora en silencio aquí (el reproductor ya
avisa por su cuenta).
"""

from __future__ import annotations

from ..models import Channel

__all__ = ["build_headers", "KEY_ALIASES", "DEFAULT_USER_AGENT", "strip_ffmpeg"]

DEFAULT_USER_AGENT: str = "theTVVIEW/1.0"

#: Alias frecuentes en EXTVLCOPT -> clave canónica. Es la misma tabla que
#: usa ``player.py``; aquí sólo importa el subconjunto de cabeceras HTTP.
KEY_ALIASES: dict[str, str] = {
    "http-user-agent": "http-user-agent",
    "user-agent": "http-user-agent",
    "user_agent": "http-user-agent",
    "http-referrer": "http-referrer",
    "referrer": "http-referrer",
    "referer": "http-referrer",
    "http-referer": "http-referrer",
}


def build_headers(channel: Channel | None) -> dict[str, str]:
    """Cabeceras HTTP para pedir el stream o el manifiesto de `channel`.

    Siempre devuelve como mínimo ``User-Agent``: sin él, algunos
    proveedores responden 403 a cualquier petición.
    """
    headers: dict[str, str] = {"User-Agent": DEFAULT_USER_AGENT}
    if channel is None:
        return headers
    for tag, raw in getattr(channel, "extra_options", None) or []:
        if tag != "EXTVLCOPT":
            continue
        key, sep, value = str(raw).partition("=")
        if not sep:
            continue
        canonical = KEY_ALIASES.get(key.strip().lower())
        value = value.strip()
        if not value:
            continue
        if canonical == "http-user-agent":
            headers["User-Agent"] = value
        elif canonical == "http-referrer":
            headers["Referer"] = value
    return headers


def strip_ffmpeg(url: str) -> str:
    """Quita el envoltorio ``ffmpeg://`` que añade ``player.py`` a mplayer."""
    text = (url or "").strip()
    if text.startswith("ffmpeg://"):
        return text[len("ffmpeg://"):]
    return text