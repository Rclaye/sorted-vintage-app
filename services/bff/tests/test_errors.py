def test_unhandled_exception_uses_error_body(client):
    response = client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_error",
        "message": "Something went wrong on our side.",
    }
