import asyncio
import threading
import time

from fastapi.testclient import TestClient

from fastapi_prod_hardening.app import create_app
from fastapi_prod_hardening.db import Pool
from fastapi_prod_hardening.metrics import Histogram, LoopLagSampler, Metrics


def test_histogram_buckets_are_cumulative():
    h = Histogram("x_seconds", "x", buckets=(0.1, 1))
    for v in (0.05, 0.5, 5):
        h.observe(v)
    text = h.render()
    assert 'x_seconds_bucket{le="0.1"} 1' in text and 'x_seconds_bucket{le="1"} 2' in text
    assert 'x_seconds_bucket{le="+Inf"} 3' in text and "x_seconds_count 3" in text


async def test_loop_lag_sampler_sees_a_blocked_loop():
    sampler = LoopLagSampler(interval_s=0.01)
    sampler.start()
    await asyncio.sleep(0.03)
    time.sleep(0.15)  # a handler blocking the loop
    await asyncio.sleep(0.03)
    await sampler.stop()
    assert sampler.histogram.sum >= 0.1


def test_pool_wait_is_recorded_when_pool_is_exhausted(tmp_path):
    metrics = Metrics()
    pool = Pool(str(tmp_path / "p.db"), size=1, wait_histogram=metrics.pool_wait)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with pool.connection():
            holding.set()
            release.wait()

    t = threading.Thread(target=hold)
    t.start()
    holding.wait()
    threading.Timer(0.1, release.set).start()
    with pool.connection():
        pass
    t.join()
    assert metrics.pool_wait.sum >= 0.08


def test_metrics_endpoint_exposes_both_series():
    with TestClient(create_app()) as c:
        text = c.get("/metrics").text
    assert "event_loop_lag_seconds_count" in text and "db_pool_wait_seconds_count" in text
