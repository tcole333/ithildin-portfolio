#!/usr/bin/env python3
"""
Medicaid Provider Spending query tool (T-MSIS, 2018-2024).

227M rows of provider-level Medicaid spending data aggregated by
billing NPI, servicing NPI, HCPCS code, and month.

Usage:
    python tools/query_medicaid.py stats
    python tools/query_medicaid.py top-billers --limit 20
    python tools/query_medicaid.py top-codes --limit 20
    python tools/query_medicaid.py provider 1376609297
    python tools/query_medicaid.py provider 1376609297 --timeline
    python tools/query_medicaid.py code T1019 --limit 20
    python tools/query_medicaid.py network 1376609297
    python tools/query_medicaid.py recoupments --limit 20
    python tools/query_medicaid.py sql "SELECT billing_npi, sum(paid) FROM m GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
"""

import argparse
import sys
from pathlib import Path

import duckdb

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PARQUET_PATH = DATA_DIR / "medicaid_spending.parquet"
BILLING_PROVIDERS_PATH = DATA_DIR / "billing_providers.parquet"
SERVICING_PROVIDERS_PATH = DATA_DIR / "servicing_providers.parquet"
HCPCS_CODES_PATH = DATA_DIR / "hcpcs_codes.parquet"


def _connect():
    """Return DuckDB connection with parquet files registered as views."""
    con = duckdb.connect()
    con.execute(f"CREATE VIEW m AS SELECT * FROM read_parquet('{PARQUET_PATH}')")
    if BILLING_PROVIDERS_PATH.exists():
        con.execute(f"CREATE VIEW bp AS SELECT * FROM read_parquet('{BILLING_PROVIDERS_PATH}')")
    if SERVICING_PROVIDERS_PATH.exists():
        con.execute(f"CREATE VIEW sp AS SELECT * FROM read_parquet('{SERVICING_PROVIDERS_PATH}')")
    if HCPCS_CODES_PATH.exists():
        con.execute(f"CREATE VIEW hcpcs AS SELECT * FROM read_parquet('{HCPCS_CODES_PATH}')")
    return con


def _fmt_money(val):
    if val is None:
        return "?"
    if val < 0:
        return f"-${abs(val):,.2f}"
    return f"${val:,.2f}"


def _fmt_int(val):
    if val is None:
        return "?"
    return f"{val:,}"


# --- Commands ---

def cmd_stats(con):
    """Dataset summary statistics."""
    r = con.execute("""
        SELECT
            count(*) as rows,
            count(DISTINCT billing_npi) as billing_npis,
            count(DISTINCT servicing_npi) as servicing_npis,
            count(DISTINCT hcpcs_code) as hcpcs_codes,
            min(claim_month) as first_month,
            max(claim_month) as last_month,
            sum(paid) as total_paid,
            sum(claims) as total_claims,
            sum(beneficiaries) as total_beneficiary_months,
            count(*) FILTER (WHERE servicing_npi IS NULL OR servicing_npi = '') as null_servicing,
            count(*) FILTER (WHERE paid < 0) as negative_paid_rows,
            sum(paid) FILTER (WHERE paid < 0) as negative_paid_total
        FROM m
    """).fetchone()

    result = {
        "rows": r[0],
        "unique_billing_npis": r[1],
        "unique_servicing_npis": r[2],
        "unique_hcpcs_codes": r[3],
        "first_month": r[4],
        "last_month": r[5],
        "total_paid": r[6],
        "total_claims": r[7],
        "total_beneficiary_months": r[8],
        "null_servicing_rows": r[9],
        "negative_paid_rows": r[10],
        "negative_paid_total": r[11],
    }

    return result


