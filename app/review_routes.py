import base64
import json
from pathlib import Path
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import get_db
from app.enrichment_routes import STATUS_LABELS
from app.export import selected_export
from app.models import Campaign
from app.review import ReviewData, OutreachInput, record_outreach, review_page

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/review")
def dashboard(request: Request, campaign_id: int | None = None, page: int = Query(1, ge=1),
              page_size: int = Query(20, ge=1, le=100), niche: str = "", district: str = "",
              website_status: str = "", valid_phone: str = "", review_status: str = "",
              outreach_status: str = "", suppression: str = "", sort: str = "priority", queue: str = "prospects",
              db: Session = Depends(get_db)):
    campaigns = list(db.scalars(select(Campaign).order_by(Campaign.id)))
    cid = campaign_id or (campaigns[0].id if campaigns else None)
    options = dict(niche=niche, district=district, website_status=website_status, valid_phone=valid_phone,
                   review_status=review_status, outreach_status=outreach_status, suppression=suppression, sort=sort, queue=queue)
    try:
        result = review_page(db, cid, page=page, page_size=page_size, **options) if cid else {
            "campaign": None, "rows": [], "total": 0, "page": 1, "pages": 1, "page_size": page_size}
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    params = {"campaign_id": cid, "page_size": page_size, **options}
    link = lambda p: "/review?" + urlencode({**params, "page": p})
    return templates.TemplateResponse(request=request, name="review.html", context={**result,
        "campaigns": campaigns, "filters": options, "status_labels": STATUS_LABELS,
        "previous_url": link(max(1, result["page"]-1)), "next_url": link(result["page"]+1),
        "return_query": urlencode({**params, "page": result["page"]})})


@router.get("/review/{campaign_id}/businesses/{business_id}")
def review_business(request: Request, campaign_id: int, business_id: int, return_query: str = "", db: Session = Depends(get_db)):
    return_query = return_query or urlencode({"campaign_id": campaign_id, "queue": "all"})
    try:
        data = ReviewData(db, campaign_id)
        if business_id not in data.businesses:
            raise ValueError("Business is not in this campaign")
        row = data.project(business_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return templates.TemplateResponse(request=request, name="review_business.html", context={"row": row,
        "campaign": data.campaign, "return_query": return_query, "status_labels": STATUS_LABELS})


@router.post("/api/campaigns/{campaign_id}/businesses/{business_id}/outreach")
def outreach_api(campaign_id: int, business_id: int, data: OutreachInput, db: Session = Depends(get_db)):
    try:
        record = record_outreach(db, campaign_id, business_id, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"record_id": record.id, "status": record.status, "recorded_at": record.recorded_at,
            "manual_only": True}


@router.post("/review/{campaign_id}/businesses/{business_id}/outreach")
async def outreach_form(request: Request, campaign_id: int, business_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        data = OutreachInput(status=str(form.get("status", "")), note=str(form.get("note", "")),
            operator_label=str(form.get("operator_label", "")), suppressed=form.get("suppressed") == "yes",
            suppression_reason=str(form.get("suppression_reason", "")))
        record_outreach(db, campaign_id, business_id, data)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, "Valid status, operator label and suppression reason required")
    return RedirectResponse(f"/review/{campaign_id}/businesses/{business_id}?" +
                            urlencode({"return_query": str(form.get("return_query", ""))}), status_code=303)


class Selection(BaseModel):
    business_ids: list[int] = Field(min_length=1, max_length=1000)


@router.post("/api/campaigns/{campaign_id}/export")
def export_api(campaign_id: int, selection: Selection, db: Session = Depends(get_db)):
    try:
        content, summary = selected_export(db, campaign_id, selection.business_ids)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return Response(content, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="selected-leads.csv"',
        "X-Export-Summary": json.dumps(summary, ensure_ascii=True), "Cache-Control": "no-store"})


@router.post("/review/{campaign_id}/export")
async def export_form(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        selected = Selection(business_ids=form.getlist("business_ids"))
        content, summary = selected_export(db, campaign_id, selected.business_ids)
    except (ValidationError, ValueError):
        raise HTTPException(422, "Select 1 to 1000 campaign business IDs")
    response = templates.TemplateResponse(request=request, name="export.html", context={"summary": summary,
        "download": "data:text/csv;charset=utf-8;base64," + base64.b64encode(content).decode(),
        "return_query": str(form.get("return_query", ""))})
    response.headers["Cache-Control"] = "no-store"
    return response
