"""Selected campaign export with independent field permissions and Excel safety."""
import csv
import io
import unicodedata
from urllib.parse import urlsplit
from app.review import ReviewData

COLUMNS = ["business_name", "niche", "district", "address", "public_phone", "public_email",
    "website_status", "website_url", "instagram_url", "facebook_url", "assessment_checked_at",
    "review_priority", "priority_reasons", "outreach_status", "contact_source_urls"]


def safe_cell(value):
    text = "" if value is None else str(value)
    significant = text.lstrip()
    while significant and (significant[0].isspace() or unicodedata.category(significant[0]).startswith("C")):
        significant = significant[1:]
    return "'" + text if significant.startswith(("=", "+", "-", "@")) else text


def selected_export(db, campaign_id, selected_ids):
    data = ReviewData(db, campaign_id)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    summary = {"selected": len(set(selected_ids)), "exported": 0, "omitted_rows": [], "omitted_fields": []}
    for bid in dict.fromkeys(selected_ids):
        if bid not in data.businesses:
            summary["omitted_rows"].append({"id": bid, "reason": "not in campaign"})
            continue
        local = data.project(bid)
        if local["suppressed"] or local["outreach_status"] == "declined":
            summary["omitted_rows"].append({"id": bid, "reason": "suppressed or declined"})
            continue
        row = data.project(bid, "export")
        if not row["identity_eligible"]:
            summary["omitted_rows"].append({"id": bid, "reason": "identity lacks export-eligible evidence"})
            continue
        social = lambda host: "; ".join(dict.fromkeys(c.normalized_value for c in row["social"]
                if (urlsplit(c.normalized_value).hostname or "").removeprefix("www.") == host))
        contacts = [*row["phones"], *row["emails"], *row["websites"], *row["social"]]
        fields = {"business_name": row["name"], "niche": data.campaign.niche if row["niche_eligible"] else "",
            "district": row["district"], "address": row["address"],
            "public_phone": "; ".join(dict.fromkeys(c.normalized_value for c in row["phones"])),
            "public_email": "; ".join(dict.fromkeys(c.normalized_value for c in row["emails"])),
            "website_status": row["status"] if row["assessment_eligible"] else "",
            "website_url": "; ".join(dict.fromkeys(c.normalized_value for c in row["websites"])),
            "instagram_url": social("instagram.com"), "facebook_url": social("facebook.com"),
            "assessment_checked_at": row["checked_at"].isoformat() if row["checked_at"] else "",
            "review_priority": row["priority"],
            "priority_reasons": "; ".join(f"+{r['points']} {r['reason']}" for r in row["reasons"]),
            "outreach_status": row["outreach_status"],
            "contact_source_urls": "; ".join(dict.fromkeys(data.evidence[c.evidence_id].source_url for c in contacts))}
        omitted = []
        if not row["assessment_eligible"] and local["assessment_eligible"]:
            omitted += ["website_status", "assessment_checked_at"]
        if not row["niche_eligible"] and local["niche_eligible"]:
            omitted += ["niche"]
        for key, kind in (("public_phone", "phones"), ("public_email", "emails"),
                          ("website_url", "websites"), ("social_urls", "social")):
            contact_kind = {"phones": "phone", "emails": "email", "websites": "website", "social": "social"}[kind]
            recorded = {c.normalized_value for c in data.contacts[bid] if c.kind == contact_kind
                        and c.validation_status == "valid" and c.normalized_value}
            exported = {c.normalized_value for c in row[kind]}
            if recorded - exported:
                omitted.append(key)
        if local["reasons"] != row["reasons"]:
            omitted += ["review_priority", "priority_reasons"]
        if omitted:
            summary["omitted_fields"].append({"id": bid, "fields": omitted,
                "reason": "restricted, expired, revoked or missing export support; priority recomputed from exportable evidence"})
        writer.writerow({k: safe_cell(v) for k, v in fields.items()})
        summary["exported"] += 1
    return output.getvalue().encode("utf-8-sig"), summary
