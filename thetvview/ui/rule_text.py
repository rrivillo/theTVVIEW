"""El vocabulario del constructor de reglas, en castellano (Fase 8 · U-04).

Una regla guardada es un diccionario con nombres en inglés (`field`,
`operator`, `value`) porque así la entiende `thetvview.collections.rules`. Este
módulo es la **única** traducción entre eso y lo que el usuario lee, y cumple
tres reglas que los tests de `test_fase8_colecciones_reglas.py` fija:

1. **Los operadores no son una lista propia.** `operadores_de()` lee
   `FIELD_TYPES` y `ALLOWED_OPERATORS` del backend. Si la interfaz tuviera su
   propia tabla, algún día dejaría de coincidir con la del backend y ofrecería
   combinaciones que `validar_regla` rechaza — y el usuario lo descubriría al
   final, cuando ya escribió toda la regla. Aquí no puede pasar: si el backend
   añade un operador, esta tabla sólo tiene que traducirlo (hay un test que lo
   exige) y el constructor lo ofrece solo.

2. **Nada se evalúa como código.** El valor del usuario es texto que se guarda
   como texto. No hay `eval`, ni `exec`, ni `compile`, ni `__import__` en este
   archivo, y hay un test que lo comprueba leyendo el fuente.

3. **Un error se explica con el motivo del backend.** Cuando algo no cuadra, el
   texto viene de `validar_regla` o del tipo que toca, no de una traducción
   nuestra que podría decir otra cosa.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from thetvview.collections.rules import ALLOWED_OPERATORS, FIELD_TYPES

__all__ = [
    "CampoInfo",
    "CAMPOS",
    "OPERADOR_ETIQUETA",
    "OPERADOR_AYUDA",
    "VALORES_CONOCIDOS",
    "operadores_de",
    "etiqueta_de_campo",
    "campos_sin_contexto",
    "CAMPOS_SIN_ORIGEN",
    "sin_origen_de",
    "valor_de",
    "ejemplo_de",
    "regla_desde",
    "frase_de",
    "resumen_de",
    "items_de_campo",
    "items_de_operador",
    "items_de_siguiente_paso",
]


@dataclass(frozen=True)
class CampoInfo:
    """Un campo de canal, con su nombre para el usuario y para qué sirve."""

    clave: str
    etiqueta: str
    ayuda: str


#: Los campos que el constructor ofrece, en el orden en que se leen.
#:
#: Se listan **sólo las claves canónicas** de `FIELD_TYPES`, no sus alias
#: (`nombre`, `grupo`, `estado_epg`…). El backend acepta los dos, pero
#: guardar siempre el mismo evita que la misma regla se lea de dos maneras
#: según quién la escribió.
CAMPOS: tuple[CampoInfo, ...] = (
    CampoInfo("name", "Nombre del canal",
              "El texto que ves en la lista."),
    CampoInfo("group", "Grupo o categoría",
              "La carpeta en la que viene."),
    CampoInfo("tvg_id", "Identificador tvg-id",
              "El código con el que la guía lo encuentra."),
    CampoInfo("source_id", "Lista de origen",
              "De qué lista saiu el canal."),
    CampoInfo("quality", "Calidad",
              "La resolución que parece tener. Es una estimación leída del "
              "nombre, no una medición."),
    CampoInfo("epg_status", "Estado de la guía",
              "Si este canal tiene guía (EPG)."),
    CampoInfo("catchup_status", "Estado de catch-up",
              "Si se puede volver a algo que ya empezó."),
    CampoInfo("last_played_at", "Última vez visto",
              "Cuándo lo viste por última vez."),
    CampoInfo("playback_health", "Salud al abrirse",
              "Si abre bien, si avisa o si no abre."),
    CampoInfo("recent_failure_count", "Fallos recientes",
              "Cuántas veces ha fallado al abrirse."),
    CampoInfo("favorites", "Favorito",
              "Si lo tienes marcado con ★."),
)

#: Cómo se lee cada operador. La clave es el identificador del backend; si
#: algún día aparece uno nuevo y no está aquí, el constructor se niega a
#: ofrecerlo en vez de ofrecerlo sin traducción.
OPERADOR_ETIQUETA: dict[str, str] = {
    "equals": "es igual a",
    "not_equals": "no es igual a",
    "contains": "contiene",
    "starts_with": "empieza por",
    "ends_with": "termina en",
    "in": "es uno de",
    "before": "es anterior a",
    "after": "es posterior a",
    "within_last": "fue en los últimos",
    "lt": "es menor que",
    "gt": "es mayor que",
    "le": "es menor o igual que",
    "ge": "es mayor o igual que",
}

#: Qué hace de verdad cada operador, en una línea.
#:
#: La etiqueta dice el verbo («es menor que») y esto dice **a qué se aplica**,
#: que es donde las reglas se escriben mal: «es menor que» dicho de una fecha
#: y dicho de un número de fallos no significan lo mismo. Sin esta nota las
#: cuatro filas de un campo de texto llevan al lado la misma explicación
#: inútil, que es ruido con forma de información.
OPERADOR_AYUDA: dict[str, str] = {
    # Estas dos se escriben sin mencionar el tipo a propósito: el mismo
    # operador sirve para texto, cifras, palabras y fechas, y una explicación
    # que valiera sólo para uno de ellos sería falsa en los otros tres.
    "equals": "vale exactamente eso",
    "not_equals": "vale cualquier otra cosa",
    "contains": "la palabra está dentro del texto",
    "starts_with": "el texto empieza por esa palabra",
    "ends_with": "el texto acaba en esa palabra",
    "in": "varios valores separados por comas",
    "before": "la fecha es más antigua que ese día",
    "after": "la fecha es más reciente que ese día",
    "within_last": "dentro de los últimos días que escribas",
    "lt": "la cifra es menor que ese número",
    "gt": "la cifra es mayor que ese número",
    "le": "la cifra es igual o menor",
    "ge": "la cifra es igual o mayor",
}

#: Valores que un campo enumerado puede valer de verdad.
#:
#: No es una lista cerrada: `quality` no la tiene porque depende del nombre de
#: cada canal. Está para poder sugerir en el constructor en vez de dejar que el
#: usuario adivine la palabra exacta, que es donde se cuelan los filtros que
#: no filtran nada.
VALORES_CONOCIDOS: dict[str, tuple[str, ...]] = {
    "epg_status": ("available", "unavailable", "unknown"),
    "catchup_status": ("available", "unavailable", "unknown"),
    "playback_health": ("online", "aviso", "offline", "desconocido"),
    "favorites": ("true", "false"),
}


def _etiqueta_de(campo: str) -> str:
    for info in CAMPOS:
        if info.clave == campo:
            return info.etiqueta
    return campo


def operadores_de(campo: str) -> list[str]:
    """Operadores válidos para `campo`, **ordenados**, según el backend.

    Un campo que el backend no conoce devuelve lista vacía: el constructor
    muestra el motivo y no arma una regla que el backend va a rechazar.

    Se descartan los que no tienen las dos traducciones. Una fila sin
    explicación no se ofrece: el rótulo del constructor es su contractual con
    el usuario, y ofrecer «gt» sin saber cómo se lee sería dejarle el
    trabajo de traducir.
    """
    tipo = FIELD_TYPES.get(campo)
    if tipo is None:
        return []
    permitidos = ALLOWED_OPERATORS.get(tipo, set())
    return sorted(op for op in permitidos
                  if op in OPERADOR_ETIQUETA and op in OPERADOR_AYUDA)


def valor_de(bruto: str, campo: str, operador: str) -> tuple[Any, str | None]:
    """Convierte lo que escribió el usuario al valor que guarda el backend.

    Devuelve ``(valor, None)`` si se pudo, o ``(None, motivo)`` si no. El
    motivo se enseña **en el propio modal**, antes de perder nada.

    El texto se devuelve tal cual: es un dato para comparar, nunca algo que
    ejecutar.
    """
    texto = (bruto or "").strip()
    if not texto:
        return None, ("Escribe un valor: sin él la regla no dice nada y la "
                      "colección se quedaría vacía.")
    tipo = FIELD_TYPES.get(campo)
    if tipo is None:
        return None, f"El campo «{campo}» no existe en esta versión."
    if operador not in ALLOWED_OPERATORS.get(tipo, set()):
        return None, (f"«{OPERADOR_ETIQUETA.get(operador, operador)}» no se "
                      f"puede usar con {_etiqueta_de(campo).lower()}.")

    if operador == "in":
        partes = [p.strip() for p in texto.split(",") if p.strip()]
        if not partes:
            return None, ("«es uno de» necesita una lista separada por comas, "
                          "por ejemplo: available, unknown.")
        if tipo == "number":
            numeros: list[int | float] = []
            for p in partes:
                n, error = _numero_de(p)
                if error:
                    return None, error
                numeros.append(n)
            return numeros, None
        return partes, None

    if tipo == "number":
        return _numero_de(texto)

    if tipo == "date":
        if operador == "within_last":
            return _numero_de(texto)
        return _fecha_de(texto)

    return texto, None


def _numero_de(texto: str) -> tuple[int | float | None, str | None]:
    try:
        return int(texto), None
    except ValueError:
        pass
    try:
        return float(texto), None
    except ValueError:
        return None, (f"«{texto}» no es un número. Escribe sólo cifras, "
                      "por ejemplo: 3")


def _fecha_de(texto: str) -> tuple[str | None, str | None]:
    """Acepta una fecha ISO o una marca de tiempo; rechaza el resto.

    El backend acepta cualquier cosa en `before`/`after` y simplemente no
    encuentra nada, así que «ayer» produciría una colección con cero canales y
    ninguna explicación. Rechazarlo aquí es lo que hace visible el error.
    """
    try:
        datetime.fromisoformat(texto.replace("Z", "+00:00"))
        return texto, None
    except (ValueError, AttributeError):
        pass
    try:
        float(texto)
        return texto, None
    except ValueError:
        return None, ("Una fecha se escribe como 2026-01-15 (o con hora "
                      "detrás: 2026-01-15 20:30).")


def ejemplo_de(campo: str, operador: str) -> str:
    """Un valor de ejemplo **válido**, para la ayuda del campo del valor.

    La ayuda que sugiere algo que el propio constructor rechaza empuja al
    usuario hacia un error que él no ha provocado; hay un test que pasa cada
    ejemplo por `valor_de`.
    """
    tipo = FIELD_TYPES.get(campo)
    if tipo == "date":
        if operador == "within_last":
            return "7"
        return "2026-01-15"
    if tipo == "number":
        return "1, 2, 3" if operador == "in" else "3"
    if tipo == "enum":
        conocidos = VALORES_CONOCIDOS.get(campo, ())
        primero = conocidos[0] if conocidos else "available"
        if operador == "in":
            return ", ".join(conocidos[:2]) if len(conocidos) >= 2 else \
                f"{primero}, desconocido"
        return primero
    if campo == "name":
        return "La 1"
    if campo == "group":
        return "Deportes"
    if campo == "tvg_id":
        return "canal.europa.1"
    if campo == "source_id":
        return "mi-lista"
    if campo == "quality":
        return "1080"
    return "texto"


def regla_desde(nodos: list[dict], modo: str, negar: bool) -> dict | None:
    """Monta el diccionario de regla que se guarda.

    Una sola condición se guarda como la hoja que es: envolverla en
    ``{"all": [hoja]}`` sería ruido que el backend tiene que traducir y que
    nadie pidió. Un modo que no sea ``any`` cae en ``all``, que es lo que el
    usuario quiere decir con «y».
    """
    if not nodos:
        return None
    if len(nodos) == 1:
        regla: dict = nodos[0]
    else:
        clave = "any" if modo == "any" else "all"
        regla = {clave: list(nodos)}
    if negar:
        # `{"not": [nodo]}` es inválido; la forma buena es `{"not": nodo}`.
        return {"not": regla}
    return regla


def _valor_legible(valor: Any) -> str:
    if isinstance(valor, list):
        return ", ".join(str(v) for v in valor)
    return str(valor)


def frase_de(nodo: Any) -> str:
    """La regla escrita como la leería una persona.

    No sustituye a la regla: la muestra. Si un nodo no se reconoce, se dice lo
    que hay en vez de callarse — un filtro vacío en pantalla parece un filtro
    que no hace nada.
    """
    if not isinstance(nodo, dict) or not nodo:
        return "(regla vacía)"
    for clave, conector in (("all", " y "), ("any", " o ")):
        if clave in nodo:
            hijos = nodo.get(clave) or []
            partes = [frase_de(h) for h in hijos if h]
            if not partes:
                return "(regla vacía)"
            return conector.join(partes)
    if "not" in nodo:
        # El «no» va al principio y no escondido en un «lo contrario»:
        # una colección que entra justo por lo contrario de lo que parece es
        # peor que una que no entra.
        return f"NO: {frase_de(nodo['not'])}"
    campo = nodo.get("field", nodo.get("campo"))
    operador = nodo.get("operator", nodo.get("operador"))
    valor = nodo.get("value", nodo.get("valor"))
    etiqueta = _etiqueta_de(campo) if isinstance(campo, str) else str(campo)
    verbo = OPERADOR_ETIQUETA.get(operador, str(operador))
    if operador == "within_last":
        return f"{etiqueta} {verbo} {_valor_legible(valor)} días"
    # Las comillas angulares son para el texto que se comparó literalmente.
    # Ponerlas alrededor de un 5 se lee como si el cinco fuera una palabra.
    if isinstance(valor, str):
        return f"{etiqueta} {verbo} «{valor}»"
    return f"{etiqueta} {verbo} {_valor_legible(valor)}"


def resumen_de(nodo: Any) -> str:
    """La regla como bloque, con una línea por condición si hay varias."""
    if isinstance(nodo, dict):
        for clave in ("all", "any"):
            hijos = nodo.get(clave)
            if isinstance(hijos, list):
                lineas = [frase_de(h) for h in hijos if h]
                if len(lineas) == 1:
                    return lineas[0]
                return "\n".join(f"{i}. {t}" for i, t in enumerate(lineas, 1))
    if isinstance(nodo, dict) and "not" in nodo:
        # Sin encabezado propio: quien llama pone el suyo («NO entra el canal
        # si:» o «Se incluirá el canal si:») y duplicarlo aquí lo salía
        # partido por la mitad en la vista previa.
        return resumen_de(nodo["not"])
    return frase_de(nodo)


# ---------------------------------------------------------------------------
# Las listas que ve el usuario
# ---------------------------------------------------------------------------


def items_de_campo() -> list:
    """`ChecklistItem` por campo.

    Los que la app no puede responder salen igual, **sin poder elegirse** y con
    el motivo en la nota. Escondidos parecían un fallo; marcados, son una
    respuesta.
    """
    from .widgets import ChecklistItem

    items = []
    for c in CAMPOS:
        motivo = sin_origen_de(c.clave)
        items.append(ChecklistItem(
            c.clave, c.etiqueta,
            note=motivo if motivo else c.ayuda,
            selectable=motivo is None,
        ))
    return items


def items_de_operador(campo: str) -> list:
    """`ChecklistItem` por operador **de ese campo**, ya filtrados."""
    from .widgets import ChecklistItem

    return [ChecklistItem(op, OPERADOR_ETIQUETA[op], note=OPERADOR_AYUDA[op])
            for op in operadores_de(campo)]


def etiqueta_de_campo(campo: str) -> str:
    """El nombre del campo para el usuario, o el identificador si no se conoce."""
    return _etiqueta_de(campo)


#: Campos que la app **no puede responder** hoy, con el motivo.
#:
#: `valor_campo` los lee de un `contexto` que le pasa quien evalúa: favoritos,
#: salud de reproducción, fallos, última vez visto, guía de programación.
#: Favoritos, última vez visto y guía tienen origen real en la app
#: (`FavoritesManager`, `RecentsManager`, `App.epg`) y `App.contexto_de_reglas`
#: los monta, así que **no** van aquí. Los otros dos no: nada desde la UI
#: escribe el historial de fallos, y la salud se sondea por pantalla y se
#: apaga al salir — no queda nada consultable.
#:
#: Aparecen en el constructor **con su motivo al lado y sin poder elegirse**.
#: Es la misma regla que fijó U-03b: lo que no se puede tocar y además no
#: aparece parece un fallo de la app.
CAMPOS_SIN_ORIGEN: dict[str, str] = {
    "recent_failure_count": "la app no guarda el historial de fallos",
    "playback_health": "la salud no queda guardada tras abrir un canal",
}


def sin_origen_de(campo: str) -> str | None:
    """Por qué `campo` no se puede usar; ``None`` si sí se puede."""
    return CAMPOS_SIN_ORIGEN.get(campo)


def campos_sin_contexto(regla: Any) -> list[str]:
    """Etiquetas de los campos de `regla` que hoy **no** se pueden contar.

    Sirve para no enseñar un «0 canales» que en realidad significa «no se ha
    mirado». Un filtro que promete una colección vacía cuando lo único que
    pasa es que le falta un dato es peor que un filtro que no promete nada.
    """
    faltan: list[str] = []

    def _revisar(nodo: Any) -> None:
        if not isinstance(nodo, dict):
            return
        for clave in ("all", "any"):
            for hijo in nodo.get(clave) or []:
                _revisar(hijo)
        if "not" in nodo:
            _revisar(nodo["not"])
        campo = nodo.get("field", nodo.get("campo"))
        if isinstance(campo, str) and sin_origen_de(campo):
            etiqueta = _etiqueta_de(campo)
            if etiqueta not in faltan:
                faltan.append(etiqueta)

    _revisar(regla)
    return faltan


def items_de_siguiente_paso() -> list:
    """Qué se puede hacer con la condición que se acaba de escribir.

    «y» y «o» se ofrecen también con una sola condición: el rótulo dice
    «añadir otra» y el modo elegido se aplica a la segunda. Ocultarlos hasta
    tener dos obligaría a un paso de más —terminar y volver a empezar— sólo
    para poder elegir cómo se unen.
    """
    from .widgets import ChecklistItem

    return [
        ChecklistItem("all", "Añadir otra y exigir TODAS (y)"),
        ChecklistItem("any", "Añadir otra y basta con UNA (o)"),
        ChecklistItem("fin", "Terminar aquí y guardar"),
        ChecklistItem("not", "Terminar aquí y negar el resultado",
                      note="lo que quede NO entra"),
    ]
