import asyncio

import httpx

from fastapi_prod_hardening.http import HttpClients


def echo_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


async def test_client_is_a_singleton_within_a_loop():
    clients = HttpClients(transport=echo_transport([]))
    assert clients.get() is clients.get()
    await clients.aclose()


def test_one_client_per_event_loop():
    clients = HttpClients(transport=echo_transport([]))

    async def grab():
        client = clients.get()
        await clients.aclose()
        return client

    assert asyncio.run(grab()) is not asyncio.run(grab())


async def test_tenant_headers_are_per_request_never_stored_on_the_client():
    seen: list[httpx.Request] = []
    clients = HttpClients(transport=echo_transport(seen))
    await clients.request("GET", "http://svc.test/a", tenant_id="t1", token="tok-1")
    await clients.request("GET", "http://svc.test/b", tenant_id="t2")
    assert seen[0].headers["x-tenant-id"] == "t1" and seen[0].headers["authorization"] == "Bearer tok-1"
    assert seen[1].headers["x-tenant-id"] == "t2" and "authorization" not in seen[1].headers
    assert "x-tenant-id" not in clients.get().headers and "authorization" not in clients.get().headers
    await clients.aclose()


async def test_closed_client_is_replaced_on_next_use():
    clients = HttpClients(transport=echo_transport([]))
    first = clients.get()
    await clients.aclose()
    assert first.is_closed and clients.get() is not first
    await clients.aclose()


async def test_explicit_limits_and_timeouts_are_applied():
    clients = HttpClients(max_connections=7, timeout=httpx.Timeout(3.0), transport=echo_transport([]))
    assert clients.limits.max_connections == 7 and clients.get().timeout.read == 3.0
    await clients.aclose()


def test_http2_is_enabled_when_h2_is_installed():
    assert HttpClients().http2 is True  # h2 arrives with httpx[http2]
    assert HttpClients(http2=False).http2 is False


def test_app_lifespan_closes_the_shared_client():
    from fastapi.testclient import TestClient

    from fastapi_prod_hardening.app import create_app

    clients = HttpClients(transport=echo_transport([]))
    with TestClient(create_app(http=clients)):
        assert not clients._clients
    assert not clients._clients
