#!/usr/bin/env python3
"""Market data tool for financial forensics investigations.

Wraps yfinance for stock prices, company profiles, insider transactions,
and event correlation analysis. All data is ephemeral (WORKDIR-only).

No API key required. Rate limit: be polite (0.5s between multi-ticker calls).

Usage:
    python tools/query_market.py price PLTR --period 6mo
    python tools/query_market.py history SMCI --start 2024-01-01 --end 2024-12-31
    python tools/query_market.py profile PLTR
    python tools/query_market.py insider SMCI --limit 30
    python tools/query_market.py correlate SMCI --events events.json --window 5
"""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: uv add yfinance", file=sys.stderr)
    sys.exit(1)

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output


def _log(query, source, count):
    """Log search to prevent redundant queries."""
    try:
        from tools.lead_tracker import log_search
        log_search(query, source, count)
    except Exception:
        pass


def _safe_float(v):
    """Convert value to float, handling None/NaN."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return None


def _safe_int(v):
    """Convert scalar-like value to int, handling None/NaN/Series."""
    if hasattr(v, "iloc"):
        if len(v) == 0:
            return None
        v = v.iloc[0]
    f = _safe_float(v)
    return int(f) if f is not None else None


def _normalize_history_columns(hist):
    """Flatten yfinance multi-level columns for single-ticker queries."""
    if hist is not None and hasattr(hist.columns, "levels") and len(hist.columns.levels) > 1:
        hist = hist.copy()
        hist.columns = hist.columns.get_level_values(0)
    return hist


def cmd_price(args):
    """Current and recent price data for a ticker."""
    ticker = yf.Ticker(args.ticker)

    try:
        info = ticker.info or {}
    except Exception:
        info = {}

    try:
        hist = ticker.history(period=args.period)
    except Exception as e:
        print(f"WARNING: Could not fetch history for {args.ticker}: {e}", file=sys.stderr)
        hist = None

    hist = _normalize_history_columns(hist)

    result = {
        "ticker": args.ticker.upper(),
        "current": {
            "price": _safe_float(info.get("currentPrice") or info.get("regularMarketPrice")),
            "previous_close": _safe_float(info.get("previousClose") or info.get("regularMarketPreviousClose")),
            "day_high": _safe_float(info.get("dayHigh")),
            "day_low": _safe_float(info.get("dayLow")),
            "volume": info.get("volume"),
            "market_cap": info.get("marketCap"),
            "fifty_two_week_high": _safe_float(info.get("fiftyTwoWeekHigh")),
            "fifty_two_week_low": _safe_float(info.get("fiftyTwoWeekLow")),
        },
        "history": [],
    }

    if hist is not None and not hist.empty:
        for date, row in hist.iterrows():
            result["history"].append({
                "date": str(date.date()) if hasattr(date, "date") else str(date),
                "close": _safe_float(row.get("Close")),
                "volume": _safe_int(row.get("Volume")),
            })

    _log(args.ticker, "yfinance-price", len(result["history"]))

    if write_output(result, args, summary=f"{args.ticker} price ({args.period})"):
        return

    cur = result["current"]
    print(f"─── {args.ticker.upper()} ───")
    if cur["price"]:
        print(f"  Price:      ${cur['price']:,.2f}")
    if cur["previous_close"] and cur["price"]:
        change = cur["price"] - cur["previous_close"]
        pct = change / cur["previous_close"] * 100
        print(f"  Change:     ${change:+,.2f} ({pct:+.2f}%)")
    if cur["fifty_two_week_high"]:
        print(f"  52w Range:  ${cur['fifty_two_week_low']:,.2f} - ${cur['fifty_two_week_high']:,.2f}")
    if cur["market_cap"]:
        print(f"  Market Cap: ${cur['market_cap']:,.0f}")
    if result["history"]:
        print(f"  History:    {len(result['history'])} data points ({args.period})")


def cmd_history(args):
    """OHLCV data for a date range."""
    try:
        hist = yf.download(
            args.ticker, start=args.start, end=args.end,
            interval=args.interval, progress=False
        )
    except Exception as e:
        print(f"ERROR: Could not fetch history for {args.ticker}: {e}", file=sys.stderr)
        return

    hist = _normalize_history_columns(hist)

    data = []
    if hist is not None and not hist.empty:
        for date, row in hist.iterrows():
            data.append({
                "date": str(date.date()) if hasattr(date, "date") else str(date),
                "open": _safe_float(row.get("Open")),
                "high": _safe_float(row.get("High")),
                "low": _safe_float(row.get("Low")),
                "close": _safe_float(row.get("Close")),
                "volume": _safe_int(row.get("Volume")),
            })

    result = {
        "ticker": args.ticker.upper(),
        "start": args.start,
        "end": args.end,
        "interval": args.interval,
        "data": data,
    }

    _log(args.ticker, "yfinance-history", len(data))

    if write_output(result, args, summary=f"{args.ticker} history {args.start} to {args.end}"):
        return

    print(f"─── {args.ticker.upper()} | {args.start} to {args.end} ({len(data)} points) ───")
    for d in data[-10:]:
        print(f"  {d['date']}  O:{d['open']:>10,.2f}  H:{d['high']:>10,.2f}  L:{d['low']:>10,.2f}  C:{d['close']:>10,.2f}  V:{d['volume']:>12,}")
    if len(data) > 10:
        print(f"  ... showing last 10 of {len(data)} points")


def cmd_profile(args):
    """Company profile — sector, industry, SIC, market cap."""
    ticker = yf.Ticker(args.ticker)

    try:
        info = ticker.info or {}
    except Exception as e:
        print(f"ERROR: Could not fetch profile for {args.ticker}: {e}", file=sys.stderr)
        return

    result = {
        "ticker": args.ticker.upper(),
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "sic": info.get("sic"),
        "market_cap": info.get("marketCap"),
        "enterprise_value": info.get("enterpriseValue"),
        "employees": info.get("fullTimeEmployees"),
        "website": info.get("website"),
        "country": info.get("country"),
        "state": info.get("state"),
        "city": info.get("city"),
        "address": info.get("address1"),
        "description": (info.get("longBusinessSummary") or "")[:500],
    }

    _log(args.ticker, "yfinance-profile", 1)

    if write_output(result, args, summary=f"{args.ticker} profile"):
        return

    print(f"─── {result['name'] or args.ticker.upper()} ({args.ticker.upper()}) ───")
    for key in ["sector", "industry", "sic", "market_cap", "enterprise_value", "employees", "website", "country", "state", "city"]:
        val = result.get(key)
        if val:
            label = key.replace("_", " ").title()
            if key in ("market_cap", "enterprise_value"):
                print(f"  {label:20s} ${val:,.0f}")
            else:
                print(f"  {label:20s} {val}")


def cmd_insider(args):
    """Insider transactions from yfinance."""
    ticker = yf.Ticker(args.ticker)

    transactions = []
    try:
        insider_df = ticker.insider_transactions
        if insider_df is not None and not insider_df.empty:
            for _, row in insider_df.head(args.limit).iterrows():
                transactions.append({
                    "name": str(row.get("Insider") or row.get("insider", "")),
                    "relation": str(row.get("Position") or row.get("position", "")),
                    "date": str(row.get("Start Date") or row.get("startDate", "")),
                    "type": str(row.get("Transaction") or row.get("transaction", "")),
                    "shares": _safe_float(row.get("Shares") or row.get("shares")),
                    "value": _safe_float(row.get("Value") or row.get("value")),
                })
    except Exception as e:
        print(f"WARNING: Could not fetch insider data for {args.ticker}: {e}", file=sys.stderr)

    result = {
        "ticker": args.ticker.upper(),
        "transactions": transactions,
    }

    _log(args.ticker, "yfinance-insider", len(transactions))

    if write_output(result, args, summary=f"{args.ticker} insider transactions"):
        return

    print(f"─── {args.ticker.upper()} Insider Transactions ({len(transactions)}) ───")
    for t in transactions:
        val_str = f"${t['value']:,.0f}" if t.get("value") else "N/A"
        shares_str = f"{t['shares']:,.0f}" if t.get("shares") else "N/A"
        print(f"  {t['date'][:10]:12s} {t['type']:15s} {t['name'][:30]:30s} {shares_str:>12s} shares  {val_str:>15s}")


def cmd_correlate(args):
    """Correlate stock price with investigation events."""
    with open(args.events) as f:
        events = json.load(f)

    if isinstance(events, dict) and "key_dates" in events:
        events = events["key_dates"]

    window = args.window

    # Determine date range needed
    dates = []
    for e in events:
        d = e.get("date")
        if d:
            dates.append(d)
    if not dates:
        print("ERROR: No dates found in events file", file=sys.stderr)
        return

    min_date = min(dates)
    max_date = max(dates)

    # Extend range by window days
    start = (datetime.strptime(min_date, "%Y-%m-%d") - timedelta(days=window + 10)).strftime("%Y-%m-%d")
    end = (datetime.strptime(max_date, "%Y-%m-%d") + timedelta(days=window + 10)).strftime("%Y-%m-%d")

    try:
        hist = yf.download(args.ticker, start=start, end=end, progress=False)
    except Exception as e:
        print(f"ERROR: Could not fetch history for {args.ticker}: {e}", file=sys.stderr)
        return

    if hist is None or hist.empty:
        print(f"ERROR: No price data for {args.ticker} in range {start} to {end}", file=sys.stderr)
        return

    hist = _normalize_history_columns(hist)

    # Build date->price lookup
    prices = {}
    for date, row in hist.iterrows():
        d = str(date.date()) if hasattr(date, "date") else str(date)
        close_val = row.get("Close")
        vol_val = row.get("Volume")
        prices[d] = {
            "close": _safe_float(close_val),
            "volume": _safe_int(vol_val),
        }

    def _nearest_price(target_date, direction=0):
        """Find price on target_date or nearest trading day."""
        dt = datetime.strptime(target_date, "%Y-%m-%d")
        for offset in range(7):
            d = (dt + timedelta(days=offset * (1 if direction >= 0 else -1))).strftime("%Y-%m-%d")
            if d in prices:
                return d, prices[d]
        return None, None

    # Compute median volume for spike detection
    volumes = [p["volume"] for p in prices.values() if p.get("volume")]
    median_vol = sorted(volumes)[len(volumes) // 2] if volumes else 0

    correlations = []
    for event in events:
        event_date = event.get("date")
        if not event_date:
            continue

        before_date = (datetime.strptime(event_date, "%Y-%m-%d") - timedelta(days=window)).strftime("%Y-%m-%d")
        after_date = (datetime.strptime(event_date, "%Y-%m-%d") + timedelta(days=window)).strftime("%Y-%m-%d")

        _, before_data = _nearest_price(before_date, direction=1)
        _, after_data = _nearest_price(after_date, direction=-1)
        _, event_data = _nearest_price(event_date, direction=1)

        price_before = before_data["close"] if before_data else None
        price_after = after_data["close"] if after_data else None
        event_volume = event_data["volume"] if event_data else None

        pct_change = None
        if price_before and price_after and price_before != 0:
            pct_change = round((price_after - price_before) / price_before * 100, 2)

        volume_spike = False
        if event_volume and median_vol and median_vol > 0:
            volume_spike = event_volume > median_vol * 2

        notable = abs(pct_change) > 5 if pct_change is not None else False

        correlations.append({
            "event": event.get("event", ""),
            "date": event_date,
            "category": event.get("category", ""),
            "price_before": price_before,
            "price_after": price_after,
            "pct_change": pct_change,
            "volume_spike": volume_spike,
            "notable": notable,
        })

    result = {
        "ticker": args.ticker.upper(),
        "window_days": window,
        "events_analyzed": len(correlations),
        "notable_events": sum(1 for c in correlations if c["notable"]),
        "correlations": correlations,
    }

    _log(args.ticker, "yfinance-correlate", len(correlations))

    if write_output(result, args, summary=f"{args.ticker} event correlation ({result['notable_events']} notable)"):
        return

    print(f"─── {args.ticker.upper()} Event Correlation ({window}-day window) ───")
    for c in correlations:
        flag = " ***" if c["notable"] else ""
        vol = " [VOL SPIKE]" if c["volume_spike"] else ""
        pct = f"{c['pct_change']:+.2f}%" if c["pct_change"] is not None else "N/A"
        print(f"  {c['date']}  {pct:>8s}{vol}{flag}  {c['event'][:60]}")
    if result["notable_events"]:
        print(f"\n  {result['notable_events']} notable events (>5% price move within {window}-day window)")


def main():
    parser = argparse.ArgumentParser(description="Market data for financial forensics (yfinance)")
    sub = parser.add_subparsers(dest="command", required=True)

    # price
    p = sub.add_parser("price", help="Current and recent price data")
    p.add_argument("ticker", help="Stock ticker (e.g., PLTR)")
    p.add_argument("--period", default="1mo",
                   choices=["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"],
                   help="History period (default: 1mo)")
    add_output_args(p)

    # history
    p = sub.add_parser("history", help="OHLCV data for a date range")
    p.add_argument("ticker", help="Stock ticker")
    p.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    p.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    p.add_argument("--interval", default="1d", choices=["1d", "1wk", "1mo"],
                   help="Data interval (default: 1d)")
    add_output_args(p)

    # profile
    p = sub.add_parser("profile", help="Company profile (sector, industry, SIC, market cap)")
    p.add_argument("ticker", help="Stock ticker")
    add_output_args(p)

    # insider
    p = sub.add_parser("insider", help="Insider transactions")
    p.add_argument("ticker", help="Stock ticker")
    p.add_argument("--limit", type=int, default=20, help="Max transactions (default: 20)")
    add_output_args(p)

    # correlate
    p = sub.add_parser("correlate", help="Correlate price with investigation events")
    p.add_argument("ticker", help="Stock ticker")
    p.add_argument("--events", required=True, help="JSON file with events (key_dates format)")
    p.add_argument("--window", type=int, default=5, help="Days before/after event (default: 5)")
    add_output_args(p)

    args = parser.parse_args()
    if not hasattr(args, "output"):
        args.output = None
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "price": cmd_price,
        "history": cmd_history,
        "profile": cmd_profile,
        "insider": cmd_insider,
        "correlate": cmd_correlate,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
