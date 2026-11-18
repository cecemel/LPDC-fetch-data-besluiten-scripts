#!/usr/bin/env python3
"""Fetch besluiten (decisions) for a set of bestuurseenheden and write one
mu-semtech migration per besluit (a .graph + .ttl pair) plus an SVG drawing.

Data comes from SPARQL endpoints with Lokaal Beslist or Centrale Vindplaats
data that follows the besluit-publicatie model:

  https://data.vlaanderen.be/doc/applicatieprofiel/besluit-publicatie/erkendestandaard/2025-12-11/shacl/besluit-publicatie-SHACL.ttl

For every besluit the script collects the resources on one fixed path and
keeps their triples:

  zitting -> agendapunt <- behandeling van agendapunt <-> besluit -> artikel

Only those resources are described. The bestuurseenheid, the bestuursorganen
and the bestuursorganen in tijd are not included. Predicates that do not
belong to the path stay in the graph as links but are not followed. The
predicates in `predicate_blacklist` are not fetched at all.

Discovery asks the registry for the bestuursorganen in tijd of a bestuur, then
runs one small query per orgaan on the harvesters. It uses the first harvester
that returns besluiten and ignores the other harvesters for that bestuur.

The registry is not queried for besluiten.

Configuration lives in config.json: the registry, the harvester endpoints, the
predicate blacklist and the bestuurseenheden to fetch. Run
`python3 fetch_besluiten.py --help` for the options. See README.md for the
Docker workflow.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import rdflib
    from rdflib import Graph, URIRef, Literal, BNode
except ImportError:  # pragma: no cover - dependency is installed in the image
    sys.stderr.write("rdflib is required: pip install rdflib\n")
    raise


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GRAPH_TEMPLATE = "http://mu.semte.ch/graphs/organizations/{uuid}/LoketLB-LPDCGebruiker"

SHACL_URL = (
    "https://data.vlaanderen.be/doc/applicatieprofiel/besluit-publicatie/"
    "erkendestandaard/2025-12-11/shacl/besluit-publicatie-SHACL.ttl"
)

CONFIG_FILENAME = "config.json"

TTL2SVG_REPO = "https://github.com/cecemel/ttl2svg.git"
TTL2SVG_REF = "dbf5246bf1385052aaf549d9fdc0ac3d7cdce9a4"

MU_UUID = URIRef("http://mu.semte.ch/vocabularies/core/uuid")
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

BESLUIT_NS = "http://data.vlaanderen.be/ns/besluit#"
ELI_NS = "http://data.europa.eu/eli/ontology#"
PROV_NS = "http://www.w3.org/ns/prov#"
DCT_NS = "http://purl.org/dc/terms/"


@dataclass(frozen=True)
class PathStep:
    source: str
    predicate: str
    inverse: bool
    target: str


# The only links the script follows. Every resource on this path is described
# in full (minus the blacklist). Other links stay in the graph as references
# but are not expanded.
PATH_STEPS = [
    PathStep("besluit", PROV_NS + "wasGeneratedBy", False, "behandeling"),
    PathStep("besluit", ELI_NS + "has_part", False, "artikel"),
    PathStep("behandeling", DCT_NS + "subject", False, "agendapunt"),
    PathStep("agendapunt", BESLUIT_NS + "behandelt", True, "zitting"),
]

PATH_ROLES = [
    "besluit",
    "behandeling",
    "artikel",
    "agendapunt",
    "zitting",
]

SPARQL_TIMEOUT = 120  # seconds per request
PROBE_TIMEOUT = 20   # seconds for the liveness ping
HTTP_RETRIES = 3
RETRY_BACKOFF = 5  # seconds
HARVESTER_DELAY = 1.0  # seconds between calls to the same harvester


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "bestuur"


def now_ts() -> str:
    return _dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")


def uri_to_safe_name(uri: str) -> str:
    """Turn a besluit URI into a safe filename fragment, preferring its UUID-ish tail."""
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-")
    return tail or "besluit"


def uuid_from_uri(uri: str) -> str | None:
    matches = UUID_RE.findall(uri)
    return matches[-1] if matches else None


def besluit_uuid_from_graph(graph: Graph, besluit_uri: str) -> str | None:
    for value in graph.objects(URIRef(besluit_uri), MU_UUID):
        if isinstance(value, Literal):
            return str(value)
    return None


def existing_besluit_uuids(data_dir: Path) -> set[str]:
    found: set[str] = set()
    if not data_dir.exists():
        return found
    for path in data_dir.glob("*.ttl"):
        matches = UUID_RE.findall(path.stem)
        if matches:
            found.add(matches[-1].lower())
    return found


# ---------------------------------------------------------------------------
# SPARQL client
# ---------------------------------------------------------------------------

class SparqlClient:
    def __init__(
        self,
        endpoint: str,
        timeout: int = SPARQL_TIMEOUT,
        logger: logging.Logger | None = None,
        delay: float = 0.0,
    ):
        self.endpoint = endpoint
        self.timeout = timeout
        self.log = logger or logging.getLogger("sparql")
        self.delay = delay
        self._last_call_end: float | None = None

    def _wait_for_slot(self) -> None:
        if self.delay <= 0 or self._last_call_end is None:
            return
        wait = self.delay - (time.monotonic() - self._last_call_end)
        if wait > 0:
            self.log.debug("waiting %.2fs before the next call to %s", wait, self.endpoint)
            time.sleep(wait)

    def _mark_call_end(self) -> None:
        self._last_call_end = time.monotonic()

    def _request(self, query: str, accept: str):
        self._wait_for_slot()
        self.log.info("SPARQL query on %s:\n%s", self.endpoint, query)
        # POST the query in the body to avoid URL-length limits (HTTP 414/431).
        data = urllib.parse.urlencode({"query": query}).encode("utf-8")
        last_err = None
        try:
            for attempt in range(1, HTTP_RETRIES + 1):
                try:
                    req = urllib.request.Request(
                        self.endpoint,
                        data=data,
                        headers={
                            "Accept": accept,
                            "Content-Type": "application/x-www-form-urlencoded",
                            "User-Agent": "lpdc-fetch-besluiten/0.1",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        return resp.read()
                except urllib.error.HTTPError as e:
                    last_err = e
                    body = ""
                    try:
                        body = e.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                    self.log.warning("HTTP %s on %s (attempt %d/%d): %s", e.code, self.endpoint, attempt, HTTP_RETRIES, body)
                    if e.code in (400,):
                        break  # query error, no point retrying
                except (urllib.error.URLError, TimeoutError) as e:
                    last_err = e
                    self.log.warning("Network error on %s (attempt %d/%d): %s", self.endpoint, attempt, HTTP_RETRIES, e)
                time.sleep(RETRY_BACKOFF * attempt)
            raise RuntimeError(f"SPARQL request failed for {self.endpoint}: {last_err}")
        finally:
            self._mark_call_end()

    def select(self, query: str) -> list[dict]:
        raw = self._request(query, "application/sparql-results+json")
        data = json.loads(raw.decode("utf-8"))
        rows = []
        for b in data.get("results", {}).get("bindings", []):
            rows.append({k: v["value"] for k, v in b.items()})
        return rows

    def select_bindings(self, query: str) -> list[dict]:
        """Raw SPARQL JSON bindings (preserving type/datatype/lang metadata)."""
        raw = self._request(query, "application/sparql-results+json")
        data = json.loads(raw.decode("utf-8"))
        return data.get("results", {}).get("bindings", [])

    def ping(self, timeout: int = PROBE_TIMEOUT) -> bool:
        """Fast liveness check: a trivial SELECT, one attempt, short timeout."""
        q = "SELECT * WHERE { ?s ?p ?o } LIMIT 1"
        params = urllib.parse.urlencode({"query": q}).encode("utf-8")
        self._wait_for_slot()
        try:
            req = urllib.request.Request(
                self.endpoint,
                data=params,
                headers={
                    "Accept": "application/sparql-results+json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "lpdc-fetch-besluiten/0.1",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            return True
        except Exception as e:
            self.log.info("ping failed for %s: %s", self.endpoint, e)
            return False
        finally:
            self._mark_call_end()


def values_block(uris: Iterable[str]) -> str:
    return " ".join(f"<{u}>" for u in uris)


# ---------------------------------------------------------------------------
# Discovery: which endpoints have data for a bestuur, and which besluiten
# ---------------------------------------------------------------------------

DISCOVERY_PREFIXES = """
PREFIX besluit: <http://data.vlaanderen.be/ns/besluit#>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX dct: <http://purl.org/dc/terms/>
PREFIX mu: <http://mu.semte.ch/vocabularies/core/>
"""

DISCOVERY_QUERY = """
SELECT DISTINCT ?besluit ?uuid WHERE {
  VALUES ?boInTijd { {organs} }
  ?besluit a besluit:Besluit .
  ?zitting besluit:isGehoudenDoor ?boInTijd .
  ?zitting besluit:behandelt ?agendapunt .
  ?behandeling dct:subject ?agendapunt .
  ?besluit prov:wasGeneratedBy ?behandeling .
  OPTIONAL { ?besluit mu:uuid ?uuid }
}
"""

ORGANS_IN_TIJD_BY_BESTUUR_QUERY = """
PREFIX besluit: <http://data.vlaanderen.be/ns/besluit#>
PREFIX mandaat: <http://data.vlaanderen.be/ns/mandaat#>
SELECT DISTINCT ?boInTijd WHERE {
  ?boInTijd a besluit:Bestuursorgaan ;
            mandaat:isTijdspecialisatieVan ?bo .
  ?bo besluit:bestuurt <{bestuur}> .
}
"""

UUID_BY_BESTUUR_QUERY = """
PREFIX mu: <http://mu.semte.ch/vocabularies/core/>
SELECT ?uuid WHERE { <{bestuur}> mu:uuid ?uuid } LIMIT 1
"""

LABEL_BY_BESTUUR_QUERY = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?label ?lang WHERE {
  <{bestuur}> skos:prefLabel ?label .
  BIND(LANG(?label) AS ?lang)
}
"""

