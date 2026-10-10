"""Real-worker protocol. Output contains IDs/counts/statuses only, never record payloads."""
import argparse
import json
import time
import httpx
from redis import Redis
from sqlalchemy import select, func
from integration_tests.safety import require_integration_database

require_integration_database()
from app.config import get_settings
from app.db import SessionLocal
from app.models import Business, Campaign, Contact, Evidence, Job, JobQuery, SearchQuery, now
from app.schemas import CampaignInput
from app.services import create_job, resume_job
from app.tasks import celery_app, discover_job
celery_app.conf.update(broker_connection_timeout=5, task_publish_retry=False,
    visibility_timeout=30, result_backend_transport_options={'visibility_timeout': 30},
    broker_transport_options={'visibility_timeout': 30, 'socket_timeout': 5, 'socket_connect_timeout': 5})


def wait(predicate, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.15)
    raise TimeoutError('Bounded runtime check did not reach expected state')


def status(job_id):
    with SessionLocal() as db:
        return db.get(Job, job_id).status


def complete(job_id):
    return wait(lambda: status_idle(job_id, 'completed'))


def status_idle(job_id, expected):
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        return job.status == expected and db.get(Campaign, job.campaign_id).lease_token is None


def counts():
    with SessionLocal() as db:
        return tuple(db.scalar(select(func.count()).select_from(m)) for m in (Business, Contact, Evidence))


def campaign(db, *, controlled=False, retry=False):
    config = {'discovery': 'fixture', 'page_size': 1, 'retry_base_seconds': 2}
    if retry:
        config['fixture_scenario'] = 'reliability'
    c = Campaign(**CampaignInput(niche='dentist', keywords={'hy' if retry else 'en':
        ['controlled-pause' if controlled else 'dentist']}, request_budget=100, provider_config=config).model_dump())
    db.add(c); db.commit()
    job = create_job(db, c.id)
    query_id = db.scalar(select(JobQuery.query_id).where(JobQuery.job_id == job.id))
    return c.id, job.id, query_id


def broker():
    return Redis.from_url(get_settings().redis_url, socket_timeout=3, socket_connect_timeout=3)


def entered(query_id):
    with broker() as redis:
        return redis.hget(f'bfverify:query:{query_id}', 'stage') == b'in_provider'


def release(query_id):
    with broker() as redis:
        redis.hset(f'bfverify:query:{query_id}', 'release', '1')


