#!/usr/bin/env python3
"""
Dune Analytics API wrapper for blockchain analytics queries.

Retrieves pre-built community analytics data from Dune's SQL query engine.
Useful for analyzing token holder distributions, on-chain financial flows,
stablecoin activity, and wallet-level transaction patterns.

API: https://api.dune.com/api/v1/
Auth: API key required. Set DUNE_API_KEY in .env.
Rate limits: Free tier — 2,500 credits/month, 10 req/min. 1 req/sec enforced here.
Credits: Each "Get Results" call costs datapoints returned (1 credit per 1 datapoint).

Usage:
    python tools/query_dune.py results 4166026
    python tools/query_dune.py results 4166026 --limit 50
    python tools/query_dune.py execute 4166026
    python tools/query_dune.py execute 4166026 --params "wallet=0xabc..."
    python tools/query_dune.py status 01HKZJ2683PHF9Q9PHHQ8FW4Q1
    python tools/query_dune.py cancel 01HKZJ2683PHF9Q9PHHQ8FW4Q1
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

BASE_URL = "https://api.dune.com/api/v1"

# Known community query IDs for investigation-relevant dashboards.
# These are public queries maintained by community analysts on dune.com.
# Query IDs sourced from dune.com dashboard URLs — may change if authors
# fork or delete. Verify with `results <ID>` before relying on them.
KNOWN_QUERIES = {
    "wlfi-holders": 4166026,       # World Liberty Financial (WLFI) token holders
    # Dashboard: https://dune.com/queries/4166026/7011557
    # Also: https://dune.com/seoul/wlfi (full WLFI dashboard)

    # USD1 stablecoin dashboard: https://dune.com/seoul/usd1
    # TRUMP memecoin dashboard: https://dune.com/magacoin/trump
    # Individual query IDs for USD1 and TRUMP sub-queries require
    # extracting from the dashboard JS — add here once confirmed.
}

# Polling configuration for execute-and-wait
POLL_INTERVAL_SEC = 5
POLL_TIMEOUT_SEC = 300  # 5 minutes

# Rate limiting
_last_request_time = 0.0


def _get_api_key():
    """Get Dune API key from environment."""
    key = os.environ.get("DUNE_API_KEY")
    if not key:
        print(
            "ERROR: DUNE_API_KEY not set. Add DUNE_API_KEY=<key> to .env "
            "or export it. Get a free key at https://dune.com/settings/api",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _rate_limit():
    """Enforce 1 request per second."""
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_request_time = time.time()


def _fetch(endpoint, params=None, method="GET", body=None, timeout=30):
    """Fetch from Dune API with authentication and rate limiting."""
    _rate_limit()

    url = f"{BASE_URL}{endpoint}"
    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"

    headers = {
        "X-Dune-API-Key": _get_api_key(),
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    }

    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    req = Request(url, data=data, headers=headers, method=method)

    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
            if not text.strip():
                return {}
            return json.loads(text)
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:500]
        except Exception:
            pass
        if e.code == 401:
            print("ERROR: Invalid Dune API key.", file=sys.stderr)
        elif e.code == 402:
            print("ERROR: Dune API credits exhausted or paid plan required.", file=sys.stderr)
        elif e.code == 404:
            print(f"ERROR: Query or execution not found (404): {err_body}", file=sys.stderr)
        elif e.code == 429:
            print("ERROR: Dune rate limit exceeded. Wait and retry.", file=sys.stderr)
        else:
            print(f"ERROR: Dune API returned {e.code}: {err_body}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"ERROR: Network error: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print("ERROR: Dune returned non-JSON response.", file=sys.stderr)
        sys.exit(1)


def _resolve_query_id(query_id_or_alias):
    """Resolve a query ID from either a numeric ID or a known alias."""
    # Check if it's a known alias
    if query_id_or_alias in KNOWN_QUERIES:
        resolved = KNOWN_QUERIES[query_id_or_alias]
        print(f"Resolved alias '{query_id_or_alias}' -> query {resolved}")
        return str(resolved)
    # Otherwise treat as numeric
    return str(query_id_or_alias)


def _format_table(rows, max_col_width=40, max_rows=None):
    """Format rows as a text table."""
    if not rows:
        print("  (no rows)")
        return

    display_rows = rows[:max_rows] if max_rows else rows

    # Collect all column names
    columns = []
    seen = set()
    for row in display_rows:
        for k in row:
            if k not in seen:
                columns.append(k)
                seen.add(k)

    # Compute column widths
    widths = {}
    for col in columns:
        header_w = len(str(col))
        max_val_w = 0
        for row in display_rows:
            val = str(row.get(col, ""))
            if len(val) > max_col_width:
                val = val[:max_col_width - 3] + "..."
            max_val_w = max(max_val_w, len(val))
        widths[col] = min(max(header_w, max_val_w), max_col_width)

    # Print header
    header = " | ".join(str(col)[:widths[col]].ljust(widths[col]) for col in columns)
    print(f"  {header}")
    sep = "-+-".join("-" * widths[col] for col in columns)
    print(f"  {sep}")

    # Print rows
    for row in display_rows:
        vals = []
        for col in columns:
            val = str(row.get(col, ""))
            if len(val) > max_col_width:
                val = val[:max_col_width - 3] + "..."
            vals.append(val.ljust(widths[col]))
        print(f"  {' | '.join(vals)}")

    if max_rows and len(rows) > max_rows:
        print(f"\n  ... {len(rows) - max_rows} more rows (use --limit to adjust)")


# -- Commands ----------------------------------------------------------------


def cmd_results(args):
    """Get latest results from a saved Dune query."""
    query_id = _resolve_query_id(args.query_id)

    params = {}
    if args.limit:
        params["limit"] = args.limit
    if args.offset:
        params["offset"] = args.offset
    if args.columns:
        params["columns"] = args.columns
    if args.filters:
        params["filters"] = args.filters
    if args.sort_by:
        params["sort_by"] = args.sort_by

    data = _fetch(f"/query/{query_id}/results", params=params, timeout=60)

    state = data.get("state", "unknown")
    metadata = data.get("result", {}).get("metadata", {})
    rows = data.get("result", {}).get("rows", [])
    total = metadata.get("total_row_count", len(rows))
    returned = metadata.get("row_count", len(rows))

    summary = (
        f"Dune query {query_id}: {returned}/{total} rows, "
        f"state={state}"
    )

    if write_output(data, args, summary=summary):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    # Human-readable output
    print(f"Query {query_id} — state: {state}")
    exec_id = data.get("execution_id", "N/A")
    print(f"Execution: {exec_id}")

    if metadata:
        print(f"Rows: {returned} returned / {total} total")
        exec_ms = metadata.get("execution_time_millis")
        if exec_ms:
            print(f"Execution time: {exec_ms / 1000:.1f}s")
        cols = metadata.get("column_names", [])
        if cols:
            print(f"Columns: {', '.join(cols)}")

    expires = data.get("expires_at")
    if expires:
        print(f"Expires: {expires}")

    print()
    display_limit = args.limit if args.limit else 30
    _format_table(rows, max_rows=display_limit)

    next_uri = data.get("next_uri")
    if next_uri:
        next_offset = data.get("next_offset", "?")
        print(f"\nMore results available (next_offset={next_offset})")


def cmd_execute(args):
    """Execute a query and wait for results."""
    query_id = _resolve_query_id(args.query_id)

    # Build request body
    body = {}
    if args.params:
        query_params = {}
        for param in args.params:
            if "=" not in param:
                print(f"ERROR: Invalid param format '{param}'. Use KEY=VALUE.", file=sys.stderr)
                sys.exit(1)
            key, val = param.split("=", 1)
            query_params[key] = val
        body["query_parameters"] = query_params
    if args.performance:
        body["performance"] = args.performance

    # Execute
    print(f"Executing query {query_id}...")
    result = _fetch(f"/query/{query_id}/execute", method="POST", body=body if body else None)
    execution_id = result.get("execution_id")
    state = result.get("state", "unknown")

    if not execution_id:
        print(f"ERROR: No execution_id returned. Response: {json.dumps(result)}", file=sys.stderr)
        sys.exit(1)

    print(f"Execution ID: {execution_id} (state: {state})")

    if args.no_wait:
        print("Use 'status' or 'results' subcommand to check progress.")
        out_data = {"execution_id": execution_id, "query_id": query_id, "state": state}
        if write_output(out_data, args, summary=f"execute query {query_id}"):
            return
        return

    # Poll for completion
    elapsed = 0
    while elapsed < POLL_TIMEOUT_SEC:
        time.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC

        status_data = _fetch(f"/execution/{execution_id}/status")
        state = status_data.get("state", "unknown")
        is_finished = status_data.get("is_execution_finished", False)
        queue_pos = status_data.get("queue_position")

        pos_str = f", queue position {queue_pos}" if queue_pos is not None else ""
        print(f"  [{elapsed}s] state={state}{pos_str}")

        if is_finished:
            break

    if not is_finished:
        print(f"ERROR: Execution timed out after {POLL_TIMEOUT_SEC}s. "
              f"Use 'status {execution_id}' to check later.", file=sys.stderr)
        sys.exit(1)

    if state == "QUERY_STATE_COMPLETED":
        # Fetch results
        params = {}
        if args.limit:
            params["limit"] = args.limit

        data = _fetch(f"/execution/{execution_id}/results", params=params, timeout=60)

        metadata = data.get("result", {}).get("metadata", {})
        rows = data.get("result", {}).get("rows", [])
        total = metadata.get("total_row_count", len(rows))
        returned = metadata.get("row_count", len(rows))
        cost = status_data.get("execution_cost_credits", "?")

        summary = (
            f"Dune query {query_id} executed: {returned}/{total} rows, "
            f"cost={cost} credits"
        )

        if write_output(data, args, summary=summary):
            return

        if getattr(args, "json_out", False):
            print(json.dumps(data, indent=2))
            return

        print(f"\nQuery {query_id} completed ({cost} credits)")
        print(f"Rows: {returned} returned / {total} total")
        print()
        display_limit = args.limit if args.limit else 30
        _format_table(rows, max_rows=display_limit)

    elif state == "QUERY_STATE_FAILED":
        error = status_data.get("error", {})
        err_msg = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
        print(f"ERROR: Query execution failed: {err_msg}", file=sys.stderr)
        sys.exit(1)

    elif state == "QUERY_STATE_CANCELED":
        print("Query execution was cancelled.")

    else:
        print(f"Query ended in state: {state}")


def cmd_status(args):
    """Check execution status."""
    data = _fetch(f"/execution/{args.execution_id}/status")

    if write_output(data, args, summary=f"status {args.execution_id}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    state = data.get("state", "unknown")
    is_finished = data.get("is_execution_finished", False)
    query_id = data.get("query_id", "?")
    submitted = data.get("submitted_at", "N/A")
    started = data.get("execution_started_at", "N/A")
    ended = data.get("execution_ended_at", "N/A")
    cost = data.get("execution_cost_credits", "N/A")
    queue_pos = data.get("queue_position")
    expires = data.get("expires_at", "N/A")

    print(f"Execution: {args.execution_id}")
    print(f"Query ID:  {query_id}")
    print(f"State:     {state} (finished={is_finished})")
    if queue_pos is not None:
        print(f"Queue Pos: {queue_pos}")
    print(f"Submitted: {submitted}")
    print(f"Started:   {started}")
    print(f"Ended:     {ended}")
    print(f"Cost:      {cost} credits")
    print(f"Expires:   {expires}")

    error = data.get("error")
    if error:
        err_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        print(f"Error:     {err_msg}")


def cmd_cancel(args):
    """Cancel a running execution."""
    data = _fetch(f"/execution/{args.execution_id}/cancel", method="POST")

    if write_output(data, args, summary=f"cancel {args.execution_id}"):
        return

    success = data.get("success", False)
    if success:
        print(f"Execution {args.execution_id} cancelled successfully.")
    else:
        print(f"Cancel response: {json.dumps(data)}")


def cmd_known(args):
    """List known query aliases."""
    if write_output(KNOWN_QUERIES, args, summary="known query aliases"):
        return

    print("Known Dune query aliases:")
    print()
    for alias, qid in sorted(KNOWN_QUERIES.items()):
        print(f"  {alias:30s} -> query {qid}")
    print()
    print("Use any alias in place of a numeric query ID:")
    print("  python tools/query_dune.py results wlfi-holders")


# -- CLI ---------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Dune Analytics API -- blockchain analytics queries",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Known query aliases (use in place of numeric IDs):
  wlfi-holders    -> 4166026  (WLFI token holders)

Dashboards (browse on dune.com, extract query IDs for API use):
  https://dune.com/seoul/wlfi   (World Liberty Financial)
  https://dune.com/seoul/usd1   (USD1 stablecoin)
  https://dune.com/magacoin/trump  (TRUMP memecoin)

Examples:
  query_dune.py results 4166026 --limit 50
  query_dune.py results wlfi-holders --output /tmp/wlfi.json
  query_dune.py execute 4166026 --params "wallet=0xabc"
  query_dune.py status 01HKZJ2683PHF9Q9PHHQ8FW4Q1
  query_dune.py cancel 01HKZJ2683PHF9Q9PHHQ8FW4Q1
  query_dune.py known
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # results
    p = sub.add_parser("results", help="Get latest results from a saved query")
    p.add_argument("query_id", help="Dune query ID (numeric) or known alias")
    p.add_argument("--limit", type=int, default=None, help="Max rows to return")
    p.add_argument("--offset", type=int, default=None, help="Row offset for pagination")
    p.add_argument("--columns", help="Comma-separated column names to return")
    p.add_argument("--filters", help="SQL WHERE clause expression for filtering rows")
    p.add_argument("--sort-by", dest="sort_by", help="SQL ORDER BY expression")
    add_output_args(p)

    # execute
    p = sub.add_parser("execute", help="Execute a query and wait for results")
    p.add_argument("query_id", help="Dune query ID (numeric) or known alias")
    p.add_argument("--params", nargs="+", metavar="KEY=VALUE",
                   help="Query parameters (e.g. --params wallet=0xabc limit=100)")
    p.add_argument("--performance", choices=["medium", "large"], default=None,
                   help="Execution tier (default: medium)")
    p.add_argument("--no-wait", action="store_true",
                   help="Submit and return immediately without waiting for results")
    p.add_argument("--limit", type=int, default=None,
                   help="Max rows to return from results")
    add_output_args(p)

    # status
    p = sub.add_parser("status", help="Check execution status")
    p.add_argument("execution_id", help="Execution ID from execute command")
    add_output_args(p)

    # cancel
    p = sub.add_parser("cancel", help="Cancel a running execution")
    p.add_argument("execution_id", help="Execution ID to cancel")
    add_output_args(p)

    # known
    p = sub.add_parser("known", help="List known query aliases")
    add_output_args(p)

    args = parser.parse_args()
    commands = {
        "results": cmd_results,
        "execute": cmd_execute,
        "status": cmd_status,
        "cancel": cmd_cancel,
        "known": cmd_known,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
