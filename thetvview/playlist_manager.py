"""Gestión y persistencia del catálogo de playlists (playlists.json).

Solo stdlib. El archivo es una lista de entradas:
    [{"name": "...", "source": "path_o_url", "added": "ISO8601",
      "kind": "m3u", "server_url": null, "username": null}, ...]

Soporta playlists M3U (kind="m3u") y fuentes Xtream (kind="xtream").
Para Xtream: server_url y username se persisten; el password **nunca** se
escribe en disco: vive en el SecretStore del SO (SDD §15, gap B7).
Para M3U: nunca hay password.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .security.local_files import chmod_private
from .security.secrets import get_store


# Prefijo de source que identifica una lista Xtream API.
XTREAM_SOURCE_PREFIX = "xtream://"


class PlaylistError(Exception):
    """Error amigable de gestión de playlists."""


def _as_bool(value: object) -> bool:
    """Interpreta el flag JSON `allow_private_network` tolerando basura."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "sí", "si")
    return False


@dataclass
class PlaylistEntry:
    """Una playlist registrada en el catálogo.

    - kind: "m3u" (default) o "xtream"
    - server_url, username: solo para kind="xtream", se persisten.
    - password: solo para kind="xtream". Vive en el SecretStore del SO y en
      la memoria de la sesión; jamás se serializa a JSON.
    """

    name: str
    source: str  # ruta local, URL, o "xtream://<server_url>" para kind=xtream
    added: str = ""  # fecha ISO8601
    kind: str = "m3u"  # "m3u" | "xtream"
    server_url: str = ""  # solo para xtream
    username: str = ""  # solo para xtream
    # password: solo para kind="xtream". En memoria para la sesión.
    password: str = field(default="", repr=False, compare=False)
    # Excepción anti-SSRF de ESTA fuente (SDD §11). Nunca global ni silencioso.
    allow_private_network: bool = False

    def __post_init__(self) -> None:
        if not self.added:
            self.added = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Normalizar kind: el prefijo xtream:// en source manda (es API Xtream).
        if self.source.startswith(XTREAM_SOURCE_PREFIX):
            self.kind = "xtream"
            if not self.server_url:
                self.server_url = self.source[len(XTREAM_SOURCE_PREFIX):]
        elif self.kind not in ("m3u", "xtream"):
            self.kind = "m3u"

    @property
    def is_xtream(self) -> bool:
        """True solo para listas Xtream API (kind="xtream", source xtream://…)."""
        return self.kind == "xtream" or self.source.startswith(XTREAM_SOURCE_PREFIX)

    @property
    def source_id(self) -> str:
        """Identificador estable de la fuente (clave del SecretStore).

        Incluye el nombre porque es único dentro de un catálogo: dos fuentes
        apuntando al mismo servidor/usuario son cuentas distintas.
        """
        if self.is_xtream:
            return f"xtream|{self.name}|{self.server_url or self.source}|{self.username}"
        return f"m3u|{self.name}|{self.source}"

    def to_dict(self) -> dict:
        """Serializa a dict para JSON. El password **nunca** se serializa.

        - kind="xtream": incluye server_url y username.
        - kind="m3u": ni server_url/username ni password.
        - ``allow_private_network`` se conserva siempre: es la excepción
          SSRF declarada para esa fuente concreta.
        """
        d = asdict(self)
        d.pop("password", None)
        if self.kind == "m3u":
            d.pop("server_url", None)
            d.pop("username", None)
        return d


