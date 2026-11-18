"""T083: order_id on spans, on log lines and in W3C baggage, wherever an order is in scope."""

import logging

from opentelemetry import baggage, propagate

from app.telemetry import ORDER_ID, current_tags, order_scope, tag_order, tag_scope

ORDER = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def add_tagging_route(client):
    log = logging.getLogger("test.order")

    @client.app.get("/tagged/{order_id}")
    def tagged(order_id: str) -> dict:
        # A sync route: it runs in a worker thread, in a copy of the request's context.
        tag_order(order_id)
        log.info("working on it")
        return {"ok": True}

    @client.app.get("/untagged")
    async def untagged() -> dict:
        log.info("nothing to tag")
        return {"ok": True}


def test_a_tagged_request_carries_order_id_on_its_span_and_every_log_line(
    client, spans, log_lines, make_token
):
    add_tagging_route(client)
    headers = {"Authorization": f"Bearer {make_token()}"}

    assert client.get(f"/tagged/{ORDER}", headers=headers).status_code == 200

    lines = log_lines()
    work = next(line for line in lines if line["message"] == "working on it")
    access = next(line for line in lines if line["message"] == "request")
    assert work[ORDER_ID] == ORDER
    assert access[ORDER_ID] == ORDER  # the access line is written outside the route's thread
    server = [s for s in spans.get_finished_spans() if s.attributes.get(ORDER_ID)]
    assert server and server[0].attributes[ORDER_ID] == ORDER


def test_the_next_request_starts_untagged(client, log_lines, make_token):
    add_tagging_route(client)
    headers = {"Authorization": f"Bearer {make_token()}"}
    client.get(f"/tagged/{ORDER}", headers=headers)
    client.get("/untagged", headers=headers)

    line = next(line for line in log_lines() if line["message"] == "nothing to tag")
    assert ORDER_ID not in line


def test_an_order_id_arriving_in_baggage_tags_the_request(client, spans, log_lines, make_token):
    add_tagging_route(client)
    headers = {
        "Authorization": f"Bearer {make_token()}",
        "baggage": f"{ORDER_ID}={ORDER}",
    }
    assert client.get("/untagged", headers=headers).status_code == 200

    line = next(line for line in log_lines() if line["message"] == "nothing to tag")
    assert line[ORDER_ID] == ORDER
    assert any(s.attributes.get(ORDER_ID) == ORDER for s in spans.get_finished_spans())


def test_outgoing_calls_carry_the_order_in_baggage():
    with tag_scope():
        tag_order(ORDER)
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
    assert f"{ORDER_ID}={ORDER}" in carrier["baggage"]


def test_order_scope_leaves_nothing_behind():
    with order_scope(ORDER):
        assert current_tags() == {ORDER_ID: ORDER}
        assert baggage.get_baggage(ORDER_ID) == ORDER
    with order_scope():
        assert current_tags() == {}
        assert baggage.get_baggage(ORDER_ID) is None
        tag_order(OTHER)  # learnt part-way through, as a worker does
        assert current_tags() == {ORDER_ID: OTHER}
    assert current_tags() == {}
    assert baggage.get_baggage(ORDER_ID) is None


def test_nothing_to_tag_is_ignored():
    with tag_scope():
        tag_order(None)
        tag_order("")
        assert current_tags() == {}


def test_baggage_that_is_not_an_order_id_is_ignored(client, log_lines, make_token):
    add_tagging_route(client)
    headers = {
        "Authorization": f"Bearer {make_token()}",
        "baggage": f"{ORDER_ID}=%3Cscript%3E,other=1",
    }
    assert client.get("/untagged", headers=headers).status_code == 200
    line = next(line for line in log_lines() if line["message"] == "nothing to tag")
    assert ORDER_ID not in line
