#!/usr/bin/env python3
"""Financial ratio calculator for structured financial statement data.

Takes JSON financial statements (from query_edgar.py sections) and computes
standard ratios for forensic analysis: profitability, liquidity, efficiency,
solvency, and earnings quality. Flags anomalies based on configurable thresholds.

Usage:
    # Analyze a single statement
    python tools/financial_ratios.py analyze $WORKDIR/income.json $WORKDIR/balance.json

    # Analyze with cash flow for quality metrics
    python tools/financial_ratios.py analyze $WORKDIR/income.json $WORKDIR/balance.json --cashflow $WORKDIR/cashflow.json

    # Output as JSON for downstream tools
    python tools/financial_ratios.py analyze income.json balance.json --output $WORKDIR/ratios.json

    # Compare multiple periods with anomaly flags
    python tools/financial_ratios.py analyze income.json balance.json --cashflow cf.json --flag-anomalies
"""

import argparse
import json
import math
import sys

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output


def _load_statement(path):
    """Load a JSON financial statement file."""
    with open(path) as f:
        return json.load(f)


def _find_item(items, *concepts):
    """Find a line item by XBRL concept suffix. Returns {period: value} or {}.

    Matches concept names ending with '_<search_term>' to avoid false positives
    like 'PrepaidExpenseAndOtherAssetsCurrent' matching before 'AssetsCurrent'.
    """
    for item in items:
        concept = item.get("concept", "")
        for c in concepts:
            # Exact suffix match after underscore: concept ends with _<term> or equals <term>
            if concept.endswith("_" + c) or concept == c:
                return item.get("values", {})
    return {}


def _find_by_label(items, *label_fragments):
    """Fallback: find a line item by label substring (case-insensitive)."""
    for item in items:
        label = item.get("label", "").lower()
        for frag in label_fragments:
            if frag.lower() in label:
                return item.get("values", {})
    return {}


def _val(values, period):
    """Get a single period's value, or None."""
    v = values.get(period)
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _pct(numerator, denominator):
    """Safe percentage calculation."""
    if denominator is None or denominator == 0 or numerator is None:
        return None
    return round(numerator / denominator * 100, 2)


def _ratio(numerator, denominator):
    """Safe ratio calculation."""
    if denominator is None or denominator == 0 or numerator is None:
        return None
    return round(numerator / denominator, 4)


def _growth(current, prior):
    """Year-over-year growth rate as percentage."""
    if prior is None or prior == 0 or current is None:
        return None
    return round((current - prior) / abs(prior) * 100, 2)


