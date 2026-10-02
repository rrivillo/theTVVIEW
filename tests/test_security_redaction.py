"""Tests de redacción de secretos (SDD §16, gap B10) — SEC-001."""

from __future__ import annotations

import unittest

from thetvview.security.redaction import (
    REDACTED,
    SecretStr,
    contains_embedded_login,
    contains_password_param,
    contains_secret,
    needs_redaction_for_storage,
    redact_exception,
    redact_iterable,
    redact_mapping,
    redact_text,
    redact_url,
)


class TestRedactText(unittest.TestCase):
    def test_query_password(self) -> None:
        url = "http://x/player_api.php?user=a&password=secret123"
        out = redact_text(url)
        self.assertIn("password=***", out)
        self.assertNotIn("secret123", out)

    def test_username_se_conserva(self) -> None:
        # Compatibilidad: el usuario debe poder ver su propio usuario.
        out = redact_text("http://x/player_api.php?username=u&password=p")
        self.assertIn("username=u", out)
        self.assertNotIn("password=p&", out)

    def test_query_keys_sensibles(self) -> None:
        for key in (
            "password",
            "pass",
            "pwd",
            "passwd",
            "token",
            "access_token",
            "api_key",
            "apikey",
            "auth",
            "authorization",
            "secret",
            "session",
        ):
            with self.subTest(key=key):
                out = redact_text(f"http://x/?{key}=VALUE123")
                self.assertNotIn("VALUE123", out)

    def test_query_no_sensible_no_se_toca(self) -> None:
        url = "http://x/?action=auth&username=bob"
        self.assertEqual(redact_text(url), url)

    def test_userinfo_url(self) -> None:
        out = redact_text("rtsp://alice:s3cr3t@cam.local:554/live")
        self.assertNotIn("s3cr3t", out)
        self.assertTrue(out.startswith("rtsp://***@"))

    def test_path_xtream(self) -> None:
        for kind in ("live", "movie", "series"):
            with self.subTest(kind=kind):
                url = f"http://x/{kind}/user1/superpass/101.ts"
                out = redact_text(url)
                self.assertNotIn("superpass", out)
                self.assertIn(f"/{kind}/{REDACTED}/{REDACTED}/", out)

    def test_authorization_header(self) -> None:
        out = redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", out)
        self.assertIn(REDACTED, out)

    def test_basic_auth_header(self) -> None:
        out = redact_text("authorization=Basic dXNlcjpwYXNzd29yZA==")
        self.assertNotIn("dXNlcjpwYXNzd29yZA==", out)

    def test_cookie_header(self) -> None:
        out = redact_text("Cookie: sessionid=tok123; theme=dark")
        self.assertNotIn("tok123", out)

    def test_texto_vacio(self) -> None:
        self.assertEqual(redact_text(""), "")

    def test_idempotente(self) -> None:
        url = "http://x/?password=abc&token=xyz"
        once = redact_text(url)
        self.assertEqual(redact_text(once), once)

    def test_redact_url_es_redact_text(self) -> None:
        self.assertEqual(redact_url("http://x/?password=abc"), redact_text("http://x/?password=abc"))


class TestRedactException(unittest.TestCase):
    def test_mensaje_redactado(self) -> None:
        exc = ValueError("fallo al llamar con password=hunter2")
        out = redact_exception(exc)
        self.assertNotIn("hunter2", out)
        self.assertIn("fallo al llamar", out)

    def test_excepcion_sin_mensaje(self) -> None:
        self.assertEqual(redact_exception(ValueError()), "")


class TestRedactMapping(unittest.TestCase):
    def test_claves_sensibles_enteras(self) -> None:
        data = {"password": "abc", "username": "u", "token": "t"}
        out = redact_mapping(data)
        self.assertEqual(out["password"], REDACTED)
        self.assertEqual(out["token"], REDACTED)
        self.assertEqual(out["username"], "u")

    def test_valores_url_se_redactan(self) -> None:
        out = redact_mapping({"stream": "http://x/live/u/p/1.ts"})
        self.assertNotIn("/p/", out["stream"])

    def test_estructuras_anidadas(self) -> None:
        data = {"user_info": {"password": "x", "user_id": 1}, "list": ["http://y/?password=z"]}
        out = redact_mapping(data)
        self.assertEqual(out["user_info"]["password"], REDACTED)
        self.assertEqual(out["user_info"]["user_id"], 1)
        self.assertNotIn("password=z", out["list"][0])

    def test_no_muta_entrada(self) -> None:
        data = {"password": "abc"}
        redact_mapping(data)
        self.assertEqual(data["password"], "abc")


class TestContainsSecret(unittest.TestCase):
    def test_detecta_query(self) -> None:
        self.assertTrue(contains_secret("http://x/?password=abc"))

    def test_detecta_path_xtream(self) -> None:
        self.assertTrue(contains_secret("http://x/live/u/p/1.ts"))

    def test_detecta_userinfo(self) -> None:
        self.assertTrue(contains_secret("http://u:p@host/"))

    def test_texto_limpio(self) -> None:
        self.assertFalse(contains_secret("http://example.com/canales.m3u8"))
        self.assertFalse(contains_secret(""))


