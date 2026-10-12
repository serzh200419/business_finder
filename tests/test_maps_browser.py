"""Representative synthetic HTML and scripted DOM, not live scraping verification."""
import base64
import json
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.maps_browser import MapsBrowserError, MapsBrowserProvider, listing_identity, listed_url_kind, parse_listing
from app.maps_selectors import state
from app.models import Business, BusinessSource, Contact, Evidence, JobQuery
from app.providers import DiscoveryRequest, get_provider
from app.schemas import ProviderConfig

HTML = Path(__file__).parent / 'fixtures' / 'maps'
URL = 'https://www.google.com/maps/place/Example/data=!4m2!3m1!1s0x1234:0xabcd'
BRANCH_URL = 'https://www.google.com/maps/place/Example/data=!4m2!3m1!1s0x1234:0xabce'
REQUEST = DiscoveryRequest(12, 'nail salon Yerevan', 'en', 'nail salon', 'Yerevan', None)


def html(name):
    return (HTML / (name + '.html')).read_text(encoding='utf-8')


@pytest.mark.parametrize('name,phone,website,kind', [
    ('normal', '+374 10 123456', 'https://studio.example/', 'candidate_business_website'),
    ('missing_phone', None, 'https://studio.example/', 'candidate_business_website'),
    ('missing_website', '+374 10 123456', None, None),
    ('social', '+374 10 123456', 'https://www.instagram.com/example-studio/', 'social_profile'),
])
def test_listing_fields_and_unverified_website_semantics(name, phone, website, kind):
    result = parse_listing(html(name), URL, REQUEST)
    assert result.name == 'Example Nail Studio'
    assert result.phone == phone and result.website == website
    assert result.district is None and result.email is None
    assert result.details['category'] == 'Nail salon'
    assert result.details['phone_normalized'] == ('+37410123456' if phone else None)
    assert result.details['listed_website_kind'] == kind
    assert result.details['website_identity_verified'] is False
    assert result.details['rating'] == 4.7 and result.details['review_count'] == 125
    assert datetime.fromisoformat(result.details['observed_at']).tzinfo is not None
    assert result.details['query'] == REQUEST.text
    assert not result.persistence_permitted and not result.export_permitted
    if not website:
        assert result.details['website_listing_state'] == 'no_website_listed_on_maps'


def test_branches_identity_and_unreliable_links():
    a = parse_listing(html('normal'), URL, REQUEST)
    b = parse_listing(html('branch'), BRANCH_URL, REQUEST)
    assert a.name == b.name and a.external_id != b.external_id
    assert a.address != b.address
    assert listing_identity(a.source_url)[0] == a.external_id
    assert listing_identity(URL + '!8m2!3d40!4d44?entry=ttu')[0] == a.external_id
    for url in ['https://www.google.com/maps/place/Example', 'https://evil.example/maps?cid=43981',
                'https://www.google.com@evil.example/maps?cid=43981', 'https://www.google.com:notaport/maps?cid=43981']:
        with pytest.raises(MapsBrowserError, match='reliable public listing'):
            listing_identity(url)
    assert listing_identity('https://www.google.com/maps/search/?api=1&query=x&query_place_id=ChIJabcdefghijklmnop')[0] == 'place_id:ChIJabcdefghijklmnop'


@pytest.mark.parametrize('page,code', [('consent','consent'),('challenge','challenge'),('changed','selectors_changed')])
def test_explicit_failures(page, code):
    with pytest.raises(MapsBrowserError) as exc:
        parse_listing(html(page), URL, REQUEST)
    assert exc.value.code == code


def test_no_results_and_url_classification():
    assert state(html('no_results')) == 'no_results'
    assert listed_url_kind('https://spyur.am/example') == 'directory'
    assert listed_url_kind('https://fresha.com/example') == 'booking_profile'
    assert listed_url_kind('javascript:alert(1)') == 'unknown'


