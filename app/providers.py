"""Discovery/search adapter contracts, fixtures, CSV and optional Maps browser factory."""
import base64
import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
import httpx
from bs4 import BeautifulSoup
from app.schemas import ProviderConfig


class ProviderError(Exception):
    pass


class PermanentProviderError(ProviderError):
    pass


class SafeProviderError(PermanentProviderError):
    """Only static, code-selected diagnostics may be persisted by the runner."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RetryableProviderError(ProviderError):
    def __init__(self, message: str, retry_after: float | str | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(value: float | str | None, at: datetime) -> float:
    if value is None:
        return 0
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(str(value))
            return max(0, (date - at.astimezone(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


@dataclass(frozen=True)
class ProviderCapabilities:
    pagination: bool = True
    stable_external_ids: bool = True
    query_search: bool = False
    district_filter: bool = True
    max_page_size: int = 500


@dataclass(frozen=True)
class DiscoveryRequest:
    query_id: int
    text: str
    language: str
    niche: str
    city: str
    district: str | None
    allowed_districts: tuple[str, ...] = ()
    variants: tuple[dict, ...] = ()
    attempt: int = 1


@dataclass(frozen=True)
class DiscoveryResult:
    external_id: str
    name: str
    city: str
    district: str | None
    niche: str
    source_url: str
    website: str | None = None
    phone: str | None = None
    provider: str = "fixture"
    persistence_permitted: bool = False
    export_permitted: bool = False
    retention_policy: str = "unspecified"
    query_id: int | None = None
    address: str | None = None
    email: str | None = None
    name_aliases: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveryPage:
    results: list[DiscoveryResult]
    next_cursor: str | None
    rejected_rows: list[dict] = field(default_factory=list)
    dataset_fingerprint: str | None = None
    accounting: dict = field(default_factory=dict)


class DiscoveryProvider(Protocol):
    identity: str
    capabilities: ProviderCapabilities

    def discover_page(self, request: DiscoveryRequest, cursor: str | None, limit: int) -> DiscoveryPage: ...


@dataclass(frozen=True)
class WebSearchResult:
    url: str
    title: str
    provider: str
    persistence_permitted: bool = False
    export_permitted: bool = False
    retention_policy: str = "unspecified"
    snippet: str = ""
    query: str = ""


class SearchConfigurationError(PermanentProviderError):
    pass


class WebSearchProvider(Protocol):
    identity: str
    def search(self, query: str, limit: int) -> list[WebSearchResult]: ...


class FixtureWebSearchProvider:
    identity = "fixture_search"

    def __init__(self, dataset: str = "empty"):
        self.dataset = dataset

    def search(self, query: str, limit: int) -> list[WebSearchResult]:
        if self.dataset == "assessment":
            from app.assessment_fixtures import fixture_cases
            results = []
            for case in fixture_cases():
                if query.startswith(case["name"] + " ") or query == case["phone"]:
                    if case.get("search_error"):
                        raise PermanentProviderError("Synthetic unavailable search")
                    results.extend(WebSearchResult(url=url, title=case["name"], provider=self.identity, query=query,
                        snippet="Synthetic public business result", persistence_permitted=True,
                        export_permitted=False, retention_policy="synthetic fixture; indefinite")
                        for url in case.get("search_results", []))
            return results[:limit]
        return []


# No live adapter is installed by default. An explicitly configured deployment may register one.
web_search_providers: dict[str, WebSearchProvider] = {}


def get_web_search_provider(name: str, dataset: str, demo_mode: bool) -> WebSearchProvider:
    if name == "fixture":
        if not demo_mode:
            raise SearchConfigurationError("Fixture search requires DEMO_MODE=true")
        return FixtureWebSearchProvider(dataset)
    if name == "live" and "live" in web_search_providers:
        return web_search_providers["live"]
    raise SearchConfigurationError("No live web search adapter configured")


def slice_page(rows: list[DiscoveryResult], cursor: str | None, limit: int,
               fingerprint: str, errors: list[dict] | None = None) -> DiscoveryPage:
    offset = 0
    if cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor))
            offset = payload["offset"]
            if payload["fingerprint"] != fingerprint or not isinstance(offset, int) or offset < 0:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise PermanentProviderError("Invalid cursor or dataset changed; start a new job")
    end = offset + limit
    continuation = None if end >= len(rows) else base64.urlsafe_b64encode(
        json.dumps({"offset": end, "fingerprint": fingerprint}).encode()).decode()
    return DiscoveryPage(rows[offset:end], continuation, errors or [], fingerprint)


def matches_district(request: DiscoveryRequest, district: str) -> bool:
    if request.allowed_districts and district not in request.allowed_districts:
        return False
    variants = {variant["district"] for variant in request.variants} if request.variants else {request.district}
    return None in variants or district in variants


class FixtureProvider:
    identity = "fixture"
    capabilities = ProviderCapabilities()

    def __init__(self, config: ProviderConfig | None = None):
        self.config = config or ProviderConfig()

    def discover_page(self, request: DiscoveryRequest, cursor: str | None, limit: int) -> DiscoveryPage:
        filename = "reliability.json" if self.config.fixture_scenario == "reliability" else "businesses.json"
        content = (Path(__file__).parent / "fixtures" / filename).read_bytes()
        if self.config.fixture_scenario == "reliability" and self.config.fixture_failures_enabled:
            if "permanent" in request.text.casefold():
                raise PermanentProviderError("Synthetic permanent query failure")
            if request.language == "hy" and cursor is None and request.attempt == 1:
                raise RetryableProviderError("Synthetic transient failure", retry_after=0.01)
        rows = []
        for row in json.loads(content):
            if row["niche"].casefold() != request.niche.casefold() or row["city"].casefold() != request.city.casefold():
                continue
            if not matches_district(request, row["district"]):
                continue
            rows.append(DiscoveryResult(**row, query_id=request.query_id, persistence_permitted=True,
                                        export_permitted=True, retention_policy="synthetic fixture; indefinite"))
        return slice_page(rows, cursor, limit, hashlib.sha256(content).hexdigest())

    def discover(self, niche: str, city: str, districts: list[str]) -> list[DiscoveryResult]:
        # Compatibility convenience for callers of the first-stage local importer.
        return self.discover_page(DiscoveryRequest(0, niche, "en", niche, city, None,
                                  tuple(districts)), None, 500).results


class CSVProvider:
    capabilities = ProviderCapabilities()
    required = ("external_id", "name", "city", "niche", "source_url")
    columns = (*required, "district", "website", "phone", "address", "email")

    def __init__(self, config: ProviderConfig):
        if not config.csv_path or not config.license_reference or not config.persistence_permitted:
            raise ValueError("CSV requires a server-local path, license reference and persistence permission")
        if config.retention_policy == "unspecified":
            raise ValueError("CSV requires an explicit retention policy")
        if set(config.column_mapping) - set(self.columns):
            raise ValueError("Unknown canonical column in mapping")
        self.config = config
        self.identity = f"csv:{config.provider_namespace}"

    def validate(self) -> tuple[list[DiscoveryResult], dict]:
        try:
            content = Path(self.config.csv_path).read_bytes()
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), strict=True)
            headers = reader.fieldnames or []
            if len(set(headers)) != len(headers):
                raise PermanentProviderError("CSV headers must be unique")
            missing = [key for key in self.required if self.config.column_mapping.get(key, key) not in headers]
            if missing:
                raise PermanentProviderError(f"Missing required CSV columns: {', '.join(missing)}")
            rows: list[tuple[int, dict]] = []
            errors = []
            for number, raw in enumerate(reader, start=2):
                if None in raw or any(value is None for value in raw.values()):
                    errors.append({"row": number, "error": "Wrong number of columns"})
                    continue
                row = {key: (raw.get(self.config.column_mapping.get(key, key)) or "").strip() for key in self.columns}
                invalid = [key for key in self.required if not row[key]]
                if invalid or len(row["external_id"]) > 250 or len(row["name"]) > 250:
                    errors.append({"row": number, "error": f"Missing/invalid required fields: {', '.join(invalid)}"})
                    continue
                if urlsplit(row["source_url"]).scheme not in {"http", "https"} or not urlsplit(row["source_url"]).hostname:
                    errors.append({"row": number, "error": "source_url must be an absolute HTTP(S) provenance URL"})
                    continue
                if len(row["city"]) > 100 or len(row["district"]) > 100:
                    errors.append({"row": number, "error": "City/district exceeds 100 characters"})
                    continue
                rows.append((number, row))
        except (OSError, UnicodeError, csv.Error) as exc:
            raise PermanentProviderError(f"Cannot read valid UTF-8 CSV: {type(exc).__name__}") from exc
        by_id: dict[str, dict] = {}
        ambiguous: set[str] = set()
        for _, row in rows:
            if row["external_id"] in by_id and by_id[row["external_id"]] != row:
                ambiguous.add(row["external_id"])
            by_id[row["external_id"]] = row
        for number, row in rows:
            if row["external_id"] in ambiguous:
                errors.append({"row": number, "error": "Conflicting rows share external_id; identity requires review"})
        accepted_rows = [row for _, row in rows if row["external_id"] not in ambiguous]
        distinct = {row["external_id"]: row for row in accepted_rows}
        results = [DiscoveryResult(**{key: row[key] for key in (*self.required, "district")},
            website=row["website"] or None, phone=row["phone"] or None, provider=self.identity,
            address=row["address"] or None, email=row["email"] or None,
            persistence_permitted=self.config.persistence_permitted, export_permitted=self.config.export_permitted,
            retention_policy=f"{self.config.retention_policy}; license: {self.config.license_reference}")
            for row in distinct.values()]
        return results, {"accepted": len(accepted_rows), "rejected": len(errors), "errors": errors,
                         "distinct_candidates": len(results), "duplicate_rows": len(accepted_rows) - len(results),
                         "dataset_fingerprint": hashlib.sha256(content).hexdigest(), "provider": self.identity}

    def discover_page(self, request: DiscoveryRequest, cursor: str | None, limit: int) -> DiscoveryPage:
        from dataclasses import replace
        results, report = self.validate()
        rows = [replace(row, query_id=request.query_id) for row in results
                if row.niche.casefold() == request.niche.casefold() and row.city.casefold() == request.city.casefold()
                and matches_district(request, row.district)]
        return slice_page(rows, cursor, limit, report["dataset_fingerprint"], report["errors"])

    def discover(self, niche: str, city: str, districts: list[str]) -> list[DiscoveryResult]:
        return self.discover_page(DiscoveryRequest(0, niche, "en", niche, city, None, tuple(districts)), None, 500).results


licensed_providers: dict[str, DiscoveryProvider] = {}


def get_provider(config: ProviderConfig, demo_mode: bool) -> DiscoveryProvider:
    if config.discovery == "maps_browser":
        from app.maps_browser import MapsBrowserProvider
        return MapsBrowserProvider(config)
    if config.discovery == "fixture":
        if not demo_mode:
            raise ValueError("Fixture provider requires DEMO_MODE=true")
        return FixtureProvider(config)
    if config.discovery == "csv":
        return CSVProvider(config)
    if not config.license_reference or not config.persistence_permitted:
        raise ValueError("Licensed provider requires storage permission and license reference")
    if config.license_reference not in licensed_providers:
        raise ValueError("No adapter registered for this license reference")
    return licensed_providers[config.license_reference]


def extract_page_title(html: str) -> str | None:
    title = BeautifulSoup(html, "html.parser").title
    return title.get_text(strip=True) if title else None


def make_http_client(timeout: float) -> httpx.Client:
    # Adapters must validate destinations before fetching; no automatic website crawling.
    return httpx.Client(timeout=timeout, follow_redirects=False)
