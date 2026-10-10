"""Per-business assessment steps reuse discovery leases, budgets and idempotent inserts."""
import copy
import random
from datetime import timedelta
from functools import lru_cache
from urllib.parse import urlsplit
from uuid import uuid4
from sqlalchemy import or_, select, update
from app.config import get_settings
from app.contacts import classify_url, normalize_contact, normalize_url, persist_contact
from app.extraction import extract_facts, identity_match, relevant_contacts
from app.fetcher import FetchError, FetchLimiter, PublicFetcher
from app.models import Business, Campaign, Contact, Evidence, Job, JobBusiness, WebsiteAssessment, now
from app.planner import digest, normalize
from app.providers import (PermanentProviderError, RetryableProviderError, get_web_search_provider,
                           retry_after_seconds)
from app.schemas import EnrichmentOptions, ManualAssessmentInput
from app.services import (LeaseBusy, LeaseLost, claim_lease, fence, insert_once, release_lease, utc)
from app.eligibility import eligible


def operational_evidence(db, evidence, at=None):
    return eligible(evidence, {e.id: e for e in db.scalars(select(Evidence))}, at=at).allowed


class EnrichmentBudget(ValueError):
    pass


def search_plan(snapshot: dict, limit: int) -> list[str]:
    names = [snapshot["name"], *snapshot.get("aliases", [])]
    queries = []
    for name in names:
        for city in [snapshot["city"], *snapshot.get("city_variants", [])]:
            queries.append(normalize(f"{name} {city}"))
        if snapshot.get("address") or snapshot.get("district"):
            queries.append(normalize(f"{name} {snapshot.get('address') or snapshot['district']}"))
    queries.extend(snapshot.get("phones", []))
    return list(dict.fromkeys(queries))[:limit]


def effective_assessment(db, business_id: int):
    manual = db.scalar(select(WebsiteAssessment).where(WebsiteAssessment.business_id == business_id,
        WebsiteAssessment.origin == "manual").order_by(WebsiteAssessment.id.desc()))
    if manual:
        return manual
    automatic = db.scalar(select(WebsiteAssessment).where(WebsiteAssessment.business_id == business_id,
        WebsiteAssessment.origin == "automatic").order_by(WebsiteAssessment.checked_at.desc(), WebsiteAssessment.id.desc()))
    return automatic or db.scalar(select(WebsiteAssessment).where(WebsiteAssessment.business_id == business_id)
                                   .order_by(WebsiteAssessment.id.desc()))


def normalize_existing_contacts(db, business_id: int):
    for contact in db.scalars(select(Contact).join(Evidence).where(Contact.business_id == business_id,
                                                                   Evidence.persistence_permitted.is_(True))):
        if not operational_evidence(db, db.get(Evidence, contact.evidence_id)):
            continue
        result = normalize_contact(contact.kind, contact.raw_value or contact.value)
        contact.raw_value = result.raw
        contact.normalized_value = result.value
        contact.value = (result.value or result.raw)[:250]
        contact.validation_status = "valid" if result.valid else "invalid"
        contact.validation_details = result.details
        evidence = db.get(Evidence, contact.evidence_id)
        contact.source_url = evidence.source_url
        # Retain source observation time, rather than claiming normalization verifies identity.
        contact.observed_at = evidence.observed_at


def add_candidate(state: dict, url: str, *, export_permitted: bool, known_match: bool = False,
                  optional: bool = False, parent_match: bool = False, retention_policy: str = "", support_ids=()):
    normalized = normalize_url(url)
    if not normalized:
        state["incomplete"] = True
        return
    candidate = next((item for item in state["candidates"] if item["url"] == normalized), None)
    if candidate:
        candidate["export_permitted"] = candidate["export_permitted"] and export_permitted
        candidate["known_match"] = candidate["known_match"] or known_match
        candidate["retention_policy"] = "; ".join(dict.fromkeys(filter(None, [candidate.get("retention_policy", ""), retention_policy])))
        candidate["support_ids"] = list(set(candidate.get("support_ids", [])) | set(support_ids))
    else:
        state["candidates"].append({"url": normalized, "classification": classify_url(normalized),
            "export_permitted": export_permitted, "known_match": known_match,
            "optional": optional, "parent_match": parent_match, "retention_policy": retention_policy,
            "support_ids": list(support_ids)})