@dataclass
class EndpointHit:
    endpoint: str
    besluiten: dict[str, str | None]


def connect_endpoints(endpoints: list[str], logger: logging.Logger) -> dict[str, SparqlClient]:
    clients: dict[str, SparqlClient] = {}
    for ep in endpoints:
        client = SparqlClient(ep, logger=logger, delay=HARVESTER_DELAY)
        if not client.ping():
            logger.info("endpoint %s not reachable, skipping", ep)
            continue
        clients[ep] = client
    return clients


def discover_besluiten_by_organs(client: SparqlClient, organs: list[str]) -> dict[str, str | None]:
    if not organs:
        return {}
    found: dict[str, str | None] = {}
    template = DISCOVERY_PREFIXES + DISCOVERY_QUERY
    for organ in organs:
        q = template.replace("{organs}", f"<{organ}>")
        client.log.debug("discover on %s for organ %s", client.endpoint, organ)
        try:
            rows = client.select(q)
        except RuntimeError as e:
            client.log.info("discover failed on %s for %s: %s", client.endpoint, organ, e)
            continue
        for row in rows:
            uri = row.get("besluit")
            if uri:
                found.setdefault(uri, row.get("uuid"))
    return found


def organs_in_tijd_for_bestuur(client: SparqlClient, bestuur_uri: str) -> list[str]:
    try:
        rows = client.select(
            ORGANS_IN_TIJD_BY_BESTUUR_QUERY.replace("{bestuur}", bestuur_uri)
        )
    except RuntimeError as e:
        client.log.info("organs_in_tijd_for_bestuur failed on %s: %s", client.endpoint, e)
        return []
    return [r["boInTijd"] for r in rows if r.get("boInTijd")]


