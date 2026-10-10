# Public enrichment and web search contract

Enrichment reuses Campaign, Business, Contact, Evidence, WebsiteAssessment, Job and campaign leases. JobBusiness adds per-business checkpoints. Working discovery remains separate and compatible.

## Search adapter

Implement `WebSearchProvider.identity` and `search(query: str, limit: int) -> list[WebSearchResult]`. Each typed result carries title, URL, snippet, provider, exact query, explicit persistence/export permission and retention policy. The runner validates associations; snippets/titles never verify identity. Restrictive defaults apply, and persistence-denied result content cannot enter durable candidates/evidence/errors.

One invocation must perform exactly one counted vendor/API request, without hidden retries or pagination. Return fewer than the requested limit only when configured coverage is complete; a full result limit is conservatively treated as truncated. RetryableProviderError supports numeric or HTTP-date Retry-After; PermanentProviderError covers permanent failures. Configuration errors must be explicit. Credentials belong in adapter configuration, never job snapshots or evidence.

Register a reviewed configured adapter at `app.providers.web_search_providers['live']`, then select `search_provider='live'`. No vendor endpoint, key variable, SDK, paid API or Google SERP/Maps browser scraper is invented or enabled here. Before registration, document license/contact-use rights, retention, rate/request costs, query/language support, provenance and exhaustion guarantees. Default provider is unavailable; fixtures explicitly require DEMO_MODE and simulate data only.

## Public fetch path

Live page access requires LIVE_PUBLIC_FETCH_ENABLED, fetch_mode='live' and exact-host AccessPolicy values for every host in a redirect chain. Require public access, policy reference, persistence/export declarations and retention policy. No wildcard/suffix permissions exist. Review source access/robots/terms externally; a 200 response is not legal permission.

Use PublicFetcher with the default PublicTransport/PublicNetworkBackend. Transport injection is trusted test composition, never an HTTP-user option. DNS answers are checked at connection time and a vetted IP is dialled as a numeric sockaddr; there is no second DNS lookup. Original Host/SNI and certificate verification remain; connected peers, mixed DNS answers, redirects and metadata/private/reserved/transition addresses are checked. Proxy/environment routing and transport retries are disabled. Independent deployment egress filtering is also appropriate.

DNS workers, connection/read/write/pool and total deadlines, redirect counts, raw bytes, process concurrency and per-host intervals are bounded. Compressed/non-HTML content is unsupported. Login/password/CAPTCHA challenges stop checks; scripts/resources never execute/load and no credentials are submitted. HTML remains ephemeral; only permission-approved facts/provenance are stored. Per-process limits require distributed coordination for multi-worker live use. System DNS calls may finish after their caller times out; outstanding resolver capacity remains bounded.

The implementation uses documented [HTTPX custom transports](https://www.python-httpx.org/advanced/transports/) and [HTTPcore network backends](https://www.encode.io/httpcore/network-backends/), informed by [OWASP SSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html). HTTPX/httpcore version ranges are bounded; rerun transport/security tests before upgrading.

## Matching, checks and review

Exact normalized phones are strong evidence, never owner/WhatsApp/delivery claims. Shared phones additionally require exact configured name and branch street address. Structured exact name/street/city can match without conflicting contacts. Similar names, multiple business entities and unrelated directories cannot establish identity. Source-permitted matched pages may contribute tel/mailto/JSON-LD, explicit sameAs/rel=me relationships and bounded optional contact/about pages. Only same-origin contact pages inherit a verified relationship; cross-origin redirects lose it. Hosted subdomains count as websites.

Required blocked, failed or truncated searches produce uncertain. Positive matching website evidence may survive explicitly optional search failure. Negative/social-only conclusions require completed configured checks; UI says 'No website found after checks', not exhaustive absence. A known matching URL needs explicit identity attestation reference or prior automatic verification. Ordinary unavailability differs from access blocks.

Every search/page attempt, redirect and retry is charged before work against job and campaign budgets. Each successful check commits derived data and its JobBusiness checkpoint atomically under campaign lease fencing. Unique keys include business and provenance; contacts never merge branches. Seed/search/page permissions intersect and retention declarations follow the chain. Manual corrections remain separate append-only records effective over automated reruns. Audit actors currently are operator labels without authentication.

Local mutation forms now have CSRF protection and local host/origin checks. Review/export/retention behavior is defined in the [local review contract](review-contract.md). Before live use, verify real PostgreSQL concurrency/upserts/leases, Redis redelivery and Celery countdown/recovery, public TLS/DNS behavior, policy maintenance and operational job reconciliation. Remote/multi-user deployment additionally requires authentication/authorization and distributed rates. SQLite/offline/MockTransport checks do not establish these integration guarantees.
