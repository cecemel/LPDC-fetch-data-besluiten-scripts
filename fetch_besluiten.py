#!/usr/bin/env python3
"""Fetch besluiten (decisions) for a set of bestuurseenheden and write one
mu-semtech migration per besluit (a .graph + .ttl pair) plus an SVG drawing.

Data comes from SPARQL endpoints with Lokaal Beslist or Centrale Vindplaats
data that follows the besluit-publicatie model. The script walks the path of
the besluit as described by the erkendestandaard 2025-12-11 SHACL shape:

  https://data.vlaanderen.be/doc/applicatieprofiel/besluit-publicatie/erkendestandaard/2025-12-11/shacl/besluit-publicatie-SHACL.ttl

The constraints are interpreted liberally: for every besluit we collect the
besluit itself and the resources reachable along the SHACL paths (behandeling,
agendapunt, zitting, stemming, mandataris, rechtsgrond, rechtsgrondonderdeel,
bestuursorgaan, bestuurseenheid, documents), and we keep all their triples.

Configuration lives in config.json: the endpoints to query and the
bestuurseenheden to fetch. Run `python3 fetch_besluiten.py --help` for the
options. See README.md for the Docker workflow.
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

# Predicates we follow backwards (object -> subject) to discover related
# resources that point at a seed. Kept intentionally tiny: broad inverse
# predicates such as eli:has_part or dct:isPartOf pull in every sibling that
# shares a part/parent (one bad artikel URI dragged in 138 besluiten), so we
# only follow the one reverse link we actually need to reconstruct the path:
# the zitting that behandelt an agendapunt.
INVERSE_PREDICATES = [
    "http://data.vlaanderen.be/ns/besluit#behandelt",  # zitting -> agendapunt
]

# Predicates whose objects we do NOT expand into new seeds, even when they are
# in an allowed namespace. These are ordering/cross-besluit links that would
# drag sibling besluiten/behandelingen into a migration that should describe a
# single besluit. The triples themselves are still kept (they are fetched as
# part of describing a resource); only further expansion is blocked.
SKIP_EXPAND_PREDICATES = {
    "http://data.vlaanderen.be/ns/besluit#gebeurtNa",        # behandeling -> previous behandeling
    "http://data.vlaanderen.be/ns/besluit#aangebrachtNa",    # agendapunt -> previous agendapunt
}

# Namespace prefixes whose resources we expand (describe) while walking. URIs
# outside this set are referenced but not expanded, so we do not drag in entire
# external corpora.
DEFAULT_ALLOW_NS = [
    "http://data.lblod.info/",
    "https://data.lblod.info/",
    "http://data.vlaanderen.be/",
    "https://data.vlaanderen.be/",
    "http://mu.semte.ch/",
]

# Even when an allow-list namespace matches, never expand these: they are
# ontology / vocabulary / authority namespaces whose description is schema, not
# instance data, and would add rdfs:Class / owl:Class noise to every migration.
# The rdf:type link itself is still kept (it is fetched with the instance).
EXPAND_EXCLUDE_NS = [
    "http://data.vlaanderen.be/ns/",
    "https://data.vlaanderen.be/ns/",
    "http://mu.semte.ch/vocabularies/",
    "http://data.lblod.info/vocabularies/",
    "http://lblod.data.gift/vocabularies/",
    "http://www.w3.org/",
    "http://schema.org/",
    "https://schema.org/",
    "http://purl.org/",
    "http://publications.europa.eu/resource/authority/",
    "http://data.europa.eu/eli/ontology#",  # the ELI ontology itself, not instances
]

SPARQL_TIMEOUT = 120  # seconds per request
PROBE_TIMEOUT = 20   # seconds for the liveness ping
HTTP_RETRIES = 3
RETRY_BACKOFF = 5  # seconds


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


def host_of(uri: str) -> str:
    try:
        parsed = urllib.parse.urlparse(uri)
        return f"{parsed.scheme}://{parsed.netloc}/"
    except Exception:
        return ""


def allowed_uri(uri: str, allow_ns: list[str], extra_hosts: set[str]) -> bool:
    if any(uri.startswith(ns) for ns in EXPAND_EXCLUDE_NS):
        return False
    if any(uri.startswith(ns) for ns in allow_ns):
        return True
    h = host_of(uri)
    return h in extra_hosts


# ---------------------------------------------------------------------------
# SPARQL client
# ---------------------------------------------------------------------------

class SparqlClient:
    def __init__(self, endpoint: str, timeout: int = SPARQL_TIMEOUT, logger: logging.Logger | None = None):
        self.endpoint = endpoint
        self.timeout = timeout
        self.log = logger or logging.getLogger("sparql")

    def _request(self, query: str, accept: str):
        # POST the query in the body to avoid URL-length limits (HTTP 414/431).
        data = urllib.parse.urlencode({"query": query}).encode("utf-8")
        last_err = None
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


def values_block(uris: Iterable[str]) -> str:
    return " ".join(f"<{u}>" for u in uris)


# ---------------------------------------------------------------------------
# Discovery: which endpoints have data for a bestuur, and which besluiten
# ---------------------------------------------------------------------------

DISCOVER_BY_ORGANS_QUERY = """
PREFIX besluit: <http://data.vlaanderen.be/ns/besluit#>
PREFIX mandaat: <http://data.vlaanderen.be/ns/mandaat#>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX dct: <http://purl.org/dc/terms/>
PREFIX eli: <http://data.europa.eu/eli/ontology#>
SELECT DISTINCT ?besluit WHERE {
  VALUES ?bo { {organs} }
  ?besluit a besluit:Besluit .
  {
    ?besluit eli:passed_by ?bo .
  }
  UNION
  {
    ?boInTijd mandaat:isTijdspecialisatieVan ?bo .
    ?besluit eli:passed_by ?boInTijd .
  }
  UNION
  {
    ?lr eli:passed_by ?bo .
    ?besluit eli:realizes ?lr .
  }
  UNION
  {
    ?boInTijd mandaat:isTijdspecialisatieVan ?bo .
    ?lr eli:passed_by ?boInTijd .
    ?besluit eli:realizes ?lr .
  }
  UNION
  {
    ?zitting besluit:isGehoudenDoor ?bo .
    ?zitting besluit:behandelt ?agendapunt .
    ?behandeling dct:subject ?agendapunt .
    ?besluit prov:wasGeneratedBy ?behandeling .
  }
  UNION
  {
    ?boInTijd mandaat:isTijdspecialisatieVan ?bo .
    ?zitting besluit:isGehoudenDoor ?boInTijd .
    ?zitting besluit:behandelt ?agendapunt .
    ?behandeling dct:subject ?agendapunt .
    ?besluit prov:wasGeneratedBy ?behandeling .
  }
  UNION
  {
    ?besluit prov:wasGeneratedBy ?behandeling .
    ?behandeling prov:wasAssociatedWith ?bo .
  }
}
"""

ORGS_BY_BESTUUR_QUERY = """
PREFIX besluit: <http://data.vlaanderen.be/ns/besluit#>
SELECT DISTINCT ?bo WHERE { ?bo besluit:bestuurt <{bestuur}> }
"""

ORGS_BY_LABEL_QUERY = """
PREFIX besluit: <http://data.vlaanderen.be/ns/besluit#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT DISTINCT ?bo WHERE {
  ?bo a besluit:Bestuursorgaan ;
      skos:prefLabel ?label .
  FILTER(LANG(?label) = "" || LANG(?label) = "nl")
  FILTER(CONTAINS(LCASE(STR(?label)), LCASE("{name}")))
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
    besluiten: list[str]


