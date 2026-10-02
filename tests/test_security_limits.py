"""Tests de los límites de recursos (SDD §18/§19, gap B14)."""

from __future__ import annotations

import math
import unittest

from thetvview.security.limits import (
    DEFAULT_LIMITS,
    Limits,
    clamp_value,
    get_limits,
    reset_limits,
    set_limits,
)


class TestDefaults(unittest.TestCase):
    def test_valores_del_sdd(self) -> None:
        lim = DEFAULT_LIMITS
        self.assertEqual(lim.connect_timeout, 10.0)
        self.assertEqual(lim.read_timeout, 30.0)
        self.assertEqual(lim.total_timeout, 60.0)
        self.assertEqual(lim.max_response_bytes, 25 * 1024 * 1024)
        self.assertEqual(lim.max_redirects, 3)
        self.assertEqual(lim.max_concurrent_requests, 8)
        self.assertEqual(lim.max_entries, 200_000)

    def test_inmutable(self) -> None:
        with self.assertRaises(Exception):
            DEFAULT_LIMITS.max_redirects = 99  # type: ignore[misc]

    def test_round_trip(self) -> None:
        self.assertEqual(Limits.from_mapping(DEFAULT_LIMITS.to_mapping()), DEFAULT_LIMITS)


class TestClamp(unittest.TestCase):
    def test_recorte_inferior(self) -> None:
        self.assertEqual(clamp_value("connect_timeout", 0), 0.1)
        self.assertEqual(clamp_value("connect_timeout", -100), 0.1)
        self.assertEqual(clamp_value("max_redirects", -1), 0)
        self.assertEqual(clamp_value("max_concurrent_requests", 0), 1)
        self.assertEqual(clamp_value("max_entries", 0), 1)

    def test_recorte_superior(self) -> None:
        self.assertEqual(clamp_value("connect_timeout", 10_000), 300.0)
        self.assertEqual(clamp_value("max_redirects", 9999), 10)
        self.assertEqual(clamp_value("max_concurrent_requests", 10_000), 64)

    def test_no_numerico_se_descarta(self) -> None:
        for bad in ("30", None, True, [1], {"a": 1}):
            with self.subTest(bad=bad):
                self.assertIsNone(clamp_value("read_timeout", bad))

    def test_nan_e_infinito_se_descartan(self) -> None:
        self.assertIsNone(clamp_value("read_timeout", math.nan))
        self.assertIsNone(clamp_value("read_timeout", math.inf))

    def test_desconocido_se_descarta(self) -> None:
        self.assertIsNone(clamp_value("clave_inventada", 5))

    def test_enteros_siguen_siendo_enteros(self) -> None:
        self.assertIsInstance(clamp_value("max_entries", 1234), int)
        self.assertIsInstance(clamp_value("connect_timeout", 12), float)


class TestFromMapping(unittest.TestCase):
    def test_acepta_prefs_sucias(self) -> None:
        lim = Limits.from_mapping(
            {
                "max_redirects": 99,  # se recorta
                "connect_timeout": 0,  # se recorta
                "bogus": 1,  # se ignora
                "max_entries": "no-numero",  # se ignora
                "read_timeout": True,  # se ignora
            }
        )
        self.assertEqual(lim.max_redirects, 10)
        self.assertEqual(lim.connect_timeout, 0.1)
        self.assertEqual(lim.max_entries, DEFAULT_LIMITS.max_entries)
        self.assertEqual(lim.read_timeout, DEFAULT_LIMITS.read_timeout)

    def test_none_o_vacio_da_por_defecto(self) -> None:
        self.assertEqual(Limits.from_mapping(None), DEFAULT_LIMITS)
        self.assertEqual(Limits.from_mapping({}), DEFAULT_LIMITS)

    def test_valores_validos_se_respetan(self) -> None:
        lim = Limits.from_mapping({"read_timeout": 5, "max_redirects": 1})
        self.assertEqual(lim.read_timeout, 5.0)
        self.assertEqual(lim.max_redirects, 1)


