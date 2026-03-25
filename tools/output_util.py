#!/usr/bin/env python3
"""Shared output utility for investigation tools.

Provides --output FILE flag that writes JSON to a file and prints a 1-line
summary to stdout, keeping agent conversation context lean.

Usage in tools:
    from tools.output_util import add_output_args, write_output

    # In argparse setup:
    add_output_args(parser)          # global flag
    add_output_args(search_parser)   # or per-subparser

    # In output section:
    if not write_output(results, args, summary="DOJ search 'bannon'"):
        # existing pretty-print code (unchanged)
        ...
"""

import json


def add_output_args(parser):
    """Add --output (and --json if missing) to an argparse parser."""
    existing = {a.dest for a in parser._actions}
    if "output" not in existing:
        parser.add_argument(
            "--output", metavar="FILE",
            help="Write JSON results to FILE (prints 1-line summary to stdout)",
        )
    if "json" not in existing and "json_out" not in existing:
        parser.add_argument(
            "--json", action="store_true", dest="json_out",
            help="Output raw JSON to stdout",
        )


def write_output(data, args, summary=None):
    """If --output is set, write JSON to file and print summary. Returns True if written.

    Args:
        data: The data to serialize (list, dict, or other JSON-serializable).
        args: Parsed argparse namespace (checks args.output).
        summary: Optional description for the summary line.
              If omitted, uses a generic count-based message.
    """
    output_path = getattr(args, "output", None)
    if not output_path:
        return False

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    # Build summary line
    if isinstance(data, list):
        count = len(data)
    elif isinstance(data, dict):
        # Try common list-valued keys
        for key in ("results", "hits", "articles", "items", "records", "data"):
            if key in data and isinstance(data[key], list):
                count = len(data[key])
                break
        else:
            count = 1
    else:
        count = 1

    desc = f" ({summary})" if summary else ""
    print(f"{count} results{desc} saved to {output_path}")
    return True
