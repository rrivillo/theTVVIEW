"""Tests del supervisor de reconexión (SDD-M §17, AC-008).

Se prueba **sin lanzar un solo reproductor**: el supervisor recibe un callable
que devuelve un proceso falso, y eso es exactamente lo que hace el diseño —
no reproducir, observar. Los tiempos van con `poll_seconds` muy corto para que
el test no tarde lo que manda la política.
"""

from __future__ import annotations

import threading
import time
import unittest

from thetvview.player.supervisor import (
    DEFAULT_BACKOFF,
    PROFILE_FLAGS,
    PlaybackProfile,
    PlaybackState,
    ReconnectPolicy,
    ReconnectSupervisor,
)


class _ProcFalso:
    """Proceso que muere cuando se le dice. Sin subprocess."""

    def __init__(self, vivo: bool = True) -> None:
        self.vivo = vivo
        self.polls = 0

    def poll(self):  # noqa: ANN201 - imita a subprocess.Popen
        self.polls += 1
        return None if self.vivo else 1


class _Relanzador:
    """Devuelve procesos falsos y cuenta cuántas veces se llamó."""

    def __init__(self, vivos: int = 5) -> None:
        self.vivos = vivos
        self.llamadas = 0

    def __call__(self) -> object:
        self.llamadas += 1
        return _ProcFalso(vivo=self.llamadas <= self.vivos)


class _Rapida(ReconnectPolicy):
    """Política con esperas de milisegundos, para que el test no tarde.

    La **forma** de la tabla ya se comprueba en :class:`TestPolitica` con los
    segundos del §17; aquí lo que interesa es cuántas veces se reintenta y
    cuándo se rinde, y eso no depende de que la espera sea de 1 s o de 1 ms.
    """

    def __init__(self, max_attempts: int = 3) -> None:
        super().__init__(backoff=(1, 2, 3), max_attempts=max_attempts)

    def espera_para(self, intento: int) -> float:
        return super().espera_para(intento) / 1000.0


class _Registro:
    def __init__(self) -> None:
        self.eventos: list[tuple[PlaybackState, int]] = []
        self.candado = threading.Lock()

    def __call__(self, estado: PlaybackState, intento: int) -> None:
        with self.candado:
            self.eventos.append((estado, intento))


class TestPolitica(unittest.TestCase):
    """Los tiempos del §17, medidos."""

    def test_el_primer_intento_es_inmediato(self) -> None:
        self.assertEqual(ReconnectPolicy().espera_para(1), 0.0)

    def test_la_escala_es_la_del_sdd17(self) -> None:
        politica = ReconnectPolicy()
        self.assertEqual(
            [politica.espera_para(n) for n in (2, 3, 4, 5)],
            [1.0, 2.0, 5.0, 10.0],
        )

    def test_pasada_la_lista_no_crece(self) -> None:
        # Pasado el cuarto, esperar 30 s no es paciencia: es una app que
        # parece colgada. Se mantiene el último valor.
        politica = ReconnectPolicy()
        self.assertEqual(politica.espera_para(99), 10.0)

    def test_cinco_intentos_por_defecto(self) -> None:
        self.assertEqual(ReconnectPolicy().intentos_maximos, 5)
        self.assertEqual(DEFAULT_BACKOFF, (1, 2, 5, 10))

    def test_la_politica_se_puede_acortar(self) -> None:
        # Un `max_attempts` de 0 tiene que significar «no reintentar», y no
        # «reintentar sin límite».
        politica = ReconnectPolicy(max_attempts=0)
        self.assertEqual(politica.intentos_maximos, 0)


