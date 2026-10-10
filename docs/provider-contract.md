# Discovery provider contract

Implement `DiscoveryProvider` in `app/providers.py`. WebSearchProvider now supports enrichment; see [the enrichment/search contract](enrichment-contract.md). No live adapter or vendor endpoint is supplied here.

## Registration and capabilities

Expose a stable `identity` string (at most 100 characters) and `ProviderCapabilities`. Stable external IDs and bounded pagination are required by the runner. Declare whether the adapter truly performs keyword search, applies canonical district filtering, and its maximum page size. Local fixtures and CSV explicitly declare that they do not perform text search. Do not declare capabilities the provider does not implement. Register a licensed discovery adapter explicitly in `licensed_providers` under the campaign's license reference; default configuration remains fixture-only and Google content is unsupported.

## Request, page and cursor

`discover_page(request: DiscoveryRequest, cursor: str | None, limit: int) -> DiscoveryPage` is exactly one billable/request-counted provider attempt. The request carries persistent query ID, normalized query text, primary language, all deduplicated variant metadata, canonical niche/city/district restrictions and the current per-page attempt ordinal. `cursor=None` starts a query. Do not translate keywords, run nested searches, silently retry HTTP requests or fetch extra pages inside this call.

Return at most `limit` results, a continuation cursor or `None` for exhaustion, optional structural row errors, and a dataset fingerprint where available. Cursors are opaque to the runner and are stored unchanged. They must be scoped to this query and stable dataset; the same cursor must safely replay after timeout/crash. A continuation must advance. Stable ordering or snapshot semantics must prevent skipped rows across pages. For expired/invalid cursors, raise a permanent error; do not silently restart or reinterpret them. The local providers pin continuation to file-byte fingerprints.

Each `DiscoveryResult` must carry this adapter's exact identity, a nonempty stable external business/branch ID, request query ID, name, city, district, niche, source provenance URL, and explicit persistence/export permissions and retention policy. Optional website and phone are source claims, not verified ownership, website availability or messaging capability. Result permissions default to false. Do not return cross-query associations; each actual query's result receives its own durable evidence association. A source appearing in many queries remains one provider/source/business identity.

## Errors and retries

Raise `RetryableProviderError` for rate limits, temporary service failures or temporary transport errors, optionally carrying numeric seconds or HTTP-date `retry_after`. Raise `PermanentProviderError` for invalid queries, invalid credentials/license, cursor invalidation, malformed responses or unsupported operations. Unexpected exceptions preserve already committed pages and surface as application errors needing operator recovery. Do not wrap programmer bugs as retryable errors. Arbitrary provider messages are not persisted; classify failures structurally and keep sensitive details out of logs.

The runner charges every call before invoking the adapter. It owns bounded retries, exponential backoff/jitter, Retry-After scheduling, cancellation between calls, candidate capacity, database identity and atomic cursor commits. Adapters must enforce explicit connect/read deadlines shorter than `DISCOVERY_LEASE_SECONDS`; results from expired worker leases are discarded. Retryability cannot guarantee exactly-once external execution: a killed worker may repeat a charged request, so the vendor must tolerate read-only replay.

## Rights and identity

Before enabling a licensed adapter, supply reviewed license metadata and retention/export declarations. The ingestion gate excludes persistence-denied or Google-tagged results and intersects export permission with the campaign setting. Discarded business content must not be hidden in exception messages or row errors. Row errors may report structural row numbers/reasons without copying restricted content.

External IDs are unique only within provider identity. Shared names/phones cannot justify merging. If source IDs are unstable, branch identities ambiguous or a dataset is rekeyed, stop and request review rather than inventing an identity. CSV namespace plus explicit source ID demonstrates the required strategy. The [local review/retention contract](review-contract.md) defines conservative declaration parsing, immediate operational exclusion and explicit cleanup. Legal-policy interpretation and source revocation notifications remain operator responsibilities.

## Validation checklist for a future adapter

Test Unicode request fidelity, real query/district behavior, empty/nonempty final pages, overlapping results, cursor replay/expiry, snapshot changes, restrictive rights, duplicate source IDs, identity changes, Retry-After parsing, bounded retries and page-size violations. Then run actual PostgreSQL/Redis/Celery tests for simultaneous deliveries, worker death during calls and commits, lease expiry/fencing and budget reservations. Offline PostgreSQL SQL and SQLite/threaded tests do not establish those runtime guarantees.

The upsert pattern follows the official SQLAlchemy [PostgreSQL](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert) and [SQLite](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#insert-on-conflict-upsert) APIs. Delivery/retry configuration follows [Celery task documentation](https://docs.celeryq.dev/en/stable/userguide/tasks.html).
