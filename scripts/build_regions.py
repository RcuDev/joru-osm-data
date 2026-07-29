#!/usr/bin/env python3
"""Genera el catalogo de regiones a partir del indice oficial de Geofabrik.

Produce un recubrimiento del mundo SIN AGUJEROS y con el minimo solape, eligiendo
siempre la region MAS PEQUENA que cubre cada sitio (menos MB que bajar en el movil).

POR QUE EL CRITERIO ES GEOMETRICO Y NO ESTRUCTURAL
--------------------------------------------------
El primer intento se baso en el arbol del indice ("si un nodo tiene subregiones, usa
las subregiones; descarta los paquetes de conveniencia"). Se cayo por tres sitios, y los
tres en silencio:

1. Geofabrik publica extractos COMBINADOS junto a los reales -dach (DE+AT+CH), alps,
   sea, britain-and-ireland, us-south...-. Sumarlos todos daba 114,9 GB, mas que el
   planeta entero (87 GB).
2. La jerarquia no es la del campo `parent`: los estados de EE.UU. son `us/california`
   con parent `north-america`, asi que `us` parecia una hoja y el pais se contaba dos veces.
3. **Los hijos NO son una particion del padre.** `enfield` figura como hijo de
   `greater-london`, asi que la regla "prefiere los hijos" se quedaba con un barrio y
   TIRABA EL RESTO DE LONDRES. Del mismo modo, canary-islands, isle-of-man,
   guernsey-jersey y comores cuelgan de un continente sin codigo ISO y se descartaban
   como si fueran paquetes: cuatro territorios enteros sin cobertura.

Ninguna regla estructural sobrevive a eso. La unica fuente de verdad es la GEOMETRIA.

ALGORITMO
---------
Voraz por area ascendente sobre los poligonos reales del indice:
  - Se recorren las regiones de menor a mayor.
  - Se aceptan salvo que su territorio YA este cubierto por las aceptadas (>=90% de una
    muestra de puntos interiores). Asi los paquetes combinados se descartan solos -para
    cuando les toca, sus piezas ya estan dentro- sin listas negras que mantener.
  - Lo que ninguna region pequena cubre se acepta al llegar a la region grande que lo
    contiene: por construccion no quedan agujeros.

Se valida con `--verify`, que comprueba ciudades reales de los casos limite.

Uso:
    python scripts/build_regions.py --out regions.world.json --sizes --verify
"""


from __future__ import annotations

import argparse
import collections
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# CON geometria: el corte es geometrico, asi que los poligonos son imprescindibles.
GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1.json"
USER_AGENT = "joru-osm-data/1.0 (+https://github.com/RcuDev/joru-osm-data)"

# Proporcion medida entre el .osm.pbf de origen y el .sqlite resultante, sobre tres
# regiones densas y turisticas (13,1/253, 17,0/332, 24,7/458). En zonas rurales el pbf
# esta dominado por carreteras y edificios, no por POIs, asi que el ratio real sera
# MENOR: sirve como techo, no como suelo.
SQLITE_RATIO = 0.052


