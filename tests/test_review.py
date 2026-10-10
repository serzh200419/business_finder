import csv
import io
import json
from datetime import timedelta
from bs4 import BeautifulSoup
import pytest
from sqlalchemy import event, select, func
from sqlalchemy.orm import Session
from app.eligibility import eligible, retention
from app.export import safe_cell, selected_export
from app.models import Business, Campaign, Contact, Evidence, OutreachRecord, WebsiteAssessment, now
from app.review import OutreachInput, ReviewData, record_outreach, review_page
from app.review_fixtures import seed_review_demo, review_demo
from app.retention import cleanup


@pytest.fixture
def review_seed(migrated_db):
    with Session(migrated_db[0]) as db:
        return seed_review_demo(db)


def test_priority_reasons_exclusive_statuses_and_config(review_seed, migrated_db):
    cid, ids = review_seed
    with Session(migrated_db[0]) as db:
        data = ReviewData(db, cid)
        social, missing = data.project(ids['social']), data.project(ids['no_site'])
        assert social['priority'] == 90 and missing['priority'] == 75
        assert social['reasons'][0]['points'] == 25 and missing['reasons'][0]['points'] == 35
        assert data.project(ids['no_phone'])['priority'] == 55
        assert data.project(ids['uncertain'])['priority'] == 40
        assert not data.project(ids['uncertain'])['queue_eligible']
        assert data.project(ids['override'])['status'] == 'no_website_found'
        assert data.project(ids['override'])['priority'] == 75
        assert data.project(ids['declined'])['priority'] == data.project(ids['suppressed'])['priority'] == 0
        db.get(Campaign, cid).review_priority = {'phone': 100, 'activity_days': 1}
        db.commit()
        social = ReviewData(db, cid).project(ids['social'])
        assert social['priority'] == 100
        assert not any('activity' in r['reason'] for r in social['reasons'])


def test_activity_requires_explicit_dated_identity_evidence(review_seed, migrated_db):
    cid, ids = review_seed
    with Session(migrated_db[0]) as db:
        e = db.scalar(select(Evidence).where(Evidence.evidence_type == 'activity'))
        for details in ({'identity_matched': True}, {'activity_at': now().isoformat()},
                        {'identity_matched': True, 'activity_at': (now()+timedelta(days=1)).isoformat()},
                        {'identity_matched': True, 'activity_at': 'yesterday'}):
            e.details = details
            db.commit()
            assert ReviewData(db, cid).project(ids['social'])['priority'] == 75


def test_campaign_specific_niche_and_global_manual_override(review_seed, migrated_db):
    cid, ids = review_seed
    with Session(migrated_db[0]) as db:
        second = Campaign(niche='cafe', city='Yerevan', keywords={}, provider_config={})
        db.add(second)
        db.flush()
        original = db.scalar(select(Evidence).where(Evidence.business_id == ids['override'], Evidence.evidence_type == 'discovery'))
        db.add(Evidence(business_id=ids['override'], campaign_id=second.id, provider='review_fixture',
            source_url='https://example.invalid/shared', persistence_permitted=True, export_permitted=True,
            retention_policy='indefinite', match_reasons=[], details=dict(original.details)))
        db.commit()
        a, b = ReviewData(db, cid).project(ids['override']), ReviewData(db, second.id).project(ids['override'])
        assert a['priority'] == 75 and b['priority'] == 55
        assert b['status'] == 'no_website_found' and not b['niche_eligible']


