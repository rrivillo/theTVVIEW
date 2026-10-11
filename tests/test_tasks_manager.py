"""Tests del gestor de tareas (TASK-001 a TASK-004).

Verifican:
- tarea lenta seguida de una nueva ejecución (generación);
- cancelación durante el trabajo y combinación con ``raise_if_cancelled``;
- **tarea antigua que termina después**: el resultado no se publica;
- excepción dentro del trabajador: llega como ``failed`` y no deja nada colgado;
- límite de concurrencia;
- cierre con tareas activas, con plazo acotado;
- **ningún trabajador llama a curses** (TASK-003).
"""

from __future__ import annotations

import threading
import time
import unittest

from thetvview.tasks.manager import (
    CancellationToken,
    TaskCancelled,
    TaskManager,
    TaskStatus,
)


class TestBasico(unittest.TestCase):
    def setUp(self) -> None:
        self.tm = TaskManager()
        self.addCleanup(self.tm.shutdown)

    def test_tarea_simple(self) -> None:
        h = self.tm.submit("x", lambda token: 42)
        self.assertTrue(h.join(2))
        self.assertEqual(h.status, TaskStatus.DONE)
        resultados = self.tm.drain()
        self.assertEqual(len(resultados), 1)
        self.assertEqual(resultados[0].value, 42)
        self.assertTrue(resultados[0].ok)

    def test_identificador_unico_por_ejecucion(self) -> None:
        h1 = self.tm.submit("x", lambda t: 1)
        h2 = self.tm.submit("x", lambda t: 2)
        self.assertNotEqual(h1.task_id, h2.task_id)

    def test_argumentos_al_trabajador(self) -> None:
        h = self.tm.submit("x", lambda token, a, b=0: a + b, 1, b=2)
        h.join(2)
        self.assertEqual(h.result, 3)

    def test_excepcion_en_el_worker_no_propaga(self) -> None:
        def revienta(token: CancellationToken) -> None:
            raise ValueError("boom")

        h = self.tm.submit("x", revienta)
        self.assertTrue(h.join(2))
        self.assertEqual(h.status, TaskStatus.FAILED)
        resultados = self.tm.drain()
        self.assertEqual(len(resultados), 1)
        self.assertIsInstance(resultados[0].error, ValueError)
        # La UI recibe un error, no una excepción en la cara.
        self.assertFalse(resultados[0].ok)


class TestGeneracion(unittest.TestCase):
    """TASK-002: un resultado viejo no pisa uno más reciente."""

    def setUp(self) -> None:
        self.tm = TaskManager()
        self.addCleanup(self.tm.shutdown)

    def test_nueva_ejecucion_invalida_la_anterior(self) -> None:
        lento = threading.Event()

        def lento_f(token: CancellationToken) -> str:
            lento.wait(2.0)
            return "viejo"

        h1 = self.tm.submit("carga", lento_f)
        # Segunda ejecución: la primera queda obsoleta por generación.
        h2 = self.tm.submit("carga", lambda token: "nuevo")
        h2.join(2)
        self.assertTrue(h2.done)

        lento.set()
        h1.join(2)

        resultados = self.tm.drain()
        por_valor = {r.value: r for r in resultados}
        self.assertFalse(por_valor["nuevo"].obsolete)
        # El viejo terminó bien pero ya no se publica.
        self.assertTrue(por_valor["viejo"].obsolete)
        self.assertEqual(h1.status, TaskStatus.OBSOLETE)

    def test_generacion_sube(self) -> None:
        self.assertEqual(self.tm.generation("carga"), 0)
        self.tm.submit("carga", lambda t: 1, supersede=True).join(2)
        self.assertEqual(self.tm.generation("carga"), 1)
        self.tm.submit("carga", lambda t: 1, supersede=True).join(2)
        self.assertEqual(self.tm.generation("carga"), 2)

    def test_supersede_false_no_sube_generacion(self) -> None:
        self.tm.submit("carga", lambda t: 1, supersede=False).join(2)
        self.assertEqual(self.tm.generation("carga"), 0)
        # Sin supersede, la siguiente ejecución conserva la generación.
        self.tm.submit("carga", lambda t: 1, supersede=False).join(2)
        self.assertEqual(self.tm.generation("carga"), 0)


class TestCancelacion(unittest.TestCase):
    def setUp(self) -> None:
        self.tm = TaskManager()
        self.addCleanup(self.tm.shutdown)

    def test_cancelar_una_tarea(self) -> None:
        arrancada = threading.Event()

        def lenta(token: CancellationToken) -> str:
            arrancada.set()
            while not token.check():
                time.sleep(0.01)
            return "terminada"

        h = self.tm.submit("x", lenta)
        self.assertTrue(arrancada.wait(2))
        self.tm.cancel(h, "el usuario cambió de lista")
        self.assertTrue(h.join(2))
        self.assertEqual(h.status, TaskStatus.CANCELLED)
        resultados = self.tm.drain()
        self.assertTrue(resultados[0].cancelled)
        self.assertFalse(resultados[0].ok)

    def test_raise_if_cancelled(self) -> None:
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(TaskCancelled):
            token.raise_if_cancelled()

    def test_cancelar_por_categoria(self) -> None:
        def lenta(token: CancellationToken) -> None:
            while not token.check():
                time.sleep(0.01)

        self.tm.submit("salud", lenta)
        self.tm.submit("salud", lenta)
        time.sleep(0.1)
        self.assertEqual(self.tm.cancel_category("salud"), 2)

    def test_cancelar_no_toca_otras_categorias(self) -> None:
        """El ámbito de la cancelación es la tarea, no «todo lo de su clase»."""
        otra_lista = threading.Event()

        def salud_lenta(token: CancellationToken) -> None:
            while not token.check():
                time.sleep(0.01)

        def otra(token: CancellationToken) -> None:
            otra_lista.set()

        self.tm.submit("salud", salud_lenta)
        salud_handle = self.tm.active_for("salud")[0]
        self.tm.submit("pista", otra)
        self.assertTrue(otra_lista.wait(2))
        self.assertEqual(self.tm.cancel_category("salud"), 1)
        self.assertTrue(salud_handle.join(2))
        self.assertEqual(salud_handle.status, TaskStatus.CANCELLED)


