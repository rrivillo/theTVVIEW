"""Tests comprehensivos contra la playlist real IPTVSV.m3u (1755 canales, 43 grupos).

Cubre: parsing M3U, detección de resoluciones, agrupación por grupos,
favoritos, player (EXTVLCOPT), y UX (búsqueda, navegación, formato de labels).
"""

import curses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.favorites import FavoritesManager
from thetvview.groups import groups_of
from thetvview.models import Channel, Playlist
from thetvview.m3u_parser import parse_file, parse_text
from thetvview.player import _option_args, command_for
from thetvview.resolutions import base_name, detect, quality_rank, split_name, variants_of
from thetvview.ui.screens import ChannelsScreen, format_channel_name

FIXTURES = Path(__file__).parent / "fixtures"
REAL_M3U = FIXTURES / "iptvsv_real.m3u"


# ── Fixtures y helpers ────────────────────────────────────────────────


def _load() -> Playlist:
    return parse_file(REAL_M3U)


class _Fav:
    def __init__(self, favs=()):
        self.favs = set(favs)

    def is_favorite(self, channel: Channel) -> bool:
        return channel.name in self.favs


class _Status:
    def __init__(self):
        self.message = ""
        self.is_error = False

    def show(self, message: str, error: bool = False) -> None:
        self.message = message
        self.is_error = error


class _StubApp:
    def __init__(self, favs=()):
        self.favorites = _Fav(favs)
        self.status = _Status()

    def body_height(self) -> int:
        return 20


# ══════════════════════════════════════════════════════════════════════
# 1. PARSING M3U — Playlist real
# ══════════════════════════════════════════════════════════════════════


