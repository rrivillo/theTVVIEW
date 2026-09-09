"""Tests mínimos para playlist_manager (persistencia JSON en tmpdir)."""

import json
import tempfile
import unittest
from pathlib import Path

from thetvview.playlist_manager import PlaylistError, PlaylistManager


class TestPlaylistManager(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "playlists.json"
        self.mgr = PlaylistManager(self.path)

    def test_load_inexistente_da_lista_vacia(self) -> None:
        self.assertEqual(self.mgr.load(), [])

    def test_add_y_get(self) -> None:
        entry = self.mgr.add("Mi Lista", "http://example.com/lista.m3u8")
        self.assertEqual(entry.name, "Mi Lista")
        got = self.mgr.get("Mi Lista")
        assert got is not None
        self.assertEqual(got.source, "http://example.com/lista.m3u8")
        self.assertTrue(got.added)  # fecha ISO rellenada

    def test_add_duplicado_falla(self) -> None:
        self.mgr.add("dup", "/tmp/a.m3u")
        with self.assertRaises(PlaylistError):
            self.mgr.add("dup", "/tmp/b.m3u")

    def test_add_campos_vacios_falla(self) -> None:
        with self.assertRaises(PlaylistError):
            self.mgr.add("", "/tmp/a.m3u")

    def test_persistencia_entre_instancias(self) -> None:
        self.mgr.add("otra", "https://x.example.com/epg.xml")
        mgr2 = PlaylistManager(self.path)
        names = [e.name for e in mgr2.load()]
        self.assertEqual(names, ["otra"])

    def test_remove(self) -> None:
        self.mgr.add("borrable", "/tmp/x.m3u")
        self.assertTrue(self.mgr.remove("borrable"))
        self.assertFalse(self.mgr.remove("borrable"))  # ya no existe

    def test_json_corrupto_lanza_error_amigable(self) -> None:
        self.path.write_text("{no es json", encoding="utf-8")
        with self.assertRaises(PlaylistError):
            self.mgr.load()

    def test_formato_no_lista_lanza_error(self) -> None:
        self.path.write_text(json.dumps({"name": "x"}), encoding="utf-8")
        with self.assertRaises(PlaylistError):
            self.mgr.load()

    def test_entrada_malformada_se_ignora(self) -> None:
        payload = json.dumps([{"source": "sin_name"}, {"name": "ok", "source": "s"}])
        self.path.write_text(payload, encoding="utf-8")
        names = [e.name for e in self.mgr.load()]
        self.assertEqual(names, ["ok"])

    # --- Xtream support ---

    def test_add_xtream(self) -> None:
        entry = self.mgr.add_xtream("Mi Xtream", "http://x.com", "user1", "pass1")
        self.assertEqual(entry.kind, "xtream")
        self.assertEqual(entry.server_url, "http://x.com")
        self.assertEqual(entry.username, "user1")
        self.assertTrue(entry.is_xtream)
        # Password is in-memory only, accessible via get_credentials
        creds = self.mgr.get_credentials("Mi Xtream")
        self.assertEqual(creds, ("http://x.com", "user1", "pass1"))

    def test_add_xtream_persists_without_password(self) -> None:
        self.mgr.add_xtream("X1", "http://x.com", "u", "secret")
        mgr2 = PlaylistManager(self.path)
        loaded = mgr2.get("X1")
        assert loaded is not None
        self.assertEqual(loaded.server_url, "http://x.com")
        self.assertEqual(loaded.username, "u")
        self.assertEqual(loaded.password, "")  # password not persisted

    def test_add_xtream_duplicate_fails(self) -> None:
        self.mgr.add_xtream("dup", "http://x.com", "u", "p")
        with self.assertRaises(PlaylistError):
            self.mgr.add_xtream("dup", "http://y.com", "u2", "p2")

    def test_add_xtream_empty_fields_fail(self) -> None:
        with self.assertRaises(PlaylistError):
            self.mgr.add_xtream("", "http://x.com", "u", "p")
        with self.assertRaises(PlaylistError):
            self.mgr.add_xtream("X", "", "u", "p")

    def test_set_password(self) -> None:
        self.mgr.add_xtream("X1", "http://x.com", "u", "")
        self.assertTrue(self.mgr.set_password("X1", "secret"))
        # Simular recarga: password se pierde
        mgr2 = PlaylistManager(self.path)
        mgr2.set_password("X1", "restored")
        creds = mgr2.get_credentials("X1")
        self.assertIsNotNone(creds)
        self.assertEqual(creds[2], "restored")

    def test_get_credentials_none_for_m3u(self) -> None:
        self.mgr.add("M3U", "/tmp/x.m3u")
        self.assertIsNone(self.mgr.get_credentials("M3U"))

    def test_get_credentials_none_without_password(self) -> None:
        self.mgr.add_xtream("X1", "http://x.com", "u", "")
        self.assertIsNone(self.mgr.get_credentials("X1"))

    def test_get_credentials_returns_tuple(self) -> None:
        self.mgr.add_xtream("X1", "http://x.com", "u", "p")
        creds = self.mgr.get_credentials("X1")
        self.assertEqual(creds, ("http://x.com", "u", "p"))

    def test_mixed_m3u_and_xtream(self) -> None:
        self.mgr.add("M3U List", "/tmp/x.m3u")
        self.mgr.add_xtream("Xtream Src", "http://x.com", "u", "p")
        entries = self.mgr.load()
        self.assertEqual(len(entries), 2)
        kinds = {e.kind for e in entries}
        self.assertEqual(kinds, {"m3u", "xtream"})

    def test_old_entries_without_kind_load_as_m3u(self) -> None:
        # Simular formato viejo sin campo 'kind'
        payload = json.dumps([{"name": "Old", "source": "/tmp/old.m3u", "added": "2026-01-01T00:00:00+00:00"}])
        self.path.write_text(payload, encoding="utf-8")
        entries = self.mgr.load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "m3u")

    def test_remove_xtream_cleans_cache(self) -> None:
        import os
        entry = self.mgr.add_xtream("XC", "http://x.com", "user1", "pass1")
        # Create a fake cache file
        from thetvview.xtream_provider import _cache_key, _cache_path
        key = _cache_key("http://x.com", "user1")
        cp = _cache_path(key, "auth")
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text("{}", encoding="utf-8")
        self.assertTrue(cp.exists())
        self.mgr.remove("XC")
        self.assertFalse(cp.exists())


if __name__ == "__main__":
    unittest.main()