def uuid_for_bestuur(client: SparqlClient, bestuur_uri: str) -> str | None:
    try:
        rows = client.select(UUID_BY_BESTUUR_QUERY.replace("{bestuur}", bestuur_uri))
    except RuntimeError as e:
        client.log.info("uuid_for_bestuur failed on %s: %s", client.endpoint, e)
        return None
    for row in rows:
        if row.get("uuid"):
            return row["uuid"]
    return None


def find_bestuur_uuid(
    registry: SparqlClient,
    clients: dict[str, SparqlClient],
    bestuur_uri: str,
    logger: logging.Logger,
) -> str | None:
    """Look up mu:uuid on the registry. Fall back to the other endpoints."""
    uuid = uuid_for_bestuur(registry, bestuur_uri)
    if uuid:
        return uuid
    for endpoint, client in clients.items():
        if endpoint == registry.endpoint:
            continue
        uuid = uuid_for_bestuur(client, bestuur_uri)
        if uuid:
            logger.info("mu:uuid for %s found on %s, not on the registry", bestuur_uri, endpoint)
            return uuid
    return None


def label_for_bestuur(client: SparqlClient, bestuur_uri: str) -> str | None:
    try:
        rows = client.select_bindings(LABEL_BY_BESTUUR_QUERY.replace("{bestuur}", bestuur_uri))
    except RuntimeError as e:
        client.log.info("label_for_bestuur failed on %s: %s", client.endpoint, e)
        return None
    labels = []
    for row in rows:
        value = row.get("label", {}).get("value")
        if value:
            labels.append((row.get("lang", {}).get("value", ""), value))
    for lang, value in labels:
        if lang == "nl":
            return value
    for lang, value in labels:
        if not lang:
            return value
    return labels[0][1] if labels else None


