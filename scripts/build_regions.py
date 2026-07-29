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

PODA (medida el 2026-07-29, ver PRUNE mas abajo)
------------------------------------------------
El recubrimiento en bruto son 530 regiones y 138,5 GB de descarga en CI, MAS que el
planeta entero (87 GB), porque acepta regiones que se solapan. La poda quita las que
NINGUN destino del mundo elegiria jamas. El criterio NO es el area -eso ya se intento y
colapso el corte a 28 regiones dejando a Londres en `europe`-: es la DEMANDA, medida
sobre las 235.063 localidades de GeoNames de mas de 500 habitantes.

Uso:
    pip install numpy
    python scripts/build_regions.py --out regions.world.json --verify
"""


from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

# CON geometria: el corte es geometrico, asi que los poligonos son imprescindibles.
GEOFABRIK_INDEX = "https://download.geofabrik.de/index-v1.json"
USER_AGENT = "joru-osm-data/1.0 (+https://github.com/RcuDev/joru-osm-data)"

# Proporcion medida entre el .osm.pbf de origen y el .sqlite resultante, sobre tres
# regiones densas y turisticas (13,1/253, 17,0/332, 24,7/458). En zonas rurales el pbf
# esta dominado por carreteras y edificios, no por POIs, asi que el ratio real sera
# MENOR: sirve como techo, no como suelo.
SQLITE_RATIO = 0.052

# Demanda real para decidir que regiones sobran: todas las localidades del mundo de mas
# de 500 habitantes (GeoNames, CC-BY). Es un fichero estable de ~10 MB y este script se
# ejecuta a mano, no en CI, asi que depender de la red aqui no rompe nada automatico.
GEONAMES_CITIES = "http://download.geonames.org/export/dump/cities500.zip"

# Lado de la celda con la que se rasteriza el mundo, en grados (~28 km en el ecuador).
# Es la MISMA rejilla con la que build_extract.py marca donde hay POIs y con la que el
# catalogo describe cada region: si se cambia aqui, cambiarla alli.
CELL_DEG = 0.25
NX, NY = int(360 / CELL_DEG), int(180 / CELL_DEG)

# Una region que ningun destino elegiria jamas es 100% coste: se descarga en CI, se
# publica en el Release y no la baja nadie. Se quitan dos familias, ambas MEDIDAS:
#   - las que no elige NI UNA de las 235.063 localidades (padres cuyos hijos ya
#     particionan el territorio: `us` frente a sus estados, `norway` frente a
#     ostlandet/vestlandet/nord-norge, `japan` frente a kanto/kansai/chubu...);
#   - las que elige un punado pero pesan tanto que un movil no puede bajarlas
#     (`asia` son 799 MB de SQLite: no es una region, es un castigo).
# Lo que quede fuera del corte cae al respaldo de emergencia (Overpass), que es
# exactamente para lo que existe.
MIN_DEMAND_FOR_HUGE = 50
HUGE_SQLITE_MB = 100


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

    # Esto deja SOLAPE: 530 regiones y 138,5 GB, mas que el planeta entero. Reducirlo
    # por area no funciona (colapsaba el corte a 28 regiones y dejaba a Londres en
    # `europe`); se reduce por DEMANDA en prune_cover(), que es la siguiente pasada.
    return [prop for prop, _ in accepted]


def cells_of(geom) -> np.ndarray:
    """Celdas de la rejilla cuyo centro cae dentro del poligono.

    Scanline vectorizado: por cada fila de la rejilla se cruzan de golpe todas las
    aristas de todos los anillos. Un punto-en-poligono por celda seria O(celdas x
    aristas) y `asia` tiene ~50.000 aristas: no acabaria.
    """
    rings = _rings(geom)
    if not rings:
        return np.zeros((0, 2), dtype=np.int32)
    x1 = np.concatenate([np.array([p[0] for p in r[:-1]]) for r in rings])
    y1 = np.concatenate([np.array([p[1] for p in r[:-1]]) for r in rings])
    x2 = np.concatenate([np.array([p[0] for p in r[1:]]) for r in rings])
    y2 = np.concatenate([np.array([p[1] for p in r[1:]]) for r in rings])
    out = []
    for j in range(max(0, int((y1.min() + 90) / CELL_DEG)),
                   min(NY - 1, int((y1.max() + 90) / CELL_DEG)) + 1):
        y = -90 + (j + 0.5) * CELL_DEG
        crosses = (y1 <= y) != (y2 <= y)
        if not crosses.any():
            continue
        xs = np.sort(x1[crosses] + (y - y1[crosses]) * (x2[crosses] - x1[crosses])
                     / (y2[crosses] - y1[crosses]))
        for a, b in zip(xs[0::2], xs[1::2]):
            ia = max(0, int(np.ceil((a + 180) / CELL_DEG - 0.5)))
            ib = min(NX - 1, int(np.floor((b + 180) / CELL_DEG - 0.5)))
            if ib >= ia:
                out.append(np.stack([np.arange(ia, ib + 1), np.full(ib - ia + 1, j)], axis=1))
    return np.concatenate(out) if out else np.zeros((0, 2), dtype=np.int32)


def fetch_cities(cache: Path) -> list[tuple[float, float]]:
    """Localidades del mundo de mas de 500 habitantes (GeoNames)."""
    if not cache.exists():
        request = urllib.request.Request(GEONAMES_CITIES, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=300) as response:
            cache.write_bytes(response.read())
    with zipfile.ZipFile(cache) as z:
        text = z.read("cities500.txt").decode("utf-8")
    rows = csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    return [(float(r[5]), float(r[4])) for r in rows]


def prune_cover(cover: list[dict], sizes: dict[str, int | None], cities) -> tuple[list[dict], list[tuple]]:
    """Quita las regiones que ningun destino del mundo elegiria. Ver PRUNE arriba."""
    rasters, areas = {}, {}
    for p in cover:
        box = _bbox(p["_geom"])
        areas[p["id"]] = (box[2] - box[0]) * (box[3] - box[1])
        rasters[p["id"]] = cells_of(p["_geom"])

    # Para cada celda, la region de bbox mas pequeno que la cubre: la que elegiria la app.
    best: dict[tuple[int, int], str] = {}
    for region, cells in rasters.items():
        for cx, cy in cells:
            key = (int(cx), int(cy))
            if key not in best or areas[region] < areas[best[key]]:
                best[key] = region

    demand: collections.Counter = collections.Counter()
    for lon, lat in cities:
        region = best.get((int((lon + 180) / CELL_DEG), int((lat + 90) / CELL_DEG)))
        if region:
            demand[region] += 1

    kept, dropped = [], []
    for p in cover:
        region = p["id"]
        mb = (sizes.get(region) or 0) * SQLITE_RATIO / 2**20
        # Una region mas pequena que una celda (Monaco, Ceuta, Bristol, Washington DC)
        # no atrapa ningun centro de la rejilla y saldria con demanda 0 por un ARTEFACTO
        # del muestreo, no por sobrar. Se conservan siempre.
        if len(rasters[region]) == 0:
            kept.append(p)
        elif demand[region] == 0:
            dropped.append((region, sizes.get(region) or 0, "no la elige ninguna localidad"))
        elif demand[region] < MIN_DEMAND_FOR_HUGE and mb > HUGE_SQLITE_MB:
            dropped.append((region, sizes.get(region) or 0,
                            f"{mb:.0f} MB de SQLite para {demand[region]} localidades"))
        else:
            kept.append(p)
    return kept, dropped


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
    # Una region que YA es raiz (los continentes que Geofabrik publica enteros, y Rusia)
    # es su propio continente. Devolver "other" la mandaba a un shard llamado asi, que
    # no dice nada y ademas rompe `--scope australia-oceania`.
    return node["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("regions.json"))
    parser.add_argument("--no-prune", action="store_true", help="deja el recubrimiento en bruto, con sus solapes")
    parser.add_argument("--verify", action="store_true", help="comprueba ciudades reales de los casos limite")
    parser.add_argument("--cache", type=Path, default=Path(".cache"), help="donde guardar el fichero de GeoNames")
    args = parser.parse_args()

    props = fetch_index()
    by_id = {p["id"]: p for p in props}
    cover = world_cover(props)
    print(f"regiones del recubrimiento: {len(cover)}")

    # Los tamanos no son informativos: la poda los necesita para descartar regiones
    # imposibles de bajar en un movil, y el workflow para repartir la matriz por peso.
    print("consultando tamanos (HEAD, sin descargar)...")
    with ThreadPoolExecutor(max_workers=12) as pool:
        urls = [(p["id"], p["urls"]["pbf"]) for p in cover]
        sizes = {region: size for (region, _), size
                 in zip(urls, pool.map(lambda x: pbf_size(x[1]), urls))}
    bruto = sum(s for s in sizes.values() if s)
    print(f"en bruto: {bruto / 2**30:,.1f} GB de pbf -> SQLite estimado {bruto * SQLITE_RATIO / 2**30:,.2f} GB")

    if not args.no_prune:
        args.cache.mkdir(exist_ok=True)
        cities = fetch_cities(args.cache / "cities500.zip")
        print(f"podando contra {len(cities):,} localidades reales...")
        cover, dropped = prune_cover(cover, sizes, cities)
        for region, size, why in sorted(dropped, key=lambda d: -d[1]):
            print(f"   fuera  {region:28s} {size / 2**20:8.0f} MB   {why}")
        podado = sum(sizes.get(p["id"]) or 0 for p in cover)
        print(f"podado : {len(cover)} regiones, {podado / 2**30:,.1f} GB de pbf "
              f"-> SQLite estimado {podado * SQLITE_RATIO / 2**30:,.2f} GB "
              f"({(1 - podado / bruto) * 100:.0f}% menos)")

    regions = []
    for p in sorted(cover, key=lambda x: x["id"]):
        entry = {
            "id": region_id(p["id"]),
            "name": p["name"],
            "country": (p.get("iso3166-1:alpha2") or [""])[0],
            "continent": continent_of(p, by_id),
            "source": p["urls"]["pbf"],
        }
        if sizes.get(p["id"]):
            entry["pbf_bytes"] = sizes[p["id"]]
        regions.append(entry)

    payload = {
        "$comment": (
            "GENERADO por scripts/build_regions.py desde el indice de Geofabrik, y podado "
            "contra las localidades reales del mundo. No editar a mano: se regenera. La "
            "geometria no se declara aqui, se deriva de los POIs reales al construir cada "
            "extracto (ver meta.cells) y acaba en catalog.json como rectangulos."
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
