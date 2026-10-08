"""Favoritos con el programa actual del EPG (SDD Favoritos §5–§19, CA-01…CA-10).

Qué fija este archivo, y por qué importa:

- **El favorito sigue siendo un canal.** El programa se resuelve al construir
  las filas y `favorites.json` no cambia ni un byte (§4, §26, CA-09).
- **Sin guía, la lista se ve igual que antes** (§11.2, §11.6): ni segunda
  columna reservada, ni `Cargando…` inventado.
- **Una guía parcial no rompe nada**: el canal que no aparece en la guía se
  queda, con `—` (§11.4), que es un dato ausente y no un error.
- **Comparar instantes, no cadenas**: un programa con offset `-0500` frente a
  una hora local tiene que resolverse bien (§22).
- **Cero red y cero disco al pintar**: ni una petición por favorito (CA-08), y
  aquí se comprueba con `mock.patch`, no confiando en que "no lo llama".
- **La jerarquía se respeta**: en una terminal estrecha se sacrifica la
  segunda columna, nunca el nombre del canal (§15).

Sin curses: doble de ventana que captura `addstr` y **falla si se escribe
fuera de la pantalla** (el mismo de `test_empty_states`), y dobles mínimos de
`App`.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from thetvview import epg_match
from thetvview.epg_parser import Epg
from thetvview.models import Channel, Program
from thetvview.ui import colors
from thetvview.ui.favorite_rows import (
    CARGANDO,
    MIN_TWO_COLUMN_CELLS,
    SIN_INFO,
    FavoriteRow,
    RowState,
    build_rows,
    channel_label,
    layout_columns,
    program_text,
    two_columns_fit,
)
from thetvview.ui.screens import FavoritesScreen, _epg_channel_id
from thetvview.ui.textwidth import cell_width
from tests.test_empty_states import StdscrCaptura


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Favoritos:
    """Doble de `FavoritesManager`: la lista ya está en memoria."""

    def __init__(self, canales=()) -> None:
        self.canales = list(canales)
        self.escritas: list[list[Channel]] = []

    def load(self) -> list[Channel]:
        return list(self.canales)

    def is_favorite(self, channel: Channel) -> bool:
        return any(c.url == channel.url for c in self.canales)

    def favorite_urls(self) -> set[str]:
        return {c.url for c in self.canales}

    def toggle(self, channel: Channel) -> bool:
        self.escritas.append(list(self.canales))
        return True


class AppFalso:
    """Lo mínimo que `FavoritesScreen` lee de `App`.

    `ensure_epg` **no** se implementa a propósito: la pantalla no debe llamarlo,
    así que si algún día lo hiciera, el `AttributeError` lo delataría en lugar
    de devolver un doble que lo escondiera.
    """

    def __init__(self, canales=(), epg=None, max_y=24, max_x=80) -> None:
        self.favorites = _Favoritos(canales)
        self.epg = epg
        self.stdscr = type("T", (), {"getmaxyx": lambda s: (max_y, max_x)})()
        self.status = type("S", (), {"show": lambda s, m, e=False: None,
                                     "is_error": False, "message": ""})()
        self.footer = type("F", (), {"show": lambda s, m, e=False: None})()
        self.avisos: list[str] = []

    def body_height(self) -> int:
        return max(1, self.stdscr.getmaxyx()[0] - 3)

    def notify_warning(self, message: str) -> None:
        self.avisos.append(message)


def canal(nombre: str, grupo=None, tvg_id=None, radio=False) -> Channel:
    return Channel(name=nombre, url=f"http://x/{nombre}", group=grupo,
                   tvg_id=tvg_id, radio=radio)


def programa(cid: str, titulo: str, inicio: datetime, fin: datetime | None) -> Program:
    return Program(channel_id=cid, title=titulo, start=inicio, stop=fin)


#: Hora fija para todos los casos: 2026-10-07 18:37 hora local (Caso 5).
AHORA = datetime(2026, 10, 7, 18, 37, tzinfo=timezone.utc)


def epg_completo() -> Epg:
    """Los cuatro canales del §29 en emisión ahora (Casos 3 y 5)."""
    return Epg(
        channels_by_id={
            "cnn": "CNN",
            "bbc": "BBC One",
            "espn": "ESPN",
            "discovery": "Discovery",
        },
        programs={
            "cnn": [
                programa("cnn", "Noticias", AHORA - timedelta(minutes=37),
                         AHORA + timedelta(minutes=23)),
                programa("cnn", "Deportes", AHORA + timedelta(minutes=23),
                         AHORA + timedelta(minutes=83)),
            ],
            "bbc": [programa("bbc", "Breakfast", AHORA - timedelta(hours=1),
                             AHORA + timedelta(hours=1))],
            "espn": [programa("espn", "Football", AHORA, AHORA + timedelta(hours=2))],
            "discovery": [programa("discovery", "Planet Earth", AHORA,
                                   AHORA + timedelta(hours=2))],
        },
    )


CUATRO = [canal("CNN"), canal("BBC One"), canal("ESPN"), canal("Discovery")]

#: Lo que emite cada canal, para las pruebas de pantalla.
TITULOS = {
    "CNN": "Noticias",
    "BBC One": "Breakfast",
    "ESPN": "Football",
    "Discovery": "Planet Earth",
}


def epg_al_aire(titulos: dict[str, str] | None = None) -> Epg:
    """Guía con algo **en emisión ahora mismo**, para las pruebas de pantalla.

    Las pruebas de render dejan que la pantalla use la hora real (es lo que
    hace de verdad), así que la guía tiene que estar construida alrededor de
    esa hora y no de la fija de los casos del §29. La clave es el `channel_id`
    y el valor el nombre visible del canal.
    """
    ahora = datetime.now().astimezone()
    pedidos = TITULOS if titulos is None else titulos
    return Epg(
        channels_by_id={cid: cid for cid in pedidos},
        programs={
            cid: [programa(cid, titulo, ahora - timedelta(hours=1),
                           ahora + timedelta(hours=1))]
            for cid, titulo in pedidos.items()
        },
    )


def pintar(app: AppFalso, max_y: int = 24, max_x: int = 80) -> StdscrCaptura:
    """Renderiza Favoritos en una ventana capturadora y devuelve lo pintado."""
    stdscr = StdscrCaptura(max_y, max_x)
    with mock.patch.object(colors, "pair", side_effect=lambda pid: pid):
        FavoritesScreen(app).render(stdscr)
    return stdscr


# ---------------------------------------------------------------------------
# 1. La lógica pura: qué programa corresponde a cada favorito (§29)
# ---------------------------------------------------------------------------


class TestBuildRows(unittest.TestCase):
    def test_sin_guia_no_inventa_programas(self):
        """Caso 2: EPG no disponible → `SIN_EPG`, sin programa en ninguna fila."""
        filas = build_rows(CUATRO, None, AHORA)
        self.assertEqual(len(filas), 4)
        for fila in filas:
            with self.subTest(canal=fila.channel.name):
                self.assertIs(fila.state, RowState.SIN_EPG)
                self.assertIsNone(fila.program)
                self.assertIsNone(fila.next_change)

    def test_guia_completa_muestra_el_programa_actual(self):
        """Caso 3: los cuatro canales tienen algo en emisión."""
        filas = build_rows(CUATRO, epg_completo(), AHORA)
        self.assertEqual(
            [program_text(f) for f in filas],
            ["Noticias", "Breakfast", "Football", "Planet Earth"],
        )
        for fila in filas:
            with self.subTest(canal=fila.channel.name):
                self.assertIs(fila.state, RowState.AL_AIRE)

    def test_guia_parcial_no_rompe_la_lista(self):
        """Caso 4: la guía conoce 2 de 4; los otros dos siguen ahí con `—`."""
        epg = Epg(
            channels_by_id={"cnn": "CNN", "bbc": "BBC One"},
            programs={
                "cnn": [programa("cnn", "Noticias", AHORA, AHORA + timedelta(hours=1))],
                "bbc": [programa("bbc", "Breakfast", AHORA, AHORA + timedelta(hours=1))],
            },
        )
        filas = build_rows(CUATRO, epg, AHORA)
        self.assertEqual(
            [program_text(f) for f in filas],
            ["Noticias", "Breakfast", SIN_INFO, SIN_INFO],
        )
        # El favorito que la guía no reconoce **no se pierde** (§18).
        self.assertEqual([f.channel.name for f in filas],
                         ["CNN", "BBC One", "ESPN", "Discovery"])

    def test_18_37_devuelve_noticias(self):
        """Caso 5: dentro de 18:00–19:00 sale Noticias."""
        fila = build_rows([canal("CNN")], epg_completo(), AHORA)[0]
        self.assertEqual(fila.program.title, "Noticias")
        self.assertEqual(fila.next_change, AHORA + timedelta(minutes=23))

    def test_19_03_devuelve_deportes(self):
        """Caso 6: el mismo canal, tres minutos después, ya da Deportes."""
        filas = build_rows([canal("CNN")], epg_completo(),
                           AHORA + timedelta(minutes=26))
        self.assertEqual(program_text(filas[0]), "Deportes")

    def test_canal_sin_tvg_id_resuelve_por_nombre(self):
        """Caso 7: sin `tvg-id` la guía se busca por nombre normalizado."""
        canal_sin_id = canal("  cnn  ")  # espacios y mayúsculas de más
        fila = build_rows([canal_sin_id], epg_completo(), AHORA)[0]
        self.assertEqual(program_text(fila), "Noticias")

    def test_canal_en_la_guia_sin_emision_ahora(self):
        """Caso 8: el canal está en la guía, pero no emite a esta hora."""
        epg = Epg(
            channels_by_id={"cnn": "CNN"},
            programs={"cnn": [programa("cnn", "Noticias", AHORA + timedelta(hours=2),
                                       AHORA + timedelta(hours=3))]},
        )
        fila = build_rows([canal("CNN")], epg, AHORA)[0]
        self.assertIs(fila.state, RowState.SIN_EMISION)
        self.assertEqual(program_text(fila), SIN_INFO)
        # El próximo cambio sí se sabe: sirve para el refresco por tiempo.
        self.assertEqual(fila.next_change, AHORA + timedelta(hours=2))

    def test_cargando_dice_cargando(self):
        """§11.5: el canal se ve antes de que llegue la guía."""
        filas = build_rows(CUATRO, None, AHORA, epg_loading=True)
        self.assertTrue(all(f.state is RowState.CARGANDO for f in filas))
        self.assertTrue(all(program_text(f) == CARGANDO for f in filas))

    def test_programa_sin_titulo_no_rompe(self):
        """§18: un programa sin título es un dato ausente, no un error."""
        epg = Epg(
            channels_by_id={"cnn": "CNN"},
            programs={"cnn": [programa("cnn", "   ", AHORA, AHORA + timedelta(hours=1))]},
        )
        fila = build_rows([canal("CNN")], epg, AHORA)[0]
        self.assertIs(fila.state, RowState.AL_AIRE)
        self.assertEqual(program_text(fila), SIN_INFO)

    def test_programa_sin_stop_no_revienta(self):
        """El último de la parrilla no trae `stop`: se trata como abierto."""
        epg = Epg(
            channels_by_id={"cnn": "CNN"},
            programs={"cnn": [programa("cnn", "Telediario", AHORA, None)]},
        )
        fila = build_rows([canal("CNN")], epg, AHORA)[0]
        self.assertEqual(program_text(fila), "Telediario")
        self.assertIsNone(fila.next_change, "sin stop ni siguiente no hay cambio conocido")


class TestZonaHoraria(unittest.TestCase):
    def test_compara_instantes_y_no_cadenas(self):
        """§22: un programa con offset `-0500` se resuelve contra hora local.

        El instante es 18:00-05:00 == 23:00 UTC. Comparando el texto de la
        fecha contra una hora local se obtainsía un resultado arbitrario; lo
        que se exige es que el intervalo se evalúe como lo que es.
        """
        inicio = datetime(2026, 10, 7, 18, 0, tzinfo=timezone(timedelta(hours=-5)))
        fin = inicio + timedelta(hours=1)
        epg = Epg(
            channels_by_id={"mexico": "Canal México"},
            programs={"mexico": [programa("mexico", "Noticias 18:00", inicio, fin)]},
        )
        dentro = inicio + timedelta(minutes=30)
        fila = build_rows([canal("Canal México")], epg, dentro)[0]
        self.assertEqual(program_text(fila), "Noticias 18:00")

        fuera = fin + timedelta(minutes=1)
        fila_fuera = build_rows([canal("Canal México")], epg, fuera)[0]
        self.assertEqual(program_text(fila_fuera), SIN_INFO)

    def test_now_sin_zona_no_compara_instantes_incomparables(self):
        """Un `now` ingenuo se interpreta en hora local; no revienta el render."""
        inicio = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)
        epg = Epg(
            channels_by_id={"cnn": "CNN"},
            programs={"cnn": [programa("cnn", "Noticias", inicio,
                                       inicio + timedelta(hours=1))]},
        )
        ingenuo = datetime(2026, 10, 7, 18, 30)  # naive
        filas = build_rows([canal("CNN")], epg, ingenuo)
        self.assertEqual(len(filas), 1)
        self.assertIn(program_text(filas[0]), ("Noticias", SIN_INFO))


# ---------------------------------------------------------------------------
# 2. El índice de asociación: O(1) y sin comparaciones agresivas (§17, §19)
# ---------------------------------------------------------------------------


class _DictQueCuenta(dict):
    """`dict` que cuenta cada lectura, para medir un recorrido de verdad."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lecturas = 0

    def __iter__(self):
        self.lecturas += 1
        return super().__iter__()

    def __getitem__(self, key):
        self.lecturas += 1
        return super().__getitem__(key)

    def __contains__(self, key):
        self.lecturas += 1
        return super().__contains__(key)


