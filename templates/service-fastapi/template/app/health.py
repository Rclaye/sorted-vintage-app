"""Dependency checks behind GET /healthz."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

Check = Callable[[], Awaitable[None]]  # raises if the dependency is unreachable


def tcp_check(url: str, default_port: int, timeout: float = 1.0) -> Check:
    """Reachable means a TCP connection to the URL's host and port succeeds."""
    parsed = urlparse(url)
    host, port = parsed.hostname or "localhost", parsed.port or default_port

    async def check() -> None:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        await writer.wait_closed()

    return check


def checks_from_settings(database_url: str, amqp_url: str) -> dict[str, Check]:
    checks: dict[str, Check] = {}
    if database_url:
        checks["database"] = tcp_check(database_url, 5432)
    if amqp_url:
        checks["queue"] = tcp_check(amqp_url, 5672)
    return checks


async def failing(checks: dict[str, Check]) -> list[str]:
    """Names of the dependencies that are not reachable."""
    names = list(checks)
    results = await asyncio.gather(*(checks[n]() for n in names), return_exceptions=True)
    down = [n for n, r in zip(names, results, strict=True) if isinstance(r, BaseException)]
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            log.warning(
                "dependency unreachable", extra={"dependency": name, "reason": repr(result)}
            )
    return down
