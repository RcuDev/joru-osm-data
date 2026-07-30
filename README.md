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
([Geofabrik](https://download.geofabrik.de/)). Este pipeline los preprocesa cada
trimestre y publica un extracto por region, de modo que la app hace **una descarga** y a
partir de ahi consulta en milisegundos y sin red.

La cobertura es **mundial**: 511 regiones, ~83 GB de `.osm.pbf` de origen y ~4,3 GB de
SQLite publicado. El 99,7% de las localidades del mundo resuelven a una region de menos
de 50 MB.

---

## Como funciona

```
Geofabrik (.osm.pbf)
      │
      ├─ scripts/build_regions.py    corta el mundo en regiones -> regions.json
      │                              (a mano, cuando cambie el indice de Geofabrik)
      ├─ scripts/plan_build.py       reparte esas regiones entre los shards del workflow
      ├─ scripts/build_extract.py    filtra POIs turisticos, calcula centroide y
      │                              tamano, poda tags -> <region>.sqlite
      ├─ scripts/build_catalog.py    reune los metadatos -> catalog.json
      │
      └─ GitHub Release              assets publicos servidos por el CDN de GitHub
```

El workflow `.github/workflows/build-extracts.yml` corre **el dia 1 de enero, abril,
julio y octubre**, y tambien a mano (`workflow_dispatch`).

**Geofabrik falla a veces**, y no por carga nuestra: el 2026-07-30 su proxy devolvía
**502 para cualquier `.pbf`**, incluido un `.md5` de 100 bytes. Por eso una región que no
se descarga **no tumba su shard**: se apunta, el resto sigue, y el modo `repair`
reconstruye después solo las que faltan. El `curl` reintenta con espera exponencial
(~4 min); si aun así no hay servicio, es una incidencia y toca esperar.

Se construye en **matriz**: `plan_build.py` reparte las regiones en shards de ~4 GB
(25 para el mundo, 9 para Europa) para que ningun job dure horas y un fallo se relance
solo. Cada shard sube sus `.sqlite` directamente al Release —que se crea como
**borrador**, asi que `/releases/latest/download/` sigue sirviendo el anterior entero
mientras tanto— y deja solo unos metadatos minusculos como artefacto. Un job final los
reune, genera `catalog.json` y publica. El catalogo **no se puede generar por shard**:
la app necesita todas las regiones en uno.

### Entradas de `workflow_dispatch`

| Entrada | Para que |
|---|---|
| `scope` | Continentes o ids de region separados por espacios. Vacio = el mundo. Ej.: `europe`, `asia africa`, `kansai kanto` |
| `repair` | Construye **solo** las regiones que falten en el Release vigente y lo rellena en su sitio, sin crear uno nuevo. Pásale el **mismo `scope`** que la corrida original |
| `publish` | Desmarcar para probar sin tocar el catalogo vivo |

> **Aviso:** GitHub **desactiva los workflows programados tras 60 dias sin actividad en el
> repositorio**, y las ejecuciones del propio workflow NO cuentan como actividad. Como aqui
> se toca poco el codigo, hay que estar atento al correo de aviso de GitHub y reactivarlo
> desde la pestana Actions (o hacer cualquier commit para reiniciar el contador).

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
  "schema": 2,
  "generated_at": "2026-07-29T10:00:00Z",
  "generator_version": "2",
  "filter_fingerprint": "7fbf39c1475f41e5",
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
      "bbox":  [134.278, 33.436, 136.984, 35.775],  // [minLon, minLat, maxLon, maxLat]
      "rects": [[135.0, 34.5, 135.75, 35.25], …]    // donde hay POIs de verdad
    }
  ]
}
```

**`rects` es lo que la app usa para elegir region; `bbox` es solo informativo.** Un
unico bbox miente: el de Portugal se estira hasta las Azores y acaba siendo mayor que el
de Espana, asi que "la region de bbox mas pequeno que contiene el destino" mandaba Lisboa
al extracto espanol. Medido sobre las 235.063 localidades del mundo de mas de 500
habitantes, el bbox unico manda al **22%** de los destinos a una region que no los
contiene (Argel a `spain`, Toronto a `us/new-york`, Santiago de Chile a `argentina`), y
ademas hay 10 regiones que cruzan el antimeridiano —Alaska, Fiyi, Nueva Zelanda,
Chukotka— cuyo bbox va de -180 a 180 y contiene **cualquier** punto del planeta. Con los
rectangulos, derivados de los POIs reales en una rejilla de 0,25°, ese error es cero.

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

## El corte del mundo (`regions.json`)

**Generado, no escrito a mano.** `regions.json` sale de `scripts/build_regions.py`, que
recorta el indice oficial de Geofabrik. Regenerarlo solo hace falta cuando Geofabrik
cambie su indice:

```bash
pip install numpy
python scripts/build_regions.py --out regions.json --verify
```

El corte es **geometrico**, no estructural, porque el arbol del indice miente de tres
formas: publica paquetes combinados que solapan con las regiones reales (`dach`, `alps`,
`us-south`…, que sumados daban 114,9 GB, mas que el planeta entero); la jerarquia no es
la del campo `parent` (los estados de EE.UU. cuelgan de `north-america`, no de `us`); y
**los hijos no son una particion del padre** (`enfield` figura como hijo de
`greater-london`, asi que "prefiere los hijos" se quedaba con un barrio y tiraba el
resto de Londres). El algoritmo acepta regiones de menor a mayor area salvo que su
territorio ya lo cubran las aceptadas.

Luego **poda por demanda**: quita las regiones que ningun destino elegiria jamas,
medido contra las 235.063 localidades del mundo de mas de 500 habitantes (GeoNames).
Son padres cuyos hijos ya particionan el territorio (`us` frente a sus estados,
`norway` frente a ostlandet/vestlandet/nord-norge) y regiones imposibles de bajar en un
movil (`asia` son 799 MB de SQLite). Tambien caen las **fantasma**: las que el indice
anuncia pero Geofabrik ya no publica -redirigen a su portada y sirven el HTML con un
200, asi que se descargarian "bien" y reventarian en osmium (le paso a `enfield`)-.
Eso baja el corte de 530 a 511 regiones y de 170,9 GB a 83,3 GB. Podar por **area** en
vez de por demanda no funciona: se probo, y colapsaba el corte a 28 regiones dejando a
Londres resolviendo a `europe` entero.

`--verify` comprueba 36 ciudades de los casos limite (Londres, Enfield, Jersey, Isla de
Man, Canarias, Azores, Comoras, Kaliningrado, Ceuta, Groenlandia, Tahiti…). **Cada una
cazo un fallo real.** Devuelve exit 1 si alguna se queda sin cobertura: no des por bueno
un corte que no pase esta verificacion.

Lo que quede fuera del corte (Miquelon, Grytviken, Diego Garcia: territorios que
Geofabrik no publica) cae al respaldo de emergencia de la app.

---

## Uso en local

```bash
pip install osmium
curl -L -o kansai.osm.pbf https://download.geofabrik.de/asia/japan/kansai-latest.osm.pbf
python scripts/build_extract.py kansai.osm.pbf dist/kansai.sqlite --region kansai
python scripts/build_catalog.py --regions regions.json --dist dist --out dist/catalog.json
```

Si tocas el workflow, **validalo antes de empujarlo** (`pip install pyyaml`):

```bash
python scripts/lint_workflow.py
```

Caza dos errores que no se ven a simple vista: expresiones `${…}` vacias -que GitHub
evalua incluso dentro de los comentarios de shell de un `run:`, tumbando el fichero
entero- y expresiones interpoladas dentro de un `run:`, que hay que pasar por `env:`.

---

## Licencia y atribucion

Los datos derivados que publica este repositorio proceden de **OpenStreetMap** y se
distribuyen bajo la **Open Database License (ODbL) 1.0**.

> © Colaboradores de OpenStreetMap — <https://www.openstreetmap.org/copyright>

Cualquier producto que use estos extractos debe mantener esa atribucion. El codigo del
pipeline (`scripts/`, workflows) es de RcuDev.
