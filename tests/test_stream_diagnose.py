"""Tests del informe de diagnóstico (SDD-M §21, AC-011).

Dos cosas se comprueban aquí, y las dos importan:

1. el informe **contesta** las cuatro preguntas del AC-011 (tipo, backend,
   estado de conexión, causa probable);
2. el informe **no filtra secretos**, aunque la URL los lleve (AC-007).

Lo segundo no es un detalle: el informe va a un modal que el usuario puede
copiar a un canal de soporte.
"""

from __future__ import annotations

import unittest

from thetvview.models import Channel
from thetvview.security.redaction import contains_secret
from thetvview.streams.diagnose import (
    StepState,
    diagnose,
    diagnose_connection,
    render_report,
)

TODOS = {"mpv": "/usr/bin/mpv", "mplayer": "/usr/bin/mplayer", "vlc": "/usr/bin/vlc"}
NINGUNO = {"mpv": None, "mplayer": None, "vlc": None}


def _canal(url: str, **kw) -> Channel:
    return Channel(name="Canal 1", url=url, **kw)


class TestContenidoDelInforme(unittest.TestCase):
    def test_contesta_las_cuatro_preguntas_del_ac011(self) -> None:
        informe = diagnose(_canal("http://proveedor.test/live/x.m3u8"), available=TODOS)
        texto = render_report(informe)
        # tipo detectado
        self.assertIn("hls", texto)
        # backend seleccionado
        self.assertIn("mpv", texto)
        # estado (READY) — la parte de conexión la añade diagnose_connection
        self.assertIn("Resultado: READY", texto)

    def test_sin_reproductor_el_veredicto_es_otro(self) -> None:
        informe = diagnose(_canal("http://p.test/a.m3u8"), available=NINGUNO)
        self.assertEqual(informe.verdict, "SIN REPRODUCTOR")
        self.assertFalse(informe.ok)
        self.assertIn("instala", informe.aviso.lower())

    def test_los_candidatos_aparecen_con_motivo(self) -> None:
        # UDP es el caso donde la tabla **no** dice que sí los tres: VLC se
        # queda en su prefetch y mplayer sí abre. Un ❌ sin motivo sería peor
        # que no informar, así que el motivo es lo que se comprueba.
        informe = diagnose(_canal("udp://239.0.0.1:5000"), available=TODOS)
        vlc = next(c for c in informe.candidates if c.name == "vlc")
        self.assertFalse(vlc.ok)
        self.assertTrue(vlc.motivo)
        texto = render_report(informe)
        self.assertIn("vlc", texto)
        self.assertIn("[--]", texto)
        # Y los que sí pueden, con su marca de OK.
        self.assertIn("[OK] reproductor mpv", texto)

    def test_url_sin_esquema_no_revienta(self) -> None:
        # El informe de un canal roto tiene que poder *decir* que está roto.
        informe = diagnose(_canal("no-es-una-url"), available=TODOS)
        self.assertIsNotNone(informe.verdict)
        self.assertTrue(render_report(informe))

    def test_url_con_credenciales_embebidas_marca_la_politica(self) -> None:
        informe = diagnose(
            _canal("rtsp://admin:P4ssw0rd@192.168.1.9:554/stream1"), available=TODOS
        )
        politica = next(s for s in informe.steps if s.name == "política de URL")
        self.assertEqual(politica.state, StepState.FAIL)
        self.assertNotIn("P4ssw0rd", politica.detail)

    def test_referencia_opaca_no_se_adivina(self) -> None:
        # Con `ipcam://` no se puede saber el transporte sin resolver la
        # referencia, y resolver aquí escribiría la contraseña en el informe.
        informe = diagnose(_canal("ipcam://Camaras/abc123"), available=TODOS)
        self.assertEqual(informe.verdict, "REFERENCIA OPACA")
        self.assertEqual(informe.protocol, "opaco")

    def test_udp_multicast_avisa_de_la_red(self) -> None:
        # §14: multicast es un problema de red, y el informe lo dice.
        informe = diagnose(_canal("udp://239.255.0.1:5000"), available=TODOS)
        texto = render_report(informe)
        self.assertIn("multicast", texto.lower())

    def test_mime_declarado_aparece(self) -> None:
        canal = _canal(
            "http://p.test/live/canal", attrs={"content-type": "video/mp2t"}
        )
        informe = diagnose(canal, available=TODOS)
        self.assertEqual(informe.mime, "video/mp2t")
        self.assertEqual(informe.protocol, "mpegts")

    def test_todo_paso_tiene_nombre_y_estado(self) -> None:
        informe = diagnose(_canal("rtmp://x/live/a"), available=TODOS)
        for paso in informe.steps:
            with self.subTest(paso=paso.name):
                self.assertTrue(paso.name)
                self.assertIsInstance(paso.state, StepState)
                self.assertTrue(paso.line())


