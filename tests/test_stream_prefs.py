"""Tests de las preferencias de multi-stream (SDD-M §33, decisión D6).

El §33 propone un TOML con ``[player]``, ``[playback]``, ``[reconnect]`` y
``[diagnostics]``. Aquí viven en ``prefs.json``: mismo formato, mismo cargador,
mismos tests. Lo que se comprueba aquí no es la forma del fichero —eso ya lo
cubrían los tests de :mod:`prefs`— sino tres cosas que el TOML del SDD daría por
supuestas y un JSON no:

1. lo que **no** viene vale un valor conocido-bueno, no ``None`` ni un error;
2. lo que viene **fuera de rango** se acota, porque el fichero lo edita una
   persona con un editor de texto;
3. los **topes que no son el mismo** no se confunden: el de reconexión (§17) es
   5 y el de cambio de reproductor (§26) es 2, y están en módulos distintos a
   propósito.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from thetvview.player.router import MAX_BACKEND_ATTEMPTS
from thetvview.player.supervisor import ReconnectPolicy
from thetvview.prefs import Prefs, PrefsManager


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "prefs.json"
        self.addCleanup(self._tmp.cleanup)
        self.m = PrefsManager(self.path)

    def _escribir(self, datos: dict) -> None:
        self.path.write_text(json.dumps(datos), encoding="utf-8")


class TestValoresPorDefecto(_Tmp):
    def test_sin_fichero_todo_lo_esperable(self) -> None:
        p = self.m.load()
        self.assertIsNone(p.preferred_backend)
        self.assertIsNone(p.connect_timeout)
        self.assertIsNone(p.startup_timeout)
        self.assertEqual(p.reconnect_max_attempts, 5)
        self.assertEqual(p.playback_profile, "balanced")
        self.assertTrue(p.diagnostics_enabled)

    def test_ausente_usa_el_defecto(self) -> None:
        self._escribir({"theme": "dark"})
        p = self.m.load()
        self.assertEqual(p.reconnect_max_attempts, 5)
        self.assertEqual(p.playback_profile, "balanced")
        self.assertTrue(p.diagnostics_enabled)

    def test_vacio_es_el_defecto(self) -> None:
        self._escribir({"preferred_backend": "", "playback_profile": ""})
        p = self.m.load()
        self.assertIsNone(p.preferred_backend)
        self.assertEqual(p.playback_profile, "balanced")


class TestBasuraAcotada(_Tmp):
    """``prefs.json`` lo edita una persona; el cargador no puede confiar."""

    def test_basura_no_rompe(self) -> None:
        self._escribir(
            {
                "preferred_backend": 42,
                "connect_timeout": "no soy un número",
                "startup_timeout": [],
                "reconnect_max_attempts": {"a": 1},
            }
        )
        p = self.m.load()
        self.assertIsNone(p.preferred_backend)
        self.assertIsNone(p.connect_timeout)
        self.assertIsNone(p.startup_timeout)
        self.assertEqual(p.reconnect_max_attempts, 5)

    def test_fuera_de_rango_usa_el_defecto(self) -> None:
        # Un 100000 en reconexión significa una app que no responde; un -1
        # significa lo mismo al revés. En los dos casos, el valor conocido.
        for valor in (-1, 999999, 21):
            with self.subTest(valor=valor):
                self._escribir({"reconnect_max_attempts": valor})
                self.assertEqual(self.m.load().reconnect_max_attempts, 5)

    def test_dentro_de_rango_se_respeta(self) -> None:
        for valor in (0, 1, 5, 20):
            with self.subTest(valor=valor):
                self._escribir({"reconnect_max_attempts": valor})
                self.assertEqual(self.m.load().reconnect_max_attempts, valor)

    def test_perfil_desconocido_usa_el_equilibrado(self) -> None:
        # Un perfil inventado significaría banderas que el reproductor puede
        # no entender. El equilibrado es el comportamiento de siempre.
        for valor in ("turbo", "LOW_LATENCY", "mucha_latencia"):
            with self.subTest(valor=valor):
                self._escribir({"playback_profile": valor})
                self.assertEqual(self.m.load().playback_profile, "balanced")

    def test_timeouts_negativos_usan_el_defecto(self) -> None:
        self._escribir({"connect_timeout": -5, "startup_timeout": -1})
        p = self.m.load()
        self.assertIsNone(p.connect_timeout)
        self.assertIsNone(p.startup_timeout)

    def test_json_roto_no_rompe(self) -> None:
        self.path.write_text("{no es json", encoding="utf-8")
        self.assertEqual(self.m.load().reconnect_max_attempts, 5)


class TestRoundTrip(_Tmp):
    def test_lo_que_se_guarda_vuelve(self) -> None:
        p = Prefs(
            preferred_backend="vlc",
            connect_timeout=8,
            startup_timeout=12,
            reconnect_max_attempts=3,
            playback_profile="low_latency",
            diagnostics_enabled=False,
        )
        self.m.save(p)
        q = self.m.load()
        self.assertEqual(q.preferred_backend, "vlc")
        self.assertEqual(q.connect_timeout, 8)
        self.assertEqual(q.startup_timeout, 12)
        self.assertEqual(q.reconnect_max_attempts, 3)
        self.assertEqual(q.playback_profile, "low_latency")
        self.assertFalse(q.diagnostics_enabled)

    def test_no_se_rompe_lo_que_ya_existia(self) -> None:
        # Un prefs.json de antes (sin estos campos) tiene que seguir
        # leyéndose: es la migración invisible del §32.
        self._escribir({"last_player": "mpv", "theme": "dark"})
        p = self.m.load()
        self.assertEqual(p.last_player, "mpv")
        self.assertEqual(p.theme, "dark")
        self.assertEqual(p.reconnect_max_attempts, 5)


class TestTopesQueNoSonElMismo(_Tmp):
    """El error fácil: confundir §17 con §26."""

    def test_reconexion_y_cambio_de_reproductor_son_distintos(self) -> None:
        # Reconectar **el mismo** reproductor: hasta 5 (§17).
        # Cambiar de reproductor: como mucho 2 (§26).
        politica = ReconnectPolicy(max_attempts=self.m.load().reconnect_max_attempts)
        self.assertEqual(politica.intentos_maximos, 5)
        self.assertEqual(MAX_BACKEND_ATTEMPTS, 2)
        self.assertNotEqual(politica.intentos_maximos, MAX_BACKEND_ATTEMPTS)

    def test_el_tope_malo_sigue_acentuado(self) -> None:
        # Aunque alguien escriba 20 en el fichero, el de cambio de
        # reproductor no se mueve: no está en el fichero.
        self._escribir({"reconnect_max_attempts": 20})
        self.assertEqual(self.m.load().reconnect_max_attempts, 20)
        self.assertEqual(MAX_BACKEND_ATTEMPTS, 2)


if __name__ == "__main__":
    unittest.main()