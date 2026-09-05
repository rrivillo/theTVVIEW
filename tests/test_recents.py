import tempfile
import unittest
from pathlib import Path

from thetvview.models import Channel
from thetvview.recents import RecentsManager


class TestRecentsManager(unittest.TestCase):
    def test_empty_load(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json")
            self.assertEqual(rm.load(), [])

    def test_push_and_load_order(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json", max_items=5)
            ch1 = Channel(name="A", url="http://a", group="G1")
            ch2 = Channel(name="B", url="http://b")
            rm.push(ch1, player="mpv")
            rm.push(ch2, player="vlc")
            items = rm.load()
            self.assertEqual([i.url for i in items], ["http://b", "http://a"])
            self.assertEqual(items[0].player, "vlc")

    def test_push_deduplicates_by_url(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json")
            ch = Channel(name="A", url="http://a")
            rm.push(ch, player="mpv")
            rm.push(ch, player="vlc")
            items = rm.load()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].player, "vlc")

    def test_max_items_limit(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json", max_items=3)
            for i in range(5):
                rm.push(Channel(name=str(i), url=f"http://{i}"), player="mpv")
            self.assertEqual(len(rm.load()), 3)
            self.assertEqual(rm.load()[0].url, "http://4")

    def test_clear(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json")
            rm.push(Channel(name="A", url="http://a"), player="mpv")
            rm.clear()
            self.assertEqual(rm.load(), [])


if __name__ == "__main__":
    unittest.main()
