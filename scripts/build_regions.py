#!/usr/bin/env python3
"""Genera el catalogo de regiones a partir del indice oficial de Geofabrik.

Produce un recubrimiento del mundo SIN SOLAPES, que es la parte delicada: Geofabrik
mezcla en el mismo indice regiones reales y "paquetes de conveniencia" que se solapan
con ellas. Sumar el indice tal cual da 114,9 GB, mas que el planeta entero (87 GB).

Reglas del corte (validadas el 2026-07-29):

1. La jerarquia se reconstruye por el PREFIJO DEL ID (`us/california` es hijo de `us`),
   no solo por el campo `parent`: en EE.UU. los estados cuelgan de `north-america`, no
   de `us`, asi que fiarse de `parent` dejaria a `us` como hoja y duplicaria el pais.
2. Un nodo colgado directamente de un CONTINENTE y SIN codigo ISO de pais es un paquete
   de conveniencia -> fuera. Caza los 27: dach (DE+AT+CH), alps, sea, britain-and-ireland,
   great-britain, us-midwest/-northeast/-pacific/-south/-west, los distritos federales
   rusos, canary-islands, azores, kosovo...
3. Si un nodo tiene subregiones se usan LAS SUBREGIONES: mas granular = descarga mas
   pequena en el movil (Japon entero son ~95 MB; Kansai, 17).
4. Antartida fuera: no es destino de viaje.

Resultado medido: 494 regiones, 72,4 GB de pbf, ~3,8 GB de SQLite, 8 MB de media y
57 MB la mayor (Quebec). 363 de las 494 bajan de 10 MB.

Uso:
    python scripts/build_regions.py --out regions.world.json
    python scripts/build_regions.py --out regions.world.json --sizes   # + tamano real (lento)
"""

from __future__ import annotations

import argparse
import collections
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1-nogeom.json"
USER_AGENT = "joru-osm-data/1.0 (+https://github.com/RcuDev/joru-osm-data)"

# Proporcion medida entre el .osm.pbf de origen y el .sqlite resultante, sobre tres
# regiones densas y turisticas (13,1/253, 17,0/332, 24,7/458). En zonas rurales el pbf
# esta dominado por carreteras y edificios, no por POIs, asi que el ratio real sera
# MENOR: sirve como techo, no como suelo.
SQLITE_RATIO = 0.052


def fetch_index() -> list[dict]:
    request = urllib.request.Request(GEOFABRIK_INDEX, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return [f["properties"] for f in json.load(response)["features"]]


def world_cover(props: list[dict]) -> list[dict]:
    by_id = {p["id"]: p for p in props}
    # Un nodo raiz CON codigo ISO de pais no es un continente: es un pais que Geofabrik
    # cuelga de la raiz. Solo pasa con Rusia, y tratarla como continente costaba caro:
    # sus 10 distritos federales -que SI son una particion real del pais, no paquetes de
    # conveniencia- caian en la regla del punto 2 y Rusia se quedaba con CERO cobertura.
    continents = {p["id"] for p in props if not p.get("parent") and not p.get("iso3166-1:alpha2")}

    bundles = {
        p["id"] for p in props
        if p.get("parent") in continents
        and not p.get("iso3166-1:alpha2")
        and "/" not in p["id"]
    }

    children: dict[str, set[str]] = collections.defaultdict(set)
    for p in props:
        if p["id"] in bundles or p["id"] in continents:
            continue
        if "/" in p["id"]:
            parent = p["id"].rsplit("/", 1)[0]
            if parent in by_id:
                children[parent].add(p["id"])
        elif p.get("parent") and p["parent"] not in continents:
            children[p["parent"]].add(p["id"])

    return [
        p for p in props
        if p["id"] not in bundles
        and p["id"] not in continents
        and not children[p["id"]]
        and not p["id"].startswith("antarctica")
        and (p.get("urls") or {}).get("pbf")
    ]


def pbf_size(url: str) -> int | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
        with urllib.request.urlopen(request, timeout=60) as response:
            return int(response.headers.get("Content-Length", 0)) or None
    except Exception:
        return None


def region_id(geofabrik_id: str) -> str:
    """Id apto para nombre de fichero: `us/california` -> `us-california`."""
    return geofabrik_id.replace("/", "-")


def continent_of(prop: dict, by_id: dict[str, dict]) -> str:
    """Raiz del arbol a la que pertenece la region. Gobierna la matriz del workflow, asi
    que Rusia sale como su propio shard (es raiz en Geofabrik y pesa lo suyo)."""
    node = prop
    seen: set[str] = set()
    while node.get("parent") and node["id"] not in seen:
        seen.add(node["id"])
        parent = by_id.get(node["parent"])
        if not parent:
            break
        node = parent
    return node["id"] if node is not prop else (prop.get("parent") or "other")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("regions.world.json"))
    parser.add_argument("--sizes", action="store_true", help="consulta el tamano real de cada pbf (HEAD)")
    args = parser.parse_args()

    props = fetch_index()
    by_id = {p["id"]: p for p in props}
    cover = world_cover(props)
    print(f"regiones del recubrimiento: {len(cover)}")

    sizes: dict[str, int | None] = {}
    if args.sizes:
        print("consultando tamanos (HEAD, sin descargar)...")
        with ThreadPoolExecutor(max_workers=12) as pool:
            urls = [(p["id"], p["urls"]["pbf"]) for p in cover]
            for (region, _), size in zip(urls, pool.map(lambda x: pbf_size(x[1]), urls)):
                sizes[region] = size
        total = sum(s for s in sizes.values() if s)
        print(f"pbf total {total / 2**30:,.1f} GB  ->  SQLite estimado {total * SQLITE_RATIO / 2**30:,.2f} GB")

    regions = []
    for p in sorted(cover, key=lambda x: x["id"]):
        entry = {
            "id": region_id(p["id"]),
            "name": p["name"],
            "country": (p.get("iso3166-1:alpha2") or [""])[0],
            "continent": continent_of(p, by_id),
            "source": p["urls"]["pbf"],
        }
        if args.sizes and sizes.get(p["id"]):
            entry["pbf_bytes"] = sizes[p["id"]]
        regions.append(entry)

    payload = {
        "$comment": (
            "GENERADO por scripts/build_regions.py desde el indice de Geofabrik. No editar a "
            "mano: se regenera. El bbox no se declara aqui, se deriva de los POIs reales al "
            "construir cada extracto (ver meta.bbox) y acaba en catalog.json."
        ),
        "schema": 1,
        "regions": regions,
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    per_continent = collections.Counter(r["continent"] for r in regions)
    print(f"\n{args.out}: {len(regions)} regiones")
    for continent, count in per_continent.most_common():
        print(f"   {continent:22s} {count:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
