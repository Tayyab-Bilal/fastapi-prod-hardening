import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_prod_hardening.cors import (
    DynamicCORS,
    DynamicCORSMiddleware,
    SQLiteOriginSource,
    validate_origin,
)


def app_with(cors: DynamicCORS) -> TestClient:
    app = FastAPI()
    app.add_middleware(DynamicCORSMiddleware, cors=cors)

    @app.get("/x")
    def x():
        return {}

    return TestClient(app)


def make(tmp_path):
    src = SQLiteOriginSource(str(tmp_path / "o.db"))
    src.add("https://app.example.com")
    return src, DynamicCORS(src)


async def test_allowed_origin_echoed(tmp_path):
    _, cors = make(tmp_path)
    await cors.refresh()
    r = app_with(cors).get("/x", headers={"origin": "https://app.example.com"})
    assert r.headers["access-control-allow-origin"] == "https://app.example.com"


async def test_disallowed_origin_not_echoed(tmp_path):
    _, cors = make(tmp_path)
    await cors.refresh()
    c = app_with(cors)
    assert "access-control-allow-origin" not in c.get(
        "/x", headers={"origin": "https://evil.example"}).headers
    pre = c.options("/x", headers={"origin": "https://evil.example",
                                   "access-control-request-method": "GET"})
    assert pre.status_code == 403


async def test_reload_keeps_last_list_on_error(tmp_path):
    src, cors = make(tmp_path)
    await cors.refresh()
    src.load = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    await cors.refresh()
    assert cors.origins == {"https://app.example.com"}


async def test_background_refresh_picks_up_new_origin(tmp_path):
    src, _ = make(tmp_path)
    cors = DynamicCORS(src, interval_s=0.01)
    await cors.start()
    src.add("https://new.example.com")
    await asyncio.sleep(0.1)
    await cors.stop()
    assert "https://new.example.com" in cors.origins


@pytest.mark.parametrize("bad", ["*", "null", "https://x.com/path", "https://user:pw@x.com",
                                 "http://x.com", "ftp://x.com", "https://x.com?a=1"])
def test_admin_rejects_wildcard_null_path_userinfo_http(bad, tmp_path):
    with pytest.raises(ValueError):
        SQLiteOriginSource(str(tmp_path / "o.db")).add(bad)


def test_admin_accepts_valid_origins():
    assert validate_origin("http://localhost:3000") == "http://localhost:3000"
    assert validate_origin("https://app.example.com/") == "https://app.example.com"
