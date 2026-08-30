"""Tests mínimos para favorites (persistencia JSON en tmpdir)."""

import json
import tempfile
import unittest
from pathlib import Path

from thetvview.favorites import FavoritesError, FavoritesManager
from thetvview.models import Channel


class TestFavoritesManager(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "favorites.json"
        self.mgr = FavoritesManager(self.path)
        self.chan = Channel(name="La 1", url="http://example.com/la1.m3u8", group="General")

    def test_load_inexistente_da_lista_vacia(self) -> None:
        self.assertEqual(self.mgr.load(), [])

    def test_toggle_anade_y_quita(self) -> None:
        self.assertTrue(self.mgr.toggle(self.chan))  # añadido
        self.assertTrue(self.mgr.is_favorite(self.chan))
        self.assertFalse(self.mgr.toggle(self.chan))  # quitado
        self.assertFalse(self.mgr.is_favorite(self.chan))

    def test_persistencia_entre_instancias(self) -> None:
        self.mgr.toggle(self.chan)
        mgr2 = FavoritesManager(self.path)
        urls = [c.url for c in mgr2.load()]
        self.assertEqual(urls, [self.chan.url])

    def test_campos_se_conservan(self) -> None:
        chan = Channel(
            name="Radio 1",
            url="http://example.com/r1",
            tvg_id="r1.es",
            group="Radio",
            radio=True,
            extra_options=[("http-user-agent", "X")],
        )
        self.mgr.toggle(chan)
        got = FavoritesManager(self.path).load()[0]
        self.assertEqual(got.tvg_id, "r1.es")
        self.assertTrue(got.radio)
        self.assertEqual(got.extra_options, [("http-user-agent", "X")])

    def test_remove(self) -> None:
        self.mgr.toggle(self.chan)
        self.assertTrue(self.mgr.remove(self.chan))
        self.assertFalse(self.mgr.remove(self.chan))

    def test_json_corrupto_lanza_error_amigable(self) -> None:
        self.path.write_text("{no es json", encoding="utf-8")
        with self.assertRaises(FavoritesError):
            self.mgr.load()

    def test_formato_no_lista_lanza_error(self) -> None:
        self.path.write_text(json.dumps({"name": "x"}), encoding="utf-8")
        with self.assertRaises(FavoritesError):
            self.mgr.load()

    def test_entrada_malformada_se_ignora(self) -> None:
        payload = json.dumps([{"name": "sin url"}, {"name": "ok", "url": "u"}])
        self.path.write_text(payload, encoding="utf-8")
        names = [c.name for c in self.mgr.load()]
        self.assertEqual(names, ["ok"])


if __name__ == "__main__":
    unittest.main()
