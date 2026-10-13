"""H-03 — Esquema v2 del historial: migración desde la versión 1.

La versión 1 del historial guardaba cinco campos: cuándo, qué canal, si
funcionó, un motivo y una fase. Con eso no se puede contestar ninguna de las
preguntas que el usuario hace de verdad:

- **¿De qué lista venía?** Dos listas pueden llamarse «Deportes» y traer el
  mismo canal con distinto resultado. Sin ``source_id`` el historial mezcla
  dos fuentes y la recurrencia miente.
- **¿Con qué reproductor?** Un fallo de mpv no dice nada de vlc. Sin
  ``player``, «este canal falla» no distingue «este canal falla siempre» de
  «este canal falla con este motor».
- **¿Por qué exactamente?** El ``motivo`` de la v1 era el veredicto del
  diagnóstico en castellano libre. Sin ``error_code`` no se puede comparar
  fallos ni agruparlos.
- **¿Y si reintentar serviría?** Sin ``recoverable`` la recurrencia cuenta
  igual un 401 que un corte de red, y el usuario recibe el mismo consejo
  para dos cosas opuestas.
- **¿Cuánto duró?** Sin ``duration_ms`` no se distingue un fallo inmediato
  de uno que tarda medio minuto en caer.

Y `source_id` entra **opaco**, no en crudo. El identificador real de una
fuente lleva dentro la URL y, en Xtream, el usuario: es exactamente el dato
que no puede llegar a un fichero que el usuario puede compartir. Al disco va
el tipo, el nombre de la lista (que no es secreto y hay que poder leer) y un
resumen del resto.

De migración se exige lo mismo que de cualquier escritura aquí: **atómica,
reversible y probada con datos reales de la versión anterior**. La fixture
`tests/fixtures/diagnostics_v1.json` no es inventada: tiene la forma exacta
que escribía la v1, con sus nueve entradas y sus veredictos en castellano.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.diagnostics.history import (
    VERSION_ACTUAL,
    HistorialDiagnostico,
    RegistroHistorial,
    source_id_opaco,
)

FIXTURES = Path(__file__).parent / "fixtures"
V1_REAL = FIXTURES / "diagnostics_v1.json"


class _Reloj:
    """Reloj falso, para que los límites no dependan de la hora real."""

    def __init__(self, inicio: float = 1_800_000_000.0) -> None:
        self.t = inicio

    def __call__(self) -> float:
        return self.t

    def avanzar(self, segundos: float) -> float:
        self.t += segundos
        return self.t


class TestLosCamposNuevos(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ruta = Path(self._tmp.name) / "diagnostics.json"
        self.reloj = _Reloj()

    def _historial(self, **kwargs) -> HistorialDiagnostico:
        return HistorialDiagnostico(self.ruta, ahora=self.reloj, **kwargs)

    def test_se_puede_registrar_todo_lo_nuevo(self):
        h = self._historial()
        h.registrar(
            "espanol.espn", False,
            motivo="No se pudo reproducir", fase="supervision",
            source_id="m3u|Deportes|http://a.test/l.m3u", player="mpv",
            error_code="network_error", recoverable=True,
            duration_ms=12_345.6,
        )
        entrada = h.entradas()[0]
        self.assertIn("Deportes", entrada.source_id)
        self.assertNotIn("a.test", entrada.source_id)
        self.assertEqual(entrada.player, "mpv")
        self.assertEqual(entrada.error_code, "network_error")
        self.assertTrue(entrada.recoverable)
        self.assertAlmostEqual(entrada.duration_ms or 0.0, 12_345.6, places=3)

    def test_lo_no_medido_no_se_inventa(self):
        """`duration_ms` ausente es «nadie midió», no cero."""
        h = self._historial()
        h.registrar("c", True)
        self.assertIsNone(h.entradas()[0].duration_ms)
        self.assertNotIn("duration_ms", h.entradas()[0].a_dict())

    def test_lo_desconocido_no_se_inventa(self):
        """`recoverable` ausente es «no se sabe», no False."""
        h = self._historial()
        h.registrar("c", False)
        self.assertIsNone(h.entradas()[0].recoverable)
        self.assertNotIn("recoverable", h.entradas()[0].a_dict())

    def test_un_error_code_inventado_no_se_guarda(self):
        """H-02 aplicado al historial: la puerta de la taxonomía también."""
        h = self._historial()
        h.registrar("c", False, error_code="fallo_de_red")
        self.assertEqual(h.entradas()[0].error_code, "")

    def test_un_error_code_valido_si_se_guarda(self):
        h = self._historial()
        h.registrar("c", False, error_code="auth_failed", recoverable=False)
        entrada = h.entradas()[0]
        self.assertEqual(entrada.error_code, "auth_failed")
        self.assertIs(entrada.recoverable, False)

    def test_recuperable_incoherente_con_el_codigo_se_corrije(self):
        """`recoverable` no es libre: sale de la política del reproductor."""
        h = self._historial()
        # auth_failed es de los que reintentar no arregla.
        h.registrar("c", False, error_code="auth_failed", recoverable=True)
        self.assertIs(h.entradas()[0].recoverable, False)

    def test_el_versionado_es_el_del_esquema_v2(self):
        self.assertEqual(VERSION_ACTUAL, 2)


class TestSourceIdOpaco(unittest.TestCase):
    """El identificador de fuente no puede llegar a disco en crudo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ruta = Path(self._tmp.name) / "diagnostics.json"

    def test_el_id_real_de_una_fuente_xtream_no_se_guarda(self):
        crudo = "xtream|Mi Panel|http://panel.test:8080|MiUsuario"
        opaco = source_id_opaco(crudo)
        self.assertNotIn("panel.test", opaco)
        self.assertNotIn("MiUsuario", opaco)
        self.assertNotIn("8080", opaco)

    def test_el_id_real_de_una_lista_m3u_no_se_guarda(self):
        crudo = "m3u|Futbol|http://usuario:clave@tv.example/lista.m3u"
        opaco = source_id_opaco(crudo)
        self.assertNotIn("clave", opaco)
        self.assertNotIn("tv.example", opaco)
        self.assertNotIn("usuario", opaco)

    def test_el_nombre_de_la_lista_si_se_guarda(self):
        """Sin esto el modal no puede decir de qué lista habla."""
        self.assertIn("Deportes", source_id_opaco("m3u|Deportes|http://a.test/l.m3u"))

    def test_es_estable(self):
        """Dos registros del mismo id real dan el mismo opaco."""
        crudo = "m3u|Deportes|http://a.test/l.m3u"
        self.assertEqual(source_id_opaco(crudo), source_id_opaco(crudo))

    def test_listas_distintas_dan_opacos_distintos(self):
        a = source_id_opaco("m3u|Deportes|http://a.test/uno.m3u")
        b = source_id_opaco("m3u|Deportes|http://a.test/dos.m3u")
        self.assertNotEqual(a, b)

    def test_lo_que_no_id_es_opaco_no_rompe(self):
        for raro in ("", "   ", None, 42):
            with self.subTest(valor=repr(raro)):
                opaco = source_id_opaco(raro)  # type: ignore[arg-type]
                self.assertIsInstance(opaco, str)

    def test_al_registrar_se_opaca_el_id(self):
        reloj = _Reloj()
        h = HistorialDiagnostico(self.ruta, ahora=reloj)
        crudo = "m3u|Deportes|http://usuario:clave@tv.example/l.m3u"
        h.registrar("espn", False, source_id=crudo)
        guardado = self.ruta.read_text(encoding="utf-8")
        self.assertNotIn("clave", guardado)
        self.assertNotIn("tv.example", guardado)


