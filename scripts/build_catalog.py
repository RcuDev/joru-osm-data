#!/usr/bin/env python3
"""Genera el catalog.json que la app pide ANTES de descargar un extracto.

Se publica junto a los .sqlite en el mismo Release, asi que anadir una region o
regenerar los datos NO exige publicar una version nueva de la app.

La geometria de cada region NO se declara a mano: sale del meta del propio extracto
(los POIs reales), asi que no puede quedar desfasada respecto a los datos.

POR QUE RECTANGULOS Y NO UN BBOX
--------------------------------
Un solo bbox miente, y mucho. El de Portugal se estira hasta las Azores y acaba siendo
MAYOR que el de Espana, asi que "la region de bbox mas pequeno que contiene el destino"
mandaba Lisboa al extracto espanol. Medido sobre las 235.063 localidades del mundo de
mas de 500 habitantes, el bbox unico manda al 22% de los destinos a una region que no
los contiene: Argel a `spain`, Toronto a `us/new-york`, Santiago de Chile a `argentina`,
Kabul a la zona norte de la India. Ademas hay 10 regiones que cruzan el antimeridiano
(Alaska, Fiyi, Nueva Zelanda, Chukotka) cuyo bbox va de -180 a 180: contiene CUALQUIER
punto del planeta.

Con la lista de rectangulos derivada de los POIs reales ese error es cero, y ademas
habilita el caso frontera: la app puede saber que parte de su area de busqueda cae
fuera de la region elegida y anadir solo la vecina que hace falta.

Modos de entrada (se pueden combinar):
    --dist dist          lee los .sqlite que haya ahi (ejecucion local)
    --meta meta          lee los <id>.json que emite cada shard del workflow
    --base catalog.json  parte de un catalogo previo (reparacion o build parcial)

Uso:
    python build_catalog.py --regions regions.json --dist dist --out dist/catalog.json
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CATALOG_SCHEMA = 2

# Tope de rectangulos por region. Generoso a proposito: la medicion sobre el mundo
# entero da una media de 20 y el peor caso ronda los 100. Existe solo para que una
# region con datos dispersos y patologicos no infle el catalogo sin limite.
MAX_RECTS = 512

# A partir de que proporcion de celdas ya cubiertas por regiones MAS PEQUENAS se
# considera que una region es solo una copia gruesa de sus hijas.
#
# La app baja la region del destino MAS las vecinas cuyo territorio llegue a su area de
# busqueda, para poder proponer excursiones. Eso obliga a echar del catalogo a las
# regiones que no son vecinas de nadie sino el mismo territorio otra vez: `spain` es
# 66,6 MB que sus 19 comunidades ya cubren, asi que sin esto un destino en Madrid se
# bajaria `madrid` (4,2 MB) Y `spain` entero.
#
# El umbral no es inventado: medido sobre el Release europeo real, `spain` esta al 100%
# y `england` al 98,6%, la siguiente candidata (`gelderland`) al 96%, y las que no deben
# caer -Andorra, Monaco, Ceuta, Isla de Man, Luxemburgo- estan todas al 0%. Ademas
# ninguna de las 99.743 localidades del mundo elige `spain` ni `england`, y quitarlas no
# deja sin cobertura a ninguna.
REDUNDANT_CELL_RATIO = 0.97


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_meta(sqlite_path: Path) -> dict[str, str]:
    db = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        return dict(db.execute("SELECT k, v FROM meta"))
    finally:
        db.close()


def merge_rects(cells: list[list[int]], cell_deg: float, max_rects: int = MAX_RECTS) -> list[list[float]]:
    """Fusiona celdas contiguas en rectangulos, en grados.

    Dos pasadas: primero tramos horizontales de celdas seguidas, luego tramos iguales
    en filas consecutivas. No es la descomposicion optima -eso es NP-dificil- pero
    sobre datos reales deja ~20 rectangulos por region, que es de sobra.
    """
    if not cells:
        return []
    rows: dict[int, list[int]] = collections.defaultdict(list)
    for cx, cy in cells:
        rows[cy].append(cx)

    runs = []
    for y, xs in rows.items():
        xs.sort()
        start = prev = xs[0]
        for x in xs[1:]:
            if x == prev + 1:
                prev = x
            else:
                runs.append([y, start, prev, y])
                start = prev = x
        runs.append([y, start, prev, y])

    runs.sort(key=lambda r: (r[1], r[2], r[0]))
    merged: list[list[int]] = []
    for y, x1, x2, _ in runs:
        if merged and merged[-1][1] == x1 and merged[-1][2] == x2 and merged[-1][3] == y - 1:
            merged[-1][3] = y
        else:
            merged.append([y, x1, x2, y])

    # Valvula de seguridad: si salieran demasiados, se van fundiendo los mas pequenos
    # en su caja comun. Pierde precision, nunca cobertura.
    while len(merged) > max_rects:
        merged.sort(key=lambda r: (r[2] - r[1] + 1) * (r[3] - r[0] + 1))
        a, b = merged[0], merged[1]
        merged = merged[2:] + [[min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]]

    return sorted(
        [round(x1 * cell_deg - 180, 4), round(y1 * cell_deg - 90, 4),
         round((x2 + 1) * cell_deg - 180, 4), round((y2 + 1) * cell_deg - 90, 4)]
        for y1, x1, x2, y2 in merged
    )


def cells_of(entry: dict, cell_deg: float = 0.25) -> set[tuple[int, int]]:
    """Celdas que ocupan los rectangulos de una region."""
    return {
        (i, j)
        for x1, y1, x2, y2 in entry["rects"]
        for i in range(round(x1 / cell_deg), round(x2 / cell_deg))
        for j in range(round(y1 / cell_deg), round(y2 / cell_deg))
    }


def redundant(entries: list[dict]) -> list[str]:
    """Regiones que son una copia gruesa de otras mas pequenas. Ver REDUNDANT_CELL_RATIO."""
    celdas = {e["id"]: cells_of(e) for e in entries}
    reclamantes: dict[tuple[int, int], list[str]] = collections.defaultdict(list)
    for region, cs in celdas.items():
        for cell in cs:
            reclamantes[cell].append(region)

    fuera = []
    for entry in entries:
        propias = celdas[entry["id"]]
        if not propias:
            continue
        cubiertas = sum(
            1 for cell in propias
            if any(len(celdas[otra]) < len(propias) for otra in reclamantes[cell] if otra != entry["id"])
        )
        if cubiertas / len(propias) >= REDUNDANT_CELL_RATIO:
            fuera.append(entry["id"])
    return fuera


def entry_from_sqlite(region: dict, asset: Path) -> dict:
    meta = read_meta(asset)
    cell_deg = float(meta.get("cell_deg", 0.25))
    cells = json.loads(meta.get("cells", "[]"))
    return {
        "id": region["id"],
        "name": region["name"],
        "country": region.get("country", ""),
        "file": asset.name,
        "format": "sqlite",
        "bytes": asset.stat().st_size,
        "sha256": sha256_of(asset),
        "poi_count": int(meta["poi_count"]),
        "osm_timestamp": meta["osm_timestamp"],
        "generator_version": meta["generator_version"],
        "filter_fingerprint": meta["filter_fingerprint"],
        "bbox": json.loads(meta["bbox"]),           # [minLon, minLat, maxLon, maxLat]
        "rects": merge_rects(cells, cell_deg),      # donde hay POIs de verdad
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--regions", type=Path, default=Path("regions.json"))
    ap.add_argument("--dist", type=Path, default=Path("dist"))
    ap.add_argument("--meta", type=Path, help="directorio con los <id>.json de cada shard")
    ap.add_argument("--base", type=Path, help="catalogo previo del que heredar regiones no reconstruidas")
    ap.add_argument("--emit-meta", type=Path,
                    help="en vez del catalogo, escribe ahi un <id>.json por extracto de --dist. "
                         "Lo usa cada shard del workflow: sube su .sqlite al Release y deja solo "
                         "estos metadatos, para que el job final arme el catalogo sin mover GB.")
    ap.add_argument("--out", type=Path, default=Path("dist/catalog.json"))
    args = ap.parse_args()

    declared = json.loads(args.regions.read_text(encoding="utf-8"))["regions"]

    if args.emit_meta:
        args.emit_meta.mkdir(parents=True, exist_ok=True)
        escritos = 0
        for region in declared:
            asset = args.dist / f"{region['id']}.sqlite"
            if not asset.exists():
                continue
            entry = entry_from_sqlite(region, asset)
            (args.emit_meta / f"{region['id']}.json").write_text(
                json.dumps(entry, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            escritos += 1
            print(f"   {region['id']:24s} {entry['poi_count']:>9,} POIs  "
                  f"{entry['bytes'] / 1048576:>7.1f} MB  {len(entry['rects']):>4} rect")
        if not escritos:
            raise SystemExit("error: no habia ningun extracto en --dist")
        print(f"{escritos} metadatos escritos en {args.emit_meta}")
        return 0

    heredadas = {}
    if args.base and args.base.exists():
        base = json.loads(args.base.read_text(encoding="utf-8"))
        heredadas = {e["id"]: e for e in base["regions"]}
        print(f"catalogo base (esquema {base.get('schema')}): {len(heredadas)} regiones")

    # Lo construido AHORA y lo heredado se validan con criterios distintos a proposito:
    # un fallo en lo que acaba de salir del pipeline es un bug y corta la publicacion;
    # una region heredada que ya no encaja simplemente se cae del catalogo y se
    # reconstruye con el modo `repair`. Mezclar ambos criterios significaria no publicar
    # NADA por culpa de datos viejos, con los extractos nuevos ya subidos al Release.
    nuevas, viejas, faltan = [], [], []
    for region in declared:
        asset = args.dist / f"{region['id']}.sqlite"
        meta_file = (args.meta / f"{region['id']}.json") if args.meta else None
        if asset.exists():
            nuevas.append(entry_from_sqlite(region, asset))
        elif meta_file and meta_file.exists():
            nuevas.append(json.loads(meta_file.read_text(encoding="utf-8")))
        elif region["id"] in heredadas:
            viejas.append(heredadas[region["id"]])
        else:
            faltan.append(region["id"])

    if not nuevas and not viejas:
        raise SystemExit("error: no se genero ningun extracto")

    # Un catalogo con extractos de filtros distintos serviria datos incoherentes entre
    # regiones del mismo viaje: se corta aqui, no en el movil.
    fingerprints = {e["filter_fingerprint"] for e in nuevas}
    if len(fingerprints) > 1:
        conteo = collections.Counter(e["filter_fingerprint"] for e in nuevas)
        raise SystemExit(f"error: extractos con filtros distintos: {conteo.most_common()}")

    sin_geometria = [e["id"] for e in nuevas if not e.get("rects")]
    if sin_geometria:
        raise SystemExit(
            f"error: {len(sin_geometria)} regiones construidas sin rectangulos: "
            f"{', '.join(sin_geometria[:10])}"
        )

    # Se descarta lo heredado que la app no podria usar junto a lo nuevo: sin geometria
    # (generador anterior a la version 2) o con otro filtro. Quedan como si no existieran
    # -el wizard avisa de que la zona no esta cubierta- hasta que se reconstruyan.
    esperado = fingerprints.pop() if fingerprints else None
    descartadas = [e["id"] for e in viejas
                   if not e.get("rects") or (esperado and e["filter_fingerprint"] != esperado)]
    entries = nuevas + [e for e in viejas if e["id"] not in descartadas]
    if descartadas:
        print(f"  aviso: {len(descartadas)} regiones heredadas se descartan por estar "
              f"desfasadas (relanza con `repair`): {', '.join(descartadas[:10])}"
              f"{'...' if len(descartadas) > 10 else ''}")
    if faltan:
        print(f"  aviso: {len(faltan)} regiones declaradas sin extracto: {', '.join(faltan[:10])}"
              f"{'...' if len(faltan) > 10 else ''}")

    copias = redundant(entries)
    if copias:
        peso = sum(e["bytes"] for e in entries if e["id"] in copias) / 1048576
        print(f"  fuera del catalogo: {len(copias)} regiones que sus hijas ya cubren "
              f"({peso:.0f} MB): {', '.join(sorted(copias))}")
        entries = [e for e in entries if e["id"] not in copias]

    catalog = {
        "schema": CATALOG_SCHEMA,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator_version": sorted({e["generator_version"] for e in entries})[-1],
        # `esperado` es None solo si este build no construyo nada y el catalogo es
        # integramente heredado; entonces la huella es la que ya tenia.
        "filter_fingerprint": esperado or entries[0]["filter_fingerprint"],
        "regions": sorted(entries, key=lambda e: e["id"]),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) + "\n",
                        encoding="utf-8")

    total = sum(e["bytes"] for e in entries)
    rects = sum(len(e["rects"]) for e in entries)
    print(f"{args.out}: {len(entries)} regiones, {total / 1048576:,.1f} MB de extractos, "
          f"{rects:,} rectangulos, catalogo de {args.out.stat().st_size / 1024:.0f} KB")
    for e in sorted(entries, key=lambda e: -e["bytes"])[:15]:
        print(f"   {e['id']:24s} {e['poi_count']:>9,} POIs  {e['bytes'] / 1048576:>7.1f} MB  "
              f"{len(e['rects']):>4} rect  {e['osm_timestamp']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