class TestIndiceEpg(unittest.TestCase):
    def setUp(self):
        self.epg = epg_completo()

    def test_tvg_id_gana_al_nombre(self):
        canal_id = canal("CNN", tvg_id="cnn")
        self.assertEqual(_epg_channel_id(self.epg, canal_id), "cnn")

    def test_tvg_id_desconocido_cae_al_nombre(self):
        """Un `tvg-id` que la guía no conoce no deja al canal sin programa."""
        canal_id = canal("CNN", tvg_id="cnn.international")
        self.assertEqual(_epg_channel_id(self.epg, canal_id), "cnn")

    def test_nombre_similar_no_se_confunde(self):
        """§19: `CNN` no es `CNN International`, aunque se parezcan."""
        self.assertIsNone(_epg_channel_id(self.epg, canal("CNN International")))

    def test_canal_fuera_de_la_guia_no_explota(self):
        self.assertIsNone(_epg_channel_id(self.epg, canal("Canal Raro")))
        self.assertIsNone(_epg_channel_id(None, canal("CNN")))

    def test_se_reutiliza_el_indice_para_la_misma_guia(self):
        """El índice se construye una vez por guía, no una por favorito."""
        primero = epg_match.for_epg(self.epg)
        self.assertIs(epg_match.for_epg(self.epg), primero)
        otro = epg_match.for_epg(epg_completo())
        self.assertIsNot(otro, primero, "otra guía ⇒ otro índice")
        self.assertIsNone(epg_match.for_epg(None))

    def test_normalizar_no_inventa_parecidos(self):
        """El índice agrupa lo que es el mismo nombre, no lo que se parece."""
        self.assertEqual(epg_match.normalize_key("  CNN   Intl  "), "cnn intl")
        self.assertNotEqual(epg_match.normalize_key("CNN"), "cnn international")
        self.assertEqual(epg_match.normalize_key(None), "")
        self.assertEqual(epg_match.normalize_key("   "), "")

    def test_guia_grande_se_resuelve_sin_recorrerla(self):
        """500 favoritos contra una guía de 20 000 canales: cero recorridos.

        Aquí no se mide tiempo (depende de la máquina), sino **accesos**: la guía
        cuenta cada lectura y, con el índice ya construido, resolver las 500
        filas tiene que dejarla en cero. Con el escaneo lineal que había antes,
        serían diez millones de lecturas.
        """
        canales_epg = _DictQueCuenta({f"id{i}": f"Canal {i}" for i in range(20_000)})
        programas_epg = _DictQueCuenta({
            cid: [programa(cid, "Algo", AHORA, AHORA + timedelta(hours=1))]
            for cid in canales_epg
        })
        epg = Epg(channels_by_id=canales_epg, programs=programas_epg)

        indice = epg_match.for_epg(epg)
        self.assertIsNotNone(indice)
        self.assertEqual(len(indice), 20_000, "el índice cubre los canales con programas")

        canales_epg.lecturas = programas_epg.lecturas = 0
        favoritos = [canal(f"Canal {i}") for i in range(500)]
        filas = build_rows(favoritos, epg, AHORA, index=indice)

        self.assertEqual(len(filas), 500)
        self.assertTrue(all(f.state is RowState.AL_AIRE for f in filas))
        self.assertEqual(
            (canales_epg.lecturas, programas_epg.lecturas), (0, 0),
            "resolver favoritos no puede recorrer la guía: eso es el N+1 del §17",
        )


