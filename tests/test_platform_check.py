import contextlib
import io
import sys
import unittest
from unittest import mock

from thetvview import platform_check as pc


class TestDetectOs(unittest.TestCase):
    def test_linux(self):
        self.assertEqual(pc.detect_os("Linux"), pc.OS_LINUX)

    def test_windows(self):
        self.assertEqual(pc.detect_os("Windows"), pc.OS_WINDOWS)

    def test_macos(self):
        self.assertEqual(pc.detect_os("Darwin"), pc.OS_MACOS)

    def test_aliases_windows(self):
        for raw in ("MSYS_NT-10.0", "MINGW64_NT-10.0", "CYGWIN_NT-10.0", "win32"):
            self.assertEqual(pc.detect_os(raw), pc.OS_WINDOWS, raw)

    def test_sistema_exotico_es_desconocido(self):
        self.assertEqual(pc.detect_os("FreeBSD"), pc.OS_UNKNOWN)
        self.assertEqual(pc.detect_os("SunOS"), pc.OS_UNKNOWN)

    def test_vacio_usa_os_name(self):
        # platform.system() rara vez devuelve '': se cae a os.name/sys.platform
        self.assertIn(pc.detect_os(""), (pc.OS_LINUX, pc.OS_WINDOWS, pc.OS_MACOS))

    def test_none_detecta_de_verdad(self):
        self.assertIn(
            pc.detect_os(None), (pc.OS_LINUX, pc.OS_WINDOWS, pc.OS_MACOS, pc.OS_UNKNOWN)
        )


class TestResolveOs(unittest.TestCase):
    def test_deteccion_clara_no_pregunta(self):
        def _no_llamar(_prompt: str) -> str:
            raise AssertionError("no debería preguntar si la detección es clara")

        out = io.StringIO()
        self.assertEqual(
            pc.resolve_os("Windows", input_fn=_no_llamar, output=out), pc.OS_WINDOWS
        )
        self.assertEqual(out.getvalue(), "")

    def test_desconocido_pregunta_y_acepta_respuestas(self):
        for answer, expected in (
            ("w", pc.OS_WINDOWS),
            ("W", pc.OS_WINDOWS),
            ("windows", pc.OS_WINDOWS),
            ("l", pc.OS_LINUX),
            ("linux", pc.OS_LINUX),
            ("", None),  # predeterminado del sistema
        ):
            with self.subTest(answer=answer):
                got = pc.resolve_os(
                    "FreeBSD",
                    input_fn=lambda _p, a=answer: a,
                    output=io.StringIO(),
                )
                if expected is None:
                    default = pc.OS_WINDOWS if sys.platform == "win32" else pc.OS_LINUX
                    self.assertEqual(got, default)
                else:
                    self.assertEqual(got, expected)

    def test_desconocido_sin_stdin_usa_predeterminado(self):
        def _eof(_prompt: str) -> str:
            raise EOFError

        got = pc.resolve_os("FreeBSD", input_fn=_eof, output=io.StringIO())
        default = pc.OS_WINDOWS if sys.platform == "win32" else pc.OS_LINUX
        self.assertEqual(got, default)

    def test_desconocido_escribe_aviso(self):
        out = io.StringIO()
        pc.resolve_os("FreeBSD", input_fn=lambda _p: "l", output=out)
        self.assertIn("no pudo identificar", out.getvalue())


class TestCursesProblem(unittest.TestCase):
    def test_ok_con_curses_y_tty(self):
        with mock.patch.object(pc, "_is_interactive_terminal", return_value=True):
            self.assertIsNone(pc.curses_problem(pc.OS_LINUX))

    def test_sin_curses_en_windows_aconseja_windows_curses(self):
        with mock.patch.dict(sys.modules, {"curses": None}):
            msg = pc.curses_problem(pc.OS_WINDOWS)
        self.assertIsNotNone(msg)
        self.assertIn("windows-curses", msg)

    def test_sin_curses_en_linux_aconseja_python3_curses(self):
        with mock.patch.dict(sys.modules, {"curses": None}):
            msg = pc.curses_problem(pc.OS_LINUX)
        self.assertIsNotNone(msg)
        self.assertIn("python3-curses", msg)

    def test_sin_tty_pide_consola_real(self):
        with mock.patch.object(pc, "_is_interactive_terminal", return_value=False):
            msg = pc.curses_problem(pc.OS_LINUX)
        self.assertIsNotNone(msg)
        self.assertIn("terminal interactiva", msg)


class TestPrepareConsole(unittest.TestCase):
    def test_fuerza_utf8_en_stdout_y_stderr(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="latin-1")
        with mock.patch.object(sys, "stdout", stream), mock.patch.object(
            sys, "stderr", stream
        ):
            pc.prepare_console(pc.OS_LINUX)
        self.assertEqual(stream.encoding, "utf-8")

    def test_streams_sin_reconfigure_no_explotan(self):
        fake = mock.Mock(spec=[])
        with mock.patch.object(sys, "stdout", fake), mock.patch.object(
            sys, "stderr", fake
        ):
            pc.prepare_console(pc.OS_LINUX)

    def test_windows_en_posix_no_llama_al_kernel32(self):
        # En un SO distinto de Windows la preparación debe ser inocua.
        fake = mock.Mock(spec=[])
        with mock.patch.object(sys, "stdout", fake), mock.patch.object(
            sys, "stderr", fake
        ):
            pc.prepare_console(pc.OS_WINDOWS)

    def test_windows_despacha_la_preparacion_de_consola(self):
        fake = mock.Mock(spec=[])
        with mock.patch.object(sys, "stdout", fake), mock.patch.object(
            sys, "stderr", fake
        ), mock.patch.object(pc, "_windows_console_utf8") as inner:
            pc.prepare_console(pc.OS_WINDOWS)
            inner.assert_called_once()


class TestRun(unittest.TestCase):
    def test_arranca_ui_tras_preflight(self):
        from thetvview import __main__ as entry

        with mock.patch.object(
            entry, "resolve_os", return_value=pc.OS_LINUX
        ) as resolve, mock.patch.object(entry, "prepare_console") as prep, mock.patch.object(
            entry, "curses_problem", return_value=None
        ), mock.patch.object(entry, "_launch") as launch:
            code = entry.run()

        self.assertEqual(code, 0)
        resolve.assert_called_once()
        prep.assert_called_once_with(pc.OS_LINUX)
        launch.assert_called_once()

    def test_aviso_de_curses_no_lanza_ui_y_sale_con_1(self):
        from thetvview import __main__ as entry

        stderr = io.StringIO()
        with mock.patch.object(entry, "resolve_os", return_value=pc.OS_WINDOWS), mock.patch.object(
            entry, "prepare_console"
        ), mock.patch.object(
            entry, "curses_problem", return_value="pip install windows-curses"
        ), mock.patch.object(
            entry, "_launch"
        ) as launch, contextlib.redirect_stderr(stderr):
            code = entry.run()

        self.assertEqual(code, 1)
        launch.assert_not_called()
        self.assertIn("windows-curses", stderr.getvalue())

    def test_ctrl_c_sale_limpio(self):
        from thetvview import __main__ as entry

        with mock.patch.object(entry, "resolve_os", return_value=pc.OS_LINUX), mock.patch.object(
            entry, "prepare_console"
        ), mock.patch.object(entry, "curses_problem", return_value=None), mock.patch.object(
            entry, "_launch", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(entry.run(), 0)


if __name__ == "__main__":
    unittest.main()
