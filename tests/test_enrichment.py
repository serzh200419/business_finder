from datetime import timedelta
import json
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.assessment_fixtures import seed_fixture_campaign, fixture_cases, fixture_html, fixture_policies, fixture_transport
from app.contacts import normalize_contact, normalize_domain
from app.enrichment import (create_enrichment_job, effective_assessment, make_fetcher, manual_correction,
                            run_enrichment_job, resume_enrichment, search_plan)
from app.extraction import extract_facts, identity_match
from app.fetcher import FetchLimiter, PublicFetcher
from app.models import Business, Campaign, Contact, Evidence, Job, JobBusiness, WebsiteAssessment, now
from app.providers import FixtureWebSearchProvider, RetryableProviderError, SearchConfigurationError, get_web_search_provider
from app.schemas import AccessPolicy, EnrichmentOptions, ManualAssessmentInput
from app.services import LeaseBusy, claim_lease, release_lease


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


@pytest.fixture
def seeded(migrated_db):
    with Session(migrated_db[0]) as db:
        campaign, options, expected = seed_fixture_campaign(db)
        return campaign.id, options, expected


def test_normalization_without_identity_inference():
    phone = normalize_contact("phone", "010 111 001")
    assert phone.valid and phone.value == "+37410111001"
    invalid = normalize_contact("phone", "bad Armenian phone")
    assert not invalid.valid and invalid.raw == "bad Armenian phone" and invalid.value is None
    email = normalize_contact("email", " Public.Office+Demo@EXAMPLE.COM ")
    assert email.value == "Public.Office+Demo@example.com" and email.details["deliverability"] == "not_checked"
    assert normalize_contact("social", "https://WWW.INSTAGRAM.COM/synthetic/#ignored").value == "https://www.instagram.com/synthetic/"
    assert normalize_domain("EXAMPLE.COM.") == "example.com"
    assert not normalize_contact("email", "invalid@@example.com").valid
    assert normalize_contact("email", "Office%Tag?Demo@EXAMPLE.COM").value == "Office%Tag?Demo@example.com"


def test_explicit_search_aliases_and_unconfigured_live():
    queries = search_plan({"name": "Սինթետիկ կլինիկա", "aliases": ["Синтетическая клиника"], "city": "Yerevan",
                           "address": "1 Demo Street", "phones": ["+37410111001"]}, 5)
    assert len(queries) == 5 and queries[-1] == "+37410111001"
    assert any("Синтетическая" in query for query in queries)
    with pytest.raises(SearchConfigurationError):
        get_web_search_provider("live", "empty", True)


def test_complete_synthetic_assessments_and_idempotent_replay(seeded, migrated_db):
    cid, options, expected = seeded
    with Session(migrated_db[0]) as db:
        job = create_enrichment_job(db, cid, options)
        result = run_enrichment_job(db, job.id)
        assert result.status == "completed"
        for item in expected:
            assessment = effective_assessment(db, item["business_id"])
            assert assessment.status == item["expected"]
            assert assessment.checked_at and assessment.search_coverage["configured"] == 2
        counts = (count(db, Contact), count(db, Evidence), count(db, WebsiteAssessment))
        second = create_enrichment_job(db, cid, options)
        assert run_enrichment_job(db, second.id).status == "completed"
        assert counts == (count(db, Contact), count(db, Evidence), count(db, WebsiteAssessment))
        invalid = db.scalar(select(Contact).where(Contact.raw_value == "not-an-Armenian-phone"))
        assert invalid.validation_status == "invalid" and invalid.normalized_value is None
        assert invalid.verification_status == "source_claim"
        branches = [item["business_id"] for item in expected if "Branch" in item["name"]]
        assert len(set(branches)) == 2
        for bid in branches:
            matched = list(db.scalars(select(Contact).where(Contact.business_id == bid, Contact.verification_status == "business_matched")))
            assert any(c.normalized_value == "+37410111009" for c in matched)
        assert all(not evidence.export_permitted for evidence in db.scalars(select(Evidence)))
        unrelated = next(item["business_id"] for item in expected if "Similar" in item["name"])
        assert not list(db.scalars(select(Contact).where(Contact.business_id == unrelated, Contact.verification_status == "business_matched")))
        assert all("html" not in evidence.details for evidence in db.scalars(select(Evidence)))


