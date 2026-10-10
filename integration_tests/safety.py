import os
from sqlalchemy.engine import make_url


def require_integration_database():
    url = make_url(os.environ.get('DATABASE_URL', 'sqlite:///demo.db'))
    if os.environ.get('BF_RUNTIME_INTEGRATION') != '1' or url.get_backend_name() != 'postgresql' or not (url.database or '').startswith('bf_integration'):
        raise RuntimeError('Dedicated bf_integration PostgreSQL database and BF_RUNTIME_INTEGRATION=1 required')
    return url
