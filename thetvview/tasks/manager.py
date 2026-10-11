"""Gestor de tareas en segundo plano (P0.3, TASK-001 a TASK-004).

El plan pide un administrador **ligero** —sin framework pesado— con:

- identificador único por ejecución y **generación** por categoría, para que
  un resultado viejo no pise uno más reciente (TASK-002);
- estado observable y **señal de cancelación cooperativa**;
- **cola de resultados** drenada por el hilo principal: ningún trabajador
  toca ``curses`` ni muta la interfaz (TASK-003);
- registro de recursos a cerrar;
- límites de concurrencia por categoría y global;
- cierre acotado: dejar de aceptar tareas, cancelar, esperar con plazo y
  liberar lo que quede (TASK-004).

Filosofía
---------

Una tarea es ``(generación, función)``. La función corre en un hilo daemon,
recibe un :class:`CancellationToken` para comprobar la cancelación en puntos
seguros, y devuelve un valor. Ese valor **no** se aplica al modelo: se deja
en la cola y lo aplica el hilo principal en :meth:`TaskManager.drain`. Esa
separación es la que garantiza que un trabajador no pueda tocar la interfaz,
porque no tiene referencia a ella.

La **generación** es lo que impide el pisado: cada vez que se lanza una tarea
de una categoría, su generación sube. Cuando el resultado vuelve, el gestor
comprueba si su generación sigue siendo la vigente; si no, lo marca como
obsoleto y **no lo publica**. La tarea obsoleta sí termina su limpieza.

Ambito de la cancelación
------------------------

Cancelar una tarea **no** cancela las demás de su categoría ni de otras: el
ámbito es la tarea concreta, salvo que se pida el de la categoría
(:meth:`TaskManager.cancel_category`). Dos sondeos de canales distintos
pueden convivir; dos cargas de *la misma* fuente, no.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

__all__ = [
    "TaskStatus",
    "CancellationToken",
    "TaskHandle",
    "TaskResult",
    "TaskManager",
    "TaskCancelled",
]

T = TypeVar("T")


class TaskCancelled(Exception):
    """La tarea se paró porque se pidió su cancelación."""


class TaskStatus:
    """Estados posibles de una tarea."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OBSOLETE = "obsolete"  # terminó bien, pero su resultado ya no se publica


class CancellationToken:
    """Señal de cancelación cooperativa y seguro de parada por excepción.

    Se comparte con el trabajador, que la consulta en puntos seguros::

        for chunk in stream():
            token.raise_if_cancelled()
            procesar(chunk)

    ``check()`` es el sondeo barato; ``raise_if_cancelled()`` es el idiomático
    porque convierte la cancelación en una excepción y garantiza que el
    ``finally`` del trabajador se ejecute.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> bool:
        """Devuelve True si hay que parar (alias de :attr:`cancelled`)."""
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Espera a la cancelación. True si se canceló dentro del plazo."""
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TaskCancelled("la tarea fue cancelada")


@dataclass
class TaskHandle(Generic[T]):
    """Identidad y estado de una ejecución concreta."""

    task_id: int
    category: str
    generation: int
    status: str = TaskStatus.PENDING
    result: Any = None
    error: BaseException | None = None
    token: CancellationToken = field(default_factory=CancellationToken)
    thread: threading.Thread | None = None
    #: Razón de la cancelación, si la hubo.
    cancel_reason: str = ""

    @property
    def done(self) -> bool:
        return self.status in (
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.OBSOLETE,
        )

    @property
    def running(self) -> bool:
        return self.status == TaskStatus.RUNNING

    @property
    def cancelled(self) -> bool:
        return self.status == TaskStatus.CANCELLED

    @property
    def failed(self) -> bool:
        return self.status == TaskStatus.FAILED

    def cancel(self, reason: str = "") -> None:
        """Pide la cancelación cooperativa de esta tarea."""
        if reason:
            self.cancel_reason = reason
        self.token.cancel()

    def join(self, timeout: float | None = None) -> bool:
        """Espera al hilo. True si terminó dentro del plazo."""
        thread = self.thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()


@dataclass
class TaskResult:
    """Un resultado (o un fallo) listo para que lo aplique el hilo principal."""

    handle: TaskHandle
    value: Any = None
    error: BaseException | None = None
    cancelled: bool = False
    obsolete: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled and not self.obsolete


