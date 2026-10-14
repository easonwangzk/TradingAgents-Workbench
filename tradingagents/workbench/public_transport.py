"""Pinned HTTPS transport for public workbench outbound requests."""

import ipaddress
import re
import socket
import ssl
from http.cookiejar import CookieJar, DefaultCookiePolicy
from types import MappingProxyType

import httpcore
import httpx
from httpcore import NetworkBackend, NetworkStream
from httpcore._backends.sync import SyncBackend
from httpx._config import DEFAULT_LIMITS, create_ssl_context
from httpx._transports.default import ResponseStream, map_httpcore_exceptions

_HOST_RE = re.compile(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
_DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0, read=20.0, write=20.0, pool=5.0)


def _default_headers():
    with httpx.Client(trust_env=False) as client:
        return MappingProxyType(dict(client.headers))


_DEFAULT_HEADERS = _default_headers()


class TransportPolicyError(RuntimeError):
    pass


def _validate_tls_configuration(verify):
    if verify is True:
        return
    if isinstance(verify, ssl.SSLContext) and (
        verify.verify_mode != ssl.CERT_REQUIRED or not verify.check_hostname
    ):
        raise TransportPolicyError("Outbound provider destination is invalid.")
    raise TransportPolicyError("Outbound provider destination is invalid.")


def _validate_hostname(host):
    if type(host) is not str or not _HOST_RE.fullmatch(host):
        raise TransportPolicyError("Outbound provider destination is invalid.")
    if "%" in host:
        raise TransportPolicyError("Outbound provider destination is invalid.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host.lower()
    raise TransportPolicyError("Outbound provider destination is invalid.")


def _validated_public_ip(value):
    if type(value) is not str or "%" in value:
        raise TransportPolicyError("Outbound provider destination is invalid.")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise TransportPolicyError("Outbound provider destination is invalid.") from None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        raise TransportPolicyError("Outbound provider destination is invalid.")
    if (
        not ip.is_global
        or ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise TransportPolicyError("Outbound provider destination is invalid.")
    return ip.compressed


def _resolve_public_ips(host, port, resolver):
    validated_host = _validate_hostname(host)
    if port != 443:
        raise TransportPolicyError("Outbound provider destination is invalid.")
    try:
        records = resolver(validated_host, port, 0, socket.SOCK_STREAM, socket.IPPROTO_TCP, 0)
    except OSError:
        raise TransportPolicyError("Outbound provider destination is invalid.") from None
    if not records:
        raise TransportPolicyError("Outbound provider destination is invalid.")
    addresses = []
    try:
        for family, socktype, proto, _canonname, sockaddr in records:
            if socktype != socket.SOCK_STREAM or proto not in (0, socket.IPPROTO_TCP):
                raise TransportPolicyError("Outbound provider destination is invalid.")
            if family == socket.AF_INET:
                if type(sockaddr) is not tuple or len(sockaddr) != 2:
                    raise TransportPolicyError("Outbound provider destination is invalid.")
                addresses.append(_validated_public_ip(sockaddr[0]))
            elif family == socket.AF_INET6:
                if type(sockaddr) is not tuple or len(sockaddr) != 4 or sockaddr[2] != 0 or sockaddr[3] != 0:
                    raise TransportPolicyError("Outbound provider destination is invalid.")
                addresses.append(_validated_public_ip(sockaddr[0]))
            else:
                raise TransportPolicyError("Outbound provider destination is invalid.")
    except (TypeError, ValueError):
        raise TransportPolicyError("Outbound provider destination is invalid.") from None
    if not addresses:
        raise TransportPolicyError("Outbound provider destination is invalid.")
    return validated_host, tuple(addresses)


class VerifiedNetworkStream(NetworkStream):
    def __init__(self, wrapped, *, host):
        self._wrapped = wrapped
        self._host = host

    def read(self, max_bytes, timeout=None):
        return self._wrapped.read(max_bytes, timeout=timeout)

    def write(self, buffer, timeout=None):
        return self._wrapped.write(buffer, timeout=timeout)

    def close(self):
        return self._wrapped.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        if server_hostname != self._host:
            raise TransportPolicyError("Outbound provider destination is invalid.")
        stream = self._wrapped.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            timeout=timeout,
        )
        if stream is self._wrapped:
            return self
        return VerifiedNetworkStream(stream, host=self._host)

    def get_extra_info(self, info):
        return self._wrapped.get_extra_info(info)


class VerifiedNetworkBackend(NetworkBackend):
    def __init__(self, *, backend=None, resolver=socket.getaddrinfo):
        self._backend = SyncBackend() if backend is None else backend
        self._resolver = resolver

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        validated_host, addresses = _resolve_public_ips(host, port, self._resolver)
        stream = self._backend.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        return VerifiedNetworkStream(stream, host=validated_host)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise TransportPolicyError("Outbound provider destination is invalid.")

    def sleep(self, seconds):
        self._backend.sleep(seconds)


class PinnedTransport(httpx.BaseTransport):
    def __init__(
        self,
        *,
        verify=True,
        cert=None,
        http1=True,
        http2=False,
        limits=DEFAULT_LIMITS,
        local_address=None,
        socket_options=None,
        network_backend=None,
        resolver=socket.getaddrinfo,
    ):
        _validate_tls_configuration(verify)
        ssl_context = create_ssl_context(verify=verify, cert=cert, trust_env=False)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=http1,
            http2=http2,
            retries=0,
            local_address=local_address,
            network_backend=VerifiedNetworkBackend(backend=network_backend, resolver=resolver),
            socket_options=socket_options,
        )

    def __enter__(self):
        self._pool.__enter__()
        return self

    def __exit__(self, exc_type=None, exc_value=None, traceback=None):
        with map_httpcore_exceptions():
            self._pool.__exit__(exc_type, exc_value, traceback)

    def close(self):
        with map_httpcore_exceptions():
            self._pool.close()

    def handle_request(self, request):
        if request.url.scheme != "https" or request.url.username or request.url.password:
            raise TransportPolicyError("Outbound provider destination is invalid.")
        if request.url.host is None or request.url.port not in (None, 443):
            raise TransportPolicyError("Outbound provider destination is invalid.")
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with map_httpcore_exceptions():
            resp = self._pool.handle_request(req)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=ResponseStream(resp.stream),
            extensions=resp.extensions,
        )


