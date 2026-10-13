"""Caché de combinaciones exitosas (FR-CAP-007, plan Fase 5/CAP-002).

Cuando el router descubre que un reproductor **abre** un transporte, esa
combinación se guarda con lo que pide el FR-CAP-007: **fecha, versión del
motor, ámbito y caducidad**, y se invalida al cambiar la versión del motor,
las capacidades relevantes o la configuración.

Decisiones que son parte del diseño, no detalles:

- **La clave es el transporte, nunca la URL.** Una URL de IPTV puede llevar
  token en la query o userinfo reconstruido; persistir la URL sería volcar
  secretos a disco. El transporte (``http``, ``udp``…) es además lo único
  sobre lo que decide el router, así que la granularidad es exactamente la
  que se necesita.
- **Sólo la ruta del router la usa.** ``play_channel`` la consulta cuando
  el usuario no eligió reproductor (si lo eligió, su preferencia manda) y
  sólo guarda éxitos de esa ruta: un lanzamiento explícito ya deja rastro
  en ``prefs``. El router sigue siendo puro: la caché se lee y se escribe
  en :func:`thetvview.ui.screens.play_channel`, fuera de
  :func:`thetvview.player.router.order_candidates`.
- **Nunca lanza.** ``recordar`` devuelve False y ``buscar`` devuelve None
  si algo falla: una caché rota no puede impedir que se abra un canal.
- **Nace en :meth:`thetvview.ui.app.App.run`**, no en ``__init__``: los
  tests construyen App sin ``run()``, así que ni leen ni escriben el
  fichero. Fuera de ahí es opcional vía ``getattr(app, "combinaciones",
  None)``.

La versión del motor se obtiene ejecutando ``<binario> --version`` con
timeout y sin shell, memorizada por ``(ruta, mtime, tamaño)``: si el binario
cambia, cambia la versión memorizada, que es justo la invalidación que pide
el FR.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .. import config
from ..security.local_files import chmod_private
from ..storage.json_store import atomic_write_json, read_json_with_recovery
from . import protocols as _protocols

__all__ = [
    "AMBITO_TRANSPORTE",
    "Exito",
    "SuccessCache",
    "TTL_DIAS",
    "MAX_ENTRADAS",
    "default_cache",
    "detectar_version",
    "huella_capacidades",
    "huella_configuracion",
    "reset_default_cache",
]

#: Ámbito por defecto: la clave describe un transporte. El campo existe
#: abierto (FR-CAP-007 pide ámbito) para que mañana quepa, p. ej., un
#: ámbito por canal sin cambiar el formato del fichero.
AMBITO_TRANSPORTE: str = "transporte"

#: Caducidad de una entrada: 30 días. Un binario que lleva un mes
#: funcionando y luego deja de hacerlo ya está cubierto por el historial de
#: fallos de la sesión; la caché sólo acelera lo que sigue vigente.
TTL_DIAS: int = 30

#: Tope de entradas: además de la caducidad, el FR-CAP-010 del diagnóstico
#: (y el buen juicio) exigen no crecer sin límite. Con un transporte por
#: entrada, 100 es un techo holgado.
MAX_ENTRADAS: int = 100

#: Timeout y longitud de la línea de ``--version``.
_VERSION_TIMEOUT: float = 3.0
_VERSION_MAX: int = 200

#: (ruta, mtime_ns, tamaño) → primera línea de la versión. Un ``--version``
#: por llamada sería un subprocess en cada canal; así es un subprocess por
#: binario y sesión.
_VERSION_MEMO: dict[tuple[str, int, int], str] = {}


def detectar_version(ruta: str | None) -> str:
    """Primera línea de ``<ruta> --version``, o "" si no se pudo.

    Sin shell, con timeout y con ``stdin=DEVNULL``. Si el binario no
    existe, tarda o no dice nada, devuelve "": la versión desconocida no
    rompe nada (las entradas con versión desconocida no se invalidan por
    versión, porque no hay evidencia de cambio).
    """
    if not ruta:
        return ""
    try:
        stat = os.stat(ruta)
    except OSError:
        return ""
    clave_memo = (os.path.abspath(ruta), stat.st_mtime_ns, stat.st_size)
    memorizada = _VERSION_MEMO.get(clave_memo)
    if memorizada is not None:
        return memorizada
    if len(_VERSION_MEMO) > 64:
        _VERSION_MEMO.clear()
    linea = ""
    try:
        proc = subprocess.run(
            [str(ruta), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_TIMEOUT,
            check=False,
        )
        bruto = proc.stdout or ""
        if not bruto.strip():
            bruto = proc.stderr or ""
        for candidata in bruto.splitlines():
            if candidata.strip():
                linea = candidata.strip()[:_VERSION_MAX]
                break
    except (OSError, subprocess.SubprocessError):
        linea = ""
    _VERSION_MEMO[clave_memo] = linea
    return linea


def _resumen(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]


def huella_capacidades(reproductor: str) -> str:
    """Huella de las capacidades relevantes de `reproductor`.

    Relevantes = la fila de transportes que hace que la combinación
    (transporte × motor) funcione o no. Si mañana se añade un transporte a
    la fila de VLC, las entradas de VLC dejan de valer y se descartan en la
    siguiente lectura (FR-CAP-007: «se invalidará al cambiar … capacidades
    relevantes»).
    """
    nombre = str(reproductor or "").strip().lower()
    fila = _protocols.PLAYER_PROTOCOL_SUPPORT.get(nombre)
    partes = ["", "", ""]
    if fila is not None:
        partes = [
            fila.name,
            ",".join(sorted(fila.transports)),
            ",".join(sorted(fila.medido or ())),
        ]
    return _resumen("|".join(partes))


def huella_configuracion() -> str:
    """Huella de la configuración que cambia el significado de una combinación.

    Hoy: el orden de :data:`thetvview.config.SUPPORTED_PLAYERS`, que es lo
    que fija qué motor se prueba primero. Si se añade una opción que cambie
    el argv construido, entra aquí también (y entonces las entradas se
    invalidan, que es lo que pide el FR).
    """
    return _resumen(",".join(config.SUPPORTED_PLAYERS))


@dataclass(frozen=True)
class Exito:
    """Una combinación guardada, con todo lo que pide FR-CAP-007."""

    ambito: str
    clave: str
    reproductor: str
    fecha: str
    version_motor: str
    huella_capacidades: str
    huella_config: str
    caduca: str

    @classmethod
    def from_mapping(cls, data: object) -> Exito | None:
        """Construye desde JSON; None si el dato no es usable (se ignora)."""
        if not isinstance(data, dict):
            return None
        campos = (
            "ambito", "clave", "reproductor", "fecha",
            "version_motor", "huella_capacidades", "huella_config", "caduca",
        )
        valores = []
        for campo in campos:
            valor = data.get(campo)
            if not isinstance(valor, str):
                return None
            valores.append(valor)
        return cls(*valores)

    def to_mapping(self) -> dict[str, str]:
        return asdict(self)


class SuccessCache:
    """Almacén de combinaciones exitosas con invalidación (FR-CAP-007).

    Args:
        path: fichero JSON. Si es None se usa
            :data:`thetvview.config.PLAYER_CACHE_JSON` **en cada operación**
            (así un test que parchee config sigue viendo el cambio).
        ttl_dias: caducidad de cada entrada.
        max_entradas: tope de entradas guardadas (se descarta la más
            antigua al pasarlo).
        ahora: reloj inyectable para tests.
        version_fn: detector de versión inyectable para tests (recibe la
            ruta del binario o None).
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        ttl_dias: int = TTL_DIAS,
        max_entradas: int = MAX_ENTRADAS,
        ahora: Callable[[], datetime] | None = None,
        version_fn: Callable[[str | None], str] | None = None,
    ) -> None:
        self._path_explicito = Path(path) if path is not None else None
        self.ttl_dias = int(ttl_dias)
        self.max_entradas = max(1, int(max_entradas))
        self._ahora = ahora or (lambda: datetime.now(timezone.utc))
        self._version_fn = version_fn or detectar_version

    # -- ruta y E/S -------------------------------------------------------

    @property
    def ruta(self) -> Path:
        """Fichero efectivo (la explícita o la de config, resuelta ahora)."""
        if self._path_explicito is not None:
            return self._path_explicito
        return Path(config.PLAYER_CACHE_JSON)

    def _cargar(self) -> list[Exito]:
        try:
            crudo = read_json_with_recovery(self.ruta)
        except Exception:  # noqa: BLE001 - corrupto sin backup: sin caché
            return []
        if not isinstance(crudo, dict):
            return []
        entradas = crudo.get("entradas")
        if not isinstance(entradas, list):
            return []
        resultado = []
        for elemento in entradas:
            exito = Exito.from_mapping(elemento)
            if exito is not None:
                resultado.append(exito)
        return resultado

    def _guardar(self, entradas: list[Exito]) -> None:
        atomic_write_json(
            self.ruta,
            {"entradas": [e.to_mapping() for e in entradas]},
        )
        try:
            chmod_private(self.ruta)
        except Exception:  # noqa: BLE001 - permisos: se intenta, no exige
            pass

    # -- operaciones ------------------------------------------------------

    def entradas(self) -> tuple[Exito, ...]:
        """Todas las entradas guardadas, sin validar (para informes/tests)."""
        try:
            return tuple(self._cargar())
        except Exception:  # noqa: BLE001
            return ()

    def recordar(
        self,
        clave: str,
        reproductor: str,
        *,
        ambito: str = AMBITO_TRANSPORTE,
    ) -> bool:
        """Guarda la combinación (ámbito, clave) → `reproductor` como exitosa.

        Returns:
            True si se persistió; False si no (clave vacía, disco a
            rechazar…). Nunca lanza: se llama justo después de abrir un
            canal y eso no puede fallar por una caché.
        """
        clave = str(clave or "").strip().lower()
        reproductor = str(reproductor or "").strip().lower()
        if not clave or not reproductor:
            return False
        try:
            momento = self._ahora().astimezone(timezone.utc)
            entrada = Exito(
                ambito=str(ambito or AMBITO_TRANSPORTE),
                clave=clave,
                reproductor=reproductor,
                fecha=momento.isoformat(),
                version_motor=self._version_de(reproductor),
                huella_capacidades=huella_capacidades(reproductor),
                huella_config=huella_configuracion(),
                caduca=(momento + timedelta(days=self.ttl_dias)).isoformat(),
            )
            previas = [
                e
                for e in self._cargar()
                if not (e.ambito == entrada.ambito and e.clave == entrada.clave)
            ]
            # La nueva va **primero**: si dos fechas empatan (reloj fijo de
            # tests), el sort estable conserva este orden y el tope se
            # come la más antigua, nunca la recién guardada.
            previas.insert(0, entrada)
            previas.sort(key=lambda e: e.fecha, reverse=True)
            self._guardar(previas[: self.max_entradas])
            return True
        except Exception:  # noqa: BLE001 - ver docstring
            return False

    def buscar(
        self,
        clave: str,
        *,
        ambito: str = AMBITO_TRANSPORTE,
    ) -> Exito | None:
        """Combinación exitosa vigente para (ámbito, clave), o None.

        «Vigente» = no caducada, con la versión del motor actual, con las
        capacidades relevantes iguales y con la configuración igual. Lo que
        no lo esté **se purga**, no se devuelve en falso.
        """
        clave = str(clave or "").strip().lower()
        if not clave:
            return None
        try:
            momento = self._ahora().astimezone(timezone.utc)
            vivas: list[Exito] = []
            ganadora: Exito | None = None
            hubo_cambios = False
            for entrada in self._cargar():
                valida = (
                    entrada.ambito == ambito
                    and entrada.clave == clave
                    and self._vigente(entrada, momento)
                )
                if entrada.ambito == ambito and entrada.clave == clave:
                    if valida and ganadora is None:
                        ganadora = entrada
                    else:
                        hubo_cambios = True  # caducada o inválida: fuera
                        continue
                vivas.append(entrada)
            if hubo_cambios:
                self._guardar(vivas)
            return ganadora
        except Exception:  # noqa: BLE001 - ver docstring
            return None

    def purgar(self) -> int:
        """Descarta entradas caducadas o invalidadas; devuelve cuántas."""
        try:
            momento = self._ahora().astimezone(timezone.utc)
            previas = self._cargar()
            vivas = [e for e in previas if self._vigente(e, momento)]
            quitadas = len(previas) - len(vivas)
            if quitadas:
                self._guardar(vivas)
            return quitadas
        except Exception:  # noqa: BLE001
            return 0

    def olvidar(self, clave: str, *, ambito: str = AMBITO_TRANSPORTE) -> bool:
        """Borra la entrada de (ámbito, clave)."""
        clave = str(clave or "").strip().lower()
        try:
            previas = self._cargar()
            vivas = [
                e for e in previas if not (e.ambito == ambito and e.clave == clave)
            ]
            if len(vivas) == len(previas):
                return False
            self._guardar(vivas)
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- invalidación -----------------------------------------------------

    def _vigente(self, entrada: Exito, momento: datetime) -> bool:
        """¿Sigue valiendo esta entrada? (caducidad + version + huellas)."""
        # 1. Caducidad.
        try:
            caduca = datetime.fromisoformat(entrada.caduca)
        except ValueError:
            return False
        if caduca.tzinfo is None:
            caduca = caduca.replace(tzinfo=timezone.utc)
        if momento > caduca:
            return False
        # 2. Versión del motor: sólo invalida si **ambas** son conocidas y
        #    difieren. Desconocido ≠ cambiado.
        actual = self._version_de(entrada.reproductor)
        if actual and entrada.version_motor and actual != entrada.version_motor:
            return False
        # 3. Capacidades relevantes y configuración actuales.
        if entrada.huella_capacidades != huella_capacidades(entrada.reproductor):
            return False
        if entrada.huella_config != huella_configuracion():
            return False
        return True

    def _version_de(self, reproductor: str) -> str:
        try:
            ruta = config.find_player(reproductor)
        except Exception:  # noqa: BLE001
            ruta = None
        try:
            return str(self._version_fn(ruta) or "")
        except Exception:  # noqa: BLE001
            return ""


_CACHE_POR_DEFECTO: SuccessCache | None = None


def default_cache() -> SuccessCache:
    """La caché de proceso (se crea vacía: no toca disco hasta operar)."""
    global _CACHE_POR_DEFECTO
    if _CACHE_POR_DEFECTO is None:
        _CACHE_POR_DEFECTO = SuccessCache()
    return _CACHE_POR_DEFECTO


def reset_default_cache() -> None:
    """Olvida la caché de proceso (tests)."""
    global _CACHE_POR_DEFECTO
    _CACHE_POR_DEFECTO = None
