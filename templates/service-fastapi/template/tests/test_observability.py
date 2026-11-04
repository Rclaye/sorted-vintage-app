import logging

from opentelemetry.trace import SpanKind

from tests.test_auth import bearer

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_SPAN = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN}-01"


def test_incoming_traceparent_is_continued(client, make_token, spans):
    client.get("/ping", headers={**bearer(make_token()), "traceparent": TRACEPARENT})
    servers = [s for s in spans.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert servers, "expected a server span"
    server = servers[0]
    assert format(server.context.trace_id, "032x") == TRACE_ID
    assert format(server.parent.span_id, "016x") == PARENT_SPAN


def test_json_log_carries_required_fields_and_trace_ids(client, make_token, log_lines):
    client.get("/ping", headers={**bearer(make_token()), "traceparent": TRACEPARENT})
    lines = log_lines()
    request_log = next(line for line in lines if line["message"] == "request")
    for field in ("timestamp", "level", "service", "env", "trace_id", "span_id", "message"):
        assert field in request_log
    assert request_log["env"] == "test"
    assert request_log["trace_id"] == TRACE_ID
    assert len(request_log["span_id"]) == 16
    assert request_log["route"] == "/ping"
    assert request_log["status"] == 200


def test_logs_outside_a_request_have_empty_trace_ids(client, log_lines):
    logging.getLogger("x").info("hello")
    line = log_lines()[-1]
    assert line["trace_id"] == ""
    assert line["message"] == "hello"