def connect_endpoints(endpoints: list[str], logger: logging.Logger) -> dict[str, SparqlClient]:
    clients: dict[str, SparqlClient] = {}
    for ep in endpoints:
        client = SparqlClient(ep, logger=logger)
        if not client.ping():
            logger.info("endpoint %s not reachable, skipping", ep)
            continue
        clients[ep] = client
    return clients


def discover_besluiten_by_organs(client: SparqlClient, organs: list[str]) -> list[str]:
    if not organs:
        return []
    q = DISCOVER_BY_ORGANS_QUERY.replace("{organs}", values_block(organs))
    try:
        rows = client.select(q)
    except RuntimeError as e:
        client.log.info("discover (by organs) failed on %s: %s", client.endpoint, e)
        return []
    return [r["besluit"] for r in rows if r.get("besluit")]


def organs_for_bestuur(client: SparqlClient, bestuur_uri: str) -> list[str]:
    try:
        rows = client.select(ORGS_BY_BESTUUR_QUERY.replace("{bestuur}", bestuur_uri))
    except RuntimeError as e:
        client.log.info("organs_for_bestuur failed on %s: %s", client.endpoint, e)
        return []
    return [r["bo"] for r in rows if r.get("bo")]


def organs_by_label(client: SparqlClient, name: str) -> list[str]:
    # Escape quotes in the name; this is interpolated into a string literal.
    safe = name.replace('"', "")
    q = ORGS_BY_LABEL_QUERY.replace("{name}", safe)
    try:
        rows = client.select(q)
    except RuntimeError as e:
        client.log.info("organs_by_label failed on %s: %s", client.endpoint, e)
        return []
    return [r["bo"] for r in rows if r.get("bo")]


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


