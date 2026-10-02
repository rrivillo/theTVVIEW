"""Parser XML endurecido sin `defusedxml` (SDD §38/§39, gap B6), solo stdlib.

`xml.etree.ElementTree` con expat **sí** expande entidades internas: un
*<DOCTYPE> con subconjunto interno* permite *billion laughs* (DoS por
expansión). Las entidades externas no se resuelven y el DTD externo no se
descarga (expat no lo fetchea), pero el plan y el SDD prohíben ambos.

Por eso, antes de tocar expat se hace un barrido del texto:

=========================  ================================================
``<!DOCTYPE``              rechazado siempre (DTD: interno o externo)
``<!ENTITY``               rechazado siempre (amplificación y XXE)
=========================  ================================================

Ningún XMLTV legítimo necesita un DOCTYPE: el formato define todo el
vocabulario en el propio feed.

Después del parseo se comprueban **profundidad** y **número de nodos**
para cortar árboles deliberadamente profundos o gigantes.

Contrato de errores
-------------------

- Amenaza de seguridad, límite de profundidad/nodos o XML con bytes de
  más → :class:`thetvview.security.ParseError` (hereda de ``ValueError``).
- XML sintácticamente malformado → se propaga **tal cual**
  ``xml.etree.ElementTree.ParseError``, para que los ``except`` y los
  mensajes (``"XMLTV inválido: …"``) preexistentes no cambien.
- Tamaño de respuesta excedido → :class:`thetvview.security.ResponseTooLargeError`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .errors import ParseError, ResponseTooLargeError
from .limits import get_limits, human_size

__all__ = [
    "parse_xml",
    "assert_safe_text",
    "check_tree",
]

# Un solo espaciado opcional entre "<!" y la clave ("<! DOCTYPE" es válido).
_DOCTYPE_RE = re.compile(r"<!\s*doctype", re.IGNORECASE)
_ENTITY_RE = re.compile(r"<!\s*entity", re.IGNORECASE)


def assert_safe_text(text: str) -> None:
    """Rechaza declaraciones de DTD y entidades en `text`.

    Raises:
        ParseError: si aparece ``<!DOCTYPE`` o ``<!ENTITY``.
    """
    if _DOCTYPE_RE.search(text):
        raise ParseError(
            "El documento declara un DTD (DOCTYPE), que XMLTV no necesita. "
            "Se rechaza por seguridad: un DTD puede ejecutar entidades y "
            "agotar la memoria del programa."
        )
    if _ENTITY_RE.search(text):
        raise ParseError(
            "El documento declara entidades XML personalizadas. "
            "Se rechaza por seguridad (expansión infinita de entidades)."
        )


def check_tree(root: ET.Element, *, max_depth: int, max_nodes: int) -> None:
    """Comprueba profundidad y número de elementos de un árbol ya parseado.

    Iterativo (sin recursión): un XMLTV deliberadamente profundo no provoca
    ``RecursionError`` ni desbordamiento de pila antes de rechazarlo.
    """
    depth = 0
    count = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        node, level = stack.pop()
        count += 1
        if count > max_nodes:
            raise ParseError(
                f"El documento tiene demasiados elementos (más de {max_nodes}); "
                "se detiene el parseo por seguridad."
            )
        if level > depth:
            depth = level
            if depth > max_depth:
                raise ParseError(
                    f"El documento es demasiado profundo (más de {max_depth} "
                    "niveles); se detiene el parseo por seguridad."
                )
        for child in node:
            stack.append((child, level + 1))


def _check_size(data: str | bytes, max_bytes: int) -> None:
    size = len(data)
    if size > max_bytes:
        raise ResponseTooLargeError(
            f"El documento supera el límite de {human_size(int(max_bytes))}; "
            "no se puede procesar."
        )


def parse_xml(
    data: str | bytes,
    *,
    max_bytes: int | None = None,
    max_depth: int | None = None,
    max_nodes: int | None = None,
) -> ET.Element:
    """Parsea XML rechazando DTD, entidades y árboles excesivos.

    Args:
        data: XML como texto o bytes (se decodifica ``utf-8-sig`` con
            ``errors="replace"``).
        max_bytes: límite de tamaño (por defecto ``limits.max_file_bytes``).
        max_depth: profundidad máxima (por defecto ``limits.max_xml_depth``).
        max_nodes: número máximo de elementos (por defecto
            ``limits.max_xml_nodes``).

    Returns:
        La raíz :class:`xml.etree.ElementTree.Element`.

    Raises:
        ResponseTooLargeError: el documento supera `max_bytes`.
        ParseError: declara DOCTYPE/entidades o excede profundidad/nodos.
        ET.ParseError: XML sintácticamente malformado (se propaga tal cual).
    """
    lim = get_limits()
    size_limit = lim.max_file_bytes if max_bytes is None else int(max_bytes)
    depth_limit = lim.max_xml_depth if max_depth is None else int(max_depth)
    nodes_limit = lim.max_xml_nodes if max_nodes is None else int(max_nodes)
    if size_limit < 1 or depth_limit < 1 or nodes_limit < 1:
        raise ParseError("Límites de XML no válidos.")

    if isinstance(data, (bytes, bytearray, memoryview)):
        _check_size(data, size_limit)
        text = bytes(data).decode("utf-8-sig", errors="replace")
    elif isinstance(data, str):
        _check_size(data, size_limit)
        text = data
    else:
        raise ParseError("El contenido XML debe ser texto o bytes.")

    assert_safe_text(text)

    # ET.ParseError se propaga a propósito (contrato preexistente).
    root = ET.fromstring(text)
    check_tree(root, max_depth=depth_limit, max_nodes=nodes_limit)
    return root
