"""El camino completo de una cámara: de la línea M3U al argv del reproductor.

Un solo test de recorrido, y existe por una razón concreta: las piezas sueltas
están cubiertas en otros archivos, pero **el orden** no. Si el parser no
registra la credencial, o si `resolve_channel_url` no la recupera, o si el
`Channel` se serializa mal, los tests de cada pieza pasan y el usuario recibe
una cámara que no reproduce.

El recorrido real, con la app de verdad y sólo el `Popen` sustituido:

```text
línea M3U → parse_text → Channel(ipcam://) → resolve_channel_url → argv
```

Y con un paso más: la lista se guarda en favoritos y se relee del disco, que es
donde una credencial se colaría si el diseño no estuviera bien.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from thetvview.cam_ref import PREFIX
from thetvview.favorites import FavoritesManager
from thetvview.m3u_parser import parse_text
from thetvview.models import Channel
from thetvview.player import command_for, launch, url_de_camara_resuelta
from thetvview.security.secrets import MemorySecretStore, get_store, reset_store
from thetvview.security.url_policy import PURPOSE_STREAM, validate_url
from thetvview.stream_ref import resolve_channel_url
from thetvview.ui import screens as ui_screens

CAMARA = "rtsp://admin:P4ssw0rd@192.168.1.9:554/stream1"
LISTA = f'#EXTM3U\n#EXTINF:-1 tvg-id="cam1" group-title="Cámaras",Portón\n{CAMARA}\n'


class _Proc:
    def __init__(self) -> None:
        self.pid = 31337

    def poll(self):  # noqa: ANN201
        return None


class _Status:
    def __init__(self) -> None:
        self.last = ""
        self.was_error = False

    def show(self, message: str, error: bool = False) -> None:
        self.last = message
        self.was_error = error


class _PrefsStub:
    def set_last_player(self, name: str | None) -> None:
        pass

    def load(self):  # noqa: ANN201
        from thetvview.prefs import Prefs

        return Prefs()


class _App:
    def __init__(self) -> None:
        self.status = _Status()
        self.prefs = _PrefsStub()
        self.stack: list = []
        self.epg = None
        self.avises: list[str] = []

    def push(self, screen) -> None:
        self.stack.append(screen)

    def body_height(self) -> int:
        return 10

    def notify_warning(self, message: str) -> None:
        self.avises.append(message)

    def notify_error(self, message: str) -> None:
        self.status.show(message, error=True)


class TestRecorridoCompleto(unittest.TestCase):
    def setUp(self) -> None:
        self._previo = os.environ.get("THETVVIEW_SECRET_STORE")
        os.environ["THETVVIEW_SECRET_STORE"] = "memory"
        reset_store()
        self.addCleanup(self._restaurar)

    def _restaurar(self) -> None:
        reset_store()
        if self._previo is None:
            os.environ.pop("THETVVIEW_SECRET_STORE", None)
        else:
            os.environ["THETVVIEW_SECRET_STORE"] = self._previo

    def test_de_la_linea_al_argv(self) -> None:
        # 1. El parser convierte la línea y registra la credencial.
        lista = parse_text(LISTA, source="http://p/l.m3u", name="Camaras")
        canal = lista.channels[0]
        self.assertTrue(canal.url.startswith(PREFIX))
        self.assertNotIn("P4ssw0rd", canal.url)

        # 2. La resolución devuelve la URL real, con credenciales.
        resuelta = resolve_channel_url(canal.url, get_store())
        self.assertEqual(resuelta, CAMARA)

        # 3. Y la referencia opaca, por sí sola, **no** llega al reproductor:
        #    el prefijo no es un esquema y la política lo rechaza. Ésta es la
        #    garantía de que un `str(channel)` filtrado no se convierta en una
        #    orden de reproducción.
        with self.assertRaises(Exception):
            command_for(canal, "mpv", player_path="/usr/bin/mpv")

        # 4. Sólo la reconstrucción de la app, marcada como tal, construye argv.
        with url_de_camara_resuelta(resuelta):
            argv = command_for(
                replace(canal, url=resuelta), "mpv", player_path="/usr/bin/mpv"
            )
        self.assertEqual(argv[-2:], ["--", CAMARA])

        # 5. Y fuera del bloque vuelve a rechazarse: la marca no sobrevive.
        with self.assertRaises(Exception):
            command_for(
                replace(canal, url=resuelta), "mpv", player_path="/usr/bin/mpv"
            )

        # 6. La referencia opaca tampoco pasa la política por otro camino.
        with self.assertRaises(Exception):
            validate_url(canal.url, PURPOSE_STREAM)

    def test_play_channel_reproduce(self) -> None:
        lista = parse_text(LISTA, source="http://p/l.m3u", name="Camaras")
        canal = lista.channels[0]
        app = _App()
        with mock.patch(
            "thetvview.player.core.subprocess.Popen", return_value=_Proc()
        ) as popen, mock.patch(
            "thetvview.ui.screens.ChannelHealthMonitor", return_value=mock.Mock()
        ):
            ui_screens.play_channel(app, canal, player_name="mpv")
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-2:], ["--", CAMARA])
        self.assertEqual(len(app.stack), 1)
        # El `Channel` que se muestra ya lleva la URL buena, y el original
        # sigue siendo el opaco: el dominio no se toca.
        self.assertEqual(app.stack[0].channel.url, CAMARA)
        self.assertTrue(canal.url.startswith(PREFIX))

    def test_sin_credenciales_registradas_no_reproduce_con_un_mensaje(self) -> None:
        # Una referencia huérfana (almacén limpio, p. ej. otro perfil del SO)
        # tiene que fallar con un modal que diga qué hacer, no con una
        # excepción ni con un `rtsp://camara/…` sin contraseña.
        lista = parse_text(LISTA, source="http://p/l.m3u", name="Camaras")
        canal = lista.channels[0]
        reset_store()  # como si el keyring no tuviera la clave
        app = _App()
        with mock.patch("thetvview.player.core.subprocess.Popen") as popen:
            ui_screens.play_channel(app, canal, player_name="mpv")
        self.assertEqual(popen.call_count, 0)
        self.assertTrue(app.status.was_error)
        self.assertIn("cámaras", app.status.last.lower())
        self.assertNotIn("P4ssw0rd", app.status.last)

    def test_favorito_y_reciente_siguen_siendo_reproducibles(self) -> None:
        # El recorrido que importa para el usuario: marcar la cámara como
        # favorita, releerla del disco y reproducirla.
        lista = parse_text(LISTA, source="http://p/l.m3u", name="Camaras")
        canal = lista.channels[0]
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "favorites.json"
            fav = FavoritesManager(ruta)
            fav.toggle(canal)
            # Lo releído del disco es lo que se reproduciría en otra sesión.
            guardado = fav.load()[0]
            self.assertEqual(guardado.url, canal.url)
            self.assertEqual(
                resolve_channel_url(guardado.url, get_store()), CAMARA
            )
            texto = ruta.read_text(encoding="utf-8")
            self.assertNotIn("P4ssw0rd", texto)
            self.assertNotIn("192.168.1.9", texto)

    def test_el_json_del_favorito_se_puede_releer(self) -> None:
        lista = parse_text(LISTA, source="http://p/l.m3u", name="Camaras")
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "favorites.json"
            FavoritesManager(ruta).toggle(lista.channels[0])
            datos = json.loads(ruta.read_text(encoding="utf-8"))
            self.assertEqual(Channel(**datos[0]).url, lista.channels[0].url)


class TestElRestoDeLaListaNoSeToca(unittest.TestCase):
    """Un solo cambio de parseo no puede romper lo que ya funcionaba."""

    LISTA_MIXTA = f"""#EXTM3U
