"""M3U-003/M3U-004 · FR-M3U-005/006/007 y §8.3: fuzzing reproducible.

El objetivo no es encontrar el fallo más Castor: es que **ninguna**
entrada dentro de los límites admitidos provoque un fallo no
controlado, y que la misma entrada se interprete siempre igual.

Como el fuzzing es aleatorio, la semilla es parte del resultado:

- se puede forzar con ``M3U_FUZZ_SEED=<n>``;
- la semilla **por defecto** es una lista corta y fija;
- cuando algo falla, la semilla se imprime **y** se guarda en
  ``tests/fixtures/fuzz_seeds.txt``, que se vuelve a leer en la próxima
  vuelta. Lo que tropieza hoy se vuelve a tropezar mañana.

Invariantes comprobados como propiedades (no como casos sueltos):

1. ninguna excepción no controlada (``ParseError`` sí se acepta: es el
   límite de entradas, documentado);
2. los canales nunca superan el límite del parser;
3. ningún canal sale sin nombre o sin URL, y ningún ``tvg-*`` vacío se
   disfraza de dato;
4. dos parses de la misma entrada dan exactamente el mismo resultado
   (interpretación determinista, §8.4);
5. un archivo grande no se come la CPU (rendimiento lineal, FR-M3U-007).
"""

from __future__ import annotations

import os
import random
import tempfile
import time
import unittest
from pathlib import Path

from thetvview import m3u_encoding
from thetvview.m3u_parser import parse_file, parse_text
from thetvview.security.errors import ParseError
from thetvview.security.limits import get_limits

#: Donde se guardan las semillas que han fallado (M3U-003).
SEMILLAS = Path(__file__).parent / "fixtures" / "fuzz_seeds.txt"

#: Semillas de la pasada normal. Cortas y fijas: si el parser cambia, esta
#: lista sigue significando lo mismo.
SEMILLAS_POR_DEFECTO: tuple[int, ...] = (1, 7, 42, 1234, 20261013)

_DIRECTIVAS = (
    "#EXTM3U",
    "#EXTINF:-1",
    "#EXTINF:",
    "#EXTVLCOPT:http-user-agent=theTVVIEW",
    "#KODIPROP:inputstream.adaptive.manifest_type=hls",
    "#EXTGRP:Noticias",
    "#PLAYLIST",
    "#EXT-X-START:TIME-OFFSET=10",
    "#EXTVLCOPT:",
    "#",
)

_FRAGMENTOS = (
    'tvg-id="c1"',
    'tvg-id="canal.es, extra"',
    'tvg-name="Simpson, Los"',
    'group-title="Deportes, Locales"',
    'group-title="-"',
    'tvg-logo="tvg-logo="http://x/y.png"',
    'radio="true"',
    'unknown="valor"',
    "suelto=1",
    'tvg-id="roto',
    '"',
    ",",
    ",,",
    "  ",
    "Ñandú",
    "😀",
    "a" * 300,
)

_URLS = (
    "http://x/1.ts",
    "https://u:p@h/2.m3u8",
    "rtmp://a/b",
    "/local/path.ts",
    "",
    "   ",
    "x" * 500,
)


def _texto_aleatorio(rng: random.Random, lineas: int) -> str:
    """Una lista M3U sintética y rota a propósito, pero siempre `str`."""
    cuerpo: list[str] = []
    for _ in range(lineas):
        dado = rng.random()
        if dado < 0.12:
            cuerpo.append(rng.choice(_DIRECTIVAS))
        elif dado < 0.62:
            atributos = " ".join(rng.sample(_FRAGMENTOS, rng.randint(0, 4)))
            nombre = rng.choice(_FRAGMENTOS + (f"Canal {rng.randint(0, 999)}",))
            separador = "," if rng.random() < 0.9 else ""
            cuerpo.append(f"#EXTINF:{atributos}{separador}{nombre}")
        elif dado < 0.92:
            cuerpo.append(rng.choice(_URLS))
        else:
            cuerpo.append(
                "".join(chr(rng.randint(32, 0x2FFF)) for _ in range(rng.randint(0, 40)))
            )
    return "\n".join(cuerpo)


def _parsear(case: unittest.TestCase, texto: str):
    """Parsea que sólo puede lanzar ``ParseError`` (el límite de entradas)."""
    try:
        return parse_text(texto, source="fuzz.m3u")
    except ParseError:
        return None
    except Exception as exc:  # noqa: BLE001 - aquí el fallo ES el hallazgo
        raise AssertionError(
            f"excepción no controlada del parser: {type(exc).__name__}: {exc}"
        ) from exc


