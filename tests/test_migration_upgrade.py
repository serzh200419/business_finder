import io
from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from app.config import get_settings


def test_legacy_ambiguity_requires_review(migrated_db):
    engine, config = migrated_db
    command.downgrade(config, "d17c23448d86")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO businesses (id,name,city,identity_key) VALUES (1,'Same','Yerevan','legacy-1'),(2,'Same','Yerevan','legacy-2')"))
        connection.execute(text("INSERT INTO business_sources (business_id,provider,external_id,source_url) VALUES (1,'fixture','one','https://example.invalid/1'),(2,'fixture','one','https://example.invalid/2')"))
    with pytest.raises(RuntimeError, match="Ambiguous legacy"):
        command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(text("UPDATE business_sources SET external_id='two' WHERE business_id=2"))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT review_required, review_reasons FROM businesses")).all()
        assert len(rows) == 2 and all(row[0] and "Legacy" in row[1] for row in rows)


def test_postgresql_offline_sql(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://leads:leads@localhost/leads")
    get_settings.cache_clear()
    try:
        buffer = io.StringIO()
        config = Config("alembic.ini", output_buffer=buffer)
        command.upgrade(config, "head", sql=True)
        sql = buffer.getvalue()
        assert "TIMESTAMP WITH TIME ZONE" in sql
        assert "uq_source_identity UNIQUE (provider, external_id)" in sql
        assert "CREATE TABLE job_queries" in sql and "uq_evidence_key" in sql
    finally:
        get_settings.cache_clear()
