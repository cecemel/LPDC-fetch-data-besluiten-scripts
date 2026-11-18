# besluiten fetch

```bash
mkdir -p out
docker build -t besluiten-fetch .
docker run --rm -v "$PWD/out:/app/out" besluiten-fetch
```

> Warning: this is LLM generated. Read the code before you trust it.

Fetch besluiten for a set of bestuurseenheden from SPARQL endpoints. For each besluit it writes a mu-semtech migration: a `.ttl`, a `.graph` and a `.svg`.

The data follows the besluit-publicatie model. For each besluit the script follows one fixed path and keeps the triples of every resource on it:

```
zitting -> agendapunt <- behandeling van agendapunt <-> besluit -> artikel
```

The output holds only the zitting and the path below it. The bestuurseenheid, its organen and the organen in tijd are not included. Centrale Vindplaats supplies the organen in tijd for discovery only. Other links stay in the graph as references, but the script does not follow them. The predicates in `predicate_blacklist` are not fetched at all.

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
    },
    {
      "predicate": "http://data.vlaanderen.be/ns/besluit#heeftAanwezige",
      "comment": "Attendance lists add one mandataris record per member."
    }
  ],
  "besturen": [
    "http://data.lblod.info/id/bestuurseenheden/<uuid>",
    {"uri": "http://data.lblod.info/id/bestuurseenheden/<uuid>", "name": "Puurs"}
  ]
}
```

The registry is the first source for the `mu:uuid` and `skos:prefLabel` of a bestuur, and for the organen in tijd used in discovery. If a value is missing there, the script falls back to the harvesters. The registry is not queried for besluiten. The endpoints are the harvesters with the besluiten.

`besturen` takes URIs. An entry with `name` is also allowed. The name is optional and only for clarity. The output filename uses `skos:prefLabel` from the registry, with a fallback to the harvesters.

`predicate_blacklist` holds predicates that are not fetched. An entry is a URI string or an object with `predicate` and an optional `comment`. The comment is logged at startup. Remove the entry to keep the predicate.

`--endpoints` overrides the harvester list for one run. `--only` takes one or more exact bestuur URIs. You can mount another config without a rebuild: `-v "$PWD/config.json:/app/config.json:ro"`.

The target graph is `http://mu.semte.ch/graphs/organizations/<mu:uuid>/LoketLB-LPDCGebruiker`. The `<mu:uuid>` comes from the registry, with a fallback to the harvesters. It is not derived from the URI. You can change the graph with `graph_template` in the config. It must contain `{uuid}`.

## output

All files land flat in `out/data/`. The bestuur is in the filename.

For each besluit:

- `<timestamp>-<bestuur-slug>-<uuid-besluit>.ttl`: the besluit and its path.
- `<timestamp>-<bestuur-slug>-<uuid-besluit>.graph`: the target graph.
- `<timestamp>-<bestuur-slug>-<uuid-besluit>.svg`: UML-like drawing. Hover shows the full value, click copies it.
- `<bestuur-slug>-besluiten-index.json`: the besluiten found for the bestuur.

A run also writes `out/logs/fetch-<ts>.log` and `out/logs/summary-<ts>.json`.

A besluit is skipped if a `.ttl` for its UUID is already in `out/data/`.

## options

```
--config PATH             config file, default config.json
--out PATH                output root, default ./out
--endpoints LIST          override the endpoints from the config
--besluit-list-file PATH  JSON with besluit URIs per bestuur, skips discovery and uses all live harvesters
--limit N                 max besluiten per bestuur
--triple-cap N            stop fetching a besluit past N triples
--dry-run                 list only, write nothing
--no-svg                  skip the drawing
--ttl2svg PATH            ttl2svg clone or visualize_ttl.py
--only LIST               only these bestuur URIs
--sparql-timeout N        timeout per request, default 120
-v                        debug logging
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

Discovery asks the registry for the organen in tijd of the bestuur, then runs one small query per orgaan on the harvesters. It tries the harvesters in config order and stops at the first one that returns besluiten. That harvester is used for the whole bestuur; the other harvesters are not used for discovery or fetching. A manual `--besluit-list-file` skips discovery, so all live harvesters are used.

Every discovery and fetch query is logged at INFO with its endpoint and query text. Harvester calls wait 1 second between requests. Registry calls are not delayed.

A bestuur without `mu:uuid` or `skos:prefLabel` on the registry or the harvesters is skipped. The script does not derive the graph or the folder from the URI.

The default config blacklists `prov:wasDerivedFrom`, the chair, the secretary, attendance lists, votes and `extractedDecisionContent`. The comments in `config.json` say why. Remove an entry to keep the predicate. Large migrations make large SVGs; use `--no-svg` if needed.

The script keeps other links, such as `besluit:gebeurtNa` and `besluit:aangebrachtNa`, but does not follow them. One migration describes one besluit and its path.