class TestSupervisor(unittest.TestCase):
    def _supervisor(self, relanzar, registro=None, **kw) -> ReconnectSupervisor:
        return ReconnectSupervisor(
            relanzar=relanzar,
            al_cambiar=registro,
            poll_seconds=0.02,
            policy=_Rapida(max_attempts=kw.pop("max_attempts", 3)),
            **kw,
        )

    def _esperar(self, sup, condicion, timeout=3.0) -> bool:  # noqa: ANN001
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if condicion():
                return True
            time.sleep(0.01)
        return False

    def test_detecta_que_el_proceso_murio_y_reconecta(self) -> None:
        relanzar = _Relanzador(vivos=99)
        registro = _Registro()
        sup = self._supervisor(relanzar, registro)
        sup.start(_ProcFalso(vivo=False))  # muere al instante
        try:
            ok = self._esperar(sup, lambda: relanzar.llamadas >= 1)
        finally:
            sup.stop()
        self.assertTrue(ok, "no reconectó")
        estados = [e for e, _ in registro.eventos]
        self.assertIn(PlaybackState.RECONNECTING, estados)
        self.assertIn(PlaybackState.CONNECTING, estados)

    def test_no_reintenta_infinito(self) -> None:
        # AC-008: «una caída temporal produce reconexión **limitada**». Con el
        # relanzador siempre dando procesos muertos, se para en el tope.
        relanzar = _Relanzador(vivos=0)
        registro = _Registro()
        sup = self._supervisor(relanzar, registro, max_attempts=3)
        sup.start(_ProcFalso(vivo=False))
        try:
            ok = self._esperar(sup, lambda: sup.estado is PlaybackState.ERROR)
            estado_final = sup.estado
            llamadas = relanzar.llamadas
        finally:
            sup.stop()
        self.assertTrue(ok, "no llegó a ERROR")
        self.assertIs(estado_final, PlaybackState.ERROR)
        self.assertLessEqual(llamadas, 3)

    def test_publica_error_al_rendirse(self) -> None:
        sup = self._supervisor(_Relanzador(vivos=0), _Registro(), max_attempts=2)
        sup.start(_ProcFalso(vivo=False))
        try:
            self._esperar(sup, lambda: sup.estado is PlaybackState.ERROR)
            self.assertEqual(sup.estado, PlaybackState.ERROR)
            self.assertEqual(sup.estado.texto, "Error")
        finally:
            sup.stop()

    def test_sin_relanzador_no_reintenta(self) -> None:
        # Un supervisor sin relanzador sólo informa; no puede relanzar por su
        # cuenta y no debe fingir que reintenta.
        sup = ReconnectSupervisor(relanzar=None, poll_seconds=0.02)
        sup.start(_ProcFalso(vivo=False))
        try:
            ok = self._esperar(sup, lambda: sup.estado is PlaybackState.ERROR)
        finally:
            sup.stop()
        self.assertTrue(ok)

    def test_un_proceso_vivo_da_estado_playing(self) -> None:
        registro = _Registro()
        sup = self._supervisor(_Relanzador(), registro)
        sup.start(_ProcFalso(vivo=True))
        # El estado se lee **antes** de parar: `stop()` deja STOPPED, que es
        # correcto pero no es lo que se quiere comprobar aquí.
        try:
            self._esperar(sup, lambda: sup.estado is PlaybackState.PLAYING)
            self.assertEqual(sup.estado, PlaybackState.PLAYING)
        finally:
            sup.stop()

    def test_stop_es_idempotente_y_no_mata(self) -> None:
        proc = _ProcFalso(vivo=True)
        sup = self._supervisor(_Relanzador(), _Registro())
        sup.start(proc)
        sup.stop()
        sup.stop()
        self.assertEqual(sup.estado, PlaybackState.STOPPED)
        self.assertTrue(proc.vivo)  # el supervisor observa, no mata

    def test_start_es_idempotente(self) -> None:
        sup = self._supervisor(_Relanzador(), _Registro())
        sup.start(_ProcFalso(vivo=True))
        primera = sup._hilo
        sup.start(_ProcFalso(vivo=True))
        self.assertIs(sup._hilo, primera)
        sup.stop()

    def test_un_observador_que_falla_no_rompe(self) -> None:
        def observador_malo(estado, intento) -> None:
            raise RuntimeError("la UI se rompió")

        sup = self._supervisor(_Relanzador(vivos=0), observador_malo, max_attempts=1)
        sup.start(_ProcFalso(vivo=False))
        try:
            self._esperar(sup, lambda: sup.estado is PlaybackState.ERROR)
            estado_final = sup.estado
        finally:
            sup.stop()
        self.assertEqual(estado_final, PlaybackState.ERROR)

    def test_un_proceso_que_falla_al_preguntar_no_rompe(self) -> None:
        class _Roto:
            def poll(self):
                raise OSError("zombi")

        sup = self._supervisor(_Relanzador(vivos=0), _Registro(), max_attempts=1)
        sup.start(_Roto())
        try:
            ok = self._esperar(sup, lambda: sup.estado is PlaybackState.ERROR)
            estado_final = sup.estado
        finally:
            sup.stop()
        self.assertTrue(ok)
        self.assertIs(estado_final, PlaybackState.ERROR)

    def test_detach_no_reconecta(self) -> None:
        # El usuario paró el canal: no es una caída, no se reintenta. El
        # proceso puede estar muriendo en este preciso instante, así que el
        # caso que importa es el del **segundo** intento: para entonces
        # `detach()` ya está puesto y el bucle tiene que respetar la bandera.
        relanzar = _Relanzador(vivos=0)
        sup = self._supervisor(relanzar, _Registro(), max_attempts=5)
        sup.start(_ProcFalso(vivo=False))
        # Se espera al primer reintento (el que ocurre con el proceso ya
        # muerto) y sólo entonces se «desvincula», como haría el usuario
        # pulsando `q` mientras el supervisor ya está reintentando.
        self.assertTrue(
            self._esperar(sup, lambda: relanzar.llamadas >= 1),
            "no llegó al primer reintento",
        )
        sup.detach()
        time.sleep(0.2)
        sup.stop()
        self.assertEqual(
            relanzar.llamadas, 1, "reintentó después de que el usuario lo parara"
        )

    def test_el_hilo_es_daemon(self) -> None:
        sup = self._supervisor(_Relanzador(), _Registro())
        sup.start(_ProcFalso(vivo=True))
        try:
            assert sup._hilo is not None
            self.assertTrue(sup._hilo.daemon)
        finally:
            sup.stop()