def compute_ratios(income, balance, cashflow=None):
    """Compute financial ratios across all available periods.

    Args:
        income: Parsed income statement JSON (from query_edgar.py sections)
        balance: Parsed balance sheet JSON
        cashflow: Optional parsed cash flow statement JSON

    Returns:
        dict with metadata + per-period ratio computations + anomaly flags
    """
    inc_items = income.get("line_items", [])
    bs_items = balance.get("line_items", [])
    cf_items = (cashflow or {}).get("line_items", [])

    # Collect all periods across statements
    all_periods = sorted(set(
        income.get("periods", []) +
        balance.get("periods", []) +
        (cashflow or {}).get("periods", [])
    ))

    # Map standard XBRL concepts (suffix match after underscore)
    # Income statement
    revenue = _find_item(inc_items, "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet")
    cogs = _find_item(inc_items, "CostOfRevenue", "CostOfGoodsAndServicesSold")
    gross_profit = _find_item(inc_items, "GrossProfit")
    operating_income = _find_item(inc_items, "OperatingIncomeLoss")
    net_income = _find_item(inc_items, "NetIncomeLoss", "ProfitLoss")
    total_opex = _find_item(inc_items, "OperatingExpenses")
    rd_expense = _find_item(inc_items, "ResearchAndDevelopmentExpense")
    sga_expense = _find_item(inc_items, "SellingGeneralAndAdministrativeExpense")
    ga_expense = _find_item(inc_items, "GeneralAndAdministrativeExpense")
    interest_expense = _find_item(inc_items, "InterestExpense", "InterestExpenseNonoperating")

    # Balance sheet (exact concept suffixes to avoid partial matches)
    total_assets = _find_item(bs_items, "Assets")
    total_current_assets = _find_item(bs_items, "AssetsCurrent")
    total_liabilities = _find_item(bs_items, "Liabilities")
    total_current_liab = _find_item(bs_items, "LiabilitiesCurrent")
    total_equity = _find_item(bs_items, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "StockholdersEquity")
    cash = _find_item(bs_items, "CashAndCashEquivalentsAtCarryingValue")
    receivables = _find_item(bs_items, "AccountsReceivableNetCurrent")
    inventory = _find_item(bs_items, "InventoryNet")
    goodwill = _find_item(bs_items, "Goodwill")
    accounts_payable = _find_item(bs_items, "AccountsPayableCurrent")

    # Cash flow
    operating_cf = _find_item(cf_items, "NetCashProvidedByUsedInOperatingActivities")
    investing_cf = _find_item(cf_items, "NetCashProvidedByUsedInInvestingActivities")
    financing_cf = _find_item(cf_items, "NetCashProvidedByUsedInFinancingActivities")
    depreciation = _find_item(cf_items, "DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet")

    # Label-based fallback for concepts that vary across companies
    if not total_assets:
        total_assets = _find_by_label(bs_items, "total assets")
    if not total_liabilities:
        total_liabilities = _find_by_label(bs_items, "total liabilities")
    if not total_equity:
        total_equity = _find_by_label(bs_items, "total stockholders' equity", "total equity")
    if not operating_cf:
        operating_cf = _find_by_label(cf_items, "net cash provided by operating", "net cash used in operating")
    if not depreciation:
        depreciation = _find_by_label(cf_items, "depreciation and amortization")

    period_results = []
    for i, period in enumerate(all_periods):
        rev = _val(revenue, period)
        cg = _val(cogs, period)
        gp = _val(gross_profit, period)
        oi = _val(operating_income, period)
        ni = _val(net_income, period)
        ta = _val(total_assets, period)
        tca = _val(total_current_assets, period)
        tl = _val(total_liabilities, period)
        tcl = _val(total_current_liab, period)
        te = _val(total_equity, period)
        c = _val(cash, period)
        ar = _val(receivables, period)
        inv = _val(inventory, period)
        gw = _val(goodwill, period)
        ap = _val(accounts_payable, period)
        ocf = _val(operating_cf, period)
        dep = _val(depreciation, period)
        ie = _val(interest_expense, period)

        # Prior period for growth calcs
        prior = all_periods[i - 1] if i > 0 else None
        prev_rev = _val(revenue, prior) if prior else None
        prev_ar = _val(receivables, prior) if prior else None
        prev_inv = _val(inventory, prior) if prior else None

        ratios = {"period": period}

        # Profitability
        ratios["gross_margin_pct"] = _pct(gp, rev)
        ratios["operating_margin_pct"] = _pct(oi, rev)
        ratios["net_margin_pct"] = _pct(ni, rev)
        ratios["roe_pct"] = _pct(ni, te)
        ratios["roa_pct"] = _pct(ni, ta)

        # Growth
        ratios["revenue_growth_pct"] = _growth(rev, prev_rev)

        # Liquidity
        ratios["current_ratio"] = _ratio(tca, tcl)
        if tca is not None and inv is not None and tcl is not None and tcl != 0:
            ratios["quick_ratio"] = round((tca - (inv or 0)) / tcl, 4)
        else:
            ratios["quick_ratio"] = None

        # Solvency
        ratios["debt_to_equity"] = _ratio(tl, te)
        if ie is not None and oi is not None and ie != 0:
            ratios["interest_coverage"] = round(oi / abs(ie), 2)
        else:
            ratios["interest_coverage"] = None

        # Efficiency
        if rev and ar:
            avg_ar = (ar + (prev_ar or ar)) / 2
            ratios["receivables_turnover"] = _ratio(rev, avg_ar)
            ratios["days_sales_outstanding"] = round(365 / ratios["receivables_turnover"], 1) if ratios["receivables_turnover"] else None
        else:
            ratios["receivables_turnover"] = None
            ratios["days_sales_outstanding"] = None

        if cg and inv:
            avg_inv = (inv + (prev_inv or inv)) / 2
            ratios["inventory_turnover"] = _ratio(cg, avg_inv)
            ratios["days_inventory"] = round(365 / ratios["inventory_turnover"], 1) if ratios["inventory_turnover"] else None
        else:
            ratios["inventory_turnover"] = None
            ratios["days_inventory"] = None

        # Earnings quality (requires cash flow)
        if ocf is not None and ni is not None:
            ratios["cash_conversion"] = _ratio(ocf, ni) if ni != 0 else None
            # Accruals ratio: (net income - operating CF) / total assets
            if ta and ta != 0:
                ratios["accruals_ratio_pct"] = _pct(ni - ocf, ta)
            else:
                ratios["accruals_ratio_pct"] = None
        else:
            ratios["cash_conversion"] = None
            ratios["accruals_ratio_pct"] = None

        # EBITDA proxy (operating income + depreciation)
        if oi is not None and dep is not None:
            ratios["ebitda"] = oi + dep
            ratios["ebitda_margin_pct"] = _pct(oi + dep, rev)
        else:
            ratios["ebitda"] = None
            ratios["ebitda_margin_pct"] = None

        # Composition
        ratios["goodwill_to_assets_pct"] = _pct(gw, ta)
        ratios["receivables_growth_pct"] = _growth(ar, prev_ar) if ar and prev_ar else None
        ratios["inventory_growth_pct"] = _growth(inv, prev_inv) if inv and prev_inv else None

        # Key absolute values for context
        ratios["revenue"] = rev
        ratios["net_income"] = ni
        ratios["operating_cf"] = ocf
        ratios["total_assets"] = ta
        ratios["total_equity"] = te

        period_results.append(ratios)

    # Anomaly detection
    anomalies = _detect_anomalies(period_results)

    return {
        "company": income.get("company") or balance.get("company"),
        "form": income.get("form") or balance.get("form"),
        "filing_date": income.get("filing_date") or balance.get("filing_date"),
        "periods": all_periods,
        "ratios": period_results,
        "anomalies": anomalies,
    }


