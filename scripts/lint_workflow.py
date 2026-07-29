#!/usr/bin/env python3
"""Valida el workflow antes de empujarlo. Ejecutar tras cualquier edicion:

    python scripts/lint_workflow.py

Comprueba dos cosas que GitHub rechaza o desaconseja y que no se ven a simple vista:

1. Expresiones ${{ }} VACIAS en cualquier punto del fichero. GitHub evalua las
   expresiones tambien dentro de los bloques `run:`, incluidos los comentarios de
   shell: un '${{ }}' escrito en un comentario tumba el fichero entero con
   "An expression was expected" y ademas hace que Actions muestre el path en vez
   del nombre del workflow (no puede leer el `name:`).

2. Cualquier expresion dentro de un `run:`. Aunque sea sintacticamente valida, el
   valor se interpola en el script antes de ejecutarlo; si viene de un input es
   inyeccion de comandos. La forma correcta es pasarla por `env:`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)


def lint(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    problems = []

    for match in EXPRESSION.finditer(raw):
        if not match.group(1).strip():
            line = raw[:match.start()].count("\n") + 1
            problems.append(f"{path.name}:{line}  expresion vacia {match.group(0)!r}")

    workflow = yaml.safe_load(raw)
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            script = step.get("run")
            if not script:
                continue
            for match in EXPRESSION.finditer(script):
                problems.append(
                    f"{path.name}  paso {step.get('name')!r}: {match.group(0)} "
                    f"dentro de un `run:` -> pasarlo por `env:`"
                )
    return problems


def main() -> int:
    root = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    files = sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml"))
    if not files:
        print("no hay workflows que revisar")
        return 0

    problems = [p for f in files for p in lint(f)]
    for problem in problems:
        print(f"  {problem}")
    print(f"{len(files)} workflow(s) revisado(s): "
          f"{'SIN PROBLEMAS' if not problems else f'{len(problems)} problema(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