class TestSinSecretos(unittest.TestCase):
    """AC-007: ni el informe ni un paso pueden filtrar nada."""

    URLS_CON_SEGREDO = (
        "http://p.test/live/x.m3u8?token=SECRETO123&password=OTRO456",
        "rtsp://admin:P4ssw0rd@192.168.1.9:554/stream1",
        "https://usuario:clave@proveedor.test/a.m3u8",
        "http://p.test/live/u/clave/101.ts",
    )

    def test_el_informe_no_repete_la_url(self) -> None:
        for url in self.URLS_CON_SEGREDO:
            with self.subTest(url=url):
                texto = render_report(diagnose(_canal(url), available=TODOS))
                for secreto in ("SECRETO123", "OTRO456", "P4ssw0rd", "clave"):
                    self.assertNotIn(secreto, texto)

    def test_el_informe_no_tiene_pareja_secreto_valor(self) -> None:
        # Por si un día alguien añade la URL «redactada»: el predicado es
        # más estricto que una búsqueda de la palabra clave.
        for url in self.URLS_CON_SEGREDO:
            with self.subTest(url=url):
                texto = render_report(diagnose(_canal(url), available=TODOS))
                self.assertFalse(contains_secret(texto))

    def test_un_nombre_que_parece_una_url_se_redacta(self) -> None:
        # El nombre del canal es texto del usuario y se muestra tal cual —es
        # su etiqueta, no un secreto del sistema—, pero si trae algo con
        # forma de URL con credenciales, `redact_text` lo limpia igual que
        # cualquier otro texto del informe.
        canal = Channel(
            name="Camara rtsp://admin:P4ssw0rd@192.168.1.9/stream1",
            url="http://p.test/a.m3u8",
        )
        texto = render_report(diagnose(canal, available=TODOS))
        self.assertNotIn("P4ssw0rd", texto)
        self.assertIn("Camara", texto)

    def test_el_texto_de_un_error_tambien_pasa_por_redact_text(self) -> None:
        from thetvview.streams.diagnose import DiagnoseReport

        informe = DiagnoseReport(channel_name="C")
        informe.add("prueba", StepState.FAIL, "falló con ?password=OTRO456")
        self.assertNotIn("OTRO456", informe.steps[0].detail)


class TestConexion(unittest.TestCase):
    """`diagnose_connection` sí abre red, pero sólo por el cliente seguro."""

    def test_sin_host_no_inventa_pasos(self) -> None:
        pasos = diagnose_connection(_canal("no-es-una-url"))
        self.assertTrue(pasos)
        self.assertEqual(pasos[0].state, StepState.SKIP)

    def test_transporte_no_http_no_intenta_http(self) -> None:
        # Con `rtsp://` no tiene sentido un `HEAD`: lo negocia el reproductor
        # y el informe tiene que decirlo en vez de fingir una prueba. Se usa un
        # host numérico (127.0.0.1) para que el paso de DNS no separe al
        # apakah el nombre no resuelve.
        pasos = diagnose_connection(_canal("rtsp://127.0.0.1:554/stream"))
        nombres = [p.name for p in pasos]
        self.assertIn("DNS", nombres)
        http = next(p for p in pasos if p.name == "HTTP")
        self.assertEqual(http.state, StepState.SKIP)

    def test_dns_fallido_corta_el_resto(self) -> None:
        # Si el nombre no resuelve, los pasos siguientes darían un error que
        # no es la causa real. El informe para ahí y lo dice.
        pasos = diagnose_connection(
            _canal("http://no-existe-este-host.invalid/x.m3u8")
        )
        self.assertEqual(pasos[0].name, "DNS")
        self.assertEqual(pasos[0].state, StepState.FAIL)
        self.assertNotIn("HTTP", [p.name for p in pasos])

    def test_paso_de_dns_siempre_devuelve_algo(self) -> None:
        pasos = diagnose_connection(_canal("http://localhost.invalido/x.m3u8"))
        self.assertTrue(pasos)
        self.assertIn(pasos[0].name, {"DNS", "política de URL"})