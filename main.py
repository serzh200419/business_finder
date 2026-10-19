"""Google Places Text Search -> Excel-friendly CSV. Edit settings in main()."""
import csv
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.internationalPhoneNumber,places.nationalPhoneNumber,"
    "places.websiteUri,places.googleMapsUri,places.primaryType,"
    "places.businessStatus,nextPageToken"
)
COLUMNS = [
    "business_name", "phone_number", "address", "has_website", "website",
    "instagram_url", "facebook_url",
    "google_maps_url", "category", "business_status", "place_id",
    "search_keywords", "collected_at",
]
HTTP_TIMEOUT = (5, 30)  # Connection and read seconds; certificate verification stays on.
MAX_RETRIES = 2
MAX_RETRY_DELAY = 30  # Never retry earlier than a longer Retry-After; stop that keyword.


def utc_now():
    return datetime.now(timezone.utc)


def atomic_write(path, write_contents, encoding="utf-8"):
    """Write beside the destination, then replace; failed writes leave it intact."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="",
                dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            write_contents(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def read_usage(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_json_object)
        if not isinstance(data, dict):
            raise ValueError()
        for month, count in data.items():
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) or type(count) is not int or count < 0:
                raise ValueError()
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        raise RuntimeError("Cannot read valid usage.json. Restore/repair its UTC month counters; do not delete it to reset usage.") from None


def reserve_request(usage_path, counters, run_limit, monthly_limit):
    month = utc_now().strftime("%Y-%m")
    if counters["attempts"] >= run_limit:
        raise RuntimeError("Per-run request limit reached; remaining keywords are incomplete.")
    if counters["usage"].get(month, 0) >= monthly_limit:
        raise RuntimeError("Local monthly request limit reached; no further request sent.")
    updated = {**counters["usage"], month: counters["usage"].get(month, 0) + 1}
    try:
        atomic_write(usage_path, lambda handle: json.dump(updated, handle, indent=2))
    except OSError:
        raise RuntimeError("Cannot update usage.json. Check folder permissions/free space and restore writable accounting before running again.") from None
    counters["usage"] = updated
    counters["attempts"] += 1  # Reservation persists before HTTP, even if interrupted next.


def retry_delay(value, attempt):
    delay = float(2 ** attempt)
    if value:
        try:
            seconds = float(value)
            if not math.isfinite(seconds):
                raise ValueError()
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(value)
                seconds = (date.replace(tzinfo=date.tzinfo or timezone.utc) - utc_now()).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 0
        delay = max(delay, seconds)
    if delay > MAX_RETRY_DELAY:
        raise ValueError("Required Retry-After delay exceeds 30 seconds; keyword stopped without retrying early.")
    return delay


def configuration_error(response):
    """Inspect error hints but return static messages only; never echo provider data."""
    try:
        error = response.json().get("error", {})
        hint = json.dumps(error).lower()
    except (ValueError, TypeError, AttributeError):
        hint = ""
    if any(word in hint for word in ("billing", "payment")):
        return "Google billing is unavailable. Enable/check billing for the API key's project."
    if any(word in hint for word in ("service_disabled", "accessnotconfigured", "has not been used", "is disabled")):
        return "Places API (New) is disabled. Enable it in the key's Google Cloud project."
    if any(word in hint for word in ("api_key", "api key", "referer", "ip_address_blocked")):
        return "API key invalid or restricted. Check PLACES_API_KEY and API/application restrictions for this Windows client."
    if response.status_code in (401, 403):
        return "Google denied access. Check the key/restrictions, Places API (New) enablement and project billing."
    return None


def request_page(session, key, payload, usage_path, counters, run_limit, monthly_limit):
    for attempt in range(MAX_RETRIES + 1):
        reserve_request(usage_path, counters, run_limit, monthly_limit)
        try:
            response = session.post(SEARCH_URL, json=payload,
                headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK},
                timeout=HTTP_TIMEOUT, allow_redirects=False)
        except (requests.exceptions.InvalidHeader, requests.exceptions.InvalidURL):
            raise RuntimeError("Invalid API key/header or request configuration. Check .env; no automatic retry.") from None
        except requests.RequestException:
            if attempt == MAX_RETRIES:
                raise ValueError("Network/TLS connection failed after bounded retries. Check internet, firewall/proxy and system certificates.") from None
            time.sleep(retry_delay(None, attempt))
            continue
        with response:
            if response.status_code == 200:
                try:
                    body = response.json()
                    if not isinstance(body, dict) or not isinstance(body.get("places", []), list):
                        raise ValueError()
                    if any(not isinstance(place, dict) for place in body.get("places", [])):
                        raise ValueError()
                    if not isinstance(body.get("nextPageToken", ""), str):
                        raise ValueError()
                    return body
                except (ValueError, TypeError):
                    raise ValueError("Google returned an invalid search page; keyword stopped.") from None
            error = configuration_error(response)
            if error:
                raise RuntimeError(error)
            transient = response.status_code == 429 or 500 <= response.status_code <= 599
            if not transient:
                raise ValueError(f"Google HTTP {response.status_code}: check this keyword/request configuration; no retry.")
            if attempt == MAX_RETRIES:
                if response.status_code == 429:
                    raise RuntimeError("Google quota/rate limit exhausted after retries. Check project quotas and usage before restarting.")
                raise ValueError("Google server failure remained after two retries; keyword stopped.")
            delay = retry_delay(response.headers.get("Retry-After"), attempt)
        time.sleep(delay)


def text(value):
    return value if isinstance(value, str) else ""


def merge_places(records, places, keyword):
    observed = utc_now().isoformat()
    for place in places:
        place_id = text(place.get("id")).strip()
        if not place_id:
            print("Warning: skipped a result missing its required place_id.")
            continue
        name = place.get("displayName")
        website = text(place.get("websiteUri"))
        instagram_url, facebook_url = "", ""
        if not website.strip():
            website = ""
        try:
            hostname = (urlparse(website).hostname or "").lower()
        except ValueError:
            hostname = ""
        if hostname == "instagram.com" or hostname.endswith(".instagram.com"):
            instagram_url, website = website, ""
        elif hostname == "facebook.com" or hostname.endswith(".facebook.com"):
            facebook_url, website = website, ""
        incoming = {
            "business_name": text(name.get("text")) if isinstance(name, dict) else "",
            "phone_number": text(place.get("internationalPhoneNumber")) or text(place.get("nationalPhoneNumber")),
            "address": text(place.get("formattedAddress")), "website": website,
            "instagram_url": instagram_url, "facebook_url": facebook_url,
            "google_maps_url": text(place.get("googleMapsUri")), "category": text(place.get("primaryType")),
            "business_status": text(place.get("businessStatus")), "place_id": place_id,
        }
        if place_id not in records:
            records[place_id] = {**incoming, "search_keywords": []}
        row = records[place_id]
        # Preserve the first non-empty observation of each field, filling earlier gaps.
        for field, value in incoming.items():
            if not row[field] and value:
                row[field] = value
        if keyword not in row["search_keywords"]:
            row["search_keywords"].append(keyword)
        row["has_website"] = "TRUE" if row["website"] else "FALSE"
        row["collected_at"] = observed


def safe_cell(value):
    value = str(value)
    # Inspect beyond whitespace/control/format characters without altering readability.
    visible = value
    while visible and (visible[0].isspace() or unicodedata.category(visible[0]).startswith("C")):
        visible = visible[1:]
    if visible.startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")):
        return "'" + value
    return value


def save_csv(path, records):
    def write(handle):
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in records.values():
            values = {**row, "search_keywords": " | ".join(row["search_keywords"])}
            writer.writerow({column: safe_cell(values[column]) for column in COLUMNS})
    try:
        atomic_write(path, write, encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise RuntimeError("Cannot replace CSV. Close it in Excel, check the output directory/permissions/free space, then rerun. Previous valid CSV is preserved.") from None


def collect(key, keywords, output_path, usage_path, max_results, run_limit, monthly_limit):
    """Sequential collection. Tests pass a fake key and mock HTTP; no .env read here."""
    records, outcomes = {}, {}
    counters = {"attempts": 0, "usage": {}}
    saved_count, saved_with_website, successful_pages = 0, 0, 0
    stopped, usage_readable = False, False
    try:
        counters["usage"] = read_usage(usage_path)
        usage_readable = True
        if not key or not key.strip():
            raise RuntimeError("Missing PLACES_API_KEY. Set it in .env beside main.py; enable Places API (New) and billing.")
        if any(type(limit) is not int or limit < 0 for limit in (max_results, run_limit, monthly_limit)) or max_results == 0:
            raise RuntimeError("Result limit must be positive; request limits must be non-negative integers.")
        limit = min(max_results, 60)  # Currently documented API total maximum, not a coverage guarantee.
        with requests.Session() as session:
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            for keyword in keywords:
                if not isinstance(keyword, str) or not keyword.strip():
                    raise RuntimeError("Keywords must be non-empty strings in main().")
                if keyword in outcomes:
                    continue
                page_number, returned, seen_tokens = 1, 0, set()
                payload = {"textQuery": keyword, "pageSize": min(20, limit)}
                outcomes[keyword] = "incomplete: interrupted"
                while True:
                    print(f"Keyword: {keyword} | page {page_number} | requests attempted: {counters['attempts']}")
                    try:
                        body = request_page(session, key, payload, usage_path, counters, run_limit, monthly_limit)
                    except ValueError as exc:
                        outcomes[keyword] = "failed: " + str(exc)
                        print(outcomes[keyword])
                        break
                    places = body.get("places", [])
                    merge_places(records, places[:limit-returned], keyword)
                    successful_pages += 1
                    save_csv(output_path, records)
                    saved_count = len(records)
                    saved_with_website = sum(row["has_website"] == "TRUE" for row in records.values())
                    returned += min(len(places), limit-returned)
                    print(f"Page returned: {len(places)} | unique businesses: {len(records)} | requests attempted: {counters['attempts']}")
                    token = body.get("nextPageToken", "")
                    if not token:
                        outcomes[keyword] = "complete: API returned no further pages"
                        break
                    if returned >= limit:
                        outcomes[keyword] = "incomplete: configured/API result limit reached"
                        break
                    if token in seen_tokens:
                        outcomes[keyword] = "failed: repeated pagination token; stopped"
                        break
                    seen_tokens.add(token)
                    # Retain textQuery/pageSize and every original parameter; only token changes.
                    payload = {**payload, "pageToken": token}
                    page_number += 1
    except KeyboardInterrupt:
        print("Interrupted. Previously saved pages remain available.")
        stopped = True
        if successful_pages:
            try:
                save_csv(output_path, records)
                saved_count = len(records)
                saved_with_website = sum(row["has_website"] == "TRUE" for row in records.values())
            except RuntimeError as exc:
                print(str(exc))
    except RuntimeError as exc:
        print("Stopped: " + str(exc))
        for keyword, outcome in outcomes.items():
            if outcome == "incomplete: interrupted":
                outcomes[keyword] = "incomplete: " + str(exc)
        stopped = True
    month = utc_now().strftime("%Y-%m")
    for keyword in keywords:
        if isinstance(keyword, str):
            outcomes.setdefault(keyword, "incomplete: not started")
    # Count the last successfully written snapshot, not merely fetched rows.
    print(f"\nUnique businesses saved this run: {saved_count}")
    print(f"With / without returned website: {saved_with_website} / {saved_count-saved_with_website}")
    print(f"Requests attempted this run: {counters['attempts']}")
    print(f"Local monthly requests ({month} UTC): {counters['usage'].get(month, 0) if usage_readable else 'unknown (usage file unreadable)'}")
    print(f"Output: {output_path}")
    if not successful_pages:
        print("No page succeeded; any existing CSV was left unchanged.")
    for keyword, outcome in outcomes.items():
        print(f"{keyword}: {outcome}")
    return 1 if stopped or any(not value.startswith("complete:") for value in outcomes.values()) else 0


def main():
    keywords = [
        "nail salon Yerevan",
        "beauty salon Yerevan",
    ]
    output_file = "businesses.csv"
    max_results_per_keyword = 60
    max_requests_per_run = 10
    max_requests_per_month = 100

    try:
        load_dotenv(BASE_DIR / ".env", override=False)
    except (OSError, UnicodeError):
        print("Cannot load .env. Check its permissions and UTF-8 encoding; file contents were not logged.")
        return 1
    return collect(os.getenv("PLACES_API_KEY", ""), keywords,
        BASE_DIR / output_file, BASE_DIR / "usage.json", max_results_per_keyword,
        max_requests_per_run, max_requests_per_month)


if __name__ == "__main__":
    raise SystemExit(main())
