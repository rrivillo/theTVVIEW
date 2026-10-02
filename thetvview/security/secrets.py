"""Almacenamiento de contraseñas sin dependencias pip (SDD §15).

Objetivo: la contraseña Xtream **se guarda** (no se vuelve a pedir en esta
máquina) y **nunca** acaba en ``playlists.json`` en texto plano.

Diseño::

    SecretStore (Protocol): get/set/delete_password
    ├── FallbackSecretStore   ← orquesta: keyring primero, memoria después
    │   ├── KeyringSecretStore ← persistente, cifra el SO
    │   │   ├── Linux     ctypes → libsecret-1 (Secret Service: GNOME Keyring/KWallet)
    │   │   ├── macOS     ctypes → Security.framework (SecItem, generic password)
    │   │   └── Windows   ctypes → crypt32 (DPAPI, CryptProtectData)
    │   └── MemorySecretStore  ← sesión actual / CI / sin keyring

Nada de criptografía artesanal: en Linux/macOS el secreto vive en el
almacén del escritorio y en Windows lo cifra DPAPI para el usuario actual.
Cero dependencias pip, cero ``shell=True``.

Variables de entorno:

- ``THETVVIEW_SECRET_STORE=memory``  → solo memoria (tests y CI).
- ``THETVVIEW_SECRET_STORE=keyring`` → fuerza keyring y **no** degrada.
- cualquier otro valor / ausente    → auto (keyring con fallback a memoria).
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import json
import os
import sys
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

# Nombre del "servicio" (campo Service en el keyring) y del atributo que
# identifica la fuente dentro del esquema libsecret.
SERVICE: str = "org.thetvview.credentials"
SCHEMA_NAME: str = "org.thetvview.credentials"
ATTRIBUTE: str = "source"


@runtime_checkable
class SecretStore(Protocol):
    """Interfaz mínima de un almacén de secretos."""

    #: True si lo escrito sobrevive a un reinicio del proceso.
    persistent: bool

    def get_password(self, key: str) -> str | None: ...

    def set_password(self, key: str, password: str) -> None: ...

    def delete_password(self, key: str) -> None: ...


class SecretStoreError(Exception):
    """Fallo interno del almacén (siempre degrada, nunca rompe la app)."""


# ---------------------------------------------------------------------------
# Memoria (tests, CI, máquinas sin keyring)
# ---------------------------------------------------------------------------


class MemorySecretStore:
    """Almacén en RAM: no persiste entre procesos. Siempre disponible."""

    persistent = False

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def get_password(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def set_password(self, key: str, password: str) -> None:
        with self._lock:
            if password:
                self._data[key] = password
            else:
                self._data.pop(key, None)

    def delete_password(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# ---------------------------------------------------------------------------
# Linux: libsecret-1 (Secret Service vía D-Bus)
# ---------------------------------------------------------------------------


class _GError(ctypes.Structure):
    _fields_ = [
        ("domain", ctypes.c_uint32),
        ("code", ctypes.c_int),
        ("message", ctypes.c_char_p),
    ]


class _SecretSchemaAttribute(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("type", ctypes.c_int)]


class _SecretSchema(ctypes.Structure):
    # Reproduce struct _SecretSchema de libsecret/secret-schema.h (0.21):
    # name, flags, attributes[32] (arreglo embebido, NO puntero), reserved,
    # reserved1..reserved7.
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("flags", ctypes.c_int),
        ("attributes", _SecretSchemaAttribute * 32),
        ("reserved", ctypes.c_int),
        *[(f"reserved{i}", ctypes.c_void_p) for i in range(1, 8)],
    ]


_SECRET_SCHEMA_ATTRIBUTE_STRING = 0


class _LibsecretStore:
    """Backend Linux basado en libsecret (GNOME Keyring / KWallet vía SS)."""

    persistent = True

    def __init__(self) -> None:
        libname = ctypes.util.find_library("secret-1") or "libsecret-1.so.0"
        self._lib = ctypes.CDLL(libname)
        glib = ctypes.util.find_library("glib-2.0") or "libglib-2.0.so.0"
        self._glib = ctypes.CDLL(glib)

        # Firmas reales de libsecret >= 0.21 (sin SecretSchemaFlags y con
        # GCancellable). Los varargs son pares atributo/valor terminados en
        # NULL. Declarar argtypes evita desalineación de argumentos (segfault).
        schema_p = ctypes.POINTER(_SecretSchema)
        gerr_p = ctypes.POINTER(ctypes.POINTER(_GError))
        self._lib.secret_password_store_sync.restype = ctypes.c_int
        self._lib.secret_password_store_sync.argtypes = [
            schema_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_void_p, gerr_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_void_p,
        ]
        self._lib.secret_password_lookup_sync.restype = ctypes.c_void_p
        self._lib.secret_password_lookup_sync.argtypes = [
            schema_p, ctypes.c_void_p, gerr_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_void_p,
        ]
        self._lib.secret_password_clear_sync.restype = ctypes.c_int
        self._lib.secret_password_clear_sync.argtypes = [
            schema_p, ctypes.c_void_p, gerr_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_void_p,
        ]
        self._glib.g_free.restype = None
        self._glib.g_free.argtypes = [ctypes.c_void_p]
        self._glib.g_error_free.restype = None
        self._glib.g_error_free.argtypes = [ctypes.POINTER(_GError)]

        schema = _SecretSchema()
        schema.name = SCHEMA_NAME.encode("utf-8")
        schema.flags = 0
        schema.attributes[0].name = ATTRIBUTE.encode("utf-8")
        schema.attributes[0].type = _SECRET_SCHEMA_ATTRIBUTE_STRING
        schema.attributes[1].name = None
        schema.attributes[1].type = 0
        self._schema = schema  # mantener vivo

    # -- helpers ------------------------------------------------------------

    def _call_store(self, key: str, password: str) -> None:
        err = ctypes.POINTER(_GError)()
        ok = self._lib.secret_password_store_sync(
            ctypes.byref(self._schema),
            None,  # colección por defecto
            f"theTVVIEW · {key}".encode("utf-8"),
            password.encode("utf-8"),
            None,  # GCancellable
            ctypes.byref(err),
            ATTRIBUTE.encode("utf-8"),
            key.encode("utf-8"),
            None,  # fin de varargs
        )
        if not ok:
            raise SecretStoreError(self._take_error(err))

    def _call_lookup(self, key: str) -> str | None:
        err = ctypes.POINTER(_GError)()
        ptr = self._lib.secret_password_lookup_sync(
            ctypes.byref(self._schema),
            None,  # GCancellable
            ctypes.byref(err),
            ATTRIBUTE.encode("utf-8"),
            key.encode("utf-8"),
            None,
        )
        if not ptr:
            if err:
                raise SecretStoreError(self._take_error(err))
            return None
        try:
            return ctypes.string_at(ptr).decode("utf-8", "replace")
        finally:
            self._glib.g_free(ptr)

    def _call_clear(self, key: str) -> None:
        err = ctypes.POINTER(_GError)()
        self._lib.secret_password_clear_sync(
            ctypes.byref(self._schema),
            None,  # GCancellable
            ctypes.byref(err),
            ATTRIBUTE.encode("utf-8"),
            key.encode("utf-8"),
            None,
        )
        # Un fallo aquí no es grave: la entrada puede no existir.
        if err:
            raise SecretStoreError(self._take_error(err))

    def _take_error(self, err) -> str:  # noqa: ANN001 - ctypes pointer
        try:
            msg = (err.contents.message or b"").decode("utf-8", "replace")
        except Exception:
            msg = "error de keyring"
        try:
            self._glib.g_error_free(err)
        except Exception:
            pass
        return msg or "error de keyring"

    # -- SecretStore --------------------------------------------------------

    def get_password(self, key: str) -> str | None:
        return self._call_lookup(key)

    def set_password(self, key: str, password: str) -> None:
        if not password:
            self.delete_password(key)
            return
        self._call_store(key, password)

    def delete_password(self, key: str) -> None:
        self._call_clear(key)


# ---------------------------------------------------------------------------
# macOS: Security.framework (SecItem*, generic password)
# ---------------------------------------------------------------------------


class _MacKeychainStore:
    """Backend macOS basado en el keychain del usuario (SecItem API)."""

    persistent = True

    _KCF_STRING_UTF8 = 0x08000100
    _ERR_SEC_DUPLICATE = -25299

    def __init__(self) -> None:
        self._sec = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        self._cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )

        cf = self._cf
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        ]
        cf.CFDataCreate.restype = ctypes.c_void_p
        cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
        ]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        sec = self._sec
        sec.SecItemAdd.restype = ctypes.c_int32
        sec.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        sec.SecItemUpdate.restype = ctypes.c_int32
        sec.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        sec.SecItemCopyMatching.restype = ctypes.c_int32
        sec.SecItemCopyMatching.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        sec.SecItemDelete.restype = ctypes.c_int32
        sec.SecItemDelete.argtypes = [ctypes.c_void_p]

        # Símbolos globales de CoreFoundation necesarios como valores.
        self._key_cb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
        self._val_cb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
        self._true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        self._keep: list[int] = []

    # -- helpers CF ---------------------------------------------------------

    def _cfstr(self, value: str) -> int:
        ptr = self._cf.CFStringCreateWithCString(
            None, value.encode("utf-8"), self._KCF_STRING_UTF8
        )
        if not ptr:
            raise SecretStoreError("CFStringCreateWithCString falló")
        return ptr

    def _cfdata(self, value: bytes) -> int:
        buf = ctypes.create_string_buffer(value)
        ptr = self._cf.CFDataCreate(None, ctypes.cast(buf, ctypes.c_void_p), len(value))
        if not ptr:
            raise SecretStoreError("CFDataCreate falló")
        return ptr

    def _dict(self, pairs: list[tuple[str, object]]) -> int:
        keys = (ctypes.c_void_p * len(pairs))()
        values = (ctypes.c_void_p * len(pairs))()
        keep: list[int] = []
        try:
            for i, (key, value) in enumerate(pairs):
                k = self._cfstr(key)
                keep.append(k)
                keys[i] = k
                if isinstance(value, str):
                    v = self._cfstr(value)
                elif isinstance(value, bytes):
                    v = self._cfdata(value)
                elif isinstance(value, bool):
                    # kCFBooleanTrue / kCFBooleanFalse (puntero de CF).
                    true_ptr = getattr(self._true, "value", None) or 0
                    v = int(true_ptr) if value else 0
                elif isinstance(value, int):
                    v = int(value)
                else:
                    v = 0
                keep.append(v)
                values[i] = v
            d = self._cf.CFDictionaryCreate(
                None, keys, values, len(pairs), self._key_cb, self._val_cb
            )
        finally:
            # CFDictionary retiene claves/valores: ya podemos soltarlas.
            for ptr in keep:
                self._cf.CFRelease(ptr)
        if not d:
            raise SecretStoreError("CFDictionaryCreate falló")
        self._keep.append(d)
        return d

    def _release_all(self) -> None:
        for ptr in self._keep:
            try:
                self._cf.CFRelease(ptr)
            except Exception:
                pass
        self._keep = []

    def _base_pairs(self, key: str) -> list[tuple[str, object]]:
        return [("class", "genp"), ("acct", key), ("svce", SERVICE)]

    # -- SecretStore --------------------------------------------------------

    def get_password(self, key: str) -> str | None:
        self._keep = []
        try:
            query = self._dict(
                self._base_pairs(key)
                + [("returnattributes", True), ("returndata", True)]
            )
            result = ctypes.c_void_p()
            status = self._sec.SecItemCopyMatching(query, ctypes.byref(result))
            if status != 0 or not result.value:
                return None
            try:
                data = result.value
                length = int(self._cf.CFDataGetLength(data) or 0)
                if length <= 0:
                    return None
                raw_ptr = self._cf.CFDataGetBytePtr(data)
                if not raw_ptr:
                    return None
                return ctypes.string_at(raw_ptr, length).decode("utf-8", "replace")
            finally:
                self._cf.CFRelease(result.value)
        finally:
            self._release_all()

    def set_password(self, key: str, password: str) -> None:
        if not password:
            self.delete_password(key)
            return
        self._keep = []
        try:
            attrs = self._dict(
                self._base_pairs(key)
                + [
                    ("label", f"theTVVIEW - {key}"),
                    ("pdata", password.encode("utf-8")),
                ]
            )
            status = self._sec.SecItemAdd(attrs, None)
            if status == self._ERR_SEC_DUPLICATE:
                query = self._dict(self._base_pairs(key))
                update = self._dict([("pdata", password.encode("utf-8"))])
                status = self._sec.SecItemUpdate(query, update)
            if status != 0:
                raise SecretStoreError(f"SecItem devolvió {status}")
        finally:
            self._release_all()

    def delete_password(self, key: str) -> None:
        self._keep = []
        try:
            query = self._dict(self._base_pairs(key))
            self._sec.SecItemDelete(query)
        finally:
            self._release_all()


# ---------------------------------------------------------------------------
# Windows: DPAPI (crypt32) + fichero local cifrado
# ---------------------------------------------------------------------------


class _WindowsDpapiStore:
    """Backend Windows: CryptProtectData (DPAPI) + fichero en data/.

    DPAPI enlaza el blob al usuario y a la máquina: otro usuario o copia del
    fichero fuera de la sesión no puede descifrarlo. Es el mismo mecanismo
    que usan los navegantes y el gestor de credenciales de Windows.
    """

    persistent = True

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]

    def __init__(self, path: Path) -> None:
        self._crypt32 = ctypes.WinDLL("crypt32")  # type: ignore[attr-defined]
        self._kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        self._crypt32.CryptProtectData.restype = ctypes.c_int
        self._crypt32.CryptUnprotectData.restype = ctypes.c_int
        self._path = path

    def _protect(self, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data)
        inbuf = self._DataBlob(len(data), ctypes.cast(buf, ctypes.c_void_p))
        out = self._DataBlob()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(inbuf), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise SecretStoreError("CryptProtectData falló")
        try:
            raw = ctypes.string_at(out.pbData, out.cbData)
        finally:
            self._kernel32.LocalFree(out.pbData)
        return raw

    def _unprotect(self, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data)
        inbuf = self._DataBlob(len(data), ctypes.cast(buf, ctypes.c_void_p))
        out = self._DataBlob()
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(inbuf), None, None, None, None, 0, ctypes.byref(out)
        ):
            raise SecretStoreError("CryptUnprotectData falló")
        try:
            raw = ctypes.string_at(out.pbData, out.cbData)
        finally:
            self._kernel32.LocalFree(out.pbData)
        return raw

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def get_password(self, key: str) -> str | None:
        blob = self._load().get(key)
        if not blob:
            return None
        try:
            raw = self._unprotect(base64.b64decode(blob))
        except Exception:
            return None
        return raw.decode("utf-8", "replace")

    def set_password(self, key: str, password: str) -> None:
        data = self._load()
        if not password:
            data.pop(key, None)
        else:
            data[key] = base64.b64encode(
                self._protect(password.encode("utf-8"))
            ).decode("ascii")
        self._save(data)

    def delete_password(self, key: str) -> None:
        data = self._load()
        if key in data:
            data.pop(key, None)
            self._save(data)


# ---------------------------------------------------------------------------
# Keyring selectivo + orquestación
# ---------------------------------------------------------------------------


def _make_keyring_store() -> SecretStore | None:
    """Devuelve el backend nativo del SO, o None si no está disponible."""
    system = sys.platform
    try:
        if system.startswith("linux"):
            if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
                return None
            return _LibsecretStore()
        if system == "darwin":
            return _MacKeychainStore()
        if system in ("win32", "cygwin"):
            from thetvview import config as _config

            return _WindowsDpapiStore(_config.DATA_DIR / "secrets.dpapi.json")
    except Exception:
        return None
    return None


class FallbackSecretStore:
    """Keyring primario + memoria como red de seguridad.

    - ``get``: keyring y, si no tiene la clave, memoria de la sesión.
    - ``set``: escribe siempre en memoria (la sesión funciona hoy mismo) y
      además, en best-effort, en el keyring (persistencia entre sesiones).
    - ``delete``: limpia los dos.
    """

    def __init__(self, primary: SecretStore, fallback: MemorySecretStore) -> None:
        self.primary = primary
        self.fallback = fallback
        self.persistent: bool = bool(getattr(primary, "persistent", False))

    def get_password(self, key: str) -> str | None:
        try:
            value = self.primary.get_password(key)
        except Exception:
            value = None
        if value is None:
            value = self.fallback.get_password(key)
        return value

    def set_password(self, key: str, password: str) -> None:
        self.fallback.set_password(key, password)
        try:
            self.primary.set_password(key, password)
        except Exception:
            pass

    def delete_password(self, key: str) -> None:
        self.fallback.delete_password(key)
        try:
            self.primary.delete_password(key)
        except Exception:
            pass


_STORE_LOCK = threading.Lock()
_STORE: SecretStore | None = None


def get_store() -> SecretStore:
    """Devuelve el almacén de secretos activo (singleton por proceso).

    - ``THETVVIEW_SECRET_STORE=memory`` → solo memoria (tests y CI).
    - ``THETVVIEW_SECRET_STORE=keyring`` → exige el nativo; si no existe se
      queda en memoria (nunca se guarda en claro en disco).
    - ausente/otro → auto: keyring con memoria como red de seguridad.
    """
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        mode = (os.environ.get("THETVVIEW_SECRET_STORE") or "auto").strip().lower()
        memory = MemorySecretStore()
        if mode == "memory":
            _STORE = memory
            return _STORE
        primary = _make_keyring_store()
        _STORE = (
            FallbackSecretStore(primary, memory) if primary is not None else memory
        )
        return _STORE


def reset_store() -> None:
    """Descarta el singleton (solo para tests)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
