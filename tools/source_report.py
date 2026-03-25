#!/usr/bin/env python3
"""
Data source coverage report for the OSINT investigation platform.

Lists all available data sources, record counts, and status.

Usage:
    python tools/source_report.py
    python tools/source_report.py --json
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ithildin.core.paths import doj_db_path

PROJECT_ROOT = Path(__file__).parent.parent


def load_env_file():
    """Load key=value pairs from .env into process env (without overriding existing values)."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def check_sqlite(path, count_query):
    """Check if a SQLite DB exists and get record count."""
    if not path.exists():
        return {"status": "missing", "path": str(path), "records": 0}
    try:
        db = sqlite3.connect(str(path))
        count = db.execute(count_query).fetchone()[0]
        db.close()
        size_mb = path.stat().st_size / (1024 * 1024)
        return {"status": "available", "path": str(path), "records": count, "size_mb": round(size_mb, 1)}
    except Exception as e:
        return {"status": "error", "path": str(path), "error": str(e)}


def check_parquet(path):
    """Check if a parquet file exists and get row count."""
    if not path.exists():
        return {"status": "missing", "path": str(path), "records": 0}
    try:
        import pandas as pd
        df = pd.read_parquet(path)
        size_mb = path.stat().st_size / (1024 * 1024)
        return {"status": "available", "path": str(path), "records": len(df), "size_mb": round(size_mb, 1)}
    except ModuleNotFoundError:
        # pandas may not be installed in lightweight environments. Presence + size still indicates availability.
        size_mb = path.stat().st_size / (1024 * 1024)
        return {"status": "available", "path": str(path), "records": 0, "size_mb": round(size_mb, 1), "note": "Install pandas to compute row count"}
    except Exception as e:
        return {"status": "error", "path": str(path), "error": str(e)}


def check_directory(path):
    """Check if a directory exists and count files."""
    if not path.exists():
        return {"status": "missing", "path": str(path), "records": 0}
    try:
        count = sum(1 for _ in path.rglob("*") if _.is_file())
        size_mb = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / (1024 * 1024)
        return {"status": "available", "path": str(path), "records": count, "size_mb": round(size_mb, 1)}
    except Exception as e:
        return {"status": "error", "path": str(path), "error": str(e)}


def check_neo4j():
    """Check if ICIJ Neo4j is running."""
    try:
        import subprocess
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=icij-offshore-leaks", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            return {"status": "running", "bolt": "bolt://localhost:7689", "info": result.stdout.strip()}
        return {"status": "stopped", "start_cmd": "./scripts/start_icij_db.sh"}
    except Exception:
        return {"status": "unknown", "start_cmd": "./scripts/start_icij_db.sh"}


def check_api(name, test_url=None, headers=None):
    """Check if an API is reachable."""
    if not test_url:
        return {"status": "configured"}
    try:
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError
        # SEC EDGAR requires User-Agent header
        req_headers = {"User-Agent": "OSINT-Research osint-research@proton.me"}
        if headers:
            req_headers.update(headers)
        # DugganUSA requires Bearer token auth
        if "dugganusa.com" in test_url:
            api_key = os.environ.get("DUGGANUSA_API_KEY")
            if api_key:
                req_headers["Authorization"] = f"Bearer {api_key}"
            else:
                return {"status": "no_api_key", "start_cmd": "export DUGGANUSA_API_KEY=<key>"}
        req = Request(test_url, headers=req_headers)
        with urlopen(req, timeout=5) as resp:
            return {"status": "available" if resp.status == 200 else f"error:{resp.status}"}
    except HTTPError as e:
        return {"status": f"error:{e.code}", "error": str(e)}
    except (URLError, Exception) as e:
        return {"status": "unreachable", "error": str(e)}


