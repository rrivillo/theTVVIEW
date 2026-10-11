import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thetvview.models import Channel
from thetvview.recents import RecentsManager, relative_time


class TestRecentsManager(unittest.TestCase):
    def test_empty_load(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json")
            self.assertEqual(rm.load(), [])

    def test_push_and_load_order(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json", max_items=5)
            ch1 = Channel(name="A", url="http://a", group="G1")
            ch2 = Channel(name="B", url="http://b")
            rm.push(ch1, player="mpv")
            rm.push(ch2, player="vlc")
            items = rm.load()
            self.assertEqual([i.url for i in items], ["http://b", "http://a"])
            self.assertEqual(items[0].player, "vlc")

    def test_push_deduplicates_by_url(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json")
            ch = Channel(name="A", url="http://a")
            rm.push(ch, player="mpv")
            rm.push(ch, player="vlc")
            items = rm.load()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].player, "vlc")

    def test_max_items_limit(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json", max_items=3)
            for i in range(5):
                rm.push(Channel(name=str(i), url=f"http://{i}"), player="mpv")
            self.assertEqual(len(rm.load()), 3)
            self.assertEqual(rm.load()[0].url, "http://4")

    def test_clear(self):
        with tempfile.TemporaryDirectory() as td:
            rm = RecentsManager(Path(td) / "recents.json")
            rm.push(Channel(name="A", url="http://a"), player="mpv")
            rm.clear()
            self.assertEqual(rm.load(), [])

    def test_load_tolera_json_roto(self):
        # Un historial roto no impide abrir la app: el store conserva una
        # copia .corrupt-* y el historial vuelve a empezar vacío.
        with tempfile.TemporaryDirectory() as td:
            ruta = Path(td) / "recents.json"
            ruta.write_text("{no es json", encoding="utf-8")
            self.assertEqual(RecentsManager(ruta).load(), [])


class TestRelativeTime(unittest.TestCase):
    """`relative_time` es pura: se pasa un `now` fijo y no hay reloj que valga.

    Todos los casos con `NOW` constante, para que el test no dependa de la
    hora a la que corre (NFR-09).
    """

    NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)

    def _hace(self, **kwargs) -> str:
        return relative_time((self.NOW - timedelta(**kwargs)).isoformat(), self.NOW)

    def test_ahora_mismo(self):
        self.assertEqual(self._hace(seconds=0), "ahora mismo")
        self.assertEqual(self._hace(seconds=59), "ahora mismo")

    def test_minutos(self):
        self.assertEqual(self._hace(minutes=4), "hace 4 min")
        self.assertEqual(self._hace(minutes=1), "hace 1 min")
        self.assertEqual(self._hace(minutes=59), "hace 59 min")

    def test_horas(self):
        self.assertEqual(self._hace(hours=2), "hace 2 h")
        self.assertEqual(self._hace(hours=23), "hace 23 h")

    def test_ayer_y_dias(self):
        self.assertEqual(self._hace(days=1), "ayer")
        self.assertEqual(self._hace(days=3), "hace 3 días")
        self.assertEqual(self._hace(days=6), "hace 6 días")

    def test_semanas(self):
        self.assertEqual(self._hace(weeks=1), "hace 1 semana")
        self.assertEqual(self._hace(weeks=2), "hace 2 semanas")
        self.assertEqual(self._hace(days=7), "hace 1 semana")

    def test_futuro_no_dice_hace_negativo(self):
        futuro = (self.NOW + timedelta(minutes=3)).isoformat()
        self.assertEqual(relative_time(futuro, self.NOW), "sin fecha")

    def test_marca_invalida_o_ausente(self):
        for malo in ("", "   ", None, "basura", "2026-13-45T99:99:99", "ayer"):
            with self.subTest(marca=malo):
                self.assertEqual(relative_time(malo, self.NOW), "sin fecha")

    def test_sin_zona_se_toma_como_utc(self):
        # Un reloj sin zona no es un dato inválido: es UTC, que es como las
        # escribe esta misma app.
        naive = (self.NOW - timedelta(minutes=4)).replace(tzinfo=None).isoformat()
        self.assertEqual(relative_time(naive, self.NOW), "hace 4 min")

    def test_ahora_por_defecto_no_lanza(self):
        # Sin `now` usa el reloj, pero nunca revienta.
        marca = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.assertEqual(relative_time(marca), "ahora mismo")

    def test_now_invalido_degrada(self):
        """Un `now` que no se puede leer no lanza: dice que no hay fecha."""
        self.assertEqual(
            relative_time(self.NOW.isoformat(), object()), "sin fecha"
        )


if __name__ == "__main__":
    unittest.main()