def find_bestuur_label(
    registry: SparqlClient,
    clients: dict[str, SparqlClient],
    bestuur_uri: str,
    logger: logging.Logger,
) -> str | None:
    """Look up skos:prefLabel on the registry. Fall back to the other endpoints."""
    label = label_for_bestuur(registry, bestuur_uri)
    if label:
        return label
    for endpoint, client in clients.items():
        if endpoint == registry.endpoint:
            continue
        label = label_for_bestuur(client, bestuur_uri)
        if label:
            logger.info("skos:prefLabel for %s found on %s, not on the registry", bestuur_uri, endpoint)
            return label
    return None


def collect_organs_in_tijd(
    registry: SparqlClient,
    bestuur_uri: str,
    logger: logging.Logger,
) -> list[str]:
    """Collect the bestuursorganen in tijd of a bestuur from the registry."""
    organs_in_tijd = organs_in_tijd_for_bestuur(registry, bestuur_uri)
    logger.info(
        "registry %s knows %d bestuursorgaan(en) in tijd for %s",
        registry.endpoint, len(organs_in_tijd), bestuur_uri,
    )
    return sorted(organs_in_tijd)


def probe_endpoints(
    clients: dict[str, SparqlClient],
    organs_in_tijd: list[str],
    bestuur_uri: str,
    logger: logging.Logger,
) -> EndpointHit | None:
    if not organs_in_tijd:
        logger.warning("no bestuursorgaan in tijd found for %s on the registry", bestuur_uri)
        return None
    logger.info(
        "processing %d bestuursorgaan(en) in tijd one by one for %s",
        len(organs_in_tijd), bestuur_uri,
    )
    for ep, client in clients.items():
        besluiten = discover_besluiten_by_organs(client, organs_in_tijd)
        logger.info("endpoint %s returned %d besluiten for %s", ep, len(besluiten), bestuur_uri)
        if besluiten:
            logger.info(
                "using %s for %s; ignoring the other harvesters", ep, bestuur_uri
            )
            return EndpointHit(endpoint=ep, besluiten=besluiten)
    return None


# ---------------------------------------------------------------------------
# Per-besluit fetch (fixed path)
# ---------------------------------------------------------------------------

def binding_to_node(term: dict):
    """Convert a SPARQL JSON result term to an rdflib node."""
    t = term.get("type")
    val = term.get("value", "")
    if t in ("uri", "bnode"):
        if t == "bnode":
            return BNode(val)
        return URIRef(val)
    # literal or typed-literal
    lang = term.get("xml:lang")
    dtype = term.get("datatype")
    if lang:
        return Literal(val, lang=lang)
    if dtype:
        return Literal(val, datatype=URIRef(dtype))
    return Literal(val)


def select_triples(clients: list[SparqlClient], query: str) -> Graph:
    """Run a SELECT that projects ?s ?p ?o on every client and merge the graphs."""
    g = Graph()
    for client in clients:
        try:
            bindings = client.select_bindings(query)
        except RuntimeError as e:
            client.log.warning("query failed on %s: %s", client.endpoint, e)
            continue
        for b in bindings:
            s = binding_to_node(b["s"])
            p = binding_to_node(b["p"])
            o = binding_to_node(b["o"])
            if s is None or p is None or o is None:
                continue
            g.add((s, p, o))
    return g


def describe_subjects(
    clients: list[SparqlClient],
    uris: list[str],
    blacklist: set[str] | None = None,
    batch: int = 40,
) -> Graph:
    """All triples whose subject is one of `uris`, minus the blacklisted predicates."""
    out = Graph()
    exclude = ", ".join(f"<{p}>" for p in sorted(blacklist or ()))
    filter_clause = f" FILTER(?p NOT IN ({exclude}))" if exclude else ""
    for i in range(0, len(uris), batch):
        chunk = uris[i : i + batch]
        q = (
            "SELECT ?s ?p ?o WHERE { VALUES ?s { " + values_block(chunk) + " } "
            "?s ?p ?o ." + filter_clause + " }"
        )
        out += select_triples(clients, q)
    return out


