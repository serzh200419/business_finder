# Isolated runtime verification

## Observed result in this workspace

Docker was not on PATH and Docker Desktop was absent at both the standard all-users and per-user locations. No real PostgreSQL, Redis or Celery service was started. These checks are **BLOCKED**, not passed. The PostgreSQL tests skip without explicit integration opt-in. No controlled public host configuration was supplied: public TLS/DNS checks are **NOT RUN**. Fast tests remain deterministic SQLite/offline checks; SQL generation, mock HTTP transports and Celery configuration assertions are not runtime verification.

## Windows setup

Check supported Windows/hardware requirements and enable CPU virtualization in BIOS/UEFI. In an administrator PowerShell terminal, install WSL if missing, reboot when requested, then update/check it:

```powershell
wsl --install
# Reboot if requested, then:
wsl --update
wsl --version
```

Docker currently requires WSL 2.1.5 or newer for its WSL backend. Download Docker Desktop for your architecture from the [official Windows installation page](https://docs.docker.com/desktop/setup/install/windows-install/), run the installer, select the WSL 2 backend and start Docker Desktop from the Start menu. Use Linux containers. Reopen PowerShell after installation so PATH updates apply. Microsoft's [WSL installation instructions](https://learn.microsoft.com/en-us/windows/wsl/install) cover installation/update failures.

```powershell
docker version
docker compose version
docker info --format '{{.ServerVersion}}'
```

The daemon must be running. This verification override requires Compose **2.24.4+** for `!override`/`!reset` port handling; update Docker Desktop if older. Image pulls and installing Python packages during image builds need network access; no vendor API credentials or paid calls are used.

## One operator command

From the repository root, with the project's development environment installed:

```powershell
.\.venv\Scripts\python scripts/runtime_verify.py
```

Exit codes: 0 no failed/blocked checks, 1 at least one failure, 2 at least one blocked check. NOT RUN is explicit and does not mean passed. `--skip-fast` omits the offline suite. `--keep-running` leaves the new isolated stack running for inspection. Without that flag the script stops its own containers on completion; it does not remove containers, schemas, databases or volumes. Build/command output is captured rather than printed to avoid connection strings, credentials and record payloads in the summary. Failure summaries name the stage and exit code; investigate the isolated services separately.

Each invocation generates a NEW `bfverify-<random>` Compose project, uses `compose.yaml` plus `compose.integration.yaml`, an empty Compose env file and a dedicated **bf_integration** database. It never selects an existing project. PostgreSQL host port publication is removed and the web port is dynamically assigned on 127.0.0.1. The ordinary application's database/volume and .env are not modified. The same existing PostgreSQL, Redis, migrate, web and worker architecture is used; verify is a test runner, not production infrastructure. Runtime state/project/port/status is saved in ignored `runtime-verification.json`, with no secrets or business fields. Every generated project retains its own volume; repeated runs consume disk, which operators can manage separately after inspecting ownership. This tool contains no volume/database deletion command.

The stack build budget is 600 seconds; startup health budget is 120 seconds. SQL connections/statement/lock waits are bounded at 5/5/3 seconds. Redis/Celery publishing and inspection use short connection/socket/control timeouts. Normal worker workflow has a 300-second outer budget, interruption recovery 180 seconds, and individual progress waits 60 seconds. A failure stops later dependent checks, reporting BLOCKED. Stop on timeout also terminates leftover verification processes unless --keep-running was requested; when keeping it, inspect/stop the isolated services manually.

Checks performed when Docker is available:

1. PostgreSQL and Redis health, migrated container successful exit, and web/worker start timestamps after migration completion; web health also requires a real SELECT 1.
2. Seven targeted real-PostgreSQL tests: upgrade/drift/downgrade/re-upgrade, provider namespace uniqueness, replay, atomic page rollback, simultaneous lease acquisition, expiry/replacement/stale fencing, restrictive rights and per-field export. Each uses its own generated schema in the dedicated database. Generated schemas remain for failure diagnostics; none of these tests resets public/user data.
3. Redis PING; actual worker ping and registration; queued fixture discovery; persisted retry deadline observed before queued completion; queued cancellation between fixture pages and explicit resume; queued enrichment and all ten expected synthetic assessments; replay without new contacts/evidence; real HTTP health/dashboard/selected export yielding eight of twelve synthetic rows. Eager mode is explicitly rejected. Job progress comes from PostgreSQL, not a fake task result backend.
4. Pause a fixture at a Redis barrier after request reservation but before page commit. Kill ONLY the isolated worker container with SIGKILL; restart it. Attempt observed automatic redelivery for 75 seconds. If it does not complete, ensure the campaign lease is idle, explicitly resume and enqueue the persisted job, then verify completion and duplicate-free replay. Summary names **automatic_redelivery_observed** or **explicit_manual_resume_observed** only after that path succeeds.

The verification worker bootstrap lives in `integration_tests/worker.py`, only loaded by the override. It adds a fixture barrier (40-second maximum wait) and uses a ten-second campaign lease and thirty-second Redis visibility timeout for the experiment. It does not register a new production task/provider. A normal worker continues to use `app.tasks.celery_app` and the existing defaults.

### Acknowledgement and recovery limits

Application tasks use late acknowledgements, reject-on-worker-loss and prefetch one. `discover_job.retry` publishes another attempt for a persisted retry deadline; database lease/fencing prevents simultaneous deliveries from committing the same campaign. A failure after committing but before acknowledgement can replay. An ordinary unexpected task exception is not a promise of requeue/recovery: it preserves checkpoints for manual resume.

The standard Redis broker visibility timeout is one hour unless configured otherwise; a whole-worker hard kill cannot perform a graceful acknowledgement/requeue. The integration override's thirty-second setting accelerates the experiment and is not a production recommendation. Countdown/redelivery interactions can repeat messages. See [Celery's Redis visibility and shutdown documentation](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html). No automatic crash recovery has yet been observed in this environment because Docker is absent. A successful manual fallback proves only that explicit resume path, not automatic redelivery.

