"""Tests del parser XML endurecido (SDD §38/§39, gap B6)."""

from __future__ import annotations

import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from thetvview.epg_parser import parse_text as parse_epg_text
from thetvview.security.errors import ParseError, ResponseTooLargeError
from thetvview.security.limits import reset_limits, set_limits
from thetvview.security.xml_safe import assert_safe_text, check_tree, parse_xml

FIXTURES = Path(__file__).parent / "fixtures"

# El clásico "billion laughs": 10 niveles x 10 = 10^10 expansiones.
BILLION_LAUGHS = (
    "<!DOCTYPE lolz ["
    '<!ENTITY lol "lol">'
    '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
    '<!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">'
    '<!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">'
    '<!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">'
    '<!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">'
    '<!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">'
    '<!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">'
    "]><lolz>&lol9;</lolz>"
)

# XXE clásico: intenta leer /etc/passwd.
XXE = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
    "<r>&x;</r>"
)


class TestRechazos(unittest.TestCase):
    def tearDown(self) -> None:
        reset_limits()

    def test_doctype_interno_rechazado(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse_xml('<!DOCTYPE tv [<!ATTLIST tv id ID #IMPLIED>]><tv/>')
        self.assertIn("DTD", str(ctx.exception))

    def test_doctype_externo_rechazado(self) -> None:
        with self.assertRaises(ParseError):
            parse_xml('<?xml version="1.0"?><!DOCTYPE tv SYSTEM "x.dtd"><tv/>')

    def test_doctype_case_insensitive(self) -> None:
        with self.assertRaises(ParseError):
            parse_xml("<!DocType tv><tv/>")

    def test_entity_sin_doctype_rechazado(self) -> None:
        with self.assertRaises(ParseError):
            parse_xml("<tv><!ENTITY x 'y'></tv>")

    def test_entity_case_insensitive(self) -> None:
        with self.assertRaises(ParseError):
            parse_xml("<tv><!EnTiTy x 'y'></tv>")

    def test_billion_laughs_rechazado_y_rapido(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ParseError):
            parse_xml(BILLION_LAUGHS)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_xxe_rechazado_antes_de_expasar(self) -> None:
        with self.assertRaises(ParseError):
            parse_xml(XXE)

    def test_assert_safe_text_permite_lo_normal(self) -> None:
        assert_safe_text('<?xml version="1.0"?><tv><channel id="a"/></tv>')
        # Comentarios y CDATA con texto corriente son inocuos.
        assert_safe_text("<tv><!-- hola --><![CDATA[archivo & <titulo>]]></tv>")

    def test_cdata_con_literal_doctype_tambien_se_rechaza(self) -> None:
        # El barrido es sobre el texto crudo: es un falso positivo
        # aceptado (nadie pone «<!DOCTYPE>» dentro de un título de XMLTV)
        # y evita que un comentario o CDATA oculte una declaración real.
        with self.assertRaises(ParseError):
            parse_xml("<tv><![CDATA[<!DOCTYPE x>]]></tv>")


class TestLimites(unittest.TestCase):
    def tearDown(self) -> None:
        reset_limits()

    def test_profundidad_maxima(self) -> None:
        deep = "<a>" * 10 + "x" + "</a>" * 10
        parse_xml(deep, max_depth=20)
        with self.assertRaises(ParseError) as ctx:
            parse_xml(deep, max_depth=3)
        self.assertIn("profundo", str(ctx.exception))

    def test_numero_de_nodos_maximo(self) -> None:
        many = "<r>" + "<c/>" * 50 + "</r>"
        with self.assertRaises(ParseError) as ctx:
            parse_xml(many, max_nodes=10)
        self.assertIn("elementos", str(ctx.exception))

    def test_tamano_en_bytes(self) -> None:
        with self.assertRaises(ResponseTooLargeError):
            parse_xml("<tv>" + "a" * 500 + "</tv>", max_bytes=100)

    def test_tamano_en_texto(self) -> None:
        with self.assertRaises(ResponseTooLargeError):
            parse_xml("<tv/>" * 100, max_bytes=50)

    def test_limits_globales_profundidad(self) -> None:
        set_limits({"max_xml_depth": 2})
        with self.assertRaises(ParseError):
            parse_xml("<a><b><c/></b></a>")

    def test_limits_globales_nodos(self) -> None:
        # max_xml_nodes se recorta a un mínimo de 10 en clamp_value.
        set_limits({"max_xml_nodes": 10})
        with self.assertRaises(ParseError):
            parse_xml("<a>" + "<c/>" * 10 + "</a>")

    def test_limits_globales_tamano(self) -> None:
        # max_file_bytes se recorta a un mínimo de 1024 en clamp_value.
        set_limits({"max_file_bytes": 1024})
        with self.assertRaises(ResponseTooLargeError):
            parse_xml("<tv>" + "a" * 2000 + "</tv>")

    def test_limites_invalidos(self) -> None:
        with self.assertRaises(ParseError):
            parse_xml("<a/>", max_depth=0)


class TestContrato(unittest.TestCase):
    def tearDown(self) -> None:
        reset_limits()

    def test_xml_bien_formado_no_lanza(self) -> None:
        root = parse_xml((FIXTURES / "sample.xmltv").read_bytes())
        self.assertEqual(root.tag, "tv")

    def test_malformado_propaga_et_parseerror(self) -> None:
        with self.assertRaises(ET.ParseError):
            parse_xml("<tv><channel></tv>")

    def test_bytes_con_bom(self) -> None:
        root = parse_xml(b"\xef\xbb\xbf<tv id='x'/>")
        self.assertEqual(root.get("id"), "x")

    def test_tipo_invalido(self) -> None:
        with self.assertRaises(ParseError):
            parse_xml(123)  # type: ignore[arg-type]

    def test_check_tree_directo(self) -> None:
        root = ET.fromstring("<a><b><c/></b></a>")
        check_tree(root, max_depth=3, max_nodes=3)
        with self.assertRaises(ParseError):
            check_tree(root, max_depth=3, max_nodes=2)
        with self.assertRaises(ParseError):
            check_tree(root, max_depth=2, max_nodes=10)


class TestIntegracionEPG(unittest.TestCase):
    def tearDown(self) -> None:
        reset_limits()

    def test_parse_text_normal_sigue_funcionando(self) -> None:
        text = (FIXTURES / "sample.xmltv").read_text(encoding="utf-8")
        epg = parse_epg_text(text)
        self.assertTrue(epg.channels_by_id)

    def test_parse_text_con_doctype_lanza_valueerror(self) -> None:
        # ParseError hereda de ValueError: los callers existentes siguen igual.
        with self.assertRaises(ValueError):
            parse_epg_text('<?xml version="1.0"?><!DOCTYPE tv SYSTEM "x.dtd"><tv/>')

    def test_parse_text_malformado_mensaje_de_antes(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_epg_text("<tv><channel></tv>")
        self.assertIn("XMLTV inválido", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
