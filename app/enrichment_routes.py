from pathlib import Path
from urllib.parse import urlencode
from typing import Annotated
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.contacts import normalize_url
from app.db import get_db
from app.enrichment import (create_enrichment_job, effective_assessment, enrichment_summary,
                            manual_correction, resume_enrichment, run_enrichment_job)
from app.models import Business, Contact, Evidence, Job, WebsiteAssessment
from app.schemas import AssessmentStatus, EnrichmentOptions, ManualAssessmentInput
from app.services import LeaseBusy, LeaseLost

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATUS_LABELS = {"not_checked": "Not checked", "website_found": "Website found",
    "social_only": "Social only", "no_website_found": "No website found after checks",
    "website_unavailable": "Website unavailable", "uncertain": "Uncertain"}


def business_details(db, business_id):
    business = db.get(Business, business_id)
    if not business:
        raise HTTPException(404, "Business not found")
    from app.eligibility import eligible
    evidence_by_id = {e.id: e for e in db.scalars(select(Evidence))}
    identities = [e.details["identity"] for e in evidence_by_id.values() if e.business_id == business_id
                  and isinstance(e.details.get("identity"), dict) and eligible(e, evidence_by_id).allowed]
    identity = identities[-1] if identities else {}
    assessment = effective_assessment(db, business_id)
    if assessment and not eligible(evidence_by_id.get(assessment.evidence_id), evidence_by_id).allowed:
        assessment = None
    automatic = db.scalar(select(WebsiteAssessment).where(WebsiteAssessment.business_id == business_id,
        WebsiteAssessment.origin == "automatic").order_by(WebsiteAssessment.checked_at.desc()))
    contacts = []
    for contact, evidence in db.execute(select(Contact, Evidence).join(Evidence).where(Contact.business_id == business_id)):
        if not eligible(evidence, evidence_by_id).allowed:
            continue
        contacts.append({"kind": contact.kind, "raw": contact.raw_value or contact.value,
            "normalized": contact.normalized_value, "validation_status": contact.validation_status,
            "validation_details": contact.validation_details, "verification_status": contact.verification_status,
            "observed_at": contact.observed_at, "source_url": normalize_url(contact.source_url or evidence.source_url),
            "evidence_id": evidence.id, "persistence_permitted": evidence.persistence_permitted,
            "export_permitted": eligible(evidence, evidence_by_id, purpose="export").allowed})
    audits = [{"status": item.status, "reason": item.reasons, "actor": item.corrected_by,
               "previous_status": item.previous_status, "checked_at": item.checked_at}
              for item in db.scalars(select(WebsiteAssessment).where(WebsiteAssessment.business_id == business_id,
                 WebsiteAssessment.origin == "manual").order_by(WebsiteAssessment.id.desc()))]
    if automatic and not eligible(evidence_by_id.get(automatic.evidence_id), evidence_by_id).allowed:
        automatic = None
    return {"business_id": business.id, "name": identity.get("name", "Identity withheld: no eligible support"),
        "city": identity.get("city", ""), "district": identity.get("district", ""),
        "address": identity.get("address", ""), "contacts": contacts,
        "assessment": {"status": assessment.status if assessment.status in STATUS_LABELS else "not_checked",
            "label": STATUS_LABELS.get(assessment.status, "Not checked"),
            "reasons": assessment.reasons, "checked_at": assessment.checked_at, "origin": assessment.origin} if assessment else {
                "status": "not_checked", "label": "Not checked", "reasons": [], "checked_at": None, "origin": None},
        "candidate_urls": automatic.candidate_urls if automatic else [],
        "search_coverage": automatic.search_coverage if automatic else {}, "audit_trail": audits}


def job_response(db, job):
    return {"job_id": job.id, "status": job.status, "kind": job.kind,
            "result": enrichment_summary(db, job), "stop_reason": job.result.get("stop_reason")}


@router.post("/api/campaigns/{campaign_id}/enrich")
def enrich(campaign_id: int, options: EnrichmentOptions, db: Session = Depends(get_db)):
    try:
        job = create_enrichment_job(db, campaign_id, options)
        job = run_enrichment_job(db, job.id)
    except (LeaseBusy, LeaseLost) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return job_response(db, job)


@router.post("/api/campaigns/{campaign_id}/enrich/enqueue", status_code=202)
def enqueue_enrichment(campaign_id: int, options: EnrichmentOptions, db: Session = Depends(get_db)):
    try:
        job = create_enrichment_job(db, campaign_id, options)
    except (LeaseBusy, LeaseLost) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    from app.tasks import discover_job
    try:
        task = discover_job.delay(job.id)
    except Exception:
        raise HTTPException(503, "Worker broker unavailable; persisted job can be resumed locally")
    return {"job_id": job.id, "task_id": task.id}


class EnrichmentResumeInput(BaseModel):
    request_budget: int | None = Field(default=None, ge=0, le=10000)


@router.post("/api/enrichment/jobs/{job_id}/resume")
def resume(job_id: int, options: EnrichmentResumeInput, db: Session = Depends(get_db)):
    try:
        job = resume_enrichment(db, job_id, request_budget=options.request_budget)
        job = run_enrichment_job(db, job.id)
    except (LeaseBusy, LeaseLost) as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return job_response(db, job)


@router.get("/api/businesses/{business_id}")
def inspect_business(business_id: int, db: Session = Depends(get_db)):
    return business_details(db, business_id)


@router.get("/businesses")
def list_businesses(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse("/review", status_code=303)


@router.get("/businesses/{business_id}")
def business_detail(request: Request, business_id: int, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request=request, name="business.html",
        context={"detail": business_details(db, business_id), "status_labels": STATUS_LABELS})


@router.post("/api/businesses/{business_id}/assessment/correct")
def correct(business_id: int, data: ManualAssessmentInput, db: Session = Depends(get_db)):
    try:
        assessment = manual_correction(db, business_id, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"assessment_id": assessment.id, "status": assessment.status, "origin": "manual"}


@router.post("/businesses/{business_id}/assessment/correct")
def correct_form(business_id: int, status: Annotated[AssessmentStatus, Form()],
                 reason: Annotated[str, Form()], actor: Annotated[str, Form()],
                 review_return: Annotated[str, Form()] = "", db: Session = Depends(get_db)):
    try:
        data = ManualAssessmentInput(status=status, reason=reason, actor=actor)
    except ValidationError:
        raise HTTPException(422, "Valid status, reason and reviewer name required")
    correct(business_id, data, db)
    suffix = "?" + urlencode({"review_return": review_return}) if review_return else ""
    return RedirectResponse(f"/businesses/{business_id}" + suffix, status_code=303)
