
"""Durable discovery. Network calls are outside page transactions; commits are fenced."""
import random
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4
from app.contacts import persist_contact
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from app.config import get_settings
from app.models import (Business, BusinessSource, Campaign, Contact, Evidence, Job,
                        JobCandidate, JobQuery, SearchQuery, WebsiteAssessment, now)
from app.planner import digest, plan_campaign
from app.providers import (DiscoveryPage, DiscoveryRequest, PermanentProviderError,
                           RetryableProviderError, get_provider, retry_after_seconds)
from app.schemas import CampaignInput, ProviderConfig


class LeaseBusy(ValueError):
    pass


class LeaseLost(ValueError):
    pass


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def insert_once(db: Session, model, values: dict, keys: list[str], *, return_created: bool = False):
    dialect = db.get_bind().dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise ValueError("Discovery supports PostgreSQL and SQLite only")
    statement = (sqlite_insert if dialect == "sqlite" else pg_insert)(model).values(**values)
    inserted_id = db.execute(statement.on_conflict_do_nothing(index_elements=keys).returning(model.id)).scalar_one_or_none()
    db.flush()
    row = db.scalar(select(model).where(*(getattr(model, key) == values[key] for key in keys)))
    return (row, inserted_id is not None) if return_created else row


def sync_plan(db: Session, campaign: Campaign) -> list[SearchQuery]:
    plan = plan_campaign(CampaignInput.model_validate(campaign, from_attributes=True))
    queries = []
    for query in plan["queries"]:
        queries.append(insert_once(db, SearchQuery, {"campaign_id": campaign.id, **query},
                                   ["campaign_id", "plan_key"]))
    return queries


def create_job(db: Session, campaign_id: int, *, fresh: bool = False) -> Job:
    # A write locks campaign creation on both supported databases.
    db.execute(update(Campaign).where(Campaign.id == campaign_id).values(requests_used=Campaign.requests_used))
    campaign = db.get(Campaign, campaign_id, populate_existing=True)
    if campaign is None:
        raise ValueError("Campaign not found")
    if campaign.lease_token and campaign.lease_until and utc(campaign.lease_until) > now():
        db.rollback()
        raise LeaseBusy("Campaign has an active worker lease")
    existing = db.scalar(select(Job).where(Job.campaign_id == campaign_id, Job.kind == "discovery",
        Job.status.in_(["pending", "running", "partial"])).order_by(Job.id.desc()))
    if existing:
        db.commit()
        if fresh:
            raise ValueError("Resume or cancel the unfinished job before creating a fresh job")
        return existing
    config = ProviderConfig.model_validate(campaign.provider_config)
    provider = get_provider(config, get_settings().demo_mode)
    if not provider.capabilities.stable_external_ids or not provider.capabilities.pagination:
        raise ValueError("Provider must support stable IDs and bounded pagination")
    queries = sync_plan(db, campaign)
    if not queries:
        raise ValueError("Search plan contains no queries; configure at least one keyword")
    job = Job(campaign_id=campaign_id, provider_snapshot={
        "config": config.model_dump(), "identity": provider.identity,
        "niche": campaign.niche, "city": campaign.city, "districts": campaign.districts}, status="pending")
    db.add(job)
    db.flush()
    for query in queries:
        db.add(JobQuery(job_id=job.id, query_id=query.id))
    db.commit()
    return job


def claim_lease(db: Session, campaign_id: int, at: datetime) -> str:
    token = str(uuid4())
    changed = db.execute(update(Campaign).execution_options(synchronize_session=False).where(Campaign.id == campaign_id,
        or_(Campaign.lease_token.is_(None), Campaign.lease_until <= at)).values(
        lease_token=token, lease_until=at + timedelta(seconds=get_settings().discovery_lease_seconds)))
    db.commit()
    if changed.rowcount != 1:
        raise LeaseBusy("Another worker holds the campaign lease")
    return token


def fence(db: Session, campaign_id: int, token: str, at: datetime):
    # This update holds a write/row lock until the page transaction commits.
    changed = db.execute(update(Campaign).execution_options(synchronize_session=False).where(Campaign.id == campaign_id,
        Campaign.lease_token == token, Campaign.lease_until > at).values(
        lease_until=at + timedelta(seconds=get_settings().discovery_lease_seconds)))
    if changed.rowcount != 1:
        db.rollback()
        raise LeaseLost("Worker lease expired or was replaced; page discarded")


def release_lease(db: Session, campaign_id: int, token: str):
    db.execute(update(Campaign).where(Campaign.id == campaign_id, Campaign.lease_token == token)
               .values(lease_token=None, lease_until=None))
    db.commit()


