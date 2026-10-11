"""Tests de la política de cachés (RES-005, STORE-005).

Verifican:
- Las entradas caducadas por TTL se borran.
- Al superar el tope de tamaño se borra lo más **antiguo**, no lo más nuevo.
- La limpieza **no toca la persistencia** (prefs, listas, favoritos, backups).
- Una caché con permiso raro no impide limpiar las demás ni lanzar.
- Es idempotente: limpiarla dos veces no borra más la segunda vez.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from thetvview import config
from thetvview.storage import cache_policy


class TestLimpiarCache(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.policy = cache_policy.CachePolicy(
            nombre="prueba", directorio=self.dir, ttl=100.0, max_bytes=1000
        )
        self.ahora = time.time()

    def _fichero(self, nombre: str, tamaño: int, edad: float) -> Path:
        ruta = self.dir / nombre
        ruta.write_bytes(b"x" * tamaño)
        cuando = self.ahora - edad
        os.utime(ruta, (cuando, cuando))
        return ruta

    def test_directorio_vacio(self) -> None:
        self.assertEqual(cache_policy.limpiar_cache(self.policy, ahora=self.ahora), (0, 0))

    def test_directorio_inexistente(self) -> None:
        policy = cache_policy.CachePolicy(
            nombre="nada", directorio=self.dir / "no-existe", ttl=1, max_bytes=1
        )
        self.assertEqual(cache_policy.limpiar_cache(policy, ahora=self.ahora), (0, 0))

    def test_borra_caducados(self) -> None:
        viejo = self._fichero("viejo.json", 100, edad=500)  # > ttl 100
        nuevo = self._fichero("nuevo.json", 100, edad=10)
        cache_policy.limpiar_cache(self.policy, ahora=self.ahora)
        self.assertFalse(viejo.exists())
        self.assertTrue(nuevo.exists())

    def test_no_borra_dentro_del_ttl(self) -> None:
        ruta = self._fichero("fresco.json", 100, edad=10)
        cache_policy.limpiar_cache(self.policy, ahora=self.ahora)
        self.assertTrue(ruta.exists())

    def test_recorta_por_tope_borrando_lo_mas_antiguo(self) -> None:
        policy = cache_policy.CachePolicy(
            nombre="corta", directorio=self.dir, ttl=10_000, max_bytes=250
        )
        antiguo = self._fichero("a.json", 100, edad=300)  # el más viejo
        medio = self._fichero("b.json", 100, edad=200)
        reciente = self._fichero("c.json", 100, edad=10)  # el más nuevo
        cache_policy.limpiar_cache(policy, ahora=self.ahora)
        # Total 300 > 250: sobra borrar uno, y es el más antiguo.
        self.assertFalse(antiguo.exists())
        self.assertTrue(medio.exists())
        self.assertTrue(reciente.exists())

    def test_dry_run_no_borra(self) -> None:
        viejo = self._fichero("viejo.json", 100, edad=500)
        cache_policy.limpiar_cache(self.policy, ahora=self.ahora, dry_run=True)
        self.assertTrue(viejo.exists())

    def test_espacio_usado(self) -> None:
        self._fichero("a.json", 100, edad=10)
        self._fichero("b.json", 250, edad=10)
        self.assertEqual(cache_policy.espacio_usado(self.dir), 350)

    def test_es_idempotente(self) -> None:
        self._fichero("viejo.json", 100, edad=500)
        considered, borrados = cache_policy.limpiar_cache(self.policy, ahora=self.ahora)
        self.assertEqual(borrados, 100)
        # La segunda pasada no encuentra nada que borrar.
        _considered2, borrados2 = cache_policy.limpiar_cache(
            self.policy, ahora=self.ahora
        )
        self.assertEqual(borrados2, 0)


class TestNoTocaPersistencia(unittest.TestCase):
    """STORE-005: la limpieza de caché no puede borrar datos del usuario."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)

    def test_la_limpieza_no_alcanza_los_json_de_persistencia(self) -> None:
        # Ficheros de persistencia en el mismo directorio que la caché.
        persistencia = [
            self.data / "prefs.json",
            self.data / "playlists.json",
            self.data / "favorites.json",
            self.data / "recents.json",
        ]
        for ruta in persistencia:
            ruta.write_text('{"schema_version": 1}', encoding="utf-8")

        # Una caché con entradas viejas en el mismo directorio.
        cache = self.data / "epg_cache"
        cache.mkdir()
        for i in range(3):
            f = cache / f"e{i}.xml"
            f.write_text("x", encoding="utf-8")
            os.utime(f, (0, 0))  # muy viejo

        # La política de "epg" apunta al subdirectorio, no a data/.
        policy = cache_policy.CachePolicy(
            nombre="epg", directorio=cache, ttl=10.0, max_bytes=1024
        )
        cache_policy.limpiar_cache(policy, ahora=time.time())

        for ruta in persistencia:
            self.assertTrue(ruta.exists(), f"la limpieza borró {ruta.name}")

    def test_las_politicas_no_apuntan_a_persistencia(self) -> None:
        for nombre, policy in cache_policy._policies().items():
            nombre_ruta = Path(policy.directorio).name
            self.assertIn(
                nombre_ruta,
                {"playlist_cache", "epg_cache", "xtream_cache", "track_cache"},
                f"la política «{nombre}» apunta a un directorio que no es caché",
            )

    def test_limpiar_todo_no_rompe_si_una_cache_falla(self) -> None:
        original = cache_policy.limpiar_cache

        def falla_a_veces(policy, **kwargs):
            if policy.nombre == "epg":
                raise OSError("permiso denegado")
            return original(policy, **kwargs)

        with mock.patch.object(cache_policy, "limpiar_cache", falla_a_veces):
            # No debe lanzar: una caché con permiso raro no impide el resto.
            resultado = cache_policy.limpiar_todo()
        self.assertIsInstance(resultado, dict)


class TestArranque(unittest.TestCase):
    def test_ensure_dirs_no_rompe_con_la_limpieza(self) -> None:
        """``ensure_dirs`` llama a la limpieza; no debe propagar fallos."""
        with mock.patch.object(
            cache_policy, "limpiar_todo", side_effect=OSError("raro")
        ):
            with mock.patch.object(config, "DATA_DIR", Path(tempfile.mkdtemp())):
                config.ensure_dirs()  # no debe lanzar


if __name__ == "__main__":
    unittest.main()
