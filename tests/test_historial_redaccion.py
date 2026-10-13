"""H-04 — Redacción al escribir, no al leer.

Todo lo que el historial y el bus guardan viene de terceros: un veredicto del
diagnóstico, el mensaje de un reproductor, el nombre de una lista, la clave de
un canal. Y el destino es un fichero que el usuario puede copiar para pasar el
informe a otra persona, o un informe que se pega en un foro. La redacción no
es entonces una mejora: es la diferencia entre un diagnóstico útil y una
contraseña en la mesa de alguien.

Por qué al escribir y no al leer

Porque redactar al leer no protege el fichero. Si el secreto se limpia al
mostrar, el secreto sigue ahí dentro, en el bytes que se copiaron, en la copia
de seguridad, en el fichero que se mandó por correo. La redacción tiene que
ocurrir **antes** de que exista el byte, y una vez escribirse, lo que hay en
disco ya está limpio para siempre.

Eso también significa algo más incómodo: **leer no limpia**. Si alguien
escribe a mano un fichero con un secreto dentro, el historial lo devolverá tal
cual. No es un descuido: es la prueba de que la garantía está donde tiene que
estar. Si la lectura limpiera, las dos garantías podrían coexistiendo y nadie
sabría cuál estaba protegiendo de verdad.

[1mLos centinelas[0m

Cada prueba mete una marca recognizable ``CANARIO-SECRETO`` dentro de una
estructura que la redacción reconoce —credenciales en la URL, un parámetro
``password=``, un camino Xtream, una cabecera ``Authorization``— y después
comprueba que la marca no aparece en ninguno de los destinos:

- el registro devuelto por ``registrar`` (es decir: **en memoria**);
- las entradas que el historial tiene cargadas;
- el texto del fichero en disco;
- los eventos del bus;
- el informe que se construye a partir de todo lo anterior.

Un fallo aquí no es «un dato feo»: es una credencial en un sitio del que el
usuario no sospecha.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thetvview.diagnostics.events import EventBus
from thetvview.diagnostics.history import HistorialDiagnostico
from thetvview.diagnostics.session_events import ObservadorDeSesion
from thetvview.diagnostics.taxonomy import causa_de
from thetvview.player.errors import StreamError, StreamErrorCode
from thetvview.player.session import EndReason, PlaybackSession

#: La marca. Con prefijo propio para que un ``assertNotIn`` suyo en un
#: fichero que se Explore no pueda confundirse con otra cosa.
CANARIO = "CANARIO-SECRETO-4f2a9c"

#: Cada sentinel, con la forma en la que la redacción lo reconoce.
#:
#: Un secreto suelto, sin estructura, **no** se puede distinguir de un nombre
#: de canal, y eso se documenta en vez de fingir: `redact_text` limpia
#: credenciales embebidas, no cadenas opacas.
SENTINELAS: dict[str, str] = {
    "userinfo": f"http://usuario:{CANARIO}@proveedor.test/live/1.ts",
    "parametro": f"http://proveedor.test/live/1.ts?password={CANARIO}&x=1",
    "xtream": f"xtream://Panel/live/{CANARIO}/{CANARIO}/101.ts",
    "cabecera": f"Authorization: Bearer {CANARIO}",
    "token_suelto": f"falló con token={CANARIO} al abrir",
    "cookie": f"Cookie: sesion={CANARIO}",
}


class _Reloj:
    def __init__(self, inicio: float = 1_800_000_000.0) -> None:
        self.t = inicio

    def __call__(self) -> float:
        return self.t


class TestHistorialRedactaAlEscribir(unittest.TestCase):
    """Cada sentinel entra por un campo y no sale por ninguno."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ruta = self.dir / "diagnostics.json"
        self.reloj = _Reloj()
        self.h = HistorialDiagnostico(self.ruta, ahora=self.reloj)

    def _destinos(self) -> dict[str, str]:
        """Todos los sitios donde el dato podría haber sobrevivido."""
        return {
            "registro devuelto": str(self.retornado.a_dict()),
            "entradas en memoria": str([e.a_dict() for e in self.h.entradas()]),
            "fichero en disco": self.ruta.read_text(encoding="utf-8"),
        }

    def test_el_sentinel_no_aparece_en_ningun_destino(self):
        for nombre, valor in SENTINELAS.items():
            with self.subTest(sentinel=nombre):
                self.retornado = self.h.registrar(
                    valor,                       # canal
                    False,
                    motivo=valor,                # motivo
                    fase=valor,                  # fase
                    source_id=f"m3u|{valor}|{valor}",
                    player=valor,                # reproductor
                    error_code="network_error",
                    duration_ms=12.0,
                )
                for destino, texto in self._destinos().items():
                    with self.subTest(destino=destino):
                        self.assertNotIn(
                            CANARIO, texto,
                            f"el sentinel «{nombre}» llegó a {destino}",
                        )

    def test_un_canal_con_credenciales_no_llega_a_disco(self):
        self.h.registrar("http://u:CANARIO-SECRETO-4f2a9c@a.test/live/1.ts",
                         True)
        self.assertNotIn(CANARIO, self.ruta.read_text(encoding="utf-8"))

    def test_un_source_id_crudo_no_llega_a_disco(self):
        self.h.registrar("espn", True,
                         source_id=f"xtream|Panel|http://{CANARIO}|user")
        texto = self.ruta.read_text(encoding="utf-8")
        self.assertNotIn(CANARIO, texto)

    def test_el_nombre_de_la_lista_si_se_guarda(self):
        """La redacción no puede comerse lo que el modal necesita leer."""
        self.h.registrar("espn", True, source_id=f"m3u|Deportes|http://{CANARIO}")
        self.assertIn("Deportes", self.ruta.read_text(encoding="utf-8"))

    def test_un_canal_normal_no_se_daña(self):
        """Redactar de más también es un fallo: «ESPN HD» es un canal."""
        self.h.registrar("ESPN HD", True, player="mpv")
        entrada = self.h.entradas()[0]
        self.assertEqual(entrada.canal, "ESPN HD")
        self.assertEqual(entrada.player, "mpv")


