"""Tests para xtream_models y normalización al dominio."""

import unittest

from thetvview.models import Channel
from thetvview.xtream_models import (
    ContentType,
    Movie,
    Series,
    XtreamPlaylist,
    build_category_map,
    normalize_live_stream,
    normalize_series_stream,
    normalize_vod_stream,
)
from thetvview.xtream_provider import XtreamCategory, XtreamStream


class TestContentType(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(ContentType.LIVE.value, "live")
        self.assertEqual(ContentType.VOD.value, "movie")
        self.assertEqual(ContentType.SERIES.value, "series")


class TestNormalizeLiveStream(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = XtreamStream(
            num=1,
            name="ESPN",
            stream_id=101,
            stream_type="live",
            stream_icon="http://logo.png",
            category_id="1",
            epg_channel_id="espn.us",
        )

    def test_returns_channel(self) -> None:
        ch = normalize_live_stream(self.stream, "http://x.com", "u", "p")
        self.assertIsInstance(ch, Channel)
        self.assertEqual(ch.name, "ESPN")
        self.assertIn("/live/u/p/101.ts", ch.url)

    def test_epg_channel_id_used_as_tvg_id(self) -> None:
        ch = normalize_live_stream(self.stream, "http://x.com", "u", "p")
        self.assertEqual(ch.tvg_id, "espn.us")

    def test_fallback_tvg_id_when_no_epg(self) -> None:
        self.stream.epg_channel_id = None
        ch = normalize_live_stream(self.stream, "http://x.com", "u", "p")
        self.assertEqual(ch.tvg_id, "xtream:101")

    def test_attrs_xtream_metadata(self) -> None:
        ch = normalize_live_stream(self.stream, "http://x.com", "u", "p")
        self.assertEqual(ch.attrs["xtream_id"], "101")
        self.assertEqual(ch.attrs["category_id"], "1")
        self.assertEqual(ch.attrs["content_type"], "live")

    def test_logo_assigned(self) -> None:
        ch = normalize_live_stream(self.stream, "http://x.com", "u", "p")
        self.assertEqual(ch.tvg_logo, "http://logo.png")


class TestNormalizeVodStream(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = XtreamStream(
            num=1,
            name="Die Hard",
            stream_id=201,
            stream_type="movie",
            stream_icon="http://dh.png",
            category_id="10",
            rating="8.5",
            plot="A cop in a skyscraper",
        )

    def test_returns_movie(self) -> None:
        m = normalize_vod_stream(self.stream, "http://x.com", "u", "p", "Action")
        self.assertIsInstance(m, Movie)
        self.assertEqual(m.name, "Die Hard")
        self.assertEqual(m.category, "Action")
        self.assertEqual(m.rating, "8.5")
        self.assertIn("/movie/u/p/201.mp4", m.url)

    def test_content_type_is_vod(self) -> None:
        m = normalize_vod_stream(self.stream, "http://x.com", "u", "p")
        self.assertEqual(m.content_type, ContentType.VOD)


class TestNormalizeSeriesStream(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = XtreamStream(
            num=1,
            name="Breaking Bad",
            stream_id=301,
            stream_type="series",
            category_id="20",
        )

    def test_returns_series(self) -> None:
        s = normalize_series_stream(self.stream, "Drama")
        self.assertIsInstance(s, Series)
        self.assertEqual(s.name, "Breaking Bad")
        self.assertEqual(s.category, "Drama")
        self.assertEqual(s.series_id, 301)


class TestBuildCategoryMap(unittest.TestCase):
    def test_map(self) -> None:
        cats = [
            XtreamCategory(category_id="1", name="Sports"),
            XtreamCategory(category_id="2", name="News"),
        ]
        m = build_category_map(cats)
        self.assertEqual(m, {"1": "Sports", "2": "News"})


class TestXtreamPlaylist(unittest.TestCase):
    def test_creation(self) -> None:
        pl = XtreamPlaylist(name="Test", server_url="http://x.com", username="u")
        self.assertEqual(pl.name, "Test")
        self.assertEqual(len(pl.channels), 0)
        self.assertEqual(len(pl.movies), 0)
        self.assertEqual(len(pl.series_list), 0)


if __name__ == "__main__":
    unittest.main()
