"""Tests de etiquetas de canal en la UI (marcadores de favorito y radio)."""

import unittest

from thetvview.models import Channel
from thetvview.ui.screens import ChannelsScreen, format_channel_name


def ch(name: str, group: str | None = None, radio: bool = False) -> Channel:
    return Channel(name=name, url=f"http://x/{name}", group=group, radio=radio)


class _Fav:
    def __init__(self, favs=()):
        self.favs = set(favs)

    def is_favorite(self, channel: Channel) -> bool:
        return channel.name in self.favs


class _StubApp:
    def __init__(self, favs=()):
        self.favorites = _Fav(favs)

    def body_height(self) -> int:
        return 10


class TestFormatChannelName(unittest.TestCase):
    def test_canal_normal(self):
        self.assertEqual(format_channel_name(ch("La 1")), "La 1")

    def test_con_grupo(self):
        self.assertEqual(
            format_channel_name(ch("La 1", "Noticias")), "[Noticias] La 1"
        )

    def test_favorito(self):
        self.assertEqual(format_channel_name(ch("La 1"), favorite=True), "★ La 1")

    def test_radio(self):
        self.assertEqual(format_channel_name(ch("Radio Pop", radio=True)), "♪ Radio Pop")

    def test_favorito_y_radio(self):
        out = format_channel_name(ch("R5", "Música", radio=True), favorite=True)
        self.assertEqual(out, "★ ♪ [Música] R5")


class TestLabelsInScreens(unittest.TestCase):
    def setUp(self):
        self.playlist = PlaylistFixture().playlist

    def test_lista_marca_radio_y_favorito(self):
        app = _StubApp(favs={"La 1"})
        screen = ChannelsScreen(app, self.playlist)
        labels = [screen._label_for(i) for i in range(len(screen.channels))]
        self.assertIn("★ [Noticias] La 1", labels)
        self.assertIn("♪ Radio Pop", labels)


class PlaylistFixture:
    def __init__(self):
        from thetvview.models import Playlist

        self.playlist = Playlist(
            name="Test",
            channels=[ch("La 1", "Noticias"), ch("Radio Pop", radio=True)],
        )


if __name__ == "__main__":
    unittest.main()
