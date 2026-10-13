"""Límites explícitos del análisis de manifiestos (FR-STREAM-004).

Un manifiesto es un fichero de texto que viene de un tercero: tamaño,
profundidad, cantidad de referencias y tiempo de análisis se acotan **de
forma explícita y visible**, no por casualidad. Este módulo es el único
sitio donde viven esos cuatro números:

======================  ==================  ==================================
límite                  por defecto         dónde se aplica
======================  ==================  ==================  ============
``max_bytes``           8 MiB               HLS: tamaño del texto · DASH: además
                                            ``xml_safe.parse_xml`` (bytes)
``max_depth``           64                  DASH: profundidad del XML (HLS es
                                            plano por diseño: anidar atributos
                                            con comillas no anida documentos)
``max_refs``            10 000              HLS: variantes + ``EXT-X-MEDIA`` +
                                            ``EXTINF`` · DASH: ``BaseURL``,
                                            ``Representation`` y ``Segment*``
``max_seconds``         5,0 s               HLS y DASH: presupuesto de tiempo
                                            del análisis (reloj monótono)
======================  ==================  ==================================

En DASH, la memoria ya estaba cubierta aparte por ``DASH_MAX_NODES`` (200 000
nodos), que es el límite de *memoria* del FR-STREAM-004.

Reloj: :func:`ahora` existe para que los tests midan el límite de tiempo sin
dormir; se parchea ``thetvview.streams.manifest_limits.ahora``.

Errores: cada parser lanza el suyo — :class:`thetvview.streams.hls.HlsParseError`
en HLS y :class:`ManifestLimitError` (un ``ValueError``) en DASH— para que los
llamadores actuales, que ya capturan esos tipos, sigan distinguiendo «el
manifiesto es hostil» de «la red falló» (FR-STREAM-003).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

__all__ = [
    "DEFAULT_MANIFEST_LIMITS",
    "ManifestLimitError",
    "ManifestLimits",
    "ahora",
]


@dataclass(frozen=True)
class ManifestLimits:
    """Los cuatro límites del FR-STREAM-004 en un solo objeto."""

    #: Tamaño máximo del manifiesto, en bytes.
    max_bytes: int = 8 * 1024 * 1024
    #: Profundidad máxima de anidamiento (DASH/XML; HLS es plano).
    max_depth: int = 64
    #: Cantidad máxima de referencias (entradas/URI) que se acumulan.
    max_refs: int = 10_000
    #: Presupuesto de tiempo del análisis, en segundos (reloj monótono).
    max_seconds: float = 5.0


#: Lo que se aplica si nadie pasa límites a mano.
DEFAULT_MANIFEST_LIMITS = ManifestLimits()


class ManifestLimitError(ValueError):
    """Se ha excedido un límite de :class:`ManifestLimits`.

    Es un ``ValueError`` a propósito: los llamadores de DASH ya distinguen
    «XML inutilizable» por ``ValueError`` y este caso entra en la misma
    familia (el manifiesto no sirve), no en la de los fallos de red.
    """


def ahora() -> float:
    """Reloj de los límites de tiempo: monótono y parcheable en tests."""
    return time.monotonic()
