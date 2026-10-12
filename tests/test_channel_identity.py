"""Identidad de canal y actualización de listas (CHAN-001…CHAN-004, SDD §9).

Lo que estos tests protegen, en una frase cada uno:

- un favorito **no** se pierde porque el proveedor rotó la URL (CHAN-002);
- dos canales con el mismo nombre y distinto `tvg-id` **no** se fusionan
  (CHAN-001/005);
- una lista vacía o truncada **no** destruye la buena (CHAN-003);
- los cambios entre dos versiones **se pueden resumir** (CHAN-004);
- los encabezados condicionales sólo se usan cuando están presentes, y un 304
  **no** saltan la validación de integridad (CHAN-004/009).

Sin red: las descargas se doblan. Sin reproductor. Sin curses.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.channel_identity import (
    AmbiguousIdentity,
    ChannelIdentity,
    ChangeKind,
    Confidence,
    IdentityKind,
    channel_identity,
    identity_key,
    normalize_name,
    normalize_url,
    reconcile,
    resumen_para_ui,
)
from thetvview.favorites import FavoritesManager
from thetvview.m3u_parser import (
    MIN_RATIO,
    _headers_condicionales,
    _leer_meta,
    _meta_de_respuesta,
    _meta_path_for,
    load_url,
    resumir_cambios,
    validate_playlist,
)
from thetvview.models import Channel, Playlist


def ch(
    nombre: str,
    url: str = "",
    tvg_id: str = "",
    group: str = "",
    radio: bool = False,
) -> Channel:
    return Channel(
        name=nombre,
        url=url or f"http://x/{nombre}",
        tvg_id=tvg_id or None,
        group=group or None,
        radio=radio,
    )


def pl(canalas: list[Channel]) -> Playlist:
    return Playlist(name="p", channels=list(canalas))


# ---------------------------------------------------------------------------
# CHAN-001 · identidad y normalización conservadora
# ---------------------------------------------------------------------------


class TestNormalizacion(unittest.TestCase):
    def test_el_nombre_solo_se_recorta_y_se_pasa_a_minusculas(self) -> None:
        self.assertEqual(normalize_name("  Canal   Uno "), "canal uno")

    def test_no_se_quitan_numeros_ni_calidad(self) -> None:
        """FR-CHAN-004: `HD`, `1080p` y el idioma son parte del nombre."""
        self.assertNotEqual(normalize_name("Canal 1 HD"), normalize_name("Canal 1"))
        self.assertNotEqual(normalize_name("Canal 1 HD"), normalize_name("Canal 1 SD"))

    def test_no_se_quitan_ni_la_region_ni_el_idioma(self) -> None:
        self.assertNotEqual(normalize_name("Fox HD"), normalize_name("Fox Latam HD"))
        self.assertNotEqual(normalize_name("CNN ES"), normalize_name("CNN"))

    def test_no_se_quitan_los_acentos(self) -> None:
        self.assertNotEqual(normalize_name("Televisión"), normalize_name("Television"))

    def test_un_nombre_vacio_da_clave_vacia(self) -> None:
        self.assertEqual(normalize_name(""), "")
        self.assertEqual(normalize_name(None), "")


class TestNormalizacionDeUrl(unittest.TestCase):
    def test_esquema_host_y_puerto_por_defecto_se_normalizan(self) -> None:
        self.assertEqual(
            normalize_url("HTTP://Ejemplo.COM:80/live/1.ts"),
            normalize_url("http://ejemplo.com/live/1.ts"),
        )

    def test_un_puerto_no_estandar_se_conserva(self) -> None:
        self.assertIn(":8080", normalize_url("http://ejemplo.com:8080/a"))

    def test_la_barra_final_no_cambia_la_identidad(self) -> None:
        self.assertEqual(normalize_url("http://x/live/1/"), normalize_url("http://x/live/1"))

    def test_el_fragmento_no_viaja_al_servidor(self) -> None:
        self.assertEqual(normalize_url("http://x/a#frag"), normalize_url("http://x/a"))

    def test_la_query_se_conserva_entera_y_en_orden(self) -> None:
        """Reordenar la query puede cambiar lo que el servidor devuelve."""
        a = normalize_url("http://x/a?b=2&a=1")
        b = normalize_url("http://x/a?a=1&b=2")
        self.assertNotEqual(a, b)

    def test_las_credenciales_no_se_quedan_en_la_clave(self) -> None:
        clave = normalize_url("rtsp://alice:secreto@cam.local/live")
        self.assertNotIn("secreto", clave)
        self.assertIn("***", clave)

    def test_una_referencia_opaca_se_compara_redactada(self) -> None:
        """`xtream://` no es una URL que se pueda partir, pero sí lleva credencial."""
        clave = normalize_url("xtream://usuario@servidor/live/1.ts")
        self.assertNotIn("usuario", clave)
        self.assertTrue(clave.startswith("xtream://"))


class TestPrecedenciaDeIdentidad(unittest.TestCase):
    def test_el_tvg_id_manda(self) -> None:
        ident = channel_identity(ch("Canal", "http://x/1.ts", tvg_id="c1"))
        self.assertIs(ident.kind, IdentityKind.TVG_ID)
        self.assertIs(ident.confidence, Confidence.ALTA)
        self.assertTrue(ident.fuerte)

    def test_sin_tvg_id_manda_la_url(self) -> None:
        ident = channel_identity(ch("Canal", "http://x/1.ts"))
        self.assertIs(ident.kind, IdentityKind.URL)
        self.assertIs(ident.confidence, Confidence.MEDIA)
        self.assertFalse(ident.fuerte)

    def test_sin_url_manda_el_nombre_con_el_grupo(self) -> None:
        ident = channel_identity(Channel(name="Canal", url="", group="Noticias"))
        self.assertIs(ident.kind, IdentityKind.NAME)
        self.assertIn("noticias", ident.value)
        self.assertIs(ident.confidence, Confidence.BAJA)

    def test_un_canal_sin_nada_no_tiene_identidad(self) -> None:
        ident = channel_identity(Channel(name="", url=""))
        self.assertFalse(ident.resoluble)
        self.assertEqual(ident.clave, "")
        self.assertEqual(identity_key(Channel(name="", url="")), "")

    def test_cambiar_la_url_no_cambia_la_identidad_con_tvg_id(self) -> None:
        antes = identity_key(ch("Canal", "http://x/1.ts", tvg_id="c1"))
        despues = identity_key(ch("Canal HD", "http://x/999.ts", tvg_id="c1"))
        self.assertEqual(antes, despues)

    def test_la_procedencia_forma_parte_de_la_clave(self) -> None:
        a = identity_key(ch("Canal", tvg_id="c1"), "m3u|Lista A|http://a")
        b = identity_key(ch("Canal", tvg_id="c1"), "m3u|Lista B|http://b")
        self.assertNotEqual(a, b)


class TestGuardarYLeerIdentidad(unittest.TestCase):
    def test_ida_y_vuelta(self) -> None:
        ident = ChannelIdentity.crea("fuente", IdentityKind.TVG_ID, "c1")
        leida = ChannelIdentity.desde_guardado(ident.para_guardar())
        self.assertIsNotNone(leida)
        assert leida is not None
        self.assertEqual(leida.value, "c1")
        self.assertIs(leida.kind, IdentityKind.TVG_ID)

    def test_un_campo_desconocido_no_revienta(self) -> None:
        """Un `favorites.json` de una versión más nueva no puede impedir cargar."""
        self.assertIsNone(ChannelIdentity.desde_guardado({"kind": "magia"}))
        self.assertIsNone(ChannelIdentity.desde_guardado({"value": "x"}))
        self.assertIsNone(ChannelIdentity.desde_guardado("no soy un dict"))
        self.assertIsNone(ChannelIdentity.desde_guardado(None))

    def test_el_informe_no_lleva_secretos(self) -> None:
        ident = ChannelIdentity.crea("f", IdentityKind.URL, "http://x/a?password=secreto")
        self.assertNotIn("secreto", ident.para_informe()["valor"])


# ---------------------------------------------------------------------------
# CHAN-001/005 · reconciliación sin fusiones
# ---------------------------------------------------------------------------


class TestReconcile(unittest.TestCase):
    def test_detecta_lo_nuevo(self) -> None:
        rep = reconcile([], [ch("Nuevo", tvg_id="n1")])
        self.assertEqual(len(rep.added), 1)
        self.assertIs(rep.added[0].kind, ChangeKind.ADDED)
        self.assertTrue(rep.hay_cambios)

    def test_detecta_lo_que_se_va(self) -> None:
        rep = reconcile([ch("Viejo", tvg_id="v1")], [])
        self.assertEqual(len(rep.removed), 1)
        self.assertEqual(len(rep.orphans), 1)

    def test_detecta_lo_modificado(self) -> None:
        rep = reconcile(
            [ch("Canal", "http://x/1.ts", tvg_id="c1")],
            [ch("Canal", "http://x/999.ts", tvg_id="c1")],
        )
        self.assertEqual(len(rep.changed), 1)
        self.assertEqual(rep.changed[0].campo, "url")

    def test_una_url_nueva_no_es_un_canal_nuevo(self) -> None:
        """Sin `tvg-id` tampoco: la URL normalizada es la identidad técnica."""
        rep = reconcile(
            [ch("Canal", "http://x/live/1")],
            [ch("Canal", "http://x/live/1/")],
        )
        self.assertEqual(len(rep.unchanged), 1)
        self.assertEqual(len(rep.changed), 0)
        self.assertFalse(rep.hay_cambios)

    def test_un_cambio_de_url_real_si_cuenta_como_modificado(self) -> None:
        """Con `tvg-id` la URL es técnica: si cambia, el canal sigue siendo el."""
        rep = reconcile(
            [ch("Canal", "http://x/live/1.ts", tvg_id="c1")],
            [ch("Canal", "http://x/live/9.ts", tvg_id="c1")],
        )
        self.assertEqual(len(rep.changed), 1)
        self.assertEqual(rep.changed[0].campo, "url")

    def test_sin_tvg_id_cambiar_la_url_es_otro_canal(self) -> None:
        """La URL normalizada **es** la identidad técnica: si cambia, es otro."""
        rep = reconcile(
            [ch("Canal", "http://x/live/1.ts")],
            [ch("Canal", "http://x/live/9.ts")],
        )
        self.assertEqual(len(rep.removed), 1)
        self.assertEqual(len(rep.added), 1)
        self.assertEqual(len(rep.changed), 0)

    def test_cambiar_solo_el_logo_no_marca_modificado(self) -> None:
        """Un proveedor que renueva logos no ha cambiado los canales."""
        rep = reconcile(
            [
                Channel(
                    name="Canal",
                    url="http://x/1.ts",
                    tvg_id="c",
                    tvg_logo="http://x/logo.png",
                )
            ],
            [
                Channel(
                    name="Canal",
                    url="http://x/1.ts",
                    tvg_id="c",
                    tvg_logo="http://x/logo-v2.png",
                )
            ],
        )
        self.assertEqual(len(rep.unchanged), 1)

    def test_sin_cambios_no_dice_que_hubo_cambios(self) -> None:
        rep = reconcile([ch("Canal", tvg_id="c1")], [ch("Canal", tvg_id="c1")])
        self.assertFalse(rep.hay_cambios)
        self.assertEqual(len(rep.unchanged), 1)

    def test_mismo_nombre_distinto_tvg_id_no_se_fusionan(self) -> None:
        """FR-CHAN-001: el nombre no es identidad."""
        a = ch("Fox", "http://x/1.ts", tvg_id="fox.a")
        b = ch("Fox", "http://x/2.ts", tvg_id="fox.b")
        rep = reconcile([a], [a, b])
        self.assertEqual(len(rep.added), 1, "Fox B se habría fundido con Fox A")
        self.assertEqual(rep.added[0].after, "Fox")

    def test_homonimos_sin_identificador_se_marcan_ambiguos(self) -> None:
        rep = reconcile(
            [],
            [ch("CNN", "http://x/1.ts"), ch("CNN", "http://x/2.ts")],
        )
        self.assertEqual(len(rep.ambiguous), 1)
        self.assertEqual(rep.ambiguous[0].cuantos, 2)
        self.assertIn("no se pueden distinguir", rep.ambiguous[0].explicacion())

    def test_homonimos_todos_distinguibles_no_son_ambiguedad(self) -> None:
        rep = reconcile(
            [],
            [ch("CNN", "http://x/1.ts", tvg_id="a"), ch("CNN", "http://x/2.ts", tvg_id="b")],
        )
        self.assertEqual(rep.ambiguous, [])

    def test_un_canal_sin_identidad_no_rompe_la_reconciliacion(self) -> None:
        rep = reconcile([ch("Uno", tvg_id="u1")], [Channel(name="", url=""), ch("Uno", tvg_id="u1")])
        self.assertEqual(len(rep.unchanged), 1)

    def test_el_resumen_tiene_los_cuatro_numeros(self) -> None:
        rep = reconcile(
            [ch("A", tvg_id="a"), ch("B", tvg_id="b")],
            [ch("A", tvg_id="a"), ch("C", tvg_id="c")],
        )
        texto = rep.resumen()
        for numero in ("1 nuevos", "1 fuera", "1 sin cambios"):
            self.assertIn(numero, texto)

    def test_el_modal_avisa_de_huerfanos_y_ambiguedad(self) -> None:
        rep = reconcile([ch("Se fue", tvg_id="s")], [ch("CNN", "http://x/1"), ch("CNN", "http://x/2")])
        modal = rep.para_modal()
        self.assertIn("sin canal en la lista", modal)
        self.assertIn("no se han fusionado", modal)

    def test_el_resumen_para_ui_acota_las_listas(self) -> None:
        rep = reconcile([], [ch(f"C{i}", tvg_id=f"c{i}") for i in range(50)])
        lineas = resumen_para_ui(rep)
        self.assertTrue(any("y 30 más" in linea for linea in lineas))

    def test_el_informe_no_lleva_la_url_cruda(self) -> None:
        rep = reconcile(
            [], [ch("Canal", "http://user:pass@x/a.ts", tvg_id="c1")]
        )
        self.assertNotIn("pass", rep.added[0].after)


# ---------------------------------------------------------------------------
# CHAN-002 · favoritos: identidad en vez de URL
# ---------------------------------------------------------------------------


class TestFavoritosConIdentidad(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "favorites.json"
        self.mgr = FavoritesManager(self.path)

    def _leer_json(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_cambiar_la_url_no_rompe_el_favorito(self) -> None:
        """El gap que motivations todo este ticket."""
        self.mgr.toggle(ch("La 1", "http://p/live/1.ts", tvg_id="la1.es"))
        rotado = ch("La 1", "http://p/live/9999.ts", tvg_id="la1.es")
        self.assertTrue(self.mgr.is_favorite(rotado))

    def test_toggle_compara_por_url_exacta(self) -> None:
        """Pregunta y acción son dos cosas: `toggle` nunca adivina.

        Con la URL movida, `is_favorite` dice que sí (por identidad) pero
        `toggle` quita **exactamente** la línea que se le pasó. Si quitara por
        `tvg-id` borraría un favorito que sigue en pie.
        """
        self.mgr.toggle(ch("La 1", "http://p/live/1.ts", tvg_id="la1.es"))
        self.mgr.toggle(ch("La 1", "http://p/live/9999.ts", tvg_id="la1.es"))
        self.assertEqual(len(self.mgr.load()), 2)
        self.assertTrue(
            self.mgr.is_favorite(ch("La 1", "http://p/live/1.ts", tvg_id="la1.es"))
        )

    def test_una_referencia_opaca_no_se_confunde_por_tvg_id(self) -> None:
        """SDD Catch-up: directo y archivo son dos favoritos distintos."""
        directo = Channel(name="ESPN", url="xtream://u/p/live/7.ts", tvg_id="7")
        archivo = Channel(name="ESPN", url="xtream-ts://u/p/live/7.ts", tvg_id="7")
        self.mgr.toggle(directo)
        # Mismo `tvg-id`, así que por identidad parecería el mismo canal: pero en
        # Xtream la URL **es** el modo (directo o archivo) y son dos favoritos.
        self.assertFalse(self.mgr.is_favorite(archivo))
        self.mgr.toggle(archivo)
        self.assertEqual(len(self.mgr.load()), 2)
        # Y quitar el directo no toca el archivo.
        self.mgr.toggle(directo)
        self.assertEqual([c.url for c in self.mgr.load()], [archivo.url])

    def test_la_identidad_se_deduce_y_no_se_persiste(self) -> None:
        """El archivo no crece: la identidad sale del propio canal."""
        self.mgr.toggle(ch("La 1", "http://p/live/1.ts", tvg_id="la1.es"))
        entrada = self._leer_json()[0]
        self.assertNotIn("identity", entrada)
        self.assertEqual(entrada["tvg_id"], "la1.es")
        self.assertEqual(
            self.mgr.identities()["http://p/live/1.ts"],
            {"kind": "tvg-id", "value": "la1.es"},
        )

    def test_otro_canal_con_otro_tvg_id_no_se_confunde(self) -> None:
        self.mgr.toggle(ch("La 1", "http://p/live/1.ts", tvg_id="la1.es"))
        self.assertFalse(self.mgr.is_favorite(ch("La 2", "http://p/live/2.ts", tvg_id="la2.es")))

    def test_la_identidad_se_deduce_del_canal(self) -> None:
        self.mgr.toggle(ch("La 1", "http://p/live/1.ts", tvg_id="la1.es"))
        self.assertEqual(
            self.mgr.identities()["http://p/live/1.ts"],
            {"kind": "tvg-id", "value": "la1.es"},
        )

    def test_lectura_dual_un_archivo_sin_identity_funciona(self) -> None:
        """El archivo de un usuario anterior a esta mejora, tal cual."""
        self.path.write_text(
            json.dumps(
                [{"name": "La 1", "url": "http://p/live/1.ts", "tvg_id": None}]
            ),
            encoding="utf-8",
        )
        self.assertEqual(len(self.mgr.load()), 1)
        self.assertTrue(self.mgr.is_favorite(ch("La 1", "http://p/live/1.ts")))

    def test_una_identidad_ajena_no_impide_cargar(self) -> None:
        self.path.write_text(
            json.dumps(
                [
                    {
                        "name": "La 1",
                        "url": "http://p/live/1.ts",
                        "identity": {"kind": "inventado", "value": "x"},
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.assertEqual(len(self.mgr.load()), 1)

    def test_favorito_sin_tvg_id_queda_pendiente_de_confirmar(self) -> None:
        self.mgr.toggle(ch("Radio", "http://p/r/1.ts"))
        self.assertEqual(self.mgr.pending_confirmation(), ["http://p/r/1.ts"])

    def test_favorito_con_tvg_id_no_esta_pendiente(self) -> None:
        self.mgr.toggle(ch("La 1", "http://p/live/1.ts", tvg_id="la1.es"))
        self.assertEqual(self.mgr.pending_confirmation(), [])

    def test_sin_tvg_id_sigue_funcionando_por_url(self) -> None:
        self.mgr.toggle(ch("Radio", "http://p/r/1.ts"))
        self.assertTrue(self.mgr.is_favorite(ch("Radio", "http://p/r/1.ts")))
        self.assertFalse(self.mgr.is_favorite(ch("Radio", "http://p/r/2.ts")))

    def test_se_hace_copia_bak_antes_de_escribir(self) -> None:
        self.mgr.toggle(ch("Uno", "http://p/1.ts", tvg_id="u1"))
        self.mgr.toggle(ch("Dos", "http://p/2.ts", tvg_id="d1"))
        copias = list(self.path.parent.glob(".favorites.json.bak-*"))
        self.assertEqual(len(copias), 1)
        self.assertIn("Uno", copias[0].read_text(encoding="utf-8"))

    def test_el_bak_permite_rollback(self) -> None:
        self.mgr.toggle(ch("Uno", "http://p/1.ts", tvg_id="u1"))
        self.mgr.toggle(ch("Dos", "http://p/2.ts", tvg_id="d1"))
        bak = next(self.path.parent.glob(".favorites.json.bak-*"))
        restaurado = json.loads(bak.read_text(encoding="utf-8"))
        self.assertEqual([c["name"] for c in restaurado], ["Uno"])
        # Y la app sabe recuperar desde el backup sin intervención del usuario.
        self.assertEqual(
            [c.name for c in FavoritesManager(self.path).load()], ["Uno", "Dos"]
        )

    def test_quitar_tambien_quita_la_identidad(self) -> None:
        self.mgr.toggle(ch("Uno", "http://p/1.ts", tvg_id="u1"))
        self.mgr.toggle(ch("Uno", "http://p/1.ts", tvg_id="u1"))
        self.assertEqual(self.mgr.identities(), {})

    def test_cada_entrada_sigue_siendo_un_channel(self) -> None:
        """`Channel(**entrada)` es un contrato del archivo, no se rompe."""
        self.mgr.toggle(ch("Uno", "http://p/1.ts", tvg_id="u1"))
        self.mgr.toggle(ch("Radio", "http://p/r.ts", radio=True))
        for entrada in self._leer_json():
            with self.subTest(entrada=entrada["url"]):
                self.assertEqual(Channel(**entrada).url, entrada["url"])

    def test_un_archivo_con_identity_de_una_version_intermedia_carga(self) -> None:
        """Si alguien guardó el campo experimental, se ignora en vez de romper."""
        self.path.write_text(
            json.dumps(
                [
                    {
                        "name": "La 1",
                        "url": "http://p/live/1.ts",
                        "tvg_id": "la1.es",
                        "identity": {"kind": "tvg-id", "value": "la1.es"},
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.assertEqual(len(self.mgr.load()), 1)
        self.assertTrue(self.mgr.is_favorite(ch("La 1", "http://p/live/9.ts", tvg_id="la1.es")))

    def test_abrir_la_app_no_reescribe_un_favorites_correcto(self) -> None:
        self.mgr.toggle(ch("Uno", "http://p/1.ts", tvg_id="u1"))
        antes = self.path.stat().st_mtime_ns
        FavoritesManager(self.path).load()
        self.assertEqual(self.path.stat().st_mtime_ns, antes)


# ---------------------------------------------------------------------------
# CHAN-003 · validar antes de sustituir
# ---------------------------------------------------------------------------


class TestValidacionDeLista(unittest.TestCase):
    def test_una_lista_vacia_no_pasa(self) -> None:
        v = validate_playlist(pl([]), pl([ch("A", tvg_id="a")]))
        self.assertFalse(v.ok)
        self.assertTrue(v.vacia)
        self.assertIn("llegó vacía", v.motivo)

    def test_una_lista_truncada_no_pasa(self) -> None:
        previa = pl([ch(str(i), tvg_id=str(i)) for i in range(100)])
        nueva = pl([ch(str(i), tvg_id=str(i)) for i in range(10)])
        v = validate_playlist(nueva, previa)
        self.assertFalse(v.ok)
        self.assertLess(v.proporcion, MIN_RATIO)
        self.assertIn("parece incompleta", v.motivo)

    def test_una_lista_un_poco_mas_corta_si_pasa(self) -> None:
        previa = pl([ch(str(i), tvg_id=str(i)) for i in range(100)])
        nueva = pl([ch(str(i), tvg_id=str(i)) for i in range(80)])
        self.assertTrue(validate_playlist(nueva, previa).ok)

    def test_sin_lista_anterior_cualquier_lista_no_vacia_pasa(self) -> None:
        self.assertTrue(validate_playlist(pl([ch("A", tvg_id="a")]), None).ok)

    def test_la_lista_nueva_vacia_sin_anterior_no_pasa(self) -> None:
        self.assertFalse(validate_playlist(pl([]), None).ok)

    def test_el_umbral_es_configurable(self) -> None:
        previa = pl([ch(str(i), tvg_id=str(i)) for i in range(10)])
        nueva = pl([ch(str(i), tvg_id=str(i)) for i in range(2)])
        self.assertFalse(validate_playlist(nueva, previa).ok)
        self.assertTrue(validate_playlist(nueva, previa, min_ratio=0.1).ok)

    def test_el_mensaje_dice_cuantos_habia(self) -> None:
        v = validate_playlist(pl([]), pl([ch("A", tvg_id="a")]))
        self.assertIn("1", v.motivo)

    def test_el_modal_de_una_validacion_valida_dice_los_cambios(self) -> None:
        v = validate_playlist(pl([ch("A", tvg_id="a")]), pl([ch("A", tvg_id="a")]))
        v.resumen_cambios = "1 nuevos · 0 fuera · 0 modificados · 1 sin cambios"
        self.assertIn("1 sin cambios", v.para_modal())


class TestCargaDeListaConValidacion(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name) / "cache"
        self.url = "https://proveedor.invalid/get.php"
        self.buena = (
            "#EXTM3U\n"
            + "".join(
                f'#EXTINF:-1 tvg-id="c{i}",Canal {i}\nhttp://x/{i}.ts\n' for i in range(20)
            )
        )

    def _descargar(self, texto: str, **kw):
        from thetvview.m3u_parser import FetchResult

        return mock.patch(
            "thetvview.m3u_parser.fetch_bytes",
            return_value=FetchResult(texto.encode("utf-8"), {"content-type": "audio/x-mpegurl"}),
        )

    def test_una_descarga_vacia_conserva_la_lista_buena(self) -> None:
        with self._descargar(self.buena):
            primera = load_url(self.url, cache_dir=self.cache, condicional=False)
        with self._descargar("#EXTM3U\n"):
            segunda = load_url(self.url, cache_dir=self.cache, condicional=False)
        self.assertEqual(len(primera.channels), 20)
        self.assertEqual(len(segunda.channels), 20)

    def test_una_descarga_truncada_conserva_la_lista_buena(self) -> None:
        with self._descargar(self.buena):
            load_url(self.url, cache_dir=self.cache, condicional=False)
        corta = "#EXTM3U\n#EXTINF:-1 tvg-id=\"x\",Uno\nhttp://x/1.ts\n"
        with self._descargar(corta):
            segunda = load_url(self.url, cache_dir=self.cache, condicional=False)
        self.assertEqual(len(segunda.channels), 20)

    def test_el_archivo_de_cache_no_se_reescribe_con_una_lista_mala(self) -> None:
        with self._descargar(self.buena):
            load_url(self.url, cache_dir=self.cache, condicional=False)
        archivo = next(self.cache.glob("playlist_*.m3u"))
        antes = archivo.read_bytes()
        with self._descargar("#EXTM3U\n"):
            load_url(self.url, cache_dir=self.cache, condicional=False)
        self.assertEqual(archivo.read_bytes(), antes)

    def test_si_el_usuario_confirma_se_aplica_igual(self) -> None:
        with self._descargar(self.buena):
            load_url(self.url, cache_dir=self.cache, condicional=False)
        corta = "#EXTM3U\n#EXTINF:-1 tvg-id=\"x\",Uno\nhttp://x/1.ts\n"
        with self._descargar(corta):
            segunda = load_url(
                self.url,
                cache_dir=self.cache,
                condicional=False,
                force_refresh=True,
                confirmar=lambda v: True,
            )
        self.assertEqual([c.name for c in segunda.channels], ["Uno"])

    def test_un_confirmar_que_falla_no_rompe_la_carga(self) -> None:
        with self._descargar(self.buena):
            load_url(self.url, cache_dir=self.cache, condicional=False)

        def revienta(v):  # noqa: ANN001
            raise RuntimeError("el modal se rompió")

        with self._descargar("#EXTM3U\n"):
            segunda = load_url(
                self.url,
                cache_dir=self.cache,
                condicional=False,
                force_refresh=True,
                confirmar=revienta,
            )
        self.assertEqual(len(segunda.channels), 20)

    def test_el_motivo_de_rechazo_queda_disponible(self) -> None:
        from thetvview import m3u_parser

        with self._descargar(self.buena):
            load_url(self.url, cache_dir=self.cache, condicional=False)
        with self._descargar("#EXTM3U\n"):
            load_url(
                self.url, cache_dir=self.cache, condicional=False, force_refresh=True
            )
        self.assertIsNotNone(m3u_parser.last_validation)
        assert m3u_parser.last_validation is not None
        self.assertFalse(m3u_parser.last_validation.ok)

    def test_una_actualizacion_interrumpida_no_deja_el_catalogo_parcial(self) -> None:
        """Si el commit falla, la versión anterior sigue entera."""
        from thetvview.m3u_parser import FetchResult

        with self._descargar(self.buena):
            load_url(self.url, cache_dir=self.cache, condicional=False)
        archivo = next(self.cache.glob("playlist_*.m3u"))
        buena_bytes = archivo.read_bytes()
        # Una lista **válida**: pasa la comprobación y llega al commit, que es
        # justo el punto donde puede fallar el disco.
        nueva = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1 tvg-id="c{i}",Canal {i}\nhttp://x/{i}.ts?v=2\n'
            for i in range(20)
        )
        with mock.patch(
            "thetvview.m3u_parser.fetch_bytes",
            return_value=FetchResult(nueva.encode("utf-8")),
        ), mock.patch(
            "thetvview.m3u_parser.atomic_write_bytes",
            side_effect=OSError("disco lleno"),
        ):
            resultado = load_url(
                self.url,
                cache_dir=self.cache,
                condicional=False,
                force_refresh=True,
            )
        # La lista nueva se devuelve (el usuario la pidió) pero el archivo
        # anterior sigue completo: nada de catálogo a medias.
        self.assertEqual(len(resultado.channels), 20)
        self.assertEqual(archivo.read_bytes(), buena_bytes, "el cache quedó a medias")
        # Y el archivo sigue siendo parseable: no hay catálogo parcial.
        desde_disco = load_url(
            self.url, cache_dir=self.cache, condicional=False
        )
        self.assertEqual(len(desde_disco.channels), 20)


# ---------------------------------------------------------------------------
# CHAN-004/009 · encabezados condicionales y resumen de cambios
# ---------------------------------------------------------------------------


class TestEncabezadosCondicionales(unittest.TestCase):
    def test_no_se_manda_nada_sin_sidecar(self) -> None:
        self.assertEqual(_headers_condicionales({}), {})

    def test_etag_y_last_modified_cuando_estan(self) -> None:
        headers = _headers_condicionales(
            {"etag": '"abc"', "last_modified": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )
        self.assertEqual(headers["If-None-Match"], '"abc"')
        self.assertEqual(
            headers["If-Modified-Since"], "Wed, 21 Oct 2026 07:28:00 GMT"
        )

    def test_un_valor_vacio_no_se_manda(self) -> None:
        """Mandarlo vacío hace que algunos servidores respondan 304 siempre."""
        self.assertEqual(_headers_condicionales({"etag": "  "}), {})

    def test_la_meta_de_una_respuesta_sin_encabezados_vacia(self) -> None:
        self.assertEqual(_meta_de_respuesta("http://x", {"content-type": "text/plain"}), {})

    def test_la_meta_se_guarda_y_se_relee(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "playlist_x.m3u"
            cache.write_bytes(b"x")
            _meta_escribir = None  # noqa: F841 - marcador de legibilidad
            from thetvview.m3u_parser import _escribir_meta

            _escribir_meta(cache, {"etag": '"v1"', "last_modified": "ayer"})
            self.assertEqual(_leer_meta(cache), {"etag": '"v1"', "last_modified": "ayer"})
            self.assertTrue(_meta_path_for(cache).exists())

    def test_un_sidecar_corrupto_no_rompe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "playlist_x.m3u"
            _meta_path_for(cache).write_text("{no es json", encoding="utf-8")
            self.assertEqual(_leer_meta(cache), {})


class TestResumenDeCambios(unittest.TestCase):
    def test_el_resumen_cubre_las_cuatro_categorias(self) -> None:
        previa = pl([ch("A", tvg_id="a"), ch("B", tvg_id="b")])
        nueva = pl([ch("A", tvg_id="a"), ch("C", tvg_id="c")])
        rep = resumir_cambios(previa, nueva)
        self.assertEqual(len(rep.unchanged), 1)
        self.assertEqual(len(rep.added), 1)
        self.assertEqual(len(rep.removed), 1)

    def test_sin_lista_previa_todo_es_nuevo(self) -> None:
        rep = resumir_cambios(None, pl([ch("A", tvg_id="a"), ch("B", tvg_id="b")]))
        self.assertEqual(len(rep.added), 2)
        self.assertEqual(rep.removed, [])


class TestAmbiguousIdentity(unittest.TestCase):
    def test_la_explicacion_distingue_los_dos_casos(self) -> None:
        indistinto = AmbiguousIdentity("k", "CNN", 2, False)
        distintos = AmbiguousIdentity("k", "CNN", 2, True)
        self.assertIn("no se pueden distinguir", indistinto.explicacion())
        self.assertIn("identificadores distintos", distintos.explicacion())


if __name__ == "__main__":
    unittest.main()

# ---------------------------------------------------------------------------
# La UI: una lista inválida se explica en un modal, nunca en silencio
# ---------------------------------------------------------------------------


class _PantallaFalsa:
    """Pantalla mínima con la lista que se está recargando."""

    def __init__(self, playlist: Playlist) -> None:
        self.playlist = playlist

    def refresh_from_playlist(self, nueva: Playlist) -> None:
        self.playlist = nueva


class TestRecargaEnLaInterfaz(unittest.TestCase):
    def setUp(self) -> None:
        from thetvview.ui import app as ui_app

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        for atributo, nombre in (
            ("PLAYLISTS_JSON", "playlists.json"),
            ("FAVORITES_JSON", "favorites.json"),
            ("PREFS_JSON", "prefs.json"),
            ("RECENTS_JSON", "recents.json"),
            ("THEME_JSON", "theme.json"),
        ):
            parche = mock.patch.object(ui_app.config, atributo, base / nombre)
            parche.start()
            self.addCleanup(parche.stop)
        self.app = ui_app.App(None)  # noqa: ANN001
        self.fuente = "https://proveedor.invalid/get.php"
        self.buena = (
            "#EXTM3U\n"
            + "".join(
                f'#EXTINF:-1 tvg-id="c{i}",Canal {i}\nhttp://x/{i}.ts\n'
                for i in range(20)
            )
        )
        activa = Playlist(
            name="Lista",
            source=self.fuente,
            channels=[
                ch(f"Canal {i}", f"http://x/{i}.ts", tvg_id=f"c{i}")
                for i in range(20)
            ],
        )
        self.app.playlist_cache[self.fuente] = activa
        self.app.push(_PantallaFalsa(activa))

    def _descargar(self, texto: str):
        from thetvview.m3u_parser import FetchResult

        return mock.patch(
            "thetvview.m3u_parser.fetch_bytes",
            return_value=FetchResult(texto.encode("utf-8"), {"etag": '"v2"'}),
        )

    def test_una_lista_vacia_abre_un_modal_para_confirmar(self) -> None:
        with self._descargar("#EXTM3U\n"), mock.patch.object(
            self.app, "_confirm", return_value=False
        ) as confirmar:
            self.app.reload_current_playlist()
        confirmar.assert_called_once()
        titulo, mensaje = confirmar.call_args[0]
        self.assertIn("incompleta", titulo)
        self.assertIn("llegó vacía", mensaje)
        self.assertIn("20", mensaje)
        # Y la lista buena sigue ahí.
        self.assertEqual(
            len(self.app.playlist_cache[self.fuente].channels), 20
        )

    def test_si_el_usuario_acepta_se_aplica(self) -> None:
        nueva = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1 tvg-id="c{i}",Canal {i}\nhttp://x/{i}.ts?v=2\n'
            for i in range(20)
        )
        with self._descargar("#EXTM3U\n"), mock.patch.object(
            self.app, "_confirm", return_value=True
        ):
            self.app.reload_current_playlist()
        self.assertEqual(len(self.app.playlist_cache[self.fuente].channels), 0)

    def test_el_resumen_dice_los_cambios(self) -> None:
        nueva = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1 tvg-id="c{i}",Canal {i}\nhttp://x/{i}.ts\n'
            for i in range(21)
        )
        with self._descargar(nueva):
            self.app.reload_current_playlist()
        msg = self.app.status.message
        self.assertIn("1 nuevos", msg)
        self.assertIn("20 sin cambios", msg)

    def test_el_cambio_de_url_con_mismo_tvg_id_no_cuenta_como_nuevo(self) -> None:
        """El resumen ya no dice «1 nuevo y 20 fuera» cuando sólo cambió la URL."""
        nueva = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1 tvg-id="c{i}",Canal {i}\nhttp://x/{i}.ts?token=nuevo\n'
            for i in range(20)
        )
        with self._descargar(nueva):
            self.app.reload_current_playlist()
        msg = self.app.status.message
        self.assertIn("20 modificados", msg)
        self.assertIn("0 nuevos", msg)
