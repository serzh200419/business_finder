"""Bulk local review projection, deterministic priority and manual history."""
from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlencode
from pydantic import BaseModel, Field
from sqlalchemy import select
from app.contacts import normalize_url
from app.eligibility import eligible, dated, utc
from app.models import Business, Campaign, Contact, Evidence, OutreachRecord, WebsiteAssessment, now
from app.schemas import PriorityConfig

STATUSES = {"new", "reviewed", "contacted", "replied", "declined", "won"}
ASSESSMENTS = {"not_checked", "website_found", "social_only", "no_website_found", "website_unavailable", "uncertain"}


class OutreachInput(BaseModel):
    status: str = Field(pattern="^(new|reviewed|contacted|replied|declined|won)$")
    note: str = Field(default="", max_length=2000)
    operator_label: str = Field(min_length=1, max_length=100)
    suppressed: bool | None = None
    suppression_reason: str | None = Field(default=None, max_length=1000)


class ReviewData:
    """Fixed query count; local campaign projection paginates after scoring."""
    def __init__(self, db, campaign_id, at=None):
        self.at = at or now()
        self.campaign = db.get(Campaign, campaign_id)
        if not self.campaign:
            raise ValueError("Campaign not found")
        self.config = PriorityConfig.model_validate(self.campaign.review_priority)
        self.evidence = {e.id: e for e in db.scalars(select(Evidence))}
        ids = {e.business_id for e in self.evidence.values() if e.campaign_id == campaign_id}
        self.businesses = {b.id: b for b in db.scalars(select(Business).where(Business.id.in_(ids)))}
        self.contacts, self.assessments, self.histories = defaultdict(list), defaultdict(list), defaultdict(list)
        for c in db.scalars(select(Contact).where(Contact.business_id.in_(ids)).order_by(Contact.id)):
            self.contacts[c.business_id].append(c)
        for a in db.scalars(select(WebsiteAssessment).where(WebsiteAssessment.business_id.in_(ids)).order_by(WebsiteAssessment.id)):
            self.assessments[a.business_id].append(a)
        for r in db.scalars(select(OutreachRecord).where(OutreachRecord.business_id.in_(ids),
                OutreachRecord.campaign_id == campaign_id).order_by(OutreachRecord.id)):
            self.histories[r.business_id].append(r)
        self.by_business = defaultdict(list)
        for e in self.evidence.values():
            self.by_business[e.business_id].append(e)

    def allows(self, e, purpose="display", suppressed=False):
        return eligible(e, self.evidence, at=self.at, purpose=purpose, suppressed=suppressed).allowed

    def assessment(self, bid):
        manual = [a for a in self.assessments[bid] if a.origin == "manual"]
        if manual:
            return manual[-1]  # An expired correction invalidates rather than silently reverting it.
        candidates = [a for a in self.assessments[bid]
            if self.evidence.get(a.evidence_id) and self.evidence[a.evidence_id].campaign_id == self.campaign.id]
        return max(candidates, key=lambda a: (a.origin == "automatic", utc(a.checked_at or a.assessed_at), a.id), default=None)

    def project(self, bid, purpose="display"):
        b = self.businesses[bid]
        records = self.histories[bid]
        outreach = records[-1].status if records else "new"
        seeds = [e for e in self.by_business[bid] if e.evidence_type == "discovery" and self.allows(e, purpose)]
        # Per-field identity snapshots avoid exporting another source's canonical claims.
        identity_seeds = [e for e in seeds if isinstance(e.details.get("identity"), dict)]
        identity = identity_seeds[-1].details["identity"] if identity_seeds else {}
        from app.maps_browser import listed_url_kind
        listed_website = normalize_url(identity.get("website") or "")
        listing = identity_seeds[-1].details.get("listing", {}) if identity_seeds else {}
        display_seeds = [e for e in self.by_business[bid] if e.evidence_type == "discovery" and self.allows(e)]
        contacts = [c for c in self.contacts[bid] if c.validation_status == "valid" and c.normalized_value
                    and self.allows(self.evidence.get(c.evidence_id), purpose)]
        phones = [c for c in contacts if c.kind == "phone"]
        emails = [c for c in contacts if c.kind == "email"]
        social = [c for c in contacts if c.kind == "social" and c.verification_status == "business_matched"
                  and normalize_url(c.normalized_value)]
        websites = [c for c in contacts if c.kind == "website" and c.verification_status == "business_matched"
                    and normalize_url(c.normalized_value)]
        a = self.assessment(bid)
        assessment_allowed = bool(a and self.allows(self.evidence.get(a.evidence_id), purpose))
        status = a.status if assessment_allowed and a.status in ASSESSMENTS else "not_checked"
        reasons = []
        def add(points, label):
            if points:
                reasons.append({"points": points, "reason": label})
        if status in {"no_website_found", "social_only"}:
            add(getattr(self.config, status), "No website found after checks" if status == "no_website_found" else "Matching social profile only")
        if phones:
            add(self.config.phone, "Valid public business phone with eligible provenance")
        niche_support = [e for e in identity_seeds if e.campaign_id == self.campaign.id and
                         e.details["identity"].get("niche", "").casefold() == self.campaign.niche.casefold()]
        if niche_support:
            add(self.config.niche, f"Campaign niche match: {self.campaign.niche}")
        if social:
            add(self.config.social, "Identity-matched social profile")
        activity = [e for e in self.by_business[bid] if self.allows(e, purpose)
                    and e.evidence_type == "activity" and e.details.get("identity_matched") is True]
        if any((date := dated(e.details.get("activity_at"))) and
               utc(self.at) - timedelta(days=self.config.activity_days) <= date <= utc(self.at) for e in activity):
            add(self.config.activity, "Explicitly dated recent business activity")
        if b.suppressed or outreach == "declined":
            reasons = []
        needs_check = bool(identity and status in {"not_checked", "uncertain", "website_unavailable"}
            and not b.suppressed and outreach not in {"declined", "won"})
        manual_searches = [("Search name + Yerevan", "https://www.google.com/search?" +
            urlencode({"q": identity["name"] + " Yerevan"}))] if identity.get("name") else []
        manual_searches.extend(("Search public phone " + contact.normalized_value,
            "https://www.google.com/search?" + urlencode({"q": contact.normalized_value})) for contact in phones)
        return {"id": bid, "name": identity.get("name", "Identity withheld: no eligible support"),
            "district": identity.get("district", ""), "address": identity.get("address", ""),
            "identity_eligible": bool(identity_seeds and identity.get("name")), "niche_eligible": bool(niche_support),
            "status": status, "assessment_eligible": assessment_allowed,
            "listed_website": listed_website, "listed_website_kind": listed_url_kind(listed_website),
            "website_listing_state": listing.get("website_listing_state"),
            "source_url": normalize_url(identity_seeds[-1].source_url) if identity_seeds else None,
            "manual_searches": manual_searches, "needs_website_check": needs_check,
            "needs_check_priority": (20 if phones else 0) + (20 if not listed_website else
                15 if listed_url_kind(listed_website) == "social_profile" else 0),
            "assessment": a if assessment_allowed else None,
            "checked_at": a.checked_at if assessment_allowed else None,
            "phones": phones, "emails": emails, "social": social, "websites": websites,
            "priority": min(100, sum(r["points"] for r in reasons)), "reasons": reasons,
            "outreach_status": outreach, "review_status": "new" if outreach == "new" else "reviewed",
            "suppressed": b.suppressed, "suppression_reason": b.suppression_reason,
            "queue_eligible": bool(identity and status in {"social_only", "no_website_found"}
                                   and not b.suppressed and outreach not in {"declined", "won"}),
            "record_type": "synthetic" if self.campaign.provider_config.get("discovery") == "fixture" else
                self.campaign.provider_config.get("record_type", "imported/licensed"),
            "history": records}


