# besluiten fetch

```bash
mkdir -p out
docker build -t besluiten-fetch .
docker run --rm -v "$PWD/out:/app/out" besluiten-fetch
```

> Warning: this is LLM generated. Read the code before you trust it.

Fetch besluiten for a set of bestuurseenheden from SPARQL endpoints. For each besluit it writes a mu-semtech migration: a `.ttl`, a `.graph` and a `.svg`.

The data follows the besluit-publicatie model. The script walks the path of the besluit from the erkende standaard 2025-12-11 SHACL shape and keeps all triples of the resources on that path.

## start

```bash
BESTUUR="http://data.lblod.info/id/bestuurseenheden/c8f33e80-6f19-4ba1-a758-9d85f42d28d4"
docker run --rm -v "$PWD/out:/app/out" besluiten-fetch --dry-run --only "$BESTUUR"
docker run --rm -v "$PWD/out:/app/out" besluiten-fetch --limit 1 --only "$BESTUUR"
docker run --rm -v "$PWD/out:/app/out" besluiten-fetch --only "$BESTUUR"
docker run --rm -v "$PWD/out:/app/out" besluiten-fetch
```

The first run only lists what it finds. The second writes one migration to `out/data/`. The third fetches all besluiten for that bestuur. The last one fetches all besluiten for every bestuur in `config.json`.

## config

`config.json` holds the registry, the harvesters and the bestuurseenheden. Edit it before you run.

```json
{
  "registry": "https://centrale-vindplaats.lblod.info/sparql",
  "endpoints": ["https://lokaalbeslist-harvester-0.s.redhost.be/sparql"],
  "predicate_blacklist": [
    {
      "predicate": "http://www.w3.org/ns/prov#wasDerivedFrom",
      "comment": "Shared resources carry thousands of publication links."
    }
  ],
  "besturen": [
    "http://data.lblod.info/id/bestuurseenheden/<uuid>",
    {"uri": "http://data.lblod.info/id/bestuurseenheden/<uuid>", "name": "Puurs"}
  ]
}
```

The registry is the source for the `mu:uuid` and `skos:prefLabel` of a bestuur, and for the `bestuurt` link. The endpoints are the harvesters with the besluiten.

`besturen` takes URIs. An entry with `name` is also allowed. The name is optional and only for clarity. The output folder uses `skos:prefLabel` from the registry.

`predicate_blacklist` holds predicates that are not fetched. An entry is a URI string or an object with `predicate` and an optional `comment`. The comment is logged at startup. Remove the entry to keep the predicate.

`--endpoints` overrides the harvester list for one run. `--only` takes one or more exact bestuur URIs. You can mount another config without a rebuild: `-v "$PWD/config.json:/app/config.json:ro"`.

The target graph is `http://mu.semte.ch/graphs/organizations/<mu:uuid>/LoketLB-LPDCGebruiker`. The `<mu:uuid>` comes from the registry, not from the URI. You can change the graph with `graph_template` in the config. It must contain `{uuid}`.

## output

All files land flat in `out/data/`. The bestuur is in the filename.

For each besluit:

- `<bestuur-slug>__<timestamp>-<besluit-id>.ttl`: the besluit and its context.
- `<bestuur-slug>__<timestamp>-<besluit-id>.graph`: the target graph.
- `<bestuur-slug>__<timestamp>-<besluit-id>.svg`: UML-like drawing. Hover shows the full value, click copies it.
- `<bestuur-slug>__besluiten-index.json`: the besluiten found for the bestuur.

A run also writes `out/logs/fetch-<ts>.log` and `out/logs/summary-<ts>.json`.

## options

```
--config PATH         config file, default config.json
--out PATH            output root, default ./out
--endpoints LIST      override the endpoints from the config
--besluit-list-file   JSON with besluit URIs per bestuur, skips discovery
--limit N             max besluiten per bestuur
--max-depth N         hops from the besluit, default 3
--triple-cap N        stop expanding after N triples
--dry-run             list only, write nothing
--no-svg              skip the drawing
--ttl2svg PATH        ttl2svg clone or visualize_ttl.py
--allow-ns LIST       extra namespaces to expand
--only LIST           only these bestuur URIs
--sparql-timeout N    timeout per request, default 120
-v                    debug logging
```

## without docker

```bash
pip install -r requirements.txt
apt install graphviz git
python3 fetch_besluiten.py --limit 5
python3 fetch_besluiten.py
```

The first run is a test. The second fetches everything in `config.json`.

The ttl2svg clone happens on first use: [cecemel/ttl2svg](https://github.com/cecemel/ttl2svg) is cloned into `~/.cache/besluiten-fetch/ttl2svg`. Later runs use that clone. Point `--ttl2svg PATH` or `TTL2SVG_PATH` at a local clone if you have one.

## notes

The Docker build clones the same pinned ttl2svg commit. The image needs network during build, not at runtime.

A bestuur without `mu:uuid` or `skos:prefLabel` in the registry is skipped. The script does not derive the graph or the folder from the URI.

The default config blacklists `prov:wasDerivedFrom`. The comment in `config.json` says why. Remove the entry to keep the predicate. Large migrations make large SVGs; use `--no-svg` if needed.

The script keeps `besluit:gebeurtNa` and `besluit:aangebrachtNa` but does not follow them. One migration describes one besluit and its context.