class PlaylistManager:
    """CRUD mínimo sobre playlists.json con escritura atómica."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # Passwords Xtream en memoria (cache de sesión).
        # key = clave del SecretStore (catálogo + fuente); valor = password.
        self._passwords: dict[str, str] = {}

    # --- Persistencia -----------------------------------------------------

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlaylistError(
                f"No se pudo leer {self.path}: {exc.__class__.__name__}. "
                "El archivo puede estar corrupto."
            ) from exc
        if not isinstance(data, list):
            raise PlaylistError(f"Formato inesperado en {self.path} (se esperaba lista).")
        return [e for e in data if isinstance(e, dict)]

    def _write(self, entries: list[PlaylistEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(
            [e.to_dict() for e in entries],
            ensure_ascii=False,
            indent=2,
        )
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)  # atómico en POSIX
        # Sin passwords dentro, pero igualmente restringimos a 0o600.
        chmod_private(self.path)

    # --- SecretStore --------------------------------------------------------

    def _store(self):  # type: ignore[no-untyped-def]
        """Almacén de secretos del SO (o memoria en CI/tests)."""
        return get_store()

    def _secret_key(self, entry: PlaylistEntry) -> str:
        """Clave del SecretStore: única por catálogo + fuente.

        El catálogo entra en la clave para que dos `playlists.json` distintos
        (tests, usuarios) no colisionen en el keyring compartido del SO.
        """
        return f"{self.path}::{entry.source_id}"

    # --- API pública --------------------------------------------------------

    def load(self) -> list[PlaylistEntry]:
        """Carga todas las entradas; lista vacía si el archivo no existe.

        Entradas viejas sin 'kind' se cargan como kind="m3u" (migración invisible).
        El password Xtream se lee del SecretStore; un password en texto plano
        heredado de una versión anterior se migra al store y se borra del disco.
        """
        store = self._store()
        out: list[PlaylistEntry] = []
        migrated = False
        for raw in self._read():
            try:
                source = str(raw["source"])
                kind = str(raw.get("kind", "m3u"))
                # El prefijo xtream:// identifica la API Xtream (migración).
                if source.startswith(XTREAM_SOURCE_PREFIX):
                    kind = "xtream"
                entry = PlaylistEntry(
                    name=str(raw["name"]),
                    source=source,
                    added=str(raw.get("added", "")),
                    kind=kind,
                    server_url=str(raw.get("server_url", "")),
                    username=str(raw.get("username", "")),
                    allow_private_network=_as_bool(raw.get("allow_private_network")),
                )
            except KeyError:
                continue  # entrada malformada: se ignora sin romper la app
            if entry.is_xtream:
                key = self._secret_key(entry)
                legacy = str(raw.get("password", ""))
                stored = self._passwords.get(key) or store.get_password(key)
                if legacy:
                    if not stored:
                        try:
                            store.set_password(key, legacy)
                        except Exception:
                            pass  # sin keyring: no romper la carga
                        stored = legacy
                    migrated = True  # el texto plano ya no debe estar en disco
                if stored:
                    self._passwords[key] = stored
                    entry.password = stored
            out.append(entry)
        if migrated:
            try:
                self._write(out)
            except OSError:
                pass
        return out

    def add(
        self,
        name: str,
        source: str,
        *,
        allow_private_network: bool = False,
    ) -> PlaylistEntry:
        """Añade una playlist IPTV (.m3u/.m3u8/.ts); falla amigablemente si el nombre ya existe."""
        name, source = name.strip(), source.strip()
        if not name or not source:
            raise PlaylistError("Nombre y origen son obligatorios.")
        entries = self.load()
        if any(e.name == name for e in entries):
            raise PlaylistError(f"Ya existe una playlist llamada '{name}'.")
        entry = PlaylistEntry(
            name=name,
            source=source,
            kind="m3u",
            allow_private_network=bool(allow_private_network),
        )
        entries.append(entry)
        self._write(entries)
        return entry

    def add_xtream(
        self,
        name: str,
        server_url: str,
        username: str,
        password: str,
        *,
        allow_private_network: bool = False,
    ) -> PlaylistEntry:
        """Añade una fuente Xtream. El password va al SecretStore, nunca a disco."""
        name = name.strip()
        server_url = server_url.strip()
        username = username.strip()
        if not name or not server_url or not username:
            raise PlaylistError("Nombre, servidor y usuario son obligatorios.")
        entries = self.load()
        if any(e.name == name for e in entries):
            raise PlaylistError(f"Ya existe una playlist llamada '{name}'.")
        # Source sintético para backward compat
        source = f"{XTREAM_SOURCE_PREFIX}{server_url}"
        entry = PlaylistEntry(
            name=name,
            source=source,
            kind="xtream",
            server_url=server_url,
            username=username,
            password=password or "",
            allow_private_network=bool(allow_private_network),
        )
        entries.append(entry)
        self._write(entries)
        self._store_password(entry, password or "")
        return entry

    def set_allow_private_network(self, name: str, enabled: bool) -> bool:
        """Activa/desactiva la excepción SSRF de una fuente concreta.

        Es la única forma de permitir un destino privado: por fuente y
        explícita (SDD §11 «Excepción»). Devuelve False si la fuente no existe.
        """
        entries = self.load()
        for entry in entries:
            if entry.name == name:
                entry.allow_private_network = bool(enabled)
                self._write(entries)
                return True
        return False

    def allows_private_network(self, name: str) -> bool:
        """True si la fuente `name` acepta destinos privados/internos."""
        entry = self.get(name)
        return bool(entry is not None and entry.allow_private_network)

    def allow_private_for(self, source: str) -> bool:
        """Excepción anti-SSRF declarada para una fuente concreta (SDD §11).

        Útil cuando sólo se tiene el `source` (URL o path), no el nombre:
        es el caso de la carga de playlists y del EPG que declara la lista.
        Devuelve False si la fuente no está en el catálogo: por defecto la
        red privada está bloqueada.
        """
        want = (source or "").strip()
        if not want:
            return False
        for entry in self.load():
            if (entry.source or "").strip() == want:
                return bool(entry.allow_private_network)
        return False

    def _store_password(self, entry: PlaylistEntry, password: str) -> None:
        """Guarda (o borra) el password en el SecretStore y refresca la cache."""
        key = self._secret_key(entry)
        try:
            store = self._store()
            if password:
                store.set_password(key, password)
            else:
                store.delete_password(key)
        except Exception:
            pass  # sin keyring disponible: no romper la operación
        if password:
            self._passwords[key] = password
        else:
            self._passwords.pop(key, None)

    def get_credentials(self, name: str) -> tuple[str, str, str] | None:
        """Devuelve (server_url, username, password) de una entrada Xtream.

        El password se resuelve desde el SecretStore (cacheado en memoria).
        Solo Xtream: para M3U siempre devuelve None.
        Si no hay password configurado, devuelve None (hay que pedirlo).
        """
        entry = self.get(name)
        if entry is None or not entry.is_xtream:
            return None
        password = entry.password or ""
        if not password:
            return None
        return entry.server_url, entry.username, password

    def set_password(self, name: str, password: str) -> bool:
        """Establece/cambia la password de una entrada Xtream y la persiste.

        Solo Xtream: para M3U devuelve False y no hace nada.
        Devuelve True si la entrada existía y era Xtream.
        """
        return self.update_password(name, password)

    def update_password(self, name: str, new_password: str) -> bool:
        """Cambia la contraseña de una lista Xtream (guardada en el SecretStore).

        Solo aplica a kind="xtream". Invalida la cache de auth del proveedor
        para que la próxima apertura re-autentique con la nueva contraseña.
        Devuelve True si se actualizó, False si no existe o no es Xtream.
        """
        entries = self.load()
        found = False
        target: PlaylistEntry | None = None
        for e in entries:
            if e.name == name and e.is_xtream:
                e.password = new_password or ""
                found = True
                target = e
                break
        if not found or target is None:
            return False
        self._write(entries)
        self._store_password(target, new_password or "")
        # Invalidar cache de auth: la password vieja ya no vale.
        if target.server_url and target.username:
            try:
                from .xtream_provider import _clear_cache

                _clear_cache(target.server_url, target.username)
            except Exception:
                pass
        return True

    def remove(self, name: str) -> bool:
        """Elimina por nombre. Limpia cache Xtream y el secreto si aplica.

        Devuelve True si se eliminó algo.
        """
        entry = self.get(name)
        entries = self.load()
        kept = [e for e in entries if e.name != name]
        if len(kept) == len(entries):
            return False
        self._write(kept)
        # El secreto ya no tiene dueño: borrarlo del almacén.
        if entry is not None and entry.is_xtream:
            try:
                self._store().delete_password(self._secret_key(entry))
            except Exception:
                pass
            self._passwords.pop(self._secret_key(entry), None)
        # Limpiar cache Xtream si la entrada era Xtream
        if entry is not None and entry.is_xtream and entry.server_url and entry.username:
            try:
                from .xtream_provider import _clear_cache
                _clear_cache(entry.server_url, entry.username)
            except Exception:
                pass  # fallo silencioso: no afecta al usuario
        return True

    def get(self, name: str) -> PlaylistEntry | None:
        return next((e for e in self.load() if e.name == name), None)
