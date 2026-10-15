"""Fase 8 · U-06 — Fuentes del canal: el vocabulario y el índice de candidatas.

La Fase 5A dejó el modelo (`LogicalChannel`, `SourceCandidate`, `Association`),
el detector (`sources.detection`) y el almacén (`sources.associations`). Nadie
los había conectado a la interfaz, así que la app no tenía forma de responder
a la pregunta más normal que surge con dos listas: **«¿de dónde saco este canal?
¿hay otra copia que funcione mejor?»**

Este módulo es el puente, y son tres cosas:

1. **El vocabulario en castellano** de niveles y señales, igual que `filters`
   (U-05) lo fue de los filtros y `rule_text` (U-04) el de las reglas. El
   detector habla `high`/`tvg-id`/`epg`; el usuario lee otra cosa.

2. :class:`IndiceFuentes`, que encuentra las candidatas de **un** canal sin
   pasar por el catálogo entero en cada tecla.

3. La decisión de **qué se le pasa al detector**: ninguna. Este módulo
   agrupa; quien decide si dos canales son el mismo es
   `sources.detection`, y si se duplicara esa política aquí habría dos reglas
   de fusión en el repo —justo lo que NFR-008 prohíbe.

Por qué hace falta un índice
---------------------------

Medido en este repositorio: recorrer 90 000 canales de dos listas y agruparlos
por identificador y por nombre cuesta **~1 s**. Eso no es «un poco lento» en un
modal: es un parpadeo de un segundo cada vez que se abre. De ahí que el índice
se construya **una vez, en segundo plano** (`App.pedir_indice_fuentes`) y se
guarde, y que sólo se recorra sobre las listas **ajenas** a la que se está
viendo: las candidatas de un canal nunca están en su propia lista.

Sobre la señal de EPG
---------------------

`detectar_candidatas` admite un `epg_index` que añade la señal `epg`. Aquí **no
se le pasa**. No es una omisión por simplicidad: la señal sólo se concede si
`ae in epg_ids and be in epg_ids and ae == be`, donde `ae`/`be` son el tvg-id (o
el nombre). O sea, **sólo cuando los identificadores ya coinciden**, que es
justo cuando ya se-ha-concedido la señal `tvg-id`… y `calcular_confianza` da
`high` en cuanto ve `tvg-id`. La señal `epg` no cambia ni una fila ni un nivel:
sólo hace la lista de razones más larga.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from thetvview.channel_identity import normalize_name
from thetvview.sources.detection import detectar_candidatas

__all__ = [
    "SENALES_CORTAS",
    "SENALES_LARGAS",
    "NIVELES",
    "MAX_CANDIDATAS_MOSTRADAS",
    "Candidata",
    "IndiceFuentes",
    "construir_indice",
    "deteccion_para",
    "nivel_legible",
    "senales_cortas",
    "senales_largas",
    "frase_de_senales",
    "enumerar",
    "resumen_de_candidatas",
]


#: Cuántas candidatas caben en el modal. El detector acepta de más: dos
#: listas pueden traer el mismo canal repetido por grupo, y 400 filas no son
#: una respuesta. Lo que no se muestra **se dice** (`resumen_de_candidatas`):
#: un recorte callado se lee como «no hay más».
MAX_CANDIDATAS_MOSTRADAS = 25


#: Nivel del detector -> lo que se lee. Sin «probabilidad»: A-06 lo prohíbe
#: («nunca probabilidad sin calibrar») y además el número no significaría nada.
NIVELES: dict[str, str] = {
    "high": "alta",
    "medium": "media",
    "low": "baja",
}

#: Señal -> la palabra corta que cabe en la nota de una fila. La nota se
#: recorta al ancho del modal, así que aquí no caben frases.
SENALES_CORTAS: dict[str, str] = {
    "tvg-id": "identificador",
    "name": "nombre",
    "group": "grupo",
    "language": "idioma",
    "region": "región",
    "epg": "guía",
}

#: Señal -> la frase entera, para donde hay sitio (el cuerpo del modal de
#: decisión). La corta es un resumen fiel de ésta, no otra afirmación.
SENALES_LARGAS: dict[str, str] = {
    "tvg-id": "el mismo identificador del proveedor (tvg-id)",
    "name": "el mismo nombre",
    "group": "el mismo grupo",
    "language": "el mismo idioma",
    "region": "la misma región",
    "epg": "los dos aparecen en la guía",
}


def nivel_legible(confianza: str) -> str:
    """`high` -> «alta». Una señal que no se conoce se muestra tal cual.

    No se inventa una traducción: si el detector algún día devuelve algo que
    este módulo no sabe leer, el usuario ve el código crudo y la app no
    finge entenderlo.
    """
    return NIVELES.get(str(confianza or "").strip().lower(),
                        str(confianza or "sin nivel").strip().lower())


def senales_cortas(senales: Iterable[str], maximo: int = 3) -> str:
    """Las señales en corto: «identificador, nombre y grupo».

    ``maximo`` recorta la lista, pero **lo dice**: con más de tres la nota
    pasa de una línea y el recorte del modal se comería el final, que es
    justo donde está la última señal. Recortar callado haría creer que sólo
    coincidían en dos cosas.
    """
    lista = list(senales)
    if len(lista) > maximo:
        cuantas = len(lista) - maximo
        cabeza = [SENALES_CORTAS.get(s, s) for s in lista[:maximo]]
        return enumerar(cabeza) + f" y {cuantas} más"
    return enumerar([SENALES_CORTAS.get(s, s) for s in lista])


def senales_largas(senales: Iterable[str]) -> str:
    """Las señales enteras: «el mismo identificador… y el mismo grupo»."""
    return enumerar([SENALES_LARGAS.get(s, s) for s in senales])


def enumerar(partes: list[str]) -> str:
    """«a», «a y b», «a, b y c». En castellano, sin comillas ni Oxford."""
    limpio = [str(p).strip() for p in partes if str(p).strip()]
    if not limpio:
        return ""
    if len(limpio) == 1:
        return limpio[0]
    return ", ".join(limpio[:-1]) + " y " + limpio[-1]


def frase_de_senales(senales: Iterable[str]) -> str:
    """Frase completa para el cuerpo de un modal."""
    largas = senales_largas(senales)
    return f"Coinciden: {largas}." if largas else "Sin señales que comparar."


def resumen_de_candidatas(total: int, mostradas: int) -> str:
    """Lo que el modal dice de las candidatas, con las cuentas comprobadas."""
    if total <= 0:
        return "sin candidatas"
    if mostradas >= total:
        return f"{total} candidata{'s' if total != 1 else ''}"
    plural = "s" if total != 1 else ""
    return (
        f"{mostradas} de {total} candidatas · "
        f"las otras {total - mostradas} no se muestran"
    )


@dataclass(frozen=True)
class Candidata:
    """Una fuente que el detector acepta como el mismo canal."""

    fuente: str
    canal: Any
    nivel: str
    senales: tuple[str, ...]

    @property
    def nota(self) -> str:
        """La nota corta de la fila: «alta · identificador y nombre»."""
        cortas = senales_cortas(self.senales)
        return f"{nivel_legible(self.nivel)} · {cortas}" if cortas \
            else nivel_legible(self.nivel)


class IndiceFuentes:
    """Canales de las listas de comparación, agrupados para no recorrerlos.

    Dos cubos por canal, y sólo dos:

    - ``tvg:<identificador>``: el identificador del proveedor, que es lo único
      que no rota solo.
    - ``nom:<nombre normalizado>``: el nombre con la misma normalización que
      usa el detector (`normalize_name`).

    **No hay cubo por grupo**, y es deliberado: un grupo puede tener cinco mil
    canales, y `detectar_candidatas` es O(n²) sobre lo que se le pase. Un par
    que sólo coincide en grupo no es el mismo canal a ojos de §9.2, así que
    buscarlo sería ofrece fusionar lo que el detector ya se niega a fusionar.
    """

    __slots__ = ("cubos", "listas", "canales", "_token")

    def __init__(
        self,
        cubos: dict[str, list[tuple[str, Any]]],
        listas: tuple[str, ...] = (),
        token: Any = None,
    ) -> None:
        self.cubos = cubos
        self.listas = listas
        #: Cuántos canales se han agrupado. Lo lleva `construir_indice`, que
        #: es el único sitio donde se sabe el total (un canal con nombre y sin
        #: tvg-id sólo entra en un cubo).
        self.canales = 0
        self._token = token

    def __len__(self) -> int:
        return len(self.cubos)

    def claves_de(self, canal) -> list[str]:  # noqa: ANN001 - Channel
        """Los dos cubos en los que puede aparecer `canal`."""
        claves: list[str] = []
        tvg = str(getattr(canal, "tvg_id", "") or "").strip()
        if tvg:
            claves.append("tvg:" + tvg.casefold())
        nombre = normalize_name(getattr(canal, "name", ""))
        if nombre:
            claves.append("nom:" + nombre)
        return claves

    def candidatas_de(self, canal) -> list[tuple[str, Any]]:  # noqa: ANN001
        """Canales de **otras** listas que podrían ser este mismo canal.

        Sin duplicados y en orden estable: un canal que coincide por
        identificador *y* por nombre estaba en los dos cubos, y ofrecerlo dos
        veces haría creer que hay dos fuentes donde hay una.
        """
        vistos: set[int] = {id(canal)}
        salida: list[tuple[str, Any]] = []
        for clave in self.claves_de(canal):
            for fuente, otro in self.cubos.get(clave, ()):
                if id(otro) in vistos:
                    continue
                vistos.add(id(otro))
                salida.append((fuente, otro))
        return salida


def construir_indice(
    listas: Iterable[tuple[str, Iterable[Any]]],
    *,
    token: Any = None,
    cancelar: Callable[[], bool] | None = None,
) -> IndiceFuentes:
    """Agrupa los canales de `listas` (nombre, canales) por identificador y nombre.

    Se comprueba la cancelación cada 2 000 canales: sin eso, un catálogo grande
    seguiría constructing hasta el final después de que el usuario haya
    cancelado, que es la mitad del motivo por el que se cancela.
    """
    cubos: dict[str, list[tuple[str, Any]]] = {}
    nombres: list[str] = []
    total = 0
    _normalize = normalize_name
    _str = str
    i = 0
    for nombre, canales in listas:
        nombre_str = _str(nombre)
        nombres.append(nombre_str)
        for canal in canales:
            if i % 2000 == 0:
                if callable(cancelar) and cancelar():
                    raise _Cancelado()
                _token_check = getattr(token, "check", None)
                if _token_check is not None and _token_check():
                    raise _Cancelado()
            total += 1
            i += 1
            tvg = getattr(canal, "tvg_id", "") or ""
            if tvg:
                tvg = _str(tvg).strip()
                if tvg:
                    clave = "tvg:" + tvg.casefold()
                    bucket = cubos.get(clave)
                    if bucket is None:
                        bucket = []
                        cubos[clave] = bucket
                    bucket.append((nombre_str, canal))
            clave_nombre = _normalize(getattr(canal, "name", ""))
            if clave_nombre:
                clave = "nom:" + clave_nombre
                bucket = cubos.get(clave)
                if bucket is None:
                    bucket = []
                    cubos[clave] = bucket
                bucket.append((nombre_str, canal))
    indice = IndiceFuentes(cubos, tuple(nombres))
    indice.canales = total
    return indice


class _Cancelado(Exception):
    """El índice se dejó a medias: no es un fallo, es una cancelación."""


def deteccion_para(canal, candidatas: list[tuple[str, Any]]) -> list[Candidata]:  # noqa: ANN001
    """Le pasa las candidatas al detector y devuelve lo que él acepta.

    El detector devuelve **todos los pares** del lote, así que entre las
    candidatas también salen parejas entre ellas (A-B, A-C, B-C…); aquí sólo
    interesan las del canal con el usuario. Sin ese filtro el modal enseñaría
    «esta lista tiene este canal» sin decir de qué lista.

    El emparejamiento es por **identidad de objeto** y no por `==`: `Channel`
    es un dataclass, así que dos canales con los mismos campos son iguales, y
    con `==` una candidata idéntica a otra se atribuiría a la primera lista que
    se encontrara: una información que no existe.
    """
    if not candidatas:
        return []
    canales = [canal] + [c for _, c in candidatas]
    fuente_de = {id(c): nombre for nombre, c in candidatas}
    try:
        deteccion = detectar_candidatas(canales)
    except Exception:
        # Un canal raro (un `attrs` que no es un dict, por ejemplo) no puede
        # dejar el modal sin abrir. Sin candidatas es una respuesta honesta.
        return []
    salida: list[Candidata] = []
    for d in deteccion:
        if d.left is not canal:
            continue
        salida.append(
            Candidata(
                fuente_de.get(id(d.right), ""),
                d.right,
                d.confidence,
                tuple(d.signals),
            )
        )
    # Orden estable y predecible: primero por lista, luego por nombre. El
    # detector no garantiza orden y el modal no puede cambiar de fila entre
    # dos aperturas del mismo canal.
    salida.sort(
        key=lambda c: (
            c.fuente,
            str(getattr(c.canal, "name", "")),
            str(getattr(c.canal, "url", "")),
        )
    )
    return salida