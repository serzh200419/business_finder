from datetime import datetime, timezone
from typing import Any
from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    niche: Mapped[str] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(100), default="Yerevan")
    districts: Mapped[list[str]] = mapped_column(JSON, default=list)
    keywords: Mapped[dict[str, list[str]]] = mapped_column(JSON, default=dict)
    provider_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    request_budget: Mapped[int] = mapped_column(default=20)
    requests_used: Mapped[int] = mapped_column(default=0)
    candidate_budget: Mapped[int] = mapped_column(default=100)
    query_limit: Mapped[int] = mapped_column(default=100)
    city_variants: Mapped[dict] = mapped_column(JSON, default=dict)
    district_variants: Mapped[dict] = mapped_column(JSON, default=dict)
    review_priority: Mapped[dict] = mapped_column(JSON, default=dict)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SearchQuery(Base):
    __tablename__ = "search_queries"
    __table_args__ = (UniqueConstraint("campaign_id", "plan_key", name="uq_query_plan"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(20))
    district: Mapped[str | None] = mapped_column(String(100))
    plan_key: Mapped[str | None] = mapped_column(String(64))
    variants: Mapped[list[dict]] = mapped_column(JSON, default=list)


class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(250))
    city: Mapped[str] = mapped_column(String(100))
    district: Mapped[str | None] = mapped_column(String(100))
    website: Mapped[str | None] = mapped_column(Text)
    identity_key: Mapped[str] = mapped_column(String(500), unique=True)
    review_required: Mapped[bool] = mapped_column(default=False)
    review_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    address: Mapped[str | None] = mapped_column(Text)
    name_aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    suppressed: Mapped[bool] = mapped_column(default=False)
    suppression_reason: Mapped[str | None] = mapped_column(Text)


class BusinessSource(Base):
    __tablename__ = "business_sources"
    __table_args__ = (UniqueConstraint("business_id", "provider", "external_id"),
                      UniqueConstraint("provider", "external_id", name="uq_source_identity"))
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(String(250))
    source_url: Mapped[str] = mapped_column(Text)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(String(250))
    raw_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    validation_status: Mapped[str] = mapped_column(String(30), default="not_checked")
    validation_details: Mapped[dict] = mapped_column(JSON, default=dict)
    verification_status: Mapped[str] = mapped_column(String(30), default="source_claim")
    source_url: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    contact_key: Mapped[str | None] = mapped_column(String(64), unique=True)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    source_url: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(100))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    match_reasons: Mapped[list[str]] = mapped_column(JSON)
    retention_policy: Mapped[str] = mapped_column(Text)
    persistence_permitted: Mapped[bool]
    export_permitted: Mapped[bool]
    query_id: Mapped[int | None] = mapped_column(ForeignKey("search_queries.id", ondelete="CASCADE"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("business_sources.id", ondelete="CASCADE"))
    evidence_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    parent_evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id", ondelete="SET NULL"))
    evidence_type: Mapped[str] = mapped_column(String(30), default="discovery")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    support_evidence_ids: Mapped[list[int]] = mapped_column(JSON, default=list)


class WebsiteAssessment(Base):
    __tablename__ = "website_assessments"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(50))
    reasons: Mapped[list[str]] = mapped_column(JSON)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate_urls: Mapped[list[dict]] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(30), default="discovery")
    assessment_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    corrected_by: Mapped[str | None] = mapped_column(String(100))
    previous_status: Mapped[str | None] = mapped_column(String(50))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    provider_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    kind: Mapped[str] = mapped_column(String(30), default="discovery")
    request_budget: Mapped[int] = mapped_column(default=100)
    requests_used: Mapped[int] = mapped_column(default=0)


class JobBusiness(Base):
    __tablename__ = "job_businesses"
    __table_args__ = (UniqueConstraint("job_id", "business_id", name="uq_job_business"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    seed_evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(default=0)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_kind: Mapped[str | None] = mapped_column(String(30))


class JobQuery(Base):
    __tablename__ = "job_queries"
    __table_args__ = (UniqueConstraint("job_id", "query_id", name="uq_job_query"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    query_id: Mapped[int] = mapped_column(ForeignKey("search_queries.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    cursor: Mapped[str | None] = mapped_column(Text)
    pages_committed: Mapped[int] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_kind: Mapped[str | None] = mapped_column(String(30))
    error_message: Mapped[str | None] = mapped_column(Text)
    skipped_unlicensed: Mapped[int] = mapped_column(default=0)
    rejected_rows: Mapped[list[dict]] = mapped_column(JSON, default=list)
    dataset_fingerprint: Mapped[str | None] = mapped_column(String(64))


class JobCandidate(Base):
    __tablename__ = "job_candidates"
    __table_args__ = (UniqueConstraint("job_id", "source_id", name="uq_job_candidate"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("business_sources.id", ondelete="CASCADE"))
    newly_created: Mapped[bool]


class OutreachRecord(Base):
    """Manual history only; this application never sends messages."""
    __tablename__ = "outreach_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    note: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(30), default="new")
    operator_label: Mapped[str] = mapped_column(String(100), default="local operator")
    suppressed: Mapped[bool] = mapped_column(default=False)
    suppression_reason: Mapped[str | None] = mapped_column(Text)
    previous_state: Mapped[dict] = mapped_column(JSON, default=dict)