# ---------------------------------------------------------------------------
# 3. CA-08: construir las filas no toca la red ni el disco
# ---------------------------------------------------------------------------


class TestSinRedNiDisco(unittest.TestCase):
    def test_build_rows_no_pide_nada(self):
        """Con `mock.patch` explícito: cargar la lista no descarga ni lee."""
        from thetvview import epg_parser, favorites

        with (
            mock.patch.object(epg_parser, "load_url") as carga,
            mock.patch.object(epg_parser, "parse_file") as parseo,
            mock.patch.object(favorites, "json") as _json,
            mock.patch("builtins.open", side_effect=AssertionError("se abrió un fichero")),
        ):
            filas = build_rows(CUATRO, epg_completo(), AHORA)
            FavoritesScreen(AppFalso(CUATRO, epg_completo()))
        self.assertEqual(len(filas), 4)
        carga.assert_not_called()
        parseo.assert_not_called()
        _json.loads.assert_not_called()

    def test_la_pantalla_no_llama_a_ensure_epg(self):
        """La app real ni siquiera expone ese atributo en el doble: si se
        llamara, el `AttributeError` aparecería aquí."""
        app = AppFalso(CUATRO, epg_al_aire())
        self.assertFalse(hasattr(app, "ensure_epg"))
        stdscr = pintar(app)
        self.assertIn("Noticias", stdscr.texto)

    def test_favorites_json_no_guarda_el_programa(self):
        """§4/§26: el favorito sigue siendo un canal y sólo un canal."""
        import json
        import tempfile
        from pathlib import Path

        from thetvview.favorites import FavoritesManager

        ruta = Path(tempfile.mkdtemp()) / "favorites.json"
        gestor = FavoritesManager(ruta)
        gestor.toggle(canal("CNN", tvg_id="cnn"))

        app = AppFalso(gestor.load(), epg_completo())
        pintar(app)

        guardado = json.loads(ruta.read_text(encoding="utf-8"))
        self.assertEqual(len(guardado), 1)
        self.assertEqual(guardado[0]["name"], "CNN")
        for clave in ("program", "programa", "current_program", "epg"):
            with self.subTest(clave=clave):
                self.assertNotIn(clave, guardado[0])


