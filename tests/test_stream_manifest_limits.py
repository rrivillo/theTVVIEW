"""Tests de STREAM-001 (FR-STREAM-001 y FR-STREAM-004), Fase 5.

Dos cosas verifica este fichero:

- la **URL final** tras redirecciones es la base del análisis: el manejador
  de ``SafeHttpClient.stream`` la expone y ``probe`` la usa también cuando
  el servidor ignora el ``Range``;
- los **límites explícitos** de tamaño, profundidad, referencias y tiempo
  del análisis de manifiestos: HLS corta por tamaño, referencias y tiempo;
  DASH por referencias y tiempo, además de los de bytes/profundidad/nodos
  que ya tenía vía ``xml_safe``.

Nada aquí abre un socket de verdad: la red va mockeada o con el servidor
local que ya usan los tests de probe.
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest import mock

from thetvview.security.errors import ResponseTooLargeError
from thetvview.streams import manifest_limits, probe as probe_mod
from thetvview.streams.dash import parse_dash
from thetvview.streams.hls import (
    HlsParseError,
    parse_hls,
    parse_master_playlist,
    parse_media_playlist,
)
from thetvview.streams.manifest_limits import (
    DEFAULT_MANIFEST_LIMITS,
    ManifestLimitError,
    ManifestLimits,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "streams"


def _master(variantes: int) -> str:
    lineas = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for i in range(variantes):
        lineas.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={1_000_000 + i},'
            'RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"'
        )
        lineas.append(f"variant{i}.m3u8")
    return "\n".join(lineas) + "\n"


def _media(segmentos: int) -> str:
    lineas = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6"]
    for i in range(segmentos):
        lineas.append("#EXTINF:6.000,")
        lineas.append(f"segment{i}.ts")
    return "\n".join(lineas) + "\n"


def _mpd(representaciones: int) -> str:
    partes = [
        '<?xml version="1.0"?>',
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">',
        "<Period><AdaptationSet mimeType=\"video/mp4\">",
    ]
    for i in range(representaciones):
        partes.append(
            f'<Representation id="v{i}" codecs="avc1.4d401f" '
            f'bandwidth="{1_000_000 + i}" width="1280" height="720"/>'
        )
    partes.append("</AdaptationSet></Period></MPD>")
    return "".join(partes)


class TestHlsLimites(unittest.TestCase):
    """FR-STREAM-004 en HLS: tamaño, referencias y tiempo."""

    def test_tamano(self) -> None:
        with self.assertRaises(HlsParseError) as ctx:
            parse_hls("x" * 200, limits=ManifestLimits(max_bytes=100))
        self.assertIn("100 bytes", str(ctx.exception))

    def test_referencias_en_master(self) -> None:
        with self.assertRaises(HlsParseError) as ctx:
            parse_master_playlist(_master(6), limits=ManifestLimits(max_refs=5))
        self.assertIn("referencias", str(ctx.exception))

    def test_referencias_en_media(self) -> None:
        # Un media playlist sin límite de referencias sería el hueco: sus
        # EXTINF también son referencias y también se acumulan.
        with self.assertRaises(HlsParseError) as ctx:
            parse_media_playlist(_media(6), limits=ManifestLimits(max_refs=5))
        self.assertIn("referencias", str(ctx.exception))

    def test_tiempo(self) -> None:
        reloj = mock.Mock(side_effect=[0.0, 1.0])
        with mock.patch.object(manifest_limits, "ahora", reloj):
            with self.assertRaises(HlsParseError) as ctx:
                parse_master_playlist(
                    _master(2), limits=ManifestLimits(max_seconds=0.5)
                )
        self.assertIn("0.5 s", str(ctx.exception))

    def test_el_tamano_se_comprueba_antes_de_cualquier_analisis(self) -> None:
        # Ni siquiera la detección master/media recorre el texto gigante.
        with self.assertRaises(HlsParseError):
            parse_hls("#EXTM3U\n" + "#EXTINF:6.0,\nx.ts\n" * 50,
                      limits=ManifestLimits(max_bytes=50))

    def test_los_defaults_dejan_pasar_un_master_real(self) -> None:
        texto = (_FIXTURES / "master_full.m3u8").read_text(encoding="utf-8")
        caps = parse_hls(texto)
        self.assertTrue(caps.video_variants)
        self.assertEqual(DEFAULT_MANIFEST_LIMITS.max_refs, 10_000)


class TestDashLimites(unittest.TestCase):
    """FR-STREAM-004 en DASH: referencias y tiempo (bytes/profundidad/nodos
    ya los cubre xml_safe)."""

    def test_referencias(self) -> None:
        with self.assertRaises(ManifestLimitError) as ctx:
            parse_dash(_mpd(6), max_refs=5)
        self.assertIn("referencias", str(ctx.exception))

    def test_tiempo(self) -> None:
        reloj = mock.Mock(side_effect=[0.0, 1.0])
        with mock.patch.object(manifest_limits, "ahora", reloj):
            with self.assertRaises(ManifestLimitError) as ctx:
                parse_dash(_mpd(1), max_seconds=0.5)
        self.assertIn("0.5 s", str(ctx.exception))

    def test_es_un_valueerror_como_los_demas_malos(self) -> None:
        # Los llamadores ya distinguen «el XML no sirve» por ValueError.
        self.assertTrue(issubclass(ManifestLimitError, ValueError))

    def test_los_defaults_dejan_pasar_un_mpd_real(self) -> None:
        texto = (_FIXTURES / "full.mpd").read_text(encoding="utf-8")
        caps = parse_dash(texto)
        self.assertGreaterEqual(len(caps.video_variants), 2)


class TestRelojDeLosLimites(unittest.TestCase):
    def test_es_monotono(self) -> None:
        self.assertGreaterEqual(manifest_limits.ahora(), 0.0)


class TestManejadorDeStream(unittest.TestCase):
    """``SafeHttpClient.stream`` enseña la URL final (FR-STREAM-001)."""

    def test_expone_final_url_y_se_itera(self) -> None:
        from thetvview.security.safe_http import SafeHttpClient

        cliente = SafeHttpClient()
        opened = mock.Mock()
        opened.status = 200
        opened.final_url = "http://cdn.example/final/manifest.m3u8"
        opened.fp = io.BytesIO(b"parte1parte2")
        opened.headers = {"content-length": "12"}
        with mock.patch.object(cliente, "_open_validated", return_value=opened):
            handle = cliente.stream("http://orig.example/live/canal.m3u8")
        self.assertEqual(handle.final_url, "http://cdn.example/final/manifest.m3u8")
        self.assertEqual(b"".join(handle), b"parte1parte2")
        handle.close()  # idempotente: ya agotado, no revienta


class TestReadPrefix(unittest.TestCase):
    """Cuando el servidor ignora el Range, la base sigue siendo la final."""

    def test_devuelve_la_url_final(self) -> None:
        handle = mock.MagicMock()
        handle.final_url = "http://cdn.example/final/manifest.m3u8"
        handle.__iter__ = mock.Mock(return_value=iter([b"x" * 100]))
        cliente = mock.Mock()
        cliente.stream.return_value = handle
        prefix, final, _ctype = probe_mod._read_prefix(
            cliente, "http://orig.example/live/canal.m3u8", {}, 1.0
        )
        self.assertEqual(prefix, b"x" * 100)
        self.assertEqual(final, "http://cdn.example/final/manifest.m3u8")
        handle.close.assert_called_once()

    def test_sin_url_final_conserva_la_original(self) -> None:
        handle = mock.MagicMock()
        handle.final_url = ""
        handle.__iter__ = mock.Mock(return_value=iter([b"abc"]))
        cliente = mock.Mock()
        cliente.stream.return_value = handle
        _prefix, final, _ctype = probe_mod._read_prefix(
            cliente, "http://orig.example/canal.ts", {}, 1.0
        )
        self.assertEqual(final, "http://orig.example/canal.ts")


class _Descarga:
    """Cliente falso: HEAD fijo y respuestas de request por orden."""

    def __init__(self, head_ctype: str, head_length: int | None, *respuestas):
        self.head = mock.Mock()
        self.head.return_value = mock.Mock(
            status=200,
            headers=(
                {"content-type": head_ctype}
                if head_length is None
                else {"content-type": head_ctype, "content-length": str(head_length)}
            ),
            final_url="http://orig.example/x",
        )
        self.request = mock.Mock(side_effect=list(respuestas))

    def __call__(self, *_args, **_kwargs):  # usado por mock.patch
        return self


def _respuesta(body: bytes, ctype: str = "", status: int = 200):
    resp = mock.Mock()
    resp.body = body
    resp.final_url = "http://cdn.example/final/x"
    resp.headers = {"content-type": ctype} if ctype else {}
    resp.status = status
    return resp


class TestTopeDelGet(unittest.TestCase):
    """FR-STREAM-004: el cuerpo del manifiesto se pide con el tope justo."""

    def _descarga(self, cliente):
        with mock.patch.object(probe_mod, "SafeHttpClient", return_value=cliente):
            return probe_mod._download(
                "http://orig.example/x", {}, allow_private=False, timeout=5.0
            )

    def test_mpd_se_pide_hasta_max_mpd_bytes(self) -> None:
        cliente = _Descarga(
            "application/dash+xml", 4_000_000, _respuesta(b"<MPD/>", "application/dash+xml")
        )
        self._descarga(cliente)
        self.assertEqual(
            cliente.request.call_args.kwargs["max_bytes"], probe_mod.MAX_MPD_BYTES
        )

    def test_cuerpo_pequeno_no_manifiesto_usa_el_tope_estrecho(self) -> None:
        cliente = _Descarga("video/mp2t", 1000, _respuesta(b"\x47" * 1000, "video/mp2t"))
        self._descarga(cliente)
        self.assertEqual(
            cliente.request.call_args.kwargs["max_bytes"], probe_mod.MAX_MANIFEST_BYTES
        )

    def test_si_excede_el_tope_cae_al_range_sin_materializar(self) -> None:
        # El servidor declara un manifiesto gigante (o nada): no se traga
        # el cuerpo y se va al Range acotado, como con un directo sin fin.
        cliente = _Descarga(
            "application/dash+xml",
            None,
            ResponseTooLargeError("grande"),
            _respuesta(b"#EXTM3U\n", "application/vnd.apple.mpegurl", 206),
        )
        body, final, ctype, status = self._descarga(cliente)
        self.assertEqual(body, b"#EXTM3U\n")
        self.assertEqual(final, "http://cdn.example/final/x")
        self.assertEqual(status, 206)
        self.assertEqual(cliente.request.call_count, 2)
        segunda = cliente.request.call_args_list[1]
        self.assertEqual(segunda.kwargs["max_bytes"], probe_mod.RANGE_PROBE_BYTES)
        self.assertIn("Range", segunda.kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