def generate_report():
    """Generate full source coverage report."""
    sources = {}

    # Local SQLite databases
    sources["DOJ Vol 11"] = {
        "description": "331K OCR'd pages from DOJ Volume 11 release",
        "query_tool": "tools/query_doj.py",
        **check_sqlite(
            doj_db_path(),
            "SELECT COUNT(*) FROM documents"
        ),
    }

    sources["LMSBAND"] = {
        "description": "60K files, 851K entities, 110K co-occurrences",
        "query_tool": "tools/query_lmsband.py",
        **check_sqlite(
            PROJECT_ROOT / "datasets" / "lmsband_epstein_files.db",
            "SELECT COUNT(*) FROM files"
        ),
    }

    sources["Unified DB"] = {
        "description": "Consolidated emails, docs, entities, triples",
        "query_tool": "tools/query_unified.py",
        **check_sqlite(
            PROJECT_ROOT / "datasets" / "unified_epstein.db",
            "SELECT COUNT(*) FROM emails"
        ),
    }

    sources["Doc-Explorer"] = {
        "description": "25K docs, 107K triples, 27K entities (RDF format)",
        "query_tool": "sqlite3 (direct)",
        **check_sqlite(
            PROJECT_ROOT / "datasets" / "Epstein-doc-explorer" / "document_analysis.db",
            "SELECT COUNT(*) FROM rdf_triples"
        ),
    }

    sources["Investigation DB"] = {
        "description": "Leads, findings, connections, search log, sessions",
        "query_tool": "tools/lead_tracker.py / tools/findings_tracker.py",
        **check_sqlite(
            PROJECT_ROOT / "investigation.db",
            "SELECT COUNT(*) FROM leads"
        ),
    }

    # Parquet files
    sources["HF Emails Parquet"] = {
        "description": "4,272 House Oversight emails",
        "query_tool": "pandas read_parquet()",
        **check_parquet(PROJECT_ROOT / "datasets" / "epstein-emails-hf" / "emails.parquet"),
    }

    sources["FBI Files Parquet"] = {
        "description": "8,150 FBI docs (Textract OCR)",
        "query_tool": "pandas read_parquet()",
        **check_parquet(PROJECT_ROOT / "datasets" / "svetfm_fbi_files.parquet"),
    }

    sources["Email Threads Parquet"] = {
        "description": "5,082 email threads",
        "query_tool": "pandas read_parquet()",
        **check_parquet(PROJECT_ROOT / "datasets" / "notesbymuneeb_epstein_email_threads.parquet"),
    }

    # File directories
    sources["DDoSecrets EML"] = {
        "description": "13K+ raw .eml files from jeeproject yahoo",
        "query_tool": "tools/search_emails.py",
        **check_directory(PROJECT_ROOT / "datasets" / "epstein-archive" / "data" / "emails" / "jeeproject_yahoo"),
    }

    sources["Barak Emails"] = {
        "description": "1,411 Ehud Barak email files (.html + .meta)",
        "query_tool": "tools/search_emails.py",
        **check_directory(PROJECT_ROOT / "datasets" / "epstein-archive" / "data" / "emails" / "ehud_barak_emails"),
    }

    # Neo4j
    sources["ICIJ Offshore Leaks"] = {
        "description": "~800K offshore entities (Neo4j)",
        "query_tool": "tools/query_icij.py",
        **check_neo4j(),
    }

    # Corporate Registry
    sources["Corporate Registry"] = {
        "description": "FL, NY, NM, PA corps, officers, filings (unified schema)",
        "query_tool": "tools/query_registry.py",
        **check_sqlite(
            PROJECT_ROOT / "registry.db",
            "SELECT COUNT(*) FROM registry_entities"
        ),
    }

    # Investigations DB (ingested PDFs)
    sources["Investigations DB"] = {
        "description": "Ingested investigation reports (PDF → FTS5)",
        "query_tool": "tools/query_investigations.py",
        **check_sqlite(
            PROJECT_ROOT / "datasets" / "investigations.db",
            "SELECT COUNT(*) FROM documents"
        ),
    }

    # FAA Registry
    sources["FAA Registry"] = {
        "description": "US aircraft registration (owner names, N-numbers, addresses)",
        "query_tool": "tools/ingest_faa.py",
        **check_sqlite(
            PROJECT_ROOT / "datasets" / "faa_registry.db",
            "SELECT COUNT(*) FROM aircraft"
        ),
    }

    # Government spending
    sources["USAspending"] = {
        "description": "All federal spending — contracts, grants, loans, subawards (no auth)",
        "query_tool": "tools/query_usaspending.py",
        **check_api("USAspending", "https://api.usaspending.gov/api/v2/references/toptier_agencies/"),
    }

    sam_key = os.environ.get("SAM_API_KEY")
    sources["SAM.gov"] = {
        "description": "Entity registrations, exclusions/debarments, contract awards, opportunities",
        "query_tool": "tools/query_sam.py",
        **check_api("SAM.gov", f"https://api.sam.gov/entity-information/v4/exclusions?api_key={sam_key}&q=test" if sam_key else None),
    }
    if not sam_key:
        sources["SAM.gov"]["status"] = "no_api_key"
        sources["SAM.gov"]["start_cmd"] = "export SAM_API_KEY=<key> (free at sam.gov → Account Details → API Key)"

    # SAM.gov Bulk Data (local SQLite)
    sources["SAM.gov Bulk"] = {
        "description": "874K entities, 167K exclusions (monthly extract, local SQLite)",
        "query_tool": "tools/ingest_sam.py",
        **check_sqlite(
            PROJECT_ROOT / "datasets" / "sam.db",
            "SELECT COUNT(*) FROM sam_entities"
        ),
    }

    # OpenSanctions (bulk ingested locally)
    sources["OpenSanctions"] = {
        "description": "Global sanctions/PEP/debarment graph (local SQLite)",
        "query_tool": "tools/query_opensanctions.py",
        **check_sqlite(
            PROJECT_ROOT / "datasets" / "opensanctions.db",
            "SELECT COUNT(*) FROM os_entities"
        ),
    }

    # CourtListener / RECAP
    courtlistener_token = os.environ.get("COURTLISTENER_TOKEN")
    sources["CourtListener / RECAP"] = {
        "description": "Federal dockets, parties, and RECAP archive records",
        "query_tool": "tools/query_courtlistener.py",
        **check_api(
            "CourtListener",
            "https://www.courtlistener.com/api/rest/v4/dockets/?page_size=1",
            headers={"Authorization": f"Token {courtlistener_token}"} if courtlistener_token else None,
        ),
    }
    if not courtlistener_token:
        sources["CourtListener / RECAP"]["status"] = "no_api_key"
        sources["CourtListener / RECAP"]["start_cmd"] = "export COURTLISTENER_TOKEN=<token>"

    # UK Companies House
    companies_house_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    ch_headers = None
    if companies_house_key:
        auth = base64.b64encode(f"{companies_house_key}:".encode()).decode()
        ch_headers = {"Authorization": f"Basic {auth}"}
    sources["UK Companies House"] = {
        "description": "UK corporate registry + PSC beneficial ownership",
        "query_tool": "tools/ingest_uk_companies_house.py",
        **check_api(
            "Companies House",
            "https://api.company-information.service.gov.uk/search/companies?q=test&items_per_page=1",
            headers=ch_headers,
        ),
    }
    if not companies_house_key:
        sources["UK Companies House"]["status"] = "no_api_key"
        sources["UK Companies House"]["start_cmd"] = "export COMPANIES_HOUSE_API_KEY=<key>"

    # Swiss Zefix (public SPARQL)
    sources["Swiss Zefix"] = {
        "description": "Swiss commercial registry via public SPARQL endpoint",
        "query_tool": "tools/query_zefix.py",
        **check_api("Zefix", "https://lindas.admin.ch/query/"),
    }

    # Medicare API
    sources["Medicare (CMS API)"] = {
        "description": "Provider-level Medicare spending (no auth)",
        "query_tool": "tools/query_medicare.py",
        **check_api(
            "CMS",
            "https://data.cms.gov/data-api/v1/dataset/8889d81e-2ee7-448f-8713-f071038289b5/data?size=1",
        ),
    }

    # SBA PPP Loans (bulk parquet)
    sources["SBA PPP Loans"] = {
        "description": "~11M PPP/EIDL loans — borrower, address, lender, NAICS, forgiveness",
        "query_tool": "tools/query_ppp.py",
        **check_parquet(PROJECT_ROOT / "data" / "ppp_loans.parquet"),
    }

    # Medicaid parquet corpus
    sources["Medicaid Spending Parquet"] = {
        "description": "T-MSIS Medicaid spending parquet (billing, servicing, HCPCS)",
        "query_tool": "tools/query_medicaid.py",
        **check_parquet(PROJECT_ROOT / "data" / "medicaid_spending.parquet"),
    }

    # FinCEN files (local CSV corpus)
    sources["FinCEN Files"] = {
        "description": "Leaked SAR transaction + bank connection datasets",
        "query_tool": "tools/query_fincen.py",
        **check_directory(PROJECT_ROOT / "datasets" / "fincen_files"),
    }

    # DocumentCloud
    sources["DocumentCloud"] = {
        "description": "Public document archive + project APIs",
        "query_tool": "tools/query_documentcloud.py",
        **check_api("DocumentCloud", "https://api.www.documentcloud.org/api/projects/216915/"),
    }

    # MuckRock FOIA
    sources["MuckRock FOIA"] = {
        "description": "Public FOIA request metadata + release links",
        "query_tool": "tools/query_muckrock.py",
        **check_api("MuckRock", "https://www.muckrock.com/api_v1/foia/?page_size=1"),
    }

    # HigherGov (paid API)
    highergov_key = os.environ.get("HIGHERGOV_API_KEY")
    sources["HigherGov"] = {
        "description": "Federal contract/grant/vehicle intelligence (paid API)",
        "query_tool": "tools/query_highergov.py",
        **check_api(
            "HigherGov",
            f"https://www.highergov.com/api-external/agency/?api_key={highergov_key}&page_size=1" if highergov_key else None,
        ),
    }
    if not highergov_key:
        sources["HigherGov"]["status"] = "no_api_key"
        sources["HigherGov"]["start_cmd"] = "export HIGHERGOV_API_KEY=<key>"

    # APIs
    sources["DugganUSA API"] = {
        "description": "329K+ docs across all 12 DOJ datasets",
        "query_tool": "tools/duggan_search.py",
        **check_api("DugganUSA", "https://analytics.dugganusa.com/api/v1/search?q=test&indexes=epstein_files&limit=1"),
    }

    sources["LittleSis API"] = {
        "description": "Power network relationships (500+ Epstein connections pre-mapped)",
        "query_tool": "tools/query_littlesis.py",
        **check_api("LittleSis", "https://littlesis.org/api/entities/36043"),
    }

    sources["SEC EDGAR EFTS"] = {
        "description": "Full-text search across all SEC filings (1,237 Epstein hits)",
        "query_tool": "tools/query_edgar.py",
        **check_api("EDGAR", "https://efts.sec.gov/LATEST/search-index?q=test&size=1"),
    }

    # NYC ACRIS Property Records
    sources["NYC ACRIS"] = {
        "description": "NYC property transactions (deeds, mortgages, liens) via SODA API",
        "query_tool": "tools/query_acris.py",
        **check_api("ACRIS"),  # SODA API is slow to respond — skip live check
    }

    # FEC Campaign Finance
    sources["FEC Campaign Finance"] = {
        "description": "Political donations (Schedule A contributions)",
        "query_tool": "tools/query_fec.py",
        **check_api("FEC", "https://api.open.fec.gov/v1/?api_key=DEMO_KEY"),
    }

    # Federal Lobbying (LDA)
    sources["Federal Lobbying (LDA)"] = {
        "description": "1.9M+ lobbying filings, registrants, clients",
        "query_tool": "tools/query_lobbying.py",
        **check_api("LDA", "https://lda.senate.gov/api/v1/filings/?page_size=1"),
    }

    # FARA Foreign Agents
    fara_info = check_sqlite(
        PROJECT_ROOT / "investigation.db",
        "SELECT COUNT(*) FROM fara_registrants"
    )
    if fara_info.get("status") == "error":
        fara_info = {"status": "not_ingested", "records": 0}
        fara_info["start_cmd"] = "python tools/query_fara.py download && python tools/query_fara.py ingest"
    sources["FARA Foreign Agents"] = {
        "description": "7K registrants, 17K foreign principals, 151K documents",
        "query_tool": "tools/query_fara.py",
        **fara_info,
    }

    # Shodan (infrastructure recon)
    shodan_key = os.environ.get("SHODAN_API_KEY")
    sources["Shodan"] = {
        "description": "Internet-connected devices, DNS, SSL certs (infrastructure recon)",
        "query_tool": "tools/query_shodan.py",
        **check_api("Shodan", f"https://api.shodan.io/api-info?key={shodan_key}" if shodan_key else None),
    }

    # crt.sh Certificate Transparency
    sources["crt.sh CT Logs"] = {
        "description": "Certificate Transparency logs — subdomain enum, cert timeline, issuer tracking",
        "query_tool": "tools/query_crtsh.py",
        **check_api("crt.sh", "https://crt.sh/?q=example.com&output=json"),
    }

    # Wayback Machine CDX
    sources["Wayback Machine"] = {
        "description": "Historical web snapshots — timeline reconstruction, removed content detection",
        "query_tool": "tools/query_wayback.py",
        **check_api("Wayback", "https://web.archive.org/cdx/search/cdx?url=example.com&output=json&limit=1"),
    }

    # FDIC BankFind
    sources["FDIC BankFind"] = {
        "description": "FDIC-insured bank institutions, failures, financials, branches (no auth)",
        "query_tool": "tools/query_fdic.py",
        **check_api("FDIC", "https://api.fdic.gov/banks/institutions?limit=1"),
    }

    # URLScan.io
    sources["URLScan.io"] = {
        "description": "Passive web scans — tech stacks, linked domains, hosting, HTTP transactions",
        "query_tool": "tools/query_urlscan.py",
        **check_api("URLScan", "https://urlscan.io/api/v1/search/?q=domain:example.com&size=1"),
    }

    # OCCRP Aleph
    aleph_key = os.environ.get("ALEPH_API_KEY")
    sources["OCCRP Aleph"] = {
        "description": "OCCRP investigative datasets — corporate registries, leaks, sanctions, court records (200+ datasets)",
        "query_tool": "tools/query_aleph.py",
        **check_api("Aleph", "https://aleph.occrp.org/api/2/statistics"),
    }
    if aleph_key:
        sources["OCCRP Aleph"]["auth"] = "api_key"

    # GDELT
    sources["GDELT"] = {
        "description": "Global event/news monitoring — themes, organizations, tones, source URLs",
        "query_tool": "tools/query_gdelt.py",
        **check_api("GDELT", "https://api.gdeltproject.org/api/v2/doc/doc?query=test&mode=artlist&maxrecords=1&format=json"),
    }

    # FINRA BrokerCheck
    sources["FINRA BrokerCheck"] = {
        "description": "Broker/adviser registration, disciplinary history, CRD lookup",
        "query_tool": "tools/query_finra.py",
        **check_api("FINRA", "https://api.brokercheck.finra.org/search/individual?query=test&hl=true&nrows=1"),
    }

    # France SIRENE
    sources["France SIRENE"] = {
        "description": "French company registry (SIREN/SIRET, NAF codes, addresses)",
        "query_tool": "tools/query_france.py",
        **check_api("SIRENE", "https://api.insee.fr/api-sirene/3.11/siret?q=denominationUniteLegale:test&nombre=1"),
    }

    # HUDOC (ECHR)
    sources["HUDOC (ECHR)"] = {
        "description": "European Court of Human Rights — judgments, decisions, communications (20K+ judgments)",
        "query_tool": "tools/query_hudoc.py",
        **check_api("HUDOC"),
    }

    # Israel Corp Authority
    sources["Israel Corp Authority"] = {
        "description": "Israeli corporate registry — company details, officers, filings",
        "query_tool": "tools/query_israel.py",
        **check_api("Israel"),
    }

    # GLEIF (LEI)
    sources["GLEIF (LEI)"] = {
        "description": "Global Legal Entity Identifiers — ownership chains, fund managers, ultimate parents",
        "query_tool": "tools/query_gleif.py",
        **check_api("GLEIF", "https://api.gleif.org/api/v1/lei-records?page[size]=1"),
    }

    # OffshoreAlert
    sources["OffshoreAlert"] = {
        "description": "Offshore finance investigations, court filings, regulatory actions",
        "query_tool": "tools/query_investigations.py (OffshoreAlert category)",
        "status": "configured",
    }

    # Blockchain tools
    etherscan_key = os.environ.get("ETHERSCAN_API_KEY")
    sources["Etherscan"] = {
        "description": "Ethereum blockchain — transactions, token transfers, contract interactions",
        "query_tool": "tools/query_etherscan.py",
        **check_api("Etherscan", f"https://api.etherscan.io/api?module=stats&action=ethprice&apikey={etherscan_key}" if etherscan_key else None),
    }
    if not etherscan_key:
        sources["Etherscan"]["status"] = "no_api_key"

    sources["Solscan"] = {
        "description": "Solana blockchain — account activity, token balances, transaction history",
        "query_tool": "tools/query_solscan.py",
        **check_api("Solscan"),
    }

    sources["Dune Analytics"] = {
        "description": "Cross-chain blockchain analytics via SQL queries",
        "query_tool": "tools/query_dune.py",
        "status": "configured",
    }

    # GovInfo (congressional)
    govinfo_key = os.environ.get("GOVINFO_API_KEY")
    sources["GovInfo (GPO)"] = {
        "description": "Congressional hearings (CHRG), committee reports (CRPT), GAO reports, CRS reports",
        "query_tool": "tools/query_govinfo.py",
        **check_api("GovInfo", f"https://api.govinfo.gov/collections?api_key={govinfo_key}&pageSize=1" if govinfo_key else None),
    }
    if not govinfo_key:
        sources["GovInfo (GPO)"]["status"] = "no_api_key"
        sources["GovInfo (GPO)"]["start_cmd"] = "export GOVINFO_API_KEY=<key> (free at https://api.data.gov/signup/)"

    # Congress.gov
    congress_key = os.environ.get("CONGRESS_API_KEY")
    sources["Congress.gov"] = {
        "description": "Bills, members, committees, nominations, CRS reports (structured metadata)",
        "query_tool": "tools/query_congress.py",
        **check_api("Congress", f"https://api.congress.gov/v3/bill?api_key={congress_key}&format=json&limit=1" if congress_key else None),
    }
    if not congress_key:
        sources["Congress.gov"]["status"] = "no_api_key"
        sources["Congress.gov"]["start_cmd"] = "export CONGRESS_API_KEY=<key> (free at https://api.congress.gov/sign-up/)"

    # ICIJ Reconciliation API (no auth needed)
    sources["ICIJ Reconciliation API"] = {
        "description": "Match entity names against 810K+ offshore entities (no Neo4j needed)",
        "query_tool": "tools/query_icij.py reconcile / reconcile-all",
        **check_api("ICIJ Reconcile", "https://offshoreleaks.icij.org/api/v1/reconcile"),
    }

    return sources


