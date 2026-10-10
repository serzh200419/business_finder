from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email.utils import format_datetime
from threading import Event
from dataclasses import replace
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models import (Business, BusinessSource, Campaign, Contact, Evidence, Job,
                        JobQuery, WebsiteAssessment, now)
from app.planner import plan_campaign
from app.providers import (CSVProvider, DiscoveryPage, FixtureProvider, PermanentProviderError,
                           RetryableProviderError, retry_after_seconds)
from app.schemas import CampaignInput, ProviderConfig
from app.services import (LeaseBusy, LeaseLost, claim_lease, create_job, fence, job_summary,
                          release_lease, resume_job, run_job)


def campaign(client, **changes):
    values = {"niche": "dentist", "keywords": {"en": ["dentist"]}, "request_budget": 100,
              "candidate_budget": 100, "provider_config": {"discovery": "fixture", "page_size": 1}}
    values.update(changes)
    response = client.post("/api/campaigns", json=values)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_search_expansion_unicode_and_dedup():
    data = CampaignInput(niche="dentist", districts=["Kentron"],
        keywords={"hy": ["ատամնաբույժ", "  ատամնաբույժ  "], "ru": ["стоматология"], "en": ["dentist", "стоматология"]},
        city_variants={"hy": ["Երևան", "Երեւան"]},
        district_variants={"Kentron": {"hy": ["Կենտրոն"]}})
    plan = plan_campaign(data)
    assert plan == plan_campaign(data)
    assert len(plan["queries"]) == 6  # Armenian ligature and expanded spelling casefold identically
    assert any(q["text"] == "ատամնաբույժ Երևան Կենտրոն" for q in plan["queries"])
    russian = next(q for q in plan["queries"] if q["text"] == "стоматология Yerevan")
    assert {v["language"] for v in russian["variants"]} == {"ru", "en"}
    limited = plan_campaign(data.model_copy(update={"query_limit": 2}))
    assert len(limited["queries"]) == 2 and limited["truncated"]


def test_plan_dry_run_never_calls_provider(client, migrated_db, monkeypatch):
    cid = campaign(client, keywords={"hy": ["ատամնաբույժ"]})
    monkeypatch.setattr("app.services.get_provider", lambda *args: pytest.fail("dry run called provider"))
    plan = client.get(f"/api/campaigns/{cid}/plan").json()
    assert plan["queries"][0]["language"] == "hy"
    with Session(migrated_db[0]) as db:
        assert db.get(Campaign, cid).requests_used == 0 and count(db, Job) == 0


def test_pagination_overlap_identity_and_rerun(client, migrated_db):
    cid = campaign(client, keywords={"hy": ["ատամնաբույժ"], "ru": ["стоматология"], "en": ["dentist"]},
        provider_config={"discovery": "fixture", "fixture_scenario": "reliability", "fixture_failures_enabled": False, "page_size": 2})
    first = client.post(f"/api/campaigns/{cid}/discover").json()
    assert first["status"] == "completed" and first["result"]["pages_committed"] == 6
    assert first["result"]["accepted"] == first["result"]["newly_created"] == 4
    with Session(migrated_db[0]) as db:
        assert count(db, Business) == count(db, BusinessSource) == 4
        assert count(db, Evidence) == count(db, Contact) == count(db, WebsiteAssessment) == 12
        same_names = list(db.scalars(select(Business).where(Business.name == "Synthetic Arev Dental")))
        assert len(same_names) == 2 and all(b.review_required for b in same_names)
        assert db.scalar(select(func.count(func.distinct(Contact.value)))) == 2  # branch phones do not merge
        assert db.scalar(select(func.count(func.distinct(Evidence.query_id)))) == 3
    second = client.post(f"/api/campaigns/{cid}/discover").json()
    assert second["job_id"] != first["job_id"]
    assert second["result"]["existing"] == 4 and second["result"]["newly_created"] == 0
    with Session(migrated_db[0]) as db:
        assert count(db, Evidence) == 12 and count(db, Contact) == 12


def test_page_replay_is_idempotent(client, migrated_db):
    cid = campaign(client)
    with Session(migrated_db[0]) as db:
        job = create_job(db, cid)
        run_job(db, job.id, max_pages=1)
        assert count(db, Evidence) == 1
        progress = db.scalar(select(JobQuery).where(JobQuery.job_id == job.id))
        assert progress.cursor
        progress.cursor = None  # simulate replay from an older checkpoint
        progress.status = "pending"
        db.commit()
        run_job(db, job.id, max_pages=1)
        assert count(db, Business) == count(db, Evidence) == count(db, Contact) == 1
        result = run_job(db, job.id)
        assert result.status == "completed" and count(db, Evidence) == 2


