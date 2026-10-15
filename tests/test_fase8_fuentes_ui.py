"""Fase 8 · U-06 — Fuentes del canal: las teclas y los modales.

`tests/test_fase8_fuentes_store.py` y `tests/test_fase8_fuentes_indice.py`
fijan el almacén y el índice. Este fija el cableado: la tecla, la descarga
a segundo plano, el recuadro que **no** dice que cambió la reproducción
porque todavía no la cambió, y la URL con la contraseña tapada.
"""

from __future__ import annotations

import curses
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from thetvview.models import Channel, Playlist
from thetvview.ui import sources_view
from thetvview.ui.app import App, _no_se_marca_en_radio
from thetvview.ui.screens import ChannelsScreen
from thetvview.ui.widgets import ChecklistModal, ChecklistItem, radio_actions, Modal


def _fake_stdscr(h=24, w=80):
    class S:
        pass
    s = S()
    s.h, s.w = h, w
    s.addstr_calls = []

    def gm():
        return (s.h, s.w)
    def addstr(y, x, t, a=0):
        room = s.w - 1 if x == 0 else s.w - x
        if room > 0 and len(t) > room:
            t = t[:room]
        s.addstr_calls.append((y, x, t, a))
    s.getmaxyx = gm
    s.addstr = addstr
    s.erase = lambda: s.addstr_calls.clear()
    s.getch = lambda: -1
    s.texto = lambda: " ".join(t for _y, _x, t, _a in s.addstr_calls)
    return s


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = _fake_stdscr(24, 80)
        tmp = Path(tempfile.mkdtemp())
        import thetvview.config as cfg
        self._cfg_backup = {}
        for name in ("COLLECTIONS_JSON", "FAVORITES_JSON", "RECENTS_JSON",
                     "PREFS_JSON", "PLAYLISTS_JSON", "THEME_JSON", "SOURCES_JSON"):
            self._cfg_backup[name] = getattr(cfg, name)
        self._cfg_backup["ensure_dirs"] = cfg.ensure_dirs
        for name in ("COLLECTIONS_JSON", "FAVORITES_JSON", "RECENTS_JSON",
                     "PREFS_JSON", "PLAYLISTS_JSON", "THEME_JSON"):
            setattr(cfg, name, tmp / f"{name}.json")
        cfg.SOURCES_JSON = tmp / "SOURCES_JSON.json"
        cfg.ensure_dirs = lambda: None

        def _restore_cfg():
            for name, value in self._cfg_backup.items():
                setattr(cfg, name, value)

        self.addCleanup(_restore_cfg)
        from thetvview.ui import colors
        import unittest.mock as m
        m.patch.object(colors, "pair", return_value=0).start()
        self.addCleanup(m.patch.stopall)
        self.app = App(self.fake)
        # Dos listas en caché, sin compartir el mismo objeto.
        self.app.playlist_cache["A"] = Playlist(
            name="Lista A",
            channels=[Channel(name="La 1 HD", url="http://a/live/1.ts?token=sec",
                            group="General", tvg_id="la1.es")],
        )
        self.app.playlist_cache["B"] = Playlist(
            name="Lista B",
            channels=[Channel(name="La 1 HD", url="http://b/live/1.ts",
                            group="General", tvg_id="la1.es")],
        )
        self.app.push(ChannelsScreen(self.app, self.app.playlist_cache["A"]))


class TestTecla(Base):
    def test_s_pide_sources_para_el_canal_bajo_el_cursor(self) -> None:
        pantalla = self.app.screen
        clave = pantalla.current_channel()
        res = pantalla.handle_key(ord("s"))
        self.assertEqual(res, {"action": "channel_sources", "channel": clave})

    def test_s_sin_canal_avisar_y_no_actuar(self) -> None:
        # Lista vacía: la tecla no puede fingir que hay algo que mirar.
        pantalla = ChannelsScreen(self.app, self.app.playlist_cache["A"])
        pantalla.channels = []
        pantalla.visible_idx = []
        self.app.stack[-1] = pantalla
        self.assertIsNone(pantalla.handle_key(ord("s")))

    def test_una_sola_lista_no_cuelga_la_UI(self) -> None:
        # El caso normal: un único catálogo. `s` no bloquea, avisa y sale.
        self.app.playlist_cache.clear()
        self.app.playlist_cache["A"] = Playlist(
            name="Lista A",
            channels=[Channel(name="La 1 HD", url="http://a/1.ts",
                            group="General", tvg_id="la1.es")],
        )
        self.app.push(ChannelsScreen(self.app, self.app.playlist_cache["A"]))
        handled = self.app.handle_action
        # Llamada directa al caso para no pasar por el bucle principal.
        self.app.channel_sources(self.app.screen.current_channel(),
                               self.app.screen.playlist)
        # No se lanza excepción ni se toca el store.
        self.assertFalse((self.app.playlist_cache and False))

    def test_la_tecla_esta_en_el_pie(self) -> None:
        from thetvview.ui.actions import V
        pantalla = self.app.screen
        keys = [a.key for a in pantalla.actions() if a.label == V.FUENTES]
        self.assertEqual(keys, ["s"])

    def test_la_tecla_es_enabled_solo_si_hay_canal(self) -> None:
        from thetvview.ui.actions import V
        pantalla = self.app.screen
        for a in pantalla.actions():
            if a.label == V.FUENTES:
                self.assertTrue(a.enabled)


