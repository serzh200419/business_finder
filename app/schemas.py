from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discovery: Literal["fixture", "csv", "licensed"] = "fixture"
    csv_path: str | None = None
    license_reference: str | None = None
    persistence_permitted: bool = False
    export_permitted: bool = False
    retention_policy: str = "unspecified"
    provider_namespace: str = Field(default="default", min_length=1, max_length=80)
    record_type: Literal["imported", "synthetic", "licensed"] = "imported"
    column_mapping: dict[str, str] = Field(default_factory=dict)
    fixture_scenario: Literal["basic", "reliability"] = "basic"
    fixture_failures_enabled: bool = True
    page_size: int = Field(default=50, ge=1, le=500)
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_base_seconds: float = Field(default=1, ge=0.01, le=60)
    retry_cap_seconds: float = Field(default=60, ge=1, le=3600)


class PriorityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    no_website_found: int = Field(default=35, ge=0, le=100)
    social_only: int = Field(default=25, ge=0, le=100)
    phone: int = Field(default=20, ge=0, le=100)
    niche: int = Field(default=20, ge=0, le=100)
    social: int = Field(default=10, ge=0, le=100)
    activity: int = Field(default=15, ge=0, le=100)
    activity_days: int = Field(default=90, ge=1, le=365)


class CampaignInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    niche: str = Field(min_length=1, max_length=200)
    city: str = Field(default="Yerevan", min_length=1, max_length=100)
    districts: list[str] = Field(default_factory=list)
    keywords: dict[str, list[str]] = Field(default_factory=dict)
    provider_config: ProviderConfig = Field(default_factory=ProviderConfig)
    request_budget: int = Field(default=20, ge=0, le=10000)
    candidate_budget: int = Field(default=100, ge=0, le=100000)
    query_limit: int = Field(default=100, ge=1, le=1000)
    city_variants: dict[str, list[str]] = Field(default_factory=dict)
    district_variants: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    review_priority: PriorityConfig = Field(default_factory=PriorityConfig)

    @field_validator("keywords", "city_variants")
    @classmethod
    def validate_languages(cls, value):
        if set(value) - {"hy", "ru", "en"}:
            raise ValueError("Supported languages: hy, ru, en")
        if any(not text.strip() for values in value.values() for text in values):
            raise ValueError("Keyword and city variants must not be blank")
        return value

    @field_validator("district_variants")
    @classmethod
    def validate_district_languages(cls, value):
        for variants in value.values():
            cls.validate_languages(variants)
        return value


class CampaignOutput(CampaignInput):
    model_config = ConfigDict(from_attributes=True)
    id: int
    requests_used: int


AssessmentStatus = Literal["not_checked", "website_found", "social_only", "no_website_found", "website_unavailable", "uncertain"]


class AccessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_access_permitted: bool = False
    persistence_permitted: bool = False
    export_permitted: bool = False
    retention_policy: str = "unspecified"
    policy_reference: str = ""


class EnrichmentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_provider: Literal["fixture", "live", "unavailable"] = "unavailable"
    fixture_dataset: Literal["empty", "assessment"] = "empty"
    fetch_mode: Literal["fixture", "live"] = "fixture"
    business_ids: list[int] = Field(default_factory=list)
    request_budget: int = Field(default=100, ge=0, le=10000)
    query_limit: int = Field(default=4, ge=1, le=10)
    optional_search_indices: list[int] = Field(default_factory=list)
    result_limit: int = Field(default=5, ge=1, le=10)
    candidate_page_limit: int = Field(default=6, ge=1, le=20)
    contact_page_limit: int = Field(default=1, ge=0, le=2)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_base_seconds: float = Field(default=1, ge=0.01, le=60)
    retry_cap_seconds: float = Field(default=60, ge=1, le=3600)
    page_policies: dict[str, AccessPolicy] = Field(default_factory=dict)
    known_matching_urls: dict[int, list[str]] = Field(default_factory=dict)
    known_matching_reference: str = ""
    name_aliases: dict[int, list[str]] = Field(default_factory=dict)
    city_variants: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_attestation(self):
        if self.known_matching_urls and not self.known_matching_reference.strip():
            raise ValueError("Known matching URLs require an explicit identity attestation reference")
        if any(index < 0 or index >= self.query_limit for index in self.optional_search_indices):
            raise ValueError("Optional search indices must lie within the configured query limit")
        return self


class ManualAssessmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: AssessmentStatus
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(min_length=1, max_length=100)
