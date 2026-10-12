"""Concrete sequential Playwright adapter; persistent Google-source gate retained."""
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from bs4 import BeautifulSoup
from app.contacts import classify_url, normalize_contact, normalize_url
from app.maps_selectors import END_RESULTS, FIELDS, field, listing_links, results_panel, state
from app.models import now
from app.providers import DiscoveryPage, DiscoveryResult, ProviderCapabilities, SafeProviderError

RESTRICTION = (
    "Google-tagged source persistence is blocked by the existing application policy; "
    "Maps browser access, license_reference, persistence_permitted and export_permitted "
    "cannot override this gate. No listing data or listing cursors were saved. "
    "Use a separately permitted CSV/licensed source, or explicitly revise the source "
    "policy in a separate reviewed change. See docs/maps-browser.md."
)
ERRORS = {
    'source_handling_restricted': RESTRICTION,
    'browser_missing': 'Install the Maps extra and Chromium: pip install -e ".[maps]"; python -m playwright install chromium.',
    'browser_unavailable': 'Dedicated browser could not start. Install Chromium and close any other run using .local/maps-browser-profile.',
    'challenge': 'Maps challenge detected. Stopped without bypass; use a permitted source or retry manually later.',
    'consent': 'Complete normal consent in the visible dedicated browser, then explicitly resume. No consent choice was made automatically.',
    'timeout': 'Maps navigation/results timed out. Check the visible browser/network, then resume saved checkpoints.',
    'selectors_changed': 'Maps listing/results markup was not recognized. Update app/maps_selectors.py after inspecting the visible UI; no empty success was reported.',
    'identity_unavailable': 'No reliable public listing CID/place ID was present; listing was not assigned a name/position identity.',
    'invalid_cursor': 'Maps cursor/config mismatch; start a new job without deleting existing data.',
    'cancelled': 'Maps operation cancelled; resume explicitly to continue saved checkpoints.',
    'browser_budget': 'Maps search/listing navigation budget reached. Check saved counters; use a new bounded campaign after reviewing the limits.',
}


class MapsBrowserError(SafeProviderError):
    def __init__(self, code):
        super().__init__(code, ERRORS[code])


def source_preflight():
    # Preserve the unconditional Google-provider rule in save_page. No rights switch.
    raise MapsBrowserError('source_handling_restricted')


def listing_identity(url):
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == 'https' and parts.hostname in {'www.google.com', 'google.com', 'maps.google.com'}
            and not parts.username and not parts.password and parts.port in {None, 443}
            and (parts.path.startswith('/maps') or parts.hostname == 'maps.google.com'))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise MapsBrowserError('identity_unavailable')
    query = parse_qs(parts.query)
    cid = query.get('cid', [''])[0]
    match = re.search(r'!1s0x[0-9a-f]+:0x([0-9a-f]+)(?:!|$)', unquote(url), re.I)
    if match:
        cid = str(int(match.group(1), 16))
    if len(cid) <= 20 and cid.isdecimal() and 0 < int(cid) < 2**64:
        return 'cid:' + str(int(cid)), 'https://www.google.com/maps?' + urlencode({'cid': str(int(cid)), 'hl': 'en'})
    place_id = query.get('query_place_id', [''])[0]
    match = re.search(r'!1s(ChI[A-Za-z0-9_-]+)(?:!|$)', unquote(url))
    if match:
        place_id = match.group(1)
    if re.fullmatch(r'ChI[A-Za-z0-9_-]{10,200}', place_id):
        return 'place_id:' + place_id, 'https://www.google.com/maps/search/?' + urlencode({'api': 1, 'query': 'place', 'query_place_id': place_id, 'hl': 'en'})
    raise MapsBrowserError('identity_unavailable')


def listed_url_kind(url):
    if not url:
        return None
    normalized = normalize_url(url)
    if not normalized:
        return 'unknown'
    kind = classify_url(normalized)
    return {'website': 'candidate_business_website', 'social': 'social_profile',
            'directory': 'directory', 'booking': 'booking_profile'}.get(kind, 'unknown')


