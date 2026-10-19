from pathlib import Path

import yaml

WF = Path(__file__).parent.parent / ".github" / "workflows"
# Example templates live outside .github/workflows so GitHub does not run them.
TPL = Path(__file__).parent.parent / "ci-templates"


def load(name):
    folder = TPL if name.endswith(".example.yml") else WF
    doc = yaml.safe_load((folder / name).read_text())
    assert "jobs" in doc and (True in doc or "on" in doc)  # PyYAML reads the key `on` as True
    return doc


def test_ci_workflow_is_valid():
    jobs = load("ci.yml")["jobs"]
    matrix = jobs["test"]["strategy"]["matrix"]["python-version"]
    assert {"3.11", "3.12"} <= set(matrix)


def test_change_aware_example_is_valid_and_keeps_sandbox():
    doc = load("change-aware-ci.example.yml")
    text = (TPL / "change-aware-ci.example.yml").read_text()
    assert {"changes", "test-cached", "test-full-image"} <= set(doc["jobs"])
    assert "dorny/paths-filter" in text and "ci:no-cache" in text
    assert text.count("--network none") >= 2  # both paths keep the offline test sandbox
    assert "schedule" in (doc.get(True) or doc["on"])


def test_change_aware_example_byte_compiles_and_gates_the_report():
    text = (TPL / "change-aware-ci.example.yml").read_text()
    assert "--compile-bytecode" in text
    assert text.count("check_junit.py") >= 2  # both the cached and the full-image path


def test_db_suites_example_is_strict_about_skips_and_checks_migrations():
    doc = load("db-suites.example.yml")
    text = (TPL / "db-suites.example.yml").read_text()
    assert "postgres" in doc["jobs"]["db-tests"]["services"]
    assert "--strict-skips" in text and "--max-skips 0" in text and "check_migrations.py" in text


def test_own_ci_runs_the_junit_gate():
    assert "check_junit.py report.xml" in (WF / "ci.yml").read_text()
