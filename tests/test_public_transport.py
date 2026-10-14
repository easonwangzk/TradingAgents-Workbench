import socket
import ssl

import httpx
import pytest


class RecordingStream:
    def __init__(self, chunks, *, events):
        self._chunks = list(chunks)
        self._events = events

    def read(self, max_bytes, timeout=None):
        self._events.append(("read", max_bytes, timeout))
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if len(chunk) <= max_bytes:
            return chunk
        self._chunks.insert(0, chunk[max_bytes:])
        return chunk[:max_bytes]

    def write(self, buffer, timeout=None):
        self._events.append(("write", bytes(buffer), timeout))

    def close(self):
        self._events.append(("close",))

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._events.append(("start_tls", server_hostname, timeout))
        return self

    def get_extra_info(self, info):
        if info == "ssl_object":
            return type("SslObject", (), {"selected_alpn_protocol": lambda self: None})()
        return None


class RecordingBackend:
    def __init__(self, *, response_chunks):
        self.response_chunks = response_chunks
        self.events = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.events.append(("connect_tcp", host, port, timeout, local_address, socket_options))
        return RecordingStream(list(self.response_chunks), events=self.events)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("unix sockets are not expected")

    def sleep(self, seconds):
        self.events.append(("sleep", seconds))


@pytest.mark.unit
def test_pinned_transport_connects_to_validated_literal_ip_and_preserves_tls_hostname():
    from tradingagents.workbench.public_transport import PinnedTransport

    backend = RecordingBackend(
        response_chunks=[
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: application/json\r\nContent-Length: 11\r\n\r\n",
            b'{"ok":true}',
        ]
    )
    resolver_calls = []

    def resolver(host, port, family=0, type=0, proto=0, flags=0):
        resolver_calls.append((host, port, family, type, proto, flags))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2606:2800:220:1:248:1893:25c8:1946", port, 0, 0),
            ),
        ]

    transport = PinnedTransport(network_backend=backend, resolver=resolver)
    with httpx.Client(transport=transport, follow_redirects=False, timeout=httpx.Timeout(20.0, connect=5.0)) as client:
        response = client.send(httpx.Request("POST", "https://api.example.com/v1/responses", content=b"{}"))

    assert response.status_code == 200
    assert resolver_calls == [("api.example.com", 443, 0, 1, 6, 0)]
    assert backend.events[0][:3] == ("connect_tcp", "93.184.216.34", 443)
    assert ("start_tls", "api.example.com", 5.0) in backend.events


@pytest.mark.unit
def test_pinned_transport_rejects_mixed_public_and_private_resolution_before_connect():
    from tradingagents.workbench.public_transport import PinnedTransport, TransportPolicyError

    backend = RecordingBackend(response_chunks=[])

    def resolver(host, port, family=0, type=0, proto=0, flags=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.4", port)),
        ]

    transport = PinnedTransport(network_backend=backend, resolver=resolver)
    with (
        httpx.Client(transport=transport, follow_redirects=False, timeout=httpx.Timeout(20.0, connect=5.0)) as client,
        pytest.raises(TransportPolicyError),
    ):
        client.send(httpx.Request("POST", "https://api.example.com/v1/responses", content=b"{}"))

    assert backend.events == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@api.example.com/v1/responses",
        "https://api.example.com:444/v1/responses",
    ],
)
def test_pinned_transport_rejects_userinfo_and_non_443_port_before_connect(url):
    from tradingagents.workbench.public_transport import PinnedTransport, TransportPolicyError

    backend = RecordingBackend(response_chunks=[])
    transport = PinnedTransport(network_backend=backend, resolver=lambda *args: [])

    with (
        httpx.Client(transport=transport, follow_redirects=False, timeout=httpx.Timeout(20.0, connect=5.0)) as client,
        pytest.raises(TransportPolicyError),
    ):
        client.send(httpx.Request("POST", url, content=b"{}"))

    assert backend.events == []


@pytest.mark.unit
def test_create_pinned_http_client_disables_redirects_and_environment_proxies():
    from tradingagents.workbench.public_transport import PinnedTransport, create_pinned_http_client

    with create_pinned_http_client() as client:
        assert client.follow_redirects is False
        assert client._trust_env is False
        assert isinstance(client._transport, PinnedTransport)
        assert client.timeout.as_dict() == {"connect": 5.0, "read": 20.0, "write": 20.0, "pool": 5.0}


@pytest.mark.unit
@pytest.mark.parametrize("status_code", [200, 429])
def test_pinned_client_discards_upstream_cookies_and_remains_valid(status_code):
    from tradingagents.workbench.public_transport import (
        create_pinned_http_client,
        validate_gateway_http_client,
    )

    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(status_code, headers={"set-cookie": "upstream=fixture; Path=/; Secure"}, json={"ok": True})

    with create_pinned_http_client(transport=httpx.MockTransport(upstream)) as client:
        client.send(httpx.Request("GET", "https://api.example.com/models"))
        assert not client.cookies
        validate_gateway_http_client(client)
        client.get("https://api.example.com/models")
        assert all("cookie" not in request.headers for request in requests)


@pytest.mark.unit
@pytest.mark.parametrize(
    "verify",
    [
        False,
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    ],
)
def test_pinned_transport_rejects_insecure_tls_configuration(verify):
    from tradingagents.workbench.public_transport import PinnedTransport, TransportPolicyError

    if isinstance(verify, ssl.SSLContext):
        verify.check_hostname = False
        verify.verify_mode = ssl.CERT_NONE

    with pytest.raises(TransportPolicyError):
        PinnedTransport(verify=verify)


@pytest.mark.unit
def test_validate_gateway_http_client_rejects_mutated_event_hooks_and_mounts():
    from tradingagents.workbench.public_transport import (
        TransportPolicyError,
        create_pinned_http_client,
        validate_gateway_http_client,
    )

    client = create_pinned_http_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})))
    client.event_hooks["request"].append(lambda request: None)
    with pytest.raises(TransportPolicyError):
        validate_gateway_http_client(client)
    client.event_hooks["request"].clear()
    client._mounts["all://"] = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with pytest.raises(TransportPolicyError):
        validate_gateway_http_client(client)
    client.close()


@pytest.mark.unit
def test_pinned_transport_rejects_malformed_getaddrinfo_rows_without_uncaught_value_error():
    from tradingagents.workbench.public_transport import PinnedTransport, TransportPolicyError

    backend = RecordingBackend(response_chunks=[])

    def resolver(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (object(), port))]

    transport = PinnedTransport(network_backend=backend, resolver=resolver)
    with (
        httpx.Client(transport=transport, follow_redirects=False, timeout=httpx.Timeout(20.0, connect=5.0)) as client,
        pytest.raises(TransportPolicyError),
    ):
        client.send(httpx.Request("POST", "https://api.example.com/v1/responses", content=b"{}"))

    assert backend.events == []
