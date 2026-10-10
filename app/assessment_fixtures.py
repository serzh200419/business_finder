"""Synthetic search and HTTP fixtures. MockTransport never falls through to a network."""
import json
from pathlib import Path
import httpx
from app.schemas import AccessPolicy


def fixture_cases() -> list[dict]:
    return json.loads((Path(__file__).parent / "fixtures/assessment.json").read_text(encoding="utf-8"))


def fixture_html(case: dict, *, contact_page: bool = False) -> str:
    name, phone, address = case["name"], case["phone"], case["address"]
    if case["id"] == "similar":
        name, phone, address = "Synthetic Similar Dental Other", "+37410999999", "99 Other Street"
    node = {"@context": "https://schema.org", "@type": "Dentist", "name": name,
            "telephone": phone, "email": "Public.Office+Demo@EXAMPLE.COM",
            "address": {"@type": "PostalAddress", "streetAddress": address,
                        "addressLocality": case["city"], "addressRegion": case["district"]}}
    return ('<!doctype html><title>' + name + '</title><h1>' + name + '</h1>' +
            '<script type="application/ld+json">' + json.dumps(node, ensure_ascii=False) + '</script>' +
            '<a href="tel:' + phone + '">Synthetic public phone</a>' +
            '<a href="mailto:Public.Office+Demo@EXAMPLE.COM">Synthetic email</a>' +
            ('' if contact_page else '<a href="/contact">Contact</a>'))


def fixture_pages() -> dict[str, tuple[int, dict, str]]:
    pages = {}
    for case in fixture_cases():
        for url in case["search_results"]:
            if case["id"] == "unavailable":
                pages[url] = (404, {}, "")
            elif case["id"] == "redirect":
                pages[url] = (302, {"location": "https://new.synthetic.example/"}, "")
                pages["https://new.synthetic.example/"] = (200, {}, fixture_html(case))
                pages["https://new.synthetic.example/contact"] = (200, {}, fixture_html(case, contact_page=True))
            else:
                pages[url] = (200, {}, fixture_html(case))
                if case["id"] != "social":
                    pages[str(httpx.URL(url).copy_with(path="/contact"))] = (200, {}, fixture_html(case, contact_page=True))
    return pages


def fixture_policies() -> dict[str, AccessPolicy]:
    return {httpx.URL(url).host: AccessPolicy(public_access_permitted=True, persistence_permitted=True,
            export_permitted=False, retention_policy="synthetic fixture; indefinite",
            policy_reference="owned synthetic HTML fixture") for url in fixture_pages()}


def fixture_transport() -> httpx.MockTransport:
    pages = fixture_pages()
    def handler(request):
        status, headers, html = pages.get(str(request.url), (404, {}, ""))
        return httpx.Response(status, headers={"content-type": "text/html; charset=utf-8", **headers},
                              stream=httpx.ByteStream(html.encode("utf-8")))
    return httpx.MockTransport(handler)


def seed_fixture_campaign(db):
    from app.config import get_settings
    from app.contacts import persist_contact
    from app.models import Business, BusinessSource, Campaign, Evidence
    from app.planner import digest
    from app.schemas import EnrichmentOptions
    from app.services import insert_once
    if not get_settings().demo_mode:
        raise ValueError("Synthetic seeding requires DEMO_MODE=true")
    campaign = Campaign(niche="synthetic assessment demo", city="Yerevan", request_budget=1000,
                         keywords={"en": ["synthetic dentist"]}, provider_config={"discovery": "fixture"})
    db.add(campaign)
    db.flush()
    known = {}
    outcomes = []
    for case in fixture_cases():
        business = insert_once(db, Business, {"name": case["name"], "city": case["city"],
            "district": case["district"], "address": case["address"], "website": case["listed_url"],
            "identity_key": digest("assessment_fixture", case["id"])}, ["identity_key"])
        source = insert_once(db, BusinessSource, {"business_id": business.id, "provider": "assessment_fixture",
            "external_id": case["id"], "source_url": f"https://example.invalid/synthetic/{case['id']}"}, ["provider", "external_id"])
        evidence = insert_once(db, Evidence, {"business_id": business.id, "campaign_id": campaign.id,
            "source_id": source.id, "source_url": source.source_url, "provider": source.provider,
            "match_reasons": ["Synthetic seed only; every contact and business is fictional"],
            "retention_policy": "synthetic fixture; indefinite", "persistence_permitted": True,
            "export_permitted": False, "evidence_type": "discovery",
            "details": {"identity": {"name": case["name"], "city": case["city"], "district": case["district"],
                         "address": case["address"], "website": case["listed_url"], "niche": campaign.niche}},
            "evidence_key": digest("assessment_seed", str(campaign.id), case["id"])}, ["evidence_key"])
        persist_contact(db, business.id, evidence, "phone", case["phone"])
        if case.get("known_matching"):
            known[business.id] = [case["listed_url"]]
        outcomes.append({"business_id": business.id, "name": case["name"], "expected": case["expected"]})
    db.commit()
    return campaign, EnrichmentOptions(search_provider="fixture", fixture_dataset="assessment", query_limit=2, request_budget=200,
        known_matching_urls=known, known_matching_reference="Synthetic fixture identity attestation only",
        contact_page_limit=1), outcomes
