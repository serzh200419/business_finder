from pathlib import Path
import json
from threading import Lock
from urllib.parse import urlencode
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import Business, BusinessSource, Campaign, Evidence, Job, SearchQuery, WebsiteAssessment, now
from app.schemas import CampaignInput, CampaignOutput
from app.services import (LeaseBusy, LeaseLost, create_job, finish_job, job_summary, resume_job, run_discovery,
                          run_job, sync_plan, utc)
from app.planner import plan_campaign
from app.providers import CSVProvider, ProviderError
from app.schemas import ProviderConfig
from pydantic import BaseModel
from app.config import get_settings
from app.providers import get_provider, licensed_providers, web_search_providers, SafeProviderError

app = FastAPI(title="Yerevan Lead Discovery")
from app.local_security import LocalSecurityMiddleware
app.add_middleware(LocalSecurityMiddleware)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
local_run_lock = Lock()


def find_campaign(campaign_id: int, db: Session) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    return campaign


def write_campaign(campaign: Campaign, data: CampaignInput, db: Session):
    if campaign.id:
        db.execute(update(Campaign).where(Campaign.id == campaign.id).values(requests_used=Campaign.requests_used))
        db.refresh(campaign)
        if campaign.lease_token and campaign.lease_until and utc(campaign.lease_until) > now():
            raise HTTPException(409, "Campaign is leased by a worker")
    for key, value in data.model_dump().items():
        setattr(campaign, key, value)
    db.add(campaign)
    db.flush()
    sync_plan(db, campaign)  # Keep historical queries and their evidence associations.
    db.commit()
    return campaign


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(503, "Database unavailable")
    return {"status": "ok", "database": "ok"}


@app.post("/api/campaigns", response_model=CampaignOutput, status_code=201)
def create_campaign(data: CampaignInput, db: Session = Depends(get_db)):
    return write_campaign(Campaign(), data, db)


@app.get("/api/campaigns", response_model=list[CampaignOutput])
def list_campaigns(db: Session = Depends(get_db)):
    return list(db.scalars(select(Campaign).order_by(Campaign.id)))


@app.get("/api/campaigns/{campaign_id}", response_model=CampaignOutput)
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    return find_campaign(campaign_id, db)


@app.put("/api/campaigns/{campaign_id}", response_model=CampaignOutput)
def update_campaign(campaign_id: int, data: CampaignInput, db: Session = Depends(get_db)):
    return write_campaign(find_campaign(campaign_id, db), data, db)


@app.delete("/api/campaigns/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = find_campaign(campaign_id, db)
    db.execute(update(Campaign).where(Campaign.id == campaign_id).values(requests_used=Campaign.requests_used))
    db.refresh(campaign)
    if campaign.lease_token and campaign.lease_until and utc(campaign.lease_until) > now():
        raise HTTPException(409, "Cancel work and wait for its lease to release before deleting")
    db.delete(campaign)
    db.commit()
    return Response(status_code=204)


@app.post("/api/campaigns/{campaign_id}/discover")
def discover(campaign_id: int, db: Session = Depends(get_db)):
    find_campaign(campaign_id, db)
    try:
        job = run_discovery(db, campaign_id)
    except (LeaseBusy, LeaseLost) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"job_id": job.id, "status": job.status, "result": job.result}


@app.get("/api/campaigns/{campaign_id}/leads")
def leads(campaign_id: int, db: Session = Depends(get_db)):
    find_campaign(campaign_id, db)
    rows = db.execute(select(Business, Evidence, WebsiteAssessment).join(
        Evidence, Evidence.business_id == Business.id).join(
        WebsiteAssessment, WebsiteAssessment.evidence_id == Evidence.id).where(Evidence.campaign_id == campaign_id))
    from app.eligibility import eligible, identity_snapshot
    evidence_by_id = {e.id: e for e in db.scalars(select(Evidence))}
    return [{"business_id": b.id, "name": identity_snapshot(e, evidence_by_id).get("name", "Identity withheld"),
             "website": identity_snapshot(e, evidence_by_id).get("website"),
             "query_id": e.query_id, "source_id": e.source_id,
             "review_required": b.review_required, "review_reasons": b.review_reasons,
             "assessment": a.status, "reasons": a.reasons, "source_url": e.source_url,
             "provider": e.provider, "observed_at": e.observed_at,
             "retention_policy": e.retention_policy, "persistence_permitted": e.persistence_permitted,
             "export_permitted": eligible(e, evidence_by_id, purpose="export").allowed} for b, e, a in rows
            if eligible(e, evidence_by_id).allowed]


