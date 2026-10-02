"""Tests de `thetvview.catchup` (SDD_IPTV_Catchup_Timeshift).

El contrato que se verifica aquí es la **regla de oro** (§21):

    sólo la capacidad declarada por el proveedor habilita catch-up.

Ni el EPG, ni una URL live, ni que otro canal del mismo proveedor tenga
archivo. Y sin ventana declarada, la respuesta es «no».

Los 8 tests obligatorios del §19 se cubren uno a uno en
`TestTestsObligatoriosDelSdd`, con el nombre de su número, para que se
vea el rastro contra el SDD.
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from thetvview import catchup
from thetvview.catchup import (
    DISABLED,
    SECONDS_PER_DAY,
    CatchupCapability,
    CatchupError,
    CatchupNotAvailableError,
    CatchupOutsideArchiveWindowError,
    CatchupRef,
    CatchupRequest,
    CatchupState,
    M3UCatchupAdapter,
    GenericCatchupAdapter,
    XtreamCatchupAdapter,
    can_use_catchup,
    capability_for,
)
from thetvview.epg_parser import parse_text as parse_epg_text
from thetvview.favorites import FavoritesManager
from thetvview.m3u_parser import parse_text as parse_m3u_text
from thetvview.models import Channel, Program
from thetvview.recents import RecentsManager
from thetvview.stream_ref import (
    TS_PREFIX,
    MissingCredentialsError,
    StreamRef,
    resolve_channel_url,
)

NOW = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
PACKAGE_DIR = Path(__file__).resolve().parent.parent / "thetvview"


# --- Utilidades de fixtures -------------------------------------------------


@dataclass
class _Stream:
    """Lo mínimo de `XtreamStream` que consume `from_xtream_stream`."""

    tv_archive: int = 0
    tv_archive_duration: int = 0


def xtream_channel(archive: int = 1, duration: int = 7, **attrs: str) -> Channel:
    """Canal normalizado desde Xtream, con su declaración de archivo."""
    base = {
        "xtream_id": "101",
        "source_name": "Panel",
        "category_id": "5",
        "content_type": "live",
        "tv_archive": str(archive),
        "tv_archive_duration": str(duration),
    }
    base.update(attrs)
    return Channel(
        name="ESPN",
        url=StreamRef("Panel", "live", "101").to_opaque(),
        tvg_id="espanol.espn",
        attrs=base,
    )


def m3u_channel(**attrs: str) -> Channel:
    """Canal de M3U con sus metadatos catchup* ya en `attrs`."""
    return Channel(name="Canal 123", url="http://panel.example.com/live/123.ts",
                   tvg_id="channel.123", attrs=dict(attrs))


def program_ago(*, days: float = 0, hours: float = 0, minutes: float = 90) -> Program:
    """Programa que empezó `days`/`hours` atrás y dura `minutes`."""
    start = NOW - timedelta(days=days, hours=hours)
    return Program(
        channel_id="espanol.espn",
        title="Película",
        start=start,
        stop=start + timedelta(minutes=minutes),
    )


def epg_with_programs(count: int = 5) -> "object":
    """EPG mínimo con la parrilla de un canal, incluido el pasado."""
    lines = ["<tv>", '<channel id="espanol.espn"><display-name>ESPN</display-name></channel>']
    for i in range(count):
        start = NOW - timedelta(hours=count - i)
        stop = start + timedelta(hours=1)
        fmt = "%Y%m%d%H%M%S +0000"
        lines.append(
            f'<programme start="{start.strftime(fmt)}" stop="{stop.strftime(fmt)}" '
            f'channel="espanol.espn"><title>Prog {i}</title></programme>'
        )
    lines.append("</tv>")
    return parse_epg_text("\n".join(lines))


class _Catalogo:
    """Duck-typing de `PlaylistManager.get_credentials()`."""

    def __init__(self, creds: dict | None = None) -> None:
        self.creds = creds or {}

    def get_credentials(self, name: str):
        return self.creds.get(name)


# --- Traducción de la representación del proveedor (§4, §5) ----------------


class TestTraduccionProveedor(unittest.TestCase):
    def test_xtream_7_dias(self) -> None:
        cap = catchup.from_xtream_stream(_Stream(1, 7))
        self.assertTrue(cap.enabled)
        self.assertTrue(cap.provider_declared)
        self.assertEqual(cap.archive_duration_seconds, 7 * SECONDS_PER_DAY)
        self.assertEqual(cap.archive_duration_days, 7)
        self.assertEqual(cap.protocol, catchup.PROTOCOL_XTREAM)

    def test_xtream_sin_declaracion(self) -> None:
        self.assertEqual(catchup.from_xtream_stream(_Stream(0, 0)), DISABLED)

    def test_xtream_declara_pero_sin_ventana_es_fail_closed(self) -> None:
        """Panel que dice `tv_archive=1` sin duración: sin catch-up (§16)."""
        cap = catchup.from_xtream_stream(_Stream(1, 0))
        self.assertFalse(cap.enabled)
        self.assertFalse(cap.provider_declared)
        self.assertIsNone(catchup.archive_start(NOW, cap))

    def test_xtream_acepta_los_tipos_que_manda_un_panel(self) -> None:
        for valor in ("1", "1.0", 1, " 1 "):
            with self.subTest(valor=valor):
                cap = catchup.from_xtream_stream(
                    mock.Mock(tv_archive=valor, tv_archive_duration="7")
                )
                self.assertTrue(cap.enabled)
        for valor in ("", "siete", None, "0"):
            with self.subTest(valor=valor):
                cap = catchup.from_xtream_stream(
                    mock.Mock(tv_archive=1, tv_archive_duration=valor)
                )
                self.assertFalse(cap.enabled)

    def test_m3u_tres_condiciones(self) -> None:
        cap = catchup.from_m3u_attrs({
            "catchup": "append",
            "catchup_days": "7",
            "catchup_source": "http://panel/ts?start={start}&duration={duration}",
        })
        self.assertTrue(cap.enabled)
        self.assertTrue(cap.provider_declared)
        self.assertEqual(cap.archive_duration_days, 7)
        self.assertEqual(cap.protocol, catchup.PROTOCOL_M3U)

    def test_m3u_incompleto_es_fail_closed(self) -> None:
        casos = {
            "sin catchup": {"catchup_days": "7", "catchup_source": "http://p/{start}"},
            "catchup=none": {"catchup": "none", "catchup_days": "7",
                             "catchup_source": "http://p/{start}"},
            "catchup vacío": {"catchup": "", "catchup_days": "7",
                              "catchup_source": "http://p/{start}"},
            "sin days": {"catchup": "append", "catchup_source": "http://p/{start}"},
            "days 0": {"catchup": "append", "catchup_days": "0",
                       "catchup_source": "http://p/{start}"},
            "sin source": {"catchup": "append", "catchup_days": "7"},
        }
        for nombre, attrs in casos.items():
            with self.subTest(caso=nombre):
                self.assertEqual(catchup.from_m3u_attrs(attrs), DISABLED)

    def test_capability_for_despacha_por_el_origen(self) -> None:
        xtream = capability_for(xtream_channel(1, 7))
        m3u = capability_for(m3u_channel(catchup="append", catchup_days="3",
                                          catchup_source="http://p/ts?{start}"))
        self.assertEqual(xtream.protocol, catchup.PROTOCOL_XTREAM)
        self.assertEqual(m3u.protocol, catchup.PROTOCOL_M3U)
        self.assertEqual(m3u.archive_duration_days, 3)

    def test_capability_for_sin_attrs_devuelve_disabled(self) -> None:
        self.assertEqual(capability_for(Channel(name="x", url="http://x/1.ts")), DISABLED)
        self.assertEqual(capability_for(Channel(name="x", url="http://x/1.ts",
                                                attrs={"otracosa": "1"})), DISABLED)

    def test_m3u_parser_deja_los_catchup_en_attrs(self) -> None:
        """H2: el parser no interpreta los catchup*, sólo los transporta."""
        texto = (
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="channel.123" catchup="append" catchup-days="7" '
            'catchup-source="http://panel/ts?start={start}&duration={duration}",Canal 123\n'
            "http://panel.example.com/live/123.ts\n"
            '#EXTINF:-1 tvg-id="channel.999",Canal 999\n'
            "http://panel.example.com/live/999.ts\n"
        )
        playlist = parse_m3u_text(texto)
        con, sin = playlist.channels
        cap = capability_for(con)
        self.assertTrue(cap.enabled)
        self.assertEqual(cap.archive_duration_days, 7)
        self.assertEqual(cap.source_template,
                         "http://panel/ts?start={start}&duration={duration}")
        self.assertEqual(capability_for(sin), DISABLED)

    def test_normalize_live_stream_copia_la_declaracion(self) -> None:
        """H3: tv_archive* viajan a `attrs` (no se descartan)."""
        from thetvview.xtream_models import normalize_live_stream
        from thetvview.xtream_provider import XtreamStream

        stream = XtreamStream(num=1, name="ESPN", stream_id=101,
                              stream_type="live", category_id="1",
                              tv_archive=1, tv_archive_duration=7)
        ch = normalize_live_stream(stream, "http://srv", "u", "secreto",
                                   source_name="Panel")
        self.assertEqual(ch.attrs["tv_archive"], "1")
        self.assertEqual(ch.attrs["tv_archive_duration"], "7")
        self.assertEqual(ch.attrs["source_name"], "Panel")
        cap = capability_for(ch)
        self.assertTrue(cap.enabled)
        self.assertEqual(cap.archive_duration_days, 7)
        self.assertNotIn("secreto", ch.url)


# --- La invariante (§7 / §21) ----------------------------------------------


class TestInvarianteUnica(unittest.TestCase):
    def test_true_solo_con_las_dos_condiciones(self) -> None:
        self.assertTrue(can_use_catchup(CatchupCapability(enabled=True, provider_declared=True)))
        self.assertFalse(can_use_catchup(CatchupCapability(enabled=True, provider_declared=False)))
        self.assertFalse(can_use_catchup(CatchupCapability(enabled=False, provider_declared=True)))
        self.assertFalse(can_use_catchup(CatchupCapability(enabled=False, provider_declared=False)))
        self.assertFalse(can_use_catchup(None))

    def test_la_existencia_de_epg_no_habilita(self) -> None:
        """§20.2 / §16: EPG disponible + tv_archive=0 → deshabilitado."""
        epg = epg_with_programs()
        self.assertIn("espanol.espn", epg.programs)  # sí hay EPG...
        cap = capability_for(xtream_channel(0, 7))
        self.assertFalse(can_use_catchup(cap))  # ...y aun así, no hay archivo

    def test_la_existencia_de_url_live_no_habilita(self) -> None:
        """§20.3: un canal con URL live válida sigue sin catch-up."""
        canal = Channel(name="X", url="http://panel.example.com/live/1.ts")
        self.assertFalse(can_use_catchup(capability_for(canal)))

    def test_otro_canal_del_mismo_proveedor_no_habilita(self) -> None:
        """§20.15: el archivo del canal A no dice nada del canal B."""
        con_archivo = xtream_channel(1, 7)
        attrs = dict(con_archivo.attrs)
        attrs["tv_archive"] = "0"
        attrs["tv_archive_duration"] = "0"
        sin_archivo = Channel(name="Otro", url="xtream://Panel/live/202.ts", attrs=attrs)
        self.assertTrue(can_use_catchup(capability_for(con_archivo)))
        self.assertFalse(can_use_catchup(capability_for(sin_archivo)))

    def test_el_protocolo_xtream_no_implica_catchup(self) -> None:
        """§21: usar Xtream o M3U no equivale a tener archivo."""
        self.assertEqual(capability_for(xtream_channel(0, 0)), DISABLED)
        self.assertEqual(capability_for(m3u_channel(catchup="none")), DISABLED)

    def test_proveedor_sin_informacion_se_trata_como_no_disponible(self) -> None:
        """§20.14: fail-closed."""
        self.assertFalse(can_use_catchup(capability_for(Channel(name="X", url="http://x"))))


# --- Los 8 tests obligatorios del §19 ---------------------------------------


class TestTestsObligatoriosDelSdd(unittest.TestCase):
    def test_1_tv_archive_cero(self) -> None:
        """Test 1: tv_archive = 0 → enabled == False, sin petición histórica."""
        cap = capability_for(xtream_channel(0, 7))
        self.assertFalse(cap.enabled)
        with self.assertRaises(CatchupNotAvailableError):
            catchup.build_playback_request(
                xtream_channel(0, 7), cap,
                CatchupRequest("101", NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
            )

    def test_2_sin_metadatos(self) -> None:
        """Test 2: tv_archive y catchup ausentes → enabled == False."""
        for canal in (Channel(name="X", url="http://x/1.ts"),
                      Channel(name="X", url="http://x/1.ts", attrs={})):
            with self.subTest(canal=canal.name):
                self.assertFalse(capability_for(canal).enabled)

    def test_3_tv_archive_uno_siete_dias(self) -> None:
        """Test 3: tv_archive=1, duration=7 → enabled y ventana de 7 días."""
        cap = catchup.from_xtream_stream(_Stream(1, 7))
        self.assertTrue(cap.enabled)
        self.assertEqual(cap.archive_duration_days, 7)
        self.assertEqual(catchup.archive_start(NOW, cap), NOW - timedelta(days=7))

    def test_4_epg_pasado_sin_catchup(self) -> None:
        """Test 4: evento visible, acción de catch-up oculta, 0 peticiones."""
        epg = epg_with_programs()
        programas = epg.programmes_for("espanol.espn")
        self.assertTrue(programas)  # el evento se ve
        canal = xtream_channel(0, 7)  # pero sin archivo declarado
        for prog in programas:
            with self.subTest(prog=prog.title):
                estado = catchup.classify(canal, prog, NOW)
                self.assertIs(estado, CatchupState.DISABLED_BY_PROVIDER)
                with self.assertRaises(CatchupNotAvailableError):
                    catchup.build_playback_request(
                        canal, capability_for(canal),
                        CatchupRequest("101", prog.start, prog.stop or prog.start),
                    )

    def test_5_programa_dentro_de_ventana(self) -> None:
        """Test 5: archivo de 7 días, evento de hace 2 → disponible."""
        cap = capability_for(xtream_channel(1, 7))
        prog = program_ago(days=2)
        self.assertTrue(catchup.is_within_archive_window(prog, cap, NOW))
        self.assertIs(catchup.classify(xtream_channel(1, 7), prog, NOW),
                      CatchupState.AVAILABLE)

    def test_6_programa_fuera_de_ventana(self) -> None:
        """Test 6: archivo de 7 días, evento de hace 10 → no disponible."""
        cap = capability_for(xtream_channel(1, 7))
        prog = program_ago(days=10)
        self.assertFalse(catchup.is_within_archive_window(prog, cap, NOW))
        self.assertIs(catchup.classify(xtream_channel(1, 7), prog, NOW),
                      CatchupState.OUTSIDE_ARCHIVE_WINDOW)
        with self.assertRaises(CatchupOutsideArchiveWindowError):
            catchup.build_playback_request(
                xtream_channel(1, 7), cap, CatchupRequest("101", prog.start, prog.stop),
            )

    def test_7_seek_dentro_del_programa(self) -> None:
        """Test 7: evento 15:00–16:30, seek 15:45 → empieza ~15:45."""
        inicio = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
        fin = inicio + timedelta(minutes=90)
        seek = inicio + timedelta(minutes=45)
        canal = xtream_channel(1, 7)
        cap = capability_for(canal)
        # El programa es el de las 15:00, pero se pide desde las 15:45.
        prog = Program(channel_id="espanol.espn", title="Película", start=inicio, stop=fin)
        self.assertTrue(catchup.is_within_archive_window(prog, cap, seek))
        playback = catchup.build_playback_request(
            canal, cap, CatchupRequest("101", seek, fin), now=seek,
        )
        ref = CatchupRef.parse(playback.url)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.start_epoch, int(seek.timestamp()))
        self.assertEqual(int(ref.start_datetime().timestamp()), int(seek.timestamp()))
        self.assertEqual(ref.start_datetime(), seek)  # misma zona que el cliente
        self.assertEqual(ref.duration_seconds, 45 * 60)  # hasta el final del programa
        self.assertEqual(playback.duration, 45 * 60)

    def test_8_el_proveedor_cambia_la_capacidad(self) -> None:
        """Test 8: enabled true → false da capacidad false y UI deshabilitada."""
        antes = capability_for(xtream_channel(1, 7))
        despues = capability_for(xtream_channel(0, 7))
        self.assertTrue(can_use_catchup(antes))
        self.assertFalse(can_use_catchup(despues))
        self.assertIs(catchup.classify(xtream_channel(0, 7), program_ago(days=1), NOW),
                      CatchupState.DISABLED_BY_PROVIDER)
        with self.assertRaises(CatchupNotAvailableError):
            catchup.build_playback_request(
                xtream_channel(0, 7), despues,
                CatchupRequest("101", NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
            )


# --- Ventana temporal (§5, FR-004, FR-005) ---------------------------------


class TestVentanaArchivo(unittest.TestCase):
    def setUp(self) -> None:
        self.cap = capability_for(xtream_channel(1, 7))

    def test_intersecha_en_el_borde_inferior(self) -> None:
        inicio = NOW - timedelta(days=7) + timedelta(minutes=5)
        prog = Program("c", "", inicio, inicio + timedelta(minutes=30))
        self.assertTrue(catchup.is_within_archive_window(prog, self.cap, NOW))

    def test_termina_exactamente_en_el_borde_esta_fuera(self) -> None:
        fin = NOW - timedelta(days=7)
        prog = Program("c", "", fin - timedelta(minutes=30), fin)
        self.assertFalse(catchup.is_within_archive_window(prog, self.cap, NOW))

    def test_programa_sin_stop_dentro_de_ventana(self) -> None:
        prog = Program("c", "", NOW - timedelta(hours=1), None)
        self.assertTrue(catchup.is_within_archive_window(prog, self.cap, NOW))

    def test_programa_futuro_no_es_catchup(self) -> None:
        prog = Program("c", "", NOW + timedelta(minutes=5), NOW + timedelta(minutes=90))
        self.assertFalse(catchup.is_within_archive_window(prog, self.cap, NOW))

    def test_naive_se_interpreta_en_hora_local(self) -> None:
        local = NOW.astimezone()
        prog = Program("c", "", local - timedelta(hours=3), local - timedelta(hours=2))
        self.assertTrue(catchup.is_within_archive_window(prog, self.cap, NOW))

    def test_sin_ventana_no_hay_archivo(self) -> None:
        self.assertIsNone(catchup.archive_start(NOW, DISABLED))
        self.assertFalse(
            catchup.is_within_archive_window(program_ago(hours=1), DISABLED, NOW)
        )

    def test_la_capacidad_se_deriva_bajo_demanda_no_se_guarda(self) -> None:
        """H1: `Channel` no crece; la capacidad se calcula al preguntar."""
        canal = xtream_channel(1, 7)
        nombres = {f.name for f in dataclasses.fields(Channel)}
        self.assertNotIn("catchup", nombres)
        self.assertNotIn("catchup_capability", nombres)
        self.assertEqual(
            nombres,
            {"name", "url", "tvg_id", "tvg_name", "tvg_logo", "group", "radio",
             "attrs", "extra_options"},
        )
        # Y sigue funcionando el round-trip asdict/Channel(**raw) de favoritos.
        self.assertEqual(Channel(**dataclasses.asdict(canal)), canal)


# --- Estados y mensajes (§12, §13) -----------------------------------------


class TestEstadosYMensajes(unittest.TestCase):
    def test_mensajes_distinguen_las_tres_situaciones(self) -> None:
        cap = capability_for(xtream_channel(1, 7))
        self.assertIn("no ofrece archivo",
                      catchup.describe_state(CatchupState.DISABLED_BY_PROVIDER))
        fuera = catchup.describe_state(CatchupState.OUTSIDE_ARCHIVE_WINDOW, cap)
        self.assertIn("ya no está disponible", fuera)
        self.assertIn("7", fuera)
        self.assertIn("falló", catchup.describe_state(CatchupState.ERROR))
        # Los tres mensajes son distintos entre sí: §20.13.
        mensajes = {
            catchup.describe_state(CatchupState.DISABLED_BY_PROVIDER),
            fuera,
            catchup.describe_state(CatchupState.ERROR),
        }
        self.assertEqual(len(mensajes), 3)

    def test_mensajes_en_espanol_sin_secrets(self) -> None:
        for state in CatchupState:
            with self.subTest(state=state):
                msg = catchup.describe_state(state, capability_for(xtream_channel(1, 7)))
                self.assertTrue(msg.strip())
                self.assertNotIn("password", msg.lower())
                self.assertNotIn("hunter", msg.lower())

    def test_classify_request_usa_el_mismo_criterio(self) -> None:
        cap = capability_for(xtream_channel(1, 7))
        dentro = CatchupRequest("101", NOW - timedelta(days=2), NOW - timedelta(days=2, hours=-1))
        fuera = CatchupRequest("101", NOW - timedelta(days=10), NOW - timedelta(days=10, hours=-1))
        self.assertIs(catchup.classify_request(cap, dentro, NOW), CatchupState.AVAILABLE)
        self.assertIs(catchup.classify_request(cap, fuera, NOW),
                      CatchupState.OUTSIDE_ARCHIVE_WINDOW)
        self.assertIs(catchup.classify_request(DISABLED, dentro, NOW),
                      CatchupState.DISABLED_BY_PROVIDER)


# --- Adaptadores y construcción (§9, §10, §17) -----------------------------


class TestAdaptadores(unittest.TestCase):
    def test_cada_adaptador_gestiona_su_mecanismo(self) -> None:
        self.assertIsInstance(catchup.adapter_for(capability_for(xtream_channel(1, 7))),
                              XtreamCatchupAdapter)
        m3u_cap = capability_for(m3u_channel(
            catchup="append", catchup_days="7",
            catchup_source="http://panel/ts?start={start}"))
        self.assertIsInstance(catchup.adapter_for(m3u_cap), M3UCatchupAdapter)
        self.assertIsNone(catchup.adapter_for(DISABLED))

    def test_adaptador_generico_nunca_maneja_nada(self) -> None:
        """§17: no hay constructor genérico que pueda producir una URL."""
        generico = GenericCatchupAdapter()
        for cap in (DISABLED,
                    CatchupCapability(enabled=True, provider_declared=True),
                    CatchupCapability(enabled=True, provider_declared=True,
                                      protocol=catchup.PROTOCOL_GENERIC),
                    CatchupCapability(enabled=True, provider_declared=True,
                                      protocol=catchup.PROTOCOL_XTREAM),
                    CatchupCapability(enabled=True, provider_declared=True,
                                      protocol=catchup.PROTOCOL_M3U,
                                      source_template="http://p/{start}")):
            with self.subTest(protocol=cap.protocol):
                self.assertFalse(generico.can_handle(cap))
        with self.assertRaises(CatchupError):
            generico.build(xtream_channel(), DISABLED, CatchupRequest("1", NOW, NOW))

    def test_build_xtream_devuelve_referencia_opaca(self) -> None:
        canal = xtream_channel(1, 7)
        cap = capability_for(canal)
        playback = catchup.build_playback_request(
            canal, cap,
            CatchupRequest("101", NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
            now=NOW,
        )
        self.assertTrue(playback.url.startswith(TS_PREFIX))
        self.assertNotIn("password", playback.url)
        self.assertEqual(playback.duration, 3600)
        self.assertIs(playback.channel, canal)  # el canal original no se toca

    def test_build_m3u_renderiza_la_plantilla_declarada(self) -> None:
        canal = m3u_channel(catchup="append", catchup_days="7", catchup_source=(
            "http://panel.example.com/ts.php?user=1&start={start}"
            "&end={end}&duration={duration}&utc={utc}"))
        cap = capability_for(canal)
        inicio = NOW - timedelta(hours=2)
        playback = catchup.build_playback_request(
            canal, cap, CatchupRequest("123", inicio, inicio + timedelta(minutes=90)),
            now=NOW,
        )
        self.assertIn(f"start={int(inicio.timestamp())}", playback.url)
        self.assertIn(f"end={int((inicio + timedelta(minutes=90)).timestamp())}",
                      playback.url)
        self.assertIn("duration=5400", playback.url)
        self.assertIn("utc=", playback.url)
        self.assertNotIn("{", playback.url)  # ningún placeholder sin sustituir
        self.assertEqual(playback.url.count("&"), 4)

    def test_plantilla_hostil_se_rechaza(self) -> None:
        """Riesgo nº2: `catchup-source` es entrada no confiable de un tercero."""
        for plantilla in (
            "file:///etc/passwd?start={start}",
            "javascript:alert(1)?start={start}",
            "ftp://panel/{start}",
            "-rf --load={start}",
            "smb://panel/share/{start}",
        ):
            with self.subTest(plantilla=plantilla):
                canal = m3u_channel(catchup="append", catchup_days="7",
                                    catchup_source=plantilla)
                with self.assertRaises(CatchupError):
                    catchup.build_playback_request(
                        canal, capability_for(canal),
                        CatchupRequest("123", NOW - timedelta(hours=1), NOW), now=NOW,
                    )

    def test_placeholder_desconocido_es_error_explicito(self) -> None:
        canal = m3u_channel(catchup="append", catchup_days="7",
                            catchup_source="http://p/ts?ep={episodio}")
        with self.assertRaises(CatchupError) as cm:
            catchup.build_playback_request(
                canal, capability_for(canal),
                CatchupRequest("123", NOW - timedelta(hours=1), NOW), now=NOW,
            )
        self.assertIn("episodio", str(cm.exception))
        self.assertIn("start", str(cm.exception))  # dice cuáles reconoce

    def test_plantilla_sin_marcador_de_tiempo_es_error(self) -> None:
        canal = m3u_channel(catchup="append", catchup_days="7",
                            catchup_source="http://p/ts/fijo")
        with self.assertRaises(CatchupError):
            catchup.build_playback_request(
                canal, capability_for(canal),
                CatchupRequest("123", NOW - timedelta(hours=1), NOW), now=NOW,
            )

    def test_plantilla_con_llaves_literales_se_acepta(self) -> None:
        rendered = catchup.render_source_template(
            "http://p/ts/{start}?q={{lito}}", CatchupRequest("1", NOW, NOW),
        )
        self.assertEqual(rendered, f"http://p/ts/{int(NOW.timestamp())}?q={{lito}}")

    def test_plantilla_vacia_o_con_formato_se_rechazan(self) -> None:
        for plantilla in ("", "   ", "http://p/{start:%H}", "http://p/{start!r}",
                          "http://p/{start"):
            with self.subTest(plantilla=plantilla):
                with self.assertRaises(CatchupError):
                    catchup.render_source_template(
                        plantilla, CatchupRequest("1", NOW, NOW))

    def test_valores_de_plantilla_salen_de_nuestros_datetimes(self) -> None:
        inicio = NOW - timedelta(hours=3)
        valores = catchup.template_values(CatchupRequest("1", inicio, NOW))
        self.assertEqual(valores["start"], int(inicio.timestamp()))
        self.assertEqual(valores["end"], int(NOW.timestamp()))
        self.assertEqual(valores["duration"], 3 * 3600)
        self.assertEqual(valores["utc"],
                         inicio.astimezone().strftime("%Y-%m-%d:%H-%M-%S"))
        self.assertNotIn(" ", str(valores["utc"]))  # válido en una query
        self.assertEqual(set(valores), {"start", "end", "duration", "utc"})

    def test_se_valida_antes_de_construir_nunca_al_reves(self) -> None:
        """§10 / §20.9: si la validación falla, no sale ninguna URL."""
        canal = m3u_channel(catchup="append", catchup_days="7",
                            catchup_source="http://p/ts/{start}")
        cap = capability_for(canal)
        with mock.patch.object(M3UCatchupAdapter, "build",
                               autospec=True) as build:
            with self.assertRaises(CatchupNotAvailableError):
                catchup.build_playback_request(canal, DISABLED,
                                               CatchupRequest("1", NOW, NOW), now=NOW)
            with self.assertRaises(CatchupOutsideArchiveWindowError):
                catchup.build_playback_request(
                    canal, cap,
                    CatchupRequest("1", NOW - timedelta(days=30),
                                   NOW - timedelta(days=29)),
                    now=NOW,
                )
            build.assert_not_called()


# --- No sondeo de endpoints (§17) ------------------------------------------

#: Un tramo de path al estilo "sonda": ``/archive/``, ``/catchup/`` o
#: ``/timeshift/`` seguido de barra. Es exactamente la forma que tendría
#: un cliente que prueba endpoints a ciegas (§17). El endpoint **declarado**
#: de Xtream (``/streaming/timeshift.php``) no entra: no es un tramo así y,
#: además, sólo se construye cuando el proveedor lo declara.
_ENDPOINT_PROBE_RE = re.compile(r"/(?:archive|catchup|timeshift)/", re.IGNORECASE)

_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


class TestNoSondeoDeEndpoints(unittest.TestCase):
    """§17: el cliente no prueba endpoints de catch-up a ciegas.

    Se comprueba sobre el AST, no sobre el texto: si mañana alguien
    "optimiza" el módulo con un bucle que prueba Archive, Catchup o
    Timeshift uno detrás de otro hasta que uno responde, este test falla
    aunque el código esté lleno de comentarios de buenas intenciones.

    Los docstrings se excluyen porque no se ejecutan: documentar lo que el
    SDD prohíbe no es infringirlo.
    """

    @staticmethod
    def _docstrings(tree: ast.AST) -> set[int]:
        """ids de los nodos Constant que son docstrings."""
        ids: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, _DOCSTRING_OWNERS) and node.body:
                first = node.body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    ids.add(id(first.value))
        return ids

    def _sondas(self, path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = self._docstrings(tree)
        encontradas: list[str] = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                for match in _ENDPOINT_PROBE_RE.finditer(node.value):
                    encontradas.append(f"{path.name}:{node.lineno} {match.group(0)}")
        return encontradas

    def test_catchup_py_no_trae_ningun_endpoint(self) -> None:
        self.assertEqual(self._sondas(PACKAGE_DIR / "catchup.py"), [],
                         "catchup.py no puede construir endpoints de archivo")

    def test_el_paquete_no_sondea_endpoints_de_archivo(self) -> None:
        """Ningún módulo del paquete escribe una ruta de sonda."""
        sondas: list[str] = []
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            sondas.extend(self._sondas(path))
        self.assertEqual(
            sondas, [],
            "el paquete no debe construir rutas de sonda de archivo (§17); "
            f"encontradas: {sondas}",
        )

    def test_catchup_py_no_hace_red(self) -> None:
        """El módulo es puro: sin red, sin subprocess, sin procesos nuevos."""
        mod = PACKAGE_DIR / "catchup.py"
        tree = ast.parse(mod.read_text(encoding="utf-8"), filename=str(mod))
        prohibidos = {
            "socket", "ssl", "subprocess", "http", "ftplib", "smtplib",
            "telnetlib", "asyncio", "shutil", "pickle",
        }
        vistos: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                vistos.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                vistos.add(node.module.split(".")[0])
        intrusos = sorted(vistos & prohibidos)
        self.assertEqual(intrusos, [],
                         f"catchup.py no debería importar {intrusos}")

    def test_no_se_abre_ninguna_url_en_el_dominio(self) -> None:
        """`urlopen` sólo puede aparecer en el módulo de red."""
        mod = PACKAGE_DIR / "catchup.py"
        fuente = mod.read_text(encoding="utf-8")
        for prohibido in ("urlopen", "SafeHttpClient", "subprocess", "Popen"):
            with self.subTest(token=prohibido):
                self.assertNotIn(prohibido, fuente)


# --- Referencia opaca de catch-up (§2.1) -----------------------------------


class TestReferenciaOpaca(unittest.TestCase):
    def setUp(self) -> None:
        self.ref = CatchupRef("Mi Panel", "101", 1700000000, 3600)
        self.url = self.ref.to_opaque()

    def test_formato(self) -> None:
        self.assertEqual(self.url, "xtream-ts://Mi%20Panel/101.ts?start=1700000000&dur=3600")
        self.assertTrue(self.url.startswith(TS_PREFIX))

    def test_roundtrip(self) -> None:
        self.assertEqual(CatchupRef.parse(self.url), self.ref)

    def test_idempotente(self) -> None:
        otra = CatchupRef.parse(self.url).to_opaque()
        self.assertEqual(otra, self.url)

    def test_no_es_una_url_jugable(self) -> None:
        from thetvview.security.url_policy import PURPOSE_STREAM, validate_url

        with self.assertRaises(Exception):
            validate_url(self.url, PURPOSE_STREAM)

    def test_no_lleva_credenciales(self) -> None:
        from thetvview.security.redaction import contains_embedded_login

        for texto in (self.url, repr(self.ref), CatchupRef("user:pass@x", "1", 1, 1).to_opaque()):
            with self.subTest(texto=texto):
                self.assertNotIn("password", texto)
                self.assertFalse(contains_embedded_login(texto))

    def test_parse_ignora_lo_que_no_es_una_ref(self) -> None:
        for texto in (None, "", "http://x/1.ts", "xtream://Panel/live/1.ts",
                      TS_PREFIX, TS_PREFIX + "rota",
                      TS_PREFIX + "Panel/101.ts",  # sin start
                      TS_PREFIX + "Panel/101?start=1&dur=1",  # sin extensión
                      TS_PREFIX + "Panel/live/101.ts?start=1",  # demasiados tramos
                      TS_PREFIX + "Panel/.ts?start=1"):
            with self.subTest(texto=texto):
                self.assertIsNone(CatchupRef.parse(texto))  # type: ignore[arg-type]

    def test_resolve_con_credenciales_del_catalogo(self) -> None:
        mgr = _Catalogo({"Mi Panel": ("http://srv:8080", "usuario", "p&ss")})
        url = self.ref.resolve(mgr)
        self.assertIn("/streaming/timeshift.php?", url)
        self.assertIn("stream=101", url)
        self.assertIn("duration=3600", url)
        # gap B9: la contraseña con & no inyecta parámetros.
        self.assertNotIn("p&ss=", url)
        self.assertIn("p%26ss", url)

    def test_resolve_sin_credenciales_lanza_error_amigable(self) -> None:
        with self.assertRaises(MissingCredentialsError) as cm:
            self.ref.resolve(_Catalogo())
        self.assertIn("Mi Panel", str(cm.exception))
        self.assertNotIn("p&ss", str(cm.exception))

    def test_resolve_channel_url_despacha_ambos_prefijos(self) -> None:
        mgr = _Catalogo({"Panel": ("http://srv:8080", "u", "p"),
                         "Mi Panel": ("http://srv:8080", "u", "p")})
        # directo
        live = resolve_channel_url(StreamRef("Panel", "live", "101").to_opaque(), mgr)
        self.assertEqual(live, "http://srv:8080/live/u/p/101.ts")
        # archivo
        arch = resolve_channel_url(self.url, mgr)
        self.assertIn("timeshift.php", arch)
        # crudo (M3U): intacto
        self.assertEqual(resolve_channel_url("http://x/1.m3u8", mgr), "http://x/1.m3u8")

    def test_ref_corrupta_no_llega_al_reproductor(self) -> None:
        with self.assertRaises(MissingCredentialsError):
            resolve_channel_url(TS_PREFIX + "Panel/rota", _Catalogo({"x": (1, 2, 3)}))

    def test_stream_ref_no_confunde_los_dos_prefijos(self) -> None:
        self.assertIsNone(StreamRef.parse(self.url))


# --- Persistencia sin credenciales (SDD §20.12) ---------------------------


class TestPersistenciaSinCredenciales(unittest.TestCase):
    _AT = "2026-10-01T20:00:00+00:00"

    def setUp(self) -> None:
        self.ref = CatchupRef("Panel", "101", 1700000000, 3600)
        self.url = self.ref.to_opaque()

    def test_sobrevive_a_favoritos(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "favorites.json"
            favs = FavoritesManager(path)
            self.assertTrue(favs.toggle(Channel(name="ESPN", url=self.url)))
            self.assertTrue(favs.is_favorite(Channel(name="ESPN", url=self.url)))
            crudo = path.read_text(encoding="utf-8")
            self.assertNotIn("password", crudo)
            self.assertIn("1700000000", crudo)
            # Y al releer, la referencia sigue siendo la misma.
            self.assertEqual(favs.load()[0].url, self.url)

    def test_sobrevive_a_recientes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "recents.json"
            mgr = RecentsManager(path)
            mgr.push(Channel(name="ESPN", url=self.url), "mpv")
            crudo = path.read_text(encoding="utf-8")
            self.assertNotIn("password", crudo)
            self.assertEqual(mgr.load()[0].url, self.url)

    def test_un_canal_xtream_no_crece_por_el_catchup(self) -> None:
        """H1: `asdict`/`Channel(**raw)` siguen funcionando sin migración."""
        canal = xtream_channel(1, 7)
        ref_url = self.url
        canal_catchup = Channel(name=canal.name, url=ref_url,
                                tvg_id=canal.tvg_id, attrs=dict(canal.attrs))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "favorites.json"
            favs = FavoritesManager(path)
            favs.toggle(canal)
            favs.toggle(canal_catchup)
            self.assertEqual(len(favs.load()), 2)
            # Las dos referencias son distintas (live vs archivo), y el
            # favorito del directo conserva su identidad.
            self.assertTrue(favs.is_favorite(canal))

    def test_json_de_la_ref_es_reproducible(self) -> None:
        """Una referencia guardada en un `.json` cualquiera sigue parseando."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "catchup.json"
            path.write_text(json.dumps({"url": self.url}), encoding="utf-8")
            url = json.loads(path.read_text(encoding="utf-8"))["url"]
            self.assertEqual(CatchupRef.parse(url), self.ref)