def test_resume_after_checkpoint_and_budget_exhaustion(seeded, migrated_db):
    cid, options, _ = seeded
    with Session(migrated_db[0]) as db:
        job_id = create_enrichment_job(db, cid, options.model_copy(update={"request_budget": 1})).id
        result = run_enrichment_job(db, job_id)
        assert result.status == "partial" and result.result["stop_reason"] == "request_budget"
        assert result.requests_used == 1
    with Session(migrated_db[0]) as restarted:
        resume_enrichment(restarted, job_id, request_budget=200)
        assert run_enrichment_job(restarted, job_id).status == "completed"
        assert restarted.get(Job, job_id).requests_used == 34


def test_manual_correction_survives_rerun_and_is_audited(seeded, migrated_db, client):
    cid, options, expected = seeded
    bid = expected[0]["business_id"]
    with Session(migrated_db[0]) as db:
        run_enrichment_job(db, create_enrichment_job(db, cid, options).id)
    result = client.post(f"/api/businesses/{bid}/assessment/correct", json={"status": "uncertain", "reason": "Synthetic reviewer correction", "actor": "Demo reviewer"})
    assert result.status_code == 200
    with Session(migrated_db[0]) as db:
        run_enrichment_job(db, create_enrichment_job(db, cid, options).id)
        current = effective_assessment(db, bid)
        assert current.status == "uncertain" and current.origin == "manual"
        assert current.previous_status == "website_found"
    detail = client.get(f"/api/businesses/{bid}").json()
    assert detail["assessment"]["origin"] == "manual" and len(detail["audit_trail"]) == 1
    assert detail["audit_trail"][0]["actor"] == "Demo reviewer"
    assert client.get(f"/businesses/{bid}").status_code == 200
    assert "No website found after checks" in client.get(f"/businesses/{bid}").text


def test_incomplete_search_uncertain_and_positive_evidence_can_win(seeded, migrated_db):
    cid, options, expected = seeded
    hosted = next(item["business_id"] for item in expected if "Hosted" in item["name"])
    absent = next(item["business_id"] for item in expected if "No Result" in item["name"])
    options = options.model_copy(update={"business_ids": [hosted, absent], "search_provider": "unavailable"})
    with Session(migrated_db[0]) as db:
        job = run_enrichment_job(db, create_enrichment_job(db, cid, options).id)
        assert job.status == "completed"
        assert effective_assessment(db, hosted).status == "uncertain"
        assert effective_assessment(db, absent).status == "uncertain"

    from app.providers import PermanentProviderError
    class OptionalFailed(FixtureWebSearchProvider):
        def search(self, query, limit):
            if query.endswith("4 Demo Street"):
                raise PermanentProviderError("synthetic optional failure")
            return super().search(query, limit)
    with Session(migrated_db[0]) as db:
        retry_options = options.model_copy(update={"business_ids": [hosted], "search_provider": "fixture", "optional_search_indices": [1]})
        job = create_enrichment_job(db, cid, retry_options)
        run_enrichment_job(db, job.id, search_provider=OptionalFailed("assessment"))
        assert effective_assessment(db, hosted).status == "website_found"


def test_page_and_seed_permission_propagation(seeded, migrated_db):
    cid, options, expected = seeded
    bid = expected[0]["business_id"]
    denied = AccessPolicy(public_access_permitted=True, persistence_permitted=False,
                         policy_reference="synthetic no storage", retention_policy="no storage")
    restricted = options.model_copy(update={"business_ids": [bid], "page_policies": {"missing.synthetic.example": denied}})
    with Session(migrated_db[0]) as db:
        run_enrichment_job(db, create_enrichment_job(db, cid, restricted).id)
        assert effective_assessment(db, bid).status == "uncertain"
        assert not list(db.scalars(select(Evidence).where(Evidence.business_id == bid, Evidence.evidence_type == "public_page")))
        assert not list(db.scalars(select(Contact).where(Contact.business_id == bid, Contact.verification_status == "business_matched")))
        for seed in db.scalars(select(Evidence).where(Evidence.business_id == bid, Evidence.evidence_type == "discovery")):
            seed.persistence_permitted = False
        db.commit()
        with pytest.raises(ValueError, match="persistence-permitted"):
            create_enrichment_job(db, cid, options.model_copy(update={"business_ids": [bid]}))


