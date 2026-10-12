"""Precisión y rendimiento del EPG (EPG-001…EPG-004, SDD §10).

Lo que se comprueba aquí:

- **equivalencia exacta** entre `now_playing`/`next_programme` con índice y las
  mismas consultas hechas con un escaneo lineal, sobre las mismas fixtures;
- la política temporal: una fecha sin zona se marca, no se disimula;
- una guía vacía o incompleta **no** sustituye a la activa, y el motivo se ve
  en un modal;
- la ambigüedad de nombres se registra y no se fusiona.

Puro: sin red, sin disco, sin curses. El servidor local de `test_epg_url` ya
cubre la descarga.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from thetvview.epg_parser import (
    Epg,
    EpgValidation,
    _parse_ts,
    aviso_zona_pendiente,
    build_index,
    parse_text,
    reinicia_avisos_tz,
)
from thetvview.models import Channel, Program


UTC = timezone.utc


#: Centinela para «sin stop», porque `None` ya significa «sin stop» y hay que
#: poder distinguirlo de «stop = el que toque».
_SIN_STOP = object()


def _prog(cid: str, inicio: datetime, duracion_h: float = 1.0, stop=_SIN_STOP) -> Program:
    fin = inicio + timedelta(hours=duracion_h) if stop is _SIN_STOP else stop
    return Program(channel_id=cid, title=f"P {inicio.isoformat()}", start=inicio, stop=fin)


def _escaneo_lineal(epg: Epg, cid: str, when: datetime):
    """La implementación de antes, palabra por palabra.

    Vive aquí **a propósito**: si el índice y el escaneo dejaran de coincidir,
    el test tiene que poder decir cuál de los dos se equivoca, y eso sólo es
    posible teniendo el escaneo a la vista.
    """
    for prog in epg.programmes_for(cid):
        if prog.start <= when and (prog.stop is None or when < prog.stop):
            return prog
    return None


def _escaneo_lineal_siguiente(epg: Epg, cid: str, when: datetime):
    for prog in epg.programmes_for(cid):
        if prog.start > when:
            return prog
    return None


def _parrilla(n_canales: int, n_prog: int, *, hora: int = 0) -> dict[str, list[Program]]:
    base = datetime(2026, 3, 1, hora, tzinfo=UTC)
    salida: dict[str, list[Program]] = {}
    for c in range(n_canales):
        cid = f"canal.{c}"
        lista = [_prog(cid, base + timedelta(hours=2 * i)) for i in range(n_prog)]
        lista.sort(key=lambda p: p.start)
        salida[cid] = lista
    return salida


# ---------------------------------------------------------------------------
# EPG-002 · el índice es equivalente al escaneo, exactamente
# ---------------------------------------------------------------------------


class TestEquivalenciaDelIndice(unittest.TestCase):
    """El requisito que el plan llama «el riesgo que más daño hace» (§10).

    Un índice mal resuelto devuelve el programa equivocado **sin avisar**, y eso
    es peor que un escaneo lento. Por eso la comparación es exacta, sobre las
    mismas fixtures, y no una aproximación.
    """

    def _epg(self, n_canales: int = 5, n_prog: int = 30) -> Epg:
        programas = _parrilla(n_canales, n_prog)
        canales = {cid: cid.title() for cid in programas}
        return Epg(channels_by_id=canales, programs=programas)

    def test_now_playing_coincide_en_toda_la_linea_de_tiempo(self) -> None:
        epg = self._epg()
        base = datetime(2026, 3, 1, tzinfo=UTC)
        for cid in ("canal.0", "canal.3"):
            for minuto in range(0, 60 * 61, 7):
                when = base + timedelta(minutes=minuto)
                with self.subTest(cid=cid, minuto=minuto):
                    self.assertEqual(
                        epg.now_playing(cid, when),
                        _escaneo_lineal(epg, cid, when),
                    )

    def test_next_programme_coincide_en_toda_la_linea_de_tiempo(self) -> None:
        epg = self._epg()
        base = datetime(2026, 3, 1, tzinfo=UTC)
        for cid in ("canal.0", "canal.4"):
            for minuto in range(0, 60 * 61, 7):
                when = base + timedelta(minutes=minuto)
                with self.subTest(cid=cid, minuto=minuto):
                    self.assertEqual(
                        epg.next_programme(cid, when),
                        _escaneo_lineal_siguiente(epg, cid, when),
                    )

    def test_antes_del_primer_programa_no_hay_nada(self) -> None:
        epg = self._epg()
        antes = datetime(2026, 2, 28, tzinfo=UTC)
        self.assertIsNone(epg.now_playing("canal.0", antes))
        self.assertIsNone(_escaneo_lineal(epg, "canal.0", antes))

    def test_despues_del_ultimo_no_hay_siguiente(self) -> None:
        epg = self._epg(n_prog=5)
        despues = datetime(2026, 4, 1, tzinfo=UTC)
        self.assertIsNone(epg.next_programme("canal.0", despues))

    def test_un_canal_desconocido_no_revienta(self) -> None:
        epg = self._epg()
        self.assertIsNone(epg.now_playing("no.existe", datetime.now(UTC)))
        self.assertIsNone(epg.next_programme("no.existe", datetime.now(UTC)))

    def test_el_ultimo_programa_sin_stop_cuenta_como_abierto(self) -> None:
        """Es el comportamiento de siempre: el último es el que sigue en el aire."""
        base = datetime(2026, 3, 1, tzinfo=UTC)
        cid = "c"
        programas = {
            cid: [
                _prog(cid, base, 1.0, stop=base + timedelta(hours=1)),
                _prog(cid, base + timedelta(hours=1), stop=None),
            ]
        }
        epg = Epg(channels_by_id={cid: "C"}, programs=programas)
        dentro = base + timedelta(hours=5)
        self.assertIsNotNone(epg.now_playing(cid, dentro))
        self.assertEqual(
            epg.now_playing(cid, dentro), _escaneo_lineal(epg, cid, dentro)
        )

    def test_programas_solapados_devuelven_el_mismo_que_el_escaneo(self) -> None:
        """Un proveedor puede publicar dos programas a la vez."""
        base = datetime(2026, 3, 1, tzinfo=UTC)
        cid = "c"
        programas = {
            cid: [
                _prog(cid, base, 3.0),
                _prog(cid, base + timedelta(hours=1), 3.0),
                _prog(cid, base + timedelta(hours=2), 1.0),
            ]
        }
        epg = Epg(channels_by_id={cid: "C"}, programs=programas)
        for horas in range(0, 5):
            when = base + timedelta(hours=horas)
            with self.subTest(horas=horas):
                self.assertEqual(
                    epg.now_playing(cid, when), _escaneo_lineal(epg, cid, when)
                )

    def test_solape_anidado_con_un_programa_largo_de_fondo(self) -> None:
        """El caso que la bisección sin prefijo de paradas se tragaba.

        Un programa largo de fondo con dos cortos encima: mirar sólo el
        programa **inmediatamente anterior** hace que, dentro del corto
        segundo, la bisección devuelva ese corto… pero el escaneo lineal
        devuelve el largo de fondo, que sigue en el aire. Es el fallo
        silencioso que el plan P1 marca como el más dañino: el índice
        responde distinto al escaneo **sin avisar**.
        """
        base = datetime(2026, 3, 1, tzinfo=UTC)
        cid = "c"
        programas = {
            cid: [
                _prog(cid, base, 3.0),                       # 00:00–03:00 largo
                _prog(cid, base + timedelta(hours=1), 0.5),  # 01:00–01:30
                _prog(cid, base + timedelta(hours=2), 0.5),  # 02:00–02:30
                _prog(cid, base + timedelta(hours=5), 1.0),  # 05:00–06:00
            ]
        }
        epg = Epg(channels_by_id={cid: "C"}, programs=programas)
        dentro_del_segundo_corto = base + timedelta(hours=2, minutes=15)
        # El escaneo lineal manda: el largo de fondo es el que está en aire.
        self.assertEqual(
            epg.now_playing(cid, dentro_del_segundo_corto),
            _escaneo_lineal(epg, cid, dentro_del_segundo_corto),
        )
        self.assertEqual(
            epg.now_playing(cid, dentro_del_segundo_corto).start, base
        )
        # En el hueco real (04:00, tras el largo y antes del último) no
        # inventa nada: ni el corto ni el largo siguen en el aire.
        hueco = base + timedelta(hours=4)
        self.assertIsNone(epg.now_playing(cid, hueco))
        self.assertIsNone(_escaneo_lineal(epg, cid, hueco))

    def test_next_programme_en_una_parrilla_desordenada(self) -> None:
        """Antes devolvía el programa **actual**: era el cuerpo de now_playing.

        En un canal desordenado el fallback lineal de `next_programme`
        estaba copiado del de `now_playing`, así que «a continuación»
        devolvía el que ya estaba en el aire. Aquí se comprueba contra el
        escaneo lineal de referencia, que sí mira `start > when`.
        """
        base = datetime(2026, 3, 1, tzinfo=UTC)
        cid = "c"
        programas = {
            cid: [
                _prog(cid, base + timedelta(hours=2), 1.0),
                _prog(cid, base, 1.0),
                _prog(cid, base + timedelta(hours=4), 1.0),
            ]
        }
        epg = Epg(channels_by_id={cid: "C"}, programs=programas)
        self.assertIn(cid, epg.unordered_channels)
        for horas in range(0, 6):
            when = base + timedelta(hours=horas)
            with self.subTest(horas=horas):
                self.assertEqual(
                    epg.next_programme(cid, when),
                    _escaneo_lineal_siguiente(epg, cid, when),
                )
        # Dentro del segundo programa (02:30), el siguiente es el de las 04:00,
        # no el propio.
        dentro = base + timedelta(hours=2, minutes=30)
        self.assertEqual(
            epg.next_programme(cid, dentro).start, base + timedelta(hours=4)
        )

    def test_duracion_cero_no_roba_el_turno_al_siguiente(self) -> None:
        base = datetime(2026, 3, 1, tzinfo=UTC)
        cid = "c"
        programas = {
            cid: [
                _prog(cid, base, 0.0),
                _prog(cid, base, 1.0),
            ]
        }
        programas[cid][1] = _prog(cid, base + timedelta(hours=1), 1.0)
        epg = Epg(channels_by_id={cid: "C"}, programs=programas)
        for horas in (0, 1, 2):
            when = base + timedelta(hours=horas)
            with self.subTest(horas=horas):
                self.assertEqual(
                    epg.now_playing(cid, when), _escaneo_lineal(epg, cid, when)
                )

    def test_una_parrilla_desordenada_se_indexa_igual(self) -> None:
        """El índice ordena; la lista no se toca, así que se compara por inicio."""
        base = datetime(2026, 3, 1, tzinfo=UTC)
        cid = "c"
        programas = {
            cid: [
                _prog(cid, base + timedelta(hours=2), 1.0),
                _prog(cid, base, 1.0),
                _prog(cid, base + timedelta(hours=4), 1.0),
            ]
        }
        epg = Epg(channels_by_id={cid: "C"}, programs=programas)
        for horas in range(0, 6):
            when = base + timedelta(hours=horas)
            with self.subTest(horas=horas):
                self.assertEqual(
                    epg.now_playing(cid, when), _escaneo_lineal(epg, cid, when)
                )

    def test_una_consulta_naive_no_lanza(self) -> None:
        """Un reloj sin zona no puede romper la pantalla.

        Se toma como UTC, que es la política declarada: tratarlo como hora local
        sería inventar una zona que nadie declaró.
        """
        epg = self._epg()
        naive = datetime(2026, 3, 1, 2, 30)  # = 02:30 UTC, dentro de un bloque
        self.assertIsNotNone(epg.now_playing("canal.0", naive))
        self.assertIsNotNone(epg.next_programme("canal.0", naive))

    def test_una_consulta_naive_y_una_aware_dan_el_mismo_programa(self) -> None:
        epg = self._epg()
        naive = datetime(2026, 3, 1, 2, 30)
        self.assertEqual(
            epg.now_playing("canal.0", naive),
            epg.now_playing("canal.0", naive.replace(tzinfo=UTC)),
        )

    def test_una_guia_mezcla_de_zonas_no_rompe_la_busqueda(self) -> None:
        """Un `Epg` construido a mano con zonas mezcladas se indexa sin fallar.

        `parse_text` no puede producir esto (normaliza todo al cargar), pero el
        tipo es público y un importador podría hacerlo.
        """
        base = datetime(2026, 3, 1, 12, tzinfo=UTC)
        cid = "c"
        programas = {
            cid: [
                Program(
                    channel_id=cid,
                    title="b",
                    start=base.replace(tzinfo=None),
                    stop=base.replace(tzinfo=None) + timedelta(hours=1),
                ),
                Program(
                    channel_id=cid,
                    title="a",
                    start=base + timedelta(hours=1),
                    stop=base + timedelta(hours=2),
                ),
            ]
        }
        epg = Epg(channels_by_id={cid: "C"}, programs=programas)
        self.assertEqual(epg.now_playing(cid, base + timedelta(minutes=30)).title, "b")
        self.assertEqual(epg.now_playing(cid, base + timedelta(minutes=90)).title, "a")

    def test_el_indice_se_construye_al_crear_el_epg(self) -> None:
        epg = self._epg(n_canales=3, n_prog=4)
        self.assertEqual(sorted(epg.starts_by_channel), ["canal.0", "canal.1", "canal.2"])
        for cid, inicios in epg.starts_by_channel.items():
            with self.subTest(cid=cid):
                self.assertEqual(inicios, sorted(inicios))
                self.assertEqual(len(inicios), 4)

    def test_build_index_sirve_por_si_misma(self) -> None:
        indice = build_index(_parrilla(2, 3))
        self.assertEqual(len(indice), 2)
        for inicios in indice.values():
            self.assertEqual(inicios, sorted(inicios))


# ---------------------------------------------------------------------------
# EPG-001 · política temporal
# ---------------------------------------------------------------------------


class TestPoliticaTemporal(unittest.TestCase):
    def setUp(self) -> None:
        reinicia_avisos_tz()

    def test_un_offset_se_respeta_y_se_normaliza(self) -> None:
        momento, asumido = _parse_ts("20260825080000 +0200")
        self.assertFalse(asumido)
        self.assertEqual(momento, datetime(2026, 8, 25, 6, 0, tzinfo=UTC))

    def test_sin_offset_se_marca_como_asumido(self) -> None:
        momento, asumido = _parse_ts("20260825080000")
        self.assertTrue(asumido)
        self.assertEqual(momento, datetime(2026, 8, 25, 8, 0, tzinfo=UTC))

    def test_una_fecha_invalida_no_se_adivina(self) -> None:
        momento, asumido = _parse_ts("no es una fecha")
        self.assertIsNone(momento)
        self.assertFalse(asumido)

    def test_fecha_vacia_no_devuelve_nada(self) -> None:
        self.assertEqual(_parse_ts(""), (None, False))
        self.assertEqual(_parse_ts(None), (None, False))

    def test_el_aviso_sale_una_vez_por_fuente(self) -> None:
        primer = aviso_zona_pendiente("https://epg.ejemplo/guia.xml")
        segundo = aviso_zona_pendiente("https://epg.ejemplo/guia.xml")
        self.assertTrue(primer)
        self.assertEqual(segundo, "")

    def test_una_fuente_distinta_avisa(self) -> None:
        aviso_zona_pendiente("https://a/guia.xml")
        self.assertTrue(aviso_zona_pendiente("https://b/guia.xml"))

    def test_el_programa_hereda_la_marca(self) -> None:
        texto = (
            "<tv>"
            '<programme channel="c" start="20260825080000" stop="20260825090000">'
            "<title>Sin zona</title></programme>"
            '<programme channel="c" start="20260825100000 +0200" stop="20260825110000 +0200">'
            "<title>Con zona</title></programme>"
            "</tv>"
        )
        epg = parse_text(texto)
        por_titulo = {p.title: p for p in epg.programs["c"]}
        self.assertTrue(por_titulo["Sin zona"].tz_assumed)
        self.assertFalse(por_titulo["Con zona"].tz_assumed)

    def test_reiniciar_avisos_permite_volver_a_avisar(self) -> None:
        aviso_zona_pendiente("f1")
        reinicia_avisos_tz()
        self.assertTrue(aviso_zona_pendiente("f1"))

    def test_parsear_no_consume_el_aviso(self) -> None:
        """Parsear una guía sin zona no se traga el aviso del usuario.

        Antes `_parse_ts` registraba el aviso al parsear, de modo que cuando
        la UI lo pedía devolvía «» y el modal nunca se abría: el usuario
        veía horas desplazadas sin saberlo. El registro lo hace quien
        **muestra** el aviso, no quien parsea.
        """
        texto = (
            "<tv>"
            '<programme channel="c" start="20260825080000" stop="20260825090000">'
            "<title>Sin zona</title></programme>"
            "</tv>"
        )
        parse_text(texto)
        self.assertTrue(aviso_zona_pendiente("f1"))


# ---------------------------------------------------------------------------
# EPG-001 · intervalos, cruce de medianoche y horario de verano (FR-EPG-007)
# ---------------------------------------------------------------------------


class TestIntervalosYCruces(unittest.TestCase):
    """Lo que se **define** y se prueba con intervalos raros de feeds reales.

    La política vive en el bloque de relleno de `parse_text`: un stop
    imposible (anterior o igual al arranque) se descarta y entra en el
    relleno normal, así que en el modelo nunca aparece un fin anterior al
    inicio. Los solapes, que sí son legales, están en la sección de
    equivalencia, junto al resto de casos que decide la consulta y no el
    parseo.
    """

    def _guia(self, *programas: str) -> Epg:
        return parse_text("<tv>" + "".join(programas) + "</tv>")

    def test_un_stop_anterior_al_inicio_se_acota_con_el_siguiente(self) -> None:
        epg = self._guia(
            '<programme channel="c" start="20260301100000 +0000" '
            'stop="20260301090000 +0000"><title>Roto</title></programme>',
            '<programme channel="c" start="20260301110000 +0000" '
            'stop="20260301120000 +0000"><title>Después</title></programme>',
        )
        roto = epg.programs["c"][0]
        # El intervalo imposible no sobrevive: lo acota el arranque del
        # siguiente, que es el mismo relleno de un stop ausente.
        self.assertEqual(roto.stop, datetime(2026, 3, 1, 11, 0, tzinfo=UTC))
        cuando = datetime(2026, 3, 1, 10, 30, tzinfo=UTC)
        self.assertIs(epg.now_playing("c", cuando), roto)
        self.assertIs(_escaneo_lineal(epg, "c", cuando), roto)

    def test_un_stop_imposible_el_ultimo_queda_abierto(self) -> None:
        epg = self._guia(
            '<programme channel="c" start="20260301100000 +0000" '
            'stop="20260301090000 +0000"><title>Roto</title></programme>',
        )
        self.assertIsNone(epg.programs["c"][0].stop)  # política del último sin stop
        tarde = datetime(2026, 3, 1, 20, 0, tzinfo=UTC)
        self.assertIs(epg.now_playing("c", tarde), epg.programs["c"][0])

    def test_duracion_cero_se_acota_igual(self) -> None:
        """Un stop igual al arranque no cubre nada; al parsear se acota."""
        epg = self._guia(
            '<programme channel="c" start="20260301100000 +0000" '
            'stop="20260301100000 +0000"><title>Cero</title></programme>',
            '<programme channel="c" start="20260301110000 +0000" '
            'stop="20260301120000 +0000"><title>Después</title></programme>',
        )
        cero = epg.programs["c"][0]
        self.assertEqual(cero.stop, datetime(2026, 3, 1, 11, 0, tzinfo=UTC))
        cuando = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
        self.assertIs(epg.now_playing("c", cuando), cero)
        self.assertIs(_escaneo_lineal(epg, "c", cuando), cero)

    def test_cruce_de_medianoche_explicito(self) -> None:
        epg = self._guia(
            '<programme channel="c" start="20260301233000 +0100" '
            'stop="20260302013000 +0100"><title>Madrugada</title></programme>',
        )
        prog = epg.programs["c"][0]
        # 00:30 del día siguiente en el reloj; 23:30 del primero en UTC.
        cruzando = datetime(2026, 3, 1, 23, 30, tzinfo=UTC)
        self.assertIs(epg.now_playing("c", cruzando), prog)
        self.assertIs(
            epg.next_programme("c", datetime(2026, 3, 1, 22, 0, tzinfo=UTC)), prog
        )

    def test_cruce_de_medianoche_por_relleno(self) -> None:
        """El stop ausente se acota con el arranque del día siguiente."""
        epg = self._guia(
            '<programme channel="c" start="20260301230000 +0100">'
            "<title>Sin stop</title></programme>",
            '<programme channel="c" start="20260302010000 +0100" '
            'stop="20260302020000 +0100"><title>Mañana</title></programme>',
        )
        sin_stop = epg.programs["c"][0]
        self.assertEqual(sin_stop.stop, datetime(2026, 3, 2, 0, 0, tzinfo=UTC))
        cruzando = datetime(2026, 3, 1, 23, 30, tzinfo=UTC)  # 00:30 del día 2
        self.assertIs(epg.now_playing("c", cruzando), sin_stop)
        self.assertIs(_escaneo_lineal(epg, "c", cruzando), sin_stop)

    def test_horario_de_verano_la_hora_que_no_existe(self) -> None:
        """2026-03-29: a las 02:00 +0100 se salta a las 03:00 +0200 (Europa)."""
        epg = self._guia(
            '<programme channel="c" start="20260329010000 +0100" '
            'stop="20260329020000 +0100"><title>Antes</title></programme>',
            '<programme channel="c" start="20260329030000 +0200" '
            'stop="20260329040000 +0200"><title>Después</title></programme>',
        )
        antes, despues = epg.programs["c"]
        # En el reloj hay una hora —02:00 a 03:00— que no existe ese día; en
        # UTC los dos tramos son contiguos y la comparación no revienta.
        self.assertEqual(antes.stop, despues.start)
        self.assertIs(
            epg.now_playing("c", datetime(2026, 3, 29, 0, 30, tzinfo=UTC)), antes
        )
        self.assertIs(
            epg.now_playing("c", datetime(2026, 3, 29, 1, 30, tzinfo=UTC)), despues
        )

    def test_horario_de_verano_la_hora_que_se_repite(self) -> None:
        """2026-10-25: a las 03:00 +0200 se vuelve a las 02:00 +0100 (Europa)."""
        epg = self._guia(
            '<programme channel="c" start="20261025020000 +0200" '
            'stop="20261025030000 +0200"><title>Primera 02:00</title></programme>',
            '<programme channel="c" start="20261025020000 +0100" '
            'stop="20261025030000 +0100"><title>Segunda 02:00</title></programme>',
        )
        primera, segunda = epg.programs["c"]
        # Las dos «02:00» son horas de reloj distintas; sin normalizar serían
        # el mismo instante y una taparía a la otra.
        self.assertLess(primera.start, segunda.start)
        self.assertIs(
            epg.now_playing("c", datetime(2026, 10, 25, 0, 30, tzinfo=UTC)), primera
        )
        self.assertIs(
            epg.now_playing("c", datetime(2026, 10, 25, 1, 30, tzinfo=UTC)), segunda
        )
        # El orden del parseo sigue siendo por instante, no por hora de reloj.
        self.assertIs(
            epg.next_programme("c", datetime(2026, 10, 25, 0, 0, tzinfo=UTC)),
            segunda,
        )


# ---------------------------------------------------------------------------
# EPG-003 · validación antes de sustituir
# ---------------------------------------------------------------------------


class TestValidacionDeEpg(unittest.TestCase):
    def test_una_guia_vacia_no_pasa(self) -> None:
        v = Epg(channels_by_id={}, programs={}).validar()
        self.assertFalse(v.ok)
        self.assertTrue(v.vacia)
        self.assertIn("viene vacía", v.motivo)

    def test_canales_sin_programas_no_pasa(self) -> None:
        v = Epg(channels_by_id={"a": "A"}, programs={"a": []}).validar()
        self.assertFalse(v.ok)

    def test_una_guia_normal_pasa(self) -> None:
        v = Epg(channels_by_id={"a": "A"}, programs=_parrilla(1, 3)).validar()
        self.assertTrue(v.ok)
        self.assertGreater(v.programas, 0)

    def test_avisa_de_la_zona_asumida_sin_rechazar(self) -> None:
        base = datetime(2026, 3, 1, tzinfo=UTC)
        programas = {"a": [_prog("a", base, 1.0)]}
        programas["a"][0].tz_assumed = True
        v = Epg(channels_by_id={"a": "A"}, programs=programas).validar()
        self.assertTrue(v.ok)
        self.assertEqual(v.con_zona_asumida, 1)
        self.assertTrue(v.avisos)
        self.assertIn("zona horaria", v.avisos[0])

    def test_una_guia_menor_del_30_por_ciento_no_pasa(self) -> None:
        nueva = EpgValidation(ok=True, canales=5, programas=10)
        veredicto = EpgValidation.contra_creciente(nueva, 1000)
        self.assertFalse(veredicto.ok)
        self.assertIn("parece incompleta", veredicto.motivo)

    def test_una_guia_similara_pasa(self) -> None:
        nueva = EpgValidation(ok=True, canales=900, programas=10)
        self.assertTrue(EpgValidation.contra_creciente(nueva, 1000).ok)

    def test_sin_guia_anterior_todo_pasa(self) -> None:
        nueva = EpgValidation(ok=True, canales=1, programas=1)
        self.assertTrue(EpgValidation.contra_creciente(nueva, 0).ok)

    def test_lo_ya_rechazado_no_se_revalida(self) -> None:
        vacia = EpgValidation(ok=False, motivo="viene vacía")
        self.assertFalse(EpgValidation.contra_creciente(vacia, 1000).ok)


class TestLaAppNoPierdeLaGuiaBuena(unittest.TestCase):
    def setUp(self) -> None:
        from thetvview.ui import app as ui_app

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = self._tmp_path()
        for atributo, nombre in (
            ("PLAYLISTS_JSON", "playlists.json"),
            ("FAVORITES_JSON", "favorites.json"),
            ("PREFS_JSON", "prefs.json"),
            ("RECENTS_JSON", "recents.json"),
            ("THEME_JSON", "theme.json"),
        ):
            parche = mock.patch.object(ui_app.config, atributo, base / nombre)
            parche.start()
            self.addCleanup(parche.stop)
        self.app = ui_app.App(None)  # noqa: ANN001
        self.buena = Epg(channels_by_id={"c": "C"}, programs=_parrilla(1, 20))
        self.app.epg = self.buena
        self.app.epg_source = "https://epg.ejemplo/guia.xml"

    def _tmp_path(self):
        from pathlib import Path

        return Path(self._tmp.name)

    def _canal(self) -> Channel:
        return Channel(name="C", url="http://x/c.ts", tvg_id="canal.0")

    def test_una_guia_vacia_no_reemplaza_a_la_activa(self) -> None:
        vacia = Epg(channels_by_id={}, programs={})
        with mock.patch("thetvview.ui.app.load_epg_source", return_value=vacia):
            self.app.ensure_epg(self._canal(), force_refresh=True)
        self.assertTrue(self.app.epg is self.buena)
        self.assertIn("viene vacía", self.app.status.message)
        self.assertTrue(self.app.status.is_error)

    def test_una_guia_truncada_no_reemplaza_a_la_activa(self) -> None:
        corta = Epg(channels_by_id={"c": "C"}, programs=_parrilla(1, 2))
        with mock.patch("thetvview.ui.app.load_epg_source", return_value=corta):
            self.app.ensure_epg(self._canal(), force_refresh=True)
        self.assertTrue(self.app.epg is self.buena)
        self.assertIn("parece incompleta", self.app.status.message)

    def test_se_abre_un_modal_explicando_que_se_conserva(self) -> None:
        vacia = Epg(channels_by_id={}, programs={})
        with mock.patch("thetvview.ui.app.load_epg_source", return_value=vacia), \
             mock.patch.object(self.app, "_show_notice") as modal:
            self.app.ensure_epg(self._canal(), force_refresh=True)
        self.assertTrue(modal.called)
        titulos = [llamada[0][0] for llamada in modal.call_args_list]
        mensajes = " ".join(llamada[0][1] for llamada in modal.call_args_list)
        self.assertTrue(any("guía" in t.lower() for t in titulos))
        self.assertIn("conserva", mensajes)

    def test_una_guia_valida_si_reemplaza(self) -> None:
        buena = Epg(channels_by_id={"c": "C"}, programs=_parrilla(1, 25))
        with mock.patch("thetvview.ui.app.load_epg_source", return_value=buena):
            self.app.ensure_epg(self._canal(), force_refresh=True)
        self.assertTrue(self.app.epg is buena)
        self.assertEqual(len(self.app.epg.programs["canal.0"]), 25)

    def test_el_reporte_de_una_actualizacion_automatica_tambien_protege(self) -> None:
        """El camino en segundo plano (_drain_tasks) protege igual que el manual."""
        from thetvview.ui.app import _EpgLoadOutcome

        vacia = Epg(channels_by_id={}, programs={})
        with mock.patch.object(self.app, "_show_notice"):
            self.app._tasks.submit(
                "epg-load",
                lambda token, srcs, allow_private=False: _EpgLoadOutcome(
                    epg=vacia, source="https://epg.ejemplo/guia.xml"
                ),
                ["https://epg.ejemplo/guia.xml"],
            ).join(2.0)
            self.app._drain_tasks()
        self.assertTrue(self.app.epg is self.buena)


# ---------------------------------------------------------------------------
# EPG-004 · ambigüedad y método de correspondencia
# ---------------------------------------------------------------------------


class TestCorrespondenciaYAmbiguedad(unittest.TestCase):
    def test_identificador_exacto_tiene_prioridad(self) -> None:
        from thetvview.epg_match import EpgIndex

        epg = Epg(
            channels_by_id={"real": "Canal Real"},
            programs={"real": [_prog("real", datetime(2026, 3, 1, tzinfo=UTC), 1.0)]},
        )
        canal = Channel(name="Otro nombre", url="http://x/1.ts", tvg_id="real")
        self.assertEqual(EpgIndex(epg).resolve(canal), "real")

    def test_el_nombre_normalizado_es_el_fallback(self) -> None:
        from thetvview.epg_match import EpgIndex

        epg = Epg(
            channels_by_id={"x": "Canal  Uno"},
            programs={"x": [_prog("x", datetime(2026, 3, 1, tzinfo=UTC), 1.0)]},
        )
        canal = Channel(name="canal  uno", url="http://x/1.ts")
        self.assertEqual(EpgIndex(epg).resolve(canal), "x")

    def test_un_canal_sin_correspondencia_no_inventa(self) -> None:
        from thetvview.epg_match import EpgIndex

        epg = Epg(
            channels_by_id={"x": "Otro"},
            programs={"x": [_prog("x", datetime(2026, 3, 1, tzinfo=UTC), 1.0)]},
        )
        canal = Channel(name="Desconocido", url="http://x/1.ts")
        self.assertIsNone(EpgIndex(epg).resolve(canal))

    def test_la_normalizacion_de_nombres_no_es_agresiva(self) -> None:
        from thetvview.epg_match import normalize_key

        self.assertNotEqual(normalize_key("Canal 1 HD"), normalize_key("Canal 1"))
        self.assertNotEqual(normalize_key("CNN"), normalize_key("CNN International"))
        self.assertNotEqual(normalize_key("Televisión"), normalize_key("Television"))

    def test_la_etiqueta_original_se_conserva(self) -> None:
        """Normalizar es para comparar, no para mostrar."""
        epg = Epg(
            channels_by_id={"x": "Canal  Ünö"},
            programs={"x": [_prog("x", datetime(2026, 3, 1, tzinfo=UTC), 1.0)]},
        )
        self.assertEqual(epg.channel_name("x"), "Canal  Ünö")

    # -- alias de nombres (FR-EPG-002) --------------------------------------

    def test_parsear_guarda_los_display_name_alternativos(self) -> None:
        texto = (
            "<tv>"
            '<channel id="c1"><display-name>Canal Uno</display-name>'
            '<display-name lang="en">Channel One</display-name>'
            "<display-name>C Uno</display-name></channel>"
            '<channel id="c2"><display-name>Canal Dos</display-name></channel>'
            '<channel id="c3"><display-name>Repetido</display-name></channel>'
            '<channel id="c3"><display-name>Repetido</display-name></channel>'
            "</tv>"
        )
        epg = parse_text(texto)
        # El primero es el canónico: es la etiqueta que se muestra.
        self.assertEqual(epg.channels_by_id["c1"], "Canal Uno")
        # El resto son alias de búsqueda, sin repetirse ni repetir el canónico.
        self.assertEqual(epg.aliases_by_id["c1"], ["Channel One", "C Uno"])
        self.assertNotIn("c2", epg.aliases_by_id)
        self.assertNotIn("c3", epg.aliases_by_id)

    def test_un_alias_del_display_name_alternativo_casa(self) -> None:
        from thetvview.epg_match import EpgIndex, METODO_ALIAS

        epg = Epg(
            channels_by_id={"x": "Channel One"},
            aliases_by_id={"x": ["Canal Uno"]},
            programs={"x": [_prog("x", datetime(2026, 3, 1, tzinfo=UTC), 1.0)]},
        )
        canal = Channel(name="Canal Uno", url="http://x/1.ts")
        match = EpgIndex(epg).resolve_match(canal)
        self.assertEqual(match.channel_id, "x")
        self.assertEqual(match.method, METODO_ALIAS)
        self.assertTrue(match.ok)
        self.assertFalse(match.ambiguo)

    def test_si_la_tvg_name_no_esta_en_la_guia_se_prueba_el_nombre(self) -> None:
        from thetvview.epg_match import EpgIndex, METODO_NOMBRE

        epg = Epg(
            channels_by_id={"x": "Canal Viejo"},
            programs={"x": [_prog("x", datetime(2026, 3, 1, tzinfo=UTC), 1.0)]},
        )
        canal = Channel(name="Canal Viejo", url="u", tvg_name="Canal Nuevo")
        match = EpgIndex(epg).resolve_match(canal)
        self.assertEqual(match.channel_id, "x")
        self.assertEqual(match.method, METODO_NOMBRE)

    # -- método de la correspondencia (FR-EPG-004) --------------------------

    def test_el_metodo_explica_por_que_caso(self) -> None:
        from thetvview.epg_match import (
            EpgIndex,
            METODO_IDENTIFICADOR,
            METODO_NOMBRE,
            METODO_SIN,
        )

        epg = Epg(
            channels_by_id={"x": "Canal Uno"},
            programs={"x": [_prog("x", datetime(2026, 3, 1, tzinfo=UTC), 1.0)]},
        )
        indice = EpgIndex(epg)
        por_id = indice.resolve_match(Channel(name="Nada que ver", url="u", tvg_id="x"))
        self.assertEqual((por_id.channel_id, por_id.method), ("x", METODO_IDENTIFICADOR))
        por_nombre = indice.resolve_match(Channel(name="canal uno", url="u"))
        self.assertEqual((por_nombre.channel_id, por_nombre.method), ("x", METODO_NOMBRE))
        self.assertEqual(por_nombre.clave, "canal uno")
        sin = indice.resolve_match(Channel(name="Desconocido", url="u"))
        self.assertEqual((sin.channel_id, sin.method), (None, METODO_SIN))
        self.assertFalse(sin.ok)
        self.assertEqual(sin.clave, "")

    # -- ambigüedad (FR-EPG-003) --------------------------------------------

    def test_dos_canales_con_el_mismo_nombre_no_se_fusionan(self) -> None:
        from thetvview.epg_match import EpgIndex

        base = datetime(2026, 3, 1, tzinfo=UTC)
        epg = Epg(
            channels_by_id={"sd": "Canal 1", "hd": "Canal 1"},
            programs={"sd": [_prog("sd", base)], "hd": [_prog("hd", base)]},
        )
        indice = EpgIndex(epg)
        match = indice.resolve_match(Channel(name="Canal 1", url="u"))
        # Se resuelve el de siempre (el primero de la guía), no uno aleatorio…
        self.assertEqual(match.channel_id, "sd")
        # …pero queda claro que hubo que elegir, y entre qué.
        self.assertTrue(match.ambiguo)
        self.assertEqual(match.candidatos, ("sd", "hd"))
        # …y la clave queda registrada, no sólo decidida.
        self.assertEqual(indice.ambiguos["canal 1"], ("sd", "hd"))

    def test_la_ambiguedad_no_influye_en_quien_tiene_identificador(self) -> None:
        from thetvview.epg_match import EpgIndex

        base = datetime(2026, 3, 1, tzinfo=UTC)
        epg = Epg(
            channels_by_id={"sd": "Canal 1", "hd": "Canal 1"},
            programs={"sd": [_prog("sd", base)], "hd": [_prog("hd", base)]},
        )
        indice = EpgIndex(epg)
        match = indice.resolve_match(Channel(name="Canal 1", url="u", tvg_id="hd"))
        self.assertEqual(match.channel_id, "hd")  # la identidad manda
        self.assertFalse(match.ambiguo)  # el tvg-id no admite dudas

    def test_un_canal_que_repite_su_nombre_como_alias_no_es_ambiguo(self) -> None:
        from thetvview.epg_match import EpgIndex

        epg = Epg(
            channels_by_id={"x": "Canal Uno"},
            aliases_by_id={"x": ["Canal Uno"]},  # feed redundante, no dudoso
            programs={"x": [_prog("x", datetime(2026, 3, 1, tzinfo=UTC), 1.0)]},
        )
        indice = EpgIndex(epg)
        match = indice.resolve_match(Channel(name="Canal Uno", url="u"))
        self.assertFalse(match.ambiguo)
        self.assertEqual(match.candidatos, ("x",))
        self.assertEqual(dict(indice.ambiguos), {})

    def test_auditar_registra_lo_que_no_casa_y_lo_que_no_se_usa(self) -> None:
        from thetvview.epg_match import auditar

        base = datetime(2026, 3, 1, tzinfo=UTC)
        cids = ("a", "b1", "b2", "sin_uso")
        epg = Epg(
            channels_by_id={
                "a": "Canal A",
                "b1": "Canal Duo",
                "b2": "Canal Duo",
                "sin_uso": "Nadie Lo Pide",
            },
            programs={cid: [_prog(cid, base)] for cid in cids},
        )
        canales = [
            Channel(name="Canal A", url="u1"),
            Channel(name="Canal Duo", url="u2"),
            Channel(name="Fantasma", url="u3"),
        ]
        informe = auditar(epg, canales)
        self.assertEqual(informe.sin_epg, ("Fantasma",))
        # `b2` sólo era un candidato homónimo: ningún canal es ese id.
        self.assertEqual(informe.epg_sin_canal, ("b2", "sin_uso"))
        self.assertEqual(informe.ambiguos, {"canal duo": ("b1", "b2")})

    def test_auditar_sin_guia_devuelve_el_retrato_vacio(self) -> None:
        from thetvview.epg_match import auditar

        informe = auditar(None, [Channel(name="X", url="u")])
        self.assertEqual(informe.sin_epg, ())
        self.assertEqual(informe.epg_sin_canal, ())
        self.assertEqual(informe.ambiguos, {})


if __name__ == "__main__":
    unittest.main()