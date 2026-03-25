"""
CourtListener API client with rate limiting.

Provides authenticated access to CourtListener's REST API v4 with:
- Rate limiting (5,000 requests/hour)
- Automatic pagination
- Retry logic for transient failures

API Docs: https://www.courtlistener.com/help/api/rest/
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class RateLimiter:
    """Simple rate limiter for API requests."""

    max_requests: int = 5000  # Per hour
    window_seconds: int = 3600  # 1 hour
    request_times: list = field(default_factory=list)

    def wait_if_needed(self) -> None:
        """Wait if we're approaching the rate limit."""
        now = datetime.now()
        cutoff = now - timedelta(seconds=self.window_seconds)

        # Remove old requests
        self.request_times = [t for t in self.request_times if t > cutoff]

        # Check if we need to wait
        if len(self.request_times) >= self.max_requests:
            oldest = min(self.request_times)
            wait_until = oldest + timedelta(seconds=self.window_seconds)
            wait_seconds = (wait_until - now).total_seconds()
            if wait_seconds > 0:
                logger.warning(f"Rate limit reached, waiting {wait_seconds:.1f}s")
                time.sleep(wait_seconds)

        self.request_times.append(now)

    def requests_remaining(self) -> int:
        """Get approximate requests remaining in current window."""
        now = datetime.now()
        cutoff = now - timedelta(seconds=self.window_seconds)
        self.request_times = [t for t in self.request_times if t > cutoff]
        return max(0, self.max_requests - len(self.request_times))


