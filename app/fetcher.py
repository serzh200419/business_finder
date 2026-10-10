"""Bounded public HTTP fetches; validated IPs are dialled directly, never re-resolved."""
import ipaddress
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit
import httpcore
import httpx
from httpcore._backends.sync import SyncStream
from app.schemas import AccessPolicy


class FetchError(Exception):
    def __init__(self, code: str, *, retryable: bool = False, retry_after: str | None = None):
        super().__init__(code)
        self.code, self.retryable, self.retry_after = code, retryable, retry_after


def public_ip(value: str) -> str:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise FetchError("blocked_address")
    forbidden = ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
    if (not ip.is_global or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            or getattr(ip, "ipv4_mapped", None) or str(ip) == "168.63.129.16"
            or any(ip in ipaddress.ip_network(network) for network in forbidden if ip.version == 6)):
        raise FetchError("blocked_address")
    return str(ip)


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise FetchError("unsupported_url")
        if parts.port not in {None, 80, 443} or "\\" in url or any(ord(c) < 32 for c in url):
            raise FetchError("unsupported_url")
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")) or "%" in host:
            raise FetchError("blocked_address")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            public_ip(host)
        return host
    except (ValueError, UnicodeError):
        raise FetchError("unsupported_url")


_dns_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="public-dns")
_dns_slots = threading.BoundedSemaphore(4)


def resolve_addresses(host: str, port: int, timeout: float | None) -> list[str]:
    try:
        return [public_ip(host)]
    except FetchError:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise
    if not _dns_slots.acquire(blocking=False):
        raise FetchError("dns_capacity", retryable=True)
    future = _dns_pool.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    future.add_done_callback(lambda _: _dns_slots.release())
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in future.result(timeout=timeout or 5)))
        return [public_ip(address) for address in addresses]
    except (FutureTimeout, socket.gaierror):
        raise FetchError("dns_unavailable", retryable=True)


def dial_numeric(address: str, port: int, timeout: float | None):
    ip = ipaddress.ip_address(public_ip(address))
    sock = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Numeric sockaddr on an explicitly selected family: no second DNS lookup.
        sock.connect((str(ip), port, 0, 0) if ip.version == 6 else (str(ip), port))
        return SyncStream(sock)
    except TimeoutError as exc:
        sock.close()
        raise FetchError("timeout", retryable=True) from exc
    except OSError as exc:
        sock.close()
        raise FetchError("connection_unavailable", retryable=True) from exc


class PublicNetworkBackend(httpcore.NetworkBackend):
    def __init__(self, resolver=resolve_addresses, dialer=dial_numeric):
        self.resolver, self.dialer = resolver, dialer

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        validate_url(f"https://[{host}]/" if ":" in host else f"https://{host}/")
        if port not in {80, 443}:
            raise FetchError("unsupported_port")
        started = time.monotonic()
        addresses = self.resolver(host, port, timeout)
        # Reject the entire DNS answer if any address is unsafe, before connecting.
        addresses = [public_ip(address) for address in addresses]
        if not addresses:
            raise FetchError("dns_unavailable", retryable=True)
        remaining = max(0.01, (timeout or 5) - (time.monotonic() - started))
        stream = self.dialer(addresses[0], port, remaining)
        peer = stream.get_extra_info("server_addr")
        try:
            valid_peer = bool(peer and public_ip(peer[0]) == addresses[0])
        except FetchError:
            valid_peer = False
        if not valid_peer:
            stream.close()
            raise FetchError("blocked_peer")
        return stream

    def connect_unix_socket(self, *args, **kwargs):
        raise FetchError("unsupported_transport")


class CoreStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        yield from self.stream

    def close(self):
        self.stream.close()


class PublicTransport(httpx.BaseTransport):
    def __init__(self, backend=None, concurrency=2):
        self.pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(),
            network_backend=backend or PublicNetworkBackend(), max_connections=concurrency,
            max_keepalive_connections=0, retries=0, http2=False)

    def handle_request(self, request):
        validate_url(str(request.url))
        response = self.pool.handle_request(httpcore.Request(method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                            port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions))
        return httpx.Response(response.status, headers=response.headers, stream=CoreStream(response.stream),
                              extensions=response.extensions)

    def close(self):
        self.pool.close()


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    url: str
    html: str
    redirects: tuple[str, ...]
    policy: AccessPolicy


class FetchLimiter:
    def __init__(self, concurrency=2, interval=1.0, clock=time.monotonic, sleep=time.sleep):
        self.slots = threading.BoundedSemaphore(concurrency)
        self.lock = threading.Lock()
        self.next_allowed: dict[str, float] = {}
        self.interval, self.clock, self.sleep = interval, clock, sleep

    def rate_limit(self, host: str):
        with self.lock:
            at = self.clock()
            delay = max(0, self.next_allowed.get(host, at) - at)
            self.next_allowed[host] = at + delay + self.interval
        if delay:
            self.sleep(delay)


