import os
from uuid import uuid4
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from app.config import get_settings
from integration_tests.safety import require_integration_database


@pytest.fixture
def postgres_db(monkeypatch):
    if os.environ.get('RUN_INTEGRATION_TESTS') != '1':
        pytest.skip('Real PostgreSQL tests require RUN_INTEGRATION_TESTS=1 and a dedicated database')
    base = require_integration_database()
    schema = 'bf_test_' + uuid4().hex
    admin = create_engine(base, connect_args={'connect_timeout': 5})
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))  # Generated identifier only.
    admin.dispose()
    url = base.update_query_dict({'options': f'-csearch_path={schema} -cstatement_timeout=5000 -clock_timeout=3000', 'connect_timeout': '5'})
    monkeypatch.setenv('DATABASE_URL', url.render_as_string(hide_password=False))
    monkeypatch.setenv('DEMO_MODE', 'true')
    get_settings.cache_clear()
    config = Config('alembic.ini')
    command.upgrade(config, 'head')
    engine = create_engine(url, pool_pre_ping=True)
    yield engine, config
    engine.dispose()
    get_settings.cache_clear()
    # Retain the generated test schema for diagnostics; no databases/volumes are deleted.
