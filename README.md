# joru-osm-data

Pipeline de datos de POIs para la app **Joru**. Convierte extractos regionales de
OpenStreetMap en ficheros SQLite que la app descarga y consulta **en local**, sin
depender de ninguna API en tiempo real.

Este repositorio contiene **solo el pipeline y los datos derivados**. El codigo de la
app vive en otro sitio.

---

## Por que existe

Joru obtenia los POIs de Overpass en vivo. Overpass limita a una peticion concurrente
por IP y penaliza las rafagas con 403/429: un plan de una megaciudad dispara decenas de
consultas seguidas y acababa en cascada de reintentos, minutos de espera y planes
incompletos.

Los datos de OSM se distribuyen tambien como **descarga masiva sin cuota**
([Geofabrik](https://download.geofabrik.de/)). Este pipeline los preprocesa una vez al
mes y publica un extracto por region, de modo que la app hace **una descarga** y a
partir de ahi consulta en milisegundos y sin red.

---

## Como funciona

```
Geofabrik (.osm.pbf)
      │
      ├─ scripts/build_extract.py    filtra POIs turisticos, calcula centroide y
      │                              tamano, poda tags -> <region>.sqlite
      ├─ scripts/build_catalog.py    reune los extractos -> catalog.json
      │
      └─ GitHub Release              assets publicos servidos por el CDN de GitHub
```

El workflow `.github/workflows/build-extracts.yml` corre **el dia 1 de cada mes** y
tambien a mano (`workflow_dispatch`).

### URL estable

La app pide siempre la release mas reciente, asi que regenerar los datos o anadir
regiones **no exige publicar una version nueva de la app**:

```
https://github.com/<owner>/joru-osm-data/releases/latest/download/catalog.json
https://github.com/<owner>/joru-osm-data/releases/latest/download/<region>.sqlite
```

---

## Formato

### `catalog.json`

```jsonc
{
  "schema": 1,
  "generated_at": "2026-07-29T10:00:00Z",
  "generator_version": "1",
  "filter_fingerprint": "67685a26b631f4d5",
  "regions": [
    {
      "id": "kansai",
      "name": "Kansai",
      "country": "JP",
      "file": "kansai.sqlite",
      "format": "sqlite",
      "bytes": 17203200,
      "sha256": "…",
      "poi_count": 105328,
      "osm_timestamp": "2026-07-28T20:21:00Z",
      "bbox": [134.278, 33.436, 136.984, 35.775]   // [minLon, minLat, maxLon, maxLat]
    }
  ]
}
```

### `<region>.sqlite`

```sql
CREATE TABLE poi (
  id     TEXT PRIMARY KEY,   -- "node/123" | "way/456" | "relation/789"
  lon    REAL NOT NULL,      -- centroide (nodo: su posicion; way/relacion: centro del bbox)
  lat    REAL NOT NULL,
  size_m REAL,               -- diagonal del bbox en metros; NULL en nodos puntuales
  tags   TEXT NOT NULL       -- JSON con los tags de OSM podados
);
CREATE INDEX idx_poi_lonlat ON poi(lon, lat);

CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
```

Los tags se guardan **crudos**: la clasificacion (categoria, cocina, dieta, duracion,
notabilidad) vive en la app, para poder ajustarla sin regenerar los extractos.

---

## Anadir una region

1. Buscar el extracto en <https://download.geofabrik.de/> (cuanto mas ajustado, mejor).
2. Anadir la entrada a `regions.json`:
   ```json
   { "id": "toscana", "name": "Toscana", "country": "IT",
     "source": "https://download.geofabrik.de/europe/italy/centro-latest.osm.pbf" }
   ```
3. Lanzar el workflow a mano. Con el campo *regions* vacio reconstruye todo y publica;
   con una seleccion parcial solo sube un artefacto para inspeccion, sin publicar
   (un catalogo parcial dejaria al resto de regiones sin resolver en la app).

El bbox **no** se declara: se deriva de los POIs reales de cada extracto.

---

## Uso en local

```bash
pip install osmium
curl -L -o kansai.osm.pbf https://download.geofabrik.de/asia/japan/kansai-latest.osm.pbf
python scripts/build_extract.py kansai.osm.pbf dist/kansai.sqlite --region kansai
python scripts/build_catalog.py --regions regions.json --dist dist --out dist/catalog.json
```

---

## Licencia y atribucion

Los datos derivados que publica este repositorio proceden de **OpenStreetMap** y se
distribuyen bajo la **Open Database License (ODbL) 1.0**.

> © Colaboradores de OpenStreetMap — <https://www.openstreetmap.org/copyright>

Cualquier producto que use estos extractos debe mantener esa atribucion. El codigo del
pipeline (`scripts/`, workflows) es de RcuDev.