# ---------------------------------------------------------------------------
# 4. El reparto en dos columnas (§15, §28)
# ---------------------------------------------------------------------------


class TestColumnas(unittest.TestCase):
    def test_dos_columnas_alineadas(self):
        filas = build_rows(CUATRO, epg_completo(), AHORA)
        self.assertTrue(two_columns_fit(80))
        pares = layout_columns(filas, 80)
        self.assertEqual(len(pares), 4)
        # Los cuatro programas empiezan en la misma celda.
        inicio = cell_width(pares[0][0])
        for canal_txt, programa_txt in pares:
            with self.subTest(canal=canal_txt.strip()):
                self.assertEqual(cell_width(canal_txt), inicio)
                self.assertTrue(programa_txt)

    def test_titulo_largo_no_rompe_la_fila(self):
        filas = build_rows(
            [canal("CNN"), canal("Discovery Channel International HD")],
            Epg(
                channels_by_id={"cnn": "CNN"},
                programs={"cnn": [programa(
                    "cnn", "Un programa con un título larguísimo que no cabe de "
                           "ninguna manera en una terminal de ochenta columnas",
                    AHORA, AHORA + timedelta(hours=1))]},
            ),
            AHORA,
        )
        ancho = 80 - 3 - 1  # prefijo de selección + scrollbar reservado
        for canal_txt, programa_txt in layout_columns(filas, 80):
            with self.subTest(canal=canal_txt.strip()):
                self.assertLessEqual(cell_width(canal_txt) + cell_width(programa_txt), ancho)

    def test_terminal_estrecha_deja_una_sola_columna(self):
        """§15: por debajo del mínimo se quita el programa, no el nombre."""
        filas = build_rows(CUATRO, epg_completo(), AHORA)
        ancho = MIN_TWO_COLUMN_CELLS - 1
        self.assertFalse(two_columns_fit(ancho))
        for canal_txt, programa_txt in layout_columns(filas, ancho):
            with self.subTest(canal=canal_txt.strip()):
                self.assertEqual(programa_txt, "")
                self.assertTrue(canal_txt.strip())

    def test_nombre_con_doble_ancho_no_descoloca(self):
        """Emoji y coreano ocupan dos celdas: se miden, no se cuentan."""
        filas = build_rows(
            [canal("📺 TV Coverage"), canal("BBC One")],
            epg_completo(),
            AHORA,
        )
        for canal_txt, programa_txt in layout_columns(filas, 80):
            with self.subTest(canal=canal_txt.strip()):
                self.assertLessEqual(
                    cell_width(canal_txt) + cell_width(programa_txt), 80 - 4
                )

    def test_radio_con_distintivo_y_sin_grupo_ni_estrella(self):
        """El `★` por fila y el grupo se retiran: aquí todo es favorito."""
        self.assertEqual(channel_label(canal("CNN", grupo="Noticias")), "CNN")
        self.assertTrue(channel_label(canal("R5", radio=True)).startswith("♪ "))

    def test_sin_filas_no_revienta(self):
        self.assertEqual(layout_columns([], 80), [])
        self.assertEqual(layout_columns([], 10), [])