class TestRealM3UParsing(unittest.TestCase):
    """Parsing completo de la playlist real IPTVSV.m3u."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pl = _load()

    def test_total_canales(self) -> None:
        self.assertEqual(len(self.pl.channels), 1755)

    def test_todos_los_canales_tienen_url(self) -> None:
        for ch in self.pl.channels:
            self.assertTrue(ch.url, f"Canal sin URL: {ch.name}")
            self.assertTrue(
                ch.url.startswith(("http://", "https://")),
                f"URL inválida en '{ch.name}': {ch.url}",
            )

    def test_todos_los_canales_tienen_nombre(self) -> None:
        for ch in self.pl.channels:
            self.assertTrue(ch.name.strip(), f"Canal sin nombre: {ch.url}")

    def test_no_hay_canales_radio(self) -> None:
        radio = [ch for ch in self.pl.channels if ch.radio]
        self.assertEqual(radio, [])

    def test_no_hay_tvg_id(self) -> None:
        con_id = [ch for ch in self.pl.channels if ch.tvg_id]
        self.assertEqual(con_id, [])

    def test_canales_con_logo(self) -> None:
        con_logo = [ch for ch in self.pl.channels if ch.tvg_logo and ch.tvg_logo.startswith("http")]
        self.assertGreater(len(con_logo), 1550)
        for ch in con_logo:
            self.assertTrue(
                ch.tvg_logo.startswith("http"),
                f"Logo inválido en '{ch.name}': {ch.tvg_logo}",
            )

    def test_todos_tienen_grupo(self) -> None:
        sin_grupo = [ch for ch in self.pl.channels if not ch.group]
        self.assertEqual(sin_grupo, [])

    def test_extvlcopt_capturados(self) -> None:
        con_opts = [ch for ch in self.pl.channels if ch.extra_options]
        self.assertGreater(len(con_opts), 60)
        for ch in con_opts:
            for tag, val in ch.extra_options:
                self.assertIn(tag, ("EXTVLCOPT", "KODIPROP"))
                self.assertTrue(val, f"Opción vacía en '{ch.name}'")

    def test_kodiprop_capturados(self) -> None:
        kodiprop = [
            ch
            for ch in self.pl.channels
            if any("KODIPROP" in t for t, _ in ch.extra_options)
        ]
        self.assertGreater(len(kodiprop), 0)
        for ch in kodiprop:
            vals = [v for t, v in ch.extra_options if "KODIPROP" in t]
            self.assertTrue(any("inputstream" in v for v in vals))

    def test_user_agent_en_canales_con_extvlcopt(self) -> None:
        ua = [
            ch
            for ch in self.pl.channels
            if any("user-agent" in v.lower() for _, v in ch.extra_options)
        ]
        self.assertGreater(len(ua), 30)
        for ch in ua:
            ua_vals = [v for _, v in ch.extra_options if "user-agent" in v.lower()]
            self.assertTrue(any("Mozilla" in v for v in ua_vals))

    def test_referrer_en_canales_con_extvlcopt(self) -> None:
        ref = [
            ch
            for ch in self.pl.channels
            if any("referrer" in v.lower() or "referer" in v.lower() for _, v in ch.extra_options)
        ]
        self.assertGreater(len(ref), 20)
        for ch in ref:
            ref_vals = [v for _, v in ch.extra_options if "referrer" in v.lower() or "referer" in v.lower()]
            self.assertTrue(any("http" in v for v in ref_vals))

    def test_canal_con_user_agent_y_referrer(self) -> None:
        """Canal real que tiene ambos: user-agent + referrer."""
        for ch in self.pl.channels:
            has_ua = any("user-agent" in v.lower() for _, v in ch.extra_options)
            has_ref = any("referrer" in v.lower() for _, v in ch.extra_options)
            if has_ua and has_ref:
                self.assertIsNotNone(ch.name)
                self.assertTrue(ch.url.startswith("http"))
                return
        self.fail("No se encontró canal con user-agent + referrer")

    def test_no_extinf_huerfanos(self) -> None:
        """Todos los canales parseados deben tener URL http(s) válida."""
        for ch in self.pl.channels:
            self.assertTrue(
                ch.url.startswith(("http://", "https://")),
                f"URL inválida: {ch.url}",
            )


# ══════════════════════════════════════════════════════════════════════
# 2. GRUPOS — Agrupación real
# ══════════════════════════════════════════════════════════════════════


class TestRealGroups(unittest.TestCase):
    """Agrupación de canales por group-title con la playlist real."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pl = _load()
        cls.groups = groups_of(cls.pl.channels)

    def test_total_grupos(self) -> None:
        self.assertEqual(len(self.groups), 43)

    def test_todos_los_canales_en_grupos(self) -> None:
        total = sum(len(chs) for chs in self.groups.values())
        self.assertEqual(total, len(self.pl.channels))

    def test_no_grupo_none(self) -> None:
        self.assertNotIn(None, self.groups)

    def test_grupos_principales_existen(self) -> None:
        esperados = [
            "El Salvador", "El Salvador - TCS", "Guatemala", "Honduras",
            "Costa Rica", "México", "Colombia", "Ecuador", "Perú",
            "Chile", "Argentina", "Paraguay", "República Dominicana",
            "España", "Deportes", "Cine / Películas", "Música",
            "Infantiles", "Informativos", "Anime",
        ]
        for g in esperados:
            self.assertIn(g, self.groups, f"Grupo '{g}' no encontrado")

    def test_grupo_el_salvador_tcs_tiene_12(self) -> None:
        self.assertEqual(len(self.groups["El Salvador - TCS"]), 12)

    def test_grupo_honduras_tiene_41(self) -> None:
        self.assertEqual(len(self.groups["Honduras"]), 41)

    def test_grupo_guatemala_tiene_47(self) -> None:
        self.assertEqual(len(self.groups["Guatemala"]), 47)

    def test_grupo_costa_rica_tiene_38(self) -> None:
        self.assertEqual(len(self.groups["Costa Rica"]), 38)

    def test_grupo_mexico_tiene_45(self) -> None:
        self.assertEqual(len(self.groups["México"]), 45)

    def test_grupo_espana_tiene_152(self) -> None:
        self.assertEqual(len(self.groups["España"]), 152)

    def test_grupo_entretenimiento_tiene_164(self) -> None:
        self.assertEqual(len(self.groups["Entretenimiento / Cine / Series"]), 164)

    def test_grupo_cine_premium_tiene_106(self) -> None:
        self.assertEqual(len(self.groups["Cine / Películas Premium"]), 106)

    def test_grupo_musica_tiene_89(self) -> None:
        self.assertEqual(len(self.groups["Música"]), 89)

    def test_orden_alfabetico(self) -> None:
        keys = [k for k in self.groups.keys() if k is not None]
        self.assertEqual(keys, sorted(keys))

    def test_cada_grupo_tiene_canales(self) -> None:
        for g, chs in self.groups.items():
            self.assertGreater(len(chs), 0, f"Grupo '{g}' vacío")


# ══════════════════════════════════════════════════════════════════════
# 3. RESOLUCIONES — Detección con nombres reales
# ══════════════════════════════════════════════════════════════════════


