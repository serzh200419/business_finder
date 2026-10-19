
# Places → CSV

One small Windows/Python script. Edit keywords and limits inside `main()` in `main.py`, then run it. Your existing `.env` is preserved. No browser, database, server or worker is needed. Python 3.10+.

## Setup and run (PowerShell)

From this repository's directory:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# Create only if missing; never overwrite your existing key:
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
# Set PLACES_API_KEY=your_key locally; do not paste/share the value.
notepad main.py
python main.py
# Offline tests; no key, .env access, billing or network needed:
python -m unittest -v
```

If activation is blocked, use `.\.venv\Scripts\python.exe` instead of `python`; activation is optional. Enable **Places API (New)** and billing in the key's Google Cloud project, with API/application restrictions appropriate for your Windows client. Existing process environment variables take precedence over `.env`.

Defaults in `main()`: `nail salon Yerevan`, `beauty salon Yerevan`; `businesses.csv`; 60 results per keyword; 10 attempted requests per run; 100 per UTC calendar month. Edit these directly. `.env`, output and `usage.json` resolve beside `main.py`, even when launched from another directory. Run only **one instance at a time**, using this same usage file.

## Output

CSV columns, in order:

```text
business_name,phone_number,address,has_website,website,instagram_url,facebook_url,google_maps_url,category,business_status,place_id,search_keywords,collected_at
```

`websiteUri` is classified by its parsed hostname, case-insensitively. Exact `instagram.com` / `facebook.com` hosts and their subdomains go into `instagram_url` / `facebook_url`, preserving the original URL and leaving `website` empty. Other non-empty URLs go into `website`; paths containing social names and lookalikes such as `fakeinstagram.com` remain websites. Missing URLs leave all three columns empty.

`has_website` is uppercase `TRUE` only when `website` is non-empty; social-only URLs produce `FALSE`. It does not verify a working/matching website or prove absence elsewhere. Missing optional values stay empty. Deduplication uses only `place_id`, preserving branches and all matching keywords separated by ` | `. The first non-empty field observation is retained, so repeated observations can fill both website and social columns; `collected_at` is the latest observation of that ID in UTC.

Every successful page atomically replaces the CSV with this run's accumulated collection. The first successful page replaces earlier runs; a successful empty search writes a header-only CSV. If no page succeeds, the previous CSV stays untouched. Close the file in Excel before running: a failed replacement stops collection and preserves the previous valid CSV. Ctrl+C retains saved pages. Reruns start fresh rather than resuming.

Excel-compatible UTF-8 BOM preserves Armenian/Russian text. Formula-like cells, including leading `+` phone numbers and dangerous prefixes after whitespace/control characters, receive a literal apostrophe as a text escape. Phones remain strings, not formulas; some CSV viewers show that apostrophe. Take care when editing/re-exporting through spreadsheet tools, which can alter escaping.

## Limits, pricing and source terms

[Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search) currently documents up to 20 results per page and 60 across pages (subject to change). Results are limited, can vary, and need not match the Maps website or exhaust all Yerevan businesses. Pagination preserves original parameters; repeated tokens stop the keyword. The script clamps larger configured result limits to 60. Completion reports distinguish no further API pages from configured limits/errors.

Phone/website fields select **Text Search Enterprise** pricing. Checked **2026-10-16**, the [official global pricing list](https://developers.google.com/maps/billing-and-pricing/pricing) lists **1,000 free monthly events** for that SKU, then **US$35 per 1,000** in the first paid tier. This is not a permanently free service; account eligibility, other usage, regional pricing and future changes matter. Verify your applicable pricing before running.

`usage.json` stores attempted requests under UTC `YYYY-MM` keys, including pages, retries and failed/interrupted attempts. Each increment is atomically persisted **before** HTTP. Corrupt/unwritable accounting stops requests; repair or restore the file rather than deleting it. The counter covers only this script/file and cannot guarantee zero charges if your key/account is used elsewhere, the file is deleted, or pricing changes. Google budget alerts are notifications, **not a hard spending cap**; review Cloud quotas too.

Connection/read timeouts are 5/30 seconds. Network failures, 429 and 5xx receive at most two retries with bounded backoff; Retry-After seconds/dates are honored. Delays above 30 seconds stop that keyword rather than retrying early. Authentication/configuration errors are not retried. Global problems/limits stop the run; keyword-specific failures retain results and allow later keywords to continue. Summaries show counts, output path and each keyword's outcome. Exit code 0 means all searches reached no further pages; 1 means a limit, error or interruption left work incomplete.

Read Google's [Places storage/attribution policies](https://developers.google.com/maps/documentation/places/web-service/policies) and applicable [service terms](https://cloud.google.com/maps-platform/terms/maps-service-terms). Places content storage/export is restricted; the place-ID exception does not grant unrestricted permanent storage of names, phones and addresses for prospecting. An API key or public visibility does not grant those rights. Confirm that your CSV use and retention are permitted; this script is not a policy engine.

Verification uses mocked HTTP and temporary files only. No live API request or validation of your actual key/billing has been performed.