## Logs and unfinished-job recovery

For a run started with --keep-running:

```powershell
$run = Get-Content runtime-verification.json -Raw | ConvertFrom-Json
$project = $run.project
if ($project -notmatch '^bfverify-[0-9a-f]{16}$') { throw 'Expected isolated verifier project' }
docker compose -p $project -f compose.yaml -f compose.integration.yaml ps -a
docker compose -p $project -f compose.yaml -f compose.integration.yaml logs --tail 100 migrate web worker redis
docker compose -p $project -f compose.yaml -f compose.integration.yaml exec -T verify python -m pytest -q --tb=short integration_tests/test_postgres.py
# Replace 1 with the unfinished job ID. Wait for the lease to be idle before resume.
docker compose -p $project -f compose.yaml -f compose.integration.yaml exec -T verify python -m app.cli job 1
docker compose -p $project -f compose.yaml -f compose.integration.yaml exec -T verify python -m app.cli resume 1
# Queue a resumed discovery job instead of running it inline:
docker compose -p $project -f compose.yaml -f compose.integration.yaml exec -T verify python -c 'from app.db import SessionLocal; from app.services import resume_job; from app.tasks import discover_job; db=SessionLocal(); resume_job(db,1); db.close(); discover_job.delay(1)'
# Enrichment uses its existing explicit command:
docker compose -p $project -f compose.yaml -f compose.integration.yaml exec -T verify python -m app.cli enrichment-resume 1 --request-budget 200
# Stop this isolated project while retaining its data:
docker compose -p $project -f compose.yaml -f compose.integration.yaml stop
```

