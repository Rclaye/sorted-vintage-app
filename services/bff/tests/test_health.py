async def _down() -> None:
    raise ConnectionRefusedError("nope")


def test_healthz_ok_when_dependencies_reachable(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_needs_no_token(client):
    assert client.get("/healthz").status_code == 200


def test_healthz_503_when_database_down(client, checks):
    checks["database"] = _down
    response = client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "dependency_unavailable"
    assert "database" in body["message"]


def test_healthz_503_when_queue_down(client, checks):
    checks["queue"] = _down
    response = client.get("/healthz")
    assert response.status_code == 503
    assert "queue" in response.json()["message"]
