"""Tests de la referencia opaca de cámara (SDD-M Fase 5, AC-007).

El problema que resuelve: una lista de cámaras trae
``rtsp://admin:clave@192.168.1.9:554/stream1`` y, sin más, esa contraseña acaba
en ``data/recents.json``, en ``data/favorites.json`` y en la barra de estado.

El mecanismo es el mismo que ya usa Xtream (``xtream://``), y estos tests
comprueban las dos mitades: que la referencia **no** lleva nada, y que
resolverla devuelve la URL buena.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from thetvview.cam_ref import (
    PREFIX,
    CamCredentials,
    CamRef,
    MissingCamCredentialsError,
    build_cam_ref,
    camera_id_of,
    is_cam_ref,
    registrar_camara,
    split_credentials,
)
from thetvview.favorites import FavoritesManager
from thetvview.m3u_parser import parse_text
from thetvview.models import Channel
from thetvview.recents import RecentsManager
from thetvview.security.redaction import (
    contains_embedded_login,
    needs_redaction_for_storage,
    redact_text,
)
from thetvview.security.secrets import MemorySecretStore, get_store, reset_store
from thetvview.stream_ref import is_opaque_ref, resolve_channel_url

CAMARA = "rtsp://admin:P4ssw0rd@192.168.1.9:554/stream1"
CLAVE = "P4ssw0rd"


class TestSepararCredenciales(unittest.TestCase):
    def test_separa_el_terceto(self) -> None:
        self.assertEqual(
            split_credentials(CAMARA),
            ("admin", CLAVE, "rtsp://192.168.1.9:554/stream1"),
        )

    def test_solo_rtsp(self) -> None:
        # Una URL HTTP con token o userinfo no es una cámara: se deja intacta,
        # o las listas IPTV dejarían de reproducirse.
        for url in (
            "http://u:p@x/a.m3u8",
            "https://x/a.m3u8?token=abc",
            "rtmp://u:p@x/live/a",
            "udp://239.0.0.1:5000",
            "",
            None,
        ):
            with self.subTest(url=url):
                self.assertIsNone(split_credentials(url))  # type: ignore[arg-type]

    def test_rtsp_sin_credenciales_no_toca(self) -> None:
        self.assertIsNone(split_credentials("rtsp://cam.local:554/stream"))

    def test_url_malformada_no_rompe(self) -> None:
        self.assertIsNone(split_credentials("rtsp://user:pass@[::1/live"))

    def test_contrasena_con_simbolos_especiales(self) -> None:
        # `@` y `:` en la clave rompen un netloc si se concatena en crudo.
        partes = split_credentials("rtsp://u:a:b@c@cam.local/live")
        assert partes is not None
        self.assertEqual(partes[1], "a:b@c")
        self.assertEqual(partes[2], "rtsp://cam.local/live")


class TestIdentificador(unittest.TestCase):
    def test_es_estable_y_no_revela_el_host(self) -> None:
        ident = camera_id_of("rtsp://192.168.1.9:554/stream1")
        self.assertEqual(len(ident), 32)
        self.assertNotIn("192.168.1.9", ident)
        self.assertNotIn("stream1", ident)

    def test_no_depende_de_la_contrasena(self) -> None:
        # Si dependiera, cambiar la clave en el M3U crearía una cámara
        # distinta y la anterior quedaría huérfana en el almacén.
        self.assertEqual(
            camera_id_of("rtsp://192.168.1.9:554/stream1"),
            camera_id_of("rtsp://otro:distinta@192.168.1.9:554/stream1"),
        )

    def test_camaras_distintas_dan_identificadores_distintos(self) -> None:
        self.assertNotEqual(
            camera_id_of("rtsp://a.local:554/stream1"),
            camera_id_of("rtsp://a.local:554/stream2"),
        )


class TestReferenciaOpaca(unittest.TestCase):
    def test_roundtrip(self) -> None:
        ref = CamRef("Cámaras del jardín", "abc123")
        self.assertEqual(ref.to_opaque(), f"{PREFIX}C%C3%A1maras%20del%20jard%C3%ADn/abc123")
        self.assertEqual(CamRef.parse(ref.to_opaque()), ref)

    def test_no_lleva_nada_sensible(self) -> None:
        preparado = build_cam_ref("Cámaras", CAMARA)
        assert preparado is not None
        referencia, _ = preparado
        for secreto in (CLAVE, "192.168.1.9", "stream1", "admin"):
            with self.subTest(secreto=secreto):
                self.assertNotIn(secreto, referencia)

    def test_referencia_corrupta_no_se_resuelve(self) -> None:
        for mala in (f"{PREFIX}solo-un-segmento", f"{PREFIX}a/b/c", f"{PREFIX}/abc"):
            with self.subTest(url=mala):
                self.assertIsNone(CamRef.parse(mala))

    def test_es_opaca_para_favoritos_y_recientes(self) -> None:
        preparado = build_cam_ref("Cámaras", CAMARA)
        assert preparado is not None
        self.assertTrue(is_cam_ref(preparado[0]))
        self.assertTrue(is_opaque_ref(preparado[0]))
        # Y no necesita redacción: no hay nada que limpiar.
        self.assertFalse(needs_redaction_for_storage(preparado[0]))
        self.assertFalse(contains_embedded_login(preparado[0]))

    def test_prefijo_no_es_un_esquema_real(self) -> None:
        # `rtsps://` habría colisionado con el esquema real de RTSP sobre TLS;
        # `ipcam://` no lo usa ningún protocolo.
        self.assertNotIn(PREFIX.rstrip(":/"), ("http", "rtsp", "rtsps", "rtmp", "udp"))


class TestRegistroYResolucion(unittest.TestCase):
    def test_id_y_vuelta(self) -> None:
        store = MemorySecretStore()
        preparado = build_cam_ref("Cámaras", CAMARA)
        assert preparado is not None
        referencia, _ = preparado
        # El `store` explícito evita depender del singleton del proceso.
        self.assertEqual(registrar_camara("Cámaras", CAMARA, store), referencia)
        self.assertEqual(resolve_channel_url(referencia, store), CAMARA)

    def test_sin_credenciales_da_error_explicito(self) -> None:
        # El mensaje dice qué hacer y no lleva ni la URL ni el usuario.
        store = MemorySecretStore()
        preparado = build_cam_ref("Cámaras", CAMARA)
        assert preparado is not None
        with self.assertRaises(MissingCamCredentialsError) as ctx:
            resolve_channel_url(preparado[0], store)
        mensaje = str(ctx.exception)
        self.assertNotIn(CLAVE, mensaje)
        self.assertNotIn("192.168.1.9", mensaje)
        self.assertIn("carga", mensaje.lower())

    def test_referencia_corrupta_no_llega_al_reproductor(self) -> None:
        store = MemorySecretStore()
        with self.assertRaises(MissingCamCredentialsError):
            resolve_channel_url(f"{PREFIX}basura", store)

    def test_almacen_roto_no_rompe(self) -> None:
        class _Roto:
            def get_password(self, key: str) -> str:
                raise OSError("keyring bloqueado")

        preparado = build_cam_ref("Cámaras", CAMARA)
        assert preparado is not None
        with self.assertRaises(MissingCamCredentialsError):
            resolve_channel_url(preparado[0], _Roto())

    def test_blob_dañado_no_rompe(self) -> None:
        store = MemorySecretStore()
        preparado = build_cam_ref("Cámaras", CAMARA)
        assert preparado is not None
        ref = CamRef.parse(preparado[0])
        assert ref is not None
        from thetvview.cam_ref import cam_key

        store.set_password(cam_key(ref.source_name, ref.camera_id), "no soy json")
        with self.assertRaises(MissingCamCredentialsError):
            resolve_channel_url(preparado[0], store)

    def test_almacen_sin_get_password_da_error(self) -> None:
        preparado = build_cam_ref("Cámaras", CAMARA)
        assert preparado is not None
        with self.assertRaises(MissingCamCredentialsError):
            resolve_channel_url(preparado[0], object())


class TestEnElParserM3U(unittest.TestCase):
    LISTA = f"""#EXTM3U
