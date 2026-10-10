"""Local manual review, evidence retention and support dependencies."""
from alembic import op
import sqlalchemy as sa

revision = "8c3d4e5f6a7b"
down_revision = "7b2c3d4e5f6a"
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table("campaigns") as b:
        b.add_column(sa.Column("review_priority", sa.JSON(), nullable=False, server_default="{}"))
    with op.batch_alter_table("businesses") as b:
        b.add_column(sa.Column("suppressed", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("suppression_reason", sa.Text()))
    with op.batch_alter_table("evidence") as b:
        for name in ("expires_at", "revoked_at", "cleaned_at"):
            b.add_column(sa.Column(name, sa.DateTime(timezone=True)))
        b.add_column(sa.Column("support_evidence_ids", sa.JSON(), nullable=False, server_default="[]"))
    with op.batch_alter_table("outreach_records") as b:
        b.add_column(sa.Column("campaign_id", sa.Integer()))
        b.create_foreign_key("fk_outreach_campaign", "campaigns", ["campaign_id"], ["id"], ondelete="SET NULL")
        b.add_column(sa.Column("status", sa.String(30), nullable=False, server_default="new"))
        b.add_column(sa.Column("operator_label", sa.String(100), nullable=False, server_default="local operator"))
        b.add_column(sa.Column("suppressed", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("suppression_reason", sa.Text()))
        b.add_column(sa.Column("previous_state", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    with op.batch_alter_table("campaigns") as b:
        b.drop_column("review_priority")
    with op.batch_alter_table("outreach_records") as b:
        b.drop_constraint("fk_outreach_campaign", type_="foreignkey")
        for name in ("campaign_id", "status", "operator_label", "suppressed", "suppression_reason", "previous_state"):
            b.drop_column(name)
    with op.batch_alter_table("evidence") as b:
        for name in ("expires_at", "revoked_at", "cleaned_at", "support_evidence_ids"):
            b.drop_column(name)
    with op.batch_alter_table("businesses") as b:
        b.drop_column("suppressed")
        b.drop_column("suppression_reason")
