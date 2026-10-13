"""M3U-001 · FR-M3U-001: política de codificación explícita.

La lista la escribió otro y puede venir en cualquier cosa. Lo que se
comprueba aquí:

- el BOM UTF-8 se come él solo y no ensucia el primer ``#EXTM3U``;
- una lista heredada (cp1252/latin-1) se lee **bien**, y la decisión
  queda registrada con aviso de modal;
- una lista indescifrable sustituye, pero **no en silencio**: cuenta, se
  avisa y queda en el registro;
- el aviso se ve **una vez**, no en cada carga.
"""

from __future__ import annotations

import codecs
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview import m3u_encoding, m3u_parser
from thetvview.m3u_encoding import (
    CODIFICACIONES_HEREDADAS,
    DecisionCodificacion,
    avisos_recientes,
    decodificar_m3u,
    texto_aviso_pendiente,
)


class TestDecodificar(unittest.TestCase):
    def setUp(self) -> None:
        m3u_encoding.AVISOS.clear()

    def test_utf8_sin_bom(self) -> None:
        decision = decodificar_m3u("#EXTM3U\n#EXTINF:-1,Ñandú\n".encode("utf-8"))
        self.assertEqual(decision.codificacion, "utf-8")
        self.assertIn("Ñandú", decision.texto)
        self.assertFalse(decision.debe_avisar())
        self.assertIsNone(decision.aviso())

    def test_bom_utf8_se_consume(self) -> None:
        crudo = codecs.BOM_UTF8 + "#EXTM3U\n#EXTINF:-1,A\n".encode("utf-8")
        decision = decodificar_m3u(crudo)
        self.assertEqual(decision.codificacion, "utf-8-sig")
        # El BOM no puede aparecer pegado al #EXTM3U.
        self.assertTrue(decision.texto.startswith("#EXTM3U"))
        self.assertNotIn("﻿", decision.texto)
        self.assertFalse(decision.debe_avisar())

    def test_cp1252_heredada(self) -> None:
        crudo = "#EXTINF:-1,Canción Ñandú\n".encode("cp1252")
        decision = decodificar_m3u(crudo)
        self.assertEqual(decision.codificacion, "cp1252")
        self.assertIn("Canción Ñandú", decision.texto)
        self.assertTrue(decision.es_heredada)
        self.assertFalse(decision.hubo_sustitucion)
        aviso = decision.aviso()
        assert aviso is not None
        self.assertIn("cp1252", aviso)

    def test_latin1_cuando_cp1252_no_alcanza(self) -> None:
        # 0x81 no existe en cp1252: obliga a caer a latin-1 sin perder nada.
        crudo = b"#EXTINF:-1,Ca" + bytes([0x81]) + b"non\n"
        decision = decodificar_m3u(crudo)
        self.assertEqual(decision.codificacion, "latin-1")
        self.assertFalse(decision.hubo_sustitucion)
        self.assertIn(decision.codificacion, decision.aviso() or "")

    def test_reemplazo_nunca_silencioso(self) -> None:
        """Si ninguna codificación encaja, se sustituye **y se dice**."""
        crudo = b"#EXTINF:-1,Ca\xfffe\n"
        # Sin heredadas que valgan, sólo queda el reemplazo explícito.
        with mock.patch.object(m3u_encoding, "CODIFICACIONES_HEREDADAS", ()):
            decision = decodificar_m3u(crudo)
        self.assertEqual(decision.codificacion, "utf-8")
        self.assertGreater(decision.sustituciones, 0)
        self.assertTrue(decision.hubo_sustitucion)
        aviso = decision.aviso()
        assert aviso is not None
        self.assertIn("sustituido", aviso)
        # Y quedó registrado: el evento de la decisión, no un secreto.
        self.assertEqual(len(avisos_recientes()), 1)

    def test_utf8_invalido_no_se_disfraza_de_utf8(self) -> None:
        """Bytes que no son UTF-8 se dicen heredados, no «utf-8 con ruidos»."""
        crudo = b"#,M\xc3\xa9s\xff\n"
        decision = decodificar_m3u(crudo)
        self.assertNotEqual(decision.codificacion, "utf-8")
        # Se leyó sin perder bytes: heredada, no sustituida.
        self.assertFalse(decision.hubo_sustitucion)
        self.assertNotIn("�", decision.texto)
        self.assertTrue(decision.texto.startswith("#,M"))

    def test_registro_acotado(self) -> None:
        self.assertGreater(m3u_encoding.AVISOS.maxlen, 0)
        for _ in range(m3u_encoding.AVISOS.maxlen + 10):
            m3u_encoding.registrar(
                DecisionCodificacion(texto="x", codificacion="cp1252", origen="a.m3u")
            )
        self.assertEqual(len(avisos_recientes()), m3u_encoding.AVISOS.maxlen)

    def test_el_aviso_se_entrega_una_sola_vez(self) -> None:
        m3u_encoding.registrar(
            DecisionCodificacion(texto="x", codificacion="cp1252", origen="a.m3u")
        )
        self.assertIsNotNone(texto_aviso_pendiente())
        self.assertIsNone(texto_aviso_pendiente())

    def test_utf8_no_genera_registro(self) -> None:
        m3u_encoding.registrar(DecisionCodificacion(texto="x", codificacion="utf-8"))
        self.assertEqual(len(avisos_recientes()), 0)
        self.assertIsNone(texto_aviso_pendiente())

    def test_jerarquia_de_codificaciones(self) -> None:
        self.assertEqual(CODIFICACIONES_HEREDADAS[0], "cp1252")
        self.assertEqual(CODIFICACIONES_HEREDADAS[-1], "latin-1")