@app.get("/api/campaigns/{campaign_id}/plan")
def preview_plan(campaign_id: int, db: Session = Depends(get_db)):
    campaign = find_campaign(campaign_id, db)
    return plan_campaign(CampaignInput.model_validate(campaign, from_attributes=True))


@app.post("/api/csv/validate")
def validate_csv(config: ProviderConfig, db: Session = Depends(get_db)):
    try:
        provider = CSVProvider(config)
        results, report = provider.validate()
    except (ValueError, ProviderError) as exc:
        raise HTTPException(422, str(exc))
    existing = sum(db.scalar(select(BusinessSource.id).where(BusinessSource.provider == row.provider,
                      BusinessSource.external_id == row.external_id)) is not None for row in results)
    return {**report, "existing": existing, "newly_created": 0,
            "would_create": len(results) - existing, "dry_run": True}


class ResumeOptions(BaseModel):
    retry_failed: bool = False
    disable_fixture_failures: bool = False


@app.post("/api/campaigns/{campaign_id}/jobs", status_code=201)
def new_job(campaign_id: int, db: Session = Depends(get_db)):
    find_campaign(campaign_id, db)
    try:
        job = create_job(db, campaign_id)
    except (LeaseBusy, LeaseLost) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"job_id": job.id, "status": job.status}


@app.get("/api/jobs/{job_id}")
def inspect_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return {"job_id": job.id, "status": job.status, "result": job_summary(db, job),
            "stop_reason": job.result.get("stop_reason"), "cancel_requested": job.cancel_requested}


@app.post("/api/jobs/{job_id}/resume")
def resume(job_id: int, options: ResumeOptions, db: Session = Depends(get_db)):
    if db.get(Job, job_id) is None:
        raise HTTPException(404, "Job not found")
    try:
        job = resume_job(db, job_id, **options.model_dump())
        job = run_job(db, job.id)
    except (LeaseBusy, LeaseLost) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"job_id": job.id, "status": job.status, "result": job.result}


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.status == "completed":
        raise HTTPException(409, "Completed job cannot be cancelled")
    job.cancel_requested = True
    db.commit()
    campaign = db.get(Campaign, job.campaign_id, populate_existing=True)
    db.execute(update(Campaign).where(Campaign.id == campaign.id).values(requests_used=Campaign.requests_used))
    db.refresh(campaign)
    if not campaign.lease_token or not campaign.lease_until or utc(campaign.lease_until) <= now():
        finish_job(db, job, "cancelled")
        db.commit()
    return {"job_id": job.id, "status": job.status, "cancel_requested": True}


@app.post("/api/campaigns/{campaign_id}/enqueue", status_code=202)
def enqueue(campaign_id: int, db: Session = Depends(get_db)):
    find_campaign(campaign_id, db)
    from app.tasks import discover_job
    try:
        job = create_job(db, campaign_id)
        task = discover_job.delay(job.id)
    except (LeaseBusy, LeaseLost) as exc:
        raise HTTPException(409, str(exc))
    except Exception:
        raise HTTPException(503, "Worker broker unavailable")
    return {"task_id": task.id, "campaign_id": campaign_id, "job_id": job.id}


@app.get("/api/campaigns/{campaign_id}/jobs")
def jobs(campaign_id: int, db: Session = Depends(get_db)):
    find_campaign(campaign_id, db)
    return [{"id": j.id, "status": j.status, "result": job_summary(db, j)}
            for j in db.scalars(select(Job).where(Job.campaign_id == campaign_id))]


@app.get("/")
def index(request: Request, message: str = "", db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="index.html", context={
        "campaigns": list(db.scalars(select(Campaign))),
        "jobs": list(db.scalars(select(Job).order_by(Job.id.desc()).limit(20))),
        "message": message, "demo_mode": get_settings().demo_mode,
        "licensed_count": len(licensed_providers), "live_search": "live" in web_search_providers})


