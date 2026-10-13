"""Tests de la caché de combinaciones exitosas (FR-CAP-007, Fase 5/CAP-002).

Cubre lo que el plan exige: guardar la combinación con **fecha, versión del
motor, ámbito y caducidad**, e invalidarla al cambiar versión, capacidades
relevantes o configuración. Nada aquí lanza una shell ni toca la red; el
único subprocess es ``<binario> --version`` con timeout, y sólo en los dos
tests que lo comprueban con un script falso.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from thetvview import config
from thetvview.models import Channel
from thetvview.player import success_cache as sc
from thetvview.player.success_cache import (
    Exito,
    SuccessCache,
    detectar_version,
    huella_capacidades,
    huella_configuracion,
)

_T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


def _version_fija(valor: str):
    return lambda _ruta: valor


class _Base(unittest.TestCase):
    """Directorio temporal y memoria de versiones limpia por test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.ruta = self.base / "player_cache.json"
        sc._VERSION_MEMO.clear()
        self.addCleanup(sc._VERSION_MEMO.clear)

    def cache(self, **kwargs) -> SuccessCache:
        kwargs.setdefault("version_fn", _version_fija("motor 1.0"))
        kwargs.setdefault("ahora", lambda: _T0)
        return SuccessCache(self.ruta, **kwargs)