def _detect_anomalies(period_results):
    """Flag financial anomalies across periods."""
    anomalies = []

    if len(period_results) < 2:
        return anomalies

    for i in range(1, len(period_results)):
        curr = period_results[i]
        prev = period_results[i - 1]
        period = curr["period"]

        # Revenue growing faster than receivables should shrink DSO
        rev_g = curr.get("revenue_growth_pct")
        ar_g = curr.get("receivables_growth_pct")
        if rev_g is not None and ar_g is not None and ar_g > rev_g + 20:
            anomalies.append({
                "period": period,
                "type": "receivables_outpacing_revenue",
                "severity": "high",
                "detail": f"Receivables grew {ar_g}% vs revenue {rev_g}% — potential collection issues or channel stuffing",
            })

        # Inventory growing faster than COGS
        inv_g = curr.get("inventory_growth_pct")
        if rev_g is not None and inv_g is not None and inv_g > rev_g + 20:
            anomalies.append({
                "period": period,
                "type": "inventory_outpacing_revenue",
                "severity": "medium",
                "detail": f"Inventory grew {inv_g}% vs revenue {rev_g}% — potential obsolescence or demand slowdown",
            })

        # Net income positive but operating cash flow negative
        ni = curr.get("net_income")
        ocf = curr.get("operating_cf")
        if ni is not None and ocf is not None and ni > 0 and ocf < 0:
            anomalies.append({
                "period": period,
                "type": "earnings_cash_divergence",
                "severity": "high",
                "detail": f"Net income ${ni:,.0f} but operating cash flow ${ocf:,.0f} — earnings quality concern",
            })

        # Accruals ratio > 10%
        accruals = curr.get("accruals_ratio_pct")
        if accruals is not None and accruals > 10:
            anomalies.append({
                "period": period,
                "type": "high_accruals",
                "severity": "medium",
                "detail": f"Accruals ratio {accruals}% — earnings may not be sustainable",
            })

        # Gross margin compression > 5pp
        gm_curr = curr.get("gross_margin_pct")
        gm_prev = prev.get("gross_margin_pct")
        if gm_curr is not None and gm_prev is not None and (gm_prev - gm_curr) > 5:
            anomalies.append({
                "period": period,
                "type": "margin_compression",
                "severity": "medium",
                "detail": f"Gross margin fell from {gm_prev}% to {gm_curr}% ({gm_prev - gm_curr:.1f}pp compression)",
            })

        # Pass-through entity indicator: gross margin < 5%
        if gm_curr is not None and gm_curr < 5:
            anomalies.append({
                "period": period,
                "type": "pass_through_indicator",
                "severity": "high",
                "detail": f"Gross margin only {gm_curr}% — may indicate pass-through entity, not real operations",
            })

        # Goodwill > 40% of total assets
        gw_pct = curr.get("goodwill_to_assets_pct")
        if gw_pct is not None and gw_pct > 40:
            anomalies.append({
                "period": period,
                "type": "high_goodwill",
                "severity": "medium",
                "detail": f"Goodwill is {gw_pct}% of total assets — impairment risk",
            })

    return anomalies