def campaign_candidates(db: Session, campaign_id: int) -> int:
    return db.scalar(select(func.count(func.distinct(Evidence.business_id))).where(
        Evidence.campaign_id == campaign_id)) or 0


def job_summary(db: Session, job: Job) -> dict:
    if job.kind == "enrichment":
        from app.enrichment import enrichment_summary
        return enrichment_summary(db, job)
    queries = list(db.scalars(select(JobQuery).where(JobQuery.job_id == job.id).order_by(JobQuery.id)))
    candidates = list(db.scalars(select(JobCandidate).where(JobCandidate.job_id == job.id)))
    campaign = db.get(Campaign, job.campaign_id, populate_existing=True)
    errors = { (error["row"], error["error"]): error for query in queries for error in query.rejected_rows }
    return {"accepted": len(candidates), "persisted": len(candidates),
            "newly_created": sum(c.newly_created for c in candidates),
            "existing": sum(not c.newly_created for c in candidates),
            "rejected": len(errors), "row_errors": list(errors.values()),
            "skipped_unlicensed": sum(q.skipped_unlicensed for q in queries),
            "requests": sum(q.attempts for q in queries), "requests_used": campaign.requests_used,
            "request_budget": campaign.request_budget, "candidate_budget": campaign.candidate_budget,
            "campaign_candidates": campaign_candidates(db, campaign.id),
            "pages_committed": sum(q.pages_committed for q in queries),
            "queries_completed": sum(q.status == "completed" for q in queries),
            "queries_failed": sum(q.status == "failed" for q in queries),
            "queries_unfinished": sum(q.status not in {"completed", "failed", "cancelled"} for q in queries),
            "queries": [{"id": q.query_id, "status": q.status, "cursor": q.cursor,
                         "pages_committed": q.pages_committed, "attempts": q.attempts,
                         "error_kind": q.error_kind, "error": q.error_message,
                         "next_attempt_at": utc(q.next_attempt_at).isoformat() if q.next_attempt_at else None}
                        for q in queries]}


def finish_job(db: Session, job: Job, reason: str | None = None):
    if job.kind == "enrichment":
        from app.enrichment import finish_enrichment
        return finish_enrichment(db, job, reason)
    queries = list(db.scalars(select(JobQuery).where(JobQuery.job_id == job.id)))
    if job.cancel_requested:
        for query in queries:
            if query.status not in {"completed", "failed"}:
                query.status = "cancelled"
        job.status = "cancelled"
    elif all(q.status == "completed" for q in queries):
        job.status = "completed"
    elif queries and all(q.status == "failed" for q in queries):
        job.status = "failed"
    else:
        job.status = "partial"
    db.flush()
    job.result = {**job_summary(db, job), "stop_reason": None if job.status == "completed" else reason}


