"""Tests de la política anti-SSRF (SDD §11, gap B3) — SEC-005."""

from __future__ import annotations

import socket
import unittest
from unittest import mock

from thetvview.security.errors import NetworkError, SSRFBlockedError
from thetvview.security.ssrf import (
    blocked_reason,
    check_host,
    check_ip,
    check_url,
    is_blocked_ip,
    resolve_and_check,
)


BLOCKED_BY_DEFAULT = (
    "127.0.0.1",
    "127.1.2.3",
    "10.0.0.5",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "169.254.169.254",  # metadata de nube
    "100.64.0.1",  # CGNAT
    "0.0.0.0",
    "224.0.0.1",  # multicast
    "255.255.255.255",  # broadcast
    "::1",
    "fe80::1",
    "fd00::1",  # ULA
    "ff02::1",
    "::",
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "::ffff:10.0.0.1",  # IPv4-mapped privada
)

PUBLIC_OK = ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2001:4860:4860::8888", "::ffff:8.8.8.8")

PRIVATE_OK_WITH_FLAG = (
    "127.0.0.1",
    "10.0.0.5",
    "172.16.0.1",
    "192.168.1.1",
    "100.64.0.1",
    "::1",
    "fd00::1",
    "::ffff:10.0.0.1",
)

STILL_BLOCKED_WITH_FLAG = (
    "169.254.169.254",  # metadata
    "fe80::1",  # link-local
    "224.0.0.1",
    "0.0.0.0",
    "255.255.255.255",
)


class TestIpPolicy(unittest.TestCase):
    def test_bloqueadas_por_defecto(self) -> None:
        for ip in BLOCKED_BY_DEFAULT:
            with self.subTest(ip=ip):
                self.assertTrue(is_blocked_ip(ip), ip)
                self.assertIsNotNone(blocked_reason(ip))

    def test_publicas_permitidas(self) -> None:
        for ip in PUBLIC_OK:
            with self.subTest(ip=ip):
                self.assertFalse(is_blocked_ip(ip), ip)
                self.assertIsNone(blocked_reason(ip))

    def test_excepcion_por_fuente_salva_lan_y_loopback(self) -> None:
        for ip in PRIVATE_OK_WITH_FLAG:
            with self.subTest(ip=ip):
                self.assertIsNone(blocked_reason(ip, allow_private=True), ip)

    def test_excepcion_no_salva_metadata_ni_link_local(self) -> None:
        for ip in STILL_BLOCKED_WITH_FLAG:
            with self.subTest(ip=ip):
                self.assertIsNotNone(
                    blocked_reason(ip, allow_private=True),
                    f"{ip} debe seguir bloqueado con allow_private",
                )

    def test_check_ip_lanza(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            check_ip("127.0.0.1")
        check_ip("8.8.8.8")  # no lanza
        check_ip("127.0.0.1", allow_private=True)

    def test_ip_invalida(self) -> None:
        self.assertIsNotNone(blocked_reason("no-es-ip"))
        with self.assertRaises(SSRFBlockedError):
            check_ip("no-es-ip")

    def test_zone_id_ipv6(self) -> None:
        self.assertIsNotNone(blocked_reason("fe80::1%eth0"))
        with self.assertRaises(SSRFBlockedError):
            check_ip("fe80::1%eth0")

    def test_mensaje_no_escala_la_topologia(self) -> None:
        with self.assertRaises(SSRFBlockedError) as ctx:
            check_ip("10.0.0.5")
        self.assertNotIn("10.0.0.5", str(ctx.exception))


class TestHostNames(unittest.TestCase):
    def test_nombres_locales_bloqueados(self) -> None:
        for host in ("localhost", "LOCALHOST.", "foo.local", "db.internal",
                     "printer.localdomain", "localhost.localdomain"):
            with self.subTest(host=host):
                with self.assertRaises(SSRFBlockedError):
                    check_host(host)

    def test_metadata_bloqueada_siempre(self) -> None:
        for host in ("metadata.google.internal", "Metadata.Google.Internal",
                     "instance-data"):
            with self.subTest(host=host):
                with self.assertRaises(SSRFBlockedError):
                    check_host(host, allow_private=True)

    def test_nombres_publicos_permitidos(self) -> None:
        for host in ("example.com", "cdn.strem.io", "1.1.1.1"):
            with self.subTest(host=host):
                check_host(host)

    def test_excepcion_permite_nombres_locales(self) -> None:
        check_host("localhost", allow_private=True)
        check_host("mi-servidor.local", allow_private=True)

    def test_host_vacio(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            check_host("")

    def test_ip_literal_se_como_ip(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            check_host("169.254.169.254")
        check_host("8.8.8.8")


class TestResolveAndCheck(unittest.TestCase):
    def test_literal_no_necesita_dns(self) -> None:
        self.assertEqual(resolve_and_check("8.8.8.8", 80), ["8.8.8.8"])
        with self.assertRaises(SSRFBlockedError):
            resolve_and_check("192.168.1.1", 80)
        resolve_and_check("192.168.1.1", 80, allow_private=True)

    def test_localhost_se_bloquea_tras_resolver(self) -> None:
        # getaddrinfo("localhost") → 127.0.0.1 → bloqueado por IP.
        with self.assertRaises(SSRFBlockedError):
            resolve_and_check("localhost", 80, allow_private=False)

    def test_todas_las_ips_se_comprueban(self) -> None:
        """DNS rebinding: pública + privada en la misma respuesta."""
        fake = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 80)),
        ]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            with self.assertRaises(SSRFBlockedError):
                resolve_and_check("rebind.evil", 80)

    def test_respuesta_totalmente_publica_devuelve_todo(self) -> None:
        fake = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 80)),
        ]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            self.assertEqual(
                resolve_and_check("dual.evil", 80), ["8.8.8.8", "1.1.1.1"]
            )

    def test_error_dns_es_network_error_amigable(self) -> None:
        with mock.patch(
            "socket.getaddrinfo",
            side_effect=socket.gaierror(-2, "Name or service not known"),
        ):
            with self.assertRaises(NetworkError) as ctx:
                resolve_and_check("no-existe.evil", 80)
        self.assertIn("conectar", str(ctx.exception).lower())

    def test_dns_colgado_no_congela(self) -> None:
        def _hang(*args, **kwargs):
            import time

            time.sleep(5)

        with mock.patch("socket.getaddrinfo", side_effect=_hang):
            with self.assertRaises(NetworkError) as ctx:
                resolve_and_check("colgado.evil", 80, timeout=0.05)
        self.assertIn("tiempo de espera", str(ctx.exception).lower())