class TaskManager:
    """Administrador de tareas con generación, cancelación y cola de salida.

    Uso típico desde la TUI::

        manager = TaskManager()
        handle = manager.submit("epg-load", cargar_epg, fuentes)

        # ... en el bucle principal:
        for resultado in manager.drain():
            if resultado.ok:
                self.epg = resultado.value

        # al cerrar:
        manager.shutdown(join_timeout=2.0)
    """

    def __init__(
        self,
        *,
        max_global: int = 6,
        max_per_category: int = 3,
        on_error: Callable[[TaskResult], None] | None = None,
    ) -> None:
        """
        Args:
            max_global: máximo de tareas **corriendo** a la vez, en total.
            max_per_category: máximo por categoría.
            on_error: callback para los fallos. Se invoca en el hilo principal
                (desde :meth:`drain`) para que el mensaje llegue a un modal.
        """
        if max_global < 1:
            max_global = 1
        if max_per_category < 1:
            max_per_category = 1
        self._max_global = max_global
        self._max_per_category = max_per_category
        self._on_error = on_error

        self._lock = threading.Lock()
        self._next_id = 1
        self._generations: dict[str, int] = {}
        self._running: dict[int, TaskHandle] = {}
        self._results: deque[TaskResult] = deque()
        self._closing = False

    # -- consultas -----------------------------------------------------------

    @property
    def closing(self) -> bool:
        """True cuando el gestor ya no admite tareas nuevas."""
        with self._lock:
            return self._closing

    @property
    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def generation(self, category: str) -> int:
        """Generación vigente de `category` (0 si nunca se lanzó)."""
        with self._lock:
            return self._generations.get(category, 0)

    def is_current(self, handle: TaskHandle) -> bool:
        """True si `handle` sigue siendo lo último de su categoría.

        Es el **guard de obsolescencia** (TASK-002): antes de publicar un
        resultado se pregunta esto, y si es False el resultado se marca
        ``obsolete`` y se descarta (la limpieza ya se hizo).
        """
        with self._lock:
            return self._generations.get(handle.category, 0) == handle.generation

    def active_for(self, category: str) -> list[TaskHandle]:
        with self._lock:
            return [h for h in self._running.values() if h.category == category]

    # -- envío ---------------------------------------------------------------

    def submit(
        self,
        category: str,
        func: Callable[[CancellationToken], T],
        *args: Any,
        supersede: bool = True,
        **kwargs: Any,
    ) -> TaskHandle:
        """Lanza `func` en segundo plano y devuelve su identificador.

        Args:
            category: nombre de la categoría (p. ej. ``"epg-load"``). Decide
                el límite de concurrencia y el ámbito de la generación.
            func: recibe el :class:`CancellationToken` y devuelve el valor.
            supersede: si True (lo normal), lanza una generación nueva y marca
                como obsoletas las anteriores de la categoría. Si False,
                convive con ellas.
            args/kwargs: se pasan a `func` después del token.

        Returns:
            El :class:`TaskHandle` de la ejecución.

        Raises:
            RuntimeError: si el gestor está cerrándose.
        """
        with self._lock:
            if self._closing:
                raise RuntimeError("el gestor de tareas está cerrándose")
            if supersede:
                self._generations[category] = self._generations.get(category, 0) + 1
                generation = self._generations[category]
            else:
                generation = self._generations.get(category, 0)
            task_id = self._next_id
            self._next_id += 1

        handle: TaskHandle[T] = TaskHandle(
            task_id=task_id, category=category, generation=generation
        )

        def _runner() -> None:
            handle.status = TaskStatus.RUNNING
            valor: Any = None
            error: BaseException | None = None
            cancelado = False
            try:
                valor = func(handle.token, *args, **kwargs)
                handle.token.raise_if_cancelled()
            except TaskCancelled:
                cancelado = True
            except BaseException as exc:  # noqa: BLE001 - el worker nunca rompe la app
                error = exc
                # Traza al stderr del proceso, no a la UI: un modal con una
                # traza de 40 líneas no ayuda a nadie.
                traceback.print_exc()
            finally:
                with self._lock:
                    self._running.pop(task_id, None)
                obsoleto = not self.is_current(handle)
                handle.result = valor
                handle.error = error
                if cancelado:
                    handle.status = TaskStatus.CANCELLED
                elif obsoleto:
                    # Terminó bien, pero otra ejecución de su categoría ya es
                    # la vigente: se limpia pero no se publica.
                    handle.status = TaskStatus.OBSOLETE
                elif error is not None:
                    handle.status = TaskStatus.FAILED
                else:
                    handle.status = TaskStatus.DONE
                with self._lock:
                    self._results.append(
                        TaskResult(
                            handle=handle,
                            value=valor,
                            error=error,
                            cancelled=cancelado,
                            obsolete=obsoleto,
                        )
                    )

        thread = threading.Thread(
            target=_runner, name=f"task-{category}-{task_id}", daemon=True
        )
        handle.thread = thread
        with self._lock:
            self._running[task_id] = handle
        thread.start()
        return handle

    # -- publicación ---------------------------------------------------------

    def drain(self, category: str = "") -> list[TaskResult]:
        """Vacía y devuelve la cola de resultados (se llama al hilo principal).

        Args:
            category: si se indica, devuelve **sólo** los resultados de esa
                categoría y deja el resto en la cola. Cada consumidor drena
                lo suyo, de modo que un mismo gestor puede alimentar varias
                partes de la interfaz sin que una se lleve el resultado de la
                otra.

        Los resultados obsoletos y cancelados **no** se entregan como valores
        utilizables: vienen marcados, y el llamante decide. Un resultado
        obsoleto sí aparece en la lista (para que la UI pueda limpiar), pero
        con ``obsolete=True`` y sin valor publicable.
        """
        with self._lock:
            if not category:
                lote = list(self._results)
                self._results.clear()
            else:
                lote = []
                quedan: deque[TaskResult] = deque()
                for resultado in self._results:
                    if resultado.handle.category == category:
                        lote.append(resultado)
                    else:
                        quedan.append(resultado)
                self._results = quedan
        fallidos = [r for r in lote if r.error is not None and not r.obsolete]
        if self._on_error is not None:
            for resultado in fallidos:
                try:
                    self._on_error(resultado)
                except Exception:  # noqa: BLE001 - un callback roto no rompe el drain
                    pass
        return lote

    def pending(self) -> int:
        """Resultados aún no drenados."""
        with self._lock:
            return len(self._results)

    # -- cancelación ---------------------------------------------------------

    def cancel(self, handle: TaskHandle, reason: str = "") -> bool:
        """Cancela una tarea concreta. True si estaba viva."""
        handle.cancel(reason)
        return not handle.done

    def cancel_category(self, category: str, reason: str = "") -> int:
        """Cancela todas las tareas vivas de `category`. Devuelve cuántas."""
        victims = self.active_for(category)
        for h in victims:
            h.cancel(reason)
        return len(victims)

    def cancel_all(self, reason: str = "") -> int:
        """Cancela todas las tareas vivas."""
        with self._lock:
            victims = list(self._running.values())
        for h in victims:
            h.cancel(reason)
        return len(victims)

    # -- límites -------------------------------------------------------------

    def at_capacity(self, category: str = "") -> bool:
        """True si no cabe otra tarea (global o de `category`).

        No es un bloqueo: quien llama decide qué hacer (normalmente, avisar
        al usuario en un modal en vez de crear un hilo más). Con ``category``
        vacío sólo comprueba el tope global.
        """
        with self._lock:
            if len(self._running) >= self._max_global:
                return True
            if category:
                return (
                    sum(1 for h in self._running.values() if h.category == category)
                    >= self._max_per_category
                )
        return False

    def counts(self) -> tuple[int, int]:
        """(tareas corriendo, tope global). Para decidir antes de lanzar."""
        with self._lock:
            return len(self._running), self._max_global

    # -- cierre --------------------------------------------------------------

    def shutdown(
        self, *, join_timeout: float = 2.0, reason: str = "la app se está cerrando"
    ) -> int:
        """Cierre acotado (TASK-004).

        1. Deja de admitir tareas nuevas.
        2. Cancela las vivas.
        3. Espera **como mucho** ``join_timeout`` en total.
        4. Devuelve cuántas quedaron sin terminar (deben ser cero; si no, el
           proceso se cierra igual porque los hilos son daemon).

        Es idempotente: llamarla dos veces no hace daño.
        """
        with self._lock:
            if self._closing:
                vivos = list(self._running.values())
            else:
                self._closing = True
                vivos = list(self._running.values())

        for h in vivos:
            h.cancel(reason)

        # Espera con plazo global, no por tarea: 20 tareas de 2 s no pueden
        # costar 40 s de cierre.
        fin = time.monotonic() + max(0.0, join_timeout)
        for h in vivos:
            restante = fin - time.monotonic()
            if restante <= 0:
                break
            h.join(restante)

        with self._lock:
            pendientes = [h for h in self._running.values() if not h.done]
            self._running.clear()
        return len(pendientes)

    # -- utilidades ----------------------------------------------------------

    def clear_results(self) -> None:
        """Descarta la cola sin entregarla (para tests)."""
        with self._lock:
            self._results.clear()

    def __enter__(self) -> "TaskManager":
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.shutdown()
        return False
