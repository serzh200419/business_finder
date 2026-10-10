import httpx
import pytest
from app.fetcher import (FetchError, FetchLimiter, PublicFetcher, PublicNetworkBackend,
                         PublicTransport, public_ip)
from app.schemas import AccessPolicy

POLICY = AccessPolicy(public_access_permitted=True, persistence_permitted=True,
                      export_permitted=False, policy_reference="synthetic test policy", retention_policy="synthetic")


def response(status=200, html="<title>Synthetic</title>", **headers):
    return httpx.Response(status, headers={"content-type": "text/html; charset=utf-8", **headers},
                          stream=httpx.ByteStream(html.encode()))


def fetcher(handler, **options):
    return PublicFetcher({"public.synthetic.example": POLICY, **options.pop("policies", {})},
                         transport=httpx.MockTransport(handler), limiter=FetchLimiter(interval=0), **options)


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/",
    "http://10.1.2.3/", "http://100.100.100.200/", "http://168.63.129.16/", "http://localhost/",
    "http://metadata.google.internal/", "file:///etc/passwd", "https://user:pass@public.synthetic.example/",
    "http://public.synthetic.example:8080/"])
def test_unsafe_targets_blocked_before_transport(url):
    client = fetcher(lambda _: pytest.fail("unsafe URL reached transport"))
    try:
        with pytest.raises(FetchError):
            client.fetch(url)
    finally:
        client.close()


@pytest.mark.parametrize("address", ["192.0.2.1", "198.51.100.1", "224.0.0.1", "0.0.0.0",
    "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::"])
def test_reserved_and_transition_addresses_blocked(address):
    with pytest.raises(FetchError):
        public_ip(address)


def test_private_redirect_and_policy_on_every_hop():
    calls = []
    def private(request):
        calls.append(str(request.url))
        return response(302, "", location="http://169.254.169.254/latest/meta-data/")
    client = fetcher(private)
    try:
        with pytest.raises(FetchError, match="blocked_address"):
            client.fetch("https://public.synthetic.example/")
        assert len(calls) == 1
    finally:
        client.close()
    client = fetcher(lambda _: response(302, "", location="https://other.synthetic.example/"))
    try:
        with pytest.raises(FetchError, match="access_policy_blocked"):
            client.fetch("https://public.synthetic.example/")
    finally:
        client.close()


def test_redirect_rights_intersection_and_request_count():
    calls = []
    def handler(request):
        return response(302, "", location="https://other.synthetic.example/") if request.url.host.startswith("public") else response()
    restrictive = POLICY.model_copy(update={"persistence_permitted": False})
    client = fetcher(handler, policies={"other.synthetic.example": restrictive})
    try:
        page = client.fetch("https://public.synthetic.example/", before_request=lambda: calls.append(1))
        assert len(calls) == 2 and not page.policy.persistence_permitted
        assert page.url == "https://other.synthetic.example/" and len(page.redirects) == 1
    finally:
        client.close()


@pytest.mark.parametrize("html,headers,code", [
    ("<div class='g-recaptcha'>Challenge</div>", {}, "access_challenge"),
    ('<input type="password">', {}, "access_challenge"),
    ("x" * 100, {}, "response_too_large"),
    ("pdf", {"content-type": "application/pdf"}, "unsupported_content"),
    ("compressed", {"content-encoding": "gzip"}, "unsupported_compression")])
def test_bounded_and_unsupported_responses(html, headers, code):
    client = fetcher(lambda _: response(html=html, **headers), max_bytes=50)
    try:
        with pytest.raises(FetchError, match=code):
            client.fetch("https://public.synthetic.example/")
    finally:
        client.close()


def test_redirect_limit_retry_after_and_rate_limit():
    client = fetcher(lambda _: response(302, "", location="/next"), max_redirects=1)
    try:
        with pytest.raises(FetchError, match="redirect_limit"):
            client.fetch("https://public.synthetic.example/")
    finally:
        client.close()
    client = fetcher(lambda _: response(429, "", **{"retry-after": "12"}))
    try:
        with pytest.raises(FetchError) as exc:
            client.fetch("https://public.synthetic.example/")
        assert exc.value.retryable and exc.value.retry_after == "12"
    finally:
        client.close()
    moment, sleeps = [0.0], []
    def sleep(seconds):
        sleeps.append(seconds)
        moment[0] += seconds
    limiter = FetchLimiter(interval=2, clock=lambda: moment[0], sleep=sleep)
    limiter.rate_limit("same.example")
    limiter.rate_limit("same.example")
    limiter.rate_limit("other.example")
    assert sleeps == [2.0]


class SyntheticStream:
    def __init__(self, peer="93.184.216.34"):
        self.peer, self.closed, self.writes, self.sni = peer, False, [], None
        self.data = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
    def read(self, max_bytes, timeout=None):
        data, self.data = self.data[:max_bytes], self.data[max_bytes:]
        return data
    def write(self, data, timeout=None):
        self.writes.append(data)
    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.sni = server_hostname
        assert ssl_context.check_hostname
        return self
    def get_extra_info(self, key):
        return (self.peer, 443) if key == "server_addr" else False if key == "is_readable" else None
    def close(self):
        self.closed = True


def test_dns_pinning_at_connection_and_rebinding_denied():
    resolutions, dials, streams = [], [], []
    def resolver(host, port, timeout):
        resolutions.append(host)
        return ["93.184.216.34"] if len(resolutions) == 1 else ["127.0.0.1"]
    def dialer(address, port, timeout):
        dials.append(address)
        stream = SyntheticStream()
        streams.append(stream)
        return stream
    backend = PublicNetworkBackend(resolver=resolver, dialer=dialer)
    client = PublicFetcher({"public.synthetic.example": POLICY}, transport=PublicTransport(backend), limiter=FetchLimiter(interval=0))
    try:
        assert client.fetch("https://public.synthetic.example/").html == "ok"
        assert dials == ["93.184.216.34"] and resolutions == ["public.synthetic.example"]
        assert streams[0].sni == "public.synthetic.example"
        assert b"Host: public.synthetic.example" in b"".join(streams[0].writes)
        with pytest.raises(FetchError, match="blocked_address"):
            client.fetch("https://public.synthetic.example/")
        assert len(dials) == 1  # No second hostname resolution in the socket dialer.
    finally:
        client.close()


def test_mixed_dns_answer_and_connected_peer_rejected():
    backend = PublicNetworkBackend(resolver=lambda *args: ["93.184.216.34", "10.0.0.1"],
                                   dialer=lambda *args: pytest.fail("mixed DNS answer was dialled"))
    with pytest.raises(FetchError, match="blocked_address"):
        backend.connect_tcp("public.synthetic.example", 443)
    stream = SyntheticStream(peer="127.0.0.1")
    backend = PublicNetworkBackend(resolver=lambda *args: ["93.184.216.34"], dialer=lambda *args: stream)
    with pytest.raises(FetchError, match="blocked_peer"):
        backend.connect_tcp("public.synthetic.example", 443)
    assert stream.closed
