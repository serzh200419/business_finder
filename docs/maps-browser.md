# Google Maps browser adapter: implemented, persistent runs blocked

`maps_browser` is a concrete sequential Playwright adapter in `app/maps_browser.py`, selected through the existing `DiscoveryProvider` factory. Its source identity is **google_maps_browser**; it does not disguise Google as a licensed or fixture source. SQLite, the existing direct execution routes and checkpoint tables remain unchanged. No paid API, internal Maps endpoint, stealth plugin, CAPTCHA solver, proxy rotation or external worker is used.

**Current blocker:** `app/services.py:save_page` already rejects every Google-tagged provider from persistent business/evidence storage. This rule is retained and explicitly extended to the `maps_browser` campaign selection. `source_preflight()` stops the job before launching a browser or saving listing URLs/cursors. Job details show `source_handling_restricted`, zero searches/visits, and the exact restriction. Setting `license_reference`, `persistence_permitted=true`, or `export_permitted=true` cannot override it. Browser access itself grants no storage/export rights. No Maps persistence/export override is available in `.env` or the form. A separately reviewed, explicit source-policy change would be needed before persistent collection can run; alternatively use independently permitted CSV/licensed data.

## Necessary installation and normal startup

From the repository directory, after the usual first launcher setup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-local.ps1 -CheckOnly
.\.venv\Scripts\python -m pip install -e '.[maps]'
.\.venv\Scripts\python -m playwright install chromium
.\start-local.ps1
```

Playwright is an optional dependency; other local workflows do not require it. Its [browser installation command](https://playwright.dev/python/docs/browsers) installs a matching Chromium build. The provider uses a [persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context) in **this repository's `.local/maps-browser-profile`**, never your personal Chrome/Edge profile. That directory is excluded from Git/builds and may contain cookies/cache: keep it private. No screenshots, page dumps, HAR recordings or business detail logs are created by the adapter. Visible browser is default; manual normal consent can be completed during the bounded wait. Challenges halt, with no bypass. Closing the browser stops the run; explicit resume is required.

## Exact 30-candidate configuration and current outcome

UI: create a campaign with niche `nail salon`, city `Yerevan`, English keyword `nail salon`, select **Google Maps browser**, leave `maps_listing_limit=30` (default), and click **Run discovery directly**. The UI currently reports the policy block and saves no candidate data. It cannot presently collect 30 persisted leads; installing a browser does not resolve the source-policy gate.

The equivalent bounded campaign is `examples/maps-campaign.json`:

```powershell
.\.venv\Scripts\python -m app.cli create examples/maps-campaign.json
# Substitute the printed campaign ID:
.\.venv\Scripts\python -m app.cli discover 1
.\.venv\Scripts\python -m app.cli jobs 1
```

These commands currently produce a **failed, policy-blocked job**, not live candidates. After an independently reviewed policy resolution, the same adapter/selector/checkpoint integration would be available; that future outcome has not been verified. The example deliberately declares neither storage nor export permission.

## Browser behavior and accounting

The adapter opens URL-encoded public Maps search URLs with English UI language, recognizes results/feed, single-place, explicit no-results, consent or challenge states, and scopes listing links to the results panel. It scrolls sequentially and deduplicates by identity. Collection checkpoints advance each scroll round. Stop reasons include listing limit, explicit end, bounded no-progress and scroll-round limit. An unrecognized/empty results panel is an explicit error, not fabricated zero results. Listings are then visited sequentially, one per atomic provider page; a later browser failure leaves earlier committed checkpoints available to resume. No fixture fallback exists.

Default: 30 listing links per query, one visible dedicated browser session per run. Configuration fields on `ProviderConfig`:

| Field | Default | Meaning |
|---|---:|---|
| `maps_listing_limit` | 30 | Maximum collected listing identities per query, 1–100 |
| `maps_headless` | false | Visible by default; consent requires normal visible interaction |
| `maps_navigation_timeout` | 15 | Seconds per navigation, max 25 |
| `maps_operation_timeout` | 45 | Seconds per provider checkpoint step, max 60 |
| `maps_no_progress_rounds` | 3 | Bounded rounds without new identities |
| `maps_scroll_round_limit` | 30 | Maximum scroll/collection rounds |
| `maps_search_budget` | 10 | Job-wide search navigation attempts, including reopened searches |
| `maps_visit_budget` | 100 | Job-wide listing navigation attempts, including failed/replayed visits |

Campaign `request_budget`/`requests_used` retain the existing runner's **checkpoint-step accounting**, not an API request count for this provider. A collection round or one listing attempt uses one checkpoint step. Actual search and listing navigation attempts are separately reserved and persisted before navigation; re-opening/replaying is charged again, while scrolling is not a new search. Progress includes `collected`, `stage`, `visit_index`, navigation counters and structural per-listing failures. Candidate budget still caps distinct persisted business identities, not collected links. UI-created Maps campaigns receive 200 step units; the one-query example uses 100 step units, one search, 30 visits and a 30-candidate budget. Resume preserves original job configuration; do not edit limits and expect an existing cursor to change. Current blocked runs consume zero steps/navigation attempts and persist no source content.

Every checkpoint commits through the existing cancellation/lease/fencing/replay machinery; browser waits observe cancellation and transactions renew the lease via progress callbacks. Keep operation timeouts below your configured lease duration. The existing local UI rejects overlapping run clicks; synchronous routes run off the event loop, so health/review can be opened in another tab. There is no additional queue or automatic restart.

## Identity, fields and manual website review

Identity is `google_maps_browser` plus a reliable CID from the public listing URL (`cid:` decimal, including the second hex token in displayed listing URLs), or a public `query_place_id`/place-ID URL token when a CID is unavailable. Canonical URLs remove view/tracking parameters. A link without either identity is explicitly rejected; names and result positions are never IDs. Separate branch CIDs remain separate businesses. No undocumented endpoint is called. CID and place-ID representations are distinct; automatic cross-representation merging is not implemented.

The parser returns displayed name, category, address, raw/normalized phone, listed website, Maps URL/identifier, optional rating/review count/closed status, query and UTC observation time. Missing optional fields are null. District, email, owner, WhatsApp and social profiles not actually listed are not inferred. City/niche are campaign context, not independently confirmed location/category: review the observed address/category. Districts influence query wording only; hard geographical district filtering is not implemented. Website URLs are classified as candidate business website, social profile, directory, booking profile or unknown. Classification does not verify business identity. A missing button means **no website listed on Maps**, never verified `no_website_found`/`social_only`.

For eligible existing CSV/licensed records, open **Needs website check**. Unchecked/uncertain/unavailable candidates are ordered by valid public phone and missing/social listed link, separate from the assessed no-website prospect queue. Open a business to use clickable name + Yerevan and phone search links and its source listing. Compare name/address/phone across sources. Use **Contacts, evidence and manual website correction**, recording your reason and operator label. The audited correction can then support assessed prospect review. Discovery and manual review do not need automated search configuration. Missing search configuration still blocks automated real enrichment. Export continues to check per-field rights, retention and suppression; manual checking does not grant rights to restricted Google data.

## Live UI inspection and opt-in smoke

No Playwright package/browser is installed in the task environment. Available page inspection of `https://www.google.com/maps?hl=en` returned the JavaScript-only placeholder, not an executable Maps UI. Therefore **current live selectors were not verified**. `app/maps_selectors.py` centralizes semantic locators and provisional field fallbacks; the HTML fixtures are synthetic examples, not captured live UI. Unknown selector states fail explicitly. Interactive current-DOM inspection is still required on a permitted run before claiming this provider works against live Maps.

```powershell
.\.venv\Scripts\python -m app.maps_smoke --live --config examples/maps-campaign.json
```

The smoke is fixed to **nail salon Yerevan**, maximum **5 listings**, bounded scroll rounds/navigation/operation times, and prints only counts/static error codes. It preflights configuration/source handling before any browser/network work. Under the retained policy it reports **NOT RUN**, even with Playwright installed. Its transient parser branch is unreachable while that policy blocks source handling; no restricted data is exported or saved to make a test pass.

Run deterministic verification with `.\.venv\Scripts\python -m pytest -q`. Parsing tests cover normal listing, missing phone/website, social listed link, distinct branches, consent/challenge, explicit no-results and changed markup. Scripted browser tests cover deduplication, incremental collection, resume/replay, bounded no-progress, budgets and cancellation. These do not prove live scraping works.

Observed results for this change: **130 tests passed** with one existing TestClient deprecation warning; Python compilation and whitespace checks passed. The opt-in smoke reported **NOT RUN** with the missing Playwright package and retained Google-source storage policy as blockers. No real listings were fetched, persisted or exported, and no current executable Maps UI was inspected.