def save_page(db: Session, job: Job, progress: JobQuery, page: DiscoveryPage,
              config: ProviderConfig, provider_identity: str, token: str, at: datetime,
              limit: int, expected_cursor: str | None):
    """Evidence, associations and cursor advance commit together; replay is constraint-safe."""
    fence(db, job.campaign_id, token, at)
    db.refresh(progress)
    if progress.cursor != expected_cursor:
        raise PermanentProviderError("Stale page cursor")
    if len(page.results) > limit:
        raise PermanentProviderError("Provider returned more than the reserved candidate capacity")
    if page.next_cursor is not None and page.next_cursor == expected_cursor:
        raise PermanentProviderError("Provider returned a non-advancing cursor")
    if progress.dataset_fingerprint and page.dataset_fingerprint != progress.dataset_fingerprint:
        raise PermanentProviderError("Dataset changed during import; start a new job")
    query = db.get(SearchQuery, progress.query_id)
    for result in page.results:
        if (result.provider != provider_identity or result.query_id != query.id or not result.external_id
                or len(result.external_id) > 250 or not result.name or not result.source_url):
            raise PermanentProviderError("Provider identity/query association or required fields invalid")
        if (not result.persistence_permitted or result.provider.casefold().startswith("google")
                or (config.discovery != "fixture" and not config.persistence_permitted)):
            progress.skipped_unlicensed += 1
            continue
        identity = digest(result.provider, result.external_id)
        source = db.scalar(select(BusinessSource).where(BusinessSource.provider == result.provider,
                                                       BusinessSource.external_id == result.external_id))
        newly_created = source is None
        if source is None:
            similar = list(db.scalars(select(Business).where(Business.name == result.name)))
            business = insert_once(db, Business, {
                "name": result.name, "city": result.city, "district": result.district,
                "website": result.website, "identity_key": identity,
                "address": result.address, "name_aliases": list(result.name_aliases),
                "review_required": bool(similar), "review_reasons": ["Same name, distinct source identity"] if similar else []},
                ["identity_key"])
            for other in similar:
                other.review_required = True
                if "Same name, distinct source identity" not in other.review_reasons:
                    other.review_reasons = [*other.review_reasons, "Same name, distinct source identity"]
            source, newly_created = insert_once(db, BusinessSource, {"business_id": business.id, "provider": result.provider,
                "external_id": result.external_id, "source_url": result.source_url}, ["provider", "external_id"], return_created=True)
        business = db.get(Business, source.business_id)
        if (business.name, business.city, business.district) != (result.name, result.city, result.district):
            business.review_required = True
            reason = "Source ID has changed identity fields; canonical record retained for review"
            if reason not in business.review_reasons:
                business.review_reasons = [*business.review_reasons, reason]
        insert_once(db, JobCandidate, {"job_id": job.id, "source_id": source.id,
                    "newly_created": newly_created}, ["job_id", "source_id"])
        evidence_key = digest(str(job.campaign_id), str(query.id), result.provider, result.external_id)
        evidence = db.scalar(select(Evidence).where(Evidence.evidence_key == evidence_key))
        export_allowed = result.export_permitted and (config.discovery == "fixture" or config.export_permitted)
        if evidence is not None:
            # Reimports cannot widen rights of previously restricted evidence.
            evidence.export_permitted = evidence.export_permitted and export_allowed
            continue
        evidence = insert_once(db, Evidence, {
            "business_id": business.id, "campaign_id": job.campaign_id, "query_id": query.id,
            "source_id": source.id, "evidence_key": evidence_key, "source_url": result.source_url,
            "provider": result.provider, "observed_at": at,
            "match_reasons": [f"query: {query.text}", f"language: {query.language}", f"district: {result.district}"],
            "retention_policy": result.retention_policy, "persistence_permitted": True,
            "export_permitted": export_allowed,
            "details": {"record_type": "synthetic" if config.discovery == "fixture" else config.record_type,
                        "identity": {"name": result.name, "city": result.city, "district": result.district,
                        "address": result.address, "website": result.website, "niche": result.niche}}}, ["evidence_key"])
        db.add(WebsiteAssessment(business_id=business.id, evidence_id=evidence.id,
            status="website_listed" if result.website else "candidate_for_review",
            reasons=["Source lists website; not verified"] if result.website else ["Source omits website; absence unverified"]))
        if result.phone:
            persist_contact(db, business.id, evidence, "phone", result.phone)
        if result.email:
            persist_contact(db, business.id, evidence, "email", result.email)
    progress.cursor = page.next_cursor
    progress.pages_committed += 1
    progress.status = "completed" if page.next_cursor is None else "pending"
    progress.consecutive_failures = 0
    progress.next_attempt_at = None
    progress.error_kind = progress.error_message = None
    progress.dataset_fingerprint = page.dataset_fingerprint
    progress.rejected_rows = page.rejected_rows
    db.commit()


def resume_job(db: Session, job_id: int, *, retry_failed: bool = False,
               disable_fixture_failures: bool = False) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise ValueError("Job not found")
    if job.kind == "enrichment":
        from app.enrichment import resume_enrichment
        return resume_enrichment(db, job_id)
    db.execute(update(Campaign).where(Campaign.id == job.campaign_id).values(requests_used=Campaign.requests_used))
    campaign = db.get(Campaign, job.campaign_id, populate_existing=True)
    if campaign.lease_token and campaign.lease_until and utc(campaign.lease_until) > now():
        raise LeaseBusy("Cannot resume while a worker lease is active")
    if disable_fixture_failures:
        if job.provider_snapshot.get("config", {}).get("discovery") != "fixture" or not get_settings().demo_mode:
            raise ValueError("Fault disabling is restricted to synthetic fixtures")
        snapshot = dict(job.provider_snapshot)
        snapshot["config"] = {**snapshot["config"], "fixture_failures_enabled": False}
        job.provider_snapshot = snapshot
    if job.status == "completed":
        return job
    for query in db.scalars(select(JobQuery).where(JobQuery.job_id == job_id)):
        if query.status == "cancelled" or (retry_failed and query.status == "failed"):
            query.status = "pending"
            query.consecutive_failures = 0
            query.next_attempt_at = None
            query.error_kind = query.error_message = None
    job.cancel_requested = False
    job.status = "pending"
    db.commit()
    return job