def _print_stats(s):
    print(f"\n  Medicaid Provider Spending Dataset (T-MSIS)")
    print(f"  {'='*50}")
    print(f"  Rows:              {_fmt_int(s['rows'])}")
    print(f"  Period:            {s['first_month']} to {s['last_month']}")
    print(f"  Total Paid:        {_fmt_money(s['total_paid'])}")
    print(f"  Total Claims:      {_fmt_int(s['total_claims'])}")
    print(f"  Beneficiary-months:{_fmt_int(s['total_beneficiary_months'])}")
    print(f"  Billing NPIs:      {_fmt_int(s['unique_billing_npis'])}")
    print(f"  Servicing NPIs:    {_fmt_int(s['unique_servicing_npis'])}")
    print(f"  HCPCS Codes:       {_fmt_int(s['unique_hcpcs_codes'])}")
    print(f"  Null servicing:    {_fmt_int(s['null_servicing_rows'])} rows ({100*s['null_servicing_rows']/s['rows']:.1f}%)")
    print(f"  Negative paid:     {_fmt_int(s['negative_paid_rows'])} rows ({_fmt_money(s['negative_paid_total'])})")


def cmd_top_billers(con, limit=50, year=None):
    """Top billing NPIs by total paid."""
    where = f"WHERE claim_month LIKE '{year}%'" if year else ""
    rows = con.execute(f"""
        SELECT
            billing_npi,
            sum(paid) as total_paid,
            sum(claims) as total_claims,
            sum(beneficiaries) as total_bene,
            count(DISTINCT hcpcs_code) as codes_billed,
            count(DISTINCT servicing_npi) as servicing_npis,
            count(DISTINCT claim_month) as active_months
        FROM m {where}
        GROUP BY billing_npi
        ORDER BY total_paid DESC
        LIMIT {limit}
    """).fetchall()

    records = []
    for r in rows:
        records.append({
            "billing_npi": r[0],
            "total_paid": r[1],
            "total_claims": r[2],
            "total_beneficiaries": r[3],
            "codes_billed": r[4],
            "servicing_npis": r[5],
            "active_months": r[6],
            "avg_per_claim": r[1] / r[2] if r[2] else 0,
        })
    return {"total": len(records), "records": records}


def _print_top_billers(data):
    print(f"\n  Top {data['total']} Billing NPIs by Total Paid")
    print(f"  {'='*90}")
    print(f"  {'#':>3} {'NPI':>12} {'Total Paid':>16} {'Claims':>12} {'Bene':>10} {'Codes':>5} {'Svc NPIs':>8} {'$/Claim':>10}")
    print(f"  {'-'*90}")
    for i, r in enumerate(data["records"], 1):
        print(f"  {i:>3} {r['billing_npi']:>12} {_fmt_money(r['total_paid']):>16} {_fmt_int(r['total_claims']):>12} {_fmt_int(r['total_beneficiaries']):>10} {r['codes_billed']:>5} {r['servicing_npis']:>8} {_fmt_money(r['avg_per_claim']):>10}")


def cmd_top_codes(con, limit=50, year=None):
    """Top HCPCS codes by total paid."""
    where = f"WHERE claim_month LIKE '{year}%'" if year else ""
    rows = con.execute(f"""
        SELECT
            hcpcs_code,
            sum(paid) as total_paid,
            sum(claims) as total_claims,
            sum(beneficiaries) as total_bene,
            count(DISTINCT billing_npi) as billing_npis,
            sum(paid) / NULLIF(sum(claims), 0) as avg_per_claim
        FROM m {where}
        GROUP BY hcpcs_code
        ORDER BY total_paid DESC
        LIMIT {limit}
    """).fetchall()

    records = []
    for r in rows:
        records.append({
            "hcpcs_code": r[0],
            "total_paid": r[1],
            "total_claims": r[2],
            "total_beneficiaries": r[3],
            "billing_npis": r[4],
            "avg_per_claim": r[5],
        })
    return {"total": len(records), "records": records}


def _print_top_codes(data):
    print(f"\n  Top {data['total']} HCPCS Codes by Total Paid")
    print(f"  {'='*90}")
    print(f"  {'#':>3} {'Code':>8} {'Total Paid':>16} {'Claims':>14} {'Bene':>12} {'Providers':>9} {'$/Claim':>10}")
    print(f"  {'-'*90}")
    for i, r in enumerate(data["records"], 1):
        print(f"  {i:>3} {r['hcpcs_code']:>8} {_fmt_money(r['total_paid']):>16} {_fmt_int(r['total_claims']):>14} {_fmt_int(r['total_beneficiaries']):>12} {_fmt_int(r['billing_npis']):>9} {_fmt_money(r['avg_per_claim']):>10}")


