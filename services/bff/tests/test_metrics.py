def metrics_text(client) -> str:
    return client.get("/metrics").text


def test_metrics_exposes_duration_histogram_by_route_and_status(client):
    client.get("/ping")
    text = metrics_text(client)
    assert "# TYPE http_server_request_duration_seconds histogram" in text
    assert 'route="/ping"' in text
    assert 'status="200"' in text


def test_redirects_are_counted_with_their_route(client):
    client.get("/login", follow_redirects=False)  # 303 to Keycloak
    assert 'route="/login",status="303"' in metrics_text(client).replace('method="GET",', "")


def test_unknown_paths_do_not_create_new_label_values(client):
    client.get("/random/123")
    text = metrics_text(client)
    assert "/random/123" not in text
    assert 'route="unmatched"' in text


def test_server_errors_are_counted(client):
    client.get("/boom")
    assert 'status="500"' in metrics_text(client)
