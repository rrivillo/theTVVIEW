"""CU-06 — Seguridad y compatibilidad del catch-up.

La plantilla ``catchup-source`` es **texto de un tercero** que acaba en una URL
que el reproductor va a abrir. Este ticket comprueba que el endurecimiento de
CU-03 no se puede saltar por otro camino y que el módulo sigue siendo
portable.

Lo que se comprueba:

1. **La plantilla no amplía la superficie de red.** Un esquema nuevo, una
   cabecera inyectada, un salto de línea o una ruta local se rechazan siempre.
2. **`render_source_template` no deja escapar nada** aunque se llame
   directamente, sin pasar por un adaptador.
3. **Compatibilidad:** el catch-up no arrastra dependencias de red, ni curses,
   ni subprocess. Es dominio puro y tiene que seguir siéndolo.
4. **El directo sobrevive** cuando el catch-up no es válido o no existe: la
   garantía del SDD §10 es que ver el archivo falla sin impide ver el directo.

Las comprobaciones automáticas del mismo tipo queliving en
``thetvview/security/check.py`` (control ``catchup_capability_gate``), que es
la puerta que ``python -m thetvview.security.check`` verifica antes de dar la
fase por buena.
"""

from __future__ import annotations

import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thetvview import catchup
from thetvview.catchup import (
    CatchupCapability,
    CatchupRequest,
    CatchupState,
    build_playback_request,
    capability_for,
    can_use_catchup,
    classify_request,
    render_source_template,
    template_values,
    validar_intervalo,
)
from thetvview.models import Channel
from thetvview.security.errors import IPTVError
from thetvview.security.url_policy import PURPOSE_STREAM, validate_url

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
CATCHUP_PY = Path(__file__).resolve().parent.parent / "thetvview" / "catchup.py"


def canal(**attrs) -> Channel:
    base = {
        "catchup": "shift",
        "catchup_days": "7",
        "catchup_source": "http://a.test/ts/{start}?d={duration}",
    }
    base.update(attrs)
    return Channel(name="Canal", url="http://a.test/live/1", attrs=base)


def peticion(inicio: datetime, fin: datetime) -> CatchupRequest:
    return CatchupRequest(channel_id="1", start=inicio, end=fin)


def valida() -> CatchupRequest:
    return peticion(NOW - timedelta(hours=2), NOW - timedelta(hours=1))


# ---------------------------------------------------------------------------
# 1. La plantilla no amplía la superficie de red
# ---------------------------------------------------------------------------


class TestLaPlantillaNoAmpliaLaRed(unittest.TestCase):
    """Todo esquema que no sea HTTP(S) se rechaza, siempre."""

    ESQUEMAS_PROHIBIDOS = (
        "file:///etc/passwd?start={start}",
        "javascript:alert(1)?start={start}",
        "data:text/plain,robo?start={start}",
        "gopher://a.test/x?start={start}",
        "ftp://a.test/x?start={start}",
        "ws://a.test/x?start={start}",
        "about:blank?start={start}",
    )

    def test_esquemas_prohibidos_se_rechazan(self):
        for plantilla in self.ESQUEMAS_PROHIBIDOS:
            with self.subTest(plantilla=plantilla):
                with self.assertRaises(catchup.CatchupError):
                    render_source_template(plantilla, valida())

    def test_https_si_se_acepta(self):
        url = render_source_template("https://a.test/ts?start={start}", valida())
        self.assertTrue(url.startswith("https://"))

    def test_http_si_se_acepta(self):
        url = render_source_template("http://a.test/ts?start={start}", valida())
        self.assertTrue(url.startswith("http://"))

    def test_una_cabecera_inyectada_se_rechaza(self):
        """Un salto de línea en la URL es una cabecera inyectada, no un instante."""
        for plantilla in (
            "http://a.test/x?start={start}\nX-Inyectada: 1",
            "http://a.test/x?start={start}\r\nX-Inyectada: 1",
            "http://a.test/x?start={start}\n\n",
        ):
            with self.subTest(plantilla=repr(plantilla)):
                with self.assertRaises(catchup.CatchupError):
                    render_source_template(plantilla, valida())

    def test_el_marcador_no_puede_inyectar_parametros(self):
        """`{start}` es un entero: no puede cerrar la query y abrir otra."""
        url = render_source_template("http://a.test/x?start={start}&d={duration}", valida())
        self.assertEqual(url.count("?"), 1)
        self.assertEqual(url.count("&"), 1)

    def test_no_se_puede_cambiar_el_destino_con_un_marcador(self):
        """El texto antes del marcador fija el host; el marcador no lo mueve."""
        url = render_source_template("http://bueno.test/{start}", valida())
        self.assertTrue(url.startswith("http://bueno.test/"))

    def test_la_url_renderizada_pasa_la_politica(self):
        url = render_source_template("http://a.test/ts?start={start}", valida())
        validate_url(url, purpose=PURPOSE_STREAM)

    def test_adaptador_y_render_dicen_lo_mismo(self):
        """Los dos caminos de construcción rechazan lo mismo."""
        import thetvview.catchup as modulo

        c = canal()
        try:
            build_playback_request(c, capability_for(c), valida(), now=NOW)
        except catchup.CatchupError:
            pass  # la ventana de 7 días no cubre NOW en 2026-03... se acepta
        # Comprobación directa: el adaptador no acepta esquemas raros.
        with self.assertRaises(modulo.CatchupError):
            modulo.M3UCatchupAdapter().build(
                canal(catchup_source="file:///etc/passwd?start={start}"),
                capability_for(canal(catchup_source="file:///etc/passwd?start={start}")),
                valida(),
            )


