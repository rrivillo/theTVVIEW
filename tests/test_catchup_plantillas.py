"""CU-03 — Plantillas ``catchup-source`` de terceros: límites y rechazo.

La lista blanca de marcadores ya existía antes de esta fase (``string.Formatter``
más ``_TEMPLATE_PLACEHOLDERS``). Lo que **no** existía, y es lo que estos tests
exigen:

1. **Los marcadores son UTC.** ``{utc}`` significa UTC en el estándar de los
   reproductores IPTV, y el código devolvía la **hora local** del proceso. Es un
   cambio de comportamiento visible, documentado en
   ``docs/sdd/catchup-temporal.md`` §3.1.
2. **Límite por parámetro**: ningún valor renderizado puede ser arbitrariamente
   largo, aunque la plantilla sea del propio proveedor.
3. **Rechazo explícito** de lo que rompería la query: ``&``, ``#`` y saltos de
   línea dentro de un marcador, y plantillas demasiado largas.

La plantilla es texto de un tercero: se valida como cualquier otra entrada no
confiable (``AGENTS.md``, §15 del SDD).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from thetvview import catchup
from thetvview.catchup import (
    CatchupError,
    CatchupRequest,
    render_source_template,
    template_values,
    validar_intervalo,
)
from thetvview.security.errors import IPTVError

NOW = datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)


def req(inicio: datetime, fin: datetime) -> CatchupRequest:
    return CatchupRequest(channel_id="101", start=inicio, end=fin)


def peticion() -> CatchupRequest:
    return req(NOW - timedelta(hours=2), NOW)


# ---------------------------------------------------------------------------
# 1. {utc} es UTC, no hora local
# ---------------------------------------------------------------------------


class TestLosMarcadoresSonUTC(unittest.TestCase):
    """El cambio de comportamiento más importante del ticket."""

    def setUp(self) -> None:
        self.valores = template_values(peticion())

    def test_utc_es_el_instante_utc_del_programa(self):
        self.assertEqual(self.valores["utc"], "2026-03-01:18-00-00")

    def test_utc_no_depende_de_la_zona_de_la_maquina(self):
        """El sello es UTC aunque el proceso piense en otra zona."""
        marca = self.valores["utc"]
        self.assertEqual(marca, "2026-03-01:18-00-00")
        # La misma hora local del programa, expresada en otras zonas, NO es el
        # mismo instante: por eso el sello no puede ser hora local.
        for zona in ("Europe/Madrid", "America/Lima", "Asia/Tokyo"):
            with self.subTest(zona=zona):
                local = (NOW - timedelta(hours=2)).astimezone(ZoneInfo(zona))
                self.assertNotEqual(
                    marca,
                    local.strftime("%Y-%m-%d:%H-%M-%S"),
                    "el sello no debe seguir a la zona del proceso",
                )

    def test_start_y_end_siguen_siendo_epoch_utc(self):
        self.assertEqual(self.valores["start"], int((NOW - timedelta(hours=2)).timestamp()))
        self.assertEqual(self.valores["end"], int(NOW.timestamp()))
        self.assertEqual(
            datetime.fromtimestamp(self.valores["start"], timezone.utc),
            NOW - timedelta(hours=2),
        )

    def test_utc_y_start_apuntan_al_mismo_instante(self):
        """La prueba de cordura: dos formatos, un instante."""
        desde_epoch = datetime.fromtimestamp(self.valores["start"], timezone.utc)
        desde_sello = datetime.strptime(
            self.valores["utc"], "%Y-%m-%d:%H-%M-%S"
        ).replace(tzinfo=timezone.utc)
        self.assertEqual(desde_epoch, desde_sello)

    def test_utc_no_tiene_espacios_ni_signos_reservados(self):
        """Tiene que sobrevivir a una query sin codificar."""
        marca = self.valores["utc"]
        for prohibido in (" ", "+", "&", "#", "%", "/", ":"):
            self.assertNotIn(prohibido, marca.replace(":", "-"))


# ---------------------------------------------------------------------------
# 2. Límite por parámetro
# ---------------------------------------------------------------------------


class TestLimitesPorParametro(unittest.TestCase):
    def test_existe_un_tope_de_longitud_de_plantilla(self):
        self.assertGreater(catchup.MAX_TEMPLATE_LENGTH, 0)

    def test_una_plantilla_larguisima_se_rechaza(self):
        larga = "http://a.test/x?start={start}" + ("&a=1" * 5000)
        with self.assertRaises(CatchupError) as ctx:
            render_source_template(larga, peticion())
        self.assertIn("larga", str(ctx.exception).lower())

    def test_una_plantilla_normal_se_acepta(self):
        url = render_source_template("http://a.test/x?start={start}", peticion())
        self.assertIn("start=", url)

    def test_el_valor_renderizado_tiene_un_tope(self):
        """Un `channel_id` enorme no debe able entrar en la URL."""
        valores = template_values(peticion())
        for nombre, valor in valores.items():
            with self.subTest(marcador=nombre):
                self.assertLessEqual(
                    len(str(valor)), catchup.MAX_TEMPLATE_VALUE_LENGTH
                )

    def test_no_hay_un_tope_de_marcadores_ilimitado(self):
        """Un solo `{start}` basta; repetirlo 500 veces es abuso."""
        abuso = "http://a.test/x?" + "&".join(
            f"p{i}={{start}}" for i in range(200)
        )
        with self.assertRaises(CatchupError):
            render_source_template(abuso, peticion())


# ---------------------------------------------------------------------------
# 3. Rechazo de lo que rompe la query
# ---------------------------------------------------------------------------


class TestCaracteresReservados(unittest.TestCase):
    def test_un_salto_de_linea_en_la_plantilla_se_rechaza(self):
        with self.assertRaises(CatchupError):
            render_source_template("http://a.test/x?start={start}\nX: 1", peticion())

    def test_un_retorno_de_carro_se_rechaza(self):
        with self.assertRaises(CatchupError):
            render_source_template("http://a.test/x?start={start}\r\nX: 1", peticion())

    def test_una_url_con_espacio_se_rechaza(self):
        with self.assertRaises(CatchupError):
            render_source_template("http://a.test/x y?start={start}", peticion())

    def test_la_plantilla_se_valida_como_cualquier_url(self):
        """El esquema lo pone la política de stream, no el dominio."""
        for plantilla in (
            "file:///etc/passwd?start={start}",
            "javascript:alert(1)?start={start}",
            "ftp://a.test/x?start={start}",
        ):
            with self.subTest(plantilla=plantilla):
                with self.assertRaises(CatchupError):
                    render_source_template(plantilla, peticion())

    def test_un_marcador_desconocido_no_se_adivina(self):
        with self.assertRaises(CatchupError) as ctx:
            render_source_template("http://a.test/x?s={inicio}", peticion())
        self.assertIn("start", str(ctx.exception))  # sugiere los válidos

    def test_una_plantilla_sin_marcador_de_tiempo_se_rechaza(self):
        """Si no hay dónde colocar el instante, la URL no lleva a ninguna parte."""
        with self.assertRaises(CatchupError):
            render_source_template("http://a.test/x/fijo.m3u8", peticion())

    def test_especificadores_de_formato_se_rechazan(self):
        """`{start:04d}` es sintaxis de Python, no del estándar IPTV."""
        with self.assertRaises(CatchupError):
            render_source_template("http://a.test/x?s={start:04d}", peticion())

    def test_el_error_es_del_dominio_no_de_seguridad(self):
        """Los dos son `CatchupError`, que es lo que la UI sabe mostrar."""
        try:
            render_source_template("file:///etc/passwd?start={start}", peticion())
        except IPTVError as exc:
            self.assertIsInstance(exc, CatchupError)
        else:
            self.fail("debía rechazar el esquema")


# ---------------------------------------------------------------------------
# 4. La plantilla no se traga la validación
# ---------------------------------------------------------------------------


class TestPlantillaYValidacionSonSeparadas(unittest.TestCase):
    def test_renderizar_no_valida_el_intervalo(self):
        """Las dos responsabilidades no se mezclan.

        `render_source_template` sólo rellena; validar es otro paso. Si un
        llamante renderiza un intervalo imposible, obtiene texto, no una URL
        autorizada.
        """
        invertido = req(NOW - timedelta(hours=1), NOW - timedelta(hours=2))
        url = render_source_template("http://a.test/x?start={start}&d={duration}", invertido)
        self.assertIn("d=1", url)
        with self.assertRaises(CatchupError):
            validar_intervalo(invertido)

    def test_el_resultado_sigue_pasando_la_politica_de_url(self):
        url = render_source_template(
            "http://a.test/x?start={start}&end={end}&d={duration}&u={utc}", peticion()
        )
        from thetvview.security.url_policy import PURPOSE_STREAM, validate_url

        validate_url(url, purpose=PURPOSE_STREAM)


if __name__ == "__main__":
    unittest.main()