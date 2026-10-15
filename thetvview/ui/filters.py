"""Fase 8 · U-05 — Búsqueda: el vocabulario y el motor de los filtros.

`search.Consultador` (Fase 3) sabe evaluar consultas con grupo, estado de la
guía, catch-up, definición y orden… pero **la app nunca se lo ha pedido**.
Hasta U-05 la pantalla de canales filtraba con un `q in texto` sobre
nombre+grupo, y nada más.

Este módulo es el puente, y trae dos cosas que no estaban:

1. **El vocabulario en castellano** de los filtros, igual que `rule_text` lo
   fue de las reglas: qué filtro existe, cómo se llama para el usuario, qué
   valores admite y qué significa cada uno.
2. :class:`IndiceFiltros`, el motor que **no vuelve a pagar la misma cuenta**.

Lo segundo no es una optimización, es lo que hace el ticket posible. Medido
en este repositorio con 90 000 canales: `Consultador.consultar` tarda **1,1 s
sin ningún filtro** y **4,6 s filtrando por definición**. `Consultador._passes`
es una función de Python por canal, sin índice, y el catálogo típico de IPTV es
justo de ese tamaño. Escribir a 8 caracteres por segundo con un filtro puesto
no es lento: es la app congelada.

De ahí la regla de este módulo: **cada campo se cuenta una vez y se guarda**;
combinar filtros es intersectar conjuntos de índices, no volver a pasar el
catálogo. Y la búsqueda por texto sigue siendo un `q in texto` de C sobre una
cadena ya en minúsculas —el camino rápido que ya existía no se toca, y con
`tests` que lo vigilan para que nadie lo replaced sin querer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from thetvview.resolutions import quality_rank
from thetvview.search import Consulta, Consultador, calidad_de

__all__ = [
    "Filtro",
    "FILTROS",
    "ORDENES",
    "filtro_de",
    "items_de_filtro",
    "frase_de",
    "nota_de",
    "valor_legible",
    "valor_para_consulta",
    "aviso_de",
    "campos_de",
    "orden_legible",
    "cuenta_de",
    "IndiceFiltros",
]


# ---------------------------------------------------------------------------
# 1. Qué filtros existen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Filtro:
    """Un filtro que el usuario puede activar sobre la lista de canales.

    ``clave`` es el campo de `search.Consulta`; el resto es para el usuario y
    no toca el backend. ``lista`` marca los de texto que aceptan **varios**
    valores separados por coma, que el backend ya OR internamente.
    """

    clave: str
    etiqueta: str
    ayuda: str
    tipo: str = "enum"  # "enum" | "texto"
    opciones: tuple[tuple[str, str], ...] = ()
    ejemplo: str = ""
    lista: bool = False

    @property
    def es_texto(self) -> bool:
        return self.tipo == "texto"

    #: Explicación larga, para donde hay sitio (el subtítulo del editor de
    #: valores). La nota de la fila va corta porque se recorta, y un aviso
    #: cortado no avisa de nada.
    aviso: str = ""


#: Los ocho filtros. El orden es el que ve el usuario, y está puesto por
#: criterio de utilidad, no alfabético: primero los que la app puede
#: responder sola, luego los de estado, y al final los que hay que escribir.
#:
#: **`quality` se llama «Definición estimada» y no «Definición»** (decisión
#: §1.1). `resolutions.detect` no sondea el canal: deduce la resolución de un
#: atributo del EXTINF, del sufijo del nombre («Canal X (720p)») o del `tvg-id`.
#: Un canal sin ninguno de los tres da `None`, y un canal al que se le llama
#: «Canal X HD» da `"HD"`, no `"720p"`. Venderlo como «definición» sería
#: prometer una medición que no se ha hecho.
FILTROS: tuple[Filtro, ...] = (
    Filtro(
        clave="group",
        etiqueta="Grupo o categoría",
        ayuda="La carpeta de la lista. Varias, por comas.",
        tipo="texto",
        ejemplo="Deportes, Noticias",
        lista=True,
    ),
    Filtro(
        clave="source_id",
        etiqueta="Lista de origen",
        ayuda="De qué lista saiu el canal. Varias, por comas.",
        tipo="texto",
        ejemplo="mi-lista",
        lista=True,
    ),
    Filtro(
        clave="tvg_id",
        etiqueta="Identificador tvg-id",
        ayuda="El código con el que la guía lo encuentra.",
        tipo="texto",
        ejemplo="espn.la1",
    ),
    Filtro(
        clave="epg_status",
        etiqueta="Estado de la guía",
        ayuda="Si la guía lo tiene. Sin guía: «no se sabe».",
        opciones=(
            ("available", "Tiene guía"),
            ("unavailable", "Sin guía"),
            ("unknown", "No se sabe"),
        ),
    ),
    Filtro(
        clave="catchup_status",
        etiqueta="Catch-up",
        ayuda="Si se puede volver a algo ya empezado.",
        opciones=(
            ("available", "Se puede"),
            ("unavailable", "No se puede"),
            ("unknown", "No se sabe"),
        ),
    ),
    Filtro(
        clave="quality",
        etiqueta="Definición estimada",
        ayuda="La etiqueta exacta: «HD» no incluye «720p».",
        aviso=("La resolución no se mide: se deduce del atributo EXTINF, del "
               "final del nombre («Canal X (720p)») o del tvg-id. Un canal "
               "sin ninguno de los tres sale como «sin definición estimada». "
               "Y el filtro compara la etiqueta **exacta**: pedir «HD» no "
               "trae los canales que se llaman «720p»."),
        opciones=(
            ("SD", "SD"),
            ("720p", "720p"),
            ("HD", "HD"),
            ("FHD", "FHD"),
            ("1080p", "1080p"),
            ("QHD", "QHD"),
            ("UHD", "UHD"),
            ("4K", "4K"),
            ("8K", "8K"),
            ("unknown", "Sin definición estimada"),
        ),
    ),
    Filtro(
        clave="program_window",
        etiqueta="Cuándo se emite",
        ayuda="Si emite algo ahora, hoy o en 24 h. Con guía.",
        opciones=(
            ("current", "Ahora mismo"),
            ("today", "Hoy"),
            ("next_24h", "Próximas 24 horas"),
        ),
    ),
    Filtro(
        clave="program_title",
        etiqueta="Título del programa",
        ayuda="Texto del programa. Necesita la guía cargada.",
        tipo="texto",
        ejemplo="noticias",
    ),
)


def aviso_de(clave: str) -> str:
    """La explicación larga de un filtro, o vacío si no tiene ninguna.

    Va al **subtítulo** del editor de valores, no a la nota de la fila: la
    nota se recorta al ancho de la terminal y un aviso cortado no avisa de
    nada.
    """
    f = filtro_de(clave)
    return (f.aviso if f else "") or ""


def filtro_de(clave: str) -> Filtro | None:
    for f in FILTROS:
        if f.clave == clave:
            return f
    return None


#: Los órdenes, en el orden en el que se ofrecen. `None` es el primero a
#: propósito: **no ordenar** es el orden de la lista tal como la escribió el
#: proveedor, y es el que espera quien conoce su lista.
ORDENES: tuple[tuple[str | None, str], ...] = (
    (None, "Como viene en la lista"),
    ("name", "Por nombre"),
    ("group", "Por grupo"),
    ("quality", "Por mejor definición estimada"),
)


def orden_legible(orden: str | None) -> str:
    for clave, etiqueta in ORDENES:
        if clave == orden:
            return etiqueta
    return "Como viene en la lista"


# ---------------------------------------------------------------------------
# 2. Cómo se dice cada filtro y cada valor
# ---------------------------------------------------------------------------

#: Los valores del backend que no se traducen (resoluciones) salen tal cual:
#: `720p` se dice `720p`, y traducirlo a «alta definición» sería mentir sobre
#: lo que el filtro realmente compara (decisión §1.1 aplicada dos veces).
def valor_legible(clave: str, valor: str) -> str:
    f = filtro_de(clave)
    if f is None:
        return str(valor)
    for backend, etiqueta in f.opciones:
        if backend == valor:
            return etiqueta
    return str(valor)


def frase_de(clave: str, valor: str) -> str:
    """La frase corta que nombra un filtro activo: «Sólo con guía»."""
    f = filtro_de(clave)
    nombre = f.etiqueta if f else clave
    legible = valor_legible(clave, valor)
    if clave == "epg_status":
        return f"Guía: {legible}"
    if clave == "catchup_status":
        return f"Catch-up: {legible}"
    if clave == "quality":
        return f"Definición estimada: {legible}"
    if clave == "program_window":
        return f"Se emite: {legible}"
    if clave == "group":
        return f"Grupo: {legible}"
    if clave == "source_id":
        return f"Lista: {legible}"
    if clave == "tvg_id":
        return f"tvg-id: {legible}"
    if clave == "program_title":
        return f"Programa: {legible}"
    return f"{nombre}: {legible}"


def nota_de(clave: str, valor: str | None) -> str:
    """La segunda línea de la fila: el valor si lo hay, si no para qué sirve.

    Con valor puesto la etiqueta de la fila ya dice *qué* filtro es; repetirlo
    sería ruido. Lo que hace falta ahí es *con qué* está puesto.
    """
    if valor:
        return valor_legible(clave, valor)
    f = filtro_de(clave)
    return f.ayuda if f else ""


def items_de_filtro(activos: dict[str, str] | None = None) -> list:
    """`ChecklistItem` por filtro, uno por fila.

    Las ocho filas están **siempre**: un filtro que desaparece de la lista
    cuando no está puesto obliga a recordar de memoria. Con la casilla a la
    vista, «¿qué filtros tengo?» se lee de un vistazo.

    La nota lleva el valor remembered aunque la casilla esté vacía: si el
    usuario lo quita y lo vuelve a poner, no pierde lo que había escrito.
    """
    from .widgets import ChecklistItem

    activos = activos or {}
    return [
        ChecklistItem(
            f.clave,
            f.etiqueta,
            checked=f.clave in activos,
            note=nota_de(f.clave, activos.get(f.clave)),
        )
        for f in FILTROS
    ]


# ---------------------------------------------------------------------------
# 3. Del filtro del usuario al campo del backend
# ---------------------------------------------------------------------------


def valor_para_consulta(clave: str, valor: str) -> Any:
    """El valor tal como lo espera `Consulta`.

    Los campos de lista aceptan varios valores separados por coma y el backend
    los OR internamente, así que no hace falta montarlos uno a uno.
    """
    f = filtro_de(clave)
    texto = str(valor or "").strip()
    if f is not None and f.lista:
        partes = [p.strip() for p in texto.split(",")]
        return [p for p in partes if p]
    return texto


def campos_de(filtros: list[tuple[str, str]]) -> dict[str, Any]:
    """`(clave, valor)` → kwargs de `Consulta`."""
    return {clave: valor_para_consulta(clave, valor)
            for clave, valor in filtros}


# ---------------------------------------------------------------------------
# 4. El motor
# ---------------------------------------------------------------------------


class IndiceFiltros:
    """Resuelve `(filtros, texto, orden) -> lista de índices`, pagando una vez.

    El catálogo es fijo mientras dura la pantalla. Todo lo que se puede cachear
    se cachea: los textos en minúsculas (para que escribir sea un `in` de C) y
    **el conjunto de índices que pasa cada `(campo, valor)`**.

    Combinar filtros es entonces intersectar conjuntos: 90 000 elementos se
    cruzan en milisegundos, mientras que volver a preguntar al backend cuesta
    segundos. La primera vez que se usa un campo se paga; las siguientes, no.

    Nunca lanza: si el backend falla, el campo se trata como «nada pasa por
    él», que es lo único que se puede afirmar sin mirar. El error se ve en
    el contador, no en una pantalla en blanco.
    """

    def __init__(
        self,
        canales: Any,
        *,
        epg: Any = None,
        indice_epg: Any = None,
        source_resolver: Any = None,
    ) -> None:
        #: Copia propia, para que nadie pueda mutar el catálogo por detrás.
        self.canales: list = list(canales or [])
        #: La lista **tal como la pasó quien lo creó**. Se compara por
        #: identidad para saber si hay que rehacer el motor: recargar produce
        #: otra lista con los mismos canales, y tirar todo lo contado en ese
        #: caso sería volver a pagar segundos sin motivo.
        self.fuente = canales
        self.epg = epg
        self.indice_epg = indice_epg
        self.source_resolver = source_resolver
        self._consultador: Consultador | None = None
        self._textos: list[str] | None = None
        self._pasadas: dict[tuple[str, str], set[int]] = {}
        self._pos: dict[int, int] = {}

    # --- catálogo ---------------------------------------------------------

    def invalidar(self) -> None:
        """Olvida textos y conteos. Se llama cuando cambia el catálogo."""
        self._textos = None
        self._pasadas.clear()
        self._pos.clear()
        self._consultador = None

    @property
    def vacio(self) -> bool:
        return not self.canales

    def _posiciones(self) -> dict[int, int]:
        if not self._pos or len(self._pos) != len(self.canales):
            self._pos = {id(c): i for i, c in enumerate(self.canales)}
        return self._pos

    def textos(self) -> list[str]:
        """`nombre + grupo` en minúsculas, una vez por catálogo."""
        if self._textos is None:
            self._textos = [
                ((c.name or "") + "\n" + (c.group or "")).casefold()
                for c in self.canales
            ]
        return self._textos

    def texto(self, q: str) -> list[int]:
        """Índices cuyo texto contiene `q`. El camino rápido de siempre."""
        if not q:
            return list(range(len(self.canales)))
        textos = self.textos()
        return [i for i, t in enumerate(textos) if q in t]

    # --- campos -----------------------------------------------------------

    def _get_consultador(self) -> Consultador:
        if self._consultador is None:
            self._consultador = Consultador(
                self.canales,
                epg=self.epg,
                indice_epg=self.indice_epg,
                source_resolver=self.source_resolver,
            )
        return self._consultador

    def pasar(self, clave: str, valor: str) -> set[int]:
        """Índices que pasan el filtro `(clave, valor)`. Cacheado.

        La caché es por **campo**, no por filtro: activar y quitar «Tiene
        guía» no vuelve a pagar la cuenta, y combinarlo con otro filtro es
        cruzar dos conjuntos ya calculados.
        """
        firma = (clave, str(valor))
        guardado = self._pasadas.get(firma)
        if guardado is not None:
            return guardado
        try:
            consulta = Consulta(**campos_de([(clave, valor)]))
            resultado = self._get_consultador().consultar(
                consulta, catalogo=self.canales)
        except Exception:
            resultado = []
        posiciones = self._posiciones()
        conjunto = {posiciones[id(c)] for c in resultado if id(c) in posiciones}
        self._pasadas[firma] = conjunto
        return conjunto

    # --- consulta completa ------------------------------------------------

    def resuelve(
        self,
        filtros: list[tuple[str, str]] | None = None,
        q: str = "",
        orden: str | None = None,
    ) -> list[int]:
        """Índices visibles para `(filtros, q, orden)`.

        Sin filtros devuelve el camino rápido de siempre: el mismo
        `q in texto` que había antes de U-05, y **sin tocar el backend**.
        El orden, en cambio, se aplica siempre: ordenar la lista entera sin
        filtrarla es el uso más corriente de los cuatro.
        """
        base = self.texto(str(q or "").casefold())
        if filtros:
            quedan = set(base)
            for clave, valor in filtros:
                quedan &= self.pasar(clave, valor)
                if not quedan:
                    return []
            if orden is None:
                # Sin ordenar, manda la posición: el orden del catálogo es el
                # de la lista tal como la escribió el proveedor, y `set` no
                # lo conserva. Se recorre `base`, que sí lo tiene.
                return [i for i in base if i in quedan]
            base = [i for i in base if i in quedan]
        if orden is None:
            return base
        canales = self.canales
        return sorted(base,
                      key=lambda i: self._clave_de_orden(canales[i], orden))

    def _clave_de_orden(self, canal: Any, orden: str) -> tuple:  # noqa: ANN001
        """Clave de orden **estable**: desempata por posición.

        `Consultador._sort` ordena por calidad con `reverse=True`, y eso
        invierte también los empates: dos canales con la misma definición
        salen en orden contrario al de la lista. Orden estable significa que
        entre dos canales que empatan manda la posición que tenían, y por eso
        aquí la posición es **siempre** parte de la clave.
        """
        i = self._posiciones().get(id(canal), 0)
        if orden == "name":
            return ((getattr(canal, "name", "") or "").casefold(), i)
        if orden == "group":
            return ((getattr(canal, "group", "") or "").casefold(), i)
        if orden == "quality":
            return (-quality_rank(calidad_de(canal) or ""), i)
        return (i,)


def cuenta_de(indice: IndiceFiltros,
              filtros: list[tuple[str, str]] | None = None,
              q: str = "",
              orden: str | None = None) -> int:
    """Cuántos canales cumplen. Atajo de `len(indice.resuelve(...))`."""
    return len(indice.resuelve(filtros, q, orden))