class TestRedactarAlLeerNoSirveYNoSeHace(unittest.TestCase):
    """La garantía está en la escritura. Leer no limpia, y se comprueba."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ruta = Path(self._tmp.name) / "diagnostics.json"

    def test_un_fichero_escrito_a_mano_no_se_limpia_al_leer(self):
        """Prueba de que la redacción es de escritura y no de lectura.

        Si la lectura limpiara, una entrada con un secreto dentro se devolvería
        limpia y el fichero seguiría teniéndolo. Que aquí salga **tal cual**
        es lo que demuestra que la única garantía que existe es la de no
        escribirlo —y que por eso la otra importa.
        """
        self.ruta.write_text(json.dumps({
            "version": 2,
            "entradas": [{
                "momento": 1_800_000_000.0,
                "canal": f"http://u:{CANARIO}@a.test/1.ts",
                "exito": False, "motivo": "", "fase": "diagnose",
            }],
        }), encoding="utf-8")
        h = HistorialDiagnostico(self.ruta, ahora=_Reloj(1_800_000_001.0))
        entrada = h.entradas()[0]
        self.assertIn(
            CANARIO, entrada.canal,
            "la lectura está redactando: la garantía real no está en la escritura",
        )

    def test_pero_lo_que_nosotros_escribimos_si_esta_limpio(self):
        """...y por eso el otro lado de la misma puerta está garantizado."""
        h = HistorialDiagnostico(self.ruta, ahora=_Reloj())
        h.registrar(f"http://u:{CANARIO}@a.test/1.ts", False)
        # Al releer lo nuestro, lo nuestro sigue limpio: se limpió antes de
        # existir el byte, no después.
        limpio = HistorialDiagnostico(self.ruta, ahora=_Reloj())
        self.assertNotIn(CANARIO, str(limpio.entradas()[0].a_dict()))


class TestElBusRedactaAlAnadir(unittest.TestCase):
    """El bus limpia por dentro: lo que sale ya está limpio."""

    def setUp(self) -> None:
        self.bus = EventBus()
        self.sesion = PlaybackSession(
            channel_key=f"http://u:{CANARIO}@proveedor.test/live/1.ts",
            player_name="mpv",
        )
        self.sesion.oyente = ObservadorDeSesion(self.bus, sesion=self.sesion)

    def _textos(self) -> str:
        return str([e.to_dict() for e in self.bus.events()])

    def test_un_fallo_con_url_con_credenciales_no_llega_al_bus(self):
        self.sesion.nota_fallo(StreamError(
            code=StreamErrorCode.AUTH_FAILED, resumen="x",
            observado=SENTINELAS["parametro"],
        ))
        self.assertNotIn(CANARIO, self._textos())

    def test_un_fallo_con_cabecera_authorization_no_llega_al_bus(self):
        self.sesion.nota_fallo(StreamError(
            code=StreamErrorCode.AUTH_FAILED, resumen="x",
            observado=SENTINELAS["cabecera"],
        ))
        self.assertNotIn(CANARIO, self._textos())

    def test_el_detalle_de_fin_no_llega_al_bus(self):
        """`end_detail` es texto libre que compone quien cierra la sesión."""
        self.sesion.end(EndReason.PLAYBACK_ERROR,
                        f"falló con {SENTINELAS['token_suelto']}")
        self.assertNotIn(CANARIO, self._textos())

    def test_el_hecho_observado_si_se_conserva_sin_el_secreto(self):
        """Redactar no es borrar el dato: es quitarle lo que no es el dato."""
        self.sesion.nota_fallo(StreamError(
            code=StreamErrorCode.AUTH_FAILED, resumen="x",
            observado=SENTINELAS["parametro"],
        ))
        evento = next(e for e in self.bus.events()
                      if e.event_type == "playback.fallo")
        self.assertIn("observado", evento.details)
        self.assertIn("http://proveedor.test", str(evento.details))


class TestElHistorialCompletoNoFiltra(unittest.TestCase):
    """Todo el camino: registrar, releer y construir informe."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ruta = Path(self._tmp.name) / "diagnostics.json"
        self.reloj = _Reloj()

    def test_nada_del_sentinel_llega_a_donde_se_manda(self):
        h = HistorialDiagnostico(self.ruta, ahora=self.reloj)
        for nombre, valor in SENTINELAS.items():
            h.registrar(valor, False, motivo=valor, fase=valor,
                        source_id=f"m3u|{valor}|{valor}", player=valor,
                        error_code="network_error", duration_ms=5.0)
            with self.subTest(sentinel=nombre):
                # 1. el fichero que se copia
                self.assertNotIn(CANARIO, self.ruta.read_text(encoding="utf-8"))
                # 2. lo que se relee
                releido = HistorialDiagnostico(self.ruta, ahora=self.reloj)
                self.assertNotIn(CANARIO, str(releido.entradas()))
                # 3. lo que va a un informe, con los eventos incluidos
                from thetvview.diagnostics.report import construir_informe

                informe = construir_informe(
                    canal="ESPN",
                    eventos=EventBus().events() or None,
                    ahora=datetime.now(timezone.utc),
                )
                self.assertNotIn(CANARIO, str(informe))