def intersect_policy(left: AccessPolicy, right: AccessPolicy) -> AccessPolicy:
    return AccessPolicy(public_access_permitted=left.public_access_permitted and right.public_access_permitted,
        persistence_permitted=left.persistence_permitted and right.persistence_permitted,
        export_permitted=left.export_permitted and right.export_permitted,
        retention_policy="; ".join(dict.fromkeys([left.retention_policy, right.retention_policy])),
        policy_reference="; ".join(dict.fromkeys([left.policy_reference, right.policy_reference])))


class PublicFetcher:
    def __init__(self, policies: dict[str, AccessPolicy], *, transport=None, limiter=None,
                 max_bytes=262144, max_redirects=3, timeout=5.0, total_timeout=15.0):
        self.policies = policies
        self.limiter = limiter or FetchLimiter()
        self.max_bytes, self.max_redirects = max_bytes, max_redirects
        self.timeout, self.total_timeout = timeout, total_timeout
        self.client = httpx.Client(transport=transport or PublicTransport(), trust_env=False,
                                  follow_redirects=False, timeout=timeout, headers={
                                      "User-Agent": "BusinessFinderPublicReview/0.1", "Accept-Encoding": "identity",
                                      "Accept": "text/html,application/xhtml+xml"})

    def fetch(self, url: str, before_request: Callable[[], None] = lambda: None) -> FetchedPage:
        original, redirects = url, []
        policy = None
        start = time.monotonic()
        if not self.limiter.slots.acquire(timeout=self.timeout):
            raise FetchError("concurrency_limit", retryable=True)
        try:
            for _ in range(self.max_redirects + 1):
                host = validate_url(url)
                hop_policy = self.policies.get(host, AccessPolicy())
                if not hop_policy.public_access_permitted or not hop_policy.policy_reference:
                    raise FetchError("access_policy_blocked")
                policy = hop_policy if policy is None else intersect_policy(policy, hop_policy)
                if urlsplit(url).path.lower().startswith(("/login", "/signin", "/captcha", "/challenge")):
                    raise FetchError("access_challenge")
                self.limiter.rate_limit(host)
                remaining = self.total_timeout - (time.monotonic() - start)
                if remaining <= 0:
                    raise FetchError("timeout", retryable=True)
                before_request()  # Budgets include redirects and errors, before each attempt.
                try:
                    with self.client.stream("GET", url, timeout=min(self.timeout, remaining)) as response:
                        if response.status_code in {401, 403}:
                            raise FetchError("access_challenge")
                        if response.status_code == 429 or response.status_code >= 500:
                            raise FetchError("page_unavailable", retryable=True, retry_after=response.headers.get("retry-after"))
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if not response.headers.get("location"):
                                raise FetchError("unsupported_redirect")
                            url = urljoin(url, response.headers["location"])
                            redirects.append(url)
                            continue
                        if response.status_code != 200:
                            raise FetchError("page_unavailable")
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                        if content_type not in {"text/html", "application/xhtml+xml"}:
                            raise FetchError("unsupported_content")
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise FetchError("unsupported_compression")
                        size = response.headers.get("content-length")
                        if size and (not size.isdigit() or int(size) > self.max_bytes):
                            raise FetchError("response_too_large")
                        chunks, length = [], 0
                        for chunk in response.iter_raw():
                            length += len(chunk)
                            if length > self.max_bytes:
                                raise FetchError("response_too_large")
                            if time.monotonic() - start > self.total_timeout:
                                raise FetchError("timeout", retryable=True)
                            chunks.append(chunk)
                        if time.monotonic() - start > self.total_timeout:
                            raise FetchError("timeout", retryable=True)
                        html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                        lowered = html.casefold()
                        if any(marker in lowered for marker in ("g-recaptcha", "hcaptcha", "cf-chl-", "verify you are human")):
                            raise FetchError("access_challenge")
                        if 'type="password"' in lowered or "type='password'" in lowered:
                            raise FetchError("access_challenge")
                        return FetchedPage(original, str(response.url), html, tuple(redirects), policy)
                except (httpx.TimeoutException, httpcore.TimeoutException, TimeoutError):
                    raise FetchError("timeout", retryable=True)
                except (httpx.HTTPError, httpcore.NetworkError, httpcore.ProtocolError, OSError) as exc:
                    cause, seen = exc, set()
                    while cause is not None and id(cause) not in seen:
                        seen.add(id(cause))
                        if isinstance(cause, ssl.SSLCertVerificationError):
                            raise FetchError("tls_verification_failed") from None
                        cause = cause.__cause__ or cause.__context__
                    raise FetchError("connection_unavailable", retryable=True)
            raise FetchError("redirect_limit")
        finally:
            self.limiter.slots.release()

    def close(self):
        self.client.close()
