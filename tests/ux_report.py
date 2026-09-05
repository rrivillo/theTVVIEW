"""Informe visual de los tests UX — solo Python stdlib.

Muestra una tarjeta compacta en terminal con barras de progreso ANSI
y guarda un JSON en ``data/ux_last.json`` para inspeccion posterior.

Uso:
    python -m tests.ux_report [--verbose] [--json PATH] [--failfast]

Sin dependencias externas: argparse, json, sys, time, unittest.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path

CHECK = "\u2713"
CROSS = "\u2717"

RESET = "\033[0m"
BOLD = "\033[1m"
FG_GREEN = "\033[38;5;114m"
FG_RED = "\033[38;5;203m"
FG_YELLOW = "\033[38;5;221m"
FG_CYAN = "\033[38;5;215m"
FG_WHITE = "\033[38;5;252m"
FG_DIM = "\033[38;5;240m"

SUITE_ORDER = ["journey", "pantallas", "busqueda", "layout", "tema", "feedback"]
SUITE_LABELS = {
    "journey": "Journey navegacion",
    "pantallas": "Por pantalla",
    "busqueda": "Busqueda + vacios",
    "layout": "Layout + resize",
    "tema": "Tema + a11y",
    "feedback": "Feedback + golden",
}
CLASS_TO_SUITE = {
    "TestUxJourney": "journey",
    "TestUxPerScreen": "pantallas",
    "TestUxSearchEmpty": "busqueda",
    "TestUxLayout": "layout",
    "TestUxThemeA11y": "tema",
    "TestUxFeedbackGolden": "feedback",
}


def use_color():
    """False si NO_COLOR o salida no interactiva."""
    return not os.environ.get("NO_COLOR") and sys.stdout.isatty()


def paint(text, color, enabled=True):
    """Aplica color ANSI solo si esta permitido."""
    if not enabled:
        return text
    return "%s%s%s" % (color, text, RESET)


def bar(ratio, width=8, enabled=True):
    """Barra de progreso con bloques Unicode."""
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * width))
    cells = "\u2588" * filled + "\u25cb" * (width - filled)
    if not enabled:
        return cells
    if ratio >= 0.9:
        color = FG_GREEN
    elif ratio >= 0.7:
        color = FG_YELLOW
    else:
        color = FG_RED
    return paint(cells, color)


class UxResult(unittest.TextTestResult):
    """Agrupa resultados por suite y guarda tiempos y tracebacks."""

    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.suites = {}
        self.failures_detail = []
        self.started = time.monotonic()
        self._current_start = 0.0

    def suite_key(self, test):
        return CLASS_TO_SUITE.get(type(test).__name__, type(test).__name__)

    def _ensure(self, key):
        if key not in self.suites:
            self.suites[key] = {"total": 0, "passed": 0,
                                "failed": 0, "error": 0, "skipped": 0}

    def startTest(self, test):  # noqa: N802 (API de unittest)
        super().startTest(test)
        self._current_start = time.monotonic()
        key = self.suite_key(test)
        self._ensure(key)
        self.suites[key]["total"] += 1

    def addSuccess(self, test):  # noqa: N802 (API de unittest)
        super().addSuccess(test)
        self.suites[self.suite_key(test)]["passed"] += 1

    def _record_problem(self, test, err, kind):
        key = self.suite_key(test)
        self.suites[key][kind] += 1
        self.failures_detail.append({
            "id": str(test),
            "suite": key,
            "err": "".join(traceback.format_exception(*err)),
        })

    def addFailure(self, test, err):  # noqa: N802 (API de unittest)
        super().addFailure(test, err)
        self._record_problem(test, err, "failed")

    def addError(self, test, err):  # noqa: N802 (API de unittest)
        super().addError(test, err)
        self._record_problem(test, err, "error")

    def addSkip(self, test, reason):  # noqa: N802 (API de unittest)
        super().addSkip(test, reason)
        self.suites[self.suite_key(test)]["skipped"] += 1


class UxRunner(unittest.TextTestRunner):
    """Runner que imprime la tarjeta visual y guarda el JSON."""

    def __init__(self, stream=None, verbosity=0, failfast=False,
                 json_path="data/ux_last.json", verbose=False):
        super().__init__(stream=stream, verbosity=verbosity,
                         failfast=failfast, resultclass=UxResult)
        self.json_path = Path(json_path)
        self.verbose_detail = verbose

    def run(self, test):
        result = super().run(test)
        self.print_card(result)
        self.save_json(result)
        return result

    def print_card(self, result):
        color = use_color()
        width = 58
        total = result.testsRun
        passed = sum(s["passed"] for s in result.suites.values())
        failed = sum(s["failed"] + s["error"] for s in result.suites.values())
        duration = time.monotonic() - result.started
        ratio = passed / total if total else 0.0

        out = self.stream or sys.stderr
        header = "theTVVIEW \u00b7 UX Experience \u00b7 %d tests" % total
        out.write("\n" + paint("\u256d\u2500 %s" % header, FG_CYAN, color) + "\n")
        for key in SUITE_ORDER:
            if key not in result.suites:
                continue
            suite = result.suites[key]
            ok = suite["failed"] == 0 and suite["error"] == 0
            icon = paint(CHECK, FG_GREEN, color) if ok else paint(CROSS, FG_RED, color)
            label = SUITE_LABELS.get(key, key)
            cells = bar(suite["passed"] / suite["total"] if suite["total"] else 0,
                        enabled=color)
            extra = ""
            bad = suite["failed"] + suite["error"]
            if bad:
                extra = " %d fallo%s" % (bad, "s" if bad > 1 else "")
            out.write("\u2502 %s %-22s %s %d/%d%s\n"
                      % (icon, label, cells, suite["passed"], suite["total"], extra))
        status = paint("OK", FG_GREEN, color) if failed == 0 else paint("FAIL", FG_RED, color)
        out.write(paint("\u2570\u2500 Total: %d/%d %s %d%% \u00b7 %.2fs"
                        % (passed, total, status, round(ratio * 100), duration),
                        FG_CYAN, color) + "\n")

        if result.failures_detail:
            out.write(paint("\nFallos:", FG_RED, color) + "\n")
            shown = result.failures_detail if self.verbose_detail else result.failures_detail[:5]
            for item in shown:
                out.write(paint("  %s" % item["id"], FG_DIM, color) + "\n")
                if self.verbose_detail:
                    out.write(item["err"] + "\n")
                else:
                    lines = item["err"].strip().splitlines()
                    picked = next((l.strip() for l in lines
                                   if "Error" in l or "Assertion" in l), "")
                    detail = picked or (lines[-1].strip() if lines else "")
                    out.write(paint("    %s" % detail, FG_YELLOW, color) + "\n")
            rest = len(result.failures_detail) - len(shown)
            if rest > 0:
                out.write(paint("  ... +%d mas (usa --verbose)" % rest, FG_DIM, color) + "\n")
        out.write("Ancho minimo: 10x40 · --verbose muestra tracebacks\n")
        out.flush()

    def save_json(self, result):
        duration = time.monotonic() - result.started
        total = result.testsRun
        passed = sum(s["passed"] for s in result.suites.values())
        failed = sum(s["failed"] + s["error"] for s in result.suites.values())
        data = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(duration, 3),
            "total": total,
            "passed": passed,
            "failed": failed,
            "suites": [
                {"id": key,
                 "total": result.suites[key]["total"],
                 "passed": result.suites[key]["passed"],
                 "failed": result.suites[key]["failed"],
                 "error": result.suites[key]["error"]}
                for key in SUITE_ORDER if key in result.suites
            ],
            "failures": result.failures_detail,
        }
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.json_path.with_suffix(self.json_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.json_path)


def build_suite():
    """Suite completa de tests/test_ux_experience.py."""
    return unittest.TestLoader().loadTestsFromName("tests.test_ux_experience")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Runner visual de tests UX (stdlib).")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Muestra tracebacks completos")
    parser.add_argument("--json", default="data/ux_last.json",
                        help="Ruta del JSON de salida")
    parser.add_argument("--failfast", action="store_true",
                        help="Detener en el primer fallo")
    args = parser.parse_args(argv)
    runner = UxRunner(verbosity=0, failfast=args.failfast,
                      json_path=args.json, verbose=args.verbose)
    result = runner.run(build_suite())
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
