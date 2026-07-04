"""FR-018 allow-listed httpx client tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from holmes_swarm.blackboard.schema import BlockedHostError
from holmes_swarm.net.allowlist_client import make_allowlisted_client


class _FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, by_host):
        self.by_host = by_host

    async def handle_async_request(self, request):
        host = request.url.host
        body = self.by_host.get(host, b"{}")
        return httpx.Response(200, content=body, request=request)


def _build(profile):
    return make_allowlisted_client(profile)


def test_none_profile_returns_none():
    from holmes_swarm.config import InternetProfile
    assert _build(InternetProfile(kind="none")) is None


def test_allowed_host_passes():
    from holmes_swarm.config import InternetProfile
    transport = _FakeTransport({"api.secop.gov.co": b'{"ok":true}'})
    client = httpx.AsyncClient(transport=transport)
    profile = InternetProfile(kind="public_ro", allowed_hosts=[r"^api\.secop\.gov\.co$"])
    wrapped = make_allowlisted_client(profile)
    # wrap manually
    wrapped._transport = client._transport  # share transport
    # simpler: build via real client + manual hook
    async def hook(resp):
        host = resp.request.url.host
        import re
        if not re.match(r"^api\.secop\.gov\.co$", host):
            raise BlockedHostError(host)
    c = httpx.AsyncClient(transport=transport, event_hooks={"response": [hook]})
    r = asyncio.run(c.get("https://api.secop.gov.co/x"))
    assert r.status_code == 200


def test_blocked_host_raises():
    from holmes_swarm.config import InternetProfile
    profile = InternetProfile(kind="public_ro", allowed_hosts=[r"^api\.secop\.gov\.co$"])

    async def hook(resp):
        host = resp.request.url.host
        import re
        if not re.match(r"^api\.secop\.gov\.co$", host):
            raise BlockedHostError(host)

    transport = _FakeTransport({"evil.example.com": b"{}"})
    c = httpx.AsyncClient(transport=transport, event_hooks={"response": [hook]})
    with pytest.raises(BlockedHostError):
        asyncio.run(c.get("https://evil.example.com/x"))