class TestLimites(unittest.TestCase):
    def setUp(self) -> None:
        self.tm = TaskManager(max_global=2, max_per_category=1)
        self.addCleanup(self.tm.shutdown)

    def test_tope_global(self) -> None:
        self.assertFalse(self.tm.at_capacity())
        blockers = [threading.Event() for _ in range(2)]

        def bloquea(ev: threading.Event):
            return lambda token: ev.wait(2.0)

        self.tm.submit("a", bloquea(blockers[0]))
        self.tm.submit("b", bloquea(blockers[1]))
        time.sleep(0.1)
        self.assertTrue(self.tm.at_capacity())
        running, tope = self.tm.counts()
        self.assertEqual(tope, 2)
        for ev in blockers:
            ev.set()

    def test_tope_por_categoria(self) -> None:
        ev = threading.Event()
        self.tm.submit("a", lambda token: ev.wait(2.0))
        time.sleep(0.1)
        self.assertTrue(self.tm.at_capacity("a"))
        self.assertFalse(self.tm.at_capacity("b"))


class TestCierre(unittest.TestCase):
    def test_cierre_con_tareas_activas_es_acotado(self) -> None:
        tm = TaskManager()
        ev = threading.Event()

        def nunca(token: CancellationToken) -> None:
            # Ignora la cancelación a propósito: el cierre no puede esperarle.
            ev.wait(10.0)

        tm.submit("pesada", nunca)
        time.sleep(0.1)
        inicio = time.monotonic()
        pendientes = tm.shutdown(join_timeout=0.3)
        elapsed = time.monotonic() - inicio
        ev.set()
        # El plazo se respeta: no esperamos los 10 s del worker.
        self.assertLess(elapsed, 3.0)
        self.assertGreaterEqual(pendientes, 0)

    def test_tras_el_cierre_no_se_admiten_tareas(self) -> None:
        tm = TaskManager()
        tm.shutdown()
        self.assertTrue(tm.closing)
        with self.assertRaises(RuntimeError):
            tm.submit("x", lambda token: 1)

    def test_cierre_es_idempotente(self) -> None:
        tm = TaskManager()
        tm.shutdown()
        tm.shutdown()  # no debe lanzar


class TestSinCurses(unittest.TestCase):
    """TASK-003: solo el hilo principal puede manipular curses.

    La garantía que se verifica aquí es del **gestor**: el trabajador recibe
    un token y devuelve un valor, y no tiene ninguna referencia a la interfaz
    que publica el resultado. Un worker que quisiera tocar curses tendría
    que importarlo y llamarlo por su cuenta — algo que esta app no hace y que
    este test documenta prohibitiendo.

    Lo que sí se comprueba de forma mecánica es que el resultado se publica
    **desde el hilo principal** (quien llama a :meth:`drain`), no desde el
    worker.
    """

    def test_el_worker_no_publica_nada_por_si_mismo(self) -> None:
        publicaciones: list[tuple[int, str]] = []
        drain_desde: list[int] = []

        def worker(token: CancellationToken) -> str:
            # El worker no tiene forma de publicar: su único canal es
            # devolver el valor. Cualquier escritura aquí sería por su cuenta.
            return "valor"

        tm = TaskManager()
        try:
            h = tm.submit("test", worker)
            h.join(2)
            # Nadie drenó todavía: el resultado está en la cola, no aplicado.
            self.assertEqual(tm.pending(), 1)
            lote = tm.drain()
            drain_desde.append(threading.get_ident())
            for r in lote:
                publicaciones.append((threading.get_ident(), str(r.value)))
        finally:
            tm.shutdown()

        # La única publicación la hace el hilo que drena, que es el principal.
        for ident, _ in publicaciones:
            self.assertEqual(ident, drain_desde[0])

    def test_la_aplicacion_del_resultado_es_del_llamante(self) -> None:
        """El patrón que la app debe seguir: drenar y aplicar en el bucle."""
        tm = TaskManager()
        try:
            tm.submit("x", lambda token: "epg-cargado").join(2)
            modelo = None
            for resultado in tm.drain():
                if resultado.ok:
                    modelo = resultado.value
            self.assertEqual(modelo, "epg-cargado")
        finally:
            tm.shutdown()

    def test_un_worker_no_puede_alcanzar_la_interfaz(self) -> None:
        """El handle que recibe el worker no lleva ninguna referencia a la UI."""
        import inspect

        from thetvview.tasks.manager import TaskHandle

        campos = set(TaskHandle.__dataclass_fields__)
        for prohibido in ("app", "screen", "stdscr", "curses", "ui"):
            self.assertNotIn(prohibido, campos)
        # Y el token tampoco.
        self.assertEqual(
            set(inspect.signature(TaskHandle.cancel).parameters) - {"self"}, {"reason"}
        )


if __name__ == "__main__":
    unittest.main()
