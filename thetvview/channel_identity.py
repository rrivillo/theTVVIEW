"""Identidad lógica de un canal y reconciliación entre versiones (SDD §9, P1.6).

El problema
-----------

:mod:`thetvview.favorites` dice, literalmente en su primer párrafo, que «la
identidad de un favorito es su `url`». Eso convierte cada giro de token del
proveedor, cada cambio de `get.php` y cada renumeración de un stream en la
pérdida silenciosa de los favoritos del usuario: la línea deja de estar en la
lista y el favorito queda apuntando a algo que ya no existe, sin aviso.

La identidad correcta no es la URL: es el `tvg-id`, que es **lo que dice el
proveedor que es ese canal**. La URL es la identidad técnica (cómo abrirlo) y
el nombre visible es la etiqueta. Mezclarlas es lo que produce el problema.

Tres identidades, no una
------------------------

============================  ==========================================
identidad                     para qué sirve
============================  ==========================================
origen (:attr:`source_id`)    qué lista o cuenta define el canal
técnica (URL)                 cómo abrirlo, cambia sin que cambie el canal
lógica (:class:`ChannelIdentity`)  a qué se atan favoritos y preferencias
============================  ==========================================

Precedencia y por qué
---------------------

1. ``tvg-id`` — confianza alta. Es del proveedor y no depende de cómo se sirva.
2. URL normalizada — confianza media. Cambia cuando el proveedor rota un token,
   así que no sirve como identidad, pero **sí** como llave de salida mientras no
   haya `tvg-id`.
3. Nombre + grupo — confianza baja. Sirve para *encontrar* candidatos, nunca
   para fusionar.

Empate: no se fusiona
---------------------

Si dos canales comparten nombre y grupo y no tienen `tvg-id`, son **canales
distintos** a ojos de esta app. Fusionarlos sería mover los favoritos de uno al
otro sin que nadie lo pidiera, y §9.2 lo prohíbe. Se mantienen separados y la
ambigüedad queda **registrada** (:attr:`ReconcileReport.ambiguous`), que es lo que
permite al usuario decidir después.

Relación con :func:`thetvview.tracks.prefs.channel_key`
--------------------------------------------------------

Ya existe una ``channel_key`` en el módulo de preferencias de pistas, y **no se
toca ni se sustituye**: aquella es el *ámbito* de unas preferencias (un hash
corto, sin material del canal, pensado para un `prefs.json`) y ésta es la
*identidad lógica* del canal, con procedencia y confianza. Son preguntas
distintas y confundirlas sería la forma rápida de tener dos políticas
distintas de identidad en el mismo repo (NFR-008).

Puro: sin red, sin disco, sin curses.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit, urlunsplit

from .security.redaction import redact_text

__all__ = [
    "IdentityKind",
    "Confidence",
    "ChannelIdentity",
    "ChangeKind",
    "ChannelChange",
    "ReconcileReport",
    "channel_identity",
    "identity_key",
    "normalize_url",
    "normalize_name",
    "reconcile",
    "resumen_para_ui",
]


class IdentityKind(str, Enum):
    """De dónde sale la identidad y cuánto se puede confiar en ella."""

    #: El identificador del proveedor. Es lo único que no rota solo.
    TVG_ID = "tvg-id"
    #: La dirección, normalizada. Cambia con los tokens.
    URL = "url"
    #: Nombre + grupo. Sirve para buscar, no para fusionar.
    NAME = "name"

    def __str__(self) -> str:  # pragma: no cover - comodidad de depuración
        return self.value


class Confidence(str, Enum):
    """Nivel de confianza de una identidad, en orden descendente."""

    ALTA = "alta"
    MEDIA = "media"
    BAJA = "baja"
    NINGUNA = "ninguna"

    @property
    def valor(self) -> float:
        """0..1. Sirve para ordenar y comparar, no para decidir solo."""
        return {
            Confidence.ALTA: 1.0,
            Confidence.MEDIA: 0.6,
            Confidence.BAJA: 0.3,
            Confidence.NINGUNA: 0.0,
        }[self]

    def __str__(self) -> str:  # pragma: no cover
        return self.value


#: Qué confianza corresponde a cada procedencia. Vive aquí para que añadir una
#: procedencia y clasificarla sea el mismo gesto.
_CONFIANZA_POR_TIPO: dict[IdentityKind, Confidence] = {
    IdentityKind.TVG_ID: Confidence.ALTA,
    IdentityKind.URL: Confidence.MEDIA,
    IdentityKind.NAME: Confidence.BAJA,
}


# ---------------------------------------------------------------------------
# Normalizaciones (conservadoras a propósito)
# ---------------------------------------------------------------------------


def normalize_name(nombre: str | None) -> str:
    """Clave de comparación de un nombre visible: sólo minúsculas y espacios.

    **No** quita acentos, números, región, idioma ni calidad. `Canal 1 HD` y
    `Canal 1 SD` siguen siendo dos nombres distintos, y `CNN` no se convierte en
    `CNN International`: son canales distintos (§9.2, FR-CHAN-004). Quitar
    sufijos «parece» una mejora y en la práctica traslada los favoritos de un
    canal a otro sin que nadie lo pidiera.

    ``str.split()`` sin argumentos recorta los extremos y colapsa los espacios
    intermedios (incluidos los no ASCII), así que no hace falta mantener una
    lista de «caracteres de espacio» al día.
    """
    if not nombre:
        return ""
    return " ".join(str(nombre).casefold().split())


#: Puertos que no aporta nada en la identidad: `http://x:80/a` y `http://x/a`
#: son el mismo recurso. El resto **se conserva**: un puerto distinto puede ser
#: otro servidor con otro canal.
_PUERTOS_POR_DEFECTO = {"http": "80", "https": "443"}


def normalize_url(url: str | None) -> str:
    """URL normalizada para comparar dos listas. No la usa como identidad sola.

    Lo que cambia y se normaliza (todo lo que no cambia el recurso):

    - esquema y host en minúsculas;
    - puerto por defecto eliminado (``http://x:80`` = ``http://x``);
    - ``#fragmento`` eliminado (no viaja al servidor);
    - barra final del camino eliminada (``/a/`` = ``/a``).

    Lo que **no** se toca, y es deliberado:

    - la query **entera y en su orden**: es donde viven los tokens, y
      reordenarlos puede cambiar lo que el servidor devuelve;
    - mayúsculas del camino: en HTTP son significativas y quitarlas fundiría dos
      recursos que el servidor distingue;
    - las credenciales: se redactan con
      :func:`~thetvview.security.redaction.redact_text` **antes** de comparar,
      porque comparar el userinfo real no aportaría nada y sí dejaría un
      secreto en memoria.
    """
    texto = redact_text(str(url or "").strip())
    if not texto:
        return ""
    if "://" not in texto:
        # Referencias opacas (xtream://, ipcam://…) no son URLs que comparar;
        # se comparan tal cual, sólo con espacios recortados.
        return texto
    try:
        partes = urlsplit(texto)
    except ValueError:
        return texto
    host = (partes.hostname or "").lower()
    if not host:
        return texto
    puerto = ""
    try:
        if partes.port and str(partes.port) != _PUERTOS_POR_DEFECTO.get(
            partes.scheme.lower(), ""
        ):
            puerto = f":{partes.port}"
    except ValueError:  # puerto inválido: se deja la URL como estaba
        return texto
    if partes.username:
        # Red de seguridad: si `redact_text` no lo quitó (credencial con una
        # forma que el redactor no reconoce), aquí se descarta igual.
        host = f"{REDACTED}@{host}"
    camino = partes.path or ""
    if len(camino) > 1 and camino.endswith("/"):
        camino = camino.rstrip("/") or "/"
    # `urlunsplit` pone el `?` y el `#` ella misma: pasar la query con `?`
    # puesto produciría `??` en la clave de comparación.
    return urlunsplit(
        (partes.scheme.lower(), f"{host}{puerto}", camino, partes.query, "")
    )


def _digest(texto: str) -> str:
    """Huella corta y estable de un valor largo.

    Los nombres pueden ser largos y con acentos: la clave de identidad debe ser
    comparable y no arrastrar el texto completo al informe.
    """
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:32]


REDACTED: str = "***"


# ---------------------------------------------------------------------------
# Identidad
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelIdentity:
    """La identidad lógica de un canal: qué lo hace ser *ese* canal.

    Es un **valor**: dos identidades con la misma :attr:`clave` son el mismo canal
    a efectos de favoritos y preferencias, y con distinta clave son canales
    distintos aunque se llamen igual.
    """

    #: De qué lista o cuenta viene (``PlaylistEntry.source_id``, o "" si no se
    #: sabe). Entra en la clave para que dos proveedores con el mismo `tvg-id`
    #: no se confundan.
    source_id: str = ""
    #: De dónde sale la identidad.
    kind: IdentityKind = IdentityKind.NAME
    #: El valor, ya normalizado.
    value: str = ""
    #: Confianza de esa procedencia. Se deduce de :attr:`kind`.
    confidence: Confidence = Confidence.BAJA

    @classmethod
    def crea(cls, source_id: str, kind: IdentityKind, value: str) -> "ChannelIdentity":
        """Construye la identidad con su confianza derivada del tipo."""
        return cls(
            source_id=str(source_id or ""),
            kind=kind,
            value=str(value or ""),
            confidence=_CONFIANZA_POR_TIPO[kind],
        )

    @property
    def clave(self) -> str:
        """Clave comparable. Vacia si no hay valor (identidad no resoluble)."""
        if not self.value:
            return ""
        base = f"{self.source_id}|{self.kind.value}|{self.value}"
        # El `tvg-id` y la URL normalizada ya son estables; el nombre se
        # resume porque puede ser largo y con acentos.
        if self.kind is IdentityKind.NAME:
            base = f"{self.source_id}|{self.kind.value}|{_digest(self.value)}"
        return base

    @property
    def resoluble(self) -> bool:
        """False si no hay ningún dato con el que identificar el canal."""
        return bool(self.value)

    @property
    def fuerte(self) -> bool:
        """True si la confianza alcanza para atar favoritos sin dudarlo."""
        return self.confidence is Confidence.ALTA

    def para_guardar(self) -> dict[str, str]:
        """Forma que va en ``favorites.json`` junto al canal.

        Deliberadamente **mínima**: tipo y valor. La procedencia y la confianza
        se deducen del tipo y no hace falta guardarlas dos veces.
        """
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def desde_guardado(cls, raw: object, source_id: str = "") -> "ChannelIdentity | None":
        """Lee la identidad de una entrada de ``favorites.json``.

        Devuelve ``None`` si no la hay o si está corrupta: un campo desconocido
        o mal escrito **no** puede impedir cargar los favoritos (H11).
        """
        if not isinstance(raw, dict):
            return None
        tipo = str(raw.get("kind") or "").strip().lower()
        valor = str(raw.get("value") or "").strip()
        if not tipo or not valor:
            return None
        try:
            kind = IdentityKind(tipo)
        except ValueError:
            return None
        return cls(str(source_id or ""), kind, valor)

    def para_informe(self) -> dict[str, str]:
        """Datos para el informe: tipo, confianza y valor (ya sin secretos)."""
        return {
            "tipo": self.kind.value,
            "confianza": self.confidence.value,
            "valor": redact_text(self.value),
        }


def channel_identity(canal, source_id: str = "") -> ChannelIdentity:  # noqa: ANN001 - Channel
    """La identidad de `canal`, por orden de preferencia de fiabilidad.

    1. ``tvg-id`` → confianza **alta**. Es del proveedor y no rota solo.
    2. URL normalizada → confianza **media**. Es cómo se abre hoy, así que sirve
       para encontrar un favorito aunque no sea lo bastante firme para fusionar.
    3. nombre + grupo → confianza **baja**. Sirve para *buscar* candidatos.

    Un canal sin `tvg-id`, sin URL y sin nombre no tiene identidad: se devuelve
    una vacía, y el llamante decide qué hacer en vez de inventar una.
    """
    tvg_id = str(getattr(canal, "tvg_id", "") or "").strip()
    if tvg_id:
        return ChannelIdentity.crea(source_id, IdentityKind.TVG_ID, tvg_id)
    url = normalize_url(getattr(canal, "url", ""))
    if url:
        return ChannelIdentity.crea(source_id, IdentityKind.URL, url)
    nombre = normalize_name(getattr(canal, "name", ""))
    grupo = normalize_name(getattr(canal, "group", ""))
    if nombre:
        return ChannelIdentity.crea(
            source_id,
            IdentityKind.NAME,
            f"{grupo}|{nombre}" if grupo else nombre,
        )
    return ChannelIdentity.crea(source_id, IdentityKind.NAME, "")


def identity_key(canal, source_id: str = "") -> str:
    """Clave de identidad de `canal`, o "" si no se puede resolver."""
    return channel_identity(canal, source_id).clave


# ---------------------------------------------------------------------------
# Reconciliación
# ---------------------------------------------------------------------------


class ChangeKind(str, Enum):
    """Qué le pasó a un canal entre dos versiones de la lista."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


@dataclass(frozen=True)
class ChannelChange:
    """Un cambio concreto, con lo que había y lo que hay."""

    kind: ChangeKind
    identity: ChannelIdentity
    #: Descripción del antes (nombre/URL anterior). Vacío si es nuevo.
    before: str = ""
    #: Descripción del después. Vacío si se eliminó.
    after: str = ""
    #: Qué campo cambió, si cambió algo. Vacío si no cambió.
    campo: str = ""

    @property
    def nombre(self) -> str:
        """Nombre visible del canal, para el informe."""
        return self.after or self.before

    def resumen(self) -> str:
        """Una línea para el modal."""
        if self.kind is ChangeKind.ADDED:
            return f"+ {self.nombre}"
        if self.kind is ChangeKind.REMOVED:
            return f"− {self.nombre}"
        if self.kind is ChangeKind.CHANGED:
            return f"~ {self.nombre} ({self.campo})"
        return f"= {self.nombre}"


@dataclass
class AmbiguousIdentity:
    """Dos o más canales comparten identidad débil (FR-CHAN-005)."""

    clave: str
    #: Nombre visible que comparten.
    nombre: str = ""
    #: Cuántos canales la comparten.
    cuantos: int = 2
    #: True si además se han resuelto por `tvg-id` distintos: entonces son
    #: canales distintos de verdad y la ambigüedad sólo viene del nombre.
    distintos_tvg: bool = False

    def explicacion(self) -> str:
        """Por qué hay ambigüedad, en una frase apta para un modal."""
        base = (
            f"{self.cuantos} canales comparten el mismo nombre y no declaran "
            "identificador, así que no se pueden distinguir con seguridad"
        )
        if self.distintos_tvg:
            return base + "; sí tienen identificadores distintos entre ellos"
        return base


@dataclass
class ReconcileReport:
    """Qué cambió de una versión de la lista a la otra (FR-CHAN-008).

    Es un **informe**, no una acción: reconciliar dice qué pasó, y decidir qué
    hacer con ello es de quien lo llama (que puede pedir confirmación antes de
    tocar favoritos, o no).
    """

    added: list[ChannelChange] = field(default_factory=list)
    removed: list[ChannelChange] = field(default_factory=list)
    changed: list[ChannelChange] = field(default_factory=list)
    unchanged: list[ChannelChange] = field(default_factory=list)
    #: Canales que estaban y ya no están. **No** se borran solos: un favorito
    #: de un canal desaparecido se marca para revisar (FR-CHAN-005, §9.4).
    orphans: list[ChannelChange] = field(default_factory=list)
    #: Identidades débiles compartidas por más de un canal.
    ambiguous: list[AmbiguousIdentity] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.added)
            + len(self.removed)
            + len(self.changed)
            + len(self.unchanged)
        )

    @property
    def hay_cambios(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def resumen(self) -> str:
        """Una línea con los cuatro números del resumen."""
        return (
            f"{len(self.added)} nuevos · {len(self.removed)} fuera · "
            f"{len(self.changed)} modificados · {len(self.unchanged)} sin cambios"
        )

    def para_modal(self) -> str:
        """Texto listo para un modal, con los avisos que apliquen."""
        partes = [self.resumen()]
        if self.orphans:
            partes.append(
                f"{len(self.orphans)} favoritos se quedan sin canal en la lista "
                "hasta que lo revises."
            )
        if self.ambiguous:
            partes.append(
                f"{len(self.ambiguous)} nombres están repetidos sin "
                "identificador: no se han fusionado."
            )
        return "\n".join(partes)


#: Campos que, si cambian, hacen que un canal se considere **modificado** y no
#: otro canal. Los visibles se miran antes que los técnicos.
#:
#: ``tvg_logo`` **no** está: un proveedor que cambia el logo de la mitad del
#: catálogo hizo «modificados» a 20 000 canales sin que cambiara ninguno, y el
#: usuario dejaría de leer el resumen. El logo no cambia el canal.
_CAMPOS_VISIBLES = ("name", "tvg_name", "group")
_CAMPOS_TECNICOS = ("url", "radio")

#: Campos que se comparan con su forma **normalizada**, porque una diferencia
#: cosmética (una barra final, un puerto por defecto) no es un cambio de canal.
_CAMPOS_NORMALIZADOS = {"url"}


def _comparable(valor: object) -> str:
    if valor is None:
        return ""
    return str(valor).strip()


def reconcile(
    previa: list,
    nueva: list,
    *,
    source_id: str = "",
) -> ReconcileReport:
    """Compara dos versiones de una lista y dice qué cambió (FR-CHAN-008).

    Args:
        previa: la lista que hay activa (canales de :class:`~thetvview.models.Channel`).
        nueva: la lista recién descargada.
        source_id: procedencia común de ambas. Si no se pasa, se compara sin
            ella, que es lo correcto al comparar dos versiones de la **misma**
            lista.

    Reglas:

    - la identidad fuerte (`tvg-id`) manda; si no hay, se usa la URL y, en
      último término, nombre + grupo;
    - dos canales con el mismo nombre y distinto `tvg-id` **no** se fusionan:
      salen como dos canales distintos, y la ambigüedad del nombre queda
      registrada;
    - un canal que cambió de URL pero conserva `tvg-id` sale como **modificado**
      (misma línea, dirección nueva), no como uno que salió y otro que entró;
    - los canales que desaparecen se listan en :attr:`ReconcileReport.orphans`
      además de en :attr:`ReconcileReport.removed`: desaparecer **no es** lo mismo
      que borrarse (puede ser una lista incompleta), y quien llama tiene que
      poder distinguirlos.
    """
    claves_previas: dict[str, tuple[object, ChannelIdentity]] = {}
    for canal in previa:
        identidad = channel_identity(canal, source_id)
        clave = identidad.clave
        if not clave:
            continue
        # Una clave repetida en la versión previa **no** se pisa: el primer
        # canal es el que se compara y el resto se resuelve por la ambigüedad
        # de nombre, que es donde se mira de verdad. Fusionarlos aquí sería
        # inventar una correspondencia que nadie declaró.
        claves_previas.setdefault(clave, (canal, identidad))

    reporte = ReconcileReport()

    for canal in nueva:
        identidad = channel_identity(canal, source_id)
        clave = identidad.clave
        if not clave:
            continue  # canal sin nada que lo identifique: no se puede resumir
        anterior = claves_previas.pop(clave, None)
        if anterior is None:
            reporte.added.append(
                ChannelChange(ChangeKind.ADDED, identidad, after=_etiqueta(canal))
            )
            continue
        canal_previo, identidad_previa = anterior
        campo = _campo_que_cambio(canal_previo, canal)
        if campo:
            reporte.changed.append(
                ChannelChange(
                    ChangeKind.CHANGED,
                    identidad,
                    before=_etiqueta(canal_previo),
                    after=_etiqueta(canal),
                    campo=campo,
                )
            )
        else:
            reporte.unchanged.append(
                ChannelChange(
                    ChangeKind.UNCHANGED,
                    identidad,
                    before=_etiqueta(canal_previo),
                    after=_etiqueta(canal),
                )
            )

    for canal, identidad in claves_previas.values():
        cambio = ChannelChange(
            ChangeKind.REMOVED, identidad, before=_etiqueta(canal)
        )
        reporte.removed.append(cambio)
        # Huérfano: estaba y ya no. No se borra de favoritos aquí: sólo se avisa.
        reporte.orphans.append(cambio)

    _marcar_ambiguedades(reporte, previa)
    _marcar_ambiguedades(reporte, nueva)
    return reporte


def _marcar_ambiguedades(reporte: ReconcileReport, canales: list) -> None:
    """Registra los nombres repetidos que **no** se pueden distinguir (FR-CHAN-005).

    Sólo se marca lo que es un riesgo real:

    - dos o más canales con el mismo nombre normalizado **y** al menos uno sin
      ``tvg-id``. Ahí no hay forma honesta de saber si son el mismo canal, y la
      política es mantenerlos separados y decirlo.
    - dos o más con el mismo nombre y ``tvg-id`` **distintos** entre sí sí son
      canales distintos de verdad (no se fusionan, y eso ya está garantizado
      por la identidad fuerte), pero se anotan igual con ``distintos_tvg`` para
      que el informe explique por qué el usuario ve dos canales iguales.

    Marcar cada homónimo de una lista de 100 000 canales llenaría el informe de
    ruido, así que un nombre que **todos** sus canales distinguen por
    ``tvg-id`` no se registra como ambiguo.
    """
    grupos: dict[str, list] = {}
    for canal in canales:
        nombre = normalize_name(getattr(canal, "name", ""))
        if not nombre:
            continue
        grupo = normalize_name(getattr(canal, "group", ""))
        grupos.setdefault(f"{grupo}|{nombre}", []).append(canal)
    ya = {a.clave for a in reporte.ambiguous}
    for clave, miembros in grupos.items():
        if len(miembros) < 2 or clave in ya:
            continue
        tvg = {
            str(getattr(c, "tvg_id", "") or "").strip()
            for c in miembros
            if str(getattr(c, "tvg_id", "") or "").strip()
        }
        if len(tvg) == len(miembros) and tvg:
            # Todos distinguibles: homónimos legítimos, no ambigüedad.
            continue
        reporte.ambiguous.append(
            AmbiguousIdentity(
                clave=clave,
                nombre=str(getattr(miembros[0], "name", "") or ""),
                cuantos=len(miembros),
                distintos_tvg=len(tvg) > 1,
            )
        )


def _etiqueta(canal) -> str:  # noqa: ANN001 - Channel
    """Nombre visible del canal, ya redactado por si colara una URL."""
    return redact_text(str(getattr(canal, "name", "") or getattr(canal, "url", "") or ""))


def _campo_que_cambio(previo, nuevo) -> str:  # noqa: ANN001 - Channel
    """Nombre del primer campo que cambió, o "" si nada cambió.

    Se comparan primero los campos **visibles** y después los técnicos: si
    cambió el nombre y la URL a la vez, lo que interesa al usuario es el nombre.
    """
    for campo in _CAMPOS_VISIBLES:
        if _comparable(getattr(previo, campo, "")) != _comparable(
            getattr(nuevo, campo, "")
        ):
            return campo
    for campo in _CAMPOS_TECNICOS:
        antes = getattr(previo, campo, "")
        ahora = getattr(nuevo, campo, "")
        if campo in _CAMPOS_NORMALIZADOS:
            # `http://x/a` y `http://x/a/` son el mismo recurso: sin normalizar,
            # media lista parecería «modificada» después de un simple cambio de
            # formato en el servidor.
            iguales = normalize_url(antes) == normalize_url(ahora)
        else:
            iguales = _comparable(antes) == _comparable(ahora)
        if not iguales:
            return campo
    return ""


def resumen_para_ui(reporte: ReconcileReport) -> list[str]:
    """Líneas sueltas para pintar una lista de cambios en la interfaz."""
    lineas: list[str] = []
    for titulo, cambios in (
        ("Nuevos", reporte.added),
        ("Modificados", reporte.changed),
        ("Fuera de la lista", reporte.removed),
    ):
        if not cambios:
            continue
        lineas.append(f"{titulo}: {len(cambios)}")
        for cambio in cambios[:20]:
            lineas.append(f"  {cambio.resumen()}")
        if len(cambios) > 20:
            lineas.append(f"  … y {len(cambios) - 20} más")
    for ambigua in reporte.ambiguous:
        lineas.append(f"Sin distinguir: {ambigua.nombre or ambigua.clave}")
        lineas.append(f"  {ambigua.explicacion()}")
    return lineas