def test_filters_pagination_and_fixed_query_count(review_seed, migrated_db, client):
    cid, ids = review_seed
    statements = []
    engine = migrated_db[0]
    def collect(*args):
        statements.append(args[2])
    event.listen(engine, 'before_cursor_execute', collect)
    try:
        with Session(engine) as db:
            result = review_page(db, cid, page_size=2, queue='all', sort='assessment_date')
            assert result['total'] == 12 and result['pages'] == 6 and len(result['rows']) == 2
            assert len(statements) <= 7
            assert review_page(db, cid, district='Arabkir', valid_phone='no')['total'] == 1
            assert review_page(db, cid, website_status='uncertain')['total'] == 1
            assert review_page(db, cid, outreach_status='declined', review_status='reviewed')['total'] == 1
            assert review_page(db, cid, suppression='yes')['total'] == 1
            assert review_page(db, cid, niche='cafe')['total'] == 0
            queue = review_page(db, cid, queue='prospects')['rows']
            assert all(r['status'] in {'social_only', 'no_website_found'} and not r['suppressed'] and
                       r['outreach_status'] != 'declined' for r in queue)
    finally:
        event.remove(engine, 'before_cursor_execute', collect)
    response = client.get('/review', params={'campaign_id': cid, 'queue': 'all', 'page_size': 2, 'valid_phone': 'yes'})
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, 'html.parser')
    next_link = soup.find('a', string='Next')['href']
    assert 'valid_phone=yes' in next_link and 'queue=all' in next_link and 'page=2' in next_link
    assert 'synthetic' in response.text and 'review_priority' not in response.text
    detail = soup.find('a', href=lambda x: x and '/review/' in x and '/businesses/' in x)['href']
    assert 'return_query=' in detail and client.get(detail).status_code == 200


def test_expired_revoked_and_parent_permissions_excluded_immediately(review_seed, migrated_db, client):
    cid, ids = review_seed
    with Session(migrated_db[0]) as db:
        data = ReviewData(db, cid)
        for key in ('expired', 'revoked'):
            row = data.project(ids[key])
            assert row['status'] == 'not_checked' and row['priority'] == 0 and not row['phones']
            assert not row['identity_eligible']
        seed = next(e for e in data.evidence.values() if e.business_id == ids['no_site'] and e.evidence_type == 'discovery')
        child = next(e for e in data.evidence.values() if e.parent_evidence_id == seed.id)
        seed.export_permitted = False
        assert not eligible(child, data.evidence, purpose='export').allowed
        seed.revoked_at = now()
        assert not eligible(child, data.evidence).allowed
    detail = client.get(f"/api/businesses/{ids['expired']}").json()
    assert detail['contacts'] == [] and detail['assessment']['status'] == 'not_checked'
    assert 'Synthetic Expired' not in detail['name']


@pytest.mark.parametrize('policy,allowed', [('indefinite', True), ('30 days', True),
    ('expires: 2099-01-01T00:00:00Z', True), ('30 days; expires: 2000-01-01T00:00:00Z', False),
    ('unspecified', False), ('forever maybe', False), ('indefinite; bad expiry', False),
    ('expires: 2099-01-01', False), ('', False)])
def test_retention_declarations_fail_closed(policy, allowed):
    e = Evidence(id=1, observed_at=now(), retention_policy=policy, persistence_permitted=True,
                 export_permitted=True, support_evidence_ids=[])
    assert eligible(e, {1: e}).allowed == allowed


def test_explicit_dependencies_missing_cycles_and_revocation():
    a = Evidence(id=1, observed_at=now(), retention_policy='indefinite', persistence_permitted=True,
                 export_permitted=True, support_evidence_ids=[2])
    b = Evidence(id=2, observed_at=now(), retention_policy='indefinite', persistence_permitted=True,
                 export_permitted=True, support_evidence_ids=[])
    assert not eligible(a, {1: a}).allowed
    assert eligible(a, {1: a, 2: b}).allowed
    b.revoked_at = now()
    assert not eligible(a, {1: a, 2: b}).allowed
    b.revoked_at, b.support_evidence_ids = None, [1]
    assert not eligible(a, {1: a, 2: b}).allowed


