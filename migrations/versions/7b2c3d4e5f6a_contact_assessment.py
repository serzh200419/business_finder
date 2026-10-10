"""Raw contacts, assessment evidence/audit and per-business job checkpoints."""
from alembic import op
import sqlalchemy as sa

revision = "7b2c3d4e5f6a"
down_revision = "6a1b2c3d4e5f"
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table("businesses") as batch:
        batch.add_column(sa.Column("address", sa.Text()))
        batch.add_column(sa.Column("name_aliases", sa.JSON(), nullable=False, server_default="[]"))
    with op.batch_alter_table("contacts") as batch:
        for name in ("raw_value", "normalized_value", "source_url"):
            batch.add_column(sa.Column(name, sa.Text()))
        batch.add_column(sa.Column("validation_status", sa.String(30), nullable=False, server_default="not_checked"))
        batch.add_column(sa.Column("validation_details", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("verification_status", sa.String(30), nullable=False, server_default="source_claim"))
        batch.add_column(sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()))
        batch.add_column(sa.Column("contact_key", sa.String(64)))
        batch.create_unique_constraint("uq_contact_key", ["contact_key"])
    op.execute(sa.text("UPDATE contacts SET raw_value = value"))
    with op.batch_alter_table("evidence") as batch:
        batch.add_column(sa.Column("parent_evidence_id", sa.Integer()))
        batch.create_foreign_key("fk_evidence_parent", "evidence", ["parent_evidence_id"], ["id"], ondelete="SET NULL")
        batch.add_column(sa.Column("evidence_type", sa.String(30), nullable=False, server_default="discovery"))
        batch.add_column(sa.Column("details", sa.JSON(), nullable=False, server_default="{}"))
    with op.batch_alter_table("website_assessments") as batch:
        batch.add_column(sa.Column("checked_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("search_coverage", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("candidate_urls", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("origin", sa.String(30), nullable=False, server_default="discovery"))
        batch.add_column(sa.Column("assessment_key", sa.String(64)))
        batch.create_unique_constraint("uq_assessment_key", ["assessment_key"])
        batch.add_column(sa.Column("corrected_by", sa.String(100)))
        batch.add_column(sa.Column("previous_status", sa.String(50)))
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("kind", sa.String(30), nullable=False, server_default="discovery"))
        batch.add_column(sa.Column("request_budget", sa.Integer(), nullable=False, server_default="100"))
        batch.add_column(sa.Column("requests_used", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("job_businesses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_id", sa.Integer(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seed_evidence_id", sa.Integer(), sa.ForeignKey("evidence.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)), sa.Column("error_kind", sa.String(30)),
        sa.UniqueConstraint("job_id", "business_id", name="uq_job_business"))


def downgrade():
    op.drop_table("job_businesses")
    with op.batch_alter_table("jobs") as batch:
        for name in ("kind", "request_budget", "requests_used"):
            batch.drop_column(name)
    with op.batch_alter_table("website_assessments") as batch:
        batch.drop_constraint("uq_assessment_key", type_="unique")
        for name in ("checked_at", "search_coverage", "candidate_urls", "origin", "assessment_key", "corrected_by", "previous_status"):
            batch.drop_column(name)
    with op.batch_alter_table("evidence") as batch:
        batch.drop_constraint("fk_evidence_parent", type_="foreignkey")
        for name in ("parent_evidence_id", "evidence_type", "details"):
            batch.drop_column(name)
    with op.batch_alter_table("contacts") as batch:
        batch.drop_constraint("uq_contact_key", type_="unique")
        for name in ("raw_value", "normalized_value", "source_url", "validation_status", "validation_details", "verification_status", "observed_at", "contact_key"):
            batch.drop_column(name)
    with op.batch_alter_table("businesses") as batch:
        batch.drop_column("address")
        batch.drop_column("name_aliases")