def _comprobar_invariantes(case: unittest.TestCase, texto: str) -> None:
    playlist = _parsear(case, texto)
    if playlist is None:
        return
    case.assertLessEqual(len(playlist.channels), get_limits().max_entries)
    for canal in playlist.channels:
        case.assertIsInstance(canal.name, str)
        case.assertTrue(canal.name.strip(), "un canal sin nombre no es un canal")
        case.assertIsInstance(canal.url, str)
        case.assertTrue(canal.url.strip(), "un canal sin URL no se puede abrir")
        for valor in (canal.tvg_id, canal.tvg_name, canal.tvg_logo):
            case.assertTrue(valor is None or isinstance(valor, str))
            if isinstance(valor, str):
                case.assertTrue(
                    valor.strip(), "un tvg-* vacío debe ser None, no cadena"
                )
    otra = _parsear(case, texto)
    if otra is None:
        return
    case.assertEqual(
        [(c.name, c.url, c.tvg_id, c.tvg_name, c.group) for c in playlist.channels],
        [(c.name, c.url, c.tvg_id, c.tvg_name, c.group) for c in otra.channels],
        "la misma entrada se interpretó de dos maneras",
    )


def _anotar_semilla(semilla: int, destino: Path = SEMILLAS) -> None:
    """Guarda la semilla para que la próxima vuelta tropiece lo mismo."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("a", encoding="utf-8") as archivo:
        archivo.write(f"{semilla}\n")


class TestFuzzM3U(unittest.TestCase):
    def _semillas(self) -> list[int]:
        forcible = os.environ.get("M3U_FUZZ_SEED", "").strip()
        if forcible:
            return [int(forcible)]
        guardadas: list[int] = []
        if SEMILLAS.exists():
            for linea in SEMILLAS.read_text(encoding="utf-8").splitlines():
                if linea.strip().isdigit():
                    guardadas.append(int(linea.strip()))
        return [*SEMILLAS_POR_DEFECTO, *guardadas]

    def test_invariantes_por_semilla(self) -> None:
        for semilla in self._semillas():
            with self.subTest(semilla=semilla):
                rng = random.Random(semilla)
                for _ in range(20):
                    try:
                        _comprobar_invariantes(
                            self, _texto_aleatorio(rng, rng.randint(0, 60))
                        )
                    except AssertionError:
                        _anotar_semilla(semilla)
                        raise AssertionError(
                            f"fuzz M3U: fallo con la semilla {semilla} "
                            f"(M3U_FUZZ_SEED={semilla} para reproducirla; "
                            f"queda guardada en {SEMILLAS.name})"
                        ) from None

    def test_bytes_arbitrarios_no_rompen_la_decodificacion(self) -> None:
        """Basura binaria: se decodifica o se sustituye, pero nunca se rompe."""
        with tempfile.TemporaryDirectory() as tmp:
            for semilla in (1, 2, 3, 4):
                rng = random.Random(semilla)
                crudo = bytes(rng.randrange(256) for _ in range(rng.randint(0, 800)))
                decision = m3u_encoding.decodificar_m3u(crudo, origen="fuzz")
                self.assertIsInstance(decision.texto, str)
                ruta = Path(tmp) / f"basura-{semilla}.m3u"
                ruta.write_bytes(crudo)
                try:
                    playlist = parse_file(ruta)
                except (ParseError, OSError):
                    continue  # refusado a propósito: también es controlado
                for canal in playlist.channels:
                    self.assertTrue(canal.name.strip())
                    self.assertTrue(canal.url.strip())

    def test_no_se_bloquea_con_una_entrada_grande(self) -> None:
        """FR-M3U-007: lineal con el tamaño. El tope es generoso a propósito."""
        rng = random.Random(20261013)
        grande = _texto_aleatorio(rng, 20_000)
        inicio = time.monotonic()
        _comprobar_invariantes(self, grande)
        self.assertLess(time.monotonic() - inicio, 20.0)

    def test_la_semilla_se_guarda_para_la_proxima(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "sub" / "fuzz_seeds.txt"
            _anotar_semilla(99, destino)
            _anotar_semilla(99, destino)
            lineas = destino.read_text(encoding="utf-8").split()
            self.assertEqual(lineas, ["99", "99"])


if __name__ == "__main__":
    unittest.main()
