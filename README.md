# Yerevan business finder

A Python/FastAPI application for discovering and enriching Yerevan business candidates using synthetic fixtures and trusted CSV imports. Campaign CRUD, the server-rendered UI, SQLAlchemy 2, PostgreSQL, Alembic, Celery/Redis and working paginated discovery remain. Contact normalization and evidence-based website assessments add permitted public-page extraction, connection-time address protection, resumable enrichment checkpoints and audited manual review.

Public contact normalization and evidence-based website assessment now run through resumable enrichment jobs and a business review UI. Missing listing URLs remain unverified, and negative assessments mean only that configured checks found no website. Local review priority, selected-lead CSV export and manual outreach history are available. No paid calls, live provider adapter or outbound messaging is implemented. Localhost is the only supported deployment mode.

## Start locally without credentials

PowerShell, Python 3.11+:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'
$env:DATABASE_URL = 'sqlite:///./demo.db'
$env:DEMO_MODE = 'true'
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open http://localhost:8000; interactive API docs are at `/docs`. The initial UI creates basic fixture campaigns (dentist/cafe), runs discovery and links to plans, jobs and evidence. The CLI uses the same planner/providers/transactions as the API and works without a running HTTP server. SQLite supports local demonstrations and tests; it is not evidence of PostgreSQL runtime behavior. Package installation initially requires internet. Environment variables override `.env`.

On POSIX activate `.venv/bin/activate`, use `python`, and use `export DATABASE_URL=sqlite:///./demo.db`, `export DEMO_MODE=true` and `export PYTHONIOENCODING=utf-8`.

## Exact commands for planning, fixtures, CSV and resume

Run these in a second terminal with the same environment variables. Commands print IDs; substitute the printed campaign/job IDs where `1` appears below.

```powershell
# Complete deterministic demonstration: plan -> discovery -> partial failure -> resume -> summary
.\.venv\Scripts\python -m app.cli fixture-demo
# Compatibility entry point for the same demonstration
.\.venv\Scripts\python scripts/demo.py

# Create a manually controlled fixture campaign
.\.venv\Scripts\python -m app.cli create examples/fixture-campaign.json
# Preview only; no provider calls or budget consumption
.\.venv\Scripts\python -m app.cli plan 1
.\.venv\Scripts\python -m app.cli discover 1
# Inspect every job for the campaign, or one specific job
.\.venv\Scripts\python -m app.cli jobs 1
.\.venv\Scripts\python -m app.cli job 1
# Resume pending pages/retries, preserving cursor and completed queries
.\.venv\Scripts\python -m app.cli resume 1
# Explicitly reopen failed queries after addressing their cause
.\.venv\Scripts\python -m app.cli resume 1 --retry-failed
# Synthetic fixture only: disable its intentional fault and reopen failed queries
.\.venv\Scripts\python -m app.cli resume 1 --retry-failed --disable-fixture-failures

# Validate CSV without inserting any data (mapping and permission metadata are in the JSON)
.\.venv\Scripts\python -m app.cli csv-validate examples/csv-campaign.json
.\.venv\Scripts\python -m app.cli create examples/csv-campaign.json
# Replace 1 with the new CSV campaign ID. Repeat to demonstrate idempotent reimport.
.\.venv\Scripts\python -m app.cli csv-import 1
.\.venv\Scripts\python -m app.cli csv-import 1
```

The reliability fixture has four **synthetic** businesses with Armenian, Russian and English names, two distinct source identities with identical names, and branches sharing synthetic phone numbers. Four queries each return two pages; the same businesses appear across queries. One Armenian query fails transiently once; `permanent` deliberately fails permanently. The demo first reports partial failure, then explicitly reopens that query with fixture faults disabled. This is a controlled synthetic fault repair, not automatic recovery from a real permanent vendor error.

