#!/usr/bin/env python3
"""Genera el catalog.json que la app pide ANTES de descargar un extracto.

Se publica junto a los .sqlite en el mismo Release, asi que anadir una region o
regenerar los datos NO exige publicar una version nueva de la app.

El bbox de cada region NO se declara a mano: sale del meta del propio extracto (los
POIs reales), asi que no puede quedar desfasado respecto a los datos.

Uso:
    python build_catalog.py --regions regions.json --dist dist --out dist/catalog.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CATALOG_SCHEMA = 1


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--regions", type=Path, default=Path("regions.json"))
    ap.add_argument("--dist", type=Path, default=Path("dist"))
    ap.add_argument("--out", type=Path, default=Path("dist/catalog.json"))
    args = ap.parse_args()

    declared = json.loads(args.regions.read_text(encoding="utf-8"))["regions"]

    entries, fingerprints, generators = [], set(), set()
    for region in declared:
        asset = args.dist / f"{region['id']}.sqlite"
        if not asset.exists():
            print(f"  aviso: {region['id']} declarada pero sin extracto, se omite")
            continue
        meta = read_meta(asset)
        fingerprints.add(meta["filter_fingerprint"])
        generators.add(meta["generator_version"])
        entries.append({
            "id": region["id"],
            "name": region["name"],
            "country": region["country"],
            "file": asset.name,
            "format": "sqlite",
            "bytes": asset.stat().st_size,
            "sha256": sha256_of(asset),
            "poi_count": int(meta["poi_count"]),
            "osm_timestamp": meta["osm_timestamp"],
            "bbox": json.loads(meta["bbox"]),  # [minLon, minLat, maxLon, maxLat]
        })

    if not entries:
        raise SystemExit("error: no se genero ningun extracto")
    # Un catalogo con extractos de filtros distintos serviria datos incoherentes entre
    # regiones del mismo viaje: se corta aqui, no en el movil.
    if len(fingerprints) > 1:
        raise SystemExit(f"error: extractos con filtros distintos: {sorted(fingerprints)}")

    catalog = {
        "schema": CATALOG_SCHEMA,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator_version": generators.pop(),
        "filter_fingerprint": fingerprints.pop(),
        "regions": sorted(entries, key=lambda e: e["id"]),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total = sum(e["bytes"] for e in entries)
    print(f"{args.out}: {len(entries)} regiones, {total / 1048576:.1f} MB en total")
    for e in entries:
        print(f"   {e['id']:12s} {e['poi_count']:>8,} POIs  {e['bytes'] / 1048576:>6.1f} MB  {e['osm_timestamp']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