# ---------------------------------------------------------------------------
# 5. El render (sin curses)
# ---------------------------------------------------------------------------


class TestRender(unittest.TestCase):
    def test_sin_guia_la_lista_se_ve_igual_que_antes(self):
        """§11.6: sin EPG no se reserva la segunda columna."""
        stdscr = pintar(AppFalso(CUATRO, None))
        self.assertNotIn(SIN_INFO, stdscr.texto)
        self.assertNotIn(CARGANDO, stdscr.texto)
        # Cada fila es la etiqueta de siempre: `★` y grupo, sin columna nueva.
        pintadas = [texto for _y, _x, texto, _attr in stdscr.calls]
        self.assertEqual(len(pintadas), len(CUATRO))
        for canal_esperado, texto in zip(CUATRO, pintadas):
            with self.subTest(canal=canal_esperado.name):
                self.assertIn(f"★ {canal_esperado.name}", texto)

    def test_con_guia_muestra_canal_y_programa(self):
        stdscr = pintar(AppFalso(CUATRO, epg_al_aire()))
        self.assertIn("CNN", stdscr.texto)
        self.assertIn("Noticias", stdscr.texto)
        self.assertIn("Planet Earth", stdscr.texto)
        # Y sin el `★` de siempre: en dos columnas sólo va el nombre (§D5).
        self.assertNotIn("★", stdscr.texto)

    def test_guia_parcial_muestra_el_guion_atenuado(self):
        """El `—` de los canales sin información es discreto, no un error."""
        epg = epg_al_aire({"CNN": "Noticias"})
        stdscr = pintar(AppFalso(CUATRO, epg))
        self.assertIn("Noticias", stdscr.texto)
        self.assertIn(SIN_INFO, stdscr.texto)
        atenuados = [c for c in stdscr.calls if SIN_INFO in c[2]]
        self.assertTrue(atenuados, "el guion debe salir atenuado, no con el par normal")
        for _y, _x, texto, attr in atenuados:
            with self.subTest(texto=texto):
                self.assertEqual(attr, colors.PAIR_DIM)

    def test_ventana_minima_40x10_no_desborda(self):
        """El mínimo del proyecto: 40×10 (AC-13)."""
        for epg in (None, epg_completo()):
            with self.subTest(epg="guia" if epg else "sin guia"):
                pintar(AppFalso(CUATRO, epg), max_y=10, max_x=40)

    def test_30_columnas_deja_una_sola_columna(self):
        app = AppFalso(CUATRO, epg_al_aire(), max_x=30)
        stdscr = pintar(app, max_x=30)
        self.assertIn("CNN", stdscr.texto)
        self.assertNotIn("Noticias", stdscr.texto)
        for _y, _x, texto, _attr in stdscr.calls:
            self.assertLessEqual(cell_width(texto.rstrip()), 30 - 1)

    def test_scrollbar_no_se_pisa_con_la_segunda_columna(self):
        """Con desborde, la última celda es del scrollbar, no del programa."""
        muchos = [canal(f"Canal {i}") for i in range(40)]
        epg = epg_al_aire({c.name: "Noticiario" for c in muchos})
        stdscr = pintar(AppFalso(muchos, epg))
        self.assertTrue(stdscr.calls)
        for y, x, texto, _attr in stdscr.calls:
            with self.subTest(y=y, x=x):
                if x >= 79:
                    self.assertNotIn("Noticiario", texto, "el scrollbar se ha comido texto")

    def test_la_fila_seleccionada_conserva_su_resaltado(self):
        app = AppFalso(CUATRO, epg_al_aire())
        pantalla = FavoritesScreen(app)
        pantalla.list.selected = 2
        stdscr = StdscrCaptura()
        with mock.patch.object(colors, "pair", side_effect=lambda pid: pid):
            pantalla.render(stdscr)
        fila = [c for c in stdscr.calls if c[0] == 3]
        self.assertTrue(fila, "la fila 3 (índice 2) tiene que estar pintada")
        for _y, _x, _texto, attr in fila:
            with self.subTest(attr=attr):
                self.assertEqual(attr, colors.PAIR_SELECTED)
        self.assertTrue(any("Football" in c[2] for c in fila))