def cmd_provider(con, npi, timeline=False):
    """Look up a specific NPI (as billing or servicing provider)."""
    # Summary as billing provider
    billing = con.execute(f"""
        SELECT
            sum(paid) as total_paid,
            sum(claims) as total_claims,
            sum(beneficiaries) as total_bene,
            count(DISTINCT hcpcs_code) as codes_billed,
            count(DISTINCT servicing_npi) as servicing_npis,
            min(claim_month) as first_month,
            max(claim_month) as last_month,
            count(DISTINCT claim_month) as active_months
        FROM m WHERE billing_npi = '{npi}'
    """).fetchone()

    # Summary as servicing provider
    servicing = con.execute(f"""
        SELECT
            sum(paid) as total_paid,
            sum(claims) as total_claims,
            count(DISTINCT billing_npi) as billing_npis,
            count(DISTINCT hcpcs_code) as codes_billed,
            min(claim_month) as first_month,
            max(claim_month) as last_month
        FROM m WHERE servicing_npi = '{npi}'
    """).fetchone()

    # Top codes when billing
    top_codes = con.execute(f"""
        SELECT hcpcs_code, sum(paid) as total_paid, sum(claims) as total_claims
        FROM m WHERE billing_npi = '{npi}'
        GROUP BY hcpcs_code ORDER BY total_paid DESC LIMIT 10
    """).fetchall()

    # Network: who does this NPI bill for / who bills for them
    servicing_for = con.execute(f"""
        SELECT servicing_npi, sum(paid) as total_paid, sum(claims) as total_claims
        FROM m WHERE billing_npi = '{npi}' AND servicing_npi != '{npi}'
            AND servicing_npi IS NOT NULL AND servicing_npi != ''
        GROUP BY servicing_npi ORDER BY total_paid DESC LIMIT 10
    """).fetchall()

    billed_by = con.execute(f"""
        SELECT billing_npi, sum(paid) as total_paid, sum(claims) as total_claims
        FROM m WHERE servicing_npi = '{npi}' AND billing_npi != '{npi}'
        GROUP BY billing_npi ORDER BY total_paid DESC LIMIT 10
    """).fetchall()

    result = {
        "npi": npi,
        "as_billing": {
            "total_paid": billing[0],
            "total_claims": billing[1],
            "total_beneficiaries": billing[2],
            "codes_billed": billing[3],
            "servicing_npis": billing[4],
            "first_month": billing[5],
            "last_month": billing[6],
            "active_months": billing[7],
        },
        "as_servicing": {
            "total_paid": servicing[0],
            "total_claims": servicing[1],
            "billing_npis": servicing[2],
            "codes_billed": servicing[3],
            "first_month": servicing[4],
            "last_month": servicing[5],
        },
        "top_codes": [{"code": r[0], "paid": r[1], "claims": r[2]} for r in top_codes],
        "services_for": [{"npi": r[0], "paid": r[1], "claims": r[2]} for r in servicing_for],
        "billed_by": [{"npi": r[0], "paid": r[1], "claims": r[2]} for r in billed_by],
    }

    if timeline:
        tl = con.execute(f"""
            SELECT claim_month, sum(paid) as total_paid, sum(claims) as total_claims
            FROM m WHERE billing_npi = '{npi}'
            GROUP BY claim_month ORDER BY claim_month
        """).fetchall()
        result["timeline"] = [{"month": r[0], "paid": r[1], "claims": r[2]} for r in tl]

    return result