def test_restart_after_committed_page(client, migrated_db, monkeypatch):
    import app.services as services
    cid = campaign(client)
    original = services.save_page
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("process interrupted after page commit")
    with Session(migrated_db[0]) as db:
        job_id = create_job(db, cid).id
        monkeypatch.setattr(services, "save_page", interrupted)
        with pytest.raises(RuntimeError):
            run_job(db, job_id)
    monkeypatch.setattr(services, "save_page", original)
    with Session(migrated_db[0]) as restarted:
        result = run_job(restarted, job_id)
        assert result.status == "completed" and result.result["requests"] == 2
        assert count(restarted, Business) == count(restarted, Evidence) == 2


def test_page_transaction_rolls_back(client, migrated_db, monkeypatch):
    import app.services as services
    cid = campaign(client)
    real_insert = services.insert_once
    def fail_at_evidence(db, model, *args, **kwargs):
        if model is Evidence:
            raise RuntimeError("interrupted halfway through page")
        return real_insert(db, model, *args, **kwargs)
    with Session(migrated_db[0]) as db:
        job_id = create_job(db, cid).id
        monkeypatch.setattr(services, "insert_once", fail_at_evidence)
        with pytest.raises(RuntimeError):
            run_job(db, job_id)
        assert count(db, Business) == count(db, BusinessSource) == count(db, Evidence) == 0
        progress = db.scalar(select(JobQuery).where(JobQuery.job_id == job_id))
        assert progress.cursor is None and progress.pages_committed == 0
    monkeypatch.setattr(services, "insert_once", real_insert)
    with Session(migrated_db[0]) as db:
        assert run_job(db, job_id).status == "completed"
        assert count(db, Evidence) == 2


def test_duplicate_worker_protection(client, migrated_db, monkeypatch):
    cid = campaign(client)
    engine = migrated_db[0]
    started, proceed = Event(), Event()
    class Blocking(FixtureProvider):
        def discover_page(self, *args):
            started.set()
            assert proceed.wait(5)
            return super().discover_page(*args)
    monkeypatch.setattr("app.services.get_provider", lambda config, *_: Blocking(config))
    with Session(engine) as db:
        job_id = create_job(db, cid).id
    def worker():
        with Session(engine) as db:
            return run_job(db, job_id).status
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(worker)
        assert started.wait(5)
        try:
            with Session(engine) as second:
                with pytest.raises(LeaseBusy):
                    run_job(second, job_id)
        finally:
            proceed.set()
        assert result.result(timeout=5) == "completed"
    with Session(engine) as db:
        assert count(db, Evidence) == 2 and db.get(Campaign, cid).requests_used == 2


def test_abandoned_lease_recovery_and_stale_fencing(client, migrated_db):
    cid = campaign(client)
    engine = migrated_db[0]
    with Session(engine) as abandoned:
        job_id = create_job(abandoned, cid).id
        old_token = claim_lease(abandoned, cid, now())
        progress = abandoned.scalar(select(JobQuery).where(JobQuery.job_id == job_id))
        progress.status = "running"
        progress.attempts = 1
        abandoned.get(Campaign, cid).requests_used = 1
        abandoned.commit()
    future = now() + timedelta(minutes=5)
    with Session(engine) as recovered:
        result = run_job(recovered, job_id, clock=lambda: future)
        assert result.status == "completed" and result.result["requests"] == 3
        with pytest.raises(LeaseLost):
            fence(recovered, cid, old_token, future)


def test_budgets_stop_before_provider_and_resume(client, migrated_db):
    cid = campaign(client, request_budget=1)
    first = client.post(f"/api/campaigns/{cid}/discover").json()
    assert first["status"] == "partial" and first["result"]["stop_reason"] == "request_budget"
    assert first["result"]["queries_unfinished"] == 1
    with Session(migrated_db[0]) as db:
        db.get(Campaign, cid).request_budget = 3
        db.commit()
    result = client.post(f"/api/jobs/{first['job_id']}/resume", json={}).json()
    assert result["status"] == "completed" and result["result"]["requests_used"] == 2
    zero = campaign(client, candidate_budget=0)
    result = client.post(f"/api/campaigns/{zero}/discover").json()
    assert result["status"] == "partial" and result["result"]["requests"] == 0
    assert result["result"]["stop_reason"] == "candidate_budget"