@app.post("/campaigns")
def create_form(niche: str = Form(...), district: str = Form(""), city: str = Form("Yerevan"),
                keywords_en: str = Form(""), keywords_hy: str = Form(""), keywords_ru: str = Form(""),
                provider: str = Form("fixture"), provider_options: str = Form("{}"), db: Session = Depends(get_db)):
    try:
        config = ProviderConfig.model_validate({**json.loads(provider_options), "discovery": provider})
        get_provider(config, get_settings().demo_mode)  # Never substitute fixtures for an unavailable adapter.
        keywords = {lang: [v.strip() for v in text.split(",") if v.strip()]
                    for lang, text in [("en", keywords_en), ("hy", keywords_hy), ("ru", keywords_ru)] if text.strip()}
        write_campaign(Campaign(), CampaignInput(niche=niche, city=city,
            districts=[v.strip() for v in district.split(",") if v.strip()],
            keywords=keywords or {"en": [niche]}, provider_config=config,
            request_budget=200 if provider == "maps_browser" else 20), db)
    except (ValueError, TypeError, ProviderError):
        db.rollback()
        return RedirectResponse("/?" + urlencode({"message": "Campaign configuration invalid or provider unavailable. CSV requires a path, license reference, storage permission and retention policy; see local workflow instructions."}), status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/campaigns/{campaign_id}/discover")
def discover_form(campaign_id: int, db: Session = Depends(get_db)):
    return local_action(db, campaign_id, "discovery")


def local_action(db: Session, campaign_id: int, kind: str, *, job_id: int | None = None,
                 retry_failed: bool = False):
    # Sync FastAPI routes run off the event loop: review/health remain usable while a run waits.
    # Reject overlapping clicks instead of introducing a local queue.
    if not local_run_lock.acquire(blocking=False):
        return RedirectResponse("/?" + urlencode({"message": "A local run is active. Wait for it to finish before starting another."}), status_code=303)
    try:
        from app.enrichment import create_enrichment_job, run_enrichment_job, resume_enrichment
        from app.schemas import EnrichmentOptions
        from app.providers import get_web_search_provider
        campaign = find_campaign(campaign_id, db)
        if job_id is not None:
            job = db.get(Job, job_id)
            if job is None or job.campaign_id != campaign_id:
                raise ValueError("Job not found")
            if job.kind == "enrichment":
                job = resume_enrichment(db, job_id)
                job = run_enrichment_job(db, job.id)
            else:
                job = resume_job(db, job_id, retry_failed=retry_failed)
                job = run_job(db, job.id)
        elif kind == "discovery":
            job = run_discovery(db, campaign_id)
        else:
            synthetic = campaign.provider_config.get("discovery") == "fixture" or campaign.provider_config.get("record_type") == "synthetic"
            options = EnrichmentOptions(search_provider="fixture" if synthetic else "live",
                fetch_mode="fixture" if synthetic else "live")
            get_web_search_provider(options.search_provider, options.fixture_dataset, get_settings().demo_mode)
            if not synthetic and not get_settings().live_public_fetch_enabled:
                raise ValueError("Live public fetching disabled")
            job = create_enrichment_job(db, campaign_id, options)
            job = run_enrichment_job(db, job.id)
        message = f"Job {job.id}: {job.status}. See job details for progress, errors and retry deadlines; Resume continues saved checkpoints."
        if job.result.get("provider_restriction"):
            message = job.result["provider_restriction"]
    except SafeProviderError as exc:
        db.rollback()
        message = str(exc)
    except (ValueError, ProviderError, HTTPException):
        db.rollback()
        message = "Run unavailable: check provider configuration, remaining budget and job details. After an interruption, wait up to 120 seconds for the lease to expire, then Resume. Real enrichment requires a registered live search adapter, live-fetch enablement and per-host access policies."
    except Exception:
        db.rollback()
        message = "Local run failed; saved checkpoints are retained. Inspect job details and resume after correcting configuration."
    finally:
        local_run_lock.release()
    return RedirectResponse("/?" + urlencode({"message": message}), status_code=303)


@app.post("/campaigns/{campaign_id}/enrich")
def enrich_form(campaign_id: int, db: Session = Depends(get_db)):
    return local_action(db, campaign_id, "enrichment")


@app.post("/local/jobs/{job_id}/resume")
def resume_form(job_id: int, retry_failed: bool = Form(False), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return local_action(db, job.campaign_id, job.kind, job_id=job_id, retry_failed=retry_failed)


@app.post("/local/jobs/{job_id}/cancel")
def cancel_form(job_id: int, db: Session = Depends(get_db)):
    cancel(job_id, db)
    return RedirectResponse("/?" + urlencode({"message": "Cancellation requested; it is observed between steps and during bounded Maps waits. Resume explicitly to continue."}), status_code=303)


from app.enrichment_routes import router as enrichment_router
app.include_router(enrichment_router)
from app.review_routes import router as review_router
app.include_router(review_router)