def _print_provider(data):
    b = data["as_billing"]
    s = data["as_servicing"]
    print(f"\n  NPI: {data['npi']}")
    print(f"  {'='*60}")
    print(f"  As Billing Provider:")
    print(f"    Total Paid:    {_fmt_money(b['total_paid'])}")
    print(f"    Claims:        {_fmt_int(b['total_claims'])}")
    print(f"    Beneficiaries: {_fmt_int(b['total_beneficiaries'])}")
    print(f"    HCPCS Codes:   {b['codes_billed']}")
    print(f"    Svc NPIs:      {b['servicing_npis']}")
    print(f"    Active:        {b['first_month']} to {b['last_month']} ({b['active_months']} months)")
    if s["total_paid"]:
        print(f"  As Servicing Provider:")
        print(f"    Total Paid:    {_fmt_money(s['total_paid'])}")
        print(f"    Billed by:     {s['billing_npis']} NPIs")

    if data["top_codes"]:
        print(f"\n  Top Codes:")
        for c in data["top_codes"]:
            print(f"    {c['code']:>8}  {_fmt_money(c['paid']):>16}  {_fmt_int(c['claims']):>12} claims")

    if data["services_for"]:
        print(f"\n  Bills for (servicing NPIs != self):")
        for n in data["services_for"]:
            print(f"    {n['npi']:>12}  {_fmt_money(n['paid']):>16}  {_fmt_int(n['claims']):>12} claims")

    if data["billed_by"]:
        print(f"\n  Billed by (other billing NPIs):")
        for n in data["billed_by"]:
            print(f"    {n['npi']:>12}  {_fmt_money(n['paid']):>16}  {_fmt_int(n['claims']):>12} claims")

    if "timeline" in data:
        print(f"\n  Monthly Timeline:")
        for t in data["timeline"]:
            bar_len = max(1, int(40 * t["paid"] / max(r["paid"] for r in data["timeline"]))) if data["timeline"] else 0
            print(f"    {t['month']}  {_fmt_money(t['paid']):>14}  {'#' * bar_len}")


def cmd_code(con, hcpcs, limit=20, year=None):
    """Look up a specific HCPCS code — top billers for that code."""
    where = f"AND claim_month LIKE '{year}%'" if year else ""
    rows = con.execute(f"""
        SELECT
            billing_npi,
            sum(paid) as total_paid,
            sum(claims) as total_claims,
            sum(beneficiaries) as total_bene,
            sum(paid) / NULLIF(sum(claims), 0) as avg_per_claim,
            min(claim_month) as first,
            max(claim_month) as last
        FROM m WHERE hcpcs_code = '{hcpcs}' {where}
        GROUP BY billing_npi ORDER BY total_paid DESC LIMIT {limit}
    """).fetchall()

    # Total for this code
    total = con.execute(f"""
        SELECT sum(paid), sum(claims), count(DISTINCT billing_npi)
        FROM m WHERE hcpcs_code = '{hcpcs}' {where}
    """).fetchone()

    records = []
    for r in rows:
        records.append({
            "billing_npi": r[0], "total_paid": r[1], "total_claims": r[2],
            "total_beneficiaries": r[3], "avg_per_claim": r[4],
            "first_month": r[5], "last_month": r[6],
        })
    return {
        "hcpcs_code": hcpcs,
        "total_paid": total[0], "total_claims": total[1], "total_providers": total[2],
        "records": records,
    }


def _print_code(data):
    print(f"\n  HCPCS Code: {data['hcpcs_code']}")
    print(f"  Total Paid: {_fmt_money(data['total_paid'])}  |  Claims: {_fmt_int(data['total_claims'])}  |  Providers: {_fmt_int(data['total_providers'])}")
    print(f"  {'='*95}")
    print(f"  {'#':>3} {'NPI':>12} {'Total Paid':>16} {'Claims':>12} {'Bene':>10} {'$/Claim':>10} {'Period'}")
    print(f"  {'-'*95}")
    for i, r in enumerate(data["records"], 1):
        print(f"  {i:>3} {r['billing_npi']:>12} {_fmt_money(r['total_paid']):>16} {_fmt_int(r['total_claims']):>12} {_fmt_int(r['total_beneficiaries']):>10} {_fmt_money(r['avg_per_claim']):>10} {r['first_month']}-{r['last_month']}")


def cmd_network(con, npi, depth=1):
    """Show billing network around an NPI."""
    # Direct connections
    edges = con.execute(f"""
        SELECT billing_npi, servicing_npi, sum(paid) as total_paid, sum(claims) as total_claims
        FROM m
        WHERE (billing_npi = '{npi}' OR servicing_npi = '{npi}')
            AND billing_npi != servicing_npi
            AND servicing_npi IS NOT NULL AND servicing_npi != ''
        GROUP BY billing_npi, servicing_npi
        ORDER BY total_paid DESC
    """).fetchall()

    result = {
        "center_npi": npi,
        "edges": [{"billing_npi": r[0], "servicing_npi": r[1], "paid": r[2], "claims": r[3]} for r in edges],
        "unique_connected_npis": len(set(r[0] for r in edges) | set(r[1] for r in edges)) - 1,
    }
    return result


