"""Conjunto de datos y script de medición (RES-006).

Los datos se generan aquí, no se guardan: una lista M3U real trae
credenciales del usuario y no puede versionarse. Se generan con la misma
forma que una lista real para que el perfil de coste sea representativo.

El SDD pide medir antes de optimizar: «optimizar sólo los cuellos de botella
reproducibles». Este script genera tres listas M3U sintéticas (pequeña,
mediana y grande), mide el parseo y la escritura atómica con ``tracemalloc`` y
``time``, y deja el resultado en un formato que se pueda comparar entre
ejecuciones.

Uso::

    python -m tests.bench.medir            # mide y muestra el informe
    python -m tests.bench.medir --json     # salida JSON, para comparar
    python -m tests.bench.medir --guardar   # escribe en tests/bench/resultado.json

Qué mide
--------

- **Parseo** de M3U: tiempo y pico de memoria (``tracemalloc``).
- **Escritura atómica**: tiempo de persistir y releer un JSON del tamaño de
  la lista de canales.
- **Búsqueda**: tiempo de una búsqueda lineal sobre todos los canales, que es
  lo que hace la TUI en cada tecla.

Por qué datos sintéticos y no reales
------------------------------------

Una lista real trae credenciales del usuario y no se puede versionar. Los
datasets se generan con la misma forma que una lista real (canales con grupo,
`tvg-id` y atributos) para que el perfil de coste sea representativo, sin
depender de ningún proveedor externo ni de Internet — que es lo que exige
TEST-002.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path

from thetvview.m3u_parser import parse_text
from thetvview.storage.json_store import atomic_write_json, read_json_with_recovery

#: Tamaños de los tres perfiles. El «mediano» está por debajo de lo que se ve
#: en listas reales de IPTV gratuitas; el «grande» es del orden de un catálogo
#: grande, y por eso es el que puede enseñar un cuello de botella.
TAMANOS: dict[str, int] = {
    "pequena": 500,
    "mediana": 10_000,
    "grande": 100_000,
}

#: Dónde se deja el informe si se pide guardarlo.
RESULTADO = Path(__file__).with_name("resultado.json")


# ---------------------------------------------------------------------------
# Generación de datos
# ---------------------------------------------------------------------------

GRUPOS = (
    "Noticias", "Deportes", "Películas", "Infantiles", "Documentales",
    "Musicales", "Series", "Autorreales", "Clásicos", "Adultos",
)


def generar_m3u(canal: int) -> str:
    """Una lista M3U de `canal` entradas, con la forma de una lista real.

    Se escribe como texto (no se guarda en disco) porque el parseo es lo que
    se mide, y escribirlo añadiría ruido de disco a la medición.
    """
    lineas = ["#EXTM3U"]
    for i in range(canal):
        grupo = GRUPOS[i % len(GRUPOS)]
        lineas.append(
            f'#EXTINF:-1 tvg-id="canal.{i}.es" tvg-name="Canal {i}" '
            f'tvg-logo="http://proveedor.invalid/logo/{i}.png" group-title="{grupo}",'
            f"Canal {i} HD"
        )
        # Una URL de provider con token, como las reales: el redactado y el
        # parseo tienen que trabajar con la misma forma de dato.
        lineas.append(
            f"http://proveedor.invalid/live/usuario/{i}.ts?token=SEÑUELO{i}"
        )
    return "\n".join(lineas) + "\n"


def generar_payload_json(canal: int) -> dict:
    """Un JSON con la forma de `favorites.json` para `canal` entradas."""
    return {
        "schema_version": 1,
        "channels": [
            {
                "name": f"Canal {i}",
                "url": f"http://proveedor.invalid/live/usuario/{i}.ts",
                "group": GRUPOS[i % len(GRUPOS)],
                "tvg_id": f"canal.{i}.es",
            }
            for i in range(canal)
        ],
    }


# ---------------------------------------------------------------------------
# Medición
# ---------------------------------------------------------------------------


@dataclass
class Medida:
    """Una medición: cuánto tardó y cuánta memoria pico usó."""

    operation: str
    entradas: int
    segundos: float
    pico_mb: float

    @property
    def ms_por_1000(self) -> float:
        """Milissegundos por cada mil entradas. Comparable entre tamaños."""
        if not self.entradas:
            return 0.0
        return self.segundos * 1000.0 / (self.entradas / 1000.0)


def _medir(operation: str, entradas: int, fn) -> Medida:  # noqa: ANN001
    """Ejecuta `fn` midiendo **tiempo y memoria por separado**.

    Importa separarlos: ``tracemalloc`` multiplica el tiempo del código
    trazado por un orden de magnitud, y si se mide lo dos a la vez el tiempo
    que sale no es el que el usuario espera sino el del profiler. Aquí el
    tiempo se mide sin traza, y la traza se pone sólo para el pico de
    memoria. Por eso la columna de tiempo es la que sirve para decidir si algo
    es lento, y la de memoria sólo para saber cuánto ocupa.
    """
    # (1) Tiempo real, sin tracer encima.
    inicio = time.perf_counter()
    fn()
    transcurrido = time.perf_counter() - inicio

    # (2) Memoria, con traza (el coste va a la basura: es una medición).
    tracemalloc.start()
    try:
        fn()
        pico = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    return Medida(
        operation=operation,
        entradas=entradas,
        segundos=round(transcurrido, 4),
        pico_mb=round(pico / (1024 * 1024), 2),
    )


def medir_parseo(entradas: int) -> Medida:
    """Tiempo y memoria de parsear una M3U de `entradas` canales."""
    texto = generar_m3u(entradas)
    return _medir("parseo", entradas, lambda: parse_text(texto))


def medir_escritura(entradas: int, tmp_dir: Path) -> Medida:
    """Tiempo de escribir y releer un JSON del tamaño de la lista.

    El ciclo es idempotente a propósito: la medición se ejecuta dos veces (una
    para el tiempo, otra para la memoria) y la segunda debe medir lo mismo
    que la primera, no el caso especial de un fichero inexistente.
    """
    destino = Path(tmp_dir) / "bench.json"
    atomic_write_json(destino, {"schema_version": 1}, schema_version=None)
    payload = generar_payload_json(entradas)

    def ciclo() -> None:
        atomic_write_json(destino, payload, schema_version=None)
        read_json_with_recovery(destino, schema_version=None)

    return _medir("escritura", entradas, ciclo)


def medir_busqueda(entradas: int) -> Medida:
    """Tiempo de una búsqueda lineal, que es lo que se hace en cada tecla.

    El parseo queda **fuera** de la medición: si se midiera aquí, el número
    sería el del parseo otra vez y no diría nada sobre la búsqueda.
    """
    playlist = parse_text(generar_m3u(entradas))
    canales = list(playlist.channels)
    termino = "Canal 999999"  # no existe: fuerza recorrer toda la lista

    def buscar() -> None:
        for canal in canales:
            if termino in (canal.name or ""):
                break

    return _medir("busqueda", entradas, buscar)


def entorno() -> dict:
    """Lo necesario para saber si dos números son comparables."""
    return {
        "python": sys.version.split()[0],
        "plataforma": platform.platform(),
        "maquina": platform.machine(),
    }


def medir_todo() -> dict:
    """Todas las mediciones de los tres perfiles, con el entorno."""
    import tempfile

    medidas: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for nombre, entradas in TAMANOS.items():
            for medida in (
                medir_parseo(entradas),
                medir_busqueda(entradas),
                medir_escritura(entradas, tmp),
            ):
                medidas.append(
                    {
                        "perfil": nombre,
                        **asdict(medida),
                        "ms_por_1000": round(medida.ms_por_1000, 3),
                    }
                )
    return {"entorno": entorno(), "medidas": medidas}


# ---------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------


def formatear(datos: dict) -> str:
    """Informe legible para leer de un vistazo en una terminal."""
    ent = datos["entorno"]
    lineas = [
        "Medición de parseo y persistencia (RES-006)",
        f"  Python {ent['python']} · {ent['plataforma']}",
        "",
        f"  {'perfil':<8} {'operación':<10} {'entradas':>9} {'tiempo':>10} {'pico MB':>9} {'ms/1k':>9}",
        f"  {'-' * 8} {'-' * 10} {'-' * 9} {'-' * 10} {'-' * 9} {'-' * 9}",
    ]
    for m in datos["medidas"]:
        lineas.append(
            f"  {m['perfil']:<8} {m['operation']:<10} {m['entradas']:>9,} "
            f"{m['segundos']:>9.3f}s {m['pico_mb']:>8.2f} {m['ms_por_1000']:>8.2f}"
        )
    lineas.append("")
    lineas.append("Los datos son sintéticos: misma forma que una lista real, sin credenciales.")
    return "\n".join(lineas)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="salida JSON (para comparar entre ejecuciones)"
    )
    parser.add_argument(
        "--guardar", action="store_true", help=f"escribe el informe en {RESULTADO}"
    )
    args = parser.parse_args(argv)

    datos = medir_todo()

    if args.guardar:
        RESULTADO.write_text(
            json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(datos, ensure_ascii=False, indent=2) if args.json else formatear(datos))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