# --- Construcción con credenciales reales (xtream_security) ----------------


class TestBuildTimeshiftUrl(unittest.TestCase):
    def test_url_de_archivo_con_credenciales_percent_encoded(self) -> None:
        from thetvview.xtream_security import build_timeshift_url

        url = build_timeshift_url(
            "srv.example.com:8080", "us er", "p&ss=1", 101, 1700000000, 5400, "ts",
        )
        self.assertTrue(url.startswith("http://srv.example.com:8080/streaming/timeshift.php?"))
        query = url.split("?", 1)[1]
        for clave in ("username=", "password=", "stream=", "start=", "duration=",
                      "extension="):
            self.assertIn(clave, query)
        self.assertIn("username=us%20er", url)
        self.assertIn("password=p%26ss%3D1", url)
        # La contraseña con & = no crea parámetros extra.
        self.assertEqual(query.count("&"), 5)  # 6 parámetros, 5 '&'

    def test_rechaza_instantes_y_duraciones_invalidas(self) -> None:
        from thetvview.xtream_errors import InvalidSourceError
        from thetvview.xtream_security import build_timeshift_url

        with self.assertRaises(InvalidSourceError):
            build_timeshift_url("srv", "u", "p", 1, "no-es-un-instante", 10)
        with self.assertRaises(InvalidSourceError):
            build_timeshift_url("srv", "u", "p", 1, 1700000000, "mucho")

    def test_el_formato_del_start_es_una_constante(self) -> None:
        from thetvview.xtream_security import (
            TIMESHIFT_TIME_FORMAT,
            build_timeshift_url,
        )

        self.assertEqual(TIMESHIFT_TIME_FORMAT, "%Y-%m-%d:%H-%M-%S")
        url = build_timeshift_url("srv", "u", "p", 1, 1700000000, 60)
        stamp = url.split("start=")[1].split("&")[0]
        esperado = datetime.fromtimestamp(1700000000, tz=timezone.utc).astimezone()
        self.assertEqual(stamp, esperado.strftime(TIMESHIFT_TIME_FORMAT))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()