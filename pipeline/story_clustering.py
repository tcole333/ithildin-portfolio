#!/usr/bin/env python3
"""Identify and prepare article topic clusters from investigation.db.

Outputs cluster definitions with all relevant findings/connections/evidence
that a writing agent will use to generate articles.
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "investigation.db"
OUTPUT_DIR = Path(__file__).parent.parent / "content"

# Evidence reference classification patterns — order matters (first match wins)
SOURCE_TYPE_PATTERNS = [
    ("efta", re.compile(r"^EFTA\d", re.IGNORECASE)),
    ("house_oversight", re.compile(r"^HOUSE_OVERSIGHT", re.IGNORECASE)),
    ("acris", re.compile(r"^ACRIS", re.IGNORECASE)),
    ("fec", re.compile(r"^FEC", re.IGNORECASE)),
    ("littlesis", re.compile(r"^LittleSis", re.IGNORECASE)),
    ("usvi_registry", re.compile(r"^USVI", re.IGNORECASE)),
    ("courtlistener", re.compile(r"^(?:CourtListener|CL:)", re.IGNORECASE)),
    ("fara", re.compile(r"^FARA", re.IGNORECASE)),
    ("lda", re.compile(r"^LDA", re.IGNORECASE)),
    ("sec_edgar", re.compile(r"^(?:SEC|EDGAR)", re.IGNORECASE)),
    ("irs_990", re.compile(r"^(?:990|IRS.?990|ProPublica|PP990|PROPUBLICA)", re.IGNORECASE)),
    ("ds10", re.compile(r"^DS10", re.IGNORECASE)),
    ("lmsband", re.compile(r"^LMSBAND", re.IGNORECASE)),
    ("doj_vol11", re.compile(r"^DOJ.?(?:Vol|11)", re.IGNORECASE)),
    ("duggan", re.compile(r"^DugganUSA", re.IGNORECASE)),
    ("unified", re.compile(r"^Unified", re.IGNORECASE)),
    ("gleif", re.compile(r"^GLEIF", re.IGNORECASE)),
    ("opensanctions", re.compile(r"^OpenSanctions", re.IGNORECASE)),
    ("icij", re.compile(r"^ICIJ", re.IGNORECASE)),
    ("occrp", re.compile(r"^OCCRP", re.IGNORECASE)),
    ("gdelt", re.compile(r"^GDELT", re.IGNORECASE)),
    ("faa", re.compile(r"^FAA", re.IGNORECASE)),
    ("ucc", re.compile(r"^UCC", re.IGNORECASE)),
    ("state_registry", re.compile(r"^(?:FL_SUNBIZ|FL.SunBiz|FL:|NY_DOS|NY.SoS|NY.DOS|NM.SoS|DC_|OC:|UK.Companies)", re.IGNORECASE)),
    ("muckrock", re.compile(r"^MUCKROCK", re.IGNORECASE)),
    ("documentcloud", re.compile(r"^DOCUMENTCLOUD", re.IGNORECASE)),
    ("offshorealert", re.compile(r"^OffshoreAlert", re.IGNORECASE)),
    ("url", re.compile(r"^https?://", re.IGNORECASE)),
]

# All searchable tool names in search_log
ALL_SEARCH_TOOLS = {
    "query_doj", "duggan_search", "query_lmsband", "query_unified",
    "query_epstein20k", "query_edgar", "query_990", "query_acris",
    "parse_ds10_financials", "query_fec", "query_fara", "query_lobbying",
    "query_courtlistener", "query_littlesis", "query_aleph",
    "query_icij", "query_opensanctions", "query_gdelt",
    "query_registry", "query_gleif", "query_faa",
}


def classify_evidence_ref(ref: str) -> str:
    """Classify an evidence_ref string into a source type."""
    if not ref:
        return "unknown"
    for source_type, pattern in SOURCE_TYPE_PATTERNS:
        if pattern.search(ref):
            return source_type
    return "other"

# Seed clusters identified from 11 waves of findings
CLUSTERS = [
    {
        "id": "apollo-money-pipeline",
        "title": "The Apollo Money Pipeline",
        "subtitle": "How $158M+ flowed from 3 billionaires to a convicted sex offender",
        "style_angle": "How do you move $40M to a felon through legitimate banking?",
        "targets": ["Leon Black", "Marc Rowan", "Joshua Harris", "Southern Trust Company",
                     "Apollo Global Management", "The 2017 Caterpillar Trust", "Ariane de Rothschild",
                     "Edmond de Rothschild Bank"],
        "keywords": ["apollo", "black", "rowan", "harris", "caterpillar trust", "STC",
                     "rothschild", "family office", "$158M", "$40M"],
    },
    {
        "id": "wexner-trust-architecture",
        "title": "Wexner Trust Architecture",
        "subtitle": "A masterclass in using trusts to obscure beneficial ownership",
        "targets": ["Leslie Wexner", "Les Wexner", "Financial Trust Company", "NWO LLC",
                     "Maple Inc", "L Brands"],
        "keywords": ["wexner", "FTC", "NWO", "maple", "L brands", "BBWI", "7.8M shares",
                     "9 e 71st"],
    },
    {
        "id": "deutsche-bank-plumbing",
        "title": "Deutsche Bank Plumbing",
        "subtitle": "What 579 transactions and $304M tell us about compliance theater",
        "targets": ["Deutsche Bank", "WE LLC", "Southern Trust Company", "Jeffrey Epstein"],
        "keywords": ["deutsche bank", "DS10", "SAR", "compliance", "WE LLC"],
    },
    {
        "id": "gulf-intelligence-web",
        "title": "The Gulf Intelligence Web",
        "subtitle": "The geopolitics of a financier's Rolodex",
        "targets": ["Jabor Al Thani", "Sultan Bin Sulayem", "Raafat Alsabbagh",
                     "Fettah Tamince", "Elliott Broidy"],
        "keywords": ["qatar", "saudi", "UAE", "al thani", "HBJ", "radical breakthrough",
                     "broidy", "nader"],
    },
    {
        "id": "shadow-lobbying-empire",
        "title": "Shadow Lobbying Empire",
        "subtitle": "How to lobby Congress without technically lobbying Congress",
        "targets": ["International Peace Institute", "IPI", "Humpty Dumpty Institute", "HDI",
                     "Terje Rod-Larsen"],
        "keywords": ["IPI", "HDI", "lobbying", "FARA", "congress", "shadow"],
    },
    {
        "id": "corporate-shell-network",
        "title": "The Corporate Shell Network",
        "subtitle": "The corporate structure diagram that takes a full wall",
        "targets": ["IGO Company LLC", "Southern Financial LLC", "Maple Inc",
                     "JEGE Inc", "Darren Indyke", "Richard Kahn"],
        "keywords": ["shell", "LLC", "corporate", "BBVI", "5-tier", "indyke"],
    },
    {
        "id": "legal-shield",
        "title": "The Legal Shield",
        "subtitle": "When your lawyers are also your intelligence service",
        "targets": ["Ken Starr", "Brad Karp", "Reid Weingarten", "Dechert LLP",
                     "Brad Edwards"],
        "keywords": ["starr", "karp", "paul weiss", "dechert", "privilege", "defense"],
    },
    {
        "id": "science-tech-interface",
        "title": "Science & Tech Interface",
        "subtitle": "Philanthropy as a social technology",
        "targets": ["Carbyne/Reporty", "Ian Osborne", "Peter Thiel", "Valar Ventures",
                     "Masha Drokova", "Enhanced Education"],
        "keywords": ["gates", "nikolic", "carbyne", "edge foundation", "science", "tech",
                     "osborne", "thiel", "hedosophia"],
    },
    {
        "id": "norwegian-connection",
        "title": "The Norwegian Connection",
        "subtitle": "An ex-diplomat, a defense minister, and a registered sex offender",
        "targets": ["Terje Rod-Larsen", "Ehud Barak", "Thorbjorn Jagland"],
        "keywords": ["norway", "norwegian", "rod-larsen", "barak", "jagland", "telenor"],
    },
    {
        "id": "inner-circle-operations",
        "title": "Inner Circle Operations",
        "subtitle": "The org chart of a criminal enterprise that filed its taxes",
        "targets": ["Darren Indyke", "Richard Kahn", "Lesley Groff", "Karyna Shuliak"],
        "keywords": ["indyke", "kahn", "groff", "galbraith", "shuliak", "inner circle"],
    },
    {
        "id": "usvi-operations",
        "title": "USVI Operations",
        "subtitle": "Why the US Virgin Islands is the Delaware of the Caribbean",
        "targets": ["Southern Trust Company", "Southern Financial LLC", "Maple Inc",
                     "Gratitude America", "Gratitude America Ltd"],
        "keywords": ["USVI", "virgin islands", "southern trust", "great st james",
                     "little st james", "gratitude"],
    },
    {
        "id": "thiel-network-architecture",
        "title": "The Thiel Network",
        "subtitle": "From Stanford Review to Pentagon — how one network colonized the defense-intelligence state",
        "style_angle": "The network's shape is determined by the variety of the system it's trying to capture",
        "targets": ["Peter Thiel", "Founders Fund", "Palantir Technologies", "Palantir",
                     "Trae Stephens", "Palmer Luckey", "Kenneth Howery", "David Sacks",
                     "Michael Kratsios", "JD Vance", "Blake Masters", "Joe Lonsdale",
                     "Keith Rabois", "Rockbridge Network", "Stanford Review",
                     "Anduril Industries", "Anduril", "Valar Ventures", "8VC"],
        "keywords": ["thiel", "paypal mafia", "founders fund", "stanford review",
                     "palantir", "anduril", "rockbridge", "thiel fellowship",
                     "valar ventures", "8VC", "lonsdale", "rabois", "nosek",
                     "clarium", "mithril", "thiel capital", "defense tech",
                     "government-in-waiting", "personnel pipeline"],
    },
    {
        "id": "anduril-defense-unicorn",
        "title": "Anduril: Rewriting Defense",
        "subtitle": "How a VR company founder and a Palantir veteran built a $30.5 billion defense contractor in seven years",
        "style_angle": "The company that makes autonomous weapons is structured like a startup and funded like a unicorn — and now it's irreplaceable",
        "targets": ["Anduril Industries", "Anduril", "Palmer Luckey", "Trae Stephens",
                     "Christian Brose", "Founders Fund", "Andreessen Horowitz",
                     "Oculus", "Shield AI", "Peter Thiel", "Brian Schimpf"],
        "keywords": ["anduril", "lattice", "fury", "roadrunner", "altius", "ghost",
                     "IVAS", "arsenal-1", "autonomous", "luckey", "stephens",
                     "series G", "$30.5 billion", "$2.3 billion", "defense tech",
                     "SHIELD IDIQ", "golden dome", "SBI", "palmer", "oculus"],
    },
    {
        "id": "political-influence-machine",
        "title": "The Political Influence Machine",
        "subtitle": "Campaign finance as relationship management",
        "targets": ["Gratitude America", "Gratitude America Ltd", "IPI", "HDI"],
        "keywords": ["FEC", "bundling", "plaskett", "richardson", "clinton", "campaign",
                     "political", "donation"],
    },
    {
        "id": "golden-dome-black-box",
        "title": "Golden Dome's Black Box",
        "subtitle": "How a $3.6 trillion missile defense program bypassed transparency through OTA contracts and classified procurement",
        "style_angle": "The most expensive defense program in history has no public cost schedule, no competitive bidding, and its lead contractors are run by the people who designed the program",
        "targets": ["Golden Dome", "Anduril Industries", "Anduril", "Palmer Luckey", "Trae Stephens",
                     "SpaceX", "Palantir Technologies", "Palantir", "Shield AI",
                     "Founders Fund", "Peter Thiel", "Michael Kratsios",
                     "Space Development Agency", "Missile Defense Agency"],
        "keywords": ["golden dome", "SBI", "space-based interceptor", "SHIELD IDIQ",
                     "OTA", "other transaction", "missile defense", "C2", "command and control",
                     "$175B", "$542B", "$3.6T", "CBO", "autonomous", "Fury", "Roadrunner",
                     "defense tech", "Arsenal-1", "prototype"],
    },
    {
        "id": "doge-data-operation",
        "title": "DOGE: The Data Operation",
        "subtitle": "How temporary advisors got permanent access to the federal government's most sensitive systems",
        "style_angle": "DOGE wasn't about efficiency — it was about making the federal government legible to a private network",
        "targets": ["DOGE", "Department of Government Efficiency", "Elon Musk",
                     "Aram Moghaddassi", "Michael Russo", "Gavin Kliger", "Daniel Berulis",
                     "Vivek Ramaswamy", "Amy Gleason", "Tom Krause"],
        "keywords": ["DOGE", "government efficiency", "data access", "Treasury", "OPM",
                     "personnel data", "payment system", "Privacy Act", "FISMA", "CFAA",
                     "Peters report", "whistleblower", "exfiltration", "CIO",
                     "embedded", "permanent position", "special government employee",
                     "master database", "consolidated"],
    },
    {
        "id": "parallel-diplomatic-corps",
        "title": "The Parallel Diplomatic Corps",
        "subtitle": "How IPI, HDI, and Gratitude America functioned as influence infrastructure outside FARA",
        "style_angle": "Epstein built institutions that brokered between governments — then those institutions needed him",
        "targets": [
            "International Peace Institute", "IPI",
            "Humpty Dumpty Institute", "HDI",
            "Terje Rod-Larsen", "Ehud Barak", "Thorbjorn Jagland",
            "Gratitude America", "Gratitude America Ltd",
            "Erika Kellerhals", "Richard Kahn", "Elliott Broidy",
        ],
        "keywords": [
            "IPI", "HDI", "lobbying", "FARA", "congress", "shadow",
            "norway", "norwegian", "rod-larsen", "barak", "jagland", "telenor",
            "FEC", "bundling", "plaskett", "richardson", "clinton", "campaign",
            "political", "donation", "gratitude", "kellerhals", "diplomatic",
        ],
    },
]


def gather_cluster_data(conn: sqlite3.Connection, cluster: dict) -> dict:
    """Gather all findings, connections, and evidence for a cluster."""
    targets = cluster["targets"]
    keywords = cluster.get("keywords", [])

    # Findings for named targets
    placeholders = ",".join("?" * len(targets))
    findings = conn.execute(
        f"""
        SELECT id, target_name, finding_type, summary, detail, source_datasets,
               confidence, date_of_event, claim_type, verification_status
        FROM findings
        WHERE target_name IN ({placeholders}) AND verification_status != 'retracted'
        ORDER BY date_of_event IS NULL, date_of_event
        """,
        targets,
    ).fetchall()

    # Also search by keywords in summary/detail
    keyword_findings = []
    for kw in keywords:
        kw_rows = conn.execute(
            """
            SELECT id, target_name, finding_type, summary, detail, source_datasets,
                   confidence, date_of_event, claim_type, verification_status
            FROM findings
            WHERE (summary LIKE ? OR detail LIKE ?) AND verification_status != 'retracted'
            """,
            (f"%{kw}%", f"%{kw}%"),
        ).fetchall()
        keyword_findings.extend(kw_rows)

    # Deduplicate
    seen_ids = {f["id"] for f in findings}
    for f in keyword_findings:
        if f["id"] not in seen_ids:
            findings.append(f)
            seen_ids.add(f["id"])

    # Get evidence for all findings
    finding_ids = list(seen_ids)
    evidence = {}
    for fid in finding_ids:
        evs = conn.execute(
            "SELECT evidence_type, evidence_ref, source_quote, source_page, assessment FROM finding_evidence WHERE finding_id = ?",
            (fid,),
        ).fetchall()
        if evs:
            evidence[fid] = [dict(e) for e in evs]

    # Connections between targets
    connections = []
    for i, ta in enumerate(targets):
        for tb in targets[i + 1 :]:
            conns = conn.execute(
                """
                SELECT id, person_a, person_b, relationship_type, description, strength, date_range
                FROM connections
                WHERE ((person_a = ? AND person_b = ?) OR (person_a = ? AND person_b = ?))
                  AND verification_status != 'retracted'
                """,
                (ta, tb, tb, ta),
            ).fetchall()
            connections.extend(conns)

    # Compute source diversity from evidence refs
    source_counts: dict[str, int] = {}
    for fid, ev_list in evidence.items():
        for ev in ev_list:
            ref = ev.get("evidence_ref", "")
            stype = classify_evidence_ref(ref)
            source_counts[stype] = source_counts.get(stype, 0) + 1

    total_refs = sum(source_counts.values())
    dominant_pct = 0.0
    if total_refs > 0:
        dominant_pct = round(max(source_counts.values()) / total_refs * 100, 1)

    # Check which search tools have NOT been searched for this cluster's targets
    searched_tools = set()
    for target in targets:
        rows = conn.execute(
            "SELECT DISTINCT source FROM search_log WHERE query_text LIKE ?",
            (f"%{target}%",),
        ).fetchall()
        for r in rows:
            searched_tools.add(r["source"])
    unsearched = sorted(ALL_SEARCH_TOOLS - searched_tools)

    source_diversity = {
        **{k: v for k, v in sorted(source_counts.items(), key=lambda x: -x[1])},
        "total": total_refs,
        "dominant_source_pct": dominant_pct,
        "source_types_used": len(source_counts),
        "unsearched_sources": unsearched,
    }

    return {
        **cluster,
        "findings": [dict(f) for f in findings],
        "evidence": {str(k): v for k, v in evidence.items()},
        "connections": [dict(c) for c in connections],
        "source_diversity": source_diversity,
        "stats": {
            "total_findings": len(findings),
            "total_connections": len(connections),
            "unique_targets": len(set(f["target_name"] for f in findings)),
        },
    }


def _resolve_output_path(args: argparse.Namespace) -> Path:
    if args.output_file:
        out_path = args.output_file
    elif args.cluster:
        # Safe default for single-cluster runs: avoid clobbering content/clusters.json.
        out_path = args.output_dir / f"cluster-{args.cluster}.json"
    else:
        out_path = args.output_dir / "clusters.json"

    if args.cluster and out_path.name == "clusters.json" and not args.allow_overwrite_main:
        raise ValueError(
            "Refusing to overwrite clusters.json for single-cluster export. "
            "Use --output-file <path> or pass --allow-overwrite-main."
        )

    return out_path


def main():
    parser = argparse.ArgumentParser(description="Prepare article cluster data")
    parser.add_argument("--cluster", help="Single cluster ID to export")
    parser.add_argument("--list", action="store_true", help="List available clusters")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--output-file",
        type=Path,
        help="Explicit output file path. Defaults to content/clusters.json for full export, "
             "or content/cluster-<id>.json for --cluster.",
    )
    parser.add_argument(
        "--allow-overwrite-main",
        action="store_true",
        help="Allow --cluster exports to overwrite clusters.json (unsafe default disabled).",
    )
    args = parser.parse_args()

    if args.list:
        for c in CLUSTERS:
            print(f"  {c['id']}: {c['title']}")
        return

    try:
        out_path = _resolve_output_path(args)
    except ValueError as err:
        print(err)
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    clusters_to_export = CLUSTERS
    if args.cluster:
        clusters_to_export = [c for c in CLUSTERS if c["id"] == args.cluster]
        if not clusters_to_export:
            print(f"Unknown cluster: {args.cluster}")
            return

    results = []
    for cluster in clusters_to_export:
        data = gather_cluster_data(conn, cluster)
        results.append(data)
        sd = data["source_diversity"]
        print(f"  {cluster['id']}: {data['stats']['total_findings']} findings, "
              f"{data['stats']['total_connections']} connections, "
              f"{data['stats']['unique_targets']} targets, "
              f"{sd['source_types_used']} source types ({sd['dominant_source_pct']}% dominant)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nExported {len(results)} clusters to {out_path}")
    conn.close()


if __name__ == "__main__":
    main()
