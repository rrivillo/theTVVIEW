"""Tests para xtream_provider y xtream_epg con fake server local."""

import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from thetvview.xtream_config import XtreamConfig
from thetvview.xtream_errors import AuthenticationError, NetworkError
from thetvview.models import Program
from thetvview.xtream_epg import short_epg_to_programs, merge_short_epg_into_programs
from thetvview.xtream_provider import (
    authenticate,
    get_live_categories,
    get_live_streams,
    get_vod_categories,
    get_vod_streams,
    get_series_categories,
    get_series,
    get_short_epg,
    make_stream_url,
)
from thetvview.xtream_security import normalize_server_url


# --- Fake Xtream Server -----------------------------------------------------

_FAKE_USER = "testuser"
_FAKE_PASS = "testpass"
_FAKE_TOKEN = "fake_server_info"


class FakeXtreamHandler(BaseHTTPRequestHandler):
    """Handler minimal que simula respuestas Xtream."""

    def log_message(self, format, *args):  # noqa: A002
        pass  # silenciar logs

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        action = params.get("action", [""])[0]
        username = params.get("username", [""])[0]
        password = params.get("password", [""])[0]

        # Auth check
        if username != _FAKE_USER or password != _FAKE_PASS:
            self._json_response(403, {"error": "Forbidden"})
            return

        if action == "auth":
            self._json_response(200, {
                "user_info": {
                    "username": _FAKE_USER,
                    "status": "Active",
                    "exp_date": "1700000000",
                },
                "server_info": {"url": "localhost"},
            })
        elif action == "get_live_categories":
            self._json_response(200, [
                {"category_id": "1", "name": "Sports", "parent_id": 0},
                {"category_id": "2", "name": "News", "parent_id": 0},
            ])
        elif action == "get_live_streams":
            cat = params.get("category_id", [None])[0]
            streams = [
                {"num": 1, "name": "ESPN", "stream_id": 101, "stream_type": "live",
                 "category_id": "1", "stream_icon": "http://logo.png"},
                {"num": 2, "name": "CNN", "stream_id": 102, "stream_type": "live",
                 "category_id": "2", "stream_icon": "http://cnn.png"},
                {"num": 3, "name": "Fox Sports", "stream_id": 103, "stream_type": "live",
                 "category_id": "1", "epg_channel_id": "fox.us"},
            ]
            if cat is not None:
                streams = [s for s in streams if s["category_id"] == cat]
            self._json_response(200, streams)
        elif action == "get_vod_categories":
            self._json_response(200, [
                {"category_id": "10", "name": "Action", "parent_id": 0},
            ])
        elif action == "get_vod_streams":
            self._json_response(200, [
                {"num": 1, "name": "Die Hard", "stream_id": 201, "stream_type": "movie",
                 "category_id": "10", "stream_icon": "http://dh.png",
                 "rating": "8.5", "plot": "A cop in a skyscraper"},
            ])
        elif action == "get_series_categories":
            self._json_response(200, [
                {"category_id": "20", "name": "Drama", "parent_id": 0},
            ])
        elif action == "get_series":
            self._json_response(200, [
                {"num": 1, "name": "Breaking Bad", "stream_id": 301,
                 "stream_type": "series", "category_id": "20"},
            ])
        elif action == "get_series_info":
            self._json_response(200, {
                "info": {"name": "Breaking Bad"},
                "seasons": [{"season_number": 1}],
                "episodes": {"1": [{"id": "e1", "title": "Pilot"}]},
            })
        elif action == "get_short_epg":
            self._json_response(200, {
                "epg_listings": [
                    {"title": base64_encode("Sports Center"),
                     "start": "2026-09-08T10:00:00Z",
                     "end": "2026-09-08T12:00:00Z",
                     "channel_id": "101"},
                ],
            })
        else:
            self._json_response(400, {"error": f"Unknown action: {action}"})

    def _json_response(self, code: int, data) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


def base64_encode(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()


class XtreamTestCase(unittest.TestCase):
    """Tests contra el fake server."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeXtreamHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _cfg(self, user: str = _FAKE_USER, pw: str = _FAKE_PASS) -> XtreamConfig:
        return XtreamConfig(server_url=self.base_url, username=user, password=pw)

    def test_auth_ok(self) -> None:
        info = authenticate(self._cfg())
        self.assertIn("user_info", info)
        self.assertEqual(info["user_info"]["status"], "Active")

    def test_auth_bad_credentials(self) -> None:
        with self.assertRaises(AuthenticationError):
            authenticate(self._cfg(user="wrong", pw="wrong"))

    def test_live_categories(self) -> None:
        cats = get_live_categories(self._cfg())
        self.assertEqual(len(cats), 2)
        self.assertEqual(cats[0].name, "Sports")

    def test_live_streams_all(self) -> None:
        streams = get_live_streams(self._cfg())
        self.assertEqual(len(streams), 3)

    def test_live_streams_by_category(self) -> None:
        streams = get_live_streams(self._cfg(), category_id="1")
        self.assertEqual(len(streams), 2)
        names = {s.name for s in streams}
        self.assertIn("ESPN", names)

    def test_vod_categories(self) -> None:
        cats = get_vod_categories(self._cfg())
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0].name, "Action")

    def test_vod_streams(self) -> None:
        streams = get_vod_streams(self._cfg())
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].name, "Die Hard")
        self.assertEqual(streams[0].rating, "8.5")

    def test_series_categories(self) -> None:
        cats = get_series_categories(self._cfg())
        self.assertEqual(len(cats), 1)

    def test_series(self) -> None:
        series = get_series(self._cfg())
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].name, "Breaking Bad")

    def test_short_epg(self) -> None:
        listings = get_short_epg(self._cfg(), "101")
        self.assertEqual(len(listings), 1)

    def test_make_stream_url(self) -> None:
        url = make_stream_url(self._cfg(), 101, "live", "ts")
        self.assertIn("/live/testuser/testpass/101.ts", url)

    def test_network_error_timeout(self) -> None:
        cfg = XtreamConfig(server_url="http://192.0.2.1:1", username="u", password="p")
        with self.assertRaises(NetworkError):
            authenticate(cfg, force_refresh=True)


class TestShortEpgAdapter(unittest.TestCase):
    def test_converts_listings(self) -> None:
        listings = [
            {"title": base64_encode("Show A"),
             "start": "2026-09-08T10:00:00Z",
             "end": "2026-09-08T11:00:00Z",
             "channel_id": "ch1"},
            {"title": "Show B",
             "start": "2026-09-08T12:00:00Z",
             "end": "2026-09-08T13:00:00Z",
             "channel_id": "ch1"},
        ]
        programs = short_epg_to_programs(listings)
        self.assertEqual(len(programs), 2)
        self.assertEqual(programs[0].title, "Show A")
        self.assertEqual(programs[0].channel_id, "ch1")
        self.assertIsNotNone(programs[0].stop)

    def test_skips_entries_without_start(self) -> None:
        listings = [{"title": "No start", "channel_id": "x"}]
        programs = short_epg_to_programs(listings)
        self.assertEqual(len(programs), 0)

    def test_merge_deduplicates(self) -> None:
        p1 = Program(channel_id="x", title="A", start=datetime(2026, 1, 1, tzinfo=timezone.utc))
        p2 = Program(channel_id="x", title="B", start=datetime(2026, 1, 1, tzinfo=timezone.utc))
        merged = merge_short_epg_into_programs([p1], [p2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "B")  # short_epg tiene prioridad


if __name__ == "__main__":
    unittest.main()