class TestCheckUrl(unittest.TestCase):
    def test_url_publica_ok(self) -> None:
        parts = check_url("http://example.com/lista.m3u8", resolve=False)
        self.assertEqual(parts.host, "example.com")

    def test_url_a_ip_privada_bloqueada(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            check_url("http://192.168.1.10/lista.m3u", resolve=False)
        check_url("http://192.168.1.10/lista.m3u", resolve=False, allow_private=True)

    def test_url_a_metadata_bloqueada_incluso_con_excepcion(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            check_url(
                "http://169.254.169.254/latest/meta-data/",
                resolve=False,
                allow_private=True,
            )

    def test_url_a_localhost_bloqueada(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            check_url("http://localhost:8080/x", resolve=False)

    def test_url_a_hostname_local_bloqueado(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            check_url("http://mi-servidor.local/x", resolve=False)

    def test_stream_tambien_se_comprueba(self) -> None:
        with self.assertRaises(SSRFBlockedError):
            check_url(
                "http://10.0.0.5/live/u/p/1.ts", purpose="stream", resolve=False
            )

    def test_url_mal_formada_error_distinto(self) -> None:
        from thetvview.security.errors import InvalidUrlError

        with self.assertRaises(InvalidUrlError):
            check_url("file:///etc/passwd", resolve=False)

    def test_ffmpeg_relativo_sin_host_no_requiere_dns(self) -> None:
        parts = check_url("ffmpeg://rel/x.m3u8", purpose="stream", resolve=True)
        self.assertEqual(parts.host, "")

    def test_resolucion_real_de_un_literal_publico(self) -> None:
        # Sin salir de la máquina: una IP literal no toca el DNS.
        self.assertEqual(check_url("http://1.1.1.1/x", resolve=True).host, "1.1.1.1")


class TestAllowPrivateFlagPerSource(unittest.TestCase):
    """La excepción SSRF vive por fuente en playlists.json, nunca global."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from thetvview.playlist_manager import PlaylistManager

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mgr = PlaylistManager(Path(self._tmp.name) / "playlists.json")

    def test_por_defecto_es_false(self) -> None:
        entry = self.mgr.add("Mi LAN", "http://192.168.1.10/lista.m3u")
        self.assertFalse(entry.allow_private_network)
        self.assertFalse(self.mgr.allows_private_network("Mi LAN"))
        raw = self.mgr.path.read_text(encoding="utf-8")
        self.assertIn('"allow_private_network": false', raw)

    def test_se_persiste_y_se_recupera(self) -> None:
        self.mgr.add("Mi LAN", "http://192.168.1.10/lista.m3u")
        self.assertTrue(self.mgr.set_allow_private_network("Mi LAN", True))
        self.assertTrue(self.mgr.allows_private_network("Mi LAN"))
        # Otra instancia (nueva sesión) lo ve igual.
        from thetvview.playlist_manager import PlaylistManager

        other = PlaylistManager(self.mgr.path)
        self.assertTrue(other.allows_private_network("Mi LAN"))

    def test_xtream_tambien_lo_lleva(self) -> None:
        self.mgr.add_xtream("Panel", "http://192.168.1.20:8080", "u", "p")
        self.assertFalse(self.mgr.allows_private_network("Panel"))
        self.assertTrue(self.mgr.set_allow_private_network("Panel", True))
        self.assertTrue(self.mgr.allows_private_network("Panel"))

    def test_fuente_inexistente(self) -> None:
        self.assertFalse(self.mgr.set_allow_private_network("NoExiste", True))
        self.assertFalse(self.mgr.allows_private_network("NoExiste"))

    def test_flag_raro_en_json_se_interpreta_como_false(self) -> None:
        import json

        from thetvview.playlist_manager import PlaylistManager

        self.mgr.add("L", "http://x/a.m3u")
        data = json.loads(self.mgr.path.read_text(encoding="utf-8"))
        data[0]["allow_private_network"] = "yes"
        self.mgr.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(PlaylistManager(self.mgr.path).allows_private_network("L"))
        data[0]["allow_private_network"] = "banana"
        self.mgr.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(PlaylistManager(self.mgr.path).allows_private_network("L"))

    def test_el_flag_no_es_secreto_y_el_password_si(self) -> None:
        self.mgr.add_xtream("Panel", "http://192.168.1.20:8080", "u", "s3cret",
                            allow_private_network=True)
        raw = self.mgr.path.read_text(encoding="utf-8")
        self.assertIn('"allow_private_network": true', raw)
        self.assertNotIn("s3cret", raw)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