def create_enrichment_job(db, campaign_id: int, options: EnrichmentOptions) -> Job:
    db.execute(update(Campaign).where(Campaign.id == campaign_id).values(requests_used=Campaign.requests_used))
    campaign = db.get(Campaign, campaign_id, populate_existing=True)
    if not campaign:
        raise ValueError("Campaign not found")
    if campaign.lease_token and campaign.lease_until and utc(campaign.lease_until) > now():
        raise LeaseBusy("Campaign has an active worker")
    existing = db.scalar(select(Job).where(Job.campaign_id == campaign_id, Job.kind == "enrichment",
        Job.status.in_(["pending", "running", "partial"])).order_by(Job.id.desc()))
    if existing:
        db.commit()
        return existing
    seeds = list(db.scalars(select(Evidence).where(Evidence.campaign_id == campaign_id,
        Evidence.evidence_type == "discovery", Evidence.persistence_permitted.is_(True)).order_by(Evidence.id)))
    by_business = {}
    for seed in seeds:
        if operational_evidence(db, seed):
            by_business.setdefault(seed.business_id, seed)
    ids = options.business_ids or list(by_business)
    if not ids or set(ids) - set(by_business):
        raise ValueError("Businesses must have persistence-permitted discovery evidence in this campaign")
    job = Job(campaign_id=campaign_id, kind="enrichment", status="pending",
              provider_snapshot={"options": options.model_dump()}, request_budget=options.request_budget)
    db.add(job)
    db.flush()
    for business_id in dict.fromkeys(ids):
        business = db.get(Business, business_id)
        seed = by_business[business_id]
        contacts = list(db.scalars(select(Contact).join(Evidence).where(Contact.business_id == business_id,
            Evidence.campaign_id == campaign_id, Evidence.persistence_permitted.is_(True), Contact.kind == "phone")))
        contacts = [c for c in contacts if operational_evidence(db, db.get(Evidence, c.evidence_id))]
        phones = list(dict.fromkeys(result.value for contact in contacts
                     if (result := normalize_contact("phone", contact.raw_value or contact.value)).valid))
        shared = False
        for other in db.scalars(select(Contact).join(Evidence).where(Contact.business_id != business_id,
            Contact.kind == "phone", Evidence.persistence_permitted.is_(True))):
            number = normalize_contact("phone", other.raw_value or other.value)
            shared = shared or bool(number.valid and number.value in phones)
        identity = seed.details.get("identity", {})
        snapshot = {"name": identity.get("name", business.name), "city": identity.get("city", business.city),
                    "district": identity.get("district", business.district), "address": identity.get("address", business.address),
                    "aliases": [*business.name_aliases, *options.name_aliases.get(business_id, [])],
                    "city_variants": options.city_variants, "phones": phones,
                    "shared_phone": shared}
        state = {"business": snapshot, "queries": search_plan(snapshot, options.query_limit),
                 "search_index": 0, "candidate_index": 0, "candidates": [], "checks": [],
                 "matched_websites": [], "matched_social": [], "known_unavailable": [],
                 "incomplete": False, "contact_pages_added": 0, "pages_checked": 0}
        state["optional_search_indices"] = options.optional_search_indices
        previous = effective_assessment(db, business_id)
        known = set(options.known_matching_urls.get(business_id, []))
        if previous and previous.status == "website_found" and operational_evidence(db, db.get(Evidence, previous.evidence_id)):
            known.update(item["url"] for item in previous.candidate_urls if item.get("matched"))
        listed = identity.get("website", business.website)
        if listed:
            add_candidate(state, listed, export_permitted=seed.export_permitted,
                          known_match=listed in known, retention_policy=seed.retention_policy)
        db.add(JobBusiness(job_id=job.id, business_id=business_id, seed_evidence_id=seed.id, state=state))
    db.commit()
    return job


