"""Migración de los hilos heredados al gestor de tareas (TASK-001).

El gestor ya existía y el EPG ya lo usaba, pero tres hilos seguían con su
propio ``threading.Thread``: el warming del catálogo, el sondeo de pistas y el
monitor de salud del canal. Estos tests comprueban que los tres:

- **arrancan** y hacen su trabajo (que no es lo mismo que «no se rompe»);
- **se cancelan** de verdad, sin dejar trabajo a medias;
- **se paran con plazo** al cerrar;
- ya **no crean hilos propios** que el cierre pueda no alcanzar.

Ese último punto es el que distingue «migrado» de «envuelto en una función».
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from thetvview.channel_health import ChannelHealthMonitor
from thetvview.models import Channel, Playlist
from thetvview.streams.probe import ProbeResult, TrackProbe
from thetvview.tasks.manager import TaskManager


class TestTrackProbeMigrado(unittest.TestCase):
    """El sondeo de pistas usa el gestor, no un hilo suelto."""

    def setUp(self) -> None:
        self.tm = TaskManager()
        self.addCleanup(self.tm.shutdown)

    def _probe(self) -> TrackProbe:
        probe = TrackProbe(
            Channel(name="C", url="http://a.test/master.m3u8"),
            allow_private=True,
            timeout=1.0,
            use_cache=False,
            cache_dir=None,
        )
        probe._task_manager = self.tm
        return probe

    def test_arranca_y_entrega_resultado(self) -> None:
        probe = self._probe()
        recibidos: list[ProbeResult] = []
        with mock.patch(
            "thetvview.streams.probe.probe_capabilities",
            return_value=ProbeResult(capabilities=mock.Mock()),
        ):
            probe.start(recibidos.append)
            for _ in range(100):
                if recibidos:
                    break
                time.sleep(0.02)
        self.assertEqual(len(recibidos), 1)
        probe.stop()

    def test_las_tareas_van_al_gestor_inyectado(self) -> None:
        probe = self._probe()
        with mock.patch(
            "thetvview.streams.probe.probe_capabilities",
            return_value=ProbeResult(capabilities=mock.Mock()),
        ):
            probe.start(lambda r: None)
            # El trabajo se ve en el gestor, no en un hilo propio.
            for _ in range(100):
                if self.tm.active_for("track-probe") or probe.result is not None:
                    break
                time.sleep(0.02)
        self.assertIsNotNone(probe._handle)
        probe.stop()

    def test_stop_cancela_en_vez_de_soltar(self) -> None:
        """``stop`` pide cancelación: no espera a que el sondeo acabe."""
        probe = self._probe()
        arrancado = threading.Event()

        def lento(_channel, **_kwargs):
            arrancado.set()
            time.sleep(30)
            return ProbeResult(capabilities=mock.Mock())

        with mock.patch(
            "thetvview.streams.probe.probe_capabilities", side_effect=lento
        ):
            probe.start(lambda r: None)
            self.assertTrue(arrancado.wait(2))
            inicio = time.monotonic()
            probe.stop(timeout=0.3)
            elapsed = time.monotonic() - inicio
        # No esperó los 30 s del sondeo.
        self.assertLess(elapsed, 3.0)

    def test_no_deja_hilo_vivo_tras_stop(self) -> None:
        probe = self._probe()
        with mock.patch(
            "thetvview.streams.probe.probe_capabilities",
            return_value=ProbeResult(capabilities=mock.Mock()),
        ):
            probe.start(lambda r: None)
            probe.stop(timeout=2.0)
        self.assertFalse(probe.is_running)


class TestHealthMonitorMigrado(unittest.TestCase):
    """El monitor de salud usa el gestor y para de verdad."""

    def setUp(self) -> None:
        self.tm = TaskManager()
        self.addCleanup(self.tm.shutdown)

    def _monitor(self, **kw) -> ChannelHealthMonitor:
        return ChannelHealthMonitor(
            Channel(name="C", url="http://a.test/x.m3u8"),
            auto_start=False,
            task_manager=self.tm,
            **kw,
        )

    def test_start_lanza_una_tarea_del_gestor(self) -> None:
        monitor = self._monitor(interval=0.5)
        with mock.patch(
            "thetvview.channel_health.probe_channel_url",
            return_value=mock.Mock(latency_ms=1.0, success=True, http_code=200),
        ):
            monitor.start()
            for _ in range(100):
                if self.tm.active_for("health-probe"):
                    break
                time.sleep(0.02)
        self.assertTrue(self.tm.active_for("health-probe"))
        monitor.stop()

    def test_stop_es_rapido_aunque_el_intervalo_sea_largo(self) -> None:
        """Con un intervalo de 60 s, ``stop`` no puede esperar a la siguiente."""
        monitor = self._monitor(interval=60.0)
        with mock.patch(
            "thetvview.channel_health.probe_channel_url",
            return_value=mock.Mock(latency_ms=1.0, success=True, http_code=200),
        ):
            monitor.start()
            time.sleep(0.1)
            inicio = time.monotonic()
            monitor.stop()
            elapsed = time.monotonic() - inicio
        self.assertLess(elapsed, 3.0)
        self.assertFalse(monitor.is_running)

    def test_la_sonda_inmediata_sigue_haciendose(self) -> None:
        """La primera sonda no espera al intervalo: es para pintar rápido."""
        monitor = self._monitor(interval=60.0)
        llamadas: list[int] = []

        def cuenta(*_a, **_k):
            llamadas.append(1)
            return mock.Mock(latency_ms=1.0, success=True, http_code=200)

        with mock.patch("thetvview.channel_health.probe_channel_url", side_effect=cuenta):
            monitor.start()
            for _ in range(100):
                if llamadas:
                    break
                time.sleep(0.02)
            monitor.stop()
        self.assertGreaterEqual(len(llamadas), 1)


class TestWarmCatalogMigrado(unittest.TestCase):
    """El warming del catálogo no muta la caché desde el hilo."""

    def setUp(self) -> None:
        from thetvview.ui.app import App

        self.App = App
        self.tm = TaskManager()
        self.addCleanup(self.tm.shutdown)

    def _app(self, **kw):  # noqa: ANN003
        """Un App sin terminal: sólo lo que necesita el warming."""
        app = self.App.__new__(self.App)
        app._tasks = self.tm
        app._warm_lock = threading.Lock()
        app._warm_queue: list[str] = []
        app._warm_queued: set[str] = set()
        app._warm_failed: dict[str, float] = {}
        app._warm_thread = False
        app._warm_stop = threading.Event()
        app._warm_indexes: list[str] = []
        app.playlist_cache = {}
        app.playlist_cache_meta = {}
        app._base_indexes = {}
        app._allow_private = lambda source: False
        return app

    def _esperar(self, app, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.tm.active_for("catalog-warm"):
                break
            for h in self.tm.active_for("catalog-warm"):
                h.join(timeout=0.05)
        app._drain_tasks()

    def test_el_worker_no_toca_la_cache(self) -> None:
        """La clave: el hilo descarga pero **no** escribe en `playlist_cache`."""
        app = self._app()
        app.warm_catalog = self.App.warm_catalog.__get__(app)
        app._drain_catalog_warm = self.App._drain_catalog_warm.__get__(app)
        app._first_unindexed = self.App._first_unindexed.__get__(app)
        app._warm_worker = self.App._warm_worker.__get__(app)
        app._base_index = lambda key, playlist: None

        with (
            mock.patch.object(
                self.App,
                "pending_catalog_sources",
                lambda self: ["http://a.test/l.m3u"],
            ),
            mock.patch(
                "thetvview.ui.app.load_playlist_source",
                return_value=Playlist(name="L", channels=[]),
            ),
        ):
            app.warm_catalog()
            # Antes de drenar: el hilo pudo descargar, pero la caché sigue
            # vacía porque nadie la ha aplicado todavía.
            for h in self.tm.active_for("catalog-warm"):
                h.join(timeout=5)
            self.assertEqual(app.playlist_cache, {})
            self._esperar(app)
        self.assertIn("http://a.test/l.m3u", app.playlist_cache)

    def test_el_gestor_es_el_que_lanza(self) -> None:
        app = self._app()
        app.warm_catalog = self.App.warm_catalog.__get__(app)
        app._warm_worker = self.App._warm_worker.__get__(app)
        app._first_unindexed = lambda: None
        app._base_index = lambda key, playlist: None

        with (
            mock.patch.object(
                self.App,
                "pending_catalog_sources",
                lambda self: ["http://a.test/l.m3u"],
            ),
            mock.patch(
                "thetvview.ui.app.load_playlist_source",
                return_value=Playlist(name="L", channels=[]),
            ),
        ):
            app.warm_catalog()
            lanzadas = self.tm.active_for("catalog-warm")
            self.assertTrue(lanzadas, "el warming no se lanzó por el gestor")
            app._warm_stop.set()
            for h in lanzadas:
                h.cancel("test")
                h.join(timeout=2)
            app._warm_thread = False


if __name__ == "__main__":
    unittest.main()
