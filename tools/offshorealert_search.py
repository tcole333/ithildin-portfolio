#!/usr/bin/env python3
"""Search OffshoreAlert — offshore court filings, investigative articles, MLATs, regulatory actions.

29K+ offshore court cases (Bahamas, Bermuda, BVI, Cayman Islands),
4,500+ investigative articles, 1,400+ MLATs, regulatory actions.

Requires OFFSHOREALERT_EMAIL and OFFSHOREALERT_PASSWORD in .env or env vars.

NOTE: Individual article pages and document downloads are gated behind reCAPTCHA
verification. The search page and WP REST API bypass this. For full article content
or PDF downloads, use a browser session (Playwright).

Usage:
    uv run python tools/offshorealert_search.py search "deutsche bank" -v
    uv run python tools/offshorealert_search.py search "leon black" --output /tmp/results.json
    uv run python tools/offshorealert_search.py search "liquid funding bermuda" -a
    uv run python tools/offshorealert_search.py api-search "epstein"
    uv run python tools/offshorealert_search.py entities "jeffrey epstein"  # extract tagged entities
"""

import os
import re
import json
import time
import argparse
from pathlib import Path
from urllib.parse import urljoin, quote_plus

import cloudscraper
from bs4 import BeautifulSoup

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

BASE_URL = "https://www.offshorealert.com"
LOGIN_URL = f"{BASE_URL}/my-account/"
SEARCH_URL = f"{BASE_URL}/"
API_SEARCH_URL = f"{BASE_URL}/wp-json/wp/v2/search"
DOWNLOAD_URL = f"{BASE_URL}/download/document/"

# Cache session across calls within one process
_session = None
_logged_in = False


def _get_credentials():
    """Load OffshoreAlert credentials from env or .env file."""
    email = os.environ.get("OFFSHOREALERT_EMAIL")
    password = os.environ.get("OFFSHOREALERT_PASSWORD")
    if email and password:
        return email, password

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("OFFSHOREALERT_EMAIL="):
                email = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("OFFSHOREALERT_PASSWORD="):
                password = line.split("=", 1)[1].strip().strip('"').strip("'")
    if email and password:
        return email, password
    raise RuntimeError(
        "OFFSHOREALERT_EMAIL and OFFSHOREALERT_PASSWORD not set. "
        "Add them to your .env file."
    )


def _get_session():
    """Get or create a cloudscraper session."""
    global _session
    if _session is None:
        _session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "desktop": True}
        )
    return _session