def test_cleanup_dry_run_independent_data_and_manual_audits(review_seed, migrated_db):
    cid, ids = review_seed
    with Session(migrated_db[0]) as db:
        count = lambda model: db.scalar(select(func.count()).select_from(model))
        before = count(Contact), count(Evidence), count(OutreachRecord)
        manual = db.scalar(select(WebsiteAssessment).where(WebsiteAssessment.origin == 'manual'))
        audit = manual.reasons[:]
        preview = cleanup(db)
        assert (count(Contact), count(Evidence), count(OutreachRecord)) == before
        report = cleanup(db, execute=True)
        assert report['contacts_deleted'] == 3 and report['assessments_invalidated'] == 2
        assert count(Evidence) == before[1] and count(OutreachRecord) == before[2]
        assert manual.reasons == audit and manual.status == 'no_website_found'
        assert ReviewData(db, cid).project(ids['independent'])['phones']
        assert db.get(Business, ids['expired']).name.startswith('Retained business #')
        assert not db.get(Business, ids['expired']).address
        tombstones = list(db.scalars(select(Evidence).where(Evidence.cleaned_at.is_not(None))))
        assert all(e.details == {} and e.source_url == '' for e in tombstones)
        second = cleanup(db, execute=True)
        assert second['contacts_deleted'] == second['evidence_tombstones'] == second['assessments_invalidated'] == 0
        assert second['canonical_records_redacted'] == 0


def test_cleanup_retains_unknown_declarations_and_invalidates_expired_manual(review_seed, migrated_db):
    cid, ids = review_seed
    with Session(migrated_db[0]) as db:
        manual = db.scalar(select(WebsiteAssessment).where(WebsiteAssessment.origin == 'manual'))
        support = db.get(Evidence, manual.evidence_id)
        support.expires_at = now() - timedelta(seconds=1)
        unknown = db.scalar(select(Evidence).where(Evidence.business_id == ids['mixed'], Evidence.evidence_type == 'discovery'))
        unknown.retention_policy = 'unknown expiry'
        db.commit()
        assert ReviewData(db, cid).project(ids['override'])['status'] == 'not_checked'
        report = cleanup(db, execute=True)
        assert manual.status == 'no_website_found' and manual.reasons
        assert not unknown.cleaned_at and report['unknown_retention_excluded_but_not_deleted'] > 0
        assert ReviewData(db, cid).project(ids['override'])['status'] == 'not_checked'


def test_export_field_permissions_unicode_and_selection_scope(review_seed, migrated_db, client):
    cid, ids = review_seed
    with Session(migrated_db[0]) as db:
        content, summary = selected_export(db, cid, list(ids.values()) + [99999, ids['mixed']])
        assert content.startswith(b'\xef\xbb\xbf') and summary['selected'] == 13 and summary['exported'] == 8
        rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
        mixed = next(r for r in rows if 'Mixed' in r['business_name'])
        assert mixed['public_phone'] and not mixed['public_email']
        assert 'restricted' not in mixed['contact_source_urls']
        override = next(r for r in rows if 'Override' in r['business_name'])
        assert not override['website_status'] and override['review_priority'] == '40'
        assert 'Армат' not in content.decode('utf-8-sig')  # Armenian text remains Armenian.
        assert 'Արմատ Клиника' in content.decode('utf-8-sig')
        formula = next(r for r in rows if 'HYPERLINK' in r['business_name'])
        assert formula['business_name'].startswith("'\t\ufeff=")
        assert all(r['public_phone'].startswith("'+374") for r in rows if r['public_phone'])
        assert all(not any(key in r['business_name'] for key in ('Declined', 'Suppressed', 'Expired', 'Revoked')) for r in rows)
    response = client.post(f'/api/campaigns/{cid}/export', json={'business_ids': list(ids.values())})
    assert response.status_code == 200 and response.content.startswith(b'\xef\xbb\xbf')
    assert json.loads(response.headers['x-export-summary'])['exported'] == 8


@pytest.mark.parametrize('value', ['=1+1', '+cmd', '-formula', '@SUM(1)', ' \t=1', '\x00\x1b+cmd', '\ufeff @cmd', '\n\r-1'])
def test_formula_protection_after_whitespace_and_controls(value):
    assert safe_cell(value) == "'" + value
    assert safe_cell('Արմատ, Клиника "Demo"') == 'Արմատ, Клиника "Demo"'


