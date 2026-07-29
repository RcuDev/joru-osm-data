#!/usr/bin/env python3
"""Decide QUE regiones construye cada job del workflow y las reparte por peso.

Por que una matriz y no un solo job: el mundo son ~83 GB de .osm.pbf que descargar y
procesar. Cabria en el limite de 6 horas por job, pero un fallo a las 5 horas lo tira
todo. Troceado, cada shard es un cuarto de hora y se relanza solo el que falle.

Por que se reparte por PESO y no por continente: Europa son 40 GB y Centroamerica 0,6.
Un shard por continente dejaria a Europa 20 veces mas larga que el resto, que es
exactamente el fallo que la matriz venia a evitar. Los shards se nombran igualmente
`<continente>-<n>` para que se puedan seleccionar por continente y para que el log se
lea.

Uso:
    python scripts/plan_build.py --regions regions.json --scope europe
    python scripts/plan_build.py --regions regions.json --repair --present assets.txt
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

# Objetivo de pbf por shard. A los 12-15 MB/s medidos de proceso, 4 GB son ~6 min de
# CPU mas la descarga: en torno al cuarto de hora por shard.
TARGET_BYTES_PER_SHARD = 4 << 30

VALID_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def plan(regions: list[dict], scope: str, repair: bool, present: set[str]) -> list[dict]:
    seleccion = scope.split()
    for token in seleccion:
        if not VALID_ID.match(token):
            raise SystemExit(f"::error::seleccion invalida: {token!r}")

    if seleccion:
        continentes = {r["continent"] for r in regions}
        ids = {r["id"] for r in regions}
        desconocidos = [t for t in seleccion if t not in continentes and t not in ids]
        if desconocidos:
            raise SystemExit(
                f"::error::no existe ninguna region ni continente llamado: {', '.join(desconocidos)}.\n"
                f"::error::continentes disponibles: {', '.join(sorted(continentes))}"
            )
        regions = [r for r in regions if r["continent"] in seleccion or r["id"] in seleccion]

    if repair:
        # Solo lo que falta en el Release vigente: un trimestre en el que un shard
        # fallo deja huecos, y reconstruir el mundo entero por tres regiones es absurdo.
        regions = [r for r in regions if r["id"] not in present]

    por_continente: dict[str, list[dict]] = collections.defaultdict(list)
    for region in sorted(regions, key=lambda r: -r.get("pbf_bytes", 0)):
        por_continente[region["continent"]].append(region)

    shards: list[dict] = []
    for continente in sorted(por_continente):
        actual, peso = [], 0
        for region in por_continente[continente]:
            # Una region que ella sola pasa del objetivo va en su propio shard: partirla
            # no se puede y acompanarla solo alarga el shard.
            if actual and peso + region.get("pbf_bytes", 0) > TARGET_BYTES_PER_SHARD:
                shards.append({"continent": continente, "regions": actual, "bytes": peso})
                actual, peso = [], 0
            actual.append(region["id"])
            peso += region.get("pbf_bytes", 0)
        if actual:
            shards.append({"continent": continente, "regions": actual, "bytes": peso})

    numeracion: collections.Counter = collections.Counter()
    salida = []
    for shard in shards:
        numeracion[shard["continent"]] += 1
        salida.append({
            "name": f"{shard['continent']}-{numeracion[shard['continent']]}",
            "regions": " ".join(shard["regions"]),
            "gb": round(shard["bytes"] / 2**30, 1),
        })
    return salida


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--regions", type=Path, default=Path("regions.json"))
    ap.add_argument("--scope", default="", help="continentes o ids de region, separados por espacios")
    ap.add_argument("--repair", action="store_true", help="solo las que falten en el Release vigente")
    ap.add_argument("--present", type=Path, help="fichero con los assets del Release vigente, uno por linea")
    args = ap.parse_args()

    regions = json.loads(args.regions.read_text(encoding="utf-8"))["regions"]
    present = set()
    if args.present and args.present.exists():
        present = {line.strip().removesuffix(".sqlite")
                   for line in args.present.read_text(encoding="utf-8").splitlines()
                   if line.strip().endswith(".sqlite")}

    shards = plan(regions, args.scope, args.repair, present)
    if not shards:
        print("nada que construir" + (" (el Release ya esta completo)" if args.repair else ""))
        if salida := os.environ.get("GITHUB_OUTPUT"):
            Path(salida).open("a", encoding="utf-8").write("matrix=[]\nempty=true\n")
        return 0

    total = sum(len(s["regions"].split()) for s in shards)
    print(f"{total} regiones en {len(shards)} shards:")
    for shard in shards:
        ids = shard["regions"].split()
        print(f"   {shard['name']:22s} {shard['gb']:5.1f} GB  {len(ids):>3} regiones  "
              f"{' '.join(ids[:4])}{' ...' if len(ids) > 4 else ''}")

    if salida := os.environ.get("GITHUB_OUTPUT"):
        with Path(salida).open("a", encoding="utf-8") as fh:
            fh.write(f"matrix={json.dumps(shards, ensure_ascii=False)}\n")
            fh.write("empty=false\n")
            fh.write(f"regions={total}\n")
            fh.write(f"shards={len(shards)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
