from pathlib import Path

import pytest
from tests_helpers import load_script

junit = load_script("check_junit")
migrations = load_script("check_migrations")
FIXTURES = Path(__file__).parent / "fixtures"


def report(tmp_path, tests, skipped=0, failures=0):
    path = tmp_path / "r.xml"
    path.write_text(f'<testsuites><testsuite tests="{tests}" skipped="{skipped}" '
                    f'failures="{failures}" errors="0"/></testsuites>')
    return str(path)


def test_missing_junit_report_fails(tmp_path):
    assert "not found" in junit.check(str(tmp_path / "nope.xml"))[0]


def test_empty_junit_report_fails(tmp_path):
    assert "zero tests" in junit.check(report(tmp_path, 0))[0]


def test_all_skipped_junit_report_fails(tmp_path):
    assert "skipped" in junit.check(report(tmp_path, 5, skipped=5))[0]


def test_garbage_junit_report_fails(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("not xml")
    assert "not valid XML" in junit.check(str(bad))[0]


def test_good_junit_report_passes_and_max_skips_is_enforced(tmp_path):
    assert junit.check(report(tmp_path, 5, skipped=1)) == []
    assert junit.check(report(tmp_path, 5, skipped=1), max_skips=0)
    assert junit.main([report(tmp_path, 5, failures=1)]) == 1


def test_strict_skips_fails_the_run_when_a_test_is_skipped(pytester):
    pytester.makepyfile("import pytest\n\ndef test_a():\n    pass\n\n"
                        "@pytest.mark.skip\ndef test_b():\n    pass\n")
    args = ["-p", "fastapi_prod_hardening.strict_skips"]
    assert pytester.runpytest_inprocess(*args).ret == 0  # off by default
    assert pytester.runpytest_inprocess(*args, "--strict-skips").ret == 1


def test_strict_skips_ignores_xfail_and_passes_clean_runs(pytester):
    pytester.makepyfile("import pytest\n\ndef test_a():\n    pass\n\n"
                        "@pytest.mark.xfail\ndef test_b():\n    assert 0\n")
    result = pytester.runpytest_inprocess("-p", "fastapi_prod_hardening.strict_skips", "--strict-skips")
    assert result.ret == 0


@pytest.mark.parametrize("folder", ["ok"])
def test_valid_migration_folder_passes(folder):
    assert migrations.check(str(FIXTURES / folder)) == []


@pytest.mark.parametrize("folder,expected", [
    ("dup", "duplicate revision id 'b2'"),
    ("missing", "missing parent 'zz'"),
    ("cycle", "cycle through revision"),
    ("multihead", "multiple heads: b2, c3"),
])
def test_bad_migration_folder_is_reported(folder, expected):
    assert any(expected in p for p in migrations.check(str(FIXTURES / folder)))


def test_migration_check_does_not_import_the_files(tmp_path):
    (tmp_path / "001.py").write_text('import definitely_not_installed\nrevision = "a"\ndown_revision = None\n')
    assert migrations.check(str(tmp_path)) == []
    assert migrations.main([str(FIXTURES / "dup")]) == 1
