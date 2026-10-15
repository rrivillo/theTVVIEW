"""Fase 8 U-07 helper module."""

from __future__ import annotations

from thetvview.sources import (
    es_error_recuperable,
    clasificar_error_recuperable,
    FailoverController,
    FailoverLimits,
    FailoverPlan,
    ResultadoReconciliacion,
    reconciliar_preferencias,
)

__all__ = [
    "FAILOVER_CANCELADO",
    "cambio_de_fuente_mensaje",
    "sin_failover_mensaje",
    "modal_discreto",
    "reconciliar",
    "FailoverController",
    "FailoverLimits",
    "FailoverPlan",
    "es_recuperable",
    "clasificar",
    "ResultadoReconciliacion",
    "reconciliar_preferencias",
]


FAILOVER_CANCELADO = "cancelado"


def es_recuperable(error):
    try:
        return bool(es_error_recuperable(error))
    except Exception:
        return bool(clasificar_error_recuperable(error))


def clasificar(error):
    return es_recuperable(error)


def cambio_de_fuente_mensaje(fuente_actual, fuente_nueva, motivo="", cancelable=True):
    actual = str(fuente_actual or "la fuente actual").strip()
    nueva = str(fuente_nueva or "otra fuente").strip()
    partes = [
        "La reproduccion va a cambiar de <<%s>> a <<%s>>." % (actual, nueva)
    ]
    if motivo:
        partes.append(str(motivo).strip())
    if cancelable:
        partes.append("Puedes cancelar y seguir con la fuente actual.")
    else:
        partes.append("No se puede cancelar este cambio.")
    return "\n\n".join(partes)


def sin_failover_mensaje(motivo):
    motivo = (motivo or "error no recuperable").strip()
    return "No se cambia de fuente: " + motivo + ".\n\nLa reproduccion sigue con la fuente actual."


def modal_discreto(resultado):
    return str(getattr(resultado, "modal_discreto", "") or "")


def reconciliar(preferencias, capacidades, confirmado_por_reproductor=False):
    return reconciliar_preferencias(
        preferencias,
        capacidades,
        confirmado_por_reproductor=bool(confirmado_por_reproductor),
    )
