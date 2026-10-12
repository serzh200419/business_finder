"""Maps semantic locators and explicitly provisional HTML fallbacks.

These are not certified current live selectors: no executable browser was available
for this change. Unknown markup fails closed rather than reporting empty results.
"""
import re
from bs4 import BeautifulSoup

FIELDS = {
    "name": 'h1, [role="heading"][aria-level="1"]',
    "phone": '[data-item-id^="phone:tel:"], a[href^="tel:"]',
    "website": 'a[data-item-id="authority"], a[aria-label^="Website:"]',
    "address": '[data-item-id="address"], [aria-label^="Address:"]',
    "category": '[data-item-id="category"], button[jsaction*="pane.rating.category"]',
    "rating": '[role="img"][aria-label*="stars"]',
    "reviews": '[aria-label*=" reviews"]',
}
NO_RESULTS = re.compile(r"no results found|no results for|couldn.t find", re.I)
END_RESULTS = re.compile(r"you.ve reached the end of the list", re.I)
CHALLENGE = re.compile(r"unusual traffic|verify you.re human|automated queries|complete the captcha", re.I)


def state(html: str, url: str = "") -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    if '/sorry/' in url or soup.select('iframe[src*="recaptcha"], .g-recaptcha') or CHALLENGE.search(text):
        return "challenge"
    if 'consent.google.' in url or re.search(r"before you continue to Google", text, re.I):
        return "consent"
    if NO_RESULTS.search(text):
        return "no_results"
    if soup.select_one('[role="feed"]'):
        return "results"
    if soup.select_one(FIELDS['name']) and soup.select_one('[data-item-id], a[href^="tel:"]'):
        return "place"
    return "unknown"


def results_panel(page):
    return page.get_by_role("feed").first


def listing_links(page):
    return results_panel(page).locator('a[href*="/maps/place/"]').evaluate_all(
        '(nodes) => nodes.map(node => node.href)')


def field(soup, name):
    node = soup.select_one(FIELDS[name])
    if node is None:
        return None
    value = node.get('aria-label') or node.get_text(' ', strip=True)
    if value:
        return re.sub(r'^(Phone|Address|Website):\s*', '', value).strip() or None
    return None