def _print_network(data):
    print(f"\n  Network for NPI {data['center_npi']}")
    print(f"  Connected NPIs: {data['unique_connected_npis']}")
    print(f"  {'='*80}")
    for e in data["edges"]:
        direction = "bills for" if e["billing_npi"] == data["center_npi"] else "billed by"
        other = e["servicing_npi"] if e["billing_npi"] == data["center_npi"] else e["billing_npi"]
        print(f"  {direction:>10} {other:>12}  {_fmt_money(e['paid']):>16}  {_fmt_int(e['claims']):>12} claims")


def cmd_recoupments(con, limit=20):
    """Providers with largest negative payments (recoupments)."""
    rows = con.execute(f"""
        SELECT
            billing_npi,
            sum(paid) FILTER (WHERE paid < 0) as recouped,
            sum(paid) FILTER (WHERE paid >= 0) as positive_paid,
            sum(paid) as net_paid,
            count(*) FILTER (WHERE paid < 0) as neg_rows
        FROM m
        GROUP BY billing_npi
        HAVING sum(paid) FILTER (WHERE paid < 0) < 0
        ORDER BY recouped ASC
        LIMIT {limit}
    """).fetchall()

    records = []
    for r in rows:
        records.append({
            "billing_npi": r[0], "recouped": r[1], "positive_paid": r[2],
            "net_paid": r[3], "negative_rows": r[4],
        })
    return {"total": len(records), "records": records}


def _print_recoupments(data):
    print(f"\n  Top {data['total']} Providers by Recoupment (Negative Payments)")
    print(f"  {'='*85}")
    print(f"  {'#':>3} {'NPI':>12} {'Recouped':>16} {'Positive':>16} {'Net':>16} {'Neg Rows':>8}")
    print(f"  {'-'*85}")
    for i, r in enumerate(data["records"], 1):
        print(f"  {i:>3} {r['billing_npi']:>12} {_fmt_money(r['recouped']):>16} {_fmt_money(r['positive_paid']):>16} {_fmt_money(r['net_paid']):>16} {r['negative_rows']:>8}")


def cmd_yearly(con):
    """Yearly spending totals."""
    rows = con.execute("""
        SELECT
            claim_month[:4] as year,
            sum(paid) as total_paid,
            sum(claims) as total_claims,
            count(DISTINCT billing_npi) as providers,
            sum(paid) / NULLIF(sum(claims), 0) as avg_per_claim
        FROM m
        GROUP BY year ORDER BY year
    """).fetchall()

    records = []
    for r in rows:
        records.append({
            "year": r[0], "total_paid": r[1], "total_claims": r[2],
            "providers": r[3], "avg_per_claim": r[4],
        })
    return {"records": records}


def _print_yearly(data):
    print(f"\n  Yearly Spending Summary")
    print(f"  {'='*75}")
    print(f"  {'Year':>6} {'Total Paid':>16} {'Claims':>14} {'Providers':>10} {'$/Claim':>10}")
    print(f"  {'-'*75}")
    for r in data["records"]:
        print(f"  {r['year']:>6} {_fmt_money(r['total_paid']):>16} {_fmt_int(r['total_claims']):>14} {_fmt_int(r['providers']):>10} {_fmt_money(r['avg_per_claim']):>10}")


