"""Tests del `security-check` (SDD §45) y de su integración en la TUI."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from thetvview import catchup, favorites, recents
from thetvview.security import check as sec_check
from thetvview.security.check import (
    CheckFailure,
    CheckResult,
    all_ok,
    format_report,
    main,
    run_checks,
)


class TestLosChecksPasan(unittest.TestCase):
    """El check completo es una prueba de integración en sí misma."""

    def test_los_checks_pasan(self) -> None:
        results = run_checks()
        keys = [r.key for r in results]
        self.assertEqual(keys, [c[0] for c in sec_check.CHECKS])
        failing = [f"{r.key}: {r.detail}" for r in results if not r.ok]
        self.assertEqual(failing, [], f"checks en rojo: {failing}")

    def test_all_ok(self) -> None:
        self.assertTrue(all_ok([CheckResult("a", "A", True)]))
        self.assertFalse(all_ok([CheckResult("a", "A", False), ]))
        self.assertTrue(all_ok([]))

    def test_son_los_10_del_sdd_mas_dependencias_y_catchup(self) -> None:
        labels = [c[1] for c in sec_check.CHECKS]
        esperados = [
            "Secret redaction",
            "HTTPS certificate verification",
            "SSRF policy",
            "Redirect policy",
            "Response limits",
            "XML safe parsing",
            "AI secret sanitizer",
            "Shell-safe player invocation",
            "Credential storage",
            "No plaintext password logs",
        ]
        self.assertEqual(labels[:10], esperados)
        self.assertIn("Zero pip dependencies", labels[10:])
        self.assertIn("Catch-up capability gate", labels[11:])
        self.assertIn("Track discovery sandbox", labels[12:])
        self.assertEqual(len(set(c[0] for c in sec_check.CHECKS)), len(sec_check.CHECKS))


class TestCatchupGate(unittest.TestCase):
    """El check 12 verifica, no supone: se prueba rompiendo cada garantía.

    Un check que no puede ponerse en rojo no es un check. Estos tests
    comprueban que las cuatro garantías del check 12 se pueden romper a
    propósito y que el check lo nota.
    """

    def _check(self) -> str:
        return sec_check.check_catchup_capability_gate()

    def test_pasa_en_verde(self) -> None:
        self.assertIn("fail-closed", self._check())

    def test_detecta_relajar_la_invariante(self) -> None:
        """Si `can_use_catchup` deja de mirar `provider_declared`, falla."""
        with mock.patch.object(
            catchup, "can_use_catchup", lambda cap: bool(cap is not None and cap.enabled),
        ):
            with self.assertRaises(CheckFailure):
                self._check()

    def test_detecta_filtrar_la_password_en_la_referencia(self) -> None:
        with mock.patch.object(
            catchup.CatchupRef, "to_opaque",
            lambda self: ("xtream-ts://Panel/101.ts?password=hunter2"
                          f"&start={self.start_epoch}&dur={self.duration_seconds}"),
        ):
            with self.assertRaises(CheckFailure):
                self._check()

    def test_detecta_una_url_resuelta_persistida(self) -> None:
        """Sin la redacción de storage, la contraseña llega a `data/`."""
        with mock.patch.object(favorites, "needs_redaction_for_storage",
                               lambda url: False), \
                mock.patch.object(recents, "needs_redaction_for_storage",
                                  lambda url: False):
            with self.assertRaises(CheckFailure):
                self._check()


class TestInforme(unittest.TestCase):
    def test_pasan(self) -> None:
        out = format_report([CheckResult("a", "Alpha", True, "ok")])
        self.assertIn("SECURITY CHECK PASSED (1/1)", out)
        self.assertIn("[\u2713]", out)
        self.assertNotIn(sec_check.FAILED_BANNER, out)

    def test_fallan(self) -> None:
        out = format_report([
            CheckResult("a", "Alpha", True, "ok"),
            CheckResult("b", "Beta", False, "roto"),
        ])
        self.assertIn(sec_check.FAILED_BANNER, out)
        self.assertIn("(1/2)", out)
        self.assertIn("[\u2717]", out)
        self.assertIn("roto", out)  # el detalle de un fallo siempre se ve

    def test_verbose_muestra_detalle(self) -> None:
        out = format_report([CheckResult("a", "Alpha", True, "detalle")], verbose=True)
        self.assertIn("detalle", out)
        out2 = format_report([CheckResult("a", "Alpha", True, "detalle")])
        self.assertNotIn("detalle", out2)

    def test_color_se_puede_desactivar(self) -> None:
        out = format_report([CheckResult("a", "A", True)], use_color=True)
        self.assertIn("\033[32m", out)
        self.assertNotIn("\033[32m", format_report([CheckResult("a", "A", True)]))


class TestCLI(unittest.TestCase):
    def test_exit_0_en_ci(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--no-color", "--only", "secret_redaction"])
        self.assertEqual(code, 0)
        self.assertIn("SECURITY CHECK PASSED", buf.getvalue())

    def test_exit_1_si_algo_falla(self) -> None:
        buf = io.StringIO()
        broken = (("falso", "Siempre falla", lambda: 1 / 0),)
        with mock.patch.object(sec_check, "CHECKS", broken):
            with redirect_stdout(buf):
                code = main(["--no-color"])
        self.assertEqual(code, 1)
        self.assertIn(sec_check.FAILED_BANNER, buf.getvalue())
        self.assertIn("ZeroDivisionError", buf.getvalue())

    def test_json_para_ci(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--json", "--only", "ssrf_policy"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["passed"])
        self.assertEqual(data["results"][0]["key"], "ssrf_policy")

    def test_check_desconocido(self) -> None:
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            main(["--only", "no_existe"])
        self.assertEqual(ctx.exception.code, 2)


class TestLosChecksDetectan(unittest.TestCase):
    """Un check que siempre pasa no sirve: hay que ver que sepa fallar."""

    def test_ssrf_detecta_un_destino_interno_no_bloqueado(self) -> None:
        with mock.patch("thetvview.security.ssrf.check_url", lambda *a, **k: None):
            with self.assertRaises(CheckFailure) as ctx:
                sec_check.check_ssrf_policy()
        self.assertIn("no bloquea", str(ctx.exception))

    def test_shell_detecta_un_shell_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "pkg"
            pkg.mkdir()
            (pkg / "bad.py").write_text(
                "import subprocess\nsubprocess.run('ls', shell=True)\n",
                encoding="utf-8",
            )
            with mock.patch.object(sec_check, "PACKAGE_DIR", pkg):
                with self.assertRaises(CheckFailure) as ctx:
                    sec_check.check_shell_safe_player()
        self.assertIn("shell=True", str(ctx.exception))

    def test_logs_detectan_un_print_de_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "pkg"
            pkg.mkdir()
            (pkg / "bad.py").write_text(
                "password = 'x'\nprint(password)\n", encoding="utf-8"
            )
            with mock.patch.object(sec_check, "PACKAGE_DIR", pkg):
                with self.assertRaises(CheckFailure) as ctx:
                    sec_check.check_no_password_logs()
        self.assertIn("password", str(ctx.exception))

    def test_dependencias_detectan_un_paquete_externo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "thetvview"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "mod.py").write_text("import requests\n", encoding="utf-8")
            (root / "requirements.txt").write_text("requests==2.0\n", encoding="utf-8")
            with mock.patch.object(sec_check, "PACKAGE_DIR", pkg), \
                    mock.patch.object(sec_check, "REPO_ROOT", root):
                with self.assertRaises(CheckFailure):
                    sec_check.check_stdlib_dependencies()

    def test_redaction_detecta_un_secreto_sin_redactar(self) -> None:
        with mock.patch("thetvview.security.redaction.redact_text", lambda t: t):
            with self.assertRaises(CheckFailure):
                sec_check.check_secret_redaction()


class TestIntegracionTUI(unittest.TestCase):
    def setUp(self) -> None:
        from thetvview.ui import app as ui_app

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        for attr, name in (
            ("PLAYLISTS_JSON", "playlists.json"),
            ("FAVORITES_JSON", "favorites.json"),
            ("PREFS_JSON", "prefs.json"),
            ("RECENTS_JSON", "recents.json"),
            ("THEME_JSON", "theme.json"),
        ):
            patcher = mock.patch.object(ui_app.config, attr, base / name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_run_security_check_termina_en_estado_sin_curses(self) -> None:
        from thetvview.ui.app import App

        app = App(None)  # noqa: ANN001 - sin terminal
        app.run_security_check()
        self.assertIn("Security check", app.status.message)
        self.assertFalse(app._in_modal)

    def test_ayuda_menciona_el_atajo(self) -> None:
        from thetvview.ui.app import build_help_text

        text = build_help_text(None)
        self.assertIn("security-check", text)
        self.assertIn("!", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestTrackDiscoverySandbox(unittest.TestCase):
    """El check 13 (plan F7) tiene que poder ponerse rojo.

    Un invariante que nadie puede romper no es un invariante: aquí se
    comprueba que cada una de las tres reglas salta cuando se relaja.
    """

    def _con_archivo_relajado(self, ruta_rel, antes, despues):
        """Relaja un invariante en el fichero y devuelve cómo deshacerlo.

        Vacía además la caché de imports: si no, el check seguiría viendo
        el módulo viejo ya cargado y el test no probaría nada.
        """
        import sys

        ruta = sec_check.PACKAGE_DIR / ruta_rel
        original = ruta.read_text(encoding="utf-8")
        self.assertIn(antes, original)
        modulo = "thetvview." + ruta_rel.removesuffix(".py").replace("/", ".")

        def _restaurar():
            ruta.write_text(original, encoding="utf-8")
            sys.modules.pop(modulo, None)

        self.addCleanup(_restaurar)
        for nombre in list(sys.modules):
            if nombre == modulo or nombre.startswith(modulo + "."):
                sys.modules.pop(nombre, None)
        ruta.write_text(original.replace(antes, despues), encoding="utf-8")

    def test_detecta_urlopen_fuera_de_safe_http(self) -> None:
        self._con_archivo_relajado(
            "streams/probe.py",
            "def probe_capabilities(",
            "def _atajo(url):\n"
            "    return urllib.request.urlopen(url)\n"
            "\n\n"
            "def probe_capabilities(",
        )
        with self.assertRaises(sec_check.CheckFailure) as ctx:
            sec_check.check_track_discovery_is_sandboxed()
        self.assertIn("safe_http", str(ctx.exception))

    def test_detecta_proxy_escuchando_fuera_de_loopback(self) -> None:
        self._con_archivo_relajado(
            "streams/pin_proxy.py",
            '        if host != LOOPBACK:',
            "        if False:",
        )
        with self.assertRaises(sec_check.CheckFailure) as ctx:
            sec_check.check_track_discovery_is_sandboxed()
        self.assertIn("loopback", str(ctx.exception))

    def test_detecta_clave_de_canal_sin_redactar(self) -> None:
        self._con_archivo_relajado(
            "tracks/prefs.py",
            "hashlib.sha256(redact_text(url)",
            "hashlib.sha256(url",
        )
        with self.assertRaises(sec_check.CheckFailure) as ctx:
            sec_check.check_track_discovery_is_sandboxed()
        self.assertIn("redacta", str(ctx.exception))

    def test_pasa_en_verde(self) -> None:
        self.assertIn("loopback", sec_check.check_track_discovery_is_sandboxed())
