"""Historial persistente de diagnósticos (DIAG-004 · FR-DIAG-005/010).

Dos cosas da este módulo:

- **Fallo nuevo vs. recurrente** (FR-DIAG-005): un historial acotado
  permite decirle al usuario «este canal ya fallaba antes» sin volver a
  la red. Se consulta antes de registrar el resultado nuevo.
- **Límites de verdad** (FR-DIAG-010): cantidad, edad y tamaño total, los
  tres a la vez y a la vista en las constantes. El historial no crece
  para siempre, ni con el tiempo, ni con una lista de canales enorme.

Cada entrada es lo mínimo: cuándo, el canal (**su clave**, nunca su URL), si
fue éxito o fallo, un motivo corto y una fase. Todo pasa por redacción **al
registrarse** (nunca al leer); el fichero se escribe con 0600 y de forma
atómica. Un historial que no se puede leer ni escribir se comporta como vacío:
no rompe el diagnóstico ni la TUI.

Esquema v2 (H-03)
-----------------

La v1 guardaba cinco campos y no contestaba ninguna de las preguntas que el
usuario hace: ¿de qué lista?, ¿con qué motor?, ¿por qué?, ¿reintentar
serviría?, ¿cuánto tardó? La v2 añade ``source_id`` (opaco), ``player``,
``error_code``, ``recoverable`` y ``duration_ms``.

Los cinco son aditivos y con valor por defecto, que es lo que hace la
migración posible en los dos sentidos: un lector v1 ignora las claves que no
conoce, y uno v2 deja en «desconocido» lo que no está (:meth:`escribir_como`
para retroceder). Lo que **no** se hace nunca es rellenar un hueco con una
conjetura: una entrada migrada no gana un ``error_code`` que nadie observó.
"""

from __future__ import annotations

import json
import os
import threading
import time
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..security.local_files import chmod_private, read_limited_text
from ..security.redaction import redact_text
from .taxonomy import causa_de_codigo

__all__ = [
    "HistorialDiagnostico",
    "MAX_DIAS",
    "MAX_ENTRADAS",
    "MAX_BYTES",
    "RegistroHistorial",
    "VERSION_ACTUAL",
    "source_id_opaco",
]

#: Límites del FR-DIAG-010: cantidad, edad y tamaño total (el más
#: restrictivo manda). Acotados a mano y a la vista.
MAX_ENTRADAS: int = 500
MAX_DIAS: float = 30.0
MAX_BYTES: int = 256 * 1024

#: Formato del fichero (H-03).
#:
#: v1: momento, canal, exito, motivo, fase. Suficiente para decir «este canal
#:     falló antes»; insuficiente para decir de qué lista, con qué motor,
#:     por qué, ni si esperar más iba a arreglarlo.
#: v2: añade ``source_id``, ``player``, ``error_code``, ``recoverable`` y
#:     ``duration_ms``. Los cinco son **aditivos y con valor por defecto**, que
#:     es lo que hace la migración posible en los dos sentidos: un lector v1
#:     ignora las claves que no conoce, y uno v2 rellena lo que falta con
#:     «desconocido» en vez de inventarlo.
VERSION_ACTUAL: int = 2

#: Los cinco campos de la v1. Se usan al escribir «como la v1», que es lo que
#: hace posible el retroceso (H-03).
_CAMPOS_V1: tuple[str, ...] = ("momento", "canal", "exito", "motivo", "fase")

#: Los cinco campos de la v2. Se **omiten** cuando no se saben; los de la v1
#: no, porque su forma vacía ya es lo que leían las versiones anteriores.
_CAMPOS_V2: tuple[str, ...] = (
    "source_id", "player", "error_code", "recoverable", "duration_ms",
)

#: Longitud máxima de los campos de texto: son etiquetas, no cuerpos.
_LIMITE_TEXTO = 200

#: Longitud del resumen que sustituye a la URL y al usuario en el
#: ``source_id``. Suficiente para distinguir dos cuentas, demasiado corto para
#: ser una dirección.
_LONGITUD_RESUMEN = 12

#: Marca de un ``source_id`` ya opacado. Hace la operación idempotente y
#: legible a ojo en el fichero: si empieza por esto, ya está limpio.
_PREFIJO_OPACO = "fuente:"