Logs/debug output here belongs to the isolated synthetic project. Do not paste .env, Compose-resolved environment configuration, password-bearing URLs or real provider/record payloads into reports. The verifier never prints those. The barrier jobs must be released or allowed to time out before manual resume; the supplied protocol performs that release. No transactional outbox or background lease scanner is added. Budget exhaustion, unlicensed/expired evidence and scrubbed retention checkpoints require operator reconciliation, not blind task retries.

Real PostgreSQL tests may also be explicitly run against an independently prepared dedicated database whose name starts bf_integration, setting BF_RUNTIME_INTEGRATION=1 and RUN_INTEGRATION_TESTS=1. Set DATABASE_URL securely in your shell; never print it. Run `python -m pytest -q --tb=no integration_tests/test_postgres.py`. This option does not verify Redis/Celery or startup ordering. Tests retain generated schemas and use no destructive reset.

## Controlled public HTTPS verification

No public endpoint is fetched by default. Create an operator-owned host with a public DNS address and valid HTTPS certificate, ordinary HTML endpoints, and explicit markers. Copy `examples/controlled-fetch.example.json` to an ignored local `controlled-fetch.json`, replace every placeholder, and declare only exact hosts you control. Authorization/policy reference is mandatory. This command intentionally fetches only those configured test URLs; it does not discover or enrich businesses and persists no HTML or contacts.

Provide these controlled endpoint behaviors:

- `valid_tls_host_sni`: HTML containing BFVERIFY_OK; configure the server to reject incorrect Host/SNI. The probe observes the production numeric-IP connection, certificate-verified TLS with original hostname/SNI, and outgoing Host header. It requires the marker.
- `allowed_redirect`: 302 to your explicitly permitted HTTPS destination, whose HTML contains the marker. Declare every redirect host and the expected final URL. HTTP downgrade is not permitted by the probe.
- `denied_redirect`: your valid public HTTPS endpoint redirects to http://127.0.0.1/ or an unpermitted destination. Configure expected blocked_address or access_policy_blocked accordingly. The probe must have reached the public TLS source before accepting a denial as PASS.
- `timeout`: your server delays headers/body beyond the configured short read/total timeout.
- `response_size`: your server returns HTML larger than the declared max_bytes.
- Optional `invalid_certificate`: a SECOND controlled public hostname with an untrusted or hostname-mismatched certificate; include it in allowed_hosts and configure expected tls_verification_failed. No external bad-certificate test site is used. If omitted, certificate-rejection verification is NOT RUN.

```powershell
.\.venv\Scripts\python scripts/runtime_verify.py --fetch-config controlled-fetch.json
# Direct probe, for troubleshooting only; the combined command adds a 150-second process watchdog.
.\.venv\Scripts\python -m app.fetch_verification --config controlled-fetch.json
```

This opt-in command is separate from LIVE_PUBLIC_FETCH_ENABLED job configuration. It uses the actual PublicTransport/PublicNetworkBackend, default verified SSL context and numeric socket dialer. It does not inject a fake resolver, proxy, unverified SSL context or alternate address fallback. Output includes check/status/error codes only, no URLs, HTML, credentials or certificate dumps. Missing cases are NOT RUN, not inferred from other passes. Certificate verification and timeouts now have distinct error codes; the total deadline is also checked after empty response bodies. Deterministic unsafe-address/rebinding tests remain fast/mock tests, and are never described as public TLS/DNS verification. System DNS calls can outlive a caller's deadline; the combined verifier's process watchdog is the bound for this probe.

## Minimum remaining pilot setup

Install/start Docker Desktop and rerun the verifier until the real service/worker stages pass. Investigate any manual-only crash result and plan explicit recovery before using it. Supply controlled HTTPS endpoints and run the necessary certificate/Host/SNI/redirect/timeout/size cases. Then review a licensed source's contact-use, export, retention and revocation policy; implement/configure its discovery and search adapters under the existing contracts (none is enabled here), set a small one-niche/district budget, and retain localhost/one trusted operator. Remote deployment remains unsupported. No live discovery vendor, messaging, authentication system or paid API call is introduced by verification.
