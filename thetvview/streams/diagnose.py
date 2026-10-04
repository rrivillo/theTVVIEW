"""Informe de diagnóstico de un canal (SDD-M §21, plan Fase 3).

Qué responde
-------------

El menú de reproducción, cuando un canal falla, contesta hoy «se cerró al
instante». Esto responde a las cuatro preguntas del AC-011:

```text
tipo detectado · esquema · MIME
candidatos [OK]/[--] · backend elegido
DNS · HTTP · playlist · media · veredicto
```

Y todo lo que aparece en el informe pasa por
:func:`~thetvview.security.redaction.redact_text` **antes** de existir como
texto: el §22 del SDD-M se cumple sin un módulo de ``logging`` (decisión D5,
porque el security-check del repo falla por AST si encuentra un ``log`` que
interpole algo con nombre de secreto), redactando en el punto donde nace el
texto.

Tres reglas que no son negociables
---------------------------------

1. **No sale la URL.** Ni la cruda ni la redactada: sale el **tipo**, el
   esquema y el motivo. El informe va a un modal que el usuario puede
   fotografiar y a un canal de soporte; una URL con token no va a ninguno de
   los dos sitios.
2. **No se toca la red salvo que se pregunte.** ``diagnose_connection`` sí
   abre conexión (para eso existe y lo pide un botón explícito), y lo hace por
   :mod:`thetvview.security.safe_http`, con la misma política, el mismo
   timeout y la misma excepción anti-SSRF que el resto de la app. Lo demás
   (:func:`diagnose`) es puro: no hay red, ni disco, ni curses.
3. **Un ❌ con motivo.** Cada línea que falla dice qué revisar. Un
   ``[--]`` sin explicación es peor que no informar: hace que el usuario
   piense que la app está rota.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from enum import Enum

from .. import config
from ..models import Channel
from ..player.protocols import PlayerProtocolSupport, support_for
from ..player.router import Candidate, order_candidates
from ..security.errors import IPTVError
from ..security.redaction import redact_text
from ..security.url_policy import PURPOSE_STREAM, validate_url
from .detector import Detection, detect_protocol
from .transport import Transport, describe as describe_transport, is_multicast_host

__all__ = [
    "StepState",
    "Step",
    "DiagnoseReport",
    "diagnose",
    "diagnose_connection",
    "render_report",
]


class StepState(str, Enum):
    """Estado de una línea del informe."""

    OK = "ok"
    FAIL = "fail"
    SKIP = "skip"
    UNKNOWN = "unknown"

    @property
    def marca(self) -> str:
        return {
            StepState.OK: "[OK]",
            StepState.FAIL: "[!!]",
            StepState.SKIP: "[--]",
            StepState.UNKNOWN: "[??]",
        }[self]


@dataclass(frozen=True)
class Step:
    """Una comprobación y su veredicto."""

    name: str
    state: StepState
    detail: str = ""

    def line(self) -> str:
        """Línea lista para el modal, ya redactada."""
        return f"  {self.state.marca} {self.name}: {self.detail}".rstrip(": ")


@dataclass
class DiagnoseReport:
    """Informe completo de un canal."""

    channel_name: str
    transport: Transport = Transport.UNKNOWN
    protocol: str = "unknown"
    scheme: str = ""
    mime: str = ""
    #: Por qué se classified así, en palabras.
    detection_reason: str = ""
    #: ``True`` si el veredicto salió de una señal fuerte (cuerpo).
    confident: bool = False
    #: Candidatos: nombre → si lo abre ese transporte.
    candidates: list[Candidate] = field(default_factory=list)
    chosen: str = ""
    steps: list[Step] = field(default_factory=list)
    verdict: str = ""
    #: Mensaje de por qué no se puede, si es el caso.
    aviso: str = ""

    def add(self, name: str, state: StepState, detail: str = "") -> None:
        self.steps.append(Step(name, state, redact_text(detail)))

    @property
    def ok(self) -> bool:
        return self.verdict == "READY"


def _detection_of(channel: Channel) -> Detection:
    """Detección **sin red**.

    Con lo que hay en el canal (su URL, y el MIME que el propio canal declare)
    ya se contestan tipo, esquema y MIME. No se descarga nada: hacerlo
    inventaría un problema nuevo, porque un 403 o un timeout aparecerían como
    «no se pudo diagnosticar» en lugar de como «el canal no abre». Para probar
    la conexión está :func:`diagnose_connection`, que es explícito y usa el
    cliente seguro.
    """
    det = detect_protocol(
        url=channel.url or "", content_type=_declared_mime(channel)
    )
    return det


def _declared_mime(channel: Channel) -> str:
    """MIME que el propio canal declara, si lo declara.

    Algunas listas M3U traen el ``Content-Type`` del proveedor como atributo o
    etiqueta. Es la única fuente de MIME sin red: sin él, el informe dice el
    tipo pero no el tipo declarado, que es justo lo que el §21 pide.
    """
    for tag, raw in channel.extra_options or ():
        if str(tag).upper() in ("CONTENT-TYPE", "EXTVLCOPT"):
            clave, _, valor = str(raw).partition("=")
            if clave.strip().lower() in ("content-type", "http-content-type"):
                return valor.strip().split(";", 1)[0].strip()
    declared = (channel.attrs or {}).get("content-type", "")
    return str(declared).strip().split(";", 1)[0].strip()


def diagnose(channel: Channel, *, available: dict[str, str | None] | None = None) -> DiagnoseReport:
    """Informe **sin red** de un canal (SDD-M §21, AC-011).

    Args:
        channel: el canal tal cual está en la lista, con su referencia opaca
            si la tiene. Si la URL es ``ipcam://…`` o ``xtream://…`` no se
            intenta adivinar el transporte: no se puede sin resolver, y
            resolver aquí escribiría credenciales en un informe.

    Returns:
        :class:`DiagnoseReport`. Nunca lanza, ni siquiera con una URL inválida:
        el informe de un canal roto tiene que poder **decir** que está roto.
    """
    informe = DiagnoseReport(channel_name=channel.name or "(sin nombre)")
    url = channel.url or ""

    if url.startswith(("ipcam://", "xtream://", "xtream-ts://")):
        informe.transport = Transport.UNKNOWN
        informe.protocol = "opaco"
        informe.verdict = "REFERENCIA OPACA"
        informe.aviso = (
            "Este canal se guarda como referencia sin credenciales, así que el "
            "tipo no se puede averiguar sin resolverla. Se sabrá al reproducirlo."
        )
        informe.add("referencia", StepState.SKIP, "ipcam:// (cámara IP)")
        return informe

    try:
        validate_url(url, PURPOSE_STREAM)
        _politica = StepState.OK
        _detalle_politica = "aceptada por la política de URL"
    except IPTVError as exc:
        _politica = StepState.FAIL
        _detalle_politica = str(exc)
    informe.add("política de URL", _politica, _detalle_politica)

    det = _detection_of(channel)
    informe.transport = det.transport
    informe.protocol = det.protocol
    informe.scheme = det.scheme
    informe.mime = det.mime
    informe.detection_reason = det.reason
    informe.confident = det.confident

    nombre_tp = describe_transport(det.transport)
    informe.add(
        "tipo detectado",
        StepState.OK if det.protocol != "unknown" else StepState.UNKNOWN,
        f"{det.protocol} sobre {nombre_tp}"
        + (f" (MIME {det.mime})" if det.mime else ""),
    )
    informe.add("cómo se decidió", StepState.OK, det.reason or "sin motivo")
    informe.add(
        "pistas seleccionables",
        StepState.OK if det.is_selectable else StepState.SKIP,
        "sí" if det.is_selectable else "no: es un directo de pista única",
    )

    # Candidatos: qué reproductor abre este transporte aquí.
    informe.candidates = order_candidates(url, available=available)
    viables = [c for c in informe.candidates if c.ok]
    for cand in informe.candidates:
        soporte: PlayerProtocolSupport | None = support_for(cand.name)
        if cand.ok:
            informe.add(f"reproductor {cand.name}", StepState.OK, "abre este transporte")
        else:
            detalle = soporte.why_not(det.transport) if soporte else cand.motivo
            informe.add(f"reproductor {cand.name}", StepState.SKIP, detalle)

    if viables:
        informe.chosen = viables[0].name
        informe.add("reproductor elegido", StepState.OK, informe.chosen)
        informe.verdict = "READY"
    else:
        informe.add(
            "reproductor elegido",
            StepState.FAIL,
            "ninguno instalado abre este transporte",
        )
        from ..player.router import explain_no_backend

        informe.aviso = explain_no_backend(url, informe.candidates)
        informe.verdict = "SIN REPRODUCTOR"

    if det.transport is Transport.UDP and is_multicast_host(_host_of(url)):
        informe.add(
            "dirección multicast",
            StepState.SKIP,
            "es un grupo multicast: depende de tu red (interfaz, firewall, router)",
        )
    return informe


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def diagnose_connection(
    channel: Channel,
    *,
    allow_private: bool = False,
    timeout: float = 6.0,
) -> list[Step]:
    """Comprueba la conexión **de verdad**, paso a paso (SDD-M §21).

    Esto **sí** abre red, y lo pide el usuario explícitamente. Pasa por
    :mod:`thetvview.security.safe_http`, de modo que comparte la política de
    URL, la excepción anti-SSRF por fuente, el timeout y el límite de bytes con
    el resto de la app. No se usa ``socket`` a pelo para nada que salga de aquí:
    el ``socket.getaddrinfo`` es la única excepción, y sólo para resolver DNS.

    Args:
        allow_private: el flag ``allow_private_network`` **de la fuente**. Una
            cámara o un servidor en la LAN dan DNS «ok» y luego un rechazo por
            la política; el informe lo dice en vez de dejarlo adivinar.
    """
    pasos: list[Step] = []
    url = channel.url or ""
    host = _host_of(url)

    # 1. DNS
    if not host:
        pasos.append(Step("DNS", StepState.SKIP, "sin servidor en la dirección"))
        return pasos
    try:
        socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        pasos.append(
            Step("DNS", StepState.FAIL, f"no se resuelve «{host}» ({exc.strerror or exc})")
        )
        # Sin DNS no hay nada más que probar, y_each paso posterior daría un
        # error que no es la causa real.
        return pasos
    pasos.append(Step("DNS", StepState.OK, f"«{host}» se resuelve"))

    # 2. HTTP: sólo tiene sentido en transportes HTTP.
    from .transport import HTTP_TRANSPORTS

    if det_transport(url) not in HTTP_TRANSPORTS:
        pasos.append(
            Step(
                "HTTP",
                StepState.SKIP,
                "este transporte no es HTTP: lo negocia el reproductor",
            )
        )
        pasos.append(
            Step(
                "reproductor",
                StepState.UNKNOWN,
                "no se puede comprobar sin lanzar el reproductor",
            )
        )
        return pasos

    from ..security.safe_http import SafeHttpClient

    try:
        cliente = SafeHttpClient(timeout=timeout, allow_private=allow_private)
        estado = cliente.head(url)
    except IPTVError as exc:
        pasos.append(Step("HTTP", StepState.FAIL, redact_text(str(exc))))
        return pasos
    except OSError as exc:
        pasos.append(Step("HTTP", StepState.FAIL, f"fallo de red: {exc.__class__.__name__}"))
        return pasos
    pasos.append(Step("HTTP", StepState.OK, f"responde {estado.status or '?'}"))

    # 3. Playlist / media: qué formato dice servir.
    det = detect_protocol(
        url=url, content_type=str(estado.headers.get("content-type", ""))
    )
    if det.is_hls:
        pasos.append(
            Step("playlist", StepState.OK, "HLS: el reproductor bajará el manifiesto")
        )
        pasos.append(Step("media", StepState.SKIP, "los segmentos los pide el reproductor"))
    elif det.is_dash:
        pasos.append(Step("playlist", StepState.OK, "DASH (MPD)"))
        pasos.append(Step("media", StepState.SKIP, "los segmentos los pide el reproductor"))
    elif det.protocol == "unknown":
        pasos.append(
            Step(
                "playlist",
                StepState.UNKNOWN,
                "no se ha podido determinar el formato; puede que sea un "
                "403 o una página de error del proveedor",
            )
        )
        pasos.append(Step("media", StepState.SKIP, "sin formato conocido"))
    else:
        pasos.append(Step("playlist", StepState.SKIP, "no es un manifiesto"))
        pasos.append(Step("media", StepState.OK, det.protocol))
    return pasos


def det_transport(url: str) -> Transport:
    """Transporte de una URL sin más análisis (atajo interno)."""
    return detect_protocol(url=url).transport


def render_report(
    report: DiagnoseReport, *, pas: list[Step] | None = None
) -> str:
    """Texto del informe, listo para un modal.

    No incluye la URL en ningún caso: sólo el tipo, el esquema y el motivo.
    """
    lineas = [
        f"Canal: {redact_text(report.channel_name)}",
        f"Transporte: {describe_transport(report.transport)}"
        + (f" ({report.scheme})" if report.scheme else ""),
        f"Contenido: {report.protocol}",
    ]
    if report.mime:
        lineas.append(f"MIME: {report.mime}")
    lineas.append(f"Reproductor elegido: {report.chosen or 'ninguno'}")
    if report.steps:
        lineas.append("")
        for paso in report.steps:
            lineas.append(paso.line())
    if pas:
        lineas.append("")
        lineas.append("Conexión:")
        for paso in pas:
            lineas.append(paso.line())
    lineas.append("")
    lineas.append(f"Resultado: {report.verdict}")
    if report.aviso:
        lineas.append(redact_text(report.aviso))
    return "\n".join(lineas)