def source_id_opaco(source_id: object) -> str:
    """Convierte un ``source_id`` real en algo que pueda ir a disco.

    El identificador real de una fuente es ``xtream|<panel>|<url>|<usuario>`` o
    ``m3u|<lista>|<url>``, y las dos mitades que no son el nombre son justo lo
    que no puede aparecer en un fichero que el usuario puede compartir o copiar
    a otro equipo. Pero el **nombre** de la lista no es un secreto, y sin él el
    modal «Historial del canal» no puede decir de qué lista estaba hablando.

    Así que se queda el nombre y se cambia el resto por un resumen: la
    identidad sigue siendo estable y comparable, y lo que se guarda no sirve
    para volver a la dirección.

    **Es idempotente.** El resultado lleva el prefijo ``fuente:``, así que
    opacar algo ya opacado devuelve exactamente lo mismo. Hace falta: el
    historial opaca al escribir *y* al leer, y sin esta propiedad una ida y
    vuelta por disco would dejar el identificador hecho polvo dos veces.
    """
    if not isinstance(source_id, str):
        return ""
    bruto = source_id.strip()
    if not bruto:
        return ""
    if bruto.startswith(_PREFIJO_OPACO):
        return bruto
    partes = bruto.split("|")
    tipo = redact_text(partes[0])[:_LIMITE_TEXTO] if partes else ""
    nombre = redact_text(partes[1])[:_LIMITE_TEXTO] if len(partes) > 1 else ""
    resumen = hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:_LONGITUD_RESUMEN]
    if not nombre:
        # Sin nombre (un id mal formado): el tipo solo no distinguiría nada.
        # El resumen sí.
        return f"{_PREFIJO_OPACO}{tipo}/{resumen}"
    return f"{_PREFIJO_OPACO}{tipo}/{nombre}#{resumen}"