class TestRecargaPorCambioDeGuia(unittest.TestCase):
    """§16 sin temporizador: se refresca cuando cambian los datos."""

    def test_llega_una_guia_mientras_se_mira_la_lista(self):
        app = AppFalso(CUATRO, None)
        pantalla = FavoritesScreen(app)
        stdscr = StdscrCaptura()
        with mock.patch.object(colors, "pair", side_effect=lambda pid: pid):
            pantalla.render(stdscr)
        self.assertNotIn("Noticias", stdscr.texto)

        app.epg = epg_al_aire()  # el hilo de carga terminó
        stdscr2 = StdscrCaptura()
        with mock.patch.object(colors, "pair", side_effect=lambda pid: pid):
            pantalla.render(stdscr2)
        self.assertIn("Noticias", stdscr2.texto)

    def test_volver_desde_la_guia_recarga_las_filas(self):
        """La guía abierta con `e` puede haber cambiado `app.epg`."""
        app = AppFalso(CUATRO, None)
        pantalla = FavoritesScreen(app)
        with mock.patch.object(colors, "pair", side_effect=lambda pid: pid):
            pantalla.render(StdscrCaptura())
        app.epg = epg_al_aire()
        self.assertTrue(pantalla.filas_al_dia())
        pantalla.render(StdscrCaptura())
        self.assertFalse(pantalla.filas_al_dia(), "con los mismos datos no se rehace")
        self.assertEqual(pantalla.rows[0].program.title, "Noticias")

    def test_un_fallo_de_guia_no_rompe_la_pantalla(self):
        """CA-06: un `Epg` con datos a medias degrada, no revienta."""
        roto = Epg(channels_by_id={}, programs={})
        app = AppFalso(CUATRO, roto)
        stdscr = pintar(app)
        self.assertIn("CNN", stdscr.texto)
        self.assertIn(SIN_INFO, stdscr.texto)

    def test_recargar_no_salta_al_primer_favorito(self):
        """Rehacer las filas no puede perder el sitio del cursor.

        Sin esto, cargar la guía mientras el usuario está en el quince de su
        lista lo teletransportaría al principio: la pantalla se vería como si
        alguien hubiera pulsado Inicio.
        """
        muchos = [canal(f"Canal {i}") for i in range(60)]
        app = AppFalso(muchos, None)
        pantalla = FavoritesScreen(app)
        with mock.patch.object(colors, "pair", side_effect=lambda pid: pid):
            pantalla.render(StdscrCaptura())
        pantalla.list.selected = 42
        pantalla.list.top = 30

        app.epg = epg_al_aire({c.name: "Noticiario" for c in muchos})
        with mock.patch.object(colors, "pair", side_effect=lambda pid: pid):
            pantalla.render(StdscrCaptura())

        self.assertEqual(pantalla.list.selected, 42)
        self.assertEqual(pantalla.list.top, 30)

    def test_render_repetido_no_rehace_nada(self):
        """Sin cambios de datos, el frame repintado no reconstruye las filas."""
        app = AppFalso(CUATRO, epg_al_aire())
        pantalla = FavoritesScreen(app)
        with mock.patch.object(colors, "pair", side_effect=lambda pid: pid):
            pantalla.render(StdscrCaptura())
            filas = list(pantalla.rows)
            pantalla.render(StdscrCaptura())
            pantalla.render(StdscrCaptura())
        self.assertEqual(pantalla.rows, filas)

    def test_cargar_en_segundo_plano_no_toma_el_mando(self):
        """Estando la guía en carga, la lista dice "Cargando…" y sigue viva."""
        app = AppFalso(CUATRO, None)
        app._epg_load_thread = mock.Mock(is_alive=mock.Mock(return_value=True))
        pantalla = FavoritesScreen(app)
        self.assertTrue(all(f.state is RowState.CARGANDO for f in pantalla.rows))
        stdscr = pintar(app)
        self.assertIn(CARGANDO, stdscr.texto)
        self.assertIn("CNN", stdscr.texto)