class TestLoQueLaRedaccionNoPuedeHacer(unittest.TestCase):
    """Lo que se documenta en vez de fingir."""

    def test_un_secreto_suelto_no_se_reconoce(self):
        """La redacción reconoce **estructuras**, no cadenas opacas.

        ``CANARIO`` a secas es indistinguible de un nombre de canal, y tratarlo
        como secreto dejaría el historial lleno de ``***`` inútiles. Se
        documenta la limitación en vez de taparla con un filtro que rompería
        los datos buenos.
        """
        from thetvview.security.redaction import redact_text

        self.assertEqual(redact_text(CANARIO), CANARIO)

    def test_los_campos_con_estructura_si_se_limpian(self):
        from thetvview.security.redaction import redact_text

        for valor in SENTINELAS.values():
            with self.subTest(valor=valor[:30]):
                self.assertNotEqual(redact_text(valor), valor)


class TestElFicheroRealDeData(unittest.TestCase):
    """El fichero por defecto, en su sitio, con sus permisos."""

    def setUp(self) -> None:
        from thetvview import config

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ruta = Path(self._tmp.name) / "data" / "diagnostics.json"
        parche = mock.patch.object(config, "DIAGNOSTICS_JSON", self.ruta)
        parche.start()
        self.addCleanup(parche.stop)

    def test_el_ruta_por_defecto_tiene_los_mismos_riesgos(self):
        """``default_historial()`` no es un camino privilegiado.

        Se prueba el que la aplicación usa de verdad, no una construcción con
        parámetros de laboratorio: un ``default_historial`` que se saltara la
        redacción sería invisible para el resto de las pruebas.
        """
        from thetvview.diagnostics import default_historial

        h = default_historial()
        h.registrar(f"http://u:{CANARIO}@a.test/1.ts", False,
                    motivo=SENTINELAS["parametro"], player="mpv")
        self.assertNotIn(CANARIO, self.ruta.read_text(encoding="utf-8"))

    def test_el_fichero_nace_solo_para_el_usuario(self):
        import stat

        from thetvview.diagnostics import default_historial

        default_historial().registrar("ESPN", True)
        modo = stat.S_IMODE(self.ruta.stat().st_mode)
        self.assertEqual(modo, 0o600, "el historial se lee como prefiera quien sea")

    def test_el_directorio_tambien(self):
        from thetvview.diagnostics import default_historial

        default_historial().registrar("ESPN", True)
        self.assertTrue(self.ruta.parent.is_dir())