class Panel:
    def __init__(self, page): self.page = page
    @property
    def first(self): return self
    def locator(self, value): return self
    def evaluate_all(self, script): return self.page.panels[self.page.round]
    def inner_text(self): return 'You\'ve reached the end of the list' if self.page.end else 'Results'
    def evaluate(self, script): self.page.round = min(self.page.round+1, len(self.page.panels)-1)


class ScriptedPage:
    def __init__(self, panels=None, *, end=False):
        self.panels = panels or [[URL, URL], [URL, BRANCH_URL]]
        self.round, self.end = 0, end
        self.url = 'about:blank'
        self.body = '<html></html>'
        self.navigations = []
        self.failure = None
    def goto(self, url, **kwargs):
        self.navigations.append(url)
        self.url = url
        if self.failure:
            self.body = html(self.failure)
        elif '/maps/search/' in url:
            self.body = '<div role="feed">Results</div>'; self.round = 0
        else:
            self.body = html('normal')
    def content(self): return self.body
    def get_by_role(self, role): return Panel(self)
    def wait_for_timeout(self, timeout): pass


def provider(page, **config):
    p = MapsBrowserProvider(ProviderConfig(discovery='maps_browser', **config))
    p.page = page
    return p


def test_incremental_collection_dedupe_sequential_visits_and_replay():
    p = provider(ScriptedPage(), maps_listing_limit=2)
    first = p.discover_page(REQUEST, None, 1)
    assert first.results == []
    data = json.loads(base64.urlsafe_b64decode(first.next_cursor))
    assert len(data['links']) == 1 and not data['ready']
    second = p.discover_page(REQUEST, first.next_cursor, 1)
    assert second.accounting['collected'] == 2
    a = p.discover_page(REQUEST, second.next_cursor, 1)
    assert len(a.results) == 1 and a.next_cursor
    b = p.discover_page(REQUEST, a.next_cursor, 1)
    assert len(b.results) == 1 and b.next_cursor is None
    assert a.results[0].external_id != b.results[0].external_id
    replay = p.discover_page(REQUEST, second.next_cursor, 1)
    assert replay.results[0].external_id == a.results[0].external_id
    assert p.searches == 1 and p.visits == 3  # replay is charged again


def test_collection_resume_and_bounded_no_progress():
    p = provider(ScriptedPage(panels=[[URL]]), maps_no_progress_rounds=2)
    first = p.discover_page(REQUEST, None, 1)
    # Simulated process loss: a fresh adapter reconstructs feed scroll from checkpoint.
    resumed = provider(ScriptedPage(panels=[[URL]]), maps_no_progress_rounds=2)
    page = resumed.discover_page(REQUEST, first.next_cursor, 1)
    page = resumed.discover_page(REQUEST, page.next_cursor, 1)
    data = json.loads(base64.urlsafe_b64decode(page.next_cursor))
    assert data['ready'] and data['stop'] == 'no_progress' and len(data['links']) == 1
    assert resumed.searches == 1  # reopening the search is a real navigation reservation


def test_challenge_preserves_input_cursor_and_listing_errors_are_explicit():
    page = ScriptedPage(end=True)
    p = provider(page)
    collected = p.discover_page(REQUEST, None, 1)
    cursor = collected.next_cursor
    page.failure = 'challenge'
    with pytest.raises(MapsBrowserError) as exc:
        p.discover_page(REQUEST, cursor, 1)
    assert exc.value.code == 'challenge'
    page.failure = 'changed'
    # Unknown markup is detected by bounded waiting, tested separately via parser.
    with pytest.raises(MapsBrowserError):
        parse_listing(page.body, URL, REQUEST)
    assert collected.next_cursor == cursor


def test_navigation_budget_and_cancellation_before_navigation():
    p = provider(ScriptedPage(end=True), maps_visit_budget=1)
    collected = p.discover_page(REQUEST, None, 1)
    p.discover_page(REQUEST, collected.next_cursor, 1)
    with pytest.raises(MapsBrowserError) as exc:
        p.discover_page(REQUEST, collected.next_cursor, 1)
    assert exc.value.code == 'browser_budget'
    p = provider(ScriptedPage())
    p.cancelled = lambda: True
    with pytest.raises(MapsBrowserError) as exc:
        p.discover_page(REQUEST, None, 1)
    assert exc.value.code == 'cancelled' and p.page.navigations == []