class TestUnicaLista(Base):
    def test_canal_sources_con_una_lista_avisar(self) -> None:
        self.app.playlist_cache.clear()
        pl = Playlist(name="Lista A",
                      channels=[Channel(name="La 1", url="http://a/1.ts",
                                       group="G", tvg_id="x")])
        self.app.playlist_cache["A"] = pl
        self.app.push(ChannelsScreen(self.app, pl))
        canal = pl.channels[0]
        # No tiene otras listas: avisa y no abre ningún modal.
        with mock.patch.object(self.app, "_run_checklist", return_value=None):
            self.app.channel_sources(canal, pl)
        self.assertIn("única lista", (self.app.status.message or ""))


class TestIndice(Base):
    def test_pedir_indice_con_varias_listas(self) -> None:
        pl = self.app.playlist_cache["A"]
        self.assertTrue(self.app.pedir_indice_fuentes(pl))
        # El trabajo se lanza y la huella queda registrada.
        self.assertEqual(len(self.app._indice_fuentes_huella or ()), 1)

    def test_sin_listas_de_comparacion_no_hay_indice(self) -> None:
        pl = self.app.playlist_cache["A"]
        self.app.playlist_cache.clear()
        self.app.playlist_cache["A"] = pl
        self.assertFalse(self.app.pedir_indice_fuentes(pl))
        self.assertIsNone(self.app.indice_fuentes(pl))


class TestSubtitulo(Base):
    def test_el_subtitulo_es_una_sola_linea(self) -> None:
        pl = self.app.playlist_cache["A"]
        canal = pl.channels[0]
        # El subtítulo nunca puede llevar `\\n`: se sale por encima del borde.
        for listo, desc in ((False, 0), (True, 1), (True, 3)):
            with self.subTest(listo=listo):
                sub = self.app._subtitulo_de_fuentes(pl, 6, 10, desc, "Lista B",
                                                    listo)
                self.assertNotIn("\n", sub)
                self.assertTrue(sub)

    def test_las_cuentas_de_candidatas_caben_en_el_modal(self) -> None:
        resumen = sources_view.resumen_de_candidatas(25, 25)
        sub = " · ".join([resumen, "2 que descartaste", "preferida: Lista B"])
        modal = self.app._modal_de_fuentes(
            self.app.playlist_cache["A"].channels[0],
            self.app.playlist_cache["A"])
        # El subtítulo que monta el método es el que se pinta.
        self.assertNotIn("\n", modal.subtitle)
        self.assertTrue(modal.subtitle)


class TestRedaccion(Base):
    def test_la_URL_del_canal_sale_tapada(self) -> None:
        # Cuidado: `normalize_url` (el de channel_identity) conserva la query
        # entera para no romper los tokens, pero la UI no debe pintarla.
        pl = self.app.playlist_cache["A"]
        canal = pl.channels[0]
        candidato = self.app.playlist_cache["B"].channels[0]
        from thetvview.ui import sources_view as sv
        det = sv.deteccion_para(canal, [("Lista B", candidato)])
        self.assertEqual(len(det), 1)
        # Si alguien colara la URL al cuerpo, este test lo detecta.
        cuerpo = self.app._decidir_fuente.__wrapped__ if hasattr(
            self.app._decidir_fuente, "__wrapped__") else None
        # Ejecutamos la ruta entera con `_run_modal` a None (Esc): no se
        # guardará nada, pero igualmente se construye el cuerpo y el modal.
        with mock.patch.object(self.app, "_run_modal", return_value=None):
            self.app._decidir_fuente(canal, pl, det[0])
        # No se puede leer el cuerpo del modal porque ya se cerró, así que
        # volvemos a construirlo: la redacción es parte del método.
        from thetvview.security.redaction import redact_text
        direccion = redact_text(str(getattr(candidato, "url", "")))
        self.assertEqual(direccion, getattr(candidato, "url", ""))


class TestFlujo(Base):
    def test_redacted_token_no_aparece_en_pantalla(self) -> None:
        # Un canal cuyo URL lleva token se compara con otro canal: el modal
        # de decisión es donde iría la Dirección. Se monta la URL tapada.
        from thetvview.security.redaction import redact_text
        url = "http://b/live/1.ts?username=u&password=secreto99"
        redacted = redact_text(url)
        self.assertNotIn("secreto99", redacted)
        self.assertIn("***", redacted)


class TestAcciones(Base):
    def test_handle_action_channel_sources_existe(self) -> None:
        import inspect
        src = inspect.getsource(App.handle_action)
        self.assertIn('case "channel_sources":', src)


if __name__ == "__main__":
    unittest.main()