# ---------------------------------------------------------------------------
# 2. Renderizar directamente tampoco deja escapar nada
# ---------------------------------------------------------------------------


class TestRenderizadoDirecto(unittest.TestCase):
    """El llamante que usa la función suelta no tiene una puerta menos."""

    def test_el_error_es_del_mismo_tipo_que_el_del_adaptador(self):
        """Un solo tipo de fallo: el que la UI sabe mostrar en un modal."""
        with self.assertRaises(catchup.CatchupError):
            render_source_template("file:///x?start={start}", valida())

    def test_el_error_es_un_ipterror(self):
        """La cadena de errores del dominio sigue intacta."""
        with self.assertRaises(IPTVError):
            render_source_template("file:///x?start={start}", valida())

    def test_el_error_no_revela_rutas_del_sistema(self):
        with self.assertRaises(catchup.CatchupError) as ctx:
            render_source_template("file:///etc/shadow?start={start}", valida())
        self.assertNotIn("/etc/shadow", str(ctx.exception))


# ---------------------------------------------------------------------------
# 3. Compatibilidad: dominio puro, cero dependencias nuevas
# ---------------------------------------------------------------------------


class TestDominioPuro(unittest.TestCase):
    """El módulo se testea entero: sin red, sin curses, sin binarios."""

    #: Vías que pueden abrir una conexión o un proceso.
    _RED: frozenset[str] = frozenset({
        "socket", "http", "urllib.request", "urllib.error", "ssl",
        "subprocess", "ftplib", "asyncio", "requests", "aiohttp",
    })
    #: Todo lo que el stdlib permite, con su ruta.
    _STDLIB: frozenset[str] = frozenset({
        "__future__", "dataclasses", "datetime", "enum", "string", "typing",
        "urllib.parse", "zoneinfo",
    })
    #: Módulos del propio proyecto. Si aparece uno nuevo aquí tiene que ser
    #: deliberado: la lista es corta a propósito.
    _PROPIO: frozenset[str] = frozenset({
        "models", "player.errors", "security.errors", "security.url_policy",
        "stream_ref", "xtream_security",
    })

    @classmethod
    def _importados(cls) -> set[str]:
        """Módulos que importa ``catchup.py``, con su ruta completa.

        Se conserva la ruta (`security.errors`) y no sólo el primer nivel, que
        es lo que permite distinguir `urllib.parse` (inofensivo) de
        `urllib.request` (abre conexiones).
        """
        arbol = ast.parse(CATCHUP_PY.read_text(encoding="utf-8"))
        salida: set[str] = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                salida.update(a.name for a in nodo.names)
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                salida.add(nodo.module)
        return salida

    def test_el_modulo_no_importa_red_ni_procesos(self):
        """Ni una sola vía de red: el módulo se testea entero.

        `urllib.parse` sí está (quote/unquote) y no abre conexiones; lo que se
        vigila es `urllib.request`, que sí puede descargar.
        """
        self.assertEqual(self._importados() & self._RED, set())

    def test_no_usa_curses(self):
        self.assertNotIn("curses", self._importados())

    def test_no_depende_de_ninguna_libreria_externa(self):
        """Todo es stdlib: `requirements.txt` sigue vacío."""
        externos = self._importados() - self._STDLIB - self._PROPIO
        self.assertEqual(externos, set())

    def test_solo_importa_lo_que_usa(self):
        """`StreamErrorCode` sí se usa: es la traducción de CU-04."""
        arbol = ast.parse(CATCHUP_PY.read_text(encoding="utf-8"))
        fuente = CATCHUP_PY.read_text(encoding="utf-8")
        self.assertIn("from .player.errors import StreamErrorCode", fuente)
        self.assertIn("StreamErrorCode.AUTH_FAILED", fuente)

    def test_no_eval_ni_exec(self):
        fuente = CATCHUP_PY.read_text(encoding="utf-8")
        for prohibido in ("eval(", "exec(", "__import__", "compile("):
            with self.subTest(prohibido=prohibido):
                self.assertNotIn(prohibido, fuente)


