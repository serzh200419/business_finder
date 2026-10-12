# Local pilot workflow

From the repository directory, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\start-local.ps1`. Afterwards use `.\start-local.ps1` daily. Python 3.11+ is the only prerequisite; first package installation requires internet. The launcher preserves `.venv`, `.env` and the SQLite database, checks installed dependency versions, applies Alembic migrations, and starts one server at http://127.0.0.1:8000. Use `-CheckOnly` for migrations only, `-Port 8001` for an occupied port. Credentials and captured installation/migration errors are not printed.

Discovery/enrichment use the same services as CLI/API, directly in the web process. Each UI run attempts at most the existing 100 pages/steps per invocation; budgets and provider timeouts further bound work. No local queue or automatic retry timer exists. Review in another tab remains available; overlapping UI runs are rejected. Partial/cancelled/failed jobs have explicit Resume actions, with optional repaired discovery-query retries. If the server stops during a run, wait for the campaign lease to expire (default 120 seconds) and resume. Completed checkpoints are retained and replay uses existing identity constraints. Increase exhausted campaign budgets through `PUT /api/campaigns/{id}`; existing enrichment resume API accepts `request_budget`. Inspect saved progress/errors/deadlines using each Job details link.

## CSV configuration

Select CSV on the campaign form and supply provider JSON, for example:

```json
{
  "csv_path": "C:/Users/you/Documents/permitted-businesses.csv",
  "provider_namespace": "my-stable-directory",
  "license_reference": "my-directory-contract",
  "persistence_permitted": true,
  "export_permitted": true,
  "retention_policy": "30 days",
  "record_type": "imported"
}
```

Required CSV headers: `external_id,name,city,niche,source_url`; optional: `district,website,phone,address,email`. Configure `column_mapping` if necessary. Source URLs must be absolute HTTP(S) provenance URLs. Stable IDs/namespace and immutable files are required for resumable imports. Use `POST /api/csv/validate` or `python -m app.cli csv-validate <campaign.json>` before importing. Rights declarations must describe actual permissions. `.env` is for secrets, never the form or provider JSON. `examples/csv-campaign.json` contains synthetic verification rows, not real leads.

Imported public contacts can be reviewed/exported without search, subject to evidence rights, retention, suppression and validated contact fields. Unknown websites remain unchecked until enrichment; missing URLs alone do not prove absence. Real imported rows never use fixture enrichment automatically. No live search adapter is registered, so their UI enrichment currently reports a configuration error.

## Provider inventory and next step

| Interface | Implemented | Configuration / next adapter requirements |
|---|---|---|
| `DiscoveryProvider.discover_page(request, cursor, limit) -> DiscoveryPage` in `app/providers.py` | `FixtureProvider`, `CSVProvider`, `MapsBrowserProvider` (Google storage blocked) | Stable provider/external IDs, bounded replay-safe pages, explicit persistence/export/retention permissions, no hidden retries or paid calls. `DiscoveryRequest` includes text/language, niche/city/district, variants and attempt. |
| Licensed discovery registry `licensed_providers` | Empty; no live vendor | Concrete adapter registered by `license_reference`; campaign `discovery=licensed`, license reference and storage permission. Registration must happen in the local web/CLI process. |
| `WebSearchProvider.search(query, limit) -> list[WebSearchResult]` | `FixtureWebSearchProvider` only | Results carry URL/title/snippet/query/provider and independent persistence/export/retention rights. Concrete adapter goes in `web_search_providers['live']`. Unavailable search raises a configuration error. |
| Public website fetcher | Implemented, disabled for live use by default | `LIVE_PUBLIC_FETCH_ENABLED=true`, `EnrichmentOptions.fetch_mode=live`, and explicit per-host `page_policies` with public access, storage/export permissions, retention and policy reference. TLS, DNS/IP and redirect protections remain enforced. |

Campaign fields/JSON are defined in `app/schemas.py` (`CampaignInput`, `ProviderConfig`). Enrichment API `POST /api/campaigns/{id}/enrich` takes `EnrichmentOptions`: use `search_provider=live`, `fetch_mode=live`, request/result/query/page limits and host policies. Until policy controls are supplied in the UI, use this existing API for concrete live enrichment configuration. UI real-enrichment defaults deliberately contain no host access grants. Resume retains the original job's options/checkpoints; it does not replace them silently.

Remaining real-data blockers: no concrete discovery/search adapter, credentials or licensed dataset provided; no approved per-host fetching policies; live public fetching disabled by default. CSV can import an operator-supplied permitted real dataset now. Fixture search defaults to an empty synthetic result set, and fixture website requests have no grants outside the configured fixture policies; positive/missing-site assessments are verification outcomes only. A concrete Maps browser adapter is now present but retained Google-source storage restrictions block persistent runs; its live selectors are unverified. See [Maps browser setup, restriction and smoke command](maps-browser.md). No Places adapter exists and no working live Google Maps lead claim is made. Before the next step, choose the concrete source, its response/pagination/identity contract, credentials in `.env`, rate/request budgets, rights, retention and search coverage; see [provider contract](provider-contract.md). Infrastructure is not a prerequisite.

## Verification

```powershell
.\.venv\Scripts\python -m pytest -q
```

`tests/test_local_workflow.py` exercises migrated SQLite, campaign form, direct discovery, direct enrichment, public-phone prospect filtering and selected export; it also checks checkpoint resume, overlapping-run rejection and unavailable real providers without fixture substitution. Existing deterministic tests cover interruption/replay, enrichment/review/rights and migrations. These tests use synthetic fixtures only and do not claim live lead discovery or public-network verification.

Observed on Windows for this change: **112 tests passed**, with one existing HTTPX/TestClient deprecation warning. The PowerShell launcher reused the existing environment, migrated an isolated temporary SQLite database twice while retaining a sentinel record, and served real loopback HTTP health/UI requests. The verification server was stopped afterwards. Fresh-environment package installation was not exercised; no external-service integration was needed or run.
