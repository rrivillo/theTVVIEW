"""Encuesta de una lista: ¿cuántos canales Offering pistas de verdad?

Responde a una pregunta que los tests sintéticos no pueden responder:
**sobre esta lista concreta, ¿la aplicación puede ofrecer audio, subtítulos o
calidad, y para cuántos canales?**

No es una simulación: usa exactamente el camino real de la app

    m3u_parser → streams.probe → streams.hls/dash → tracks.manager

y por tanto hereda susTimeout, sus topes, su política anti-SSRF y su
redacción. Lo que mide es lo que el usuario verá.

Uso::

    python -m thetvview.tracks.survey lista.m3u -n 40
    python -m thetvview.tracks.survey https://…/get.php -n 60 --private
    python -m thetvview.tracks.survey lista.m3u --muestreo aleatorio

Notas de diseño:

- **La red sale sólo por** :mod:`thetvview.security.safe_http`, como en
  toda la app; `--private` es una excepción explícita por lista, igual que
  en la TUI, y nunca se activa sola (SDD §11).
- **No se escribe nada en disco**: ni caché de pistas ni preferencias
  (``cache_dir=None``, ``use_cache=False``), porque una encuesta no debe
  contaminar el estado de la app.
- **Nunca se imprime una URL con credenciales**: los ejemplos salen por
  :func:`thetvview.security.redaction.redact_url`.
- **No cuelga**: cada sondeo tiene su timeout y el conjunto además.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..m3u_parser import parse_file, parse_url
from ..models import Channel, Playlist
from ..security.redaction import redact_url
from ..streams.probe import ProbeResult, probe_capabilities
from ..tracks.manager import TrackManager
from ..tracks.models import (
    DEGRADED_NOT_EXPOSED,
    DEGRADED_PROTOCOL,
    DEGRADED_UNKNOWN,
    PROTO_DASH,
    PROTO_HLS,
)

__all__ = [
    "ChannelVerdict",
    "SurveyReport",
    "survey_playlist",
    "survey_channel",
    "load_playlist",
    "main",
    "diversificar_por_host",
    "host_de",
    "prioriza_canales",
    "VERDICT_LABELS",
]

# Códigos de veredicto. Cada uno es una **razón distinta** de que el menú no
# aparezca, que es justo lo que hace falta para diagnosticar sin adivinar.
OK_MENU: str = "menu"
OK_SINGLE: str = "una_pista"
OK_NO_MENU: str = "sin_alternativas"
NOT_HTTP: str = "no_http"
TS: str = "mpegts"
UNKNOWN_BODY: str = "cuerpo_no_reconocible"
HTTP_FORBIDDEN: str = "http_403"
HTTP_MISSING: str = "http_404"
HTTP_OTHER: str = "http_otro"
NETWORK: str = "red_o_timeout"
PARSE_ERROR: str = "manifiesto_ilegible"
SKIPPED: str = "sin_url"

VERDICT_LABELS: dict[str, str] = {
    OK_MENU: "SÍ hay menú de pistas",
    OK_SINGLE: "Una sola pista (nada que elegir)",
    OK_NO_MENU: "Manifiesto legible pero sin alternativas",
    NOT_HTTP: "La URL no es HTTP/HTTPS (rtmp, udp…)",
    TS: "MPEG-TS (pista única por definición)",
    UNKNOWN_BODY: "El servidor no devolvió un manifiesto",
    HTTP_FORBIDDEN: "HTTP 403 (suele faltar Referer/UA)",
    HTTP_MISSING: "HTTP 404 (el canal no existe)",
    HTTP_OTHER: "Otro error HTTP del proveedor",
    NETWORK: "Error de red o tiempo agotado",
    PARSE_ERROR: "El manifiesto se leyó pero es ilegible",
    SKIPPED: "El canal no tiene URL",
}

#: Veredictos con los que **el canal se reproduce igual**, sólo que no hay
#: nada que elegir. Son la respuesta honesta a "no me salen opciones": la
#: lista es de una sola pista, no un fallo de la app.
SIN_MENU_VERDICTS: frozenset[str] = frozenset(
    {OK_SINGLE, OK_NO_MENU, TS, NOT_HTTP}
)

#: Veredictos en los que **no se pudo averiguarlo**: el proveedor respondió
#: mal, no"$ hay nada que elegir. Son los que hay que mirar primero.
FALLO_VERDICTS: frozenset[str] = frozenset(
    {UNKNOWN_BODY, HTTP_FORBIDDEN, HTTP_MISSING, HTTP_OTHER, NETWORK,
     PARSE_ERROR, SKIPPED}
)


@dataclass
class ChannelVerdict:
    """Qué se descubrió en un canal concreto."""

    name: str
    code: str
    detail: str = ""
    protocol: str = ""
    audio_tracks: int = 0
    subtitle_tracks: int = 0
    variants: int = 0
    #: Las tres booleans del menú: qué secciones aparecerían.
    menu_audio: bool = False
    menu_subtitles: bool = False
    menu_quality: bool = False
    url_redacted: str = ""

    @property
    def has_menu(self) -> bool:
        return self.menu_audio or self.menu_subtitles or self.menu_quality

    def describe(self) -> str:
        """Una línea legible, con la URL ya redactada."""
        piezas = [f"{self.code}: {VERDICT_LABELS.get(self.code, self.code)}"]
        if self.protocol:
            piezas.append(f"protocolo={self.protocol}")
        if self.code == OK_MENU:
            piezas.append(
                f"audio={self.audio_tracks} subt={self.subtitle_tracks} "
                f"variantes={self.variants}"
            )
            secciones = [
                nombre
                for nombre, activo in (
                    ("audio", self.menu_audio),
                    ("subtítulos", self.menu_subtitles),
                    ("calidad", self.menu_quality),
                )
                if activo
            ]
            piezas.append("menú: " + ", ".join(secciones))
        if self.detail:
            piezas.append(self.detail)
        if self.url_redacted:
            piezas.append(f"url={self.url_redacted}")
        return " | ".join(piezas)


@dataclass
class SurveyReport:
    """Resultado agregado de la encuesta."""

    source: str = ""
    total_canales: int = 0
    #: Cuántos canales se llegaron a preguntar de verdad. Se rellena al
    #: terminar, no al empezar: si se paró por objetivo son menos que los
    #: planeados, y decir el número mayor mentiría en cada porcentaje.
    sondeados: int = 0
    #: Cuántos se pretendían preguntar (con `--conseguir`, puede que no se
    #: llegara: son los canales que quedaron sin mirar).
    planeados: int = 0
    veredictos: list[ChannelVerdict] = field(default_factory=list)
    segundos: float = 0.0
    #: Cuántos canales con menú se querían (0 = medir la lista entera).
    objetivo: int = 0
    #: True si se paró antes de mirar la lista entera porque ya se había
    #: alcanzado el objetivo. Entonces el informe **no** es una medición
    #: completa de la lista y no debe leerse como tal.
    paro_antes: bool = False

    @property
    def con_menu(self) -> int:
        return sum(1 for v in self.veredictos if v.code == OK_MENU)

    @property
    def sin_menu(self) -> int:
        """Canales que se reproducen igual pero no ofrecen nada que elegir."""
        return sum(1 for v in self.veredictos if v.code in SIN_MENU_VERDICTS)

    @property
    def fallos(self) -> int:
        """Canales en los que no se pudo saber nada (o no hay nada que ver)."""
        return sum(1 for v in self.veredictos if v.code in FALLO_VERDICTS)

    @property
    def proporcion_con_menu(self) -> float:
        return (self.con_menu * 100.0 / self.sondeados) if self.sondeados else 0.0

    def reparto(self) -> Counter:
        return Counter(v.code for v in self.veredictos)

    def resumen(self) -> str:
        """Resumen en texto, pensado para pegar en un informe."""
        lineas: list[str] = []
        total = max(1, self.sondeados)
        lineas.append(f"Lista: {self.source}")
        lineas.append(
            f"Canales en la lista: {self.total_canales} · sondeados: {self.sondeados} "
            f"· tiempo: {self.segundos:.1f} s"
        )
        if self.paro_antes:
            lineas.append(
                f"(parado: ya había {self.objetivo} canales con menú; esto **no** "
                "es el porcentaje de la lista entera)"
            )
        elif self.planeados > self.sondeados:
            lineas.append(
                f"(sin parar por objetivo: {self.planeados - self.sondeados} "
                "canales se quedaron sin mirar)"
            )
        lineas.append("")
        lineas.append(
            f"SÍ hay menú de pistas: {self.con_menu} "
            f"({self.proporcion_con_menu:.1f} % de lo sondeado)"
        )
        lineas.append(
            f"Sin nada que elegir (se reproducen igual): {self.sin_menu}"
        )
        lineas.append(f"No se pudo averiguar (fallo del proveedor): {self.fallos}")
        lineas.append("")
        lineas.append("Reparto por veredicto:")
        for code, n in self.reparto().most_common():
            lineas.append(f"  {n:5}  {code} — {VERDICT_LABELS.get(code, code)}")
        return "\n".join(lineas)

    def ejemplos(self, limite: int = 5) -> str:
        """Los primeros ejemplos de cada veredicto, con la URL redactada."""
        vistos: Counter = Counter()
        lineas: list[str] = []
        for veredicto in self.veredictos:
            if vistos[veredicto.code] >= limite:
                continue
            vistos[veredicto.code] += 1
            lineas.append(f"  - {veredicto.describe()}")
        return "\n".join(lineas) if lineas else "  (sin ejemplos)"


# ---------------------------------------------------------------------------
# Un canal
# ---------------------------------------------------------------------------


def survey_channel(
    channel: Channel,
    *,
    allow_private: bool = False,
    timeout: float = 8.0,
) -> ChannelVerdict:
    """Analiza un canal con el camino real y devuelve su veredicto."""
    redacted = redact_url(str(getattr(channel, "url", "") or ""))
    nombre = channel.name or "(sin nombre)"
    if not redacted:
        return ChannelVerdict(nombre, SKIPPED, url_redacted="")

    try:
        result = probe_capabilities(
            channel,
            allow_private=allow_private,
            timeout=timeout,
            use_cache=False,
            cache_dir=None,
        )
    except Exception as exc:  # noqa: BLE001 - la encuesta no debe parar nunca
        return ChannelVerdict(
            nombre, NETWORK, detail=_short(str(exc)), url_redacted=redacted
        )

    return _verdict_from_result(nombre, result, redacted)


def _verdict_from_result(
    nombre: str, result: ProbeResult, redacted: str
) -> ChannelVerdict:
    caps = result.capabilities
    if caps is None:
        return ChannelVerdict(
            nombre,
            _code_for_failure(result),
            detail=_short(result.reason),
            url_redacted=redacted,
        )

    protocol = caps.protocol
    comun = dict(
        name=nombre,
        protocol=protocol,
        audio_tracks=len(caps.audio_tracks),
        subtitle_tracks=len(caps.subtitle_tracks),
        variants=len(caps.video_variants),
        url_redacted=redacted,
    )

    if caps.degraded_reason == DEGRADED_PROTOCOL:
        return ChannelVerdict(code=TS, detail=_short(caps.note or ""), **comun)
    if caps.degraded_reason == DEGRADED_UNKNOWN:
        return ChannelVerdict(
            code=UNKNOWN_BODY, detail=_short(caps.note or ""), **comun
        )
    if caps.degraded_reason == DEGRADED_NOT_EXPOSED:
        return ChannelVerdict(
            code=OK_SINGLE,
            detail=_short(caps.note or "el manifiesto declara una sola pista"),
            **comun,
        )

    # Manifiesto legible: se pregunta al gestor, que es quien decide qué
    # se mostraría en pantalla.
    manager = TrackManager(caps, None)
    opciones = manager.options()
    veredicto = ChannelVerdict(code=OK_NO_MENU, **comun)
    veredicto.menu_audio = opciones.audio_selectable
    veredicto.menu_subtitles = opciones.subtitles_selectable
    # La calidad se ofrece por el proxy de pinning, que sólo existe en HLS.
    veredicto.menu_quality = (
        opciones.quality_selectable and protocol == PROTO_HLS
    )
    veredicto.code = OK_MENU if veredicto.has_menu else OK_NO_MENU
    if veredicto.code == OK_NO_MENU:
        veredicto.detail = (
            f"manifiesto {protocol} con {veredicto.audio_tracks} audios, "
            f"{veredicto.subtitle_tracks} subtítulos y {veredicto.variants} "
            "variantes: no hay ningún tipo con dos o más"
        )
    return veredicto


def _code_for_failure(result: ProbeResult) -> str:
    status = result.http_status
    motivo = (result.reason or "").lower()
    if status == 403:
        return HTTP_FORBIDDEN
    if status == 404:
        return HTTP_MISSING
    if status:
        return HTTP_OTHER
    if "no es http" in motivo:
        return NOT_HTTP
    if "ilegible" in motivo or "vacío" in motivo or "no es una lista" in motivo:
        return PARSE_ERROR
    return NETWORK


def _short(text: str, limit: int = 90) -> str:
    limpio = " ".join(str(text or "").split())
    if len(limpio) <= limit:
        return limpio
    return limpio[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Una lista
# ---------------------------------------------------------------------------


def load_playlist(source: str, *, allow_private: bool = False) -> Playlist:
    """Carga la lista por el camino de la app: fichero local o URL."""
    texto = str(source or "").strip()
    if not texto:
        raise ValueError("Falta la lista.")
    if texto.lower().startswith(("http://", "https://")):
        return parse_url(texto, allow_private=allow_private)
    return parse_file(texto)


def survey_playlist(
    source: str | Playlist,
    *,
    limite: int = 40,
    workers: int = 4,
    timeout: float = 8.0,
    allow_private: bool = False,
    muestreo: str = "inicio",
    progreso: Callable[[int, int], None] | None = None,
    objetivo: int = 0,
    diversificar: bool = True,
    priorizar: bool = True,
) -> SurveyReport:
    """Encuesta hasta `limite` canales de una lista.

    Args:
        source: ruta o URL de la M3U, o una :class:`Playlist` ya cargada.
        limite: cuántos canales analizar como máximo (0 = todos).
        workers: cuántos sondeos a la vez. Poco a propósito: son servidores
            de terceros y la app debe ser buena-citada.
        timeout: presupuesto por canal.
        allow_private: excepción anti-SSRF **de esta lista**, explícita.
        muestreo: "inicio" (los primeros), "aleatorio" o "fin".
        progreso: callback ``(hechos, total)`` para verlo avanzar.
        objetivo: si es mayor que 0, **se para en cuanto se han encontrado
            esa cantidad de canales con menú** y no sigue preguntando al
            resto. Es lo que hace falta para "dame cinco queSirvan" sin
            recorrientar 1810 canales: el gasto de red se ajusta a la
            pregunta, no al tamaño de la lista. Los lotes ya lanzados se
            terminan (no se cancela red a medias) y el informe dice si se
            paró antes de tiempo.
        diversificar: intercalar los canales por host, de modo que los
            primeros sondeos visiten proveedores distintos.
        priorizar: ordenar por las pistas de URL que sí han dado resultado
            en listas reales (ver :func:`prioriza_canales`).

    Con `objetivo=0` el comportamiento es el de siempre: se analiza todo lo
    seleccionado y el informe es una medición completa de la lista.
    """
    playlist = load_playlist(source, allow_private=allow_private) if isinstance(
        source, str
    ) else source
    canales = _ordenar_para_sondear(
        playlist.channels,
        limite=limite,
        muestreo=muestreo,
        diversificar=diversificar,
        priorizar=priorizar,
    )

    inicio = time.monotonic()
    reporte = SurveyReport(
        source=playlist.source or playlist.name,
        total_canales=len(playlist.channels),
        planeados=len(canales),
        objetivo=objetivo,
    )

    if not canales:
        reporte.segundos = time.monotonic() - inicio
        return reporte

    workers = max(1, min(workers, 8))
    lote = max(workers * 2, 4)
    hechos = 0
    objetivo = max(0, int(objetivo))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for inicio_lote in range(0, len(canales), lote):
            if objetivo and reporte.con_menu >= objetivo:
                reporte.paro_antes = True
                break
            trozo = canales[inicio_lote:inicio_lote + lote]
            futuros = [
                pool.submit(
                    survey_channel,
                    canal,
                    allow_private=allow_private,
                    timeout=timeout,
                )
                for canal in trozo
            ]
            for futuro in as_completed(futuros):
                veredicto = futuro.result()
                reporte.veredictos.append(veredicto)
                hechos += 1
                if progreso is not None:
                    progreso(hechos, len(canales))
    # Orden estable por nombre: dos ejecuciones se leen igual.
    reporte.veredictos.sort(key=lambda v: (v.code, v.name))
    reporte.sondeados = len(reporte.veredictos)
    reporte.segundos = time.monotonic() - inicio
    return reporte


def _seleccionar(
    canales: Sequence[Channel], limite: int, muestreo: str
) -> list[Channel]:
    total = len(canales)
    if limite <= 0 or limite >= total:
        return list(canales)
    if muestreo == "aleatorio":
        return list(random.sample(list(canales), limite))
    if muestreo == "fin":
        return list(canales[-limite:])
    return list(canales[:limite])


def _ordenar_para_sondear(
    canales: Sequence[Channel],
    *,
    limite: int,
    muestreo: str,
    diversificar: bool,
    priorizar: bool,
) -> list[Channel]:
    """Elige **qué canales se preguntan** y en qué orden.

    El orden importa más que el recorte: si ``limite`` se aplica **antes** de
    priorizar, asking por los primeros N de la lista es pedirle opinion a un
    solo proveedor (en IPTV-SV hay 59 canales del mismo host), y encima se
    pierde justo los que podían cumplir. Por eso la prioridad se aplica
    primero y ``limite`` recorta después.

    - ``aleatorio``: baraja y ya. Si el usuario pide azar, se respeta el azar:
      no se reordena ni se agrupa.
    - ``inicio`` (por defecto): prioriza y diversifica sobre la lista entera.
    - ``fin``: mira el final de la lista y, dentro de esa ventana, prioriza y
      diversifica igual.
    """
    if muestreo == "aleatorio":
        barajados = list(canales)
        random.shuffle(barajados)
        return barajados if limite <= 0 else barajados[:limite]

    if muestreo == "fin" and limite > 0 and len(canales) > limite:
        ventana = list(canales[-limite:])
    else:
        ventana = list(canales)
    if priorizar:
        ventana = prioriza_canales(ventana)
    if diversificar:
        ventana = diversificar_por_host(ventana)
    return ventana if limite <= 0 else ventana[:limite]


# ---------------------------------------------------------------------------
# No demorar: elegir rápido los canales que de verdad sirven para la prueba
# ---------------------------------------------------------------------------


def host_de(canal: Channel) -> str:
    """``host:puerto`` de la URL del canal, o "" si no es HTTP."""
    url = str(getattr(canal, "url", "") or "")
    if "://" not in url:
        return ""
    return url.split("://", 1)[-1].split("/", 1)[0]


def diversificar_por_host(canales: Sequence[Channel]) -> list[Channel]:
    """Reparte los canales de forma que los primeros sean de hosts distintos.

    Por qué: los canales del mismo host los sirve el mismo proveedor, con el
    mismo software y el mismo tipo de manifiesto, así que se comportan igual
    entre sí. En una lista como la de IPTV-SV hay 59 canales de un solo host
    (``181.224.255.210:8001``) y 1810 en total: sondear los primeros N tal cual
    es, con suerte, preguntar a un solo proveedor y obtener cinco veces lo
    mismo.

    Intercalar por host hace que **cada sondeo visite a un proveedor distinto**,
    que es justo lo que hace falta para una muestra representativa. No sesga
    el resultado hacia ningún host: el orden dentro de cada host se conserva,
    así que ningún canal se pierde, sólo se aplaza.
    """
    colas: dict[str, list[Channel]] = {}
    for canal in canales:
        colas.setdefault(host_de(canal), []).append(canal)
    # Los hosts sin más canales pasan primero: son los que se agotan antes y
    # los que más aportan a la variedad.
    orden = sorted(colas, key=lambda h: (len(colas[h]), h))
    mezclado: list[Channel] = []
    while True:
        vacios = [h for h in orden if colas[h]]
        if not vacios:
            return mezclado
        # Ronda: uno de cada host. Luego los que aún quedan, en el mismo orden.
        for host in vacios:
            mezclado.append(colas[host].pop(0))
        orden = vacios


def prioriza_canales(canales: Sequence[Channel]) -> list[Channel]:
    """Ordena con pistas de por dónde suele haber alternativas.

    **Heurística, no promesa**: sólo cambia el orden en el que se pregunta, no
    lo que se responde. Sirve para que un ``--conseguir`` con pocos sondeos
    alcance antes a los canales que podrían cumplir, y nunca hace que un canal
    quede fuera de la lista completa.

    Las pistas vienen de medir las listas reales:

    - ``master`` en la ruta: lo es por nombre, casi siempre con variantes.
    - ``playlist.m3u8`` / ``live-stream-playlist`` / ``hls/live``: los masters
      de emisión que publican ladder ABR.
    - ``index.m3u8``: timeshift, y a menudo trae escalera de calidad.
    - un mismo host repetido: se aleja, para no gastarse sondeos arriba (de
      eso se encarga mejor :func:`diversificar_por_host`).
    """
    def puntos(canal: Channel) -> int:
        ruta = str(getattr(canal, "url", "") or "").split("://", 1)[-1]
        ruta = ruta.split("/", 1)[-1].split("?", 1)[0].lower()
        if "master" in ruta:
            return 0
        for marca, peso in (
            ("live-stream-playlist", 1),
            ("/playlist.m3u8", 1),
            ("/hls/live/", 1),
            ("index.m3u8", 2),
            (".m3u8", 3),
        ):
            if marca in ruta:
                return peso
        return 9  # .ts, sin manifiesto, o forma desconocida

    return sorted(canales, key=puntos)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m thetvview.tracks.survey lista.m3u [-n 40] [-j 4]`."""
    parser = argparse.ArgumentParser(
        prog="python -m thetvview.tracks.survey",
        description=(
            "Analiza una lista M3U y dice para cuántos canales hay realmente "
            "opciones de audio, subtítulos o calidad que ofrecer."
        ),
    )
    parser.add_argument("lista", help="ruta o URL de la lista M3U/M3U8")
    parser.add_argument(
        "-n", "--limite", type=int, default=40,
        help="canales a analizar como máximo (0 = todos). Por defecto 40.",
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=4,
        help="sondeos simultáneos (1-8). Por defecto 4.",
    )
    parser.add_argument(
        "--timeout", type=float, default=8.0,
        help="segundos por canal. Por defecto 8.",
    )
    parser.add_argument(
        "--private", action="store_true",
        help="permite destinos privados/loopback de ESTA lista (hay que "
             "confirmarlo a mano en la app; aquí es explícito)",
    )
    parser.add_argument(
        "--muestreo", choices=("inicio", "aleatorio", "fin"), default="inicio",
        help="qué canales tomar de la lista. Por defecto los primeros.",
    )
    parser.add_argument(
        "--ejemplos", type=int, default=5,
        help="ejemplos a mostrar por veredicto (0 = ninguno). Por defecto 5.",
    )
    parser.add_argument(
        "--solo-menu", action="store_true",
        help="muestra sólo los canales que SÍ tendrían menú",
    )
    parser.add_argument(
        "--conseguir", type=int, default=0, metavar="N",
        help="para en cuanto encuentre N canales con menú. Es lo que hace "
             "falta para no demorarse en una lista larga: no se pregunta a "
             "los canales que ya no hacen falta.",
    )
    parser.add_argument(
        "--sin-diversificar", action="store_true",
        help="no intercalar por host (por defecto se intercala, para que "
             "los primeros sondeos visiten proveedores distintos)",
    )
    parser.add_argument(
        "--sin-priorizar", action="store_true",
        help="no ordenar por las pistas de URL que suelen dar resultado",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        playlist = load_playlist(args.lista, allow_private=args.private)
    except Exception as exc:  # noqa: BLE001 - mensaje apto para consola
        print(f"No se pudo cargar la lista: {exc}", file=sys.stderr)
        return 2

    print(
        f"Lista con {len(playlist.channels)} canales. "
        f"Analizando hasta {args.limite if args.limite > 0 else 'todos'}…",
        file=sys.stderr,
    )
    hecho = {"n": 0}

    def _avanza(hechos: int, total: int) -> None:
        if hechos == hecho["n"]:
            return
        hecho["n"] = hechos
        print(f"\r  {hechos}/{total}", end="", file=sys.stderr, flush=True)

    reporte = survey_playlist(
        playlist,
        limite=args.limite,
        workers=args.jobs,
        timeout=args.timeout,
        allow_private=args.private,
        muestreo=args.muestreo,
        progreso=_avanza,
        objetivo=args.conseguir,
        diversificar=not args.sin_diversificar,
        priorizar=not args.sin_priorizar,
    )
    print("\r", end="", file=sys.stderr)

    print(reporte.resumen())
    if args.conseguir:
        print(
            f"\nCanales con menú (los {min(args.conseguir, reporte.con_menu)} "
            "primeros):"
        )
        for veredicto in [v for v in reporte.veredictos if v.has_menu][
            : args.conseguir
        ]:
            print(f"  - {veredicto.describe()}")
    if args.ejemplos > 0:
        veredictos = reporte.veredictos
        if args.solo_menu:
            veredictos = [v for v in veredictos if v.has_menu]
        print("\nEjemplos:")
        for veredicto in veredictos[: args.ejemplos]:
            print(f"  - {veredicto.describe()}")
        if not args.solo_menu:
            print(reporte.ejemplos(args.ejemplos))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