class TestGlobal(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(reset_limits)

    def test_get_por_defecto(self) -> None:
        reset_limits()
        self.assertEqual(get_limits(), DEFAULT_LIMITS)

    def test_set_con_mapping(self) -> None:
        lim = set_limits({"max_redirects": 7})
        self.assertEqual(lim.max_redirects, 7)
        self.assertEqual(get_limits().max_redirects, 7)

    def test_set_con_dataclass(self) -> None:
        custom = Limits(max_redirects=2)
        self.assertIs(set_limits(custom), custom)
        self.assertEqual(get_limits().max_redirects, 2)

    def test_set_none_y_reset(self) -> None:
        set_limits({"max_redirects": 7})
        self.assertEqual(set_limits(None), DEFAULT_LIMITS)
        set_limits({"max_redirects": 7})
        reset_limits()
        self.assertEqual(get_limits(), DEFAULT_LIMITS)

    def test_set_con_basura_no_destruye_la_seguridad(self) -> None:
        lim = set_limits({"max_redirects": -50, "read_timeout": "x"})
        # Nunca se puede desactivar un límite con datos corruptos.
        self.assertGreaterEqual(lim.max_redirects, 0)
        self.assertGreaterEqual(lim.read_timeout, 0.1)


class TestMaxEntriesAplicado(unittest.TestCase):
    """`max_entries` tiene que cortar de verdad en M3U y en la API Xtream."""

    def tearDown(self) -> None:
        reset_limits()

    # -- M3U -----------------------------------------------------------------

    def test_m3u_con_lista_normal_pasa(self) -> None:
        from thetvview.m3u_parser import parse_text

        text = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1,Canal {i}\nhttp://h/{i}.ts\n' for i in range(5)
        )
        set_limits({"max_entries": 10})
        self.assertEqual(len(parse_text(text).channels), 5)

    def test_m3u_que_excede_el_tope_lanza(self) -> None:
        from thetvview.m3u_parser import parse_text
        from thetvview.security.errors import ParseError

        text = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1,Canal {i}\nhttp://h/{i}.ts\n' for i in range(50)
        )
        set_limits({"max_entries": 10})
        with self.assertRaises(ParseError) as ctx:
            parse_text(text)
        self.assertIn("límite", str(ctx.exception))

    def test_m3u_por_defecto_admite_listas_grandes(self) -> None:
        from thetvview.m3u_parser import parse_text

        text = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1,Canal {i}\nhttp://h/{i}.ts\n' for i in range(300)
        )
        self.assertEqual(len(parse_text(text).channels), 300)

    # -- API Xtream ----------------------------------------------------------

    def test_respuesta_json_que_excede_el_tope(self) -> None:
        import json
        from unittest import mock

        from thetvview.security.errors import InvalidSourceError
        from thetvview.xtream_client import api_call

        payload = json.dumps([{"stream_id": i} for i in range(50)]).encode()
        set_limits({"max_entries": 10})
        with mock.patch(
            "thetvview.xtream_client.SafeHttpClient.request",
            return_value=_FakeResponse(payload),
        ):
            with self.assertRaises(InvalidSourceError) as ctx:
                api_call("http://panel/player_api.php?x=1")
        self.assertIn("elementos", str(ctx.exception))

    def test_respuesta_json_bajo_el_tope_pasa(self) -> None:
        import json
        from unittest import mock

        from thetvview.xtream_client import api_call

        payload = json.dumps([{"stream_id": i} for i in range(5)]).encode()
        set_limits({"max_entries": 10})
        with mock.patch(
            "thetvview.xtream_client.SafeHttpClient.request",
            return_value=_FakeResponse(payload),
        ):
            data = api_call("http://panel/player_api.php?x=1")
        self.assertEqual(len(data), 5)  # type: ignore[arg-type]


class _FakeResponse:
    """Sustituto de `SafeHttpClient.request` para probar el guard de entradas.

    Sólo los tres atributos que `xtream_client` lee de la respuesta.
    """

    def __init__(self, payload: bytes, status: int = 200, reason: str = "OK") -> None:
        self.status = status
        self.reason = reason
        self.body = payload


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
