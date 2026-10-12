import csv
import io
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.main import local_run_lock
from app.models import Campaign, Job


def token(client):
    return BeautifulSoup(client.get('/').text, 'html.parser').select_one('[name=csrf_token]')['value']


def test_local_ui_discovery_enrichment_review_export(client, migrated_db, monkeypatch):
    def no_broker(*args, **kwargs):
        raise AssertionError('Local workflow must not publish Celery tasks')
    monkeypatch.setattr('app.tasks.discover_job.delay', no_broker)
    assert client.get('/health').json()['database'] == 'ok'
    csrf = token(client)
    response = client.post('/campaigns', data={'csrf_token': csrf, 'niche': 'dentist', 'city': 'Yerevan',
        'district': 'Kentron', 'keywords_en': 'dentist, dental clinic', 'provider': 'fixture'})
    assert response.status_code == 200
    with Session(migrated_db[0]) as db:
        campaign = db.scalar(select(Campaign))
        cid = campaign.id
        assert campaign.keywords == {'en': ['dentist', 'dental clinic']}
        assert campaign.districts == ['Kentron']
    assert 'SYNTHETIC' in response.text
    for action in ['discover', 'enrich']:
        response = client.post(f'/campaigns/{cid}/{action}', data={'csrf_token': csrf})
        assert 'completed' in response.text
    page = client.get(f'/review?campaign_id={cid}&queue=prospects&valid_phone=yes')
    soup = BeautifulSoup(page.text, 'html.parser')
    ids = [int(item['value']) for item in soup.select('[name=business_ids]')]
    assert len(ids) == 1
    assert 'No website found after checks' in page.text
    response = client.post(f'/api/campaigns/{cid}/export', json={'business_ids': ids})
    rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(rows) == 1 and '+37410123456' in response.text
    # Replay reuses the canonical identity and export selection.
    client.post(f'/campaigns/{cid}/discover', data={'csrf_token': csrf})
    assert client.post(f'/api/campaigns/{cid}/export', json={'business_ids': ids}).status_code == 200


def test_local_resume_and_sequential_guard(client, migrated_db):
    cid = client.post('/api/campaigns', json={'niche': 'dentist'}).json()['id']
    jid = client.post(f'/api/campaigns/{cid}/jobs').json()['job_id']
    csrf = token(client)
    local_run_lock.acquire()
    try:
        assert 'A local run is active' in client.post(f'/local/jobs/{jid}/resume', data={'csrf_token': csrf}).text
        assert client.get('/health').status_code == 200
        with Session(migrated_db[0]) as db:
            assert db.get(Job, jid).status == 'pending'
    finally:
        local_run_lock.release()
    assert 'completed' in client.post(f'/local/jobs/{jid}/resume', data={'csrf_token': csrf}).text


def test_local_unavailable_providers_never_substitute_fixtures(client, migrated_db, tmp_path):
    csrf = token(client)
    response = client.post('/campaigns', data={'csrf_token': csrf, 'niche': 'dentist',
        'provider': 'licensed', 'provider_options': '{"license_reference":"unavailable","persistence_permitted":true}'})
    assert 'provider unavailable' in response.text
    with Session(migrated_db[0]) as db:
        assert db.scalar(select(Campaign)) is None
    path = tmp_path / 'real.csv'
    path.write_text('external_id,name,city,niche,source_url,phone\n1,Operator dataset,Yerevan,dentist,https://example.invalid,+37410123456\n')
    cid = client.post('/api/campaigns', json={'niche': 'dentist', 'provider_config': {
        'discovery': 'csv', 'csv_path': str(path), 'license_reference': 'owned',
        'persistence_permitted': True, 'retention_policy': 'indefinite'}}).json()['id']
    client.post(f'/campaigns/{cid}/discover', data={'csrf_token': csrf})
    assert 'Real enrichment requires a registered live search adapter' in client.post(f'/campaigns/{cid}/enrich', data={'csrf_token': csrf}).text
    with Session(migrated_db[0]) as db:
        assert db.scalar(select(Job).where(Job.kind == 'enrichment')) is None
