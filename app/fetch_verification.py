"""Opt-in controlled-public-host probe using the production pinned transport."""
import argparse
import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit
import httpcore
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from app.fetcher import PublicFetcher, PublicTransport, PublicNetworkBackend, FetchError, FetchLimiter, validate_url
from app.schemas import AccessPolicy


class ProbeCase(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Literal['valid_tls_host_sni', 'allowed_redirect', 'denied_redirect', 'timeout', 'response_size', 'invalid_certificate']
    url: str
    expected: Literal['ok', 'blocked_address', 'access_policy_blocked', 'timeout', 'response_too_large', 'tls_verification_failed']
    marker: str = ''
    expected_final_url: str | None = None
    max_bytes: int = Field(default=262144, ge=64, le=1048576)
    timeout: float = Field(default=3, ge=.1, le=10)
    total_timeout: float = Field(default=10, ge=.1, le=20)


class ProbeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    policy_reference: str = Field(min_length=1)
    allowed_hosts: list[str] = Field(min_length=1, max_length=10)
    cases: list[ProbeCase] = Field(min_length=1, max_length=6)

    @model_validator(mode='after')
    def controlled_only(self):
        if len({c.name for c in self.cases}) != len(self.cases):
            raise ValueError('Duplicate probe names')
        expected = {'valid_tls_host_sni': {'ok'}, 'allowed_redirect': {'ok'},
            'denied_redirect': {'blocked_address', 'access_policy_blocked'}, 'timeout': {'timeout'},
            'response_size': {'response_too_large'}, 'invalid_certificate': {'tls_verification_failed'}}
        for case in self.cases:
            if urlsplit(case.url).scheme != 'https' or validate_url(case.url) not in self.allowed_hosts:
                raise ValueError('Probe requires an explicit allowed public HTTPS host')
            if case.expected not in expected[case.name]:
                raise ValueError('Probe expectation does not exercise the named protection')
            if case.expected_final_url and (urlsplit(case.expected_final_url).scheme != 'https' or validate_url(case.expected_final_url) not in self.allowed_hosts):
                raise ValueError('Expected destination must be explicitly allowed HTTPS')
            if case.name in {'valid_tls_host_sni', 'allowed_redirect'} and not case.marker:
                raise ValueError('Controlled success pages need an explicit marker')
        for host in self.allowed_hosts:
            if validate_url(f'https://{host}/') != host:
                raise ValueError('Allowed hosts must be normalized exact hostnames')
        return self


class ObservedStream(httpcore.NetworkStream):
    def __init__(self, stream, host, events):
        self.stream, self.host, self.events = stream, host, events

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        for line in buffer.split(b'\r\n'):
            if line.lower().startswith(b'host:'):
                self.events.append({'host_preserved': line[5:].strip().decode('ascii') == self.host})
        return self.stream.write(buffer, timeout)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.check_hostname and ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert server_hostname == self.host
        stream = self.stream.start_tls(ssl_context, server_hostname, timeout)
        certificate = stream.get_extra_info('ssl_object')
        assert certificate and certificate.getpeercert()
        self.events.append({'tls_verified': True, 'sni_preserved': True})
        return ObservedStream(stream, self.host, self.events)

    def get_extra_info(self, key):
        return self.stream.get_extra_info(key)

    def close(self):
        self.stream.close()


class ObservedBackend(PublicNetworkBackend):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if port != 443:
            raise FetchError('unsupported_port')  # Controlled probes never follow an HTTP downgrade.
        stream = super().connect_tcp(host, port, timeout, local_address, socket_options)
        self.events.append({'public_peer_pinned': True})
        return ObservedStream(stream, host, self.events)


def run_probes(config: ProbeConfig):
    policies = {host: AccessPolicy(public_access_permitted=True, persistence_permitted=False,
        export_permitted=False, retention_policy='do not persist', policy_reference=config.policy_reference) for host in config.allowed_hosts}
    results = []
    for case in config.cases:
        events = []
        fetcher = PublicFetcher(policies, transport=PublicTransport(ObservedBackend(events)),
            limiter=FetchLimiter(interval=1), timeout=case.timeout, total_timeout=case.total_timeout, max_bytes=case.max_bytes)
        actual = 'ok'
        try:
            page = fetcher.fetch(case.url)
            assert case.marker in page.html if case.marker else True
            assert not case.expected_final_url or page.url == case.expected_final_url
            if case.name == 'allowed_redirect':
                assert page.redirects
            if case.expected == 'ok':
                assert any(e.get('tls_verified') for e in events)
                assert any(e.get('host_preserved') for e in events)
                assert all('host_preserved' not in e or e['host_preserved'] for e in events)
        except FetchError as exc:
            actual = exc.code
        except Exception:
            actual = 'probe_assertion_failed'
        finally:
            fetcher.close()
        if actual == case.expected and case.name != 'invalid_certificate':
            # A denial/limit is meaningful only after reaching the configured public TLS source.
            if not any(e.get('tls_verified') for e in events) or not any(e.get('host_preserved') for e in events):
                actual = 'controlled_source_not_reached'
        if actual == case.expected and case.name == 'invalid_certificate' and not events:
            actual = 'controlled_source_not_reached'
        results.append({'check': case.name, 'status': 'PASS' if actual == case.expected else 'FAIL', 'observed': actual})
    configured = {case.name for case in config.cases}
    for name in ProbeCase.model_fields['name'].annotation.__args__:
        if name not in configured:
            results.append({'check': name, 'status': 'NOT RUN', 'reason': 'controlled endpoint not configured'})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        results = run_probes(ProbeConfig.model_validate_json(args.config.read_text(encoding='utf-8-sig')))
    except Exception:
        print(json.dumps([{'check': 'public_https', 'status': 'FAIL', 'reason': 'invalid controlled-host configuration'}]))
        raise SystemExit(1) from None
    print(json.dumps(results))
    raise SystemExit(1 if any(r['status'] == 'FAIL' for r in results) else 0)


if __name__ == '__main__':
    main()