def test_manual_outreach_audit_and_global_suppression(review_seed, migrated_db, client):
    cid, ids = review_seed
    response = client.post(f"/api/campaigns/{cid}/businesses/{ids['no_site']}/outreach", json={
        'status': 'contacted', 'note': 'Recorded my manual phone call', 'operator_label': 'Operator label'})
    assert response.status_code == 200 and response.json()['manual_only']
    with Session(migrated_db[0]) as db:
        record_outreach(db, cid, ids['no_site'], OutreachInput(status='replied', operator_label='Operator label',
            suppressed=True, suppression_reason='Requested no further contact'))
        rows = ReviewData(db, cid).histories[ids['no_site']]
        assert [r.status for r in rows] == ['contacted', 'replied']
        assert rows[-1].previous_state['status'] == 'contacted'
        assert db.get(Business, ids['no_site']).suppressed
        assert not ReviewData(db, cid).project(ids['no_site'])['queue_eligible']
        record_outreach(db, cid, ids['no_site'], OutreachInput(status='reviewed', operator_label='Operator label'))
        assert db.get(Business, ids['no_site']).suppressed
        assert db.get(Business, ids['no_site']).suppression_reason == 'Requested no further contact'
        with pytest.raises(ValueError):
            record_outreach(db, cid, ids['no_site'], OutreachInput(status='reviewed', operator_label='x', suppressed=True, suppression_reason=''))


def test_csrf_hosts_origins_and_successful_forms(review_seed, client):
    cid, ids = review_seed
    assert client.post('/campaigns', data={'niche': 'dentist'}).status_code == 403
    assert client.get('/', headers={'Host': 'attacker.example'}).status_code == 400
    for origin in ('https://attacker.example', 'null', 'http://localhost:9999', 'http://['):
        assert client.post('/api/campaigns', json={'niche': 'dentist'}, headers={'Origin': origin}).status_code == 403
    assert client.post('/api/campaigns', json={'niche': 'dentist'}, headers={'Origin': 'http://localhost'}).status_code == 201
    page = client.get(f"/review/{cid}/businesses/{ids['no_site']}")
    token = BeautifulSoup(page.text, 'html.parser').find('input', {'name': 'csrf_token'})['value']
    target = f"/review/{cid}/businesses/{ids['no_site']}/outreach"
    fields = {'status': 'reviewed', 'note': 'manual note', 'operator_label': 'local operator', 'csrf_token': token}
    assert client.post(target, data={**fields, 'csrf_token': 'Արմատ'}).status_code == 403
    assert client.post(target, data=fields, follow_redirects=False).status_code == 303
    assert client.post(f'/review/{cid}/export', data={'business_ids': str(ids['no_site']), 'csrf_token': token}).status_code == 200
    assert client.post(f"/businesses/{ids['no_site']}/assessment/correct", data={
        'status': 'social_only', 'reason': 'manual correction', 'actor': 'operator label', 'csrf_token': token},
        follow_redirects=False).status_code == 303


def test_complete_review_demo(migrated_db, tmp_path):
    with Session(migrated_db[0]) as db:
        output = tmp_path / 'demo.csv'
        result = review_demo(db, output)
        assert result['synthetic_only'] and result['export']['exported'] == 8
        assert output.read_bytes().startswith(b'\xef\xbb\xbf')


def test_expired_contact_is_reported_without_disclosing_value(review_seed, migrated_db):
    cid, ids = review_seed
    with Session(migrated_db[0]) as db:
        evidence = db.scalar(select(Evidence).where(Evidence.business_id == ids['mixed'], Evidence.export_permitted.is_(False)))
        evidence.expires_at = now() - timedelta(seconds=1)
        db.commit()
        local = ReviewData(db, cid).project(ids['mixed'])
        assert not local['emails']
        content, summary = selected_export(db, cid, [ids['mixed']])
        assert summary['omitted_fields'][0]['fields'] == ['public_email']
        assert b'Restricted.Office' not in content