def cmd_analyze(args):
    """Compute ratios from financial statement JSON files."""
    income = _load_statement(args.income)
    balance = _load_statement(args.balance)
    cashflow = _load_statement(args.cashflow) if args.cashflow else None

    result = compute_ratios(income, balance, cashflow)

    if write_output(result, args, summary=f"ratios for {result.get('company', 'unknown')}"):
        # Print anomalies to stdout even when writing to file
        anomalies = result.get("anomalies", [])
        if anomalies:
            print(f"\n{len(anomalies)} anomalies detected:")
            for a in anomalies:
                print(f"  [{a['severity'].upper()}] {a['period']}: {a['detail']}")
        return

    # Pretty-print to stdout
    print(f"─── Financial Ratios: {result.get('company', 'Unknown')} ───")
    print(f"    Filing: {result.get('form')} dated {result.get('filing_date')}")
    print()

    for r in result["ratios"]:
        print(f"  Period: {r['period']}")
        print(f"    Revenue:         ${r['revenue']:>15,.0f}" if r.get("revenue") else "    Revenue:         N/A")
        print(f"    Net Income:      ${r['net_income']:>15,.0f}" if r.get("net_income") else "    Net Income:      N/A")
        print(f"    Operating CF:    ${r['operating_cf']:>15,.0f}" if r.get("operating_cf") else "    Operating CF:    N/A")
        print()
        print(f"    Gross Margin:    {r['gross_margin_pct']:>8.1f}%" if r.get("gross_margin_pct") is not None else "    Gross Margin:         N/A")
        print(f"    Operating Margin:{r['operating_margin_pct']:>8.1f}%" if r.get("operating_margin_pct") is not None else "    Operating Margin:     N/A")
        print(f"    Net Margin:      {r['net_margin_pct']:>8.1f}%" if r.get("net_margin_pct") is not None else "    Net Margin:           N/A")
        print(f"    ROE:             {r['roe_pct']:>8.1f}%" if r.get("roe_pct") is not None else "    ROE:                  N/A")
        print(f"    Revenue Growth:  {r['revenue_growth_pct']:>8.1f}%" if r.get("revenue_growth_pct") is not None else "    Revenue Growth:       N/A")
        print()
        print(f"    Current Ratio:   {r['current_ratio']:>8.2f}" if r.get("current_ratio") is not None else "    Current Ratio:        N/A")
        print(f"    Quick Ratio:     {r['quick_ratio']:>8.2f}" if r.get("quick_ratio") is not None else "    Quick Ratio:          N/A")
        print(f"    D/E Ratio:       {r['debt_to_equity']:>8.2f}" if r.get("debt_to_equity") is not None else "    D/E Ratio:            N/A")
        print()
        print(f"    AR Turnover:     {r['receivables_turnover']:>8.2f}" if r.get("receivables_turnover") is not None else "    AR Turnover:          N/A")
        print(f"    DSO:             {r['days_sales_outstanding']:>8.1f} days" if r.get("days_sales_outstanding") is not None else "    DSO:                  N/A")
        print(f"    Inv Turnover:    {r['inventory_turnover']:>8.2f}" if r.get("inventory_turnover") is not None else "    Inv Turnover:         N/A")
        print(f"    Days Inventory:  {r['days_inventory']:>8.1f} days" if r.get("days_inventory") is not None else "    Days Inventory:       N/A")
        print()
        if r.get("cash_conversion") is not None:
            print(f"    Cash Conversion: {r['cash_conversion']:>8.2f}x")
        if r.get("accruals_ratio_pct") is not None:
            print(f"    Accruals Ratio:  {r['accruals_ratio_pct']:>8.1f}%")
        print(f"    {'─' * 40}")

    anomalies = result.get("anomalies", [])
    if anomalies:
        print(f"\n  {len(anomalies)} ANOMALIES DETECTED:")
        for a in anomalies:
            print(f"    [{a['severity'].upper()}] {a['period']}: {a['detail']}")
    else:
        print("\n  No anomalies detected.")


