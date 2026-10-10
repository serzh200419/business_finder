"""Verification-only real Celery worker with a bounded Redis-controlled fixture barrier."""
import time
from redis import Redis
from integration_tests.safety import require_integration_database

require_integration_database()
from app.tasks import celery_app
from app.providers import FixtureProvider
from app.config import get_settings

celery_app.conf.update(broker_connection_timeout=5, broker_connection_max_retries=1,
    visibility_timeout=30, result_backend_transport_options={'visibility_timeout': 30},
    task_publish_retry=False, broker_transport_options={'visibility_timeout': 30, 'socket_timeout': 5, 'socket_connect_timeout': 5})
original = FixtureProvider.discover_page


def controlled_page(self, request, cursor, limit):
    if 'controlled-pause' in request.text:
        broker = Redis.from_url(get_settings().redis_url, socket_timeout=3, socket_connect_timeout=3)
        key = f'bfverify:query:{request.query_id}'
        broker.hincrby(key, 'entries', 1)
        broker.hset(key, 'stage', 'in_provider')
        broker.expire(key, 600)
        deadline = time.monotonic() + 40
        while not broker.hget(key, 'release'):
            if time.monotonic() >= deadline:
                raise RuntimeError('Controlled fixture barrier timed out; explicitly resume')
            time.sleep(.1)
        broker.close()
    return original(self, request, cursor, limit)


FixtureProvider.discover_page = controlled_page