# ---------------------------------------------------------------------------
# 6. La tecla `e`: la guía que ya existe (§3)
# ---------------------------------------------------------------------------


class TestTeclaEPG(unittest.TestCase):
    def test_e_abre_la_guia_del_favorito_de_una(self):
        pantalla = FavoritesScreen(AppFalso(CUATRO, epg_completo()))
        accion = pantalla.handle_key(ord("e"))
        self.assertEqual(accion["action"], "show_epg")
        self.assertIs(accion["channel"], CUATRO[0])
        # Sin `epg_url`: un favorito puede venir de cualquier lista.
        self.assertNotIn("epg_url", accion)

    def test_e_sigue_el_cursor(self):
        pantalla = FavoritesScreen(AppFalso(CUATRO, epg_completo()))
        pantalla.list.selected = 2
        self.assertIs(pantalla.handle_key(ord("e"))["channel"], CUATRO[2])

    def test_e_sin_favoritos_avisa_con_modal(self):
        app = AppFalso([], epg_completo())
        pantalla = FavoritesScreen(app)
        self.assertIsNone(pantalla.handle_key(ord("e")))
        self.assertTrue(app.avisos, "sin canal bajo el cursor hay que avisar")

    def test_e_esta_en_las_tres_listas(self):
        """§10/R-09: la barra, los atajos y el manejador dicen lo mismo."""
        from thetvview.ui.actions import P, V

        pantalla = FavoritesScreen(AppFalso(CUATRO, epg_completo()))
        accion = next(a for a in pantalla.actions() if a.key == "e")
        self.assertEqual(accion.label, V.EPG)
        self.assertEqual(accion.priority, P.SECUNDARIA)
        self.assertIn("e EPG", pantalla.shortcuts())
        self.assertIsNotNone(pantalla.handle_key(ord("e")))

    def test_enter_sigue_reproduciendo_el_favorito(self):
        """CA-02: la reproducción no se toca."""
        pantalla = FavoritesScreen(AppFalso(CUATRO, epg_completo()))
        accion = pantalla.handle_key(10)
        self.assertEqual(accion["action"], "open_channel")
        self.assertIs(accion["channel"], CUATRO[0])

    def test_f_sigue_quitando_el_favorito(self):
        pantalla = FavoritesScreen(AppFalso(CUATRO, epg_completo()))
        accion = pantalla.handle_key(ord("f"))
        self.assertEqual(accion["action"], "unfavorite")
        self.assertIs(accion["channel"], CUATRO[0])


# ---------------------------------------------------------------------------
# 7. El dato que se calcula, no que se guarda
# ---------------------------------------------------------------------------