FORENSIC_NOTES = {
    "gross_margin_pct": {
        "above": "Software-heavy model or cost misclassification (expenses in opex not COGS)",
        "below": "Pass-through entity, commoditized product, or heavy hardware mix",
    },
    "operating_margin_pct": {
        "above": "Operational efficiency, underinvestment, or one-time gains inflating margin",
        "below": "Heavy investment phase, inefficiency, or SBC-heavy compensation structure",
    },
    "net_margin_pct": {
        "above": "Tax efficiency (NOLs, jurisdictional arbitrage) or one-time gains",
        "below": "Structural losses, debt burden, or impairment charges",
    },
    "days_sales_outstanding": {
        "above": "Collection issues, aggressive revenue recognition, or channel stuffing",
        "below": "Cash-intensive business model or prepaid customer base",
    },
    "accruals_ratio_pct": {
        "above": "Earnings quality concern — income not converting to cash",
        "below": "Strong cash conversion — operating cash exceeds reported earnings",
    },
    "debt_to_equity": {
        "above": "Leverage risk, potential covenant pressure, or acquisition-funded growth",
        "below": "Underleveraged — may indicate inefficient capital structure or cash hoarding",
    },
    "current_ratio": {
        "above": "Excess liquidity — inefficient asset deployment or defensive cash position",
        "below": "Liquidity risk — may struggle to meet short-term obligations",
    },
    "cash_conversion": {
        "above": "Cash generation exceeds reported earnings — strong quality",
        "below": "Earnings not converting to cash — working capital consumption or manipulation",
    },
    "goodwill_to_assets_pct": {
        "above": "Acquisition-heavy strategy with impairment risk",
        "below": "Organic growth — minimal acquisition premium on balance sheet",
    },
    "roe_pct": {
        "above": "High returns may indicate leverage effect, small equity base, or unsustainable profitability",
        "below": "Low returns on equity — value destruction or heavy reinvestment phase",
    },
    "revenue_growth_pct": {
        "above": "Rapid growth may mask underlying quality issues or unsustainable acquisition-driven expansion",
        "below": "Stagnation or market share loss",
    },
}