class TestNeedsRedactionForStorage(unittest.TestCase):
    """La puerta de favoritos y recientes (SDD §37, §20.12)."""

    def test_password_en_query_sí_cuenta(self) -> None:
        url = ("http://srv:8080/streaming/timeshift.php?username=u"
               "&password=hunter2&stream=101&start=2026-10-01:20-00-00")
        self.assertTrue(contains_password_param(url))
        self.assertTrue(needs_redaction_for_storage(url))
        # La contraseña va en la query, no en userinfo ni en el path.
        self.assertFalse(contains_embedded_login(url))

    def test_login_embebido_sigue_contando(self) -> None:
        for url in ("http://u:p@host/live.ts", "http://srv/live/u/p/1.ts"):
            with self.subTest(url=url):
                self.assertTrue(needs_redaction_for_storage(url))

    def test_token_de_m3u_no_se_toca(self) -> None:
        """Sin `?token=` una lista M3U no se puede reproducir."""
        url = "http://panel.example.com/live/1.ts?token=tok123"
        self.assertFalse(contains_password_param(url))
        self.assertFalse(needs_redaction_for_storage(url))

    def test_referencias_opacas_no_necesitan_redaccion(self) -> None:
        from thetvview.catchup import CatchupRef
        from thetvview.stream_ref import PREFIX

        for url in (PREFIX + "Panel/live/101.ts",
                    CatchupRef("Panel", "101", 1700000000, 60).to_opaque(),
                    "https://cdn.example.com/live.m3u8",
                    ""):
            with self.subTest(url=url):
                self.assertFalse(needs_redaction_for_storage(url))

    def test_password_siemparecido_tiene_credencial(self) -> None:
        for clave in ("password", "PASSWORD", "passwd", "pwd", "pass"):
            with self.subTest(clave=clave):
                self.assertTrue(contains_password_param(f"http://x/ts?{clave}=s"))
                self.assertFalse(contains_password_param(f"http://x/ts?{clave}="))

    def test_no_confunde_password_con_palabras(self) -> None:
        for url in ("http://x/ts?mypasswordless=1", "http://x/password:1234/ts",
                    "http://x/password/1234.ts"):
            with self.subTest(url=url):
                self.assertFalse(contains_password_param(url))


class TestSecretStr(unittest.TestCase):
    def test_nunca_muestra_el_valor(self) -> None:
        s = SecretStr("hunter2")
        self.assertEqual(str(s), REDACTED)
        self.assertEqual(repr(s), f"SecretStr('{REDACTED}')")
        self.assertEqual(f"{s}", REDACTED)
        self.assertEqual(f"{s:>8}", format(REDACTED, ">8"))

    def test_reveal_devuelve_el_valor(self) -> None:
        self.assertEqual(SecretStr("hunter2").reveal(), "hunter2")

    def test_from_any(self) -> None:
        first = SecretStr("a")
        self.assertEqual(SecretStr.from_any(first).reveal(), "a")
        self.assertEqual(SecretStr.from_any(None).reveal(), "")

    def test_falsy_y_longitud_oculta(self) -> None:
        self.assertFalse(SecretStr(""))
        self.assertTrue(SecretStr("x"))
        # La longitud no expone la del password real.
        self.assertEqual(len(SecretStr("mucholargo")), len(REDACTED))

    def test_igualdad_y_hash(self) -> None:
        self.assertEqual(SecretStr("abc"), SecretStr("abc"))
        self.assertEqual(SecretStr("abc"), "abc")
        self.assertEqual(hash(SecretStr("abc")), hash("abc"))

    def test_repr_en_excepcion_no_fuga(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            raise ValueError(f"login falló con {SecretStr('topsecret')}")
        self.assertNotIn("topsecret", str(ctx.exception))


class TestRedactIterable(unittest.TestCase):
    def test_argv_sin_secretos(self) -> None:
        out = redact_iterable(["mpv", "http://x/live/u/p/1.ts", "--no-video"])
        self.assertNotIn("http://x/live/u/p/1.ts", out)
        self.assertIn("mpv", out)
        self.assertIn("--no-video", out)


class TestCompatRedactSecret(unittest.TestCase):
    """Comportamiento histórico de xtream_security.redact_secret (B10)."""

    def test_password_query(self) -> None:
        from thetvview.xtream_security import redact_secret

        out = redact_secret("http://x.com/player_api.php?user=a&password=secret123")
        self.assertEqual(out, "http://x.com/player_api.php?user=a&password=***")

    def test_username_intacto(self) -> None:
        from thetvview.xtream_security import redact_secret

        self.assertEqual(redact_secret("http://x/?username=u&password=p"),
                         "http://x/?username=u&password=***")

    def test_url_sin_password_unchanged(self) -> None:
        from thetvview.xtream_security import redact_secret

        url = "http://x/player_api.php?username=u&action=auth"
        self.assertEqual(redact_secret(url), url)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
