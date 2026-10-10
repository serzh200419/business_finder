"""Isolated runtime verification. No destructive database/volume operations."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class CheckFailure(Exception):
    pass


def command(args, timeout=60, env=None, accept_nonzero=False):
    try:
        result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired):
        raise CheckFailure('command unavailable or timed out') from None
    if result.returncode and not accept_nonzero:
        # Never print arbitrary command output: it can contain credentials or record payloads.
        raise CheckFailure(f'command returned exit {result.returncode}')
    return result.stdout.strip()


def json_output(output):
    try:
        return json.loads(output)
    except ValueError:
        return [json.loads(line) for line in output.splitlines() if line.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-fast', action='store_true', help='Do not run the default offline suite')
    parser.add_argument('--keep-running', action='store_true', help='Leave only the new isolated stack running')
    parser.add_argument('--fetch-config', type=Path, help='Explicit operator-owned public HTTPS endpoint configuration')
    args = parser.parse_args(argv)
    checks = []
    def report(name, status, reason=''):
        checks.append({'check': name, 'status': status, 'reason': reason})
        print(f'{status:7} {name}' + (f': {reason}' if reason else ''), flush=True)
    if args.skip_fast:
        report('Fast SQLite/offline suite', 'NOT RUN', '--skip-fast selected')
    else:
        try:
            output = command([sys.executable, '-m', 'pytest', '-q', '--tb=no', 'tests'], timeout=180)
            match = re.search(r'\d+ passed[^\n]*', output)
            report('Fast SQLite/offline suite', 'PASS', match.group(0) if match else 'pytest completed')
        except CheckFailure as exc:
            report('Fast SQLite/offline suite', 'FAIL', str(exc))
    docker = shutil.which('docker')
    runtime_names = ['Migration startup ordering / PostgreSQL / Redis / web health',
                     'Real PostgreSQL integration tests', 'Real Redis/Celery queued workflow', 'Worker interruption/recovery']
    available = bool(docker)
    if available:
        try:
            endpoint = os.environ.get('DOCKER_HOST')
            if not endpoint:
                context = command([docker, 'context', 'show'], 15)
                endpoint = command([docker, 'context', 'inspect', context, '--format', '{{.Endpoints.docker.Host}}'], 15)
            if not endpoint.startswith(('unix://', 'npipe://')):
                raise CheckFailure('Local Docker socket/named pipe required; remote daemon is unsupported')
            version = command([docker, 'compose', 'version', '--short'], 15)
            digits = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
            if not digits or tuple(map(int, digits.groups())) < (2, 24, 4):
                raise CheckFailure('Compose >=2.24.4 required for safe port overrides')
            command([docker, 'info', '--format', '{{.ServerVersion}}'], 15)
            report('Docker / Compose', 'PASS', 'CLI, daemon and Compose version available')
        except CheckFailure as exc:
            available = False
            report('Docker / Compose', 'BLOCKED', str(exc))
    else:
        report('Docker / Compose', 'BLOCKED', 'docker executable not found on PATH')
    project = None
    state = {'checks': checks}
    if not available:
        for name in runtime_names:
            report(name, 'BLOCKED', 'Docker runtime unavailable; no real service check performed')
    else:
        project = 'bfverify-' + uuid4().hex[:16]
        state['project'] = project
        # Empty env file prevents accidental consumption of the user's .env.
        with tempfile.TemporaryDirectory(prefix='bfverify-') as scratch:
            empty = Path(scratch) / 'empty.env'
            empty.write_text('', encoding='utf-8')
            compose = [docker, 'compose', '--env-file', str(empty), '-p', project,
                       '-f', 'compose.yaml', '-f', 'compose.integration.yaml']
            state['compose_files'] = ['compose.yaml', 'compose.integration.yaml']
            completed = set()
            try:
                command(compose + ['up', '--build', '-d', '--wait', '--wait-timeout', '120', 'db', 'redis', 'web', 'worker', 'verify'], 600)
                rows = json_output(command(compose + ['ps', '-a', '--format', 'json'], 15))
                rows = rows if isinstance(rows, list) else [rows]
                services = {r['Service']: r for r in rows}
                assert all(services[s].get('Health') == 'healthy' for s in ('db', 'redis', 'web'))
                assert services['migrate'].get('ExitCode') == 0 and services['worker']['State'] == 'running'
                def container_state(service):
                    cid = command(compose + ['ps', '-a', '-q', service], 15)
                    return json.loads(command([docker, 'inspect', '--format', '{{json .State}}', cid], 15))
                migration = container_state('migrate')
                assert migration['ExitCode'] == 0 and migration['Status'] == 'exited'
                assert all(container_state(s)['StartedAt'] >= migration['FinishedAt'] for s in ('web', 'worker'))
                state['web_binding'] = command(compose + ['port', 'web', '8000'], 15)
                assert re.fullmatch(r'127\.0\.0\.1:[0-9]+', state['web_binding'])
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open('http://' + state['web_binding'] + '/health', timeout=5) as response:
                    assert json.load(response) == {'status': 'ok', 'database': 'ok'}
                report(runtime_names[0], 'PASS', 'Dedicated bf_integration database; migrate exited before web/worker start')
                completed.add(runtime_names[0])
                postgres_output = command(compose + ['exec', '-T', 'verify', 'python', '-m', 'pytest', '-q', '--tb=no', 'integration_tests/test_postgres.py'], 180)
                assert re.search(r'\b7 passed\b', postgres_output) and 'skipped' not in postgres_output
                report(runtime_names[1], 'PASS', '7 targeted tests against real PostgreSQL')
                completed.add(runtime_names[1])
                result = json.loads(command(compose + ['exec', '-T', 'verify', 'python', '-m', 'integration_tests.worker_checks', 'normal'], 300))
                assert result['status'] == 'PASS'
                report(runtime_names[2], 'PASS', 'Real worker: queue, persisted retry, cancel/resume, enrichment, replay, review/export')
                completed.add(runtime_names[2])
                prepared = json.loads(command(compose + ['exec', '-T', 'verify', 'python', '-m', 'integration_tests.worker_checks', 'prepare-interruption'], 75))
                state['interrupted_job_id'] = prepared['job_id']
                command(compose + ['kill', '-s', 'SIGKILL', 'worker'], 20)
                command(compose + ['up', '-d', 'worker'], 45)
                result = json.loads(command(compose + ['exec', '-T', 'verify', 'python', '-m', 'integration_tests.worker_checks',
                    'finish-interruption', '--job-id', str(prepared['job_id']), '--query-id', str(prepared['query_id'])], 180))
                assert result['status'] == 'PASS'
                report(runtime_names[3], 'PASS', result['recovery'])
                completed.add(runtime_names[3])
            except Exception as exc:
                remaining = [name for name in runtime_names if name not in completed]
                if remaining:
                    report(remaining[0], 'FAIL', str(exc) if isinstance(exc, CheckFailure) else 'runtime assertion failed')
                    for name in remaining[1:]:
                        report(name, 'BLOCKED', 'Earlier runtime check failed')
            finally:
                if not args.keep_running:
                    try:
                        command(compose + ['stop', '--timeout', '10'], 60)
                        report('Isolated stack shutdown', 'PASS', 'Stopped containers; all databases and volumes retained')
                    except CheckFailure as exc:
                        report('Isolated stack shutdown', 'FAIL', str(exc))
                print(f'Isolated project: {project}; containers/volumes retained. See docs/runtime-verification.md for logs/recovery.', flush=True)
    if args.fetch_config:
        try:
            output = command([sys.executable, '-m', 'app.fetch_verification', '--config', str(args.fetch_config.resolve())], 150, accept_nonzero=True)
            for item in json.loads(output):
                report('Public HTTPS: ' + item['check'], item['status'], item.get('observed', item.get('reason', '')))
        except (CheckFailure, ValueError, TypeError, KeyError) as exc:
            report('Public TLS/DNS', 'FAIL', str(exc) if isinstance(exc, CheckFailure) else 'Invalid probe response')
    else:
        report('Public TLS/DNS', 'NOT RUN', 'No operator-controlled host configuration supplied')
    (ROOT / 'runtime-verification.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    return 1 if any(c['status'] == 'FAIL' for c in checks) else 2 if any(c['status'] == 'BLOCKED' for c in checks) else 0


if __name__ == '__main__':
    raise SystemExit(main())