def enrichment_summary(db, job: Job) -> dict:
    progress = list(db.scalars(select(JobBusiness).where(JobBusiness.job_id == job.id).order_by(JobBusiness.id)))
    evidence = {e.id: e for e in db.scalars(select(Evidence))}
    def visible(item):
        ids = [item.seed_evidence_id, *item.state.get("support_ids", [])]
        return all(eligible(evidence.get(eid), evidence).allowed for eid in ids)
    return {"requests_used": job.requests_used, "request_budget": job.request_budget,
        "businesses_completed": sum(item.status == "completed" for item in progress),
        "businesses_unfinished": sum(item.status not in {"completed", "cancelled"} for item in progress),
        "businesses": [{"business_id": item.business_id, "status": item.status, "attempts": item.attempts,
            "error_kind": item.error_kind, "next_attempt_at": utc(item.next_attempt_at).isoformat() if item.next_attempt_at else None,
            "checks": item.state.get("checks", []) if visible(item) else [],
            "evidence_eligible": visible(item)} for item in progress]}


def reserve_request(db, job_id: int, token: str, clock=now):
    job = db.get(Job, job_id, populate_existing=True)
    fence(db, job.campaign_id, token, clock())
    campaign = db.get(Campaign, job.campaign_id, populate_existing=True)
    if job.cancel_requested:
        raise EnrichmentBudget("cancelled")
    if job.requests_used >= job.request_budget or campaign.requests_used >= campaign.request_budget:
        raise EnrichmentBudget("request_budget")
    job.requests_used += 1
    campaign.requests_used += 1
    db.commit()


@lru_cache
def shared_fetch_limiter() -> FetchLimiter:
    settings = get_settings()
    return FetchLimiter(settings.fetch_concurrency, settings.fetch_domain_interval)


def make_fetcher(options: EnrichmentOptions):
    settings = get_settings()
    if options.fetch_mode == "fixture":
        if not settings.demo_mode:
            raise ValueError("Fixture fetch requires DEMO_MODE=true")
        from app.assessment_fixtures import fixture_policies, fixture_transport
        policies = fixture_policies() if options.fixture_dataset == "assessment" else {}
        policies.update(options.page_policies)
        return PublicFetcher(policies, transport=fixture_transport(), limiter=FetchLimiter(interval=0),
                             max_bytes=settings.fetch_max_bytes, max_redirects=settings.fetch_max_redirects)
    if not settings.live_public_fetch_enabled:
        raise ValueError("Live public fetching is disabled; configure explicit source policies first")
    return PublicFetcher(options.page_policies, limiter=shared_fetch_limiter(), max_bytes=settings.fetch_max_bytes,
        max_redirects=settings.fetch_max_redirects, timeout=min(settings.http_timeout, 5),
        total_timeout=settings.fetch_total_timeout)


def choose_assessment(state: dict, finished: bool) -> tuple[str, list[str]]:
    if any(check["kind"] == "search" and check.get("required", True) and
           (check["status"] != "completed" or not check.get("coverage_complete", True))
           for check in state["checks"]):
        return "uncertain", ["Required search failed, blocked or incomplete; retained positive evidence needs review"]
    if state["matched_websites"]:
        return "website_found", ["Matching business website found", *state["matched_websites"][0]["reasons"]]
    if state["known_unavailable"]:
        return "website_unavailable", ["Previously verified/explicitly attested matching website could not be fetched"]
    if not finished or state["incomplete"]:
        return "uncertain", ["Required checks incomplete, blocked, failed or identity unverified"]
    if state["matched_social"]:
        return "social_only", ["Matching social profile; completed configured website checks found no matching website"]
    return "no_website_found", ["No website found after checks; this is not exhaustive proof of absence"]


def record_assessment(db, job: Job, progress: JobBusiness, *, finished: bool, at):
    seed = db.get(Evidence, progress.seed_evidence_id)
    if not operational_evidence(db, seed, at):
        return
    state = progress.state
    status, reasons = choose_assessment(state, finished)
    evidence = insert_once(db, Evidence, {"business_id": progress.business_id, "campaign_id": job.campaign_id,
        "source_url": seed.source_url, "provider": "assessment", "observed_at": at,
        "match_reasons": reasons, "retention_policy": seed.retention_policy,
        "persistence_permitted": True, "export_permitted": False, "parent_evidence_id": seed.id,
        "evidence_type": "assessment", "details": {"checks": state["checks"]},
        "evidence_key": digest("assessment", str(job.campaign_id), str(progress.business_id))}, ["evidence_key"])
    evidence.details = {"checks": state["checks"]}
    evidence.support_evidence_ids = list(dict.fromkeys(state.get("support_ids", [])))
    evidence.match_reasons = reasons
    evidence.observed_at = at
    assessment = insert_once(db, WebsiteAssessment, {"business_id": progress.business_id,
        "evidence_id": evidence.id, "status": status, "reasons": reasons, "assessed_at": at,
        "origin": "automatic", "assessment_key": digest("auto", str(job.campaign_id), str(progress.business_id))}, ["assessment_key"])
    assessment.status, assessment.reasons, assessment.checked_at = status, reasons, at
    assessment.search_coverage = {"configured": len(state["queries"]), "checked": state["search_index"],
        "complete": finished and not state["incomplete"], "checks": state["checks"],
        "search_provider": job.provider_snapshot["options"]["search_provider"],
        "fetch_mode": job.provider_snapshot["options"]["fetch_mode"]}
    assessment.candidate_urls = [{"url": candidate["url"], "classification": candidate["classification"],
        "matched": any(match["url"] == candidate["url"] for match in [*state["matched_websites"], *state["matched_social"]])}
        for candidate in state["candidates"]]