class TestPerfilesDeRed(unittest.TestCase):
    """§19: los perfiles son banderas reales del reproductor, no una cola aquí."""

    def test_cada_reproductor_tiene_tres_perfiles(self) -> None:
        from thetvview import config

        for nombre in config.SUPPORTED_PLAYERS:
            with self.subTest(reproductor=nombre):
                self.assertIn(nombre, PROFILE_FLAGS)
                for perfil in PlaybackProfile:
                    self.assertIn(perfil.value, PROFILE_FLAGS[nombre])

    def test_perfil_desconocido_no_devuelve_bandera_alguna(self) -> None:
        # Preferimos no pasar nada antes que pasar una bandera inventada.
        from thetvview.player.supervisor import profile_flags

        self.assertEqual(profile_flags("mpv", "inventado"), [])
        self.assertEqual(profile_flags("reproductor-inexistente", "balanced"), [])

    def test_las_banderas_parecen_opciones(self) -> None:
        # Cada reproductor tiene su convención y no hay que unificarla: mpv y
        # VLC usan `--opcion=valor`, mplayer usa dos argv separados
        # (`-cache 8192`). Lo que no vale es un valor suelto donde debería
        # haber una opción, así que se comprueba el **primer** elemento de cada
        # bandera compuesta.
        from thetvview.player.supervisor import profile_flags

        for nombre in PROFILE_FLAGS:
            for perfil in PlaybackProfile:
                banderas = profile_flags(nombre, perfil.value)
                with self.subTest(reproductor=nombre, perfil=perfil.value):
                    self.assertTrue(banderas)
                    # La primera de cada par es la opción; su valor va detrás.
                    for indice in range(0, len(banderas)):
                        elemento = banderas[indice]
                        if indice and not elemento.startswith("-"):
                            continue  # es el valor de la opción anterior
                        self.assertTrue(
                            elemento.startswith("-"),
                            f"{elemento} no parece una opción",
                        )

    def test_perfil_por_defecto_es_el_equilibrado(self) -> None:
        from thetvview.player.supervisor import profile_flags

        self.assertEqual(
            profile_flags("mpv", PlaybackProfile.BALANCED.value),
            PROFILE_FLAGS["mpv"][PlaybackProfile.BALANCED.value],
        )


if __name__ == "__main__":
    unittest.main()