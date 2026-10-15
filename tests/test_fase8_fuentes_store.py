"""Fase 8 · U-06 — Asociación manual de fuentes: el almacén persistente.

`AssociationStore` es lo que hace que «descartar esta fuente» signifique algo
mañana. Estos tests fijan lo que esestore tiene que garantizar y, sobre todo,
lo que tiene que **no** guardar.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from thetvview.models import Channel
from thetvview.sources.associations import (
    MAX_RECHAZOS_POR_CANAL,
    AssociationStore,
    AsociacionesError,
    clave_candidata,
    clave_logica,
)
from thetvview.storage.schemas import validate_sources


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "sources.json"
        self.addCleanup(self.tmp.cleanup)

    def store(self) -> AssociationStore:
        return AssociationStore(self.ruta)

    def leido(self) -> dict:
        return json.loads(self.ruta.read_text(encoding="utf-8"))


class TestClaves(_Base):
    """Las claves tienen que sobrevivir a un reinicio. Ése es su único trabajo."""

    def test_la_clave_logica_no_depende_del_proceso(self) -> None:
        canal = Channel(name="La 1 HD", url="http://p/live/1.ts?token=a",
                        tvg_id="la1.es")
        otra = Channel(name="La 1 HD", url="http://p/live/1.ts?token=b",
                       tvg_id="la1.es")
        # El token giró: es otro canal en la memoria, y el mismo para el
        # usuario. La clave tiene que ser la misma.
        self.assertEqual(clave_logica(canal, "mi-lista"),
                         clave_logica(otra, "mi-lista"))

    def test_la_clave_logica_cambia_con_la_lista(self) -> None:
        canal = Channel(name="La 1 HD", url="http://p/1.ts", tvg_id="la1.es")
        self.assertNotEqual(clave_logica(canal, "mi-lista"),
                            clave_logica(canal, "otra-lista"))

    def test_la_clave_candidata_no_depende_del_token(self) -> None:
        antes = Channel(name="La 1", url="http://p/1.ts?token=aaa",
                        tvg_id="la1.es")
        ahora = Channel(name="La 1", url="http://p/1.ts?token=bbb",
                        tvg_id="la1.es")
        self.assertEqual(clave_candidata("otra", antes),
                         clave_candidata("otra", ahora))

    def test_la_clave_candidata_distingue_listas_y_canales(self) -> None:
        a = Channel(name="La 1", url="http://p/1.ts", tvg_id="la1.es")
        b = Channel(name="La 2", url="http://p/2.ts", tvg_id="la2.es")
        self.assertNotEqual(clave_candidata("A", a), clave_candidata("B", a))
        self.assertNotEqual(clave_candidata("A", a), clave_candidata("A", b))

    def test_un_canal_sin_nada_devuelve_clave_vacia(self) -> None:
        # Sin tvg-id, sin URL y sin nombre no hay nada estable. Devolver una
        # clave inventada sería guardar una asociación que no vuelve a salir.
        self.assertEqual(clave_logica(Channel(name="", url=""), "mi-lista"), "")

    def test_un_canal_solo_con_nombre_si_algo_se_puede_guardar(self) -> None:
        canal = Channel(name="La 1", url="")
        self.assertTrue(clave_logica(canal, "mi-lista"))


class TestAsociar(_Base):
    def test_asociar_guarda_y_se_lee(self) -> None:
        s = self.store()
        s.asociar("canal-1", "cand-1", "Lista B")
        self.assertEqual(s.fuente_preferida("canal-1"), "Lista B")
        self.assertTrue(s.hay_preferencia("canal-1"))

    def test_lo_guardado_sobrevive_a_otra_instancia(self) -> None:
        self.store().asociar("canal-1", "cand-1", "Lista B")
        # Otra instancia = otra sesión. Si esto fallara, el descarte duraría
        # lo que el proceso, y A-03 pide que se recuerde.
        self.assertEqual(self.store().fuente_preferida("canal-1"), "Lista B")

    def test_asociar_no_toca_los_descartes(self) -> None:
        s = self.store()
        s.rechazar("canal-1", "cand-9")
        s.asociar("canal-1", "cand-1", "Lista B")
        self.assertTrue(self.store().esta_rechazada("canal-1", "cand-9"))

    def test_asociar_tampoco_borra_la_entrada_original(self) -> None:
        # No hay nada que borrar: este store no guarda canales, sólo claves.
        # El test fija esa frontera para que nadie lo extienda a URL cruda.
        s = self.store()
        s.asociar("canal-1", "cand-1", "Lista B")
        self.assertNotIn("http", self.ruta.read_text(encoding="utf-8"))

    def test_olvidar_deja_el_canal_sin_preferencia(self) -> None:
        s = self.store()
        s.asociar("canal-1", "cand-1", "Lista B")
        self.assertTrue(s.olvidar("canal-1"))
        self.assertFalse(self.store().hay_preferencia("canal-1"))

    def test_olvidar_no_devuelve_los_descartes(self) -> None:
        # Son gestos distintos. Quien descartó una fuente porque se caía
        # quiere que siga descartada aunque luego associated otra.
        s = self.store()
        s.rechazar("canal-1", "cand-9")
        s.asociar("canal-1", "cand-1", "Lista B")
        s.olvidar("canal-1")
        self.assertTrue(self.store().esta_rechazada("canal-1", "cand-9"))

    def test_olvidar_lo_que_no_hay_no_falla(self) -> None:
        self.assertFalse(self.store().olvidar("nada"))

    def test_no_se_puede_asociar_sin_clave(self) -> None:
        with self.assertRaises(AsociacionesError):
            self.store().asociar("", "cand-1", "Lista B")

    def test_no_se_puede_asociar_sin_fuente(self) -> None:
        with self.assertRaises(AsociacionesError):
            self.store().asociar("canal-1", "cand-1", "")


class TestRechazar(_Base):
    def test_rechazar_se_lee(self) -> None:
        s = self.store()
        s.rechazar("canal-1", "cand-9")
        self.assertEqual(self.store().rechazadas("canal-1"), {"cand-9"})

    def test_rechazar_dos_veces_no_duplica(self) -> None:
        s = self.store()
        s.rechazar("canal-1", "cand-9")
        s.rechazar("canal-1", "cand-9")
        self.assertEqual(len(self.store().rechazadas("canal-1")), 1)

    def test_rechazar_mantiene_el_orden_reciente(self) -> None:
        s = self.store()
        for i in range(3):
            s.rechazar("canal-1", f"cand-{i}")
        # El orden vive en el archivo (es lo que permite podar por antigüedad);
        # lo que se lee es un conjunto, porque un descarte no se recorre.
        self.assertEqual(
            self.leido()["rechazos"]["canal-1"],
            ["cand-0", "cand-1", "cand-2"],
        )

    def test_los_descartes_no_crecen_sin_limite(self) -> None:
        # Un archivo que crece sin control es un archivo que se ha llenado
        # solo. Se conservan los más recientes, que son los reconocibles.
        s = self.store()
        for i in range(MAX_RECHAZOS_POR_CANAL + 20):
            s.rechazar("canal-1", f"cand-{i}")
        guardados = self.leido()["rechazos"]["canal-1"]
        self.assertEqual(len(guardados), MAX_RECHAZOS_POR_CANAL)
        self.assertEqual(guardados[-1], f"cand-{MAX_RECHAZOS_POR_CANAL + 19}")
        self.assertNotIn("cand-0", self.store().rechazadas("canal-1"))

    def test_rechazar_sin_clave_no_hace_nada(self) -> None:
        s = self.store()
        s.rechazar("", "cand-9")
        s.rechazar("canal-1", "")
        self.assertFalse(self.ruta.exists())

    def test_los_descartes_son_por_canal(self) -> None:
        s = self.store()
        s.rechazar("canal-1", "cand-9")
        self.assertEqual(self.store().rechazadas("canal-2"), set())


class TestRobustez(_Base):
    def test_archivo_corrupto_no_impide_arrancar(self) -> None:
        self.ruta.write_text("{esto no es json", encoding="utf-8")
        s = self.store()
        self.assertEqual(s.fuente_preferida("canal-1"), "")
        self.assertEqual(s.rechazadas("canal-1"), set())

    def test_archivo_corrupto_no_se_pisa_al_leer(self) -> None:
        self.ruta.write_text("{roto", encoding="utf-8")
        self.store().fuente_preferida("canal-1")
        # El original queda a salvo: `read_json_with_recovery` lo aparta.
        self.assertTrue(self.ruta.exists())

    def test_un_formato_inesperado_no_se_traga_en_silencio(self) -> None:
        # Una lista en vez de un objeto: se ignora, pero al escribir se
        # replaces por el formato bueno en vez de acumular basura.
        self.ruta.write_text("[1, 2, 3]", encoding="utf-8")
        s = self.store()
        s.asociar("canal-1", "cand-1", "Lista B")
        self.assertEqual(self.store().fuente_preferida("canal-1"), "Lista B")

    def test_entradas_basura_se_ignoran_y_no_tumban_el_resto(self) -> None:
        self.ruta.write_text(
            json.dumps(
                {
                    "asociaciones": {
                        "buena": {"fuente": "Lista B"},
                        "mala": "no soy un objeto",
                        "sin-fuente": {},
                    },
                    "rechazos": {"buena": ["c1", 2, "c2"], "mala": "nope"},
                }
            ),
            encoding="utf-8",
        )
        s = self.store()
        self.assertEqual(s.fuente_preferida("buena"), "Lista B")
        self.assertEqual(s.rechazadas("buena"), {"c1", "c2"})

    def test_lo_que_se_escribe_pasa_su_propio_validador(self) -> None:
        s = self.store()
        s.asociar("canal-1", "cand-1", "Lista B")
        s.rechazar("canal-1", "cand-9")
        validate_sources(self.leido())  # no lanza

    def test_el_archivo_lleva_schema_version(self) -> None:
        s = self.store()
        s.asociar("canal-1", "cand-1", "Lista B")
        self.assertEqual(self.leido().get("schema_version"), 1)

    def test_no_se_guarda_ningun_secreto(self) -> None:
        # El store sólo recibe claves; aun así, el test vigila que nadie
        # empiece a pasar la URL por aquí «para depurar».
        s = self.store()
        s.asociar("clave-1", "cand-1", "Lista B")
        s.rechazar("clave-1", "cand-9")
        crudo = self.ruta.read_text(encoding="utf-8")
        for secreto in ("password", "token", "username"):
            self.assertNotIn(secreto, crudo.lower())


class TestValidador(unittest.TestCase):
    """El validador es la puerta de entrada: un archivo raro no llega a la UI."""

    def test_un_objeto_válido_pasa(self) -> None:
        validate_sources({"asociaciones": {"a": {"fuente": "B"}},
                          "rechazos": {"a": ["c1"]}})

    def test_una_lista_no_pasa(self) -> None:
        with self.assertRaises(Exception):
            validate_sources([])

    def test_claves_vacias_no_pasan(self) -> None:
        with self.assertRaises(Exception):
            validate_sources({"asociaciones": {"  ": {"fuente": "B"}}})

    def test_una_fuente_que_no_es_texto_no_pasa(self) -> None:
        with self.assertRaises(Exception):
            validate_sources({"asociaciones": {"a": {"fuente": 3}}})

    def test_descartes_que_no_son_texto_no_pasan(self) -> None:
        with self.assertRaises(Exception):
            validate_sources({"rechazos": {"a": [1, 2]}})

    def test_un_millon_de_claves_no_pasa(self) -> None:
        # El tope está para que un archivo lleno a mano no se quede con toda
        # la memoria justo al abrir el modal que lo muestra.
        from thetvview.storage.schemas import _MAX_CLAVES_FUENTES

        with self.assertRaises(Exception):
            validate_sources(
                {"asociaciones": {str(i): {"fuente": "B"}
                                  for i in range(_MAX_CLAVES_FUENTES + 1)}}
            )

    def test_un_canal_con_miles_de_descartes_no_pasa(self) -> None:
        with self.assertRaises(Exception):
            validate_sources(
                {"rechazos": {"a": [f"c{i}" for i in range(5_000)]}}
            )

    def test_esta_registrado_como_formo_conocido(self) -> None:
        from thetvview.storage.schemas import get_validator

        self.assertIs(get_validator("sources"), validate_sources)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()