def run_job(db: Session, job_id: int, *, max_pages: int = 100,
            clock: Callable[[], datetime] = now, jitter: Callable[[], float] = random.random) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise ValueError("Job not found")
    if job.kind == "enrichment":
        from app.enrichment import run_enrichment_job
        return run_enrichment_job(db, job_id, max_steps=max_pages, clock=clock, jitter=jitter)
    if job.status in {"completed", "cancelled"}:
        return job
    if "config" not in job.provider_snapshot:
        raise ValueError("Legacy job has no checkpoints; create a new discovery job")
    campaign_id = job.campaign_id
    db.commit()
    token = claim_lease(db, campaign_id, clock())
    processed = 0
    reason = None
    try:
        fence(db, campaign_id, token, clock())
        job = db.get(Job, job_id, populate_existing=True)
        config = ProviderConfig.model_validate(job.provider_snapshot["config"])
        provider = get_provider(config, get_settings().demo_mode)
        if provider.identity != job.provider_snapshot["identity"]:
            raise ValueError("Provider identity changed since checkpoint")
        job.status = "running"
        for query in db.scalars(select(JobQuery).where(JobQuery.job_id == job.id, JobQuery.status == "running")):
            query.status = "pending"  # previous worker's uncommitted page is recoverable
        db.commit()
        while processed < max_pages:
            fence(db, campaign_id, token, clock())
            db.refresh(job)
            if job.cancel_requested:
                reason = "cancelled"
                break
            campaign = db.get(Campaign, campaign_id, populate_existing=True)
            capacity = campaign.candidate_budget - campaign_candidates(db, campaign_id)
            if capacity <= 0 or campaign.requests_used >= campaign.request_budget:
                reason = "candidate_budget" if capacity <= 0 else "request_budget"
                break
            progress = db.scalar(select(JobQuery).where(JobQuery.job_id == job.id,
                JobQuery.status == "pending", or_(JobQuery.next_attempt_at.is_(None),
                JobQuery.next_attempt_at <= clock())).order_by(JobQuery.id))
            if progress is None:
                reason = "awaiting_retry_or_failed_queries"
                break
            query = db.get(SearchQuery, progress.query_id)
            limit = min(config.page_size, provider.capabilities.max_page_size, capacity)
            cursor = progress.cursor
            progress_id = progress.id
            request = DiscoveryRequest(query.id, query.text, query.language, job.provider_snapshot["niche"],
                job.provider_snapshot["city"], query.district, tuple(job.provider_snapshot["districts"]),
                tuple(query.variants), progress.consecutive_failures + 1)
            campaign.requests_used += 1
            progress.attempts += 1
            progress.status = "running"
            db.commit()  # charge before provider call, even if worker crashes
            processed += 1
            try:
                page = provider.discover_page(request, cursor, limit)
                # Roll back reads so cancellation from another session is observable.
                db.rollback()
                job = db.get(Job, job_id, populate_existing=True)
                progress = db.get(JobQuery, progress_id, populate_existing=True)
                save_page(db, job, progress, page, config, provider.identity, token, clock(), limit, cursor)
            except (RetryableProviderError, PermanentProviderError) as exc:
                db.rollback()
                fence(db, campaign_id, token, clock())
                progress = db.get(JobQuery, progress_id, populate_existing=True)
                progress.consecutive_failures += 1
                retryable = isinstance(exc, RetryableProviderError)
                progress.error_kind = "retryable" if retryable else "permanent"
                # Never persist arbitrary provider messages which may contain restricted content.
                progress.error_message = "Retryable provider error" if retryable else "Permanent provider/contract error"
                if retryable and progress.consecutive_failures <= config.max_retries:
                    delay = min(config.retry_cap_seconds, config.retry_base_seconds * 2 ** (progress.consecutive_failures - 1))
                    delay = min(config.retry_cap_seconds, delay * (1 + max(0, min(1, jitter()))))
                    delay = max(delay, retry_after_seconds(exc.retry_after, clock()))
                    progress.next_attempt_at = clock() + timedelta(seconds=delay)
                    progress.status = "pending"
                else:
                    progress.status = "failed"
                    progress.next_attempt_at = None
                db.commit()
        if processed >= max_pages:
            reason = "batch_limit"
        fence(db, campaign_id, token, clock())
        job = db.get(Job, job_id, populate_existing=True)
        finish_job(db, job, reason)
        db.commit()
        return job
    except LeaseLost:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        # Preserve committed checkpoints. Unexpected errors remain manually resumable.
        fence(db, campaign_id, token, clock())
        job = db.get(Job, job_id, populate_existing=True)
        finish_job(db, job, "interrupted; inspect worker logs")
        db.commit()
        raise
    finally:
        db.rollback()
        release_lease(db, campaign_id, token)


def run_discovery(db: Session, campaign_id: int) -> Job:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError("Campaign not found")
    if campaign.requests_used >= campaign.request_budget:
        raise ValueError("Campaign request budget exhausted")
    job = create_job(db, campaign_id)
    return run_job(db, job.id)