def process_search(db, job, progress, state, options, provider, query, token, clock):
    reserve_request(db, job.id, token, clock)
    results = provider.search(query, options.result_limit)
    fence(db, job.campaign_id, token, clock())
    seed = db.get(Evidence, progress.seed_evidence_id)
    complete = True
    if len(results) >= options.result_limit:
        state["incomplete"] = True  # conservative: adapter has not declared result exhaustion
        complete = False
    for result in results[:options.result_limit]:
        if result.provider != provider.identity or result.query != query:
            raise PermanentProviderError("Search association contract invalid")
        if not result.persistence_permitted or result.provider.casefold().startswith("google") or not seed.persistence_permitted:
            state["incomplete"] = True
            complete = False
            continue
        normalized = normalize_url(result.url)
        if not normalized:
            state["incomplete"] = True
            complete = False
            continue
        search_evidence = insert_once(db, Evidence, {"business_id": progress.business_id, "campaign_id": job.campaign_id,
            "source_url": normalized, "provider": result.provider, "observed_at": clock(),
            "match_reasons": [f"Search query: {query}; not identity verification"],
            "retention_policy": f"{seed.retention_policy}; {result.retention_policy}", "persistence_permitted": True,
            "export_permitted": seed.export_permitted and result.export_permitted, "parent_evidence_id": seed.id,
            "evidence_type": "search", "details": {"query": query, "title": result.title[:250], "snippet": result.snippet[:500]},
            "evidence_key": digest("search", str(job.campaign_id), str(progress.business_id), query, normalized)}, ["evidence_key"])
        search_evidence.export_permitted = search_evidence.export_permitted and seed.export_permitted and result.export_permitted
        state.setdefault("support_ids", []).append(search_evidence.id)
        add_candidate(state, normalized, export_permitted=seed.export_permitted and result.export_permitted,
                      retention_policy=result.retention_policy, support_ids=[search_evidence.id])
    state["checks"].append({"kind": "search", "query": query, "provider": provider.identity,
        "status": "completed", "required": state["search_index"] not in options.optional_search_indices,
        "coverage_complete": complete})
    state["search_index"] += 1


