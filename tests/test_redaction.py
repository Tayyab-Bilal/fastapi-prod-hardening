import io
import logging

from fastapi_prod_hardening.redaction import SecretScrubFormatter, install_redaction


def setup(secrets=()):
    stream = io.StringIO()
    root = logging.getLogger("redaction-test-root")
    root.handlers.clear()
    root.propagate = False
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    install_redaction(root, secrets=secrets)
    return root, stream


def test_dsn_password_scrubbed():
    f = SecretScrubFormatter("%(message)s")
    rec = logging.LogRecord("x", logging.INFO, "", 0, "connect postgresql://app:hunter2@db:5432/acme",
                            None, None)
    out = f.format(rec)
    assert "hunter2" not in out and "postgresql://app:***@db:5432/acme" in out


def test_child_logger_record_redacted_at_handler():
    root, stream = setup(secrets=["my-secret-value"])
    logging.getLogger("redaction-test-root.child.deep").info("dsn postgresql://u:pw123@h/db "
                                                              "x=my-secret-value")
    out = stream.getvalue()
    assert "pw123" not in out and "my-secret-value" not in out and "***" in out


def test_logger_level_filter_misses_propagated_record():
    stream = io.StringIO()
    root = logging.getLogger("filter-demo-root")
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(logging.StreamHandler(stream))
    seen = []
    root.addFilter(lambda record: seen.append(record) or True)  # the "obvious" fix
    logging.getLogger("filter-demo-root.child").info("password=postgresql://u:leaky@h/db")
    assert seen == [] and "leaky" in stream.getvalue()  # filter never ran, secret got through


def test_api_key_and_bearer_scrubbed():
    root, stream = setup()
    root.info("key sk-abcdefghijklmnop1234 hdr Authorization: Bearer eyJhbGciOi.abc.def")
    out = stream.getvalue()
    assert "sk-abcdefghijklmnop1234" not in out and "eyJhbGciOi" not in out
    assert "sk-***" in out and "Bearer ***" in out


def test_scrubbing_twice_equals_scrubbing_once():
    f = SecretScrubFormatter("%(message)s", secrets=["s3cret"])
    text = "postgresql://u:pw@h/db Bearer abc.def sk-abcdefghijklmnopqrstuv s3cret"
    assert f.scrub(f.scrub(text)) == f.scrub(text)


def test_reinstall_after_framework_replaces_handlers():
    root, stream = setup(secrets=["tok-123"])
    root.handlers.clear()  # a framework (a task runner) hijacks the root logger
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.info("leak tok-123")
    assert "tok-123" in stream.getvalue()  # the new handler is unprotected...
    install_redaction(root, secrets=["tok-123"])
    root.info("leak tok-123")
    assert stream.getvalue().count("tok-123") == 1  # ...until we reinstall