Fresh-database sample: partial summary has four accepted/new sources, six committed pages, three completed queries, one failed query and eight charged requests. Resumed summary has four accepted/new sources, eight committed pages, four completed queries, no failed/unfinished queries and ten charged requests. There are four businesses and sixteen query/business evidence associations. Running the demo again creates a separate campaign but reuses global source/business identity: the new campaign reports four existing sources and zero newly created sources.

The CSV example uses a column mapping, accepts two synthetic rows and reports one rejected row. A first import creates two sources; reimport reports two existing and zero newly created. Rejected rows are not secretly discarded: summary includes row numbers and errors. No phone is labelled as an owner contact or WhatsApp account.

## Search planner

Campaign fields include `niche`, `city`, `districts`, explicit `keywords` keyed by `hy`, `ru`, `en`, provider configuration, `request_budget`, `candidate_budget`, `query_limit`, optional `city_variants` and `district_variants`. No translation/LLM/API is required. An absent keyword dictionary falls back to the configured niche in English for compatibility with the initial UI.

```json
{
  "niche": "dentist",
  "city": "Yerevan",
  "districts": ["Kentron"],
  "keywords": {"hy": ["ատամնաբույժ"], "ru": ["стоматология"], "en": ["dentist"]},
  "city_variants": {"hy": ["Երևան"], "ru": ["Ереван"], "en": ["Yerevan"]},
  "district_variants": {"Kentron": {"hy": ["Կենտրոն"], "ru": ["Кентрон"]}},
  "query_limit": 20,
  "candidate_budget": 100,
  "request_budget": 50,
  "provider_config": {"discovery": "fixture", "page_size": 2}
}
```

Expansion is deterministic: language order hy/ru/en, configured keyword/city order, city-only then configured district variants. Unicode is NFC-normalized, whitespace collapsed, and identical casefolded query strings deduplicated. All contributing language/city/district variants are retained in query metadata even when strings collapse. `query_limit` caps unique planned queries; preview returns `truncated` when later unique queries are omitted. Query records remain attached to historical evidence when a campaign is edited. Jobs snapshot provider configuration, niche, city, district filters and query IDs. Edits while a campaign is leased are rejected; editing after partial completion does not rewrite that job's query snapshot.

Fixture/CSV providers declare `query_search=false`: they filter canonical niche/city/district and intentionally return overlapping results across keyword variants; they do not pretend to perform live text search. Canonical city/district values remain the matching criteria even when query text has translated variants.

## API

- Campaign CRUD: `POST /api/campaigns`, `GET /api/campaigns`, `GET/PUT/DELETE /api/campaigns/{id}`. PUT replaces the configurable fields; counters cannot be edited. Increase budgets with PUT to resume budget-stopped jobs.
- Plan dry run: `GET /api/campaigns/{id}/plan`.
- CSV dry run: `POST /api/csv/validate` with the `provider_config` object from the CSV example. Returns accepted/rejected row counts, errors, distinct candidates, existing sources and would-create count; no inserts.
- Discovery: `POST /api/campaigns/{id}/discover`. Continues an unfinished job or creates a new one after completion. A repeated completed run reuses source/evidence identity.
- Persist a pending job: `POST /api/campaigns/{id}/jobs`; inspect with `GET /api/jobs/{job_id}` or `GET /api/campaigns/{id}/jobs`.
- Resume synchronously: `POST /api/jobs/{job_id}/resume` with `{}` or `{"retry_failed":true}`. Fixture-only repair additionally uses `"disable_fixture_failures":true`. Cancellation is reversible through explicit resume.
- Cancel: `POST /api/jobs/{job_id}/cancel`. Workers check cancellation between batches; an already fetched page may commit before cancellation stops the next batch.
- Queue discovery/resume via Celery: `POST /api/campaigns/{id}/enqueue`. The pending Job is saved before publishing; broker failure returns 503 and leaves it available for local/manual recovery. Retry-due queries are scheduled from their saved deadlines.
- Local evidence review: `GET /api/campaigns/{id}/leads`, including query/source IDs, source rights, review flags and reasons. Selected export is documented in the local review workflow below.
- `/health` checks database connectivity and returns 503 on failure. Redis has its own Compose health check.

