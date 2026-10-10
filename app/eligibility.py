"""One conservative policy gate for operational display, scoring and export."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from app.models import Evidence, now


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def dated(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def retention(evidence):
    """Intersect every nonempty declaration; unknown declarations fail closed."""
    deadlines = [utc(evidence.expires_at)] if evidence.expires_at else []
    explicit = evidence.expires_at is not None
    for raw_part in evidence.retention_policy.split(";"):
        raw_part = raw_part.strip()
        part = raw_part.casefold()
        if not part or part in {"synthetic fixture", "synthetic demo", "owned synthetic fixture"} or part.startswith("license:"):
            continue
        if part == "indefinite":
            explicit = True
        elif re.fullmatch(r"[1-9][0-9]{0,5} days", part):
            deadlines.append(utc(evidence.observed_at) + timedelta(days=int(part.split()[0])))
            explicit = True
        elif part.startswith("expires:") and dated(raw_part[8:].strip()):
            deadlines.append(dated(raw_part[8:].strip()))
            explicit = True
        else:
            return "unknown", None
    if not explicit:
        return "unknown", None
    return "declared", min(deadlines) if deadlines else None


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    reasons: tuple[str, ...] = ()


def identity_snapshot(evidence, evidence_by_id):
    seen = set()
    while evidence and evidence.id not in seen:
        seen.add(evidence.id)
        if not eligible(evidence, evidence_by_id).allowed:
            return {}
        if isinstance(evidence.details.get("identity"), dict):
            return evidence.details["identity"]
        evidence = evidence_by_id.get(evidence.parent_evidence_id)
    return {}


def eligible(evidence: Evidence | None, evidence_by_id: dict[int, Evidence], *,
             purpose="display", at=None, suppressed=False, _seen=None) -> Eligibility:
    at = utc(at or now())
    if suppressed:
        return Eligibility(False, ("suppressed",))
    if evidence is None:
        return Eligibility(False, ("missing supporting evidence",))
    seen = set(_seen or ())
    if evidence.id in seen or len(seen) >= 100:
        return Eligibility(False, ("cyclic evidence support",))
    seen.add(evidence.id)
    reasons = []
    if not evidence.persistence_permitted:
        reasons.append("persistence forbidden")
    if purpose == "export" and not evidence.export_permitted:
        reasons.append("export forbidden")
    if evidence.revoked_at:
        reasons.append("revoked evidence")
    if evidence.cleaned_at:
        reasons.append("cleaned evidence")
    mode, expiry = retention(evidence)
    if mode == "unknown":
        reasons.append("unknown retention expiry")
    if expiry and expiry <= at:
        reasons.append("expired evidence")
    dependencies = set(evidence.support_evidence_ids or [])
    if evidence.parent_evidence_id:
        dependencies.add(evidence.parent_evidence_id)
    for parent in dependencies:
        result = eligible(evidence_by_id.get(parent), evidence_by_id, purpose=purpose, at=at, _seen=seen)
        reasons.extend(result.reasons)
    return Eligibility(not reasons, tuple(dict.fromkeys(reasons)))
