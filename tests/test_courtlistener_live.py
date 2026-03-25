"""
Live integration tests for CourtListener API client.

These tests hit the actual CourtListener API to verify endpoints work
with our auth token. They use a well-known case (US v. Battle, Sr.,
1:04-cr-20159, S.D. Fla.) as a stable test fixture.

Run: uv run pytest tests/test_courtlistener_live.py -v
Skip: uv run pytest tests/test_courtlistener_live.py -v -k "not live"
"""
from __future__ import annotations

import os
import pytest
from pathlib import Path

pytestmark = [
    pytest.mark.live_data,
    pytest.mark.slow,
]

# Load .env
env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"'))


@pytest.fixture(scope="module")
def client():
    from tools.courtlistener_api_client import CourtListenerClient
    token = os.environ.get("COURTLISTENER_TOKEN")
    if not token:
        pytest.skip("COURTLISTENER_TOKEN not set")
    return CourtListenerClient(token=token)


# Known test fixtures
BATTLE_DOCKET_ID = 5375813  # US v. Battle, Sr. (1:04-cr-20159)
BATTLE_OPINION_CLUSTER = 2328330  # 473 F.Supp.2d 1185 (Rule 29 ruling)


# ── Search API ────────────────────────────────────────────────


class TestSearch:
    def test_recap_search(self, client):
        results = client.search("Battle Corporation", search_type="r", court="flsd", max_results=3)
        assert len(results) > 0
        assert any("Battle" in r.get("caseName", "") for r in results)

    def test_opinion_search(self, client):
        results = client.search("Battle RICO enterprise", search_type="o", max_results=3)
        assert len(results) > 0

    def test_recap_document_search(self, client):
        results = client.search("Battle Corporation RICO", search_type="rd", court="flsd", max_results=3)
        assert len(results) > 0

    def test_people_search(self, client):
        results = client.search("Gold", search_type="p", max_results=3)
        assert len(results) > 0


# ── Party / Attorney / Firm (via search API) ─────────────────


class TestPartySearch:
    def test_search_by_party(self, client):
        results = client.search_by_party("Battle", court="flsd", max_results=5)
        assert len(results) > 0
        # Should return docket results with party data
        first = results[0]
        assert "caseName" in first or "case_name" in first

    def test_search_by_attorney(self, client):
        results = client.search_by_attorney("Blakey", court="flsd", max_results=3)
        assert len(results) >= 0  # May not find results, but shouldn't 403

    def test_search_by_firm(self, client):
        results = client.search_by_firm("Kirkland", max_results=3)
        assert len(results) > 0

    def test_party_search_does_not_403(self, client):
        """The old /parties/ endpoint 403'd. The new search API workaround should not."""
        # This should complete without raising PermissionError
        results = client.search_by_party("Epstein", max_results=1)
        assert isinstance(results, list)


# ── Dockets ──────────────────────────────────────────────────


class TestDockets:
    def test_get_docket(self, client):
        d = client.get_docket(BATTLE_DOCKET_ID)
        assert d["case_name"] == "United States v. Battle, Sr."
        assert d["docket_number"] == "1:04-cr-20159"
        assert d["court_id"] == "flsd"

    def test_docket_has_key_fields(self, client):
        d = client.get_docket(BATTLE_DOCKET_ID)
        for field in ["date_filed", "pacer_case_id", "assigned_to_str", "clusters"]:
            assert field in d, f"Missing field: {field}"


# ── Opinions ─────────────────────────────────────────────────


class TestOpinions:
    def test_get_opinion_has_text(self, client):
        """The Battle Rule 29 opinion should have full text."""
        # Get cluster to find opinion IDs
        cluster = client.get_cluster(BATTLE_OPINION_CLUSTER)
        sub_opinions = cluster.get("sub_opinions", [])
        assert len(sub_opinions) > 0

        # Get the first opinion
        oid = sub_opinions[0].rstrip("/").split("/")[-1]
        opinion = client.get_opinion(int(oid))

        # Should have text in at least one format
        has_text = any(
            opinion.get(f) and len(opinion.get(f, "")) > 1000
            for f in ["html_lawbox", "html_columbia", "html_with_citations", "plain_text"]
        )
        assert has_text, "Opinion should have substantial text content"


# ── Clusters ─────────────────────────────────────────────────


class TestClusters:
    def test_get_cluster(self, client):
        c = client.get_cluster(BATTLE_OPINION_CLUSTER)
        assert "case_name" in c
        assert "sub_opinions" in c
        assert "citation_count" in c

    def test_get_clusters_by_docket(self, client):
        clusters = client.get_clusters(docket_id=BATTLE_DOCKET_ID, max_results=5)
        assert isinstance(clusters, list)


# ── Citation Graph ───────────────────────────────────────────