def quick_health_check(source_name):
    """Run health check for a single named source.

    Returns {available: bool, note: str, status: str}.
    Matches source names case-insensitively.
    """
    load_env_file()
    sources = generate_report()

    # Case-insensitive lookup
    match = None
    for name, info in sources.items():
        if name.lower() == source_name.lower():
            match = (name, info)
            break
    if not match:
        # Try partial match
        for name, info in sources.items():
            if source_name.lower() in name.lower():
                match = (name, info)
                break

    if not match:
        return {"available": False, "note": f"Unknown source: {source_name}", "status": "unknown"}

    name, info = match
    status = info.get("status", "unknown")
    available = status in ("available", "running", "configured")
    note = info.get("error") or info.get("note") or info.get("start_cmd") or ""
    return {"available": available, "note": note, "status": status, "name": name}


def main():
    load_env_file()

    parser = argparse.ArgumentParser(description="OSINT data source coverage report")
    parser.add_argument("-j", "--json", action="store_true")
    sub = parser.add_subparsers(dest="command")

    # check <source_name>
    check_p = sub.add_parser("check", help="Quick health check for a single source")
    check_p.add_argument("source_name", help="Source name (case-insensitive, partial match)")

    args = parser.parse_args()

    if args.command == "check":
        result = quick_health_check(args.source_name)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            icon = "[OK]" if result["available"] else "[!!]"
            print(f"{icon} {result.get('name', args.source_name)}: {result['status']}")
            if result["note"]:
                print(f"     {result['note']}")
        return

    sources = generate_report()

    if args.json:
        print(json.dumps(sources, indent=2))
        return

    print("\n" + "=" * 80)
    print("OSINT DATA SOURCE REPORT")
    print("=" * 80)

    available = 0
    total = len(sources)

    def status_icon(status):
        if status in ("available", "running", "configured"):
            return "[OK]"
        if status in ("missing", "not_ingested"):
            return "[--]"
        if status in ("no_api_key", "unreachable", "unknown"):
            return "[??]"
        if isinstance(status, str) and status.startswith("error"):
            return "[!!]"
        if status == "stopped":
            return "[!!]"
        return "[??]"

    def is_available(status):
        if status in ("available", "running", "configured"):
            return True
        return False

    for name, info in sources.items():
        status = info.get("status", "?")
        icon = status_icon(status)

        records = info.get("records", "")
        records_str = f" ({records:,} records)" if isinstance(records, int) and records > 0 else ""
        size = info.get("size_mb", "")
        size_str = f" [{size} MB]" if size else ""

        print(f"\n{icon} {name}{records_str}{size_str}")
        print(f"     {info['description']}")
        print(f"     Tool: {info.get('query_tool', '?')}")
        if info.get("start_cmd"):
            print(f"     Start: {info['start_cmd']}")
        if info.get("note"):
            print(f"     Note: {info['note']}")
        if info.get("error"):
            print(f"     Error: {info['error']}")

        if is_available(status):
            available += 1

    print(f"\n{'='*80}")
    print(f"Sources available: {available}/{total}")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