def cmd_anomalies(con, limit=50, min_paid=10_000_000):
    """Flag anomalous providers using multiple signals."""
    rows = con.execute(f"""
        WITH provider_stats AS (
            SELECT billing_npi,
                sum(paid) as total_paid,
                sum(claims) as total_claims,
                sum(paid)/NULLIF(sum(claims),0) as avg_per_claim,
                count(DISTINCT hcpcs_code) as code_count,
                count(DISTINCT servicing_npi) as svc_count,
                min(claim_month) as first_month,
                max(claim_month) as last_month,
                count(DISTINCT claim_month) as active_months
            FROM m
            GROUP BY billing_npi
            HAVING sum(paid) > {min_paid}
        ),
        top_code AS (
            SELECT billing_npi, hcpcs_code,
                sum(paid) as code_paid,
                ROW_NUMBER() OVER (PARTITION BY billing_npi ORDER BY sum(paid) DESC) as rn
            FROM m GROUP BY billing_npi, hcpcs_code
        ),
        code_medians AS (
            SELECT hcpcs_code,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY paid/NULLIF(claims,0)) as median_per_claim
            FROM m WHERE claims > 100
            GROUP BY hcpcs_code
        )
        SELECT ps.*,
            tc.hcpcs_code as top_code,
            tc.code_paid as top_code_paid,
            round(100.0 * tc.code_paid / ps.total_paid, 1) as top_code_pct,
            cm.median_per_claim as code_median,
            ps.avg_per_claim / NULLIF(cm.median_per_claim, 0) as cost_ratio,
            bp.org_name, bp.city, bp.state, bp.taxonomy_code
        FROM provider_stats ps
        LEFT JOIN top_code tc ON ps.billing_npi = tc.billing_npi AND tc.rn = 1
        LEFT JOIN code_medians cm ON tc.hcpcs_code = cm.hcpcs_code
        LEFT JOIN bp ON ps.billing_npi = bp.npi
        WHERE bp.entity_type = 2  -- organizations only
        ORDER BY
            -- Composite anomaly score: high concentration + high cost ratio + few codes
            (CASE WHEN tc.code_paid / ps.total_paid > 0.9 THEN 2 ELSE 0 END)
            + (CASE WHEN ps.avg_per_claim / NULLIF(cm.median_per_claim, 0) > 3 THEN 2 ELSE 0 END)
            + (CASE WHEN ps.code_count <= 3 THEN 1 ELSE 0 END)
            + (CASE WHEN ps.active_months < 36 THEN 1 ELSE 0 END)
            DESC,
            ps.total_paid DESC
        LIMIT {limit}
    """).fetchall()

    records = []
    for r in rows:
        records.append({
            "billing_npi": r[0], "total_paid": r[1], "total_claims": r[2],
            "avg_per_claim": r[3], "code_count": r[4], "svc_count": r[5],
            "first_month": r[6], "last_month": r[7], "active_months": r[8],
            "top_code": r[9], "top_code_paid": r[10], "top_code_pct": r[11],
            "code_median": r[12], "cost_ratio": r[13],
            "org_name": r[14], "city": r[15], "state": r[16], "taxonomy": r[17],
        })
    return {"total": len(records), "records": records}


def _print_anomalies(data):
    print(f"\n  Top {data['total']} Anomalous Providers (organizations, >{_fmt_money(10_000_000)} paid)")
    print(f"  {'='*120}")
    print(f"  {'#':>3} {'NPI':>12} {'Total Paid':>14} {'$/Cl':>8} {'Ratio':>5} {'Cd':>3} {'Mths':>4} {'Top%':>5} {'St':>2} Name")
    print(f"  {'-'*120}")
    for i, r in enumerate(data["records"], 1):
        ratio = f"{r['cost_ratio']:.1f}x" if r['cost_ratio'] else "?"
        name = (r['org_name'] or '?')[:45]
        print(f"  {i:>3} {r['billing_npi']:>12} {_fmt_money(r['total_paid']):>14} {_fmt_money(r['avg_per_claim']):>8} {ratio:>5} {r['code_count']:>3} {r['active_months']:>4} {r['top_code_pct']:>4}% {r['state'] or '?':>2} {name}")


def cmd_sql(con, query):
    """Run arbitrary SQL against the dataset (table alias: m)."""
    rows = con.execute(query).fetchall()
    cols = [desc[0] for desc in con.description]
    records = [dict(zip(cols, row)) for row in rows]
    return {"columns": cols, "total": len(records), "records": records}


def _print_sql(data):
    if not data["records"]:
        print("  No results")
        return
    cols = data["columns"]
    # Auto-width columns
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in data["records"])) for c in cols}
    header = "  ".join(f"{c:>{widths[c]}}" for c in cols)
    print(f"\n  {header}")
    print(f"  {'-' * len(header)}")
    for r in data["records"]:
        line = "  ".join(f"{str(r.get(c, '')):>{widths[c]}}" for c in cols)
        print(f"  {line}")
    print(f"\n  {data['total']} rows")


