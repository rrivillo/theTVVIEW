"""H-01 (integración) — Abrir un canal deja rastro de verdad en el bus.

`test_historial_eventos.py` prueba el traductor con la sesión montada a mano.
Este prueba lo otro, que es lo que importa: que ``play_channel`` —el camino
que de verdad usa el usuario— **ata** la sesión al bus, y que un ciclo completo
produce lo que el DoD de la Fase 2 exige: intento, inicio confirmado, y fallo
o fin.

Y lo que no se debe romper por el camino:

- sin bus (dobles de test, ``App`` sin ``run()``) la reproducción funciona
  exactamente igual;
- un bus roto no impide ver el canal, pero el fallo queda anotado;
- ningún evento lleva la URL del canal ni su credencial.

El.launch se simula como en ``test_playback_session_integrada``: ni red, ni
reproductor real, ni curses.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.diagnostics.events import EventBus, Severity
from thetvview.diagnostics.session_events import ObservadorDeSesion
from thetvview.models import Channel
from thetvview.player import core as player_core
from thetvview.player.evidence import Evidence, EvidenceLevel
from thetvview.player.session import EndReason
from thetvview.ui import app as ui_app
from thetvview.ui import screens as ui_screens


class ProcFalso:
    vivo = True
    pid = 4242

    def poll(self):  # noqa: ANN201
        return None

    def terminate(self) -> None:
        self.vivo = False

    def kill(self) -> None:
        self.vivo = False

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 1


class _BusRoto:
    def emit(self, evento):  # noqa: ANN001, ANN201
        raise RuntimeError("el bus está en llamas")

    def events(self):  # noqa: ANN201
        return []


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
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

    def canal(self) -> Channel:
        # Sin credenciales embebidas a propósito: `validate_url` las rechaza
        # antes de llegar a la sesión, y un canal así no abriría nada. La
        # redacción de secretos se prueba en `test_historial_eventos.py`, con
        # un `StreamError` que sí los trae.
        return Channel(
            name="ESPN HD",
            url="http://proveedor.invalid/live/d/1.ts",
            tvg_id="d.1",
        )

    def abrir(self, bus: object | None = None) -> None:
        """Ejecuta `play_channel` con el lanzamiento simulado."""
        self.app.eventos = bus
        with (
            mock.patch.object(player_core.config, "find_player",
                              return_value="/usr/bin/mpv"),
            mock.patch("thetvview.player.core.subprocess.Popen",
                       return_value=ProcFalso()),
            mock.patch.object(ui_app.config, "find_player",
                              return_value="/usr/bin/mpv"),
        ):
            ui_screens.play_channel(self.app, self.canal(), "mpv")

    def tipos(self, bus: EventBus) -> list[str]:
        return [e.event_type for e in bus.events()]


class TestPlayChannelDejaRastro(Base):
    def setUp(self) -> None:
        super().setUp()
        self.bus = EventBus()

    def test_la_sesion_nace_atada_al_bus(self):
        self.abrir(self.bus)
        self.assertTrue(self.bus.events(), "abrir un canal no dejó ningún evento")

    def test_hay_hito_de_lanzamiento(self):
        self.abrir(self.bus)
        self.assertIn("playback.lanzado", self.tipos(self.bus))

    def test_todo_lo_del_bus_es_de_esta_sesion(self):
        self.abrir(self.bus)
        sesiones = {e.session_id for e in self.bus.events()}
        self.assertEqual(len(sesiones), 1, "un canal, una sesión")

    def test_el_identificador_de_correlacion_es_el_de_la_sesion(self):
        self.abrir(self.bus)
        sesion = self.app._sessions.actual
        for evento in self.bus.events():
            self.assertEqual(evento.correlation_id, sesion.correlation_id)

    def test_el_fin_llega_con_su_motivo(self):
        self.abrir(self.bus)
        self.app._cerrar_recursos()
        fin = [e for e in self.bus.events() if e.event_type == "playback.fin"]
        self.assertEqual(len(fin), 1)
        self.assertEqual(fin[0].reason_code, EndReason.APP_SHUTDOWN.value)
        self.assertIs(fin[0].severity, Severity.INFO)

    def test_el_fin_dice_cuanto_vivio(self):
        self.abrir(self.bus)
        self.app._cerrar_recursos()
        fin = next(e for e in self.bus.events() if e.event_type == "playback.fin")
        self.assertIn("duracion_viva_s", fin.details)

    def test_ningun_evento_lleva_la_url(self):
        self.abrir(self.bus)
        self.app._cerrar_recursos()
        crudo = str([e.to_dict() for e in self.bus.events()])
        self.assertNotIn("proveedor.invalid", crudo)
        self.assertNotIn("http://", crudo)
        # Y lo que sí puede llevar es la clave del canal, que es un identificador.
        self.assertIn("d.1", crudo)

    def test_un_canal_es_una_sesion_pero_el_historial_crece(self):
        self.abrir(self.bus)
        antes = len(self.bus)
        self.abrir(self.bus)
        self.assertGreater(len(self.bus), antes)
        # ...pero la sesión nueva cancela la vieja, y queda escrito.
        tipos = self.tipos(self.bus)
        self.assertIn("playback.fin", tipos)


class TestElInicioConfirmadoExigeMedicion(Base):
    """El dato no aparece solo: hay que medirlo."""

    def setUp(self) -> None:
        super().setUp()
        self.bus = EventBus()
        self.abrir(self.bus)

    def _medir(self, nivel: EvidenceLevel) -> None:
        sesion = self.app._sessions.actual
        observador = sesion.oyente
        self.assertIsInstance(observador, ObservadorDeSesion)
        observador.evidencia(Evidence(level=nivel, reason="test"))

    def test_sin_medir_no_se_confirma(self):
        self.assertNotIn("playback.inicio_confirmado", self.tipos(self.bus))

    def test_una_medida_de_proceso_vivo_no_confirma(self):
        self._medir(EvidenceLevel.PROCESS_ALIVE)
        self.assertNotIn("playback.inicio_confirmado", self.tipos(self.bus))

    def test_una_medida_de_fuente_abierta_no_confirma(self):
        self._medir(EvidenceLevel.SOURCE_ACCEPTED)
        self.assertNotIn("playback.inicio_confirmado", self.tipos(self.bus))

    def test_una_medida_de_medios_si_confirma(self):
        self._medir(EvidenceLevel.MEDIA_SEEN)
        self.assertIn("playback.inicio_confirmado", self.tipos(self.bus))

    def test_confirmar_marca_el_reloj_de_medios_en_la_sesion(self):
        self._medir(EvidenceLevel.MEDIA_SEEN)
        self.assertIsNotNone(self.app._sessions.actual.first_media_at)

    def test_sin_saber_no_confirma(self):
        """`UNKNOWN` es que no se pudo comprobar, no que sí."""
        self._medir(EvidenceLevel.UNKNOWN)
        self.assertNotIn("playback.inicio_confirmado", self.tipos(self.bus))


class TestSinBusNoCambiaNada(Base):
    """Sin bus se reproduce igual. Antes de este ticket, también."""

    def test_play_channel_sin_bus_no_falla(self):
        self.abrir(None)
        self.assertIsNotNone(self.app._sessions.actual)

    def test_la_sesion_sin_bus_no_tiene_oyente(self):
        self.abrir(None)
        self.assertIsNone(self.app._sessions.actual.oyente)

    def test_sin_bus_el_cierre_sigue_sellando(self):
        self.abrir(None)
        # `shutdown()` desregistra la sesión, así que se guarda antes.
        sesion = self.app._sessions.actual
        self.app._cerrar_recursos()
        self.assertTrue(sesion.ended)
        self.assertIs(sesion.end_reason, EndReason.APP_SHUTDOWN)


class TestUnBusRotoNoImpideVer(Base):
    def test_reproducir_aunque_el_bus_reventes(self):
        self.abrir(_BusRoto())
        self.assertFalse(
            self.app._sessions.actual.ended, "no se pudo ver el canal"
        )

    def test_el_problema_queda_anotado_en_el_observador(self):
        self.abrir(_BusRoto())
        observador = self.app._sessions.actual.oyente
        self.assertIsInstance(observador, ObservadorDeSesion)
        self.assertTrue(observador.errores, "el fallo del bus no se anotó")


if __name__ == "__main__":
    unittest.main()