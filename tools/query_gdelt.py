#!/usr/bin/env python3
"""
GDELT 2.0 API wrapper for OSINT investigations.

Searches the Global Database of Events, Language, and Tone — global news
coverage tracking. Useful for media analysis of Epstein-related persons,
sentiment tracking, geographic distribution, and co-mention networks.

NOTE: GDELT DOC/Context APIs have a ~3-month rolling window only.
Historical data requires BigQuery access (deferred enhancement).

Usage:
    python tools/query_gdelt.py articles "Jeffrey Epstein" --limit 50 --timespan 3m
    python tools/query_gdelt.py articles "Epstein" --domain nytimes.com --tone-below -5
    python tools/query_gdelt.py context "Epstein arrest" --timespan 1w --limit 100
    python tools/query_gdelt.py timeline "Jeffrey Epstein" --mode volume
    python tools/query_gdelt.py timeline "Jeffrey Epstein" --mode tone
    python tools/query_gdelt.py geo "Jeffrey Epstein"
    python tools/query_gdelt.py cooccurrence "Jeffrey Epstein" --targets "Bannon,Gates,Wexner"
"""

import argparse
import csv
import io
import json
import sys
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

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


DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
CONTEXT_URL = "https://api.gdeltproject.org/api/v2/context/context"
GEO_URL = "https://api.gdeltproject.org/api/v2/geo/geo"

USER_AGENT = "Mozilla/5.0 (compatible; OSINT-Research/1.0)"
RATE_LIMIT_DELAY = 6.0  # GDELT enforces 1 request per 5 seconds; 6s for safety


_last_request_time = 0