def normal_checks():
    assert not celery_app.conf.task_always_eager
    with broker() as redis:
        assert redis.ping()
    inspector = celery_app.control.inspect(timeout=5)
    assert inspector.ping()
    assert any({'app.tasks.discover_job', 'app.tasks.discover_campaign'} <= set(names)
               for names in (inspector.registered() or {}).values())
    with SessionLocal() as db:
        cid, job_id, _ = campaign(db, retry=True)
    discover_job.delay(job_id)
    def scheduled():
        with SessionLocal() as db:
            p = db.scalar(select(JobQuery).where(JobQuery.job_id == job_id))
            return p.next_attempt_at is not None and p.attempts == 1 and db.get(Job, job_id).status == 'partial'
    wait(scheduled)
    complete(job_id)
    with SessionLocal() as db:
        p = db.scalar(select(JobQuery).where(JobQuery.job_id == job_id))
        assert p.attempts > p.pages_committed  # An actual queued retry consumed a request.
    before = counts()
    with SessionLocal() as db:
        replay = create_job(db, cid, fresh=True).id
    discover_job.delay(replay); complete(replay)
    assert counts() == before
    with SessionLocal() as db:
        cancel_cid, cancel_id, query_id = campaign(db, controlled=True)
    discover_job.delay(cancel_id)
    wait(lambda: entered(query_id))
    with SessionLocal() as db:
        db.get(Job, cancel_id).cancel_requested = True
        db.commit()
    release(query_id)
    wait(lambda: status_idle(cancel_id, 'cancelled'))
    with SessionLocal() as db:
        p = db.scalar(select(JobQuery).where(JobQuery.job_id == cancel_id))
        assert p.pages_committed == 1 and p.cursor is not None
        assert db.scalar(select(func.count()).select_from(Evidence).where(Evidence.campaign_id == cancel_cid)) == 1
        resume_job(db, cancel_id)
    discover_job.delay(cancel_id); complete(cancel_id)
    from app.assessment_fixtures import seed_fixture_campaign
    from app.enrichment import create_enrichment_job, effective_assessment
    with SessionLocal() as db:
        enrichment_campaign, options, expected = seed_fixture_campaign(db)
        enrichment_id = create_enrichment_job(db, enrichment_campaign.id, options).id
    discover_job.delay(enrichment_id); complete(enrichment_id)
    with SessionLocal() as db:
        assert all(effective_assessment(db, item['business_id']).status == item['expected'] for item in expected)
    before = counts()
    with SessionLocal() as db:
        replay = create_enrichment_job(db, enrichment_campaign.id, options).id
    discover_job.delay(replay); complete(replay)
    assert counts() == before
    from app.review_fixtures import seed_review_demo
    with SessionLocal() as db:
        review_id, ids = seed_review_demo(db)
    with httpx.Client(base_url='http://web:8000', headers={'Host': 'localhost'}, timeout=5, trust_env=False) as client:
        assert client.get('/health').json()['status'] == 'ok'
        assert client.get('/review', params={'campaign_id': review_id, 'queue': 'all'}).status_code == 200
        exported = client.post(f'/api/campaigns/{review_id}/export', json={'business_ids': list(ids.values())})
        assert exported.status_code == 200 and exported.content.startswith(b'\xef\xbb\xbf')
        assert json.loads(exported.headers['x-export-summary'])['exported'] == 8
    return {'status': 'PASS', 'checks': ['redis_ping', 'worker_registration', 'queued_discovery', 'persisted_retry',
        'cancel_explicit_resume', 'queued_enrichment', 'idempotent_replay', 'web_review_export']}


def prepare_interruption():
    with SessionLocal() as db:
        _, job_id, query_id = campaign(db, controlled=True)
    discover_job.delay(job_id)
    wait(lambda: entered(query_id))
    return {'job_id': job_id, 'query_id': query_id, 'stage': 'in_provider'}


def finish_interruption(job_id, query_id):
    release(query_id)
    automatic = False
    try:
        wait(lambda: status_idle(job_id, 'completed'), seconds=75)
        automatic = True
    except TimeoutError:
        # Documented fallback, not a claim of automatic Redis redelivery.
        with SessionLocal() as db:
            c = db.get(Campaign, db.get(Job, job_id).campaign_id)
            from app.services import utc
            if c.lease_until and utc(c.lease_until) > now():
                raise RuntimeError('Lease still active; recovery is blocked')
            resume_job(db, job_id)
        discover_job.delay(job_id); complete(job_id)
    with SessionLocal() as db:
        cid = db.get(Job, job_id).campaign_id
        assert db.scalar(select(func.count()).select_from(Evidence).where(Evidence.campaign_id == cid)) == 2
    before = counts()
    with SessionLocal() as db:
        replay = create_job(db, cid, fresh=True).id
    discover_job.delay(replay); complete(replay)
    assert counts() == before
    return {'status': 'PASS', 'recovery': 'automatic_redelivery_observed' if automatic else 'explicit_manual_resume_observed',
            'duplicates': 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['normal', 'prepare-interruption', 'finish-interruption'])
    parser.add_argument('--job-id', type=int)
    parser.add_argument('--query-id', type=int)
    args = parser.parse_args()
    try:
        result = normal_checks() if args.phase == 'normal' else prepare_interruption() if args.phase == 'prepare-interruption' else finish_interruption(args.job_id, args.query_id)
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
