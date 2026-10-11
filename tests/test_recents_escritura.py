"""Recientes: la escritura que faltaba (FR-REC-01) y su identidad (FR-REC-04).

La clase `RecentsManager` estaba completa y probada —persistencia, redacción,
escritura atómica, deduplicación— pero **nadie la llamaba**: la pantalla de
Recientes estaba siempre vacía. Estos tests fijan el hueco que faltaba:

- reproducir un canal lo deja en Recientes;
- lo que se escribe es el canal **original**, nunca el que lleva las
  credenciales resueltas (es la diferencia entre un historial útil y una
  contraseña en disco);
- dos canales con el mismo nombre y distinta URL siguen siendo dos entradas;
- un `recents.json` antiguo (sin `tvg_id`) sigue cargando y deduplicando;
- un `recents.json` roto no impide abrir la app;
- el orden es por última reproducción aunque el archivo se edite a mano.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.m3u_parser import parse_text
from thetvview.models import Channel
from thetvview.recents import RecentItem, RecentsManager
from thetvview.security.secrets import get_store, reset_store
from thetvview.ui import screens as ui_screens

CAMARA = "rtsp://admin:P4ssw0rd@192.168.1.9:554/stream1"
LISTA_CAM = f'#EXTM3U\n#EXTINF:-1 tvg-id="cam1" group-title="Cámaras",Portón\n{CAMARA}\n'


# ---------------------------------------------------------------------------
# Dobles mínimos (los mismos que en test_cam_flow: la app real, el Popen no)
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self) -> None:
        self.pid = 4242

    def poll(self):  # noqa: ANN201
        return None


class _Status:
    def show(self, message: str, error: bool = False) -> None:
        pass


class _PrefsStub:
    def set_last_player(self, name: str | None) -> None:
        pass

    def load(self):  # noqa: ANN201
        from thetvview.prefs import Prefs

        return Prefs()


class _App:
    """App sin terminal: `recents` real sobre un tmpdir, avisos a la lista."""

    def __init__(self, ruta: Path) -> None:
        self.status = _Status()
        self.prefs = _PrefsStub()
        self.stack: list = []
        self.epg = None
        self.avises: list[str] = []
        self.recents = RecentsManager(ruta)

    def push(self, screen) -> None:
        self.stack.append(screen)

    def body_height(self) -> int:
        return 10

    def notify_warning(self, message: str) -> None:
        self.avises.append(message)

    def notify_error(self, message: str) -> None:
        self.status.show(message, error=True)


class BaseRecentsTemporal(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ruta = Path(self.dir.name) / "recents.json"
        self.rm = RecentsManager(self.ruta)


# ---------------------------------------------------------------------------
# 1. La escritura: reproducir deja el canal en Recientes (FR-REC-01)
# ---------------------------------------------------------------------------


class TestReproducirDejaEnRecientes(BaseRecentsTemporal):
    def setUp(self) -> None:
        super().setUp()
        previo = os.environ.get("THETVVIEW_SECRET_STORE")
        os.environ["THETVVIEW_SECRET_STORE"] = "memory"
        reset_store()
        self.addCleanup(self._restaurar, previo)

    def _restaurar(self, previo) -> None:
        reset_store()
        if previo is None:
            os.environ.pop("THETVVIEW_SECRET_STORE", None)
        else:
            os.environ["THETVVIEW_SECRET_STORE"] = previo

    def test_play_channel_escribe_en_recientes(self) -> None:
        canal = Channel(name="La 1", url="http://example.test/live/1.m3u8",
                        group="Noticias", tvg_id="la1")
        app = _App(self.ruta)
        with mock.patch(
            "thetvview.player.core.subprocess.Popen", return_value=_Proc()
        ), mock.patch(
            "thetvview.ui.screens.ChannelHealthMonitor", return_value=mock.Mock()
        ):
            ui_screens.play_channel(app, canal, player_name="mpv")

        items = self.rm.load()
        self.assertEqual([i.name for i in items], ["La 1"])
        self.assertEqual(items[0].player, "mpv")
        self.assertEqual(items[0].tvg_id, "la1")

    def test_se_escribe_el_canal_original_no_el_resuelto(self) -> None:
        """El caso peligroso: la credencial resuelta **no** puede tocar disco.

        Una cámara llega como referencia opaca; `play_channel` la resuelve a una
        URL con usuario y contraseña y es esa la que va al reproductor. Lo que
        va a Recientes es la de entrada.
        """
        lista = parse_text(LISTA_CAM, source="http://p/l.m3u", name="Camaras")
        canal = lista.channels[0]
        app = _App(self.ruta)
        with mock.patch(
            "thetvview.player.core.subprocess.Popen", return_value=_Proc()
        ), mock.patch(
            "thetvview.ui.screens.ChannelHealthMonitor", return_value=mock.Mock()
        ):
            ui_screens.play_channel(app, canal, player_name="mpv")

        # El reproductor sí recebeu la URL con credenciales…
        self.assertEqual(app.stack[0].channel.url, CAMARA)
        # …pero a disco no salió nada de eso.
        crudo = self.ruta.read_text(encoding="utf-8")
        self.assertNotIn("P4ssw0rd", crudo)
        self.assertNotIn(CAMARA, crudo)
        escritos = self.rm.load()
        self.assertEqual(len(escritos), 1)
        self.assertNotIn("P4ssw0rd", escritos[0].url)
        self.assertNotEqual(escritos[0].url, CAMARA)

    def test_un_disco_que_no_permite_guardar_no_impide_reproducir(self) -> None:
        """Un historial inservible no puede tumbar la reproducción."""
        canal = Channel(name="La 1", url="http://example.test/live/1.m3u8")
        app = _App(self.ruta)
        app.recents = mock.Mock()
        app.recents.push.side_effect = OSError("disco lleno")
        with mock.patch(
            "thetvview.player.core.subprocess.Popen", return_value=_Proc()
        ) as popen, mock.patch(
            "thetvview.ui.screens.ChannelHealthMonitor", return_value=mock.Mock()
        ):
            ui_screens.play_channel(app, canal, player_name="mpv")
        popen.assert_called_once()
        self.assertEqual(len(app.stack), 1)

    def test_app_sin_recents_no_revienta(self) -> None:
        """Doble de test o versión vieja: `app.recents` puede no existir."""
        canal = Channel(name="La 1", url="http://example.test/live/1.m3u8")
        app = _App(self.ruta)
        del app.recents
        with mock.patch(
            "thetvview.player.core.subprocess.Popen", return_value=_Proc()
        ) as popen, mock.patch(
            "thetvview.ui.screens.ChannelHealthMonitor", return_value=mock.Mock()
        ):
            ui_screens.play_channel(app, canal, player_name="mpv")
        popen.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Identidad (FR-REC-04, T-REC-03)
# ---------------------------------------------------------------------------


class TestIdentidad(BaseRecentsTemporal):
    def test_mismo_nombre_distinta_url_son_dos(self) -> None:
        a = Channel(name="Canal 1", url="http://a.test/1.m3u8")
        b = Channel(name="Canal 1", url="http://b.test/1.m3u8")
        self.rm.push(a, player="mpv")
        self.rm.push(b, player="vlc")
        items = self.rm.load()
        self.assertEqual(len(items), 2)
        self.assertEqual({i.url for i in items}, {a.url, b.url})

    def test_mismo_tvg_id_actualiza_en_vez_de_duplicar(self) -> None:
        ch = Channel(name="La 1", url="http://a.test/1.m3u8", tvg_id="la1")
        self.rm.push(ch, player="mpv")
        # Mismo canal con otra URL (la lista cambió): es el mismo canal.
        otro = Channel(name="La 1 HD", url="http://a.test/1-hd.m3u8", tvg_id="la1")
        self.rm.push(otro, player="vlc")
        items = self.rm.load()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].player, "vlc")
        self.assertEqual(items[0].name, "La 1 HD")

    def test_json_antiguo_sin_tvg_id_deduplica_por_url(self) -> None:
        """Compatibilidad hacia atrás (NFR-07): lo de antes sigue funcionando."""
        self.ruta.write_text(
            json.dumps(
                [
                    {"name": "Vieja", "url": "http://a.test/1.m3u8",
                     "group": None, "player": "mpv",
                     "at_utc": "2026-01-01T10:00:00+00:00"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.assertIsNone(self.rm.load()[0].tvg_id)
        self.rm.push(Channel(name="Vieja", url="http://a.test/1.m3u8"), player="vlc")
        self.assertEqual(len(self.rm.load()), 1)
        self.assertEqual(self.rm.load()[0].player, "vlc")

    def test_url_con_credenciales_se_redacta_al_guardar(self) -> None:
        canal = Channel(name="Privado", url="http://user:p4ss@h.test/live/1.m3u8?password=x")
        self.rm.push(canal, player="mpv")
        crudo = self.ruta.read_text(encoding="utf-8")
        self.assertNotIn("p4ss", crudo)


# ---------------------------------------------------------------------------
# 3. Datos dañados y orden (FR-REC-02, FR-REC-08)
# ---------------------------------------------------------------------------


class TestDatosDanadosYOrden(BaseRecentsTemporal):
    def test_json_corrupto_no_impide_abrir(self) -> None:
        self.ruta.write_text("{esto no es json", encoding="utf-8")
        # Un historial roto no impide abrir la app: se trata como vacío
        # y se puede seguir escribiendo encima.
        self.assertEqual(self.rm.load(), [])
        self.rm.push(Channel(name="La 1", url="http://a.test/1"), player="mpv")
        self.assertEqual(len(self.rm.load()), 1)

    def test_json_que_no_es_lista_se_ignora(self) -> None:
        self.ruta.write_text('{"name": "La 1"}', encoding="utf-8")
        self.assertEqual(self.rm.load(), [])

    def test_entradas_sin_url_se_saltan(self) -> None:
        self.ruta.write_text(
            json.dumps([{"name": "Rota"}, {"name": "Ok", "url": "http://a/1"}]),
            encoding="utf-8",
        )
        self.assertEqual([i.name for i in self.rm.load()], ["Ok"])

    def test_orden_por_marca_descendente_aunque_esten_desordenadas(self) -> None:
        self.ruta.write_text(
            json.dumps(
                [
                    {"name": "Viejo", "url": "http://a/1",
                     "at_utc": "2026-01-01T10:00:00+00:00"},
                    {"name": "Reciente", "url": "http://a/2",
                     "at_utc": "2026-01-03T10:00:00+00:00"},
                    {"name": "Medio", "url": "http://a/3",
                     "at_utc": "2026-01-02T10:00:00+00:00"},
                ]
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            [i.name for i in self.rm.load()], ["Reciente", "Medio", "Viejo"]
        )

    def test_marca_invalida_no_rompe_el_orden(self) -> None:
        self.ruta.write_text(
            json.dumps(
                [
                    {"name": "Basura", "url": "http://a/1", "at_utc": "no-es-fecha"},
                    {"name": "Buena", "url": "http://a/2",
                     "at_utc": "2026-01-02T10:00:00+00:00"},
                ]
            ),
            encoding="utf-8",
        )
        items = self.rm.load()
        self.assertEqual(items[0].name, "Buena")
        self.assertEqual(len(items), 2)

    def test_push_conserva_el_tope(self) -> None:
        rm = RecentsManager(self.ruta, max_items=2)
        for i in range(4):
            rm.push(Channel(name=f"C{i}", url=f"http://a/{i}"), player="mpv")
        self.assertEqual(len(rm.load()), 2)

    def test_identidad_de_registro_sin_url_devuelve_none(self) -> None:
        self.assertIsNone(RecentItem(name="X", url="", group=None, player="mpv").identidad())


if __name__ == "__main__":
    unittest.main()