## Budgets, retries, resumability and identity

**Request budget:** campaign-lifetime, not per invocation/job. Every attempted `discover_page` call consumes one unit before calling the provider. Retries, errors, empty pages, replay and calls interrupted by a crash all consume requests. Dry-run planning/CSV validation and local database operations consume none. Adapter-internal HTTP retries or hidden page fetches are forbidden because they would bypass accounting. Reserve budget before work; exhaustion leaves pending queries and a partial job until an operator increases the budget.

**Candidate budget:** distinct persisted canonical businesses evidenced in that campaign, across all its jobs. New provider identities have separate canonical businesses. Duplicate candidates, page replay, CSV reimport and additional query associations do not consume additional slots. Restricted results and rejected CSV rows do not consume candidate slots. Page size is capped by remaining capacity before calling the provider. At zero remaining capacity, no additional page requests occur, even if the next page might contain only duplicates; increase the budget to finish all associations. An adapter returning more than the reserved page size causes a permanent contract error and the entire page is discarded. Existing legacy merged businesses count once pending manual review.

**Retries:** `max_retries` (default two) is additional attempts per page beyond the initial call. Retryable errors persist failure count and next-attempt deadline. Delay uses `min(retry_cap_seconds, retry_base_seconds * 2^(failures-1))` with multiplicative jitter in [1,2], capped again. A valid numeric or HTTP-date Retry-After is a minimum delay and can exceed the cap; invalid values are ignored. Successful page commit resets the per-page failure count. Permanent errors and exhausted retries mark that query failed and do not automatically reopen it. Explicit `--retry-failed` gives a repaired query a new bounded retry cycle; request budget still spans every cycle. Synchronous runs return partial while deadlines are in the future; rerun/resume when due. Celery schedules the next due execution without sleeping a worker. Calls interrupted by a process crash have unknown outcome and are charged again on recovery, bounded by campaign request budget.

**Checkpoints and leases:** JobQuery records hold query IDs, status, opaque continuation cursor, attempts, page counts, retry deadline and dataset fingerprint. Each page transaction atomically saves businesses/sources, evidence associations, derived contact/assessment rows and cursor advancement. Database unique constraints and PostgreSQL/SQLite `ON CONFLICT` insertion prevent duplication on replay. No page is saved halfway. Campaign-scoped leases serialize all queries/jobs for that campaign, which is stronger than excluding workers only per query. `DISCOVERY_LEASE_SECONDS` defaults to 120; each short transaction renews it. Lease tokens fence page commits: an expired/replaced worker cannot write results or advance a cursor. Provider I/O must finish within the lease; future adapters need explicit timeouts. Expired abandoned leases can be reclaimed by a subsequent worker/resume. Lease scheduling assumes reasonably synchronized worker clocks. There is no periodic sweeper; broker redelivery or an operator's resume triggers recovery.

**States:** pending before execution; running while leased; completed only when every planned query is completed; partial for mixed results, budgets, batch limits or pending retries; failed when every query failed; cancelled after cancellation is applied. Completed pages are not reprocessed on resume. One invocation processes at most 100 attempted pages; unfinished work remains partial and Celery schedules continuation. Unexpected application errors preserve checkpoints and remain manually resumable rather than being represented as successful discovery.

**Identity:** `(provider, external_id)` is globally unique for BusinessSource. New Business identity is a length-safe hash of that pair, never of names or phones. Different providers or branches are separate businesses unless later human review decides otherwise. Exact same-name records under distinct source IDs are flagged for review; changed name/city/district under an existing ID also flags review and retains canonical fields. Cross-source fuzzy matching is not implemented. Evidence uniqueness is campaign + planned query + provider identity + external ID. All query/business associations survive reruns; reimport does not append duplicate evidence, contacts or assessments. Existing evidence export rights can only become more restrictive on reimport. This is discovery observation identity, not a versioned change-history store.

