"""Tests para módulos Foundation de Xtream: errors, client, security, config."""

import unittest

from thetvview.xtream_errors import (
    AuthenticationError,
    InvalidSourceError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnsupportedProviderError,
)
from thetvview.xtream_security import (
    build_api_url,
    build_stream_url,
    normalize_server_url,
    redact_secret,
    validate_credentials,
)
from thetvview.xtream_config import XtreamConfig


class TestXtreamErrors(unittest.TestCase):
    def test_hierarchy(self) -> None:
        self.assertTrue(issubclass(AuthenticationError, ProviderError))
        self.assertTrue(issubclass(InvalidSourceError, ProviderError))
        self.assertTrue(issubclass(NetworkError, ProviderError))
        self.assertTrue(issubclass(RateLimitError, ProviderError))
        self.assertTrue(issubclass(UnsupportedProviderError, ProviderError))

    def test_catch_all_with_provider_error(self) -> None:
        with self.assertRaises(ProviderError):
            raise AuthenticationError("bad creds")


class TestRedactSecret(unittest.TestCase):
    def test_redact_password_in_url(self) -> None:
        url = "http://x.com/player_api.php?user=a&password=secret123"
        result = redact_secret(url)
        self.assertEqual(result, "http://x.com/player_api.php?user=a&password=***")

    def test_no_password_unchanged(self) -> None:
        url = "http://x.com/player_api.php?user=a"
        self.assertEqual(redact_secret(url), url)

    def test_multiple_params(self) -> None:
        url = "http://x.com/player_api.php?username=u&password=p&action=auth"
        result = redact_secret(url)
        self.assertIn("password=***", result)
        self.assertIn("username=u", result)
        self.assertIn("action=auth", result)


class TestNormalizeServerUrl(unittest.TestCase):
    def test_strips_trailing_slash(self) -> None:
        self.assertEqual(normalize_server_url("http://x.com/"), "http://x.com")

    def test_adds_http_if_missing(self) -> None:
        self.assertEqual(normalize_server_url("xtream.example.com"), "http://xtream.example.com")

    def test_preserves_https(self) -> None:
        self.assertEqual(normalize_server_url("https://x.com"), "https://x.com")

    def test_preserves_port(self) -> None:
        self.assertEqual(normalize_server_url("http://x.com:8080/"), "http://x.com:8080")

    def test_strips_path(self) -> None:
        self.assertEqual(normalize_server_url("http://x.com/some/path"), "http://x.com")

    def test_empty_raises(self) -> None:
        with self.assertRaises(InvalidSourceError):
            normalize_server_url("")

    def test_whitespace_only_raises(self) -> None:
        with self.assertRaises(InvalidSourceError):
            normalize_server_url("   ")


class TestValidateCredentials(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(validate_credentials("http://x.com", "user"), [])

    def test_empty_server(self) -> None:
        errors = validate_credentials("", "user")
        self.assertEqual(len(errors), 1)
        self.assertIn("servidor", errors[0])

    def test_empty_username(self) -> None:
        errors = validate_credentials("http://x.com", "")
        self.assertEqual(len(errors), 1)
        self.assertIn("usuario", errors[0])

    def test_both_empty(self) -> None:
        errors = validate_credentials("", "")
        self.assertEqual(len(errors), 2)


class TestBuildApiUrl(unittest.TestCase):
    def test_builds_correctly(self) -> None:
        url = build_api_url("http://x.com", "user1", "pass1", "auth")
        self.assertEqual(url, "http://x.com/player_api.php?username=user1&password=pass1&action=auth")

    def test_normalizes_server(self) -> None:
        url = build_api_url("http://x.com/", "u", "p", "get_live")
        self.assertIn("http://x.com/player_api.php", url)


class TestBuildStreamUrl(unittest.TestCase):
    def test_live_stream(self) -> None:
        url = build_stream_url("http://x.com", "u", "p", 123, "live", "ts")
        self.assertEqual(url, "http://x.com/live/u/p/123.ts")

    def test_movie_stream(self) -> None:
        url = build_stream_url("http://x.com", "u", "p", 456, "movie", "mp4")
        self.assertEqual(url, "http://x.com/movie/u/p/456.mp4")


class TestXtreamConfig(unittest.TestCase):
    def test_creation(self) -> None:
        cfg = XtreamConfig(server_url="http://x.com", username="u", password="p")
        self.assertEqual(cfg.server_url, "http://x.com")
        self.assertEqual(cfg.username, "u")
        self.assertEqual(cfg.password, "p")

    def test_default_password_empty(self) -> None:
        cfg = XtreamConfig(server_url="http://x.com", username="u")
        self.assertEqual(cfg.password, "")


if __name__ == "__main__":
    unittest.main()