class TestMigracionDesdeLaVersion1(unittest.TestCase):
    """Migración probada con datos reales, no con un json inventado."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ruta = self.dir / "diagnostics.json"
        self.reloj = _Reloj()
        # El reloj se pone en el momento del último dato de la fixture, para
        # que la limpieza por edad (30 días) no tire nada durante el test.
        crudo = json.loads(V1_REAL.read_text(encoding="utf-8"))
        self.datos_v1 = crudo
        self.reloj.t = max(e["momento"] for e in crudo["entradas"]) + 86_400.0
        self.ruta.write_text(V1_REAL.read_text(encoding="utf-8"), encoding="utf-8")

    def test_la_fixture_es_de_verdad_version_1(self):
        self.assertEqual(self.datos_v1["version"], 1)
        self.assertGreaterEqual(len(self.datos_v1["entradas"]), 5)
        # Campos que la v1 NO conocía y la v2 sí:
        for campo in ("source_id", "player", "error_code", "recoverable",
                      "duration_ms"):
            for entrada in self.datos_v1["entradas"]:
                self.assertNotIn(campo, entrada)

    def test_se_leen_todas_las_entradas(self):
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        self.assertEqual(len(h.entradas()), len(self.datos_v1["entradas"]))

    def test_los_datos_viejos_no_se_pierden(self):
        """Migrar no puede perder nada de lo que ya se sabía."""
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        antes = {(e["momento"], e["canal"], e["exito"], e["motivo"], e["fase"])
                 for e in self.datos_v1["entradas"]}
        despues = {(e.momento, e.canal, e.exito, e.motivo, e.fase)
                   for e in h.entradas()}
        self.assertEqual(antes, despues)

    def test_los_campos_nuevos_vienen_desconocidos(self):
        """Migrados no son inventados: lo que no se sabía, no se rellena."""
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        for entrada in h.entradas():
            with self.subTest(canal=entrada.canal):
                self.assertEqual(entrada.source_id, "")
                self.assertEqual(entrada.player, "")
                self.assertEqual(entrada.error_code, "")
                self.assertIsNone(entrada.recoverable)
                self.assertIsNone(entrada.duration_ms)

    def test_al_escribir_sube_a_la_version_2(self):
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        h.registrar("nuevo", True)
        escrito = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(escrito["version"], 2)

    def test_al_reescribir_no_se_pierde_nada_del_viejo(self):
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        h.registrar("nuevo", True)
        h2 = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        self.assertEqual(len(h2.entradas()), len(self.datos_v1["entradas"]) + 1)

    def test_migrar_es_atomico(self):
        """Nunca queda un fichero a medias: o entero o el de antes."""
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        with mock.patch.object(
            HistorialDiagnostico, "_escribir",
            side_effect=OSError("disco lleno"),
        ):
            h.registrar("nuevo", True)  # no lanza
        # El fichero en disco sigue siendo el viejo, entero y válido.
        en_disco = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(en_disco["version"], 1)
        self.assertEqual(len(en_disco["entradas"]), len(self.datos_v1["entradas"]))

    def test_no_quedan_temporales_tras_la_migracion(self):
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        h.registrar("nuevo", True)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])


class TestLaMigracionEsReversible(unittest.TestCase):
    """Se puede volver a la versión 1, y lo que se pierde se sabe."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ruta = Path(self._tmp.name) / "diagnostics.json"
        self.reloj = _Reloj()

    def test_se_puede_escribir_un_fichero_v1(self):
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        h.registrar("espn", False, motivo="READY", fase="diagnose")
        h.escribir_como(1)
        escrito = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(escrito["version"], 1)
        self.assertEqual(set(escrito["entradas"][0]), {
            "momento", "canal", "exito", "motivo", "fase"
        })

    def test_la_v1_conserva_los_cinco_campos_originales(self):
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        h.registrar("espn", True, motivo="READY", fase="diagnose",
                    player="mpv", error_code="network_error", duration_ms=10.0)
        h.escribir_como(1)
        h2 = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        entrada = h2.entradas()[0]
        self.assertEqual(entrada.canal, "espn")
        self.assertEqual(entrada.motivo, "READY")
        # Y lo que no cabe en la v1 se pierde con honestidad: no se rellena.
        self.assertEqual(entrada.player, "")
        self.assertIsNone(entrada.duration_ms)

    def test_un_lector_v1_sigue_leyendo_el_fichero_v2(self):
        """Al revés también: lo nuevo no rompe a lo viejo.

        Es la otra mitad de «reversible»: un `diagnostics.json` v2 escrito por
        esta versión se puede abrir con la anterior, porque la v1 leía con
        ``.get()`` e ignoraba las claves que no conocía.
        """
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        h.registrar("espn", False, motivo="READY", player="vlc")
        datos = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(datos["version"], 2)

        # Lectura «a la v1»: sólo los cinco campos conocidos.
        class _LectorV1:
            def __init__(self, datos: dict) -> None:
                self.datos = datos

            def leer(self) -> list[dict]:
                crudos = self.datos.get("entradas")
                if not isinstance(crudos, list):
                    return []
                return [
                    {
                        "momento": float(e.get("momento")),
                        "canal": str(e.get("canal", "")),
                        "exito": bool(e.get("exito", False)),
                        "motivo": str(e.get("motivo", "")),
                        "fase": str(e.get("fase", "")),
                    }
                    for e in crudos
                ]

        leido = _LectorV1(datos).leer()
        self.assertEqual(len(leido), 1)
        self.assertEqual(leido[0]["canal"], "espn")
        self.assertNotIn("player", leido[0])