def parse_listing(html, url, request):
    page_state = state(html, url)
    if page_state in {'consent', 'challenge'}:
        raise MapsBrowserError(page_state)
    if page_state != 'place':
        raise MapsBrowserError('selectors_changed')
    identity, canonical = listing_identity(url)
    soup = BeautifulSoup(html, 'html.parser')
    name = field(soup, 'name')
    if not name:
        raise MapsBrowserError('selectors_changed')
    phone = field(soup, 'phone')
    website_node = soup.select_one(FIELDS['website'])
    website = normalize_url(website_node.get('href', '')) if website_node else None
    rating_match = re.search(r'([0-5](?:\.\d+)?)\s+stars', field(soup, 'rating') or '', re.I)
    review_match = re.search(r'([\d,]+)\s+reviews', field(soup, 'reviews') or '', re.I)
    text = soup.get_text(' ', strip=True)
    operational = next((s for s in ['Permanently closed', 'Temporarily closed'] if s.casefold() in text.casefold()), None)
    return DiscoveryResult(identity, name, request.city, None, request.niche, canonical,
        website=website, phone=phone, provider='google_maps_browser', query_id=request.query_id,
        address=field(soup, 'address'), persistence_permitted=False, export_permitted=False,
        retention_policy='restricted: Google-tagged storage blocked', details={
            'category': field(soup, 'category'), 'phone_raw': phone,
            'phone_normalized': normalize_contact('phone', phone).value if phone else None,
            'listed_website_kind': listed_url_kind(website), 'website_identity_verified': False,
            'maps_listing_url': canonical, 'provider_identifier': identity,
            'rating': float(rating_match.group(1)) if rating_match else None,
            'review_count': int(review_match.group(1).replace(',', '')) if review_match else None,
            'operational_status': operational, 'query': request.text, 'observed_at': now().isoformat(),
            'website_listing_state': 'listed_unverified' if website_node else 'no_website_listed_on_maps'})