def collect_base_organs(
    clients: dict[str, SparqlClient], bestuur_uri: str, name: str, logger: logging.Logger
) -> list[str]:
    """Collect the base bestuursorganen for a bestuur from every endpoint.

    The `bestuurt` link lives on the registry, while the zitting and the
    bestuursorgaan in tijd live on the harvesters. So we gather the base organs
    from all endpoints, then use them everywhere.
    """
    organs: set[str] = set()
    for ep, client in clients.items():
        found = organs_for_bestuur(client, bestuur_uri)
        logger.info("endpoint %s knows %d bestuursorgaan(en) for %s", ep, len(found), bestuur_uri)
        organs.update(found)
    if not organs and name:
        for ep, client in clients.items():
            found = organs_by_label(client, name)
            if found:
                logger.info("label fallback found %d orgaan(en) for %s on %s", len(found), name, ep)
                organs.update(found)
    return sorted(organs)


def probe_endpoints(
    clients: dict[str, SparqlClient], bestuur_uri: str, name: str, logger: logging.Logger
) -> list[EndpointHit]:
    organs = collect_base_organs(clients, bestuur_uri, name, logger)
    label = name or bestuur_uri
    if not organs:
        logger.warning("no bestuursorgaan found for %s on any endpoint", label)
        return []
    logger.info("using %d base bestuursorgaan(en) for %s", len(organs), label)
    hits: list[EndpointHit] = []
    for ep, client in clients.items():
        besluiten = discover_besluiten_by_organs(client, organs)
        logger.info("endpoint %s returned %d besluiten for %s", ep, len(besluiten), bestuur_uri)
        if besluiten:
            hits.append(EndpointHit(endpoint=ep, besluiten=besluiten))
    return hits


# ---------------------------------------------------------------------------
# Per-besluit subgraph fetch (walk the SHACL path, liberal)
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
    exclude = " ".join(f"<{p}>" for p in sorted(blacklist or ()))
    filter_clause = f" FILTER(?p NOT IN ({exclude}))" if exclude else ""
    for i in range(0, len(uris), batch):
        chunk = uris[i : i + batch]
        q = (
            "SELECT ?s ?p ?o WHERE { VALUES ?s { " + values_block(chunk) + " } "
            "?s ?p ?o ." + filter_clause + " }"
        )
        out += select_triples(clients, q)
    return out


