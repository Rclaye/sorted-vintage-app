"""Wait for Keycloak, Payments and Listings before seeding.

The seed runs as a PostSync hook, which can start while those services are still coming up (the
rebuild drill, 2026-11-19: Payments refused the connection and the seed failed, so the verify
users kept their old passwords). Each dependency must answer 200 before the run starts; until then
the seed waits and logs what it is still waiting for, up to a deadline below the Job's 900s.
"""

import time
from collections.abc import Callable

import httpx

from app.config import Settings
from app.site import Log, SeedError


def checks(settings: Settings) -> dict[str, str]:
    return {
        "keycloak": f"{settings.keycloak_url}/realms/{settings.realm}",
        "payments": f"{settings.payments_url}/healthz",
        "listings": f"{settings.listings_url}/healthz",
    }


def answer(http: httpx.Client, url: str) -> str:
    try:
        r = http.get(url, timeout=5)
    except httpx.HTTPError as exc:
        return type(exc).__name__
    return "" if r.status_code == 200 else str(r.status_code)


def wait_ready(
    settings: Settings,
    http: httpx.Client,
    log: Log,
    deadline: float = 600,
    every: float = 10,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    pending = checks(settings)
    start = clock()
    while True:
        for name, url in list(pending.items()):
            problem = answer(http, url)
            if not problem:
                log(f"GET {name}: ready after {int(clock() - start)}s")
                del pending[name]
            else:
                log(f"GET {name}: not ready ({problem})")
        if not pending:
            return
        if clock() - start >= deadline:
            raise SeedError(f"not ready after {int(deadline)}s: {', '.join(pending)}")
        sleep(every)
