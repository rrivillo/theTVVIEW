"""Tests de lectura acotada de ficheros locales (gap B11) y permisos."""

from __future__ import annotations

import gzip
import os
import tempfile
import unittest
from pathlib import Path

from thetvview.security.limits import reset_limits, set_limits
from thetvview.security.local_files import (
    PRIVATE_DIR_MODE,
    PRIVATE_FILE_MODE,
    chmod_private,
    chmod_private_tree,
    read_limited_bytes,
    read_limited_text,
)


class _TmpDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        reset_limits()


class TestLecturaBasica(_TmpDir):
    def test_lee_texto_utf8_sig(self) -> None:
        p = self.root / "a.m3u"
        p.write_bytes(b"\xef\xbb\xbf#EXTM3U\n#EXTINF:-1,X\nhttp://x/1.ts\n")
        text = read_limited_text(p)
        self.assertTrue(text.startswith("#EXTM3U"))
        self.assertNotIn("﻿", text)

    def test_lee_bytes(self) -> None:
        p = self.root / "b.bin"
        p.write_bytes(b"\x00\x01\x02")
        self.assertEqual(read_limited_bytes(p), b"\x00\x01\x02")

    def test_fichero_inexistente(self) -> None:
        with self.assertRaises(OSError) as ctx:
            read_limited_text(self.root / "no-existe.xml")
        self.assertIn("no-existe.xml", str(ctx.exception))

    def test_directorio_rechazado(self) -> None:
        with self.assertRaises(OSError) as ctx:
            read_limited_text(self.root)
        self.assertIn("no es un fichero normal", str(ctx.exception))


class TestLimites(_TmpDir):
    def test_supera_el_limite(self) -> None:
        p = self.root / "grande.m3u"
        p.write_bytes(b"x" * 5000)
        with self.assertRaises(OSError) as ctx:
            read_limited_text(p, max_bytes=1000)
        msg = str(ctx.exception)
        self.assertIn("límite", msg)
        self.assertIn(str(p), msg)

    def test_limite_exacto_lo_permite(self) -> None:
        p = self.root / "exacto.m3u"
        p.write_bytes(b"12345")
        self.assertEqual(read_limited_text(p, max_bytes=5), "12345")

    def test_limite_por_defecto_viene_de_limits(self) -> None:
        p = self.root / "grande.m3u"
        p.write_bytes(b"x" * 3000)
        set_limits({"max_file_bytes": 1024})
        with self.assertRaises(OSError):
            read_limited_text(p)

    def test_tamano_se_comprueba_antes_de_leer(self) -> None:
        # Un fichero enorme no se llega a leer ni un byte.
        p = self.root / "enorme.m3u"
        p.write_bytes(b"x" * 4096)
        with self.assertRaises(OSError):
            read_limited_bytes(p, max_bytes=100)


class TestSymlinks(_TmpDir):
    def test_symlink_rechazado(self) -> None:
        target = self.root / "real.m3u"
        target.write_text("#EXTM3U\n", encoding="utf-8")
        link = self.root / "link.m3u"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("el SO no permite symlinks")
        with self.assertRaises(OSError) as ctx:
            read_limited_text(link)
        self.assertIn("enlace simbólico", str(ctx.exception))

    def test_symlink_permitido_con_la_politica_explicita(self) -> None:
        target = self.root / "real.m3u"
        target.write_text("#EXTM3U\n", encoding="utf-8")
        link = self.root / "link.m3u"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("el SO no permite symlinks")
        self.assertEqual(read_limited_text(link, allow_symlinks=True), "#EXTM3U\n")


class TestGzip(_TmpDir):
    def test_gzip_se_descomprime(self) -> None:
        p = self.root / "epg.xml.gz"
        p.write_bytes(gzip.compress(b"<tv></tv>"))
        self.assertEqual(read_limited_text(p), "<tv></tv>")

    def test_gzip_por_magia_sin_sufijo(self) -> None:
        p = self.root / "epg.bin"
        p.write_bytes(gzip.compress(b"hola"))
        self.assertEqual(read_limited_text(p), "hola")

    def test_bomba_gzip_no_expande(self) -> None:
        """2 MB de ceros comprimidos ~2 KB: el tope va sobre el resultado."""
        payload = b"\x00" * (2 * 1024 * 1024)
        blob = gzip.compress(payload)
        p = self.root / "bomba.gz"
        p.write_bytes(blob)
        limit = len(blob) + 64
        self.assertLess(limit, len(payload))
        with self.assertRaises(OSError) as ctx:
            read_limited_text(p, max_bytes=limit)
        self.assertIn("límite", str(ctx.exception))

    def test_gzip_truncado(self) -> None:
        blob = gzip.compress(b"contenido de prueba " * 100)
        p = self.root / "roto.gz"
        p.write_bytes(blob[: len(blob) // 2])
        with self.assertRaises(OSError) as ctx:
            read_limited_text(p)
        self.assertIn("dañado", str(ctx.exception))

    def test_gzip_valido_por_debajo_del_limite(self) -> None:
        blob = gzip.compress(b"<tv>ok</tv>")
        p = self.root / "ok.gz"
        p.write_bytes(blob)
        self.assertEqual(read_limited_text(p, max_bytes=len(blob) + 16), "<tv>ok</tv>")


class TestPermisos(_TmpDir):
    def test_chmod_privado(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows no tiene chmod POSIX")
        p = self.root / "playlists.json"
        p.write_text("[]", encoding="utf-8")
        self.assertTrue(chmod_private(p))
        self.assertEqual(p.stat().st_mode & 0o777, PRIVATE_FILE_MODE)
        d = self.root / "cache"
        d.mkdir()
        self.assertTrue(chmod_private(d, directory=True))
        self.assertEqual(d.stat().st_mode & 0o777, PRIVATE_DIR_MODE)

    def test_chmod_arbol(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows no tiene chmod POSIX")
        d = self.root / "xtream_cache"
        d.mkdir()
        (d / "a.json").write_text("{}", encoding="utf-8")
        self.assertTrue(chmod_private_tree(d))
        self.assertEqual((d / "a.json").stat().st_mode & 0o777, PRIVATE_FILE_MODE)

    def test_chmod_no_existente(self) -> None:
        self.assertFalse(chmod_private(self.root / "nada"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