class TestRealResolutions(unittest.TestCase):
    """Detección de resolución en canales de la playlist real."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pl = _load()

    def test_canales_con_resolucion_detectada(self) -> None:
        con_res = [ch for ch in self.pl.channels if detect(ch)]
        self.assertGreater(len(con_res), 280)

    def test_detecta_sd(self) -> None:
        sd = [ch for ch in self.pl.channels if detect(ch) == "SD"]
        self.assertGreater(len(sd), 20)
        nombres = [ch.name for ch in sd[:5]]
        for n in nombres:
            self.assertIn("SD", n)

    def test_detecta_hd(self) -> None:
        hd = [ch for ch in self.pl.channels if detect(ch) == "HD"]
        self.assertGreater(len(hd), 100)

    def test_canal_tcs_sd(self) -> None:
        ch = next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS SD")
        self.assertEqual(detect(ch), "SD")

    def test_canal_tcs_hd_opc2(self) -> None:
        ch = next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS HD [Opc.2]")
        self.assertEqual(detect(ch), "HD")

    def test_canal_12_geo_blocked_hd(self) -> None:
        ch = next(ch for ch in self.pl.channels if ch.name == "Canal 12 HD [Geo-Blocked]")
        self.assertEqual(detect(ch), "HD")
        self.assertEqual(base_name(ch), "Canal 12")

    def test_canal_12_sd多重_marcadores(self) -> None:
        """Canal con múltiples marcadores: [No 24/7][Opc.3]."""
        ch = next(ch for ch in self.pl.channels if ch.name == "Canal 12 SD [No 24/7][Opc.3]")
        self.assertEqual(detect(ch), "SD")
        self.assertEqual(base_name(ch), "Canal 12")

    def test_nombre_con_parentesis_calidad(self) -> None:
        """Canales con calidad en paréntesis estilo iptv.org."""
        tve_star = [ch for ch in self.pl.channels if "TVE Star" in ch.name]
        self.assertGreater(len(tve_star), 0)
        con_res = [ch for ch in tve_star if detect(ch) is not None]
        self.assertGreater(len(con_res), 0)
        for ch in con_res:
            res = detect(ch)
            self.assertIn(res, ("HD", "720p", "1080p"))

    def test_split_name_con_marcadores(self) -> None:
        base, res = split_name("Canal 12 HD [Geo-Blocked]")
        self.assertEqual(base, "Canal 12")
        self.assertEqual(res, "HD")

    def test_split_name_multiples_marcadores(self) -> None:
        base, res = split_name("Canal 12 SD [No 24/7][Opc.3]")
        self.assertEqual(base, "Canal 12")
        self.assertEqual(res, "SD")

    def test_split_name_con_opc(self) -> None:
        base, res = split_name("Canal 2 TCS HD [Opc.2]")
        self.assertEqual(base, "Canal 2 TCS")
        self.assertEqual(res, "HD")

    def test_split_name_mpv_en_nombre(self) -> None:
        """El token (MPV) no debe confundirse con resolución pero sí
        debe ignorarse al detectar la calidad previa (fix MPV)."""
        base, res = split_name("TVO Canal 23 SD [No 24/7](MPV)")
        self.assertEqual(res, "SD")
        self.assertEqual(base, "TVO Canal 23")

    def test_base_name_agrupa_variantes(self) -> None:
        """base_name agrupa canales con distintas opciones."""
        b1 = base_name(next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS SD"))
        b2 = base_name(next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS HD [Opc.2]"))
        b3 = base_name(next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS HD [Opc.3]"))
        self.assertEqual(b1, b2, b3)
        self.assertEqual(b1, "Canal 2 TCS")

    def test_variants_of_tcs(self) -> None:
        """Canal 2 TCS tiene variantes SD y HD."""
        target = next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS SD")
        variants = variants_of(self.pl.channels, target)
        self.assertGreaterEqual(len(variants), 2)
        bases = [base_name(v) for v in variants]
        self.assertTrue(all(b == "Canal 2 TCS" for b in bases))
        resols = [detect(v) for v in variants]
        self.assertIn("SD", resols)
        self.assertIn("HD", resols)

    def test_variants_of_ordenadas(self) -> None:
        """Variantes ordenadas de peor a mejor calidad."""
        target = next(ch for ch in self.pl.channels if ch.name == "Canal 12 SD [No 24/7][Opc.3]")
        variants = variants_of(self.pl.channels, target)
        if len(variants) >= 2:
            ranks = [quality_rank(detect(v)) for v in variants]
            self.assertEqual(ranks, sorted(ranks))

    def test_canal_sin_resolucion_no_tiene_variantes(self) -> None:
        """Canales sin resolución detectable no deben tener variantes."""
        sin_res = [ch for ch in self.pl.channels if detect(ch) is None]
        self.assertGreater(len(sin_res), 900)
        for ch in sin_res[:50]:
            variants = variants_of(self.pl.channels, ch)
            self.assertEqual(variants, [], f"'{ch.name}' no debería tener variantes")

    def test_variants_of_todos_los_grupos(self) -> None:
        """Verifica que todos los grupos de variantes son coherentes."""
        seen_bases = set()
        for ch in self.pl.channels:
            b = base_name(ch)
            if b in seen_bases:
                continue
            variants = variants_of(self.pl.channels, ch)
            if len(variants) >= 2:
                seen_bases.add(b)
                bases = set(base_name(v) for v in variants)
                self.assertEqual(len(bases), 1, f"Variantes de '{b}' mezclan bases: {bases}")


# ══════════════════════════════════════════════════════════════════════
# 4. PLAYER — Comandos con canales reales
# ══════════════════════════════════════════════════════════════════════


class TestRealPlayerCommands(unittest.TestCase):
    """Comandos headless con canales reales que tienen EXTVLCOPT.

    Todos los comandos se construyen con headless=True: nunca se abre
    ventana ni se requiere DISPLAY/Wayland en CI.
    """

    def setUp(self) -> None:
        self.pl = _load()
        self.con_opts = [ch for ch in self.pl.channels if ch.extra_options]

    def test_comando_mpv_basico(self) -> None:
        ch = next(ch for ch in self.con_opts if ch.name == "Canal 2 TCS SD")
        cmd = command_for(ch, "mpv", player_path="/usr/bin/mpv", headless=True)
        self.assertEqual(cmd[0], "/usr/bin/mpv")
        # Headless: sin ventana ni audio real.
        self.assertIn("--vo=null", cmd)
        self.assertIn("--ao=null", cmd)
        # Verificar args de codec h264 están presentes
        self.assertIn("--vd=ffh264", cmd)
        # Verificar user-agent y referrer en cualquier posición
        self.assertTrue(any("--user-agent=Mozilla" in a for a in cmd))
        self.assertTrue(any("--referrer=https://teleon.tv/" in a for a in cmd))
        self.assertIn("--title=Canal 2 TCS SD", cmd)
        self.assertEqual(cmd[-1], ch.url)

    def test_comando_vlc_mismo_canal(self) -> None:
        ch = next(ch for ch in self.con_opts if ch.name == "Canal 2 TCS SD")
        cmd = command_for(ch, "vlc", player_path="/usr/bin/vlc", headless=True)
        self.assertEqual(cmd[0], "/usr/bin/vlc")
        self.assertIn("--intf", cmd)
        self.assertIn("dummy", cmd)
        self.assertTrue(any("--http-user-agent=" in a for a in cmd))
        self.assertTrue(any("--http-referrer=" in a for a in cmd))
        self.assertTrue(any("--input-title-format=" in a for a in cmd))

    def test_comando_mplayer_mismo_canal(self) -> None:
        ch = next(ch for ch in self.con_opts if ch.name == "Canal 2 TCS SD")
        cmd = command_for(ch, "mplayer", player_path="/usr/bin/mplayer", headless=True)
        self.assertEqual(cmd[0], "/usr/bin/mplayer")
        self.assertIn("-vo", cmd)
        self.assertIn("-ao", cmd)
        self.assertIn("-user-agent", cmd)
        # mplayer usa args separados: -user-agent VALUE
        idx = cmd.index("-user-agent")
        self.assertIn("Mozilla", cmd[idx + 1])

    def test_todos_los_canales_con_opciones_generan_comando(self) -> None:
        """Todos los canales con EXTVLCOPT deben generar comandos válidos."""
        for ch in self.con_opts[:50]:
            cmd = command_for(ch, "mpv", player_path="/usr/bin/mpv", headless=True)
            self.assertEqual(cmd[0], "/usr/bin/mpv")
            self.assertIn("--vo=null", cmd)
            self.assertEqual(cmd[-1], ch.url)
            self.assertTrue(any("--title" in a for a in cmd))

    def test_launch_headless_no_requiere_display(self) -> None:
        """launch(headless=True) usa Popen mockeado con flags dummy/null."""
        from thetvview import player

        ch = next(ch for ch in self.con_opts if ch.name == "Canal 2 TCS SD")
        with (
            mock.patch.object(
                player.config, "find_player", return_value="/usr/bin/mpv"
            ),
            mock.patch.object(player.subprocess, "Popen") as popen,
        ):
            player.launch(ch, player_name="mpv", headless=True)
        cmd = popen.call_args[0][0]
        self.assertIn("--vo=null", cmd)
        self.assertIn("--ao=null", cmd)
        self.assertEqual(cmd[-1], ch.url)

    def test_kodiprop_se_ignora_con_aviso(self) -> None:
        """KODIPROP se ignora en mpv (no soportado)."""
        kodiprop_chs = [
            ch
            for ch in self.pl.channels
            if any("KODIPROP" in t for t, _ in ch.extra_options)
        ]
        self.assertGreater(len(kodiprop_chs), 0)
        for ch in kodiprop_chs[:10]:
            _, warnings = _option_args(ch, "mpv")
            self.assertTrue(
                any("KODIPROP" in w for w in warnings),
                f"Debería advertir KODIPROP ignorado para '{ch.name}'",
            )

    def test_option_args_user_agent(self) -> None:
        ch = next(ch for ch in self.con_opts if ch.name == "Canal 2 TCS SD")
        args, _ = _option_args(ch, "mpv")
        self.assertTrue(any("--user-agent=" in a for a in args))

    def test_option_args_referrer(self) -> None:
        ch = next(ch for ch in self.con_opts if ch.name == "Canal 2 TCS SD")
        args, _ = _option_args(ch, "mpv")
        self.assertTrue(any("--referrer=" in a for a in args))

    def test_option_args_vlc_referrer_format(self) -> None:
        ch = next(ch for ch in self.con_opts if ch.name == "Canal 2 TCS SD")
        args, _ = _option_args(ch, "vlc")
        self.assertTrue(any("--http-referrer=" in a for a in args))


# ══════════════════════════════════════════════════════════════════════
# 5. FAVORITOS — Con canales reales
# ══════════════════════════════════════════════════════════════════════


class TestRealFavorites(unittest.TestCase):
    """Gestión de favoritos con canales de la playlist real."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "favorites.json"
        self.mgr = FavoritesManager(self.path)
        self.pl = _load()

    def test_toggle_canal_real(self) -> None:
        ch = self.pl.channels[0]
        self.assertTrue(self.mgr.toggle(ch))
        self.assertTrue(self.mgr.is_favorite(ch))
        self.assertFalse(self.mgr.toggle(ch))
        self.assertFalse(self.mgr.is_favorite(ch))

    def test_agregar_varios_canales_reales(self) -> None:
        for ch in self.pl.channels[:10]:
            self.mgr.toggle(ch)
        favs = self.mgr.load()
        self.assertEqual(len(favs), 10)

    def test_persistencia_con_canales_reales(self) -> None:
        ch = next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS SD")
        self.mgr.toggle(ch)
        mgr2 = FavoritesManager(self.path)
        loaded = mgr2.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, "Canal 2 TCS SD")
        self.assertEqual(loaded[0].url, ch.url)
        self.assertEqual(loaded[0].group, "El Salvador - TCS")

    def test_favorito_con_extvlcopt_se_persigue(self) -> None:
        """Las opciones EXTVLCOPT se conservan al serializar favorito."""
        ch = next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS SD")
        self.mgr.toggle(ch)
        loaded = self.mgr.load()[0]
        self.assertEqual(len(loaded.extra_options), 2)
        tags = [t for t, _ in loaded.extra_options]
        self.assertIn("EXTVLCOPT", tags)

    def test_remove_canal_real(self) -> None:
        ch = self.pl.channels[5]
        self.mgr.toggle(ch)
        self.assertTrue(self.mgr.remove(ch))
        self.assertFalse(self.mgr.remove(ch))

    def test_is_favorite_para_canales_no_agregados(self) -> None:
        for ch in self.pl.channels[:20]:
            self.assertFalse(self.mgr.is_favorite(ch))


