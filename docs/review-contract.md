# Local review, export and retention contract

Localhost, one trusted operator and one web process are the supported deployment mode. There is no authentication/authorization. Operator labels are append-only audit text, never verified identities. Do not expose the service remotely or share it with untrusted local users. No status transition sends messages, invokes a provider or schedules communication.

## Projection and priority

`ReviewData` loads evidence, campaign businesses, contacts, assessments and outreach history in a fixed number of queries. It applies eligibility before projecting fields, computes scores, filters and sorts in memory, then paginates. This avoids N+1 queries but is intended for small local datasets; evidence is loaded globally to evaluate cross-campaign dependency chains. Large datasets need a database projection/index strategy.

Dashboard defaults to the no-website prospect queue. All review records exposes uncertain, not_checked, website_unavailable, declined, suppressed and won records. The prospect queue excludes those statuses and suppression/decline/won. Review status is new when the current outreach status is new, reviewed otherwise. Filters survive pagination and the review/detail/back links. Selection is for the currently displayed page; export accepts explicit selected IDs, not an implicit entire filtered dataset.

Weights are typed `Campaign.review_priority` configuration, editable through campaign CRUD: no_website_found 35 OR social_only 25, valid eligible public phone 20, campaign-specific evidenced niche 20, identity-matched social profile 10, explicit dated identity-matched activity 15. Total caps at 100. Activity must be `Evidence.evidence_type='activity'` with timezone-aware `details.activity_at` and `details.identity_matched=true`; observed_at alone never proves activity. The configurable activity window defaults to 90 days and excludes future dates. This is review order, not purchase probability.

One business can have multiple campaign discovery observations. Niche points require an eligible identity snapshot in the selected campaign matching that campaign's niche; another campaign's match does not transfer. Eligible public contacts/activity can support the shared identity across campaigns. Automatic assessments are campaign-specific. Latest manual website correction is global and takes precedence, including after reruns; if its evidence becomes ineligible, status becomes not_checked rather than silently reverting to an automatic assessment. Outreach status/notes are campaign-specific; do-not-contact suppression is global. All changes append an OutreachRecord with previous state, timestamp and operator label. Existing legacy note records are retained, not assigned to an invented campaign.

## Eligibility and expiry

`app.eligibility.eligible` is the shared gate for display, scoring and export. Every support dependency must pass: persistence permitted, export permitted when exporting, no revoked_at, no cleaned_at, declared retention, and no elapsed deadline. Dependencies include parent_evidence_id and explicit support_evidence_ids. Missing/cyclic/overlong dependency chains fail closed. Suppression is an explicit gate input for outreach/export; suppressed records remain visible for local review but contribute no priority and cannot be exported. Decline also excludes export/outreach.

Current retention strings are interpreted conservatively, case-insensitively:

- `indefinite` is an explicit unlimited declaration.
- `N days` means N whole days after that observation's observed_at (1 to 999999).
- `expires: <ISO-8601 timestamp with timezone>` is an absolute deadline, including `Z`.
- Semicolon-separated declarations intersect; earliest deadline wins. `expires_at` is an additional deadline, not an override that can widen a source declaration.
- Empty separator fragments, exact `synthetic fixture`, `synthetic demo`, `owned synthetic fixture` annotations and `license:` annotations are metadata. They cannot alone establish an expiry policy.
- All other text, unspecified, date-only expiry, malformed expiry and unknown terms are ineligible immediately, even alongside indefinite. Unknown policies require operator reconciliation; cleanup does not guess a deadline or delete them.

Expiry occurs at `deadline <= current UTC time`. SQLite naive stored datetimes are interpreted as UTC. Revocation takes effect when revoked_at is non-null. Derived evidence also retains flattened declarations, and new enrichment records explicit search/page dependencies so later revocations propagate. Older derived records have only the dependencies actually recorded at ingestion; missing historical provenance cannot be reconstructed with certainty. Existing identity records without per-source field snapshots cannot be exported; reimport into a new campaign to collect explicit fresh identity evidence. Do not retroactively mark restricted data exportable.

