"""Allow-listed httpx async client (FR-017 ... FR-023).

Default for any newly registered agent = "no internet access" (FR-019). Agents whose
InternetProfile.kind == "none" should NOT receive a client at all (callers pass None).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional

import httpx

from ..blackboard.schema import BlockedHostError


def _host_matches(host: str, patterns: Iterable[str]) -> bool:
    for p in patterns:
        try:
            if re.match(p, host):
                return True
        except re.error:
            continue
    return False


def make_allowlisted_client(
    profile,  # InternetProfile
    *,
    logger: Optional[Any] = None,
    timeout: float = 10.0,
) -> Optional[httpx.AsyncClient]:
    """Return an httpx.AsyncClient that enforces the InternetProfile allow-list.

    Returns None if profile.kind == "none" (caller should treat that as "no client").
    """
    if profile.kind == "none":
        return None

    allowed_hosts: List[str] = list(profile.allowed_hosts or [])
    if profile.kind == "llm_only" and profile.llm_endpoint:
        host = profile.llm_endpoint.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        if host and host not in allowed_hosts:
            allowed_hosts.append(f"^{re.escape(host)}$")

    def _check_request(request: httpx.Request) -> None:
        host = request.url.host or ""
        if not _host_matches(host, allowed_hosts):
            if logger is not None:
                logger.warning("blocked_host", host=host, url=str(request.url))
            raise BlockedHostError(f"host not in allow-list: {host}")

    async def _check_response(response: httpx.Response) -> None:
        host = response.request.url.host or ""
        if not _host_matches(host, allowed_hosts):
            if logger is not None:
                logger.warning("blocked_host", host=host, url=str(response.request.url))
            raise BlockedHostError(f"host not in allow-list: {host}")

    client = httpx.AsyncClient(timeout=timeout, event_hooks={"request": [_check_request], "response": [_check_response]})
    return client
