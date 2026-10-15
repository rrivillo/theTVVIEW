"""Pruebas de los cimientos de la Fase 8 (P2.6 UI · U-01).

U-01 no es una pantalla: es lo que tiene que existir **antes** de que cualquier
pantalla de la Fase 8 pueda escribirse sin romper la suite.

- Etiquetas: `ui.actions.validate()` rechaza cualquier rótulo fuera del
  vocabulario cerrado (`V`). Sin las etiquetas de la Fase 8 el primer render
  del pie revienta en vez de fallar en desarrollo.
- Rutas: `config.COLLECTIONS_JSON` y `config.SCHEDULED_TASKS_JSON` son las que
  leen los managers; sin ellas, colecciones y tareas viven sólo en memoria.
- Validador: `storage.schemas` tiene que validar el documento del planificador,
  porque es lo que impide que un `start_at` corrupto llegue a la interfaz.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from thetvview import config
from thetvview.storage.errors import StorageError
from thetvview.storage.schemas import (
    SCHEMAS,
    get_validator,
    validate_scheduled_tasks,
)
from thetvview.ui.actions import LABELS, Action, V, validate


class TestVocabularioDeLaFase8(unittest.TestCase):
    """U-01: las etiquetas de la Fase 8 existen y son del vocabulario cerrado."""

    def test_etiquetas_de_la_fase8_declaradas(self) -> None:
        esperadas = (
            V.COLECCIONES,
            V.COLECCION,
            V.FUENTES,
            V.GRABAR,
            V.PROGRAMAR,
            V.PROGRAMACIONES,
            V.ESTADO,
            V.REGLA,
            V.FILTROS,
        )
        for etiqueta in esperadas:
            self.assertIn(etiqueta, LABELS, f"{etiqueta!r} no está en el vocabulario")

    def test_validar_acepta_un_catalogo_de_la_fase8(self) -> None:
        validate(
            [
                Action("Enter", V.VER, essential=True),
                Action("c", V.COLECCIONES),
                Action("g", V.GRABAR),
                Action("p", V.PROGRAMAR),
                Action("f", V.FUENTES),
                Action("s", V.FILTROS),
                Action("Esc", V.VOLVER),
            ]
        )  # no lanza

    def test_ninguna_etiqueta_nueva_reemplaza_a_otra_existente(self) -> None:
        """La Fase 8 **añade** vocabulario, no lo redefine.

        Si `GRABAR` valiera lo mismo que `DETENER`, el pie no podría
        distinguir «detener el reproductor» de «detener la grabación», y la
        ayuda `?` mentiría.
        """
        nuevos = {
            V.COLECCIONES: "Colecciones",
            V.FUENTES: "Fuentes",
            V.GRABAR: "Grabar",
            V.PROGRAMAR: "Programar",
            V.ESTADO: "Estado",
            V.FILTROS: "Filtros",
        }
        for constante, valor in nuevos.items():
            with self.subTest(constante=constante):
                self.assertEqual(constante, valor)

        previos = {V.DETENER, V.EDITAR, V.ELIMINAR, V.ANADIR, V.LIMPIAR}
        for etiqueta in nuevos:
            self.assertNotIn(etiqueta, previos)

    def test_el_vocabulario_crece_y_no_se_reescribe(self) -> None:
        """Las etiquetas del núcleo siguen ahí: es aditivo, no una sustitución."""
        for etiqueta in ("Ver", "Abrir", "Buscar", "Favorito", "Ayuda"):
            self.assertIn(etiqueta, LABELS)


class TestRutasDeLaFase8(unittest.TestCase):
    """U-01: los managers tienen dónde leer y escribir."""

    def test_colecciones_vive_en_data_dir(self) -> None:
        self.assertEqual(config.COLLECTIONS_JSON, config.DATA_DIR / "collections.json")

    def test_tareas_programadas_viven_en_data_dir(self) -> None:
        self.assertEqual(
            config.SCHEDULED_TASKS_JSON, config.DATA_DIR / "scheduled_tasks.json"
        )

    def test_destino_de_grabaciones_propuesto(self) -> None:
        self.assertEqual(config.RECORDINGS_DIR, config.DATA_DIR / "grabaciones")

    def test_ensure_dirs_no_crea_la_carpeta_de_grabaciones(self) -> None:
        """Una carpeta vacía que el usuario nunca pidió es ruido en su disco.

        `RECORDINGS_DIR` es una **propuesta** de destino: la crea
        `recording.validar_destino()` cuando el usuario la elige.
        """
        import unittest.mock as mock

        base = Path(tempfile.mkdtemp())
        with (
            mock.patch.object(config, "DATA_DIR", base),
            mock.patch.object(config, "EPG_CACHE_DIR", base / "epg"),
            mock.patch.object(config, "PLAYLIST_CACHE_DIR", base / "pl"),
            mock.patch.object(config, "XTREAM_CACHE_DIR", base / "xt"),
            mock.patch.object(config, "TRACK_CACHE_DIR", base / "tr"),
            mock.patch.object(config, "DIAGNOSTICS_DIR", base / "diag"),
            mock.patch.object(config, "RECORDINGS_DIR", base / "grabaciones"),
        ):
            config.ensure_dirs()
        self.assertFalse((base / "grabaciones").exists())
        self.assertTrue(base.exists())

    def test_el_nombre_del_archivo_coincide_con_la_ruta(self) -> None:
        """`storage.json_store` registra validadores por nombre de fichero."""
        self.assertEqual(
            config.SCHEDULED_TASKS_JSON.name, "scheduled_tasks.json"
        )
        self.assertIn("scheduled_tasks", SCHEMAS)
        self.assertIs(get_validator("scheduled_tasks"), validate_scheduled_tasks)
        self.assertIs(get_validator("SCHEDULED_TASKS"), validate_scheduled_tasks)


class TestValidadorDelPlanificador(unittest.TestCase):
    """U-01: un archivo corrupto se rechaza al leer, no se pinta."""

    def _tarea(self, **cambios) -> dict:
        base = {
            "task_id": "t-1",
            "source": "http://ejemplo.local/stream.m3u8",
            "start_at": 1800000000.0,
            "status": "scheduled",
        }
        base.update(cambios)
        return base

    def test_acepta_el_documento_versionado(self) -> None:
        validate_scheduled_tasks({"version": 1, "tasks": [self._tarea()]})

    def test_acepta_la_lista_legada(self) -> None:
        validate_scheduled_tasks([self._tarea()])

    def test_rechaza_otra_cosa(self) -> None:
        with self.assertRaises(StorageError):
            validate_scheduled_tasks("no soy nada")

    def test_rechaza_tarea_sin_identificador(self) -> None:
        with self.assertRaises(StorageError):
            validate_scheduled_tasks([self._tarea(task_id="")])

    def test_rechaza_tarea_sin_fuente(self) -> None:
        with self.assertRaises(StorageError):
            validate_scheduled_tasks([self._tarea(source="  ")])

    def test_rechaza_instante_corrupto(self) -> None:
        """Es el campo que la interfaz usa para ordenar y para decidir."""
        with self.assertRaises(StorageError):
            validate_scheduled_tasks([self._tarea(start_at="mañana")])

    def test_rechaza_estado_inventado(self) -> None:
        with self.assertRaises(StorageError):
            validate_scheduled_tasks([self._tarea(status="durmiendo")])

    def test_rechaza_conflicto_que_no_es_texto(self) -> None:
        with self.assertRaises(StorageError):
            validate_scheduled_tasks([self._tarea(conflicts=[{"id": "x"}])])

    def test_acepta_conflicto_correcto(self) -> None:
        validate_scheduled_tasks([self._tarea(conflicts=["t-2", "t-3"])])

    def test_rechaza_booleano_como_instante(self) -> None:
        """En Python ``True`` es un ``int``: sin esta comprobación, ``True``
        valdría como epoch 1 y la tarea aparecería perdida desde 1970."""
        with self.assertRaises(StorageError):
            validate_scheduled_tasks([self._tarea(start_at=True)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()