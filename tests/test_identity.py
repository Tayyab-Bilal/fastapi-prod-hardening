import logging

import pytest
from fastapi.testclient import TestClient

from fastapi_prod_hardening.app import create_app
from fastapi_prod_hardening.identity import Hs256Verifier, Identity, InvalidToken


def auth(token):
    return {"authorization": f"Bearer {token}"}


def test_identity_from_token_not_body(token):
    c = TestClient(create_app(secret="test-secret", mode="measure"))
    r = c.post("/v1/tasks", json={"title": "x", "tenant_id": "someone-else"}, headers=auth(token))
    assert r.json()["tenant_id"] == "t1"


def test_measure_mode_logs_mismatch_and_allows(token, caplog):
    c = TestClient(create_app(secret="test-secret", mode="measure"))
    with caplog.at_level(logging.WARNING, logger="identity_check"):
        r = c.post("/v1/tasks", json={"title": "x", "tenant_id": "evil"}, headers=auth(token))
    assert r.status_code == 200
    assert "identity_mismatch" in caplog.text and "tenant_id" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="identity_check"):
        c.post("/v1/tasks", json={"title": "x", "tenant_id": "t1"}, headers=auth(token))
    assert caplog.text == ""  # matching ids are silent


def test_enforce_mode_rejects_mismatch(token):
    c = TestClient(create_app(secret="test-secret", mode="enforce"))
    assert c.post("/v1/tasks", json={"title": "x", "tenant_id": "evil"},
                  headers=auth(token)).status_code == 403
    assert c.get("/v1/whoami?user_id=evil", headers=auth(token)).status_code == 403
    assert c.post("/v1/tasks", json={"title": "x", "tenant_id": "t1"},
                  headers=auth(token)).status_code == 200


def test_tampered_token_rejected(token, verifier):
    header, payload, sig = token.split(".")
    forged = Hs256Verifier("test-secret")
    other = forged.issue(Identity("t2", "admin")).split(".")[1]
    for bad in (f"{header}.{other}.{sig}", token + "x", "garbage"):
        with pytest.raises(InvalidToken):
            verifier.verify(bad)
    c = TestClient(create_app(secret="test-secret"))
    assert c.get("/v1/whoami", headers=auth(f"{header}.{other}.{sig}")).status_code == 401
    assert c.get("/v1/whoami", headers=auth(token)).json() == {"tenant_id": "t1", "user_id": "u1"}