def _request(url, as_json=True, _retries=1):
    """Make an API request to GDELT, respecting global rate limit."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_DELAY:
        time.sleep(RATE_LIMIT_DELAY - elapsed)

    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json" if as_json else "*/*",
    })
    try:
        _last_request_time = time.time()
        with urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            if as_json:
                return json.loads(data)
            return data
    except HTTPError as e:
        body = e.read().decode()[:500]
        if e.code == 429 and _retries > 0:
            print(f"  Rate limited, waiting {RATE_LIMIT_DELAY + 2}s...", file=sys.stderr)
            time.sleep(RATE_LIMIT_DELAY + 2)
            _last_request_time = time.time()
            return _request(url, as_json=as_json, _retries=_retries - 1)
        print(f"ERROR: HTTP {e.code} from GDELT: {body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot reach GDELT: {e.reason}", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        # Context API returns HTML/text for invalid timespans
        print("ERROR: Non-JSON response from GDELT (check timespan format)", file=sys.stderr)
        return None


def _context_timespan(ts):
    """Convert DOC API shorthand timespan to Context API full-word format.

    DOC API uses: 15min, 1h, 1d, 1w, 1m, 3m
    Context API uses: 1hours, 3days, 3months, FULL
    Context API max in days unit is 3. Weeks not supported.
    """
    if not ts or ts.upper() == "FULL":
        return "FULL"
    import re
    m = re.match(r'^(\d+)(min|h|d|w|m)$', ts.lower())
    if not m:
        return ts  # pass through as-is (user may already use full-word format)
    num, unit = int(m.group(1)), m.group(2)
    if unit == "min":
        return f"{num}hours"  # Context API min is ~1 hour; round up
    elif unit == "h":
        return f"{num}hours"
    elif unit == "d":
        return f"{min(num, 3)}days"  # Context API max is 3 days
    elif unit == "w":
        return f"{min(num, 3)}months"  # weeks not supported; use months
    elif unit == "m":
        return f"{num}months"
    return ts


def _build_query(query, domain=None, source_country=None, source_lang=None, tone_below=None):
    """Build a GDELT query string with optional filters."""
    parts = [query]
    if domain:
        parts.append(f"domain:{domain}")
    if source_country:
        parts.append(f"sourcecountry:{source_country}")
    if source_lang:
        parts.append(f"sourcelang:{source_lang}")
    if tone_below is not None:
        parts.append(f"tone<{tone_below}")
    return " ".join(parts)


def cmd_articles(args):
    """Search articles via DOC 2.0 API (artlist mode)."""
    query = _build_query(
        args.query, domain=args.domain, source_country=args.source_country,
        source_lang=args.source_lang, tone_below=args.tone_below,
    )

    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": min(args.limit, 250),
        "format": "json",
    }
    if args.timespan:
        params["timespan"] = args.timespan
    if args.sort:
        sort_map = {"date": "DateDesc", "rel": "HybridRel", "tone": "ToneDesc"}
        params["sort"] = sort_map.get(args.sort, args.sort)

    url = f"{DOC_URL}?{urlencode(params)}"
    data = _request(url)
    if not data:
        return

    articles = data.get("articles", [])
    _log(args.query, "gdelt", len(articles))
    print(f"GDELT articles for '{args.query}': {len(articles)} results")
    print()

    for i, art in enumerate(articles, 1):
        title = art.get("title", "?")
        url_val = art.get("url", "")
        date = art.get("seendate", "?")
        domain_val = art.get("domain", "?")
        country = art.get("sourcecountry", "?")
        lang = art.get("language", "?")
        tone = art.get("tone", 0)

        # Format tone with +/- indicator
        tone_str = f"{tone:+.1f}" if isinstance(tone, (int, float)) else str(tone)

        print(f"  [{i}] {title}")
        print(f"      Date: {date} | Domain: {domain_val} | Country: {country} | Lang: {lang}")
        print(f"      Tone: {tone_str}")
        print(f"      URL: {url_val}")
        print()

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_context(args):
    """Search sentence-level snippets via Context 2.0 API.

    NOTE: Context API has a ~3 day rolling window for day-based timespans.
    Uses full-word timespan format (3months, 3days) not DOC shorthand (3m, 3d).
    """
    params = {
        "query": args.query,
        "mode": "artlist",
        "maxrecords": min(args.limit, 200),
        "format": "json",
    }
    if args.timespan:
        params["timespan"] = _context_timespan(args.timespan)

    url = f"{CONTEXT_URL}?{urlencode(params)}"
    data = _request(url)
    if not data:
        return

    articles = data.get("articles", [])
    _log(args.query, "gdelt", len(articles))
    print(f"GDELT context for '{args.query}': {len(articles)} snippets")
    print()

    for i, art in enumerate(articles, 1):
        url_val = art.get("url", "")
        date = art.get("seendate", "?")
        domain = art.get("domain", "?")
        context = art.get("context", "")

        # Context may be a list of sentences or a single string
        if isinstance(context, list):
            context = " ".join(context)

        # Truncate long contexts
        if len(context) > 300:
            context = context[:297] + "..."

        print(f"  [{i}] {domain} ({date})")
        print(f"      {context}")
        print(f"      URL: {url_val}")
        print()

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_timeline(args):
    """Generate article count or tone timeline via DOC 2.0 API."""
    mode_map = {
        "volume": "timelinevol",
        "tone": "timelinetone",
    }
    mode = mode_map.get(args.mode, args.mode)

    params = {
        "query": args.query,
        "mode": mode,
        "format": "json",
    }
    if args.timespan:
        params["timespan"] = args.timespan

    url = f"{DOC_URL}?{urlencode(params)}"
    data = _request(url)
    if not data:
        return

    timeline = data.get("timeline", [])
    if not timeline:
        print(f"No timeline data for '{args.query}'")
        return

    # Timeline is typically a list of series, each with data points
    print(f"GDELT timeline ({args.mode}) for '{args.query}':")
    print()

    for series in timeline:
        series_name = series.get("series", "")
        data_points = series.get("data", [])
        if series_name:
            print(f"  Series: {series_name}")

        for dp in data_points:
            date = dp.get("date", "?")
            value = dp.get("value", dp.get("norm", 0))
            # Simple bar chart
            bar_len = min(int(abs(value) / 5), 60) if isinstance(value, (int, float)) else 0
            bar = "#" * bar_len
            print(f"    {date}: {value:>8.1f} {bar}")
        print()

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_geo(args):
    """Geographic distribution via GEO 2.0 API.

    NOTE: GEO API may return empty for person-name queries.
    Works best with event/topic queries that have geographic dispersion.
    """
    params = {
        "query": args.query,
        "mode": "PointData",
        "format": "GeoJSON",
    }
    if args.timespan:
        params["timespan"] = args.timespan

    url = f"{GEO_URL}?{urlencode(params)}"
    data = _request(url)
    if not data:
        return

    features = data.get("features", [])
    if not features:
        print(f"No geographic data for '{args.query}'")
        return

    print(f"GDELT geographic distribution for '{args.query}': {len(features)} locations")
    print()

    # Aggregate by country/location name
    locations = {}
    for feat in features:
        props = feat.get("properties", {})
        name = props.get("name", props.get("html", "?"))
        count = props.get("count", props.get("urlcount", 1))
        # Clean name
        if "<" in name:
            # Strip HTML tags
            import re
            name = re.sub(r"<[^>]+>", "", name).strip()
        if name in locations:
            locations[name] += count
        else:
            locations[name] = count

    # Sort by count descending
    for name, count in sorted(locations.items(), key=lambda x: -x[1])[:30]:
        bar = "#" * min(count, 50)
        print(f"  {name:<40} {count:>6} {bar}")

    if args.json_out:
        print(json.dumps(data, indent=2, default=str))


def cmd_cooccurrence(args):
    """Measure co-mention volumes between a subject and multiple targets.

    Uses timelinevol to sum daily article counts (one request per target).
    """
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    if not targets:
        print("ERROR: --targets must be a comma-separated list of names", file=sys.stderr)
        sys.exit(1)

    print(f"GDELT co-occurrence: '{args.query}' with {len(targets)} targets")
    if args.timespan:
        print(f"  Timespan: {args.timespan}")
    print()

    results = []
    for i, target in enumerate(targets):
        # Use near10 proximity operator
        coquery = f'near10:"{args.query} {target}"'
        params = {
            "query": coquery,
            "mode": "timelinevol",
            "format": "json",
        }
        if args.timespan:
            params["timespan"] = args.timespan

        url = f"{DOC_URL}?{urlencode(params)}"
        data = _request(url)

        count = 0
        if data and "timeline" in data:
            for series in data["timeline"]:
                for dp in series.get("data", []):
                    count += dp.get("value", dp.get("norm", 0))
            count = int(count)

        results.append((target, count))

    # Sort by count descending
    results.sort(key=lambda x: -x[1])

    max_count = max(r[1] for r in results) if results else 1
    for target, count in results:
        bar_len = int((count / max(max_count, 1)) * 40)
        bar = "#" * bar_len
        print(f"  {target:<30} {count:>8} {bar}")

    if args.json_out:
        print(json.dumps(results, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(
        description="GDELT 2.0 API for OSINT media analysis",
        epilog="Query syntax: \"exact phrase\", (A OR B), near5:\"w1 w2\", tone<-5, "
               "sourcecountry:US, sourcelang:French, domain:nytimes.com"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # articles
    p = sub.add_parser("articles", help="Search articles (DOC API, artlist mode)")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=50, help="Max results (max 250)")
    p.add_argument("--timespan", help="Time range: e.g., 1w, 1m, 3m, 15min")
    p.add_argument("--domain", help="Filter by domain (e.g., nytimes.com)")
    p.add_argument("--source-country", help="Filter by source country (e.g., US, NO, IL)")
    p.add_argument("--source-lang", help="Filter by source language (e.g., English, French)")
    p.add_argument("--tone-below", type=float, help="Only articles with tone below threshold (e.g., -5)")
    p.add_argument("--sort", choices=["date", "rel", "tone"], default="date",
                   help="Sort order: date (newest), rel (relevance), tone (most negative)")
    add_output_args(p)

    # context
    p = sub.add_parser("context", help="Sentence-level snippets (Context API)")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=50, help="Max results (max 200)")
    p.add_argument("--timespan", help="Time range: e.g., 1w, 1m, 3m")
    add_output_args(p)

    # timeline
    p = sub.add_parser("timeline", help="Article count or tone over time")
    p.add_argument("query")
    p.add_argument("--mode", choices=["volume", "tone"], default="volume",
                   help="volume = daily article counts, tone = average sentiment per day")
    p.add_argument("--timespan", help="Time range: e.g., 1w, 1m, 3m")
    add_output_args(p)

    # geo
    p = sub.add_parser("geo", help="Geographic distribution of coverage")
    p.add_argument("query")
    p.add_argument("--timespan", help="Time range: e.g., 1w, 1m, 3m")
    add_output_args(p)

    # cooccurrence
    p = sub.add_parser("cooccurrence", help="Co-mention volumes with multiple targets")
    p.add_argument("query", help="Subject (e.g., 'Jeffrey Epstein')")
    p.add_argument("--targets", required=True,
                   help="Comma-separated target names (e.g., 'Bannon,Gates,Wexner')")
    p.add_argument("--timespan", help="Time range: e.g., 1w, 1m, 3m")
    add_output_args(p)

    args = parser.parse_args()
    if not hasattr(args, "json_out"):
        args.json_out = False

    handlers = {
        "articles": cmd_articles,
        "context": cmd_context,
        "timeline": cmd_timeline,
        "geo": cmd_geo,
        "cooccurrence": cmd_cooccurrence,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