def test_candidate_capacity_limits_page_size(client, migrated_db, monkeypatch):
    limits = []
    class Tracked(FixtureProvider):
        def discover_page(self, request, cursor, limit):
            limits.append(limit)
            return super().discover_page(request, cursor, limit)
    monkeypatch.setattr("app.services.get_provider", lambda config, *_: Tracked(config))
    cid = campaign(client, candidate_budget=1, provider_config={"discovery": "fixture", "page_size": 500})
    result = client.post(f"/api/campaigns/{cid}/discover").json()
    assert limits == [1] and result["result"]["accepted"] == 1 and result["status"] == "partial"


def test_retry_backoff_retry_after_and_bound(client, migrated_db, monkeypatch):
    class Failing(FixtureProvider):
        def discover_page(self, request, cursor, limit):
            raise RetryableProviderError("synthetic restricted error details", retry_after="7")
    monkeypatch.setattr("app.services.get_provider", lambda config, *_: Failing(config))
    cid = campaign(client, provider_config={"discovery": "fixture", "max_retries": 2, "retry_base_seconds": 2})
    at = now()
    with Session(migrated_db[0]) as db:
        job_id = create_job(db, cid).id
        first = run_job(db, job_id, clock=lambda: at, jitter=lambda: 0.5)
        assert first.status == "partial"
        progress = db.scalar(select(JobQuery).where(JobQuery.job_id == job_id))
        assert progress.next_attempt_at.replace(tzinfo=at.tzinfo) == at + timedelta(seconds=7)
        assert progress.error_message == "Retryable provider error"
        run_job(db, job_id, clock=lambda: at + timedelta(seconds=6))
        assert db.get(Campaign, cid).requests_used == 1
        run_job(db, job_id, clock=lambda: at + timedelta(seconds=7), jitter=lambda: 1)
        db.refresh(progress)
        assert progress.next_attempt_at.replace(tzinfo=at.tzinfo) == at + timedelta(seconds=15)  # 4*(1+1)
        final = run_job(db, job_id, clock=lambda: at + timedelta(seconds=15))
        assert final.status == "failed" and final.result["requests"] == 3
    assert retry_after_seconds(format_datetime(at + timedelta(seconds=20)), at) == pytest.approx(20, abs=1)


def test_permanent_failure_partial_resume_fixture(client, migrated_db):
    cid = campaign(client, keywords={"en": ["dentist", "permanent"]},
        provider_config={"discovery": "fixture", "fixture_scenario": "reliability", "page_size": 2})
    response = client.post(f"/api/campaigns/{cid}/discover").json()
    assert response["status"] == "partial" and response["result"]["queries_failed"] == 1
    job_id = response["job_id"]
    unchanged = client.post(f"/api/jobs/{job_id}/resume", json={}).json()
    assert unchanged["result"]["requests"] == response["result"]["requests"]
    result = client.post(f"/api/jobs/{job_id}/resume", json={"retry_failed": True, "disable_fixture_failures": True}).json()
    assert result["status"] == "completed" and result["result"]["accepted"] == 4
    assert result["result"]["queries_completed"] == 2


def test_cancellation_between_pages(client, migrated_db, monkeypatch):
    engine = migrated_db[0]
    cid = campaign(client)
    with Session(engine) as db:
        job_id = create_job(db, cid).id
    class Cancelling(FixtureProvider):
        def discover_page(self, *args):
            with Session(engine) as other:
                other.get(Job, job_id).cancel_requested = True
                other.commit()
            return super().discover_page(*args)
    monkeypatch.setattr("app.services.get_provider", lambda config, *_: Cancelling(config))
    with Session(engine) as db:
        result = run_job(db, job_id)
        assert result.status == "cancelled" and result.result["requests"] == 1
        assert count(db, Evidence) == 1
    monkeypatch.setattr("app.services.get_provider", lambda config, *_: FixtureProvider(config))
    assert client.post(f"/api/jobs/{job_id}/resume", json={}).json()["status"] == "completed"


