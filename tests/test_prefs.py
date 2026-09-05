import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview import config
from thetvview.models import Channel
from thetvview.prefs import Prefs, PrefsManager


class TestPrefsManager(unittest.TestCase):
    def test_load_missing_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            pm = PrefsManager(Path(td) / "prefs.json")
            self.assertEqual(pm.load(), Prefs())

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            pm = PrefsManager(Path(td) / "prefs.json")
            p = Prefs(last_player="vlc", last_group_sort="count", theme="dark")
            pm.save(p)
            self.assertEqual(pm.load(), p)

    def test_set_last_player(self):
        with tempfile.TemporaryDirectory() as td:
            pm = PrefsManager(Path(td) / "prefs.json")
            pm.set_last_player("mpv")
            self.assertEqual(pm.load().last_player, "mpv")

    def test_set_last_group_sort_valid(self):
        with tempfile.TemporaryDirectory() as td:
            pm = PrefsManager(Path(td) / "prefs.json")
            pm.set_last_group_sort("count")
            self.assertEqual(pm.load().last_group_sort, "count")

    def test_set_last_group_sort_ignored_for_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            pm = PrefsManager(Path(td) / "prefs.json")
            pm.set_last_group_sort("bad")
            self.assertEqual(pm.load().last_group_sort, "name")


class TestPrefsConfigPaths(unittest.TestCase):
    def test_paths_are_under_data_dir(self):
        self.assertTrue(str(config.PREFS_JSON).endswith("prefs.json"))
        self.assertTrue(str(config.RECENTS_JSON).endswith("recents.json"))
        self.assertEqual(config.PREFS_JSON.parent, config.DATA_DIR)
        self.assertEqual(config.RECENTS_JSON.parent, config.DATA_DIR)


if __name__ == "__main__":
    unittest.main()