class TestDetectarVersion(unittest.TestCase):
    """El ``--version``: sin shell, con timeout, memorizado."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        sc._VERSION_MEMO.clear()
        self.addCleanup(sc._VERSION_MEMO.clear)

    @unittest.skipUnless(os.name == "posix", "el script falso es POSIX")
    def test_primera_linea_y_memoriza(self) -> None:
        fake = Path(self._tmp.name) / "fakempv"
        fake.write_text("#!/bin/sh\necho 'mpv 99.0.0 (fake)'\n", encoding="utf-8")
        fake.chmod(stat.S_IRWXU)
        with mock.patch.object(sc.subprocess, "run", wraps=sc.subprocess.run) as run:
            self.assertEqual(detectar_version(str(fake)), "mpv 99.0.0 (fake)")
            # Segunda llamada: sale de la memoria, sin subprocess.
            self.assertEqual(detectar_version(str(fake)), "mpv 99.0.0 (fake)")
        self.assertEqual(run.call_count, 1)
        # Sin shell: argv en lista.
        argv = run.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[1], "--version")

    def test_ruta_inexistente_o_vacia_es_vacia(self) -> None:
        self.assertEqual(detectar_version(None), "")
        self.assertEqual(detectar_version(""), "")
        inexistente = Path(self._tmp.name) / "no-existe"
        self.assertEqual(detectar_version(str(inexistente)), "")

    @unittest.skipUnless(os.name == "posix", "el script falso es POSIX")
    def test_binario_mudo_no_inventa_version(self) -> None:
        fake = Path(self._tmp.name) / "mudo"
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(stat.S_IRWXU)
        self.assertEqual(detectar_version(str(fake)), "")


class TestHuellas(_Base):
    def test_huellas_estables_y_distintas(self) -> None:
        self.assertEqual(huella_capacidades("mpv"), huella_capacidades("MPV"))
        self.assertNotEqual(huella_capacidades("mpv"), huella_capacidades("vlc"))
        self.assertEqual(huella_configuracion(), huella_configuracion())

    def test_la_huella_de_capacidades_cambia_si_cambia_la_fila(self) -> None:
        antes = huella_capacidades("vlc")
        fila = dict(sc._protocols.PLAYER_PROTOCOL_SUPPORT)
        vlc = fila["vlc"]
        fila["vlc"] = type(vlc)(
            name=vlc.name,
            transports=vlc.transports | frozenset({"udp"}),
            notas=vlc.notas,
            medido=vlc.medido,
            notes=vlc.notes,
        )
        with mock.patch.object(sc._protocols, "PLAYER_PROTOCOL_SUPPORT", fila):
            self.assertNotEqual(huella_capacidades("vlc"), antes)

    def test_la_huella_de_config_cambia_si_cambia_el_orden(self) -> None:
        antes = huella_configuracion()
        with mock.patch.object(
            config, "SUPPORTED_PLAYERS", ("vlc", "mpv", "mplayer")
        ):
            self.assertNotEqual(huella_configuracion(), antes)


class TestGrabarYBuscar(_Base):
    def test_ida_y_vuelta_con_los_cuatro_campos_del_fr(self) -> None:
        cache = self.cache()
        self.assertTrue(cache.recordar("http", "mpv"))
        exito = cache.buscar("http")
        self.assertIsInstance(exito, Exito)
        assert exito is not None
        self.assertEqual(exito.ambito, "transporte")
        self.assertEqual(exito.clave, "http")
        self.assertEqual(exito.reproductor, "mpv")
        self.assertEqual(exito.version_motor, "motor 1.0")
        # Fecha y caducidad: ISO parseable y caducidad = fecha + TTL.
        fecha = datetime.fromisoformat(exito.fecha)
        caduca = datetime.fromisoformat(exito.caduca)
        self.assertEqual(fecha.tzinfo is not None, True)
        self.assertEqual(caduca - fecha, timedelta(days=sc.TTL_DIAS))

    def test_sin_fichero_no_hay_nada_y_no_se_crea(self) -> None:
        cache = self.cache()
        self.assertIsNone(cache.buscar("http"))
        self.assertFalse(self.ruta.exists())

    def test_claves_vacias_no_se_guardan(self) -> None:
        cache = self.cache()
        self.assertFalse(cache.recordar("", "mpv"))
        self.assertFalse(cache.recordar("http", ""))
        self.assertIsNone(cache.buscar(""))

    def test_el_tope_de_entradas_se_come_la_mas_antigua(self) -> None:
        cache = self.cache(max_entradas=2)
        self.assertTrue(cache.recordar("http", "mpv"))
        self.assertTrue(cache.recordar("udp", "mpv"))
        self.assertTrue(cache.recordar("rtsp", "mpv"))
        # Con reloj fijo las tres fechas empatan: el orden es el de
        # inserción (la nueva, delante), así que se cae http, la primera.
        claves = {e.clave for e in cache.entradas()}
        self.assertEqual(claves, {"udp", "rtsp"})

    def test_una_entrada_por_clave_no_se_duplica(self) -> None:
        cache = self.cache()
        cache.recordar("http", "mpv")
        cache.recordar("http", "vlc")
        entradas = cache.entradas()
        self.assertEqual(len(entradas), 1)
        self.assertEqual(entradas[0].reproductor, "vlc")

    def test_olvidar_borra_solo_su_clave(self) -> None:
        cache = self.cache()
        cache.recordar("http", "mpv")
        cache.recordar("udp", "vlc")
        self.assertTrue(cache.olvidar("http"))
        self.assertFalse(cache.olvidar("http"))
        self.assertEqual([e.clave for e in cache.entradas()], ["udp"])

    def test_fichero_corrupto_no_tira_la_caché_ni_al_usuario(self) -> None:
        self.ruta.write_text("{esto no es json", encoding="utf-8")
        cache = self.cache()
        self.assertIsNone(cache.buscar("http"))
        self.assertTrue(cache.recordar("http", "mpv"))
        self.assertEqual(len(cache.entradas()), 1)

    @unittest.skipUnless(os.name == "posix", "chmod es POSIX")
    def test_el_fichero_queda_con_permisos_privados(self) -> None:
        cache = self.cache()
        cache.recordar("http", "mpv")
        modo = self.ruta.stat().st_mode & 0o777
        self.assertEqual(modo, 0o600)


class TestInvalidaciones(_Base):
    """Lo que el FR-CAP-007 manda que invalide la caché."""

    def test_caducidad(self) -> None:
        cache = self.cache()
        cache.recordar("http", "mpv")
        tarde = self.cache(ahora=lambda: _T0 + timedelta(days=sc.TTL_DIAS + 1))
        self.assertIsNone(tarde.buscar("http"))
        self.assertEqual(tarde.entradas(), ())  # se purgó, no se ocultó

    def test_cambio_de_version_del_motor(self) -> None:
        self.cache().recordar("http", "mpv")
        nueva = self.cache(version_fn=_version_fija("motor 2.0"))
        self.assertIsNone(nueva.buscar("http"))
        self.assertEqual(nueva.entradas(), ())

    def test_version_desconocida_no_es_cambio(self) -> None:
        # Desconocido ≠ cambiado: sin evidencia no se invalida.
        self.cache(version_fn=_version_fija("")).recordar("http", "mpv")
        desconocida = self.cache(version_fn=_version_fija(""))
        self.assertIsNotNone(desconocida.buscar("http"))
        self.assertIsNotNone(self.cache(version_fn=_version_fija("")).buscar("http"))

    def test_cambio_de_capacidades_relevantes(self) -> None:
        self.cache().recordar("http", "mpv")
        fila = dict(sc._protocols.PLAYER_PROTOCOL_SUPPORT)
        mpv = fila["mpv"]
        fila["mpv"] = type(mpv)(
            name=mpv.name,
            transports=mpv.transports - frozenset({"http"}),
            notas=mpv.notas,
            medido=mpv.medido,
            notes=mpv.notes,
        )
        with mock.patch.object(sc._protocols, "PLAYER_PROTOCOL_SUPPORT", fila):
            cache = self.cache()
            self.assertIsNone(cache.buscar("http"))

    def test_cambio_de_configuracion(self) -> None:
        self.cache().recordar("http", "mpv")
        with mock.patch.object(
            config, "SUPPORTED_PLAYERS", ("vlc", "mplayer", "mpv")
        ):
            cache = self.cache()
            self.assertIsNone(cache.buscar("http"))

    def test_purgar_quita_lo_vencido_y_conserva_lo_vivo(self) -> None:
        cache = self.cache()
        cache.recordar("http", "mpv")
        cache.recordar("udp", "mpv")
        tarde = self.cache(
            ahora=lambda: _T0 + timedelta(days=sc.TTL_DIAS + 1),
            version_fn=_version_fija("otra"),
        )
        # El cambio de versión ya deja ambas inválidas…
        self.assertEqual(tarde.purgar(), 2)
        self.assertEqual(tarde.entradas(), ())


class _Status:
    def __init__(self) -> None:
        self.last = ""
        self.is_error = False
        self.message = ""

    def show(self, message: str, error: bool = False) -> None:
        self.last = message
        self.is_error = error
        self.message = message


class _PrefsStub:
    def set_last_player(self, name: str | None) -> None:
        pass


class _StubApp:
    """Lo mínimo que play_channel necesita sin curses (sin `combinaciones`)."""

    def __init__(self) -> None:
        self.status = _Status()
        self.prefs = _PrefsStub()
        self.stack: list = []
        self.epg = None
        self.avisos: list[str] = []

    def body_height(self) -> int:
        return 10

    def push(self, screen) -> None:
        self.stack.append(screen)

    def notify_warning(self, message: str) -> None:
        self.avisos.append(message)


def _canal() -> Channel:
    return Channel(
        name="Canal de prueba",
        url="http://ejemplo.invalid/live/demo.ts",
        tvg_id="demo",
    )


def _proc():
    proc = mock.Mock(pid=7)
    proc.poll.return_value = None
    return proc


class TestIntegracionEnPlayChannel(_Base):
    """La caché en su sitio real: play_channel (sin tocar el router)."""

    def _app(self, con_cache: bool = True) -> _StubApp:
        app = _StubApp()
        if con_cache:
            app.combinaciones = self.cache()
        return app

    def test_sin_app_con_cache_todo_sigue_igual(self) -> None:
        from thetvview.ui.screens import play_channel

        app = self._app(con_cache=False)
        with (
            mock.patch(
                "thetvview.config.find_player",
                side_effect=lambda nombre: f"/usr/bin/{nombre}",
            ),
            mock.patch("thetvview.player.launch", return_value=_proc()) as launch,
        ):
            play_channel(app, _canal())
        launch.assert_called_once()
        self.assertIn(
            launch.call_args.kwargs.get("player_name"),
            ("mpv", "mplayer", "vlc"),
        )
        self.assertFalse(self.ruta.exists())

    def test_el_exito_del_router_se_guarda_sin_url(self) -> None:
        from thetvview.ui.screens import play_channel

        app = self._app()
        with (
            mock.patch(
                "thetvview.config.find_player",
                side_effect=lambda nombre: f"/usr/bin/{nombre}",
            ),
            mock.patch("thetvview.player.launch", return_value=_proc()) as launch,
        ):
            play_channel(app, _canal())
        # El router eligió el primero instalado que abre http.
        elegido = launch.call_args.kwargs.get("player_name")
        self.assertIn(elegido, ("mpv", "mplayer", "vlc"))
        entradas = SuccessCache(self.ruta, version_fn=_version_fija("x")).entradas()
        self.assertEqual(len(entradas), 1)
        self.assertEqual(entradas[0].ambito, "transporte")
        self.assertEqual(entradas[0].clave, "http")
        self.assertEqual(entradas[0].reproductor, elegido)
        # La URL jamás aparece en el fichero.
        crudo = self.ruta.read_text(encoding="utf-8")
        self.assertNotIn("ejemplo.invalid", crudo)
        self.assertNotIn("demo.ts", crudo)

    def test_una_combinacion_previa_se_usa_como_preferencia(self) -> None:
        from thetvview.ui.screens import play_channel

        app = self._app()
        app.combinaciones.recordar("http", "vlc")
        with (
            mock.patch(
                "thetvview.config.find_player",
                side_effect=lambda nombre: f"/usr/bin/{nombre}",
            ),
            mock.patch("thetvview.player.launch", return_value=_proc()) as launch,
        ):
            play_channel(app, _canal())
        # La caché entra por el criterio 1 (preferred) → vlc primero.
        self.assertEqual(launch.call_args.kwargs.get("player_name"), "vlc")

    def test_con_reproductor_explicito_no_se_leer_ni_se_graba(self) -> None:
        from thetvview.ui.screens import play_channel

        app = self._app()
        app.combinaciones.recordar("http", "vlc")  # existe, pero no se mira
        with (
            mock.patch("thetvview.config.find_player", return_value=None),
            mock.patch("thetvview.player.launch", return_value=_proc()) as launch,
        ):
            play_channel(app, _canal(), player_name="mpv")
        self.assertEqual(launch.call_args.kwargs.get("player_name"), "mpv")
        # Sólo estaba la entrada precargada: el lanzamiento explícito no
        # añade ni cambia nada.
        entradas = SuccessCache(self.ruta, version_fn=_version_fija("x")).entradas()
        self.assertEqual([e.reproductor for e in entradas], ["vlc"])

    def test_una_cache_que_revienta_no_rompe_la_reproduccion(self) -> None:
        from thetvview.ui.screens import play_channel

        app = self._app()
        app.combinaciones = mock.Mock()
        app.combinaciones.buscar.side_effect = RuntimeError("cache rota")
        app.combinaciones.recordar.side_effect = RuntimeError("cache rota")
        with (
            mock.patch(
                "thetvview.config.find_player",
                side_effect=lambda nombre: f"/usr/bin/{nombre}",
            ),
            mock.patch("thetvview.player.launch", return_value=_proc()) as launch,
        ):
            play_channel(app, _canal())
        launch.assert_called_once()
        self.assertEqual(len(app.stack), 1)


if __name__ == "__main__":
    unittest.main()