def test_csv_mapping_malformed_bom_and_reimport(client, migrated_db, tmp_path):
    path = tmp_path / "input.csv"
    path.write_text("ID,Label,city,niche,URL,phone\n1,Սինթետիկ,Yerevan,dentist,https://example.invalid/1,+37410123456\n2,Synthetic,Yerevan,dentist,https://example.invalid/2,+37410123456\n,Missing ID,Yerevan,dentist,https://example.invalid/3,\n3,Extra,Yerevan,dentist,https://example.invalid/3,,extra\n", encoding="utf-8-sig")
    config = {"discovery": "csv", "csv_path": str(path), "provider_namespace": "owned-test",
        "column_mapping": {"external_id": "ID", "name": "Label", "source_url": "URL"},
        "license_reference": "owned", "persistence_permitted": True, "export_permitted": False,
        "retention_policy": "30 days", "page_size": 1}
    dry = client.post("/api/csv/validate", json=config).json()
    assert dry["accepted"] == 2 and dry["rejected"] == 2 and dry["would_create"] == 2
    assert {e["row"] for e in dry["errors"]} == {4, 5}
    with Session(migrated_db[0]) as db:
        assert count(db, Business) == 0
    cid = campaign(client, provider_config=config)
    first = client.post(f"/api/campaigns/{cid}/discover").json()
    assert first["result"]["newly_created"] == 2 and first["result"]["rejected"] == 2
    second = client.post(f"/api/campaigns/{cid}/discover").json()
    assert second["result"]["existing"] == 2 and second["result"]["newly_created"] == 0
    with Session(migrated_db[0]) as db:
        assert count(db, Business) == count(db, Evidence) == 2
        assert all(not e.export_permitted and e.persistence_permitted and "30 days" in e.retention_policy
                   for e in db.scalars(select(Evidence)))
    assert client.post("/api/csv/validate", json=config).json()["existing"] == 2


def test_csv_conflicting_ids_and_changed_dataset(client, migrated_db, tmp_path):
    path = tmp_path / "rows.csv"
    header = "external_id,name,city,niche,source_url\n"
    path.write_text(header + "1,One,Yerevan,dentist,https://example.invalid/1\n1,Other,Yerevan,dentist,https://example.invalid/1\n", encoding="utf-8")
    config = ProviderConfig(discovery="csv", csv_path=str(path), license_reference="owned",
                            persistence_permitted=True, retention_policy="30 days", page_size=1)
    rows, report = CSVProvider(config).validate()
    assert not rows and report["rejected"] == 2
    path.write_text(header + "1,One,Yerevan,dentist,https://example.invalid/1\n2,Two,Yerevan,dentist,https://example.invalid/2\n", encoding="utf-8")
    cid = campaign(client, provider_config=config.model_dump())
    with Session(migrated_db[0]) as db:
        job = create_job(db, cid)
        run_job(db, job.id, max_pages=1)
        path.write_text(path.read_text(encoding="utf-8") + "3,Three,Yerevan,dentist,https://example.invalid/3\n", encoding="utf-8")
        final = run_job(db, job.id)
        assert final.status == "failed" and count(db, Business) == 1


def test_rights_are_restricted_and_provider_ids_are_scoped(client, migrated_db, monkeypatch):
    from app.providers import DiscoveryResult, ProviderCapabilities
    class Custom:
        capabilities = ProviderCapabilities()
        def __init__(self, identity, export):
            self.identity, self.export = identity, export
        def discover_page(self, request, cursor, limit):
            return DiscoveryPage([DiscoveryResult("same-id", "Same name", "Yerevan", "Kentron", "dentist",
                "https://example.invalid/source", provider=self.identity, persistence_permitted=True, export_permitted=self.export,
                query_id=request.query_id)], None)
    current = [Custom("licensed:one", False)]
    monkeypatch.setattr("app.services.get_provider", lambda *args: current[0])
    config = {"discovery": "licensed", "license_reference": "test", "persistence_permitted": True, "export_permitted": True}
    first = campaign(client, provider_config=config)
    assert client.post(f"/api/campaigns/{first}/discover").json()["status"] == "completed"
    current[0] = Custom("licensed:one", True)
    client.post(f"/api/campaigns/{first}/discover")
    current[0] = Custom("licensed:two", True)
    second = campaign(client, provider_config=config)
    client.post(f"/api/campaigns/{second}/discover")
    with Session(migrated_db[0]) as db:
        assert count(db, Business) == 2 and count(db, BusinessSource) == 2
        evidence = list(db.scalars(select(Evidence).where(Evidence.campaign_id == first)))
        assert len(evidence) == 1 and not evidence[0].export_permitted