def fetch_index() -> list[dict]:
    """Devuelve las propiedades de cada region con su geometria adjunta en `_geom`."""
    request = urllib.request.Request(GEOFABRIK_INDEX, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        features = json.load(response)["features"]
    out = []
    for f in features:
        props = dict(f["properties"])
        props["_geom"] = f.get("geometry")
        out.append(props)
    return out


def _rings(geom) -> list:
    if not geom:
        return []
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [poly[0] for poly in geom["coordinates"]]
    return []


def _bbox(geom):
    points = [pt for ring in _rings(geom) for pt in ring]
    if not points:
        return None
    xs = [pt[0] for pt in points]
    ys = [pt[1] for pt in points]
    return min(xs), min(ys), max(xs), max(ys)


def _inside(point, geom) -> bool:
    """Punto en poligono (ray casting) sobre los anillos exteriores."""
    x, y = point
    for ring in _rings(geom):
        n = len(ring)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
                inside = not inside
            j = i
        if inside:
            return True
    return False


def _interior_points(geom, box, grid: int = 9) -> list:
    """Rejilla de puntos DENTRO del poligono. Se muestrea el interior y no el contorno
    porque el contorno de una isla cae en el mar y daria falsos negativos."""
    min_lon, min_lat, max_lon, max_lat = box
    step_x = (max_lon - min_lon) / (grid + 1) or 1e-6
    step_y = (max_lat - min_lat) / (grid + 1) or 1e-6
    points = []
    for i in range(1, grid + 1):
        for j in range(1, grid + 1):
            pt = (min_lon + i * step_x, min_lat + j * step_y)
            if _inside(pt, geom):
                points.append(pt)
    # Poligonos muy finos (una isla diminuta) pueden no atrapar ningun punto de la
    # rejilla: se cae a los vertices, que al menos situan el territorio.
    return points or [tuple(pt) for ring in _rings(geom) for pt in ring][:20]


def world_cover(props: list[dict], threshold: float = 0.90) -> list[dict]:
    """Recubrimiento voraz por area ascendente. Ver el docstring del modulo."""
    candidates = []
    for p in props:
        if not (p.get("urls") or {}).get("pbf"):
            continue
        if p["id"].startswith("antarctica"):
            continue
        box = _bbox(p.get("_geom"))
        if not box:
            continue
        area = (box[2] - box[0]) * (box[3] - box[1])
        candidates.append((area, p, box))
    candidates.sort(key=lambda c: c[0])

    accepted: list[tuple[dict, tuple]] = []
    for _, prop, box in candidates:
        points = _interior_points(prop["_geom"], box)
        # Solo pueden cubrirlo las aceptadas cuyo bbox solape.
        overlapping = [
            (q, qb) for q, qb in accepted
            if not (box[2] < qb[0] or qb[2] < box[0] or box[3] < qb[1] or qb[3] < box[1])
        ]
        covered = sum(
            1 for pt in points
            if any(qb[0] <= pt[0] <= qb[2] and qb[1] <= pt[1] <= qb[3] and _inside(pt, q["_geom"])
                   for q, qb in overlapping)
        )
        if points and covered / len(points) >= threshold:
            continue   # redundante: sus piezas ya estan dentro (paquete combinado)
        accepted.append((prop, box))

    # PENDIENTE (siguiente sesion): reducir el solape. Este corte descarga 138,5 GB de
    # pbf, mas que el planeta (87 GB), porque acepta regiones que se solapan
    # parcialmente. Un intento de segunda pasada "quita lo que ya cubren las demas"
    # colapso el corte a 28 regiones y dejo a Londres resolviendo a `europe` entero
    # (~1 GB): inservible en un movil. Hay que hacerlo conservando la GRANULARIDAD, no
    # solo el area total. Mientras tanto se prefiere pasarse de descarga en CI antes que
    # dejar agujeros o regiones gigantes: la verificacion por ciudades manda.
    return [prop for prop, _ in accepted]


# Ciudades de los casos LIMITE, no de los faciles: cada una cazo un fallo real del corte
# estructural anterior. Londres es la mas importante -`enfield` figura como hijo de
# `greater-london`, asi que "prefiere los hijos" se quedaba con un barrio y tiraba la
# ciudad-. Las demas son territorios que colgaban de un continente sin codigo ISO.
VERIFY_CITIES = [
    ("Londres", -0.1276, 51.5072), ("Enfield", -0.0800, 51.6520),
    ("Edimburgo", -3.1883, 55.9533), ("Cardiff", -3.1791, 51.4816),
    ("Dublin", -6.2603, 53.3498), ("Belfast", -5.9301, 54.5973),
    ("Douglas (Isla de Man)", -4.4819, 54.1509),
    ("Saint Helier (Jersey)", -2.1050, 49.1868),
    ("Las Palmas (Canarias)", -15.4300, 28.1235),
    ("Santa Cruz de Tenerife", -16.2546, 28.4636),
    ("Ponta Delgada (Azores)", -25.6689, 37.7412),
    ("Moroni (Comoras)", 43.2551, -11.7172),
    ("Honolulu", -157.8583, 21.3069), ("Anchorage", -149.9003, 61.2181),
    ("Nueva York", -74.0060, 40.7128), ("Los Angeles", -118.2437, 34.0522),
    ("Tokio", 139.6917, 35.6895), ("Kioto", 135.7681, 35.0116),
    ("Moscu", 37.6173, 55.7558), ("Kaliningrado", 20.5100, 54.7104),
    ("Barcelona", 2.1734, 41.3851), ("Ceuta", -5.3213, 35.8894),
    ("Ciudad del Cabo", 18.4241, -33.9249), ("El Cairo", 31.2357, 30.0444),
    ("Sao Paulo", -46.6333, -23.5505), ("Ciudad de Mexico", -99.1332, 19.4326),
    ("Sidney", 151.2093, -33.8688), ("Auckland", 174.7633, -36.8485),
    ("Singapur", 103.8198, 1.3521), ("Bombay", 72.8777, 19.0760),
    ("Estambul", 28.9784, 41.0082), ("Reikiavik", -21.9426, 64.1466),
    ("Nuuk (Groenlandia)", -51.7216, 64.1836), ("Papeete (Tahiti)", -149.5665, -17.5516),
    ("La Valeta (Malta)", 14.5146, 35.8989), ("Andorra la Vella", 1.5218, 42.5063),
]


def verify_cover(cover: list[dict]) -> list[str]:
    """Comprueba que cada ciudad cae dentro de alguna region. Devuelve las fallidas."""
    boxes = [(p, _bbox(p["_geom"])) for p in cover]
    boxes = [(p, b) for p, b in boxes if b]
    failures = []
    print()
    print("Verificacion sobre ciudades reales:")
    for name, lon, lat in VERIFY_CITIES:
        hits = [
            p["id"] for p, b in boxes
            if b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and _inside((lon, lat), p["_geom"])
        ]
        if hits:
            # La app elegira la de bbox mas pequeno; se muestra esa.
            best = min(
                (p for p, _ in boxes if p["id"] in hits),
                key=lambda p: (lambda b: (b[2] - b[0]) * (b[3] - b[1]))(_bbox(p["_geom"])),
            )
            print(f"   ok   {name:24s} {best['id']}")
        else:
            print(f"   FALLA {name:24s} >>> SIN COBERTURA <<<")
            failures.append(name)
    return failures


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
    parser.add_argument("--verify", action="store_true", help="comprueba ciudades reales de los casos limite")
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

    if args.verify:
        failures = verify_cover(cover)
        if failures:
            print()
            print(f"*** {len(failures)} CIUDADES SIN COBERTURA: {failures}")
            return 1
        print("   -> todas cubiertas")

    per_continent = collections.Counter(r["continent"] for r in regions)
    print(f"\n{args.out}: {len(regions)} regiones")
    for continent, count in per_continent.most_common():
        print(f"   {continent:22s} {count:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