The incremental migration retains old data and flags legacy name-based businesses for review, because earlier merges cannot be automatically undone. It refuses ambiguous duplicate legacy provider/external IDs instead of silently choosing a business; resolve those source rows before upgrading. Old uncheckpointed pending/running jobs become failed; start new discovery rather than resuming nonexistent checkpoints. Legacy evidence is retained separately with nullable new keys; subsequent query-aware observations use new stable keys. Downgrade removes this stage's checkpoint data.

## CSV identity, validation and rights

`examples/csv-campaign.json` shows explicit canonical-to-input column mapping. Required canonical columns are `external_id,name,city,niche,source_url`; optional columns are `district,website,phone`. Files are read as UTF-8 or UTF-8 BOM. Blank/missing fields, invalid provenance URLs, extra/missing columns and conflicting external IDs produce row-numbered errors. Unreadable files, invalid UTF-8, missing/duplicate headers and unrecoverable CSV syntax errors fail validation as a whole. Identical duplicate rows collapse to one candidate; conflicting rows sharing an ID are all rejected for review.

CSV identity is `csv:{provider_namespace}` plus the explicitly supplied `external_id`. Use a stable dataset namespace and stable branch/business IDs across exports, file renames, row reordering and reimports. Never use row numbers, names, phones or transient filenames as IDs. Different datasets must use different namespaces; the default namespace is suitable only for one controlled dataset. CSV provenance comes from each row's explicit HTTP(S) source URL. Neither filename nor a shared phone proves identity or ownership.

Persistence requires an explicit license reference, `persistence_permitted=true`, and retention policy. Export defaults to false and is never inferred. Dry-run validation also requires this declared import configuration, but creates no data. During import, effective permissions are the intersection of provider result and campaign declaration. Retention policy and permission flags are attached to every Evidence record. Arbitrary provider exception messages are not stored, to avoid accidentally persisting restricted provider content. File errors retain structural row numbers/reasons rather than business field contents.

Snapshot fingerprints are SHA-256 of file bytes; opaque cursors incorporate that fingerprint. Changing a file after a saved page is a permanent query failure, preventing cursor drift and silent omission. Cancel the unfinished job and start a new run for a changed dataset; reuse the same namespace/IDs. Files must be immutable during a run and shared at the same path across web/worker containers. Validation currently loads the CSV in memory, with no large-file streaming or upload UI. CSV paths are trusted server-local filesystem access.

