from fastapi.testclient import TestClient

from fastapi_prod_hardening.app import create_app


def test_app_end_to_end(token):
    with TestClient(create_app(secret="test-secret")) as c:
        assert c.get("/health").json() == {"ok": True}
        r = c.post("/v1/ask", headers={"authorization": f"Bearer {token}"})
        assert r.json() == {"answer": "hello world"}  # primary failed once, backup answered