def test_duplicate_candidates_do_not_spend_candidate_budget(client):
    cid = campaign(client, candidate_budget=3, keywords={"en": ["dentist", "dental"]},
                   provider_config={"discovery": "fixture", "page_size": 2})
    result = client.post(f"/api/campaigns/{cid}/discover").json()
    assert result["status"] == "completed"
    assert result["result"]["campaign_candidates"] == 2
    assert result["result"]["accepted"] == 2 and result["result"]["pages_committed"] == 3


def test_retry_consumes_last_request_before_budget_stop(client, migrated_db, monkeypatch):
    class Failed(FixtureProvider):
        def discover_page(self, *args):
            raise RetryableProviderError("test")
    monkeypatch.setattr("app.services.get_provider", lambda config, *_: Failed(config))
    cid = campaign(client, request_budget=1)
    response = client.post(f"/api/campaigns/{cid}/discover").json()
    assert response["status"] == "partial"
    assert response["result"]["requests"] == 1
    assert response["result"]["stop_reason"] == "request_budget"
    response = client.post(f"/api/jobs/{response['job_id']}/resume", json={}).json()
    assert response["result"]["requests"] == 1


def test_invalid_csv_headers_and_utf8(tmp_path):
    path = tmp_path / "invalid.csv"
    config = ProviderConfig(discovery="csv", csv_path=str(path), license_reference="owned",
                            persistence_permitted=True, retention_policy="30 days")
    path.write_text("name,city\nExample,Yerevan\n", encoding="utf-8")
    with pytest.raises(PermanentProviderError, match="Missing required"):
        CSVProvider(config).validate()
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(PermanentProviderError, match="UTF-8"):
        CSVProvider(config).validate()


def test_contract_overflow_discards_entire_page(client, migrated_db, monkeypatch):
    class Overflow(FixtureProvider):
        def discover_page(self, request, cursor, limit):
            return super().discover_page(request, cursor, limit + 1)
    monkeypatch.setattr("app.services.get_provider", lambda config, *_: Overflow(config))
    cid = campaign(client, candidate_budget=1)
    response = client.post(f"/api/campaigns/{cid}/discover").json()
    assert response["status"] == "failed" and response["result"]["accepted"] == 0
    with Session(migrated_db[0]) as db:
        assert count(db, Business) == 0 and count(db, Evidence) == 0


def test_merged_query_preserves_both_district_associations(client, migrated_db):
    cid = campaign(client, districts=["Kentron", "Arabkir"],
        district_variants={"Kentron": {"en": ["Central"]}, "Arabkir": {"en": ["Central"]}})
    plan = client.get(f"/api/campaigns/{cid}/plan").json()
    assert len(plan["queries"]) == 2
    assert {v["district"] for v in plan["queries"][1]["variants"]} == {"Kentron", "Arabkir"}
    result = client.post(f"/api/campaigns/{cid}/discover").json()
    assert result["status"] == "completed"
    with Session(migrated_db[0]) as db:
        assert count(db, Business) == 2 and count(db, Evidence) == 4


def test_expired_worker_cannot_commit_or_release_replacement_lease(client, migrated_db, monkeypatch):
    engine = migrated_db[0]
    cid = campaign(client)
    moment = [now()]
    replacement_token = []
    class Replaced(FixtureProvider):
        def discover_page(self, *args):
            moment[0] += timedelta(minutes=5)
            with Session(engine) as replacement:
                replacement_token.append(claim_lease(replacement, cid, moment[0]))
            return super().discover_page(*args)
    monkeypatch.setattr("app.services.get_provider", lambda config, *_: Replaced(config))
    with Session(engine) as db:
        job_id = create_job(db, cid).id
        with pytest.raises(LeaseLost):
            run_job(db, job_id, clock=lambda: moment[0])
        assert count(db, Business) == count(db, Evidence) == 0
        assert db.get(Campaign, cid, populate_existing=True).lease_token == replacement_token[0]
        release_lease(db, cid, replacement_token[0])
    monkeypatch.setattr("app.services.get_provider", lambda config, *_: FixtureProvider(config))
    with Session(engine) as db:
        assert run_job(db, job_id).status == "completed"
