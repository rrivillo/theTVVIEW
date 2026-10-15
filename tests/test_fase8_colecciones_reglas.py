"""Fase 8 · U-04 — Constructor guiado de reglas de colección.

U-04 pide tres cosas y este archivo las fija por separado:

1. **Guiado de verdad**: el operador se elige de una lista que **el backend
  -filteró**. Si la interfaz tuviera su propia lista de operadores, algún día
   dejarían de coincidir con `ALLOWED_OPERATORS` y el constructor ofrecería
   combinaciones que `validar_regla` rechaza. Aquí no hay lista propia: la
   única fuente es `thetvview.collections.rules`.

2. **`validar_regla` en vivo**: el valor se valida antes de guardar, en el
   propio modal, y el motivo se ve sin salir de ahí.

3. **Vista previa antes de guardar**: cuántos canales coinciden con la regla
   que se está escribiendo. Sin esto el usuario pulsa «Guardar» y descubre
   que su colección tiene cero canales.

Y dos cosas que NO se pueden romper:

- **Nada se evalúa como código.** El valor del usuario es texto que se guarda
  como texto; `eval`, `exec` y compañía no aparecen por ninguna parte. Hay un
  test que mete una cadena de código malicioso y comprueba que se queda
  guardada como una cadena inerte.
- **Sin catálogo no se dice «0 canales»**: es el mismo error que U-03 ya
  corrigió en el contador de las colecciones del sistema, repetido aquí.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.collections.rules import (
    ALLOWED_OPERATORS,
    FIELD_TYPES,
    evaluar_coleccion,
    validar_regla,
)
from thetvview.collections.schema import Coleccion
from thetvview.models import Channel
from thetvview.ui import colors, rule_text
from thetvview.ui.app import App
from thetvview.ui.screens import CollectionsScreen
from thetvview.ui.widgets import ChecklistItem, ChecklistModal


def canal(nombre="Canal", grupo="Noticias", url=None, tvg_id=""):
    return Channel(name=nombre, url=url or f"http://ejemplo.test/{nombre}",
                   group=grupo, tvg_id=tvg_id)


def hoja(campo: str, operador: str, valor) -> dict:
    return {"field": campo, "operator": operador, "value": valor}


class FakeStdscr:
    def __init__(self, h: int = 24, w: int = 80) -> None:
        self.h, self.w = h, w
        self.addstr_calls: list[tuple[int, int, str, int]] = []

    def getmaxyx(self):
        return (self.h, self.w)

    def addstr(self, y, x, text, attr=0):
        room = self.w - 1 if x == 0 else self.w - x
        if room > 0 and len(text) > room:
            text = text[:room]
        self.addstr_calls.append((y, x, text, attr))

    def resize(self, h, w):
        self.h, self.w = h, w

    def erase(self):
        self.addstr_calls.clear()

    def getch(self):
        return -1

    def texto(self):
        return " ".join(t for _y, _x, t, _a in self.addstr_calls)


class BaseApp(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeStdscr(24, 80)
        tmp = Path(tempfile.mkdtemp())
        import thetvview.config as cfg

        patches = [
            mock.patch.object(cfg, "COLLECTIONS_JSON", tmp / "collections.json"),
            mock.patch.object(cfg, "PLAYLISTS_JSON", tmp / "p.json"),
            mock.patch.object(cfg, "FAVORITES_JSON", tmp / "f.json"),
            mock.patch.object(cfg, "PREFS_JSON", tmp / "prefs.json"),
            mock.patch.object(cfg, "RECENTS_JSON", tmp / "r.json"),
            mock.patch.object(cfg, "THEME_JSON", tmp / "theme.json"),
            mock.patch.object(cfg, "ensure_dirs", lambda: None),
            mock.patch.object(colors, "pair", return_value=0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = App(self.fake)
        self.manager = self.app.collections


# ---------------------------------------------------------------------------
# 1. Los operadores salen del backend, no de una copia
# ---------------------------------------------------------------------------


class TestOperadoresVienenDelBackend(unittest.TestCase):
    def test_los_operadores_de_un_texto_son_los_del_backend(self) -> None:
        self.assertEqual(
            set(rule_text.operadores_de("group")),
            ALLOWED_OPERATORS[FIELD_TYPES["group"]],
        )

    def test_un_campo_de_texto_nunca_ofrece_una_comparacion_de_cifras(self) -> None:
        """`equals` sirve para los dos tipos; `lt`/`gt` sólo para cifras.

        Es el punto del guiado: un campo de texto no puede llevar una
        comparación numérica, y eso no lo dice un error después, no se ofrece.
        """
        solo_cifras = {"lt", "gt", "le", "ge"}
        for op in rule_text.operadores_de("name"):
            with self.subTest(operador=op):
                self.assertIn(op, ALLOWED_OPERATORS["text"])
                self.assertNotIn(op, solo_cifras)

    def test_un_campo_numerico_solo_ofrece_operadores_numericos(self) -> None:
        for op in rule_text.operadores_de("recent_failure_count"):
            with self.subTest(operador=op):
                self.assertIn(op, ALLOWED_OPERATORS["number"])

    def test_cada_operador_ofertado_pasa_la_validacion_del_backend(self) -> None:
        """La lista entera, montada como regla, la acepta `validar_regla`.

        Si esto falla, el constructor está ofreciendo algo que el backend
        rechaza — y el usuario lo descubre al final, cuando ya escribió todo.
        """
        for campo in rule_text.CAMPOS:
            tipo = FIELD_TYPES[campo.clave]
            for op in rule_text.operadores_de(campo.clave):
                valor, error = rule_text.valor_de(
                    rule_text.ejemplo_de(campo.clave, op), campo.clave, op
                )
                with self.subTest(campo=campo.clave, operador=op):
                    self.assertIsNone(error, error)
                    validar_regla(hoja(campo.clave, op, valor))

    def test_todo_campo_del_constructor_existe_en_el_backend(self) -> None:
        for campo in rule_text.CAMPOS:
            with self.subTest(campo=campo.clave):
                self.assertIn(campo.clave, FIELD_TYPES)

    def test_cada_campo_explica_lo_que_mira(self) -> None:
        """Un campo sin explicación obliga a adivinarlo en el constructor."""
        for campo in rule_text.CAMPOS:
            with self.subTest(campo=campo.clave):
                self.assertTrue(campo.etiqueta.strip())
                self.assertTrue(campo.ayuda.strip())

    def test_todo_operador_del_backend_tiene_etiqueta_y_explicacion(self) -> None:
        """Si aparece un operador nuevo, tiene que traducirse antes de ofrecerse.

        Y también explicarse: una fila sin nota dice menos que una fila con
        una nota repetida cuatro veces, así que el filtro de `operadores_de`
        exige las dos.
        """
        for ops in ALLOWED_OPERATORS.values():
            for op in ops:
                with self.subTest(operador=op):
                    self.assertIn(op, rule_text.OPERADOR_ETIQUETA)
                    self.assertTrue(rule_text.OPERADOR_ETIQUETA[op].strip())
                    self.assertIn(op, rule_text.OPERADOR_AYUDA)
                    self.assertTrue(rule_text.OPERADOR_AYUDA[op].strip())

    def test_la_calidad_se_ofrece_como_estimada(self) -> None:
        """Decisión §1.1: la calidad inferida nunca se afirma como hecho."""
        calidad = next(c for c in rule_text.CAMPOS if c.clave == "quality")
        self.assertIn("estim", calidad.ayuda.lower())

    def test_un_campo_fantasma_no_revienta(self) -> None:
        self.assertEqual(rule_text.operadores_de("campo_fantasma"), [])

    def test_no_hay_campos_repetidos_en_el_constructor(self) -> None:
        claves = [c.clave for c in rule_text.CAMPOS]
        self.assertEqual(len(claves), len(set(claves)))


# ---------------------------------------------------------------------------
# 2. El valor: se convierte, y se explica cuando no se puede
# ---------------------------------------------------------------------------


class TestValorDe(unittest.TestCase):
    def test_un_numero_se_convierte(self) -> None:
        valor, error = rule_text.valor_de("3", "recent_failure_count", "gt")
        self.assertIsNone(error)
        self.assertEqual(valor, 3)

    def test_un_no_numero_se_explica_y_no_se_adivina(self) -> None:
        valor, error = rule_text.valor_de("tres", "recent_failure_count", "gt")
        self.assertIsNone(valor)
        self.assertTrue(error)
        self.assertIn("número", error.lower())

    def test_el_vacio_se_rechaza_con_motivo(self) -> None:
        valor, error = rule_text.valor_de("   ", "group", "contains")
        self.assertIsNone(valor)
        self.assertTrue(error)

    def test_una_lista_separte_por_comas(self) -> None:
        valor, error = rule_text.valor_de("available, unknown",
                                          "epg_status", "in")
        self.assertIsNone(error)
        self.assertEqual(valor, ["available", "unknown"])

    def test_es_uno_de_una_lista_vacia_se_explica(self) -> None:
        """`, ,` no es una lista: no tiene nada dentro.

        Sin explanation se guardaría como una regla que no puede coincidir
        con ningún canal y que en pantalla parecería una elección válida.
        """
        valor, error = rule_text.valor_de(", , ", "epg_status", "in")
        self.assertIsNone(valor)
        self.assertTrue(error)
        self.assertIn("comas", error.lower())

    def test_es_uno_de_una_palabra_sigue_siendo_una_lista(self) -> None:
        """Con un solo valor, `in` y `equals` dicen lo mismo — y lo que se
        guarda es una lista, que es lo que el backend espera."""
        valor, error = rule_text.valor_de("available", "epg_status", "in")
        self.assertIsNone(error)
        self.assertEqual(valor, ["available"])

    def test_una_lista_numerica_se_convierte(self) -> None:
        valor, error = rule_text.valor_de("1, 2, 3", "recent_failure_count", "in")
        self.assertIsNone(error)
        self.assertEqual(valor, [1, 2, 3])

    def test_una_fecha_iso_se_acepta(self) -> None:
        valor, error = rule_text.valor_de("2026-01-15", "last_played_at", "after")
        self.assertIsNone(error)
        self.assertEqual(valor, "2026-01-15")

    def test_una_fecha_que_no_existe_se_explica(self) -> None:
        valor, error = rule_text.valor_de("ayer", "last_played_at", "after")
        self.assertIsNone(valor)
        self.assertTrue(error)

    def test_una_fecha_imposible_se_explica(self) -> None:
        valor, error = rule_text.valor_de("2026-13-45", "last_played_at", "after")
        self.assertIsNone(valor)
        self.assertTrue(error)

    def test_los_ultimos_dias_piden_un_numero(self) -> None:
        valor, error = rule_text.valor_de("7", "last_played_at", "within_last")
        self.assertIsNone(error)
        self.assertEqual(valor, 7)
        valor, error = rule_text.valor_de("una semana", "last_played_at",
                                          "within_last")
        self.assertIsNone(valor)
        self.assertTrue(error)

    def test_un_campo_fantasma_no_convierte_nada(self) -> None:
        valor, error = rule_text.valor_de("x", "campo_fantasma", "contains")
        self.assertIsNone(valor)
        self.assertTrue(error)

    def test_texto_se_guarda_exacto_sin_evaluar(self) -> None:
        """El valor es **dato**, nunca código.

        `str` es lo único que se guarda, y `validar_regla` lo acepta como
        texto. Si algún día alguien metiera un `eval` aquí, esta regla dejaría
        de ser cierta y el filtro podría ejecutar lo que el usuario escribió.
        """
        peligroso = "__import__('os').system('touch /tmp/the-tv-view-pwned')"
        valor, error = rule_text.valor_de(peligroso, "group", "contains")
        self.assertIsNone(error)
        self.assertEqual(valor, peligroso)
        validar_regla(hoja("group", "contains", valor))
        self.assertFalse(Path("/tmp/the-tv-view-pwned").exists())

    def test_los_ejemplos_de_ayuda_tambien_son_validos(self) -> None:
        """Lo que la ayuda sugiere tiene que pasar la conversión.

        Si el ejemplo no pasa, el constructor empuja al usuario hacia un error
        que él no puede haber provocado.
        """
        for campo in rule_text.CAMPOS:
            for op in rule_text.operadores_de(campo.clave):
                valor, error = rule_text.valor_de(
                    rule_text.ejemplo_de(campo.clave, op), campo.clave, op
                )
                with self.subTest(campo=campo.clave, operador=op):
                    self.assertIsNone(error, error)
                    self.assertIsNotNone(valor)


# ---------------------------------------------------------------------------
# 3. Montar la regla
# ---------------------------------------------------------------------------


class TestReglaDesde(unittest.TestCase):
    a = hoja("group", "contains", "Deportes")
    b = hoja("epg_status", "equals", "available")

    def test_una_sola_condicion_es_la_hoja_sola(self) -> None:
        """Envolver una hoja en `{"all": [hoja]}` sería ruido que el backend
        tiene que Translated y el usuario no pidió."""
        self.assertEqual(rule_text.regla_desde([self.a], "all", False), self.a)

    def test_varias_con_todas(self) -> None:
        regla = rule_text.regla_desde([self.a, self.b], "all", False)
        self.assertEqual(regla, {"all": [self.a, self.b]})

    def test_varias_con_alguna(self) -> None:
        regla = rule_text.regla_desde([self.a, self.b], "any", False)
        self.assertEqual(regla, {"any": [self.a, self.b]})

    def test_negar_envuelve_y_es_la_forma_correcta(self) -> None:
        """`{"not": [nodo]}` es inválido; la buena forma es `{"not": nodo}`."""
        regla = rule_text.regla_desde([self.a], "all", True)
        self.assertEqual(regla, {"not": self.a})

    def test_negar_tambien_envuelve_un_compuesto(self) -> None:
        regla = rule_text.regla_desde([self.a, self.b], "any", True)
        self.assertEqual(regla, {"not": {"any": [self.a, self.b]}})

    def test_sin_condiciones_no_se_inventa_una_regla(self) -> None:
        self.assertIsNone(rule_text.regla_desde([], "all", False))

    def test_todo_lo_que_monta_el_constructor_lo_acepta_el_backend(self) -> None:
        for modo in ("all", "any"):
            for negar in (False, True):
                for nodos in ([self.a], [self.a, self.b]):
                    regla = rule_text.regla_desde(nodos, modo, negar)
                    with self.subTest(modo=modo, negar=negar, n=len(nodos)):
                        validar_regla(regla)  # no debe lanzar ReglaInvalida

    def test_el_modo_ignorado_cae_en_todas(self) -> None:
        self.assertEqual(
            rule_text.regla_desde([self.a, self.b], "modo_imaginario", False),
            {"all": [self.a, self.b]},
        )


# ---------------------------------------------------------------------------
# 4. Leerla en Castellano
# ---------------------------------------------------------------------------


class TestFraseDe(unittest.TestCase):
    a = hoja("group", "contains", "Deportes")
    b = hoja("epg_status", "equals", "available")

    def test_una_hoja_se_lee_como_una_frase(self) -> None:
        frase = rule_text.frase_de(self.a)
        self.assertIn("Grupo", frase)
        self.assertIn("contiene", frase)
        self.assertIn("Deportes", frase)

    def test_los_dias_se_dicen_en_dias(self) -> None:
        frase = rule_text.frase_de(hoja("last_played_at", "within_last", 7))
        self.assertIn("7", frase)
        self.assertIn("día", frase.lower())

    def test_todas_las_condiciones_dicen_y(self) -> None:
        frase = rule_text.frase_de({"all": [self.a, self.b]})
        self.assertIn(" y ", frase)
        self.assertIn("Deportes", frase)
        self.assertIn("available", frase)

    def test_algunas_dicen_o(self) -> None:
        frase = rule_text.frase_de({"any": [self.a, self.b]})
        self.assertIn(" o ", frase)

    def test_negar_lo_dice_explicitamente(self) -> None:
        frase = rule_text.frase_de({"not": self.a})
        self.assertTrue(any(m in frase.lower() for m in ("no", "menos", "excepto")))

    def test_una_hoja_desconocida_no_revienta(self) -> None:
        frase = rule_text.frase_de({"field": "x", "operator": "y", "value": 1})
        self.assertTrue(frase.strip())

    def test_el_resumen_numera_las_condiciones(self) -> None:
        resumen = rule_text.resumen_de({"all": [self.a, self.b]})
        self.assertIn("1.", resumen)
        self.assertIn("2.", resumen)

    def test_el_resumen_no_numera_nada_con_una_sola(self) -> None:
        self.assertNotIn("1.", rule_text.resumen_de(self.a))


# ---------------------------------------------------------------------------
# 5. El checklist en modo «una de estas»
# ---------------------------------------------------------------------------


class TestChecklistEnModoRadio(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.object(colors, "pair", return_value=0)
        patch.start()
        self.addCleanup(patch.stop)
        self.fake = FakeStdscr()
        self.items = [
            ChecklistItem("name", "Nombre del canal", note="el texto de la lista"),
            ChecklistItem("group", "Grupo", note="la carpeta"),
        ]

    def radio(self):
        return ChecklistModal("Elige", self.items, radio=True)

    def test_enter_elige_y_no_marca(self) -> None:
        modal = self.radio()
        self.assertEqual(modal.handle_key(10), ("pick", 0))

    def test_el_flecha_abajo_mueve_y_elige_el_siguiente(self) -> None:
        modal = self.radio()
        import curses

        modal.handle_key(curses.KEY_DOWN)
        self.assertEqual(modal.handle_key(10), ("pick", 1))

    def test_en_radio_no_hay_casillas_dibujadas(self) -> None:
        modal = self.radio()
        modal.render(self.fake)
        self.assertNotIn("[✓]", self.fake.texto())
        self.assertNotIn("[ ]", self.fake.texto())

    def test_en_radio_el_pie_dice_elegir(self) -> None:
        from thetvview.ui.widgets import radio_actions

        modal = ChecklistModal("Elige", self.items, radio=True,
                               actions=radio_actions())
        modal.render(self.fake)
        self.assertIn("Elegir", self.fake.texto())
        self.assertNotIn("Marcar", self.fake.texto())

    def test_el_constructor_usa_radio_para_no_ofrecer_una_casilla(self) -> None:
        modal = self.app_modal_items()
        self.assertTrue(all(m.radio for m in modal))

    def app_modal_items(self):
        from thetvview.ui.widgets import radio_actions

        return [
            ChecklistModal("Campos", rule_text.items_de_campo(), radio=True,
                           actions=radio_actions()),
            ChecklistModal("Operadores",
                           rule_text.items_de_operador("group"), radio=True,
                           actions=radio_actions()),
        ]

    def test_los_items_de_campo_llevan_alguna_explicacion(self) -> None:
        """Todos la llevan: los elegibles dicen *qué miran* y los que no,
        *por qué no se pueden usar* (U-04b)."""
        items = rule_text.items_de_campo()
        self.assertTrue(items)
        for item in items:
            with self.subTest(campo=item.key):
                self.assertTrue(item.note)

    def test_los_items_de_operador_dependen_del_campo(self) -> None:
        texto = [i.key for i in rule_text.items_de_operador("group")]
        self.assertIn("contains", texto)
        self.assertNotIn("gt", texto)

    def test_cada_operador_explica_algo_distinto(self) -> None:
        """Cuatro filas con la misma nota al lado son ruido con forma de dato."""
        notas = [i.note for i in rule_text.items_de_operador("group")]
        self.assertEqual(len(notas), len(set(notas)))

    def test_cada_campo_dice_su_nombre_y_no_su_clave(self) -> None:
        """`group` es la clave del backend; el usuario lee «Grupo o categoría»."""
        self.assertEqual(rule_text.etiqueta_de_campo("group"),
                         "Grupo o categoría")
        # Una clave que no existe se devuelve tal cual, para que el aviso sea
        # rastreable en vez de quedarse en blanco.
        self.assertEqual(rule_text.etiqueta_de_campo("no_existe"), "no_existe")

    def test_los_pasos_no_muestran_la_clave_del_backend(self) -> None:
        """Nada de lo que se escribe en un modal enseña identificadores."""
        juntos = "|".join(
            [i.label + " " + i.note for i in rule_text.items_de_campo()]
            + [i.label + " " + i.note
               for i in rule_text.items_de_operador("group")]
            + [i.label + " " + i.note
               for i in rule_text.items_de_operador("last_played_at")]
        )
        for clave in ("group", "epg_status", "last_played_at",
                      "recent_failure_count", "playback_health"):
            with self.subTest(clave=clave):
                self.assertNotIn(clave, juntos)

    def test_los_items_de_operador_de_un_campo_fantasma_estan_vacios(self) -> None:
        self.assertEqual(rule_text.items_de_operador("campo_fantasma"), [])


# ---------------------------------------------------------------------------
# 6. El asistente, paso a paso
# ---------------------------------------------------------------------------


class TestAsistenteDeRegla(BaseApp):
    def _campos(self, *elegidos):
        """Responde el asistente paso a paso sin terminal."""
        seq = list(elegidos)

        def _elegir_uno(titulo, items, **kw):
            clave = seq.pop(0)
            self.assertIn(clave, [i.key for i in items], items)
            return clave

        return seq, _elegir_uno

    def test_una_condicion_completa_devuelve_la_hoja(self) -> None:
        # [campo, operador, «siguiente paso»]
        _seq, _elegir = self._campos("group", "contains", "fin")
        with mock.patch.object(self.app, "_elegir_uno", _elegir), \
             mock.patch.object(self.app, "_pedir_valor",
                               return_value=("valor", "Deportes")):
            regla = self.app.build_collection_rule()
        self.assertEqual(regla, hoja("group", "contains", "Deportes"))

    def test_dos_condiciones_con_y_producen_all(self) -> None:
        _seq, _elegir = self._campos("group", "contains", "all",
                                     "epg_status", "equals", "fin")
        with mock.patch.object(self.app, "_elegir_uno", _elegir), \
             mock.patch.object(
                 self.app, "_pedir_valor",
                 side_effect=[("valor", "Deportes"), ("valor", "available")]):
            regla = self.app.build_collection_rule()
        validar_regla(regla)
        self.assertEqual(regla["all"][0]["field"], "group")
        self.assertEqual(regla["all"][1]["field"], "epg_status")

    def test_dos_condiciones_con_o_producen_any(self) -> None:
        _seq, _elegir = self._campos("group", "contains", "any",
                                     "favorites", "equals", "fin")
        with mock.patch.object(self.app, "_elegir_uno", _elegir), \
             mock.patch.object(
                 self.app, "_pedir_valor",
                 side_effect=[("valor", "Deportes"), ("valor", "true")]):
            regla = self.app.build_collection_rule()
        self.assertIn("any", regla)

    def test_terminar_negando_produce_un_not(self) -> None:
        _seq, _elegir = self._campos("group", "contains", "not")
        with mock.patch.object(self.app, "_elegir_uno", _elegir), \
             mock.patch.object(self.app, "_pedir_valor",
                               return_value=("valor", "Deportes")):
            regla = self.app.build_collection_rule()
        self.assertEqual(regla, {"not": hoja("group", "contains", "Deportes")})

    def test_abandonar_en_cualquier_paso_no_devuelve_nada(self) -> None:
        """Cancelar en el campo, en el operador o en el valor: nada cambia.

        El asistente se cierra con Esc en cualquiera de los tres y devuelve
        `None`, que `edit_collection_rule` lee como «no toques nada». Un `None`
        de cancelación colado como valor sería una condición escrita a medias
        y guardada sin querer.
        """
        casos = {
            "en el campo": [None],
            "en el operador": ["group", None],
            "en el valor": ["group", "contains"],
        }
        for nombre, respuestas in casos.items():
            with self.subTest(paso=nombre):
                pendientes = list(respuestas)

                def _eligir(*_a, **_k):
                    return pendientes.pop(0) if pendientes else None

                with mock.patch.object(self.app, "_elegir_uno", _eligir), \
                     mock.patch.object(self.app, "_pedir_valor",
                                       return_value=("valor", "Deportes")):
                    self.assertIsNone(self.app.build_collection_rule())

    def test_cancelar_el_valor_no_guarda_una_condicion_a_medias(self) -> None:
        """Si el usuario se arrepiente del valor, la hoja no se arrastra al
        siguiente paso: sería una condición que nunca se eligió del todo."""
        _seq, _elegir = self._campos("group", "contains", "fin")
        with mock.patch.object(self.app, "_elegir_uno", _elegir), \
             mock.patch.object(self.app, "_pedir_valor",
                               return_value=("cancelar", None)):
            self.assertIsNone(self.app.build_collection_rule())


class TestValidarElValorEnVivo(BaseApp):
    def _validar(self, campo, operador):
        return self.app._validar_valor_de_regla(campo, operador)

    def test_un_valor_bueno_no_da_error(self) -> None:
        validar = self._validar("group", "contains")
        self.assertIsNone(validar({"valor": "Deportes"}))

    def test_un_valor_malo_da_el_motivo_del_backend(self) -> None:
        validar = self._validar("recent_failure_count", "gt")
        error = validar({"valor": "muchos"})
        self.assertTrue(error)
        self.assertIn("número", error.lower())

    def test_una_combinacion_imposible_da_el_motivo_del_backend(self) -> None:
        """aunque la lista guided no lo ofrezca, si llegara, lo avisa."""
        validar = self._validar("group", "gt")
        error = validar({"valor": "3"})
        self.assertTrue(error)

    def test_el_error_nunca_incluye_el_campo_de_ejemplo_del_usuario(self) -> None:
        """El mensaje habla del campo, no de lo que el usuario escribió.

        Un campo `group` puede contener texto que parece una credencial si la
        lista de origenmintió; el mensaje de error no debe convertirlo en
        algo que se pantalla.
        """
        validar = self._validar("source_id", "gt")
        error = validar({"valor": "usuario:clave@ejemplo.test"})
        self.assertTrue(error)
        self.assertNotIn("clave@", error)


class TestContarLaRegla(BaseApp):
    def catalogo(self, *canales):
        from thetvview.models import Playlist

        self.app.playlist_cache["t"] = Playlist(
            name="T", channels=list(canales) or [
                canal("La 1", "General"),
                canal("Dep 1", "Deportes"),
                canal("Dep 2", "Deportes"),
            ])
        return self.app.playlist_cache["t"].channels

    def test_cuenta_los_canales_que_cumplen_la_regla(self) -> None:
        self.catalogo()
        regla = hoja("group", "contains", "Deportes")
        self.assertEqual(self.app._cuenta_de_regla(regla), 2)

    def test_sin_catalogo_no_dice_cero(self) -> None:
        """«0 canales» sin listas cargadas sería mentira: no se ha mirado nada."""
        self.app.playlist_cache.clear()
        self.assertIsNone(self.app._cuenta_de_regla(hoja("group", "contains", "X")))

    def test_una_regla_rota_no_revienta_el_conteo(self) -> None:
        self.catalogo()
        self.assertIsNone(self.app._cuenta_de_regla({"campo": "raro"}))

    def test_un_campo_sin_origen_de_datos_no_se_cuenta(self) -> None:
        """Historial de fallos y salud no los escribe nada desde la UI.

        `valor_campo` devolvería «0» y «desconocido» para *todos* los canales,
        y una regla sobre ellos contaría cero siempre: el cero parecería real
        y no lo sería. U-04b los marcó como no utilizables en el constructor.
        """
        self.catalogo()
        self.assertIsNone(self.app._cuenta_de_regla(
            hoja("recent_failure_count", "gt", 5)))
        self.assertIsNone(self.app._cuenta_de_regla(
            hoja("playback_health", "equals", "online")))

    def test_los_campos_del_catálogo_sí_se_cuentan(self) -> None:
        self.catalogo()
        self.assertIsNotNone(self.app._cuenta_de_regla(
            hoja("group", "contains", "Deportes")))
        self.assertIsNotNone(self.app._cuenta_de_regla(
            hoja("name", "contains", "La")))

    def test_una_condicion_que_sí_se_cuenta_no_ensucia_el_mensaje(self) -> None:
        """Con una parte contable y otra que no, el mensaje explica cuál."""
        self.catalogo()
        regla = {"all": [hoja("group", "contains", "Deportes"),
                         hoja("playback_health", "equals", "online")]}
        _titulo, mensaje = self.app._mensaje_de_vista_previa(regla)
        self.assertIn("Salud al abrirse", mensaje)
        self.assertNotIn("canales de tus listas", mensaje)

    def test_el_mensaje_dice_que_campo_impide_contar(self) -> None:
        self.catalogo()
        _titulo, mensaje = self.app._mensaje_de_vista_previa(
            hoja("recent_failure_count", "gt", 5))
        self.assertIn("Fallos recientes", mensaje)
        self.assertNotIn("0 canal", mensaje)

    def test_la_cabecera_no_se_contradice_con_un_not(self) -> None:
        """«Se incluirá el canal si: NO entra...» dice las dos cosas."""
        self.catalogo()
        _titulo, mensaje = self.app._mensaje_de_vista_previa(
            {"not": hoja("group", "contains", "Deportes")})
        cabecera = mensaje.splitlines()[0]
        self.assertIn("NO", cabecera)
        self.assertNotIn("Se incluirá", cabecera)

    def test_lo_que_cuenta_es_evaluar_coleccion_y_no_otra_cosa(self) -> None:
        """La vista previa tiene que ser **la** evaluación que Reggie el resto,
        no una cuenta propia que podía divergir."""
        self.catalogo()
        regla = {"all": [hoja("group", "contains", "Deportes")]}
        esperado = evaluar_coleccion(
            Coleccion(id="x", name="x", type="smart", rules=regla),
            self.app.playlist_cache["t"].channels,
        )
        self.assertEqual(len(esperado), self.app._cuenta_de_regla(regla))


# ---------------------------------------------------------------------------
# 7. La tecla R
# ---------------------------------------------------------------------------


class TestTeclaR(BaseApp):
    def pantalla(self, catalogo=None) -> CollectionsScreen:
        return CollectionsScreen(self.app, catalogo or [canal()])

    def test_r_pide_la_regla(self) -> None:
        col = self.manager.crear("Deportes", tipo="manual").id
        pantalla = self.pantalla()
        self.assertEqual(pantalla.handle_key(ord("R")),
                         {"action": "collection_rule", "cid": col})

    def test_r_esta_declarada_y_anunciada(self) -> None:
        self.manager.crear("Deportes", tipo="manual")
        pantalla = self.pantalla()
        etiqueta = next(a for a in pantalla.actions() if a.key == "R")
        self.assertEqual(etiqueta.label, "Regla")
        from tests.test_actions_por_pantalla import shortcut_keys

        self.assertIn("R", shortcut_keys(pantalla.shortcuts()))

    def test_una_vista_derivada_no_anuncia_regla(self) -> None:
        self.manager.crear("Favoritos", tipo="system")
        pantalla = self.pantalla()
        self.assertFalse(next(a for a in pantalla.actions() if a.key == "R").enabled)

    def test_pulsar_r_en_una_vista_derivada_explica_por_que(self) -> None:
        self.manager.crear("Favoritos", tipo="system")
        pantalla = self.pantalla()
        avisos: list[str] = []
        with mock.patch.object(self.app, "notify_warning", avisos.append):
            self.assertIsNone(pantalla.handle_key(ord("R")))
        self.assertTrue(avisos)

    def test_sin_colecciones_r_no_inventa_trabajo(self) -> None:
        from tests.test_actions_por_pantalla import shortcut_keys

        pantalla = self.pantalla()
        self.assertNotIn("R", {a.key for a in pantalla.actions()})
        self.assertNotIn("R", shortcut_keys(pantalla.shortcuts()))

    def test_r_y_r_minuscula_no_se_confunden(self) -> None:
        """`r` es «salir de la lista» en otras pantallas; aquí `R` es la regla."""
        self.manager.crear("Deportes", tipo="manual")
        pantalla = self.pantalla()
        self.assertEqual(pantalla.handle_key(ord("R"))["action"],
                         "collection_rule")


class TestEditarReglaDeUnaColeccion(BaseApp):
    def _wizard(self, regla):
        return mock.patch.object(self.app, "build_collection_rule",
                                 return_value=regla)

    def _confirmar(self, *args, **kw):
        return mock.patch.object(self.app, "_confirm_preview",
                                 return_value=True)

    def test_darle_regla_convierte_la_coleccion_en_inteligente(self) -> None:
        col = self.manager.crear("Fútbol", tipo="manual").id
        regla = hoja("group", "contains", "Deportes")
        with self._wizard(regla), self._confirmar():
            self.app.edit_collection_rule(col)
        guardada = self.manager.obtener(col)
        self.assertEqual(guardada.type, "smart")
        self.assertEqual(guardada.rules, regla)

    def test_una_coleccion_vacia_no_pide_perdon_antes_de_convertirla(self) -> None:
        """Sin canales elegidos a mano no hay nada que perder: preguntar sería
        ruido en el camino normal."""
        col = self.manager.crear("Vacía", tipo="manual").id
        with self._wizard(hoja("group", "contains", "x")), \
             mock.patch.object(self.app, "_confirm") as confirmar, \
             self._confirmar():
            self.app.edit_collection_rule(col)
        self.assertEqual(confirmar.call_count, 0)

    def test_con_canales_el_aviso_dice_cuantos_dejan_de_contar(self) -> None:
        c = canal("Dep 1", "Deportes")
        col = self.manager.crear("Fútbol", tipo="manual",
                                 members=[self.manager.identidad(c)]).id
        with self._wizard(hoja("group", "contains", "x")), \
             mock.patch.object(self.app, "_confirm", return_value=True) as conf, \
             self._confirmar():
            self.app.edit_collection_rule(col)
        (titulo, mensaje), _ = conf.call_args
        self.assertIn("1", mensaje)

    def test_cancelar_el_aviso_no_convierte_nada(self) -> None:
        c = canal("Dep 1", "Deportes")
        col = self.manager.crear("Fútbol", tipo="manual",
                                 members=[self.manager.identidad(c)]).id
        with self._wizard(hoja("group", "contains", "x")), \
             mock.patch.object(self.app, "_confirm", return_value=False):
            self.app.edit_collection_rule(col)
        self.assertEqual(self.manager.obtener(col).type, "manual")

    def test_cancelar_el_asistente_no_toca_la_coleccion(self) -> None:
        col = self.manager.crear("Fútbol", tipo="manual").id
        with self._wizard(None):
            self.app.edit_collection_rule(col)
        self.assertEqual(self.manager.obtener(col).type, "manual")
        self.assertIsNone(self.manager.obtener(col).rules)

    def test_cancelar_la_vista_previa_no_guarda(self) -> None:
        col = self.manager.crear("Fútbol", tipo="manual").id
        with self._wizard(hoja("group", "contains", "x")), \
             mock.patch.object(self.app, "_confirm_preview", return_value=False):
            self.app.edit_collection_rule(col)
        self.assertIsNone(self.manager.obtener(col).rules)

    def test_una_coleccion_inexistente_no_revienta(self) -> None:
        with self._wizard(None):
            self.app.edit_collection_rule("col-fantasma")

    def test_una_vista_del_sistema_no_se_convierte(self) -> None:
        col = self.manager.crear("Favoritos", tipo="system").id
        avisos: list[str] = []
        with mock.patch.object(self.app, "notify_warning", avisos.append):
            self.app.edit_collection_rule(col)
        self.assertEqual(self.manager.obtener(col).type, "system")
        self.assertTrue(avisos)

    def test_rechazar_la_regla_muestra_el_motivo_del_backend(self) -> None:
        """Una regla que el backend rechaza no llega ni a la vista previa.

        Enseñar «0 canales coinciden» de una regla que ni siquiera es válida
        sería un número inventado, y es justo el número que el usuario está
        mirando para decidir.
        """
        col = self.manager.crear("Fútbol", tipo="manual").id
        with self._wizard({"all": []}):
            with mock.patch.object(self.app, "show_error") as err:
                self.app.edit_collection_rule(col)
        (ui_err,), _kw = err.call_args
        self.assertTrue(ui_err.cuerpo().strip())
        self.assertEqual(self.manager.obtener(col).type, "manual")

    def test_una_coleccion_inteligente_cambia_de_regla(self) -> None:
        col = self.manager.crear("Fútbol", tipo="smart",
                                 rules=hoja("group", "contains", "Viejos")).id
        nueva = hoja("group", "contains", "Deportes")
        with self._wizard(nueva), self._confirmar():
            self.app.edit_collection_rule(col)
        self.assertEqual(self.manager.obtener(col).rules, nueva)

    def test_los_canales_elegidos_a_mano_no_se_borran_del_archivo(self) -> None:
        """Convierten a inteligente y dejan de contar, pero **no se pierden**:
        la migración inversa es otro ticket, no un funeral."""
        c = canal("Dep 1", "Deportes")
        clave = self.manager.identidad(c)
        col = self.manager.crear("Fútbol", tipo="manual",
                                 members=[clave]).id
        with self._wizard(hoja("group", "contains", "Deportes")), \
             mock.patch.object(self.app, "_confirm", return_value=True), \
             self._confirmar():
            self.app.edit_collection_rule(col)
        self.assertIn(clave, self.manager.obtener(col).members)


class TestCamposSinContexto(unittest.TestCase):
    """`campos_sin_contexto` camina la regla entera, compuesto a compuesto."""

    def test_una_hoja_sin_contexto_se_nombre(self) -> None:
        self.assertEqual(
            rule_text.campos_sin_contexto(hoja("playback_health", "equals", "x")),
            ["Salud al abrirse"],
        )

    def test_los_nombres_son_de_lector_no_de_codigo(self) -> None:
        """«playback_health» no le dice nada a nadie; «Salud al abrirse» sí."""
        for campo in sorted(rule_text.CAMPOS_SIN_ORIGEN):
            with self.subTest(campo=campo):
                self.assertEqual(rule_text.campos_sin_contexto(
                    hoja(campo, "equals", "x")),
                    [rule_text.etiqueta_de_campo(campo)])

    def test_catchup_y_calidad_sí_se_cuentan(self) -> None:
        """Se calculan del propio canal, sin nada más cargado."""
        self.assertEqual(
            rule_text.campos_sin_contexto(hoja("catchup_status", "equals", "x")),
            [])
        self.assertEqual(
            rule_text.campos_sin_contexto(hoja("quality", "equals", "1080")), [])

    def test_baja_dentro_de_un_all(self) -> None:
        regla = {"all": [hoja("group", "contains", "x"),
                         hoja("playback_health", "equals", "online")]}
        self.assertEqual(
            rule_text.campos_sin_contexto(regla), ["Salud al abrirse"])

    def test_baja_dentro_de_un_not(self) -> None:
        regla = {"not": {"all": [hoja("group", "contains", "x"),
                                 hoja("recent_failure_count", "gt", 5)]}}
        self.assertEqual(
            rule_text.campos_sin_contexto(regla), ["Fallos recientes"])

    def test_no_repite_un_campo_que_aparece_dos_veces(self) -> None:
        regla = {"all": [hoja("recent_failure_count", "gt", 1),
                         hoja("recent_failure_count", "gt", 5)]}
        self.assertEqual(
            rule_text.campos_sin_contexto(regla), ["Fallos recientes"])

    def test_una_regla_vacia_no_inventa_campos(self) -> None:
        self.assertEqual(rule_text.campos_sin_contexto({}), [])
        self.assertEqual(rule_text.campos_sin_contexto(None), [])


class TestLaConfirmacionDeLaVistaPrevia(BaseApp):
    def _catalogo(self):
        from thetvview.models import Playlist

        self.app.playlist_cache["t"] = Playlist(
            name="T",
            channels=[canal("Dep 1", "Deportes"), canal("La 1", "General")],
        )

    def test_el_mensaje_dice_cuantos_canales(self) -> None:
        self._catalogo()
        _titulo, mensaje = self.app._mensaje_de_vista_previa(
            hoja("group", "contains", "Deportes"))
        self.assertIn("1", mensaje)

    def test_el_mensaje_repite_la_regla_en_castellano(self) -> None:
        """Guardar sin ver la regla escrita es guardar a ciegas."""
        self._catalogo()
        _titulo, mensaje = self.app._mensaje_de_vista_previa(
            hoja("group", "contains", "Deportes"))
        self.assertIn("Deportes", mensaje)
        self.assertIn("contiene", mensaje)

    def test_sin_catalogo_lo_dice_y_no_inventa_un_cero(self) -> None:
        self.app.playlist_cache.clear()
        _titulo, mensaje = self.app._mensaje_de_vista_previa(
            hoja("group", "contains", "x"))
        self.assertNotIn("0 canal", mensaje)
        self.assertTrue(mensaje.strip())

    def test_cancelar_devuelve_que_no(self) -> None:
        self._catalogo()
        with mock.patch.object(self.app, "_run_modal", return_value="cancelar"):
            self.assertFalse(
                self.app._confirm_preview(hoja("group", "contains", "x")))

    def test_aceptar_devuelve_que_si(self) -> None:
        self._catalogo()
        with mock.patch.object(self.app, "_run_modal", return_value="guardar"):
            self.assertTrue(
                self.app._confirm_preview(hoja("group", "contains", "x")))

    def test_sin_interfaz_no_guarda_nada_por_una_sola_vez(self) -> None:
        app = App(None)
        self.assertFalse(app._confirm_preview(hoja("group", "contains", "x")))


# ---------------------------------------------------------------------------
# 8. Nada se ejecuta
# ---------------------------------------------------------------------------


class TestNadaSeEvaluaComoCodigo(BaseApp):
    def test_el_modulo_de_reglas_no_evalua_nada_como_codigo(self) -> None:
        """Se mira el **árbol**, no el texto.

        Buscar la cadena «eval» en el fuente daría un falso positivo en
        cualquier comentario que hable de eval —este docstring mismo— y un
        falso negativo si alguien se escreve el nombre con obfuscación. Lo
        que importa es si existe una *llamada*.
        """
        import ast

        arbol = ast.parse(Path(rule_text.__file__).read_text(encoding="utf-8"))
        llamados = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                func = nodo.func
                if isinstance(func, ast.Name):
                    llamados.add(func.id)
                elif isinstance(func, ast.Attribute):
                    llamados.add(func.attr)
        for prohibido in ("eval", "exec", "compile", "__import__",
                          "getattr", "globals", "locals"):
            with self.subTest(llamada=prohibido):
                self.assertNotIn(prohibido, llamados)

    def test_una_regla_con_texto_de_codigo_se_guarda_inerte(self) -> None:
        col = self.manager.crear("Fútbol", tipo="manual").id
        regla = hoja("group", "contains", "__import__('os').getcwd()")
        with mock.patch.object(self.app, "build_collection_rule",
                               return_value=regla), \
             mock.patch.object(self.app, "_confirm_preview", return_value=True):
            self.app.edit_collection_rule(col)
        self.assertEqual(self.manager.obtener(col).rules["value"],
                         "__import__('os').getcwd()")

    def test_el_error_del_gestor_no_tiene_volcar_la_excepcion(self) -> None:
        """Al usuario se le da el motivo del backend, no su volcado interno."""
        col = self.manager.crear("Fútbol", tipo="manual").id
        with mock.patch.object(self.app, "build_collection_rule",
                               return_value={"all": []}):
            with mock.patch.object(self.app, "show_error") as err:
                self.app.edit_collection_rule(col)
        (ui_err,), _kw = err.call_args
        self.assertNotIn("Traceback", ui_err.cuerpo())
        self.assertNotIn("ReglaInvalida", ui_err.cuerpo())


class TestSinInterfaz(BaseApp):
    def test_editar_regla_sin_terminal_no_hace_nada(self) -> None:
        col = self.manager.crear("Fútbol", tipo="manual").id
        app = App(None)
        app.collections = self.manager
        app.edit_collection_rule(col)
        self.assertEqual(self.manager.obtener(col).type, "manual")