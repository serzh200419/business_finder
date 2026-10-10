"""Clearly synthetic, credential-free review/export/cleanup demonstration."""
from datetime import timedelta
from sqlalchemy import select
from app.config import get_settings
from app.contacts import persist_contact
from app.export import selected_export
from app.models import Business, Campaign, Evidence, WebsiteAssessment, now
from app.review import OutreachInput, ReviewData, record_outreach
from app.retention import cleanup


def seed_review_demo(db):
    if not get_settings().demo_mode:
        raise ValueError("Review fixtures require DEMO_MODE=true")
    campaign = Campaign(niche="synthetic dentist", city="Yerevan", keywords={"en": ["synthetic"]},
                        provider_config={"discovery": "fixture"})
    db.add(campaign)
    db.flush()
    cases = [
        ("social", "Synthetic Social Արմատ Клиника", "social_only"),
        ("no_site", "Synthetic No Website Dental", "no_website_found"),
        ("uncertain", "Synthetic Uncertain Dental", "uncertain"),
        ("no_phone", "Synthetic Missing Phone Dental", "no_website_found"),
        ("mixed", "Synthetic Mixed Permissions Dental", "no_website_found"),
        ("expired", "Synthetic Expired Dental", "no_website_found"),
        ("revoked", "Synthetic Revoked Dental", "social_only"),
        ("override", "Synthetic Manual Override Dental", "website_found"),
        ("declined", "Synthetic Declined Dental", "no_website_found"),
        ("suppressed", "Synthetic Suppressed Dental", "no_website_found"),
        ("formula", "\t\ufeff=HYPERLINK(\"https://example.invalid\",\"Synthetic\")", "no_website_found"),
        ("independent", "Synthetic Independent Support Dental", "no_website_found"),
    ]
    ids = {}
    for key, name, status in cases:
        b = Business(name=name, city="Yerevan", district="Kentron" if key != "no_phone" else "Arabkir",
                     address="1 Synthetic Street", identity_key=f"review-fixture:{campaign.id}:{key}")
        db.add(b)
        db.flush()
        ids[key] = b.id
        seed = Evidence(business_id=b.id, campaign_id=campaign.id, source_url=f"https://example.invalid/review/{key}",
            provider="review_fixture", match_reasons=["Synthetic owned fixture identity"], retention_policy="synthetic fixture; indefinite",
            persistence_permitted=True, export_permitted=True, evidence_type="discovery", details={"identity": {
                "name": name, "city": b.city, "district": b.district, "address": b.address, "niche": campaign.niche}})
        if key in {"expired", "independent"}:
            seed.expires_at = now() - timedelta(days=1)
        if key == "revoked":
            seed.revoked_at = now()
        db.add(seed)
        db.flush()
        if key != "no_phone":
            persist_contact(db, b.id, seed, "phone", "+37410123456")
        if key == "independent":
            independent = Evidence(business_id=b.id, campaign_id=campaign.id,
                source_url="https://example.invalid/review/independent-new", provider="review_fixture",
                match_reasons=["Independent synthetic source"], retention_policy="indefinite", persistence_permitted=True,
                export_permitted=True, details=dict(seed.details))
            db.add(independent)
            db.flush()
            persist_contact(db, b.id, independent, "phone", "+37410123456")
            seed = independent
        if key == "mixed":
            restricted = Evidence(business_id=b.id, campaign_id=campaign.id, source_url="https://example.invalid/restricted",
                provider="review_fixture", match_reasons=["Synthetic contact, no export"], retention_policy="indefinite",
                persistence_permitted=True, export_permitted=False, parent_evidence_id=seed.id, evidence_type="public_page")
            db.add(restricted)
            db.flush()
            persist_contact(db, b.id, restricted, "email", "Restricted.Office@example.invalid", matched=True)
        if key == "social":
            persist_contact(db, b.id, seed, "social", "https://www.instagram.com/synthetic_review_demo/", matched=True)
            db.add(Evidence(business_id=b.id, campaign_id=campaign.id, source_url="https://example.invalid/activity",
                provider="review_fixture", match_reasons=["Fictional dated business announcement"], retention_policy="indefinite",
                persistence_permitted=True, export_permitted=True, parent_evidence_id=seed.id, evidence_type="activity",
                details={"activity_at": (now()-timedelta(days=2)).isoformat(), "identity_matched": True}))
        ae = Evidence(business_id=b.id, campaign_id=campaign.id, source_url=seed.source_url,
            provider="review_fixture", match_reasons=["Synthetic completed assessment"], retention_policy="indefinite",
            persistence_permitted=True, export_permitted=True, parent_evidence_id=seed.id, evidence_type="assessment")
        db.add(ae)
        db.flush()
        db.add(WebsiteAssessment(business_id=b.id, evidence_id=ae.id, status=status, origin="automatic",
            reasons=["Synthetic assessment; no live checks"], checked_at=now(), search_coverage={"complete": True}))
    db.commit()
    for key, status, suppressed in [("declined", "declined", False), ("suppressed", "reviewed", True)]:
        record_outreach(db, campaign.id, ids[key], OutreachInput(status=status, operator_label="synthetic operator",
            note="Synthetic manual action only", suppressed=suppressed, suppression_reason="Synthetic do-not-contact" if suppressed else ""))
    from app.enrichment import manual_correction
    from app.schemas import ManualAssessmentInput
    manual_correction(db, ids["override"], ManualAssessmentInput(status="no_website_found",
        reason="Synthetic operator correction preserved over automated reruns", actor="synthetic operator label"))
    return campaign.id, ids


def review_demo(db, output=None):
    cid, ids = seed_review_demo(db)
    content, summary = selected_export(db, cid, list(ids.values()))
    if output:
        output.write_bytes(content)
    if summary["exported"] != 8:
        raise RuntimeError("Unexpected synthetic export outcome")
    preview = cleanup(db)
    applied = cleanup(db, execute=True)
    data = ReviewData(db, cid)
    if not data.project(ids["independent"])["phones"] or data.project(ids["override"])["status"] != "no_website_found":
        raise RuntimeError("Cleanup did not preserve independent support/manual correction")
    return {"synthetic_only": True, "campaign_id": cid, "business_ids": ids, "export": summary,
            "cleanup_preview": preview, "cleanup_applied": applied}
