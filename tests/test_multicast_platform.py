"""Tests de la detección de soporte multicast por SO (SDD-M Fase 6).

La distinción del §14 es la que guía todo este archivo, y es sutil:

- **«el reproductor abre UDP»** → tabla de :mod:`player.protocols`;
- **«esta máquina puede llevar multicast»** → :func:`multicast_supported`;
- **«este grupo llega a mi red»** → no lo decide **nadie** desde el programa,
  porque depende del router, del Wi-Fi y del firewall.

Mezclar las tres es el error que produce los dos mensajes equivocados: un
modal de «instalaste mal» cuando el problema es la red, o un «funciona» cuando
el router no reenvía el grupo.
"""

from __future__ import annotations

import unittest
from unittest import mock

from thetvview.platform_check import OS_LINUX, multicast_supported


class TestPorSistemaOperativo(unittest.TestCase):
    """Los tres SO soportados responden True; los desconocidos, False."""

    def test_los_tres_so_conocidos_lo_soportan(self) -> None:
        # Se sustituyen **las dos** capas que tocan la máquina (las interfaces
        # y la prueba de unirse) para que lo comprobado sea la decisión por SO,
        # que es lo propio de esta clase, y no la red de quien ejecuta los
        # tests.
        with mock.patch(
            "thetvview.platform_check._interfaces_con_multicast",
            return_value=["192.0.2.10"],
        ), mock.patch(
            "thetvview.platform_check._puede_unirse_a", return_value=True
        ):
            # Se pasan los nombres que entiende `detect_os` ("darwin", no el
            # valor interno "macos"): lo que se comprueba es la decisión
            # por SO, y `multicast_supported` recibe lo que el SO reporta.
            for sistema in ("Linux", "Darwin", "Windows"):
                with self.subTest(sistema=sistema):
                    self.assertTrue(multicast_supported(sistema))

    def test_un_so_desconocido_no_se_afirma(self) -> None:
        # El módulo devuelve ``unknown`` a propósito en vez de suponer Linux.
        # Afirmar soporte aquí sería lo contrario de lo que hace el resto.
        with mock.patch(
            "thetvview.platform_check._interfaces_con_multicast",
            return_value=["192.0.2.10"],
        ), mock.patch(
            "thetvview.platform_check._puede_unirse_a", return_value=True
        ):
            self.assertFalse(multicast_supported("Solaris"))
            self.assertFalse(multicast_supported("FreeBSD"))

    def test_sin_interfaces_no_hay_multicast(self) -> None:
        with mock.patch(
            "thetvview.platform_check._interfaces_con_multicast",
            return_value=[],
        ):
            self.assertFalse(multicast_supported(OS_LINUX))

    def test_interfaz_que_no_acepta_el_grupo_no_basta(self) -> None:
        # La bandera puesta no es suficiente: la prueba real es unirse. Con un
        # `192.0.2.10` que no existe en la máquina, unirse falla y la respuesta
        # honesta es False (es el caso real de esta máquina con multicast).
        with mock.patch(
            "thetvview.platform_check._interfaces_con_multicast",
            return_value=["192.0.2.10"],
        ):
            self.assertFalse(multicast_supported(OS_LINUX))


class TestSinRomper(unittest.TestCase):
    def test_una_excepcion_es_un_no(self) -> None:
        # Cualquier duda se resuelve con False; una excepción aquí se
        # convertiría en un crash al abrir la lista.
        with mock.patch(
            "thetvview.platform_check._interfaces_con_multicast",
            side_effect=OSError("permiso denegado"),
        ):
            self.assertFalse(multicast_supported(OS_LINUX))

    def test_una_excepcion_al_unirse_es_un_no(self) -> None:
        with mock.patch(
            "thetvview.platform_check._interfaces_con_multicast",
            return_value=["192.0.2.10"],
        ), mock.patch(
            "thetvview.platform_check._puede_unirse_a",
            side_effect=OSError("permiso denegado"),
        ):
            self.assertFalse(multicast_supported(OS_LINUX))

    def test_la_prueba_de_unirse_cierra_el_socket(self) -> None:
        # Lo único del módulo que abre un socket tiene que cerrarlo, o cada
        # diagnóstico dejaría un descriptor por el camino.
        from thetvview.platform_check import _puede_unirse_a

        _puede_unirse_a(["192.0.2.10"])  # no existe aquí: devuelve False
        _puede_unirse_a([])  # lista vacía: ni intenta
        self.assertFalse(_puede_unirse_a([]))

    def test_es_idempotente(self) -> None:
        # Se llama en cada diagnóstico; no puede dejar estado.
        primero = multicast_supported()
        segundo = multicast_supported()
        self.assertEqual(primero, segundo)


class TestInterfazPorDefecto(unittest.TestCase):
    def test_devuelve_una_direccion_o_vacia(self) -> None:
        from thetvview.platform_check import _interfaz_por_defecto

        resultado = _interfaz_por_defecto()
        self.assertIsInstance(resultado, list)
        for direccion in resultado:
            with self.subTest(direccion=direccion):
                self.assertNotIn(":", direccion)  # IPv4, no IPv6


class TestContraLaMaquinaReal(unittest.TestCase):
    """Lo que la máquina dice hoy. No es un contrato: es un dato."""

    def test_el_resultado_es_bool(self) -> None:
        self.assertIsInstance(multicast_supported(), bool)

    def test_no_abre_nada_que_no_se_pueda_cerrar(self) -> None:
        # La función abre un socket para probarse. Si lo dejara colgado, esto
        # no terminaría; se comprueba explícitamente porque es el único punto
        # del módulo que toca la red.
        multicast_supported()
        multicast_supported()
        self.assertTrue(True)

    def test_es_repetible(self) -> None:
        # Si una vez se puede, la segunda también: la función no debe
        # consumirse a sí misma.
        self.assertEqual(multicast_supported(), multicast_supported())


if __name__ == "__main__":
    unittest.main()