def test_cleanup_refuses_active_worker_lease(review_seed, migrated_db):
    cid, _ = review_seed
    with Session(migrated_db[0]) as db:
        db.get(Campaign, cid).lease_until = now() + timedelta(minutes=1)
        db.commit()
        assert cleanup(db)['dry_run']
        with pytest.raises(ValueError, match='leases'):
            cleanup(db, execute=True)


def test_expired_manual_audit_can_receive_fresh_explicit_correction(review_seed, migrated_db):
    from app.enrichment import manual_correction
    from app.schemas import ManualAssessmentInput
    cid, ids = review_seed
    with Session(migrated_db[0]) as db:
        old = db.scalar(select(WebsiteAssessment).where(WebsiteAssessment.origin == 'manual'))
        db.get(Evidence, old.evidence_id).expires_at = now() - timedelta(seconds=1)
        db.commit()
        assert ReviewData(db, cid).project(ids['override'])['status'] == 'not_checked'
        new = manual_correction(db, ids['override'], ManualAssessmentInput(status='social_only', actor='operator label', reason='Fresh review'))
        assert new.id != old.id and old.status == 'no_website_found'
        assert ReviewData(db, cid).project(ids['override'])['status'] == 'social_only'


def test_expired_enrichment_checkpoints_hidden_before_cleanup(migrated_db, client):
    from app.assessment_fixtures import seed_fixture_campaign
    from app.enrichment import create_enrichment_job, run_enrichment_job
    from app.models import JobBusiness
    with Session(migrated_db[0]) as db:
        campaign, options, expected = seed_fixture_campaign(db)
        options = options.model_copy(update={'business_ids': [expected[0]['business_id']]})
        job = run_enrichment_job(db, create_enrichment_job(db, campaign.id, options).id)
        progress = db.scalar(select(JobBusiness).where(JobBusiness.job_id == job.id))
        assert progress.state['checks']
        page = db.scalar(select(Evidence).where(Evidence.evidence_type == 'public_page'))
        page.revoked_at = now()
        db.commit()
        job_id, cid = job.id, campaign.id
    summary = client.get(f'/api/jobs/{job_id}').json()['result']['businesses'][0]
    assert not summary['checks'] and not summary['evidence_eligible']
    assert 'missing.synthetic.example' not in client.get('/').text
    assert not client.get(f'/api/campaigns/{cid}/jobs').json()[0]['result']['businesses'][0]['checks']


def test_csv_import_enters_review_with_captured_field_permissions(client, migrated_db, tmp_path):
    path = tmp_path / 'owned.csv'
    path.write_text('external_id,name,city,district,niche,source_url,phone,email,address\n'
        'one,Imported Արմատ Клиника,Yerevan,Kentron,dentist,https://example.invalid/owned,+37410123456,Office+Demo@EXAMPLE.COM,1 Owned Street\n', encoding='utf-8')
    cid = client.post('/api/campaigns', json={'niche': 'dentist', 'keywords': {'en': ['dentist']},
        'provider_config': {'discovery': 'csv', 'csv_path': str(path), 'license_reference': 'operator-owned-test-data',
            'persistence_permitted': True, 'export_permitted': True, 'retention_policy': '30 days', 'record_type': 'imported'}}).json()['id']
    assert client.post(f'/api/campaigns/{cid}/discover').status_code == 200
    with Session(migrated_db[0]) as db:
        rows = review_page(db, cid, queue='all')['rows']
        assert len(rows) == 1 and rows[0]['record_type'] == 'imported' and rows[0]['priority'] == 40
        content, summary = selected_export(db, cid, [rows[0]['id']])
        exported = next(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
        assert summary['exported'] == 1 and exported['address'] == '1 Owned Street'
        assert exported['public_email'] == 'Office+Demo@example.com'
        assert exported['business_name'] == 'Imported Արմատ Клиника'