class StatelessCookiePolicy(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def create_pinned_http_client(*, transport=None, timeout=_DEFAULT_TIMEOUT):
    return httpx.Client(
        transport=PinnedTransport() if transport is None else transport,
        cookies=CookieJar(policy=StatelessCookiePolicy()),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )


def validate_gateway_http_client(client):
    if not isinstance(client, httpx.Client):
        raise TransportPolicyError("Outbound provider client is unavailable.")
    transport = getattr(client, "_transport", None)
    if not isinstance(transport, (PinnedTransport, httpx.MockTransport)):
        raise TransportPolicyError("Outbound provider client is unavailable.")
    if client.follow_redirects:
        raise TransportPolicyError("Outbound provider client is unavailable.")
    if getattr(client, "_auth", None) is not None or str(getattr(client, "base_url", "")):
        raise TransportPolicyError("Outbound provider client is unavailable.")
    if str(getattr(client, "_params", "")) or getattr(client, "cookies", None):
        raise TransportPolicyError("Outbound provider client is unavailable.")
    event_hooks = getattr(client, "event_hooks", None)
    if type(event_hooks) is not dict:
        raise TransportPolicyError("Outbound provider client is unavailable.")
    if any(type(items) is not list or items for items in event_hooks.values()):
        raise TransportPolicyError("Outbound provider client is unavailable.")
    mounts = getattr(client, "_mounts", None)
    if type(mounts) is not dict or mounts:
        raise TransportPolicyError("Outbound provider client is unavailable.")
    if dict(client.headers) != dict(_DEFAULT_HEADERS):
        raise TransportPolicyError("Outbound provider client is unavailable.")
    if isinstance(transport, PinnedTransport) and getattr(client, "_trust_env", True):
        raise TransportPolicyError("Outbound provider client is unavailable.")
    return client