def main():
    parser = argparse.ArgumentParser(description="Query Medicaid Provider Spending data (T-MSIS 2018-2024)")
    sub = parser.add_subparsers(dest="command")

    # stats
    p = sub.add_parser("stats", help="Dataset summary statistics")
    add_output_args(p)

    # top-billers
    p = sub.add_parser("top-billers", help="Top billing NPIs by total paid")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--year", help="Filter to year (e.g. 2024)")
    add_output_args(p)

    # top-codes
    p = sub.add_parser("top-codes", help="Top HCPCS codes by total paid")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--year", help="Filter to year (e.g. 2024)")
    add_output_args(p)

    # provider
    p = sub.add_parser("provider", help="Look up specific NPI")
    p.add_argument("npi", help="NPI number")
    p.add_argument("--timeline", action="store_true", help="Include monthly timeline")
    add_output_args(p)

    # code
    p = sub.add_parser("code", help="Top billers for a specific HCPCS code")
    p.add_argument("hcpcs", help="HCPCS code (e.g. T1019)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--year", help="Filter to year")
    add_output_args(p)

    # network
    p = sub.add_parser("network", help="Billing network around an NPI")
    p.add_argument("npi", help="Center NPI")
    add_output_args(p)

    # recoupments
    p = sub.add_parser("recoupments", help="Providers with largest negative payments")
    p.add_argument("--limit", type=int, default=20)
    add_output_args(p)

    # yearly
    p = sub.add_parser("yearly", help="Yearly spending totals")
    add_output_args(p)

    # anomalies
    p = sub.add_parser("anomalies", help="Flag anomalous providers (high cost ratio, single-code concentration)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--min-paid", type=float, default=10_000_000, help="Minimum total paid threshold")
    add_output_args(p)

    # sql
    p = sub.add_parser("sql", help="Run arbitrary SQL (table: m, bp, sp, hcpcs)")
    p.add_argument("query", help="SQL query")
    add_output_args(p)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    con = _connect()

    if args.command == "stats":
        result = cmd_stats(con)
        if not write_output(result, args, summary="Dataset stats"):
            _print_stats(result)

    elif args.command == "top-billers":
        result = cmd_top_billers(con, limit=args.limit, year=args.year)
        if not write_output(result, args, summary=f"Top {args.limit} billers"):
            _print_top_billers(result)

    elif args.command == "top-codes":
        result = cmd_top_codes(con, limit=args.limit, year=args.year)
        if not write_output(result, args, summary=f"Top {args.limit} HCPCS codes"):
            _print_top_codes(result)

    elif args.command == "provider":
        result = cmd_provider(con, args.npi, timeline=args.timeline)
        if not write_output(result, args, summary=f"NPI {args.npi}"):
            _print_provider(result)

    elif args.command == "code":
        result = cmd_code(con, args.hcpcs, limit=args.limit, year=args.year)
        if not write_output(result, args, summary=f"HCPCS {args.hcpcs}"):
            _print_code(result)

    elif args.command == "network":
        result = cmd_network(con, args.npi)
        if not write_output(result, args, summary=f"Network for {args.npi}"):
            _print_network(result)

    elif args.command == "recoupments":
        result = cmd_recoupments(con, limit=args.limit)
        if not write_output(result, args, summary=f"Top {args.limit} recoupments"):
            _print_recoupments(result)

    elif args.command == "yearly":
        result = cmd_yearly(con)
        if not write_output(result, args, summary="Yearly totals"):
            _print_yearly(result)

    elif args.command == "anomalies":
        result = cmd_anomalies(con, limit=args.limit, min_paid=args.min_paid)
        if not write_output(result, args, summary=f"Top {args.limit} anomalies"):
            _print_anomalies(result)

    elif args.command == "sql":
        result = cmd_sql(con, args.query)
        if not write_output(result, args, summary=f"SQL query"):
            _print_sql(result)


if __name__ == "__main__":
    main()