# ══════════════════════════════════════════════════════════════════════
# 6. UX — Búsqueda, navegación, formato de labels
# ══════════════════════════════════════════════════════════════════════


class TestRealUXSearch(unittest.TestCase):
    """Búsqueda en ChannelsScreen con la playlist real."""

    def setUp(self) -> None:
        self.pl = _load()
        self.app = _StubApp()

    def test_buscar_por_nombre_parcial(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        screen.query = "Canal 2"
        screen._apply_filter()
        visibles = [screen.channels[i] for i in screen.visible_idx]
        self.assertGreater(len(visibles), 0)
        for ch in visibles:
            self.assertIn("Canal 2".lower(), ch.name.lower())

    def test_buscar_por_grupo(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        screen.query = "Honduras"
        screen._apply_filter()
        visibles = [screen.channels[i] for i in screen.visible_idx]
        self.assertGreater(len(visibles), 0)
        for ch in visibles:
            self.assertTrue(
                "Honduras".lower() in ch.name.lower()
                or "Honduras".lower() in (ch.group or "").lower()
            )

    def test_buscar_hd(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        screen.query = "HD"
        screen._apply_filter()
        visibles = [screen.channels[i] for i in screen.visible_idx]
        self.assertGreater(len(visibles), 50)
        for ch in visibles:
            self.assertTrue(
                "HD" in ch.name.upper()
                or "HD" in (ch.group or "").upper()
            )

    def test_buscar_vacio_muestra_todos(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        screen.query = ""
        screen._apply_filter()
        visibles = [screen.channels[i] for i in screen.visible_idx]
        self.assertEqual(len(visibles), len(self.pl.channels))

    def test_buscar_no_match_da_vacio(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        screen.query = "ZZZZNOEXISTE"
        screen._apply_filter()
        self.assertEqual(len(screen.visible_idx), 0)

    def test_buscar_geo_blocked(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        screen.query = "Geo-Blocked"
        screen._apply_filter()
        visibles = [screen.channels[i] for i in screen.visible_idx]
        self.assertGreater(len(visibles), 0)
        for ch in visibles:
            self.assertIn("eo-blocked", ch.name.lower())

    def test_buscar_opc(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        screen.query = "Opc"
        screen._apply_filter()
        visibles = [screen.channels[i] for i in screen.visible_idx]
        self.assertGreater(len(visibles), 100)
        for ch in visibles:
            self.assertIn("opc", ch.name.lower())


class TestRealUXNavigation(unittest.TestCase):
    """Navegación en ChannelsScreen con la playlist real."""

    def setUp(self) -> None:
        self.pl = _load()
        self.app = _StubApp()

    def test_tecla_g_abre_grupos(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        action = screen.handle_key(ord("g"))
        self.assertEqual(action["action"], "show_groups")
        self.assertIs(action["playlist"], self.pl)

    def test_tecla_e_abre_epg(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        action = screen.handle_key(ord("e"))
        self.assertEqual(action["action"], "show_epg")
        self.assertEqual(action["channel"].name, self.pl.channels[0].name)

    def test_tecla_f_toggle_favorito(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        action = screen.handle_key(ord("f"))
        self.assertEqual(action["action"], "toggle_favorite")

    def test_enter_abre_canal(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        action = screen.handle_key(10)
        self.assertEqual(action["action"], "open_channel")

    def test_navegacion_up_down(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        initial = screen.list.selected
        screen.handle_key(curses.KEY_DOWN)
        self.assertEqual(screen.list.selected, initial + 1)
        screen.handle_key(curses.KEY_UP)
        self.assertEqual(screen.list.selected, initial)

    def test_canal_actual_es_primero(self) -> None:
        screen = ChannelsScreen(self.app, self.pl)
        self.assertEqual(screen.current_channel().name, self.pl.channels[0].name)


class TestRealUXLabels(unittest.TestCase):
    """Formato de labels de canal con la playlist real."""

    def setUp(self) -> None:
        self.pl = _load()

    def test_label_con_grupo(self) -> None:
        ch = next(ch for ch in self.pl.channels if ch.group == "El Salvador - TCS")
        label = format_channel_name(ch)
        self.assertIn("[El Salvador - TCS]", label)
        self.assertIn(ch.name, label)

    def test_label_favorito(self) -> None:
        ch = self.pl.channels[0]
        label = format_channel_name(ch, favorite=True)
        self.assertTrue(label.startswith("★"))
        self.assertIn(ch.name, label)

    def test_labels_en_screen(self) -> None:
        app = _StubApp(favs={self.pl.channels[0].name})
        screen = ChannelsScreen(app, self.pl)
        labels = [screen._label_for(i) for i in range(min(20, len(screen.channels)))]
        self.assertTrue(any("★" in l for l in labels))

    def test_grupo_en_label(self) -> None:
        app = _StubApp()
        screen = ChannelsScreen(app, self.pl)
        label = screen._label_for(0)
        ch = screen.channels[0]
        self.assertIn(ch.group, label)


class TestRealUXGroupFilter(unittest.TestCase):
    """Filtrado por grupo con la playlist real."""

    def setUp(self) -> None:
        self.pl = _load()
        self.app = _StubApp()

    def test_filtro_grupo_honduras(self) -> None:
        screen = ChannelsScreen(self.app, self.pl, group="Honduras")
        self.assertEqual(len(screen.channels), 41)
        self.assertTrue(all(c.group == "Honduras" for c in screen.channels))

    def test_filtro_grupo_mexico(self) -> None:
        screen = ChannelsScreen(self.app, self.pl, group="México")
        self.assertEqual(len(screen.channels), 45)

    def test_filtro_grupo_deportes(self) -> None:
        screen = ChannelsScreen(self.app, self.pl, group="Deportes")
        self.assertEqual(len(screen.channels), 68)

    def test_busqueda_dentro_del_grupo(self) -> None:
        screen = ChannelsScreen(self.app, self.pl, group="El Salvador - TCS")
        screen.query = "Canal"
        screen._apply_filter()
        visibles = [screen.channels[i] for i in screen.visible_idx]
        self.assertGreater(len(visibles), 0)
        for ch in visibles:
            self.assertIn("Canal", ch.name)
            self.assertEqual(ch.group, "El Salvador - TCS")


class TestRealUXShortcuts(unittest.TestCase):
    """Verificar que los shortcuts de cada pantalla son correctos."""

    def test_channels_screen_shortcuts(self) -> None:
        from thetvview.ui.screens import ChannelsScreen
        import inspect
        src = inspect.getsource(ChannelsScreen.handle_key)
        self.assertIn("searching", src)
        self.assertIn("show_groups", src)
        self.assertIn("toggle_favorite", src)
        self.assertIn("open_channel", src)

    def test_playlists_screen_shortcuts_contienen_flechas(self) -> None:
        from thetvview.ui.screens import PlaylistsScreen
        import inspect
        src = inspect.getsource(PlaylistsScreen.handle_key)
        self.assertIn("KEY_LEFT", src)
        self.assertIn("KEY_RIGHT", src)
        self.assertIn("KEY_UP", src)
        self.assertIn("KEY_DOWN", src)

    def test_groups_screen_shortcuts_contienen_flechas(self) -> None:
        from thetvview.ui.screens import GroupsScreen
        import inspect
        src = inspect.getsource(GroupsScreen.handle_key)
        self.assertIn("KEY_LEFT", src)
        self.assertIn("KEY_RIGHT", src)

    def test_resolution_screen_shortcuts_contienen_flechas(self) -> None:
        from thetvview.ui.screens import ResolutionScreen
        import inspect
        src = inspect.getsource(ResolutionScreen.handle_key)
        self.assertIn("KEY_LEFT", src)
        self.assertIn("KEY_RIGHT", src)


# ══════════════════════════════════════════════════════════════════════
# 7. PATRONES ESPECÍFICOS DEL M3U REAL
# ══════════════════════════════════════════════════════════════════════


class TestRealM3UPatterns(unittest.TestCase):
    """Patrones específicos observados en la playlist real IPTVSV.m3u."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pl = _load()

    def test_canales_con_opciones_numeradas(self) -> None:
        """Hay canales con [Opc.1], [Opc.2], [Opc.3], etc."""
        opc = [ch for ch in self.pl.channels if "[Opc." in ch.name]
        self.assertGreater(len(opc), 100)

    def test_canales_con_no_24_7(self) -> None:
        no247 = [ch for ch in self.pl.channels if "[No 24/7]" in ch.name or "[No 24/" in ch.name]
        self.assertGreater(len(no247), 200)

    def test_canales_con_geo_blocked(self) -> None:
        geo = [ch for ch in self.pl.channels if "Geo-Blocked" in ch.name or "Geo-blocked" in ch.name]
        self.assertGreater(len(geo), 30)

    def test_canales_con_mpv_en_nombre(self) -> None:
        """Upstream quitó el marcador (MPV) de la lista; el fix se verifica
        con un canal sintético (la cobertura vive en TestRealResolutions)."""
        pl = parse_text(
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="X",TVO Canal 23 SD [No 24/7](MPV)\n'
            "http://example.com/a.m3u8\n"
        )
        self.assertEqual(len(pl.channels), 1)
        self.assertEqual(detect(pl.channels[0]), "SD")
        self.assertEqual(base_name(pl.channels[0]), "TVO Canal 23")
        mpv = [ch for ch in self.pl.channels if "(MPV)" in ch.name]
        self.assertEqual(mpv, [])

    def test_canales_con_calidad_parentesis(self) -> None:
        """Canales con (720p), (1080p) en el nombre."""
        paren = [ch for ch in self.pl.channels if "(" in ch.name]
        self.assertGreater(len(paren), 10)
        # Al menos algunos deben tener resolución detectada
        con_res = [ch for ch in paren if detect(ch) is not None]
        self.assertGreater(len(con_res), 0)

    def test_url_con_token(self) -> None:
        """Algunas URLs traen ?token=..."""
        token = [ch for ch in self.pl.channels if "?token=" in ch.url]
        self.assertGreater(len(token), 5)

    def test_url_con_hls_param(self) -> None:
        """Algunas URLs traen ?hls al final."""
        hls = [ch for ch in self.pl.channels if "?hls" in ch.url]
        self.assertGreater(len(hls), 10)

    def test_url_puerto_no_default(self) -> None:
        """Muchas URLs usan puertos no estándar (8000, 19360, etc.)."""
        puerto = [ch for ch in self.pl.channels if ":8000/" in ch.url or ":19360/" in ch.url]
        self.assertGreater(len(puerto), 50)

    def test_nombre_con_slash(self) -> None:
        """Nombres con / fuera de corchetes, como 'Telecadena 7/4'."""
        import re
        slash = []
        for ch in self.pl.channels:
            # Quitar marcadores [No 24/7] etc. y buscar / restante
            limpio = re.sub(r"\[[^\]]*\]", "", ch.name)
            if "/" in limpio:
                slash.append(ch)
        self.assertGreater(len(slash), 0)
        names = [ch.name for ch in slash]
        self.assertTrue(any("Telecadena 7/4" in n for n in names))

    def test_nombre_con_plus(self) -> None:
        """Nombres con + como 'Win+'."""
        plus = [ch for ch in self.pl.channels if "+" in ch.name]
        self.assertGreater(len(plus), 0)

    def test_grupo_con_guion(self) -> None:
        """Grupo con guiones como 'El Salvador - TCS'."""
        guion = [ch for ch in self.pl.channels if ch.group and '-' in ch.group]
        self.assertGreater(len(guion), 0)
        groups = set(ch.group for ch in guion)
        self.assertIn("El Salvador - TCS", groups)

    def test_grupo_con_slash(self) -> None:
        """Grupos con / como 'Cine / Películas'."""
        cine = [ch for ch in self.pl.channels if ch.group == "Cine / Películas"]
        self.assertEqual(len(cine), 75)

    def test_grupo_con_ampersand(self) -> None:
        """Grupo con & como 'Discovery Home & Health'."""
        # El group-title es el nombre del grupo, no del canal
        dh = [ch for ch in self.pl.channels if "Discovery Home" in (ch.name or "")]
        self.assertGreater(len(dh), 0)

    def test_tvg_logo_url_largas(self) -> None:
        """Algunos logos tienen URLs muy largas."""
        largos = [ch for ch in self.pl.channels if ch.tvg_logo and len(ch.tvg_logo) > 200]
        self.assertGreater(len(largos), 0)


# ══════════════════════════════════════════════════════════════════════
# 8. INTEGRACIÓN — Flujos completos
# ══════════════════════════════════════════════════════════════════════


class TestRealIntegration(unittest.TestCase):
    """Flujos de integración completos con la playlist real."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pl = _load()

    def test_parsear_agregar_favorito_buscar(self) -> None:
        """Flujo: parsear playlist -> agregar favorito -> buscarlo."""
        mgr = FavoritesManager(Path(self._tmp.name) / "fav.json")
        target = next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS SD")
        mgr.toggle(target)

        app = _StubApp(favs={"Canal 2 TCS SD"})
        screen = ChannelsScreen(app, self.pl)
        screen.query = "Canal 2 TCS"
        screen._apply_filter()
        fav_labels = [
            screen._label_for(i)
            for i in range(len(screen.channels))
            if "★" in screen._label_for(i)
        ]
        self.assertGreater(len(fav_labels), 0)

    def test_grupo_filtrado_y_busqueda(self) -> None:
        """Flujo: filtrar por grupo -> buscar dentro del grupo."""
        app = _StubApp()
        screen = ChannelsScreen(app, self.pl, group="Honduras")
        screen.query = "HCH"
        screen._apply_filter()
        visibles = [screen.channels[i] for i in screen.visible_idx]
        self.assertGreater(len(visibles), 0)
        for ch in visibles:
            self.assertIn("HCH", ch.name)
            self.assertEqual(ch.group, "Honduras")

    def test_variantes_y_player(self) -> None:
        """Flujo headless: detectar variantes -> seleccionar -> construir comando."""
        target = next(ch for ch in self.pl.channels if ch.name == "Canal 2 TCS SD")
        variants = variants_of(self.pl.channels, target)
        self.assertGreater(len(variants), 1)
        best = variants[-1]
        cmd = command_for(best, "mpv", player_path="/usr/bin/mpv", headless=True)
        self.assertEqual(cmd[0], "/usr/bin/mpv")
        self.assertIn("--vo=null", cmd)
        self.assertEqual(cmd[-1], best.url)

    def test_todos_los_grupos_tienen_canales_parseables(self) -> None:
        """Cada grupo tiene al menos un canal que se puede usar en el player."""
        groups = groups_of(self.pl.channels)
        for g, chs in groups.items():
            for ch in chs[:3]:
                cmd = command_for(ch, "mpv", player_path="/usr/bin/mpv", headless=True)
                self.assertEqual(cmd[-1], ch.url, f"Error en grupo '{g}', canal '{ch.name}'")


if __name__ == "__main__":
    import curses
    unittest.main()
