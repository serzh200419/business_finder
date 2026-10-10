"""Search plans, source identity, fenced leases and durable query checkpoints."""
from alembic import op
import sqlalchemy as sa

revision = "6a1b2c3d4e5f"
down_revision = "d17c23448d86"
branch_labels = depends_on = None


def upgrade():
    # Do not silently resolve conflicting legacy provider IDs.
    if not op.get_context().as_sql:
        duplicates = op.get_bind().execute(sa.text(
            "SELECT provider, external_id FROM business_sources GROUP BY provider, external_id HAVING COUNT(*) > 1"
        )).first()
        if duplicates:
            raise RuntimeError("Ambiguous legacy provider IDs: resolve business_sources before migration")
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(sa.Column("candidate_budget", sa.Integer(), nullable=False, server_default="100"))
        batch.add_column(sa.Column("query_limit", sa.Integer(), nullable=False, server_default="100"))
        batch.add_column(sa.Column("city_variants", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("district_variants", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("lease_token", sa.String(36)))
        batch.add_column(sa.Column("lease_until", sa.DateTime(timezone=True)))
    with op.batch_alter_table("search_queries") as batch:
        batch.add_column(sa.Column("district", sa.String(100)))
        batch.add_column(sa.Column("plan_key", sa.String(64)))
        batch.add_column(sa.Column("variants", sa.JSON(), nullable=False, server_default="[]"))
        batch.create_unique_constraint("uq_query_plan", ["campaign_id", "plan_key"])
    with op.batch_alter_table("businesses") as batch:
        batch.add_column(sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("review_reasons", sa.JSON(), nullable=False, server_default="[]"))
    op.execute(sa.text("UPDATE businesses SET review_required = true, review_reasons = '[\"Legacy name-based identity requires review\"]'"))
    with op.batch_alter_table("business_sources") as batch:
        batch.create_unique_constraint("uq_source_identity", ["provider", "external_id"])
    with op.batch_alter_table("evidence") as batch:
        batch.add_column(sa.Column("query_id", sa.Integer()))
        batch.add_column(sa.Column("source_id", sa.Integer()))
        batch.add_column(sa.Column("evidence_key", sa.String(64)))
        batch.create_foreign_key("fk_evidence_query", "search_queries", ["query_id"], ["id"], ondelete="CASCADE")
        batch.create_foreign_key("fk_evidence_source", "business_sources", ["source_id"], ["id"], ondelete="CASCADE")
        batch.create_unique_constraint("uq_evidence_key", ["evidence_key"])
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("provider_snapshot", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute(sa.text("UPDATE jobs SET status = 'failed' WHERE status IN ('pending', 'running', 'partial')"))
    op.create_table("job_queries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("query_id", sa.Integer(), sa.ForeignKey("search_queries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("cursor", sa.Text()),
        sa.Column("pages_committed", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("error_kind", sa.String(30)), sa.Column("error_message", sa.Text()),
        sa.Column("skipped_unlicensed", sa.Integer(), nullable=False),
        sa.Column("rejected_rows", sa.JSON(), nullable=False),
        sa.Column("dataset_fingerprint", sa.String(64)),
        sa.UniqueConstraint("job_id", "query_id", name="uq_job_query"))
    op.create_table("job_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("business_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("newly_created", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("job_id", "source_id", name="uq_job_candidate"))


def downgrade():
    op.drop_table("job_candidates")
    op.drop_table("job_queries")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("provider_snapshot")
        batch.drop_column("cancel_requested")
    with op.batch_alter_table("evidence") as batch:
        batch.drop_constraint("fk_evidence_query", type_="foreignkey")
        batch.drop_constraint("fk_evidence_source", type_="foreignkey")
        batch.drop_constraint("uq_evidence_key", type_="unique")
        for name in ("query_id", "source_id", "evidence_key"):
            batch.drop_column(name)
    with op.batch_alter_table("business_sources") as batch:
        batch.drop_constraint("uq_source_identity", type_="unique")
    with op.batch_alter_table("businesses") as batch:
        batch.drop_column("review_required")
        batch.drop_column("review_reasons")
    with op.batch_alter_table("search_queries") as batch:
        batch.drop_constraint("uq_query_plan", type_="unique")
        for name in ("district", "plan_key", "variants"):
            batch.drop_column(name)
    with op.batch_alter_table("campaigns") as batch:
        for name in ("candidate_budget", "query_limit", "city_variants", "district_variants", "lease_token", "lease_until"):
            batch.drop_column(name)
