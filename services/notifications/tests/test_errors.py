from tests.test_auth import bearer


def test_unhandled_exception_uses_error_body(client, make_token):
    response = client.get("/boom", headers=bearer(make_token()))
    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_error",
        "message": "Something went wrong on our side.",
    }