class TestRegistroRoundTrip(unittest.TestCase):
    """Un registro que sale del JSON vuelve igual, y los raros no entran."""

    def test_ida_y_vuelta_completa(self):
        original = RegistroHistorial(
            momento=1_800_000_000.5, canal="espn", exito=False,
            motivo="No se pudo reproducir", fase="supervision",
            source_id=source_id_opaco("m3u|Deportes|http://a.test/l.m3u"),
            player="mpv",
            error_code="network_error", recoverable=True, duration_ms=1234.5,
        )
        vuelta = RegistroHistorial.de_dict(original.a_dict())
        self.assertEqual(original, vuelta)

    def test_un_registro_v1_izado_no_lanza(self):
        vuelta = RegistroHistorial.de_dict({
            "momento": 10.0, "canal": "a", "exito": True,
            "motivo": "READY", "fase": "diagnose",
        })
        assert vuelta is not None
        self.assertEqual(vuelta.canal, "a")
        self.assertEqual(vuelta.source_id, "")

    def test_un_duration_ms_negativo_se_rechaza(self):
        """No es una medición posible: es un dato imposible."""
        with self.assertRaises(ValueError):
            RegistroHistorial(momento=1.0, canal="a", exito=True,
                              duration_ms=-1.0)

    def test_un_duration_ms_no_numerico_se_rechaza(self):
        with self.assertRaises(ValueError):
            RegistroHistorial(momento=1.0, canal="a", exito=True,
                              duration_ms="un rato")  # type: ignore[arg-type]