class CourtListenerClient:
    """
    Client for CourtListener REST API v4.

    Requires an API token from https://www.courtlistener.com/

    Example usage:
        client = CourtListenerClient(token="your-api-token")

        # Search for cases
        results = client.search("Enron bankruptcy")

        # Get a specific docket
        docket = client.get_docket(12345)

        # Get parties for a docket
        parties = client.get_parties(docket_id=12345)

        # Search financial disclosures
        disclosures = client.get_financial_disclosures(person_id=1213)
    """

    BASE_URL = "https://www.courtlistener.com/api/rest/v4"

    # Court type filters
    FEDERAL_COURTS = [
        "scotus",  # Supreme Court
        "ca1", "ca2", "ca3", "ca4", "ca5", "ca6", "ca7", "ca8", "ca9", "ca10", "ca11", "cadc", "cafc",  # Circuit Courts
        # District courts use format like "dcd", "nysd", "cacd", etc.
    ]

    def __init__(
        self,
        token: Optional[str] = None,
        rate_limit: int = 5000,
    ):
        """
        Initialize the client.

        Args:
            token: CourtListener API token (or set COURTLISTENER_TOKEN env var)
            rate_limit: Max requests per hour (default 5000)
        """
        self.token = token or os.environ.get("COURTLISTENER_TOKEN")
        if not self.token:
            logger.warning(
                "No CourtListener API token provided. "
                "Set COURTLISTENER_TOKEN env var or pass token parameter. "
                "Anonymous requests have severe rate limits."
            )

        self.session = requests.Session()
        if self.token:
            self.session.headers["Authorization"] = f"Token {self.token}"

        self.session.headers["User-Agent"] = "offshore-leaks-research/1.0"
        self.rate_limiter = RateLimiter(max_requests=rate_limit)

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        retries: int = 3,
    ) -> dict:
        """
        Make a rate-limited API request.

        Args:
            method: HTTP method
            endpoint: API endpoint (without base URL)
            params: Query parameters
            json_body: JSON body for POST requests (e.g., citation-lookup)
            retries: Number of retries for transient failures

        Returns:
            JSON response data
        """
        self.rate_limiter.wait_if_needed()

        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"

        for attempt in range(retries):
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body
                )

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning(f"Rate limited, waiting {retry_after}s")
                    time.sleep(retry_after)
                    continue

                if response.status_code == 401:
                    raise ValueError(
                        "Authentication failed. Check your COURTLISTENER_TOKEN."
                    )

                if response.status_code == 403:
                    raise PermissionError(
                        f"Access denied for {endpoint}. This endpoint requires "
                        "'select user' access (contact mike@free.law). "
                        "Use the search API with field operators as a workaround."
                    )

                response.raise_for_status()
                return response.json()

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"Request failed, retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    raise

        return {}

    def _paginate(
        self,
        endpoint: str,
        params: Optional[dict] = None,
        max_results: Optional[int] = None,
    ) -> Iterator[dict]:
        """
        Paginate through API results.

        Args:
            endpoint: API endpoint
            params: Query parameters
            max_results: Maximum results to return

        Yields:
            Individual result objects
        """
        params = params or {}
        count = 0

        while True:
            data = self._request("GET", endpoint, params)

            for result in data.get("results", []):
                yield result
                count += 1
                if max_results and count >= max_results:
                    return

            # Check for next page
            next_url = data.get("next")
            if not next_url:
                break

            # Extract cursor/page from next URL
            # CourtListener uses cursor-based pagination for id/date ordering
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(next_url)
            next_params = parse_qs(parsed.query)
            params = {k: v[0] for k, v in next_params.items()}

    # =========================================================================
    # Search API
    # =========================================================================

    def search(
        self,
        query: str,
        search_type: str = "o",  # o=opinions, r=recap/dockets, p=people
        court: Optional[str] = None,
        max_results: int = 100,
        **kwargs,
    ) -> list[dict]:
        """
        Search CourtListener.

        Args:
            query: Search query
            search_type: Type of search (o=opinions, r=recap, p=people, rd=recap docs)
            court: Filter to specific court (e.g., "scotus", "ca9")
            max_results: Maximum results to return
            **kwargs: Additional filter parameters

        Returns:
            List of search results
        """
        params = {"q": query, "type": search_type}
        if court:
            params["court"] = court
        params.update(kwargs)

        results = list(self._paginate("search/", params, max_results))
        logger.info(f"Search '{query}' returned {len(results)} results")
        return results

    def search_cases(
        self,
        query: str,
        court: Optional[str] = None,
        date_filed_after: Optional[str] = None,
        date_filed_before: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """
        Search for court cases/dockets.

        Args:
            query: Search query
            court: Court filter
            date_filed_after: Filter cases filed after this date (YYYY-MM-DD)
            date_filed_before: Filter cases filed before this date
            max_results: Maximum results

        Returns:
            List of docket results
        """
        params = {}
        if date_filed_after:
            params["date_filed__gte"] = date_filed_after
        if date_filed_before:
            params["date_filed__lte"] = date_filed_before

        return self.search(
            query,
            search_type="r",  # RECAP/dockets
            court=court,
            max_results=max_results,
            **params,
        )

    # =========================================================================
    # Docket API
    # =========================================================================

    def get_docket(self, docket_id: int) -> dict:
        """
        Get a specific docket by ID.

        Args:
            docket_id: CourtListener docket ID

        Returns:
            Docket details
        """
        return self._request("GET", f"dockets/{docket_id}/")

    def get_dockets(
        self,
        court: Optional[str] = None,
        date_filed_after: Optional[str] = None,
        date_filed_before: Optional[str] = None,
        max_results: int = 100,
        **kwargs,
    ) -> list[dict]:
        """
        Get dockets with filters.

        Args:
            court: Court filter (e.g., "scotus", "nysd")
            date_filed_after: Filter by filing date
            date_filed_before: Filter by filing date
            max_results: Maximum results
            **kwargs: Additional filters

        Returns:
            List of dockets
        """
        params = {}
        if court:
            params["court"] = court
        if date_filed_after:
            params["date_filed__gte"] = date_filed_after
        if date_filed_before:
            params["date_filed__lte"] = date_filed_before
        params.update(kwargs)

        return list(self._paginate("dockets/", params, max_results))

    # =========================================================================
    # Party / Attorney / Firm Search (via Search API field operators)
    # The /parties/ and /attorneys/ REST endpoints require "select user"
    # access. These methods use the search API instead, which returns party,
    # attorney, and firm data embedded in RECAP search results.
    # =========================================================================

    def search_by_party(
        self,
        party_name: str,
        court: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Search for cases involving a specific party."""
        query = f'party:"{party_name}"'
        return self.search(query, search_type="r", court=court, max_results=max_results)

    def search_by_attorney(
        self,
        attorney_name: str,
        court: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Search for cases involving a specific attorney."""
        query = f'attorney:"{attorney_name}"'
        return self.search(query, search_type="r", court=court, max_results=max_results)

    def search_by_firm(
        self,
        firm_name: str,
        court: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Search for cases involving a specific law firm."""
        query = f'firm:"{firm_name}"'
        return self.search(query, search_type="r", court=court, max_results=max_results)

    # =========================================================================
    # Opinions API
    # =========================================================================

    def get_opinion(self, opinion_id: int) -> dict:
        """Get a specific opinion."""
        return self._request("GET", f"opinions/{opinion_id}/")

    def get_opinions(
        self,
        court: Optional[str] = None,
        date_filed_after: Optional[str] = None,
        date_filed_before: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Get opinions with filters."""
        params = {}
        if court:
            params["cluster__docket__court"] = court
        if date_filed_after:
            params["cluster__date_filed__gte"] = date_filed_after
        if date_filed_before:
            params["cluster__date_filed__lte"] = date_filed_before

        return list(self._paginate("opinions/", params, max_results))

    # =========================================================================
    # People / Judges API
    # =========================================================================

    def get_person(self, person_id: int) -> dict:
        """Get a person (usually a judge) by ID."""
        return self._request("GET", f"people/{person_id}/")

    def search_judges(
        self,
        name: str,
        court: Optional[str] = None,
        max_results: int = 50,
    ) -> list[dict]:
        """Search judges via the search API (type=p). Most reliable for name search."""
        return self.search(name, search_type="p", court=court, max_results=max_results)

    def list_people(
        self,
        name_last: Optional[str] = None,
        court: Optional[str] = None,
        max_results: int = 50,
    ) -> list[dict]:
        """List people from /people/ REST endpoint with filters."""
        params = {}
        if name_last:
            params["name_last__istartswith"] = name_last
        if court:
            params["positions__court"] = court
        return list(self._paginate("people/", params, max_results))

    def get_positions(self, person_id: int, max_results: int = 100) -> list[dict]:
        """Get career positions for a judge (court, role, appointer, dates)."""
        return list(self._paginate("positions/", {"person": person_id}, max_results))

    def get_political_affiliations(self, person_id: int) -> list[dict]:
        """Get political affiliations for a judge."""
        return list(self._paginate("political-affiliations/", {"person": person_id}, 100))

    def get_educations(self, person_id: int) -> list[dict]:
        """Get education history for a judge."""
        return list(self._paginate("educations/", {"person": person_id}, 100))

    # =========================================================================
    # Opinion Clusters API
    # =========================================================================

    def get_cluster(self, cluster_id: int) -> dict:
        """Get an opinion cluster (groups all opinions for one ruling)."""
        return self._request("GET", f"clusters/{cluster_id}/")

    def get_clusters(
        self,
        docket_id: Optional[int] = None,
        court: Optional[str] = None,
        date_filed_after: Optional[str] = None,
        date_filed_before: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """List opinion clusters with filters."""
        params = {}
        if docket_id:
            params["docket"] = docket_id
        if court:
            params["docket__court__id"] = court
        if date_filed_after:
            params["date_filed__gte"] = date_filed_after
        if date_filed_before:
            params["date_filed__lte"] = date_filed_before
        return list(self._paginate("clusters/", params, max_results))

    # =========================================================================
    # Citation Graph API
    # =========================================================================

    def get_citing_opinions(self, opinion_id: int, max_results: int = 100) -> list[dict]:
        """Get opinions that cite a given opinion (by opinion ID)."""
        return list(self._paginate(
            "opinions-cited/", {"cited_opinion": opinion_id}, max_results
        ))

    def get_cited_by_opinion(self, opinion_id: int, max_results: int = 100) -> list[dict]:
        """Get opinions cited by a given opinion (by opinion ID)."""
        return list(self._paginate(
            "opinions-cited/", {"citing_opinion": opinion_id}, max_results
        ))

    def resolve_citations(self, text: str) -> dict:
        """POST citation text to resolve to cluster IDs. Prevents citation hallucinations."""
        return self._request("POST", "citation-lookup/", json_body={"text": text})

    # =========================================================================
    # Financial Disclosures API (1.9M investment records)
    # =========================================================================

    def get_financial_disclosures(
        self,
        person_id: Optional[int] = None,
        year: Optional[int] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Get financial disclosure documents."""
        params = {}
        if person_id:
            params["person"] = person_id
        if year:
            params["year"] = year
        return list(self._paginate("financial-disclosures/", params, max_results))

    def get_investments(
        self,
        person_id: Optional[int] = None,
        description: Optional[str] = None,
        min_value: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Search judge investment holdings. description__icontains searches by company name."""
        params = {}
        if person_id:
            params["financial_disclosure__person"] = person_id
        if description:
            params["description__icontains"] = description
        if min_value:
            params["gross_value_code__gte"] = min_value
        return list(self._paginate("investments/", params, max_results))

    def get_gifts(
        self,
        person_id: Optional[int] = None,
        source: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Get gift disclosures. source__icontains searches by gift source."""
        params = {}
        if person_id:
            params["financial_disclosure__person"] = person_id
        if source:
            params["source__icontains"] = source
        return list(self._paginate("gifts/", params, max_results))

    def get_debts(self, person_id: Optional[int] = None, max_results: int = 100) -> list[dict]:
        """Get judge debt disclosures."""
        params = {}
        if person_id:
            params["financial_disclosure__person"] = person_id
        return list(self._paginate("debts/", params, max_results))

    def get_non_investment_incomes(
        self,
        person_id: Optional[int] = None,
        source: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Get non-investment income >$200 (speeches, teaching, consulting)."""
        params = {}
        if person_id:
            params["financial_disclosure__person"] = person_id
        if source:
            params["source_type__icontains"] = source
        return list(self._paginate("non-investment-incomes/", params, max_results))

    def get_spouse_incomes(self, person_id: Optional[int] = None, max_results: int = 100) -> list[dict]:
        """Get spouse income disclosures."""
        params = {}
        if person_id:
            params["financial_disclosure__person"] = person_id
        return list(self._paginate("spouse-incomes/", params, max_results))

    def get_reimbursements(
        self,
        person_id: Optional[int] = None,
        source: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Get travel reimbursement disclosures. source__icontains searches by who paid."""
        params = {}
        if person_id:
            params["financial_disclosure__person"] = person_id
        if source:
            params["source__icontains"] = source
        return list(self._paginate("reimbursements/", params, max_results))

    def get_disclosure_positions(self, person_id: Optional[int] = None, max_results: int = 100) -> list[dict]:
        """Get officer/director/trustee positions disclosed by judges."""
        params = {}
        if person_id:
            params["financial_disclosure__person"] = person_id
        return list(self._paginate("disclosure-positions/", params, max_results))

    def get_agreements(self, person_id: Optional[int] = None, max_results: int = 100) -> list[dict]:
        """Get agreements disclosed during reporting period."""
        params = {}
        if person_id:
            params["financial_disclosure__person"] = person_id
        return list(self._paginate("agreements/", params, max_results))

    # =========================================================================
    # FJC Integrated Database
    # =========================================================================

    def search_fjc(
        self,
        plaintiff: Optional[str] = None,
        defendant: Optional[str] = None,
        nature_of_suit: Optional[str] = None,
        date_filed_after: Optional[str] = None,
        date_filed_before: Optional[str] = None,
        district: Optional[str] = None,
        class_action: Optional[bool] = None,
        max_results: int = 100,
    ) -> list[dict]:
        """Search FJC Integrated Database. Plaintiff/defendant use startswith (no contains)."""
        params = {}
        if plaintiff:
            params["plaintiff__istartswith"] = plaintiff
        if defendant:
            params["defendant__istartswith"] = defendant
        if nature_of_suit:
            params["nature_of_suit"] = nature_of_suit
        if date_filed_after:
            params["date_filed__gte"] = date_filed_after
        if date_filed_before:
            params["date_filed__lte"] = date_filed_before
        if district:
            params["district"] = district
        if class_action is not None:
            params["class_action"] = str(class_action).lower()
        return list(self._paginate("fjc-integrated-database/", params, max_results))

    # =========================================================================
    # Audio / Oral Arguments
    # =========================================================================

    def search_audio(self, query: str, court: Optional[str] = None, max_results: int = 100) -> list[dict]:
        """Search oral argument recordings."""
        return self.search(query, search_type="oa", court=court, max_results=max_results)

    def get_audio(self, audio_id: int) -> dict:
        """Get a specific oral argument recording."""
        return self._request("GET", f"audio/{audio_id}/")

    # =========================================================================
    # Bankruptcy
    # =========================================================================

    def search_bankruptcy(self, max_results: int = 100, **kwargs) -> list[dict]:
        """Search bankruptcy information (35M records)."""
        return list(self._paginate("bankruptcy-information/", kwargs, max_results))

    # =========================================================================
    # Originating Court Info
    # =========================================================================

    def get_originating_court_info(self, docket_id: int) -> list[dict]:
        """Get lower court information for an appellate docket."""
        return list(self._paginate("originating-court-information/", {"docket": docket_id}, 10))

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def get_endpoint_options(self, endpoint: str) -> dict:
        """Get available filters and options for an endpoint (OPTIONS request)."""
        return self._request("OPTIONS", endpoint)

    def get_rate_limit_status(self) -> dict:
        """Get current rate limit status."""
        return {
            "requests_remaining": self.rate_limiter.requests_remaining(),
            "max_per_hour": self.rate_limiter.max_requests,
        }


def main():
    """CLI for testing the API client directly."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="CourtListener API client (testing)")
    parser.add_argument("--token", help="API token (or set COURTLISTENER_TOKEN)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("search", help="Search CourtListener")
    p.add_argument("query", help="Search query")
    p.add_argument("--type", default="r", help="Search type (o/r/p/rd/oa)")
    p.add_argument("--court", help="Court filter")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("docket", help="Get docket details")
    p.add_argument("docket_id", type=int)

    p = sub.add_parser("party", help="Search by party name (via search API)")
    p.add_argument("name")
    p.add_argument("--court", help="Court filter")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("judge", help="Search judges")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("investments", help="Search judge investments")
    p.add_argument("query", help="Company/description to search")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("citations", help="Citation graph for a cluster")
    p.add_argument("cluster_id", type=int)
    p.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    client = CourtListenerClient(token=args.token)

    if args.command == "search":
        for r in client.search(args.query, search_type=args.type, court=args.court, max_results=args.limit):
            print(json.dumps(r, indent=2, default=str))
    elif args.command == "docket":
        print(json.dumps(client.get_docket(args.docket_id), indent=2, default=str))
    elif args.command == "party":
        for r in client.search_by_party(args.name, court=args.court, max_results=args.limit):
            print(json.dumps(r, indent=2, default=str))
    elif args.command == "judge":
        for j in client.search_judges(name=args.name, max_results=args.limit):
            print(json.dumps(j, indent=2, default=str))
    elif args.command == "investments":
        for inv in client.get_investments(description=args.query, max_results=args.limit):
            print(json.dumps(inv, indent=2, default=str))
    elif args.command == "citations":
        citing = client.get_citing_opinions(args.cluster_id, max_results=args.limit)
        cited = client.get_cited_by_opinion(args.cluster_id, max_results=args.limit)
        print(f"Cites {len(cited)} opinions, cited by {len(citing)} opinions")
        print(json.dumps({"citing": citing, "cited_by": cited}, indent=2, default=str))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
