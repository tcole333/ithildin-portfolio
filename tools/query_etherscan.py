#!/usr/bin/env python3
"""
Etherscan API wrapper for Ethereum blockchain analysis.

Queries token holders, transfer events, balances, transactions, and contract
metadata. Primary use: tracing ERC-20 governance tokens and stablecoins
(e.g., World Liberty Financial WLFI, USD1) for OSINT financial analysis.

API: https://api.etherscan.io/v2/api
Auth: API key required. Set ETHERSCAN_API_KEY in .env.
Rate limits: Free tier 5 calls/sec, 100k calls/day. We self-limit to 1 req/sec.

Usage:
    python tools/query_etherscan.py token-holders 0xCONTRACT
    python tools/query_etherscan.py token-transfers 0xCONTRACT --address 0xHOLDER
    python tools/query_etherscan.py token-transfers 0xCONTRACT --start-block 19000000
    python tools/query_etherscan.py token-info 0xCONTRACT
    python tools/query_etherscan.py balance 0xADDRESS
    python tools/query_etherscan.py token-balance 0xCONTRACT 0xADDRESS
    python tools/query_etherscan.py tx 0xHASH
    python tools/query_etherscan.py address 0xADDRESS --limit 50
    python tools/query_etherscan.py contract 0xADDRESS
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

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

BASE_URL = "https://api.etherscan.io/v2/api"

# Track last request time for rate limiting
_last_request_time = 0.0


def _get_api_key():
    """Get Etherscan API key from environment."""
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        print("ERROR: ETHERSCAN_API_KEY not set in .env.", file=sys.stderr)
        sys.exit(1)
    return key


def _fetch(module, action, params=None, max_retries=3):
    """Fetch from Etherscan API v2 with rate limiting and retry on 429."""
    global _last_request_time

    if params is None:
        params = {}

    query = {
        "chainid": "1",
        "module": module,
        "action": action,
        "apikey": _get_api_key(),
        **params,
    }

    url = f"{BASE_URL}?{urlencode(query)}"
    req = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "OSINT-Research/1.0",
    })

    for attempt in range(max_retries):
        # Rate limit: at least 1 second between requests
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        try:
            _last_request_time = time.time()
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429:
                wait = 2 ** (attempt + 1)
                print(f"Rate limited (429). Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            body = e.read().decode() if e.fp else ""
            print(f"ERROR: Etherscan API returned {e.code}: {body[:500]}", file=sys.stderr)
            sys.exit(1)
        except URLError as e:
            print(f"ERROR: Network error: {e.reason}", file=sys.stderr)
            sys.exit(1)
        except json.JSONDecodeError:
            print("ERROR: Etherscan returned non-JSON response.", file=sys.stderr)
            sys.exit(1)

        # Etherscan returns status "0" with message "NOTOK" on errors
        status = data.get("status")
        message = data.get("message", "")
        result = data.get("result", "")

        if status == "0" and message == "NOTOK":
            # Some "NOTOK" results are just empty results, not errors
            if isinstance(result, str) and "No transactions found" in result:
                return []
            if isinstance(result, str) and "No records found" in result:
                return []
            if isinstance(result, str) and "Max rate limit reached" in result:
                wait = 2 ** (attempt + 1)
                print(f"Rate limit in response. Retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"ERROR: Etherscan API error: {result}", file=sys.stderr)
            sys.exit(1)

        return data.get("result", data)

    print("ERROR: Max retries exceeded for Etherscan API.", file=sys.stderr)
    sys.exit(1)


def _format_token_value(raw_value, decimals=18):
    """Convert raw token value to human-readable format.

    ERC-20 tokens store values as integers multiplied by 10^decimals.
    E.g., 1.5 tokens with 18 decimals = 1500000000000000000.
    """
    try:
        raw = int(raw_value)
        dec = int(decimals)
    except (ValueError, TypeError):
        return str(raw_value)

    if dec == 0:
        return str(raw)

    value = raw / (10 ** dec)
    # Use comma-separated format, trim trailing zeros
    if value == int(value):
        return f"{int(value):,}"
    else:
        # Show up to 6 decimal places
        formatted = f"{value:,.6f}".rstrip("0").rstrip(".")
        return formatted


def _format_eth(wei_value):
    """Convert Wei to ETH."""
    try:
        wei = int(wei_value)
    except (ValueError, TypeError):
        return str(wei_value)
    eth = wei / 1e18
    if eth == int(eth):
        return f"{int(eth):,} ETH"
    return f"{eth:,.6f} ETH".rstrip("0").rstrip(".")


def _format_timestamp(ts):
    """Convert Unix timestamp to human-readable date."""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        return str(ts)


def _shorten_address(addr):
    """Shorten an Ethereum address for display: 0x1234...ABCD."""
    if not addr or len(addr) < 12:
        return addr or "N/A"
    return f"{addr[:6]}...{addr[-4:]}"


# -- Commands ----------------------------------------------------------------


def cmd_token_holders(args):
    """Get top token holders for an ERC-20 contract."""
    params = {
        "contractaddress": args.contract_address,
        "page": str(args.page),
        "offset": str(args.limit),
    }
    result = _fetch("token", "tokenholderlist", params)

    if not isinstance(result, list):
        result = []

    data = {
        "contract": args.contract_address,
        "holders": result,
        "count": len(result),
    }

    if write_output(data, args, summary=f"token holders for {_shorten_address(args.contract_address)} ({len(result)} holders)"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Top Token Holders: {_shorten_address(args.contract_address)}")
    print(f"Results: {len(result)}\n")

    for i, h in enumerate(result, 1):
        addr = h.get("TokenHolderAddress", "N/A")
        raw_qty = h.get("TokenHolderQuantity", "0")
        formatted_qty = _format_token_value(raw_qty, args.decimals)
        print(f"  [{i:3d}] {addr}")
        print(f"        Quantity: {formatted_qty} (raw: {raw_qty})")


def cmd_token_transfers(args):
    """Get token transfer events for a contract, optionally filtered by address."""
    params = {
        "contractaddress": args.contract_address,
        "page": str(args.page),
        "offset": str(args.limit),
        "startblock": str(args.start_block),
        "endblock": str(args.end_block),
        "sort": args.sort,
    }
    if args.address:
        params["address"] = args.address

    result = _fetch("account", "tokentx", params)

    if not isinstance(result, list):
        result = []

    data = {
        "contract": args.contract_address,
        "transfers": result,
        "count": len(result),
    }

    if write_output(data, args, summary=f"token transfers for {_shorten_address(args.contract_address)} ({len(result)} transfers)"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Token Transfers: {_shorten_address(args.contract_address)}")
    if args.address:
        print(f"Filtered by address: {args.address}")
    print(f"Results: {len(result)}\n")

    for i, tx in enumerate(result, 1):
        from_addr = tx.get("from", "N/A")
        to_addr = tx.get("to", "N/A")
        raw_value = tx.get("value", "0")
        decimals = tx.get("tokenDecimal", "18")
        token_name = tx.get("tokenName", "")
        token_symbol = tx.get("tokenSymbol", "")
        ts = tx.get("timeStamp", "")
        tx_hash = tx.get("hash", "N/A")

        formatted_value = _format_token_value(raw_value, decimals)
        date_str = _format_timestamp(ts) if ts else "N/A"

        print(f"  [{i:3d}] {date_str}")
        print(f"        From: {from_addr}")
        print(f"        To:   {to_addr}")
        print(f"        Value: {formatted_value} {token_symbol} (raw: {raw_value})")
        print(f"        Tx: {tx_hash}")
        if i < len(result):
            print()


def cmd_token_info(args):
    """Get metadata for an ERC-20 token contract."""
    result = _fetch("token", "tokeninfo", {"contractaddress": args.contract_address})

    # tokeninfo returns a list with one element, or a dict
    if isinstance(result, list) and len(result) > 0:
        info = result[0]
    elif isinstance(result, dict):
        info = result
    else:
        info = {}

    data = {
        "contract": args.contract_address,
        "info": info,
    }

    if write_output(data, args, summary=f"token info for {_shorten_address(args.contract_address)}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Token Info: {_shorten_address(args.contract_address)}\n")

    name = info.get("tokenName", info.get("name", "N/A"))
    symbol = info.get("tokenSymbol", info.get("symbol", "N/A"))
    decimals = info.get("divisor", info.get("decimals", "N/A"))
    total_supply = info.get("totalSupply", "N/A")
    holder_count = info.get("holdersCount", info.get("holderCount", "N/A"))
    token_type = info.get("tokenType", info.get("type", "N/A"))
    website = info.get("website", "")
    description = info.get("description", "")
    bluecheck = info.get("blueCheckmark", "")

    print(f"  Name: {name}")
    print(f"  Symbol: {symbol}")
    print(f"  Decimals: {decimals}")
    if total_supply != "N/A":
        formatted_supply = _format_token_value(total_supply, decimals if decimals != "N/A" else 18)
        print(f"  Total Supply: {formatted_supply} (raw: {total_supply})")
    print(f"  Holders: {holder_count}")
    print(f"  Type: {token_type}")
    if website:
        print(f"  Website: {website}")
    if description:
        print(f"  Description: {description[:200]}")
    if bluecheck:
        print(f"  Blue Checkmark: {bluecheck}")

    # Print any additional fields not already shown
    skip_keys = {
        "tokenName", "name", "tokenSymbol", "symbol", "divisor", "decimals",
        "totalSupply", "holdersCount", "holderCount", "tokenType", "type",
        "website", "description", "blueCheckmark", "contractAddress",
    }
    extras = {k: v for k, v in info.items() if k not in skip_keys and v}
    if extras:
        print(f"\n  Additional fields:")
        for k, v in extras.items():
            val_str = str(v)
            if len(val_str) > 120:
                val_str = val_str[:120] + "..."
            print(f"    {k}: {val_str}")


def cmd_balance(args):
    """Get ETH balance for an address."""
    result = _fetch("account", "balance", {"address": args.address, "tag": "latest"})

    data = {
        "address": args.address,
        "balance_wei": result,
        "balance_eth": _format_eth(result),
    }

    if write_output(data, args, summary=f"balance for {_shorten_address(args.address)}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Address: {args.address}")
    print(f"Balance: {_format_eth(result)}")
    print(f"Wei: {result}")


def cmd_token_balance(args):
    """Get ERC-20 token balance for a specific address."""
    params = {
        "contractaddress": args.contract_address,
        "address": args.address,
        "tag": "latest",
    }
    result = _fetch("account", "tokenbalance", params)

    formatted = _format_token_value(result, args.decimals)

    data = {
        "contract": args.contract_address,
        "address": args.address,
        "balance_raw": result,
        "balance_formatted": formatted,
        "decimals": args.decimals,
    }

    if write_output(data, args, summary=f"token balance for {_shorten_address(args.address)}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Token Contract: {args.contract_address}")
    print(f"Holder Address: {args.address}")
    print(f"Balance: {formatted} (raw: {result})")


def cmd_tx(args):
    """Get transaction details by hash."""
    result = _fetch("proxy", "eth_getTransactionByHash", {"txhash": args.hash})

    if not isinstance(result, dict):
        print("ERROR: Transaction not found.", file=sys.stderr)
        sys.exit(1)

    data = {
        "transaction": result,
    }

    if write_output(data, args, summary=f"tx {_shorten_address(args.hash)}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Transaction: {result.get('hash', 'N/A')}\n")

    from_addr = result.get("from", "N/A")
    to_addr = result.get("to", "N/A")
    value_hex = result.get("value", "0x0")
    block_hex = result.get("blockNumber", "0x0")
    gas_hex = result.get("gas", "0x0")
    gas_price_hex = result.get("gasPrice", "0x0")
    nonce_hex = result.get("nonce", "0x0")
    input_data = result.get("input", "0x")

    # Convert hex values
    try:
        value_wei = int(value_hex, 16) if value_hex else 0
        block_num = int(block_hex, 16) if block_hex else 0
        gas = int(gas_hex, 16) if gas_hex else 0
        gas_price = int(gas_price_hex, 16) if gas_price_hex else 0
        nonce = int(nonce_hex, 16) if nonce_hex else 0
    except (ValueError, TypeError):
        value_wei = 0
        block_num = 0
        gas = 0
        gas_price = 0
        nonce = 0

    print(f"  From: {from_addr}")
    print(f"  To: {to_addr}")
    print(f"  Value: {_format_eth(value_wei)}")
    print(f"  Block: {block_num:,}")
    print(f"  Gas: {gas:,}")
    print(f"  Gas Price: {gas_price / 1e9:.2f} Gwei")
    print(f"  Nonce: {nonce}")
    if input_data and input_data != "0x":
        # Show method signature (first 4 bytes) and data length
        method_sig = input_data[:10] if len(input_data) >= 10 else input_data
        print(f"  Input: {method_sig}... ({len(input_data) // 2 - 1} bytes)")


def cmd_address(args):
    """Get recent transactions for an address."""
    params = {
        "address": args.address,
        "startblock": str(args.start_block),
        "endblock": str(args.end_block),
        "page": str(args.page),
        "offset": str(args.limit),
        "sort": args.sort,
    }
    result = _fetch("account", "txlist", params)

    if not isinstance(result, list):
        result = []

    data = {
        "address": args.address,
        "transactions": result,
        "count": len(result),
    }

    if write_output(data, args, summary=f"transactions for {_shorten_address(args.address)} ({len(result)} txs)"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Transactions for: {args.address}")
    print(f"Results: {len(result)}\n")

    for i, tx in enumerate(result, 1):
        from_addr = tx.get("from", "N/A")
        to_addr = tx.get("to", "N/A")
        value_wei = tx.get("value", "0")
        ts = tx.get("timeStamp", "")
        tx_hash = tx.get("hash", "N/A")
        is_error = tx.get("isError", "0")
        method_id = tx.get("methodId", "")
        func_name = tx.get("functionName", "")
        block = tx.get("blockNumber", "?")

        date_str = _format_timestamp(ts) if ts else "N/A"
        eth_value = _format_eth(value_wei)
        direction = "OUT" if from_addr.lower() == args.address.lower() else "IN"
        error_flag = " [FAILED]" if is_error == "1" else ""

        print(f"  [{i:3d}] {date_str} | Block {block}")
        print(f"        {direction}: {eth_value}{error_flag}")
        print(f"        From: {from_addr}")
        print(f"        To:   {to_addr}")
        if func_name:
            print(f"        Function: {func_name}")
        elif method_id and method_id != "0x":
            print(f"        Method: {method_id}")
        print(f"        Tx: {_shorten_address(tx_hash)}")
        if i < len(result):
            print()


def cmd_contract(args):
    """Get contract source code and ABI."""
    result = _fetch("contract", "getsourcecode", {"address": args.address})

    if isinstance(result, list) and len(result) > 0:
        info = result[0]
    elif isinstance(result, dict):
        info = result
    else:
        info = {}

    data = {
        "address": args.address,
        "contract": info,
    }

    if write_output(data, args, summary=f"contract {_shorten_address(args.address)}"):
        return

    if getattr(args, "json_out", False):
        print(json.dumps(data, indent=2))
        return

    print(f"Contract: {args.address}\n")

    name = info.get("ContractName", "N/A")
    compiler = info.get("CompilerVersion", "N/A")
    optimization = info.get("OptimizationUsed", "N/A")
    runs = info.get("Runs", "N/A")
    license_type = info.get("LicenseType", "N/A")
    proxy = info.get("Proxy", "0")
    implementation = info.get("Implementation", "")
    evm_version = info.get("EVMVersion", "")

    print(f"  Name: {name}")
    print(f"  Compiler: {compiler}")
    print(f"  Optimization: {'Yes' if optimization == '1' else 'No'} ({runs} runs)")
    if evm_version:
        print(f"  EVM Version: {evm_version}")
    print(f"  License: {license_type}")
    if proxy == "1":
        print(f"  Proxy: Yes")
        if implementation:
            print(f"  Implementation: {implementation}")

    # Source code summary (don't dump full code to terminal)
    source = info.get("SourceCode", "")
    if source:
        lines = source.count("\n") + 1
        print(f"  Source Code: {lines} lines (use --output to save full source)")
    else:
        print(f"  Source Code: Not verified")

    abi_str = info.get("ABI", "")
    if abi_str and abi_str != "Contract source code not verified":
        try:
            abi = json.loads(abi_str)
            funcs = [e for e in abi if e.get("type") == "function"]
            events = [e for e in abi if e.get("type") == "event"]
            print(f"  ABI: {len(funcs)} functions, {len(events)} events")
            if funcs:
                print(f"  Functions: {', '.join(f.get('name', '?') for f in funcs[:10])}")
                if len(funcs) > 10:
                    print(f"             ... and {len(funcs) - 10} more")
        except json.JSONDecodeError:
            print(f"  ABI: Present (parse error)")
    else:
        print(f"  ABI: Not available")


# -- CLI ---------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Etherscan API -- Ethereum blockchain analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # token-holders
    p = sub.add_parser("token-holders", help="Get top token holders for an ERC-20 contract")
    p.add_argument("contract_address", help="ERC-20 token contract address")
    p.add_argument("--limit", type=int, default=100, help="Number of holders to return (default 100)")
    p.add_argument("--page", type=int, default=1, help="Page number (default 1)")
    p.add_argument("--decimals", type=int, default=18, help="Token decimals for formatting (default 18)")
    add_output_args(p)

    # token-transfers
    p = sub.add_parser("token-transfers", help="Get token transfer events")
    p.add_argument("contract_address", help="ERC-20 token contract address")
    p.add_argument("--address", help="Filter by specific holder address")
    p.add_argument("--start-block", type=int, default=0, help="Start block (default 0)")
    p.add_argument("--end-block", type=int, default=99999999, help="End block (default 99999999)")
    p.add_argument("--limit", type=int, default=100, help="Max transfers to return (default 100)")
    p.add_argument("--page", type=int, default=1, help="Page number (default 1)")
    p.add_argument("--sort", choices=["asc", "desc"], default="desc", help="Sort order (default desc)")
    add_output_args(p)

    # token-info
    p = sub.add_parser("token-info", help="Get token metadata (name, symbol, supply, holders)")
    p.add_argument("contract_address", help="ERC-20 token contract address")
    add_output_args(p)

    # balance
    p = sub.add_parser("balance", help="Get ETH balance for an address")
    p.add_argument("address", help="Ethereum address")
    add_output_args(p)

    # token-balance
    p = sub.add_parser("token-balance", help="Get ERC-20 token balance for a specific address")
    p.add_argument("contract_address", help="ERC-20 token contract address")
    p.add_argument("address", help="Holder address to check")
    p.add_argument("--decimals", type=int, default=18, help="Token decimals for formatting (default 18)")
    add_output_args(p)

    # tx
    p = sub.add_parser("tx", help="Get transaction details by hash")
    p.add_argument("hash", help="Transaction hash")
    add_output_args(p)

    # address
    p = sub.add_parser("address", help="Get recent transactions for an address")
    p.add_argument("address", help="Ethereum address")
    p.add_argument("--start-block", type=int, default=0, help="Start block (default 0)")
    p.add_argument("--end-block", type=int, default=99999999, help="End block (default 99999999)")
    p.add_argument("--limit", type=int, default=50, help="Max transactions to return (default 50)")
    p.add_argument("--page", type=int, default=1, help="Page number (default 1)")
    p.add_argument("--sort", choices=["asc", "desc"], default="desc", help="Sort order (default desc)")
    add_output_args(p)

    # contract
    p = sub.add_parser("contract", help="Get contract source code and ABI")
    p.add_argument("address", help="Contract address")
    add_output_args(p)

    args = parser.parse_args()

    commands = {
        "token-holders": cmd_token_holders,
        "token-transfers": cmd_token_transfers,
        "token-info": cmd_token_info,
        "balance": cmd_balance,
        "token-balance": cmd_token_balance,
        "tx": cmd_tx,
        "address": cmd_address,
        "contract": cmd_contract,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