class TestCitations:
    def test_get_citing_opinions(self, client):
        """Check what cites the Battle opinion. Uses opinion ID, not cluster ID."""
        # First get an opinion ID from the cluster
        cluster = client.get_cluster(BATTLE_OPINION_CLUSTER)
        sub_opinions = cluster.get("sub_opinions", [])
        if not sub_opinions:
            pytest.skip("No sub-opinions in cluster")
        oid = int(sub_opinions[0].rstrip("/").split("/")[-1])
        results = client.get_citing_opinions(oid, max_results=5)
        assert isinstance(results, list)

    def test_get_cited_by_opinion(self, client):
        """Check what the Battle opinion cites."""
        cluster = client.get_cluster(BATTLE_OPINION_CLUSTER)
        sub_opinions = cluster.get("sub_opinions", [])
        if not sub_opinions:
            pytest.skip("No sub-opinions in cluster")
        oid = int(sub_opinions[0].rstrip("/").split("/")[-1])
        results = client.get_cited_by_opinion(oid, max_results=5)
        assert isinstance(results, list)

    def test_resolve_citations(self, client):
        """Resolve a known citation to a cluster ID."""
        result = client.resolve_citations("473 F.Supp.2d 1185")
        assert isinstance(result, (dict, list))


# ── Judges ───────────────────────────────────────────────────


class TestJudges:
    def test_search_judges(self, client):
        results = client.search_judges("Gold", max_results=3)
        assert len(results) > 0

    def test_list_people(self, client):
        results = client.list_people(name_last="Gold", max_results=3)
        assert isinstance(results, list)

    def test_get_person(self, client):
        # Find Gold first
        judges = client.list_people(name_last="Gold", max_results=1)
        if judges:
            person = client.get_person(judges[0]["id"])
            assert "name_first" in person or "name_last" in person


# ── Financial Disclosures ────────────────────────────────────


class TestFinancialDisclosures:
    def test_search_investments_by_company(self, client):
        """Search 1.9M investment records by company name."""
        results = client.get_investments(description="Palantir", max_results=5)
        assert len(results) > 0
        # Each should have a description containing "Palantir"
        for r in results:
            assert "palantir" in r.get("description", "").lower()

    def test_search_investments_goldman(self, client):
        results = client.get_investments(description="Goldman", max_results=3)
        assert len(results) > 0

    def test_search_reimbursements_federalist(self, client):
        """Find judges reimbursed by Federalist Society."""
        results = client.get_reimbursements(source="Federalist", max_results=5)
        assert len(results) > 0
        for r in results:
            assert "federalist" in r.get("source", "").lower()

    def test_get_gifts(self, client):
        results = client.get_gifts(max_results=3)
        assert isinstance(results, list)

    def test_get_debts(self, client):
        results = client.get_debts(max_results=3)
        assert isinstance(results, list)

    def test_get_disclosure_positions(self, client):
        results = client.get_disclosure_positions(max_results=3)
        assert isinstance(results, list)


# ── FJC Database ─────────────────────────────────────────────


class TestFJC:
    def test_search_by_defendant(self, client):
        """FJC uses istartswith, not icontains."""
        results = client.search_fjc(defendant="Epstein", max_results=3)
        assert isinstance(results, list)

    def test_search_by_plaintiff(self, client):
        results = client.search_fjc(plaintiff="United States", max_results=3)
        assert isinstance(results, list)
        assert len(results) > 0


# ── Judge Career ─────────────────────────────────────────────


class TestCareer:
    def test_get_positions(self, client):
        # Find a judge first
        judges = client.list_people(name_last="Gold", max_results=1)
        if not judges:
            pytest.skip("No judges found")
        positions = client.get_positions(judges[0]["id"])
        assert isinstance(positions, list)

    def test_get_educations(self, client):
        judges = client.list_people(name_last="Gold", max_results=1)
        if not judges:
            pytest.skip("No judges found")
        edu = client.get_educations(judges[0]["id"])
        assert isinstance(edu, list)


# ── Error Handling ───────────────────────────────────────────


class TestErrorHandling:
    def test_blocked_parties_endpoint_raises_permission_error(self, client):
        """The /parties/ endpoint should raise PermissionError, not generic error."""
        with pytest.raises(PermissionError, match="select user"):
            client._request("GET", "parties/", params={"docket": BATTLE_DOCKET_ID})

    def test_blocked_docket_entries_raises_permission_error(self, client):
        with pytest.raises(PermissionError, match="select user"):
            client._request("GET", "docket-entries/", params={"docket": BATTLE_DOCKET_ID})


# ── Audio (Oral Arguments) ───────────────────────────────────


class TestAudio:
    def test_search_audio(self, client):
        results = client.search_audio("Palantir", max_results=3)
        assert isinstance(results, list)
