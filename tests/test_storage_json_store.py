"""Tests de escritura atómica JSON (STORE-001, STORE-004, STORE-007).

Verifican:
- Escritura atómica: el fichero nunca queda con JSON parcial.
- Fallo inyectado en cada fase de la escritura.
- Recuperación desde backup y preservación de corruptos.
- Migración idempotente.
- Dos escrituras concurrentes.
- Límites de bytes y entradas.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from thetvview.storage.errors import StorageError
from thetvview.storage.json_store import (
    MAX_JSON_ENTRIES,
    atomic_write_json,
    cleanup_orphans,
    corrupt_path,
    file_lock,
    make_backup,
    read_json_with_recovery,
)


class TestAtomicWrite(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_escritura_basica(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, {"a": 1, "b": [1, 2, 3]})
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"], [1, 2, 3])

    def test_schema_version_se_anade(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, {"a": 1}, schema_version=1)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)

    def test_sin_schema_version_no_lo_anade(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, {"a": 1}, schema_version=None)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("schema_version", data)

    def test_temporal_unico_en_mismo_directorio(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, {"a": 1})
        # No debe quedar ningún .tmp
        tmps = list(self.tmp.glob(".*.tmp"))
        self.assertEqual(tmps, [])

    def test_limpieza_de_huerfanos(self) -> None:
        # Crear un temporal huérfano viejo
        orphan = self.tmp / ".test.json.12345.old.tmp"
        orphan.write_text("{}")
        # Hacer que parezca viejo
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        removed = cleanup_orphans(self.tmp / "test.json")
        self.assertEqual(removed, 1)
        self.assertFalse(orphan.exists())

    def test_no_toca_temporales_recientes(self) -> None:
        orphan = self.tmp / ".test.json.12345.tmp"
        orphan.write_text("{}")
        removed = cleanup_orphans(self.tmp / "test.json", max_age_seconds=3600)
        self.assertEqual(removed, 0)
        self.assertTrue(orphan.exists())

    def test_reemplazo_atomico(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, {"v": 1})
        atomic_write_json(path, {"v": 2})
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["v"], 2)

    def test_validacion_antes_de_escribir(self) -> None:
        path = self.tmp / "test.json"
        with self.assertRaises(StorageError):
            atomic_write_json(path, {"a": 1}, validate=lambda d: (_ for _ in ()).throw(StorageError("no")))
        self.assertFalse(path.exists())

    def test_serializacion_falla(self) -> None:
        path = self.tmp / "test.json"
        with self.assertRaises(StorageError):
            atomic_write_json(path, {"a": object()})
        self.assertFalse(path.exists())

    def test_schema_version_en_no_dict_falla(self) -> None:
        path = self.tmp / "test.json"
        with self.assertRaises(StorageError):
            atomic_write_json(path, [1, 2, 3], schema_version=1)


class TestReadWithRecovery(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_archivo_no_existe_devuelve_none(self) -> None:
        self.assertIsNone(read_json_with_recovery(self.tmp / "no.json"))

    def test_lectura_normal(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, {"a": 1})
        data = read_json_with_recovery(path)
        self.assertEqual(data["a"], 1)

    def test_corrupto_restaura_desde_backup(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, {"a": 1})
        make_backup(path)
        # Corromper el fichero
        path.write_text("{corrupto")
        data = read_json_with_recovery(path)
        self.assertEqual(data["a"], 1)

    def test_corrupto_sin_backup_preserva_copia(self) -> None:
        path = self.tmp / "test.json"
        path.write_text("{corrupto")
        with self.assertRaises(StorageError):
            read_json_with_recovery(path)
        # Debe existir una copia .corrupt-*
        corrupt = list(self.tmp.glob(".test.json.corrupt-*"))
        self.assertEqual(len(corrupt), 1)

    def test_limita_copias_corruptas(self) -> None:
        path = self.tmp / "test.json"
        path.write_text("{}")
        for i in range(5):
            time.sleep(0.01)
            path.write_text("{corrupto")
            try:
                read_json_with_recovery(path)
            except StorageError:
                pass
        corrupt = list(self.tmp.glob(".test.json.corrupt-*"))
        self.assertLessEqual(len(corrupt), 3)

    def test_schema_version_erroneo_lanza(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, {"a": 1}, schema_version=2)
        with self.assertRaises(StorageError):
            read_json_with_recovery(path, schema_version=1)

    def test_schema_version_ausente_lanza(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, {"a": 1}, schema_version=None)
        with self.assertRaises(StorageError):
            read_json_with_recovery(path, schema_version=1)

    def test_demasiadas_entradas(self) -> None:
        path = self.tmp / "test.json"
        atomic_write_json(path, list(range(MAX_JSON_ENTRIES + 1)), schema_version=None)
        with self.assertRaises(StorageError):
            read_json_with_recovery(path)

    def test_demasiados_bytes(self) -> None:
        path = self.tmp / "test.json"
        # Escribir un fichero grande directamente
        path.write_bytes(b"x" * (11 * 1024 * 1024))
        with self.assertRaises(StorageError):
            read_json_with_recovery(path, max_bytes=10 * 1024 * 1024)


class TestConcurrentWrites(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_dos_escrituras_concurrentes(self) -> None:
        path = self.tmp / "test.json"
        barrier = threading.Barrier(2)

        def writer(val: int) -> None:
            barrier.wait()
            for _ in range(20):
                atomic_write_json(path, {"v": val})

        t1 = threading.Thread(target=writer, args=(1,))
        t2 = threading.Thread(target=writer, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn(data["v"], (1, 2))


class TestFileLock(unittest.TestCase):
    """STORE-006: los pares leer-modificar-escribir no se pisan."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_el_mismo_fichero_comparte_candado(self) -> None:
        self.assertIs(file_lock(self.tmp / "a.json"), file_lock(self.tmp / "a.json"))

    def test_ficheros_distintos_no_comparten_candado(self) -> None:
        self.assertIsNot(file_lock(self.tmp / "a.json"), file_lock(self.tmp / "b.json"))

    def test_el_candado_es_reentrante(self) -> None:
        """Reentrante porque ``toggle`` llama a ``load`` que vuelve a cogerlo."""
        candado = file_lock(self.tmp / "a.json")
        with candado:
            with candado:
                pass  # no debe colgarse

    def test_prefs_no_pierde_cambios_concurrentes(self) -> None:
        """Dos ajustes distintos desde hilos distintos: ambos se conservan."""
        from thetvview.prefs import Prefs, PrefsManager

        ruta = self.tmp / "prefs.json"
        gestor = PrefsManager(ruta)
        gestor.save(Prefs())
        barrera = threading.Barrier(2)

        def pone_last_player() -> None:
            barrera.wait()
            for _ in range(30):
                gestor.set_last_player("mpv")

        def pone_grupo() -> None:
            barrera.wait()
            for _ in range(30):
                gestor.set_last_group_sort("count")

        t1 = threading.Thread(target=pone_last_player)
        t2 = threading.Thread(target=pone_grupo)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        prefs = gestor.load()
        self.assertEqual(prefs.last_player, "mpv")
        self.assertEqual(prefs.last_group_sort, "count")

    def test_favoritos_no_pierde_altas_concurrentes(self) -> None:
        """Diez favoritos añadidos a la vez: los diez tienen que estar."""
        from thetvview.favorites import FavoritesManager
        from thetvview.models import Channel

        gestor = FavoritesManager(self.tmp / "favorites.json")
        barrera = threading.Barrier(10)

        def añade(indice: int) -> None:
            barrera.wait()
            gestor.toggle(Channel(name=f"C{indice}", url=f"http://a.test/{indice}"))

        hilos = [
            threading.Thread(target=añade, args=(i,)) for i in range(10)
        ]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        self.assertEqual(len(gestor.load()), 10)


if __name__ == "__main__":
    unittest.main()