class TestOpacarEsIdempotente(unittest.TestCase):
    """Se opaca al escribir y al leer: sin idempotencia, la ida y vuelta
    machaca el identificador dos veces y el historial deja de agrupar."""

    def test_opacar_dos_veces_da_lo_mismo(self):
        crudo = "m3u|Deportes|http://usuario:clave@tv.example/l.m3u"
        una = source_id_opaco(crudo)
        self.assertEqual(source_id_opaco(una), una)

    def test_lo_ya_opacado_pasa_intacto(self):
        opaco = source_id_opaco("m3u|Deportes|http://a.test/l.m3u")
        self.assertEqual(source_id_opaco(opaco), opaco)

    def test_un_source_id_crudo_manoscrito_tampoco_se_guarda(self):
        """Al leer también se opaca: un fichero editado a mano no filtra."""
        h = HistorialDiagnostico.__new__(HistorialDiagnostico)
        registro = RegistroHistorial.de_dict({
            "momento": 1.0, "canal": "espn", "exito": False,
            "source_id": "xtream|Panel|http://panel.test:8080|MiUsuario",
        })
        self.assertIsNotNone(registro)
        assert registro is not None
        self.assertNotIn("panel.test", registro.source_id)
        self.assertNotIn("MiUsuario", registro.source_id)
        del h


if __name__ == "__main__":
    unittest.main()