class TestFavoriteRow(unittest.TestCase):
    def test_es_inmutable(self):
        fila = FavoriteRow(channel=canal("CNN"), program=None, state=RowState.SIN_EPG)
        with self.assertRaises(Exception):
            fila.state = RowState.AL_AIRE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 8. Con la App real: la tecla llega y abre la pantalla que dice (§10)
# ---------------------------------------------------------------------------


class TestConAppReal(unittest.TestCase):
    """Nada de dobles aquí: la pulsación pasa por el camino del usuario.

    `_despachar_tecla` es lo que invoca `App.run`, así que esto mide lo único
    que importa: si declaramos `e EPG` y la app la entrega a la pantalla, la
    acción llega al `handle_action` y abre la guía.
    """

    def setUp(self) -> None:
        from thetvview import config
        from thetvview.ui.app import App
        from thetvview.ui.screens import EpgScreen

        self.EpgScreen = EpgScreen
        tmp = Path(tempfile.mkdtemp())
        for nombre in ("PLAYLISTS_JSON", "FAVORITES_JSON", "PREFS_JSON",
                       "RECENTS_JSON", "THEME_JSON"):
            patch = mock.patch.object(config, nombre, tmp / nombre.lower())
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(config, "ensure_dirs", lambda: None):
            self.app = App(_VentanaFalsa())
        self.app.favorites = _Favoritos([canal("CNN")])
        self.app.recents = _RecientesFalsos()
        self.app.playlists.load = lambda: []  # type: ignore[method-assign]
        self.app.epg = epg_al_aire()
        self.app.stack = [FavoritesScreen(self.app)]

    def pulsar(self, key: str) -> None:
        self.app._despachar_tecla(ord(key))

    def test_e_abre_la_guia_del_favorito(self):
        self.pulsar("e")
        self.assertIsInstance(self.app.screen, self.EpgScreen)
        self.assertEqual(self.app.screen.channel.name, "CNN")
        self.assertEqual(len(self.app.stack), 2)

    def test_r_sigue_abriendo_recientes(self):
        """La tecla nueva no secuestró el reparto de las globales."""
        from thetvview.ui.screens import RecentsScreen

        self.pulsar("r")
        self.assertIsInstance(self.app.screen, RecentsScreen)

    def test_enter_sigue_llegando_al_favorito(self):
        """`Enter` conserva la reproducción: aquí se ve que llega a la pantalla."""
        with mock.patch.object(self.app, "_track_options_then_player") as lanzar:
            self.pulsar("\r")
        self.assertTrue(lanzar.called, "Enter debe seguir abriendo el canal")


class _RecientesFalsos:
    def __init__(self) -> None:
        from thetvview.recents import RecentsManager

        self._vacios = RecentsManager(Path(tempfile.mkdtemp()) / "r.json")

    def load(self):
        return []

    def clear(self) -> None:
        self._vacios.clear()


class _VentanaFalsa:
    def getmaxyx(self):
        return (24, 80)

    def addstr(self, *args, **kwargs) -> None:  # pragma: no cover - no se pinta
        pass

    def erase(self) -> None:
        pass

    def refresh(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 9. El widget: la fila no puede ser más ancha que la ventana
# ---------------------------------------------------------------------------


class TestAnchoDeFila(unittest.TestCase):
    """`ScrollableList` recorta en celdas, no en caracteres (§42, §43).

    Esto no es hipotético: `NHK ワールド` son 6 caracteres y **12 celdas**, así
    que una fila que se recorta contando caracteres se sale de la ventana, se
    come la columna del scrollbar y en la esquina inferior derecha lanza
    `curses.error`. Comprueba las dos columnas y la de una.
    """

    ANCHO = 40
    WIDE = "NHK ワールド"

    def _pintar(self, subitems=None) -> None:
        from thetvview.ui.widgets import ScrollableList

        lista = ScrollableList()
        lista.set_items([self.WIDE], subitems=subitems)
        stdscr = StdscrCaptura(max_y=10, max_x=self.ANCHO)
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(stdscr, 1, 0, 5, self.ANCHO)
        self.assertTrue(stdscr.calls)

    def test_una_columna_no_desborda(self):
        self._pintar()

    def test_dos_columnas_no_desbordan(self):
        self._pintar(subitems=["Noticiario"])

    def test_una_columna_rellena_hasta_el_ancho(self):
        """Y tampoco deja huecos: la fila se limpia, como antes del cambio."""
        from thetvview.ui.widgets import ScrollableList

        lista = ScrollableList()
        lista.set_items(["CNN"])
        stdscr = StdscrCaptura(max_y=10, max_x=self.ANCHO)
        with mock.patch.object(colors, "pair", return_value=0):
            lista.render(stdscr, 1, 0, 5, self.ANCHO)
        for _y, _x, texto, _attr in stdscr.calls:
            with self.subTest(texto=texto):
                self.assertEqual(cell_width(texto), self.ANCHO - 1)


if __name__ == "__main__":
    unittest.main()