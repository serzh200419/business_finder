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
import socket
import sys


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not make external network or DNS requests")
    original_connect = socket.socket.connect
    def guarded_connect(sock, address):
        # Windows asyncio implements its internal socketpair through loopback TCP.
        # Allow only that standard-library IPC call, never application fetches.
        if sys.platform == "win32" and sys._getframe(1).f_code.co_name == "_fallback_socketpair" and address[0] == "127.0.0.1":
            return original_connect(sock, address)
        return forbidden()
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(url)
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    yield engine, config
    engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
def client(migrated_db):
    engine, _ = migrated_db
    def override():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_db] = override
    with TestClient(app, base_url="http://localhost") as client:
        yield client
    app.dependency_overrides.clear()