#EXTINF:-1 tvg-id="cam1" group-title="Cámaras",Portón
{CAMARA}
#EXTINF:-1,Canal normal
http://proveedor.test/live/x.m3u8?token=abc123
"""

    def setUp(self) -> None:
        # El parser usa el almacén activo del proceso; aquí se fija a memoria
        # para que el test no dependa del keyring del SO ni lo ensucie.
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

    def test_la_linea_de_camara_se_convierte_en_referencia(self) -> None:
        lista = parse_text(self.LISTA, source="http://p/l.m3u", name="Cámaras")
        self.assertTrue(lista.channels[0].url.startswith(PREFIX))

    def test_el_resto_de_la_lista_no_se_toca(self) -> None:
        # El `?token=` de una lista IPTV tiene que sobrevivir intacto: sin él
        # la lista no se reproduce y no es un secreto de la app.
        lista = parse_text(self.LISTA, source="http://p/l.m3u", name="Cámaras")
        self.assertEqual(
            lista.channels[1].url, "http://proveedor.test/live/x.m3u8?token=abc123"
        )

    def test_el_nombre_y_el_grupo_se_conservan(self) -> None:
        lista = parse_text(self.LISTA, source="http://p/l.m3u", name="Cámaras")
        self.assertEqual(lista.channels[0].name, "Portón")
        self.assertEqual(lista.channels[0].group, "Cámaras")

    def test_el_canal_ya_no_tiene_credenciales(self) -> None:
        lista = parse_text(self.LISTA, source="http://p/l.m3u", name="Cámaras")
        url = lista.channels[0].url
        self.assertFalse(contains_embedded_login(url))
        self.assertNotIn(CLAVE, url)


class TestPersistenciaEnDisco(unittest.TestCase):
    """Favoritos y recientes funcionan con cámaras: la referencia es segura."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
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
        self._tmp.cleanup()

    @property
    def store(self):
        return get_store()

    def _canal(self) -> Channel:
        lista = parse_text(
            TestEnElParserM3U.LISTA, source="http://p/l.m3u", name="Cámaras"
        )
        return lista.channels[0]

    def test_favorito_no_lleva_la_contrasena(self) -> None:
        fav = FavoritesManager(self.dir / "favorites.json")
        canal = self._canal()
        fav.toggle(canal)
        texto = (self.dir / "favorites.json").read_text(encoding="utf-8")
        self.assertNotIn(CLAVE, texto)
        self.assertNotIn("192.168.1.9", texto)
        self.assertIn(PREFIX, texto)
        # Y sigue siendo reproducible: se recuperan las credenciales del almacén.
        self.assertEqual(resolve_channel_url(canal.url, self.store), CAMARA)
        self.assertTrue(fav.is_favorite(canal))

    def test_reciente_no_lleva_la_contrasena(self) -> None:
        rec = RecentsManager(self.dir / "recents.json")
        canal = self._canal()
        rec.push(canal, "mpv")
        texto = (self.dir / "recents.json").read_text(encoding="utf-8")
        self.assertNotIn(CLAVE, texto)
        self.assertNotIn("192.168.1.9", texto)

    def test_el_json_es_valido(self) -> None:
        # Un favorito con una referencia opaca tiene que seguir siendo un
        # JSON legible: nada de escapes raros que rompan la app al releerlo.
        fav = FavoritesManager(self.dir / "favorites.json")
        fav.toggle(self._canal())
        datos = json.loads((self.dir / "favorites.json").read_text(encoding="utf-8"))
        self.assertTrue(datos[0]["url"].startswith(PREFIX))


class TestRedaccionDelTextoResuelto(unittest.TestCase):
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

    def test_la_url_resuelta_se_puede_mostrar(self) -> None:
        preparado = build_cam_ref("Cámaras", CAMARA)
        assert preparado is not None
        registrar_camara("Cámaras", CAMARA)
        resuelta = resolve_channel_url(preparado[0], get_store())
        self.assertEqual(redact_text(resuelta), "rtsp://***@192.168.1.9:554/stream1")


class TestCredencialesDataclass(unittest.TestCase):
    def test_blob_ida_y_vuelta(self) -> None:
        cred = CamCredentials(location="rtsp://a:554/s", username="u", password="p")
        self.assertEqual(CamCredentials.from_blob(cred.to_blob()), cred)

    def test_blob_vacio_o_roto(self) -> None:
        for blob in ("", "no json", "[]", '{"location": ""}', "null"):
            with self.subTest(blob=blob):
                self.assertIsNone(CamCredentials.from_blob(blob))