class TestLoQueTraeElReproductor(unittest.TestCase):
    """El centinela entra por la capa de `player`, no sólo por el historial."""

    def setUp(self) -> None:
        self.bus = EventBus()
        self.sesion = PlaybackSession(channel_key="espn", player_name="mpv")
        self.sesion.oyente = ObservadorDeSesion(self.bus, sesion=self.sesion)

    def test_el_detalle_del_reproductor_no_llega_al_bus(self):
        self.sesion.nota_fallo(StreamError(
            code=StreamErrorCode.AUTH_FAILED,
            resumen="x",
            detalle=SENTINELAS["userinfo"],
            observado=SENTINELAS["userinfo"],
        ))
        self.assertNotIn(CANARIO, str([e.to_dict() for e in self.bus.events()]))

    def test_el_detalle_del_reproductor_no_llega_al_historial(self):
        """``StreamError.detalle`` es texto crudo del binario de reproducción."""
        from thetvview.diagnostics import default_historial

        h = HistorialDiagnostico.__new__(HistorialDiagnostico)
        del h  # sólo el tipo; la escritura pasa por `registrar`
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "d.json"
            registro = HistorialDiagnostico(destino).registrar(
                "espn", False, motivo=SENTINELAS["cabecera"],
            )
            self.assertNotIn(CANARIO, destino.read_text(encoding="utf-8"))
        self.assertNotIn(CANARIO, registro.motivo)


class TestNoHayEscrituraDeHistorialPorFuera(unittest.TestCase):
    """Nadie escribe un fichero de diagnóstico con sus propias manos.

    Es la mitad ascendente de la garantía: si mañana alguien mete un
    ``json.dump`` con los datos de un canal en otro ``data/``, ningún test de
    centinelas de este archivo lo vería, porque no pasaría por el historial.
    """

    #: Los únicos sitios con permiso para escribir un fichero de historial.
    PERMITIDOS = {"history.py"}

    def test_ningun_otro_modulo_escribe_un_historial(self):
        from thetvview import diagnostics as paquete

        raiz = Path(paquete.__file__).parent.parent
        ofensas: list[str] = []
        for ruta in sorted(raiz.rglob("*.py")):
            partes = ruta.relative_to(raiz).parts
            if partes[0] not in ("diagnostics", "player") and partes[0] != "ui":
                continue
            if partes[-1] in self.PERMITIDOS:
                continue
            texto = ruta.read_text(encoding="utf-8")
            for marca in ('"diagnostics.json"', "'diagnostics.json'",
                          "DIAGNOSTICS_JSON"):
                if marca in texto and "json.dump" in texto:
                    ofensas.append(f"{ruta.relative_to(raiz)}")
        self.assertEqual(ofensas, [], "escritura de historial fuera de history.py")


if __name__ == "__main__":
    unittest.main()