def test_search_permissions_dont_leak_candidate_data(seeded, migrated_db):
    from app.providers import WebSearchResult
    cid, options, expected = seeded
    bid = expected[0]["business_id"]
    class RestrictedSearch:
        identity = "restricted_search"
        def search(self, query, limit):
            return [WebSearchResult(url="https://restricted.example/secret", title="restricted name", query=query,
                                    provider=self.identity, persistence_permitted=False)]
    with Session(migrated_db[0]) as db:
        job = create_enrichment_job(db, cid, options.model_copy(update={"business_ids": [bid]}))
        run_enrichment_job(db, job.id, search_provider=RestrictedSearch())
        assert effective_assessment(db, bid).status == "uncertain"
        for evidence in db.scalars(select(Evidence)):
            assert "restricted.example" not in str(evidence.details) + evidence.source_url
        for progress in db.scalars(select(JobBusiness).where(JobBusiness.job_id == job.id)):
            assert "restricted.example" not in str(progress.state)


def test_transient_search_retry_is_persisted_and_charged(seeded, migrated_db):
    cid, options, expected = seeded
    bid = expected[0]["business_id"]
    class Transient(FixtureWebSearchProvider):
        failures = 0
        def search(self, *args):
            if self.failures == 0:
                self.failures += 1
                raise RetryableProviderError("synthetic", retry_after="2")
            return super().search(*args)
    provider = Transient("assessment")
    moment = now()
    with Session(migrated_db[0]) as db:
        job = create_enrichment_job(db, cid, options.model_copy(update={"business_ids": [bid]}))
        result = run_enrichment_job(db, job.id, search_provider=provider, clock=lambda: moment, jitter=lambda: 0)
        assert result.status == "partial" and result.requests_used == 1
        progress = db.scalar(select(JobBusiness).where(JobBusiness.job_id == job.id))
        assert progress.consecutive_failures == 1 and progress.next_attempt_at
        result = run_enrichment_job(db, job.id, search_provider=provider, clock=lambda: moment + timedelta(seconds=3))
        assert result.status == "completed" and result.requests_used == 5


def test_enrichment_lease_excludes_duplicate_worker_and_abandonment_recovers(seeded, migrated_db):
    cid, options, expected = seeded
    with Session(migrated_db[0]) as db:
        job = create_enrichment_job(db, cid, options.model_copy(update={"business_ids": [expected[0]["business_id"]]}))
        token = claim_lease(db, cid, now())
        with Session(migrated_db[0]) as other:
            with pytest.raises(LeaseBusy):
                run_enrichment_job(other, job.id)
        result = run_enrichment_job(db, job.id, clock=lambda: now() + timedelta(minutes=5))
        assert result.status == "completed"


def test_page_contacts_require_identity_and_shared_phone_needs_branch_location():
    case = fixture_cases()[0]
    facts = extract_facts(fixture_html(case), "https://synthetic.example/")
    snapshot = {"name": case["name"], "city": case["city"], "address": case["address"], "phones": [case["phone"]]}
    assert identity_match(snapshot, facts)[0]
    snapshot["shared_phone"] = True
    snapshot["address"] = "Other Branch Street"
    assert not identity_match(snapshot, facts)[0]
    other = {**snapshot, "phones": ["+37410123456"]}
    assert not identity_match(other, facts)[0]
    html = fixture_html(case) + '<script type="application/ld+json">' + json.dumps({"@type":"Dentist","name":"Other"}) + '</script>'
    assert not identity_match(snapshot, extract_facts(html, "https://synthetic.example/"))[0]


def test_interrupted_page_rolls_back_contacts_and_resumes_without_duplicates(seeded, migrated_db, monkeypatch):
    import app.enrichment as enrichment
    cid, options, expected = seeded
    original = enrichment.persist_contact
    with Session(migrated_db[0]) as db:
        job = create_enrichment_job(db, cid, options.model_copy(update={"business_ids": [expected[0]["business_id"]]}))
        baseline = count(db, Contact)
        calls = []
        def interrupted(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(result)
            raise RuntimeError("synthetic interruption after contact insert")
        monkeypatch.setattr(enrichment, "persist_contact", interrupted)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            run_enrichment_job(db, job.id)
        assert calls and count(db, Contact) == baseline
        progress = db.scalar(select(JobBusiness).where(JobBusiness.job_id == job.id))
        assert progress.state["candidate_index"] == 0
        monkeypatch.setattr(enrichment, "persist_contact", original)
        resume_enrichment(db, job.id)
        assert run_enrichment_job(db, job.id).status == "completed"
        assert effective_assessment(db, expected[0]["business_id"]).status == "website_found"
        counts = (count(db, Contact), count(db, Evidence))
        replay = create_enrichment_job(db, cid, options.model_copy(update={"business_ids": [expected[0]["business_id"]]}))
        run_enrichment_job(db, replay.id)
        assert (count(db, Contact), count(db, Evidence)) == counts
