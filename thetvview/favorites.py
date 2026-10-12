"""Persistencia de canales favoritos (data/favorites.json).

Solo stdlib. El archivo es una lista de canales serializados:
    [{"name": ..., "url": ..., "tvg_id": ..., ...}, ...]

La forma del archivo **no cambia**: una entrada sigue siendo exactamente un
``Channel``, y ``Channel(**entrada)`` sigue funcionando. Es un contrato que usan
la app, el `test_cam_flow` y quien exporte sus datos; romperlo para meter un
campo más sería pagar mucho por muy poco.

Identidad: `tvg-id` con respaldo en la URL (CHAN-002)
-------------------------------------------------------

**Antes**, la identidad de un favorito era su `url` y punto: `toggle` añadía y
quitaba por URL. Con eso, un proveedor que rota el token de sus líneas, o que
cambia el `get.php`, dejaba todos los favoritos apuntando a direcciones que ya
no existen, sin aviso. El favorito no se perdía —el archivo seguía ahí— pero
dejaba de servir para nada, y el usuario no tenía forma de saber por qué.

Ahora la identidad es **la del canal**, no la de la dirección:

1. si el canal trae ``tvg-id``, se guarda como identidad y el favorito se
   reconoce aunque su URL haya cambiado por completo;
2. si no lo trae, se guarda la URL normalizada y todo funciona como antes.

**Lectura dual**: un ``favorites.json`` sin el campo ``identity`` —o con un
campo que esta versión no entiende— se lee **exactamente** como antes, por URL.
Un archivo de usuario nunca se rompe por una mejora de la app.

Por qué ``identity`` no es un campo de :class:`~thetvview.models.Channel`
-----------------------------------------------------------------------

Porque `Channel` no crece: el repo tiene una regla explícita (H1) de que los
datos derivados se calculan al preguntar y no se persisten en el modelo. La
identidad guardada es, por definición, información de **este** archivo y no del
canal, así que vive al lado del canal en el JSON y en un mapa en memoria, sin
tocar el dataclass que usa toda la aplicación.

Por qué **no** hay un campo ``identity`` en el archivo
-------------------------------------------------------

Porque sería **redundante**: un canal con `tvg-id` ya lo guarda en su propio
campo `tvg_id`, y uno que no lo tiene usa la URL, que también está guardada.
Persistir la identidad sería escribir dos veces lo mismo y, de paso, romper el
round-trip `Channel(**entrada)` que la app da por hecho. La identidad se
**deduce** al leer, con :func:`thetvview.channel_identity.channel_identity`, y
por eso un `favorites.json` de cualquier versión pasada o futura se lee sin
migración.

Escritura atómica con copia previa
----------------------------------

``_write`` saca un ``.bak`` antes de sustituir (CHAN-002). Un ``favorites.json``
es lo único que el usuario no puede regenerar: perderlo es perder su trabajo
manual. El coste de una copia es despreciable al lado de eso.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

from .channel_identity import channel_identity
from .models import Channel
from .security.local_files import chmod_private
from .security.redaction import needs_redaction_for_storage, redact_text
from .storage.errors import StorageError
from .storage.json_store import (
    atomic_write_json,
    file_lock,
    make_backup,
    read_json_with_recovery,
)
from .storage.schemas import validate_favorites
from .stream_ref import is_opaque_ref


class FavoritesError(Exception):
    """Error amigable de gestión de favoritos."""


class FavoritesManager:
    """CRUD mínimo sobre favorites.json con escritura atómica.

    Cache en memoria: `is_favorite()`/`favorite_urls()` no tocan disco
    en cada llamada (antes hacían `load()`+JSON parse por canal, lo que
    colgaba la TUI con listas de decenas de miles de canales: N
    lecturas de disco por frame/tecla). La cache se invalida al
    escribir y se refresca si el archivo cambia por fuera (mtime).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache: list[Channel] | None = None
        self._cache_mtime: float | None = None

    # --- Persistencia -------------------------------------------------------

    def _read(self) -> list[dict]:
        """Lee el fichero de favoritos.

        El store lanza :class:`StorageError` cuando el JSON está corrupto, y
        aquí se traduce a :class:`FavoritesError`: el contrato de este módulo
        es "favoritos o un error de favoritos", y quien lo llama —la TUI— ya
        sabe presentar ese tipo en un modal. Dejar que ``StorageError``
        escapara significaría que una corrupción de ``favorites.json``
        reventara la pantalla en vez de explicarse.
        """
        try:
            data = read_json_with_recovery(self.path, schema_version=None)
        except StorageError as exc:
            raise FavoritesError(str(exc)) from exc
        if data is None:
            return []
        if not isinstance(data, list):
            raise FavoritesError(f"Formato inesperado en {self.path} (se esperaba lista).")
        return [e for e in data if isinstance(e, dict)]

    def _write(self, channels: list[Channel]) -> None:
        """Escribe la lista completa de forma atómica, con copia previa.

        La copia ``.bak`` va **antes** de tocar nada (CHAN-002): si el proceso
        muere a mitad de la escritura, `atomic_write_json` garantiza que el
        archivo principal es el viejo o el nuevo, pero no garantiza que el viejo
        siga disponible. Con el ``.bak`` sí, y `read_json_with_recovery` ya sabe
        restaurarlo.
        """
        make_backup(self.path)
        atomic_write_json(
            self.path,
            [asdict(c) for c in channels],
            schema_version=None,
            validate=validate_favorites,
        )
        chmod_private(self.path)  # 0600: puede traer URLs con login heredadas
        self._cache = list(channels)
        try:
            self._cache_mtime = self.path.stat().st_mtime
        except OSError:
            self._cache_mtime = None

    def _cache_valid(self) -> bool:
        """True si la cache en memoria sigue vigente (mtime sin cambios)."""
        if self._cache is None:
            return False
        try:
            return self.path.stat().st_mtime == self._cache_mtime
        except OSError:
            # Archivo borrado o ilegible: solo vale la cache si era vacía.
            return not self._cache and self._cache_mtime is None

    def invalidate(self) -> None:
        """Olvida la cache en memoria (forzar re-lectura en el próximo uso)."""
        self._cache = None
        self._cache_mtime = None

    # --- API pública ----------------------------------------------------------

    def load(self) -> list[Channel]:
        """Carga todos los favoritos; lista vacía si el archivo no existe.

        Usa cache en memoria: solo relee el JSON si cambió en disco.
        Devuelve una copia para que el llamante no mute la cache.
        """
        if self._cache_valid():
            return list(self._cache or [])
        out: list[Channel] = []
        migrated = False
        for raw in self._read():
            try:
                # `identity` fue un campo experimental de este archivo y ya no
                # se escribe. Se descarta aquí para que un `favorites.json`
                # que lo traiga (una versión intermedia, una copia hecha a mano)
                # siga cargándose sin romper el `Channel(**raw)`.
                raw.pop("identity", None)
                raw["extra_options"] = [tuple(p) for p in raw.get("extra_options", [])]
                # Entradas antiguas con credenciales embebidas se limpian
                # al leer y se reescriben en disco (SDD §37).
                u = str(raw.get("url") or "")
                if u and not is_opaque_ref(u) and needs_redaction_for_storage(u):
                    raw["url"] = redact_text(u)
                    migrated = True
                out.append(Channel(**raw))
            except (KeyError, TypeError):
                continue  # entrada malformada: se ignora sin romper la app
        self._cache = list(out)
        try:
            self._cache_mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            self._cache_mtime = None
        # Se reescribe sólo si hubo credenciales que limpiar. Un archivo ya
        # correcto no se toca: abrir la app no debe reescribir el
        # `favorites.json` de cada usuario ni ocupar espacio con un `.bak`.
        if migrated:
            try:
                self._write(out)
            except OSError:
                pass
        return list(out)

    def favorite_urls(self) -> set[str]:
        """Set de urls favoritas (O(1) por consulta, sin IO repetido)."""
        if self._cache_valid():
            return {c.url for c in (self._cache or [])}
        return {c.url for c in self.load()}

    def favorite_tvg_ids(self) -> set[str]:
        """`tvg-id` de los favoritos que lo declaran. O(1), sin tocar disco.

        Es el segundo criterio de :meth:`is_favorite`, y el que hace que un
        favorito sobreviva a un giro de token del proveedor.
        """
        return {
            tvg
            for tvg in (
                str(getattr(c, "tvg_id", "") or "").strip() for c in self.load()
            )
            if tvg
        }

    def is_favorite(self, channel: Channel) -> bool:
        """¿Este canal es favorito?

        Dos criterios, en orden de fiabilidad:

        1. **igualdad de URL**: lo de siempre, y sigue funcionando con cualquier
           `favorites.json` anterior a este cambio;
        2. **igualdad de `tvg-id`**, sólo cuando el canal **no** es una
           referencia opaca. Es lo que evita que un giro de token del proveedor
           convierta todos los favoritos en inservibles.

        La restricción del punto 2 no es un detalle: en Xtream la URL **es** el
        modo de reproducción (``xtream://`` para el directo,
        ``xtream-ts://`` para el archivo de catch-up), y dos favoritos con el
        mismo `tvg-id` son dos favoritos legítimamente distintos. Confundirlos
        haría que favoritar el directo borrara el archivo, o al revés (SDD
        Catch-up §14).
        """
        if channel.url in self.favorite_urls():
            return True
        tvg_id = str(getattr(channel, "tvg_id", "") or "").strip()
        if not tvg_id or is_opaque_ref(str(getattr(channel, "url", "") or "")):
            return False
        return tvg_id in self.favorite_tvg_ids()

    def pending_confirmation(self) -> list[str]:
        """URLs de favoritos atados a su dirección por no tener `tvg-id`.

        Son los que el proveedor puede invalidar en cualquier momento: se quedan
        «pendientes de confirmar» en la UI en vez de fingir que están tan seguros
        como los que sí declararon identificador (§9.4, CHAN-002).
        """
        return sorted(
            c.url
            for c in self.load()
            if not str(getattr(c, "tvg_id", "") or "").strip()
        )

    def identities(self) -> dict[str, dict[str, str]]:
        """URL → identidad deducida del favorito. Para diagnóstico y tests.

        **Deducida**, no guardada: ver el docstring del módulo. Se reconstruye en
        cada llamada a partir de los canales, así que no hay forma de que se
        quede desincronizada con el archivo.
        """
        return {
            c.url: channel_identity(c).para_guardar()
            for c in self.load()
            if c.url
        }

    def toggle(self, channel: Channel) -> bool:
        """Añade o quita el canal. True si quedó como favorito.

        Todo el par leer-modificar-escribir va bajo el candado del fichero
        (STORE-006): sin él, dos llamadas simultáneas podían leer la misma
        lista y la segunda escritura se comía el cambio de la primera.

        Compara **por URL exacta**, y no por `tvg-id` como hace
        :meth:`is_favorite`. Es deliberado: son dos preguntas distintas. «¿Está
        este canal en mis favoritos?» puede responderse por identidad, porque
        el usuario está viendo un canal concreto; pero «¿qué debo quitar?» no
        puede, porque en Xtream la misma identidad tiene varias líneas y quitar
        la de archivo cuando el usuario pidió la de directo sería perder un
        favorito que sigue en pie.
        """
        channel = self._clean(channel)
        with file_lock(self.path):
            entries = self.load()
            if any(c.url == channel.url for c in entries):
                self._write([c for c in entries if c.url != channel.url])
                return False
            entries.append(channel)
            self._write(entries)
            return True

    def remove(self, channel: Channel) -> bool:
        """Quita por url. Devuelve True si se eliminó algo."""
        channel = self._clean(channel)
        with file_lock(self.path):
            entries = self.load()
            kept = [c for c in entries if c.url != channel.url]
            if len(kept) == len(entries):
                return False
            self._write(kept)
            return True

    @staticmethod
    def _clean(channel: Channel) -> Channel:
        """Copia sin credenciales embebidas en `url` (SDD §37).

        Las referencias opacas (`xtream://` y `xtream-ts://`, directo y
        archivo) ya van sin credenciales: se dejan intactas. Una URL de
        catch-up renderizada desde una plantilla `catchup-source` que las
        traiga sí se redacta, y por tanto deja de ser reproducible al
        releerla — que es justo lo que se prefiere a filtrar la contraseña
        a disco.
        """
        if is_opaque_ref(channel.url) or not needs_redaction_for_storage(channel.url):
            return channel
        return replace(channel, url=redact_text(channel.url))
