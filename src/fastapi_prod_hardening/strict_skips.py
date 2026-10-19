"""pytest plugin: ``--strict-skips`` fails the run if any test was skipped.

For suites that need a real service (a database): a missing service must be a red build, not a
green one that quietly ran nothing. Enable with ``-p fastapi_prod_hardening.strict_skips``.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--strict-skips", action="store_true", help="fail if any test is skipped")


def pytest_configure(config: pytest.Config) -> None:
    config._skipped_ids = []  # type: ignore[attr-defined]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    # xfail also reports "skipped"; only real skips count.
    if report.skipped and not hasattr(report, "wasxfail") and report.when in ("setup", "call"):
        item.config._skipped_ids.append(item.nodeid)  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    skipped = session.config._skipped_ids  # type: ignore[attr-defined]
    if session.config.getoption("--strict-skips") and skipped and exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, config: pytest.Config) -> None:
    if config.getoption("--strict-skips") and config._skipped_ids:  # type: ignore[attr-defined]
        terminalreporter.write_line(
            f"--strict-skips: {len(config._skipped_ids)} skipped test(s) fail the run", red=True)
