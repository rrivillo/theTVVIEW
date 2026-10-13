"""Historial persistente de diagnósticos (DIAG-004 · FR-DIAG-005/010).

Dos cosas da este módulo:

- **Fallo nuevo vs. recurrente** (FR-DIAG-005): un historial acotado
  permite decirle al usuario «este canal ya fallaba antes» sin volver a
  la red. Se consulta antes de registrar el resultado nuevo.
- **Límites de verdad** (FR-DIAG-010): cantidad, edad y tamaño total, los
  tres a la vez y a la vista en las constantes. El historial no crece
  para siempre, ni con el tiempo, ni con una lista de canales enorme.

Cada entrada es lo mínimo: cuándo, el canal (**su clave**, nunca su URL),
si fue éxito o fallo, un motivo corto y una fase. Todo pasa por
redacción al registrarse; el fichero se escribe con 0600 y de forma
atómica. Un historial que no se puede leer ni escribir se comporta como
vacío: no rompe el diagnóstico ni la TUI.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..security.local_files import chmod_private, read_limited_text
from ..security.redaction import redact_text

__all__ = [
    "HistorialDiagnostico",
    "MAX_DIAS",
    "MAX_ENTRADAS",
    "MAX_BYTES",
    "RegistroHistorial",
]

#: Límites del FR-DIAG-010: cantidad, edad y tamaño total (el más
#: restrictivo manda). Acotados a mano y a la vista.
MAX_ENTRADAS: int = 500
MAX_DIAS: float = 30.0
MAX_BYTES: int = 256 * 1024

#: Formato del fichero. Subir versión sólo si cambia el sentido de algo.
_VERSION = 1

#: Longitud máxima de los campos de texto: son etiquetas, no cuerpos.
_LIMITE_TEXTO = 200


@dataclass(frozen=True)
class RegistroHistorial:
    """Un resultado de diagnóstico, mínimo y sin secretos."""

    #: Epoch del resultado.
    momento: float
    #: Clave legible del canal (tvg-id o nombre), ya redactada.
    canal: str
    #: True si el diagnóstico dio por bueno el canal.
    exito: bool
    #: Motivo corto (veredicto o código), si lo hay.
    motivo: str = ""
    #: Fase en la que se decidió, si se sabe.
    fase: str = ""

    def a_dict(self) -> dict[str, Any]:
        """Versión serializable."""
        return asdict(self)

    @classmethod
    def de_dict(cls, datos: Any) -> RegistroHistorial | None:
        """Reconstruye un registro; devuelve None si el dato no sirve.

        Un registro ilegible se **salta**: el historial es una ayuda, no
        una base de datos, y una entrada rara no puede tirar el resto.
        """
        if not isinstance(datos, dict):
            return None
        try:
            momento = float(datos.get("momento"))  # type: ignore[arg-type]
            canal = str(datos.get("canal", ""))
            exito = bool(datos.get("exito", False))
        except (TypeError, ValueError):
            return None
        return cls(
            momento=momento,
            canal=canal,
            exito=exito,
            motivo=str(datos.get("motivo", ""))[:_LIMITE_TEXTO],
            fase=str(datos.get("fase", ""))[:_LIMITE_TEXTO],
        )


class HistorialDiagnostico:
    """Historial acotado por cantidad, edad y tamaño, en JSON y 0600.

    Thread-safe: registra el hilo de la TUI y puede consultar el de la
    sonda. La escritura es atómica (tmp + rename) para que un corte de
    luz no deje el historial a medias.
    """

    def __init__(
        self,
        ruta: str | Path | None = None,
        *,
        max_entradas: int = MAX_ENTRADAS,
        max_dias: float = MAX_DIAS,
        max_bytes: int = MAX_BYTES,
        ahora: Callable[[], float] | None = None,
    ) -> None:
        self.ruta = Path(ruta) if ruta is not None else config.DIAGNOSTICS_JSON
        self.max_entradas = max(1, int(max_entradas))
        self.max_dias = float(max_dias)
        self.max_bytes = max(256, int(max_bytes))
        self._ahora: Callable[[], float] = ahora or time.time
        self._entradas: list[RegistroHistorial] = []
        self._cargado = False
        self._lock = threading.Lock()

    # -- uso ------------------------------------------------------------------

    def registrar(
        self,
        canal: str,
        exito: bool,
        *,
        motivo: str = "",
        fase: str = "",
    ) -> RegistroHistorial:
        """Anota un resultado (redactado) y lo persiste. Devuelve el registro.

        Purgar y guardar nunca lanzan: un historial que no se puede
        escribir no puede impedir diagnosticar (se queda en memoria).
        """
        registro = RegistroHistorial(
            momento=self._ahora(),
            canal=redact_text(str(canal))[:_LIMITE_TEXTO],
            exito=bool(exito),
            motivo=redact_text(str(motivo))[:_LIMITE_TEXTO],
            fase=str(fase)[:_LIMITE_TEXTO],
        )
        with self._lock:
            self._cargar()
            self._entradas.append(registro)
            self._purgar()
            try:
                self._escribir()
            except OSError:
                pass
        return registro

    def es_recurrente(self, canal: str, *, motivo: str | None = None) -> bool:
        """True si **ya hubo un fallo** de este canal en el historial.

        Se consulta antes de registrar el resultado de hoy: así el fallo
        de ahora distingue entre «nuevo» y «recurrente» (FR-DIAG-005).
        `motivo`, si se pasa, exige el mismo motivo corto.
        """
        clave = redact_text(str(canal))[:_LIMITE_TEXTO]
        with self._lock:
            self._cargar()
            anteriores = list(self._entradas)
        return any(
            (not r.exito)
            and r.canal == clave
            and (motivo is None or r.motivo == motivo)
            for r in anteriores
        )

    def entradas(self) -> list[RegistroHistorial]:
        """Copia del historial, en orden cronológico."""
        with self._lock:
            self._cargar()
            return list(self._entradas)

    def contar(self, canal: str) -> tuple[int, int]:
        """(éxitos, fallos) de un canal en el historial."""
        clave = redact_text(str(canal))[:_LIMITE_TEXTO]
        with self._lock:
            self._cargar()
            previos = [r for r in self._entradas if r.canal == clave]
        return (
            sum(1 for r in previos if r.exito),
            sum(1 for r in previos if not r.exito),
        )

    # -- Internos -------------------------------------------------------------

    def _cargar(self) -> None:
        """Carga perezosa; ya bajo candado."""
        if self._cargado:
            return
        self._cargado = True
        self._entradas = self._leer()
        self._purgar()

    def _leer(self) -> list[RegistroHistorial]:
        try:
            texto = read_limited_text(self.ruta, max_bytes=self.max_bytes)
        except OSError:
            return []
        try:
            datos = json.loads(texto)
        except ValueError:
            return []
        crudos = datos.get("entradas") if isinstance(datos, dict) else None
        if not isinstance(crudos, list):
            return []
        salidas: list[RegistroHistorial] = []
        for item in crudos:
            registro = RegistroHistorial.de_dict(item)
            if registro is not None:
                salidas.append(registro)
        salidas.sort(key=lambda r: r.momento)
        return salidas[-self.max_entradas :]

    def _purgar(self) -> None:
        """Aplica los límites de edad y cantidad. Ya bajo candado."""
        corte = self._ahora() - self.max_dias * 86_400.0
        vivas = [r for r in self._entradas if r.momento >= corte]
        if len(vivas) > self.max_entradas:
            vivas = vivas[-self.max_entradas :]
        self._entradas = vivas

    def _texto_serializado(self) -> str:
        """JSON compacto que cabe en `max_bytes`; si no, caen las más viejas."""
        while True:
            datos = {
                "version": _VERSION,
                "entradas": [r.a_dict() for r in self._entradas],
            }
            texto = json.dumps(datos, ensure_ascii=False, separators=(",", ":"))
            if (
                len(texto.encode("utf-8")) <= self.max_bytes
                or len(self._entradas) <= 1
            ):
                return texto
            # Se descarta la mitad vieja de golpe: el tamaño es un tope de
            # emergencia, no la política normal (esa es por cantidad/edad).
            self._entradas = self._entradas[len(self._entradas) // 2 :]

    def _escribir(self) -> None:
        """Escritura atómica con 0600. Lanza OSError si el disco no deja."""
        texto = self._texto_serializado()
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        chmod_private(self.ruta.parent, directory=True)
        temporal = self.ruta.with_name(self.ruta.name + ".tmp")
        descriptor = os.open(
            temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            archivo.write(texto)
        os.replace(temporal, self.ruta)
        chmod_private(self.ruta)


def default_historial() -> HistorialDiagnostico:
    """Historial en `data/diagnostics.json` con los límites por defecto."""
    return HistorialDiagnostico()