def process_page(db, job, progress, state, options, fetcher, candidate, token, clock):
    if any(not operational_evidence(db, db.get(Evidence, eid), clock()) for eid in candidate.get("support_ids", [])):
        state["checks"].append({"kind": "page", "status": "support_ineligible", "required": not candidate["optional"]})
        state["candidate_index"] += 1
        state["incomplete"] = True
        return
    if candidate["classification"] in {"directory", "marketplace", "booking", "link_hub"}:
        state["checks"].append({"kind": "page", "url": candidate["url"], "status": "profile_excluded",
                                 "classification": candidate["classification"], "required": True})
        state["candidate_index"] += 1
        return
    page = fetcher.fetch(candidate["url"], before_request=lambda: reserve_request(db, job.id, token, clock))
    fence(db, job.campaign_id, token, clock())
    seed = db.get(Evidence, progress.seed_evidence_id)
    if not page.policy.persistence_permitted or not seed.persistence_permitted:
        state["checks"].append({"kind": "page", "status": "persistence_blocked", "required": not candidate["optional"]})
        if not candidate["optional"]:
            state["incomplete"] = True
        state["candidate_index"] += 1
        return
    facts = extract_facts(page.html, page.url)
    matched, reasons = identity_match(state["business"], facts)
    # Contact/about pages inherit only the exact same-origin verified relationship.
    if candidate["parent_match"]:
        if urlsplit(page.url).netloc == urlsplit(candidate["url"]).netloc and not facts.ambiguous_entities:
            matched, reasons = True, ["Same-origin contact/about page linked from a verified matching business website"]
        else:
            matched = False
    classification = classify_url(page.url)
    state["checks"].append({"kind": "page", "url": page.url, "requested_url": candidate["url"],
        "status": "matched" if matched else "identity_unverified", "classification": classification,
        "required": not candidate["optional"], "reasons": reasons})
    if matched:
        evidence = insert_once(db, Evidence, {"business_id": progress.business_id, "campaign_id": job.campaign_id,
            "source_url": page.url, "provider": "public_page", "observed_at": clock(), "match_reasons": reasons,
            "retention_policy": f"{seed.retention_policy}; {candidate.get('retention_policy', '')}; {page.policy.retention_policy}", "persistence_permitted": True,
            "export_permitted": seed.export_permitted and candidate["export_permitted"] and page.policy.export_permitted,
            "parent_evidence_id": seed.id, "evidence_type": "public_page",
            "support_evidence_ids": candidate.get("support_ids", []),
            "details": {"requested_url": page.requested_url, "redirects": list(page.redirects),
                        "classification": classification, "identity_rules": reasons},
            "evidence_key": digest("page", str(job.campaign_id), str(progress.business_id), page.url)}, ["evidence_key"])
        evidence.export_permitted = evidence.export_permitted and seed.export_permitted and candidate["export_permitted"] and page.policy.export_permitted
        evidence.support_evidence_ids = list(set(evidence.support_evidence_ids or []) | set(candidate.get("support_ids", [])))
        state.setdefault("support_ids", []).append(evidence.id)
        for kind, raw in relevant_contacts(facts, page.url):
            persist_contact(db, progress.business_id, evidence, kind, raw, matched=True)
        if classification in {"website", "social"}:
            target = "matched_websites" if classification == "website" else "matched_social"
            if not any(item["url"] == page.url for item in state[target]):
                state[target].append({"url": page.url, "reasons": reasons})
            candidate["matched"] = True
            if page.url != candidate["url"]:
                add_candidate(state, page.url, export_permitted=evidence.export_permitted, known_match=True,
                              retention_policy=evidence.retention_policy, support_ids=[evidence.id])
                # The final URL was checked by this redirected fetch, so don't fetch it again.
                next(item for item in state["candidates"] if item["url"] == page.url)["already_checked"] = True
        if classification == "website":
            for url in facts.contact_pages[:options.contact_page_limit - state["contact_pages_added"]]:
                add_candidate(state, url, export_permitted=evidence.export_permitted, optional=True, parent_match=True,
                              retention_policy=evidence.retention_policy, support_ids=[evidence.id])
                state["contact_pages_added"] += 1
        for related in facts.related_urls:
            normalized = normalize_url(related)
            if normalized and normalized != page.url and normalized not in facts.contact_pages:
                add_candidate(state, normalized, export_permitted=evidence.export_permitted,
                              optional=classify_url(normalized) == "social", retention_policy=evidence.retention_policy,
                              support_ids=[evidence.id])
    elif not candidate["optional"]:
        state["incomplete"] = True
    state["pages_checked"] += 1
    state["candidate_index"] += 1


def finish_enrichment(db, job, reason=None):
    rows = list(db.scalars(select(JobBusiness).where(JobBusiness.job_id == job.id)))
    if job.cancel_requested:
        for row in rows:
            if row.status != "completed":
                row.status = "cancelled"
        job.status = "cancelled"
    elif rows and all(row.status == "completed" for row in rows):
        job.status = "completed"
    else:
        job.status = "partial"
    db.flush()
    job.result = {**enrichment_summary(db, job), "stop_reason": None if job.status == "completed" else reason}


