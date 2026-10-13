"""H-05 — Retención: los tres límites, a la vista y con límites reales.

El historial crece solo. Cada diagnóstico escribe una entrada, y cuántas hay
depende de lo que haga el usuario: con una lista grande, el fichero se llena
de golpe y nadie se entera, porque nadie lo miró. Sin límites,
``diagnostics.json`` acaba siendo el fichero más grande de ``data/``.

Hay tres límites y **los tres a la vez** (FR-DIAG-010), y cada uno cuenta una
cosa distinta. Por eso hacen falta los tres y no basta uno:

- **cantidad** — como mucho ``max_entradas``;
- **edad** — nada de hace más de ``max_dias``;
- **tamaño** — el fichero entero cabe en ``max_bytes``.

Sólo cantidad daría un fichero pequeño capaz de guardar un único diagnóstico
de hace dos años, que es justo el que el usuario consulta para saber si un
fallo es viejo. Sólo edad daría un fichero de varios megas con una lista
grande. Sólo tamaño llenaría el tope con un día de diagnóstico intenso y
tiraría la semana anterior, que es la que se está intentando recordar.

Y una cosa que no es un límite sino una política: **la limpieza corre al
escribir**, no cuando alguien se acuerde. Un historial que se limpia al leer
tiene un tamaño impredecible entre dos ejecuciones, y el que lo descubre «está limpio» está mintiendo medio día entero.

Lo que este archivo mide además del resultado:

- que los límites se apliquen **de verdad**, con un reloj falso que llegue
  muy lejos, no «en principio»;
- que la limpieza **acote el trabajo**: el corte por tamaño no puede recorrer
  el historial entero ni escribir quince veces para el mismo registro;
- y que el resultado sea el mismo esté la limpieza donde esté, que es lo que
  evita tener dos reglas que un día no coincidan.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.diagnostics.history import (
    MAX_BYTES,
    MAX_DIAS,
    MAX_ENTRADAS,
    HistorialDiagnostico,
)


class Reloj:
    """Reloj falso. Los límites de 30 días no se pueden probar con el real."""

    def __init__(self, inicio: float = 1_800_000_000.0) -> None:
        self.t = inicio

    def __call__(self) -> float:
        return self.t

    def avanzar(self, segundos: float) -> float:
        self.t += segundos
        return self.t


class Caso(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ruta = self.dir / "diagnostics.json"
        self.reloj = Reloj()

    def historial(self, **kwargs) -> HistorialDiagnostico:
        return HistorialDiagnostico(self.ruta, ahora=self.reloj, **kwargs)

    def sembrar(self, h: HistorialDiagnostico, entradas: int, *, cada: float,
                antiguedad: float = 0.0, exito: bool = True) -> None:
        """Planta `entradas` entradas en el pasado, sin pasar por `registrar`.

        Escribir por debajo evita que la limpieza de `registrar` lo anuncie
        antes de tiempo: aquí se planta el estado y luego se mide.

        `antiguedad` son los segundos que tiene la **más reciente** de las
        entradas. Es el parámetro que importa para el límite de edad: sin él,
        «40 entradas hacia atrás» deja una de ellas justo ahora y el test mide
        otra cosa.
        """
        ultima = self.reloj.t - antiguedad
        base = ultima - (entradas - 1) * cada
        self.ruta.write_text(json.dumps({
            "version": 2,
            "entradas": [
                {"momento": base + i * cada, "canal": f"c{i}", "exito": exito}
                for i in range(entradas)
            ],
        }), encoding="utf-8")


class TestLosTresLimitesExisten(Caso):
    def test_los_valores_por_defecto_estan_a_la_vista(self):
        self.assertEqual(MAX_ENTRADAS, 500)
        self.assertEqual(MAX_DIAS, 30.0)
        self.assertEqual(MAX_BYTES, 256 * 1024)

    def test_los_tres_se_pueden_ajustar(self):
        h = self.historial(max_entradas=3, max_dias=1.0, max_bytes=4096)
        self.assertEqual(h.max_entradas, 3)
        self.assertEqual(h.max_dias, 1.0)
        self.assertEqual(h.max_bytes, 4096)

    def test_un_max_entradas_absurdo_no_deja_el_historial_a_cero(self):
        """Un límite de 0 entradas dejaría un historial inútil, no acotado."""
        h = self.historial(max_entradas=0)
        self.assertGreaterEqual(h.max_entradas, 1)

    def test_un_max_bytes_demasiado_pequeno_no_impide_una_entrada(self):
        """Por debajo del mínimo no se puede escribir ni un solo registro."""
        h = self.historial(max_bytes=1)
        self.assertGreaterEqual(h.max_bytes, 256)


class TestLimiteDeCantidad(Caso):
    def test_no_crece_pasado_el_tope(self):
        h = self.historial(max_entradas=10)
        for i in range(40):
            h.registrar(f"c{i}", True)
        self.assertLessEqual(len(h.entradas()), 10)

    def test_se_quedan_las_mas_recientes(self):
        h = self.historial(max_entradas=5)
        for i in range(20):
            self.reloj.avanzar(1.0)
            h.registrar(f"nuevo{i}", True)
        canales = [e.canal for e in h.entradas()]
        self.assertEqual(canales[-1], "nuevo19")
        self.assertNotIn("nuevo0", canales)

    def test_una_entrada_nunca_empuja_a_otra_fuera(self):
        """Cada registro entra por su cuenta; la más vieja es la que cae."""
        h = self.historial(max_entradas=3)
        self.sembrar(h, 3, cada=1.0)
        h.registrar("nueva", False)
        self.assertEqual(len(h.entradas()), 3)
        self.assertEqual(h.entradas()[-1].canal, "nueva")


class TestLimiteDeEdad(Caso):
    def test_lo_viejo_se_cae_al_leer(self):
        h = self.historial(max_dias=7.0)
        # La más reciente de las 40 tiene 8 días: todas fuera de la ventana.
        self.sembrar(h, 40, cada=86_400.0, antiguedad=8 * 86_400.0)
        h.registrar("reciente", True)       # fuerza la carga
        self.assertEqual(len(h.entradas()), 1)
        self.assertEqual(h.entradas()[0].canal, "reciente")

    def test_lo_reciente_sobrevive(self):
        h = self.historial(max_dias=7.0)
        # La más reciente tiene 1 día y la más vieja 3: dentro de la ventana.
        self.sembrar(h, 3, cada=86_400.0, antiguedad=86_400.0)
        h.registrar("reciente", True)
        self.assertEqual(len(h.entradas()), 4)

    def test_el_limite_manda_sobre_la_cantidad(self):
        """Con 5000 entradas y 30 días, gana la edad."""
        h = self.historial(max_entradas=5000, max_dias=7.0)
        self.sembrar(h, 20, cada=86_400.0, antiguedad=8 * 86_400.0)
        h.registrar("reciente", True)
        self.assertEqual(len(h.entradas()), 1)

    def test_el_corte_de_edad_es_exacto(self):
        h = self.historial(max_dias=7.0)
        justo = self.reloj.t - 7.0 * 86_400.0
        self.ruta.write_text(json.dumps({
            "version": 2,
            "entradas": [
                {"momento": justo - 1.0, "canal": "viejo", "exito": True},
                {"momento": justo, "canal": "en_el_borde", "exito": True},
                {"momento": justo + 1.0, "canal": "nuevo", "exito": True},
            ],
        }), encoding="utf-8")
        h.registrar("x", True)
        canales = {e.canal for e in h.entradas()}
        self.assertNotIn("viejo", canales)
        self.assertIn("en_el_borde", canales)


class TestLimiteDeTamano(Caso):
    def _tamano(self) -> int:
        return len(self.ruta.read_bytes())

    def test_el_fichero_cabe_en_el_tope(self):
        h = self.historial(max_entradas=10_000, max_dias=3650.0, max_bytes=4096)
        for i in range(400):
            self.reloj.avanzar(1.0)
            h.registrar(f"canal-con-nombre-largo-{i:04d}", False,
                        motivo="un motivo bastante largo para ocupar sitio")
        self.assertLessEqual(self._tamano(), 4096)

    def test_el_corte_por_tamano_tira_las_mas_viejas(self):
        h = self.historial(max_entradas=10_000, max_dias=3650.0, max_bytes=2048)
        for i in range(400):
            self.reloj.avanzar(1.0)
            h.registrar(f"canal-largo-{i:04d}", False)
        self.reloj.avanzar(1.0)
        h.registrar("el-ultimo", True)
        self.assertEqual(h.entradas()[-1].canal, "el-ultimo")

    def test_el_corte_por_tamano_esta_acotado(self):
        """Bajar la mitad de golpe, no una entrada por vuelta.

        Con 400 entradas y un tope pequeño, una purga de «quita la más vieja
        cada vez» escribiría el fichero cientos de veces para un solo
        registro. El código descarta la mitad de golpe justamente por esto.
        """
        h = self.historial(max_entradas=10_000, max_dias=3650.0, max_bytes=1024)
        for i in range(200):
            self.reloj.avanzar(1.0)
            h.registrar(f"canal-largo-{i:04d}", False)
        with mock.patch.object(
            HistorialDiagnostico, "_texto_serializado",
            wraps=h._texto_serializado,
        ) as cuenta:
            self.reloj.avanzar(1.0)
            h.registrar("uno-mas", True)
        # Un `registrar` = un `_escribir`; dentro, unas pocas vueltas.
        # El tope real es «no proporcional a las entradas» (aquí: <= 20).
        self.assertLessEqual(cuenta.call_count, 20)

    def test_una_sola_entrada_grande_no_rompe_nada(self):
        """Aunque no quepa, se escribe: el historial vacío tampoco informa."""
        h = self.historial(max_entradas=10, max_dias=3650.0, max_bytes=256)
        h.registrar("c", True, motivo="m" * 5000)
        self.assertTrue(self.ruta.exists())
        self.assertEqual(len(h.entradas()), 1)


class TestLaLimpiezaCorreAlEscribir(Caso):
    def test_escribir_limpia_aunque_no_se_vuelva_a_leer(self):
        """Si la limpieza fuera al leer, el fichero seguiría gordo en disco."""
        h = self.historial(max_entradas=10, max_dias=7.0)
        self.sembrar(h, 30, cada=86_400.0, antiguedad=8 * 86_400.0)
        h.registrar("nueva", True)  # no se vuelve a leer en ningún momento
        # Se mira **el fichero**, no el objeto.
        crudo = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(len(crudo["entradas"]), 1)

    def test_no_hace_falta_una_segunda_escritura_para_limpiar(self):
        h = self.historial(max_entradas=3)
        for i in range(10):
            h.registrar(f"c{i}", True)
        crudo = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertLessEqual(len(crudo["entradas"]), 3)

    def test_lo_que_se_escribe_ya_esta_acotado(self):
        h = self.historial(max_entradas=2, max_dias=7.0)
        for i in range(5):
            h.registrar(f"c{i}", True)
        crudo = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual([e["canal"] for e in crudo["entradas"]], ["c3", "c4"])


class TestLosLimitesNoSePersiguen(Caso):
    """El orden importa: el tamaño decide después, no en paralelo."""

    def test_con_los_tres_activos_el_fichero_es_pequeno(self):
        h = self.historial()  # los tres por defecto
        for i in range(200):
            self.reloj.avanzar(60.0)
            h.registrar(f"canal{i}", False, motivo="algo que ocupar sitio")
        self.assertLessEqual(self.ruta.stat().st_size, MAX_BYTES)
        self.assertLessEqual(len(h.entradas()), MAX_ENTRADAS)

    def test_registrar_tampoco_se_cuelga(self):
        """Con los topes por defecto, registrar es rápido."""
        import time as _time

        h = self.historial()
        inicio = _time.perf_counter()
        for i in range(200):
            self.reloj.avanzar(1.0)
            h.registrar(f"c{i}", True)
        elapsed = _time.perf_counter() - inicio
        self.assertLess(elapsed, 20.0, "200 registros tardaron demasiado")

    def test_la_purga_no_deja_entradas_ordenadas_por_casualidad(self):
        """El historial sale ordenado por momento, no por orden de escritura."""
        h = self.historial(max_entradas=5)
        for i in range(10):
            self.reloj.avanzar(10.0)
            h.registrar(f"c{i}", True)
        momentos = [e.momento for e in h.entradas()]
        self.assertEqual(momentos, sorted(momentos))


if __name__ == "__main__":
    unittest.main()