def forward_targets(graph: Graph, nodes: set[str], predicate: str) -> set[str]:
    """Objects of `predicate` on `nodes`, taken from the graph already fetched."""
    pred = URIRef(predicate)
    targets: set[str] = set()
    for node in nodes:
        for obj in graph.objects(URIRef(node), pred):
            if isinstance(obj, URIRef):
                targets.add(str(obj))
    return targets


def inverse_targets(
    clients: list[SparqlClient],
    nodes: set[str],
    predicate: str,
    batch: int = 40,
) -> set[str]:
    """Subjects that point at one of `nodes` with `predicate`."""
    targets: set[str] = set()
    ordered = sorted(nodes)
    for i in range(0, len(ordered), batch):
        chunk = ordered[i : i + batch]
        q = (
            "SELECT ?s WHERE { VALUES ?o { " + values_block(chunk) + " } "
            f"?s <{predicate}> ?o }}"
        )
        for client in clients:
            try:
                rows = client.select(q)
            except RuntimeError as e:
                client.log.warning("query failed on %s: %s", client.endpoint, e)
                continue
            for row in rows:
                if row.get("s"):
                    targets.add(row["s"])
    return targets


def fetch_path(
    clients: list[SparqlClient],
    besluit: str,
    triple_cap: int,
    blacklist: set[str],
    logger: logging.Logger,
) -> Graph:
    """Collect the resources on the fixed besluit path and all their triples.

    `clients` are the harvesters. Only the besluit, behandeling, artikel,
    agendapunt and zitting are described. Links to other resources stay in the
    graph as references but those resources are not fetched.
    """
    g = Graph()
    role_nodes: dict[str, set[str]] = {role: set() for role in PATH_ROLES}
    role_nodes["besluit"].add(besluit)
    described: set[str] = set()

    for role in PATH_ROLES:
        nodes = role_nodes[role]
        if not nodes:
            continue

        new = sorted(nodes - described)
        if new:
            g += describe_subjects(clients, new, blacklist)
        described.update(new)

        if len(g) > triple_cap:
            logger.warning(
                "triple cap (%d) reached for %s at %s; stopping",
                triple_cap, besluit, role,
            )
            break

        for step in PATH_STEPS:
            if step.source != role or step.predicate in blacklist:
                continue
            if step.inverse:
                targets = inverse_targets(clients, nodes, step.predicate)
            else:
                targets = forward_targets(g, nodes, step.predicate)
            if targets:
                role_nodes[step.target].update(targets)

    return g


# ---------------------------------------------------------------------------

def write_migration(out_dir: Path, base: str, graph_uri: str, graph: Graph, do_svg: bool, ttl2svg: str | None, logger: logging.Logger) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ttl_path = out_dir / f"{base}.ttl"
    graph_path = out_dir / f"{base}.graph"

    graph.serialize(destination=str(ttl_path), format="turtle", encoding="utf-8")
    graph_path.write_text(graph_uri + "\n", encoding="utf-8")
    logger.info("wrote %s and %s (%d triples)", ttl_path.name, graph_path.name, len(graph))

    if do_svg:
        svg_path = out_dir / f"{base}.svg"
        try:
            run_ttl2svg(ttl2svg, ttl_path, svg_path, logger)
        except Exception as e:
            logger.warning("svg generation failed for %s: %s", ttl_path.name, e)


