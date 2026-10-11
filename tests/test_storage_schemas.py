"""Tests de esquemas y migraciones (STORE-002, STORE-003).

Verifican:
- Validación semántica por formato (tipos, campos obligatorios, rangos).
- Los campos desconocidos se conservan (no se pierden datos de una versión
  más nueva).
- La migración v0 → v1 es idempotente.
- La migración escribe por escritura atómica y valida la versión nueva.
- Un formato no versionado (v0) se reconoce como tal, no como inválido.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from thetvview.storage.errors import StorageError
from thetvview.storage.migrations import migrate_data, migrate_file, needs_migration
from thetvview.storage.schemas import (
    SCHEMA_VERSION,
    ensure_version,
    get_validator,
    stamped,
    validate_favorites,
    validate_prefs,
    validate_recents,
)


class TestValidacion(unittest.TestCase):
    def test_prefs_valido(self) -> None:
        validate_prefs(stamped({"last_player": "mpv", "theme": "dark"}))

    def test_prefs_no_dict_falla(self) -> None:
        with self.assertRaises(StorageError):
            validate_prefs([1, 2, 3])

    def test_prefs_tipo_malo_falla(self) -> None:
        with self.assertRaises(StorageError):
            validate_prefs(stamped({"theme": 42}))
        with self.assertRaises(StorageError):
            validate_prefs(stamped({"subtitles_enabled": "si"}))

    def test_prefs_rango_fuera_falla(self) -> None:
        with self.assertRaises(StorageError):
            validate_prefs(stamped({"reconnect_max_attempts": 9999}))

    def test_prefs_sort_invalido_falla(self) -> None:
        with self.assertRaises(StorageError):
            validate_prefs(stamped({"last_group_sort": "aleatorio"}))

    def test_prefs_campos_desconocidos_se_conservan(self) -> None:
        # Un fichero escrito por una versión más nueva no debe perder campos.
        data = stamped({"theme": "dark", "campo_del_futuro": "valor"})
        validate_prefs(data)  # no falla
        self.assertEqual(data["campo_del_futuro"], "valor")

    def test_favorites_no_lista_falla(self) -> None:
        with self.assertRaises(StorageError):
            validate_favorites({"no": "es lista"})

    def test_favorites_entrada_no_dict_falla(self) -> None:
        with self.assertRaises(StorageError):
            validate_favorites(["solo texto"])

    def test_recents_campos_obligatorios(self) -> None:
        validate_recents([{"name": "a", "url": "u", "player": "mpv"}])
        with self.assertRaises(StorageError):
            validate_recents([{"name": "a", "url": 5, "player": "mpv"}])

    def test_get_validator(self) -> None:
        self.assertIs(get_validator("prefs"), validate_prefs)
        self.assertIs(get_validator("favorites"), validate_favorites)
        self.assertIsNone(get_validator("cache_desconocida"))


class TestVersion(unittest.TestCase):
    def test_stamped_pones_version(self) -> None:
        self.assertEqual(stamped({"a": 1})["schema_version"], SCHEMA_VERSION)

    def test_ensure_version_v0_sin_sello(self) -> None:
        self.assertEqual(ensure_version({"a": 1}, nombre="x"), 0)

    def test_ensure_version_actual(self) -> None:
        self.assertEqual(ensure_version(stamped({"a": 1}), nombre="x"), SCHEMA_VERSION)

    def test_ensure_version_no_numero_falla(self) -> None:
        with self.assertRaises(StorageError):
            ensure_version({"schema_version": "muchas"}, nombre="x")

    def test_needs_migration(self) -> None:
        self.assertTrue(needs_migration({"a": 1}))
        self.assertFalse(needs_migration(stamped({"a": 1})))


class TestMigrateData(unittest.TestCase):
    def test_v0_a_v1(self) -> None:
        resultado = migrate_data({"theme": "dark"}, nombre="prefs")
        self.assertTrue(resultado.changed)
        self.assertEqual(resultado.from_version, 0)
        self.assertEqual(resultado.to_version, SCHEMA_VERSION)
        self.assertEqual(resultado["data"]["schema_version"], SCHEMA_VERSION)
        self.assertEqual(resultado["data"]["theme"], "dark")

    def test_es_idempotente(self) -> None:
        una = migrate_data({"theme": "dark"}, nombre="prefs")
        dos = migrate_data(una["data"], nombre="prefs")
        self.assertFalse(dos.changed)
        self.assertEqual(una["data"], dos["data"])

    def test_migrar_dos_veces_el_fichero_es_el_mismo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ruta = Path(td) / "prefs.json"
            ruta.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
            migrate_file(ruta, nombre="prefs")
            primero = ruta.read_text(encoding="utf-8")
            resultado = migrate_file(ruta, nombre="prefs")
            self.assertFalse(resultado.changed)
            self.assertEqual(ruta.read_text(encoding="utf-8"), primero)

    def test_no_dict_falla(self) -> None:
        with self.assertRaises(StorageError):
            migrate_data([1, 2], nombre="prefs")


class TestMigrateFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ruta = Path(self._tmp.name) / "prefs.json"

    def test_fichero_inexistente_no_hace_nada(self) -> None:
        resultado = migrate_file(self.ruta, nombre="prefs")
        self.assertFalse(resultado.changed)
        self.assertFalse(self.ruta.exists())

    def test_migra_y_valida(self) -> None:
        self.ruta.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        resultado = migrate_file(self.ruta, nombre="prefs")
        self.assertTrue(resultado.changed)
        datos = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(datos["schema_version"], SCHEMA_VERSION)
        self.assertEqual(datos["theme"], "dark")

    def test_deja_un_backup_del_original(self) -> None:
        self.ruta.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        migrate_file(self.ruta, nombre="prefs")
        backups = list(Path(self._tmp.name).glob(".prefs.json.bak-*"))
        self.assertEqual(len(backups), 1)
        # El backup es el formato viejo, sin schema_version.
        self.assertNotIn(
            "schema_version", json.loads(backups[0].read_text(encoding="utf-8"))
        )

    def test_migracion_escribe_atomica(self) -> None:
        self.ruta.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        migrate_file(self.ruta, nombre="prefs")
        # No debe quedar ningún temporal por medio.
        tmps = list(Path(self._tmp.name).glob(".*.tmp"))
        self.assertEqual(tmps, [])

    def test_si_el_original_es_basura_no_se_toca(self) -> None:
        # Un JSON inválido no es «una versión antigua»: es corrupción, y la
        # migración no debe inventarse un formato que leer.
        self.ruta.write_text("{no es json", encoding="utf-8")
        with self.assertRaises(StorageError):
            migrate_file(self.ruta, nombre="prefs")


if __name__ == "__main__":
    unittest.main()
