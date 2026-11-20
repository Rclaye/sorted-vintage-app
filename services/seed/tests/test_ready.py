import httpx
import pytest
import respx

from app.ready import wait_ready
from app.site import SeedError
from tests.test_run import KC, settings


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_waits_until_payments_answers_then_returns():
    clock, lines = Clock(), []
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{KC}/realms/sv-dev").mock(return_value=httpx.Response(200))
        mock.get("http://listings/healthz").mock(return_value=httpx.Response(200))
        mock.get("http://payments/healthz").mock(
            side_effect=[
                httpx.ConnectError("Connection refused"),
                httpx.Response(503),
                httpx.Response(200),
            ]
        )
        with httpx.Client() as http:
            wait_ready(settings(), http, lines.append, clock=clock, sleep=clock.sleep)
    assert "GET payments: not ready (ConnectError)" in lines
    assert "GET payments: not ready (503)" in lines
    assert lines[-1] == "GET payments: ready after 20s"
    assert sum("keycloak: ready" in line for line in lines) == 1


def test_gives_up_at_the_deadline_naming_what_is_missing():
    clock = Clock()
    with respx.mock() as mock:
        mock.get(f"{KC}/realms/sv-dev").mock(return_value=httpx.Response(200))
        mock.get("http://payments/healthz").mock(return_value=httpx.Response(200))
        mock.get("http://listings/healthz").mock(side_effect=httpx.ConnectError("refused"))
        with httpx.Client() as http, pytest.raises(SeedError, match="after 60s: listings$"):
            wait_ready(
                settings(), http, lambda _: None, deadline=60, clock=clock, sleep=clock.sleep
            )
