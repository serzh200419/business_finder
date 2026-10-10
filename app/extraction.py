"""Extract in memory first; only attribute contacts after transparent identity matching."""
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from app.contacts import classify_url, normalize_contact, normalize_url
from app.planner import normalize


@dataclass
class PageFacts:
    names: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    localities: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    contact_pages: list[str] = field(default_factory=list)
    social_profiles: list[str] = field(default_factory=list)
    related_urls: list[str] = field(default_factory=list)
    structured_business: bool = False
    ambiguous_entities: bool = False


BUSINESS_TYPES = {"LocalBusiness", "Dentist", "MedicalBusiness", "Restaurant", "CafeOrCoffeeShop",
                  "Organization", "Store", "BeautySalon", "ProfessionalService", "HealthAndBeautyBusiness"}


def extract_facts(html: str, page_url: str) -> PageFacts:
    soup = BeautifulSoup(html, "html.parser")
    facts = PageFacts()
    for tag in soup.select("title,h1")[:5]:
        facts.names.append(tag.get_text(" ", strip=True)[:250])
    nodes = []
    def visit(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, list):
            for child in value[:100]:
                visit(child, depth + 1)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if isinstance(types, str):
                types = [types.rsplit("/", 1)[-1]]
            if any(kind in BUSINESS_TYPES for kind in types):
                nodes.append(value)
            if "@graph" in value:
                visit(value["@graph"], depth + 1)
    for script in soup.select('script[type="application/ld+json"]')[:20]:
        try:
            visit(json.loads(script.string or script.get_text()))
        except (ValueError, RecursionError):
            pass
    # Several unrelated entities make page-level attribution ambiguous; do not combine them.
    facts.ambiguous_entities = len(nodes) > 1
    if len(nodes) == 1:
        node = nodes[0]
        facts.structured_business = True
        if isinstance(node.get("name"), str):
            facts.names.append(node["name"][:250])
        for source, target in (("telephone", facts.phones), ("email", facts.emails), ("url", facts.urls)):
            values = node.get(source, [])
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                target.extend(value[:2000] for value in values[:20] if isinstance(value, str))
                if source == "url":
                    facts.related_urls.extend(value[:2000] for value in values[:20] if isinstance(value, str))
        address = node.get("address", {})
        if isinstance(address, str):
            facts.addresses.append(address[:1000])
        elif isinstance(address, dict):
            for key in ("streetAddress", "addressLocality", "addressRegion"):
                if isinstance(address.get(key), str):
                    facts.addresses.append(address[key][:1000])
            facts.localities.extend(str(address[key])[:1000] for key in ("addressLocality", "addressRegion") if key in address)
        same_as = node.get("sameAs", [])
        if isinstance(same_as, str):
            same_as = [same_as]
        if isinstance(same_as, list):
            facts.urls.extend(value[:2000] for value in same_as[:20] if isinstance(value, str))
            facts.social_profiles.extend(value[:2000] for value in same_as[:20]
                                          if isinstance(value, str) and classify_url(value) == "social")
            facts.related_urls.extend(value[:2000] for value in same_as[:20] if isinstance(value, str))
    for link in soup.select("a[href]")[:500]:
        href = link.get("href", "")[:2000]
        if href.lower().startswith("tel:"):
            facts.phones.append(href[4:])
        elif href.lower().startswith("mailto:"):
            facts.emails.append(href[7:])
        else:
            absolute = normalize_url(urljoin(page_url, href))
            if absolute:
                facts.urls.append(absolute)
                if "me" in link.get("rel", []):
                    facts.related_urls.append(absolute)
                    if classify_url(absolute) == "social":
                        facts.social_profiles.append(absolute)
                if urlsplit(absolute).netloc == urlsplit(page_url).netloc and re.search(
                        r"(?:contact|about|կապ|մասին|контакт|о-нас)", link.get_text(" ", strip=True) + " " + urlsplit(absolute).path, re.I):
                    facts.contact_pages.append(absolute)
    for attr in ("names", "phones", "emails", "addresses", "localities", "urls", "contact_pages", "social_profiles", "related_urls"):
        setattr(facts, attr, list(dict.fromkeys(getattr(facts, attr))))
    return facts


def identity_match(snapshot: dict, facts: PageFacts) -> tuple[bool, list[str]]:
    if facts.ambiguous_entities:
        return False, ["Multiple business entities on page; contact attribution is ambiguous"]
    expected = set(snapshot.get("phones", []))
    actual = {phone.value for raw in facts.phones if (phone := normalize_contact("phone", raw)).valid}
    phone_match = bool(expected & actual)
    if expected and actual and not phone_match:
        return False, ["Public page phone conflicts with configured business phones"]
    name_match = bool({normalize(value).casefold() for value in [snapshot["name"], *snapshot.get("aliases", [])]} &
                      {normalize(value).casefold() for value in facts.names})
    address = snapshot.get("address")
    address_match = bool(address and normalize(address).casefold() in {normalize(value).casefold() for value in facts.addresses})
    city_match = bool({normalize(value).casefold() for value in [snapshot["city"], *snapshot.get("city_variants", [])]} &
                      {normalize(value).casefold() for value in facts.localities})
    if phone_match and not snapshot.get("shared_phone"):
        return True, ["Exact normalized public business phone match"]
    if phone_match and snapshot.get("shared_phone") and name_match and address_match:
        return True, ["Shared phone plus exact configured name and branch street address"]
    if name_match and address_match and city_match and facts.structured_business:
        return True, ["Exact configured name + street address + city in a single business JSON-LD entity"]
    return False, ["Identity unverified: name similarity or shared phone alone is insufficient"]


def relevant_contacts(facts: PageFacts, page_url: str):
    values = [("phone", value) for value in facts.phones] + [("email", value) for value in facts.emails]
    if classify_url(page_url) == "social":
        values += [("social", page_url)]
    else:
        values += [("website", page_url), ("domain", urlsplit(page_url).hostname or "")]
    values += [("social", value) for value in facts.social_profiles]
    return list(dict.fromkeys(values))
