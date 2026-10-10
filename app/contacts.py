"""Syntactic normalization does not verify ownership, deliverability or messaging support."""
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit
import phonenumbers
from app.planner import digest

SOCIAL_HOSTS = {"instagram.com", "facebook.com", "vk.com", "linkedin.com", "t.me",
                "youtube.com", "twitter.com", "x.com", "tiktok.com"}


@dataclass(frozen=True)
class NormalizedContact:
    kind: str
    raw: str
    value: str | None
    valid: bool
    details: dict


def normalize_domain(raw: str) -> str | None:
    try:
        domain = raw.strip().rstrip(".").encode("idna").decode("ascii").lower()
        if len(domain) > 253 or "." not in domain or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split(".")):
            return None
        return domain
    except (UnicodeError, ValueError):
        return None


def normalize_url(raw: str) -> str | None:
    try:
        parts = urlsplit(raw.strip())
        if parts.scheme.lower() not in {"http", "https"} or parts.username or parts.password:
            return None
        domain = normalize_domain(parts.hostname or "")
        if not domain or parts.port not in {None, 80, 443}:
            return None
        authority = domain
        if parts.port and parts.port != (443 if parts.scheme.lower() == "https" else 80):
            authority += f":{parts.port}"
        # Query strings may identify hosted profiles; preserve them, remove only fragments.
        return urlunsplit((parts.scheme.lower(), authority, parts.path or "/", parts.query, ""))
    except ValueError:
        return None


def classify_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    groups = {"social": SOCIAL_HOSTS, "directory": {"spyur.am", "yell.am", "yellowpages.com"},
              "marketplace": {"list.am", "amazon.com", "etsy.com"},
              "booking": {"booking.com", "tripadvisor.com", "fresha.com"},
              "link_hub": {"linktr.ee", "bio.site", "beacons.ai"}}
    for kind, domains in groups.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return kind
    return "website"  # Includes hosted subdomains; identity still needs verification.


def normalize_contact(kind: str, raw: str) -> NormalizedContact:
    value = raw.strip()
    if kind == "phone":
        value = unquote(value[4:] if value.lower().startswith("tel:") else value).split(";", 1)[0]
        try:
            number = phonenumbers.parse(value, "AM")
            valid = phonenumbers.is_valid_number(number)
            details = {"region": phonenumbers.region_code_for_number(number),
                       "possible": phonenumbers.is_possible_number(number), "extension": number.extension}
            normalized = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164) if valid else None
            return NormalizedContact(kind, raw, normalized, valid, details)
        except phonenumbers.NumberParseException:
            return NormalizedContact(kind, raw, None, False, {"error": "Unparseable phone"})
    if kind == "email":
        if value.lower().startswith("mailto:"):
            value = unquote(value[7:].split("?", 1)[0])
        if value.count("@") == 1:
            local, domain = value.rsplit("@", 1)
            normalized_domain = normalize_domain(domain)
            valid = bool(normalized_domain and len(local) <= 64 and
                         re.fullmatch(r"[^\s@<>(),;:\"\\]+", local) and
                         not local.startswith(".") and not local.endswith(".") and ".." not in local)
            return NormalizedContact(kind, raw, f"{local}@{normalized_domain}" if valid else None, valid,
                                     {"deliverability": "not_checked", "local_part_preserved": True})
    if kind == "domain":
        normalized = normalize_domain(value)
        return NormalizedContact(kind, raw, normalized, normalized is not None, {})
    if kind in {"website", "social"}:
        normalized = normalize_url(value)
        valid = bool(normalized and (kind != "social" or classify_url(normalized) == "social"))
        return NormalizedContact(kind, raw, normalized if valid else None, valid, {})
    return NormalizedContact(kind, raw, None, False, {"error": "Unsupported or invalid contact syntax"})


def persist_contact(db, business_id: int, evidence, kind: str, raw: str, *, matched: bool = False):
    from app.models import Contact
    from app.services import insert_once
    if not evidence.persistence_permitted:
        return None
    normalized = normalize_contact(kind, raw)
    key = digest("contact", str(business_id), str(evidence.id), kind, normalized.value or raw)
    return insert_once(db, Contact, {"business_id": business_id, "evidence_id": evidence.id,
        "kind": kind, "value": (normalized.value or raw)[:250], "raw_value": raw,
        "normalized_value": normalized.value, "validation_status": "valid" if normalized.valid else "invalid",
        "validation_details": normalized.details, "verification_status": "business_matched" if matched else "source_claim",
        "source_url": evidence.source_url, "observed_at": evidence.observed_at, "contact_key": key}, ["contact_key"])
