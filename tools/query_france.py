#!/usr/bin/env python3
"""
French company registry query tool (Annuaire des Entreprises / SIRENE).

Searches all French companies, associations, and sole proprietorships via
the free, no-auth recherche-entreprises.api.gouv.fr API.

Data includes: SIREN/SIRET, company name, dirigeants (officers with birth dates
and nationality), address history, activity codes, open/closed status, employee counts.

Usage:
    python tools/query_france.py search "Soffer Avocats"
    python tools/query_france.py search "Ron Soffer" --limit 10
    python tools/query_france.py company 380866657
    python tools/query_france.py address "4 Rue Quentin-Bauchart" --postal 75008
    python tools/query_france.py naf 69.10Z --postal 75008 --limit 20
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
    from tools.lead_tracker import log_search
except ImportError:
    from output_util import add_output_args, write_output
    from lead_tracker import log_search

BASE_URL = "https://recherche-entreprises.api.gouv.fr/search"
RATE_LIMIT = 0.5  # seconds between requests


def _get(url, retries=2):
    """GET request with retries."""
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "OSINT-Research/1.0", "Accept": "application/json"})
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise
        except URLError:
            if attempt < retries:
                time.sleep(1)
                continue
            raise


def _flatten_company(r):
    """Flatten API result into clean dict."""
    siege = r.get("siege", {})
    out = {
        "siren": r.get("siren"),
        "nom_complet": r.get("nom_complet"),
        "nom_raison_sociale": r.get("nom_raison_sociale"),
        "sigle": r.get("sigle"),
        "activite_principale": r.get("activite_principale"),
        "categorie_entreprise": r.get("categorie_entreprise"),
        "nature_juridique": r.get("nature_juridique"),
        "entrepreneur_individuel": r.get("entrepreneur_individuel"),
        "date_creation": r.get("date_creation"),
        "date_fermeture": r.get("date_fermeture"),
        "etat_administratif": r.get("etat_administratif"),
        "nombre_etablissements": r.get("nombre_etablissements"),
        "nombre_etablissements_ouverts": r.get("nombre_etablissements_ouverts"),
        "tranche_effectif_salarie": r.get("tranche_effectif_salarie"),
        # Siege (HQ)
        "siege_siret": siege.get("siret"),
        "siege_adresse": siege.get("adresse"),
        "siege_code_postal": siege.get("code_postal"),
        "siege_commune": siege.get("libelle_commune"),
        "siege_date_creation": siege.get("date_creation"),
        "siege_date_fermeture": siege.get("date_fermeture"),
        "siege_etat": siege.get("etat_administratif"),
        "siege_activite": siege.get("activite_principale"),
        "siege_lat": siege.get("latitude"),
        "siege_lon": siege.get("longitude"),
        # Dirigeants
        "dirigeants": r.get("dirigeants", []),
        # Matching establishments (address history)
        "matching_etablissements": r.get("matching_etablissements", []),
    }
    return out


def search(query, page=1, per_page=25, naf=None, postal=None, departement=None,
           nature=None, active_only=False):
    """Search French companies by name, person, or SIREN."""
    params = {"q": query, "page": page, "per_page": per_page}
    if naf:
        params["activite_principale"] = naf
    if postal:
        params["code_postal"] = postal
    if departement:
        params["departement"] = departement
    if nature:
        params["nature_juridique"] = nature
    if active_only:
        params["etat_administratif"] = "A"

    url = f"{BASE_URL}?{urlencode(params)}"
    data = _get(url)

    results = [_flatten_company(r) for r in data.get("results", [])]
    total = data.get("total_results", len(results))

    return {"total": total, "page": page, "per_page": per_page, "records": results}


def get_company(siren):
    """Get company details by SIREN number."""
    url = f"{BASE_URL}?q={siren}&page=1&per_page=5"
    data = _get(url)

    # Find exact SIREN match
    for r in data.get("results", []):
        if r.get("siren") == str(siren):
            return _flatten_company(r)

    # If no exact match, return first result
    results = data.get("results", [])
    if results:
        return _flatten_company(results[0])
    return None


def search_by_naf(naf_code, postal=None, page=1, per_page=25):
    """Search by NAF activity code (e.g., 69.10Z for legal services)."""
    params = {"activite_principale": naf_code, "page": page, "per_page": per_page}
    if postal:
        params["code_postal"] = postal

    url = f"{BASE_URL}?{urlencode(params)}"
    data = _get(url)

    results = [_flatten_company(r) for r in data.get("results", [])]
    total = data.get("total_results", len(results))

    return {"total": total, "page": page, "per_page": per_page, "records": results}


def _print_company(c, verbose=False):
    """Print a single company record."""
    status = "ACTIVE" if c["etat_administratif"] == "A" else "CLOSED"
    closed = f" (closed {c['date_fermeture']})" if c.get("date_fermeture") else ""
    print(f"\n  SIREN: {c['siren']} | {c['nom_complet']} | {status}{closed}")
    print(f"  Activity: {c.get('activite_principale', '?')} | Type: {c.get('nature_juridique', '?')}")
    if c.get("entrepreneur_individuel"):
        print(f"  Sole proprietor (entrepreneur individuel)")
    print(f"  Created: {c.get('date_creation', '?')} | Establishments: {c.get('nombre_etablissements', '?')} ({c.get('nombre_etablissements_ouverts', 0)} open)")
    if c.get("siege_adresse"):
        print(f"  HQ: {c['siege_adresse']}")

    # Dirigeants
    for d in c.get("dirigeants", []):
        nat = d.get("nationalite", "")
        birth = d.get("date_de_naissance") or d.get("annee_de_naissance", "")
        qual = d.get("qualite", "")
        name = f"{d.get('prenoms', '')} {d.get('nom', '')}".strip()
        if not name:
            name = d.get("denomination", d.get("siren", "?"))
        print(f"  Officer: {name} ({qual}) | Born: {birth} | Nationality: {nat}")

    if verbose:
        # Address history
        for est in c.get("matching_etablissements", []):
            est_status = "open" if est.get("etat_administratif") == "A" else "closed"
            dates = f"{est.get('date_creation', '?')} - {est.get('date_fermeture', 'present')}"
            print(f"  Establishment: {est.get('adresse', '?')} [{est_status}] ({dates})")


def main():
    parser = argparse.ArgumentParser(description="French company registry (SIRENE/Annuaire des Entreprises)")
    sub = parser.add_subparsers(dest="command")

    # search
    p_search = sub.add_parser("search", help="Search by name, person, or keyword")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=25, help="Results per page (max 25)")
    p_search.add_argument("--page", type=int, default=1, help="Page number")
    p_search.add_argument("--naf", help="Filter by NAF activity code (e.g., 69.10Z)")
    p_search.add_argument("--postal", help="Filter by postal code")
    p_search.add_argument("--departement", help="Filter by departement code")
    p_search.add_argument("--active", action="store_true", help="Active companies only")
    p_search.add_argument("-v", "--verbose", action="store_true", help="Show address history")
    add_output_args(p_search)

    # company
    p_company = sub.add_parser("company", help="Lookup by SIREN number")
    p_company.add_argument("siren", help="SIREN number (9 digits)")
    p_company.add_argument("-v", "--verbose", action="store_true", help="Show all details")
    add_output_args(p_company)

    # naf
    p_naf = sub.add_parser("naf", help="Search by NAF activity code")
    p_naf.add_argument("code", help="NAF code (e.g., 69.10Z for legal services)")
    p_naf.add_argument("--postal", help="Filter by postal code")
    p_naf.add_argument("--limit", type=int, default=25, help="Results per page")
    p_naf.add_argument("--page", type=int, default=1, help="Page number")
    add_output_args(p_naf)

    # address
    p_addr = sub.add_parser("address", help="Search by address")
    p_addr.add_argument("address", help="Address text to search")
    p_addr.add_argument("--postal", help="Filter by postal code")
    p_addr.add_argument("--limit", type=int, default=25, help="Results per page")
    p_addr.add_argument("-v", "--verbose", action="store_true", help="Show address history")
    add_output_args(p_addr)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "search":
        result = search(args.query, page=args.page, per_page=min(args.limit, 25),
                       naf=args.naf, postal=args.postal, departement=args.departement,
                       active_only=args.active)
        log_search("france_sirene", args.query, result["total"])

        if not write_output(result, args, summary=f"France SIRENE search '{args.query}'"):
            print(f"French SIRENE: {result['total']} results for '{args.query}'")
            for c in result["records"]:
                _print_company(c, verbose=args.verbose)

    elif args.command == "company":
        result = get_company(args.siren)
        log_search("france_sirene", f"SIREN:{args.siren}", 1 if result else 0)

        if result is None:
            print(f"No company found for SIREN {args.siren}")
            sys.exit(1)

        if not write_output(result, args, summary=f"SIREN {args.siren}"):
            _print_company(result, verbose=True)

    elif args.command == "naf":
        result = search_by_naf(args.code, postal=args.postal, page=args.page,
                              per_page=min(args.limit, 25))
        log_search("france_sirene", f"NAF:{args.code}", result["total"])

        if not write_output(result, args, summary=f"NAF {args.code}"):
            print(f"NAF {args.code}: {result['total']} results")
            for c in result["records"]:
                _print_company(c)

    elif args.command == "address":
        result = search(args.address, per_page=min(args.limit, 25), postal=args.postal)
        log_search("france_sirene", f"address:{args.address}", result["total"])

        if not write_output(result, args, summary=f"address search '{args.address}'"):
            print(f"Address search: {result['total']} results")
            for c in result["records"]:
                _print_company(c, verbose=args.verbose)


if __name__ == "__main__":
    main()
