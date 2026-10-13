"""Informe diagnóstico §11.3 (DIAG-002 · FR-DIAG-007/008/009).

Compone el modelo mínimo del SDD §11.3 a partir de datos que **ya se
recojeron** quien pudo medirlos: la sesión (:meth:`PlaybackSession.para_informe`),
el supervisor (:meth:`ReconnectSupervisor.para_informe`), los pasos del
diagnóstico de conexión y, si existe, el :class:`~thetvview.diagnostics.events.EventBus`.

Tres reglas gobiernan el módulo:

- **Cero red** (FR-DIAG-009): generar el informe no manda ni una
  solicitud. El diagnóstico activo (`diagnose_connection`) es otra cosa y
  sólo corre si el usuario lo pide; su resultado, si viene, se pinta.
- **Nada inventado** (§11.4): lo que no se conoce se escribe
  ``desconocido``; lo que no se midió no aparece ni se rellena con ceros.
- **Sin secretos** (FR-DIAG-008): todo valor que se imprime pasa por
  ``redact_text`` por si acaso, y la URL del canal nunca se pide: el
  canal va por su clave legible, no por su referencia.

La salida va a un modal **y** a un archivo creado con permisos 0600
(:func:`guardar_informe`), porque un informe existe para poder compartirse.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config
from ..security.local_files import chmod_private
from ..security.redaction import redact_text
from .events import Event

__all__ = [
    "DESCONOCIDO",
    "construir_informe",
    "guardar_informe",
]

#: Lo que se imprime donde no hay dato. No se sustituye nunca por un cero
#: ni por una suposición: §11.3 pide valores desconocidos **claramente**
#: identificados.
DESCONOCIDO = "desconocido"

#: Un volcado de dict nunca baja de este nivel: los informes son datos
#: planos, y una profundidad finita evita que un dict raro se vuelva un
#: problema.
_PROFUNDIDAD_MAX = 3


def _valor(v: Any) -> str:
    """Texto para un valor suelto, redactado y con desconocidos marcados."""
    if v is None or v == "":
        return DESCONOCIDO
    return redact_text(str(v))


def _volcar(datos: Any, salida: list[str], nivel: int = 0) -> None:
    """Añade `datos` (dict/lista/escalar) a `salida`, indentado y redactado."""
    sangria = "  " * nivel
    if isinstance(datos, Mapping):
        for clave, valor in datos.items():
            etiqueta = redact_text(str(clave))
            if isinstance(valor, (Mapping, Sequence)) and not isinstance(
                valor, (str, bytes)
            ):
                if valor:  # vacío → la línea sale con «desconocido»
                    salida.append(f"{sangria}{etiqueta}:")
                    if nivel < _PROFUNDIDAD_MAX:
                        _volcar(valor, salida, nivel + 1)
                    else:
                        salida.append(f"{sangria}  (demasiado profundo)")
                else:
                    salida.append(f"{sangria}{etiqueta}: {DESCONOCIDO}")
            else:
                salida.append(f"{sangria}{etiqueta}: {_valor(valor)}")
    elif isinstance(datos, Sequence):
        for item in datos:
            if isinstance(item, (Mapping, Sequence)) and not isinstance(
                item, (str, bytes)
            ):
                _volcar(item, salida, nivel + 1)
            else:
                salida.append(f"{sangria}- {_valor(item)}")
    else:
        salida.append(f"{sangria}{_valor(datos)}")


def _seccion(titulo: str, cuerpo: list[str]) -> list[str]:
    if not cuerpo:
        return []
    return ["", f"== {titulo} =="] + cuerpo


def construir_informe(
    *,
    canal: str = "",
    sesion: Mapping[str, Any] | None = None,
    motor: Mapping[str, Any] | None = None,
    conexion: Sequence[Any] | None = None,
    analisis: str | None = None,
    http_estado: int | None = None,
    content_type: str | None = None,
    eventos: Sequence[Event] | None = None,
    historial: str | None = None,
    version_app: str | None = None,
    ahora: datetime | None = None,
) -> str:
    """Compone el informe §11.3 como texto listo para modal y archivo.

    Todos los argumentos son opcionales: un informe sin nada sigue siendo
    un informe, y dice exactamente eso —todo desconocido— en lugar de
    lanzar o de rellenar huecos.

    Args:
        canal: clave legible del canal (**nunca** su URL).
        sesion: ``session.para_informe()``; aporta correlación, sesión,
            fase, marcas de tiempo, intentos y motivo de fin.
        motor: ``supervisor.para_informe()``; aporta evidencia, errores
            normalizados, reconexiones y espera.
        conexion: pasos del diagnóstico activo (objetos con ``.line()`` o
            textos ya redactados). Vacío o None = no se hizo red.
        analisis: resultado del análisis de manifiesto, en una frase.
        http_estado/content_type: sólo si se midieron.
        eventos: eventos del bus (ya redactados al emitirlos).
        historial: nota del historial local (fallo nuevo o recurrente,
            FR-DIAG-005), ya redactada por quien la midió.
        version_app: por defecto, la de `thetvview`.
        ahora: instante del informe; por defecto, este momento.

    Returns:
        Texto UTF-8 con encabezado, secciones presentes y la advertencia
        final de que un diagnóstico no siempre determina la causa raíz.
    """
    if version_app is None:
        from .. import __version__ as version_app
    momento = ahora or datetime.now().astimezone()

    salida: list[str] = [
        f"Informe de diagnóstico · theTVVIEW {version_app}",
        f"Generado: {momento.isoformat(timespec='seconds')}",
        "",
    ]

    datos_sesion: Mapping[str, Any] = dict(sesion or {})
    correlation_id = datos_sesion.get("correlation_id")
    salida.append(f"Correlación: {_valor(correlation_id)}")
    salida.append(f"Sesión: {_valor(datos_sesion.get('session_id'))}")
    salida.append(f"Canal: {_valor(canal or datos_sesion.get('canal'))}")
    motor_datos: Mapping[str, Any] = dict(motor or {})
    salida.append(f"Motor: {_valor(datos_sesion.get('reproductor'))}")
    salida.append(
        f"Estado: {_valor(datos_sesion.get('estado'))}"
        f" · Fase: {_valor(datos_sesion.get('fase'))}"
    )

    if datos_sesion.get("marcas"):
        salida += _seccion("Tiempos medidos (desconocido = no medido)", _sec_dict(datos_sesion["marcas"]))
    if http_estado is not None or content_type is not None:
        salida.append("")
        salida.append(
            f"HTTP: {_valor(http_estado)} · content-type: {_valor(content_type)}"
        )
    if analisis:
        salida.append(f"Análisis de manifiesto: {_valor(analisis)}")
    if datos_sesion.get("detalle_fin") or datos_sesion.get("motivo_fin"):
        salida += _seccion(
            "Motivo de finalización",
            [
                f"Motivo: {_valor(datos_sesion.get('motivo_fin'))}",
                f"Detalle: {_valor(datos_sesion.get('detalle_fin'))}",
            ],
        )

    cuerpo_motor = _sec_dict(motor_datos)
    if cuerpo_motor:
        salida += _seccion("Motor (evidencia y error normalizado)", cuerpo_motor)

    intentos = datos_sesion.get("intentos") or []
    if intentos:
        salida += _seccion("Historial de reintentos", _sec_seq(intentos))

    if historial:
        salida += _seccion(
            "Historial local (FR-DIAG-005)", [_valor(historial)]
        )

    if conexion:
        pasos = [
            p.line() if hasattr(p, "line") else _valor(p) for p in conexion
        ]
        salida += _seccion("Conexión (diagnóstico activo pedido)", pasos)

    if eventos:
        lineas = []
        for evento in eventos:
            lineas.append(
                f"- {datetime.fromtimestamp(evento.timestamp).isoformat(timespec='seconds')}"
                f" {getattr(evento.severity, 'value', evento.severity)}"
                f" {_valor(evento.event_type)}"
                f" ({_valor(evento.reason_code)})"
            )
        salida += _seccion("Eventos (§3.3, redactados al emitir)", lineas)

    salida += [
        "",
        "Nota: un diagnóstico no siempre puede determinar la causa raíz:",
        "lo anterior son evidencias medidas, no conclusiones.",
    ]
    return redact_text("\n".join(salida))


def _sec_dict(datos: Any) -> list[str]:
    lineas: list[str] = []
    _volcar(datos, lineas, nivel=0)
    return lineas


def _sec_seq(elementos: Sequence[Any]) -> list[str]:
    lineas: list[str] = []
    for indice, item in enumerate(elementos, start=1):
        lineas.append(f"Intento {indice}:")
        if isinstance(item, (Mapping, Sequence)) and not isinstance(item, (str, bytes)):
            _volcar(item, lineas, nivel=1)
        else:
            lineas.append(f"  {_valor(item)}")
    return lineas


def guardar_informe(texto: str, *, ruta: Path | None = None) -> Path:
    """Escribe el informe en un archivo **0600** y devuelve su ruta (FR-DIAG-008).

    El archivo se crea con el permiso desde el primer byte: un informe no
    puede existir ni por un instante con permisos de lectura de otros. En
    Windows no hay permisos POSIX y `chmod_private` lo salta sin más.

    Args:
        texto: informe ya compuesto por :func:`construir_informe`.
        ruta: destino; por defecto, ``data/diagnostics/informe-<fecha>.txt``.

    Returns:
        La ruta definitiva (si el nombre ya existía, se numeró).

    Raises:
        OSError: el disco no permite escribir. El llamador decide: en la
        TUI se sigue mostrando el informe en el modal aunque no se pueda
        guardar.
    """
    if ruta is None:
        momento = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = config.DIAGNOSTICS_DIR / f"informe-{momento}"
        ruta = base.with_suffix(".txt")
        contador = 2
        while ruta.exists():
            ruta = base.with_name(f"{base.name}-{contador}").with_suffix(".txt")
            contador += 1

    ruta.parent.mkdir(parents=True, exist_ok=True)
    chmod_private(ruta.parent, directory=True)
    # O_CREAT + modo 0600: nace privado. umask no puede hacerlo más ancho.
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
        archivo.write(texto)
    chmod_private(ruta)
    return ruta
