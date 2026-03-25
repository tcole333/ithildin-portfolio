#!/usr/bin/env python3
"""
Solscan Pro API wrapper for Solana blockchain analysis.

Traces SPL token holders, transfers, and transaction flows on Solana.
Primary use case: analyzing TRUMP and MELANIA meme coin holder distribution
and flow patterns (CIC Digital LLC / Fight Fight Fight LLC holdings).

API: https://pro-api.solscan.io/v2.0 (Solscan Pro API v2)
Auth: Required. Set SOLSCAN_API_KEY in .env. Get key at https://solscan.io (account -> API Management).
Auth header: `token: <API_KEY>`
Rate limits: Not officially documented; tool enforces 1 req/sec. 429 responses trigger exponential backoff.

All endpoints require a valid API key. Free Solscan accounts can generate a key
with limited daily quota. Paid plans increase rate limits and quota.

Usage:
    uv run python tools/query_solscan.py token-info TRUMP
    uv run python tools/query_solscan.py token-info 6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN
    uv run python tools/query_solscan.py token-holders TRUMP --limit 40
    uv run python tools/query_solscan.py token-holders MELANIA --min-amount 1000000
    uv run python tools/query_solscan.py token-transfers TRUMP --limit 20
    uv run python tools/query_solscan.py token-transfers TRUMP --from-addr <address>
    uv run python tools/query_solscan.py account <wallet_address>
    uv run python tools/query_solscan.py tx <signature>
    uv run python tools/query_solscan.py token-list --sort market_cap --limit 20
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

BASE_URL = "https://pro-api.solscan.io/v2.0"

# Verified token mint addresses on Solana mainnet
KNOWN_TOKENS = {
    "TRUMP": "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",   # OFFICIAL TRUMP, 6 decimals
    "MELANIA": "FUAfBo2jgks6gB4Z4LfZkqSZgzNucisEHqnNebaRxM1P",  # Melania Meme, 6 decimals
}

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))

_last_request_time = 0.0


def _get_api_key():
    """Get Solscan API key from environment."""
    key = os.environ.get("SOLSCAN_API_KEY", "")
    if not key:
        print(
            "ERROR: SOLSCAN_API_KEY not set. Add to .env or export it.\n"
            "Get a key at https://solscan.io -> Account -> API Management.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _resolve_token(token_arg):
    """Resolve a token alias (e.g. 'TRUMP') to its mint address, or pass through raw addresses."""
    upper = token_arg.upper()
    if upper in KNOWN_TOKENS:
        return KNOWN_TOKENS[upper]
    # Assume it's a raw Solana address (32-44 base58 chars)
    return token_arg


def _request(path, params=None, allow_404=False):
    """Make an authenticated request to Solscan Pro API v2."""
    global _last_request_time

    api_key = _get_api_key()

    url = f"{BASE_URL}{path}"
    if params:
        # Filter out None values
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urlencode(clean, doseq=True)

    headers = {
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
        "token": api_key,
    }

    # Rate limiting: 1 req/sec
    elapsed = time.time() - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    req = Request(url, headers=headers)
    retries = 0
    while retries < 3:
        try:
            _last_request_time = time.time()
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 404 and allow_404:
                return None
            if e.code == 401:
                body = e.read().decode()[:500]
                print(
                    f"ERROR: Authentication failed (401). Check SOLSCAN_API_KEY.\n{body}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if e.code == 429:
                retries += 1
                wait = 2 ** retries
                print(f"  Rate limited (429), waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            body = e.read().decode()[:500]
            print(f"ERROR: HTTP {e.code} from Solscan: {body}", file=sys.stderr)
            sys.exit(1)
        except URLError as e:
            print(f"ERROR: Cannot reach Solscan API: {e.reason}", file=sys.stderr)
            sys.exit(1)

    print("ERROR: Exhausted retries on rate limit", file=sys.stderr)
    sys.exit(1)


def _format_amount(raw_amount, decimals):
    """Format a raw SPL token amount using its decimals."""
    if decimals is None or decimals == 0:
        return f"{raw_amount:,}"
    value = raw_amount / (10 ** decimals)
    if value >= 1_000_000:
        return f"{value:,.0f}"
    elif value >= 1:
        return f"{value:,.2f}"
    else:
        return f"{value:.{decimals}f}"


def _format_ts(unix_ts):
    """Format a Unix timestamp to human-readable UTC string."""
    if not unix_ts:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError):
        return str(unix_ts)


def _short_addr(addr, length=8):
    """Shorten a Solana address for display: first4...last4."""
    if not addr or len(addr) <= length * 2:
        return addr or "N/A"
    return f"{addr[:length]}...{addr[-length:]}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_token_info(args):
    """Get token metadata: name, symbol, decimals, supply, holders, price."""
    mint = _resolve_token(args.mint_address)
    data = _request("/token/meta", {"address": mint})

    if not data or not data.get("success"):
        err = data.get("error_message", "Unknown error") if data else "No response"
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    info = data.get("data", {})

    if write_output(info, args, summary=f"Solscan token-info {mint[:16]}..."):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(info, indent=2, default=str))
        return

    name = info.get("name", "?")
    symbol = info.get("symbol", "?")
    decimals = info.get("decimals", 0)
    supply_raw = info.get("supply", "0")
    try:
        supply_val = int(supply_raw) / (10 ** decimals) if decimals else int(supply_raw)
    except (ValueError, TypeError):
        supply_val = supply_raw
    holders = info.get("holder", "?")
    price = info.get("price")
    mcap = info.get("market_cap")
    price_change = info.get("price_change_24h")
    creator = info.get("creator", "")
    created_time = info.get("created_time")

    print(f"=== {name} ({symbol}) ===")
    print(f"  Mint: {mint}")
    print(f"  Decimals: {decimals}")
    print(f"  Supply: {supply_val:,.0f}" if isinstance(supply_val, (int, float)) else f"  Supply: {supply_val}")
    print(f"  Holders: {holders:,}" if isinstance(holders, int) else f"  Holders: {holders}")
    if price is not None:
        print(f"  Price: ${price:,.4f}")
    if mcap is not None:
        print(f"  Market Cap: ${mcap:,.0f}")
    if price_change is not None:
        print(f"  24h Change: {price_change:+.2f}%")
    if creator:
        print(f"  Creator: {creator}")
    if created_time:
        print(f"  Created: {_format_ts(created_time)}")

    # Mint/freeze authority
    mint_auth = info.get("mint_authority")
    freeze_auth = info.get("freeze_authority")
    if mint_auth:
        print(f"  Mint Authority: {mint_auth}")
    else:
        print(f"  Mint Authority: None (supply is fixed)")
    if freeze_auth:
        print(f"  Freeze Authority: {freeze_auth}")

    print()


def cmd_token_holders(args):
    """Get top token holders with amounts and percentages."""
    mint = _resolve_token(args.mint_address)
    params = {
        "address": mint,
        "page": args.page,
        "page_size": min(args.limit, 40),
    }
    if args.min_amount:
        params["from_amount"] = str(args.min_amount)
    if args.max_amount:
        params["to_amount"] = str(args.max_amount)

    data = _request("/token/holders", params)

    if not data or not data.get("success"):
        err = data.get("error_message", "Unknown error") if data else "No response"
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    result = data.get("data", {})
    total = result.get("total", 0)
    items = result.get("items", [])

    if write_output(result, args, summary=f"Solscan holders for {mint[:16]}... ({total} total)"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(result, indent=2, default=str))
        return

    # Resolve token name for display
    token_label = args.mint_address.upper() if args.mint_address.upper() in KNOWN_TOKENS else _short_addr(mint)
    print(f"=== Top Holders: {token_label} ===")
    print(f"Total holders: {total:,}")
    print(f"Showing: {len(items)} (page {args.page})")
    print()

    for item in items:
        rank = item.get("rank", "?")
        owner = item.get("owner", "?")
        amount = item.get("amount", 0)
        decimals = item.get("decimals", 6)
        pct = item.get("percentage")
        value = item.get("value")

        formatted_amount = _format_amount(amount, decimals)
        pct_str = f" ({pct:.2f}%)" if pct is not None else ""
        val_str = f"  [${value:,.0f}]" if value is not None else ""

        print(f"  #{rank:<4} {owner}")
        print(f"        Amount: {formatted_amount}{pct_str}{val_str}")

    print()


def cmd_token_transfers(args):
    """Get token transfer events, optionally filtered by address."""
    mint = _resolve_token(args.mint_address)
    params = {
        "address": mint,
        "page": args.page,
        "page_size": min(args.limit, 100),
        "sort_by": "block_time",
        "sort_order": "desc",
    }
    if args.from_addr:
        params["from"] = args.from_addr
    if args.to_addr:
        params["to"] = args.to_addr
    if args.activity_type:
        params["activity_type[]"] = args.activity_type
    if args.exclude_zero:
        params["exclude_amount_zero"] = "true"

    data = _request("/token/transfer", params)

    if not data or not data.get("success"):
        err = data.get("error_message", "Unknown error") if data else "No response"
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    transfers = data.get("data", [])

    if write_output(transfers, args, summary=f"Solscan transfers for {mint[:16]}..."):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(transfers, indent=2, default=str))
        return

    token_label = args.mint_address.upper() if args.mint_address.upper() in KNOWN_TOKENS else _short_addr(mint)
    print(f"=== Token Transfers: {token_label} ===")
    print(f"Results: {len(transfers)} (page {args.page})")
    print()

    for tx in transfers:
        ts = _format_ts(tx.get("block_time"))
        from_addr = tx.get("from_address", "?")
        to_addr = tx.get("to_address", "?")
        amount = tx.get("amount", 0)
        decimals = tx.get("token_decimals", 6)
        sig = tx.get("trans_id", "?")
        activity = tx.get("activity_type", "")

        formatted_amount = _format_amount(amount, decimals)
        activity_label = f" [{activity}]" if activity else ""

        print(f"  {ts}{activity_label}")
        print(f"    From: {from_addr}")
        print(f"    To:   {to_addr}")
        print(f"    Amount: {formatted_amount}")
        print(f"    Tx: {sig}")
        print()


def cmd_account(args):
    """Get account info and token holdings for a wallet."""
    address = args.address

    # Get account detail
    detail = _request("/account/detail", {"address": address}, allow_404=True)

    # Get token holdings
    tokens = _request("/account/token-accounts", {
        "address": address,
        "type": "token",
        "page_size": 40,
        "hide_zero": "true" if not args.show_zero else "false",
    })

    result = {
        "detail": detail.get("data", {}) if detail and detail.get("success") else {},
        "token_accounts": tokens.get("data", []) if tokens and tokens.get("success") else [],
    }

    if write_output(result, args, summary=f"Solscan account {_short_addr(address)}"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(result, indent=2, default=str))
        return

    acct = result["detail"]
    holdings = result["token_accounts"]

    print(f"=== Account: {address} ===")
    if acct:
        lamports = acct.get("lamports", 0)
        sol_balance = lamports / 1e9
        acct_type = acct.get("type", "?")
        owner_prog = acct.get("owner_program", "?")
        print(f"  SOL Balance: {sol_balance:,.4f} SOL")
        print(f"  Type: {acct_type}")
        print(f"  Owner Program: {owner_prog}")
    else:
        print("  (Account detail not available)")

    print()
    print(f"  Token Holdings ({len(holdings)}):")
    for tok in holdings:
        token_addr = tok.get("token_address", "?")
        amount = tok.get("amount", 0)
        decimals = tok.get("token_decimals", 6)
        formatted = _format_amount(amount, decimals)

        # Check if known token
        known_label = ""
        for name, addr in KNOWN_TOKENS.items():
            if token_addr == addr:
                known_label = f" ({name})"
                break

        print(f"    {_short_addr(token_addr)}{known_label}: {formatted}")

    print()


def cmd_tx(args):
    """Get transaction details by signature."""
    data = _request("/transaction/detail", {"tx": args.signature})

    if not data or not data.get("success"):
        err = data.get("error_message", "Unknown error") if data else "No response"
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    tx = data.get("data", {})

    if write_output(tx, args, summary=f"Solscan tx {args.signature[:16]}..."):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(tx, indent=2, default=str))
        return

    tx_hash = tx.get("tx_hash", args.signature)
    block = tx.get("block_id", "?")
    ts = _format_ts(tx.get("block_time"))
    fee = tx.get("fee", 0)
    status = "Success" if tx.get("status") == 1 else "Failed"
    compute = tx.get("compute_units_consumed", "?")
    priority_fee = tx.get("priority_fee", 0)

    print(f"=== Transaction ===")
    print(f"  Signature: {tx_hash}")
    print(f"  Block: {block}")
    print(f"  Time: {ts}")
    print(f"  Status: {status}")
    print(f"  Fee: {fee / 1e9:.6f} SOL" if isinstance(fee, (int, float)) else f"  Fee: {fee}")
    if priority_fee:
        print(f"  Priority Fee: {priority_fee / 1e9:.6f} SOL")
    print(f"  Compute Units: {compute}")

    # Signers
    signers = tx.get("signer", [])
    if signers:
        print(f"\n  Signers:")
        for s in signers:
            if isinstance(s, str):
                print(f"    {s}")
            elif isinstance(s, dict):
                print(f"    {s.get('address', s)}")

    # SOL balance changes
    sol_changes = tx.get("sol_bal_change", [])
    if sol_changes:
        print(f"\n  SOL Balance Changes:")
        for c in sol_changes:
            if isinstance(c, dict):
                addr = c.get("address", "?")
                change = c.get("change", 0)
                change_sol = change / 1e9 if isinstance(change, (int, float)) else change
                print(f"    {_short_addr(addr)}: {change_sol:+,.6f} SOL")

    # Token balance changes
    token_changes = tx.get("token_bal_change", [])
    if token_changes:
        print(f"\n  Token Balance Changes:")
        for c in token_changes:
            if isinstance(c, dict):
                addr = c.get("address", "?")
                token_addr = c.get("token_address", "?")
                change = c.get("change", 0)
                decimals = c.get("token_decimals", 6)

                known_label = ""
                for name, mint in KNOWN_TOKENS.items():
                    if token_addr == mint:
                        known_label = f" ({name})"
                        break

                formatted = _format_amount(abs(change), decimals) if isinstance(change, (int, float)) else str(change)
                sign = "+" if isinstance(change, (int, float)) and change >= 0 else "-"
                print(f"    {_short_addr(addr)}: {sign}{formatted} {_short_addr(token_addr)}{known_label}")

    # Programs involved
    programs = tx.get("programs_involved", [])
    if programs:
        print(f"\n  Programs Involved:")
        for p in programs:
            if isinstance(p, str):
                print(f"    {p}")
            elif isinstance(p, dict):
                print(f"    {p.get('address', p)}")

    print()


def cmd_token_list(args):
    """List top tokens on Solana sorted by market cap, holders, or creation time."""
    params = {
        "sort_by": args.sort,
        "sort_order": args.order,
        "page": args.page,
        "page_size": min(args.limit, 100),
    }

    data = _request("/token/list", params)

    if not data or not data.get("success"):
        err = data.get("error_message", "Unknown error") if data else "No response"
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    tokens = data.get("data", [])

    if write_output(tokens, args, summary=f"Solscan token-list ({len(tokens)} tokens)"):
        return
    if getattr(args, "json_out", False):
        print(json.dumps(tokens, indent=2, default=str))
        return

    print(f"=== Solana Tokens (sorted by {args.sort}, {args.order}) ===")
    print(f"Showing: {len(tokens)} (page {args.page})")
    print()

    for i, tok in enumerate(tokens, 1):
        name = tok.get("name", "?")
        symbol = tok.get("symbol", "?")
        address = tok.get("address", "?")
        price = tok.get("price")
        mcap = tok.get("market_cap")
        holders = tok.get("holder", "?")
        change_24h = tok.get("price_24h_change")

        # Check if known
        known_flag = ""
        for kn, ka in KNOWN_TOKENS.items():
            if address == ka:
                known_flag = f" *** {kn} ***"
                break

        print(f"  {i}. {name} ({symbol}){known_flag}")
        print(f"     Address: {address}")
        if price is not None:
            print(f"     Price: ${price:,.4f}", end="")
            if change_24h is not None:
                print(f"  ({change_24h:+.2f}%)", end="")
            print()
        if mcap is not None:
            print(f"     Market Cap: ${mcap:,.0f}")
        if isinstance(holders, int):
            print(f"     Holders: {holders:,}")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Solscan Pro API for Solana blockchain analysis (token holders, transfers, transactions)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Known token aliases:\n"
            "  TRUMP   -> 6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN\n"
            "  MELANIA -> FUAfBo2jgks6gB4Z4LfZkqSZgzNucisEHqnNebaRxM1P\n"
            "\n"
            "Auth: SOLSCAN_API_KEY env var or in .env file.\n"
            "All endpoints require authentication (no free/unauthenticated access).\n"
            "Get a key at https://solscan.io -> Account -> API Management.\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # token-info
    p = sub.add_parser("token-info", help="Get token metadata (name, symbol, supply, holders, price)")
    p.add_argument("mint_address", help="Token mint address or alias (TRUMP, MELANIA)")
    add_output_args(p)

    # token-holders
    p = sub.add_parser("token-holders", help="Get top token holders with amounts and percentages")
    p.add_argument("mint_address", help="Token mint address or alias (TRUMP, MELANIA)")
    p.add_argument("--limit", type=int, default=40, help="Results per page (max 40, default 40)")
    p.add_argument("--page", type=int, default=1, help="Page number (default 1)")
    p.add_argument("--min-amount", type=str, default=None, help="Min token holding amount (raw, string)")
    p.add_argument("--max-amount", type=str, default=None, help="Max token holding amount (raw, string)")
    add_output_args(p)

    # token-transfers
    p = sub.add_parser("token-transfers", help="Get token transfer events")
    p.add_argument("mint_address", help="Token mint address or alias (TRUMP, MELANIA)")
    p.add_argument("--limit", type=int, default=40, help="Results per page (max 100, default 40)")
    p.add_argument("--page", type=int, default=1, help="Page number (default 1)")
    p.add_argument("--from-addr", default=None, help="Filter by source address")
    p.add_argument("--to-addr", default=None, help="Filter by destination address")
    p.add_argument("--activity-type", default=None,
                    help="Filter by type: SPL_TRANSFER, SPL_BURN, SPL_MINT, SPL_CREATE_ACCOUNT")
    p.add_argument("--exclude-zero", action="store_true", help="Exclude zero-amount transfers")
    add_output_args(p)

    # account
    p = sub.add_parser("account", help="Get account info and token holdings")
    p.add_argument("address", help="Solana wallet address")
    p.add_argument("--show-zero", action="store_true", help="Include zero-balance token accounts")
    add_output_args(p)

    # tx
    p = sub.add_parser("tx", help="Get transaction details by signature")
    p.add_argument("signature", help="Transaction signature")
    add_output_args(p)

    # token-list
    p = sub.add_parser("token-list", help="List top tokens on Solana")
    p.add_argument("--sort", default="market_cap",
                    choices=["market_cap", "holder", "created_time"],
                    help="Sort field (default: market_cap)")
    p.add_argument("--order", default="desc", choices=["asc", "desc"],
                    help="Sort order (default: desc)")
    p.add_argument("--limit", type=int, default=20, help="Results per page (max 100, default 20)")
    p.add_argument("--page", type=int, default=1, help="Page number (default 1)")
    add_output_args(p)

    args = parser.parse_args()

    handlers = {
        "token-info": cmd_token_info,
        "token-holders": cmd_token_holders,
        "token-transfers": cmd_token_transfers,
        "account": cmd_account,
        "tx": cmd_tx,
        "token-list": cmd_token_list,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
