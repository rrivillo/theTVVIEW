"""Fase 8 · U-08 — Grabación: autorización y formulario.

Cubre el gate `can_record()`, el primer modal de autorización con el aviso
DRM, el formulario interactivo con validación del backend, y el flujo
`_grabar` completo (gate + autorización + formulario).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thetvview.ui import recording as recording_mod
from thetvview.ui.app import App
from thetvview.ui.widgets import FormModal


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
                     "PREFS_JSON", "PLAYLISTS_JSON", "THEME_JSON", "RECORDINGS_DIR"):
            self._cfg_backup[name] = getattr(cfg, name, None)
        for name in ("COLLECTIONS_JSON", "FAVORITES_JSON", "RECENTS_JSON",
                     "PREFS_JSON", "PLAYLISTS_JSON", "THEME_JSON"):
            setattr(cfg, name, tmp / f"{name}.json")
        cfg.RECORDINGS_DIR = tmp / "grabaciones"
        cfg.ensure_dirs = lambda: None

        def _restore_cfg():
            for name, value in self._cfg_backup.items():
                if value is not None:
                    setattr(cfg, name, value)

        self.addCleanup(_restore_cfg)
        from thetvview.ui import colors
        mock.patch.object(colors, "pair", return_value=0).start()
        self.addCleanup(mock.patch.stopall)
        self.app = App(self.fake)


class TestGate(Base):
    def test_puede_grabar_false_sin_vlc(self) -> None:
        with mock.patch.object(recording_mod, "puede_grabar", return_value=False):
            self.assertFalse(self.app.puede_grabar())

    def test_puede_grabar_true_con_vlc(self) -> None:
        with mock.patch.object(recording_mod, "puede_grabar", return_value=True):
            self.assertTrue(self.app.puede_grabar())

    def test_can_record_alias(self) -> None:
        with mock.patch.object(recording_mod, "puede_grabar", return_value=True):
            self.assertTrue(self.app.can_record())

    def test_gate_exception_degrada_a_false(self) -> None:
        with mock.patch.object(recording_mod, "puede_grabar", side_effect=RuntimeError):
            self.assertFalse(self.app.puede_grabar())


class TestAutorizacionModal(Base):
    def test_modal_autorizacion_con_aviso_drm(self) -> None:
        with mock.patch.object(recording_mod, "puede_grabar", return_value=True):
            modal = self.app._modal_de_autorizacion_grabacion()
            texto = str(getattr(modal, "message", ""))
            self.assertIn("DRM", texto)

    def test_modal_autorizacion_sin_vlc_muestra_motivo(self) -> None:
        with mock.patch.object(recording_mod, "puede_grabar", return_value=False):
            with mock.patch.object(recording_mod, "backend_reason",
                                   return_value="VLC no está instalado"):
                modal = self.app._modal_de_autorizacion_grabacion()
                texto = str(getattr(modal, "message", ""))
                self.assertIn("no está disponible", texto)
                self.assertIn("VLC", texto)

    def test_autorizacion_cancelar_devuelve_false(self) -> None:
        with mock.patch.object(recording_mod, "can_record", return_value=True):
            with mock.patch.object(self.app, "_run_modal", return_value="cancelar"):
                self.assertFalse(self.app._autorizar_grabacion())

    def test_autorizacion_continuar_devuelve_true(self) -> None:
        with mock.patch.object(recording_mod, "can_record", return_value=True):
            with mock.patch.object(self.app, "_run_modal", return_value="continuar"):
                self.assertTrue(self.app._autorizar_grabacion())

    def test_autorizacion_sin_vlc_notifica_y_devuelve_false(self) -> None:
        with mock.patch.object(recording_mod, "puede_grabar", return_value=False):
            with mock.patch.object(recording_mod, "backend_reason",
                                   return_value="VLC no está instalado"):
                with mock.patch.object(self.app, "notify_warning") as nw:
                    self.assertFalse(self.app._autorizar_grabacion())
                    nw.assert_called_once()


class TestFormularioModal(Base):
    def test_modal_formulario_es_formmodal(self) -> None:
        modal = self.app._modal_de_formulario_grabacion()
        self.assertIsInstance(modal, FormModal)

    def test_modal_formulario_tiene_tres_campos(self) -> None:
        modal = self.app._modal_de_formulario_grabacion()
        self.assertEqual(len(modal.fields), 3)

    def test_modal_formulario_campos_correctos(self) -> None:
        modal = self.app._modal_de_formulario_grabacion()
        keys = [f[0] for f in modal.fields]
        self.assertIn("destino", keys)
        self.assertIn("nombre", keys)
        self.assertIn("duracion", keys)

    def test_modal_formulario_initial_destino_default(self) -> None:
        import thetvview.config as cfg
        modal = self.app._modal_de_formulario_grabacion()
        self.assertEqual(modal.values.get("destino"), str(cfg.RECORDINGS_DIR))

    def test_modal_formulario_initial_con_valores(self) -> None:
        modal = self.app._modal_de_formulario_grabacion(
            destino="/tmp/x", nombre="mi_canal", duracion=3600
        )
        self.assertEqual(modal.values["destino"], "/tmp/x")
        self.assertEqual(modal.values["nombre"], "mi_canal")
        self.assertEqual(modal.values["duracion"], "3600")


class TestFormularioGrabacion(Base):
    def test_validar_formulario_devuelve_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(recording_mod, "validar_destino") as vd:
                vd.return_value = Path(tmp) / "test.ts"
                ruta = recording_mod.validar_formulario(tmp, "test.ts", duracion=60)
                self.assertEqual(ruta, Path(tmp) / "test.ts")

    def test_validar_formulario_duracion_invalida_lanza(self) -> None:
        with self.assertRaises(Exception):
            recording_mod.validar_formulario("/tmp", "test.ts", duracion=-5)

    def test_validar_formulario_duracion_no_numerica_lanza(self) -> None:
        with self.assertRaises(Exception):
            recording_mod.validar_formulario("/tmp", "test.ts", duracion="abc")

    def test_formulario_grabacion_class_valida_duracion(self) -> None:
        with self.assertRaises(Exception):
            recording_mod.FormularioGrabacion(
                destino="/tmp", nombre="x", duracion=-1
            ).validar()

    def test_formulario_grabacion_class_path_property(self) -> None:
        with mock.patch.object(recording_mod, "validar_destino") as vd:
            vd.return_value = Path("/tmp/x.ts")
            form = recording_mod.FormularioGrabacion(destino="/tmp", nombre="x", duracion=60)
            self.assertEqual(form.path, Path("/tmp/x.ts"))

    def test_formulario_grabacion_class_valida_bien(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            form = recording_mod.FormularioGrabacion(destino=tmp, nombre="x", duracion=60)
            ruta = form.validar()
            self.assertEqual(ruta.parent, Path(tmp))


class TestGrabarFlujo(Base):
    def test_grabar_sin_vlc_devuelve_false(self) -> None:
        with mock.patch.object(recording_mod, "puede_grabar", return_value=False):
            self.assertFalse(self.app._grabar())

    def test_grabar_cancelar_autorizacion_devuelve_false(self) -> None:
        with mock.patch.object(recording_mod, "puede_grabar", return_value=True):
            with mock.patch.object(self.app, "_autorizar_grabacion", return_value=False):
                self.assertFalse(self.app._grabar())

    def test_grabar_formulario_valido_devuelve_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(recording_mod, "puede_grabar", return_value=True):
                with mock.patch.object(self.app, "_autorizar_grabacion", return_value=True):
                    with mock.patch.object(recording_mod, "validar_destino") as vd:
                        vd.return_value = Path(tmp) / "out.ts"
                        ruta = self.app._grabar(destino=tmp, nombre="out.ts", duracion=60)
                        self.assertEqual(ruta, Path(tmp) / "out.ts")

    def test_grabar_formulario_invalido_devuelve_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(recording_mod, "puede_grabar", return_value=True):
                with mock.patch.object(self.app, "_autorizar_grabacion", return_value=True):
                    with mock.patch.object(recording_mod, "validar_destino",
                                           side_effect=Exception("inválido")):
                        self.assertIsNone(self.app._grabar(destino=tmp, nombre="x", duracion=60))


class TestAliasModules(Base):
    def test_modulos_alias_existen(self) -> None:
        from thetvview.ui import grabacion
        from thetvview.ui import grabacion_view
        from thetvview.ui import grabacion_ui
        from thetvview.ui import recording_view
        from thetvview.ui import recording_ui
        self.assertTrue(hasattr(grabacion, "puede_grabar"))
        self.assertTrue(hasattr(grabacion_view, "puede_grabar"))
        self.assertTrue(hasattr(grabacion_ui, "puede_grabar"))
        self.assertTrue(hasattr(recording_view, "puede_grabar"))
        self.assertTrue(hasattr(recording_ui, "puede_grabar"))

    def test_constantes_helper(self) -> None:
        self.assertIn("DRM", recording_mod.TEXTO_AUTORIZACION)
        self.assertIn("suscripción", recording_mod.TEXTO_AUTORIZACION.lower())


class TestNormalizarNombre(Base):
    def test_normalizar_nombre_espacios(self) -> None:
        # El backend solo hace strip, no reemplaza espacios internos.
        self.assertEqual(recording_mod.normalizar_nombre("  mi canal  "), "mi canal")

    def test_normalizar_nombre_antislash(self) -> None:
        self.assertEqual(recording_mod.normalizar_nombre("a/b"), "a_b")

    def test_normalizar_nombre_vacio_lanza(self) -> None:
        with self.assertRaises(Exception):
            recording_mod.normalizar_nombre("")


if __name__ == "__main__":
    main()
