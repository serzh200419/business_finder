from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import pytest
from alembic import command
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models import Business, BusinessSource, Campaign, Contact, Evidence, JobQuery, now
from app.schemas import CampaignInput
from app.services import create_job, run_job, claim_lease, release_lease, fence, LeaseBusy, LeaseLost


def seed(db):
    c = Campaign(**CampaignInput(niche='dentist', keywords={'en': ['dentist']}, request_budget=100,
        provider_config={'discovery': 'fixture', 'page_size': 1}).model_dump())
    db.add(c)
    db.commit()
    return c.id


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_real_migrations_and_schema_drift(postgres_db):
    _, config = postgres_db
    command.check(config)
    command.downgrade(config, '7b2c3d4e5f6a')
    command.upgrade(config, 'head')
    command.check(config)


def test_real_provider_scoped_unique_identity(postgres_db):
    with Session(postgres_db[0]) as db:
        a, b = Business(name='Synthetic A', city='Yerevan', identity_key='a'), Business(name='Synthetic B', city='Yerevan', identity_key='b')
        db.add_all([a, b]); db.flush()
        db.add_all([BusinessSource(business_id=a.id, provider='licensed_a', external_id='one', source_url='https://example.invalid/a'),
                    BusinessSource(business_id=b.id, provider='licensed_b', external_id='one', source_url='https://example.invalid/b')])
        db.commit()
        assert count(db, BusinessSource) == 2
        db.add(BusinessSource(business_id=b.id, provider='licensed_a', external_id='one', source_url='https://example.invalid/duplicate'))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert count(db, BusinessSource) == 2


def test_real_page_replay_without_duplicates(postgres_db):
    with Session(postgres_db[0]) as db:
        cid = seed(db)
        assert run_job(db, create_job(db, cid).id).status == 'completed'
        before = tuple(count(db, model) for model in (Business, BusinessSource, Contact, Evidence))
        assert run_job(db, create_job(db, cid, fresh=True).id).status == 'completed'
        assert tuple(count(db, model) for model in (Business, BusinessSource, Contact, Evidence)) == before


def test_real_atomic_page_rollback(postgres_db, monkeypatch):
    import app.services as services
    original = services.insert_once
    def fail(db, model, *args, **kwargs):
        if model is Evidence:
            raise RuntimeError('Synthetic atomic page interruption')
        return original(db, model, *args, **kwargs)
    with Session(postgres_db[0]) as db:
        cid = seed(db)
        job = create_job(db, cid)
        monkeypatch.setattr(services, 'insert_once', fail)
        with pytest.raises(RuntimeError):
            run_job(db, job.id)
        assert all(count(db, model) == 0 for model in (Business, BusinessSource, Evidence, Contact))
        p = db.scalar(select(JobQuery).where(JobQuery.job_id == job.id))
        assert p.cursor is None and p.pages_committed == 0
        monkeypatch.setattr(services, 'insert_once', original)
        assert run_job(db, job.id).status == 'completed'


def test_real_concurrent_lease_acquisition(postgres_db):
    engine = postgres_db[0]
    with Session(engine) as db:
        cid = seed(db)
    barrier = Barrier(2)
    def claim():
        with Session(engine) as db:
            barrier.wait(timeout=5)
            try:
                return claim_lease(db, cid, now())
            except LeaseBusy:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        tokens = [f.result(timeout=10) for f in futures]
    assert sum(t is not None for t in tokens) == 1


def test_real_expiry_recovery_and_stale_worker_fencing(postgres_db):
    with Session(postgres_db[0]) as db:
        cid = seed(db)
        old = claim_lease(db, cid, now())
        db.get(Campaign, cid, populate_existing=True).lease_until = now() - timedelta(seconds=1)
        db.commit()
        later = now()
        replacement = claim_lease(db, cid, later)
        with pytest.raises(LeaseLost):
            fence(db, cid, old, later)
        release_lease(db, cid, old)
        assert db.get(Campaign, cid, populate_existing=True).lease_token == replacement
        fence(db, cid, replacement, later)
        db.commit()
        release_lease(db, cid, replacement)


def test_real_permission_narrowing_and_field_export(postgres_db):
    from app.export import selected_export
    from app.review_fixtures import seed_review_demo
    with Session(postgres_db[0]) as db:
        cid = seed(db)
        run_job(db, create_job(db, cid).id)
        for e in db.scalars(select(Evidence)):
            e.export_permitted = False
        db.commit()
        run_job(db, create_job(db, cid, fresh=True).id)
        assert all(not e.export_permitted for e in db.scalars(select(Evidence)))
        review_id, ids = seed_review_demo(db)
        content, summary = selected_export(db, review_id, [ids['mixed'], ids['suppressed'], ids['declined']])
        assert summary['exported'] == 1 and len(summary['omitted_rows']) == 2
        assert b'Restricted.Office' not in content and b'+374' in content
        seed_evidence = db.scalar(select(Evidence).where(Evidence.business_id == ids['mixed'], Evidence.evidence_type == 'discovery'))
        seed_evidence.export_permitted = False
        db.commit()
        assert selected_export(db, review_id, [ids['mixed']])[1]['exported'] == 0
