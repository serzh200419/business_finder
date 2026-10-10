"""Explicit local dry-run/apply cleanup; preserve tombstones and manual audits."""
from sqlalchemy import select
from app.eligibility import eligible
from app.models import Business, BusinessSource, Campaign, Contact, Evidence, Job, JobBusiness, WebsiteAssessment, now
from app.services import utc


def cleanup(db, *, execute=False, at=None):
    at = at or now()
    if execute and any(c.lease_until and utc(c.lease_until) > utc(at) for c in db.scalars(select(Campaign))):
        raise ValueError("Cleanup requires all campaign worker leases to be idle")
    evidence = {e.id: e for e in db.scalars(select(Evidence))}
    blocked = {eid for eid, e in evidence.items() if set(eligible(e, evidence, at=at).reasons)
               & {"expired evidence", "revoked evidence", "cleaned evidence"}}
    contacts = [c for c in db.scalars(select(Contact)) if c.evidence_id in blocked]
    assessments = [a for a in db.scalars(select(WebsiteAssessment))
                   if a.evidence_id in blocked and a.origin != "manual" and
                   (a.status != "not_checked" or a.candidate_urls or a.search_coverage)]
    affected = {evidence[eid].business_id for eid in blocked}
    businesses = [b for b in db.scalars(select(Business)) if b.id in affected]
    unsupported = [b for b in businesses if not any(e.business_id == b.id and e.evidence_type == "discovery"
        and eligible(e, evidence, at=at).allowed for e in evidence.values())
        and (b.name, b.city, b.district, b.address, b.website, b.name_aliases) !=
            (f"Retained business #{b.id}", "Unknown", None, None, None, [])]
    progress = [p for p in db.scalars(select(JobBusiness)) if p.business_id in affected and p.state]
    pending = [e for eid, e in evidence.items() if eid in blocked and not e.cleaned_at and e.evidence_type != "manual"]
    report = {"dry_run": not execute, "evidence_tombstones": len(pending), "contacts_deleted": len(contacts),
        "assessments_invalidated": len(assessments), "canonical_records_redacted": len(unsupported),
        "checkpoints_cleared": len(progress), "manual_audits_retained": True,
        "unknown_retention_excluded_but_not_deleted": sum("unknown retention expiry" in eligible(e, evidence, at=at).reasons
                                                         for e in evidence.values())}
    if not execute:
        return report
    for c in contacts:
        db.delete(c)
    for a in assessments:
        a.status, a.reasons, a.candidate_urls, a.search_coverage = "not_checked", ["Supporting evidence expired or revoked"], [], {}
        a.checked_at = None
    for e in pending:
        e.source_url, e.match_reasons, e.details = "", [], {}
        e.cleaned_at = at
        e.persistence_permitted = e.export_permitted = False
    for b in unsupported:
        b.name, b.city, b.district, b.address, b.website, b.name_aliases = f"Retained business #{b.id}", "Unknown", None, None, None, []
        b.review_required, b.review_reasons = True, ["No eligible identity support remains"]
    for b in businesses:
        supported = [e.details["identity"] for e in evidence.values() if e.business_id == b.id
                     and e.evidence_type == "discovery" and isinstance(e.details.get("identity"), dict)
                     and eligible(e, evidence, at=at).allowed]
        if supported:
            snapshot = supported[-1]
            b.name, b.city = snapshot.get("name", b.name), snapshot.get("city", b.city)
            b.district, b.address, b.name_aliases = snapshot.get("district"), snapshot.get("address"), []
        listed_support = {s.get("website") for s in supported if s.get("website")}
        contact_support = {c.normalized_value for c in db.scalars(select(Contact).where(Contact.business_id == b.id))
                           if c.kind == "website" and c.evidence_id not in blocked
                           and eligible(evidence.get(c.evidence_id), evidence, at=at).allowed}
        if b.website and b.website not in listed_support | contact_support:
            b.website = None
    for source in db.scalars(select(BusinessSource).where(BusinessSource.business_id.in_(affected))):
        if not any(e.source_id == source.id and eligible(e, evidence, at=at).allowed for e in evidence.values()):
            source.source_url = ""
    for p in progress:
        p.state, p.status, p.error_kind, p.next_attempt_at = {}, "cancelled", "retention_cleanup", None
        job = db.get(Job, p.job_id)
        job.cancel_requested, job.status = True, "cancelled"
        job.result = {"stop_reason": "retention_cleanup; create a fresh job from eligible evidence"}
    db.commit()
    return report
