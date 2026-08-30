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


if __name__ == "__main__":
    unittest.main()
