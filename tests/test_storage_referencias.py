"""Referencias al borrar una lista (STORE-006).

Borrar una lista no puede dejar favoritos ni recientes apuntando a un servidor
que ya no está en el catálogo: son inservibles y el usuario no se entera. Estos
tests fijan que:

- se **cuentan** antes de borrar, para poder avisar en el modal;
- se **limpian** después, y sólo si el borrado funcionó;
- no se toca lo que viene de otra lista;
- las referencias opacas de Xtream se reconocen por ``tvg-id``, no por host.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.favorites import FavoritesManager
from thetvview.models import Channel, Playlist
from thetvview.recents import RecentsManager


class _AppFalso:
    """Lo mínimo de `App` que exercised las dos funciones.

    Se construye a mano en vez de instanciar `App` (que necesita un `stdscr`)
    porque lo que se prueba aquí es la **contabilidad y la limpieza**, no el
    dibujo del modal.
    """

    def __init__(self, tmp: Path) -> None:
        self.favorites = FavoritesManager(tmp / "favorites.json")
        self.recents = RecentsManager(tmp / "recents.json")
        self.playlists: dict[str, Playlist] = {}
        # Se reutilizan las funciones reales del App para no probarlas dos veces.
        from thetvview.ui.app import App

        self.referencias_a_lista = App.referencias_a_lista.__get__(self)
        self.limpiar_referencias_a_lista = App.limpiar_referencias_a_lista.__get__(self)

    def cached_playlist(self, source: str) -> Playlist | None:
        return self.playlists.get(source)


class TestReferencias(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.app = _AppFalso(self.tmp)

    def _playlist(self, source: str, canales: list[Channel]) -> None:
        self.app.playlists[source] = Playlist(name="L", source=source, channels=canales)

    # -- contabilidad --------------------------------------------------------

    def test_sin_referencias_cuenta_cero(self) -> None:
        cuentas = self.app.referencias_a_lista("http://a.test/l.m3u")
        self.assertEqual(cuentas, {"favoritos": 0, "recientes": 0})

    def test_cuenta_favoritos_del_mismo_servidor(self) -> None:
        self.app.favorites.toggle(Channel(name="C1", url="http://a.test/1.ts"))
        self.app.favorites.toggle(Channel(name="C2", url="http://a.test/2.ts"))
        cuentas = self.app.referencias_a_lista("http://a.test/l.m3u")
        self.assertEqual(cuentas["favoritos"], 2)

    def test_no_cuenta_favoritos_de_otro_servidor(self) -> None:
        self.app.favorites.toggle(Channel(name="C1", url="http://b.test/1.ts"))
        cuentas = self.app.referencias_a_lista("http://a.test/l.m3u")
        self.assertEqual(cuentas["favoritos"], 0)

    def test_cuenta_recientes(self) -> None:
        self.app.recents.push(Channel(name="C1", url="http://a.test/1.ts"), "mpv")
        cuentas = self.app.referencias_a_lista("http://a.test/l.m3u")
        self.assertEqual(cuentas["recientes"], 1)

    def test_referencia_opaca_se_reconoce_por_tvg_id(self) -> None:
        """Una fuente Xtream no tiene host legible: se reconoce por tvg-id."""
        self._playlist(
            "xtream://http://panel.test",
            [Channel(name="Canal", url="xtream://abc", tvg_id="canal.1.es")],
        )
        self.app.favorites.toggle(
            Channel(name="Canal", url="xtream://abc", tvg_id="canal.1.es")
        )
        cuentas = self.app.referencias_a_lista("xtream://http://panel.test")
        self.assertEqual(cuentas["favoritos"], 1)

    def test_tvg_id_de_otra_lista_no_cuenta(self) -> None:
        self._playlist(
            "xtream://http://panel.test",
            [Channel(name="Canal", url="xtream://abc", tvg_id="canal.1.es")],
        )
        self.app.favorites.toggle(
            Channel(name="Otro", url="xtream://xyz", tvg_id="canal.999.es")
        )
        cuentas = self.app.referencias_a_lista("xtream://http://panel.test")
        self.assertEqual(cuentas["favoritos"], 0)

    # -- limpieza -----------------------------------------------------------

    def test_limpia_lo_que_pertenece_y_deja_el_resto(self) -> None:
        self.app.favorites.toggle(Channel(name="Mio", url="http://a.test/1.ts"))
        self.app.favorites.toggle(Channel(name="Otro", url="http://b.test/1.ts"))
        self.app.recents.push(Channel(name="Mio", url="http://a.test/1.ts"), "mpv")
        self.app.recents.push(Channel(name="Otro", url="http://b.test/1.ts"), "mpv")

        quitados = self.app.limpiar_referencias_a_lista("http://a.test/l.m3u")
        self.assertEqual(quitados, 2)

        urls_fav = {c.url for c in self.app.favorites.load()}
        self.assertEqual(urls_fav, {"http://b.test/1.ts"})
        urls_rec = {i.url for i in self.app.recents.load()}
        self.assertEqual(urls_rec, {"http://b.test/1.ts"})

    def test_limpiar_sin_referencias_no_toca_nada(self) -> None:
        self.app.favorites.toggle(Channel(name="Otro", url="http://b.test/1.ts"))
        self.assertEqual(self.app.limpiar_referencias_a_lista("http://a.test/l.m3u"), 0)
        self.assertEqual(len(self.app.favorites.load()), 1)

    def test_source_vacio_no_hace_nada(self) -> None:
        self.app.favorites.toggle(Channel(name="C", url="http://a.test/1.ts"))
        self.assertEqual(self.app.limpiar_referencias_a_lista(""), 0)
        self.assertEqual(len(self.app.favorites.load()), 1)


if __name__ == "__main__":
    unittest.main()