def login(force=False):
    """Login to OffshoreAlert. Returns True on success."""
    global _logged_in
    if _logged_in and not force:
        return True

    email, password = _get_credentials()
    session = _get_session()

    # Get login page to extract nonce
    resp = session.get(LOGIN_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the WooCommerce login nonce
    nonce_field = soup.find("input", {"name": "woocommerce-login-nonce"})
    if not nonce_field:
        # Try alternate nonce names
        nonce_field = soup.find("input", {"name": "_wpnonce"})
    nonce = nonce_field["value"] if nonce_field else ""

    # Submit login form
    login_data = {
        "username": email,
        "password": password,
        "woocommerce-login-nonce": nonce,
        "_wp_http_referer": "/my-account/",
        "login": "Log in",
        "rememberme": "forever",
    }
    resp = session.post(LOGIN_URL, data=login_data, timeout=30)
    resp.raise_for_status()

    # Check if login succeeded by looking for dashboard elements
    if "Log out" in resp.text or "my-account" in resp.url:
        _logged_in = True
        return True

    # Check for error messages
    soup = BeautifulSoup(resp.text, "html.parser")
    error = soup.find("ul", class_="woocommerce-error")
    if error:
        raise RuntimeError(f"Login failed: {error.get_text(strip=True)}")
    raise RuntimeError("Login failed: unknown error")


def _parse_search_results(html):
    """Parse search results from the HTML search page."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(class_="search-results")
    if not container:
        return [], 0

    articles = container.find_all("article")
    results = []

    for article in articles:
        result = {}

        # Post ID from class
        classes = article.get("class", [])
        for cls in classes:
            if cls.startswith("post-") and cls[5:].isdigit():
                result["post_id"] = int(cls[5:])
                break

        # Title and URL
        title_el = article.find(class_="oa-search-title")
        if title_el:
            a = title_el.find("a")
            if a:
                result["title"] = a.get_text(strip=True)
                result["url"] = a.get("href", "")

        # Date
        date_el = article.find(class_="oa-search-date")
        if date_el:
            date_span = date_el.find("span", class_="date")
            if date_span:
                result["date"] = date_span.get_text(strip=True)

            # Score and size from full text
            full_text = date_el.get_text(" ", strip=True)
            score_match = re.search(r"Score:\s*(\d+)%", full_text)
            if score_match:
                result["relevance_score"] = int(score_match.group(1))
            size_match = re.search(r"Size:\s*([\d.]+\s*(?:KB|MB|GB))", full_text)
            if size_match:
                result["file_size"] = size_match.group(1)

        # Excerpt
        content_el = article.find(class_="oa-search-content") or article.find("p")
        if content_el:
            result["excerpt"] = content_el.get_text(strip=True)[:500]

        # Categories and tags from CSS classes
        categories = [c.replace("category-", "") for c in classes if c.startswith("category-")]
        tags = [t.replace("tag-", "") for t in classes if t.startswith("tag-")]
        if categories:
            result["categories"] = categories
        if tags:
            result["tags"] = tags

        # Access status
        if "access-granted" in classes:
            result["access"] = "granted"
        elif "membership-content" in classes:
            result["access"] = "requires_subscription"

        if result.get("title"):
            results.append(result)

    # Try to get total from pagination
    total = len(results)
    # Check for page links to estimate total
    page_links = soup.find_all("a", class_="page-numbers")
    if page_links:
        max_page = 1
        for link in page_links:
            text = link.get_text(strip=True)
            if text.isdigit():
                max_page = max(max_page, int(text))
        total = max_page * 50  # approximate

    return results, total


def search(query, page=1, limit=50):
    """Search OffshoreAlert via the website search page.

    Returns (results, total_estimate) where results is a list of dicts.
    """
    login()
    session = _get_session()

    params = {"s": query}
    url = SEARCH_URL if page <= 1 else f"{BASE_URL}/page/{page}/"
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()

    results, total = _parse_search_results(resp.text)
    return results[:limit], total


def search_all(query, max_results=200):
    """Paginate through search results."""
    all_results = []
    page = 1
    total = None

    while len(all_results) < max_results:
        results, est_total = search(query, page=page)
        if total is None:
            total = est_total
        if not results:
            break
        all_results.extend(results)
        page += 1
        if len(results) < 50:
            break
        time.sleep(1)

    return all_results[:max_results], total or len(all_results)


def search_api(query, per_page=10, page=1):
    """Search via WP REST API (lighter, no auth needed, but less data)."""
    session = _get_session()
    params = {"search": query, "per_page": per_page, "page": page}
    resp = session.get(API_SEARCH_URL, params=params, timeout=30)
    resp.raise_for_status()

    total = int(resp.headers.get("X-WP-Total", 0))
    total_pages = int(resp.headers.get("X-WP-TotalPages", 0))
    results = resp.json()

    return [
        {
            "post_id": r["id"],
            "title": r["title"],
            "url": r["url"],
            "type": r.get("subtype", r.get("type", "post")),
        }
        for r in results
    ], total


def _parse_article_page(html):
    """Parse a single article/filing page for full details."""
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("article") or soup.find("main")
    if not article:
        return {}

    result = {}

    # Title
    h1 = article.find("h1")
    if h1:
        result["title"] = h1.get_text(strip=True)

    # Breadcrumb categories
    breadcrumbs = article.find("ol") or article.find("ul", class_="breadcrumb")
    if breadcrumbs:
        result["breadcrumb"] = [
            li.get_text(strip=True) for li in breadcrumbs.find_all("li") if li.get_text(strip=True)
        ]

    # Category/topic links in the header area
    topic_links = []
    topics_heading = article.find("h3", string=re.compile(r"Topics?", re.I))
    if topics_heading:
        parent = topics_heading.parent
        if parent:
            for a in parent.find_all("a"):
                topic_links.append({
                    "name": a.get_text(strip=True),
                    "url": a.get("href", ""),
                })
    if topic_links:
        result["topics"] = topic_links

    # Keywords/tags
    keywords = []
    kw_heading = article.find("h3", string=re.compile(r"Keywords?", re.I))
    if kw_heading:
        parent = kw_heading.parent
        if parent:
            for a in parent.find_all("a"):
                keywords.append({
                    "name": a.get_text(strip=True),
                    "url": a.get("href", ""),
                })
    if keywords:
        result["keywords"] = keywords

    # Article body content — all paragraphs
    paragraphs = article.find_all("p")
    body_text = []
    for p in paragraphs:
        text = p.get_text(strip=True)
        if text and len(text) > 20:
            body_text.append(text)
    if body_text:
        result["content"] = "\n\n".join(body_text)

    # Document metadata (for filings)
    for label_text in ["Pages:", "Date:", "Allegation:", "Case Number:",
                       "Defendants/Respondents:", "Plaintiffs/Applicants:",
                       "Court:", "Source:", "Value Range:"]:
        label_el = article.find(string=re.compile(re.escape(label_text)))
        if label_el:
            parent = label_el.parent
            if parent:
                # Get the sibling text
                full_text = parent.parent.get_text(" ", strip=True) if parent.parent else parent.get_text(" ", strip=True)
                value = full_text.replace(label_text, "").strip()
                key = label_text.rstrip(":").lower().replace("/", "_").replace(" ", "_")
                if value:
                    result[key] = value

    # Download link
    download_link = article.find("a", href=re.compile(r"/download/document/"))
    if download_link:
        result["download_url"] = download_link.get("href", "")
        # File info
        size_el = article.find(string=re.compile(r"File Size:"))
        if size_el:
            size_text = size_el.strip() if isinstance(size_el, str) else size_el.get_text(strip=True)
            result["file_size"] = size_text.replace("File Size:", "").strip()

    # Related content
    related_heading = article.find("h3", string=re.compile(r"Related Content", re.I))
    if related_heading:
        related_container = related_heading.parent
        if related_container:
            related = []
            for a in related_container.find_all("a"):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if href and text and "offshorealert.com" in href:
                    related.append({"title": text, "url": href})
            if related:
                result["related"] = related[:10]

    return result


def get_article(url):
    """Fetch and parse a single article page.

    NOTE: May fail due to reCAPTCHA on individual pages. Use search for discovery.
    """
    login()
    session = _get_session()
    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    # Check for reCAPTCHA redirect
    if "verify.php" in resp.url:
        return {
            "source_url": url,
            "error": "reCAPTCHA verification required. Use Playwright browser for full article access.",
        }

    result = _parse_article_page(resp.text)
    result["source_url"] = url
    return result


def extract_entities(query, max_results=200):
    """Extract all tagged entities (people, companies, orgs) from search results.

    Returns a dict of entity_slug -> {name, count, filings} aggregated across results.
    This is the primary way to discover connected entities in OffshoreAlert.
    """
    results, total = search_all(query, max_results=max_results)

    entities = {}  # slug -> {name, count, categories, filings}
    jurisdictions = {}  # slug -> count

    for r in results:
        tags = r.get("tags", [])
        categories = r.get("categories", [])
        title = r.get("title", "")

        for tag in tags:
            slug = tag
            if slug not in entities:
                # Convert slug to display name
                name = slug.replace("-", " ").title()
                entities[slug] = {"name": name, "count": 0, "filings": []}
            entities[slug]["count"] += 1
            entities[slug]["filings"].append({
                "title": title,
                "url": r.get("url", ""),
                "date": r.get("date", ""),
            })

        for cat in categories:
            # Jurisdiction categories (single-word or hyphenated country names)
            if cat in _KNOWN_JURISDICTIONS or any(cat.startswith(p) for p in ["u-s-", "british-", "cayman-", "channel-"]):
                jurisdictions[cat] = jurisdictions.get(cat, 0) + 1

    return {
        "query": query,
        "total_results": total,
        "results_analyzed": len(results),
        "entities": entities,
        "jurisdictions": jurisdictions,
    }


# Common jurisdiction category slugs
_KNOWN_JURISDICTIONS = {
    "usa", "bermuda", "cayman-islands", "british-virgin-islands", "bahamas",
    "switzerland", "hong-kong", "singapore", "malta", "cyprus", "ireland",
    "austria", "germany", "united-kingdom", "united-arab-emirates", "dubai",
    "luxembourg", "liechtenstein", "monaco", "panama", "isle-of-man",
    "jersey", "guernsey", "gibraltar", "mauritius", "seychelles",
    "u-s-virgin-islands", "marshall-islands", "st-kitts-nevis",
    "channel-islands", "antigua-and-barbuda", "belize", "costa-rica",
    "russia", "china", "india", "japan", "south-korea", "australia",
    "new-zealand", "canada", "mexico", "brazil", "spain", "france",
    "italy", "netherlands", "belgium", "sweden", "norway", "denmark",
    "finland", "israel", "south-africa", "nigeria", "kenya", "tanzania",
    "qatar", "saudi-arabia", "kuwait", "bahrain", "oman",
    "malaysia", "indonesia", "thailand", "philippines", "vietnam",
    "estonia", "latvia", "lithuania", "poland", "czech-republic",
    "hungary", "romania", "bulgaria", "croatia", "serbia",
}


def print_results(results, total, query, verbose=False):
    """Pretty-print search results."""
    print(f"\n{'='*80}")
    print(f"OffshoreAlert Search: {query}")
    print(f"Total: ~{total} | Showing: {len(results)}")
    print(f"{'='*80}\n")

    for i, r in enumerate(results, 1):
        score = f" (score: {r['relevance_score']}%)" if "relevance_score" in r else ""
        size = f" [{r['file_size']}]" if "file_size" in r else ""
        date = r.get("date", "")
        print(f"--- [{i}] {r.get('title', 'Untitled')}{score}{size} ---")
        print(f"    Date: {date}")
        print(f"    URL: {r.get('url', '')}")

        if r.get("categories"):
            cats = ", ".join(r["categories"][:8])
            print(f"    Categories: {cats}")
        if r.get("tags"):
            tags = ", ".join(r["tags"][:8])
            print(f"    Tags: {tags}")

        if verbose and r.get("excerpt"):
            print(f"    {r['excerpt'][:300]}")

        print()


def print_article(article):
    """Pretty-print a single article."""
    print(f"\n{'='*80}")
    print(f"Title: {article.get('title', 'Unknown')}")
    print(f"URL: {article.get('source_url', '')}")
    print(f"{'='*80}\n")

    for key in ["date", "case_number", "defendants_respondents", "plaintiffs_applicants",
                 "allegation", "pages", "file_size", "court", "value_range"]:
        if key in article:
            label = key.replace("_", " ").title()
            print(f"  {label}: {article[key]}")

    if article.get("topics"):
        print(f"  Topics: {', '.join(t['name'] for t in article['topics'])}")
    if article.get("keywords"):
        print(f"  Keywords: {', '.join(k['name'] for k in article['keywords'])}")

    if article.get("download_url"):
        print(f"\n  Download: {article['download_url']}")

    if article.get("content"):
        print(f"\n--- Content ---\n{article['content'][:2000]}")

    if article.get("related"):
        print(f"\n--- Related ({len(article['related'])}) ---")
        for rel in article["related"]:
            print(f"  - {rel['title']}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Search OffshoreAlert (offshore court filings, articles, MLATs, regulatory actions)"
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # Search command
    search_p = sub.add_parser("search", help="Search OffshoreAlert")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("-n", "--limit", type=int, default=50, help="Max results per page")
    search_p.add_argument("-p", "--page", type=int, default=1, help="Page number")
    search_p.add_argument("-a", "--all", action="store_true", help="Fetch all results (paginated)")
    search_p.add_argument("-v", "--verbose", action="store_true", help="Show excerpts")
    add_output_args(search_p)

    # API search command
    api_p = sub.add_parser("api-search", help="Search via WP REST API (no login needed)")
    api_p.add_argument("query", help="Search query")
    api_p.add_argument("-n", "--limit", type=int, default=10, help="Max results")
    add_output_args(api_p)

    # Entities command
    ent_p = sub.add_parser("entities", help="Extract tagged entities from search results")
    ent_p.add_argument("query", help="Search query")
    ent_p.add_argument("-n", "--limit", type=int, default=200, help="Max results to analyze")
    add_output_args(ent_p)

    # Article command
    art_p = sub.add_parser("article", help="Fetch full article details (may hit reCAPTCHA)")
    art_p.add_argument("url", help="Article URL")
    add_output_args(art_p)

    args = parser.parse_args()

    if args.command == "search":
        if args.all:
            results, total = search_all(args.query, max_results=args.limit or 200)
        else:
            results, total = search(args.query, page=args.page, limit=args.limit)
        data = {"query": args.query, "total": total, "results": results}
        if not write_output(data, args, summary=f"OffshoreAlert '{args.query}': {len(results)}/~{total}"):
            if getattr(args, "json_out", False):
                print(json.dumps(data, indent=2, default=str))
            else:
                print_results(results, total, args.query, verbose=args.verbose)

    elif args.command == "entities":
        data = extract_entities(args.query, max_results=args.limit)
        entities = data["entities"]
        jurisdictions = data["jurisdictions"]

        summary = f"OffshoreAlert entities '{args.query}': {len(entities)} entities, {len(jurisdictions)} jurisdictions"
        if not write_output(data, args, summary=summary):
            if getattr(args, "json_out", False):
                print(json.dumps(data, indent=2, default=str))
            else:
                print(f"\n{'='*80}")
                print(f"OffshoreAlert Entity Extraction: {args.query}")
                print(f"Results analyzed: {data['results_analyzed']}/~{data['total_results']}")
                print(f"{'='*80}\n")

                # Sort entities by count
                sorted_ents = sorted(entities.items(), key=lambda x: x[1]["count"], reverse=True)
                print(f"ENTITIES ({len(sorted_ents)}):")
                for slug, info in sorted_ents:
                    print(f"  {info['name']} ({info['count']} filings)")

                if jurisdictions:
                    sorted_juris = sorted(jurisdictions.items(), key=lambda x: x[1], reverse=True)
                    print(f"\nJURISDICTIONS ({len(sorted_juris)}):")
                    for slug, count in sorted_juris:
                        print(f"  {slug.replace('-', ' ').title()}: {count}")

    elif args.command == "api-search":
        results, total = search_api(args.query, per_page=args.limit)
        data = {"query": args.query, "total": total, "results": results}
        if not write_output(data, args, summary=f"OffshoreAlert API '{args.query}': {len(results)}/{total}"):
            if getattr(args, "json_out", False):
                print(json.dumps(data, indent=2, default=str))
            else:
                for r in results:
                    print(f"  [{r['post_id']}] {r['title']}")
                    print(f"       {r['url']}")
                print(f"\nTotal: {total}")

    elif args.command == "article":
        article = get_article(args.url)
        if not write_output(article, args, summary=f"OffshoreAlert article: {article.get('title', 'Unknown')}"):
            if getattr(args, "json_out", False):
                print(json.dumps(article, indent=2, default=str))
            else:
                print_article(article)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
