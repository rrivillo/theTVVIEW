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
            # Noveno y décimo del SDD-M: con RTMP/RTSP/UDP abiertos, la
            # referencia opaca de cámara (`ipcam://`) es el segundo mecanismo
            # por el que una credencial podría llegar a `data/`, y comprueba
            # las dos mitades —que no lleve nada y que no se pueda construir
            # un argv con userinfo— junto a lo que ya hacía Xtream.
            "Opaque refs carry no credentials",
            "Credential storage",
            "No plaintext password logs",
        ]
        self.assertEqual(labels[:11], esperados)
        self.assertIn("Zero pip dependencies", labels[11:])
        self.assertIn("Catch-up capability gate", labels[12:])
        self.assertIn("Track discovery sandbox", labels[13:])
        self.assertIn("Atomic persistence & versioning", labels[14:])
        self.assertEqual(len(set(c[0] for c in sec_check.CHECKS)), len(sec_check.CHECKS))


class TestPersistenciaDetectada(unittest.TestCase):
    """El check 15 verifica, no supone: se rompe cada garantía y se ve.

    Un check que sólo pasa no demuestra nada. Estos testsuwitnessen que el
    check **falla** cuando se deshace lo que dice comprobar: si alguien
    reintroduce el patrón viejo de ``tmp.write_text()`` + ``replace()``, o
    quita el ``schema_version``, o hace la migración no idempotente, el
    check tiene que ponerse rojo.
    """

    def test_detecta_escritura_no_atomica(self) -> None:
        """Si el store deja JSON parcial al fallar, el check falla.

        Se sustituye ``atomic_write_json`` por una versión que escribe el
        destino **a medias**, que es exactamente el fallo que STORE-001
        prohíbe: el usuario se queda con un ``prefs.json`` ilegible.
        """
        from thetvview.storage import json_store

        def no_atomica(path, data, **_kwargs):
            ruta = Path(path)
            ruta.parent.mkdir(parents=True, exist_ok=True)
            texto = json.dumps(data, ensure_ascii=False, indent=2)
            ruta.write_text(texto[: len(texto) // 2], encoding="utf-8")

        with mock.patch.object(json_store, "atomic_write_json", no_atomica):
            with self.assertRaises(sec_check.CheckFailure) as ctx:
                sec_check.check_persistence_atomic()
        self.assertIn("legible", str(ctx.exception))
        # Y el mensaje explica la rotura, no suelta el error interno de json.
        self.assertNotIn("Unterminated", str(ctx.exception))

    def test_detecta_falta_de_schema_version(self) -> None:
        """Si un formato pierde su validador, el check lo nota."""
        from thetvview.storage import schemas

        with mock.patch.dict(schemas.SCHEMAS, {}, clear=True):
            with self.assertRaises(sec_check.CheckFailure) as ctx:
                sec_check.check_persistence_atomic()
        self.assertIn("validador", str(ctx.exception))

    def test_detecta_migracion_no_idempotente(self) -> None:
        """Si migrar dos veces vuelve a cambiar algo, el check lo nota."""
        from thetvview.storage import migrations

        original = migrations.migrate_file

        def no_idempotente(path, **kwargs):
            resultado = original(path, **kwargs)
            # Simula una migración que se reaplica siempre.
            return migrations.MigrationResult(
                {
                    "data": resultado.data,
                    "changed": True,
                    "from_version": 0,
                    "to_version": 1,
                }
            )

        with mock.patch.object(migrations, "migrate_file", no_idempotente):
            with self.assertRaises(sec_check.CheckFailure) as ctx:
                sec_check.check_persistence_atomic()
        self.assertIn("idempotente", str(ctx.exception))

    def test_el_check_real_pasa(self) -> None:
        """Y con el código real, pasa."""
        self.assertIsInstance(sec_check.check_persistence_atomic(), str)


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

        **Se recarga con ``importlib.reload`` en vez de hacer
        ``sys.modules.pop`` + reimportar.** La diferencia parece menor y no lo
        es: ``pop`` deja vivos **dos** objetos del mismo módulo —el viejo, al
        que apuntan todas las referencias importadas antes, y el nuevo, que
        crea el siguiente ``import``— y a partir de ahí los dos caminos dejan
        de verse. Con el módulo viejo sin recargar, el check leería el código
        viejo y el test no probaría nada.

        Pasaba de verdad. ``unittest`` importa **todos** los módulos de test
        durante el descubrimiento, antes de ejecutar ninguno, así que
        :mod:`tests.test_tasks_migracion` ya tenía el ``TrackProbe`` viejo
        cuando estos tests se ejecutaban. Su ``mock.patch("…
        .probe_capabilities")`` parcheaba el módulo nuevo mientras el worker
        llamaba al viejo, el parche no interceptaba nada y el sondeo se salía
        a la red de verdad: el test pasaba en aislamiento y fallaba en la
        suite completa, sin explicación aparente.

        ``reload`` reutiliza el mismo objeto de módulo **y su mismo
        ``__dict__``**, así que las funciones y clases ya capturadas siguen
        viendo los globals actualizados. Se recarga dos veces: una para que
        el check vea el código relajado, otra para restaurarlo.

        Sólo se recarga el módulo tocado, no sus submódulos: recargar de más
        multiplicaba el tiempo de la suite sin aportar nada, porque el check
        lee el fichero con :mod:`ast` y sólo necesita el código del módulo
        modificado.
        """
        import importlib
        import sys

        ruta = sec_check.PACKAGE_DIR / ruta_rel
        original = ruta.read_text(encoding="utf-8")
        self.assertIn(antes, original)
        modulo = "thetvview." + ruta_rel.removesuffix(".py").replace("/", ".")

        def _recargar() -> None:
            mod = sys.modules.get(modulo)
            if mod is not None:
                importlib.reload(mod)

        def _restaurar() -> None:
            ruta.write_text(original, encoding="utf-8")
            _recargar()

        self.addCleanup(_restaurar)
        ruta.write_text(original.replace(antes, despues), encoding="utf-8")
        _recargar()

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
