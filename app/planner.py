"""Deterministic configured expansion; no translation or provider calls."""
import hashlib
import unicodedata
from dataclasses import asdict, dataclass
from app.schemas import CampaignInput


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def digest(*parts: str) -> str:
    # Length-safe encoding prevents collisions from embedded separators.
    import json
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass
class PlannedQuery:
    text: str
    language: str
    district: str | None
    plan_key: str
    variants: list[dict]


def plan_campaign(campaign: CampaignInput) -> dict:
    keywords = campaign.keywords or {"en": [campaign.niche]}
    planned: dict[str, PlannedQuery] = {}
    truncated = False
    for language in ("hy", "ru", "en"):
        for keyword in keywords.get(language, []):
            for city in campaign.city_variants.get(language) or [campaign.city]:
                for district in [None, *campaign.districts]:
                    aliases = [""] if district is None else (
                        campaign.district_variants.get(district, {}).get(language) or [district])
                    for alias in aliases:
                        text = normalize(f"{keyword} {city} {alias}")
                        key = digest(text.casefold())
                        metadata = {"language": language, "district": district,
                                    "keyword": normalize(keyword), "city_variant": normalize(city),
                                    "district_variant": normalize(alias)}
                        if key in planned:
                            if metadata not in planned[key].variants:
                                planned[key].variants.append(metadata)
                        elif len(planned) < campaign.query_limit:
                            planned[key] = PlannedQuery(text, language, district, key, [metadata])
                        else:
                            truncated = True
    return {"queries": [asdict(query) for query in planned.values()], "truncated": truncated,
            "query_limit": campaign.query_limit, "candidate_budget": campaign.candidate_budget}