#EXTINF:-1,Camara
{CAMARA}
#EXTINF:-1,HLS
http://proveedor.test/live/x.m3u8?token=abc123
#EXTINF:-1,Xtream
xtream://Panel/live/101.ts
"""

    def test_solo_la_linea_de_camara_cambia(self) -> None:
        lista = parse_text(self.LISTA_MIXTA, source="http://p/l.m3u", name="Mix")
        camara, hls, xtream = lista.channels
        self.assertTrue(camara.url.startswith(PREFIX))
        self.assertEqual(hls.url, "http://proveedor.test/live/x.m3u8?token=abc123")
        self.assertEqual(xtream.url, "xtream://Panel/live/101.ts")

    def test_la_lista_sigue_teniendo_tantos_canales(self) -> None:
        lista = parse_text(self.LISTA_MIXTA, source="http://p/l.m3u", name="Mix")
        self.assertEqual(len(lista.channels), 3)
        self.assertEqual([c.name for c in lista.channels], ["Camara", "HLS", "Xtream"])


class TestElAvisoDeCamaras(unittest.TestCase):
    """H11: al abrir una lista con cámaras, el usuario se entera del flujo.

    Es el único momento en que se puede decir «tu contraseña ya no está aquí»,
    porque es cuando se acaba de guardar. Si este aviso falta, el usuario ve
    un M3U distinto al que escribió y no tiene ni idea de por qué.
    """

    class _AppAviso:
        def __init__(self) -> None:
            self.status = _Status()
            self.prefs = _PrefsStub()
            self.stack: list = []
            self.epg = None
            self.notices: list[tuple[str, str]] = []
            self.playlists = mock.Mock()
            self.favorites = mock.Mock()
            self.favorites.favorite_urls.return_value = set()

        def push(self, screen) -> None:
            self.stack.append(screen)

        def body_height(self) -> int:
            return 10

        def _show_notice(self, title: str, message: str) -> None:
            self.notices.append((title, message))

        def cached_playlist(self, source: str):  # noqa: ANN201
            return getattr(self, "_playlist", None)

        def remember_playlist(self, source: str, playlist) -> None:
            self._playlist = playlist

    def setUp(self) -> None:
        self._previo = os.environ.get("THETVVIEW_SECRET_STORE")
        os.environ["THETVVIEW_SECRET_STORE"] = "memory"
        reset_store()
        self.addCleanup(self._restaurar)

    def _restaurar(self) -> None:
        reset_store()
        if self._previo is None:
            os.environ.pop("THETVVIEW_SECRET_STORE", None)
        else:
            os.environ["THETVVIEW_SECRET_STORE"] = self._previo

    def _entrada(self, source: str = "http://p/l.m3u"):  # noqa: ANN202
        from thetvview.playlist_manager import PlaylistEntry

        return PlaylistEntry(name="Camaras", source=source, kind="m3u")

    def test_abrir_una_lista_con_camaras_lo_explica(self) -> None:
        app = self._AppAviso()
        with mock.patch("thetvview.ui.app.load_playlist_source",
                        return_value=parse_text(LISTA, source="http://p/l.m3u",
                                               name="Camaras")):
            ui_screens.open_playlist(app, self._entrada())
        self.assertEqual(len(app.notices), 1)
        titulo, texto = app.notices[0]
        self.assertIn("Cámaras", titulo)
        self.assertIn("rtsp://", texto)
        # Y sin filtrar nada: ni la clave, ni el usuario, ni el host.
        for secreto in ("P4ssw0rd", "admin", "192.168.1.9", "stream1"):
            self.assertNotIn(secreto, texto)

    def test_una_lista_sin_camaras_no_dice_nada(self) -> None:
        app = self._AppAviso()
        lista = parse_text(
            '#EXTM3U\n#EXTINF:-1,HLS\nhttp://p/live/x.m3u8\n',
            source="http://p/l.m3u", name="Normal",
        )
        with mock.patch("thetvview.ui.app.load_playlist_source", return_value=lista):
            ui_screens.open_playlist(app, self._entrada())
        self.assertEqual(app.notices, [])

    def test_no_se_repite_en_la_misma_sesion(self) -> None:
        # Un aviso que sale cada vez que abres la lista deja de leerse.
        app = self._AppAviso()
        lista = parse_text(LISTA, source="http://p/l.m3u", name="Camaras")
        with mock.patch("thetvview.ui.app.load_playlist_source", return_value=lista):
            ui_screens.open_playlist(app, self._entrada())
            app.stack.clear()
            ui_screens.open_playlist(app, self._entrada())
        self.assertEqual(len(app.notices), 1)

    def test_el_texto_dice_el_numero_de_camaras(self) -> None:
        from thetvview.cam_ref import explain_camera_flow

        texto = explain_camera_flow(3)
        self.assertIn("3 cámaras", texto)
        self.assertIn("1 cámara", explain_camera_flow(1))


class TestMemoriaDelAlmacen(unittest.TestCase):
    """Un almacén roto no puede tumbar la lista, pero sí esa cámara."""

    def test_almacen_que_falla_no_tumba_el_parseo(self) -> None:
        class _Roto:
            def get_password(self, key: str) -> str | None:
                return None

            def set_password(self, key: str, value: str) -> None:
                raise OSError("keyring bloqueado")

        with mock.patch(
            "thetvview.security.secrets.get_store", return_value=_Roto()
        ):
            lista = parse_text(LISTA, source="http://p/l.m3u", name="Camaras")
        # El canal existe y es opaco; lo que falla es reproducirlo, y eso lo
        # dice el modal. Perder una cámara es mejor que perder la lista.
        self.assertEqual(len(lista.channels), 1)
        self.assertTrue(lista.channels[0].url.startswith(PREFIX))

    def test_la_credencial_se_reutiliza_entre_canales_iguales(self) -> None:
        # Dos líneas con la misma URL: una sola entrada en el almacén. Si no,
        # se acumularían secretos huérfanos en cada recarga de la lista.
        store = MemorySecretStore()
        texto = LISTA + f'#EXTINF:-1,El mismo\n{CAMARA}\n'
        with mock.patch(
            "thetvview.security.secrets.get_store", return_value=store
        ):
            lista = parse_text(texto, source="http://p/l.m3u", name="Camaras")
        self.assertEqual(lista.channels[0].url, lista.channels[1].url)
        self.assertEqual(len(store._data), 1)