from tests.test_auth import bearer


def metrics_text(client) -> str:
    return client.get("/metrics").text


def test_metrics_exposes_duration_histogram_by_route_and_status(client, make_token):
    client.get("/ping", headers=bearer(make_token()))
    text = metrics_text(client)
    assert "# TYPE http_server_request_duration_seconds histogram" in text
    assert 'route="/ping"' in text
    assert 'status="200"' in text


def test_rejected_requests_are_counted_with_their_route(client):
    client.get("/ping")  # no token
    assert 'route="/ping",status="401"' in metrics_text(client).replace('method="GET",', "")


def test_unknown_paths_do_not_create_new_label_values(client, make_token):
    client.get("/random/123", headers=bearer(make_token()))
    text = metrics_text(client)
    assert "/random/123" not in text
    assert 'route="unmatched"' in text


def test_server_errors_are_counted(client, make_token):
    client.get("/boom", headers=bearer(make_token()))
    assert 'status="500"' in metrics_text(client)
