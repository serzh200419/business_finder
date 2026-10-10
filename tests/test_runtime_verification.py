import importlib.util
import json
from pathlib import Path
import ssl
import httpx
import httpcore
import pytest
from pydantic import ValidationError
from app.fetcher import PublicFetcher, FetchError, FetchLimiter
from app.fetch_verification import ProbeConfig
from app.schemas import AccessPolicy
from integration_tests.safety import require_integration_database


def test_runtime_guard_rejects_non_test_database(monkeypatch):
    monkeypatch.setenv('BF_RUNTIME_INTEGRATION', '1')
    for url in ('sqlite:///test.db', 'postgresql+psycopg://localhost/leads', 'postgresql+psycopg://localhost/production'):
        monkeypatch.setenv('DATABASE_URL', url)
        with pytest.raises(RuntimeError, match='Dedicated'):
            require_integration_database()
    monkeypatch.setenv('DATABASE_URL', 'postgresql+psycopg://localhost/bf_integration')
    assert require_integration_database().database == 'bf_integration'


def test_operator_summary_reports_missing_docker_as_blocked(monkeypatch, tmp_path, capsys):
    spec = importlib.util.spec_from_file_location('runtime_verify', Path('scripts/runtime_verify.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module.shutil, 'which', lambda _: None)
    assert module.main(['--skip-fast']) == 2
    output = capsys.readouterr().out
    assert 'BLOCKED' in output and 'NOT RUN Public TLS/DNS' in output
    state = json.loads((tmp_path/'runtime-verification.json').read_text())
    assert not any(c['status'] == 'PASS' for c in state['checks'])


def test_verification_command_failure_never_prints_subprocess_payload(monkeypatch):
    spec = importlib.util.spec_from_file_location('runtime_verify_redaction', Path('scripts/runtime_verify.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from subprocess import CompletedProcess
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: CompletedProcess(a[0], 1,
        'password-bearing URL and restricted provider payload', 'sensitive failure detail'))
    with pytest.raises(module.CheckFailure) as failure:
        module.command(['synthetic-command'])
    assert str(failure.value) == 'command returned exit 1'


def test_controlled_fetch_config_cannot_claim_wrong_check_or_private_source():
    base = {'policy_reference': 'owned test host', 'allowed_hosts': ['controlled.example'], 'cases': [
        {'name': 'valid_tls_host_sni', 'url': 'https://controlled.example/ok', 'expected': 'ok', 'marker': 'BFVERIFY_OK'}]}
    assert ProbeConfig.model_validate(base).cases[0].expected == 'ok'
    for case in ({'name': 'timeout', 'url': 'https://controlled.example/delay', 'expected': 'ok'},
                 {'name': 'valid_tls_host_sni', 'url': 'https://127.0.0.1/', 'expected': 'ok', 'marker': 'x'},
                 {'name': 'valid_tls_host_sni', 'url': 'http://controlled.example/', 'expected': 'ok', 'marker': 'x'}):
        with pytest.raises((ValidationError, FetchError)):
            ProbeConfig.model_validate({**base, 'cases': [case]})


@pytest.mark.parametrize('failure,expected', [('timeout', 'timeout'), ('certificate', 'tls_verification_failed')])
def test_deterministic_transport_failure_classification(failure, expected):
    def handler(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('synthetic timeout')
        try:
            raise ssl.SSLCertVerificationError('synthetic certificate rejection')
        except ssl.SSLCertVerificationError as exc:
            raise httpcore.ConnectError('synthetic TLS connect error') from exc
    client = PublicFetcher({'controlled.example': AccessPolicy(public_access_permitted=True, policy_reference='synthetic')},
        transport=httpx.MockTransport(handler), limiter=FetchLimiter(interval=0))
    try:
        with pytest.raises(FetchError) as caught:
            client.fetch('https://controlled.example/')
        assert caught.value.code == expected
        assert caught.value.retryable == (failure == 'timeout')
    finally:
        client.close()
