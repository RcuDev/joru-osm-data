#!/usr/bin/env python3
"""Convierte un extracto regional de OSM (.osm.pbf) en el SQLite que consume Joru.

Paridad DELIBERADA con PoiSearchTool.CATEGORY_TAG_FILTERS de la app: una fila entra
aqui si y solo si el pipeline actual se la habria pedido a Overpass. El extracto guarda
los tags CRUDOS de OSM -el mapeo tag->SlotType vive en la app- para poder ajustarlo sin
regenerar extractos.

Uso:
    python build_extract.py region.osm.pbf salida.sqlite --region kansai
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

import osmium

# Version del generador. Subir cuando cambie el ESQUEMA de salida (no el filtro,
# que ya tiene su propia huella).
GENERATOR_VERSION = "1"

# ---------------------------------------------------------------------------
# Filtro de inclusion: criterio TURISTICO derivado de los datos de OSM, NO copia del
# CATEGORY_TAG_FILTERS heredado de la app. Manda el dato: la app (intereses, wizard,
# categorias) se adapta a lo que hay aqui, no al reves.
#
# El filtro heredado se quedaba corto de forma demostrable - dejaba fuera Fushimi
# Inari Taisha (tourism=yes), la Basilica de la Sagrada Familia y Casa Batllo
# (relaciones multipoligono), teatros, puentes y torres notables, cuevas, manantiales,
# cascadas, lagos, plazas, cementerios monumentales y estadios.
#
# Criterio: se incluye lo que un viajero puede querer VISITAR, COMER o donde ALOJARSE.
# Se excluye deliberadamente el equipamiento urbano que solo genera ruido aunque tenga
# wikidata: colegios (4.110 en Kansai), hospitales, comisarias, oficinas de correos,
# limites administrativos, carreteras, viviendas y naves industriales.
#
# Los valores CRUDOS de OSM se guardan tal cual; la clasificacion vive en la app.
# Si se toca esto cambia el filter_fingerprint, que viaja en el catalogo.
# ---------------------------------------------------------------------------
FILTERS: dict[str, frozenset[str]] = {
    "tourism": frozenset({
        "attraction", "viewpoint", "artwork", "museum", "gallery",
        "zoo", "aquarium", "theme_park", "picnic_site",
        # tourism=yes: no estandar pero es como esta mapeado Fushimi Inari Taisha,
        # la atraccion nº1 de Kioto. Son 31 objetos en todo Kansai: coste nulo.
        "yes",
        # Alojamiento, para poder sugerir/validar la "Base" del viaje.
        "hotel", "hostel", "guest_house", "apartment", "motel", "resort",
        "chalet", "camp_site", "alpine_hut",
    }),
    "historic": frozenset({
        "castle", "monument", "memorial", "ruins", "archaeological_site",
        "fort", "citadel", "manor", "palace", "church", "monastery",
        "tower", "city_gate", "aqueduct", "heritage", "building", "yes",
        "wayside_shrine", "wayside_cross", "tomb", "ship", "battlefield",
    }),
    "amenity": frozenset({
        "place_of_worship", "monastery", "marketplace", "public_bath",
        "cinema", "theatre", "arts_centre", "casino", "fountain", "townhall",
        "bar", "pub", "nightclub", "biergarten",
        "restaurant", "cafe", "fast_food", "ice_cream", "food_court",
    }),
    "leisure": frozenset({
        "garden", "park", "nature_reserve", "spa", "water_park",
        "beach_resort", "marina", "stadium",
    }),
    "natural": frozenset({
        "beach", "peak", "volcano", "waterfall", "cave_entrance", "spring",
        "hot_spring", "bay", "cliff", "glacier", "water", "saddle", "arch",
    }),
    "shop": frozenset({"mall", "department_store", "gift", "souvenir"}),
    "man_made": frozenset({
        "bridge", "lighthouse", "tower", "observatory", "windmill",
        "watermill", "pier", "obelisk",
    }),
    "waterway": frozenset({"waterfall"}),
    "place": frozenset({"square"}),
    "landuse": frozenset({"cemetery"}),
}

# Tags que la app lee de verdad. Todo lo demas se descarta: OSM trae cientos de tags
# por objeto y solo estos acaban en PoiCandidate.
#
# Las claves de FILTERS entran TODAS (son las que clasifican el POI). El resto se
# eligio midiendo su cobertura REAL sobre los POIs ya filtrados, no sobre todo OSM:
#   religion      12,8% Kansai / 7,3% Cataluna -> "Templo budista" vs "Santuario sintoista",
#                 la desambiguacion mas util que existe en Kioto
#   denomination   0,9% / 6,0%                 -> confesion concreta, acompana a religion
#   wikipedia      3,3% / 14,5%                -> 2a senal de notabilidad, independiente de wikidata
#   wheelchair     2,4% / 4,3%                 -> MobilityLevel.REDUCED, que hoy no tiene NINGUN dato.
#                 Cobertura demasiado baja para filtrar (vaciaria el plan), suficiente para
#                 no proponer nunca un `wheelchair=no` a quien pidio movilidad reducida.
# Descartados por cobertura insuficiente: heritage (0,3-0,5%, y con valores incoherentes
# 'yes'/'1'/'2'/'4' -> no sirve para una insignia UNESCO), stars (1,1%/0,1%), min_age (0,1%).
#
# Las claves de FILTERS se incluyen DERIVANDO de FILTERS, no repitiendolas a mano: al
# ampliar el filtro con man_made/waterway/place/landuse se me quedaron fuera de esta
# lista, asi que los puentes, torres, cascadas, plazas y cementerios entraban como filas
# pero SIN el tag que los clasifica -> la app los habria descartado por no tener
# categoria. Derivandolo, el desfase es imposible.
TAG_KEYS_EXACT = frozenset(FILTERS) | frozenset({
    "name", "int_name",
    "cuisine", "fee", "charge", "wikidata", "opening_hours",
    "religion", "denomination", "wikipedia", "wheelchair",
})
# name:en, name:es, name:ja-Latn, name:ja_rm... y diet:vegan, diet:halal...
TAG_KEY_PREFIXES = ("name:", "diet:")

EARTH_RADIUS_KM = 6371.0


def filter_fingerprint() -> str:
    """Huella estable del CONTRATO de extraccion, para que la app detecte un extracto
    desfasado. Cubre el filtro de inclusion Y la whitelist de tags: si solo cubriera el
    filtro, anadir un tag nuevo cambiaria lo que contienen las filas sin cambiar la
    huella, y la app no podria enterarse."""
    canonical = ";".join(
        f"{key}={','.join(sorted(values))}" for key, values in sorted(FILTERS.items())
    )
    canonical += "|tags=" + ",".join(sorted(TAG_KEYS_EXACT | set(TAG_KEY_PREFIXES)))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def matches(tags) -> bool:
    return any(tags.get(key) in values for key, values in FILTERS.items())


def prune(tags) -> dict[str, str]:
    return {
        k: v for k, v in tags
        if (k in TAG_KEYS_EXACT or k.startswith(TAG_KEY_PREFIXES)) and v
    }


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Misma formula que haversineKm en la app, en metros."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a)) * 1000.0


SCHEMA = """
CREATE TABLE poi (
  id     TEXT PRIMARY KEY,
  lon    REAL NOT NULL,
  lat    REAL NOT NULL,
  size_m REAL,
  tags   TEXT NOT NULL
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def build(pbf: Path, out: Path, region: str) -> dict:
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out)
    db.executescript(SCHEMA)

    # AREA se pide para recuperar las RELACIONES multipoligono. Overpass, tal como lo
    # consulta hoy la app, pide solo `node[...]` y `way[...]`, asi que los landmarks
    # mapeados como relacion son INVISIBLES en produccion: en Barcelona eso deja fuera
    # la Basilica de la Sagrada Familia, Casa Batllo, Casa Mila, la Catedral, la
    # Boqueria y Casa Amatller, todos con tags que si casan el filtro. Es un fallo
    # preexistente de la app, no una diferencia introducida aqui; el pipeline offline
    # lo corrige sin coste. Ver §3.4 del plan.
    entities = osmium.osm.NODE | osmium.osm.WAY | osmium.osm.RELATION | osmium.osm.AREA
    processor = (
        osmium.FileProcessor(str(pbf), entities)
        .with_areas()
        .with_filter(osmium.filter.KeyFilter(*FILTERS.keys()))
    )
    header_ts = processor.header.get("osmosis_replication_timestamp", "")

    rows, skipped, started = [], 0, time.time()
    for obj in processor:
        kind = obj.type_str()
        # Las areas derivadas de un way duplicarian el way, que ya se procesa aparte
        # con su geometria original; solo interesan las que vienen de una relacion.
        if kind == "r" or (kind == "a" and obj.from_way()):
            continue
        if not matches(obj.tags):
            continue

        if kind == "n":
            osm_type, osm_id = "node", obj.id
            lon, lat, size_m = obj.location.lon, obj.location.lat, None
        else:
            # Way o relacion: centro y tamano derivados de su bounding box,
            # EXACTAMENTE como hace hoy la app con el `out bb` de Overpass
            # (midpoint + diagonal esquina a esquina).
            if kind == "a":
                osm_type, osm_id = "relation", obj.orig_id()
                points = [n.location for ring in obj.outer_rings() for n in ring]
            else:
                osm_type, osm_id = "way", obj.id
                points = [n.location for n in obj.nodes]
            lons = [p.lon for p in points if p.valid()]
            lats = [p.lat for p in points if p.valid()]
            if not lons:
                skipped += 1
                continue
            lon, lat = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
            size_m = haversine_m(min(lats), min(lons), max(lats), max(lons))

        tags = prune(obj.tags)
        if not tags.get("name"):
            # La app descarta los POIs sin nombre (Bloque M4): no aportan nada al
            # viajero y ensucian la narrativa. Se descartan ya aqui para no cargar
            # el extracto con filas que nunca se usaran.
            skipped += 1
            continue

        rows.append((
            f"{osm_type}/{osm_id}",
            lon, lat, size_m,
            json.dumps(tags, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        ))
        if len(rows) >= 20_000:
            db.executemany("INSERT OR REPLACE INTO poi VALUES (?,?,?,?,?)", rows)
            rows.clear()

    if rows:
        db.executemany("INSERT OR REPLACE INTO poi VALUES (?,?,?,?,?)", rows)

    db.execute("CREATE INDEX idx_poi_lonlat ON poi(lon, lat)")

    count, min_lon, min_lat, max_lon, max_lat = db.execute(
        "SELECT count(*), min(lon), min(lat), max(lon), max(lat) FROM poi"
    ).fetchone()
    meta = {
        "region": region,
        "osm_timestamp": header_ts,
        "generator_version": GENERATOR_VERSION,
        "filter_fingerprint": filter_fingerprint(),
        "poi_count": str(count),
        "bbox": json.dumps([min_lon, min_lat, max_lon, max_lat]),
    }
    db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    db.commit()
    db.execute("VACUUM")
    db.close()

    return {
        **meta,
        "poi_count": count,
        "skipped": skipped,
        "bytes": out.stat().st_size,
        "seconds": round(time.time() - started, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pbf", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--region", required=True)
    args = ap.parse_args()

    if not args.pbf.exists():
        print(f"error: no existe {args.pbf}", file=sys.stderr)
        return 1

    stats = build(args.pbf, args.out, args.region)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(
        f"\n{args.region}: {stats['poi_count']:,} POIs  "
        f"{stats['bytes'] / 1048576:.1f} MB  "
        f"({stats['skipped']:,} descartados)  {stats['seconds']}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