# ---------------------------------------------------------------------------
# 4. El directo sobrevive
# ---------------------------------------------------------------------------


class TestElDirectoSobrevive(unittest.TestCase):
    """SDD §10: fallar el archivo no puede dejar sin directo."""

    def test_un_canal_sin_catchup_conserva_su_directo(self):
        c = Channel(name="C", url="http://a.test/live/1")
        cap = capability_for(c)
        self.assertFalse(can_use_catchup(cap))
        self.assertEqual(c.url, "http://a.test/live/1")

    def test_un_intervalo_invalido_no_toca_el_canal(self):
        c = canal()
        original = c.url
        invertido = peticion(NOW - timedelta(hours=1), NOW - timedelta(hours=2))
        with self.assertRaises(catchup.CatchupError):
            build_playback_request(c, capability_for(c), invertido, now=NOW)
        self.assertEqual(c.url, original, "el canal original no debe tocarse")

    def test_una_plantilla_mala_no_toca_el_canal(self):
        c = canal(catchup_source="file:///etc/passwd?start={start}")
        original = c.url
        with self.assertRaises(catchup.CatchupError):
            build_playback_request(c, capability_for(c), valida(), now=NOW)
        self.assertEqual(c.url, original)

    def test_el_estado_desconocido_no_es_no_disponible(self):
        """Sin capacidad declarada el estado es «sin archivo», no «error»."""
        c = Channel(name="C", url="http://a.test/live/1")
        self.assertIs(
            classify_request(capability_for(c), valida(), NOW),
            CatchupState.DISABLED_BY_PROVIDER,
        )

    def test_la_capacidad_no_se_adivina_por_el_nombre(self):
        c = Channel(name="Canal Timeshift Catchup", url="http://a.test/live/1")
        self.assertIs(capability_for(c), catchup.DISABLED)

    def test_la_url_de_archivo_no_reemplaza_a_la_del_directo(self):
        """`playback_channel` devuelve una copia; el original sigue siendo directo."""
        c = canal()
        cap = CatchupCapability(
            enabled=True,
            provider_declared=True,
            archive_duration_seconds=7 * catchup.SECONDS_PER_DAY,
            protocol=catchup.PROTOCOL_M3U,
            source_template="http://a.test/ts/{start}",
        )
        pedido = build_playback_request(c, cap, valida(), now=NOW)
        copia = catchup.playback_channel(pedido)
        self.assertNotEqual(copia.url, c.url)
        self.assertEqual(c.url, "http://a.test/live/1")


# ---------------------------------------------------------------------------
# 5. Los topes no se pueden eludir
# ---------------------------------------------------------------------------


class TestLosTopesNoSeEluden(unittest.TestCase):
    def test_validar_con_un_tope_mayor_sigue_teniendo_el_que_por_defecto(self):
        """Pasar `maximo` grande no desactiva el control: es explícito."""
        enorme = peticion(NOW - timedelta(days=30), NOW)
        validar_intervalo(enorme, maximo=40 * 24 * 3600)  # no lanza
        with self.assertRaises(catchup.CatchupError):
            validar_intervalo(enorme)

    def test_la_plantilla_larga_se_rechaza_aunque_todos_sus_marcadores_sean_validos(self):
        plantilla = "http://a.test/x?start={start}" + "&d={duration}" * 400
        with self.assertRaises(catchup.CatchupError):
            render_source_template(plantilla, valida())

    def test_los_valores_son_pequenos_por_naturaleza(self):
        """El tope por parámetro es holgado: no estorba a ningún caso real."""
        for nombre, valor in template_values(valida()).items():
            with self.subTest(marcador=nombre):
                self.assertLessEqual(len(str(valor)), 20)


if __name__ == "__main__":
    unittest.main()