def review_page(db, campaign_id, *, page=1, page_size=20, niche="", district="", website_status="",
                valid_phone="", review_status="", outreach_status="", suppression="", sort="priority", queue="all"):
    data = ReviewData(db, campaign_id)
    rows = [data.project(bid) for bid in data.businesses]
    rows = [r for r in rows if (not niche or data.campaign.niche.casefold() == niche.casefold())
        and (not district or r["district"] == district)
        and (not website_status or r["status"] == website_status)
        and (not valid_phone or bool(r["phones"]) == (valid_phone == "yes"))
        and (not review_status or r["review_status"] == review_status)
        and (not outreach_status or r["outreach_status"] == outreach_status)
        and (not suppression or r["suppressed"] == (suppression == "yes"))
        and (queue != "prospects" or r["queue_eligible"])
        and (queue != "needs_website_check" or r["needs_website_check"])]
    rows.sort(key=lambda r: ((utc(r["checked_at"]).timestamp() if r["checked_at"] else 0)
                            if sort == "assessment_date" else r["needs_check_priority"] if queue == "needs_website_check" else r["priority"], -r["id"]), reverse=True)
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    return {"campaign": data.campaign, "rows": rows[(page-1)*page_size:page*page_size],
            "total": total, "page": page, "pages": pages, "page_size": page_size}


def record_outreach(db, campaign_id, business_id, data: OutreachInput):
    # Campaign lock serializes per-campaign history; business lock serializes global suppression.
    campaign = db.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
    business = db.scalar(select(Business).where(Business.id == business_id).with_for_update())
    if not campaign or not business or not db.scalar(select(Evidence.id).where(
            Evidence.campaign_id == campaign_id, Evidence.business_id == business_id)):
        raise ValueError("Business is not in this campaign")
    suppression = business.suppressed if data.suppressed is None else data.suppressed
    reason = business.suppression_reason if data.suppression_reason is None else data.suppression_reason
    if suppression and not (reason or "").strip():
        raise ValueError("Suppression requires a reason")
    previous = db.scalar(select(OutreachRecord).where(OutreachRecord.business_id == business_id,
        OutreachRecord.campaign_id == campaign_id).order_by(OutreachRecord.id.desc()))
    record = OutreachRecord(business_id=business_id, campaign_id=campaign_id,
        status=data.status, note=data.note, operator_label=data.operator_label,
        suppressed=suppression, suppression_reason=reason if suppression else None,
        previous_state={"status": previous.status if previous else "new", "suppressed": business.suppressed,
                        "suppression_reason": business.suppression_reason})
    business.suppressed, business.suppression_reason = suppression, reason if suppression else None
    db.add(record)
    db.commit()
    return record
