"""Prueba de que el proyecto **no** usa ningún servicio de CI en la nube.

Decisión del proyecto: nada de GitHub Actions, ni de ningún otro servicio de
integración continua. La razón práctica está en
`docs/informe_aceptacion_P0.md` (§6.1): sin servicios externos la suite se
ejecuta a mano, con `python -m unittest`, en el sistema donde se desarrolla.

Esta prueba convierte esa decisión en una restricción con dientes. Una
decisión escrita sólo en un documento se pierde en la primera sesión en la que
alguien tiene prisa; en cambio un workflow que reaparece hace fallar la suite
en el acto, antes de que llegue a subirse.

Lo que se comprueba:

- **no existe `.github/workflows/`**: el motivo por el que existe este fichero;
- **no hay configuración de CI en ninguna parte**: ni workflows con otra ruta,
  ni claves de otro servicio (Travis, CircleCI, AppVeyor, GitLab);
- **no hay badges de estado**: un badge es la otra mitad de la interacción con
  el servicio, porque apunta a él y falla cuando el servicio se va;
- **sí queda una forma local de verificar**: `python -m thetvview.security.check`
  sigue siendo ejecutable y sale con 0.

Lo que **no** se comprueba, a propósito: que no se mencione el nombre del
servicio en un documento. Escribir «aquí no usamos GitHub Actions» es la
documentación de la decisión, no una infracción; intentar distinguirlo leyendo
texto convertiría este fichero en una lista de palabras prohibidas en lugar de
una protección.

No toca `thetvview/`, no levanta terminal simulada y no usa la red.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Extensiones con las que un workflow se puede escribir. `.toml` no es válido
# hoy para un workflow, pero se comprueba igual: el objetivo es que el
# directorio no aparezca, no sólo el fichero conocido.
EXTENSIONES_CONFIG = frozenset({".yml", ".yaml", ".toml"})

# Ficheros de texto donde puede colarse un badge de estado enlazado al servicio.
EXTENSIONES_TEXTO = frozenset({".md", ".py", ".txt", ".rst", ".html"})

# Tablas de Node y entornos que viven dentro del repo pero no son código tuyo:
# ahí puede haber YAML con las palabras "workflow" o "actions" sin que signifique
# usar ningún servicio de CI.
EXCLUIDOS = (".env", ".git", ".venv", "venv", ".opencode", "Capturas", "SDD")

# Claves que sólo existen dentro de la configuración de un servicio de CI. Se
# buscan en ficheros de configuración, nunca en prosa: una frase puede decir
# "workflow" y no estar configurando nada.
MARCAS_CONFIG = (
    "runs-on:",
    "runs_on:",
    "jobs:",
    "workflow_dispatch",
    "workflow_call",
    "uses: actions/",
    "circleci.com",
    "appveyor.com",
    "travis-ci.org",
    "gitlab-ci.yml",
)

# Un badge es la otra mitad de la interacción: es un enlace al servicio que, además,
# se rompe en el momento en que el servicio deja de ejecutar nada.
MARCAS_BADGE = (
    "shields.io",
    "badge.svg",
    "/actions/workflows/",
    "circleci.com/badge",
    "travis-ci.org",
)


def _ruta_ignorada(ruta: Path) -> bool:
    """Cierto si la ruta está dentro de un directorio excluido a propósito.

    Este propio fichero es una excepción, y es la única: tiene que escribir
    literalmente las cadenas que busca para poder buscarlas, así que se
   detectaría a sí mismo. Ignorarse no es una puerta trasera: cualquier otro
    fichero con esas cadenas sigue haciendo fallar la prueba.
    """
    if ruta.resolve() == Path(__file__).resolve():
        return True
    for nombre in EXCLUIDOS:
        base = RAIZ / nombre
        if ruta == base or base in ruta.parents:
            return True
    return False


class TestSinIntegracionContinua(unittest.TestCase):
    """Nada de CI en la nube: ni configuración, ni rastro funcional."""

    def test_no_existe_directorio_de_workflows(self) -> None:
        """La razón de ser de esta prueba."""
        workflows = RAIZ / ".github" / "workflows"

        self.assertFalse(
            workflows.exists(),
            f"{workflows} volvió a aparecer. Este proyecto no usa GitHub Actions "
            f"ni ningún otro servicio de CI (ver AGENTS.md).",
        )

    def test_no_hay_configuracion_de_ci_en_el_repo(self) -> None:
        """Ni aunque el workflow se cambiaran de sitio o de servicio."""
        ofensas: list[str] = []

        for ruta in RAIZ.rglob("*"):
            if not ruta.is_file() or ruta.suffix.lower() not in EXTENSIONES_CONFIG:
                continue
            if _ruta_ignorada(ruta):
                continue
            texto = ruta.read_text(encoding="utf-8", errors="ignore").lower()
            for marca in MARCAS_CONFIG:
                if marca in texto:
                    ofensas.append(f"{ruta.relative_to(RAIZ)}: {marca!r}")

        self.assertEqual(
            [],
            sorted(ofensas),
            "Hay configuración de CI en el repo. Este proyecto no se integra con "
            "ningún servicio: la verificación es local. Bórralo.",
        )

    def test_no_hay_badges_de_estado(self) -> None:
        """Un badge mantiene vivo el vínculo con el servicio aunque no haya workflow."""
        ofensas: list[str] = []

        for ruta in RAIZ.rglob("*"):
            if not ruta.is_file() or ruta.suffix.lower() not in EXTENSIONES_TEXTO:
                continue
            if _ruta_ignorada(ruta):
                continue
            texto = ruta.read_text(encoding="utf-8", errors="ignore").lower()
            for marca in MARCAS_BADGE:
                if marca in texto:
                    ofensas.append(f"{ruta.relative_to(RAIZ)}: {marca!r}")

        self.assertEqual(
            [],
            sorted(ofensas),
            "Quedan badges de estado que apuntan a un servicio de CI. Sin servicio "
            "no tienen nada que mostrar y son un enlace roto: quítalos.",
        )

    def test_el_check_de_seguridad_sigue_ejecutable(self) -> None:
        """Quitar la CI no puede significar quedarse sin verificación ninguna."""
        resultado = subprocess.run(
            [sys.executable, "-m", "thetvview.security.check"],
            cwd=RAIZ,
            capture_output=True,
            text=True,
            timeout=120,
        )

        self.assertEqual(
            0,
            resultado.returncode,
            "`python -m thetvview.security.check` debe salir con 0 sin depender de "
            f"ningún servicio externo.\n{resultado.stdout}\n{resultado.stderr}",
        )


if __name__ == "__main__":
    unittest.main()