import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import Session
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import Business, Campaign, Contact, Evidence
from app.providers import DiscoveryResult, DiscoveryPage, ProviderCapabilities, CSVProvider, extract_page_title
from app.schemas import ProviderConfig


def test_migration_roundtrip(migrated_db):
    engine, config = migrated_db
    assert len(inspect(engine).get_table_names()) == 13  # original nine + three progress models + revision
    command.check(config)
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    command.upgrade(config, "head")


def test_campaign_fixture_flow(client, migrated_db):
    assert client.get("/health").status_code == 200
    response = client.post("/api/campaigns", json={"niche": "dentist", "keywords": {"hy": ["Õ¡Õ¿Õ¡Õ´Õ¶Õ¡Õ¢Õ¸Ö‚ÕµÕª"], "en": ["dentist"]}, "request_budget": 1})
    assert response.status_code == 201
    campaign_id = response.json()["id"]
    assert client.get(f"/api/campaigns/{campaign_id}").json()["city"] == "Yerevan"
    assert len(client.get("/api/campaigns").json()) == 1
    response = client.post(f"/api/campaigns/{campaign_id}/discover")
    assert response.json()["result"]["persisted"] == 2
    leads = client.get(f"/api/campaigns/{campaign_id}/leads").json()
    assert {row["assessment"] for row in leads} == {"website_listed", "candidate_for_review"}
    assert all(row["provider"] == "fixture" for row in leads)
    assert client.post(f"/api/campaigns/{campaign_id}/discover").status_code == 422
    assert client.put(f"/api/campaigns/{campaign_id}", json={"niche": "cafe"}).json()["requests_used"] == 1
    assert "Yerevan Lead Discovery" in client.get("/").text
    assert client.delete(f"/api/campaigns/{campaign_id}").status_code == 204
    assert client.get(f"/api/campaigns/{campaign_id}").status_code == 404
    with Session(migrated_db[0]) as db:
        assert not list(db.scalars(select(Evidence)))
        assert not list(db.scalars(select(Contact)))


def test_unlicensed_results_never_persist(client, migrated_db, monkeypatch):
    class Restricted:
        identity = "fixture"
        capabilities = ProviderCapabilities()
        def discover_page(self, request, cursor, limit):
            return DiscoveryPage([DiscoveryResult("restricted", "Restricted Name", "Yerevan", "Kentron", "dentist", "https://example.invalid", persistence_permitted=False, query_id=request.query_id)], None)
    monkeypatch.setattr("app.services.get_provider", lambda *args: Restricted())
    cid = client.post("/api/campaigns", json={"niche": "dentist"}).json()["id"]
    assert client.post(f"/api/campaigns/{cid}/discover").json()["result"]["skipped_unlicensed"] == 1
    with Session(migrated_db[0]) as db:
        assert not list(db.scalars(select(Business)))
        assert not list(db.scalars(select(Evidence)))


def test_google_and_invalid_budget_rejected(client):
    assert client.post("/api/campaigns", json={"niche": "dentist", "provider_config": {"discovery": "google_places"}}).status_code == 422
    assert client.post("/api/campaigns", json={"niche": "dentist", "request_budget": -1}).status_code == 422


def test_csv_permissions_and_filtering(tmp_path):
    path = tmp_path / "businesses.csv"
    path.write_text("external_id,name,city,district,niche,source_url,website,phone\n1,Example,Yerevan,Kentron,dentist,https://example.invalid,,+37410123456\n", encoding="utf-8")
    with pytest.raises(ValueError):
        CSVProvider(ProviderConfig(discovery="csv", csv_path=str(path)))
    provider = CSVProvider(ProviderConfig(discovery="csv", csv_path=str(path), license_reference="own-data", persistence_permitted=True, retention_policy="30 days"))
    results = provider.discover("dentist", "Yerevan", ["Kentron"])
    assert len(results) == 1 and not results[0].export_permitted
    assert not provider.discover("cafe", "Yerevan", [])
    assert extract_page_title("<title> Example </title>") == "Example"