@dataclass(frozen=True)
class RegistroHistorial:
    """Un resultado de diagnóstico, mínimo y sin secretos."""

    #: Epoch del resultado.
    momento: float
    #: Clave legible del canal (tvg-id o nombre), ya redactada.
    canal: str
    #: True si el diagnóstico dio por bueno el canal.
    exito: bool
    #: Motivo corto en castellano libre (veredicto del diagnóstico), si lo hay.
    motivo: str = ""
    #: Fase en la que se decidió, si se sabe.
    fase: str = ""
    #: De qué lista o cuenta vino el canal, **opaco** (§H-03).
    source_id: str = ""
    #: Con qué reproductor se intentó, si se sabe.
    player: str = ""
    #: Código de la taxonomía del reproductor (H-02), o vacío si no se sabe.
    error_code: str = ""
    #: Si esperar o cambiar de motor serviría. ``None`` si no se sabe.
    recoverable: bool | None = None
    #: Duración medida en milisegundos. ``None`` si nadie midió.
    duration_ms: float | None = None

    def __post_init__(self) -> None:
        if self.duration_ms is None:
            return
        try:
            valor = float(self.duration_ms)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(
                f"duration_ms no es un número: {self.duration_ms!r}. "
                "Si nadie midió, se deja en None."
            ) from None
        if valor < 0:
            raise ValueError(
                "duration_ms no puede ser negativo: no se inventan mediciones."
            )
        object.__setattr__(self, "duration_ms", valor)

    def a_dict(self, version: int = VERSION_ACTUAL) -> dict[str, Any]:
        """Versión serializable.

        `version=1` devuelve **sólo** los cinco campos de la v1: es el camino
        de retroceso (H-03), y perder los campos nuevos a propósito es
        preferible a dejar un fichero que la v1 no sabe leer bien.

        En la v2, los campos que no se conocen **se omiten**, con la misma
        regla del §3.3 que ya usa :class:`~thetvview.diagnostics.events.Event`:
        un ``"recoverable": null`` en el disco parece un dato medido y es la
        ausencia de uno. Los cinco campos de la v1 conservan su forma —los
        escribía así y un lector viejo los espera así— aunque valgan ``""``.
        """
        if version < 2:
            return {campo: getattr(self, campo) for campo in _CAMPOS_V1}
        datos: dict[str, Any] = {
            campo: getattr(self, campo) for campo in _CAMPOS_V1
        }
        for campo in _CAMPOS_V2:
            valor = getattr(self, campo)
            if valor is None or valor == "":
                continue  # lo que no se sabe, no aparece
            datos[campo] = valor
        return datos

    @classmethod
    def de_dict(cls, datos: Any) -> RegistroHistorial | None:
        """Reconstruye un registro; devuelve None si el dato no sirve.

        Un registro ilegible se **salta**: el historial es una ayuda, no
        una base de datos, y una entrada rara no puede tirar el resto.

        Tolera las dos versiones: lo que no está se queda en su valor por
        defecto, que es «desconocido». Una entrada de la v1 no gana un
        ``source_id`` ni un ``error_code`` inventados al pasar por aquí.
        """
        if not isinstance(datos, dict):
            return None
        try:
            momento = float(datos.get("momento"))  # type: ignore[arg-type]
            canal = str(datos.get("canal", ""))
            exito = bool(datos.get("exito", False))
        except (TypeError, ValueError):
            return None
        duracion = datos.get("duration_ms")
        if duracion is not None:
            try:
                duracion = float(duracion)
            except (TypeError, ValueError):
                duracion = None
        # `recoverable` sólo se guarda si el código lo sostiene (H-02): un
        # booleano suelto permitiría que el historial dijera «reintentable»
        # sobre un 401, que es justo el consejo que hace esperar de más.
        causa = causa_de_codigo(datos.get("error_code"))
        return cls(
            momento=momento,
            canal=canal,
            exito=exito,
            motivo=str(datos.get("motivo", ""))[:_LIMITE_TEXTO],
            fase=str(datos.get("fase", ""))[:_LIMITE_TEXTO],
            source_id=source_id_opaco(datos.get("source_id")),
            player=str(datos.get("player", ""))[:_LIMITE_TEXTO],
            error_code=causa.reason_code,
            recoverable=causa.recoverable,
            duration_ms=duracion,
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
        source_id: str = "",
        player: str = "",
        error_code: str = "",
        recoverable: bool | None = None,
        duration_ms: float | None = None,
    ) -> RegistroHistorial:
        """Anota un resultado (redactado) y lo persiste. Devuelve el registro.

        Args:
            canal: clave del canal (tvg-id o nombre).
            exito: si el diagnóstico dio por bueno el canal.
            motivo: veredicto en castellano libre, si lo hay.
            fase: en qué fase se decidió.
            source_id: identificador **real** de la fuente. Se opaca aquí: lo
                que sale de este método ya es apto para disco.
            player: reproductor usado, si se sabe.
            error_code: código de la taxonomía del reproductor. Uno que no
                exista se descarta, no se guarda.
            recoverable: se **ignora** si no hay código conocido: la
                recuperabilidad la dice la política del reproductor, no quien
                escribe (H-02).
            duration_ms: duración medida. ``None`` si nadie midió; no se
                rellena con cero.

        Purgar y guardar nunca lanzan: un historial que no se puede
        escribir no puede impedir diagnosticar (se queda en memoria).
        """
        causa = causa_de_codigo(error_code)
        registro = RegistroHistorial(
            momento=self._ahora(),
            canal=redact_text(str(canal))[:_LIMITE_TEXTO],
            exito=bool(exito),
            motivo=redact_text(str(motivo))[:_LIMITE_TEXTO],
            fase=str(fase)[:_LIMITE_TEXTO],
            source_id=source_id_opaco(source_id),
            player=redact_text(str(player))[:_LIMITE_TEXTO],
            error_code=causa.reason_code,
            recoverable=causa.recoverable,
            duration_ms=None if duration_ms is None else max(0.0, float(duration_ms)),
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

    def escribir_como(self, version: int = 1) -> bool:
        """Reescribe el fichero en una versión anterior (H-03, reversible).

        Es el camino de retroceso: los datos que existían antes siguen ahí, y
        los que no existían se pierden a propósito. False si no se pudo
        escribir; nunca lanza.

        Sólo existe para versiones **anteriores**: escribir «como la v2» es lo
        normal y no necesita este método.
        """
        if version >= VERSION_ACTUAL:
            return False
        with self._lock:
            self._cargar()
            try:
                self._escribir(version=version)
            except OSError:
                return False
        return True

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

    def _texto_serializado(self, version: int = VERSION_ACTUAL) -> str:
        """JSON compacto que cabe en `max_bytes`; si no, caen las más viejas."""
        while True:
            datos = {
                "version": version,
                "entradas": [r.a_dict(version) for r in self._entradas],
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

    def _escribir(self, version: int = VERSION_ACTUAL) -> None:
        """Escritura atómica con 0600. Lanza OSError si el disco no deja."""
        texto = self._texto_serializado(version)
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