def inverse_links(
    clients: list[SparqlClient],
    uris: list[str],
    blacklist: set[str] | None = None,
    batch: int = 40,
) -> Graph:
    """Triples where one of `uris` is the object, for INVERSE_PREDICATES only."""
    out = Graph()
    predicates = [p for p in INVERSE_PREDICATES if p not in (blacklist or set())]
    if not predicates:
        return out
    preds = " ".join(f"<{p}>" for p in predicates)
    for i in range(0, len(uris), batch):
        chunk = uris[i : i + batch]
        q = (
            "SELECT ?s ?p ?o WHERE { "
            "VALUES ?p { " + preds + " } "
            "VALUES ?o { " + values_block(chunk) + " } "
            "?s ?p ?o }"
        )
        out += select_triples(clients, q)
    return out


def fetch_subgraph(
    clients: list[SparqlClient],
    besluit: str,
    max_depth: int,
    allow_ns: list[str],
    triple_cap: int,
    blacklist: set[str],
    logger: logging.Logger,
) -> Graph:
    """Walk outward from the besluit up to `max_depth` hops, keeping all triples
    of the resources along the way. Queries go to every client and the results
    are merged, so links that live on different endpoints (for example
    `bestuurt` on the registry and the zitting on a harvester) end up in the
    same migration."""
    g = Graph()
    extra_hosts = {host_of(besluit)}
    seeds: set[str] = {besluit}
    visited: set[str] = set()

    for depth in range(max_depth + 1):
        new = [u for u in seeds if u not in visited]
        if not new:
            break

        forward = describe_subjects(clients, new, blacklist)
        g += forward

        back = inverse_links(clients, new, blacklist)
        g += back

        if len(g) > triple_cap:
            logger.warning(
                "triple cap (%d) reached for %s at depth %d; stopping expansion",
                triple_cap, besluit, depth,
            )
            break

        # collect candidate URIs to expand next: objects of forward + subjects of
        # inverse. Skip objects reached via SKIP_EXPAND_PREDICATES so ordering
        # links do not pull sibling besluiten into this migration.
        candidates: set[str] = set()
        for s, p, o in forward:
            if isinstance(o, URIRef) and str(p) not in SKIP_EXPAND_PREDICATES:
                candidates.add(str(o))
        for s, p, o in back:
            if isinstance(s, URIRef):
                candidates.add(str(s))
            if isinstance(o, URIRef) and str(p) not in SKIP_EXPAND_PREDICATES:
                candidates.add(str(o))

        candidates = {
            u for u in candidates
            if allowed_uri(u, allow_ns, extra_hosts) and u not in visited
        }

        visited.update(new)
        seeds = candidates

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
    p.add_argument("--out", default="./out", help="Output root (default ./out). Data lands in <out>/data/<slug>/.")
    p.add_argument("--endpoints", help="Comma-separated SPARQL endpoints. Overrides the config file.")
    p.add_argument("--besluit-list-file", help="JSON mapping bestuur URI or prefLabel slug -> [besluit URIs] to skip discovery.")
    p.add_argument("--limit", type=int, default=0, help="Cap besluiten per bestuur (0 = no cap).")
    p.add_argument("--max-depth", type=int, default=3, help="Expansion hops from each besluit (default 3, reaches the zitting).")
    p.add_argument("--triple-cap", type=int, default=200000, help="Stop expanding a besluit subgraph past this many triples.")
    p.add_argument("--dry-run", action="store_true", help="Only discover and list besluiten; write no ttl/svg.")
    p.add_argument("--no-svg", action="store_true", help="Skip the SVG drawing.")
    p.add_argument("--ttl2svg", help="Path to a ttl2svg clone or visualize_ttl.py. Default: vendor dir, then a local clone, then the cache.")
    p.add_argument("--allow-ns", help="Comma-separated extra namespace prefixes to expand.")
    p.add_argument("--only", help="Comma-separated bestuur URIs to process (exact match).")
    p.add_argument("--sparql-timeout", type=int, default=SPARQL_TIMEOUT, help="Per-request timeout in seconds.")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = p.parse_args(argv)

    config = load_config(Path(args.config))
    out_root = Path(args.out)
    log_file = out_root / "logs" / f"fetch-{now_ts()}.log"
    logger = setup_logging(args.verbose, log_file)

    logger.info("SHACL reference: %s", SHACL_URL)
    registry = config["registry"]
    endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()] if args.endpoints else config["endpoints"]
    besturen = config["besturen"]
    graph_template = config["graph_template"]
    blacklist_entries = config["predicate_blacklist"]
    blacklist = {entry["predicate"] for entry in blacklist_entries}
    besluit_list_override = load_besluit_list(args.besluit_list_file) if args.besluit_list_file else {}
    allow_ns = list(DEFAULT_ALLOW_NS)
    if args.allow_ns:
        allow_ns += [n.strip() for n in args.allow_ns.split(",") if n.strip()]

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
    logger.info("dry_run=%s limit=%s max_depth=%s no_svg=%s", args.dry_run, args.limit, args.max_depth, args.no_svg)

    registry_client = SparqlClient(registry, logger=logger)
    if not registry_client.ping():
        logger.error("registry not reachable: %s", registry)
        return 1
    harvester_clients = connect_endpoints(endpoints, logger)
    if not harvester_clients:
        logger.error("no reachable harvester endpoint")
        return 1
    clients = {registry: registry_client}
    clients.update(harvester_clients)
    logger.info("live endpoints: %s", list(clients.keys()))

    reports: list[RunReport] = []

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

        override = besluit_list_override.get(bestuur_uri) or besluit_list_override.get(slug)
        if override is not None:
            besluit_to_endpoints = {b: "manual" for b in override}
            logger.info("using manual besluit list for %s (%d besluiten)", report.bestuur, len(override))
        else:
            hits = probe_endpoints(clients, bestuur_uri, pref_label, logger)
            if not hits:
                logger.warning("No besluiten found for %s on any endpoint.", report.bestuur)
                reports.append(report)
                continue
            besluit_to_endpoints = {}
            for hit in hits:
                report.endpoints_with_besluiten.append(hit.endpoint)
                for b in hit.besluiten:
                    besluit_to_endpoints.setdefault(b, hit.endpoint)

        besluiten = list(besluit_to_endpoints.keys())
        report.besluit_count = len(besluiten)
        logger.info("total unique besluiten for %s: %d", report.bestuur, len(besluiten))

        # write a global index (also in dry-run)
        data_dir = out_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        index_path = data_dir / f"{slug}__besluiten-index.json"
        index_payload = {
            "bestuur": report.bestuur,
            "bestuur_uuid": uuid,
            "bestuur_uri": bestuur_uri,
            "graph": graph_template.format(uuid=uuid),
            "endpoints": report.endpoints_with_besluiten,
            "besluit_count": len(besluiten),
            "besluiten": [
                {"besluit": b, "endpoint": besluit_to_endpoints[b]} for b in besluiten
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
        live_clients = list(clients.values())
        for i, besluit in enumerate(chosen, 1):
            logger.info("[%d/%d] fetching %s", i, len(chosen), besluit)
            try:
                sub = fetch_subgraph(
                    live_clients, besluit, args.max_depth, allow_ns,
                    args.triple_cap, blacklist, logger,
                )
            except Exception as e:
                logger.error("failed to fetch subgraph for %s: %s", besluit, e)
                continue
            if len(sub) == 0:
                logger.error("no triples found for %s on any endpoint", besluit)
                continue
            base = f"{slug}__{ts}-{uri_to_safe_name(besluit)}"
            write_migration(
                data_dir, base, graph_template.format(uuid=uuid), sub,
                do_svg=not args.no_svg, ttl2svg=args.ttl2svg, logger=logger,
            )
            report.written += 1

        reports.append(report)

    # summary
    logger.info("==== summary ====")
    for r in reports:
        logger.info(
            "%s: besluiten=%d written=%d endpoints=%s",
            r.bestuur, r.besluit_count, r.written, r.endpoints_with_besluiten,
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