def run_enrichment_job(db, job_id: int, *, max_steps=100, clock=now, jitter=random.random,
                       fetcher=None, search_provider=None) -> Job:
    job = db.get(Job, job_id)
    if not job or job.kind != "enrichment":
        raise ValueError("Enrichment job not found")
    if job.status in {"completed", "cancelled"}:
        return job
    campaign_id = job.campaign_id
    db.commit()
    token = claim_lease(db, campaign_id, clock())
    own_fetcher = fetcher is None
    reason = None
    try:
        options = EnrichmentOptions.model_validate(job.provider_snapshot["options"])
        fetcher = fetcher or make_fetcher(options)
        configuration_error = None
        if search_provider is None:
            try:
                search_provider = get_web_search_provider(options.search_provider, options.fixture_dataset, get_settings().demo_mode)
            except PermanentProviderError:
                configuration_error = "search_unconfigured"
        fence(db, campaign_id, token, clock())
        job = db.get(Job, job_id, populate_existing=True)
        job.status = "running"
        for row in db.scalars(select(JobBusiness).where(JobBusiness.job_id == job_id, JobBusiness.status == "running")):
            row.status = "pending"
        db.commit()
        for _ in range(max_steps):
            fence(db, campaign_id, token, clock())
            job = db.get(Job, job_id, populate_existing=True)
            if job.cancel_requested:
                reason = "cancelled"
                break
            progress = db.scalar(select(JobBusiness).where(JobBusiness.job_id == job_id, JobBusiness.status == "pending",
                or_(JobBusiness.next_attempt_at.is_(None), JobBusiness.next_attempt_at <= clock())).order_by(JobBusiness.id))
            if progress is None:
                reason = "awaiting_retry"
                break
            progress_id = progress.id
            if not operational_evidence(db, db.get(Evidence, progress.seed_evidence_id), clock()):
                progress.status, progress.state, progress.error_kind = "cancelled", {}, "evidence_ineligible"
                db.commit()
                continue
            state = copy.deepcopy(progress.state)
            normalize_existing_contacts(db, progress.business_id)
            search_step = state["search_index"] < len(state["queries"])
            if not search_step and state["candidate_index"] >= len(state["candidates"]):
                progress.status = "completed"
                record_assessment(db, job, progress, finished=True, at=clock())
                db.commit()
                continue
            candidate = None if search_step else state["candidates"][state["candidate_index"]]
            if candidate and (candidate.get("already_checked") or state["pages_checked"] >= options.candidate_page_limit):
                if not candidate.get("already_checked") and not candidate["optional"]:
                    state["incomplete"] = True
                    state["checks"].append({"kind": "page", "url": candidate["url"], "status": "page_budget", "required": True})
                state["candidate_index"] += 1
                progress.state = state
                db.commit()
                continue
            progress.status = "running"
            progress.attempts += 1
            db.commit()
            try:
                if search_step:
                    query = state["queries"][state["search_index"]]
                    if configuration_error:
                        raise PermanentProviderError(configuration_error)
                    process_search(db, job, progress, state, options, search_provider, query, token, clock)
                else:
                    process_page(db, job, progress, state, options, fetcher, candidate, token, clock)
                fence(db, campaign_id, token, clock())
                progress = db.get(JobBusiness, progress_id, populate_existing=True)
                progress.state = state
                progress.status = "pending"
                progress.consecutive_failures = 0
                progress.next_attempt_at = None
                progress.error_kind = None
                record_assessment(db, job, progress, finished=False, at=clock())
                db.commit()
            except (FetchError, PermanentProviderError, RetryableProviderError) as exc:
                db.rollback()
                fence(db, campaign_id, token, clock())
                progress = db.get(JobBusiness, progress_id, populate_existing=True)
                failures = progress.consecutive_failures + 1
                retryable = isinstance(exc, RetryableProviderError) or (isinstance(exc, FetchError) and exc.retryable)
                code = exc.code if isinstance(exc, FetchError) else (configuration_error or "search_failed")
                if retryable and failures <= options.max_retries:
                    delay = min(options.retry_cap_seconds, options.retry_base_seconds * 2 ** (failures - 1) * (1 + jitter()))
                    delay = max(delay, retry_after_seconds(getattr(exc, "retry_after", None), clock()))
                    progress.consecutive_failures = failures
                    progress.next_attempt_at = clock() + timedelta(seconds=delay)
                    progress.error_kind = code
                else:
                    if search_step:
                        state["checks"].append({"kind": "search", "query": query, "status": code,
                            "required": state["search_index"] not in options.optional_search_indices})
                        state["search_index"] += 1
                        state["incomplete"] = True
                    else:
                        state["checks"].append({"kind": "page", "url": candidate["url"], "status": code,
                                                "required": not candidate["optional"]})
                        if candidate["known_match"] and candidate["classification"] == "website" and code in {"page_unavailable", "connection_unavailable", "dns_unavailable"}:
                            state["known_unavailable"].append(candidate["url"])
                        if not candidate["optional"]:
                            state["incomplete"] = True
                        state["candidate_index"] += 1
                    progress.state = state
                    progress.consecutive_failures = 0
                    progress.next_attempt_at = None
                    progress.error_kind = code
                    record_assessment(db, job, progress, finished=False, at=clock())
                progress.status = "pending"
                db.commit()
            except EnrichmentBudget as exc:
                db.rollback()
                fence(db, campaign_id, token, clock())
                progress = db.get(JobBusiness, progress_id, populate_existing=True)
                progress.status = "pending"
                record_assessment(db, job, progress, finished=False, at=clock())
                reason = str(exc)
                db.commit()
                break
        else:
            reason = "batch_limit"
        fence(db, campaign_id, token, clock())
        job = db.get(Job, job_id, populate_existing=True)
        finish_enrichment(db, job, reason)
        db.commit()
        return job
    except (LeaseBusy, LeaseLost):
        db.rollback()
        raise
    except Exception:
        db.rollback()
        fence(db, campaign_id, token, clock())
        job = db.get(Job, job_id, populate_existing=True)
        finish_enrichment(db, job, "interrupted; inspect worker logs")
        db.commit()
        raise
    finally:
        if own_fetcher and fetcher:
            fetcher.close()
        db.rollback()
        release_lease(db, campaign_id, token)