def compare_multiple(ratio_files):
    """Compare financial ratios across multiple companies.

    Takes a list of paths to ratio JSON files (output of compute_ratios/cmd_analyze).
    Returns comparison matrix, medians, and statistical outliers with forensic notes.
    """
    companies = []
    latest_periods = {}
    all_ratios = {}

    # Key ratios to compare
    compare_keys = [
        "gross_margin_pct", "operating_margin_pct", "net_margin_pct",
        "roe_pct", "revenue_growth_pct",
        "current_ratio", "debt_to_equity",
        "days_sales_outstanding", "receivables_turnover",
        "inventory_turnover", "days_inventory",
        "cash_conversion", "accruals_ratio_pct",
        "goodwill_to_assets_pct",
    ]

    for path in ratio_files:
        data = _load_statement(path)
        company = data.get("company", path)
        ratios_list = data.get("ratios", [])
        if not ratios_list:
            continue

        # Use most recent period
        latest = ratios_list[-1]
        companies.append(company)
        latest_periods[company] = latest.get("period", "unknown")
        all_ratios[company] = latest

    if len(companies) < 2:
        return {"error": "Need at least 2 companies to compare"}

    # Build comparison matrix
    matrix = {}
    for key in compare_keys:
        values = {}
        for company in companies:
            v = all_ratios[company].get(key)
            if v is not None:
                values[company] = v
        if values:
            matrix[key] = values

    # Compute medians
    medians = {}
    for key, values in matrix.items():
        vals = sorted(values.values())
        n = len(vals)
        if n == 0:
            continue
        if n % 2 == 0:
            medians[key] = round((vals[n // 2 - 1] + vals[n // 2]) / 2, 4)
        else:
            medians[key] = vals[n // 2]

    # Detect outliers
    outliers = []
    for key, values in matrix.items():
        vals = list(values.values())
        n = len(vals)
        if n < 2:
            continue

        median = medians.get(key)
        if median is None:
            continue

        if n >= 5:
            # Use standard deviation for larger groups
            mean = sum(vals) / n
            variance = sum((v - mean) ** 2 for v in vals) / n
            stdev = variance ** 0.5
            threshold = 2.0
        else:
            # Range-based for small groups
            val_range = max(vals) - min(vals)
            stdev = val_range / 2 if val_range > 0 else 1
            threshold = 1.5

        if stdev == 0:
            continue

        for company, value in values.items():
            z = (value - median) / stdev
            if abs(z) >= threshold:
                direction = "above" if value > median else "below"
                note = FORENSIC_NOTES.get(key, {}).get(direction, "")
                outliers.append({
                    "company": company,
                    "ratio": key,
                    "value": round(value, 2),
                    "median": round(median, 2),
                    "z_score": round(z, 2),
                    "direction": direction,
                    "forensic_note": note,
                })

    # Count anomalies per company
    anomaly_counts = {c: 0 for c in companies}
    for o in outliers:
        anomaly_counts[o["company"]] = anomaly_counts.get(o["company"], 0) + 1

    return {
        "companies": companies,
        "latest_periods": latest_periods,
        "matrix": matrix,
        "medians": medians,
        "outliers": outliers,
        "anomaly_counts": anomaly_counts,
    }


def cmd_compare(args):
    """Compare ratios across multiple companies."""
    result = compare_multiple(args.files)

    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return

    if write_output(result, args, summary=f"comparison of {len(result['companies'])} companies"):
        outliers = result.get("outliers", [])
        if outliers:
            print(f"\n{len(outliers)} outliers detected:")
            for o in outliers:
                print(f"  {o['company']:20s} {o['ratio']:25s} {o['value']:>8.1f} vs median {o['median']:>8.1f} ({o['direction']})")
        return

    # Pretty-print comparison matrix
    companies = result["companies"]
    print(f"─── Peer Comparison ({len(companies)} companies) ───")
    print()

    # Header
    header = f"  {'Ratio':28s}"
    for c in companies:
        header += f" {c[:12]:>12s}"
    header += f" {'Median':>12s}"
    print(header)
    print(f"  {'─' * (28 + 13 * (len(companies) + 1))}")

    for key, values in result["matrix"].items():
        label = key.replace("_", " ").replace("pct", "%").title()
        row = f"  {label:28s}"
        for c in companies:
            v = values.get(c)
            if v is not None:
                row += f" {v:>12.1f}"
            else:
                row += f" {'N/A':>12s}"
        med = result["medians"].get(key)
        row += f" {med:>12.1f}" if med is not None else f" {'N/A':>12s}"
        print(row)

    outliers = result.get("outliers", [])
    if outliers:
        print(f"\n  {len(outliers)} OUTLIERS:")
        for o in outliers:
            print(f"    {o['company']:20s} {o['ratio']:25s} {o['value']:>8.1f} vs median {o['median']:>8.1f} (z={o['z_score']:+.1f})")
            if o.get("forensic_note"):
                print(f"    {'':20s} → {o['forensic_note']}")
    else:
        print("\n  No statistical outliers detected.")


def main():
    parser = argparse.ArgumentParser(description="Financial ratio calculator for SEC filings")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="Compute ratios from financial statement JSON files")
    p.add_argument("income", help="Path to income statement JSON (from edgar sections)")
    p.add_argument("balance", help="Path to balance sheet JSON (from edgar sections)")
    p.add_argument("--cashflow", help="Path to cash flow statement JSON")
    add_output_args(p)

    p = sub.add_parser("compare", help="Compare ratios across multiple companies")
    p.add_argument("files", nargs="+", help="Ratio JSON files (from analyze command)")
    add_output_args(p)

    args = parser.parse_args()
    handlers = {
        "analyze": cmd_analyze,
        "compare": cmd_compare,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