class TestParseFileConCodificacion(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        m3u_encoding.AVISOS.clear()

    def test_cp1252_se_parsea_con_acentos(self) -> None:
        ruta = self.dir / "heredada.m3u"
        ruta.write_bytes(
            "#EXTM3U\n#EXTINF:-1 tvg-id=\"c1\",Canción Ñandú\nhttp://x/1.ts\n".encode(
                "cp1252"
            )
        )
        pl = m3u_parser.parse_file(ruta)
        self.assertEqual(len(pl.channels), 1)
        self.assertEqual(pl.channels[0].name, "Canción Ñandú")
        # Y queda constancia de cómo se leyó.
        self.assertIsNotNone(texto_aviso_pendiente())

    def test_bom_no_deja_basura_en_el_id(self) -> None:
        ruta = self.dir / "bom.m3u"
        ruta.write_bytes(
            codecs.BOM_UTF8 + '#EXTM3U\n#EXTINF:-1 tvg-id="c1",Uno\nhttp://x/1.ts\n'.encode(
                "utf-8"
            )
        )
        pl = m3u_parser.parse_file(ruta)
        self.assertEqual(pl.channels[0].tvg_id, "c1")
        self.assertEqual(pl.channels[0].name, "Uno")

    def test_sustitucion_no_es_silenciosa(self) -> None:
        ruta = self.dir / "raro.m3u"
        ruta.write_bytes(b"#EXTM3U\n#EXTINF:-1,Ca\xfffe\nhttp://x/1.ts\n")
        with mock.patch.object(m3u_encoding, "CODIFICACIONES_HEREDADAS", ()):
            pl = m3u_parser.parse_file(ruta)
        # Se lee igual (nunca se rompe), pero avisado.
        self.assertEqual(len(pl.channels), 1)
        aviso = texto_aviso_pendiente()
        assert aviso is not None
        self.assertIn("raro.m3u", aviso)


class TestAvisoEnLaTUI(unittest.TestCase):
    """El aviso sale en un modal a través de `load_playlist_source`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        m3u_encoding.AVISOS.clear()

    def _escribir(self) -> Path:
        ruta = self.dir / "heredada.m3u"
        ruta.write_bytes(
            "#EXTM3U\n#EXTINF:-1,Canción\nhttp://x/1.ts\n".encode("cp1252")
        )
        return ruta

    def test_el_modal_recibe_el_aviso(self) -> None:
        from thetvview.ui.app import load_playlist_source

        vistos: list[str] = []
        playlist = load_playlist_source(str(self._escribir()), aviso=vistos.append)
        self.assertEqual(len(playlist.channels), 1)
        self.assertEqual(len(vistos), 1)
        self.assertIn("cp1252", vistos[0])

    def test_el_callback_global_de_la_tui(self) -> None:
        from thetvview.ui import app as app_mod

        vistos: list[str] = []
        app_mod._aviso_codificacion = vistos.append
        self.addCleanup(setattr, app_mod, "_aviso_codificacion", None)
        app_mod.load_playlist_source(str(self._escribir()))
        self.assertEqual(len(vistos), 1)
        # Cada carga es una decisión nueva: volver a abrir una lista
        # heredada vuelve a avisar (si no, el aviso mentiría: el problema
        # sigue ahí). Lo que no se repite es el **mismo** aviso pendiente.
        app_mod.load_playlist_source(str(self._escribir()))
        self.assertEqual(len(vistos), 2)
        # Una lista en UTF-8 no abre modal.
        moderna = self.dir / "moderna.m3u"
        moderna.write_text("#EXTM3U\n#EXTINF:-1,Ñ\nhttp://x/1.ts\n", encoding="utf-8")
        app_mod.load_playlist_source(str(moderna))
        self.assertEqual(len(vistos), 2)

    def test_utf8_no_abre_modal(self) -> None:
        from thetvview.ui.app import load_playlist_source

        ruta = self.dir / "moderna.m3u"
        ruta.write_text(
            "#EXTM3U\n#EXTINF:-1,Ñandú\nhttp://x/1.ts\n", encoding="utf-8"
        )
        vistos: list[str] = []
        load_playlist_source(str(ruta), aviso=vistos.append)
        self.assertEqual(vistos, [])


if __name__ == "__main__":
    unittest.main()