@pytest.mark.parametrize('body,code', [('consent','consent'),('changed','selectors_changed')])
def test_bounded_consent_and_unknown_selector_waits(monkeypatch, body, code):
    from types import SimpleNamespace
    clock = [0.0]
    page = ScriptedPage()
    page.failure = body
    page.wait_for_timeout = lambda ms: clock.__setitem__(0, clock[0]+ms/1000)
    monkeypatch.setattr('app.maps_browser.time', SimpleNamespace(monotonic=lambda: clock[0]))
    p = provider(page)
    with pytest.raises(MapsBrowserError) as exc:
        p.navigate(URL, 1)
    assert exc.value.code == code and clock[0] <= 1


def test_explicit_no_results_and_per_listing_failure():
    page = ScriptedPage(); page.failure = 'no_results'
    p = provider(page)
    empty = p.discover_page(REQUEST, None, 1)
    assert empty.results == [] and empty.next_cursor is None and empty.accounting['stage'] == 'no_results'
    page = ScriptedPage(end=True)
    p = provider(page)
    collected = p.discover_page(REQUEST, None, 1)
    original = page.goto
    def missing_name(url, **kwargs):
        original(url, **kwargs)
        page.body = '<h1></h1><button data-item-id="address">Displayed address</button>'
    page.goto = missing_name
    failed = p.discover_page(REQUEST, collected.next_cursor, 1)
    assert failed.results == [] and failed.rejected_rows[0]['row'] == 1
    assert 'markup was not recognized' in failed.rejected_rows[0]['error']


def test_google_policy_cannot_be_overridden_and_does_not_make_empty_success(client, migrated_db, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('Policy must block before browser/network')
    monkeypatch.setattr(MapsBrowserProvider, 'open', forbidden)
    config = {'discovery':'maps_browser', 'license_reference':'operator-claim',
              'persistence_permitted':True, 'export_permitted':True, 'retention_policy':'indefinite'}
    assert get_provider(ProviderConfig(**config), False).identity == 'google_maps_browser'
    cid = client.post('/api/campaigns', json={'niche':'nail salon', 'provider_config':config}).json()['id']
    result = client.post(f'/api/campaigns/{cid}/discover').json()
    assert result['status'] == 'failed'
    assert result['result']['stop_reason'] == 'source_handling_restricted'
    assert 'cannot override this gate' in result['result']['provider_restriction']
    assert result['result']['requests'] == 0 and result['result']['browser']['collected'] == 0
    with Session(migrated_db[0]) as db:
        for model in [Business, BusinessSource, Contact, Evidence]:
            assert db.scalar(select(func.count()).select_from(model)) == 0
        progress = db.scalar(select(JobQuery))
        assert progress.cursor is None and 'Google-tagged' in progress.error_message
    csrf = BeautifulSoup(client.get('/').text, 'html.parser').select_one('[name=csrf_token]')['value']
    response = client.post(f'/campaigns/{cid}/discover', data={'csrf_token':csrf})
    assert 'Google-tagged source persistence is blocked' in response.text


def test_needs_check_queue_and_audited_manual_links(client, migrated_db):
    from app.review_fixtures import seed_review_demo
    with Session(migrated_db[0]) as db:
        cid, ids = seed_review_demo(db)
    page = client.get(f'/review?campaign_id={cid}&queue=needs_website_check')
    assert page.status_code == 200 and 'Needs website check' in page.text
    detail = client.get(f'/review/{cid}/businesses/{ids["uncertain"]}')
    assert 'Search name + Yerevan' in detail.text and 'https://www.google.com/search?' in detail.text
    assert 'manual website correction' in detail.text
