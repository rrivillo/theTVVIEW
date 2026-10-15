"""Fase 8 · U-06 — Asociación manual de fuentes: el almacén persistente.

A-03 del plan («Una candidata rechazada se recuerda y no se vuelve a proponer
sin cambios relevantes o acción explícita») y el SDD lo pide como A-04
«deshacer/rechazo **persistente**». El modelo de Fase 5A (`LogicalChannel`) ya
tenía `Association` y `rejected_candidate_ids`, pero **nadie los guardaba**: son
objetos en memoria que mueren con el proceso. Sin almacén, «descartar esta
fuente» sería un gesto que dura hasta que se cierra la app.

Dos decisiones que este módulo fija y que son la diferencia entre que funcione
y que parezca funcionar:

1. **Lo que se guarda son claves, no ``candidate_id``**. ``SourceCandidate``
   genera su ``candidate_id`` con ``uuid4``, así que es distinto en cada
   ejecución: un rechazo guardado por ese identificador no se reconocería
   nunca al reiniciar. Las claves de aquí se derivan del contenido estable
   (identidad lógica del canal + lista de la que viene), y por eso sobreviven.

2. **La clave lógica incluye la lista de origen**. ``ChannelIdentity.clave``
   incluye el ``source_id`` a propósito —para que dos proveedores con el mismo
   ``tvg-id`` no se confundan— y esta la respeta. La consecuencia se dice en la
   interfaz: la asociación vale para el canal *tal y como se ve en esa lista*.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from thetvview.channel_identity import channel_identity, normalize_name, normalize_url
from thetvview.storage import json_store
from thetvview.storage.schemas import SCHEMA_VERSION, validate_sources

__all__ = [
    "AsociacionesError",
    "Asociacion",
    "AssociationStore",
    "MAX_RECHAZOS_POR_CANAL",
    "clave_logica",
    "clave_candidata",
]


#: Rechazos que se guardan por canal. El archivo no puede crecer sin límite:
#: un usuario que va descartando fuentes durante meses dejaría una lista
#: enorme de hashes que ya no significan nada (su lista cambió, su canal
# quizá ni existe). Se conservan los **más recientes**, que son los que el
# usuario todavía puede reconocer.
MAX_RECHAZOS_POR_CANAL = 50


class AsociacionesError(Exception):
    """La asociación no se pudo guardar o no se pudo leer."""


def _digest(texto: str) -> str:
    return hashlib.sha256(str(texto).encode("utf-8")).hexdigest()[:32]


def _identidad_simple(canal) -> str:  # noqa: ANN001 - Channel
    """Identidad del canal **sin** la lista de la que viene.

    Sirve para nombrar una candidata *dentro* de su propia lista: dos canales
    de la misma lista con el mismo ``tvg-id`` son el mismo canal, y el mismo
    canal con el token girado sigue siendo el mismo canal. Por eso aquí no se
    usa la URL: ``normalize_url`` conserva la query entera (ahí viven los
    tokens) y un token que gira borraría el rechazo solo.
    """
    ident = channel_identity(canal, "")
    if ident.clave:
        return ident.clave
    # Canal sin tvg-id, sin URL y sin nombre: no hay nada estable. Se cae al
    # nombre y, si tampoco hay, no hay clave y el llamante lo sabe.
    nombre = normalize_name(getattr(canal, "name", ""))
    return nombre or ""


def clave_logica(canal, source_id: str = "") -> str:  # noqa: ANN001 - Channel
    """Clave del canal lógico, tal y como lo ve la lista `source_id`.

    Es la clave de ``ChannelIdentity`` con su procedencia incluida, que es
    justo lo que ``channel_identity`` justifica: el mismo ``tvg-id`` en dos
    proveedores puede ser un canal distinto, y atarlos sería mover los
    favoritos de uno al otro sin que nadie lo pidiera (§9.2).

    Un canal sin nada con qué identificarse devuelve `""`, y el llamante
    decide: inventar una clave sería guardar una asociación que no vuelve a
    salir.
    """
    ident = channel_identity(canal, source_id)
    if ident.clave:
        return ident.clave
    nombre = normalize_name(getattr(canal, "name", ""))
    url = normalize_url(getattr(canal, "url", ""))
    if not nombre and not url:
        return ""
    return _digest(f"{source_id}|{nombre}|{url}")


def clave_candidata(fuente: str, canal) -> str:  # noqa: ANN001 - Channel
    """Clave estable de una candidata concreta de una lista concreta.

    ``fuente`` es el nombre de la playlist (lo que el usuario ve en el modal)
    y el canal aporta su identidad sin lista. Sin esto, el rechazo de una
    fuente duraría lo que dura el proceso.
    """
    simple = _identidad_simple(canal)
    if not simple:
        return ""
    return _digest(f"{fuente}|{simple}")


class Asociacion:
    """Qué fuente quedó elegida para un canal, y cuándo."""

    __slots__ = ("clave_logica", "fuente", "candidata", "fecha")

    def __init__(self, clave_logica: str, fuente: str, candidata: str, fecha: str = "") -> None:
        self.clave_logica = clave_logica
        self.fuente = fuente
        self.candidata = candidata
        self.fecha = fecha

    def __repr__(self) -> str:  # pragma: no cover - comodidad de depuración
        return (
            f"Asociacion(clave_logica={self.clave_logica!r}, fuente={self.fuente!r})"
        )

    def para_guardar(self) -> dict[str, str]:
        return {
            "fuente": self.fuente,
            "candidata": self.candidata,
            "fecha": self.fecha,
        }


class AssociationStore:
    """Asociación preferida y descartes, por canal lógico (A-03).

    Es un almacén pequeño y **tolerante a fallos**: un archivo corrupto no
    impide abrir la app, se pierde lo guardado (que se puede volver a
    escribir) y no se pisa el archivo original, que queda a salvo en la
    carpeta de recuperaciones de ``json_store``. Perder un descarte es un
    annoyance; no poder arrancar la app es un destrozo.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._asociaciones: dict[str, Asociacion] | None = None
        self._rechazos: dict[str, list[str]] | None = None

    # ----- lectura -----------------------------------------------------------

    def _cargar(self) -> None:
        if self._asociaciones is not None:
            return
        datos = self._leer()
        asociaciones: dict[str, Asociacion] = {}
        for clave, bruto in (datos.get("asociaciones") or {}).items():
            if not isinstance(clave, str) or not isinstance(bruto, dict):
                continue
            fuente = str(bruto.get("fuente") or "").strip()
            if not fuente:
                continue
            asociaciones[clave] = Asociacion(
                clave,
                fuente,
                str(bruto.get("candidata") or "").strip(),
                str(bruto.get("fecha") or "").strip(),
            )
        rechazos: dict[str, list[str]] = {}
        for clave, valores in (datos.get("rechazos") or {}).items():
            if not isinstance(clave, str) or not isinstance(valores, list):
                continue
            utiles = [str(v) for v in valores if isinstance(v, str) and v.strip()]
            if utiles:
                rechazos[clave] = self._acotar(utiles)
        self._asociaciones = asociaciones
        self._rechazos = rechazos

    def _leer(self) -> dict[str, Any]:
        # Sin `validate=` a propósito: esta lectura **tolera** y descarta lo
        # que no entiende, entrada a entrada (ver `_cargar`). Pasar el
        # validador aquí convertiría un archivo con una asociación rara en
        # «no se pudo leer», y entonces no se podría ni guardar el siguiente
        # descarte sin borrar el archivo a mano.
        try:
            datos = json_store.read_json_with_recovery(self.path, schema_version=None)
        except Exception:
            datos = None
        if isinstance(datos, dict):
            return datos
        # Un archivo que no es un objeto (por ejemplo una lista de una versión
        # futura) no se borra: se ignora lo que no se entiende y se puede
        # escribir encima cuando el usuario decida algo.
        return {}

    @staticmethod
    def _acotar(valores: list[str]) -> list[str]:
        """Deja los últimos ``MAX_RECHAZOS_POR_CANAL`` valores."""
        return list(valores[-MAX_RECHAZOS_POR_CANAL:])

    def _datos(self) -> dict[str, Any]:
        self._cargar()
        assert self._asociaciones is not None and self._rechazos is not None
        return {
            "asociaciones": {
                clave: a.para_guardar() for clave, a in self._asociaciones.items()
            },
            "rechazos": {clave: list(v) for clave, v in self._rechazos.items()},
        }

    # ----- consulta ----------------------------------------------------------

    def asociacion_de(self, clave: str) -> Asociacion | None:
        self._cargar()
        assert self._asociaciones is not None
        return self._asociaciones.get(clave)

    def fuente_preferida(self, clave: str) -> str:
        """Nombre de la lista preferida para `clave`, o `""` si no hay ninguna."""
        asoc = self.asociacion_de(clave)
        return asoc.fuente if asoc else ""

    def hay_preferencia(self, clave: str) -> bool:
        return self.asociacion_de(clave) is not None

    def rechazadas(self, clave: str) -> set[str]:
        self._cargar()
        assert self._rechazos is not None
        return set(self._rechazos.get(clave, ()))

    def esta_rechazada(self, clave_logica: str, clave_cand: str) -> bool:
        return clave_cand in self.rechazadas(clave_logica)

    # ----- escritura ---------------------------------------------------------

    def _guardar(self) -> None:
        datos = self._datos()
        try:
            json_store.make_backup(self.path)
            json_store.atomic_write_json(
                self.path,
                datos,
                schema_version=SCHEMA_VERSION,
                validate=validate_sources,
            )
        except Exception as exc:
            raise AsociacionesError(
                "No se pudo guardar la preferencia de fuente."
            ) from exc

    def asociar(self, clave: str, candidata: str, fuente: str) -> Asociacion:
        """Deja `fuente` como la fuente preferida del canal `clave`.

        **Asociar nunca borra nada**: la entrada original sigue en su lista, y
        los descartes que hubiera tampoco se tocan (son una decisión distinta
        sobre una candidata distinta).
        """
        if not clave:
            raise AsociacionesError("Ese canal no tiene identidad con la que asociarlo.")
        if not fuente:
            raise AsociacionesError("Esa fuente no tiene nombre.")
        self._cargar()
        assert self._asociaciones is not None
        asoc = Asociacion(
            clave, fuente, candidata, datetime.now(timezone.utc).isoformat()
        )
        self._asociaciones[clave] = asoc
        self._guardar()
        return asoc

    def rechazar(self, clave: str, candidata: str) -> None:
        """Recuerda que `candidata` no se vuelve a proponer (A-03)."""
        if not clave or not candidata:
            return
        self._cargar()
        assert self._rechazos is not None
        actuales = [c for c in self._rechazos.get(clave, []) if c != candidata]
        actuales.append(candidata)
        self._rechazos[clave] = self._acotar(actuales)
        self._guardar()

    def olvidar(self, clave: str) -> bool:
        """Deshace la asociación: el canal vuelve a no tener fuente preferida.

        **No deshace los descartes.** Son gestos distintos y al revés de lo que
        sería útil: quien descartó «Lista B» porque se caía no quiere que un
        «deshacer» de otra cosa le devuelva esa Candidata a la lista. La
        interfaz lo dice en el modal.
        """
        self._cargar()
        assert self._asociaciones is not None
        if clave not in self._asociaciones:
            return False
        del self._asociaciones[clave]
        self._guardar()
        return True

    def olvidar_todo(self) -> None:
        """Olvida toda asociación guardada (los descartes también).

        No lo llama la interfaz: existe para las pruebas y para quien quiera
        empezar de cero. Es el único camino que borra descartes, y por eso
        tiene que ser explícito.
        """
        self._cargar()
        assert self._asociaciones is not None and self._rechazos is not None
        self._asociaciones.clear()
        self._rechazos.clear()
        self._guardar()

    # English-ish aliases, for parity with the rest of `sources/`.
    get = asociacion_de
    preferred = fuente_preferida
    rejected = rechazadas
    associate = asociar
    reject = rechazar
    undo = olvidar


class SourcesStore(AssociationStore):
    """Alias con el nombre que usa el resto del paquete."""

    @classmethod
    def get_default(cls) -> "SourcesStore":
        from thetvview import config

        return cls(config.SOURCES_JSON)