"""Tests del SecretStore y de los gaps B7/B12 (SDD §15, SEC-001)."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.playlist_manager import PlaylistEntry, PlaylistManager
from thetvview.security import secrets as secrets_mod
from thetvview.security.local_files import (
    PRIVATE_DIR_MODE,
    PRIVATE_FILE_MODE,
    chmod_private,
    chmod_private_tree,
)
from thetvview.security.secrets import (
    FallbackSecretStore,
    MemorySecretStore,
    SecretStoreError,
    get_store,
    reset_store,
)


def _file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _is_posix() -> bool:
    return os.name == "posix"


class _EnvIsolated(unittest.TestCase):
    """Base: fuerza SecretStore en memoria y restaura el entorno."""

    def setUp(self) -> None:
        super().setUp()
        self._old = os.environ.get("THETVVIEW_SECRET_STORE")
        os.environ["THETVVIEW_SECRET_STORE"] = "memory"
        reset_store()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._old is None:
            os.environ.pop("THETVVIEW_SECRET_STORE", None)
        else:
            os.environ["THETVVIEW_SECRET_STORE"] = self._old
        reset_store()


# ---------------------------------------------------------------------------
# MemorySecretStore / orquestación
# ---------------------------------------------------------------------------


class TestMemorySecretStore(_EnvIsolated):
    def test_set_get_delete(self) -> None:
        store = MemorySecretStore()
        self.assertIsNone(store.get_password("k"))
        store.set_password("k", "v")
        self.assertEqual(store.get_password("k"), "v")
        store.delete_password("k")
        self.assertIsNone(store.get_password("k"))

    def test_password_vacia_borra(self) -> None:
        store = MemorySecretStore()
        store.set_password("k", "v")
        store.set_password("k", "")
        self.assertIsNone(store.get_password("k"))

    def test_no_persiste_entre_instancias(self) -> None:
        a, b = MemorySecretStore(), MemorySecretStore()
        a.set_password("k", "v")
        self.assertIsNone(b.get_password("k"))
        self.assertFalse(a.persistent)

    def test_clear(self) -> None:
        store = MemorySecretStore()
        store.set_password("k", "v")
        store.clear()
        self.assertIsNone(store.get_password("k"))


class TestGetStore(_EnvIsolated):
    def test_memoria_por_entorno(self) -> None:
        self.assertIsInstance(get_store(), MemorySecretStore)

    def test_singleton(self) -> None:
        self.assertIs(get_store(), get_store())

    def test_reset_store(self) -> None:
        first = get_store()
        reset_store()
        self.assertIsNot(first, get_store())

    def test_fuerza_memoria_ignora_keyring(self) -> None:
        with mock.patch.object(secrets_mod, "_make_keyring_store", return_value=None):
            reset_store()
            self.assertIsInstance(get_store(), MemorySecretStore)


class TestFallbackSecretStore(_EnvIsolated):
    class _Boom:
        persistent = True

        def get_password(self, key: str) -> str | None:
            raise SecretStoreError("sin keyring")

        def set_password(self, key: str, password: str) -> None:
            raise SecretStoreError("sin keyring")

        def delete_password(self, key: str) -> None:
            raise SecretStoreError("sin keyring")

    def test_get_prueba_primario_y_luego_memoria(self) -> None:
        memory = MemorySecretStore()
        store = FallbackSecretStore(memory, memory)
        store.set_password("k", "v")
        self.assertEqual(store.get_password("k"), "v")

    def test_fallo_de_primario_no_rompe(self) -> None:
        memory = MemorySecretStore()
        store = FallbackSecretStore(self._Boom(), memory)
        store.set_password("k", "v")
        self.assertEqual(store.get_password("k"), "v")
        store.delete_password("k")
        self.assertIsNone(store.get_password("k"))

    def test_persistent_es_el_del_primario(self) -> None:
        self.assertTrue(FallbackSecretStore(self._Boom(), MemorySecretStore()).persistent)
        self.assertFalse(FallbackSecretStore(MemorySecretStore(), MemorySecretStore()).persistent)


# ---------------------------------------------------------------------------
# Backend nativo (opt-in: nunca se toca el keyring real sin pedirlo)
# ---------------------------------------------------------------------------

_RUN_NATIVE = os.environ.get("THETVVIEW_TEST_KEYRING") == "1"


@unittest.skipUnless(
    _RUN_NATIVE and sys.platform.startswith("linux") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
    "set THETVVIEW_TEST_KEYRING=1 con D-Bus activo para probar el keyring real",
)
class TestLibsecretBackend(_EnvIsolated):
    KEY = "thetvview-unittest"

    def test_roundtrip(self) -> None:
        store = secrets_mod._make_keyring_store()
        assert store is not None
        store.delete_password(self.KEY)
        store.set_password(self.KEY, "valor-de-prueba")
        try:
            self.assertEqual(store.get_password(self.KEY), "valor-de-prueba")
        finally:
            store.delete_password(self.KEY)
        self.assertIsNone(store.get_password(self.KEY))


# ---------------------------------------------------------------------------
# B7 — la password Xtream nunca se escribe en playlists.json
# ---------------------------------------------------------------------------


class TestPlaylistPasswordNotOnDisk(_EnvIsolated):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "playlists.json"
        self.mgr = PlaylistManager(self.path)

    def _raw(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_add_xtream_no_escribe_password(self) -> None:
        self.mgr.add_xtream("API", "http://x.com", "user1", "s3cret")
        raw = self._raw()
        self.assertNotIn("password", raw[0])
        self.assertNotIn("s3cret", self.path.read_text(encoding="utf-8"))

    def test_password_recuperable_en_memoria(self) -> None:
        self.mgr.add_xtream("API", "http://x.com", "user1", "s3cret")
        creds = self.mgr.get_credentials("API")
        self.assertEqual(creds, ("http://x.com", "user1", "s3cret"))

    def test_password_sobrevive_a_otra_instancia(self) -> None:
        self.mgr.add_xtream("API", "http://x.com", "user1", "s3cret")
        other = PlaylistManager(self.path)
        loaded = other.get("API")
        assert loaded is not None
        self.assertEqual(loaded.password, "s3cret")
        self.assertEqual(other.get_credentials("API"), ("http://x.com", "user1", "s3cret"))

    def test_to_dict_nunca_incluye_password(self) -> None:
        xtream = PlaylistEntry(
            name="A", source="xtream://http://x.com",
            kind="xtream", server_url="http://x.com", username="u", password="p",
        )
        self.assertNotIn("password", xtream.to_dict())
        m3u = PlaylistEntry(name="B", source="http://y/z.m3u", password="p")
        self.assertNotIn("password", m3u.to_dict())

    def test_update_password_no_escribe_password(self) -> None:
        self.mgr.add_xtream("API", "http://x.com", "user1", "old")
        self.assertTrue(self.mgr.update_password("API", "nueva"))
        self.assertNotIn("password", self._raw()[0])
        self.assertNotIn("nueva", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.mgr.get_credentials("API")[2], "nueva")

    def test_remove_borra_el_secreto(self) -> None:
        self.mgr.add_xtream("API", "http://x.com", "user1", "s3cret")
        key = self.mgr._secret_key(self.mgr.get("API"))  # type: ignore[arg-type]
        self.assertTrue(self.mgr.remove("API"))
        self.assertIsNone(get_store().get_password(key))
        self.assertIsNone(self.mgr.get_credentials("API"))

    def test_password_vacia_no_deja_secreto_huerfano(self) -> None:
        self.mgr.add_xtream("API", "http://x.com", "user1", "s3cret")
        self.mgr.add_xtream("Otra", "http://x.com", "user1", "")
        other = PlaylistManager(self.path)
        self.assertIsNone(other.get_credentials("Otra"))
        # La fuente con password sigue intacta.
        self.assertEqual(other.get_credentials("API")[2], "s3cret")

    def test_migracion_texto_plano_heredado(self) -> None:
        legacy = [{
            "name": "Vieja",
            "source": "xtream://http://x.com",
            "added": "2024-01-01T00:00:00+00:00",
            "kind": "xtream",
            "server_url": "http://x.com",
            "username": "user1",
            "password": "heredada",
        }]
        self.path.write_text(json.dumps(legacy), encoding="utf-8")

        loaded = self.mgr.load()
        self.assertEqual(loaded[0].password, "heredada")
        # El texto plano ya no está en disco, pero la password sigue usable.
        raw = self._raw()
        self.assertNotIn("password", raw[0])
        self.assertEqual(self.mgr.get_credentials("Vieja"), ("http://x.com", "user1", "heredada"))
        # Y en una instancia nueva también.
        self.assertEqual(
            PlaylistManager(self.path).get_credentials("Vieja"),
            ("http://x.com", "user1", "heredada"),
        )

    def test_m3u_nunca_toca_el_store(self) -> None:
        self.mgr.add("Mi Lista", "/tmp/a.m3u")
        self.assertNotIn("password", self._raw()[0])


# ---------------------------------------------------------------------------
# B12 — permisos 0600/0700 en ficheros con datos sensibles
# ---------------------------------------------------------------------------


@unittest.skipUnless(_is_posix(), "chmod no aplica en Windows")
class TestPrivatePermissions(_EnvIsolated):
    def test_chmod_private_fichero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.json"
            p.write_text("{}", encoding="utf-8")
            self.assertTrue(chmod_private(p))
            self.assertEqual(_file_mode(p), PRIVATE_FILE_MODE)

    def test_chmod_private_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "sub"
            d.mkdir()
            self.assertTrue(chmod_private(d, directory=True))
            self.assertEqual(_file_mode(d), PRIVATE_DIR_MODE)

    def test_chmod_private_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "cache"
            d.mkdir()
            (d / "a.json").write_text("{}", encoding="utf-8")
            self.assertTrue(chmod_private_tree(d))
            self.assertEqual(_file_mode(d), PRIVATE_DIR_MODE)
            self.assertEqual(_file_mode(d / "a.json"), PRIVATE_FILE_MODE)

    def test_chmod_sobre_inexistente_no_lanza(self) -> None:
        self.assertFalse(chmod_private("/ruta/que/no/existe/thetvview"))

    def test_playlists_json_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = PlaylistManager(Path(tmp) / "playlists.json")
            mgr.add("L", "/tmp/a.m3u")
            self.assertEqual(_file_mode(mgr.path), PRIVATE_FILE_MODE)

    def test_recents_json_0600(self) -> None:
        from thetvview.models import Channel
        from thetvview.recents import RecentsManager

        with tempfile.TemporaryDirectory() as tmp:
            rm = RecentsManager(Path(tmp) / "recents.json")
            rm.push(Channel(name="C1", url="http://x/live/u/p/1.ts"), "mpv")
            self.assertEqual(_file_mode(rm.path), PRIVATE_FILE_MODE)

    def test_xtream_cache_dir_y_ficheros_0600(self) -> None:
        from thetvview import config
        from thetvview import xtream_provider as xp

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "XTREAM_CACHE_DIR", Path(tmp) / "xtream_cache"):
                d = xp._cache_dir()
                self.assertEqual(_file_mode(d), PRIVATE_DIR_MODE)
                target = d / "abc123_auth.json"
                xp._write_cache(target, {"auth": True})
                self.assertEqual(_file_mode(target), PRIVATE_FILE_MODE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