## Selected export

`POST /api/campaigns/{id}/export` accepts `{"business_ids": [1,2]}` (1 to 1000 IDs). CLI `export` and the dashboard call the same function. Deduplicate selection, check campaign membership again, exclude suppressed/declined, and require export-eligible identity snapshots before emitting a row. Phones, emails, website/social URLs and contact source URLs are independently gated. Automatic/manual assessment eligibility is checked separately. Missing fields remain empty. Priority and contributing reasons are recomputed from export-eligible evidence, so they may differ from local review; the current non-exportable manual override never silently falls back to an older assessment. Source claims do not make every canonical business field exportable.

Output uses UTF-8 BOM, CRLF and Python CSV quoting. Every variable cell is protected: after leading whitespace and Unicode controls/format characters, `=`, `+`, `-`, `@` causes an apostrophe to be prepended to the original cell. This includes E.164 phones, identity values, URLs, statuses and contributing reasons. Quotes, commas and newlines are handled by the CSV writer. Raw HTML, provider responses, notes, audit labels, credentials and job snapshots are not exported.

API returns a CSV attachment and JSON `X-Export-Summary` header with omitted IDs/fields and concise reasons. The server-rendered workflow presents that summary and a download link containing only the freshly eligible CSV; no export file is persisted by the web server. CLI writes only the explicitly named path. Responses use no-store. Source `record_type` is configured explicitly for synthetic CSVs; production imports default to imported. Never mix fixture content with real pilot claims.

## Cleanup

`retention-cleanup` defaults to dry-run. `--execute` re-evaluates eligibility at execution time and refuses active campaign worker leases. Stop workers/web mutations before executing; local concurrent cleanup/writes are unsupported, and this idle check is not a distributed lock.

Cleanup targets expired/revoked/previously-cleaned evidence and dependent records. It:

- Deletes contacts supported only by targeted evidence. Independently supported contact rows remain, including the same normalized value under another evidence ID.
- Invalidates non-manual assessments whose support fails, clears candidates/coverage/date, and sets not_checked. Manual assessment/evidence/outreach audit records remain unchanged, but their failed evidence chains exclude them operationally.
- Replaces non-manual evidence payloads with tombstones: empty source URL, match reasons and details, cleaned_at, persistence/export false. IDs, hashed replay keys, provider identity, deadlines/retention and dependency IDs remain to prevent permission widening or resurrection on replay.
- Rebuilds affected canonical identity fields from remaining eligible snapshots; redacts canonical name/location/address/aliases when no eligible discovery support remains. Clears unsupported website claims and source URLs. Business/source IDs remain; one expired source never deletes an entire business.
- Clears affected enrichment checkpoint payloads, cancels their jobs and removes derived result payloads. Start a fresh job from eligible evidence after cleanup; scrubbed jobs cannot be resumed.

Unknown-retention evidence is excluded but not deleted. Manual audit text and source IDs are deliberately retained; the operator must define a separate audit/minimal-identity retention policy before real data use. Cleanup is explicit and not a scheduler, filesystem CSV cleanup, database backup erasure or automatic source-policy revocation service. Restrictive flags can be set directly by the trusted local operator; no rights-management UI is added here. Already-downloaded CSVs cannot be recalled automatically.

## Local browser controls

Only localhost, 127.0.0.1 and ::1 Host values are allowed. Unsafe browser requests must have a matching local Origin; cross-site fetch metadata is rejected. Local CLI/API clients without Origin remain supported. Form mutations and form exports require a signed random token matching the HttpOnly SameSite=Strict cookie. Tokens are per web process and become invalid after restart; reload forms. JSON APIs accept application/json; plain-text cross-site requests are rejected. These controls are not authentication.

Run uvicorn on 127.0.0.1. Compose publishes web/database ports on 127.0.0.1; uvicorn's internal container bind is required for that loopback port mapping. Remote/multi-user use remains unsupported pending authentication, authorization, distributed limits and audit identity implementation.
