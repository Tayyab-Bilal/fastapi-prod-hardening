import logging

import pytest

from fastapi_prod_hardening.transcripts import PayloadTooLarge, TranscriptStore, log_task_start


class Clock:
    now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path):
    return TranscriptStore(str(tmp_path / "t.db"), clock=Clock())


def test_take_returns_body_and_deletes_it(store):
    store.put("job-1", "long meeting text")
    assert store.take("job-1") == "long meeting text"
    assert store.count() == 0 and store.take("job-1") is None


def test_task_message_carries_only_the_job_id(store):
    store.put("job-2", "secret transcript")
    message = {"job_id": "job-2"}  # what goes through the broker
    assert "secret transcript" not in str(message)
    assert store.take(message["job_id"]) == "secret transcript"


def test_sweep_removes_only_rows_older_than_24h(tmp_path):
    clock = Clock()
    store = TranscriptStore(str(tmp_path / "t.db"), clock=clock)
    store.put("old", "x")
    clock.now += 25 * 3600
    store.put("fresh", "y")
    assert store.sweep() == 1
    assert store.take("old") is None and store.take("fresh") == "y"


def test_payload_over_limit_is_rejected(tmp_path):
    store = TranscriptStore(str(tmp_path / "t.db"), max_bytes=10)
    store.put("ok", "x" * 10)
    with pytest.raises(PayloadTooLarge):
        store.put("big", "x" * 11)
    assert store.count() == 1


def test_default_limit_is_50_mb():
    from fastapi_prod_hardening.transcripts import MAX_BYTES
    assert MAX_BYTES == 50 * 1024 * 1024


def test_task_start_log_prints_argument_names_only(caplog):
    def summarise(job_id, transcript, language="en"):
        ...

    with caplog.at_level(logging.INFO):
        line = log_task_start(logging.getLogger("t"), summarise, "job-9", "TOP SECRET", language="fr")
    assert "job_id, transcript, language" in line
    assert "TOP SECRET" not in caplog.text and "job-9" not in caplog.text and "fr" not in line.split(":")[1]