class MapsBrowserProvider:
    identity = 'google_maps_browser'
    capabilities = ProviderCapabilities(query_search=True, district_filter=False, max_page_size=1)

    def __init__(self, config, *, progress=None, cancelled=None):
        self.config = config
        self.progress = progress or (lambda event: None)
        self.cancelled = cancelled or (lambda: False)
        self.context = self.page = self.playwright = None
        self.searches = self.visits = 0
        self.budget_counts = None

    def charge(self, kind):
        counts = self.budget_counts() if self.budget_counts else {'searches': self.searches, 'listing_visits': self.visits}
        maximum = self.config.maps_search_budget if kind == 'searches' else self.config.maps_visit_budget
        if counts.get(kind, 0) >= maximum:
            raise MapsBrowserError('browser_budget')
        if kind == 'searches':
            self.searches += 1
        else:
            self.visits += 1
        # Reservation happens before navigation, including failed/replayed visits.
        self.progress({'navigation_kind': kind})

    def open(self):
        if self.page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise MapsBrowserError('browser_missing') from None
        try:
            self.playwright = sync_playwright().start()
            profile = Path(__file__).resolve().parent.parent / '.local' / 'maps-browser-profile'
            profile.mkdir(parents=True, exist_ok=True)
            self.context = self.playwright.chromium.launch_persistent_context(str(profile),
                headless=self.config.maps_headless, locale='en-US', accept_downloads=False,
                timeout=self.config.maps_navigation_timeout * 1000)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(1000)
        except Exception:
            self.close()
            raise MapsBrowserError('browser_unavailable') from None

    def close(self):
        context, playwright = self.context, self.playwright
        self.context = self.page = self.playwright = None
        try:
            if context:
                context.close()
        except Exception:
            pass  # A user-closed browser must not mask the saved job/error.
        finally:
            if playwright:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def check(self, deadline):
        if self.cancelled():
            raise MapsBrowserError('cancelled')
        if time.monotonic() >= deadline:
            raise MapsBrowserError('timeout')

    def navigate(self, url, deadline):
        self.check(deadline)
        try:
            self.page.goto(url, wait_until='domcontentloaded', timeout=min(
                self.config.maps_navigation_timeout, deadline-time.monotonic()) * 1000)
        except Exception:
            raise MapsBrowserError('timeout') from None
        last_state = 'unknown'
        while True:
            try:
                self.check(deadline)
            except MapsBrowserError as exc:
                if exc.code == 'timeout' and last_state == 'consent':
                    raise MapsBrowserError('consent') from None
                if exc.code == 'timeout' and last_state == 'unknown':
                    raise MapsBrowserError('selectors_changed') from None
                raise
            current = state(self.page.content(), self.page.url)
            last_state = current
            if current == 'challenge':
                raise MapsBrowserError('challenge')
            if current == 'consent':
                self.progress({'stage': 'consent', 'error_code': 'consent'})
                if self.config.maps_headless:
                    raise MapsBrowserError('consent')
                # User may click normal consent; do not solve/bypass challenges.
            elif current != 'unknown':
                return current
            self.page.wait_for_timeout(250)

    def cursor(self, request, links, offset, failures, reason, *, ready=True, rounds=0, stalled=0):
        payload = {'v': 1, 'query': hashlib.sha256(request.text.encode()).hexdigest(),
            'links': links, 'offset': offset, 'failures': failures, 'stop': reason,
            'limit': self.config.maps_listing_limit, 'ready': ready, 'rounds': rounds, 'stalled': stalled}
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

    def discover_page(self, request, cursor, limit):
        """Transient adapter; production runner preflights before invoking this method.

        Collection checkpoint is followed by one listing per atomic page. No business
        data is written here. Caller must enforce source handling before saving cursor.
        """
        deadline = time.monotonic() + self.config.maps_operation_timeout
        self.check(deadline)
        self.open()
        data = None
        if cursor:
            try:
                data = json.loads(base64.urlsafe_b64decode(cursor))
                if data['v'] != 1 or data['query'] != hashlib.sha256(request.text.encode()).hexdigest() or data['limit'] != self.config.maps_listing_limit:
                    raise ValueError()
                if not isinstance(data['links'], list) or len(data['links']) > self.config.maps_listing_limit:
                    raise ValueError()
                for url in data['links']:
                    listing_identity(url)
                if not isinstance(data['offset'], int) or not 0 <= data['offset'] <= len(data['links']):
                    raise ValueError()
                if not 0 <= data['rounds'] <= self.config.maps_scroll_round_limit:
                    raise ValueError()
            except Exception:
                raise MapsBrowserError('invalid_cursor') from None
        if not data or not data['ready']:
            links = list(data['links']) if data else []
            failures = list(data['failures']) if data else []
            rounds, stalled = (data['rounds'], data['stalled']) if data else (0, 0)
            restored = data and state(self.page.content(), self.page.url) != 'results'
            if not data or restored:
                self.charge('searches')
                self.progress({'stage': 'search', 'collected': len(links)})
                current = self.navigate('https://www.google.com/maps/search/?' + urlencode({'api': 1, 'query': request.text, 'hl': 'en'}), deadline)
                if current == 'no_results':
                    if links:
                        raise MapsBrowserError('selectors_changed')
                    return DiscoveryPage([], None, accounting={'searches': 1, 'listing_visits': 0, 'stage': 'no_results'})
                if current == 'place':
                    _, url = listing_identity(self.page.url)
                    links = [url]
                    return DiscoveryPage([], self.cursor(request, links, 0, failures, 'single_place'), failures,
                        accounting={'searches': 1, 'listing_visits': 0, 'collected': 1, 'stage': 'collected'})
                if restored:
                    for _ in range(rounds):
                        self.check(deadline)
                        results_panel(self.page).evaluate('(node) => node.scrollBy(0, node.clientHeight)')
                        self.page.wait_for_timeout(350)
            elif rounds:
                results_panel(self.page).evaluate('(node) => node.scrollBy(0, node.clientHeight)')
                self.page.wait_for_timeout(350)
            self.check(deadline)
            current = state(self.page.content(), self.page.url)
            if current in {'challenge', 'consent'}:
                raise MapsBrowserError(current)
            if current != 'results':
                raise MapsBrowserError('selectors_changed')
            seen = {listing_identity(url)[0] for url in links}
            old_count = len(links)
            for url in listing_links(self.page):
                try:
                    identity, canonical = listing_identity(url)
                except MapsBrowserError:
                    failures.append({'row': rounds+1, 'error': ERRORS['identity_unavailable']})
                    continue
                if identity not in seen:
                    seen.add(identity)
                    links.append(canonical)
                if len(links) >= self.config.maps_listing_limit:
                    break
            rounds += 1
            stalled = stalled+1 if len(links) == old_count else 0
            reason = None
            if len(links) >= self.config.maps_listing_limit:
                reason = 'listing_limit'
            elif END_RESULTS.search(results_panel(self.page).inner_text()):
                reason = 'explicit_end'
            elif stalled >= self.config.maps_no_progress_rounds:
                reason = 'no_progress'
            elif rounds >= self.config.maps_scroll_round_limit:
                reason = 'scroll_limit'
            if not links and reason and reason != 'explicit_end':
                raise MapsBrowserError('selectors_changed')
            self.progress({'stage': 'collecting' if not reason else 'collected',
                'collected': len(links), 'failures': len(failures)})
            next_cursor = self.cursor(request, links, 0, failures, reason, ready=bool(reason), rounds=rounds, stalled=stalled)
            return DiscoveryPage([], None if not links and reason else next_cursor, failures,
                accounting={'collected': len(links), 'stage': 'collection', 'stop': reason})
        links, offset, failures = data['links'], data['offset'], data['failures']
        if offset >= len(links):
            raise MapsBrowserError('invalid_cursor')
        _, url = listing_identity(links[offset])
        self.charge('listing_visits')
        self.progress({'stage': 'listing', 'collected': len(links), 'visit_index': offset+1})
        try:
            current = self.navigate(url, deadline)
            if current != 'place':
                raise MapsBrowserError('selectors_changed')
            result = parse_listing(self.page.content(), self.page.url, request)
            expected, _ = listing_identity(url)
            if result.external_id != expected:
                raise MapsBrowserError('identity_unavailable')
            results = [result]
        except MapsBrowserError as exc:
            if exc.code in {'challenge', 'consent', 'timeout', 'cancelled'}:
                raise
            failures = [*failures, {'row': offset+1, 'error': str(exc)}]
            results = []
        next_cursor = self.cursor(request, links, offset+1, failures, data['stop']) if offset+1 < len(links) else None
        return DiscoveryPage(results, next_cursor, failures, accounting={'searches': 0,
            'listing_visits': 1, 'collected': len(links), 'visit_index': offset+1, 'stage': 'listing', 'failures': len(failures)})