def run_ttl2svg(ttl2svg: str | None, ttl_path: Path, svg_path: Path, logger: logging.Logger) -> None:
    """Draw a ttl file with ttl2svg: UML-like boxes with hover values and
    click-to-copy."""
    script = resolve_ttl2svg_script(ttl2svg, logger)
    cmd = [
        sys.executable, script, str(ttl_path), "-o", str(svg_path),
        "--uml", "--interactive",
    ]
    logger.debug("ttl2svg: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    gv_path = svg_path.with_suffix(".gv")
    if gv_path.exists():
        gv_path.unlink()


def resolve_ttl2svg_script(ttl2svg: str | None, logger: logging.Logger) -> str:
    candidates: list[Path] = []
    for value in (ttl2svg, os.environ.get("TTL2SVG_PATH")):
        if not value:
            continue
        path = Path(value).expanduser()
        candidates.append(path / "visualize_ttl.py" if path.is_dir() else path)
    candidates.extend([
        Path(__file__).resolve().parent / "vendor" / "ttl2svg" / "visualize_ttl.py",
        Path("/app/vendor/ttl2svg/visualize_ttl.py"),
        Path("ttl2svg") / "visualize_ttl.py",
        Path("../ttl2svg") / "visualize_ttl.py",
        ttl2svg_cache_dir() / "visualize_ttl.py",
    ])
    for c in candidates:
        if c.exists():
            return str(c)
    return str(clone_ttl2svg(logger))


def ttl2svg_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "besluiten-fetch" / "ttl2svg"


def clone_ttl2svg(logger: logging.Logger) -> Path:
    dest = ttl2svg_cache_dir()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    logger.info("ttl2svg not found, fetching %s into %s", TTL2SVG_REPO, dest)
    commands = [
        ["git", "init", "-q", str(dest)],
        ["git", "-C", str(dest), "remote", "add", "origin", TTL2SVG_REPO],
        ["git", "-C", str(dest), "fetch", "--depth", "1", "origin", TTL2SVG_REF],
        ["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"],
    ]
    try:
        for cmd in commands:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise RuntimeError("git is required to fetch ttl2svg. Install git or pass --ttl2svg PATH.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"could not fetch ttl2svg: {e.stderr or e}")
    script = dest / "visualize_ttl.py"
    if not script.exists():
        raise RuntimeError(f"ttl2svg checkout has no visualize_ttl.py: {dest}")
    return script


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

@dataclass
class RunReport:
    bestuur: str
    slug: str
    uuid: str
    uri: str
    endpoints_with_besluiten: list[str] = field(default_factory=list)
    besluit_count: int = 0
    written: int = 0
    skipped: int = 0


def default_config_path() -> Path:
    here = Path(__file__).resolve().parent / CONFIG_FILENAME
    return here if here.exists() else Path(CONFIG_FILENAME)


def load_config(path: Path) -> dict:
    """Read the registry, harvester endpoints, besturen and graph template."""
    if not path.exists():
        raise SystemExit(f"config file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"config file is not valid JSON: {path}: {e}")

    registry = str(data.get("registry", "")).strip()
    if not registry:
        raise SystemExit("config needs a 'registry' SPARQL endpoint")

    endpoints = [str(e).strip() for e in data.get("endpoints", []) if str(e).strip()]
    if not endpoints:
        raise SystemExit("config needs at least one entry in 'endpoints'")

    besturen: list[tuple[str, str]] = []
    for item in data.get("besturen", []):
        if isinstance(item, str):
            uri = item.strip()
            name = ""
        else:
            uri = str(item.get("uri", "")).strip()
            name = str(item.get("name", "")).strip()
        if not uri:
            raise SystemExit("every entry in 'besturen' needs a 'uri'")
        besturen.append((uri, name))
    if not besturen:
        raise SystemExit("config needs at least one entry in 'besturen'")

    graph_template = str(data.get("graph_template", GRAPH_TEMPLATE)).strip()
    if "{uuid}" not in graph_template:
        raise SystemExit("'graph_template' must contain {uuid}")

    blacklist: list[dict[str, str]] = []
    for item in data.get("predicate_blacklist", []):
        if isinstance(item, str):
            predicate = item.strip()
            comment = ""
        else:
            predicate = str(item.get("predicate", "")).strip()
            comment = str(item.get("comment", "")).strip()
        if not predicate:
            raise SystemExit("every entry in 'predicate_blacklist' needs a 'predicate'")
        blacklist.append({"predicate": predicate, "comment": comment})

    return {
        "registry": registry,
        "endpoints": endpoints,
        "besturen": besturen,
        "graph_template": graph_template,
        "predicate_blacklist": blacklist,
    }


def load_besluit_list(path: str) -> dict[str, list[str]]:
    """Load a JSON file mapping bestuur URI or prefLabel slug -> [besluit URI, ...]."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: list(v) for k, v in data.items()}


def setup_logging(verbose: bool, log_file: Path | None) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("fetch")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fetch besluiten per bestuur as mu migrations (graph+ttl+svg).")
    p.add_argument("--config", default=str(default_config_path()), help="JSON config file (default: config.json next to this script).")
    p.add_argument("--out", default="./out", help="Output root (default ./out). Data lands flat in <out>/data/.")
    p.add_argument("--endpoints", help="Comma-separated SPARQL endpoints. Overrides the config file.")
    p.add_argument("--besluit-list-file", help="JSON mapping bestuur URI or prefLabel slug -> [besluit URIs] to skip discovery.")
    p.add_argument("--limit", type=int, default=0, help="Cap besluiten per bestuur (0 = no cap).")
    p.add_argument("--triple-cap", type=int, default=200000, help="Stop fetching a besluit past this many triples.")
    p.add_argument("--dry-run", action="store_true", help="Only discover and list besluiten; write no ttl/svg.")
    p.add_argument("--no-svg", action="store_true", help="Skip the SVG drawing.")
    p.add_argument("--ttl2svg", help="Path to a ttl2svg clone or visualize_ttl.py. Default: vendor dir, then a local clone, then the cache.")
    p.add_argument("--only", help="Comma-separated bestuur URIs to process (exact match).")
    p.add_argument("--sparql-timeout", type=int, default=SPARQL_TIMEOUT, help="Per-request timeout in seconds.")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = p.parse_args(argv)

    config = load_config(Path(args.config))
    out_root = Path(args.out)
    log_file = out_root / "logs" / f"fetch-{now_ts()}.log"
    logger = setup_logging(args.verbose, log_file)

    logger.info("SHACL reference: %s", SHACL_URL)
    logger.info("Path: zitting -> agendapunt <- behandeling <-> besluit -> artikel")
    logger.info("Organen in tijd come from the registry for discovery only")
    registry = config["registry"]
    endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()] if args.endpoints else config["endpoints"]
    besturen = config["besturen"]
    graph_template = config["graph_template"]
    blacklist_entries = config["predicate_blacklist"]
    blacklist = {entry["predicate"] for entry in blacklist_entries}
    besluit_list_override = load_besluit_list(args.besluit_list_file) if args.besluit_list_file else {}

    only = {s.strip() for s in args.only.split(",")} if args.only else None

    logger.info("Registry: %s", registry)
    logger.info("Harvesters (%d): %s", len(endpoints), endpoints)
    logger.info("Besturen (%d)", len(besturen))
    logger.info("Predicate blacklist (%d):", len(blacklist))
    for entry in blacklist_entries:
        if entry["comment"]:
            logger.info("  %s: %s", entry["predicate"], entry["comment"])
        else:
            logger.info("  %s", entry["predicate"])
    logger.info("dry_run=%s limit=%s no_svg=%s", args.dry_run, args.limit, args.no_svg)

    registry_client = SparqlClient(registry, logger=logger)
    if not registry_client.ping():
        logger.error("registry not reachable: %s", registry)
        return 1
    harvester_clients = connect_endpoints(endpoints, logger)
    if not harvester_clients:
        logger.error("no reachable harvester endpoint")
        return 1
    clients = dict(harvester_clients)
    logger.info("live harvesters: %s", list(clients.keys()))

    reports: list[RunReport] = []
    data_dir = out_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    existing_uuids = existing_besluit_uuids(data_dir)

    for bestuur_uri, name in besturen:
        if only and bestuur_uri not in only:
            logger.info("skipping %s due to --only", bestuur_uri)
            continue
        logger.info("=== bestuur: %s (%s) ===", name or bestuur_uri, bestuur_uri)
        uuid = find_bestuur_uuid(registry_client, clients, bestuur_uri, logger)
        if not uuid:
            logger.error("no mu:uuid found for %s; cannot build the graph URI", bestuur_uri)
            reports.append(RunReport(bestuur=name or bestuur_uri, slug="", uuid="", uri=bestuur_uri))
            continue
        pref_label = find_bestuur_label(registry_client, clients, bestuur_uri, logger)
        if not pref_label:
            logger.error("no skos:prefLabel found for %s; cannot build the output folder", bestuur_uri)
            reports.append(RunReport(bestuur=name or bestuur_uri, slug="", uuid=uuid, uri=bestuur_uri))
            continue
        slug = slugify(pref_label)
        report = RunReport(bestuur=pref_label, slug=slug, uuid=uuid, uri=bestuur_uri)
        logger.info("mu:uuid: %s, label: %s", uuid, pref_label)

        organs_in_tijd = collect_organs_in_tijd(
            registry_client, bestuur_uri, logger
        )

        override = besluit_list_override.get(bestuur_uri) or besluit_list_override.get(slug)
        fetch_clients = list(clients.values())
        if override is not None:
            besluit_to_endpoints = {b: ("manual", None) for b in override}
            logger.info("using manual besluit list for %s (%d besluiten)", report.bestuur, len(override))
        else:
            hit = probe_endpoints(clients, organs_in_tijd, bestuur_uri, logger)
            if hit is None:
                logger.warning("No besluiten found for %s on any endpoint.", report.bestuur)
                reports.append(report)
                continue
            report.endpoints_with_besluiten.append(hit.endpoint)
            fetch_clients = [clients[hit.endpoint]]
            besluit_to_endpoints = {
                b: (hit.endpoint, besluit_uuid)
                for b, besluit_uuid in hit.besluiten.items()
            }

        besluiten = list(besluit_to_endpoints.keys())
        report.besluit_count = len(besluiten)
        logger.info("total unique besluiten for %s: %d", report.bestuur, len(besluiten))

        # write a global index (also in dry-run)
        index_path = data_dir / f"{slug}-besluiten-index.json"
        index_payload = {
            "bestuur": report.bestuur,
            "bestuur_uuid": uuid,
            "bestuur_uri": bestuur_uri,
            "graph": graph_template.format(uuid=uuid),
            "endpoints": report.endpoints_with_besluiten,
            "besluit_count": len(besluiten),
            "besluiten": [
                {
                    "besluit": b,
                    "endpoint": besluit_to_endpoints[b][0],
                    "uuid": besluit_to_endpoints[b][1] or uuid_from_uri(b),
                }
                for b in besluiten
            ],
        }
        index_path.write_text(json.dumps(index_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("wrote index %s", index_path)

        if args.dry_run:
            if args.limit:
                logger.info("[dry-run] would fetch up to %d of %d besluiten", args.limit, len(besluiten))
            else:
                logger.info("[dry-run] would fetch all %d besluiten", len(besluiten))
            reports.append(report)
            continue

        chosen = besluiten[: args.limit] if args.limit else besluiten
        ts = now_ts()
        for i, besluit in enumerate(chosen, 1):
            besluit_uuid = besluit_to_endpoints[besluit][1]
            candidates = {
                value.lower()
                for value in (besluit_uuid, uuid_from_uri(besluit))
                if value
            }
            if candidates & existing_uuids:
                logger.info("[%d/%d] skip %s, already downloaded", i, len(chosen), besluit)
                report.skipped += 1
                continue
            logger.info("[%d/%d] fetching %s", i, len(chosen), besluit)
            try:
                sub = fetch_path(
                    fetch_clients, besluit, args.triple_cap, blacklist, logger,
                )
            except Exception as e:
                logger.error("failed to fetch path for %s: %s", besluit, e)
                continue
            if len(sub) == 0:
                logger.error("no triples found for %s on any endpoint", besluit)
                continue
            file_uuid = (
                besluit_uuid_from_graph(sub, besluit)
                or besluit_uuid
                or uuid_from_uri(besluit)
                or uri_to_safe_name(besluit)
            )
            if file_uuid.lower() in existing_uuids:
                logger.info("[%d/%d] skip %s, already downloaded", i, len(chosen), besluit)
                report.skipped += 1
                continue
            base = f"{ts}-{slug}-{file_uuid}"
            write_migration(
                data_dir, base, graph_template.format(uuid=uuid), sub,
                do_svg=not args.no_svg, ttl2svg=args.ttl2svg, logger=logger,
            )
            existing_uuids.add(file_uuid.lower())
            report.written += 1

        reports.append(report)

    # summary
    logger.info("==== summary ====")
    for r in reports:
        logger.info(
            "%s: besluiten=%d written=%d skipped=%d endpoints=%s",
            r.bestuur, r.besluit_count, r.written, r.skipped, r.endpoints_with_besluiten,
        )
    summary_path = out_root / "logs" / f"summary-{now_ts()}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            [
                {
                    "bestuur": r.bestuur,
                    "uri": r.uri,
                    "slug": r.slug,
                    "uuid": r.uuid,
                    "besluit_count": r.besluit_count,
                    "written": r.written,
                    "skipped": r.skipped,
                    "endpoints": r.endpoints_with_besluiten,
                }
                for r in reports
            ],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