Google Places is unsupported as an outreach database provider; no Google adapter or browser scraping exists, and Google-tagged results are denied persistence. Do not assume Maps content is permanently storable/exportable; consult [official Places policies](https://developers.google.com/maps/documentation/places/web-service/policies). Permission declarations are operator-supplied and their legal validity is not automatically verified. Retention expiry and revocation are enforced by the operational eligibility gate; declarations and cleanup schedules still require operator review before live data use.

## Docker, development and verification

```powershell
Copy-Item .env.example .env
# In a new terminal, remove local demo overrides if set, so .env governs local Python.
docker compose up --build -d
# Compose performs the incremental migration before web/worker startup.
docker compose logs migrate web worker
docker compose exec web python -m app.cli fixture-demo
docker compose exec web python -m app.cli csv-validate examples/csv-campaign.json
docker compose down

.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m alembic check
.\.venv\Scripts\python -m alembic current
.\.venv\Scripts\python -m celery -A app.tasks.celery_app worker --pool=solo --loglevel=info
```

PostgreSQL uses a named volume retained by `down`; web/database ports bind to localhost. Default credentials are for development. Set `POSTGRES_PASSWORD` before initial database startup (URL-encode special characters in URLs). `.env.example` uses localhost for local Python, whereas Compose overrides DB/Redis hosts internally. Linux worker containers use the default pool; native Windows Celery uses `solo`.

Tests run real Alembic upgrade/downgrade/re-upgrade on temporary SQLite databases, check metadata drift and legacy ambiguity, exercise Unicode plans, overlap, replay, rollback, restart, threaded duplicate-worker exclusion, expired-lease fencing, budgets, retries/Retry-After, cancellation, malformed CSV/BOM/mapping/reimport and permission narrowing. PostgreSQL offline SQL compilation checks DDL generation only. No eager-mode test is presented as real Celery integration. In the current environment Docker, psql and redis-server are unavailable; PostgreSQL locking/upserts, real Redis delivery, Celery countdown scheduling/worker redelivery and Compose startup remain unverified.

## Before adding a live provider

Read [the provider contract](docs/provider-contract.md). Choose a licensed data source and document permitted discovery, persistence, contact use, export, retention and revocation. Decide stable provider/branch IDs, page limits, cursor expiry/replay guarantees, rate limits, query-language/district support, and paid-request accounting. Set I/O timeouts below lease duration; confirm clock synchronization. Run PostgreSQL/Redis/Celery integration tests before trusting production concurrency. Decide manual entity resolution, retention deletion, audit/history policy and operational recovery alerts.

Authentication, authorization and production access controls remain absent; retain localhost bindings. CSRF protection applies to mutation forms; host/origin restrictions apply to local operations. The review dashboard is paginated. Retention cleanup is an explicit operator command, not a background scheduler. Broker publication is not protected by a transactional outbox; a saved pending job can require manual recovery. Leases recover on redelivery/resume, not on a background scanner. Live search requires a licensed API adapter; JavaScript pages/access challenges remain unsupported. Web rate/concurrency limits are per process, and need distributed coordination before live multi-worker deployment. SQLite/offline/mock checks do not verify PostgreSQL/Redis/Celery or live TLS/DNS integration.

## Contact enrichment and website assessment

Apply the incremental migration before running these commands. Existing discovery remains compatible: historical listing assessments are retained, while business detail presents them as `not_checked` until enrichment runs. Raw values already discarded by older discovery cannot be reconstructed.

```powershell
.\.venv\Scripts\python -m alembic upgrade head
# Complete synthetic demo: checkpoint -> resume -> ten assessments -> duplicate-free rerun
.\.venv\Scripts\python -m app.cli assessment-demo
# Equivalent script entry point
.\.venv\Scripts\python scripts/assessment_demo.py

# Enrich an imported or discovered fixture campaign; replace 1 with its printed ID.
.\.venv\Scripts\python -m app.cli enrich 1 --options examples/enrichment-options.json
# Inspect check errors, evidence, contacts and normalized values
.\.venv\Scripts\python -m app.cli job 1
.\.venv\Scripts\python -m app.cli business 1
# Resume an interrupted/retry-due job, optionally increasing its separate job request budget.
.\.venv\Scripts\python -m app.cli enrichment-resume 1 --request-budget 200
# Campaign budget must also have capacity: increase it using campaign PUT if exhausted.

.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
# Review list: http://localhost:8000/businesses
# Sources, candidate URLs, status, checks and correction audit: /businesses/{business_id}

.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m alembic check
.\.venv\Scripts\python -m compileall -q app scripts
git diff --check
```

API: `POST /api/campaigns/{id}/enrich` with EnrichmentOptions; `/enrich/enqueue` queues the saved job. `GET /api/jobs/{job_id}` and `GET /api/businesses/{business_id}` inspect progress/evidence. `POST /api/enrichment/jobs/{job_id}/resume` accepts `{}` or `{"request_budget":200}`. Existing generic cancellation/resume supports enrichment jobs. Manual corrections use `POST /api/businesses/{id}/assessment/correct` with `status`, `reason`, `actor`, or the detail-page form. Actor is an operator-provided audit label, not authenticated identity. Background reruns preserve manual corrections.

Default enrichment normalizes persisted contacts but has `search_provider="unavailable"`: unconfigured search yields uncertain, never a false negative. The complete assessment demo explicitly uses deterministic fixture search and HTTPX MockTransport. All ten businesses, contacts and HTML pages are synthetic; no external network requests or credentials are needed. It resumes from a three-step checkpoint, completes with 34 counted requests, then asserts zero duplicate contacts/evidence on rerun.

| Synthetic case | Outcome |
| --- | --- |
| No listed URL; matching search website | website_found |
| Instagram listing; complete checks | social_only |
| Similar name, different phone/address | uncertain |
| Matching hosted wordpress.com subdomain | website_found |
| Matching cross-domain permitted redirect | website_found |
| Explicitly attested matching site unavailable | website_unavailable |
| Required search failure | uncertain |
| Completed empty checks; invalid Armenian phone retained | no_website_found |
| Two branches sharing a phone, matching street addresses | website_found separately |

Contacts retain raw input, validation results, E.164 for valid AM numbers, email local-part case/plus tags, normalized domains/social URLs, source URL, observed_at and verification status. Syntax does not establish identity, deliverability, ownership or WhatsApp availability. Identity-matched pages contribute tel/mailto/JSON-LD and explicitly related website/social links; optional same-origin contact/about pages have a small budget. Full page HTML remains in memory and is not persisted.

New assessment statuses: not_checked, website_found, social_only, no_website_found, website_unavailable, uncertain. UI wording is **No website found after checks**, never exhaustive absence. Exact phones are strong identity evidence; shared phones require exact name/branch street address. Structured exact name/street/city may establish a match without conflicting phone evidence; name similarity alone is insufficient. Hosted subdomains count as websites. Directory/marketplace/booking/link-hub profiles are classified separately and excluded from business-site attribution. Required failed/blocked/truncated searches yield uncertain. Explicit zero-based `optional_search_indices` can fail while positive matching website evidence establishes website_found. Negative/social-only conclusions require all configured checks completed.

Configure explicit `name_aliases` by business ID and `city_variants` in EnrichmentOptions; no translation/LLM is used. `known_matching_urls` needs a `known_matching_reference` identity attestation, or a prior verified automatic assessment; a listed URL alone is not verified. JobBusiness checkpoints retain each search/page result, indices and retry deadline. Every search attempt and actual GET, including redirects/retries, consumes both job and campaign budgets before issuing it. Policy/private-address blocks before a GET cost no HTTP request. Page/check data and checkpoint advancement commit atomically under existing campaign leases/fencing; replay keys prevent duplicates.

Derived persistence/export permissions intersect seed, search and page/redirect declarations; export defaults to false. Rights can narrow on replay but never widen existing evidence. HTML/restricted result content is not hidden in errors. Evidence eligibility now excludes expired/revoked/unknown retention immediately. Cleanup is explicit, with dry-run and execute modes; see the local review workflow below.

Live public fetch is disabled by default (`LIVE_PUBLIC_FETCH_ENABLED=false`). To enable later, select `fetch_mode="live"` and configure exact-host `page_policies` for each initial and redirected host: public access, persistence/export flags, retention and policy reference. Operators must review access/robots/terms; robots.txt is not automatically fetched. The HTTPX/HTTPcore fetcher validates DNS at connection time, directly dials a vetted numeric IP with original TLS SNI/Host, checks its peer, and rejects mixed unsafe DNS answers, private/reserved/metadata/transition addresses and unsafe redirects. It bounds redirects, time, raw bytes, concurrency and per-host rate. Proxies/automatic transport retries are disabled; login, CAPTCHA and access challenges are not bypassed. System DNS tasks may outlive a timed-out caller, but worker/queued capacity is bounded.

Read [the enrichment/search contract](docs/enrichment-contract.md) before configuring a licensed live API. No live fetch or paid call was made. Docker/psql/redis-server remain unavailable here; PostgreSQL upserts/locking, real Redis delivery, Celery scheduling/recovery and live TLS/DNS behavior remain unverified. Tests block external sockets/DNS; Windows asyncio's loopback-only internal socketpair is allowed for TestClient IPC.

## Local review and selected export

Use one trusted local operator and one web process. Authentication/authorization is absent; remote or multi-user deployment is unsupported. Run these commands with the same DATABASE_URL environment. The review demo creates 12 clearly synthetic businesses, exports eight, previews/applies cleanup, and checks independent support/manual override preservation. It makes no external requests and does not enable a live provider.

```powershell
$env:DATABASE_URL = 'sqlite:///./review-demo.db'
$env:DEMO_MODE = 'true'
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m app.cli review-demo --output examples/review-demo.csv
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open http://127.0.0.1:8000/review, select the printed campaign, filter/sort, and click a business to review its priority, manual outreach history and evidence. Choose **All review records** to inspect uncertain, unchecked, unavailable, declined and suppressed records. **No-website prospects** excludes those records and won records. Check selected rows and choose **Review export and download selected leads**; inspect omissions before downloading. Filters survive pagination/detail/back navigation. Selection applies to the displayed page. Contacted is a manual record only; no message is sent. Operator labels are unauthenticated. Global do-not-contact requires a reason.

```powershell
# Replace campaign/business IDs with those printed by the demo/import.
.\.venv\Scripts\python -m app.cli export 1 --ids 1 2 5 8 --output selected-leads.csv
# Preview across the configured database, then stop workers/web mutations before applying.
.\.venv\Scripts\python -m app.cli retention-cleanup
.\.venv\Scripts\python -m app.cli retention-cleanup --execute
# Full offline verification.
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m alembic check
.\.venv\Scripts\python -m compileall -q app migrations tests scripts
git diff --check
```

API export: POST `/api/campaigns/1/export` with application/json `{"business_ids":[1,2,5,8]}`. The CSV attachment carries `X-Export-Summary` JSON. Tracking: POST `/api/campaigns/1/businesses/2/outreach` with `{"status":"reviewed","note":"Checked source","operator_label":"local operator","suppressed":false}`. Forms require CSRF tokens; same-origin/local Host checks apply. Refresh a form after restarting the server.

Campaign CRUD accepts `review_priority`, for example `{"no_website_found":35,"social_only":25,"phone":20,"niche":20,"social":10,"activity":15,"activity_days":90}`. Website points are mutually exclusive; total caps at 100. No activity points come from observed_at alone. Shared businesses earn niche points separately for each campaign's eligible matching observation; the latest manual website correction remains authoritative across campaigns/reruns. Suppressed/declined records have zero review priority and cannot export. Priority is review order, not a purchase probability.

Sample synthetic export: **12 selected, 8 exported, 4 omitted** (expired, revoked, declined, suppressed). The mixed-permission record omits its restricted email. The manual override remains effective locally, but its non-exportable assessment/date and their priority contributions are omitted/recomputed for CSV. UTF-8 BOM preserves Armenian/Russian text; every variable cell is checked for spreadsheet formula prefixes after whitespace/control characters. A leading apostrophe also protects E.164 `+` phones. [examples/review-demo.csv](examples/review-demo.csv) contains synthetic data only.

The [review contract](docs/review-contract.md) specifies exact eligibility, retention grammar, field permissions, cleanup and audit retention. Unknown expiry fails closed. Explicit expiry/revocation excludes data before cleanup. Dry-run deletes nothing; execute removes unsupported contacts, redacts non-manual evidence payloads to tombstones, invalidates unsupported automatic assessments, rebuilds/redacts canonical claims and clears affected enrichment checkpoints. Independently supported rows, business identities and manual audits remain. Unknown policies need reconciliation and are not automatically deleted. Cleanup is not scheduled and does not erase prior exported files/backups. Older identity data without per-source snapshots is withheld from export; reimport into a new campaign with fresh reviewed declarations.

### Remaining steps before a small live discovery pilot

Runtime verification tooling is now available; start with the isolated command below rather than an existing database/project.

1. Install/start Docker Desktop and run `python scripts/runtime_verify.py`. It creates a new isolated Compose project/database, runs the targeted real PostgreSQL and queued worker tests, and reports PASS/FAIL/BLOCKED/NOT RUN. These runtime checks remain blocked here because Docker is absent; the offline suite does not establish those guarantees.
2. Choose a licensed source or operator-owned CSV. Review contact-use/persistence/export rights, expiry, revocation and manual-audit retention. Use the documented retention grammar and stable provider/branch IDs; set `record_type="imported"` or `"licensed"`, never synthetic. Existing example CSVs are fictional. Define a cleanup cadence and exported-file retention procedure.
3. For the lowest-dependency pilot, prepare a real reviewed CSV/campaign JSON following `examples/csv-campaign.json`; run `csv-validate`, `create`, then `csv-import` with the printed campaign ID. Limit the initial campaign to one niche/district and a small candidate/request budget. A live discovery API instead needs a registered reviewed adapter implementing the provider contract; none is supplied or enabled here.
4. A licensed live web-search API requires an explicitly registered configured `WebSearchProvider` with credentials, query coverage/exhaustion, request accounting and permission declarations. If fetching public pages, verify actual TLS/SNI/DNS/redirect behavior against controlled hosts first, then enable LIVE_PUBLIC_FETCH_ENABLED and configure exact-host page policies. No browser scraping or vendor endpoint is invented.
5. Back up the database, keep loopback bindings and one trusted operator, run eligibility/cleanup dry-run, then perform a small-budget enrichment. Manually inspect every candidate's identity, coverage, source rights and corrections before selecting/exporting. Review omissions and formula-safe CSV output. Record manual actions only. Resolve any integration failure before enlarging the pilot; remote/multi-user use still requires authentication/authorization.

## Runtime verification for a pilot

```powershell
# One command: offline suite, available isolated real-runtime checks, compact status summary.
.\.venv\Scripts\python scripts/runtime_verify.py
# Keep the NEW isolated stack for diagnostics; never selects your existing project.
.\.venv\Scripts\python scripts/runtime_verify.py --keep-running
# Optional, only after configuring endpoints on your own controlled public HTTPS hosts:
.\.venv\Scripts\python scripts/runtime_verify.py --fetch-config controlled-fetch.json
```

Read [runtime verification and exact Windows setup](docs/runtime-verification.md) for Docker Desktop/WSL commands, timeout budgets, logs, queued retry/cancel/resume and crash-recovery checks, and controlled-host endpoint requirements. Docker/Compose is absent in this environment: actual PostgreSQL, Redis/Celery, migration startup ordering and worker interruption checks are **BLOCKED**. Public TLS/DNS is **NOT RUN** because no controlled host was supplied. Seven PostgreSQL tests are opt-in and separate from the default fast suite. No eager execution or mock check is presented as real runtime verification.

The script uses a unique bfverify-* project, a dedicated bf_integration database, retained volumes and generated test schemas, an empty Compose env file, and an ephemeral localhost web port. It stops only its own containers unless --keep-running is selected; no database/volume deletion is performed. Exit 1 means FAIL, exit 2 BLOCKED. Summary/state lives in ignored runtime-verification.json without credentials or record data. Compose 2.24.4+ is required. A manual crash-recovery result proves only explicit resume; automatic recovery is claimed solely when observed by the real worker protocol. Production worker acknowledgement defaults are unchanged.

Observed verification for this stage: **109 fast tests passed** (one dependency deprecation warning), seven PostgreSQL tests skipped without runtime opt-in, compilation and diff checks passed. The combined operator command reported fast tests PASS, Docker-dependent checks BLOCKED and public TLS/DNS NOT RUN. The runtime override and real-service protocols remain unexecuted until Docker is available.