def resume_enrichment(db, job_id: int, *, request_budget: int | None = None):
    job = db.get(Job, job_id)
    if not job or job.kind != "enrichment":
        raise ValueError("Enrichment job not found")
    db.execute(update(Campaign).where(Campaign.id == job.campaign_id).values(requests_used=Campaign.requests_used))
    campaign = db.get(Campaign, job.campaign_id, populate_existing=True)
    if campaign.lease_token and campaign.lease_until and utc(campaign.lease_until) > now():
        raise LeaseBusy("Campaign has an active worker")
    if request_budget is not None:
        job.request_budget = request_budget
    if job.status != "completed":
        job.status = "pending"
        job.cancel_requested = False
        for row in db.scalars(select(JobBusiness).where(JobBusiness.job_id == job_id, JobBusiness.status == "cancelled")):
            row.status = "pending"
    db.commit()
    return job


def manual_correction(db, business_id: int, data: ManualAssessmentInput):
    business = db.scalar(select(Business).where(Business.id == business_id).with_for_update())
    if not business:
        raise ValueError("Business not found")
    current = effective_assessment(db, business_id)
    seed = db.get(Evidence, current.evidence_id) if current else db.scalar(select(Evidence).where(
        Evidence.business_id == business_id, Evidence.persistence_permitted.is_(True)))
    if not seed or not operational_evidence(db, seed):
        seed = next((e for e in db.scalars(select(Evidence).where(Evidence.business_id == business_id,
                     Evidence.evidence_type == "discovery").order_by(Evidence.id.desc()))
                     if operational_evidence(db, e)), None)
    if not seed:
        raise ValueError("No persistence-permitted evidence for this business")
    evidence = Evidence(business_id=business_id, campaign_id=seed.campaign_id, source_url=seed.source_url,
        provider="manual_review", observed_at=now(), match_reasons=[data.reason], retention_policy=seed.retention_policy,
        persistence_permitted=True, export_permitted=False, parent_evidence_id=seed.id, evidence_type="manual",
        evidence_key=digest("manual", str(uuid4())), details={"actor": data.actor,
        "previous_status": current.status if current else "not_checked", "status": data.status})
    db.add(evidence)
    db.flush()
    assessment = WebsiteAssessment(business_id=business_id, evidence_id=evidence.id, status=data.status,
        reasons=[data.reason], origin="manual", corrected_by=data.actor, previous_status=current.status if current else "not_checked",
        checked_at=now(), assessment_key=digest("manual-assessment", str(evidence.id)))
    db.add(assessment)
    db.commit()
    return assessment
